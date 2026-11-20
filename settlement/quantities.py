"""表底衔接与逐格分量。

- 原始表底是累积值；只有同一只表内相邻表底的差才是增量，绝不跨表相减。
- 换表与归零只能依据交接记录（meter_authority）衔接；备用表只在授权
  期间替代指定方向的主表。
- 跨过时段边界的增量，只有与表底区间对齐、铺满且合计相等的区间分量
  才能拆；否则整段只进对账，不进收费。
- 方向相反或不满足五分钟口径的整刻钟量可核对，但不得进五分钟收费。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .foundation import RevisionBook, RowRef, parse_ts


@dataclass(frozen=True)
class AuthorityWindow:
    """交接/授权记录：某方向某时段由哪只表承担，换表链条如何衔接。"""

    point: str
    direction: str
    start: object
    end: object
    meter: str
    kind: str  # main / authorized_replacement
    preceding: str | None
    ref: RowRef


@dataclass(frozen=True)
class ReadingUsed:
    meter: str
    at: object
    rev: int
    kwh: Decimal
    ref: RowRef


@dataclass(frozen=True)
class ProofUsed:
    meter: str
    start: object
    end: object
    rev: int
    kwh: Decimal
    ref: RowRef


@dataclass(frozen=True)
class QuantityProof:
    """一格用量的完整归属证据：哪只表、哪对表底、哪些分量、哪版归属。"""

    meter: str
    authority: AuthorityWindow
    readings: tuple  # 同一只表的相邻表底对
    proofs: tuple  # 跨界拆分用到的可核验分量（无分量则为空）


@dataclass(frozen=True)
class Quantity:
    kwh: Decimal
    proof: QuantityProof


@dataclass(frozen=True)
class QuantityProblem:
    state: str  # State.MISSING_QUANTITY / CONFLICT_QUANTITY / NO_AUTHORITY
    note: str


@dataclass(frozen=True)
class Segment:
    """同一只表相邻表底构成的增量段。"""

    meter: str
    start: object
    end: object
    kwh: Decimal
    lo: ReadingUsed
    hi: ReadingUsed


@dataclass(frozen=True)
class ReconOnly:
    """只能对账、不能进收费的增量（跨界未拆、口径不符、表底回退等）。"""

    meter: str
    start: object
    end: object
    kwh: Decimal
    reason: str
    refs: tuple


def meter_segments(meter: str, registers: RevisionBook) -> tuple[list[Segment], list[ReconOnly]]:
    """由一只表的有效表底序列生成增量段；回退只认交接记录，否则作废。"""
    points: list[ReadingUsed] = []
    for key in registers.keys():
        name, _at_key = key
        if name != meter:
            continue
        revision = registers.latest(key)
        if revision is None:
            continue
        kwh, at_iso = revision.value
        points.append(ReadingUsed(meter, parse_ts(at_iso), revision.rev, kwh, revision.ref))
    points.sort(key=lambda r: r.at)
    segments: list[Segment] = []
    problems: list[ReconOnly] = []
    for lo, hi in zip(points, points[1:]):
        delta = hi.kwh - lo.kwh
        if delta < 0:
            problems.append(
                ReconOnly(meter, lo.at, hi.at, delta, "表底回退且无交接记录，增量作废", (lo.ref, hi.ref))
            )
            continue
        segments.append(Segment(meter, lo.at, hi.at, delta, lo, hi))
    return segments, problems


def allocate_segment(
    seg: Segment, intervals: list, proofs: RevisionBook
) -> tuple[dict, list[ReconOnly]]:
    """把一段表底增量落到交割格上。

    恰好对齐一格：直接落格。跨越多格：只有可核验的区间分量（与格对齐、
    铺满、合计等于表底增量）才能拆；不满足则整段只进对账。
    """
    refs = (seg.lo.ref, seg.hi.ref)
    inside = [iv for iv in intervals if iv.start >= seg.start and iv.end <= seg.end]
    if not inside:
        return {}, [ReconOnly(seg.meter, seg.start, seg.end, seg.kwh, "表底增量不在营业窗口内", refs)]
    if len(inside) == 1 and inside[0].start == seg.start and inside[0].end == seg.end:
        return {inside[0].index: (seg.kwh, (), (seg.lo, seg.hi))}, []

    components: list[ProofUsed] = []
    coarse: list[ReconOnly] = []
    for key in proofs.keys():
        name, _s_key, _e_key = key
        if name != seg.meter:
            continue
        revision = proofs.latest(key)
        if revision is None:
            continue
        kwh, s_iso, e_iso = revision.value
        start, end = parse_ts(s_iso), parse_ts(e_iso)
        if not (start >= seg.start and end <= seg.end):
            continue
        proof = ProofUsed(name, start, end, revision.rev, kwh, revision.ref)
        if any(iv.start == start and iv.end == end for iv in inside):
            components.append(proof)
        else:
            coarse.append(
                ReconOnly(name, start, end, kwh, "分量未对齐格子口径（如整刻钟量），只进对账", (revision.ref,))
            )

    by_index = {}
    for proof in components:
        for iv in inside:
            if iv.start == proof.start and iv.end == proof.end:
                by_index[iv.index] = proof
    if set(by_index) != {iv.index for iv in inside}:
        recon = [ReconOnly(seg.meter, seg.start, seg.end, seg.kwh, "跨界增量缺可核验分量，不得拆入收费", refs)]
        return {}, recon + coarse
    total = sum((p.kwh for p in by_index.values()), Decimal(0))
    if total != seg.kwh:
        recon = [
            ReconOnly(
                seg.meter, seg.start, seg.end, seg.kwh, f"分量合计 {total} 与表底增量 {seg.kwh} 不符", refs
            )
        ]
        return {}, recon + coarse
    return {idx: (p.kwh, (p,), (seg.lo, seg.hi)) for idx, p in by_index.items()}, coarse


def assign_quantities(
    intervals: list,
    direction: str,
    authorities: list[AuthorityWindow],
    allocations: dict,
    registers: RevisionBook,
) -> dict:
    """逐格定向：格子必须落在唯一归属窗口内，且只能取该窗口授权表的量。"""
    out = {}
    for iv in intervals:
        wins = [w for w in authorities if w.direction == direction and w.start <= iv.start and w.end >= iv.end]
        if not wins:
            out[iv.index] = QuantityProblem("no_authority", "该方向无归属窗口")
            continue
        if len(wins) > 1:
            out[iv.index] = QuantityProblem("conflict_quantity", "归属窗口重叠")
            continue
        win = wins[0]
        alloc = allocations.get(win.meter, {})
        if iv.index in alloc:
            kwh, proof_rows, readings = alloc[iv.index]
            out[iv.index] = Quantity(kwh, QuantityProof(win.meter, win, tuple(readings), tuple(proof_rows)))
        else:
            state = "conflict_quantity" if _quarantine_near(registers, win.meter, iv) else "missing_quantity"
            note = "授权表在该格的量有同版冲突且无可用版本" if state == "conflict_quantity" else "授权表在该格无可用量"
            out[iv.index] = QuantityProblem(state, note)
    return out


def _quarantine_near(registers: RevisionBook, meter: str, iv) -> bool:
    for quarantine in registers.quarantines:
        name, at_key = quarantine.key
        if name != meter:
            continue
        at = parse_ts(at_key)
        if iv.start <= at <= iv.end:
            return True
    return False
