#!/usr/bin/env python3
"""命令行演练：资料入仓 → 逐波次结算 → 财务签收 → 差额 → 落库回查。

    python3 run_settlement.py --data data --db settlement.db
"""
from __future__ import annotations

import argparse
from decimal import Decimal
from pathlib import Path

from settlement.engine import DIR_LABEL, ROLE, Warehouse
from settlement.foundation import STATE_LABELS
from settlement.store import save


def fmt(value) -> str:
    return "—" if value is None else str(value)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(__file__).resolve().parent / "data")
    parser.add_argument("--db", type=Path, default=Path("settlement.db"))
    args = parser.parse_args()

    wh = Warehouse()
    wh.load_pack(args.data)
    wh.run()

    print("== 波次事件 ==")
    for event in wh.events:
        print(f"[w{event.wave}] {event.kind}: {event.detail}")

    for point, period in sorted(wh.grid):
        view = wh.partition_view(point, period)
        print(f"\n== 分区 {point}/{period}（旧版 / 差额 / 当前待开 并列） ==")
        for cell in view["cells"]:
            iv = cell["interval"]
            current = cell["current"]
            confirmed = cell["confirmed"] or {}
            head = (
                f"格{iv['index']} [{iv['start']} → {iv['end']}] "
                f"{DIR_LABEL[cell['direction']]}({cell['role']})"
            )
            print(
                f"{head}\n  当前: {STATE_LABELS.get(current['state'], current['state'])}"
                f" 量={fmt(current['qty_kwh'])}kWh 价={fmt(current['yuan_per_mwh'])}"
                f"(rev{fmt(current['price_rev'])}) 额={fmt(current['amount_yuan'])}"
            )
            if confirmed:
                print(
                    f"  已签: 量={fmt(confirmed.get('qty_kwh'))}kWh 价={fmt(confirmed.get('yuan_per_mwh'))}"
                    f" 额={fmt(confirmed.get('amount_yuan'))} 源={confirmed.get('source')}"
                    f"{' 【待决】' if confirmed.get('pending') else ''}"
                )
            for hist in cell["history"]:
                delta = fmt(hist["delta_amount_yuan"])
                print(
                    f"  送单 {hist['alias']}/{hist['dispatch_id']} {hist['kind']}({hist['state']}):"
                    f" 量={fmt(hist['qty_kwh'])} 价={fmt(hist['yuan_per_mwh'])}"
                    f" 额={fmt(hist['amount_yuan'])} 差额={delta}"
                )
        todo = view["to_issue"]
        print(f"当前待开: {todo['kind'] or '无'} — {todo['reason']}")
        for item in view["recon_only"]:
            print(f"  仅对账: {item['meter']} [{item['start']}→{item['end']}] {item['kwh']}kWh — {item['reason']}")

        recon = wh.reconciliation(point, period)
        print(f"\n== 独立对账（{recon['basis']}） ==")
        for direction, part in recon["directions"].items():
            print(f"  {part['label']}({part['role']}): 量={part['qty_kwh']}kWh 额={part['amount_yuan']}元")

    confirmed = [d for d in wh.dispatches if d.state == "confirmed"]
    if confirmed:
        last = confirmed[-1]
        print(f"\n== 凭单回查（{last.alias}/{last.id} 每行的表底、价格、归属） ==")
        for line in last.lines:
            trace = wh.trace(last.id, line.interval.index, line.direction)
            quantity = trace["quantity"]
            readings = ", ".join(f"{r['meter']}@{r['at']}[rev{r['rev']}]={r['kwh']}({r['ref']})" for r in quantity["readings"])
            price = trace["price"]
            print(
                f"格{line.interval.index}/{line.direction}: 量={trace['qty_kwh']}kWh 价={trace['yuan_per_mwh']}"
                f" 额={trace['amount_yuan']}\n  表底: {readings}\n"
                f"  归属: {quantity['authority']['kind']}({quantity['authority']['ref']})"
                f"  价格: rev{price['rev']} {price['status']}({price['ref']})"
            )

    save(wh, args.db)
    print(f"\n已落库 {args.db}（events/landed/quarantines/cells_current/dispatches/dispatch_lines/receipts/baseline）")


if __name__ == "__main__":
    main()
