from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from .config import Config
from .ledger import replay
from .models import D, CheckItem, PosState, q, qty


def _d(v) -> Decimal | None:
    return None if v is None else D(v)


def check_account(account_name: str, snapshot_date: date, txs, snaps, cfg: Config) -> dict:
    replay_result = replay(txs, snapshot_date)
    snap_map = {r["code"]: r for r in snaps}
    has_tx: dict[str, bool] = {}
    position_dates: dict[str, list] = {}
    bonus: dict[str, list] = {}
    dividend: dict[str, list] = {}
    for tx in txs:
        if tx.kind in ("buy", "sell", "ipo_allot", "rights_issue", "bonus_share"):
            has_tx[tx.code] = True
            position_dates.setdefault(tx.code, []).append(tx.trade_date)
        if tx.kind == "bonus_share":
            bonus.setdefault(tx.code, []).append((tx.trade_date, tx.quantity))
        if tx.kind == "dividend":
            dividend.setdefault(tx.code, []).append(tx.trade_date)

    codes = set(snap_map) | {c for c, st in replay_result.states.items() if st.quantity != 0}
    items: list[CheckItem] = []
    matched = differed = 0
    for code in sorted(codes):
        st = replay_result.states.get(code, PosState(code=code))
        snap = snap_map.get(code)
        qc = qty(st.quantity)
        qs = qty(D(snap["quantity"])) if snap else None
        avg = st.avg_cost
        cs = _d(snap["cost_price"]) if snap and snap["cost_price"] is not None else None
        if snap is None and st.quantity == 0:
            continue
        if snap is not None and st.quantity == 0 and qs == 0:
            continue
        if snap is None:
            status = "only_ledger"
        elif st.quantity == 0:
            status = "only_snapshot" if not has_tx.get(code) else "qty_missing"
        elif qc != qs:
            status = "qty_missing" if qc < qs else "qty_extra"
        elif cs is None:
            status = "no_cost"
        else:
            tol = max(cfg.cost_tol_abs, abs(cs) * cfg.cost_tol_pct / D(100))
            if abs(avg - cs) <= tol:
                status = "matched"
            else:
                status = "cost_off"
        qd = qc - qs if qs is not None else None
        cd = (avg - cs) if (avg is not None and cs is not None) else None
        hints: list[str] = []
        if status == "qty_missing" and code in bonus:
            recent = [(d, v) for d, v in bonus[code] if snapshot_date - timedelta(days=90) <= d <= snapshot_date]
            if recent:
                d, v = recent[-1]
                if qd is not None and abs(qd) == qty(v):
                    hints.append(f"差值正好等于 {d.isoformat()} 送转的 {qty(v)} 股，可能漏导入该送转行")
        if status == "qty_missing" and len(hints) < 2 and code in dividend:
            recent = [d for d in dividend[code] if snapshot_date - timedelta(days=90) <= d <= snapshot_date]
            if recent:
                hints.append(f"近期有 {recent[-1].isoformat()} 分红，确认是否漏导入除权日之后的行")
        if status == "only_ledger" and len(hints) < 2 and position_dates.get(code):
            last = max(position_dates[code])
            if (snapshot_date - last).days >= 30:
                hints.append(f"该标的最后交易在 {last.isoformat()}，可能已卖出但未导入")
        item = CheckItem(
            code=code, name=(snap["name"] if snap and snap["name"] else (st.name or code)),
            qty_calc=qc, qty_snap=qs, avg_cost_calc=qty(avg) if avg is not None else None,
            cost_snap=qty(cs) if cs is not None else None, status=status,
            qty_diff=qd, cost_diff=cd, hints=hints[:2],
        )
        items.append(item)
        if status == "matched":
            matched += 1
        else:
            differed += 1
    source = snaps[-1]["source_file"] if snaps else ""
    return {
        "account": account_name,
        "snapshot": source,
        "snapshotDate": snapshot_date.isoformat(),
        "matched": matched,
        "differed": differed,
        "items": items,
    }
