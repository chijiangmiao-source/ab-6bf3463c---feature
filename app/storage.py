"""复核记录与求解请求的 SQLite 持久化。

两张表：

- ``submissions``：已完成复核的记录（复核编号 -> 提交内容与结论）。
  只有求解正常结束且赢得终态裁决的请求才会写入；取消的请求不产生
  复核编号。
- ``requests``：每次合法提交对应的求解请求。提交被接受（输入合法）
  时先冻结规范化后的排序通道与校验、计算输入摘要并持久化为
  ``pending``；随后状态机为
  ``pending -> running -> done | cancelled | failed``。
  完成与取消竞争时，双方都以“条件 UPDATE 是否命中”这一持久化
  裁决收敛到唯一终态：只有命中 ``done`` 转移的一方才允许写入
  复核记录。

输入非法的请求在接口层直接拒绝，不产生任何记录（旧证据由前端清除）。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid

DB_PATH = os.environ.get("APP_DB", "/data/locator.db")

_lock = threading.Lock()

# 请求状态机
STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_CANCELLED = "cancelled"
STATUS_FAILED = "failed"
ACTIVE_STATUSES = (STATUS_PENDING, STATUS_RUNNING)


def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _lock, _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS submissions (
                review_id   TEXT PRIMARY KEY,
                created_at  TEXT NOT NULL DEFAULT (datetime('now')),
                payload     TEXT NOT NULL,
                conclusion  TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS requests (
                request_id       TEXT PRIMARY KEY,
                input_digest     TEXT NOT NULL,
                payload          TEXT NOT NULL,
                status           TEXT NOT NULL DEFAULT 'pending',
                progress_phase   TEXT,
                progress_percent INTEGER,
                review_id        TEXT,
                created_at       TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )


# ---------------------------------------------------------------------------
# 复核记录（已完成结论）
# ---------------------------------------------------------------------------

def save_submission(payload: dict, conclusion: dict) -> str:
    review_id = uuid.uuid4().hex[:12]
    with _lock, _connect() as conn:
        # 极小概率撞号时重试一次。
        while True:
            try:
                conn.execute(
                    "INSERT INTO submissions (review_id, payload, conclusion) "
                    "VALUES (?, ?, ?)",
                    (
                        review_id,
                        json.dumps(payload, ensure_ascii=False),
                        json.dumps(conclusion, ensure_ascii=False),
                    ),
                )
                break
            except sqlite3.IntegrityError:
                review_id = uuid.uuid4().hex[:12]
    return review_id


def load_submission(review_id: str) -> dict | None:
    if not review_id or not all(c in "0123456789abcdef" for c in review_id):
        return None
    with _connect() as conn:
        row = conn.execute(
            "SELECT review_id, created_at, payload, conclusion "
            "FROM submissions WHERE review_id = ?",
            (review_id,),
        ).fetchone()
    if row is None:
        return None
    return {
        "review_id": row["review_id"],
        "created_at": row["created_at"],
        "input": json.loads(row["payload"]),
        "conclusion": json.loads(row["conclusion"]),
    }


# ---------------------------------------------------------------------------
# 求解请求（幂等提交 + 状态机 + 终态裁决）
# ---------------------------------------------------------------------------

def _row_to_request(row: sqlite3.Row) -> dict:
    return {
        "request_id": row["request_id"],
        "input_digest": row["input_digest"],
        "input": json.loads(row["payload"]),
        "status": row["status"],
        "progress": {
            "phase": row["progress_phase"],
            "percent": row["progress_percent"],
        },
        "review_id": row["review_id"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def create_request(request_id: str, digest: str, payload: dict) -> bool:
    """登记新请求（冻结的输入 + 摘要）。返回是否真正新建。

    相同 request_id 已存在时不覆盖：调用方据此区分“相同输入的
    重传”（返回既有状态）与“复用标识但改变输入”（拒绝）。
    """
    with _lock, _connect() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO requests (request_id, input_digest, payload) "
            "VALUES (?, ?, ?)",
            (request_id, digest, json.dumps(payload, ensure_ascii=False)),
        )
        return cur.rowcount == 1


def get_request(request_id: str) -> dict | None:
    if not request_id:
        return None
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM requests WHERE request_id = ?", (request_id,)
        ).fetchone()
    if row is None:
        return None
    return _row_to_request(row)


def mark_running(request_id: str) -> bool:
    """pending -> running；若请求已被取消（或不存在）则返回 False。"""
    with _lock, _connect() as conn:
        cur = conn.execute(
            "UPDATE requests SET status = ?, updated_at = datetime('now') "
            "WHERE request_id = ? AND status = ?",
            (STATUS_RUNNING, request_id, STATUS_PENDING),
        )
        return cur.rowcount == 1


def update_progress(request_id: str, phase: str, percent: int) -> None:
    """持久化计算进度；仅对仍处于活动态的请求生效。"""
    with _lock, _connect() as conn:
        conn.execute(
            "UPDATE requests SET progress_phase = ?, progress_percent = ?, "
            "updated_at = datetime('now') "
            "WHERE request_id = ? AND status IN (?, ?)",
            (phase, int(percent), request_id, STATUS_PENDING, STATUS_RUNNING),
        )


def cancel_request(request_id: str) -> dict | None:
    """请求取消：仅当仍处于活动态时转为 cancelled（持久化裁决）。

    已完成/已取消/已失败的请求保持原终态；返回裁决后的请求行。
    """
    with _lock, _connect() as conn:
        conn.execute(
            "UPDATE requests SET status = ?, updated_at = datetime('now') "
            "WHERE request_id = ? AND status IN (?, ?)",
            (STATUS_CANCELLED, request_id, STATUS_PENDING, STATUS_RUNNING),
        )
    return get_request(request_id)


def finalize_cancelled(request_id: str) -> bool:
    """求解线程在检查点观察到取消后落库；与并发完成竞争时只一方命中。"""
    with _lock, _connect() as conn:
        cur = conn.execute(
            "UPDATE requests SET status = ?, updated_at = datetime('now') "
            "WHERE request_id = ? AND status IN (?, ?)",
            (STATUS_CANCELLED, request_id, STATUS_PENDING, STATUS_RUNNING),
        )
        return cur.rowcount == 1


def finalize_failed(request_id: str) -> bool:
    with _lock, _connect() as conn:
        cur = conn.execute(
            "UPDATE requests SET status = ?, updated_at = datetime('now') "
            "WHERE request_id = ? AND status IN (?, ?)",
            (STATUS_FAILED, request_id, STATUS_PENDING, STATUS_RUNNING),
        )
        return cur.rowcount == 1


def finalize_done(request_id: str, payload: dict, conclusion: dict) -> str | None:
    """完成裁决：仅当请求仍处于活动态时，在同一事务内

    1. 把请求转为 done 并绑定复核编号；
    2. 写入 submissions 复核记录。

    若取消已先命中（状态不再是活动态），返回 None——结果与复核
    记录一并丢弃，取消的请求绝不会生成复核编号。
    """
    with _lock, _connect() as conn:
        review_id = uuid.uuid4().hex[:12]
        cur = conn.execute(
            "UPDATE requests SET status = ?, review_id = ?, "
            "updated_at = datetime('now') "
            "WHERE request_id = ? AND status IN (?, ?)",
            (STATUS_DONE, review_id, request_id, STATUS_PENDING, STATUS_RUNNING),
        )
        if cur.rowcount != 1:
            return None
        # 已赢得终态裁决；同事务写入复核记录（撞号时换号重试）。
        while True:
            try:
                conn.execute(
                    "INSERT INTO submissions (review_id, payload, conclusion) "
                    "VALUES (?, ?, ?)",
                    (
                        review_id,
                        json.dumps(payload, ensure_ascii=False),
                        json.dumps(conclusion, ensure_ascii=False),
                    ),
                )
                break
            except sqlite3.IntegrityError:
                review_id = uuid.uuid4().hex[:12]
        conn.execute(
            "UPDATE requests SET review_id = ? WHERE request_id = ?",
            (review_id, request_id),
        )
        return review_id
