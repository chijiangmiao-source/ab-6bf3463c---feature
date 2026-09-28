"""复核请求的 SQLite 持久化与状态机裁决。

每个被接受的提交（输入合法）获得独立请求标识 request_id 与输入摘要
input_digest，并在开始求解前冻结排序通道与校验，持久化以下状态：

    computing  计算中（持续回写检查点进度）
    cancelled  已取消（终态；永不生成复核编号）
    completed  已完成（终态；可行/不可行结论均保存，分配复核编号）
    rejected   复用同一 request_id 却提交了不同输入（终态；不落编号）

裁决规则（全部在单条 SQLite 事务内完成）
----------------------------------------
- 相同 request_id + 相同 input_digest 的网络重传：返回既有请求状态，
  不重复求解（重传幂等）。
- 相同 request_id + 不同 input_digest：定位拒绝（409），请求行保持
  原状态，另写一条 rejected 审计记录，不覆盖任何证据。
- 完成与取消竞争：只允许 computing -> completed/cancelled 的 CAS
  转移；先提交事务者赢，后者看到状态已变即放弃，故两个终态只会
  收敛为其中一个。

completed 行同时是刷新后可凭复核编号取回的复核记录（review_id 与
request_id 一一对应）。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import uuid

DB_PATH = os.environ.get("APP_DB", "/data/locator.db")

# 请求状态
COMPUTING = "computing"
CANCELLED = "cancelled"
COMPLETED = "completed"
REJECTED = "rejected"

_lock = threading.Lock()

ID_ALPHABET = "0123456789abcdef"


def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    with _lock, _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS requests (
                request_id   TEXT PRIMARY KEY,
                input_digest TEXT NOT NULL,
                status       TEXT NOT NULL,
                created_at   TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at   TEXT NOT NULL DEFAULT (datetime('now')),
                payload      TEXT NOT NULL,
                phase        TEXT NOT NULL DEFAULT '',
                progress_done INTEGER NOT NULL DEFAULT 0,
                progress_total INTEGER NOT NULL DEFAULT 0,
                cancel_phase TEXT NOT NULL DEFAULT '',
                conclusion   TEXT NOT NULL DEFAULT '',
                review_id    TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_requests_review "
            "ON requests(review_id) WHERE review_id <> ''"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS rejected_replays (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id      TEXT NOT NULL,
                stored_digest   TEXT NOT NULL,
                replay_digest   TEXT NOT NULL,
                created_at      TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        # 迁移上一版同步提交留下的复核记录：它们已是 completed 终态，
        # request_id 直接沿用原 review_id，保证既有记录仍可读取。
        old = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='submissions'"
        ).fetchone()
        if old is not None:
            old_rows = conn.execute(
                "SELECT review_id, created_at, payload, conclusion "
                "FROM submissions"
            ).fetchall()
            for r in old_rows:
                payload = json.loads(r["payload"])
                conn.execute(
                    "INSERT OR IGNORE INTO requests (request_id, input_digest, "
                    "status, created_at, updated_at, payload, conclusion, review_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        r["review_id"], digest_input(payload), COMPLETED,
                        r["created_at"], r["created_at"], r["payload"],
                        r["conclusion"], r["review_id"],
                    ),
                )
            conn.execute("DROP TABLE submissions")
        # 崩溃/重启恢复：计算随旧进程终止，遗留的 computing 请求
        # 不可能再到达 completed；在启动时据持久化裁决收敛为 cancelled
        # 终态（不生成复核编号）。相同标识+相同输入的重传仍会取得这一
        # 确定状态，而不是永久看到“计算中”。
        conn.execute(
            "UPDATE requests SET status = ?, cancel_phase = 'interrupted', "
            "updated_at = datetime('now') WHERE status = ?",
            (CANCELLED, COMPUTING),
        )
        conn.execute("COMMIT")


def new_request_id() -> str:
    return uuid.uuid4().hex[:16]


def digest_input(payload: dict) -> str:
    """对冻结后的规范化输入计算稳定摘要（SHA-256 十六进制）。"""
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def valid_id(value) -> bool:
    return (
        isinstance(value, str)
        and 8 <= len(value) <= 64
        and all(c in ID_ALPHABET for c in value)
    )


def create_request(request_id: str, payload: dict) -> dict:
    """持久化一个 computing 请求；处理幂等重传与标识冲突。

    返回 {"outcome": "created" | "replayed" | "conflict", "request": ...}。
    - created：新请求，已冻结为 computing，调用方应启动求解；
    - replayed：同标识同输入重传，返回既有请求（调用方不得再求解）；
    - conflict：同标识不同输入，已落 rejected 审计，调用方应 409。
    """
    digest = digest_input(payload)
    payload_json = json.dumps(payload, ensure_ascii=False)
    with _lock, _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM requests WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO requests "
                "(request_id, input_digest, status, payload) "
                "VALUES (?, ?, ?, ?)",
                (request_id, digest, COMPUTING, payload_json),
            )
            conn.execute("COMMIT")
            return {"outcome": "created",
                    "request": get_request(request_id)}
        if row["input_digest"] != digest:
            conn.execute(
                "INSERT INTO rejected_replays "
                "(request_id, stored_digest, replay_digest) "
                "VALUES (?, ?, ?)",
                (request_id, row["input_digest"], digest),
            )
            conn.execute("COMMIT")
            return {"outcome": "conflict",
                    "request": _row_to_dict(row),
                    "replay_digest": digest}
        conn.execute("COMMIT")
        return {"outcome": "replayed",
                "request": get_request(request_id)}


def get_request(request_id: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM requests WHERE request_id = ?",
            (request_id,),
        ).fetchone()
    return _row_to_dict(row) if row is not None else None


def get_by_review_id(review_id: str) -> dict | None:
    if not valid_id(review_id):
        return None
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM requests WHERE review_id = ? AND status = ?",
            (review_id, COMPLETED),
        ).fetchone()
    return _row_to_dict(row) if row is not None else None


def update_progress(request_id: str, phase: str, done: int, total: int) -> None:
    """检查点进度回写；终态后不再覆盖（求解线程即使迟到也无害）。"""
    with _lock, _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE requests SET phase = ?, progress_done = ?, "
            "progress_total = ?, updated_at = datetime('now') "
            "WHERE request_id = ? AND status = ?",
            (phase, done, total, request_id, COMPUTING),
        )
        conn.execute("COMMIT")


def request_cancel(request_id: str) -> dict | None:
    """把 computing 请求裁决为 cancelled 终态。

    已终态（completed/cancelled）的请求原样返回当前状态，不翻转；
    不存在返回 None。完成与取消竞争时由本事务的 CAS 决定胜负。
    """
    with _lock, _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM requests WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        if row is None:
            conn.execute("COMMIT")
            return None
        if row["status"] == COMPUTING:
            conn.execute(
                "UPDATE requests SET status = ?, updated_at = datetime('now') "
                "WHERE request_id = ? AND status = ?",
                (CANCELLED, request_id, COMPUTING),
            )
        conn.execute("COMMIT")
    return get_request(request_id)


def mark_cancelled_at_checkpoint(request_id: str, phase: str) -> dict:
    """求解线程在检查点看到取消裁决后停止，并补记取消阶段。

    终态通常已由取消请求的 CAS 确定（computing -> cancelled）；
    这里仅在 status=cancelled 且尚未记录阶段时补写 cancel_phase，
    不改变任何终态。若竞争中完成事务先行提交（status=completed），
    则什么都不写，完成结论保持不变。
    """
    with _lock, _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        # 防御性 CAS：理论上 is_cancelled() 为真时状态已是 cancelled。
        conn.execute(
            "UPDATE requests SET status = ?, cancel_phase = ?, "
            "updated_at = datetime('now') "
            "WHERE request_id = ? AND status = ?",
            (CANCELLED, phase, request_id, COMPUTING),
        )
        conn.execute(
            "UPDATE requests SET cancel_phase = ? "
            "WHERE request_id = ? AND status = ? AND cancel_phase = ''",
            (phase, request_id, CANCELLED),
        )
        conn.execute("COMMIT")
    return get_request(request_id)


def complete_request(request_id: str, conclusion: dict) -> dict | None:
    """CAS 收敛为 completed 并落复核编号；已取消则放弃（返回当前行）。"""
    conclusion_json = json.dumps(conclusion, ensure_ascii=False)
    with _lock, _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM requests WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        if row is None:
            conn.execute("COMMIT")
            return None
        if row["status"] != COMPUTING:
            # 取消先赢或已终态：结论必须被丢弃，绝不覆盖页面/复核记录。
            conn.execute("COMMIT")
            return _row_to_dict(row)
        review_id = request_id
        conn.execute(
            "UPDATE requests SET status = ?, conclusion = ?, review_id = ?, "
            "updated_at = datetime('now') "
            "WHERE request_id = ? AND status = ?",
            (COMPLETED, conclusion_json, review_id, request_id, COMPUTING),
        )
        conn.execute("COMMIT")
    return get_request(request_id)


def _row_to_dict(row: sqlite3.Row) -> dict:
    return {
        "request_id": row["request_id"],
        "input_digest": row["input_digest"],
        "status": row["status"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "payload": json.loads(row["payload"]),
        "phase": row["phase"],
        "progress_done": row["progress_done"],
        "progress_total": row["progress_total"],
        "cancel_phase": row["cancel_phase"],
        "conclusion": (
            json.loads(row["conclusion"]) if row["conclusion"] else None
        ),
        "review_id": row["review_id"],
    }
