from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from .errors import err
from .models import D, PosState, ReplayRow, Tx, money, qty


def row_to_tx(r) -> Tx:
    return Tx(
        trade_date=date.fromisoformat(r["trade_date"]),
        kind=r["kind"], code=r["code"],
        quantity=D(r["quantity"]), price=D(r["price"]), amount=D(r["amount"]),
        fee_commission=D(r["fee_commission"]), fee_stamp=D(r["fee_stamp"]),
        fee_transfer=D(r["fee_transfer"]), fee_other=D(r["fee_other"]),
        note=r["note"], source_file=r["source_file"], source_line=r["source_line"],
        row_hash=r["row_hash"], name=r["name"] if "name" in r.keys() else "",
        gross_override=(abs(D(r["amount"])) if r["kind"] in ("buy", "sell", "ipo_allot", "rights_issue") else None),
    )


@dataclass
class ReplayResult:
    states: dict[str, PosState] = field(default_factory=dict)
    fees_total: Decimal = D(0)
    fees_commission: Decimal = D(0)
    fees_stamp: Decimal = D(0)
    fees_transfer: Decimal = D(0)
    fees_other: Decimal = D(0)
    dividends: Decimal = D(0)
    interest: Decimal = D(0)
    realized: Decimal = D(0)
    cashflows: list[tuple[date, Decimal]] = field(default_factory=list)


def _apply(state: PosState, tx: Tx) -> None:
    st = state
    st.last_date = tx.trade_date
    if tx.name:
        st.name = tx.name
    if tx.kind in ("buy", "ipo_allot", "rights_issue"):
        st.quantity += tx.quantity
        st.total_cost += tx.gross + tx.fees
    elif tx.kind == "sell":
        sell_qty = abs(tx.quantity)
        if sell_qty <= 0:
            raise ValueError("卖出数量必须为正")
        if st.quantity <= 0 or sell_qty > st.quantity:
            raise ValueError("卖出超过当前持仓")
        cost_out = (st.total_cost * sell_qty / st.quantity).quantize(D('0.000000000001'), rounding=ROUND_HALF_UP)
        st.realized += (tx.gross - tx.fees) - cost_out
        st.quantity -= sell_qty
        st.total_cost -= cost_out
        if st.quantity == 0:
            st.total_cost = D(0)
    elif tx.kind == "bonus_share":
        st.quantity += tx.quantity
    elif tx.kind == "dividend":
        st.dividends += tx.amount
    elif tx.kind == "interest":
        st.interest += tx.amount


def replay(txs: list[Tx], as_of: date) -> ReplayResult:
    result = ReplayResult()
    for tx in txs:
        if tx.trade_date > as_of:
            continue
        st = result.states.setdefault(tx.code, PosState(code=tx.code))
        try:
            _apply(st, tx)
        except ValueError as exc:
            raise err("ledger_negative", f"{tx.code} {tx.trade_date}: {exc}（可卖数量不足）", 2)
        result.fees_commission += tx.fee_commission
        result.fees_stamp += tx.fee_stamp
        result.fees_transfer += tx.fee_transfer
        result.fees_other += tx.fee_other
        result.fees_total += tx.fees
        if tx.kind in ("buy", "ipo_allot", "rights_issue"):
            result.cashflows.append((tx.trade_date, -(tx.gross + tx.fees)))
        elif tx.kind == "sell":
            result.cashflows.append((tx.trade_date, tx.gross - tx.fees))
        elif tx.kind == "dividend":
            result.cashflows.append((tx.trade_date, tx.amount))
        elif tx.kind == "interest":
            result.cashflows.append((tx.trade_date, tx.amount))
        elif tx.kind == "fee":
            result.cashflows.append((tx.trade_date, tx.amount))
    # dividends/realized are positional sums; recompute once to avoid double counting above
    result.dividends = sum((s.dividends for s in result.states.values()), D(0))
    result.interest = sum((s.interest for s in result.states.values()), D(0))
    result.realized = sum((s.realized for s in result.states.values()), D(0))
    return result


def explain(txs: list[Tx], code: str, as_of: date) -> tuple[list[ReplayRow], PosState]:
    st = PosState(code=code)
    rows: list[ReplayRow] = []
    for tx in txs:
        if tx.code != code or tx.trade_date > as_of:
            continue
        before = st.quantity
        _apply(st, tx)
        rows.append(ReplayRow(
            date=tx.trade_date, kind=tx.kind, quantity=tx.quantity, price=tx.price,
            amount=tx.amount, fees=tx.fees, qty_after=st.quantity, avg_cost_after=st.avg_cost,
        ))
        _ = before
    return rows, st


def positions(result: ReplayResult, prices: dict[str, Decimal]) -> list[dict]:
    out = []
    for code, st in result.states.items():
        if st.quantity <= 0:
            continue
        price = prices.get(code)
        mv = money(st.quantity * price) if price is not None else None
        out.append({
            "code": code, "name": st.name or code, "quantity": qty(st.quantity),
            "avg_cost": qty(st.avg_cost) if st.avg_cost is not None else None,
            "price": qty(price) if price is not None else None,
            "market_value": mv,
            "unrealized_pnl": money(mv - st.total_cost) if mv is not None else None,
            "total_cost": money(st.total_cost),
        })
    out.sort(key=lambda p: (-(p["market_value"] or D(0)), p["code"]))
    return out
