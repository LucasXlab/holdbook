from __future__ import annotations

import csv
import hashlib
import io
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .config import Config
from .errors import err
from .models import D, Tx, Snap, clean_text, money, qty

DATE_ALIASES = {"成交日期", "发生日期", "交易日期", "日期", "交收日期", "date"}
CODE_ALIASES = {"证券代码", "股票代码", "代码", "symbol", "code"}
NAME_ALIASES = {"证券名称", "股票名称", "名称", "name"}
KIND_ALIASES = {"操作", "买卖标志", "业务名称", "摘要", "交易类型", "business"}
QTY_ALIASES = {"成交数量", "发生数量", "数量", "qty", "quantity"}
SNAP_QTY_ALIASES = QTY_ALIASES | {"持仓数量", "股票余额", "当前持仓"}
PRICE_ALIASES = {"成交价格", "成交均价", "价格", "price"}
AMOUNT_ALIASES = {"成交金额", "发生金额", "金额", "amount"}
COMMISSION_ALIASES = {"手续费", "佣金", "commission"}
STAMP_ALIASES = {"印花税", "tax"}
TRANSFER_ALIASES = {"过户费", "transferfee"}
OTHERFEE_ALIASES = {"其他费用", "规费", "otherfee"}
COST_ALIASES = {"成本价", "参考成本价", "持仓成本", "costprice"}
MV_ALIASES = {"市值", "参考市值", "marketvalue"}

SH_PREFIX = {"600", "601", "603", "605", "688", "689"}
SZ_PREFIX = {"000", "001", "002", "003", "300", "301"}
SH_ETF = {str(x) for x in range(510, 519)} | {str(x) for x in range(560, 564)} | {"588"}
SZ_ETF = {str(x) for x in range(159, 170)}
SH_BOND = {"110", "111", "113", "118"}
SZ_BOND = {"123", "127", "128"}


class CsvProblem(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message


def norm_header(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "").strip().lower()
    return re.sub(r"\s+", "", s)


def parse_number(raw: str, field_name: str) -> Decimal:
    s = unicodedata.normalize("NFKC", raw or "").strip()
    s = s.replace("−", "-").replace(" ", "")
    if not s:
        raise ValueError(f"{field_name}为空")
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg, s = True, s[1:-1]
    if s.startswith("+"):
        s = s[1:]
    if "," in s:
        s = s.replace(",", "")
    if re.search(r"[eE]|NaN|Inf", s):
        raise ValueError(f"{field_name}不是普通十进制数")
    try:
        v = Decimal(s)
    except InvalidOperation:
        raise ValueError(f"{field_name}不是数字")
    if not v.is_finite():
        raise ValueError(f"{field_name}不是有限数")
    return -v if neg else v


def parse_date(raw: str) -> date:
    s = unicodedata.normalize("NFKC", raw or "").strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    raise ValueError("日期格式不支持")


def normalize_code(raw: str) -> tuple[str, str]:
    s = unicodedata.normalize("NFKC", raw or "").strip().upper().lstrip("\ufeff")
    if s == "CASH.CASH":
        return "CASH.CASH", "cash"
    m = re.fullmatch(r"(SH|SZ)?(\d{6})(?:\.(SH|SZ))?", s)
    if not m:
        raise ValueError("代码不是 6 位数字或 CASH.CASH")
    pre, digits, post = m.groups()
    explicit = pre or post
    inferred = None
    p3 = digits[:3]
    if p3 in SH_PREFIX:
        inferred, board = "SH", "sh_main" if p3 != "688" and p3 != "689" else "sh_star"
    elif p3 in SZ_PREFIX:
        inferred, board = "SZ", "sz_gem" if p3 in ("300", "301") else "sz_main"
    elif p3 in SH_ETF:
        inferred, board = "SH", "etf"
    elif p3 in SZ_ETF:
        inferred, board = "SZ", "etf"
    elif p3 in SH_BOND:
        inferred, board = "SH", "bond_cb"
    elif p3 in SZ_BOND:
        inferred, board = "SZ", "bond_cb"
    else:
        raise ValueError("不支持的代码前缀")
    if explicit and explicit != inferred:
        raise ValueError(f"交易所前缀与代码不匹配（推断 {inferred}）")
    return f"{digits}.{inferred}", board


def classify(text: str) -> str:
    s = re.sub(r"\s+", "", unicodedata.normalize("NFKC", text or "").lower())
    has_tax = any(x in s for x in ("税", "扣税", "代扣"))
    has_bonus = any(x in s for x in ("红股", "送股", "转增", "送转", "bonus", "送"))
    has_div = any(x in s for x in ("红利", "股息", "分红", "dividend"))
    if has_tax:
        return "fee"
    if has_bonus and has_div:
        raise ValueError("同一行同时出现现金分红和送股，请拆成两行")
    if has_bonus:
        return "bonus_share"
    if has_div:
        return "dividend"
    if any(x in s for x in ("配股", "rights")):
        return "rights_issue"
    if any(x in s for x in ("中签", "新股")):
        return "ipo_allot"
    if any(x in s for x in ("利息",)):
        return "interest"
    if any(x in s for x in ("费用", "手续费", "佣金", "规费")):
        return "fee"
    if any(x in s for x in ("银证转入", "银行转入", "转入")):
        return "transfer_in"
    if any(x in s for x in ("银证转出", "银行转出", "转出")):
        return "transfer_out"
    if any(x in s for x in ("买入", "证券买入", "buy")):
        return "buy"
    if any(x in s for x in ("卖出", "证券卖出", "sell")):
        return "sell"
    return "other"


def _read_text(path: Path, encoding: str, cfg: Config) -> str:
    if not path.exists():
        raise err("file_not_found", f"文件不存在：{path}", 1)
    if path.is_dir():
        raise err("file_not_found", f"这是目录，不是文件：{path}", 1)
    size = path.stat().st_size
    if size > cfg.max_bytes:
        raise err("bad_csv", f"文件 {path.name} 有 {size} 字节，超过上限 {cfg.max_bytes}", 2)
    raw = path.read_bytes()
    if encoding == "auto":
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            try:
                text = raw.decode("gb18030")
            except UnicodeDecodeError:
                raise err("bad_csv", f"文件 {path.name} 既不是 UTF-8 也不是 GB18030", 2)
    elif encoding == "utf-8":
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise err("bad_csv", f"文件 {path.name} 不是合法 UTF-8", 2)
    elif encoding == "gb18030":
        try:
            text = raw.decode("gb18030")
        except UnicodeDecodeError:
            raise err("bad_csv", f"文件 {path.name} 不是合法 GB18030", 2)
    else:
        raise err("bad_config", f"--encoding 只能是 auto、utf-8 或 gb18030，收到「{encoding}」")
    if "\ufffd" in text:
        raise err("bad_csv", f"文件 {path.name} 含无法解码的字符", 2)
    return text


def _delimiter(text: str) -> str:
    lines = [ln for ln in text.splitlines() if ln.strip()][:5]
    best_d, best_score = ",", (-1, -1)
    for d in (",", "\t", ";"):
        rows = list(csv.reader(io.StringIO("\n".join(lines)), delimiter=d))
        hit = 0
        fields = 0
        for row in rows[:5]:
            cells = {norm_header(c) for c in row}
            if cells & (DATE_ALIASES | CODE_ALIASES):
                hit = 1
            fields = max(fields, len(row))
        score = (hit, fields)
        if score > best_score:
            best_d, best_score = d, score
    return best_d


def _header_and_rows(text: str, delim: str, required: set[str]):
    rows = list(csv.reader(io.StringIO(text), delimiter=delim))
    header_idx = None
    for i, row in enumerate(rows[:5]):
        cells = {norm_header(c) for c in row}
        if cells & required:
            header_idx = i
            break
    if header_idx is None:
        raise CsvProblem("missing_columns", "前 5 行里找不到必需列，无法识别表头")
    header = [norm_header(c) for c in rows[header_idx]]
    data = []
    lines = []
    for lineno, row in enumerate(rows[header_idx + 1:], start=header_idx + 2):
        if not any(c.strip() for c in row):
            continue
        if row and row[0].lstrip().startswith("#"):
            continue
        data.append(row)
        lines.append(lineno)
    return header, data, header_idx + 1, lines


def _map_columns(header: list[str], mapping: dict[str, set[str]]) -> dict[str, int]:
    seen = [h for h in header if h]
    if len(seen) != len(set(seen)):
        raise CsvProblem("duplicate_column", "表头列名重复")
    out: dict[str, int] = {}
    for i, h in enumerate(header):
        for key, aliases in mapping.items():
            if h in aliases:
                if key in out:
                    raise CsvProblem("duplicate_column", f"表头重复映射到「{key}」")
                out[key] = i
    return out


def _cell(row: list[str], idx: dict[str, int], key: str) -> str:
    i = idx.get(key)
    if i is None or i >= len(row):
        return ""
    return row[i].strip()


def _trade_hash(account: str, tx: Tx, occurrence: int) -> str:
    parts = [
        account, tx.trade_date.isoformat(), tx.kind, tx.code,
        format(tx.quantity, ".4f"), format(tx.price, ".4f"), format(tx.amount, ".2f"),
        format(tx.fee_commission, ".2f"), format(tx.fee_stamp, ".2f"),
        format(tx.fee_transfer, ".2f"), format(tx.fee_other, ".2f"), str(occurrence),
    ]
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


@dataclass
class ParsedTrades:
    txs: list[Tx] = field(default_factory=list)
    errors: list[tuple[int, str]] = field(default_factory=list)
    rows_read: int = 0
    kinds: dict[str, int] = field(default_factory=dict)
    unknown: int = 0
    header_line: int = 1


def parse_trades(path: Path, account: str, cfg: Config, encoding: str = "auto") -> ParsedTrades:
    text = _read_text(path, encoding, cfg)
    delim = _delimiter(text)
    try:
        header, rows, header_line, row_lines = _header_and_rows(text, delim, DATE_ALIASES)
    except CsvProblem as exc:
        raise err(exc.code, f"{path.name}: {exc.message}", 2)
    mapping = {
        "date": DATE_ALIASES, "code": CODE_ALIASES, "name": NAME_ALIASES,
        "kind": KIND_ALIASES, "qty": QTY_ALIASES, "price": PRICE_ALIASES,
        "amount": AMOUNT_ALIASES, "commission": COMMISSION_ALIASES,
        "stamp": STAMP_ALIASES, "transfer": TRANSFER_ALIASES, "otherfee": OTHERFEE_ALIASES,
    }
    try:
        idx = _map_columns(header, mapping)
    except CsvProblem as exc:
        raise err(exc.code, f"{path.name}: {exc.message}", 2)
    missing = [name for name, key in (("日期", "date"), ("操作", "kind")) if key not in idx]
    if missing:
        raise err("missing_columns", f"{path.name}: 缺少必需列：{'、'.join(missing)}", 2)
    if not rows:
        raise err("bad_csv", f"{path.name}: 表头后没有数据行", 2)
    if len(rows) > cfg.max_rows:
        raise err("bad_csv", f"{path.name}: 数据行 {len(rows)} 超过上限 {cfg.max_rows}，未写入任何数据", 2)

    out = ParsedTrades(rows_read=len(rows), header_line=header_line)
    seen: dict[str, int] = {}
    for offset, row in enumerate(rows):
        line = row_lines[offset]
        try:
            if len(row) > len(header) and any(c.strip() for c in row[len(header):]):
                raise ValueError(f"列数 {len(row)} 多于表头 {len(header)}")
            raw_date = _cell(row, idx, "date")
            if not raw_date:
                raise ValueError("日期为空")
            d = parse_date(raw_date)
            if d > cfg.today:
                raise ValueError(f"日期 {d} 晚于今天 {cfg.today}")
            raw_kind = _cell(row, idx, "kind")
            if not raw_kind:
                raise ValueError("操作列为空")
            kind = classify(raw_kind)
            raw_code = _cell(row, idx, "code")
            code = "CASH.CASH"
            if kind not in ("other", "fee", "interest", "transfer_in", "transfer_out"):
                if not raw_code:
                    raise ValueError("缺少代码")
                code, board = normalize_code(raw_code)
            elif raw_code and raw_code not in ("0", "-"):
                code, board = normalize_code(raw_code)
            else:
                board = "cash"
            name = clean_text(_cell(row, idx, "name"))[:40]
            raw_qty = _cell(row, idx, "qty")
            raw_price = _cell(row, idx, "price")
            raw_amount = _cell(row, idx, "amount")
            fees = {
                "commission": parse_number(_cell(row, idx, "commission"), "佣金") if _cell(row, idx, "commission") else D(0),
                "stamp": parse_number(_cell(row, idx, "stamp"), "印花税") if _cell(row, idx, "stamp") else D(0),
                "transfer": parse_number(_cell(row, idx, "transfer"), "过户费") if _cell(row, idx, "transfer") else D(0),
                "otherfee": parse_number(_cell(row, idx, "otherfee"), "其他费用") if _cell(row, idx, "otherfee") else D(0),
            }
            for fn, fv in fees.items():
                if fv < 0:
                    raise ValueError(f"{fn} 不能为负")
                if fv != money(fv):
                    raise ValueError(f"{fn} 最多 2 位小数")
            q = D(0)
            p = D(0)
            amt = D(0)
            if kind in ("buy", "sell", "ipo_allot", "rights_issue"):
                if not raw_qty:
                    raise ValueError("缺少数量")
                q = abs(parse_number(raw_qty, "数量"))
                if q == 0:
                    raise ValueError("数量不能为 0")
                if raw_price:
                    p = parse_number(raw_price, "价格")
                if raw_amount:
                    amt_in = abs(parse_number(raw_amount, "金额"))
                else:
                    amt_in = None
                if p > 0:
                    gross = money(q * p)
                    if amt_in is not None and abs(amt_in - gross) > D("0.02"):
                        raise ValueError(f"金额 {amt_in} 与 数量×价格 {gross} 不一致")
                elif amt_in is not None:
                    p = qty(amt_in / q)
                    gross = money(amt_in)
                else:
                    raise ValueError("数量、价格、金额至少要有数量和价格，或数量和金额")
                if kind == "sell":
                    q = -q
                    amt = gross
                else:
                    amt = -gross
                if kind in ("buy", "ipo_allot", "rights_issue"):
                    q = abs(q)
                if kind == "sell":
                    q = -abs(q)
            elif kind == "dividend":
                if not raw_amount:
                    raise ValueError("现金分红缺少金额")
                amt = parse_number(raw_amount, "金额")
                if amt <= 0:
                    raise ValueError("现金分红金额必须为正")
                q, p = D(0), D(0)
            elif kind == "bonus_share":
                if not raw_qty:
                    raise ValueError("送股缺少数量")
                q = abs(parse_number(raw_qty, "数量"))
                if q <= 0:
                    raise ValueError("送股数量必须为正")
                if any(fees.values()):
                    raise ValueError("送股行不应有费用")
                p, amt = D(0), D(0)
            elif kind == "interest":
                if not raw_amount:
                    raise ValueError("利息行缺少金额")
                v = parse_number(raw_amount, "金额")
                if v == 0:
                    raise ValueError("利息金额不能为 0")
                if v < 0:
                    kind = "fee"
                    fee_total = abs(v)
                    amt = -fee_total
                    fees = {"commission": D(0), "stamp": D(0), "transfer": D(0), "otherfee": fee_total}
                else:
                    amt = v
                    fees = {k: D(0) for k in fees}
                q, p = D(0), D(0)
            elif kind == "fee":
                if not raw_amount:
                    raise ValueError("费用行缺少金额")
                v = parse_number(raw_amount, "金额")
                if v >= 0:
                    raise ValueError("费用行金额必须为负；利息收入请用「利息」")
                fee_total = abs(v)
                if fee_total == 0:
                    raise ValueError("费用金额不能为 0")
                amt = -fee_total
                fees = {"commission": D(0), "stamp": D(0), "transfer": D(0), "otherfee": fee_total}
                q, p = D(0), D(0)
            elif kind in ("transfer_in", "transfer_out"):
                if not raw_amount:
                    raise ValueError("转账行缺少金额")
                v = abs(parse_number(raw_amount, "金额"))
                amt = v if kind == "transfer_in" else -v
                q, p = D(0), D(0)
            else:  # other
                q, p, amt = D(0), D(0), D(0)
                fees = {k: D(0) for k in fees}

            if abs(q) > D("1000000000") or abs(p) > D("1000000000") or abs(amt) > D("1000000000000"):
                raise ValueError("数量、价格或金额超过上限")
            tx = Tx(
                trade_date=d, kind=kind, code=code, quantity=qty(q), price=qty(p), amount=money(amt),
                fee_commission=money(fees["commission"]), fee_stamp=money(fees["stamp"]),
                fee_transfer=money(fees["transfer"]), fee_other=money(fees["otherfee"]),
                note=raw_kind[:200], source_file=path.name, source_line=line, name=name,
                gross_override=(money(amt_in) if kind in ("buy", "sell", "ipo_allot", "rights_issue") and raw_amount else None),
            )
            base = "\x1f".join([
                account, d.isoformat(), kind, code, format(tx.quantity, ".4f"),
                format(tx.price, ".4f"), format(tx.amount, ".2f"),
                format(tx.fee_commission, ".2f"), format(tx.fee_stamp, ".2f"),
                format(tx.fee_transfer, ".2f"), format(tx.fee_other, ".2f"),
            ])
            occ = seen.get(base, 0)
            seen[base] = occ + 1
            tx.row_hash = _trade_hash(account, tx, occ)
            out.txs.append(tx)
            out.kinds[kind] = out.kinds.get(kind, 0) + 1
            if kind == "other":
                out.unknown += 1
        except ValueError as exc:
            out.errors.append((line, str(exc)))
    return out


@dataclass
class ParsedSnapshots:
    rows: list[Snap] = field(default_factory=list)
    errors: list[tuple[int, str]] = field(default_factory=list)
    rows_read: int = 0
    duplicates: int = 0


def parse_snapshots(path: Path, cfg: Config, encoding: str = "auto") -> ParsedSnapshots:
    text = _read_text(path, encoding, cfg)
    delim = _delimiter(text)
    try:
        header, rows, header_line, row_lines = _header_and_rows(text, delim, CODE_ALIASES)
    except CsvProblem as exc:
        raise err(exc.code, f"{path.name}: {exc.message}", 2)
    mapping = {"code": CODE_ALIASES, "name": NAME_ALIASES, "qty": SNAP_QTY_ALIASES, "cost": COST_ALIASES, "mv": MV_ALIASES}
    try:
        idx = _map_columns(header, mapping)
    except CsvProblem as exc:
        raise err(exc.code, f"{path.name}: {exc.message}", 2)
    missing = [name for name, key in (("代码", "code"), ("数量", "qty")) if key not in idx]
    if missing:
        raise err("missing_columns", f"{path.name}: 缺少必需列：{'、'.join(missing)}", 2)
    if len(rows) > cfg.max_rows:
        raise err("bad_csv", f"{path.name}: 数据行 {len(rows)} 超过上限 {cfg.max_rows}", 2)
    out = ParsedSnapshots(rows_read=len(rows))
    by_code: dict[str, int] = {}
    for offset, row in enumerate(rows):
        line = row_lines[offset]
        try:
            if len(row) > len(header) and any(c.strip() for c in row[len(header):]):
                raise ValueError(f"列数 {len(row)} 多于表头 {len(header)}")
            raw_code = _cell(row, idx, "code")
            if not raw_code:
                raise ValueError("代码为空")
            code, _ = normalize_code(raw_code)
            qv = parse_number(_cell(row, idx, "qty"), "数量")
            if qv < 0:
                raise ValueError("数量不能为负")
            cp = parse_number(_cell(row, idx, "cost"), "成本价") if _cell(row, idx, "cost") else None
            mv = parse_number(_cell(row, idx, "mv"), "市值") if _cell(row, idx, "mv") else None
            if cp is not None and cp < 0:
                raise ValueError("成本价不能为负")
            if mv is not None and mv < 0:
                raise ValueError("市值不能为负")
            snap = Snap(code=code, quantity=qty(qv), cost_price=None if cp is None else qty(cp),
                        market_value=None if mv is None else money(mv),
                        name=clean_text(_cell(row, idx, "name"))[:40], source_file=path.name)
            if code in by_code:
                out.duplicates += 1
                out.rows[by_code[code]] = snap
            else:
                by_code[code] = len(out.rows)
                out.rows.append(snap)
        except ValueError as exc:
            out.errors.append((line, str(exc)))
    return out


@dataclass
class ParsedPrice:
    code: str
    close: Decimal
    date: date


def parse_prices(path: Path, cfg: Config, as_of: date, encoding: str = "auto") -> tuple[list[ParsedPrice], list[tuple[int, str]]]:
    text = _read_text(path, encoding, cfg)
    delim = _delimiter(text)
    try:
        header, rows, header_line, row_lines = _header_and_rows(text, delim, CODE_ALIASES)
    except CsvProblem as exc:
        raise err(exc.code, f"{path.name}: {exc.message}", 2)
    mapping = {"code": CODE_ALIASES, "close": PRICE_ALIASES | {"收盘价", "close"}, "date": DATE_ALIASES}
    try:
        idx = _map_columns(header, mapping)
    except CsvProblem as exc:
        raise err(exc.code, f"{path.name}: {exc.message}", 2)
    if "code" not in idx or "close" not in idx:
        raise err("missing_columns", f"{path.name}: 缺少 code 或 close 列", 2)
    if len(rows) > cfg.max_rows:
        raise err("bad_csv", f"{path.name}: 数据行 {len(rows)} 超过上限 {cfg.max_rows}", 2)
    out: list[ParsedPrice] = []
    errors: list[tuple[int, str]] = []
    for offset, row in enumerate(rows):
        line = row_lines[offset]
        try:
            if len(row) > len(header) and any(c.strip() for c in row[len(header):]):
                raise ValueError(f"列数 {len(row)} 多于表头 {len(header)}")
            code, _ = normalize_code(_cell(row, idx, "code"))
            close = parse_number(_cell(row, idx, "close"), "close")
            if close <= 0 or close > D("1000000"):
                raise ValueError("close 必须在 (0, 1000000] 之间")
            d = parse_date(_cell(row, idx, "date")) if _cell(row, idx, "date") else as_of
            out.append(ParsedPrice(code, qty(close), d))
        except ValueError as exc:
            errors.append((line, str(exc)))
    return out, errors
