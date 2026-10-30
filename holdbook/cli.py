from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from . import __version__
from .config import Config, load_config, parse_date
from .csvio import normalize_code, parse_prices, parse_snapshots, parse_trades
from .db import (all_accounts, backup_db, connect, ensure_account, get_account,
                 init_db, insert_import, insert_tx, latest_snapshot_date,
                 reset_db, snapshots_for, source_rows, transaction,
                 transactions_for, tx_exists, upsert_instrument, upsert_snapshot)
from .errors import HoldbookError, err
from .ledger import explain as explain_ledger
from .ledger import replay, row_to_tx
from .models import D, fmt_money, fmt_pct, fmt_qty, money, q, qty
from .reconcile import check_account
from .report import atomic_write, build, html_report, text_report, to_json


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise err("bad_config", message, 2)


class Out:
    def __init__(self, json_mode: bool, quiet: bool):
        self.json = json_mode
        self.quiet = quiet

    def info(self, s: str) -> None:
        if not self.quiet and not self.json:
            print(s)

    def result(self, s: str) -> None:
        if not self.json:
            print(s)

    def data(self, obj) -> None:
        print(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))

    def warn(self, s: str) -> None:
        print(f"警告: {s}", file=sys.stderr)


def _build_parser() -> Parser:
    p = Parser(prog="holdbook", description="本地持仓对账与成本报告")
    p.add_argument("--data-dir", help="数据目录")
    p.add_argument("--json", action="store_true", help="只输出 JSON")
    p.add_argument("--today", help="覆盖今天，YYYY-MM-DD")
    p.add_argument("--quiet", action="store_true", help="抑制进度")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--version", action="store_true")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("init")
    s = sub.add_parser("import")
    s.add_argument("files", nargs="+")
    s.add_argument("--account", required=True)
    s.add_argument("--broker")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--encoding", default="auto")

    s = sub.add_parser("snapshot")
    s.add_argument("file")
    s.add_argument("--account", required=True)
    s.add_argument("--as-of")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--encoding", default="auto")

    s = sub.add_parser("check")
    s.add_argument("--account")
    s.add_argument("--as-of")
    s.add_argument("--strict", action="store_true")

    s = sub.add_parser("report")
    s.add_argument("--account")
    s.add_argument("--as-of")
    s.add_argument("--market", choices=["off", "live", "mock"])
    s.add_argument("--prices")
    s.add_argument("--format", choices=["text", "html"], default=None)
    s.add_argument("--out")
    s.add_argument("--open", action="store_true")

    s = sub.add_parser("explain")
    s.add_argument("code")
    s.add_argument("--account")
    s.add_argument("--as-of")

    s = sub.add_parser("status")
    s = sub.add_parser("backup")
    s.add_argument("--out")
    s = sub.add_parser("reset")
    s.add_argument("--yes", action="store_true")
    sub.add_parser("version")
    s = sub.add_parser("demo")
    s.add_argument("--dir", default="holdbook-demo")
    return p


def _as_of(cfg: Config, raw: str | None) -> date:
    return parse_date(raw, "--as-of") if raw else cfg.today


def _err_obj(e: HoldbookError) -> dict:
    return {"error": e.message, "code": e.code}


def _emit_error(e: HoldbookError, out: Out) -> int:
    if out.json:
        out.data(_err_obj(e))
    else:
        print(f"错误: {e.message} (code={e.code})", file=sys.stderr)
    return e.exit_code


# ---------------- commands ----------------

def cmd_init(cfg: Config, args, out: Out) -> int:
    created = init_db(cfg)
    (cfg.data_dir / "reports").mkdir(parents=True, exist_ok=True)
    msg = "已就绪（schema_version=1）" if created else "已就绪（已存在，未改动）"
    out.result(f"数据目录: {cfg.data_dir}")
    out.result(f"数据库: {cfg.db_path}")
    out.result(msg)
    if out.json:
        # --json suppresses result lines in Out.result only for JSON; emit one object
        out.data({"ok": True, "dbPath": str(cfg.db_path), "schemaVersion": 1})
    return 0


def _import_summary(files: list[dict], accounts_created: list[str], dry: bool) -> dict:
    total = {"added": 0, "skipped": 0, "errors": 0}
    for f in files:
        total["added"] += f["added"]
        total["skipped"] += f["skipped"]
        total["errors"] += len(f["errors"])
    return {"ok": True, "files": files, "totals": total, "accountsCreated": accounts_created}


def cmd_import(cfg: Config, args, out: Out) -> int:
    account = args.account.strip()
    if not account:
        raise err("bad_config", "--account 不能为空", 2)
    if len(account) > 40:
        raise err("bad_config", "--account 最长 40 个字符", 2)
    if _has_control(account):
        raise err("bad_config", "--account 不能包含控制字符（换行、ESC 等）", 2)
    if args.broker is not None:
        if len(args.broker) > 40:
            raise err("bad_config", "--broker 最长 40 个字符", 2)
        if _has_control(args.broker):
            raise err("bad_config", "--broker 不能包含控制字符（换行、ESC 等）", 2)
    conn = connect(cfg)
    parsed = []
    for raw in args.files:
        p = Path(raw)
        parsed.append((p, parse_trades(p, account, cfg, args.encoding)))

    summaries = []
    created = []
    any_errors = False
    try:
        with transaction(conn):
            account_id, was_created = ensure_account(conn, account, args.broker, cfg.now_iso())
            if was_created:
                created.append(account)
            for p, pt in parsed:
                added = skipped = 0
                for tx in pt.txs:
                    upsert_instrument(conn, tx.code, tx.name, _board(tx.code), cfg.now_iso())
                    if conn.execute("SELECT 1 FROM transactions WHERE row_hash=?", (tx.row_hash,)).fetchone():
                        skipped += 1
                        continue
                    insert_tx(conn, account_id, tx, cfg.now_iso())
                    added += 1
                status = "partial" if pt.errors else "ok"
                if pt.errors:
                    any_errors = True
                if not args.dry_run:
                    msg = f"{len(pt.errors)} 行未导入" if pt.errors else ""
                    insert_import(conn, account, p.name, cfg.now_iso(), pt.rows_read, added, skipped, len(pt.errors), status, msg)
                summaries.append({
                    "file": p.name, "rows": pt.rows_read, "added": added,
                    "skipped": skipped,
                    "errors": [{"line": ln, "message": msg} for ln, msg in pt.errors],
                })
            if args.dry_run:
                raise _DryRun()
    except _DryRun:
        pass
    finally:
        conn.close()

    for name in created:
        out.info(f"新建账户: {name}")
    for idx, (p, pt) in enumerate(parsed):
        summary = summaries[idx]
        out.info(f"账户: {account}")
        out.info(f"文件: {p.name}（{pt.rows_read} 行，表头第 {pt.header_line} 行）")
        out.info(f"新增 {summary['added']} 笔，跳过重复 {summary['skipped']} 笔，错误 {len(pt.errors)} 行")
        by_kind = ", ".join(f"{k} {v}" for k, v in sorted(pt.kinds.items()))
        out.info(f"按类型: {by_kind}" if by_kind else "按类型: 无")
        out.info(f"未识别: {pt.unknown} 行")
        for w in pt.warnings:
            print(f"警告: {w}", file=sys.stderr)
        if not out.json:
            for line, msg in pt.errors[:10]:
                print(f"错误: 第 {line} 行：{msg} (code=bad_row)", file=sys.stderr)
            if len(pt.errors) > 10:
                print(f"还有 {len(pt.errors) - 10} 行错误未显示", file=sys.stderr)
    total = {"added": sum(x["added"] for x in summaries), "skipped": sum(x["skipped"] for x in summaries),
             "errors": sum(len(x["errors"]) for x in summaries)}
    if len(parsed) > 1:
        out.info(f"合计: 新增 {total['added']} 笔，跳过 {total['skipped']} 笔，错误 {total['errors']} 行")
    if any_errors:
        if args.dry_run:
            out.info(f"试运行：有 {total['errors']} 行未导入，未写入数据库")
        else:
            out.info(f"已入库，但有 {total['errors']} 行未导入")
    elif args.dry_run:
        out.info("试运行：未写入数据库")
    if out.json:
        data = _import_summary(summaries, created, args.dry_run)
        if any_errors and not args.dry_run:
            data["committed"] = True
        out.data(data)
    return 2 if any_errors else 0


class _DryRun(Exception):
    pass


def _has_control(value: str) -> bool:
    return any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value)


def _board(code: str) -> str:
    from .csvio import normalize_code
    if code == "CASH.CASH":
        return "cash"
    try:
        return normalize_code(code)[1]
    except ValueError:
        return "unknown"


def cmd_snapshot(cfg: Config, args, out: Out) -> int:
    conn = connect(cfg)
    try:
        row = get_account(conn, args.account)
        if row is None:
            raise err("unknown_account", f"账户不存在：{args.account}", 2)
        as_of = _as_of(cfg, args.as_of)
        if as_of > cfg.today:
            raise err("bad_date", f"快照日期 {as_of} 晚于今天 {cfg.today}", 2)
        parsed = parse_snapshots(Path(args.file), cfg, args.encoding)
        overwritten = 0
        if not args.dry_run:
            with transaction(conn):
                for snap in parsed.rows:
                    upsert_instrument(conn, snap.code, snap.name or snap.code, _board(snap.code), cfg.now_iso())
                    if snap.name:
                        conn.execute("UPDATE instruments SET name=? WHERE code=?", (snap.name, snap.code))
                    if upsert_snapshot(conn, row["id"], as_of, snap.code, snap.name, snap.quantity,
                                       snap.cost_price, snap.market_value, Path(args.file).name, cfg.now_iso()):
                        overwritten += 1
                if parsed.errors:
                    pass
        else:
            for snap in parsed.rows:
                if conn.execute("SELECT 1 FROM snapshots WHERE account_id=? AND as_of_date=? AND code=?",
                                (row["id"], as_of.isoformat(), snap.code)).fetchone():
                    overwritten += 1
        if out.json:
            out.data({"ok": True, "account": args.account, "asOf": as_of.isoformat(),
                      "rows": len(parsed.rows), "overwritten": overwritten,
                      "errors": [{"line": ln, "message": msg} for ln, msg in parsed.errors]})
        else:
            if parsed.duplicates and not out.json:
                out.warn(f"同代码出现 {parsed.duplicates} 次，取最后一行")
            out.result(f"快照: {args.account} {as_of.isoformat()}，{len(parsed.rows)} 只标的")
            if overwritten:
                out.info(f"提示: 已覆盖 {overwritten} 条")
            if not out.json:
                for line, msg in parsed.errors[:10]:
                    print(f"错误: 第 {line} 行：{msg} (code=bad_row)", file=sys.stderr)
        return 2 if parsed.errors else 0
    finally:
        conn.close()


def cmd_check(cfg: Config, args, out: Out) -> int:
    try:
        conn = connect(cfg)
    except HoldbookError as exc:
        if exc.code == "no_data":
            _check_empty(out, cfg)
            return 0
        raise
    try:
        accounts = all_accounts(conn)
        if args.account:
            row = get_account(conn, args.account)
            if row is None:
                raise err("unknown_account", f"账户不存在：{args.account}", 2)
            accounts = [row]
        if not accounts:
            _check_empty(out, cfg)
            return 0
        as_of_arg = _as_of(cfg, args.as_of) if args.as_of else None
        selected = []
        missing = []
        warnings = []
        for a in accounts:
            if as_of_arg:
                snaps = snapshots_for(conn, a["id"], as_of_arg)
                if not snaps:
                    last = latest_snapshot_date(conn, a["id"])
                    missing.append((a["name"], last))
                else:
                    selected.append((a, as_of_arg, snaps))
            else:
                last = latest_snapshot_date(conn, a["id"])
                if not last:
                    warnings.append(f"{a['name']} 没有快照，已跳过")
                    continue
                d = date.fromisoformat(last)
                selected.append((a, d, snapshots_for(conn, a["id"], d)))
        if missing:
            detail = "；".join(f"{n}（最近快照 {d or '无'}）" for n, d in missing)
            raise err("no_data", f"--as-of {as_of_arg} 缺少快照：{detail}", 2)
        for w in warnings:
            if not out.json:
                out.warn(w)
        if not selected:
            _check_empty(out, cfg)
            return 0
        results = []
        for a, d, snaps in selected:
            txs = [row_to_tx(r) for r in transactions_for(conn, a["id"], d)]
            results.append(check_account(a["name"], d, txs, snaps, cfg))
        top_asof = max(r["snapshotDate"] for r in results)
        differed = sum(r["differed"] for r in results)
        if out.json:
            out.data({
                "ok": True, "asOf": top_asof, "strict": args.strict, "differed": differed,
                "accounts": [{
                    "account": r["account"], "snapshot": r["snapshot"], "snapshotDate": r["snapshotDate"],
                    "matched": r["matched"], "differed": r["differed"],
                    "items": [_item_json(i) for i in r["items"]],
                } for r in results],
            })
        else:
            for r in results:
                out.result(f"对账: {r['account']} @ {r['snapshotDate']}（快照来源 {r['snapshot']}）")
                out.result(f"标的 {r['matched'] + r['differed']} 只，一致 {r['matched']} 只，差异 {r['differed']} 只")
                out.result("")
                for it in r["items"]:
                    line = _item_text(it)
                    out.result(line)
                    for h in it.hints:
                        out.result(f"                      提示: {h}")
                if r["differed"]:
                    out.result(f"结论: 有 {r['differed']} 只对不上，先按上面提示逐条核对。")
                out.result("")
        if args.strict and differed:
            return 3
        return 0
    finally:
        conn.close()


def _check_empty(out: Out, cfg: Config) -> None:
    if out.json:
        out.data({"ok": True, "asOf": cfg.today.isoformat(), "accounts": [], "differed": 0})
    else:
        out.result("还没有数据：先运行 holdbook import 和 holdbook snapshot")


def _item_json(i) -> dict:
    def f(v):
        return None if v is None else f"{v:.4f}"
    return {
        "code": i.code, "name": i.name, "qtyCalc": f(i.qty_calc),
        "qtySnapshot": f(i.qty_snap), "avgCostCalc": f(i.avg_cost_calc),
        "costSnapshot": f(i.cost_snap), "qtyDiff": f(i.qty_diff),
        "costDiff": f(i.cost_diff), "status": i.status, "hints": i.hints,
    }


def _item_text(i) -> str:
    qc = fmt_qty(i.qty_calc)
    if i.qty_snap is None:
        return f"{i.code} {i.name}  账上 {qc} 股 / 快照 —  只有账本"
    qs = fmt_qty(i.qty_snap)
    if i.status == "matched":
        return f"{i.code} {i.name}  账上 {qc} 股 / 快照 {qs} 股  一致"
    if i.status == "cost_off":
        return f"{i.code} {i.name}  账上 {qc} 股 / 快照 {qs} 股  成本价不一致：差 {fmt_qty(i.cost_diff)}"
    if i.status == "no_cost":
        return f"{i.code} {i.name}  账上 {qc} 股 / 快照 {qs} 股  快照没有成本价"
    if i.status == "only_snapshot":
        return f"{i.code} {i.name}  账上 0 股 / 快照 {qs} 股  只有快照"
    if i.status == "qty_missing":
        return f"{i.code} {i.name}  账上 {qc} 股 / 快照 {qs} 股  账上少 {fmt_qty(abs(i.qty_diff))} 股：可能漏导入买入或送转"
    if i.status == "qty_extra":
        return f"{i.code} {i.name}  账上 {qc} 股 / 快照 {qs} 股  账上多 {fmt_qty(abs(i.qty_diff))} 股：可能漏导入卖出"
    return f"{i.code} {i.name}  账上 {qc} 股 / 快照 {qs} 股  {i.status}"


def cmd_report(cfg: Config, args, out: Out) -> int:
    if args.open and args.format == "text":
        raise err("bad_config", "--open 隐含 --format html，不能与 --format text 一起用", 2)
    if args.open and args.out == "-":
        raise err("bad_config", "--open 不能与 --out - 一起用", 2)
    if out.json and args.open:
        raise err("bad_config", "--json 不能与 --open 一起用", 2)
    if out.json and args.out:
        raise err("bad_config", "--json 不能与 --out 一起用", 2)
    fmt = "html" if args.open else (args.format or "text")
    if args.out and fmt != "html":
        raise err("bad_config", "--out 只能与 --format html 一起用", 2)
    if args.out == "-" and fmt != "html":
        raise err("bad_config", "--out - 只能与 --format html 一起用", 2)
    market = args.market or cfg.market
    as_of = _as_of(cfg, args.as_of)
    conn = connect(cfg)
    try:
        data = build(conn, cfg, args.account, as_of, market, args.prices)
        if out.json:
            out.data(to_json(data))
            return 2 if data.get("manualErrors") else 0
        if fmt == "text":
            out.result(text_report(data))
            return 2 if data.get("manualErrors") else 0
        body = html_report(data)
        if args.out == "-":
            print(body)
            print(f"报告: HTML 已写到 stdout（{len(body.encode('utf-8'))} 字节）", file=sys.stderr)
            return 2 if data.get("manualErrors") else 0
        if args.out:
            path = Path(args.out)
            if path.exists() and path.is_dir():
                raise err("file_not_writable", f"输出路径是目录：{path}", 1)
        else:
            base = cfg.report_dir / f"report-{as_of.isoformat()}-{cfg.stamp()}.html"
            path = base
            n = 2
            while path.exists():
                path = cfg.report_dir / f"report-{as_of.isoformat()}-{cfg.stamp()}-{n}.html"
                n += 1
        raw = body.encode("utf-8")
        if len(raw) > 20 * 1024 * 1024:
            raise err("report_write_failed", "报告超过 20 MiB 上限", 1)
        try:
            atomic_write(path, body)
        except OSError as exc:
            raise err("report_write_failed", f"报告写入失败：{path}（{exc}）", 1)
        out.info(f"报告: {path}（{len(raw)} 字节）")
        if args.open:
            _open_file(path, out)
        return 2 if data.get("manualErrors") else 0
    finally:
        conn.close()


def _open_file(path: Path, out: Out) -> None:
    try:
        if sys.platform == "darwin":
            rc = subprocess.run(["open", str(path)], check=False).returncode
        elif os.name == "nt":
            rc = subprocess.run(["cmd", "/c", "start", "", str(path)], check=False).returncode
        else:
            rc = subprocess.run(["xdg-open", str(path)], check=False).returncode
        if rc != 0:
            out.warn(f"打开报告失败：退出码 {rc}")
    except OSError as exc:
        out.warn(f"打开报告失败：{exc}")


def cmd_explain(cfg: Config, args, out: Out) -> int:
    try:
        code, _ = normalize_code(args.code)
    except ValueError as exc:
        raise err("bad_code", f"{args.code}: {exc}", 2)
    as_of = _as_of(cfg, args.as_of)
    conn = connect(cfg)
    try:
        accounts = all_accounts(conn)
        if args.account:
            row = get_account(conn, args.account)
            if row is None:
                raise err("unknown_account", f"账户不存在：{args.account}", 2)
            accounts = [row]
        elif len(accounts) > 1:
            raise err("bad_config", "库里有多个账户，请用 --account 指定", 2)
        if not accounts:
            raise err("no_data", "还没有数据", 1)
        a = accounts[0]
        txs = [row_to_tx(r) for r in transactions_for(conn, a["id"], as_of) if r["code"] == code]
        if not txs:
            raise err("no_data", f"{code} 没有交易", 1)
        try:
            rows, st = explain_ledger(txs, code, as_of)
        except ValueError as exc:
            raise err("ledger_negative", f"{code}: {exc}", 2)
        name_row = conn.execute("SELECT name FROM instruments WHERE code=?", (code,)).fetchone()
        name = name_row["name"] if name_row and name_row["name"] else code
        if out.json:
            out.data({
                "ok": True, "code": code, "name": name, "account": a["name"],
                "rows": [{
                    "date": r.date.isoformat(), "kind": r.kind,
                    "quantity": f"{r.quantity:.4f}", "price": f"{r.price:.4f}",
                    "amount": f"{r.amount:.2f}", "fees": f"{r.fees:.2f}",
                    "qtyAfter": f"{r.qty_after:.4f}",
                    "avgCostAfter": None if r.avg_cost_after is None else f"{r.avg_cost_after:.4f}",
                } for r in rows],
                "final": {
                    "quantity": f"{st.quantity:.4f}",
                    "avgCost": None if st.avg_cost is None else f"{st.avg_cost:.4f}",
                    "realizedPnl": f"{st.realized:.2f}",
                },
            })
        else:
            out.result(f"{code} {name} · {a['name']}")
            out.result("日期        类型        数量      价格        金额        费用      数量后")
            for r in rows:
                out.result(f"{r.date.isoformat()}  {r.kind:<10} {fmt_qty(r.quantity):>8}  {fmt_qty(r.price):>8}  "
                           f"{fmt_money(r.amount):>10}  {fmt_money(r.fees):>7}  {fmt_qty(r.qty_after):>8}")
            out.result(f"重放结果: 数量 {fmt_qty(st.quantity)}，平均成本 {fmt_qty(st.avg_cost) if st.avg_cost is not None else '—'}，已实现盈亏 {fmt_money(st.realized)}")
        return 0
    finally:
        conn.close()


def cmd_status(cfg: Config, args, out: Out) -> int:
    conn = connect(cfg)
    try:
        accounts = all_accounts(conn)
        rows = []
        for a in accounts:
            n = conn.execute("SELECT COUNT(*) FROM transactions WHERE account_id=?", (a["id"],)).fetchone()[0]
            li = conn.execute("SELECT MAX(imported_at) FROM imports WHERE account_name=?", (a["name"],)).fetchone()[0]
            ls = latest_snapshot_date(conn, a["id"])
            rows.append({"name": a["name"], "transactions": int(n), "lastImportAt": li, "lastSnapshotDate": ls})
        instruments = int(conn.execute("SELECT COUNT(*) FROM instruments WHERE code != 'CASH.CASH'").fetchone()[0])
        if out.json:
            out.data({"ok": True, "dbPath": str(cfg.db_path), "schemaVersion": 1,
                      "market": cfg.market, "accounts": rows, "instruments": instruments})
        else:
            out.result(f"库: {cfg.db_path}")
            out.result(f"schema 1 · 账户 {len(rows)} · 标的 {instruments} · 行情 {cfg.market}")
            for r in rows:
                out.result(f"  {r['name']}: {r['transactions']} 笔 · 最近导入 {r['lastImportAt'] or '—'} · 最近快照 {r['lastSnapshotDate'] or '—'}")
        return 0
    finally:
        conn.close()


def cmd_backup(cfg: Config, args, out: Out) -> int:
    conn = connect(cfg)
    try:
        path = Path(args.out) if args.out else cfg.data_dir / f"holdbook-backup-{cfg.stamp()}.db"
        if path.exists():
            if path.is_dir():
                raise err("file_not_writable", f"备份路径是目录：{path}", 1)
            raise err("conflict", f"备份目标已存在，不覆盖：{path}", 2)
        try:
            size = backup_db(conn, cfg, path)
        except Exception as exc:
            raise err("file_not_writable", f"备份失败：{path}（{exc}）", 1)
        if out.json:
            out.data({"ok": True, "path": str(path), "bytes": size})
        else:
            out.result(f"备份: {path}（{size} 字节）")
        return 0
    finally:
        conn.close()


def cmd_reset(cfg: Config, args, out: Out) -> int:
    if not args.yes:
        raise err("conflict", "reset 会删除全部数据；确认请加 --yes", 2)
    conn = connect(cfg)
    conn.close()
    deleted = reset_db(cfg)
    if out.json:
        out.data({"ok": True, "deleted": deleted})
    else:
        out.result(f"已删除 {len(deleted)} 个文件")
    return 0


def cmd_version(cfg: Config | None, args, out: Out) -> int:
    if out.json:
        out.data({"ok": True, "version": __version__, "python": platform_version()})
    else:
        out.result(f"holdbook {__version__}")
    return 0


def platform_version() -> str:
    return f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"


def cmd_demo(cfg: Config, args, out: Out) -> int:
    from .demo import run_demo
    return run_demo(Path(args.dir), out, cfg)


# ---------------- main ----------------

def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    json_flag = "--json" in argv
    quiet_flag = "--quiet" in argv
    verbose_flag = "-v" in argv or "--verbose" in argv
    argv = [a for a in argv if a not in ("--json", "--quiet")]
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
        if getattr(args, "version", False):
            return cmd_version(None, args, Out(json_flag, quiet_flag))
        if not getattr(args, "cmd", None):
            parser.print_help()
            return 0
        args.json = json_flag
        args.quiet = quiet_flag
        cfg = load_config(getattr(args, "data_dir", None), getattr(args, "today", None))
        out = Out(json_flag, quiet_flag)
        cmd = args.cmd
        if cmd == "init":
            return cmd_init(cfg, args, out)
        if cmd == "import":
            return cmd_import(cfg, args, out)
        if cmd == "snapshot":
            return cmd_snapshot(cfg, args, out)
        if cmd == "check":
            return cmd_check(cfg, args, out)
        if cmd == "report":
            return cmd_report(cfg, args, out)
        if cmd == "explain":
            return cmd_explain(cfg, args, out)
        if cmd == "status":
            return cmd_status(cfg, args, out)
        if cmd == "backup":
            return cmd_backup(cfg, args, out)
        if cmd == "reset":
            return cmd_reset(cfg, args, out)
        if cmd == "version":
            return cmd_version(cfg, args, out)
        if cmd == "demo":
            return cmd_demo(cfg, args, out)
        raise err("bad_config", f"未知命令：{cmd}", 2)
    except HoldbookError as exc:
        return _emit_error(exc, Out(json_flag, quiet_flag))
    except SystemExit:
        raise
    except Exception as exc:
        if verbose_flag:
            import traceback
            traceback.print_exc()
        e = HoldbookError("internal_error", f"内部错误：{exc}", 1)
        return _emit_error(e, Out(json_flag, quiet_flag))


if __name__ == "__main__":
    sys.exit(main())
