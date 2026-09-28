"""Compose verify 使用的 API 冒烟脚本：对运行中的 app 服务发真实请求。

覆盖：
- 健康检查、普通提交（唯一故障、多解裁决）、不可行结论持久化；
- 取消后重提：旧请求停留 cancelled、无复核编号，新请求独立完成；
- 相同输入并发重传：共享同一请求与复核编号；
- 标识冲突：复用标识改变输入被 409 可定位拒绝；
- 完成记录刷新：按复核编号取回最小故障通道与逐校验复算；
- 非法输入（可定位拒绝）、重复校验集合拒绝。

任一步失败以退出码 1 结束。
"""

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

BASE = os.environ.get("APP_URL", "http://app:8080").rstrip("/")

UNIQUE_FAULT = {
    "channels": ["CH0", "CH1", "CH2", "CH3", "CH4", "CH5"],
    "checks": [
        {"channels": ["CH0", "CH1", "CH3"], "parity": 1},
        {"channels": ["CH2", "CH3", "CH4"], "parity": 1},
        {"channels": ["CH3", "CH5"], "parity": 1},
        {"channels": ["CH0", "CH2", "CH4"], "parity": 0},
    ],
}

# 36 通道单条校验：折半枚举 2^18 个左半部分向量，提供确定取消窗口。
BIG_SLOW = {
    "channels": [f"PX{j:02d}" for j in range(36)],
    "checks": [{"channels": [f"PX{j:02d}" for j in range(36)], "parity": 1}],
}


def call(method, path, body=None, timeout=20):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def check(name, cond, detail=""):
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {name}" + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        raise SystemExit(f"冒烟失败: {name} {detail}")


def submit(body, request_id=None):
    body = dict(body)
    if request_id is not None:
        body["request_id"] = request_id
    return call("POST", "/api/submit", body)


def wait_terminal(request_id, timeout=30):
    deadline = time.time() + timeout
    while True:
        status, data = call("GET", f"/api/request/{request_id}")
        if status == 200 and data.get("status") not in ("pending", "running"):
            return data
        if time.time() > deadline:
            raise SystemExit(f"请求 {request_id} 未在 {timeout}s 内进入终态: {data}")
        time.sleep(0.03)


def main():
    print(f"API 冒烟目标: {BASE}")

    # 0. 健康检查
    status, body = call("GET", "/healthz")
    check("健康检查 200", status == 200 and body.get("status") == "ok")

    # 1. 普通提交：唯一故障 CH3（异步 -> 轮询 -> 完成记录）
    status, data = submit(UNIQUE_FAULT, "smoke-unique-0001")
    check("普通提交返回 202", status == 202, str(status))
    check("分配独立请求标识", data.get("request_id") == "smoke-unique-0001")
    check("冻结输入（通道已排序）", data["input"]["channels"]
          == sorted(data["input"]["channels"]))
    check("附带输入摘要", len(data.get("input_digest", "")) == 64)
    done = wait_terminal("smoke-unique-0001")
    check("请求收敛为 done", done["status"] == "done", str(done.get("status")))
    check("唯一故障 = CH3 且重量 1",
          done["conclusion"]["faulty"] == ["CH3"]
          and done["conclusion"]["weight"] == 1,
          str(done["conclusion"].get("faulty")))
    check("逐校验复算全部一致",
          all(r["pass"] for r in done["conclusion"]["recompute"]))
    rid_unique = done["review_id"]
    check("完成请求持有复核编号", bool(rid_unique))

    # 2. 多解裁决：{a,b,c} 奇偶 1，字典序裁决给 c。
    status, data = submit({
        "channels": ["a", "b", "c"],
        "checks": [{"channels": ["a", "b", "c"], "parity": 1}],
    }, "smoke-tiebreak-0001")
    check("多解提交 202", status == 202, str(data))
    done = wait_terminal("smoke-tiebreak-0001")
    check("多解同重量裁决给 c", done["conclusion"]["faulty"] == ["c"],
          str(done["conclusion"].get("faulty")))

    # 3. 不可行：结论必须持久化而非返回近似集合
    status, data = submit({
        "channels": ["a", "b", "c"],
        "checks": [
            {"channels": ["a", "b"], "parity": 0},
            {"channels": ["a", "b", "c"], "parity": 0},
            {"channels": ["c"], "parity": 1},
        ],
    }, "smoke-infeasible-01")
    check("不可行提交 202（保存不可行结论）", status == 202, str(data))
    done = wait_terminal("smoke-infeasible-01")
    check("结论为不可行且无故障集合",
          done["conclusion"]["feasible"] is False
          and done["conclusion"]["faulty"] == [],
          str(done.get("conclusion")))
    rid_infeasible = done["review_id"]

    # 4. 完成记录刷新：既有普通提交与不可行记录均可按原编号读取
    status, got = call("GET", f"/api/review/{rid_unique}")
    check("普通记录刷新可取回（最小故障通道+复算）", status == 200
          and got["conclusion"]["faulty"] == ["CH3"]
          and all(r["pass"] for r in got["conclusion"]["recompute"]))
    status, got = call("GET", f"/api/review/{rid_infeasible}")
    check("不可行记录刷新可取回且仍不可行",
          status == 200 and got["conclusion"]["feasible"] is False)

    # 5. 取消后重提
    status, data = submit(BIG_SLOW, "smoke-cancel-big01")
    check("大观测提交 202", status == 202, str(data))
    # 在折半枚举窗口内反复尝试取消，直到持久化裁决为 cancelled。
    deadline = time.time() + 10
    final = None
    while time.time() < deadline:
        _, cur = call("GET", "/api/request/smoke-cancel-big01")
        if cur["status"] in ("pending", "running"):
            st, cb = call("POST", "/api/cancel",
                          {"request_id": "smoke-cancel-big01"})
            check("取消接口 200", st == 200, str(cb))
        final = wait_terminal("smoke-cancel-big01", timeout=5)
        if final["status"] == "cancelled":
            break
    check("旧请求终态为 cancelled", final is not None
          and final["status"] == "cancelled", str(final))
    check("取消的请求不生成复核编号", final is not None
          and not final.get("review_id"))
    # 旧计算即使稍后结束也不得写入：等待后复核记录仍不存在、状态不变。
    time.sleep(1.0)
    _, later = call("GET", "/api/request/smoke-cancel-big01")
    check("旧计算迟到不覆盖（仍 cancelled、无编号）",
          later["status"] == "cancelled" and not later.get("review_id"))

    # 同页立即提交另一份观测：新请求只显示自身进度与结论
    status, data = submit(UNIQUE_FAULT, "smoke-resubmit-001")
    check("取消后重提 202", status == 202 and data["request_id"]
          == "smoke-resubmit-001", str(data))
    done2 = wait_terminal("smoke-resubmit-001")
    check("新请求独立完成且结论正确",
          done2["status"] == "done"
          and done2["conclusion"]["faulty"] == ["CH3"], str(done2))
    status, got = call("GET", f"/api/review/{done2['review_id']}")
    check("新请求复核记录可取回", status == 200
          and got["conclusion"]["faulty"] == ["CH3"])

    # 6. 相同输入的并发重传：同一请求状态、同一复核编号
    results = []

    def fire():
        results.append(submit(BIG_SLOW, "smoke-retry-conc01"))

    threads = [threading.Thread(target=fire) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check("并发重传全部 202", all(st == 202 for st, _ in results), str(results))
    digests = {d.get("input_digest") for _, d in results}
    check("并发重传共享输入摘要", len(digests) == 1)
    done3 = wait_terminal("smoke-retry-conc01")
    check("并发重传收敛为单一 done", done3["status"] == "done", str(done3))
    # 完成后的相同重传仍返回同一请求状态，不重复求解。
    st, again = submit(BIG_SLOW, "smoke-retry-conc01")
    check("终态后相同重传返回同一完成状态",
          st == 202 and again["status"] == "done"
          and again["review_id"] == done3["review_id"], str(again))

    # 7. 标识冲突：复用标识却改变输入 -> 409 可定位拒绝
    changed = json.loads(json.dumps(UNIQUE_FAULT))
    changed["checks"][0]["parity"] = 0
    st, conflict = submit(changed, "smoke-unique-0001")
    check("标识冲突返回 409", st == 409, str(st))
    check("冲突可定位到 request_id",
          any(e.get("field") == "request_id" for e in conflict.get("errors", [])))
    # 原请求状态不受影响。
    _, untouched = call("GET", "/api/request/smoke-unique-0001")
    check("冲突不影响原请求结论",
          untouched["status"] == "done"
          and untouched["review_id"] == rid_unique)

    # 8. 非法输入：可定位拒绝（重复通道 / 空集合 / 非法奇偶）
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

    # 9. 重复校验集合被拒绝
    status, data = call("POST", "/api/submit", {
        "channels": ["a", "b", "c"],
        "checks": [
            {"channels": ["a", "b"], "parity": 0},
            {"channels": ["b", "a"], "parity": 1},
        ],
    })
    check("重复校验集合返回 400", status == 400)
    check("重复原因可读", any("重复" in e["message"] for e in data.get("errors", [])))

    print("API 冒烟全部通过")


if __name__ == "__main__":
    main()
