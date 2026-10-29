import os
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest import mock

from holdbook import db
from holdbook.config import load_config
from holdbook.csvio import (classify, normalize_code, parse_date, parse_number,
                            parse_prices, parse_snapshots, parse_trades)
from holdbook.errors import HoldbookError
from holdbook.ledger import explain, replay
from holdbook.metrics import xirr
from holdbook.models import D, Tx
from holdbook.reconcile import check_account

ENV = {"HOLDBOOK_NOW": "2026-10-29T12:00:00Z"}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = mock.patch.dict(os.environ, ENV, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.cfg = load_config(data_dir=self.tmp.name, today="2026-10-29")

    def write(self, name, text, enc="utf-8"):
        p = Path(self.tmp.name) / name
        p.write_text(text, encoding=enc)
        return p


class ConfigTests(Base):
    def test_defaults(self):
        self.assertEqual(self.cfg.market, "off")
        self.assertEqual(self.cfg.timeout, 8)
        self.assertEqual(self.cfg.retries, 2)
        self.assertEqual(self.cfg.cost_tol_abs, Decimal("1.00"))
        self.assertEqual(self.cfg.today, date(2026, 10, 29))
        self.assertTrue(str(self.cfg.db_path).endswith("holdbook.db"))

    def test_bad_market_and_int(self):
        with mock.patch.dict(os.environ, {"HOLDBOOK_MARKET": "bad"}):
            with self.assertRaises(HoldbookError) as cm:
                load_config(data_dir=self.tmp.name)
            self.assertEqual(cm.exception.code, "bad_config")
        with mock.patch.dict(os.environ, {"HOLDBOOK_MARKET_TIMEOUT": "0"}):
            with self.assertRaises(HoldbookError):
                load_config(data_dir=self.tmp.name)

    def test_bad_now(self):
        with mock.patch.dict(os.environ, {"HOLDBOOK_NOW": "2026-01-01T00:00:00"}):
            with self.assertRaises(HoldbookError):
                load_config(data_dir=self.tmp.name)


class CsvTests(Base):
    def test_codes(self):
        self.assertEqual(normalize_code("600519"), ("600519.SH", "sh_main"))
        self.assertEqual(normalize_code("sh600519"), ("600519.SH", "sh_main"))
        self.assertEqual(normalize_code("600519.SH"), ("600519.SH", "sh_main"))
        self.assertEqual(normalize_code("000001"), ("000001.SZ", "sz_main"))
        self.assertEqual(normalize_code("300750"), ("300750.SZ", "sz_gem"))
        self.assertEqual(normalize_code("688981"), ("688981.SH", "sh_star"))
        self.assertEqual(normalize_code("510300"), ("510300.SH", "etf"))
        self.assertEqual(normalize_code("159915"), ("159915.SZ", "etf"))
        self.assertEqual(normalize_code("113050"), ("113050.SH", "bond_cb"))
        self.assertEqual(normalize_code("CASH.CASH"), ("CASH.CASH", "cash"))
        for bad in ("000001.SH", "60051", "ABCDEF", "900001"):
            with self.assertRaises(ValueError):
                normalize_code(bad)

    def test_numbers(self):
        self.assertEqual(parse_number("1,234.50", "x"), Decimal("1234.50"))
        self.assertEqual(parse_number("(1,234.50)", "x"), Decimal("-1234.50"))
        self.assertEqual(parse_number("１２３", "x"), Decimal("123"))
        for bad in ("", "NaN", "1e3", "12%"):
            with self.assertRaises(ValueError):
                parse_number(bad, "x")

    def test_dates(self):
        self.assertEqual(parse_date("2026-01-02"), date(2026, 1, 2))
        self.assertEqual(parse_date("2026/01/02"), date(2026, 1, 2))
        self.assertEqual(parse_date("20260102"), date(2026, 1, 2))
        with self.assertRaises(ValueError):
            parse_date("2026-13-01")

    def test_classify(self):
        self.assertEqual(classify("证券买入"), "buy")
        self.assertEqual(classify("证券卖出"), "sell")
        self.assertEqual(classify("红利入账"), "dividend")
        self.assertEqual(classify("送股入账"), "bonus_share")
        self.assertEqual(classify("红利税"), "fee")
        self.assertEqual(classify("利息入账"), "interest")
        self.assertEqual(classify("配股缴款"), "rights_issue")
        self.assertEqual(classify("中签"), "ipo_allot")
        self.assertEqual(classify("银证转入"), "transfer_in")
        self.assertEqual(classify("申购"), "other")
        with self.assertRaises(ValueError):
            classify("分红送股")

    def test_trade_parse_and_bom(self):
        p = self.write("t.csv", "\ufeff成交日期,操作,证券代码,证券名称,成交数量,成交价格,成交金额,手续费\n"
                      "2025-03-10,证券买入,600519,贵州茅台,100,1580,158000,5\n"
                      "2025-09-02,证券卖出,600519,贵州茅台,40,1720,68800,5\n", enc="utf-8")
        r = parse_trades(p, "华泰", self.cfg)
        self.assertEqual(r.errors, [])
        self.assertEqual(len(r.txs), 2)
        self.assertEqual(r.txs[0].code, "600519.SH")
        self.assertEqual(r.txs[0].amount, Decimal("-158000.00"))
        self.assertEqual(r.txs[1].quantity, Decimal("-40.0000"))
        self.assertEqual(r.txs[1].amount, Decimal("68800.00"))

    def test_trade_row_errors(self):
        p = self.write("t.csv", "日期,操作,代码,数量,价格,金额\n"
                      "2025-01-01,买入,600519,100,,,\n"
                      "2025-01-02,买入,600519,,10,1000\n"
                      "2025-01-03,买入,600519,10,10,999\n"
                      "2025-01-04,卖出,600519,10,10,100\n")
        r = parse_trades(p, "A", self.cfg)
        self.assertEqual(len(r.errors), 3)
        self.assertEqual(r.rows_read, 4)

    def test_fee_and_interest(self):
        p = self.write("t.csv", "日期,操作,代码,金额\n"
                      "2025-01-01,费用,, -20\n"
                      "2025-01-02,利息,,36.5\n"
                      "2025-01-03,利息,,-10\n")
        r = parse_trades(p, "A", self.cfg)
        self.assertEqual(r.errors, [])
        self.assertEqual([t.kind for t in r.txs], ["fee", "interest", "fee"])
        self.assertEqual(r.txs[0].code, "CASH.CASH")

    def test_snapshot_no_date(self):
        p = self.write("s.csv", "证券代码,证券名称,持仓数量,成本价\n600519,贵州茅台,100,10.5\n")
        r = parse_snapshots(p, self.cfg)
        self.assertEqual(r.errors, [])
        self.assertEqual(r.rows[0].code, "600519.SH")
        self.assertEqual(r.rows[0].quantity, Decimal("100.0000"))

    def test_snapshot_duplicates(self):
        p = self.write("s.csv", "代码,数量,成本价\n600519,100,10\n600519,120,11\n")
        r = parse_snapshots(p, self.cfg)
        self.assertEqual(r.duplicates, 1)
        self.assertEqual(r.rows[0].quantity, Decimal("120.0000"))

    def test_tsv_delimiter(self):
        p = self.write("t.tsv", "日期\t操作\t代码\t数量\t价格\n2025-01-01\t买入\t600519\t10\t10\n")
        r = parse_trades(p, "A", self.cfg)
        self.assertEqual(len(r.txs), 1)

    def test_prices(self):
        p = self.write("p.csv", "code,close,date\n600519,1500,2026-01-01\n")
        rows, errors = parse_prices(p, self.cfg, date(2026, 10, 29))
        self.assertEqual(errors, [])
        self.assertEqual(rows[0].close, Decimal("1500.0000"))


class LedgerTests(Base):
    def test_roundtrip_and_12dp(self):
        txs = [
            Tx(date(2025, 1, 1), "buy", "600519.SH", D("3"), D("10"), D("-30"), D("1")),
            Tx(date(2025, 1, 2), "sell", "600519.SH", D("-1"), D("11"), D("11"), D("0")),
        ]
        r = replay(txs, date(2026, 1, 1))
        st = r.states["600519.SH"]
        self.assertEqual(st.quantity, D("2"))
        self.assertEqual(st.realized, D("0.666666666667"))
        self.assertEqual(st.total_cost, D("20.666666666667"))
        self.assertEqual(r.realized, D("0.666666666667"))

    def test_bonus_dividend(self):
        txs = [
            Tx(date(2025, 1, 1), "buy", "X.SH", D("100"), D("10"), D("-1000"), D("5")),
            Tx(date(2025, 2, 1), "bonus_share", "X.SH", D("10"), D("0"), D("0")),
            Tx(date(2025, 3, 1), "dividend", "X.SH", D("0"), D("0"), D("50")),
        ]
        r = replay(txs, date(2026, 1, 1))
        st = r.states["X.SH"]
        self.assertEqual(st.quantity, D("110"))
        self.assertEqual(st.total_cost, D("1005"))
        self.assertEqual(st.dividends, D("50"))

    def test_oversell(self):
        txs = [Tx(date(2025, 1, 1), "sell", "X.SH", D("-1"), D("10"), D("10"))]
        with self.assertRaises(HoldbookError) as cm:
            replay(txs, date(2026, 1, 1))
        self.assertEqual(cm.exception.code, "ledger_negative")

    def test_explain(self):
        txs = [
            Tx(date(2025, 3, 10), "buy", "600519.SH", D("100"), D("1580"), D("-158000"), D("5")),
            Tx(date(2025, 9, 2), "sell", "600519.SH", D("-100"), D("1720"), D("172000"), D("5")),
        ]
        rows, st = explain(txs, "600519.SH", date(2026, 1, 1))
        self.assertEqual(len(rows), 2)
        self.assertEqual(st.realized, D("13990.00"))
        self.assertIsNone(st.avg_cost)


class MetricsTests(unittest.TestCase):
    def test_xirr(self):
        r, why = xirr([(date(2025, 1, 1), D("-1000")), (date(2026, 1, 1), D("1100"))])
        self.assertIsNone(why)
        self.assertAlmostEqual(r, 0.10, places=3)

    def test_xirr_no_solution(self):
        self.assertEqual(xirr([]), (None, "no_cashflow"))
        self.assertEqual(xirr([(date(2025, 1, 1), D("-1"))])[1], "single_direction")


class DbTests(Base):
    def test_init_idempotent_and_backup(self):
        self.assertTrue(db.init_db(self.cfg))
        self.assertFalse(db.init_db(self.cfg))
        conn = db.connect(self.cfg)
        out = Path(self.tmp.name) / "b.db"
        size = db.backup_db(conn, self.cfg, out)
        self.assertGreater(size, 0)
        conn.close()
        self.assertTrue(out.exists())

    def test_data_dir_is_file(self):
        f = Path(self.tmp.name) / "notdir"
        f.write_text("x")
        cfg = load_config(data_dir=str(f), today="2026-10-29")
        with self.assertRaises(HoldbookError) as cm:
            db.init_db(cfg)
        self.assertEqual(cm.exception.code, "data_dir_unwritable")

    def test_duplicate_header(self):
        p = self.write("t.csv", "日期,操作,代码,数量,数量\n2025-01-01,买入,600519,10,10\n")
        with self.assertRaises(HoldbookError) as cm:
            parse_trades(p, "A", self.cfg)
        self.assertEqual(cm.exception.code, "duplicate_column")

    def test_non_tool_db(self):
        self.cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.cfg.db_path.write_text("not sqlite")
        with self.assertRaises(HoldbookError) as cm:
            db.init_db(self.cfg)
        self.assertEqual(cm.exception.code, "db_corrupt")


class ReconcileTests(Base):
    def snaps(self, rows):
        return [{"code": c, "quantity": q, "cost_price": p, "name": n, "source_file": "s.csv"} for c, q, p, n in rows]

    def test_statuses(self):
        txs = [Tx(date(2025, 1, 1), "buy", "600519.SH", D("100"), D("10"), D("-1000"), D("5")),
               Tx(date(2025, 1, 1), "buy", "000001.SZ", D("100"), D("10"), D("-1000"), D("5"))]
        snaps = self.snaps([("600519.SH", D("130"), D("10.05"), "贵州茅台"),
                            ("000001.SZ", D("100"), D("9"), "平安银行"),
                            ("510300.SH", D("100"), D("4"), "ETF")])
        r = check_account("A", date(2026, 10, 29), txs, snaps, self.cfg)
        status = {i.code: i.status for i in r["items"]}
        self.assertEqual(status["600519.SH"], "qty_missing")
        self.assertEqual(status["000001.SZ"], "cost_off")
        self.assertEqual(status["510300.SH"], "only_snapshot")

    def test_null_propagation(self):
        txs = [Tx(date(2025, 1, 1), "buy", "600519.SH", D("100"), D("10"), D("-1000"), D("5"))]
        r = check_account("A", date(2026, 1, 1), txs, [], self.cfg)
        item = r["items"][0]
        self.assertEqual(item.status, "only_ledger")
        self.assertIsNone(item.qty_snap)
        self.assertIsNone(item.qty_diff)

    def test_bonus_hint(self):
        txs = [Tx(date(2025, 1, 1), "buy", "600519.SH", D("100"), D("10"), D("-1000"), D("5")),
               Tx(date(2026, 9, 12), "bonus_share", "600519.SH", D("300"), D("0"), D("0"))]
        snaps = self.snaps([("600519.SH", D("700"), D("10.05"), "贵州茅台")])
        r = check_account("A", date(2026, 10, 29), txs, snaps, self.cfg)
        self.assertEqual(r["items"][0].status, "qty_missing")
        self.assertTrue(any("2026-09-12" in h for h in r["items"][0].hints))
