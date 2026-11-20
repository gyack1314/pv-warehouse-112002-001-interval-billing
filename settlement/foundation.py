"""结算明细仓基础构件：金额舍入、市场时钟、版本簿与交割网格。

规则锚点：
- 金额用十进制；价为元/MWh；单方向逐格按分取整，半分远离零。
- 时段按交割点市场时钟与产品规则确定；夏令时回拨后相同钟表时分不合并。
- 表底、分量、价格均按业务键多版本留存；同版本同键冲突显式隔离，
  绝不按到达顺序选值。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP

CENT = Decimal("0.01")
KWH_PER_MWH = Decimal(1000)


def dec(text) -> Decimal:
    return Decimal(str(text).strip())


def amount_for(kwh: Decimal, yuan_per_mwh: Decimal) -> Decimal:
    """单方向逐格金额：kWh × 元/MWh，按分取整，半分远离零。"""
    return (kwh * yuan_per_mwh / KWH_PER_MWH).quantize(CENT, rounding=ROUND_HALF_UP)


def parse_ts(text: str) -> datetime:
    ts = datetime.fromisoformat(str(text).strip())
    if ts.tzinfo is None:
        raise ValueError(f"时间缺少时区偏移: {text!r}")
    return ts


def ts_key(ts: datetime) -> str:
    """区间身份按瞬时确定；夏令时回拨后相同钟表时分对应不同瞬时，不合并。"""
    return ts.astimezone(timezone.utc).isoformat()


@dataclass(frozen=True)
class RowRef:
    """指向某张来源表第几行、第几波次到达，供凭单回查。"""

    table: str
    row: int
    wave: int

    def __str__(self) -> str:
        return f"{self.table}#{self.row}@w{self.wave}"


@dataclass(frozen=True)
class Event:
    wave: int
    kind: str
    detail: str


class State:
    """格子状态：缺格、冲突、已确认零交易等必须可区分。"""

    OK = "ok"
    ZERO_CONFIRMED = "zero_confirmed"  # 已确认零交易：有证据支撑的零量
    MISSING_QUANTITY = "missing_quantity"  # 缺量
    CONFLICT_QUANTITY = "conflict_quantity"  # 量有同版冲突且无可用版本
    NO_AUTHORITY = "no_authority"  # 无归属窗口
    MISSING_PRICE = "missing_price"  # 缺价
    ESTIMATED_PRICE = "estimated_price"  # 临时价：可预估，不可出账
    CONFLICT_PRICE = "conflict_price"  # 价有同版冲突且无可用版本


STATE_LABELS = {
    State.OK: "齐备",
    State.ZERO_CONFIRMED: "已确认零交易",
    State.MISSING_QUANTITY: "缺量",
    State.CONFLICT_QUANTITY: "量冲突",
    State.NO_AUTHORITY: "无归属",
    State.MISSING_PRICE: "缺价",
    State.ESTIMATED_PRICE: "临时价",
    State.CONFLICT_PRICE: "价冲突",
}


@dataclass(frozen=True)
class PriceUsed:
    """某格某方向实际采用的价格版本，凭单可回查到行。"""

    point: str
    start: datetime
    direction: str
    rev: int
    status: str
    yuan: Decimal
    ref: RowRef


@dataclass(frozen=True)
class Revision:
    rev: int
    value: tuple
    ref: RowRef
    quarantined: bool = False


@dataclass
class Quarantine:
    """同版本同键冲突的显式隔离记录。"""

    book: str
    key: tuple
    rev: int
    refs: list
    values: list
    wave: int


class RevisionBook:
    """同一业务键的多版本留存。

    - 同键不同版本号：并存，取最高有效版本；
    - 同键同版本同值：重复投递，忽略；
    - 同键同版本不同值：显式隔离，双方均不可用，绝不按到达顺序选值。
    """

    def __init__(self, kind: str):
        self.kind = kind
        self._slots: dict[tuple, dict[int, Revision]] = {}
        self.quarantines: list[Quarantine] = []

    def add(self, key: tuple, rev: int, value: tuple, ref: RowRef, wave: int) -> list[Event]:
        slot = self._slots.setdefault(key, {})
        if rev in slot:
            current = slot[rev]
            if current.quarantined:
                quarantine = self._find_quarantine(key, rev)
                if value not in quarantine.values:
                    quarantine.values.append(value)
                quarantine.refs.append(ref)
                return [Event(wave, "QUARANTINE_ROW", f"{self.kind}{key} rev{rev} 已隔离，追加冲突记录 {ref}")]
            if current.value == value:
                return [Event(wave, "DUPLICATE_ROW", f"{self.kind}{key} rev{rev} 重复投递 {ref}，忽略")]
            slot[rev] = Revision(current.rev, current.value, current.ref, quarantined=True)
            self.quarantines.append(
                Quarantine(self.kind, key, rev, [current.ref, ref], [current.value, value], wave)
            )
            return [
                Event(
                    wave,
                    "QUARANTINE",
                    f"{self.kind}{key} rev{rev} 同版冲突，显式隔离 {current.ref} 与 {ref}，该版本不可用",
                )
            ]
        slot[rev] = Revision(rev, value, ref)
        return []

    def _find_quarantine(self, key: tuple, rev: int) -> Quarantine:
        for quarantine in self.quarantines:
            if quarantine.key == key and quarantine.rev == rev:
                return quarantine
        raise KeyError((key, rev))

    def latest(self, key: tuple) -> Revision | None:
        """最高有效版本；被隔离的版本不参与。"""
        slot = self._slots.get(key)
        if not slot:
            return None
        valid = [r for r in slot.values() if not r.quarantined]
        return max(valid, key=lambda r: r.rev) if valid else None

    def get(self, key: tuple, rev: int) -> Revision | None:
        """取指定版本，仅当存在且未被隔离；用于已签格子的证据核验。"""
        revision = self._slots.get(key, {}).get(rev)
        return revision if revision and not revision.quarantined else None

    def has_quarantine(self, key: tuple) -> bool:
        return any(q.key == key for q in self.quarantines)

    def keys(self) -> list:
        return list(self._slots)


@dataclass(frozen=True)
class Interval:
    """一个交割格：左闭右开，身份由瞬时决定而非钟表时分。"""

    point: str
    period: str
    index: int
    start: datetime
    end: datetime

    @property
    def minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)


def build_grid(calendar_rows: list[dict]) -> dict[tuple, list[Interval]]:
    """按市场日历切格：窗口边界即产品口径切换点（如十五分钟切五分钟）。"""
    spans: dict[tuple, list] = {}
    for row in calendar_rows:
        point, period = row["point"], row["period"]
        start, end = parse_ts(row["start"]), parse_ts(row["end"])
        step = int(row["minutes"])
        if not end > start:
            raise ValueError(f"营业窗口无效: {row}")
        width = (end - start).total_seconds()
        if width % (step * 60):
            raise ValueError(f"营业窗口不是 {step} 分钟的整数倍: {row}")
        cursor = start
        while cursor < end:
            nxt = cursor + timedelta(minutes=step)
            if nxt >= end:
                nxt = end  # 末格边界沿用日历原文，保留回拨后的偏移表示
            spans.setdefault((point, period), []).append((cursor, nxt))
            cursor = nxt
    grid = {}
    for key, items in spans.items():
        items.sort(key=lambda s: s[0].astimezone(timezone.utc))
        for prev, nxt in zip(items, items[1:]):
            if prev[1] > nxt[0]:
                raise ValueError(f"营业窗口重叠: {key}")
        grid[key] = [Interval(key[0], key[1], i, s, e) for i, (s, e) in enumerate(items)]
    return grid
