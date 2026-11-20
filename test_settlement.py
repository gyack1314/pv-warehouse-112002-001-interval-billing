import unittest
from decimal import Decimal
from pathlib import Path

from settlement import State, Warehouse, amount_for, parse_ts, ts_key

DATA = Path(__file__).resolve().parent / "data"
KEY = ("N4", "2026-11")


def run_pack():
    wh = Warehouse()
    wh.load_pack(DATA)
    wh.run()
    return wh


def line_map(dispatch):
    return {(l.interval.index, l.direction): l for l in dispatch.lines}


class MoneyTests(unittest.TestCase):
    def test_half_cent_rounds_away_from_zero(self):
        self.assertEqual(amount_for(Decimal("3"), Decimal("85")), Decimal("0.26"))  # 0.255
        self.assertEqual(amount_for(Decimal("3"), Decimal("-15")), Decimal("-0.05"))  # -0.045
        self.assertEqual(amount_for(Decimal("7"), Decimal("-10")), Decimal("-0.07"))
        self.assertEqual(amount_for(Decimal("6"), Decimal("92")), Decimal("0.55"))
        self.assertEqual(amount_for(Decimal("0"), Decimal("92")), Decimal("0.00"))
        self.assertEqual(amount_for(Decimal("2"), Decimal("125")), Decimal("0.25"))


class GridTests(unittest.TestCase):
    def test_dst_fallback_instants_not_merged(self):
        early = parse_ts("2026-11-01T01:30:00-04:00")
        late = parse_ts("2026-11-01T01:30:00-05:00")
        self.assertNotEqual(ts_key(early), ts_key(late))  # 相同钟表时分不合并

    def test_pack_grid(self):
        wh = run_pack()
        intervals = wh.grid[KEY]
        self.assertEqual([iv.minutes for iv in intervals], [15, 5, 5])
        self.assertEqual(intervals[0].start.isoformat(), "2026-11-01T01:45:00-04:00")
        self.assertEqual(intervals[0].end.isoformat(), "2026-11-01T01:00:00-05:00")


class WaveByWaveTests(unittest.TestCase):
    def test_full_flow(self):
        wh = Warehouse()
        wh.load_pack(DATA)

        wh.apply_wave(1)  # 临时价 + 缺分量 + 缺归属：不得出账
        self.assertEqual(len(wh.dispatches), 0)
        states = {c.state for c in wh.gate_report(KEY)}
        self.assertIn(State.MISSING_QUANTITY, states)
        self.assertIn(State.NO_AUTHORITY, states)
        cell = wh.cell(*KEY, 2, "export")
        self.assertEqual(cell.price_state, State.ESTIMATED_PRICE)  # 临时价
        self.assertIsNone(cell.amount)  # 可预估，不可出账

        wh.apply_wave(2)  # 分量、授权、正式价到齐 → 首张完整分区
        self.assertEqual(len(wh.dispatches), 1)
        first = wh.dispatches[0]
        self.assertEqual((first.kind, first.alias, first.state), ("full", "A", "issued"))
        amounts = {k: l.amount for k, l in line_map(first).items()}
        self.assertEqual(
            amounts,
            {
                (0, "import"): Decimal("0.26"),
                (0, "export"): Decimal("-0.07"),
                (1, "import"): Decimal("0.55"),
                (1, "export"): Decimal("-0.05"),
                (2, "import"): Decimal("0.00"),
                (2, "export"): Decimal("0.42"),
            },
        )

        wh.apply_wave(3)  # 修订到达，在途单保留
        self.assertEqual(len(wh.dispatches), 1)
        self.assertEqual(first.state, "issued")

        wh.apply_wave(4)  # ACK-01 签收（含重送）→ 基线更新一次 → 差额批次
        self.assertEqual(first.state, "confirmed")
        self.assertEqual(len(wh.dispatches), 2)
        diff = wh.dispatches[1]
        self.assertEqual((diff.kind, diff.alias, diff.state), ("diff", "B", "issued"))
        self.assertEqual(len(diff.lines), 1)
        line = diff.lines[0]
        self.assertEqual((line.interval.index, line.direction), (2, "export"))
        self.assertEqual((line.qty, line.yuan, line.amount), (Decimal(3), Decimal(121), Decimal("0.36")))
        self.assertEqual((line.delta_qty, line.delta_amount), (Decimal(-1), Decimal("-0.06")))
        self.assertEqual(line.baseline_source, first.id)
        self.assertEqual(
            sum(1 for e in wh.events if e.kind == "DUPLICATE_RECEIPT"), 1
        )  # 确认重送幂等

        wh.apply_wave(5)  # rev2 同版冲突 → 显式隔离，当前量回退 rev1
        self.assertEqual(len(wh.registers.quarantines), 1)
        quarantine = wh.registers.quarantines[0]
        self.assertEqual(quarantine.rev, 2)
        self.assertEqual(len(quarantine.refs), 2)
        self.assertEqual(wh.cell(*KEY, 2, "export").qty, Decimal(4))
        self.assertEqual(len(wh.dispatches), 2)  # 在途单仍保留

        wh.apply_wave(6)  # rev3 + 正式价 rev4；ACK-02 拒收 → 终止并重出完整分区
        self.assertEqual(diff.state, "terminated")
        self.assertEqual(len(wh.dispatches), 3)
        reissued = wh.dispatches[2]
        self.assertEqual((reissued.kind, reissued.alias, reissued.state), ("full", "C", "issued"))
        line = line_map(reissued)[(2, "export")]
        self.assertEqual((line.qty, line.yuan, line.amount), (Decimal(2), Decimal(125), Decimal("0.25")))

        wh.apply_wave(7)  # ACK-03 签收 → 基线整体替换
        self.assertEqual(reissued.state, "confirmed")
        self.assertEqual(len(wh.dispatches), 3)
        base = wh.baseline_cell(*KEY, 2, "export")
        self.assertEqual((base.qty, base.yuan, base.amount), (Decimal(2), Decimal(125), Decimal("0.25")))
        self.assertFalse(base.pending)


class FinalStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.wh = run_pack()

    def test_independent_reconciliation(self):
        recon = self.wh.reconciliation(*KEY)
        purchase = recon["directions"]["import"]
        sale = recon["directions"]["export"]
        self.assertEqual((purchase["role"], purchase["qty_kwh"], purchase["amount_yuan"]), ("应付", Decimal(9), Decimal("0.81")))
        self.assertEqual((sale["role"], sale["qty_kwh"], sale["amount_yuan"]), ("应收", Decimal(12), Decimal("0.13")))

    def test_zero_confirmed_distinguishable(self):
        cell = self.wh.cell(*KEY, 2, "import")
        self.assertEqual(cell.state, State.ZERO_CONFIRMED)
        self.assertEqual(cell.amount, Decimal("0.00"))
        self.assertNotEqual(cell.state, State.MISSING_QUANTITY)

    def test_no_cross_meter_subtraction(self):
        for dispatch in self.wh.dispatches:
            for line in dispatch.lines:
                if line.interval.index == 2 and line.direction == "export":
                    self.assertEqual(line.quantity_proof.meter, "N4-X")
                    self.assertTrue(all(r.meter == "N4-X" for r in line.quantity_proof.readings))

    def test_trace_from_voucher(self):
        last = self.wh.dispatches[-1]
        trace = self.wh.trace(last.id, 2, "export")
        self.assertEqual(trace["quantity"]["meter"], "N4-X")
        self.assertEqual([r["rev"] for r in trace["quantity"]["readings"]], [1, 3])
        self.assertEqual(trace["quantity"]["authority"]["kind"], "authorized_replacement")
        self.assertEqual((trace["price"]["rev"], trace["price"]["status"]), (4, "final"))
        imp = self.wh.trace(last.id, 1, "import")
        self.assertEqual(len(imp["quantity"]["proofs"]), 1)  # 跨界增量靠分量拆出
        self.assertEqual(imp["quantity"]["proofs"][0]["kwh"], "6")

    def test_calibers_side_by_side(self):
        view = self.wh.partition_view(*KEY)
        cell = next(c for c in view["cells"] if c["interval"]["index"] == 2 and c["direction"] == "export")
        self.assertEqual(cell["confirmed"]["amount_yuan"], Decimal("0.25"))
        kinds = [(h["kind"], h["state"]) for h in cell["history"]]
        self.assertEqual(kinds, [("full", "confirmed"), ("diff", "terminated"), ("full", "confirmed")])
        self.assertIsNone(view["to_issue"]["kind"])

    def test_nothing_left_to_issue(self):
        self.assertEqual(self.wh.pending_issue(KEY)["kind"], None)


class RevisionSafetyTests(unittest.TestCase):
    def test_pending_when_signed_evidence_lost(self):
        wh = run_pack()
        wh.land(
            "meter_registers",
            {"wave": "8", "meter": "N4-X", "direction": "export", "at": "2026-11-01T01:10:00-05:00", "kwh": "9", "rev": "3"},
        )
        wh.apply_wave(8)  # rev3 同版冲突 → 已签格子失去证据
        base = wh.baseline_cell(*KEY, 2, "export")
        self.assertTrue(base.pending)
        self.assertEqual(base.amount, Decimal("0.25"))  # 不得擅自冲零
        self.assertEqual(len(wh.dispatches), 3)  # 待决格不进差额
        self.assertTrue(any(e.kind == "PENDING" for e in wh.events))

    def test_stale_and_unknown_receipts_do_not_cross_versions(self):
        wh = run_pack()
        wh.land("ledger_receipts", {"wave": "8", "receipt": "ACK-09", "dispatch_alias": "A", "decision": "accept"})
        wh.land("ledger_receipts", {"wave": "8", "receipt": "ACK-10", "dispatch_alias": "C", "decision": "reject"})
        wh.land("ledger_receipts", {"wave": "8", "receipt": "ACK-11", "dispatch_alias": "Z", "decision": "accept"})
        wh.apply_wave(8)
        self.assertEqual([d.state for d in wh.dispatches], ["confirmed", "terminated", "confirmed"])
        kinds = [e.kind for e in wh.events if e.wave == 8]
        self.assertEqual(kinds.count("STALE_RECEIPT"), 2)
        self.assertEqual(kinds.count("UNKNOWN_ALIAS"), 1)
        self.assertEqual(wh.baseline_cell(*KEY, 2, "export").amount, Decimal("0.25"))


class SyntheticRuleTests(unittest.TestCase):
    def _warehouse(self):
        wh = Warehouse()
        wh.land("market_calendar", {"point": "P1", "period": "2026-10", "start": "2026-10-01T00:00:00+08:00", "end": "2026-10-01T00:10:00+08:00", "minutes": "5"})
        wh.land("meter_authority", {"wave": "1", "point": "P1", "direction": "import", "start": "2026-10-01T00:00:00+08:00", "end": "2026-10-01T00:10:00+08:00", "meter": "M1", "kind": "main", "preceding_meter": ""})
        return wh

    def test_unverifiable_split_stays_out_of_billing(self):
        wh = self._warehouse()
        wh.land("meter_registers", {"wave": "1", "meter": "M1", "direction": "import", "at": "2026-10-01T00:00:00+08:00", "kwh": "0", "rev": "1"})
        wh.land("meter_registers", {"wave": "1", "meter": "M1", "direction": "import", "at": "2026-10-01T00:10:00+08:00", "kwh": "10", "rev": "1"})
        wh.land("interval_proof", {"wave": "1", "meter": "M1", "start": "2026-10-01T00:00:00+08:00", "end": "2026-10-01T00:05:00+08:00", "kwh": "4", "rev": "1"})
        wh.land("interval_proof", {"wave": "1", "meter": "M1", "start": "2026-10-01T00:05:00+08:00", "end": "2026-10-01T00:10:00+08:00", "kwh": "4", "rev": "1"})
        for idx, start in ((0, "00:00"), (1, "00:05")):
            wh.land("prices", {"wave": "1", "point": "P1", "start": f"2026-10-01T{start}:00+08:00", "direction": "import", "rev": "1", "status": "final", "yuan_per_mwh": "100"})
        wh.apply_wave(1)  # 分量合计 8 ≠ 表底增量 10：不得拆，只进对账
        self.assertEqual(len(wh.dispatches), 0)
        self.assertEqual(wh.cell("P1", "2026-10", 0, "import").state, State.MISSING_QUANTITY)
        recon = wh.recon_only[("P1", "2026-10")]
        self.assertTrue(any("不符" in r.reason for r in recon))

    def test_quarter_hour_proof_is_recon_only_in_five_minute_market(self):
        wh = self._warehouse()
        wh.land("meter_registers", {"wave": "1", "meter": "M1", "direction": "import", "at": "2026-10-01T00:00:00+08:00", "kwh": "0", "rev": "1"})
        wh.land("meter_registers", {"wave": "1", "meter": "M1", "direction": "import", "at": "2026-10-01T00:10:00+08:00", "kwh": "10", "rev": "1"})
        wh.land("interval_proof", {"wave": "1", "meter": "M1", "start": "2026-10-01T00:00:00+08:00", "end": "2026-10-01T00:10:00+08:00", "kwh": "10", "rev": "1"})
        for idx, start in ((0, "00:00"), (1, "00:05")):
            wh.land("prices", {"wave": "1", "point": "P1", "start": f"2026-10-01T{start}:00+08:00", "direction": "import", "rev": "1", "status": "final", "yuan_per_mwh": "100"})
        wh.apply_wave(1)  # 整段分量不满足五分钟口径：可核对，不进收费
        self.assertEqual(len(wh.dispatches), 0)
        self.assertTrue(any("口径" in r.reason or "分量" in r.reason for r in wh.recon_only[("P1", "2026-10")]))

    def test_price_conflict_quarantined_not_picked_by_arrival(self):
        wh = self._warehouse()
        wh.land("meter_registers", {"wave": "1", "meter": "M1", "direction": "import", "at": "2026-10-01T00:00:00+08:00", "kwh": "0", "rev": "1"})
        wh.land("meter_registers", {"wave": "1", "meter": "M1", "direction": "import", "at": "2026-10-01T00:05:00+08:00", "kwh": "5", "rev": "1"})
        wh.land("meter_registers", {"wave": "1", "meter": "M1", "direction": "import", "at": "2026-10-01T00:10:00+08:00", "kwh": "5", "rev": "1"})
        wh.land("prices", {"wave": "1", "point": "P1", "start": "2026-10-01T00:00:00+08:00", "direction": "import", "rev": "1", "status": "final", "yuan_per_mwh": "100"})
        wh.land("prices", {"wave": "1", "point": "P1", "start": "2026-10-01T00:05:00+08:00", "direction": "import", "rev": "1", "status": "final", "yuan_per_mwh": "100"})
        wh.land("prices", {"wave": "2", "point": "P1", "start": "2026-10-01T00:05:00+08:00", "direction": "import", "rev": "2", "status": "final", "yuan_per_mwh": "110"})
        wh.land("prices", {"wave": "2", "point": "P1", "start": "2026-10-01T00:05:00+08:00", "direction": "import", "rev": "2", "status": "final", "yuan_per_mwh": "130"})
        wh.apply_wave(1)
        wh.apply_wave(2)  # rev2 同版冲突：隔离而非按到达顺序选价
        cell = wh.cell("P1", "2026-10", 1, "import")
        self.assertEqual(cell.price.yuan, Decimal(100))  # 回退到有效 rev1
        self.assertEqual(len(wh.prices.quarantines), 1)


if __name__ == "__main__":
    unittest.main()
