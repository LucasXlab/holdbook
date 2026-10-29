from __future__ import annotations

from datetime import date
from decimal import Decimal


def npv(rate: float, cfs: list[tuple[date, Decimal]], first: date) -> float:
    return sum(float(cf) / ((1.0 + rate) ** ((d - first).days / 365.0)) for d, cf in cfs)


def xirr(cfs: list[tuple[date, Decimal]]) -> tuple[float | None, str | None]:
    if not cfs:
        return None, "no_cashflow"
    cfs = [(d, cf) for d, cf in cfs if cf != 0]
    if not cfs:
        return None, "no_cashflow"
    if not any(cf > 0 for _, cf in cfs) or not any(cf < 0 for _, cf in cfs):
        return None, "single_direction"
    first = min(d for d, _ in cfs)
    total_abs = sum(abs(cf) for _, cf in cfs)
    if total_abs == 0:
        return None, "no_cashflow"
    tol = 1e-6 * float(total_abs)

    def f(r: float) -> float:
        try:
            return npv(r, cfs, first)
        except (OverflowError, ZeroDivisionError):
            return float("inf")

    prev_r = -0.9999
    prev = f(prev_r)
    bracket = None
    r = -0.99
    while r <= 10.0001:
        cur = f(r)
        if abs(cur) < tol:
            return round(r, 6), None
        if prev == 0 or (prev > 0) != (cur > 0):
            bracket = (prev_r, r)
            break
        prev_r, prev = r, cur
        r += 0.01
    if bracket is None:
        return None, "no_sign_change"
    lo, hi = bracket
    flo = f(lo)
    last_mid = lo
    for _ in range(200):
        mid = (lo + hi) / 2.0
        last_mid = mid
        fm = f(mid)
        if abs(fm) < tol:
            return round(mid, 6), None
        if (flo > 0) != (fm > 0):
            hi = mid
        else:
            lo, flo = mid, fm
    return round(last_mid, 6), "iterations_exceeded"
