"""噪声故障定位 Web 接口（仅依赖 Python 标准库）。

路由
----
GET  /                     录入页面
GET  /healthz              健康检查
POST /api/submit           提交观测：同步校验并冻结输入，异步求解，
                           返回请求标识（202）；相同标识+相同输入的
                           重传返回同一请求状态，复用标识改变输入返回 409
POST /api/cancel           取消求解：{"request_id": ...}；与完成竞争时
                           按持久化裁决收敛为唯一终态
GET  /api/request/<id>     查询请求状态（pending/running/cancelled/done/failed）
GET  /api/review/<id>      按复核编号取回已完成提交的内容与结论
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import storage
from solver import CancelledError, ValidationError, recompute, solve, validate_input
from storage import init_db, load_submission

STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_BODY = 1 << 20  # 1 MiB

# 客户端可自带请求标识（用于网络重传去重）；缺省时由服务分配。
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")

# 运行中请求的取消事件注册表：取消接口置位后，求解器在确定检查点
# （折半枚举与左右候选合并）观察到并放弃计算。终态一致性由存储层
# 的条件更新裁决，事件只用于尽早停止。
_cancel_events: dict[str, threading.Event] = {}
_cancel_events_lock = threading.Lock()


def _input_digest(payload: dict) -> str:
    """规范化输入的摘要：同一标识的重传据此判定输入是否一致。"""
    canon = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _build_conclusion(result, norm_checks) -> dict:
    """由求解结果构造结论（含逐校验复算）。"""
    ordered = list(result.channels)
    recomputed = (
        recompute(ordered, norm_checks, list(result.vector)) if result.feasible else []
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
            if result.feasible else "不存在能同时满足全部异或约束的故障向量（不可行）"
        ),
    }


def _request_state(row: dict) -> dict:
    """对外呈现的请求状态；完成时附带复核编号、输入与结论。"""
    body = {
        "request_id": row["request_id"],
        "status": row["status"],
        "input_digest": row["input_digest"],
        "progress": row["progress"],
        "review_id": row["review_id"],
        "input": row["input"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    if row["status"] == storage.STATUS_DONE and row["review_id"]:
        record = load_submission(row["review_id"])
        if record is not None:
            body["conclusion"] = record["conclusion"]
    return body


def _run_request(request_id: str) -> None:
    """后台求解线程：读取冻结输入，在确定检查点响应取消，终态落库。"""
    row = storage.get_request(request_id)
    if row is None or row["status"] not in storage.ACTIVE_STATUSES:
        return
    if not storage.mark_running(request_id):
        return  # 启动前已被取消（持久化裁决）

    event = threading.Event()
    with _cancel_events_lock:
        _cancel_events[request_id] = event

    payload = row["input"]
    last_percent = [-1]

    def cancel_check() -> None:
        if event.is_set():
            raise CancelledError()

    def progress(phase: str, percent: int) -> None:
        if percent != last_percent[0]:
            last_percent[0] = percent
            storage.update_progress(request_id, phase, percent)

    try:
        result = solve(
            payload["channels"],
            payload["checks"],
            cancel_check=cancel_check,
            progress=progress,
        )
    except CancelledError:
        # 与并发完成竞争：条件更新只一方命中；取消的请求不产生复核编号。
        storage.finalize_cancelled(request_id)
    except Exception:  # noqa: BLE001 - 兜底：内部异常落为 failed 终态
        storage.finalize_failed(request_id)
    else:
        norm_checks = [
            (tuple(ck["channels"]), int(ck["parity"])) for ck in payload["checks"]
        ]
        conclusion = _build_conclusion(result, norm_checks)
        # 持久化裁决：取消若已先命中，则结果与复核记录一并丢弃。
        storage.finalize_done(request_id, payload, conclusion)
    finally:
        with _cancel_events_lock:
            _cancel_events.pop(request_id, None)


class Handler(BaseHTTPRequestHandler):
    server_version = "PixelLocator/1.0"

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

    def _read_json_body(self) -> dict | None:
        """读取并解析 JSON 请求体；失败时已发送 400，返回 None。"""
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY:
            self._send_json(400, {
                "error": "请求体缺失或过大",
                "field": "body",
                "errors": [{"field": "body", "message": "需要 JSON 请求体且不超过 1MiB"}],
            })
            return None
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._send_json(400, {
                "error": "JSON 解析失败",
                "field": "body",
                "errors": [{"field": "body", "message": f"JSON 解析失败: {exc}"}],
            })
            return None
        if not isinstance(data, dict):
            self._send_json(400, {
                "error": "请求体必须是对象",
                "field": "body",
                "errors": [{"field": "body", "message": "请求体必须是 JSON 对象"}],
            })
            return None
        return data

    def log_message(self, fmt, *args):  # 静默默认访问日志
        return

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
            request_id = route.rsplit("/", 1)[-1]
            row = storage.get_request(request_id)
            if row is None:
                self._send_json(404, {
                    "error": "请求标识不存在",
                    "field": "request_id",
                    "request_id": request_id,
                })
            else:
                self._send_json(200, _request_state(row))
        elif route.startswith("/api/review/"):
            review_id = route.rsplit("/", 1)[-1]
            record = load_submission(review_id)
            if record is None:
                self._send_json(404, {
                    "error": "复核编号不存在",
                    "field": "review_id",
                    "review_id": review_id,
                })
            else:
                self._send_json(200, record)
        else:
            self._send_json(404, {"error": "not found"})

    # ---- POST ----
    def do_POST(self) -> None:
        route = urlparse(self.path).path
        if route == "/api/submit":
            self._handle_submit()
        elif route == "/api/cancel":
            self._handle_cancel()
        else:
            self._send_json(404, {"error": "not found"})

    def _handle_submit(self) -> None:
        data = self._read_json_body()
        if data is None:
            return

        # 请求标识：客户端可自带（网络重传去重），缺省时由服务分配。
        request_id = data.get("request_id")
        if request_id is None:
            request_id = uuid.uuid4().hex
        elif not isinstance(request_id, str) or not REQUEST_ID_RE.match(request_id):
            self._send_json(400, {
                "error": "请求标识非法",
                "errors": [{
                    "field": "request_id",
                    "message": "请求标识须为 8–64 位字母、数字、连字符或下划线",
                }],
            })
            return

        # 开始求解前：校验并冻结排序通道与校验集合。
        try:
            ordered, norm_checks = validate_input(
                data.get("channels"), data.get("checks")
            )
        except ValidationError as exc:
            self._send_json(400, {
                "error": "输入校验未通过，未生成复核记录",
                "errors": [{"field": f, "message": msg} for f, msg in exc.errors],
            })
            return

        payload = {
            "channels": ordered,
            "checks": [
                {"channels": list(members), "parity": parity}
                for members, parity in norm_checks
            ],
        }
        digest = _input_digest(payload)

        created = storage.create_request(request_id, digest, payload)
        row = storage.get_request(request_id)
        if not created:
            if row["input_digest"] != digest:
                # 复用标识却改变输入：可定位拒绝。
                self._send_json(409, {
                    "error": "请求标识冲突：该标识已被另一份不同输入使用",
                    "errors": [{
                        "field": "request_id",
                        "message": "标识冲突：该请求标识已绑定不同的输入摘要，"
                                   "请更换标识或核对后重传相同输入",
                    }],
                    "request_id": request_id,
                })
                return
            # 相同标识 + 相同输入的重传：返回同一请求状态，不重复求解。
        else:
            threading.Thread(
                target=_run_request, args=(request_id,), daemon=True
            ).start()
        self._send_json(202, _request_state(row))

    def _handle_cancel(self) -> None:
        data = self._read_json_body()
        if data is None:
            return
        request_id = data.get("request_id")
        row = storage.get_request(request_id) if isinstance(request_id, str) else None
        if row is None:
            self._send_json(404, {
                "error": "请求标识不存在",
                "field": "request_id",
                "errors": [{"field": "request_id", "message": "请求标识不存在"}],
            })
            return
        # 先置位取消事件（若求解线程在跑），再做持久化裁决；
        # 完成与取消竞争时，条件更新只一方命中，收敛为唯一终态。
        with _cancel_events_lock:
            event = _cancel_events.get(request_id)
        if event is not None:
            event.set()
        row = storage.cancel_request(request_id)
        self._send_json(200, _request_state(row))


def main() -> None:
    init_db()
    host = os.environ.get("APP_HOST", "0.0.0.0")
    port = int(os.environ.get("APP_PORT", "8080"))
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"pixel locator listening on {host}:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
