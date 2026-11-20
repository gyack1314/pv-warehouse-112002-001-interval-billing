"""结算明细仓引擎：资料入仓、逐波次处理、出账签收与回查视图。

处理约定：同一波次内先落全部资料修订，再处理财务回执，最后尝试出账；
波次内行序不影响结果。分区只有在两方向量价齐备（正式价）时才发出；
已发待签期间收到修订保留在途单；确认后按同一格子对比已确认基线产生
差额批次；未签在途单只有明确拒收才终止并重新出完整分区；修订使已签
格子失去证据时挂待决，资料补齐前不得冲零。
"""
from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from .foundation import (
    Event,
    Interval,
    PriceUsed,
    RevisionBook,
    RowRef,
    State,
    amount_for,
    build_grid,
    dec,
    parse_ts,
    ts_key,
)
from .quantities import (
    AuthorityWindow,
    Quantity,
    QuantityProblem,
    QuantityProof,
    allocate_segment,
    assign_quantities,
    meter_segments,
)

DIRECTIONS = ("import", "export")
ROLE = {"import": "应付", "export": "应收"}
DIR_LABEL = {"import": "购电", "export": "售电"}

DATA_TABLES = ("meter_registers", "interval_proof", "meter_authority", "prices")


def alias_for(seq: int) -> str:
    """财务端送单别名：A 为实际首张，B 为下一张，依此类推。"""
    out = ""
    n = seq
    while n:
        n, r = divmod(n - 1, 26)
        out = chr(65 + r) + out
    return out


@dataclass
class Cell:
    """当前口径下某格某方向的量价状态。"""

    interval: Interval
    direction: str
    qty_state: str
    price_state: str
    qty: Decimal | None = None
    quantity_proof: QuantityProof | None = None
    price: PriceUsed | None = None
    amount: Decimal | None = None
    estimate_amount: Decimal | None = None
    note: str = ""

    @property
    def state(self) -> str:
        if self.qty_state not in (State.OK, State.ZERO_CONFIRMED):
            return self.qty_state
        if self.price_state != State.OK:
            return self.price_state
        return self.qty_state

    @property
    def billable(self) -> bool:
        return self.qty_state in (State.OK, State.ZERO_CONFIRMED) and self.price_state == State.OK


@dataclass
class DispatchLine:
    interval: Interval
    direction: str
    qty: Decimal
    yuan: Decimal
    amount: Decimal
    price_rev: int
    delta_qty: Decimal | None
    delta_amount: Decimal | None
    quantity_proof: QuantityProof | None
    price: PriceUsed | None
    baseline_source: str | None


@dataclass
class Dispatch:
    """一份实际发出的送单（完整分区或差额批次），签收针对整份。"""

    id: str
    seq: int
    alias: str
    kind: str  # full / diff
    point: str
    period: str
    lines: list
    issued_wave: int
    state: str = "issued"  # issued / confirmed / terminated
    decided_wave: int | None = None


@dataclass
class BaselineCell:
    """已确认基线的一格；同一个确认最终只改变一次基线。"""

    qty: Decimal
    yuan: Decimal
    amount: Decimal
    price_rev: int
    source_dispatch: str
    quantity_proof: QuantityProof | None
    price: PriceUsed | None
    pending: bool = False  # 修订使已签格子失去证据 → 挂待决，不得冲零


def reading_dict(r) -> dict:
    return {"meter": r.meter, "at": r.at.isoformat(), "rev": r.rev, "kwh": str(r.kwh), "ref": str(r.ref)}


def proof_row_dict(p) -> dict:
    return {
        "meter": p.meter,
        "start": p.start.isoformat(),
        "end": p.end.isoformat(),
        "rev": p.rev,
        "kwh": str(p.kwh),
        "ref": str(p.ref),
    }


def authority_dict(w: AuthorityWindow) -> dict:
    return {
        "meter": w.meter,
        "kind": w.kind,
        "preceding": w.preceding,
        "start": w.start.isoformat(),
        "end": w.end.isoformat(),
        "ref": str(w.ref),
    }


def quantity_dict(q: QuantityProof | None) -> dict | None:
    if q is None:
        return None
    return {
        "meter": q.meter,
        "authority": authority_dict(q.authority),
        "readings": [reading_dict(r) for r in q.readings],
        "proofs": [proof_row_dict(p) for p in q.proofs],
    }


def price_dict(p: PriceUsed | None) -> dict | None:
    if p is None:
        return None
    return {
        "point": p.point,
        "start": p.start.isoformat(),
        "direction": p.direction,
        "rev": p.rev,
        "status": p.status,
        "yuan_per_mwh": str(p.yuan),
        "ref": str(p.ref),
    }


class Warehouse:
    """结算明细仓：从资料入仓到财务签收的全流程。"""

    def __init__(self) -> None:
        self.registers = RevisionBook("meter_registers")
        self.proofs = RevisionBook("interval_proof")
        self.prices = RevisionBook("prices")
        self.authorities: list[AuthorityWindow] = []
        self._authority_index: dict[tuple, AuthorityWindow] = {}
        self._authority_bad: set[tuple] = set()
        self.calendar_rows: list[dict] = []
        self.grid: dict[tuple, list[Interval]] = {}
        self.landed: list[tuple[str, dict, RowRef]] = []
        self._row_counts: dict[str, int] = defaultdict(int)
        self.pending_rows: dict[int, list] = defaultdict(list)
        self.pending_receipts: dict[int, list] = defaultdict(list)
        self.events: list[Event] = []
        self.dispatches: list[Dispatch] = []
        self.aliases: dict[str, Dispatch] = {}
        self.receipts_seen: dict[str, tuple] = {}
        self.receipt_log: list[dict] = []
        self.baseline: dict[tuple, BaselineCell] = {}
        self.cells: dict[tuple, list[Cell]] = {}
        self.recon_only: dict[tuple, list] = defaultdict(list)
        self._needs_full: set[tuple] = set()
        self._seq = 0

    # ---------------------------------------------------------------- 入仓

    def land(self, table: str, row: dict, wave: int | None = None) -> RowRef:
        row = dict(row)
        self._row_counts[table] += 1
        w = wave if wave is not None else int(row.get("wave") or 0)
        ref = RowRef(table, self._row_counts[table], w)
        self.landed.append((table, row, ref))
        if table == "market_calendar":
            self.calendar_rows.append(row)
            self.grid = build_grid(self.calendar_rows)
        elif table == "ledger_receipts":
            self.pending_receipts[w].append((row, ref))
        else:
            self.pending_rows[w].append((table, row, ref))
        return ref

    def load_pack(self, directory) -> None:
        """校验资料包（结构、摘要、跨表引用）后入仓。"""
        from inspect_inputs import inspect_inputs

        directory = Path(directory)
        inspect_inputs(directory)
        for table in ("market_calendar",) + DATA_TABLES + ("ledger_receipts",):
            with (directory / f"{table}.csv").open(encoding="utf-8", newline="") as stream:
                for row in csv.DictReader(stream):
                    self.land(table, row)
        self._event(0, "LAND", f"资料包 {directory} 校验通过并入仓 {len(self.landed)} 行")

    # ------------------------------------------------------------ 波次处理

    def run(self) -> None:
        for wave in sorted(set(self.pending_rows) | set(self.pending_receipts)):
            self.apply_wave(wave)

    def apply_wave(self, wave: int) -> None:
        self._event(wave, "WAVE", f"第 {wave} 波次开始处理")
        for table, row, ref in self.pending_rows.pop(wave, []):
            self._apply_row(table, row, ref, wave)
        self._recompute()
        self._check_baseline_evidence(wave)
        for row, ref in self.pending_receipts.pop(wave, []):
            self._apply_receipt(row, ref, wave)
        for key in sorted(self.grid):
            self._maybe_issue(key, wave)

    def _apply_row(self, table: str, row: dict, ref: RowRef, wave: int) -> None:
        try:
            if table == "meter_registers":
                key = (row["meter"], ts_key(parse_ts(row["at"])))
                events = self.registers.add(key, int(row["rev"]), (dec(row["kwh"]), row["at"]), ref, wave)
            elif table == "interval_proof":
                key = (row["meter"], ts_key(parse_ts(row["start"])), ts_key(parse_ts(row["end"])))
                value = (dec(row["kwh"]), row["start"], row["end"])
                events = self.proofs.add(key, int(row["rev"]), value, ref, wave)
            elif table == "prices":
                key = (row["point"], ts_key(parse_ts(row["start"])), row["direction"])
                events = self.prices.add(key, int(row["rev"]), (row["status"], dec(row["yuan_per_mwh"])), ref, wave)
            elif table == "meter_authority":
                events = self._add_authority(row, ref, wave)
            else:
                events = [Event(wave, "BAD_ROW", f"未知表 {table}，行已隔离 {ref}")]
        except Exception as exc:  # 坏行隔离，不拖垮整波
            events = [Event(wave, "BAD_ROW", f"{table} 行无法入仓 {ref}: {exc}")]
        self.events.extend(events)

    def _add_authority(self, row: dict, ref: RowRef, wave: int) -> list[Event]:
        key = (row["point"], row["direction"], ts_key(parse_ts(row["start"])), ts_key(parse_ts(row["end"])))
        window = AuthorityWindow(
            point=row["point"],
            direction=row["direction"],
            start=parse_ts(row["start"]),
            end=parse_ts(row["end"]),
            meter=row["meter"],
            kind=row["kind"],
            preceding=row.get("preceding_meter") or None,
            ref=ref,
        )
        if key in self._authority_index:
            existing = self._authority_index[key]
            same = (existing.meter, existing.kind, existing.preceding) == (window.meter, window.kind, window.preceding)
            if same:
                return [Event(wave, "DUPLICATE_ROW", f"meter_authority{key} 重复投递 {ref}，忽略")]
            self._authority_bad.add(key)
            return [Event(wave, "AUTHORITY_CONFLICT", f"meter_authority{key} 归属冲突，{ref} 与 {existing.ref} 均停用")]
        events = []
        if window.kind == "authorized_replacement":
            chain_ok = any(
                w.meter == window.preceding and w.direction == window.direction and w.end <= window.start
                for w in self.authorities
            )
            if not chain_ok:
                events.append(Event(wave, "AUTHORITY_CHAIN", f"备用表 {window.meter} 的交接记录无法衔接 {window.preceding}"))
        self._authority_index[key] = window
        self.authorities.append(window)
        return events

    # ------------------------------------------------------------ 口径计算

    def _recompute(self) -> None:
        for (point, period), intervals in self.grid.items():
            auth = [
                w
                for w in self.authorities
                if w.point == point and self._authority_key(w) not in self._authority_bad
            ]
            allocations: dict[str, dict] = {}
            recon: list = []
            for meter in sorted({w.meter for w in auth}):
                segments, problems = meter_segments(meter, self.registers)
                recon.extend(problems)
                for seg in segments:
                    got, recon_items = allocate_segment(seg, intervals, self.proofs)
                    allocations.setdefault(meter, {}).update(got)
                    recon.extend(recon_items)
            self.recon_only[(point, period)] = recon
            cells: list[Cell] = []
            for direction in DIRECTIONS:
                quantities = assign_quantities(intervals, direction, auth, allocations, self.registers)
                for iv in intervals:
                    cells.append(self._build_cell(point, iv, direction, quantities[iv.index]))
            cells.sort(key=lambda c: (c.interval.index, c.direction))
            self.cells[(point, period)] = cells

    @staticmethod
    def _authority_key(w: AuthorityWindow) -> tuple:
        return (w.point, w.direction, ts_key(w.start), ts_key(w.end))

    def _build_cell(self, point: str, iv: Interval, direction: str, outcome) -> Cell:
        cell = Cell(interval=iv, direction=direction, qty_state=State.OK, price_state=State.OK)
        if isinstance(outcome, Quantity):
            cell.qty = outcome.kwh
            cell.quantity_proof = outcome.proof
            cell.qty_state = State.ZERO_CONFIRMED if outcome.kwh == 0 else State.OK
        else:
            cell.qty_state = outcome.state
            cell.note = outcome.note
        key = (point, ts_key(iv.start), direction)
        revision = self.prices.latest(key)
        if revision is None:
            cell.price_state = State.CONFLICT_PRICE if self.prices.has_quarantine(key) else State.MISSING_PRICE
        else:
            status, yuan = revision.value
            cell.price = PriceUsed(point, iv.start, direction, revision.rev, status, yuan, revision.ref)
            cell.price_state = State.OK if status == "final" else State.ESTIMATED_PRICE
        if cell.billable:
            cell.amount = amount_for(cell.qty, cell.price.yuan)
        elif cell.qty is not None and cell.price is not None:
            cell.estimate_amount = amount_for(cell.qty, cell.price.yuan)  # 临时价只做预估
        return cell

    # ------------------------------------------------------------ 出账与签收

    def _maybe_issue(self, key: tuple, wave: int) -> None:
        cells = self.cells.get(key)
        if not cells:
            return
        in_flight = self._in_flight(key)
        if in_flight:
            if self._differs_from_dispatch(in_flight):
                self._event(
                    wave,
                    "IN_FLIGHT_KEPT",
                    f"修订到达，在途单 {in_flight.alias}/{in_flight.id} 保留，待签收或明确拒收后再处理",
                )
            return
        base_exists = self._baseline_exists(key)
        if not base_exists or key in self._needs_full:
            if all(c.billable for c in cells):
                self._issue(key, "full", cells, wave)
                self._needs_full.discard(key)
            else:
                bad = ", ".join(f"格{c.interval.index}/{c.direction}:{c.state}" for c in cells if not c.billable)
                self._event(wave, "GATE_BLOCKED", f"{key[0]}/{key[1]} 两方向量价未齐备，分区不发: {bad}")
            return
        changed, blocked = self._diff_cells(key)
        if changed:
            self._issue(key, "diff", changed, wave)
        if blocked:
            self._event(wave, "CELLS_BLOCKED", f"{key[0]}/{key[1]} {len(blocked)} 格待决或缺证据，留在仓内不进差额")

    def _diff_cells(self, key: tuple) -> tuple[list, list]:
        point, period = key
        changed, blocked = [], []
        for c in self.cells.get(key, []):
            base = self.baseline.get((point, period, c.interval.index, c.direction))
            if base is None:
                if c.billable:
                    changed.append(c)
                continue
            if base.pending:
                blocked.append(c)  # 待决格：资料补齐前不动，更不冲零
                continue
            differs = c.qty != base.qty or c.price is None or c.price.yuan != base.yuan
            if differs:
                (changed if c.billable else blocked).append(c)
        return changed, blocked

    def _issue(self, key: tuple, kind: str, cells: list, wave: int) -> Dispatch:
        point, period = key
        self._seq += 1
        alias = alias_for(self._seq)
        dispatch = Dispatch(
            id=f"{point}-{period}-{self._seq:03d}",
            seq=self._seq,
            alias=alias,
            kind=kind,
            point=point,
            period=period,
            lines=[],
            issued_wave=wave,
        )
        for c in cells:
            base = self.baseline.get((point, period, c.interval.index, c.direction))
            dispatch.lines.append(
                DispatchLine(
                    interval=c.interval,
                    direction=c.direction,
                    qty=c.qty,
                    yuan=c.price.yuan,
                    amount=c.amount,
                    price_rev=c.price.rev,
                    delta_qty=(c.qty - base.qty) if base else None,
                    delta_amount=(c.amount - base.amount) if base else None,
                    quantity_proof=c.quantity_proof,
                    price=c.price,
                    baseline_source=base.source_dispatch if base else None,
                )
            )
        self.dispatches.append(dispatch)
        self.aliases[alias] = dispatch
        self._event(wave, "ISSUE", f"送出 {alias}/{dispatch.id}（{kind}，{len(dispatch.lines)} 行），待财务签收")
        return dispatch

    def _apply_receipt(self, row: dict, ref: RowRef, wave: int) -> None:
        receipt, alias, decision = row["receipt"], row["dispatch_alias"], row["decision"]
        log = {"receipt": receipt, "alias": alias, "decision": decision, "wave": wave, "ref": str(ref)}
        if receipt in self.receipts_seen:
            self._event(wave, "DUPLICATE_RECEIPT", f"回执 {receipt} 确认重送，幂等忽略，不重复改基线")
            self.receipt_log.append(log | {"outcome": "duplicate", "dispatch_id": None})
            return
        self.receipts_seen[receipt] = (alias, decision)
        dispatch = self.aliases.get(alias)
        if dispatch is None:
            self._event(wave, "UNKNOWN_ALIAS", f"回执 {receipt} 指向未知别名 {alias}，隔离")
            self.receipt_log.append(log | {"outcome": "unknown_alias", "dispatch_id": None})
            return
        if dispatch.state != "issued":
            self._event(
                wave,
                "STALE_RECEIPT",
                f"回执 {receipt} 指向已结送单 {alias}/{dispatch.id}（{dispatch.state}），过期回执不得串版",
            )
            self.receipt_log.append(log | {"outcome": "stale", "dispatch_id": dispatch.id})
            return
        if decision == "accept":
            self._confirm(dispatch, wave)
            self.receipt_log.append(log | {"outcome": "confirmed", "dispatch_id": dispatch.id})
        elif decision == "reject":
            self._reject(dispatch, wave)
            self.receipt_log.append(log | {"outcome": "terminated", "dispatch_id": dispatch.id})
        else:
            self._event(wave, "INVALID_DECISION", f"回执 {receipt} 决定无效: {decision}")
            self.receipt_log.append(log | {"outcome": "invalid", "dispatch_id": dispatch.id})

    def _confirm(self, dispatch: Dispatch, wave: int) -> None:
        dispatch.state = "confirmed"
        dispatch.decided_wave = wave
        if dispatch.kind == "full":  # 完整分区签收后整体替换基线
            for key in [k for k in self.baseline if k[:2] == (dispatch.point, dispatch.period)]:
                del self.baseline[key]
        for line in dispatch.lines:
            self.baseline[(dispatch.point, dispatch.period, line.interval.index, line.direction)] = BaselineCell(
                qty=line.qty,
                yuan=line.yuan,
                amount=line.amount,
                price_rev=line.price_rev,
                source_dispatch=dispatch.id,
                quantity_proof=line.quantity_proof,
                price=line.price,
            )
        self._event(wave, "CONFIRM", f"{dispatch.alias}/{dispatch.id} 签收，已确认基线更新（此次确认只改一次）")

    def _reject(self, dispatch: Dispatch, wave: int) -> None:
        dispatch.state = "terminated"
        dispatch.decided_wave = wave
        self._event(wave, "TERMINATE", f"{dispatch.alias}/{dispatch.id} 明确拒收，终止在途单")
        key = (dispatch.point, dispatch.period)
        cells = self.cells.get(key, [])
        if cells and all(c.billable for c in cells):
            self._issue(key, "full", cells, wave)
        else:
            self._needs_full.add(key)
            self._event(wave, "REISSUE_BLOCKED", f"{key[0]}/{key[1]} 量价不齐，完整分区暂缓重出")

    # ------------------------------------------------------------ 证据核验

    def _check_baseline_evidence(self, wave: int) -> None:
        for key, cell in self.baseline.items():
            ok = self._evidence_ok(cell)
            if not ok and not cell.pending:
                cell.pending = True
                self._event(wave, "PENDING", f"已签格子 {key} 失去证据，挂待决；资料补齐前不得冲零")
            elif ok and cell.pending:
                cell.pending = False
                self._event(wave, "PENDING_RESOLVED", f"已签格子 {key} 证据补齐，解除待决")

    def _evidence_ok(self, cell: BaselineCell) -> bool:
        proof = cell.quantity_proof
        if proof is not None:
            for r in proof.readings:
                if self.registers.get((r.meter, ts_key(r.at)), r.rev) is None:
                    return False
            for p in proof.proofs:
                if self.proofs.get((p.meter, ts_key(p.start), ts_key(p.end)), p.rev) is None:
                    return False
        if cell.price is not None:
            key = (cell.price.point, ts_key(cell.price.start), cell.price.direction)
            if self.prices.get(key, cell.price.rev) is None:
                return False
        return True

    # ---------------------------------------------------------------- 查询

    def _in_flight(self, key: tuple) -> Dispatch | None:
        for d in self.dispatches:
            if d.state == "issued" and (d.point, d.period) == key:
                return d
        return None

    def _differs_from_dispatch(self, dispatch: Dispatch) -> bool:
        sent = {(l.interval.index, l.direction): (l.qty, l.yuan) for l in dispatch.lines}
        for c in self.cells.get((dispatch.point, dispatch.period), []):
            if not c.billable:
                continue
            if sent.get((c.interval.index, c.direction)) != (c.qty, c.price.yuan):
                return True
        return False

    def _baseline_exists(self, key: tuple) -> bool:
        return any(k[:2] == key for k in self.baseline)

    def cell(self, point: str, period: str, index: int, direction: str) -> Cell:
        for c in self.cells[(point, period)]:
            if c.interval.index == index and c.direction == direction:
                return c
        raise KeyError((point, period, index, direction))

    def baseline_cell(self, point: str, period: str, index: int, direction: str) -> BaselineCell | None:
        return self.baseline.get((point, period, index, direction))

    def gate_report(self, key: tuple) -> list[Cell]:
        return [c for c in self.cells.get(key, []) if not c.billable]

    def pending_issue(self, key: tuple) -> dict:
        """当前待开口径：若现在出账会出什么。"""
        cells = self.cells.get(key, [])
        in_flight = self._in_flight(key)
        if in_flight:
            return {"kind": None, "reason": f"在途单 {in_flight.alias}/{in_flight.id} 待签", "cells": []}
        if not self._baseline_exists(key) or key in self._needs_full:
            if cells and all(c.billable for c in cells):
                return {"kind": "full", "reason": "量价齐备，待出完整分区", "cells": [self._cell_brief(c) for c in cells]}
            return {
                "kind": None,
                "reason": "两方向量价未齐备",
                "cells": [],
                "gate": [self._cell_brief(c) for c in cells if not c.billable],
            }
        changed, blocked = self._diff_cells(key)
        if changed:
            return {"kind": "diff", "reason": "与已确认基线存在差额", "cells": [self._cell_brief(c) for c in changed]}
        reason = "与已确认基线一致" if not blocked else f"{len(blocked)} 格待决，暂不出差额"
        return {"kind": None, "reason": reason, "cells": []}

    @staticmethod
    def _cell_brief(c: Cell) -> dict:
        return {
            "index": c.interval.index,
            "direction": c.direction,
            "state": c.state,
            "qty_kwh": c.qty,
            "yuan_per_mwh": c.price.yuan if c.price else None,
            "amount_yuan": c.amount,
        }

    def partition_view(self, point: str, period: str) -> dict:
        """旧版、差额、当前待开三种口径并列。"""
        key = (point, period)
        view_cells = []
        for c in self.cells.get(key, []):
            base = self.baseline.get((point, period, c.interval.index, c.direction))
            history = []
            for d in self.dispatches:
                if (d.point, d.period) != key:
                    continue
                for line in d.lines:
                    if line.interval.index == c.interval.index and line.direction == c.direction:
                        history.append(
                            {
                                "dispatch_id": d.id,
                                "alias": d.alias,
                                "kind": d.kind,
                                "state": d.state,
                                "qty_kwh": line.qty,
                                "yuan_per_mwh": line.yuan,
                                "amount_yuan": line.amount,
                                "delta_qty_kwh": line.delta_qty,
                                "delta_amount_yuan": line.delta_amount,
                            }
                        )
            view_cells.append(
                {
                    "interval": {
                        "index": c.interval.index,
                        "start": c.interval.start.isoformat(),
                        "end": c.interval.end.isoformat(),
                        "minutes": c.interval.minutes,
                    },
                    "direction": c.direction,
                    "role": ROLE[c.direction],
                    "current": self._cell_brief(c) | {"price_rev": c.price.rev if c.price else None},
                    "confirmed": None
                    if base is None
                    else {
                        "qty_kwh": base.qty,
                        "yuan_per_mwh": base.yuan,
                        "amount_yuan": base.amount,
                        "source": base.source_dispatch,
                        "pending": base.pending,
                    },
                    "history": history,
                }
            )
        return {
            "point": point,
            "period": period,
            "cells": view_cells,
            "dispatches": [
                {
                    "id": d.id,
                    "alias": d.alias,
                    "kind": d.kind,
                    "state": d.state,
                    "issued_wave": d.issued_wave,
                    "decided_wave": d.decided_wave,
                    "lines": len(d.lines),
                }
                for d in self.dispatches
                if (d.point, d.period) == key
            ],
            "to_issue": self.pending_issue(key),
            "recon_only": [
                {"meter": r.meter, "start": r.start.isoformat(), "end": r.end.isoformat(), "kwh": str(r.kwh), "reason": r.reason}
                for r in self.recon_only.get(key, [])
            ],
        }

    def reconciliation(self, point: str, period: str, basis: str = "confirmed") -> dict:
        """购电/售电独立对账：量与应收应付各自成账，互不轧差。"""
        key = (point, period)
        directions = {}
        for direction in DIRECTIONS:
            rows = []
            total_qty = Decimal(0)
            total_amount = Decimal(0)
            if basis == "confirmed":
                sources = sorted(
                    (k, b) for k, b in self.baseline.items() if k[:2] == key and k[3] == direction
                )
                for (_p, _per, idx, _d), b in sources:
                    rows.append({"index": idx, "qty_kwh": b.qty, "amount_yuan": b.amount, "pending": b.pending})
                    total_qty += b.qty
                    total_amount += b.amount
            else:
                for c in self.cells.get(key, []):
                    if c.direction != direction or not c.billable:
                        continue
                    rows.append({"index": c.interval.index, "qty_kwh": c.qty, "amount_yuan": c.amount})
                    total_qty += c.qty
                    total_amount += c.amount
            directions[direction] = {
                "label": DIR_LABEL[direction],
                "role": ROLE[direction],
                "qty_kwh": total_qty,
                "amount_yuan": total_amount,
                "cells": rows,
            }
        return {
            "point": point,
            "period": period,
            "basis": basis,
            "directions": directions,
            "recon_only": [
                {"meter": r.meter, "start": r.start.isoformat(), "end": r.end.isoformat(), "kwh": str(r.kwh), "reason": r.reason}
                for r in self.recon_only.get(key, [])
            ],
        }

    def trace(self, dispatch_id: str, index: int, direction: str) -> dict:
        """凭单回查：哪对表底、哪份价格、哪版归属。"""
        dispatch = next(d for d in self.dispatches if d.id == dispatch_id)
        line = next(
            l for l in dispatch.lines if l.interval.index == index and l.direction == direction
        )
        return {
            "dispatch_id": dispatch.id,
            "alias": dispatch.alias,
            "kind": dispatch.kind,
            "state": dispatch.state,
            "interval": {
                "index": line.interval.index,
                "start": line.interval.start.isoformat(),
                "end": line.interval.end.isoformat(),
            },
            "direction": line.direction,
            "qty_kwh": line.qty,
            "yuan_per_mwh": line.yuan,
            "amount_yuan": line.amount,
            "delta_qty_kwh": line.delta_qty,
            "delta_amount_yuan": line.delta_amount,
            "baseline_source": line.baseline_source,
            "quantity": quantity_dict(line.quantity_proof),
            "price": price_dict(line.price),
        }

    # ---------------------------------------------------------------- 辅助

    def _event(self, wave: int, kind: str, detail: str) -> None:
        self.events.append(Event(wave, kind, detail))
