"""噪声故障定位 Web 接口（仅依赖 Python 标准库）。

路由
----
GET  /                        录入页面
GET  /healthz                 健康检查
POST /api/submit              冻结输入并启动一次异步复核请求
GET  /api/request/<id>        查询请求状态（计算进度 / 已取消 / 已完成）
POST /api/request/<id>/cancel 在确定检查点取消计算
GET  /api/review/<id>         已完成请求按复核编号取回结论与逐校验复算

请求标识（request_id）由客户端在提交时给出（幂等键）；未提供时由
服务分配。同标识同输入的重传取得同一请求状态；同标识不同输入被
定位拒绝（409）。
"""

from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import storage
from solver import (
    SolveCancelled,
    ValidationError,
    prepare,
    recompute,
    solve_frozen,
)

STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_BODY = 1 << 20  # 1 MiB

# 每个检查点开始处的可选停顿，使“大板观测”的取消窗口在验收环境中
# 确定可观测（默认 0；仅在检查点生效，小输入至多停顿一次）。
CHECKPOINT_DELAY = float(os.environ.get("APP_CHECKPOINT_DELAY", "0") or 0)


# ----------------------------------------------------------------------
# 后台求解
# ----------------------------------------------------------------------
def _build_conclusion(frozen, result) -> dict:
    ordered = list(frozen.channels)
    norm_checks = [(tuple(members), parity)
                   for members, parity in frozen.checks]
    recomputed = (
        recompute(ordered, norm_checks, list(result.vector))
        if result.feasible else []
    )
    return {
        "feasible": result.feasible,
        "weight": result.weight,
        "faulty": list(result.faulty),
        "vector": (
            {ch: bit for ch, bit in zip(result.channels, result.vector)}
            if result.feasible else {}
        ),
        "left_size": result.left_size,
        "left_index_size": result.left_index_size,
        "recompute": recomputed,
        "message": (
            f"最小故障通道 {result.weight} 个：{', '.join(result.faulty)}"
            if result.feasible
            else "不存在能同时满足全部异或约束的故障向量（不可行）"
        ),
    }


def _run_request(request_id: str, frozen) -> None:
    """工作线程：检查点响应取消，结束时以持久化 CAS 收敛唯一终态。"""

    def progress(phase: str, done: int, total: int) -> None:
        if CHECKPOINT_DELAY:
            time.sleep(CHECKPOINT_DELAY)
        storage.update_progress(request_id, phase, done, total)

    def is_cancelled() -> bool:
        req = storage.get_request(request_id)
        return req is not None and req["status"] == storage.CANCELLED

    try:
        result = solve_frozen(frozen, is_cancelled=is_cancelled,
                              progress=progress)
    except SolveCancelled as exc:
        # 取消先赢：收敛为 cancelled，不生成复核编号。
        storage.mark_cancelled_at_checkpoint(request_id, exc.phase)
        return
    # 完成与取消竞争：complete_request 内部 CAS，
    # 取消先提交时结论被丢弃，绝不写入复核记录。
    storage.complete_request(request_id, _build_conclusion(frozen, result))


def _start_request(request_id: str, frozen) -> None:
    thread = threading.Thread(
        target=_run_request, args=(request_id, frozen),
        name=f"solve-{request_id}", daemon=True,
    )
    thread.start()


# ----------------------------------------------------------------------
# 请求/响应塑形
# ----------------------------------------------------------------------
def _frozen_payload(frozen) -> dict:
    return {
        "channels": list(frozen.channels),
        "checks": [
            {"channels": list(members), "parity": parity}
            for members, parity in frozen.checks
        ],
    }


def _request_state(req: dict) -> dict:
    state = {
        "request_id": req["request_id"],
        "status": req["status"],
        "input_digest": req["input_digest"],
        "created_at": req["created_at"],
        "updated_at": req["updated_at"],
        "input": req["payload"],
    }
    if req["status"] == storage.COMPUTING:
        state["progress"] = {
            "phase": req["phase"],
            "done": req["progress_done"],
            "total": req["progress_total"],
        }
    if req["status"] == storage.CANCELLED:
        state["cancel_phase"] = req["cancel_phase"]
    if req["status"] == storage.COMPLETED:
        state["review_id"] = req["review_id"]
        state["conclusion"] = req["conclusion"]
    return state


def _review_record(req: dict) -> dict:
    return {
        "review_id": req["review_id"],
        "request_id": req["request_id"],
        "created_at": req["created_at"],
        "input": req["payload"],
        "conclusion": req["conclusion"],
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "PixelLocator/2.0"

    # ---- 工具 ----
    def _send_json(self, status: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_static(self, name: str, content_type: str) -> None:
        path = STATIC_DIR / name
        try:
            body = path.read_bytes()
        except OSError:
            self._send_json(404, {"error": "not found"})
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # 静默默认访问日志
        return

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY:
            self._send_json(400, {
                "error": "请求体缺失或过大",
                "field": "body",
                "errors": [{"field": "body",
                            "message": "需要 JSON 请求体且不超过 1MiB"}],
            })
            return None
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._send_json(400, {
                "error": "JSON 解析失败",
                "field": "body",
                "errors": [{"field": "body",
                            "message": f"JSON 解析失败: {exc}"}],
            })
            return None
        if not isinstance(data, dict):
            self._send_json(400, {
                "error": "请求体必须是对象",
                "field": "body",
                "errors": [{"field": "body",
                            "message": "请求体必须是 JSON 对象"}],
            })
            return None
        return data

    # ---- GET ----
    def do_GET(self) -> None:
        route = urlparse(self.path).path
        if route == "/" or route == "/index.html":
            self._send_static("index.html", "text/html; charset=utf-8")
        elif route == "/static/app.js":
            self._send_static("app.js", "application/javascript; charset=utf-8")
        elif route == "/healthz":
            self._send_json(200, {"status": "ok"})
        elif route.startswith("/api/request/"):
            self._handle_request_get(route)
        elif route.startswith("/api/review/"):
            self._handle_review_get(route)
        else:
            self._send_json(404, {"error": "not found"})

    def _handle_request_get(self, route: str) -> None:
        parts = [p for p in route.split("/") if p]
        # /api/request/<id>
        if len(parts) != 3:
            self._send_json(404, {"error": "not found"})
            return
        req = storage.get_request(parts[2])
        if req is None:
            self._send_json(404, {
                "error": "请求标识不存在",
                "field": "request_id",
                "request_id": parts[2],
            })
            return
        self._send_json(200, _request_state(req))

    def _handle_review_get(self, route: str) -> None:
        review_id = route.rsplit("/", 1)[-1]
        req = storage.get_by_review_id(review_id)
        if req is None:
            # 存在但未完成（计算中/已取消）的请求没有复核编号。
            pending = storage.get_request(review_id)
            detail = {
                "error": "复核编号不存在",
                "field": "review_id",
                "review_id": review_id,
            }
            if pending is not None:
                detail["reason"] = "该请求未完成，无复核记录"
                detail["status"] = pending["status"]
            self._send_json(404, detail)
            return
        self._send_json(200, _review_record(req))

    # ---- POST ----
    def do_POST(self) -> None:
        route = urlparse(self.path).path
        if route == "/api/submit":
            self._handle_submit()
        elif route.startswith("/api/request/") and route.endswith("/cancel"):
            self._handle_cancel(route)
        else:
            self._send_json(404, {"error": "not found"})

    def _handle_submit(self) -> None:
        data = self._read_json_body()
        if data is None:
            return

        request_id = data.get("request_id")
        if request_id is None:
            request_id = storage.new_request_id()
        elif not storage.valid_id(request_id):
            self._send_json(400, {
                "error": "请求标识格式非法",
                "errors": [{
                    "field": "request_id",
                    "message": "request_id 须为 8–64 位十六进制字符串",
                }],
            })
            return

        # 开始求解前完成校验与冻结；非法输入直接拒绝，不留任何请求记录。
        try:
            frozen = prepare(data.get("channels"), data.get("checks"))
        except ValidationError as exc:
            self._send_json(400, {
                "error": "输入校验未通过，未生成复核记录",
                "errors": [{"field": f, "message": msg}
                           for f, msg in exc.errors],
            })
            return

        payload = _frozen_payload(frozen)
        outcome = storage.create_request(request_id, payload)
        if outcome["outcome"] == "conflict":
            # 复用标识却改变输入：定位拒绝，既有请求保持原状态。
            self._send_json(409, {
                "error": "请求标识冲突：同一 request_id 提交了不同输入",
                "field": "request_id",
                "errors": [{
                    "field": "request_id",
                    "message": (
                        "该 request_id 已绑定另一份输入"
                        f"（已存摘要 {outcome['request']['input_digest'][:12]}…，"
                        f"本次摘要 {outcome['replay_digest'][:12]}…）；"
                        "新请求请使用新的 request_id"
                    ),
                }],
                "request_id": request_id,
                "stored_digest": outcome["request"]["input_digest"],
                "replay_digest": outcome["replay_digest"],
            })
            return

        if outcome["outcome"] == "created":
            _start_request(request_id, frozen)
            status = 202
        else:
            # 相同标识相同输入的网络重传：复用既有请求，绝不重复求解。
            status = 200
        self._send_json(status, _request_state(outcome["request"]))

    def _handle_cancel(self, route: str) -> None:
        parts = [p for p in route.split("/") if p]
        # /api/request/<id>/cancel
        if (len(parts) != 4 or parts[0] != "api" or parts[1] != "request"
                or parts[3] != "cancel"):
            self._send_json(404, {"error": "not found"})
            return
        req = storage.request_cancel(parts[2])
        if req is None:
            self._send_json(404, {
                "error": "请求标识不存在",
                "field": "request_id",
                "request_id": parts[2],
            })
            return
        self._send_json(200, _request_state(req))


def main() -> None:
    storage.init_db()
    host = os.environ.get("APP_HOST", "0.0.0.0")
    port = int(os.environ.get("APP_PORT", "8080"))
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"pixel locator listening on {host}:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
