import os
import tempfile
import threading
import time
import unittest
from datetime import date
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

from holdbook import db
from holdbook.config import load_config
from holdbook.market import fetch, load_prices, parse_quotes

ENV = {"HOLDBOOK_NOW": "2026-10-29T12:00:00Z"}


class Handler(BaseHTTPRequestHandler):
    mode = "ok"
    calls = 0

    def do_GET(self):
        type(self).calls += 1
        if self.mode == "500once" and type(self).calls == 1:
            self.send_response(500)
            self.end_headers()
            return
        if self.mode == "429once" and type(self).calls == 1:
            self.send_response(429)
            self.end_headers()
            return
        if self.mode == "404":
            self.send_response(404)
            self.end_headers()
            return
        if self.mode == "slow":
            time.sleep(2)
        if self.mode == "badprice":
            body = 'v_sh600519="1~贵州茅台~600519~0~1330.75~";'.encode("gb18030")
        elif self.mode == "bad":
            body = b"not a quote"
        else:
            body = 'v_sh600519="1~贵州茅台~600519~1354.00~1330.75~";\nv_sz000001="51~平安银行~000001~11.54~11.31~";'.encode("gb18030")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=GBK")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class MarketTests(unittest.TestCase):
    def setUp(self):
        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = mock.patch.dict(os.environ, {**ENV, "HOLDBOOK_QUOTE_BASE_URL": f"http://127.0.0.1:{self.server.server_port}/q"}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.cfg = load_config(data_dir=self.tmp.name, today="2026-10-29")
        Handler.calls = 0
        Handler.mode = "ok"

    def test_parse(self):
        q = parse_quotes('v_sh600519="1~贵州茅台~600519~1354.00~1330.75~";\nv_pv_none_match="1";')
        self.assertEqual(q["sh600519"][0], "贵州茅台")
        self.assertEqual(q["sh600519"][1], 1354)

    def test_fetch_ok(self):
        r = fetch(["600519.SH", "000001.SZ"], self.cfg, sleep=lambda _: None)
        self.assertEqual(len(r.quotes), 2)
        self.assertFalse(r.failures)
        self.assertEqual(r.quotes["600519.SH"][1], 1354)

    def test_retry_500(self):
        Handler.mode = "500once"
        r = fetch(["600519.SH"], self.cfg, sleep=lambda _: None)
        self.assertIn("600519.SH", r.quotes)
        self.assertEqual(Handler.calls, 2)

    def test_retry_429(self):
        Handler.mode = "429once"
        r = fetch(["600519.SH"], self.cfg, sleep=lambda _: None)
        self.assertIn("600519.SH", r.quotes)
        self.assertEqual(Handler.calls, 2)

    def test_404_no_retry(self):
        Handler.mode = "404"
        r = fetch(["600519.SH"], self.cfg, sleep=lambda _: None)
        self.assertIn("600519.SH", r.failures)
        self.assertEqual(Handler.calls, 1)

    def test_malformed(self):
        Handler.mode = "bad"
        r = fetch(["600519.SH"], self.cfg, sleep=lambda _: None)
        self.assertIn("600519.SH", r.failures)

    def test_bad_price(self):
        Handler.mode = "badprice"
        r = fetch(["600519.SH"], self.cfg, sleep=lambda _: None)
        self.assertIn("600519.SH", r.failures)

    def test_two_batches_for_120_codes(self):
        codes = [f"{600000+i}.SH" for i in range(120)]
        r = fetch(codes, self.cfg, sleep=lambda _: None)
        self.assertEqual(Handler.calls, 2)
        self.assertTrue(r.failures)

    def test_timeout(self):
        Handler.mode = "slow"
        cfg = load_config(data_dir=self.tmp.name, today="2026-10-29")
        object.__setattr__(cfg, "timeout", 1)
        object.__setattr__(cfg, "retries", 0)
        r = fetch(["600519.SH"], cfg, sleep=lambda _: None)
        self.assertIn("600519.SH", r.failures)

    def test_cache_and_mock(self):
        cfg = load_config(data_dir=self.tmp.name, today="2026-10-29")
        db.init_db(cfg)
        conn = db.connect(cfg)
        try:
            prices, warnings, live, failures = load_prices(conn, cfg, ["600519.SH"], date(2026, 10, 29), "mock")
            self.assertEqual(prices["600519.SH"], 1354)
            self.assertIsNone(live)
            self.assertFalse(warnings)
            prices2, _, _, _ = load_prices(conn, cfg, ["600519.SH"], date(2026, 10, 29), "off")
            self.assertEqual(prices2["600519.SH"], 1354)
        finally:
            conn.close()
