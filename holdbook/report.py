from __future__ import annotations

import html
import os
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

from .config import Config
from .csvio import normalize_code, parse_prices
from .db import (all_accounts, get_account, price_put, source_rows, transactions_for,
                 upsert_instrument)
from .errors import err
from .ledger import replay, row_to_tx
from .market import load_prices
from .metrics import xirr
from .models import D, fmt_money, fmt_pct, fmt_qty, money, q, qty


def _fmt_num(x, places=2) -> str:
    return f"{x:.{places}f}"


def _price_map(conn, cfg: Config, codes: list[str], as_of: date, market: str,
               manual_path: str | None) -> tuple[dict[str, Decimal], list[dict], str | None, dict[str, str], int]:
    warnings: list[dict] = []
    manual: dict[str, tuple[Decimal, date]] = {}
    manual_rows: list[tuple[str, str, date, Decimal]] = []
    new_codes: set[str] = set()
    manual_errors = 0
    if manual_path:
        parsed, errors = parse_prices(Path(manual_path), cfg, as_of)
        manual_errors = len(errors)
        for line, msg in errors:
            warnings.append({"code": "manual_price_error",
                             "message": f"{Path(manual_path).name} 第 {line} 行：{msg}"})
        for p in parsed:
            if p.date > as_of:
                warnings.append({"code": "manual_price_ignored", "message": f"{p.code} 价格日期 {p.date} 晚于 {as_of}，未采用"})
                continue
            try:
                norm, board = normalize_code(p.code)
            except ValueError as exc:
                raise err("bad_code", f"{p.code}: {exc}", 2)
            exists = conn.execute("SELECT 1 FROM instruments WHERE code=?", (norm,)).fetchone()
            if not exists:
                new_codes.add(norm)
                warnings.append({"code": "manual_price_new_code",
                                 "message": f"{norm} 没有交易或快照引用，已自动建标的"})
            manual_rows.append((norm, board, p.date, p.close))
            if p.code not in manual or p.date >= manual[p.code][1]:
                manual[p.code] = (p.close, p.date)
    # Fetch/cache first; writes happen only after network I/O, so no write lock is held across it.
    prices, more, live_at, failures = load_prices(conn, cfg, codes, as_of, market)
    if live_at:
        live_at = live_at.replace("T", " ").removesuffix("Z")
    for norm, board, pdate, close in manual_rows:
        if norm in new_codes:
            upsert_instrument(conn, norm, norm, board, cfg.now_iso())
        price_put(conn, norm, pdate, close, "manual", cfg.now_iso())
    prices.update({k: v[0] for k, v in manual.items()})
    warnings.extend(more)
    missing = [code for code in codes if code not in prices]
    if missing:
        preview = "、".join(missing[:5]) + ("等" if len(missing) > 5 else "")
        warnings.append({"code": "missing_price", "message": f"{len(missing)} 只标的没有价格，未计入市值与盈亏（{preview}）"})
    return prices, warnings, live_at, failures, manual_errors

def build(conn, cfg: Config, account: str | None, as_of: date, market: str,
          manual_path: str | None) -> dict:
    if account:
        row = get_account(conn, account)
        if row is None:
            raise err("unknown_account", f"账户不存在：{account}", 2)
        accounts = [row]
    else:
        accounts = all_accounts(conn)
    if not accounts:
        raise err("no_data", "还没有数据：先运行 holdbook import 和 holdbook snapshot", 1)

    agg: dict[str, dict] = {}
    fees = {"commission": D(0), "stamp": D(0), "transfer": D(0), "other": D(0)}
    realized = dividends = interest = D(0)
    cashflows = []
    trade_total = trade_buy = trade_sell = 0
    turnover = D(0)
    active_accounts = 0
    for arow in accounts:
        txs = [row_to_tx(r) for r in transactions_for(conn, arow["id"], as_of)]
        if not txs:
            continue
        meaningful = any(tx.kind not in ("other", "transfer_in", "transfer_out") for tx in txs)
        if meaningful:
            active_accounts += 1
        r = replay(txs, as_of)
        fees["commission"] += r.fees_commission
        fees["stamp"] += r.fees_stamp
        fees["transfer"] += r.fees_transfer
        fees["other"] += r.fees_other
        realized += r.realized
        dividends += r.dividends
        interest += r.interest
        cashflows.extend(r.cashflows)
        for tx in txs:
            trade_total += 1
            if tx.kind == "buy":
                trade_buy += 1
                turnover += tx.gross
            elif tx.kind == "sell":
                trade_sell += 1
                turnover += tx.gross
        for code, st in r.states.items():
            if st.quantity <= 0:
                continue
            slot = agg.setdefault(code, {"qty": D(0), "cost": D(0)})
            slot["qty"] += st.quantity
            slot["cost"] += st.total_cost

    if not agg and trade_total == 0:
        raise err("no_data", "没有可报告的持仓或交易", 1)

    codes = sorted(agg)
    prices, warnings, live_at, failures, manual_errors = _price_map(conn, cfg, codes, as_of, market, manual_path)
    if market == "live" and failures and not any(c in prices for c in codes):
        raise err("market_unavailable", "行情全部失败，且没有缓存价格；报告未生成（退出 4）", 4)

    names: dict[str, str] = {}
    for code in codes:
        row = conn.execute("SELECT name FROM instruments WHERE code=?", (code,)).fetchone()
        names[code] = (row["name"] if row and row["name"] else code)

    positions = []
    market_value = cost_all = cost_priced = unrealized = D(0)
    priced = unpriced = 0
    for code in codes:
        s = agg[code]
        qv = qty(s["qty"])
        cst = s["cost"]
        price = prices.get(code)
        mv = money(qv * price) if price is not None else None
        up = money(mv - cst) if mv is not None else None
        cost_all += cst
        if mv is not None:
            market_value += mv
            cost_priced += cst
            unrealized += up
            priced += 1
        else:
            unpriced += 1
        positions.append({
            "code": code, "name": names[code], "quantity": qv,
            "avgCost": qty(cst / qv) if qv else None,
            "price": qty(price) if price is not None else None,
            "marketValue": mv, "weight": None,
            "unrealizedPnl": up,
            "unrealizedPct": (up / cst) if (up is not None and cst) else None,
            "cost": cst,
        })
    for p in positions:
        p["weight"] = (p["marketValue"] / market_value) if (p["marketValue"] is not None and market_value) else None
    positions.sort(key=lambda p: (-(p["marketValue"] if p["marketValue"] is not None else D("-1")), p["code"]))

    top3 = sum((p["weight"] for p in positions[:3] if p["weight"] is not None), D(0)) if market_value else None
    top5 = sum((p["weight"] for p in positions[:5] if p["weight"] is not None), D(0)) if market_value else None

    final_value = market_value + sum((p["cost"] for p in positions if p["marketValue"] is None), D(0))
    if final_value > 0:
        cashflows.append((as_of, final_value))
    xr, xr_reason = xirr(cashflows)
    if unpriced and final_value > 0:
        warnings.append({"code": "xirr_cost_fallback", "message": "XIRR 期末含按成本价估值的标的"})

    fee_total = fees["commission"] + fees["stamp"] + fees["transfer"] + fees["other"]
    summary = {
        "marketValue": money(market_value),
        "totalCost": money(cost_all),
        "unrealizedPnl": money(unrealized),
        "unrealizedPct": (unrealized / cost_priced) if cost_priced else None,
        "realizedPnl": money(realized),
        "dividends": money(dividends),
        "interest": money(interest),
        "fees": {
            "commission": money(fees["commission"]), "stamp": money(fees["stamp"]),
            "transfer": money(fees["transfer"]), "other": money(fees["other"]),
            "total": money(fee_total),
        },
        "feesToTurnover": (fee_total / turnover) if turnover else None,
        "xirr": xr,
        "xirrReason": xr_reason,
        "priced": priced,
        "unpriced": unpriced,
    }

    srcs = source_rows(conn, account)
    seen = set()
    sources = []
    for r in srcs:
        if r["file"] in seen:
            continue
        seen.add(r["file"])
        sources.append({"file": r["file"], "rows": int(r["rows_read"])})
    missing_src = conn.execute(
        "SELECT DISTINCT a.name FROM transactions t JOIN accounts a ON a.id=t.account_id "
        "LEFT JOIN imports i ON i.account_name=a.name AND i.file=t.source_file "
        "WHERE i.id IS NULL"
    ).fetchall()
    if missing_src:
        names = "、".join(r[0] for r in missing_src)
        warnings.append({"code": "source_missing", "message": f"有交易记录但找不到对应 imports 记录：{names}"})

    conn.commit()
    return {
        "ok": True,
        "manualErrors": manual_errors,
        "asOf": as_of.isoformat(),
        "accounts": active_accounts,
        "instruments": len(positions),
        "market": market,
        "marketFetchedAt": live_at,
        "method": "holdbook-ledger-v1",
        "summary": summary,
        "positions": positions,
        "top3": top3,
        "top5": top5,
        "tradeStats": {"total": trade_total, "buy": trade_buy, "sell": trade_sell, "turnover": money(turnover)},
        "sources": sources,
        "warnings": warnings,
    }


def to_json(data: dict) -> dict:
    s = data["summary"]
    out = {
        "ok": True,
        "asOf": data["asOf"],
        "accounts": data["accounts"],
        "instruments": data["instruments"],
        "market": data["market"],
        "marketFetchedAt": data.get("marketFetchedAt"),
        "method": data["method"],
        "summary": {
            "marketValue": f"{s['marketValue']:.2f}",
            "totalCost": f"{s['totalCost']:.2f}",
            "unrealizedPnl": f"{s['unrealizedPnl']:.2f}",
            "unrealizedPct": None if s["unrealizedPct"] is None else round(float(s["unrealizedPct"]), 4),
            "realizedPnl": f"{s['realizedPnl']:.2f}",
            "dividends": f"{s['dividends']:.2f}",
            "interest": f"{s['interest']:.2f}",
            "fees": {k: f"{v:.2f}" for k, v in s["fees"].items()},
            "feesToTurnover": None if s["feesToTurnover"] is None else round(float(s["feesToTurnover"]), 6),
            "xirr": None if s["xirr"] is None else round(float(s["xirr"]), 6),
            "xirrReason": s["xirrReason"],
            "priced": s["priced"],
            "unpriced": s["unpriced"],
        },
        "positions": [],
        "top3": None if data["top3"] is None else round(float(data["top3"]), 4),
        "top5": None if data["top5"] is None else round(float(data["top5"]), 4),
        "tradeStats": {
            "total": data["tradeStats"]["total"],
            "buy": data["tradeStats"]["buy"],
            "sell": data["tradeStats"]["sell"],
            "turnover": f"{data['tradeStats']['turnover']:.2f}",
        },
        "sources": data["sources"],
        "warnings": data["warnings"],
    }
    for p in data["positions"]:
        out["positions"].append({
            "code": p["code"],
            "name": p["name"],
            "quantity": f"{p['quantity']:.4f}",
            "avgCost": None if p["avgCost"] is None else f"{p['avgCost']:.4f}",
            "price": None if p["price"] is None else f"{p['price']:.4f}",
            "marketValue": None if p["marketValue"] is None else f"{p['marketValue']:.2f}",
            "weight": None if p["weight"] is None else round(float(p["weight"]), 4),
            "unrealizedPnl": None if p["unrealizedPnl"] is None else f"{p['unrealizedPnl']:.2f}",
            "unrealizedPct": None if p["unrealizedPct"] is None else round(float(p["unrealizedPct"]), 4),
        })
    return out


def _pct(x) -> str:
    return fmt_pct(x) if x is not None else "—"


def text_report(d: dict) -> str:
    s = d["summary"]
    lines = [
        "持仓簿 报告",
        f"数据截止 {d['asOf']} · {d['accounts']} 个账户 · {d['instruments']} 只标的 · 行情 {d['market']}",
    ]
    if s["unpriced"]:
        lines.append(f"{s['unpriced']} 只标的没有价格，未计入市值与盈亏")
    lines += [
        "",
        f"总市值 {fmt_money(s['marketValue'])}    总成本 {fmt_money(s['totalCost'])}",
        f"未实现盈亏 {fmt_money(s['unrealizedPnl'])}（{_pct(s['unrealizedPct'])}）    已实现盈亏 {fmt_money(s['realizedPnl'])}",
        f"分红收入 {fmt_money(s['dividends'])}    费用合计 {fmt_money(s['fees']['total'])}",
        "XIRR " + _pct(s["xirr"]) + (f"（{s['xirrReason']}）" if s.get("xirrReason") else ""),
        "",
        "代码  名称  数量  成本价  现价  市值  占比  未实现盈亏",
    ]
    for p in d["positions"]:
        lines.append(
            f"{p['code']}  {p['name']}  {fmt_qty(p['quantity'])}  {fmt_qty(p['avgCost'])}  "
            f"{fmt_qty(p['price'])}  {fmt_money(p['marketValue'])}  {_pct(p['weight'])}  {fmt_money(p['unrealizedPnl'])}"
        )
    f = s["fees"]
    lines += [
        "",
        "费用",
        f"  佣金 {fmt_money(f['commission'])}  印花税 {fmt_money(f['stamp'])}  过户费 {fmt_money(f['transfer'])}  其他 {fmt_money(f['other'])}",
        f"  交易笔数 {d['tradeStats']['total']}  买入/卖出 {d['tradeStats']['buy']}/{d['tradeStats']['sell']}  费用占成交额 {_pct(s['feesToTurnover'])}",
    ]
    lines += ["", "数据来源"]
    if d["sources"]:
        for src in d["sources"]:
            lines.append(f"  {src['file']}（{src['rows']} 行）")
    else:
        lines.append("  无导入记录")
    if d["warnings"]:
        lines += ["", "提示"]
        for w in d["warnings"]:
            lines.append(f"  {w['message']}")
    lines += [
        "",
        "本工具只做账目核对与统计，不构成投资建议；结果依赖你导入的数据是否完整。",
        "公式版本 holdbook-ledger-v1。",
    ]
    return "\n".join(lines) + "\n"

CSS = """
:root{--bg:#fff;--bg-subtle:#fafafa;--bg-muted:#f2f2f3;--border:#e6e6e8;--border-strong:#d4d4d8;--text:#18181b;--text-muted:#52525b;--text-faint:#6b6b75;--accent:#2f6f4e;--accent-weak:#e8f1ec;--pnl-up:#b42318;--pnl-down:#067647}
@media(prefers-color-scheme:dark){:root{--bg:#111113;--bg-subtle:#17171a;--bg-muted:#1f1f23;--border:#2a2a2f;--border-strong:#3a3a41;--text:#f4f4f5;--text-muted:#b4b4bc;--text-faint:#8f8f98;--accent:#7fc9a4;--accent-weak:#1b2f26;--pnl-up:#ff8a80;--pnl-down:#6ee7a8}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:13px/20px -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;font-variant-numeric:tabular-nums}
.wrap{max-width:960px;margin:0 auto;padding:32px 24px 48px}
h1{font-size:20px;line-height:28px;font-weight:600;margin:0 0 4px}
h2{font-size:14px;line-height:20px;font-weight:600;margin:28px 0 8px}
.meta{color:var(--text-muted);font-size:12px;line-height:18px}
.summary{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:20px 0 8px}
.metric{background:var(--bg-subtle);border-radius:6px;padding:12px}
.metric .label{font-size:11px;color:var(--text-muted)}
.metric .value{font-size:24px;line-height:32px;font-weight:600;margin-top:2px}
.metric .sub{font-size:11px;color:var(--text-faint);margin-top:2px}
.secondary{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;color:var(--text-muted);font-size:12px;margin:8px 0 0}
.secondary b{color:var(--text);font-weight:600}
table{width:100%;border-collapse:collapse;margin-top:8px}
th,td{text-align:left;padding:8px 6px;border-bottom:1px solid var(--border);white-space:nowrap}
th{font-size:11px;color:var(--text-muted);font-weight:500}
td.num,th.num{text-align:right}
tr.total td{border-top:1px solid var(--border-strong);font-weight:600}
.bar{display:inline-block;height:6px;background:var(--accent);border-radius:4px;vertical-align:middle;margin-right:6px}
.pos{color:var(--pnl-up)}.neg{color:var(--pnl-down)}
.caption{color:var(--text-faint);font-size:12px;margin-top:6px}
.fees{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}
.fee{background:var(--bg-subtle);border-radius:6px;padding:10px}
.fee .k{font-size:11px;color:var(--text-muted)}.fee .v{font-weight:600}
.footer{margin-top:28px;color:var(--text-faint);font-size:11px;line-height:18px;border-top:1px solid var(--border);padding-top:12px}
.warn{color:var(--text-muted);font-size:12px;margin-top:8px}
@media(max-width:680px){.wrap{padding:20px 16px 36px}.summary,.secondary,.fees{grid-template-columns:repeat(2,1fr)}.metric .value{font-size:20px;line-height:28px}table,tbody,tr,td{display:block;width:100%}thead{display:none}tr{border-bottom:1px solid var(--border);padding:8px 0}td{border:0;padding:2px 0;white-space:normal}td.num{text-align:left}td[data-label]::before{content:attr(data-label) " ";color:var(--text-faint);font-size:11px}}
@media(max-width:480px){.summary,.secondary,.fees{grid-template-columns:1fr}}
@media print{.wrap{max-width:none;padding:0}h2{break-after:avoid}tr,td,th{break-inside:avoid}.summary,.metric,.fee{background:transparent}}
"""


def html_report(d: dict) -> str:
    s = d["summary"]
    esc = html.escape

    def m(v):
        return "—" if v is None else fmt_money(v)

    def qv(v):
        return "—" if v is None else fmt_qty(v)

    def p4(v):
        return "—" if v is None else f"{v:.4f}"

    def pct(v):
        return "—" if v is None else fmt_pct(v)

    rows = []
    for p in d["positions"]:
        cls = "neg" if (p["unrealizedPnl"] is not None and p["unrealizedPnl"] < 0) else ("pos" if p["unrealizedPnl"] else "")
        w = (p["weight"] * 100) if p["weight"] is not None else 0
        rows.append(
            f"<tr><td data-label=\"代码\">{esc(p['code'])}</td><td data-label=\"名称\">{esc(p['name'])}</td>"
            f"<td data-label=\"数量\" class=\"num\">{qv(p['quantity'])}</td>"
            f"<td data-label=\"成本价\" class=\"num\">{m(p['avgCost'])}</td><td data-label=\"现价\" class=\"num\">{m(p['price'])}</td>"
            f"<td data-label=\"市值\" class=\"num\">{m(p['marketValue'])}</td>"
            f"<td data-label=\"占比\" class=\"num\"><span class=\"bar\" style=\"width:{w:.1f}%\"></span>{pct(p['weight'])}</td>"
            f"<td data-label=\"浮动盈亏\" class=\"num {cls}\">{m(p['unrealizedPnl'])}</td>"
            f"<td data-label=\"收益率\" class=\"num {cls}\">{pct(p['unrealizedPct'])}</td></tr>"
        )
    total_cls = "neg" if s["unrealizedPnl"] < 0 else "pos"
    total_weight = sum((p["weight"] for p in d["positions"] if p["weight"] is not None), D(0))
    warnings_html = "".join(f"<div class=\"warn\">{esc(w['message'])}</div>" for w in d["warnings"])
    sources_html = "".join(f"<li>{esc(x['file'])}（{x['rows']} 行）</li>" for x in d["sources"])
    fetched = f" · 行情抓取 {esc(d['marketFetchedAt'])}" if d.get("marketFetchedAt") else ""
    top = f"前 3 大 {pct(d['top3'])} · 前 5 大 {pct(d['top5'])}" if d.get("top3") is not None else "—"
    body = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'">
<title>持仓簿 {esc(d['asOf'])}</title><style>{CSS}</style></head>
<body><main class="wrap">
<h1>持仓簿</h1>
<div class="meta">数据截止 {esc(d['asOf'])} · {d['accounts']} 个账户 · {d['instruments']} 只标的 · 行情 {esc(d['market'])}{fetched}</div>
<section class="summary">
<div class="metric"><div class="label">总市值</div><div class="value">{m(s['marketValue'])}</div><div class="sub">已定价 {s['priced']} 只 / 无价 {s['unpriced']} 只</div></div>
<div class="metric"><div class="label">浮动盈亏</div><div class="value {total_cls}">{m(s['unrealizedPnl'])}</div><div class="sub">{pct(s['unrealizedPct'])}</div></div>
<div class="metric"><div class="label">已实现盈亏</div><div class="value">{m(s['realizedPnl'])}</div><div class="sub">卖出净额 - 卖出成本</div></div>
<div class="metric"><div class="label">XIRR</div><div class="value">{pct(s['xirr'])}</div><div class="sub">{esc(s['xirrReason'] or '年化现金流')}</div></div>
</section>
<section class="secondary">
<div>总成本 <b>{m(s['totalCost'])}</b></div><div>分红 <b>{m(s['dividends'])}</b></div><div>利息 <b>{m(s['interest'])}</b></div><div>费用 <b>{m(s['fees']['total'])}</b></div>
<div>费用/成交额 <b>{pct(s['feesToTurnover'])}</b></div><div>成交笔数 <b>{d['tradeStats']['total']}</b></div><div>买入/卖出 <b>{d['tradeStats']['buy']}/{d['tradeStats']['sell']}</b></div><div>成交额 <b>{m(d['tradeStats']['turnover'])}</b></div>
</section>
<h2>持仓</h2>
<table><thead><tr><th>代码</th><th>名称</th><th class="num">数量</th><th class="num">成本价</th><th class="num">现价</th><th class="num">市值</th><th class="num">占比</th><th class="num">浮动盈亏</th><th class="num">收益率</th></tr></thead>
<tbody>{''.join(rows)}<tr class="total"><td colspan="5">合计</td><td class="num">{m(s['marketValue'])}</td><td class="num">{pct(total_weight)}</td><td class="num {total_cls}">{m(s['unrealizedPnl'])}</td><td class="num {total_cls}">{pct(s['unrealizedPct'])}</td></tr></tbody></table>
<div class="caption">{top}</div>
{warnings_html}
<h2>费用</h2><div class="fees">
<div class="fee"><div class="k">佣金</div><div class="v">{m(s['fees']['commission'])}</div></div>
<div class="fee"><div class="k">印花税</div><div class="v">{m(s['fees']['stamp'])}</div></div>
<div class="fee"><div class="k">过户费</div><div class="v">{m(s['fees']['transfer'])}</div></div>
<div class="fee"><div class="k">其他</div><div class="v">{m(s['fees']['other'])}</div></div>
</div>
<h2>数据来源</h2><ul>{sources_html or '<li>无导入记录</li>'}</ul>
<div class="footer">公式版本 {esc(d['method'])}。本报告只用于个人对账，不构成投资建议。数据来自用户导入的券商交割单与持仓快照；行情为第三方公开报价，可能延迟或有误。</div>
</main></body></html>"""
    return body


def atomic_write(path: Path, text: str, replace=os.replace) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()
