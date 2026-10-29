from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .errors import err

DEFAULT_DATA_DIR = "~/.local/share/holdbook"
DEFAULT_QUOTE_BASE = "https://qt.gtimg.cn/q"


def _env(name: str, default: str) -> str:
    v = os.environ.get(name)
    return default if v is None or v == "" else v


def _parse_int(name: str, raw: str, lo: int, hi: int) -> int:
    try:
        v = int(raw)
    except ValueError:
        raise err("bad_config", f"{name} 必须是 {lo}–{hi} 的整数，收到「{raw}」")
    if not (lo <= v <= hi):
        raise err("bad_config", f"{name} 必须是 {lo}–{hi} 的整数，收到「{raw}」")
    return v


def _parse_decimal(name: str, raw: str, lo: Decimal, hi: Decimal | None = None) -> Decimal:
    try:
        v = Decimal(raw)
    except InvalidOperation:
        raise err("bad_config", f"{name} 必须是数字，收到「{raw}」")
    if not v.is_finite() or v < lo or (hi is not None and v > hi):
        bound = f"{lo}" + (f"–{hi}" if hi is not None else " 以上")
        raise err("bad_config", f"{name} 必须在 {bound} 之间，收到「{raw}」")
    return v


def parse_date(raw: str, name: str = "日期") -> date:
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise err("bad_date", f"{name} 必须是 YYYY-MM-DD，收到「{raw}」")


def parse_now(raw: str | None) -> datetime:
    if not raw:
        return datetime.now(timezone.utc)
    try:
        v = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise err("bad_config", f"HOLDBOOK_NOW 必须是带时区的 ISO 8601 时间，收到「{raw}」")
    if v.tzinfo is None:
        raise err("bad_config", "HOLDBOOK_NOW 必须带时区，例如 2026-10-29T21:00:00Z")
    return v.astimezone(timezone.utc)


@dataclass(frozen=True)
class Config:
    data_dir: Path
    db_path: Path
    market: str
    quote_base: str
    timeout: int
    retries: int
    max_rows: int
    max_bytes: int
    cost_tol_pct: Decimal
    cost_tol_abs: Decimal
    today: date
    now: datetime
    log_level: str

    @property
    def report_dir(self) -> Path:
        return self.data_dir / "reports"

    def now_iso(self) -> str:
        return self.now.strftime("%Y-%m-%dT%H:%M:%SZ")

    def stamp(self) -> str:
        return self.now.strftime("%Y%m%d-%H%M%S")


def load_config(data_dir: str | None = None, today: str | None = None) -> Config:
    market = _env("HOLDBOOK_MARKET", "off")
    if market not in ("off", "live", "mock"):
        raise err("bad_config", f"HOLDBOOK_MARKET 只能是 off、live 或 mock，收到「{market}」")
    level = _env("HOLDBOOK_LOG_LEVEL", "warn")
    if level not in ("debug", "info", "warn", "error"):
        raise err("bad_config", f"HOLDBOOK_LOG_LEVEL 只能是 debug、info、warn 或 error，收到「{level}」")

    raw_dir = data_dir if data_dir is not None else _env("HOLDBOOK_DATA_DIR", DEFAULT_DATA_DIR)
    dd = Path(raw_dir).expanduser()
    if not dd.is_absolute():
        dd = Path.cwd() / dd
    dd = dd.resolve()

    raw_db = _env("HOLDBOOK_DB_PATH", "")
    dbp = Path(raw_db).expanduser() if raw_db else dd / "holdbook.db"
    if not dbp.is_absolute():
        dbp = Path.cwd() / dbp
    dbp = dbp.resolve()

    quote = _env("HOLDBOOK_QUOTE_BASE_URL", DEFAULT_QUOTE_BASE)
    if not (quote.startswith("http://") or quote.startswith("https://")):
        raise err("bad_config", "HOLDBOOK_QUOTE_BASE_URL 必须以 http:// 或 https:// 开头")

    raw_today = today if today is not None else _env("HOLDBOOK_TODAY", "")
    today_d = parse_date(raw_today, "--today") if raw_today else datetime.now().date()

    return Config(
        data_dir=dd,
        db_path=dbp,
        market=market,
        quote_base=quote,
        timeout=_parse_int("HOLDBOOK_MARKET_TIMEOUT", _env("HOLDBOOK_MARKET_TIMEOUT", "8"), 1, 60),
        retries=_parse_int("HOLDBOOK_MARKET_RETRIES", _env("HOLDBOOK_MARKET_RETRIES", "2"), 0, 5),
        max_rows=_parse_int("HOLDBOOK_MAX_IMPORT_ROWS", _env("HOLDBOOK_MAX_IMPORT_ROWS", "200000"), 1, 2_000_000),
        max_bytes=_parse_int("HOLDBOOK_MAX_IMPORT_BYTES", _env("HOLDBOOK_MAX_IMPORT_BYTES", "104857600"), 1024, 104857600),
        cost_tol_pct=_parse_decimal("HOLDBOOK_COST_TOLERANCE_PCT", _env("HOLDBOOK_COST_TOLERANCE_PCT", "0.5"), Decimal("0"), Decimal("100")),
        cost_tol_abs=_parse_decimal("HOLDBOOK_COST_TOLERANCE_ABS", _env("HOLDBOOK_COST_TOLERANCE_ABS", "1.00"), Decimal("0")),
        today=today_d,
        now=parse_now(os.environ.get("HOLDBOOK_NOW")),
        log_level=level,
    )
