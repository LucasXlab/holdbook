import io
import json
import os
import sqlite3
import stat
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest import mock

from holdbook import db
from holdbook.cli import main
from holdbook.config import load_config
from holdbook.csvio import parse_snapshots, parse_trades
from holdbook.demo import SNAPSHOT, TRADES
from holdbook.errors import HoldbookError
from holdbook.ledger import replay
from holdbook.market import fetch, load_prices, parse_quotes
from holdbook.models import D, Tx
from holdbook.report import atomic_write, build, html_report
from holdbook.reconcile import check_account

ENV = {"HOLDBOOK_NOW": "2026-10-29T12:00:00Z"}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, ENV, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.cfg = load_config(data_dir=self.tmp.name, today="2026-10-29")

    def write(self, name, text, enc="utf-8"):
        p = self.dir / name
        p.write_text(text, encoding=enc)
        return p

    def invoke(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = main(["--data-dir", self.tmp.name, "--today", "2026-10-29", *args])
        return rc, out.getvalue(), err.getvalue()


class ConfigDbEdges(Base):
    def test_bad_log_level_message(self):
        with mock.patch.dict(os.environ, {"HOLDBOOK_LOG_LEVEL": "loud"}):
            with self.assertRaises(HoldbookError) as cm:
                load_config(data_dir=self.tmp.name)
        self.assertEqual(cm.exception.code, "bad_config")
        self.assertIn("HOLDBOOK_LOG_LEVEL", cm.exception.message)

    def test_init_modes(self):
        self.assertTrue(db.init_db(self.cfg))
        self.assertEqual(stat.S_IMODE(self.cfg.data_dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.cfg.db_path.stat().st_mode), 0o600)

    def test_schema_two_conflict(self):
        db.init_db(self.cfg)
        conn = sqlite3.connect(self.cfg.db_path)
        conn.execute("UPDATE meta SET value='2' WHERE key='schema_version'")
        conn.commit()
        conn.close()
        with self.assertRaises(HoldbookError) as cm:
            db.connect(self.cfg)
        self.assertEqual(cm.exception.code, "conflict")

    def test_unrelated_sqlite_conflict(self):
        conn = sqlite3.connect(self.cfg.db_path)
        conn.execute("CREATE TABLE other(x)")
        conn.commit()
        conn.close()
        with self.assertRaises(HoldbookError) as cm:
            db.init_db(self.cfg)
        self.assertEqual(cm.exception.code, "conflict")

    def test_backup_existing_and_reset_guard(self):
        self.assertEqual(self.invoke("init")[0], 0)
        target = self.dir / "b.db"
        target.write_text("old")
        rc, out, err = self.invoke("backup", "--out", str(target))
        self.assertEqual(rc, 2)
        self.assertEqual(target.read_text(), "old")
        rc, out, err = self.invoke("reset")
        self.assertEqual(rc, 2)
        self.assertTrue(self.cfg.db_path.exists())


class CsvEdges(Base):
    def test_gb18030_semicolon(self):
        p = self.dir / "gb.csv"
        p.write_bytes("日期;操作;代码;数量;价格;金额\n2025-01-02;买入;600519;10;100;1000\n".encode("gb18030"))
        r = parse_trades(p, "A", self.cfg)
        self.assertEqual(len(r.txs), 1)
        self.assertEqual(r.txs[0].code, "600519.SH")

    def test_same_file_identical_rows_both(self):
        p = self.write("t.csv", "日期,操作,代码,数量,价格\n2025-01-01,买入,600519,10,10\n2025-01-01,买入,600519,10,10\n")
        r = parse_trades(p, "A", self.cfg)
        self.assertEqual(len(r.txs), 2)
        self.assertNotEqual(r.txs[0].row_hash, r.txs[1].row_hash)

    def test_row_limit(self):
        cfg = replace(self.cfg, max_rows=1)
        p = self.write("t.csv", "日期,操作,代码,数量,价格\n2025-01-01,买入,600519,10,10\n2025-01-02,买入,600519,10,10\n")
        with self.assertRaises(HoldbookError) as cm:
            parse_trades(p, "A", cfg)
        self.assertEqual(cm.exception.code, "bad_csv")

    def test_snapshot_duplicate_warns_last(self):
        p = self.write("s.csv", "代码,数量,成本价\n600519,100,10\n600519,110,11\n")
        r = parse_snapshots(p, self.cfg)
        self.assertEqual(len(r.rows), 1)
        self.assertEqual(r.rows[0].quantity, D("110.0000"))
        self.assertEqual(r.duplicates, 1)

    def test_snapshot_missing_code(self):
        p = self.write("s.csv", "数量,成本价\n100,10\n")
        with self.assertRaises(HoldbookError) as cm:
            parse_snapshots(p, self.cfg)
        self.assertEqual(cm.exception.code, "missing_columns")

    def test_future_snapshot(self):
        p = self.write("s.csv", "代码,数量\n600519,100\n")
        t = self.write("t.csv", "日期,操作,代码,数量,价格\n2025-01-01,买入,600519,10,10\n")
        self.assertEqual(self.invoke("init")[0], 0)
        self.assertEqual(self.invoke("import", str(t), "--account", "A")[0], 0)
        rc, out, err = self.invoke("snapshot", str(p), "--account", "A", "--as-of", "2026-12-01")
        self.assertEqual(rc, 2)
        self.assertIn("bad_date", err)


class LedgerReconcileEdges(Base):
    def test_rights_issue_adds_cost(self):
        txs = [Tx(date(2025, 1, 1), "buy", "600519.SH", D("100"), D("10"), D("-1000"), D("5")),
               Tx(date(2025, 2, 1), "rights_issue", "600519.SH", D("10"), D("5"), D("-50"), D("0"))]
        st = replay(txs, date(2026, 1, 1)).states["600519.SH"]
        self.assertEqual(st.quantity, D("110.0000"))
        self.assertEqual(st.total_cost, D("1055.00"))

    def test_tolerance_boundary(self):
        txs = [Tx(date(2025, 1, 1), "buy", "600519.SH", D("100"), D("10"), D("-1000"), D("0"))]
        snaps = [{"code": "600519.SH", "quantity": D("100"), "cost_price": D("11.00"), "name": "x", "source_file": "s"}]
        r = check_account("A", date(2026, 1, 1), txs, snaps, self.cfg)
        self.assertEqual(r["items"][0].status, "matched")
        snaps[0]["cost_price"] = D("11.01")
        r = check_account("A", date(2026, 1, 1), txs, snaps, self.cfg)
        self.assertEqual(r["items"][0].status, "cost_off")

    def test_dividend_and_stale_hints(self):
        txs = [Tx(date(2025, 1, 1), "buy", "600519.SH", D("100"), D("10"), D("-1000"), D("0")),
               Tx(date(2026, 9, 1), "dividend", "600519.SH", D("0"), D("0"), D("100"), D("0"))]
        snaps = [{"code": "600519.SH", "quantity": D("130"), "cost_price": D("10"), "name": "x", "source_file": "s"}]
        r = check_account("A", date(2026, 10, 29), txs, snaps, self.cfg)
        hints = " ".join(r["items"][0].hints)
        self.assertIn("分红", hints)
        self.assertIn("2026-09-01", hints)

    def test_explain_multi_account_requires_account(self):
        self.assertEqual(self.invoke("init")[0], 0)
        p = self.write("t.csv", "日期,操作,代码,数量,价格\n2025-01-01,买入,600519,10,10\n")
        self.assertEqual(self.invoke("import", str(p), "--account", "A")[0], 0)
        self.assertEqual(self.invoke("import", str(p), "--account", "B")[0], 0)
        rc, out, err = self.invoke("explain", "600519.SH")
        self.assertEqual(rc, 2)
        self.assertIn("多个账户", err)


class MockAndReportEdges(Base):
    def test_mock_outside_table_is_ten(self):
        db.init_db(self.cfg)
        conn = db.connect(self.cfg)
        try:
            prices, warnings, live, failures = load_prices(conn, self.cfg, ["600000.SH"], date(2026, 10, 29), "mock")
            self.assertEqual(prices["600000.SH"], D("10.00"))
            self.assertFalse(warnings)
            self.assertIsNone(live)
            self.assertFalse(failures)
        finally:
            conn.close()

    def test_none_match_skipped(self):
        q = parse_quotes('v_pv_none_match="1";')
        self.assertEqual(q, {})

    def test_csp_escape_and_mode(self):
        db.init_db(self.cfg)
        conn = db.connect(self.cfg)
        try:
            with db.transaction(conn):
                aid, _ = db.ensure_account(conn, "A", "", self.cfg.now_iso())
                db.upsert_instrument(conn, "600519.SH", "<script>x</script>", "sh_main", self.cfg.now_iso())
                db.insert_tx(conn, aid, Tx(date(2025, 1, 1), "buy", "600519.SH", D("100"), D("10"), D("-1000"), D("0")), self.cfg.now_iso())
            data = build(conn, self.cfg, None, date(2026, 10, 29), "off", None)
            html = html_report(data)
            self.assertIn("Content-Security-Policy", html)
            self.assertIn("default-src 'none'", html)
            self.assertIn("&lt;script&gt;x&lt;/script&gt;", html)
            self.assertNotIn("<script>x</script>", html)
        finally:
            conn.close()

    def test_atomic_failure_cleanup_and_mode(self):
        out = self.dir / "r.html"
        atomic_write(out, "<html>ok</html>")
        self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o600)
        bad = self.dir / "bad.html"
        def boom(a, b):
            raise OSError("fail")
        with self.assertRaises(OSError):
            atomic_write(bad, "x", replace=boom)
        self.assertFalse(bad.exists())
        self.assertEqual(list(self.dir.glob(".bad.html.*.tmp")), [])


class ImportEdges(Base):
    def test_account_too_long_before_db(self):
        p = self.write("t.csv", "日期,操作,代码,数量,价格\n2025-01-01,买入,600519,10,10\n")
        rc, out, err = self.invoke("import", str(p), "--account", "x" * 41)
        self.assertEqual(rc, 2)
        self.assertIn("40", err)

    def test_transaction_rollback_on_insert_failure(self):
        self.assertEqual(self.invoke("init")[0], 0)
        p = self.write("t.csv", "日期,操作,代码,数量,价格\n2025-01-01,买入,600519,10,10\n")
        with mock.patch("holdbook.cli.insert_tx", side_effect=RuntimeError("boom")):
            rc, out, err = self.invoke("import", str(p), "--account", "A")
        self.assertEqual(rc, 1)
        conn = db.connect(self.cfg)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0], 0)
        finally:
            conn.close()

    def test_backup_integrity_and_mode(self):
        self.assertEqual(self.invoke("init")[0], 0)
        out = self.dir / "b.db"
        rc, stdout, err = self.invoke("backup", "--out", str(out))
        self.assertEqual(rc, 0, err)
        self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o600)
        conn = sqlite3.connect(out)
        try:
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        finally:
            conn.close()


class CommandEdges(Base):
    def test_file_level_error_commits_nothing(self):
        good = self.write("good.csv", "日期,操作,代码,数量,价格\n2025-01-01,买入,600519,10,10\n")
        bad = self.write("bad.csv", "日期,数量\n2025-01-01,10\n")
        self.assertEqual(self.invoke("init")[0], 0)
        rc, out, err = self.invoke("import", str(good), str(bad), "--account", "A")
        self.assertEqual(rc, 2)
        conn = db.connect(self.cfg)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0], 0)
        finally:
            conn.close()

    def test_snapshot_dry_run(self):
        t = self.write("t.csv", "日期,操作,代码,数量,价格\n2025-01-01,买入,600519,10,10\n")
        s = self.write("s.csv", "代码,数量\n600519,10\n")
        self.assertEqual(self.invoke("init")[0], 0)
        self.assertEqual(self.invoke("import", str(t), "--account", "A")[0], 0)
        rc, out, err = self.invoke("snapshot", str(s), "--account", "A", "--dry-run")
        self.assertEqual(rc, 0)
        conn = db.connect(self.cfg)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0], 0)
        finally:
            conn.close()

    def test_empty_commands(self):
        self.assertEqual(self.invoke("init")[0], 0)
        rc, out, err = self.invoke("check")
        self.assertEqual(rc, 0)
        self.assertIn("还没有", err + out)
        rc, out, err = self.invoke("report")
        self.assertEqual(rc, 1)
        self.assertIn("no_data", err)

    def test_reset_removes_all_three(self):
        self.assertEqual(self.invoke("init")[0], 0)
        for suffix in ("-wal", "-shm"):
            Path(str(self.cfg.db_path) + suffix).write_text("x")
        rc, out, err = self.invoke("reset", "--yes")
        self.assertEqual(rc, 0)
        for suffix in ("", "-wal", "-shm"):
            self.assertFalse(Path(str(self.cfg.db_path) + suffix).exists())


class ReportRegressionTests(Base):
    def test_mock_price_cache_is_committed(self):
        self.assertEqual(self.invoke("init")[0], 0)
        self.assertEqual(self.invoke("import", str(self.write("t.csv", TRADES)), "--account", "华泰")[0], 0)
        rc, out, err = self.invoke("report", "--market", "mock", "--json")
        self.assertEqual(rc, 0, out + err)
        conn = db.connect(self.cfg)
        try:
            n = conn.execute("SELECT count(*) FROM prices").fetchone()[0]
        finally:
            conn.close()
        self.assertGreaterEqual(n, 4)

    def test_report_after_full_liquidation(self):
        self.assertEqual(self.invoke("init")[0], 0)
        csv = ("日期,操作,代码,数量,价格,金额,佣金\n"
               "2026-01-01,买入,600519,100,10.00,1000.00,5.00\n"
               "2026-02-01,卖出,600519,100,12.00,1200.00,6.00\n")
        self.assertEqual(self.invoke("import", str(self.write("round.csv", csv)), "--account", "华泰")[0], 0)
        rc, out, err = self.invoke("report", "--market", "mock", "--json")
        self.assertEqual(rc, 0, out + err)
        data = json.loads(out)
        self.assertEqual(data["summary"]["marketValue"], "0.00")
        self.assertNotEqual(data["summary"]["realizedPnl"], "0.00")

    def test_amount_only_uses_amount_as_gross(self):
        csv = ("日期,操作,代码,数量,金额\n"
               "2026-01-01,买入,600519,10000,12345.67\n")
        parsed = parse_trades(self.write("amount.csv", csv), "华泰", self.cfg)
        self.assertEqual(len(parsed.txs), 1)
        tx = parsed.txs[0]
        self.assertEqual(tx.gross, D("12345.67"))
        st = replay([tx], date(2026, 10, 29)).states["600519.SH"]
        self.assertEqual(st.total_cost, D("12345.67"))

    def test_csv_physical_line_numbers(self):
        csv = ("日期,操作,代码,数量,价格,金额,佣金\n"
               "# 注释\n"
               "\n"
               "2026-01-01,买入,600519,-,10.00,,0\n")
        parsed = parse_trades(self.write("lines.csv", csv), "华泰", self.cfg)
        self.assertEqual(parsed.errors[0][0], 4)
        self.assertEqual(parsed.txs, [])

    def test_extra_fields_and_blank_operation_are_errors(self):
        csv = ("日期,操作,代码,数量,价格,金额,佣金\n"
               "2026-01-01,买入,600519,100,10.00,,0,EXTRA\n"
               "2026-01-02,,600519,100,10.00,,0\n")
        parsed = parse_trades(self.write("extra.csv", csv), "华泰", self.cfg)
        self.assertEqual(len(parsed.errors), 2)
        self.assertEqual(parsed.txs, [])


class ReviewRegressionTests(Base):
    def test_manual_price_row_errors_keep_good_rows(self):
        self.assertEqual(self.invoke("init")[0], 0)
        self.assertEqual(self.invoke("import", str(self.write("t.csv", TRADES)), "--account", "华泰")[0], 0)
        p = self.write("prices.csv", "code,close\n600519.SH,1400\n000001.SZ,BAD\n")
        rc, out, err = self.invoke("report", "--prices", str(p), "--json")
        self.assertEqual(rc, 2, out + err)
        data = json.loads(out)
        self.assertEqual(data["positions"][0]["price"], "1400.0000")

    def test_quote_price_cap_and_name_length(self):
        name = "名" * 80
        text = f'v_sh600519="1~{name}~600519~2000000~1~1~1";'
        self.assertEqual(parse_quotes(text), {})
        text = f'v_sh600519="1~{name}~600519~100~1~1~1";'
        q = parse_quotes(text)["sh600519"]
        self.assertEqual(len(q[0]), 40)

    def test_invalid_today_env_is_bad_config(self):
        with mock.patch.dict(os.environ, {"HOLDBOOK_TODAY": "2026-13-01"}):
            with self.assertRaises(HoldbookError) as cm:
                load_config(data_dir=self.tmp.name)
        self.assertEqual(cm.exception.code, "bad_config")

    def test_account_newline_and_broker_length(self):
        self.assertEqual(self.invoke("init")[0], 0)
        rc, out, err = self.invoke("import", str(self.write("t.csv", TRADES)), "--account", "A\nB")
        self.assertEqual(rc, 2)
        self.assertIn("bad_config", err)
        rc, out, err = self.invoke("import", str(self.write("t2.csv", TRADES)), "--account", "华泰", "--broker", "x" * 41)
        self.assertEqual(rc, 2)
        self.assertIn("bad_config", err)


class FinalReviewTests(Base):
    def test_xirr_same_date_is_no_sign_change(self):
        from holdbook.metrics import xirr
        self.assertEqual(xirr([(date(2024, 1, 1), D("-1000")), (date(2024, 1, 1), D("1000"))]), (None, "no_sign_change"))

    def test_trade_ignores_snapshot_only_qty_alias(self):
        csv = ("成交日期,操作,证券代码,成交数量,股票余额,成交价格,成交金额\n"
               "2026-01-01,买入,600519,100,150,10.00,1000.00\n")
        parsed = parse_trades(self.write("alias.csv", csv), "华泰", self.cfg)
        self.assertEqual(len(parsed.txs), 1)
        self.assertEqual(parsed.txs[0].quantity, D("100"))

    def test_init_keeps_existing_dir_mode(self):
        d = self.dir / "shared"
        d.mkdir(mode=0o755)
        os.chmod(d, 0o755)
        rc, out, err = self.invoke("--data-dir", str(d), "init")
        self.assertEqual(rc, 0, err)
        self.assertEqual(stat.S_IMODE(d.stat().st_mode), 0o755)

    def test_display_rounds_half_up(self):
        from holdbook.models import fmt_money, fmt_pct
        self.assertEqual(fmt_money(D("10.005")), "10.01")
        self.assertEqual(fmt_pct(D("0.12345")), "12.35%")

    def test_control_chars_stripped(self):
        csv = ("日期,操作,代码,数量,价格,金额,佣金,名称\n"
               "2026-01-01,买入,600519,100,10.00,,0,\x1b[31mEVIL\x1b[0m\n")
        parsed = parse_trades(self.write("ctrl.csv", csv), "华泰", self.cfg)
        self.assertNotIn("\x1b", parsed.txs[0].name)
