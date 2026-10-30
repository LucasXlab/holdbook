from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

Z = Decimal("0")
ONE = Decimal("1")


def D(x) -> Decimal:
    if isinstance(x, Decimal):
        return x
    return Decimal(str(x))


def q(x: Decimal, places: int) -> Decimal:
    return D(x).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def money(x: Decimal) -> Decimal:
    return q(x, 2)


def qty(x: Decimal) -> Decimal:
    return q(x, 4)


def clean_text(value: str) -> str:
    return "".join(ch for ch in value if ch >= " " and ch not in "\x7f\x85")


def fmt_money(x: Decimal | None, dash: str = "—") -> str:
    if x is None:
        return dash
    if not isinstance(x, Decimal):
        x = Decimal(str(x))
    v = x.quantize(D("0.01"), rounding=ROUND_HALF_UP)
    return f"{v:,.2f}"


def fmt_qty(x: Decimal | None, dash: str = "—") -> str:
    if x is None:
        return dash
    if not isinstance(x, Decimal):
        x = Decimal(str(x))
    s = f"{x.quantize(D('0.0001'), rounding=ROUND_HALF_UP):,.4f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


def fmt_pct(x: Decimal | float | None, dash: str = "—") -> str:
    if x is None:
        return dash
    if not isinstance(x, Decimal):
        x = Decimal(str(x))
    return f"{(x * 100).quantize(D('0.01'), rounding=ROUND_HALF_UP):.2f}%"


@dataclass
class Tx:
    trade_date: date
    kind: str
    code: str
    quantity: Decimal
    price: Decimal
    amount: Decimal
    fee_commission: Decimal = Z
    fee_stamp: Decimal = Z
    fee_transfer: Decimal = Z
    fee_other: Decimal = Z
    note: str = ""
    source_file: str = ""
    source_line: int = 0
    row_hash: str = ""
    name: str = ""
    gross_override: Decimal | None = None

    @property
    def fees(self) -> Decimal:
        return self.fee_commission + self.fee_stamp + self.fee_transfer + self.fee_other

    @property
    def gross(self) -> Decimal:
        if self.gross_override is not None:
            return money(self.gross_override)
        return money(abs(self.quantity) * self.price)


@dataclass
class Snap:
    code: str
    quantity: Decimal
    cost_price: Decimal | None
    market_value: Decimal | None
    name: str = ""
    source_file: str = ""
    as_of: date | None = None


@dataclass
class PosState:
    code: str
    name: str = ""
    quantity: Decimal = Z
    total_cost: Decimal = Z
    realized: Decimal = Z
    dividends: Decimal = Z
    interest: Decimal = Z
    last_date: date | None = None

    @property
    def avg_cost(self) -> Decimal | None:
        if self.quantity == 0:
            return None
        return (self.total_cost / self.quantity).quantize(D("0.000000000001"), rounding=ROUND_HALF_UP)


@dataclass
class CheckItem:
    code: str
    name: str
    qty_calc: Decimal
    qty_snap: Decimal
    avg_cost_calc: Decimal | None
    cost_snap: Decimal | None
    status: str
    qty_diff: Decimal
    cost_diff: Decimal | None
    hints: list[str] = field(default_factory=list)


@dataclass
class ReplayRow:
    date: date
    kind: str
    quantity: Decimal
    price: Decimal
    amount: Decimal
    fees: Decimal
    qty_after: Decimal
    avg_cost_after: Decimal | None
