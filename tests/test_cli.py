import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from holdbook.cli import main
from holdbook.demo import SNAPSHOT, TRADES

ENV = {"HOLDBOOK_NOW": "2026-10-29T12:00:00Z"}


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.trades = self.dir / "t.csv"
        self.snap = self.dir / "s.csv"
        self.trades.write_text(TRADES, encoding="utf-8")
        # deliberately 10 shares short, to exercise check --strict
        self.snap.write_text(SNAPSHOT, encoding="utf-8")
        self.env = mock.patch.dict(os.environ, ENV, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)

    def invoke(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = main(["--data-dir", self.tmp.name, "--today", "2026-10-29", *args])
        return rc, out.getvalue(), err.getvalue()

    def setup_data(self):
        self.assertEqual(self.invoke("init")[0], 0)
        self.assertEqual(self.invoke("import", str(self.trades), "--account", "华泰")[0], 0)
        self.assertEqual(self.invoke("snapshot", str(self.snap), "--account", "华泰", "--as-of", "2026-10-29")[0], 0)

    def test_init_version(self):
        rc, out, err = self.invoke("init")
        self.assertEqual(rc, 0)
        self.assertIn("已就绪", out)
        rc, out, err = self.invoke("version")
        self.assertEqual(rc, 0)
        self.assertIn("holdbook 0.1.0", out)
        rc, out, err = self.invoke("--json", "version")
        self.assertEqual(json.loads(out)["version"], "0.1.0")

    def test_import_and_idempotent(self):
        self.assertEqual(self.invoke("init")[0], 0)
        rc, out, err = self.invoke("import", str(self.trades), "--account", "华泰", "--broker", "华泰证券")
        self.assertEqual(rc, 0, out + err)
        self.assertIn("读取 9 行", out)
        rc, out, err = self.invoke("import", str(self.trades), "--account", "华泰")
        self.assertEqual(rc, 0)
        self.assertIn("跳过 9 笔", out)

    def test_import_json_and_dry_run(self):
        self.assertEqual(self.invoke("init")[0], 0)
        rc, out, err = self.invoke("--json", "import", str(self.trades), "--account", "华泰", "--dry-run")
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertEqual(data["totals"]["added"], 0)
        self.assertEqual(data["wouldAdd"], 9)
        rc, out, err = self.invoke("--json", "status")
        self.assertEqual(json.loads(out)["accounts"], [])

    def test_import_row_error_commits(self):
        self.assertEqual(self.invoke("init")[0], 0)
        bad = self.dir / "bad.csv"
        bad.write_text("日期,操作,代码,数量,价格,金额\n2025-01-01,买入,600519,10,10,100\n2025-01-02,买入,600519,,10,100\n", encoding="utf-8")
        rc, out, err = self.invoke("--json", "import", str(bad), "--account", "华泰")
        self.assertEqual(rc, 2)
        data = json.loads(out)
        self.assertTrue(data["committed"])
        self.assertEqual(data["files"][0]["added"], 1)
        self.assertEqual(len(data["files"][0]["errors"]), 1)

    def test_snapshot_check_strict(self):
        self.setup_data()
        rc, out, err = self.invoke("check")
        self.assertEqual(rc, 0)
        self.assertIn("差异 1 只", out)
        rc, out, err = self.invoke("check", "--strict")
        self.assertEqual(rc, 3)
        rc, out, err = self.invoke("--json", "check")
        data = json.loads(out)
        self.assertEqual(data["differed"], 1)
        item = next(i for i in data["accounts"][0]["items"] if i["code"] == "600519.SH")
        self.assertEqual(item["status"], "qty_extra")
        self.assertEqual(item["qtySnapshot"], "100.0000")

    def test_check_as_of_missing(self):
        self.setup_data()
        rc, out, err = self.invoke("--json", "check", "--as-of", "2026-01-01")
        self.assertEqual(rc, 2)
        self.assertEqual(json.loads(out)["error"]["code"], "no_data")

    def test_report_json_and_html(self):
        self.setup_data()
        rc, out, err = self.invoke("--json", "report", "--market", "mock")
        self.assertEqual(rc, 0, out + err)
        data = json.loads(out)
        self.assertEqual(data["method"], "holdbook-ledger-v1")
        self.assertEqual(data["summary"]["realizedPnl"], "293.50")
        self.assertEqual(data["summary"]["totalCost"], "197068.50")
        self.assertEqual(data["instruments"], 4)
        self.assertEqual(len(data["positions"]), 4)
        outfile = self.dir / "r.html"
        rc, out, err = self.invoke("report", "--market", "mock", "--format", "html", "--out", str(outfile))
        self.assertEqual(rc, 0)
        html = outfile.read_text(encoding="utf-8")
        self.assertIn("--bg:#fff", html)
        self.assertIn("viewport", html)
        self.assertIn("公式版本 holdbook-ledger-v1", html)

    def test_report_html_stdout(self):
        self.setup_data()
        rc, out, err = self.invoke("report", "--market", "mock", "--format", "html", "--out", "-")
        self.assertEqual(rc, 0)
        self.assertTrue(out.startswith("<!doctype html>"))
        self.assertIn("报告", err)

    def test_report_conflicts(self):
        self.setup_data()
        for args in (["report", "--open", "--format", "text"],
                     ["report", "--json", "--out", "a.html"],
                     ["report", "--format", "text", "--out", "a.html"]):
            rc, out, err = self.invoke(*args)
            self.assertEqual(rc, 2, args)

    def test_explain_and_status(self):
        self.setup_data()
        rc, out, err = self.invoke("--json", "explain", "600519.SH")
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertEqual(data["final"]["quantity"], "110.0000")
        self.assertEqual(data["final"]["realizedPnl"], "0.00")
        rc, out, err = self.invoke("--json", "status")
        data = json.loads(out)
        self.assertEqual(data["accounts"][0]["transactions"], 9)
        self.assertEqual(data["accounts"][0]["lastSnapshotDate"], "2026-10-29")

    def test_demo_backup_reset(self):
        demo_dir = self.dir / "demo"
        rc, out, err = self.invoke("demo", "--dir", str(demo_dir))
        self.assertEqual(rc, 0, out + err)
        self.assertTrue((demo_dir / "report.html").exists())
        rc, out, err = self.invoke("demo", "--dir", str(demo_dir))
        self.assertEqual(rc, 2)
        self.setup_data()
        rc, out, err = self.invoke("--json", "backup")
        self.assertEqual(rc, 0, out + err)
        path = json.loads(out)["path"]
        self.assertTrue(Path(path).exists())
        rc, out, err = self.invoke("--json", "reset", "--yes")
        self.assertEqual(rc, 0)
        self.assertEqual(len(json.loads(out)["deleted"]), 1)
        rc, out, err = self.invoke("status")
        self.assertEqual(rc, 1)

    def test_error_json(self):
        rc, out, err = self.invoke("--json", "status")
        self.assertEqual(rc, 1)
        self.assertEqual(json.loads(out)["error"]["code"], "no_data")
        rc, out, err = self.invoke("explain", "BAD")
        self.assertEqual(rc, 2)
        self.assertIn("bad_code", err)

    def test_manual_prices(self):
        self.setup_data()
        prices = self.dir / "p.csv"
        prices.write_text("code,close\n600519,1400\n000001,12\n510300,4.2\n000858,120\n", encoding="utf-8")
        rc, out, err = self.invoke("--json", "report", "--market", "off", "--prices", str(prices))
        self.assertEqual(rc, 0, out + err)
        data = json.loads(out)
        self.assertEqual(data["summary"]["priced"], 4)
        self.assertEqual(data["positions"][0]["price"], "1400.0000")
