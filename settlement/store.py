"""把仓内状态落 SQLite：原始行、版本隔离、格子口径、送单、回执、基线可并列查询。"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .engine import Warehouse, price_dict, quantity_dict

SCHEMA = """
CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE events (seq INTEGER PRIMARY KEY AUTOINCREMENT, wave INTEGER, kind TEXT, detail TEXT);
CREATE TABLE landed (table_name TEXT, row_no INTEGER, wave INTEGER, payload TEXT);
CREATE TABLE quarantines (book TEXT, key_json TEXT, rev INTEGER, refs TEXT, detected_wave INTEGER);
CREATE TABLE intervals (point TEXT, period TEXT, idx INTEGER, start_local TEXT, end_local TEXT, minutes INTEGER);
CREATE TABLE cells_current (
    point TEXT, period TEXT, idx INTEGER, direction TEXT, state TEXT,
    qty_kwh TEXT, price_rev INTEGER, price_status TEXT, yuan_per_mwh TEXT,
    amount_yuan TEXT, estimate_yuan TEXT, provenance TEXT
);
CREATE TABLE dispatches (
    id TEXT PRIMARY KEY, seq INTEGER, alias TEXT, kind TEXT, point TEXT, period TEXT,
    state TEXT, issued_wave INTEGER, decided_wave INTEGER
);
CREATE TABLE dispatch_lines (
    dispatch_id TEXT, idx INTEGER, direction TEXT, qty_kwh TEXT, yuan_per_mwh TEXT,
    amount_yuan TEXT, delta_qty_kwh TEXT, delta_amount_yuan TEXT, price_rev INTEGER,
    baseline_source TEXT, provenance TEXT
);
CREATE TABLE receipts (receipt TEXT, alias TEXT, decision TEXT, wave INTEGER, outcome TEXT, dispatch_id TEXT);
CREATE TABLE baseline (
    point TEXT, period TEXT, idx INTEGER, direction TEXT, qty_kwh TEXT, yuan_per_mwh TEXT,
    amount_yuan TEXT, price_rev INTEGER, source_dispatch TEXT, pending INTEGER, provenance TEXT
);
"""


def _j(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def save(wh: Warehouse, path) -> None:
    path = Path(path)
    if path.exists():
        path.unlink()
    con = sqlite3.connect(path)
    try:
        cur = con.cursor()
        cur.executescript(SCHEMA)
        cur.execute("INSERT INTO meta VALUES (?, ?)", ("generator", "settlement-warehouse"))
        cur.executemany(
            "INSERT INTO events(wave, kind, detail) VALUES (?, ?, ?)",
            [(e.wave, e.kind, e.detail) for e in wh.events],
        )
        cur.executemany(
            "INSERT INTO landed VALUES (?, ?, ?, ?)",
            [(t, ref.row, ref.wave, _j(row)) for t, row, ref in wh.landed],
        )
        quarantines = []
        for book in (wh.registers, wh.proofs, wh.prices):
            for q in book.quarantines:
                quarantines.append((q.book, _j(q.key), q.rev, _j([str(r) for r in q.refs]), q.wave))
        cur.executemany("INSERT INTO quarantines VALUES (?, ?, ?, ?, ?)", quarantines)
        cur.executemany(
            "INSERT INTO intervals VALUES (?, ?, ?, ?, ?, ?)",
            [
                (p, per, iv.index, iv.start.isoformat(), iv.end.isoformat(), iv.minutes)
                for (p, per), ivs in wh.grid.items()
                for iv in ivs
            ],
        )
        cur.executemany(
            "INSERT INTO cells_current VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    p,
                    per,
                    c.interval.index,
                    c.direction,
                    c.state,
                    _s(c.qty),
                    c.price.rev if c.price else None,
                    c.price.status if c.price else None,
                    _s(c.price.yuan) if c.price else None,
                    _s(c.amount),
                    _s(c.estimate_amount),
                    _j({"quantity": quantity_dict(c.quantity_proof), "price": price_dict(c.price)}),
                )
                for (p, per), cells in wh.cells.items()
                for c in cells
            ],
        )
        cur.executemany(
            "INSERT INTO dispatches VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (d.id, d.seq, d.alias, d.kind, d.point, d.period, d.state, d.issued_wave, d.decided_wave)
                for d in wh.dispatches
            ],
        )
        cur.executemany(
            "INSERT INTO dispatch_lines VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    d.id,
                    line.interval.index,
                    line.direction,
                    _s(line.qty),
                    _s(line.yuan),
                    _s(line.amount),
                    _s(line.delta_qty),
                    _s(line.delta_amount),
                    line.price_rev,
                    line.baseline_source,
                    _j({"quantity": quantity_dict(line.quantity_proof), "price": price_dict(line.price)}),
                )
                for d in wh.dispatches
                for line in d.lines
            ],
        )
        cur.executemany(
            "INSERT INTO receipts VALUES (?, ?, ?, ?, ?, ?)",
            [
                (r["receipt"], r["alias"], r["decision"], r["wave"], r["outcome"], r["dispatch_id"])
                for r in wh.receipt_log
            ],
        )
        cur.executemany(
            "INSERT INTO baseline VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    p,
                    per,
                    idx,
                    direction,
                    _s(b.qty),
                    _s(b.yuan),
                    _s(b.amount),
                    b.price_rev,
                    b.source_dispatch,
                    int(b.pending),
                    _j({"quantity": quantity_dict(b.quantity_proof), "price": price_dict(b.price)}),
                )
                for (p, per, idx, direction), b in wh.baseline.items()
            ],
        )
        con.commit()
    finally:
        con.close()


def _s(value):
    return None if value is None else str(value)
