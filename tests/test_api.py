"""接口测试：异步请求生命周期、幂等重传、标识冲突、取消重提、
完成/取消 CAS 竞争、复核取回、非法输入拒绝、健康检查。

以 Python 标准库 urllib 起真实 HTTP 线程，无需第三方依赖。
"""

import json
import os
import random
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

# 让测试可以独立运行；且必须在导入应用模块前设置 DB 路径
# （storage 在导入时读取 APP_DB）。
_TMP = tempfile.TemporaryDirectory()
os.environ["APP_DB"] = os.path.join(_TMP.name, "test.db")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import server  # noqa: E402
import storage  # noqa: E402

# 在检查点制造确定可观测的取消窗口（仅影响求解耗时，不改变结果）。
server.CHECKPOINT_DELAY = 0.01


def big_board():
    """36 通道 / 28 校验的较大板级观测（左半 2^18 枚举，取消窗口充足）。"""
    rng = random.Random(7)
    channels = [f"PX{j:02d}" for j in range(36)]
    checks, seen = [], set()
    while len(checks) < 28:
        k = rng.randint(2, 10)
        members = tuple(sorted(rng.sample(channels, k)))
        if members in seen:
            continue
        seen.add(members)
        checks.append({"channels": list(members), "parity": rng.randint(0, 1)})
    return {"channels": channels, "checks": checks}


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        storage.init_db()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def _req(self, method, path, body=None, raw=None):
        data = None
        headers = {}
        if raw is not None:
            data = raw
            headers["Content-Type"] = "application/json"
        elif body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, headers=headers,
                                     method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))

    def _submit(self, body, request_id=None, expect=202):
        payload = dict(body)
        if request_id is not None:
            payload["request_id"] = request_id
        status, data = self._req("POST", "/api/submit", payload)
        self.assertEqual(status, expect, data)
        return data

    def _wait_terminal(self, request_id, timeout=30):
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            status, data = self._req("GET", f"/api/request/{request_id}")
            self.assertEqual(status, 200, data)
            last = data
            if data["status"] in ("completed", "cancelled"):
                return data
            time.sleep(0.02)
        raise AssertionError(f"请求未在 {timeout}s 内到达终态: {last}")

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

    # ---- 正常生命周期 ----
    def test_submit_unique_fault_and_review(self):
        body = {
            "channels": ["CH0", "CH1", "CH2", "CH3", "CH4", "CH5"],
            "checks": [
                {"channels": ["CH0", "CH1", "CH3"], "parity": 1},
                {"channels": ["CH2", "CH3", "CH4"], "parity": 1},
                {"channels": ["CH3", "CH5"], "parity": 1},
                {"channels": ["CH0", "CH2", "CH4"], "parity": 0},
            ],
        }
        ack = self._submit(body)
        self.assertEqual(ack["status"], "computing")
        self.assertTrue(ack["request_id"])
        self.assertEqual(ack["input_digest"], storage.digest_input(
            {"channels": ack["input"]["channels"], "checks": ack["input"]["checks"]}))

        final = self._wait_terminal(ack["request_id"])
        self.assertEqual(final["status"], "completed")
        # 完成后复核编号与请求标识一一对应。
        self.assertEqual(final["review_id"], final["request_id"])
        self.assertEqual(final["conclusion"]["faulty"], ["CH3"])
        self.assertEqual(final["conclusion"]["weight"], 1)
        self.assertTrue(all(r["pass"]
                            for r in final["conclusion"]["recompute"]))

        # 刷新后凭编号取回
        status2, got = self._req("GET", f"/api/review/{final['review_id']}")
        self.assertEqual(status2, 200)
        self.assertEqual(got["review_id"], final["review_id"])
        self.assertEqual(got["conclusion"]["faulty"], ["CH3"])

    def test_infeasible_is_persisted(self):
        body = {
            "channels": ["a", "b", "c"],
            "checks": [
                {"channels": ["a", "b"], "parity": 0},
                {"channels": ["a", "b", "c"], "parity": 0},
                {"channels": ["c"], "parity": 1},
            ],
        }
        ack = self._submit(body)
        final = self._wait_terminal(ack["request_id"])
        self.assertEqual(final["status"], "completed")
        self.assertFalse(final["conclusion"]["feasible"])
        rid = final["review_id"]
        status2, got = self._req("GET", f"/api/review/{rid}")
        self.assertEqual(status2, 200)
        self.assertFalse(got["conclusion"]["feasible"])
        self.assertEqual(got["conclusion"]["faulty"], [])

    # ---- 拒绝 ----
    def test_invalid_input_rejected_with_location(self):
        body = {
            "channels": ["a", "b", "a"],
            "checks": [{"channels": [], "parity": 9}],
        }
        status, data = self._req("POST", "/api/submit", body)
        self.assertEqual(status, 400)
        fields = {e["field"] for e in data["errors"]}
        self.assertIn("channels[2]", fields)
        self.assertTrue(any(".channels" in f for f in fields))
        self.assertTrue(any(".parity" in f for f in fields))

    def test_duplicate_check_set_rejected(self):
        body = {
            "channels": ["a", "b", "c"],
            "checks": [
                {"channels": ["a", "b"], "parity": 0},
                {"channels": ["b", "a"], "parity": 1},
            ],
        }
        status, data = self._req("POST", "/api/submit", body)
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

    def test_unknown_review_id_404(self):
        status, data = self._req("GET", "/api/review/deadbeefdead")
        self.assertEqual(status, 404)
        self.assertEqual(data["field"], "review_id")

    def test_bad_request_id_format(self):
        body = {"request_id": "not-hex!!", "channels": ["a", "b"],
                "checks": [{"channels": ["a"], "parity": 0}]}
        status, data = self._req("POST", "/api/submit", body)
        self.assertEqual(status, 400)
        self.assertEqual(data["errors"][0]["field"], "request_id")

    # ---- 幂等重传 ----
    def test_same_id_same_input_replay_returns_same_request(self):
        rid = storage.new_request_id()
        body = {
            "channels": ["a", "b", "c"],
            "checks": [{"channels": ["a", "b", "c"], "parity": 1}],
        }
        first = self._submit(body, request_id=rid)
        # 完成后重传：同一请求、同一复核编号，不重复求解。
        self._wait_terminal(rid)
        status2, second = self._req("POST", "/api/submit",
                                    {"request_id": rid, **body})
        self.assertEqual(status2, 200)
        self.assertEqual(second["request_id"], rid)
        self.assertEqual(second["status"], "completed")
        self.assertEqual(second["review_id"], rid)
        self.assertEqual(second["input_digest"], first["input_digest"])
        self.assertEqual(second["conclusion"]["faulty"], ["c"])

    def test_concurrent_identical_replays_share_one_request(self):
        rid = storage.new_request_id()
        body = big_board()
        payload = json.dumps({"request_id": rid, **body}).encode("utf-8")
        results = []

        def fire():
            results.append(self._req(
                "POST", "/api/submit", raw=payload))

        # 两个相同请求在求解进行中并发到达：created/replayed 各一，
        # 但 request_id 与终态必须收敛为同一个。
        t1 = threading.Thread(target=fire)
        t2 = threading.Thread(target=fire)
        t1.start()
        time.sleep(0.05)  # 确保第一个先创建并进入 computing
        t2.start()
        t1.join()
        t2.join()
        statuses = sorted(s for s, _ in results)
        self.assertEqual(statuses, [200, 202], results)
        for _, data in results:
            self.assertEqual(data["request_id"], rid)
            self.assertEqual(data["input_digest"],
                             storage.digest_input(
                                 {"channels": body["channels"],
                                  "checks": [
                                      {"channels": sorted(c["channels"]),
                                       "parity": c["parity"]}
                                      for c in body["checks"]]}))
        final = self._wait_terminal(rid, timeout=40)
        self.assertEqual(final["status"], "completed")
        self.assertEqual(final["review_id"], rid)
        # 只有一条复核记录。
        status, got = self._req("GET", f"/api/review/{rid}")
        self.assertEqual(status, 200)
        self.assertEqual(got["request_id"], rid)

    # ---- 标识冲突 ----
    def test_same_id_changed_input_conflict_is_localized(self):
        rid = storage.new_request_id()
        body_a = {
            "channels": ["a", "b", "c"],
            "checks": [{"channels": ["a", "b", "c"], "parity": 1}],
        }
        self._submit(body_a, request_id=rid)
        self._wait_terminal(rid)

        # 复用标识却改变输入（改一条奇偶值）：定位拒绝 409。
        body_b = {
            "channels": ["a", "b", "c"],
            "checks": [{"channels": ["a", "b", "c"], "parity": 0}],
        }
        status, data = self._req("POST", "/api/submit",
                                 {"request_id": rid, **body_b})
        self.assertEqual(status, 409)
        self.assertEqual(data["field"], "request_id")
        self.assertNotEqual(data["stored_digest"], data["replay_digest"])

        # 原请求结论仍可按原编号读取，未被覆盖。
        status, got = self._req("GET", f"/api/review/{rid}")
        self.assertEqual(status, 200)
        self.assertEqual(got["conclusion"]["faulty"], ["c"])

    # ---- 取消与重提 ----
    def test_cancel_then_resubmit(self):
        # 1) 大板观测：开始后立即取消。
        ack = self._submit(big_board())
        old_id = ack["request_id"]
        # 先确认进入 computing 且有检查点进度。
        self._wait_phase(old_id)
        status, cancel_ack = self._req(
            "POST", f"/api/request/{old_id}/cancel", {})
        self.assertEqual(status, 200)
        final = self._wait_terminal(old_id, timeout=20)
        self.assertEqual(final["status"], "cancelled")
        # 取消阶段由工作线程在下一确定检查点补记（检查点停顿 0.01s，
        # 最多再等数个检查点即可观察到）。
        final = self._wait_cancel_phase(old_id)
        self.assertIn(final["cancel_phase"],
                      ("left_enumeration", "candidate_merge"))
        self.assertNotIn("review_id", final)
        self.assertNotIn("conclusion", final)

        # 取消请求不生成复核编号。
        status, data = self._req("GET", f"/api/review/{old_id}")
        self.assertEqual(status, 404)
        self.assertEqual(data["status"], "cancelled")

        # 2) 同页立即提交另一份小观测：独立请求，正常完成，
        #    旧请求保持 cancelled 不被覆盖。
        other = {
            "channels": ["a", "b", "c"],
            "checks": [{"channels": ["a", "b", "c"], "parity": 1}],
        }
        ack2 = self._submit(other)
        new_id = ack2["request_id"]
        self.assertNotEqual(new_id, old_id)
        final2 = self._wait_terminal(new_id)
        self.assertEqual(final2["status"], "completed")
        self.assertEqual(final2["conclusion"]["faulty"], ["c"])

        # 旧计算即使稍后“结束”也不得翻案：旧请求仍是 cancelled。
        time.sleep(0.2)
        status, old = self._req("GET", f"/api/request/{old_id}")
        self.assertEqual(old["status"], "cancelled")

    def test_cancel_already_completed_keeps_completed(self):
        body = {
            "channels": ["a", "b", "c"],
            "checks": [{"channels": ["a", "b"], "parity": 1}],
        }
        ack = self._submit(body)
        final = self._wait_terminal(ack["request_id"])
        self.assertEqual(final["status"], "completed")
        # 完成后再取消：终态不翻转，复核记录仍在。
        status, data = self._req(
            "POST", f"/api/request/{ack['request_id']}/cancel", {})
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "completed")
        status, got = self._req("GET", f"/api/review/{ack['request_id']}")
        self.assertEqual(status, 200)

    def test_cancel_unknown_id_404(self):
        status, data = self._req("POST", "/api/request/0123456789abcdef/cancel", {})
        self.assertEqual(status, 404)
        self.assertEqual(data["field"], "request_id")

    def _wait_phase(self, request_id):
        deadline = time.time() + 10
        while time.time() < deadline:
            status, data = self._req("GET", f"/api/request/{request_id}")
            if data.get("status") == "computing" and data.get("progress", {}).get("phase"):
                return data
            if data.get("status") in ("completed", "cancelled"):
                return data
            time.sleep(0.01)
        raise AssertionError("请求未进入带检查点的 computing 状态")

    def _wait_cancel_phase(self, request_id, timeout=5):
        deadline = time.time() + timeout
        while time.time() < deadline:
            status, data = self._req("GET", f"/api/request/{request_id}")
            self.assertEqual(status, 200)
            if data["status"] == "cancelled" and data["cancel_phase"]:
                return data
            time.sleep(0.02)
        raise AssertionError("取消阶段未被工作线程补记")

    # ---- 完成与取消竞争：持久化 CAS 只收敛一个终态 ----
    def test_completion_loses_to_persisted_cancel(self):
        rid = storage.new_request_id()
        payload = {"channels": ["a", "b"], "checks": [
            {"channels": ["a"], "parity": 1}]}
        outcome = storage.create_request(rid, payload)
        self.assertEqual(outcome["outcome"], "created")
        # 取消事务先提交。
        storage.request_cancel(rid)
        conclusion = {"feasible": True, "faulty": ["a"], "weight": 1}
        req = storage.complete_request(rid, conclusion)
        self.assertEqual(req["status"], "cancelled")
        self.assertIsNone(req["conclusion"])
        self.assertEqual(req["review_id"], "")
        self.assertIsNone(storage.get_by_review_id(rid))

    def test_cancel_checkpoint_loses_to_persisted_completion(self):
        rid = storage.new_request_id()
        payload = {"channels": ["a", "b"], "checks": [
            {"channels": ["a"], "parity": 1}]}
        storage.create_request(rid, payload)
        conclusion = {"feasible": True, "faulty": ["a"], "weight": 1}
        # 完成事务先提交：迟到的检查点取消不得覆盖。
        storage.complete_request(rid, conclusion)
        req = storage.mark_cancelled_at_checkpoint(rid, "candidate_merge")
        self.assertEqual(req["status"], "completed")
        self.assertEqual(req["conclusion"], conclusion)
        self.assertEqual(storage.get_by_review_id(rid)["review_id"], rid)


if __name__ == "__main__":
    unittest.main(verbosity=2)
