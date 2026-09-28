"""求解器单元测试：唯一故障、多解裁决、不可行、输入校验、规模边界。

运行：python -m pytest -q  （无 pytest 时：python tests/test_solver.py）
"""

import itertools
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.solver import (
    MAX_CHECKS,
    MAX_CHANNELS,
    PHASE_LEFT,
    PHASE_RIGHT,
    SolveCancelled,
    ValidationError,
    prepare,
    recompute,
    solve,
    solve_frozen,
)


def brute_force(channels, checks):
    """枚举完整向量的参考实现（仅测试用，用于核对 MITM 最优性）。"""
    channels = sorted(set(channels))
    n = len(channels)
    pos = {c: i for i, c in enumerate(channels)}
    rows = []
    for members, parity in checks:
        mask = 0
        for m in members:
            mask |= 1 << pos[m]
        rows.append((mask, parity))

    best = None  # (weight, vector tuple)
    for bits in range(1 << n):
        ok = True
        for mask, parity in rows:
            if ((mask & bits).bit_count() & 1) != parity:
                ok = False
                break
        if ok:
            vec = tuple((bits >> j) & 1 for j in range(n))
            cand = (sum(vec), vec)
            if best is None or cand < best:
                best = cand
    return channels, best


class SolverTests(unittest.TestCase):
    def test_unique_single_fault(self):
        channels = ["CH0", "CH1", "CH2", "CH3", "CH4", "CH5"]
        checks = [
            (["CH0", "CH1", "CH3"], 1),
            (["CH2", "CH3", "CH4"], 1),
            (["CH3", "CH5"], 1),
            (["CH0", "CH2", "CH4"], 0),
        ]
        r = solve(channels, checks)
        self.assertTrue(r.feasible)
        self.assertEqual(r.weight, 1)
        self.assertEqual(r.faulty, ("CH3",))
        # 折半：6 通道 -> 左 3 右 3
        self.assertEqual(r.left_size, 3)
        # 逐校验复算必须全部一致
        rows = [(tuple(m), p) for m, p in checks]
        for row in recompute(list(r.channels), rows, list(r.vector)):
            self.assertTrue(row["pass"], row)

    def test_zero_weight_solution(self):
        # 所有观测奇偶均为 0：零向量可行，最优重量 0。
        channels = ["a", "b", "c", "d"]
        checks = [(["a", "b"], 0), (["b", "c", "d"], 0)]
        r = solve(channels, checks)
        self.assertTrue(r.feasible)
        self.assertEqual(r.weight, 0)
        self.assertEqual(r.faulty, ())
        self.assertEqual(r.vector, (0, 0, 0, 0))

    def test_tie_break_lexicographic(self):
        # 仅一条校验 {a,b,c} 奇偶 1：重量 1 的解有 a / b / c 三个，
        # 选择向量按通道升序为 (x_a,x_b,x_c)，标准字典序
        # (0,0,1) < (0,1,0) < (1,0,0)，故裁决给 c。
        channels = ["c", "a", "b"]  # 故意乱序录入
        checks = [(["a", "b", "c"], 1)]
        r = solve(channels, checks)
        self.assertTrue(r.feasible)
        self.assertEqual(r.weight, 1)
        self.assertEqual(r.faulty, ("c",))
        self.assertEqual(r.vector, (0, 0, 1))

    def test_tie_break_weight_two(self):
        # 无约束（空系统不允许），构造两个重量 2 解：
        # x1 xor x2 = 0 且 x3 xor x4 = 0，外加目标使 0000 不可行，
        # 用 x1 xor x3 = 1 ：可行重量 2 解为 (1,1,0,0) 与 (0,0,1,1)，
        # 字典序较小者 (1,1,0,0) -> 通道 a,b。
        channels = ["a", "b", "c", "d"]
        checks = [
            (["a", "b"], 1),
            (["c", "d"], 0),
            (["a", "c"], 1),
        ]
        # 校验：a⊕b=1, c⊕d=0, a⊕c=1
        # 重量1？a=1 -> b=0,c=0,d=0: a⊕c=1 OK, c⊕d=0 OK -> 唯一重量1 a
        r = solve(channels, checks)
        self.assertTrue(r.feasible)
        self.assertEqual(r.weight, 1)
        self.assertEqual(r.faulty, ("a",))

        # 改为迫使 a=0：a⊕b=0, c⊕d=0, a⊕c=1, b⊕d=1
        # 重量2 解：(a,b)=00 -> (c,d)=11 -> 向量 (0,0,1,1)
        #          (a,b)=11 -> (c,d)=00 -> 向量 (1,1,0,0)
        # 标准字典序 (0,0,1,1) 更小，裁决给 c,d。
        checks2 = [
            (["a", "b"], 0),
            (["c", "d"], 0),
            (["a", "c"], 1),
            (["b", "d"], 1),
        ]
        r2 = solve(channels, checks2)
        self.assertTrue(r2.feasible)
        self.assertEqual(r2.weight, 2)
        self.assertEqual(r2.faulty, ("c", "d"))

    def test_infeasible_simple(self):
        # 同一通道集合出现奇偶 0 与 1 直接矛盾。
        channels = ["a", "b", "c"]
        checks = [(["a", "b"], 0), (["a", "b"], 1)]
        # 注意：集合重复会被输入校验拒绝；这里用不同集合制造矛盾：
        checks = [
            (["a", "b"], 0),
            (["a", "b", "c"], 0),
            (["c"], 1),
        ]
        # a⊕b=0, a⊕b⊕c=0 => c=0，与 c=1 矛盾。
        r = solve(channels, checks)
        self.assertFalse(r.feasible)
        self.assertEqual(r.weight, -1)
        self.assertEqual(r.faulty, ())
        self.assertEqual(r.vector, ())

    def test_infeasible_xor_contradiction(self):
        # 行 3 = 行1 xor 行2 但目标位不满足对应关系。
        channels = ["a", "b", "c", "d"]
        checks = [
            (["a", "b"], 1),
            (["b", "c"], 1),
            (["a", "c"], 1),  # 应为 1⊕1=0，给 1 => 矛盾
            (["d"], 0),
        ]
        r = solve(channels, checks)
        self.assertFalse(r.feasible)

    # ---- 随机对照：MITM 结果必须与完整枚举逐例一致 ----
    def test_random_against_bruteforce(self):
        rng = random.Random(20260926)
        for trial in range(60):
            n = rng.randint(2, 12)
            channels = [f"c{j:02d}" for j in range(n)]
            m = rng.randint(1, min(2 * n, 28, 2 ** n - 1))
            seen = set()
            checks = []
            while len(checks) < m:
                k = rng.randint(1, n)
                members = tuple(sorted(rng.sample(channels, k)))
                if members in seen:
                    continue
                seen.add(members)
                checks.append((list(members), rng.randint(0, 1)))

            ordered, best = brute_force(channels, checks)
            r = solve(channels, checks)
            if best is None:
                self.assertFalse(r.feasible, f"trial {trial}: 应为不可行")
                continue
            bw, bvec = best
            self.assertTrue(r.feasible, f"trial {trial}: 应可行")
            self.assertEqual(r.weight, bw, f"trial {trial}: 重量不一致")
            self.assertEqual(r.vector, bvec, f"trial {trial}: 裁决向量不一致 {checks}")

    def test_max_size_performance(self):
        # 36 通道、28 校验必须可接受时间内完成（2^18 折半枚举）。
        rng = random.Random(7)
        channels = [f"PX{j:02d}" for j in range(MAX_CHANNELS)]
        checks = []
        seen = set()
        while len(checks) < MAX_CHECKS:
            k = rng.randint(2, 10)
            members = tuple(sorted(rng.sample(channels, k)))
            if members in seen:
                continue
            seen.add(members)
            checks.append((list(members), rng.randint(0, 1)))
        r = solve(channels, checks)
        # 只要求算法正常结束并自洽；满秩随机系统大概率有低重量解或可行。
        if r.feasible:
            rows = [(tuple(m), p) for m, p in checks]
            self.assertTrue(
                all(row["pass"] for row in recompute(
                    list(r.channels), rows, list(r.vector)))
            )

    # ---- 输入校验 ----
    def test_duplicate_channel_rejected(self):
        with self.assertRaises(ValidationError) as ctx:
            solve(["a", "b", "a"], [(["a", "b"], 0)])
        fields = {f for f, _ in ctx.exception.errors}
        self.assertTrue(any("channels[2]" in f for f in fields))

    def test_channel_count_bounds(self):
        with self.assertRaises(ValidationError):
            solve(["a"], [(["a"], 0)])
        with self.assertRaises(ValidationError):
            solve([f"c{j}" for j in range(37)],
                  [(["c0", "c1"], 0)])

    def test_empty_or_duplicate_check_set_rejected(self):
        with self.assertRaises(ValidationError) as ctx:
            solve(["a", "b", "c"], [([], 0)])
        self.assertTrue(any("channels" in f for f, _ in ctx.exception.errors))

        with self.assertRaises(ValidationError) as ctx:
            solve(["a", "b", "c"],
                  [(["a", "b"], 0), (["b", "a"], 1)])
        self.assertTrue(any("重复" in m for _, m in ctx.exception.errors))

    def test_unknown_channel_and_bad_parity_rejected(self):
        with self.assertRaises(ValidationError) as ctx:
            solve(["a", "b"], [(["a", "z"], 5)])
        msgs = " ".join(m for _, m in ctx.exception.errors)
        self.assertIn("未定义通道", msgs)
        self.assertIn("奇偶", msgs)

    def test_too_many_checks(self):
        with self.assertRaises(ValidationError):
            solve(["a", "b"], [(["a"], 0)] * (MAX_CHECKS + 1))

    def test_syndrome_index_built_per_spec(self):
        # 5 通道折半为 l=2 / r=3；左半至多 2^2=4 个综合征条目。
        r = solve(["a", "b", "c", "d", "e"],
                  [(["a", "c"], 1), (["b", "d", "e"], 0)])
        self.assertTrue(r.feasible)
        self.assertEqual(r.left_size, 2)
        self.assertLessEqual(r.left_index_size, 4)

    def test_all_single_bit_syndromes(self):
        # 每条校验单独覆盖一个通道，直接定位所有观测为 1 的通道，
        # 且通道按标识排序输出。
        channels = [f"x{j}" for j in range(8)]
        checks = [
            (["x7"], 1), (["x1"], 1), (["x3"], 1),
        ] + [([f"x{j}"], 0) for j in (0, 2, 4, 5, 6)]
        r = solve(channels, checks)
        self.assertTrue(r.feasible)
        self.assertEqual(r.weight, 3)
        self.assertEqual(r.faulty, ("x1", "x3", "x7"))

    # ---- 取消检查点与冻结输入 ----
    def test_prepare_freezes_sorted_input(self):
        frozen = prepare(["c", "a", "b"], [
            (["b", "a"], 1), (["c"], 0)])
        self.assertEqual(frozen.channels, ("a", "b", "c"))
        # 校验成员已排序；重复求解得到完全一致的冻结结果（可重放）。
        again = prepare(["b", "c", "a"], [(["a", "b"], 1), (["c"], 0)])
        self.assertEqual(again.checks, frozen.checks)
        self.assertEqual(again.target, frozen.target)
        self.assertEqual(again.left_masks, frozen.left_masks)
        r = solve_frozen(frozen)
        self.assertTrue(r.feasible)
        # a⊕b=1, c=0 的最小重量解为仅 b 失效。
        self.assertEqual(r.faulty, ("b",))

    def test_cancel_at_left_enumeration_checkpoint(self):
        # 36 通道：左半 2^18 枚举，在首个检查点即取消。
        rng = random.Random(7)
        channels = [f"PX{j:02d}" for j in range(MAX_CHANNELS)]
        checks = []
        seen = set()
        while len(checks) < MAX_CHECKS:
            k = rng.randint(2, 10)
            members = tuple(sorted(rng.sample(channels, k)))
            if members in seen:
                continue
            seen.add(members)
            checks.append((list(members), rng.randint(0, 1)))
        frozen = prepare(channels, checks)

        events = []

        def progress(phase, done, total):
            events.append((phase, done, total))

        with self.assertRaises(SolveCancelled) as ctx:
            solve_frozen(frozen, is_cancelled=lambda: True,
                         progress=progress)
        self.assertEqual(ctx.exception.phase, PHASE_LEFT)
        # 取消前至少经过一个检查点（进度先于取消判定回调）。
        self.assertTrue(events)
        self.assertEqual(events[0][0], PHASE_LEFT)

    def test_cancel_only_observed_at_checkpoints(self):
        # 小输入（l=3，左半仅 8 个向量 < 检查点步长）：
        # 取消在折半枚举尾检查点即被响应，结果不产出。
        channels = ["a", "b", "c", "d", "e", "f"]
        checks = [
            (["a", "c", "e"], 1),
            (["b", "d", "f"], 0),
            (["a", "b", "c"], 1),
        ]
        frozen = prepare(channels, checks)
        phases = []
        with self.assertRaises(SolveCancelled) as ctx:
            solve_frozen(
                frozen,
                is_cancelled=lambda: True,
                progress=lambda phase, d, t: phases.append(phase),
            )
        self.assertIn(ctx.exception.phase, (PHASE_LEFT, PHASE_RIGHT))
        # 取消阶段一定是先记录过进度的确定检查点。
        self.assertIn(ctx.exception.phase, phases)

    def test_cancel_flag_flips_during_merge(self):
        # 左半枚举阶段不取消，进入候选合并后于检查点取消。
        channels = [f"PX{j:02d}" for j in range(MAX_CHANNELS)]
        rng = random.Random(11)
        checks, seen = [], set()
        while len(checks) < MAX_CHECKS:
            k = rng.randint(2, 10)
            members = tuple(sorted(rng.sample(channels, k)))
            if members in seen:
                continue
            seen.add(members)
            checks.append((list(members), rng.randint(0, 1)))
        frozen = prepare(channels, checks)

        state = {"cancel": False}

        def is_cancelled():
            return state["cancel"]

        def progress(phase, done, total):
            if phase == PHASE_RIGHT:
                state["cancel"] = True

        with self.assertRaises(SolveCancelled) as ctx:
            solve_frozen(frozen, is_cancelled=is_cancelled,
                         progress=progress)
        self.assertEqual(ctx.exception.phase, PHASE_RIGHT)

    def test_no_cancel_matches_plain_solve(self):
        channels = ["CH0", "CH1", "CH2", "CH3", "CH4", "CH5"]
        checks = [
            (["CH0", "CH1", "CH3"], 1),
            (["CH2", "CH3", "CH4"], 1),
            (["CH3", "CH5"], 1),
            (["CH0", "CH2", "CH4"], 0),
        ]
        plain = solve(channels, checks)
        frozen = prepare(channels, checks)
        seen_progress = []
        r = solve_frozen(frozen, is_cancelled=lambda: False,
                         progress=lambda p, d, t: seen_progress.append(p))
        self.assertEqual(r.faulty, plain.faulty)
        self.assertEqual(r.weight, plain.weight)
        self.assertEqual(r.vector, plain.vector)
        # 两个阶段都经过检查点。
        self.assertIn(PHASE_LEFT, seen_progress)
        self.assertIn(PHASE_RIGHT, seen_progress)


if __name__ == "__main__":
    unittest.main(verbosity=2)
