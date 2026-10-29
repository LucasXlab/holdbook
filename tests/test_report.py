import json
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from holdbook import db
from holdbook.config import load_config
from holdbook.csvio import parse_snapshots, parse_trades
from holdbook.demo import SNAPSHOT, TRADES
from holdbook.errors import HoldbookError
from holdbook.report import build, html_report, text_report, to_json

ENV = {"HOLDBOOK_NOW": "2026-10-29T12:00:00Z"}


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, ENV, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.cfg = load_config(data_dir=self.tmp.name, today="2026-10-29")
        db.init_db(self.cfg)
        self.trades = self.dir / "t.csv"
        self.snap = self.dir / "s.csv"
        self.trades.write_text(TRADES, encoding="utf-8")
        self.snap.write_text(SNAPSHOT, encoding="utf-8")
        conn = db.connect(self.cfg)
        pt = parse_trades(self.trades, "华泰", self.cfg)
        with db.transaction(conn):
            aid, _ = db.ensure_account(conn, "华泰", "华泰证券", self.cfg.now_iso())
            for tx in pt.txs:
                db.upsert_instrument(conn, tx.code, tx.name, "x", self.cfg.now_iso())
                db.insert_tx(conn, aid, tx, self.cfg.now_iso())
            ps = parse_snapshots(self.snap, self.cfg)
            for s in ps.rows:
                db.upsert_instrument(conn, s.code, s.name, "x", self.cfg.now_iso())
                db.upsert_snapshot(conn, aid, self.cfg.today, s.code, s.name, s.quantity, s.cost_price, s.market_value, self.snap.name, self.cfg.now_iso())
        conn.close()

    def test_data_and_json(self):
        conn = db.connect(self.cfg)
        try:
            data = build(conn, self.cfg, None, date(2026, 10, 29), "mock", None)
            self.assertEqual(data["summary"]["totalCost"], 197068.50)
            self.assertEqual(data["summary"]["marketValue"], 189448.00)
            self.assertEqual(data["summary"]["realizedPnl"], 293.50)
            self.assertEqual(data["summary"]["interest"], 36.50)
            self.assertEqual(data["instruments"], 4)
            j = to_json(data)
            raw = json.dumps(j, ensure_ascii=False)
            self.assertEqual(j["method"], "holdbook-ledger-v1")
            self.assertEqual(j["summary"]["fees"]["total"], "49.00")
            self.assertIsInstance(j["top3"], float)
            self.assertEqual(j["positions"][0]["price"], "1354.0000")
        finally:
            conn.close()

    def test_html_and_text(self):
        conn = db.connect(self.cfg)
        try:
            data = build(conn, self.cfg, None, date(2026, 10, 29), "mock", None)
        finally:
            conn.close()
        html = html_report(data)
        self.assertIn("<!doctype html>", html)
        self.assertIn("--bg:#fff", html)
        self.assertIn("prefers-color-scheme:dark", html)
        self.assertIn("max-width:600px", html)
        self.assertIn("公式版本 holdbook-ledger-v1", html)
        self.assertNotIn("<script", html)
        self.assertNotIn("对账结果", html)
        self.assertNotIn("对账差异", html)
        text = text_report(data)
        self.assertIn("持仓簿持仓报告", text)
        self.assertIn("公式版本 holdbook-ledger-v1", text)

    def test_no_price(self):
        conn = db.connect(self.cfg)
        try:
            data = build(conn, self.cfg, None, date(2026, 10, 29), "off", None)
            self.assertEqual(data["summary"]["priced"], 0)
            self.assertEqual(data["summary"]["unpriced"], 4)
            self.assertIsNone(data["top3"])
            self.assertTrue(any(w["code"] == "missing_price" for w in data["warnings"]))
            self.assertTrue(any(w["code"] == "xirr_cost_fallback" for w in data["warnings"]))
        finally:
            conn.close()

    def test_live_all_fail_is_exit_4(self):
        with mock.patch.dict(os.environ, {"HOLDBOOK_QUOTE_BASE_URL": "http://127.0.0.1:9/q", "HOLDBOOK_MARKET_TIMEOUT": "1", "HOLDBOOK_MARKET_RETRIES": "0"}):
            cfg = load_config(data_dir=self.tmp.name, today="2026-10-29")
        conn = db.connect(cfg)
        try:
            with self.assertRaises(HoldbookError) as cm:
                build(conn, cfg, None, date(2026, 10, 29), "live", None)
            self.assertEqual(cm.exception.code, "market_unavailable")
            self.assertEqual(cm.exception.exit_code, 4)
        finally:
            conn.close()

    def test_manual_future_ignored(self):
        p = self.dir / "p.csv"
        p.write_text("code,close,date\n600519,1400,2026-12-01\n000001,12,2026-01-01\n", encoding="utf-8")
        conn = db.connect(self.cfg)
        try:
            data = build(conn, self.cfg, None, date(2026, 10, 29), "mock", str(p))
            self.assertTrue(any(w["code"] == "manual_price_ignored" for w in data["warnings"]))
        finally:
            conn.close()
