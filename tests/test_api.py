"""接口测试：异步求解、取消、幂等重传、标识冲突、复核取回、非法拒绝。

以 Python 标准库 urllib 起真实 HTTP 线程，无需第三方依赖。
"""

import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

# 让测试可以独立运行；且必须在导入应用模块前设置 DB 路径
# （storage 在导入时读取 APP_DB）。
import sys

_TMP = tempfile.TemporaryDirectory()
os.environ["APP_DB"] = os.path.join(_TMP.name, "test.db")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import server  # noqa: E402
import storage  # noqa: E402

UNIQUE_FAULT = {
    "channels": ["CH0", "CH1", "CH2", "CH3", "CH4", "CH5"],
    "checks": [
        {"channels": ["CH0", "CH1", "CH3"], "parity": 1},
        {"channels": ["CH2", "CH3", "CH4"], "parity": 1},
        {"channels": ["CH3", "CH5"], "parity": 1},
        {"channels": ["CH0", "CH2", "CH4"], "parity": 0},
    ],
}

INFEASIBLE = {
    "channels": ["a", "b", "c"],
    "checks": [
        {"channels": ["a", "b"], "parity": 0},
        {"channels": ["a", "b", "c"], "parity": 0},
        {"channels": ["c"], "parity": 1},
    ],
}


def big_slow_body():
    """36 通道、单条校验：左半 2^18 枚举提供确定的取消窗口。"""
    return {
        "channels": [f"PX{j:02d}" for j in range(36)],
        "checks": [{"channels": [f"PX{j:02d}" for j in range(36)], "parity": 1}],
    }


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server.init_db()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def _req(self, method, path, body=None):
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, headers=headers,
                                     method=method)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))

    def _submit(self, body, request_id=None):
        if request_id is not None:
            body = {**body, "request_id": request_id}
        return self._req("POST", "/api/submit", body)

    def _wait_terminal(self, request_id, timeout=15):
        deadline = time.time() + timeout
        while True:
            status, data = self._req("GET", f"/api/request/{request_id}")
            self.assertEqual(status, 200, data)
            if data["status"] not in ("pending", "running"):
                return data
            self.assertLess(time.time(), deadline, f"请求 {request_id} 未进入终态")
            time.sleep(0.02)

    def _wait_done(self, request_id, timeout=15):
        data = self._wait_terminal(request_id, timeout)
        self.assertEqual(data["status"], "done", data)
        self.assertTrue(data["review_id"])
        return data

    # ---- 基础 ----
    def test_healthz(self):
        status, body = self._req("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")

    def test_index_served(self):
        with urllib.request.urlopen(self.base + "/", timeout=10) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("text/html", resp.headers["Content-Type"])
            self.assertIn("故障定位", resp.read().decode("utf-8"))

    # ---- 异步提交 -> 完成 -> 复核取回 ----
    def test_submit_runs_async_and_review(self):
        status, data = self._submit(UNIQUE_FAULT, "rid-unique-0001")
        self.assertEqual(status, 202, data)
        self.assertEqual(data["request_id"], "rid-unique-0001")
        self.assertIn(data["status"], ("pending", "running"))
        self.assertIn("input_digest", data)
        self.assertEqual(len(data["input"]["channels"]), 6)  # 冻结为排序通道

        done = self._wait_done(data["request_id"])
        self.assertEqual(done["conclusion"]["faulty"], ["CH3"])
        self.assertEqual(done["conclusion"]["weight"], 1)
        self.assertTrue(all(r["pass"] for r in done["conclusion"]["recompute"]))

        # 刷新后凭复核编号取回（最小故障通道 + 逐校验复算）
        status2, got = self._req("GET", f"/api/review/{done['review_id']}")
        self.assertEqual(status2, 200)
        self.assertEqual(got["review_id"], done["review_id"])
        self.assertEqual(got["conclusion"]["faulty"], ["CH3"])
        self.assertTrue(all(r["pass"] for r in got["conclusion"]["recompute"]))

    def test_infeasible_is_persisted(self):
        status, data = self._submit(INFEASIBLE, "rid-infeasible-1")
        self.assertEqual(status, 202)
        done = self._wait_done(data["request_id"])
        self.assertFalse(done["conclusion"]["feasible"])
        self.assertEqual(done["conclusion"]["faulty"], [])
        status2, got = self._req("GET", f"/api/review/{done['review_id']}")
        self.assertEqual(status2, 200)
        self.assertFalse(got["conclusion"]["feasible"])

    def test_server_assigns_request_id_when_absent(self):
        status, data = self._submit(UNIQUE_FAULT)
        self.assertEqual(status, 202)
        self.assertRegex(data["request_id"], r"^[0-9a-f]{32}$")
        self._wait_done(data["request_id"])

    # ---- 取消后重提 ----
    def test_cancel_then_resubmit(self):
        status, data = self._submit(big_slow_body(), "rid-cancel-big1")
        self.assertEqual(status, 202)
        rid_old = data["request_id"]

        # 在折半枚举窗口内取消；轮询直到取消成为终态。
        deadline = time.time() + 5
        while True:
            _, cur = self._req("GET", f"/api/request/{rid_old}")
            if cur["status"] in ("pending", "running"):
                st, cancel_body = self._req(
                    "POST", "/api/cancel", {"request_id": rid_old})
                self.assertEqual(st, 200, cancel_body)
            final = self._wait_terminal(rid_old, timeout=5)
            if final["status"] == "cancelled":
                break
            self.assertLess(time.time(), deadline, "未能在完成前取消")
        self.assertIsNone(final["review_id"], "取消的请求不得生成复核编号")

        # 旧计算即使稍后结束也不得写入复核记录：等待足够时间后仍无编号。
        time.sleep(0.8)
        _, later = self._req("GET", f"/api/request/{rid_old}")
        self.assertEqual(later["status"], "cancelled")
        self.assertIsNone(later["review_id"])

        # 同页立即提交另一份观测：新请求显示自身进度与结论。
        status, data2 = self._submit(UNIQUE_FAULT, "rid-after-cancel1")
        self.assertEqual(status, 202)
        self.assertNotEqual(data2["request_id"], rid_old)
        done = self._wait_done(data2["request_id"])
        self.assertEqual(done["conclusion"]["faulty"], ["CH3"])

        # 旧记录仍不存在，新记录可取回。
        st404, _ = self._req("GET", f"/api/review/{'0' * 12}")
        self.assertEqual(st404, 404)
        st2, got = self._req("GET", f"/api/review/{done['review_id']}")
        self.assertEqual(st2, 200)
        self.assertEqual(got["conclusion"]["faulty"], ["CH3"])

    def test_cancel_finished_request_keeps_done(self):
        status, data = self._submit(UNIQUE_FAULT, "rid-cancel-late1")
        done = self._wait_done(data["request_id"])
        # 完成后再取消：终态与复核编号保持不变。
        st, body = self._req("POST", "/api/cancel",
                             {"request_id": done["request_id"]})
        self.assertEqual(st, 200)
        self.assertEqual(body["status"], "done")
        self.assertEqual(body["review_id"], done["review_id"])

    # ---- 相同输入的（并发）重传：同一请求状态 ----
    def test_identical_retry_returns_same_request(self):
        rid = "rid-retry-same-01"
        s1, d1 = self._submit(UNIQUE_FAULT, rid)
        s2, d2 = self._submit(UNIQUE_FAULT, rid)  # 运行中重传
        self.assertEqual((s1, s2), (202, 202))
        self.assertEqual(d1["request_id"], d2["request_id"])
        self.assertEqual(d1["input_digest"], d2["input_digest"])
        done = self._wait_done(rid)

        # 完成后的重传仍返回同一请求与同一复核编号，不重复求解。
        s3, d3 = self._submit(UNIQUE_FAULT, rid)
        self.assertEqual(s3, 202)
        self.assertEqual(d3["status"], "done")
        self.assertEqual(d3["review_id"], done["review_id"])
        self.assertEqual(d3["conclusion"]["faulty"], ["CH3"])

    def test_concurrent_identical_retries_share_one_request(self):
        rid = "rid-concurrent-001"
        results = []

        def fire():
            results.append(self._submit(big_slow_body(), rid))

        threads = [threading.Thread(target=fire) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertTrue(all(st == 202 for st, _ in results))
        self.assertEqual({d["request_id"] for _, d in results}, {rid})
        done = self._wait_done(rid)
        # requests 表中只有一行；并发重传未新建额外请求。
        self.assertIsNotNone(storage.get_request(rid))

    # ---- 复用标识却改变输入：可定位拒绝 ----
    def test_request_id_conflict_rejected(self):
        s1, d1 = self._submit(UNIQUE_FAULT, "rid-conflict-001")
        self.assertEqual(s1, 202)
        self._wait_done(d1["request_id"])

        changed = json.loads(json.dumps(UNIQUE_FAULT))
        changed["checks"][0]["parity"] = 0  # 改变观测
        s2, d2 = self._submit(changed, "rid-conflict-001")
        self.assertEqual(s2, 409, d2)
        self.assertEqual(d2["errors"][0]["field"], "request_id")
        self.assertIn("冲突", d2["errors"][0]["message"])

        # 原请求状态与结论不受影响。
        got = self._wait_done("rid-conflict-001")
        self.assertEqual(got["conclusion"]["faulty"], ["CH3"])

    def test_bad_request_id_format_rejected(self):
        s, d = self._submit(UNIQUE_FAULT, "bad id!!")
        self.assertEqual(s, 400)
        self.assertEqual(d["errors"][0]["field"], "request_id")

    # ---- 终态裁决（存储层直接验证完成/取消竞争）----
    def test_terminal_adjudication_done_wins(self):
        rid = "rid-adjudicate-d01"
        self.assertTrue(storage.create_request(rid, "dig", {"channels": [],
                                                            "checks": []}))
        rid2 = storage.finalize_done(rid, {"channels": [], "checks": []},
                                     {"feasible": True})
        self.assertTrue(rid2)
        # 迟到的取消/完成都不得改变终态。
        self.assertFalse(storage.finalize_cancelled(rid))
        self.assertIsNone(storage.finalize_done(rid, {}, {}))
        row = storage.get_request(rid)
        self.assertEqual(row["status"], "done")
        self.assertIsNotNone(storage.load_submission(rid2))

    def test_terminal_adjudication_cancel_wins(self):
        rid = "rid-adjudicate-c01"
        self.assertTrue(storage.create_request(rid, "dig", {"channels": [],
                                                            "checks": []}))
        self.assertTrue(storage.cancel_request(rid)["status"] == "cancelled")
        # 迟到完成：结果与复核记录一并丢弃。
        self.assertIsNone(storage.finalize_done(rid, {}, {}))
        row = storage.get_request(rid)
        self.assertEqual(row["status"], "cancelled")
        self.assertIsNone(row["review_id"])

    # ---- 非法输入：可定位拒绝，不生成请求/复核记录 ----
    def test_invalid_input_rejected_with_location(self):
        before = storage.get_request("rid-invalid-001")
        self.assertIsNone(before)
        body = {
            "channels": ["a", "b", "a"],
            "checks": [{"channels": [], "parity": 9}],
        }
        status, data = self._submit(body, "rid-invalid-001")
        self.assertEqual(status, 400)
        fields = {e["field"] for e in data["errors"]}
        self.assertIn("channels[2]", fields)
        self.assertTrue(any(".channels" in f for f in fields))
        self.assertTrue(any(".parity" in f for f in fields))
        self.assertIsNone(storage.get_request("rid-invalid-001"),
                          "非法输入不得创建请求")

    def test_duplicate_check_set_rejected(self):
        body = {
            "channels": ["a", "b", "c"],
            "checks": [
                {"channels": ["a", "b"], "parity": 0},
                {"channels": ["b", "a"], "parity": 1},
            ],
        }
        status, data = self._submit(body, "rid-dupcheck-01")
        self.assertEqual(status, 400)
        self.assertTrue(any("重复" in e["message"] for e in data["errors"]))

    def test_bad_json_rejected(self):
        req = urllib.request.Request(
            self.base + "/api/submit",
            data=b"{not json",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=10)
            self.fail("应返回 400")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 400)
            data = json.loads(e.read().decode("utf-8"))
            self.assertEqual(data["field"], "body")

    def test_unknown_ids_404(self):
        status, data = self._req("GET", "/api/review/deadbeefdead")
        self.assertEqual(status, 404)
        self.assertEqual(data["field"], "review_id")
        status, data = self._req("GET", "/api/request/no-such-request")
        self.assertEqual(status, 404)
        self.assertEqual(data["field"], "request_id")
        status, data = self._req("POST", "/api/cancel",
                                 {"request_id": "no-such-request"})
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
