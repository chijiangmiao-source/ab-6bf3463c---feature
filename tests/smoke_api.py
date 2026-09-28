"""Compose verify 使用的 API 冒烟脚本：对运行中的 app 服务发真实请求。

覆盖：
- 健康检查、普通提交（唯一故障/多解裁决）、不可行记录持久化；
- 非法输入（可定位拒绝）与重复校验拒绝；
- 取消后同页重提（旧请求不翻案、不生成复核编号）；
- 相同标识相同输入的并发重传（共享同一请求状态）；
- 复用标识却改变输入（409 定位拒绝，原记录仍可读）；
- 已完成记录刷新取回（最小故障通道与逐校验复算）。

任一步失败以退出码 1 结束。
"""

import json
import os
import random
import threading
import time
import urllib.error
import urllib.request
import uuid

BASE = os.environ.get("APP_URL", "http://app:8080").rstrip("/")

UNIQUE_BODY = {
    "channels": ["CH0", "CH1", "CH2", "CH3", "CH4", "CH5"],
    "checks": [
        {"channels": ["CH0", "CH1", "CH3"], "parity": 1},
        {"channels": ["CH2", "CH3", "CH4"], "parity": 1},
        {"channels": ["CH3", "CH5"], "parity": 1},
        {"channels": ["CH0", "CH2", "CH4"], "parity": 0},
    ],
}

# 36 通道 / 28 校验的大板观测，用于制造确定的取消窗口。
_rng = random.Random(7)
_BIG_CHANNELS = [f"PX{j:02d}" for j in range(36)]
_BIG_CHECKS, _seen = [], set()
while len(_BIG_CHECKS) < 28:
    k = _rng.randint(2, 10)
    members = tuple(sorted(_rng.sample(_BIG_CHANNELS, k)))
    if members in _seen:
        continue
    _seen.add(members)
    _BIG_CHECKS.append({"channels": list(members), "parity": _rng.randint(0, 1)})
BIG_BODY = {"channels": _BIG_CHANNELS, "checks": _BIG_CHECKS}


def call(method, path, body=None, raw=None, timeout=30):
    data = None
    headers = {}
    if raw is not None:
        data = raw
        headers["Content-Type"] = "application/json"
    elif body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def submit(body, request_id=None, timeout=30):
    payload = dict(body)
    if request_id is not None:
        payload["request_id"] = request_id
    return call("POST", "/api/submit", payload, timeout=timeout)


def wait_terminal(request_id, timeout=40):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        status, data = call("GET", f"/api/request/{request_id}")
        assert status == 200, data
        last = data
        if data["status"] in ("completed", "cancelled"):
            return data
        time.sleep(0.05)
    raise AssertionError(f"请求 {request_id} 未在 {timeout}s 内到达终态: {last}")


def wait_computing(request_id, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        _, data = call("GET", f"/api/request/{request_id}")
        if data.get("status") == "computing":
            return data
        if data.get("status") in ("completed", "cancelled"):
            return data
        time.sleep(0.02)
    raise AssertionError(f"请求 {request_id} 未进入 computing")


def wait_cancel_phase(request_id, timeout=15):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        _, data = call("GET", f"/api/request/{request_id}")
        last = data
        if data.get("status") == "cancelled" and data.get("cancel_phase"):
            return data
        time.sleep(0.05)
    raise AssertionError(f"取消阶段未补记: {last}")


def check(name, cond, detail=""):
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {name}" + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        raise SystemExit(f"冒烟失败: {name} {detail}")


def main():
    print(f"API 冒烟目标: {BASE}")

    # 0. 健康检查
    status, body = call("GET", "/healthz")
    check("健康检查 200", status == 200 and body.get("status") == "ok")

    # 1. 普通提交：唯一故障 CH3，完成后刷新取回
    status, data = submit(UNIQUE_BODY)
    check("普通提交被接受 202", status == 202, str(data))
    rid_unique = data["request_id"]
    check("新请求状态为 computing", data["status"] == "computing")
    check("分配独立请求标识与输入摘要",
          len(rid_unique) >= 8 and bool(data.get("input_digest")))
    final = wait_terminal(rid_unique)
    check("普通提交收敛 completed", final["status"] == "completed")
    check("唯一故障 = CH3 且重量 1",
          final["conclusion"]["faulty"] == ["CH3"]
          and final["conclusion"]["weight"] == 1,
          str(final.get("conclusion")))
    check("逐校验复算全部一致",
          all(r["pass"] for r in final["conclusion"]["recompute"]))

    # 2. 多解裁决
    status, data = submit({
        "channels": ["a", "b", "c"],
        "checks": [{"channels": ["a", "b", "c"], "parity": 1}],
    })
    check("多解提交 202", status == 202, str(data))
    final2 = wait_terminal(data["request_id"])
    check("多解同重量裁决给 c",
          final2["conclusion"]["faulty"] == ["c"],
          str(final2["conclusion"].get("faulty")))

    # 3. 不可行记录持久化
    status, data = submit({
        "channels": ["a", "b", "c"],
        "checks": [
            {"channels": ["a", "b"], "parity": 0},
            {"channels": ["a", "b", "c"], "parity": 0},
            {"channels": ["c"], "parity": 1},
        ],
    })
    check("不可行提交 202", status == 202, str(data))
    rid_infeasible = data["request_id"]
    finf = wait_terminal(rid_infeasible)
    check("不可行结论保存且无故障集合",
          finf["conclusion"]["feasible"] is False
          and finf["conclusion"]["faulty"] == [],
          str(finf.get("conclusion")))

    # 4. 刷新取回：普通记录（含最小故障通道与逐校验复算）与不可行记录
    status, got = call("GET", f"/api/review/{rid_unique}")
    check("完成记录刷新可取回", status == 200
          and got["review_id"] == rid_unique
          and got["conclusion"]["faulty"] == ["CH3"])
    check("取回记录含逐校验复算",
          all(r["pass"] for r in got["conclusion"]["recompute"]))
    status, got = call("GET", f"/api/review/{rid_infeasible}")
    check("不可行记录刷新仍不可行",
          status == 200 and got["conclusion"]["feasible"] is False)

    # 5. 非法输入：可定位拒绝
    status, data = call("POST", "/api/submit", {
        "channels": ["a", "b", "a"],
        "checks": [{"channels": [], "parity": 9}],
    })
    check("非法输入返回 400", status == 400, str(status))
    fields = {e["field"] for e in data.get("errors", [])}
    check("错误信息可定位（channels[2]）", "channels[2]" in fields, str(fields))
    check("错误信息可定位（空集合）",
          any(f.endswith(".channels") for f in fields), str(fields))
    check("错误信息可定位（parity）",
          any(f.endswith(".parity") for f in fields), str(fields))

    # 6. 重复校验集合被拒绝
    status, data = call("POST", "/api/submit", {
        "channels": ["a", "b", "c"],
        "checks": [
            {"channels": ["a", "b"], "parity": 0},
            {"channels": ["b", "a"], "parity": 1},
        ],
    })
    check("重复校验集合返回 400", status == 400)
    check("重复原因可读", any("重复" in e["message"] for e in data.get("errors", [])))

    # 7. 取消后同页立即重提
    status, data = submit(BIG_BODY, timeout=60)
    check("大板观测提交 202", status == 202, str(status))
    old_id = data["request_id"]
    wait_computing(old_id)
    status, cack = call("POST", f"/api/request/{old_id}/cancel", {})
    check("取消请求 200", status == 200, str(cack))
    cold = wait_terminal(old_id, timeout=40)
    check("旧请求收敛 cancelled", cold["status"] == "cancelled", str(cold))
    cold = wait_cancel_phase(old_id)
    check("取消发生在确定检查点",
          cold.get("cancel_phase") in ("left_enumeration", "candidate_merge"),
          str(cold.get("cancel_phase")))
    check("取消请求无复核编号", not cold.get("review_id"))
    status, rev = call("GET", f"/api/review/{old_id}")
    check("取消后不生成复核记录（404）", status == 404
          and rev.get("status") == "cancelled", str(rev))

    # 同页立即提交另一份（小）观测：独立标识、独立进度与结论。
    status, data2 = submit({
        "channels": ["a", "b", "c"],
        "checks": [{"channels": ["a", "b", "c"], "parity": 1}],
    })
    new_id = data2["request_id"]
    check("重提分配独立请求标识", new_id != old_id and len(new_id) >= 8)
    cnew = wait_terminal(new_id)
    check("新请求只显示自身结论（c）",
          cnew["status"] == "completed"
          and cnew["conclusion"]["faulty"] == ["c"],
          str(cnew.get("conclusion")))
    time.sleep(0.3)
    _, old_after = call("GET", f"/api/request/{old_id}")
    check("旧计算稍后结束也不覆盖（仍 cancelled）",
          old_after["status"] == "cancelled", str(old_after))
    status, got_new = call("GET", f"/api/review/{new_id}")
    check("新请求复核记录可读", status == 200
          and got_new["conclusion"]["faulty"] == ["c"])

    # 8. 相同标识 + 相同输入的网络重传（含并发）
    rid = None
    status, data = submit(UNIQUE_BODY)
    rid = data["request_id"]
    wait_terminal(rid)
    status, replay = submit(UNIQUE_BODY, request_id=rid)
    check("同标识同输入重传 200 且返回同一请求",
          status == 200 and replay["request_id"] == rid
          and replay["status"] == "completed"
          and replay["review_id"] == rid,
          str(replay))

    # 并发重传：一个 202（创建）、一个 200（复用），共享同一请求状态。
    rid2 = uuid.uuid4().hex
    raw = json.dumps({"request_id": rid2, **BIG_BODY}).encode("utf-8")
    results = []

    def fire():
        results.append(call("POST", "/api/submit", raw=raw, timeout=60))

    t1 = threading.Thread(target=fire)
    t2 = threading.Thread(target=fire)
    t1.start()
    time.sleep(0.1)
    t2.start()
    t1.join()
    t2.join()
    codes = sorted(s for s, _ in results)
    check("并发重传：202 + 200 各一", codes == [200, 202], str(codes))
    check("并发重传共享同一请求标识与摘要",
          all(d["request_id"] == rid2 and d.get("input_digest")
              for _, d in results))
    shared = wait_terminal(rid2, timeout=60)
    check("共享请求唯一收敛 completed",
          shared["status"] == "completed" and shared["review_id"] == rid2)
    status, got = call("GET", f"/api/review/{rid2}")
    check("共享请求只有一条复核记录",
          status == 200 and got["request_id"] == rid2)

    # 9. 复用标识却改变输入：409 定位拒绝，原记录仍可读取
    changed = json.loads(json.dumps(UNIQUE_BODY))
    changed["checks"][0]["parity"] = 1 - changed["checks"][0]["parity"]
    status, data = call("POST", "/api/submit",
                        {"request_id": rid, **changed})
    check("标识冲突返回 409", status == 409, str(status))
    check("冲突定位到 request_id 且给出双摘要",
          data.get("field") == "request_id"
          and data.get("stored_digest") != data.get("replay_digest"),
          str(data))
    status, got = call("GET", f"/api/review/{rid}")
    check("冲突后原完成记录仍可按原编号读取",
          status == 200 and got["conclusion"]["faulty"] == ["CH3"])

    print("API 冒烟全部通过")


if __name__ == "__main__":
    main()
