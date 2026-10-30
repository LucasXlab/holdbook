from __future__ import annotations

import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal

from .config import Config
from .db import price_get, price_put, upsert_instrument
from .errors import err
from .models import D, clean_text

MOCK = {
    "600519.SH": ("贵州茅台", D("1354.00")),
    "000001.SZ": ("平安银行", D("11.54")),
    "510300.SH": ("沪深300ETF", D("4.001")),
    "113050.SH": ("转债", D("144.967")),
    "159915.SZ": ("创业板ETF", D("2.535")),
    "000858.SZ": ("五粮液", D("118.75")),
    "601318.SH": ("中国平安", D("62.30")),
    "512880.SH": ("证券ETF", D("1.234")),
}

TOTAL_CAP = 30.0
BATCH = 60


def _board(code: str) -> str:
    from .csvio import normalize_code
    try:
        return normalize_code(code)[1]
    except ValueError:
        return "unknown"


def qt_code(code: str) -> str:
    digits, ex = code.split(".")
    return ex.lower() + digits


def parse_quotes(text: str) -> dict[str, tuple[str, Decimal]]:
    out: dict[str, tuple[str, Decimal]] = {}
    for m in re.finditer(r'v_([a-zA-Z0-9_]+)="([^"]*)"', text):
        key, payload = m.group(1), m.group(2)
        if key.startswith("pv_none_match"):
            continue
        parts = payload.split("~")
        if len(parts) < 5:
            continue
        name = clean_text(parts[1].strip())[:40]
        try:
            price = D(parts[3])
        except Exception:
            continue
        if not price.is_finite() or price <= 0 or price > D("1000000"):
            continue
        code = key[1:] if key.startswith("v_") else key
        out[code] = (name, price)
    return out


@dataclass
class FetchResult:
    quotes: dict[str, tuple[str, Decimal]]
    failures: dict[str, str]
    fetched_at: str | None


def fetch(codes: list[str], cfg: Config, sleep=time.sleep, opener=urllib.request.urlopen) -> FetchResult:
    quotes: dict[str, tuple[str, Decimal]] = {}
    failures: dict[str, str] = {}
    started = time.monotonic()
    ordered = list(dict.fromkeys(codes))
    fetched_at = None
    for i in range(0, len(ordered), BATCH):
        batch = ordered[i:i + BATCH]
        if i and time.monotonic() - started < TOTAL_CAP:
            sleep(1.0)
        if time.monotonic() - started > TOTAL_CAP:
            for c in batch:
                failures.setdefault(c, "总时长超过 30 秒")
            continue
        mapping = {qt_code(c): c for c in batch}
        url = cfg.quote_base + "=" + ",".join(mapping)
        last = ""
        ok = False
        deadline = started + TOTAL_CAP
        for attempt in range(cfg.retries + 1):
            if time.monotonic() >= deadline:
                last = "总时长超过 30 秒"
                break
            try:
                with opener(url, timeout=cfg.timeout) as resp:
                    raw = resp.read()
                    text = raw.decode("gb18030")
                parsed = parse_quotes(text)
                fetched_at = cfg.now_iso()
                got = set()
                for qt, canonical in mapping.items():
                    if qt in parsed:
                        quotes[canonical] = parsed[qt]
                        got.add(canonical)
                for c in batch:
                    if c not in got:
                        failures[c] = "返回中没有该代码"
                ok = True
                break
            except urllib.error.HTTPError as exc:
                last = f"HTTP {exc.code}"
                if exc.code not in (429,) and not (500 <= exc.code <= 599):
                    break
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if isinstance(exc, TimeoutError) or "timed out" in str(exc) or "timeout" in str(exc):
                    last = f"连接超时（{cfg.timeout} 秒）"
                else:
                    last = f"连接失败（{exc}）"
            except UnicodeDecodeError:
                last = "响应不是 GB18030"
                break
            if attempt < cfg.retries:
                delay = 2 ** attempt
                if time.monotonic() + delay >= deadline:
                    last = "总时长超过 30 秒"
                    break
                sleep(delay)
        if not ok:
            for c in batch:
                failures.setdefault(c, last or "行情获取失败")
    return FetchResult(quotes, failures, fetched_at)


def load_prices(conn, cfg: Config, codes: list[str], as_of: date, mode: str,
                manual: dict[str, Decimal] | None = None) -> tuple[dict[str, Decimal], list[dict], str | None, dict[str, str]]:
    """Return prices, warnings, live fetched_at, failures."""
    warnings: list[dict] = []
    prices: dict[str, Decimal] = {}
    failures: dict[str, str] = {}
    for code in codes:
        row = price_get(conn, code, as_of)
        if row is not None:
            prices[code] = D(row["close"])
    if manual:
        for code, close in manual.items():
            prices[code] = close
            price_put(conn, code, as_of, close, "manual", cfg.now_iso())

    live_at = None
    missing = [c for c in codes if c not in prices]
    if mode == "live" and missing:
        result = fetch(missing, cfg)
        live_at = result.fetched_at
        for code, (name, price) in result.quotes.items():
            prices[code] = price
            board = _board(code)
            upsert_instrument(conn, code, name or code, board, cfg.now_iso())
            price_put(conn, code, as_of, price, "live", cfg.now_iso())
        failures.update(result.failures)
        for code, msg in result.failures.items():
            warnings.append({"code": "market_partial", "message": f"{code} 行情失败：{msg}"})
            failures[code] = msg
    elif mode == "mock":
        for code in missing:
            name, price = MOCK.get(code, (code, D("10.00")))
            prices[code] = price
            upsert_instrument(conn, code, name, _board(code), cfg.now_iso())
            price_put(conn, code, as_of, price, "mock", cfg.now_iso())
    elif mode == "off":
        pass
    if mode == "live" and missing and not any(c in prices for c in missing):
        if not failures:
            failures = {c: "行情获取失败" for c in missing}
    return prices, warnings, live_at, failures
