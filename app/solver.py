"""
硅像素读出板噪声故障定位 —— 最小汉明重量异或求解器。

问题模型
========
工程师录入 n 个唯一通道 (2 <= n <= 36)，以及 m 条奇偶校验
(1 <= m <= 28)。每条校验引用一个非空、互不重复的通道集合，
并给出观测奇偶值 b_i ∈ {0, 1}。

故障向量 x ∈ {0,1}^n 为每个通道是否失效；校验 i 要求
    XOR_{j in 通道集合_i} x_j = b_i。

需求要求在所有可行解中寻找汉明重量最小者；重量相同时，按通道标识
升序排列形成的选择向量（即有序通道上的 0/1 向量）做标准字典序
裁决——从最低序号通道起逐位比较，先出现 0 的向量更小，等价于
“优先让标识更小的通道保持健康”。

算法（规定实现，禁止枚举完整向量 / 随机搜索 / 高斯消元）
=======================================================
按排序后的通道折半（meet-in-the-middle）：

1. 左半通道数 l = n // 2，右半 r = n - l。
2. 枚举左半的全部 2^l 个部分向量（n <= 36 时至多 2^18 = 262144，
   且每条校验至多 28 位，综合征可用一个 Python int 表示），
   建立“左半综合征 -> 该综合征下最优左半候选”的索引：
   同综合征取重量最小者，同重量取选择向量字典序最小者。
3. 右半按重量层枚举；右半综合征 sR 必须匹配目标综合征中的
   左半贡献缺口，即需要 sL = b XOR sR。在索引中精确查找，
   命中时合并重量 wL + wR；按总重量、再按完整选择向量字典序
   裁决全局最优。右半重量层一旦不低于已知最优总重量即剪枝。

两侧枚举的都是折半后的 *部分* 向量（总迭代 2^l + 2^r 量级），
算法从不枚举任何完整 2^n 故障向量，也不使用随机搜索或高斯消元。

取消
====
折半枚举（阶段 left_enumeration）与左右候选合并（阶段
candidate_merge）在固定粒度的确定检查点上先回写持久化进度，
再读取持久化的取消裁决；取消后不产出任何结论（不生成复核编号）。
"""

from __future__ import annotations

from dataclasses import dataclass

MIN_CHANNELS = 2
MAX_CHANNELS = 36
MAX_CHECKS = 28

# 取消检查点粒度：折半枚举 / 候选合并各自每处理若干个部分向量
# 响应一次取消，使取消延迟有确定上界且进度回写不会过于频繁。
LEFT_CHECKPOINT_STEP = 2048
RIGHT_CHECKPOINT_STEP = 1024

PHASE_LEFT = "left_enumeration"     # 左半折半枚举
PHASE_RIGHT = "candidate_merge"     # 左右候选合并


class ValidationError(ValueError):
    """输入非法；errors 为可定位的拒绝信息列表 (field, message)。"""

    def __init__(self, errors: list[tuple[str, str]]):
        self.errors = errors
        super().__init__("; ".join(f"{f}: {msg}" for f, msg in errors))


class SolveCancelled(Exception):
    """求解在确定检查点响应取消；phase 记录取消发生的阶段。"""

    def __init__(self, phase: str):
        self.phase = phase
        super().__init__(f"求解在检查点被取消（阶段 {phase}）")


@dataclass(frozen=True)
class SolveResult:
    feasible: bool
    channels: tuple[str, ...]          # 排序后的全部通道
    faulty: tuple[str, ...]            # 不可行时为 ()
    weight: int                        # 不可行时为 -1
    vector: tuple[int, ...]            # 不可行时为 ()
    left_size: int                     # 折半位置（便于测试/复算展示）
    left_index_size: int               # 左半综合征索引条目数


@dataclass(frozen=True)
class FrozenInput:
    """开始求解前冻结的输入：已排序通道、规范化校验与预计算位掩码。

    一旦构造完成，后续取消/重传/并发都不会再改变排序通道或校验；
    输入摘要也基于该规范化内容计算。
    """

    channels: tuple[str, ...]
    checks: tuple[tuple[tuple[str, ...], int], ...]
    rows: tuple[tuple[int, int], ...]
    target: int
    n: int
    m: int
    l: int
    left_masks: tuple[int, ...]
    right_masks: tuple[int, ...]
    full_mask: int


def _validate(channels, checks):
    """校验原始输入，返回 (有序通道列表, [(通道集合元组, 奇偶值)])。"""
    errors: list[tuple[str, str]] = []

    # ---- 通道 ----
    if not isinstance(channels, list):
        raise ValidationError([("channels", "通道必须以数组形式提供")])

    norm_channels: list[str] = []
    seen: set[str] = set()
    for idx, ch in enumerate(channels):
        field = f"channels[{idx}]"
        if not isinstance(ch, str):
            errors.append((field, "通道标识必须是字符串"))
            continue
        name = ch.strip()
        if not name:
            errors.append((field, "通道标识不得为空"))
            continue
        if name in seen:
            errors.append((field, f"通道 {name!r} 重复"))
            continue
        seen.add(name)
        norm_channels.append(name)

    if not (MIN_CHANNELS <= len(norm_channels) <= MAX_CHANNELS):
        errors.append((
            "channels",
            f"唯一通道数量须在 {MIN_CHANNELS}–{MAX_CHANNELS} 之间，"
            f"当前 {len(norm_channels)}",
        ))

    # ---- 校验 ----
    if not isinstance(checks, list):
        raise ValidationError([("checks", "校验必须以数组形式提供")])
    if not checks:
        errors.append(("checks", "至少需要 1 条校验"))
    if len(checks) > MAX_CHECKS:
        errors.append(("checks", f"校验数量不得超过 {MAX_CHECKS} 条，当前 {len(checks)}"))

    norm_checks: list[tuple[tuple[str, ...], int]] = []
    seen_sets: set[frozenset[str]] = set()
    for idx, ck in enumerate(checks):
        cfield = f"checks[{idx}]"
        # 同时接受 API 的对象形式 {"channels": [...], "parity": 0/1}
        # 与内部/测试使用的 (通道列表, 奇偶值) 元组形式。
        if isinstance(ck, dict):
            raw_chs = ck.get("channels")
            parity = ck.get("parity")
        elif isinstance(ck, (list, tuple)) and len(ck) == 2:
            raw_chs, parity = ck
        else:
            errors.append((cfield, "校验必须是对象或 [通道集合, 奇偶值] 数组"))
            continue
        members: list[str] = []
        member_seen: set[str] = set()
        if not isinstance(raw_chs, list) or not raw_chs:
            errors.append((f"{cfield}.channels", "引用通道集合不得为空"))
        else:
            for j, ch in enumerate(raw_chs):
                mfield = f"{cfield}.channels[{j}]"
                if not isinstance(ch, str) or not ch.strip():
                    errors.append((mfield, "通道引用必须是非空字符串"))
                    continue
                name = ch.strip()
                if name not in seen:
                    errors.append((mfield, f"引用了未定义通道 {name!r}"))
                if name in member_seen:
                    errors.append((mfield, f"通道 {name!r} 在本条校验中重复引用"))
                    continue
                member_seen.add(name)
                members.append(name)
        if not isinstance(parity, int) or isinstance(parity, bool) or parity not in (0, 1):
            errors.append((f"{cfield}.parity", "观测奇偶值必须是 0 或 1"))
            parity_val = -1
        else:
            parity_val = parity

        if members:
            key = frozenset(members)
            if key in seen_sets:
                errors.append((f"{cfield}.channels", "与另一条校验引用的通道集合重复"))
            else:
                seen_sets.add(key)
        if members and parity_val in (0, 1):
            norm_checks.append((tuple(members), parity_val))

    if errors:
        raise ValidationError(errors)

    norm_channels.sort()
    return norm_channels, norm_checks


def _build_rows(ordered_channels, checks):
    """把每条校验压缩成位掩码行（位 j 对应 ordered_channels[j]）与目标位。"""
    pos = {name: j for j, name in enumerate(ordered_channels)}
    rows: list[tuple[int, int]] = []
    for members, parity in checks:
        mask = 0
        for name in members:
            mask |= 1 << pos[name]
        rows.append((mask, parity))
    return rows


def prepare(channels, checks) -> FrozenInput:
    """校验并冻结输入（排序通道、规范化校验、预计算综合征掩码）。

    必须在开始求解前调用：取消/重传/并发过程中排序通道与校验集合
    不再变化；输入摘要也基于此处的规范化结果。
    """
    ordered_channels, norm_checks = _validate(channels, checks)
    norm_checks_t = tuple((tuple(sorted(members)), parity)
                          for members, parity in norm_checks)
    rows = tuple(_build_rows(ordered_channels, norm_checks_t))
    n = len(ordered_channels)
    m = len(rows)

    target = 0
    for i, (_, parity) in enumerate(rows):
        target |= parity << i

    l = n // 2
    left_masks = [0] * l
    right_masks = [0] * (n - l)
    for i, (mask, _) in enumerate(rows):
        for j in range(l):
            if (mask >> j) & 1:
                left_masks[j] |= 1 << i
        for j in range(n - l):
            if (mask >> (l + j)) & 1:
                right_masks[j] |= 1 << i

    return FrozenInput(
        channels=tuple(ordered_channels),
        checks=norm_checks_t,
        rows=rows,
        target=target,
        n=n,
        m=m,
        l=l,
        left_masks=tuple(left_masks),
        right_masks=tuple(right_masks),
        full_mask=(1 << m) - 1,
    )


def _reverse_bits(value: int, width: int) -> int:
    """把 width 位整数位序反转。

    通道升序选择向量 (x_0,...,x_{k-1}) 的字典序，恰好等于把
    x_0 放在最高位后的整数大小关系；故位反转整数可作为
    “同重量下字典序”的单调比较键。
    """
    out = 0
    for _ in range(width):
        out = (out << 1) | (value & 1)
        value >>= 1
    return out


def _combinations_by_weight(width: int, weight: int):
    """生成恰好含 weight 个置位位的 width 位整数。"""
    chosen: list[int] = []

    def gen(start: int):
        if len(chosen) == weight:
            v = 0
            for p in chosen:
                v |= 1 << p
            yield v
            return
        for k in range(start, width - (weight - len(chosen)) + 1):
            chosen.append(k)
            yield from gen(k + 1)
            chosen.pop()

    yield from gen(0)


def _syndrome(vec: int, masks: tuple[int, ...]) -> int:
    s = 0
    while vec:
        low = vec & -vec
        s ^= masks[low.bit_length() - 1]
        vec ^= low
    return s


def _checkpoint(phase: str, processed: int, total: int,
                is_cancelled, progress) -> None:
    """确定检查点：先持久化进度，再读取持久化的取消裁决。

    任何阶段跳转都只发生在检查点上——折半枚举与左右候选合并的循环
    主体不会在任意位置中断，因此取消点是可复现、可定位的。
    """
    if progress is not None:
        progress(phase, processed, total)
    if is_cancelled is not None and is_cancelled():
        raise SolveCancelled(phase)


def solve_frozen(frozen: FrozenInput, is_cancelled=None, progress=None) -> SolveResult:
    """对已冻结输入执行折半综合征索引 + 两侧候选精确合并。

    is_cancelled() 在每个确定检查点被读取；返回真则抛出
    SolveCancelled，且不会产出任何结论。progress(phase, done, total)
    在同一检查点先于取消判定被回调，供持久化进度。
    """
    n, m, l = frozen.n, frozen.m, frozen.l
    r = n - l
    target, full_mask = frozen.target, frozen.full_mask
    left_masks, right_masks = frozen.left_masks, frozen.right_masks

    # ------------------------------------------------------------------
    # 步骤 1：左半折半，枚举全部 2^l 个部分向量并建立综合征索引。
    # 每个综合征只保留：重量最小 -> 同重量选择向量字典序最小 的候选。
    # ------------------------------------------------------------------
    index: dict[int, tuple[int, int, int]] = {}
    left_total = 1 << l
    for vec in range(left_total):
        if vec % LEFT_CHECKPOINT_STEP == 0:
            _checkpoint(PHASE_LEFT, vec, left_total, is_cancelled, progress)
        s = _syndrome(vec, left_masks)
        w = vec.bit_count()
        key = _reverse_bits(vec, l)
        kept = index.get(s)
        if kept is None or w < kept[0] or (w == kept[0] and key < kept[2]):
            index[s] = (w, vec, key)
    _checkpoint(PHASE_LEFT, left_total, left_total, is_cancelled, progress)

    # ------------------------------------------------------------------
    # 步骤 2：右半按重量层枚举，精确查找 sL = target XOR sR，
    # 合并两侧候选并裁决全局 (总重量, 完整选择向量字典序) 最优。
    # ------------------------------------------------------------------
    best_weight = n + 1
    best_full = -1
    best_key = -1

    right_total = 1 << r
    right_done = 0
    for wR in range(r + 1):
        # wL 最小为 0；只有 wR 严格大于已知最优总重量时该层才无机会。
        # wR == best_weight 的层仍可能由 wL=0 给出同重量、字典序更小的解。
        if wR > best_weight:
            break
        for vecR in _combinations_by_weight(r, wR):
            if right_done % RIGHT_CHECKPOINT_STEP == 0:
                _checkpoint(PHASE_RIGHT, right_done, right_total,
                            is_cancelled, progress)
            right_done += 1
            sR = _syndrome(vecR, right_masks)
            need = (target ^ sR) & full_mask
            kept = index.get(need)
            if kept is None:
                continue
            wL, vecL, _ = kept
            total = wL + wR
            if total > best_weight:
                continue
            full = vecL | (vecR << l)
            if total < best_weight:
                best_weight = total
                best_full = full
                best_key = _reverse_bits(full, n)
            else:
                key = _reverse_bits(full, n)
                if key < best_key:
                    best_full = full
                    best_key = key
    _checkpoint(PHASE_RIGHT, right_done, right_total, is_cancelled, progress)

    if best_full < 0:
        return SolveResult(
            feasible=False,
            channels=frozen.channels,
            faulty=(),
            weight=-1,
            vector=(),
            left_size=l,
            left_index_size=len(index),
        )

    vector = tuple((best_full >> j) & 1 for j in range(n))
    faulty = tuple(
        frozen.channels[j] for j in range(n) if (best_full >> j) & 1
    )
    return SolveResult(
        feasible=True,
        channels=frozen.channels,
        faulty=faulty,
        weight=best_weight,
        vector=vector,
        left_size=l,
        left_index_size=len(index),
    )


def solve(channels, checks, is_cancelled=None, progress=None) -> SolveResult:
    """校验/冻结输入后求解（测试与简单调用使用的便捷封装）。"""
    return solve_frozen(prepare(channels, checks), is_cancelled, progress)


def recompute(ordered_channels, checks, vector) -> list[dict]:
    """逐校验复算：用给定选择向量重算每条 XOR，并与观测值比对。"""
    values = {ch: vector[j] for j, ch in enumerate(ordered_channels)}
    results = []
    for members, parity in checks:
        got = 0
        for name in members:
            got ^= values[name]
        results.append({
            "members": list(members),
            "observed": parity,
            "recomputed": got,
            "pass": got == parity,
        })
    return results
