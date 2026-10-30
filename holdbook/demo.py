from __future__ import annotations

import csv
import io
from datetime import date
from pathlib import Path

from .config import Config, load_config
from .csvio import parse_snapshots, parse_trades
from .db import (connect, ensure_account, init_db, insert_import, insert_tx, transaction,
                 upsert_instrument, upsert_snapshot)
from .errors import err
from .reconcile import check_account
from .report import atomic_write, build, html_report

TRADES = """成交日期,操作,证券代码,证券名称,成交数量,成交价格,成交金额,手续费,印花税,过户费,其他费用
2025-03-10,证券买入,600519,贵州茅台,100,1580.00,158000.00,5.00,0,0,0
2025-04-08,证券买入,000001,平安银行,1000,11.50,11500.00,5.00,0,0,0
2025-05-12,证券买入,510300,沪深300ETF,5000,4.00,20000.00,5.00,0,0,0
2025-06-18,红利入账,600519,贵州茅台,0,0,600.00,0,0,0,0
2025-09-02,证券卖出,000001,平安银行,300,12.50,3750.00,5.00,0,0,0
2025-11-20,送股入账,600519,贵州茅台,10,0,0,0,0,0,0
2026-01-15,证券买入,000858,五粮液,100,110.00,11000.00,5.00,0,0,0
2026-03-06,红利税,600519,贵州茅台,0,0,-24.00,0,0,0,0
2026-06-30,利息入账,0,0,0,0,36.50,0,0,0,0
"""
SNAPSHOT = """证券代码,证券名称,持仓数量,成本价,市值
600519,贵州茅台,100,1436.4091,135400.00
000001,平安银行,700,11.5050,8078.00
510300,沪深300ETF,5000,4.0010,20555.00
000858,五粮液,100,110.0500,11875.00
"""


def run_demo(path: Path, out, cfg: Config) -> int:
    if path.exists():
        if path.is_file() or any(path.iterdir()):
            raise err("conflict", f"演示目录已存在且非空：{path}", 2)
    path.mkdir(parents=True, exist_ok=True)
    data_dir = path / "data"
    data_dir.mkdir()
    trades = path / "示例_交割单.csv"
    snap = path / "示例_持仓快照.csv"
    trades.write_text(TRADES, encoding="utf-8")
    snap.write_text(SNAPSHOT, encoding="utf-8")

    dcfg = load_config(data_dir=str(data_dir), today=cfg.today.isoformat())
    init_db(dcfg)
    conn = connect(dcfg)
    try:
        parsed = parse_trades(trades, "华泰", dcfg)
        with transaction(conn):
            account_id, _ = ensure_account(conn, "华泰", "华泰证券", dcfg.now_iso())
            for tx in parsed.txs:
                upsert_instrument(conn, tx.code, tx.name, _board(tx.code), dcfg.now_iso())
                insert_tx(conn, account_id, tx, dcfg.now_iso())
            insert_import(conn, "华泰", trades.name, dcfg.now_iso(), len(parsed.txs), len(parsed.txs), 0, 0, "ok")
            ps = parse_snapshots(snap, dcfg)
            for s in ps.rows:
                upsert_instrument(conn, s.code, s.name or s.code, _board(s.code), dcfg.now_iso())
                upsert_snapshot(conn, account_id, dcfg.today, s.code, s.name, s.quantity, s.cost_price, s.market_value, snap.name, dcfg.now_iso())
        with transaction(conn):
            data = build(conn, dcfg, "华泰", dcfg.today, "mock", None)
        rep = path / "report.html"
        atomic_write(rep, html_report(data))
        txs = [r for r in conn.execute("SELECT * FROM transactions WHERE account_id=? ORDER BY trade_date,id", (account_id,))]
        from .ledger import row_to_tx
        res = check_account("华泰", dcfg.today, [row_to_tx(r) for r in txs],
                            list(conn.execute("SELECT * FROM snapshots WHERE account_id=? AND as_of_date=?", (account_id, dcfg.today.isoformat()))), dcfg)
    finally:
        conn.close()
    if out.json:
        out.data({"ok": True, "dir": str(path), "report": str(rep)})
    else:
        out.result(f"演示数据: {path}")
        out.result(f"报告: {rep}")
    return 0


def _board(code: str) -> str:
    from .csvio import normalize_code
    if code == "CASH.CASH":
        return "cash"
    try:
        return normalize_code(code)[1]
    except ValueError:
        return "unknown"
