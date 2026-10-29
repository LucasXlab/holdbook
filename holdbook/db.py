from __future__ import annotations

import os
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

from .config import Config
from .errors import HoldbookError, err
from .models import D

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS accounts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  broker TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS instruments (
  code TEXT PRIMARY KEY,
  name TEXT NOT NULL DEFAULT '',
  board TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS transactions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id INTEGER NOT NULL REFERENCES accounts(id),
  trade_date TEXT NOT NULL,
  kind TEXT NOT NULL,
  code TEXT NOT NULL REFERENCES instruments(code),
  quantity TEXT NOT NULL,
  price TEXT NOT NULL,
  amount TEXT NOT NULL,
  fee_commission TEXT NOT NULL DEFAULT '0.00',
  fee_stamp TEXT NOT NULL DEFAULT '0.00',
  fee_transfer TEXT NOT NULL DEFAULT '0.00',
  fee_other TEXT NOT NULL DEFAULT '0.00',
  note TEXT NOT NULL DEFAULT '',
  source_file TEXT NOT NULL,
  source_line INTEGER NOT NULL,
  row_hash TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tx_account_date ON transactions(account_id, trade_date, id);
CREATE INDEX IF NOT EXISTS idx_tx_code ON transactions(code);
CREATE INDEX IF NOT EXISTS idx_tx_file ON transactions(source_file);
CREATE TABLE IF NOT EXISTS snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id INTEGER NOT NULL REFERENCES accounts(id),
  as_of_date TEXT NOT NULL,
  code TEXT NOT NULL REFERENCES instruments(code),
  name TEXT NOT NULL DEFAULT '',
  quantity TEXT NOT NULL,
  cost_price TEXT,
  market_value TEXT,
  source_file TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(account_id, as_of_date, code)
);
CREATE INDEX IF NOT EXISTS idx_snap_account_date ON snapshots(account_id, as_of_date);
CREATE TABLE IF NOT EXISTS prices (
  code TEXT NOT NULL REFERENCES instruments(code),
  date TEXT NOT NULL,
  close TEXT NOT NULL,
  source TEXT NOT NULL,
  fetched_at TEXT NOT NULL,
  PRIMARY KEY(code, date)
);
CREATE TABLE IF NOT EXISTS imports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  account_name TEXT NOT NULL,
  file TEXT NOT NULL,
  imported_at TEXT NOT NULL,
  rows_read INTEGER NOT NULL,
  added INTEGER NOT NULL,
  skipped INTEGER NOT NULL,
  errors INTEGER NOT NULL,
  status TEXT NOT NULL,
  message TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_imports_time ON imports(imported_at);
"""


def _apply_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.execute("INSERT OR IGNORE INTO meta(key,value) VALUES('schema_version','1')")
    conn.execute("INSERT OR IGNORE INTO meta(key,value) VALUES('created_at',?)", (datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),))
    conn.execute("PRAGMA user_version=1")
    conn.commit()


def _check_schema(conn: sqlite3.Connection) -> None:
    try:
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    except sqlite3.DatabaseError as exc:
        raise err("db_corrupt", f"数据库无法读取：{exc}", 1)
    if row is None:
        raise err("conflict", "这个数据库不是 Holdbook 的库（缺 meta.schema_version），不覆盖", 1)
    if str(row[0]) != "1":
        raise err("conflict", f"数据库版本 {row[0]} 高于本程序支持的 1，请升级", 1)
    ok = conn.execute("PRAGMA quick_check").fetchone()
    if not ok or str(ok[0]).lower() != "ok":
        raise err("db_corrupt", "数据库完整性检查失败；先运行 holdbook backup 备份，再重建", 1)


def init_db(cfg: Config) -> bool:
    """Create or open the DB. Returns True if newly created."""
    try:
        cfg.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(cfg.data_dir, 0o700)
    except OSError as exc:
        raise err("data_dir_unwritable", f"数据目录不可写：{cfg.data_dir}（{exc}）", 1)
    try:
        cfg.db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        raise err("data_dir_unwritable", f"数据库目录不可写：{cfg.db_path.parent}（{exc}）", 1)

    existed = cfg.db_path.exists()
    if existed:
        conn = None
        try:
            conn = sqlite3.connect(cfg.db_path)
            _check_schema(conn)
            conn.close()
            return False
        except HoldbookError:
            if conn is not None:
                conn.close()
            raise
        except sqlite3.DatabaseError as exc:
            if conn is not None:
                conn.close()
            raise err("db_corrupt", f"数据库无法打开：{exc}", 1)

    try:
        conn = sqlite3.connect(cfg.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _apply_schema(conn)
        conn.execute("UPDATE meta SET value=? WHERE key='created_at'", (cfg.now_iso(),))
        conn.commit()
        conn.close()
        os.chmod(cfg.db_path, 0o600)
        (cfg.data_dir / "reports").mkdir(mode=0o700, exist_ok=True)
        return True
    except OSError as exc:
        raise err("data_dir_unwritable", f"数据库文件不可写：{cfg.db_path}（{exc}）", 1)


def connect(cfg: Config) -> sqlite3.Connection:
    if not cfg.db_path.exists():
        raise err("no_data", "还没有数据库：先运行 holdbook init", 1)
    conn = None
    try:
        conn = sqlite3.connect(cfg.db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        _check_schema(conn)
        return conn
    except HoldbookError:
        if conn is not None:
            conn.close()
        raise
    except sqlite3.DatabaseError as exc:
        if conn is not None:
            conn.close()
        raise err("db_corrupt", f"数据库无法打开：{exc}", 1)


def transaction(conn: sqlite3.Connection):
    class _T:
        def __enter__(self):
            conn.execute("BEGIN IMMEDIATE")
            return conn

        def __exit__(self, exc_type, exc, tb):
            if exc_type is None:
                conn.commit()
            else:
                conn.rollback()
            return False

    return _T()


def ensure_account(conn: sqlite3.Connection, name: str, broker: str | None, now_iso: str) -> tuple[int, bool]:
    row = conn.execute("SELECT id FROM accounts WHERE name=?", (name,)).fetchone()
    if row:
        if broker is not None:
            conn.execute("UPDATE accounts SET broker=? WHERE id=?", (broker, row["id"]))
        return int(row["id"]), False
    cur = conn.execute(
        "INSERT INTO accounts(name, broker, created_at) VALUES(?,?,?)",
        (name, broker or "", now_iso),
    )
    return int(cur.lastrowid), True


def upsert_instrument(conn: sqlite3.Connection, code: str, name: str, board: str, now_iso: str) -> None:
    row = conn.execute("SELECT name, board FROM instruments WHERE code=?", (code,)).fetchone()
    if row:
        new_name = name or row["name"]
        new_board = board if board != "unknown" else row["board"]
        if new_name != row["name"] or new_board != row["board"]:
            conn.execute("UPDATE instruments SET name=?, board=?, updated_at=? WHERE code=?", (new_name, new_board, now_iso, code))
    else:
        conn.execute("INSERT INTO instruments(code,name,board,updated_at) VALUES(?,?,?,?)", (code, name, board, now_iso))


def insert_tx(conn: sqlite3.Connection, account_id: int, tx, now_iso: str) -> None:
    conn.execute(
        """INSERT INTO transactions(
          account_id, trade_date, kind, code, quantity, price, amount,
          fee_commission, fee_stamp, fee_transfer, fee_other,
          note, source_file, source_line, row_hash, created_at)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            account_id, tx.trade_date.isoformat(), tx.kind, tx.code,
            str(tx.quantity), str(tx.price), str(tx.amount),
            str(tx.fee_commission), str(tx.fee_stamp), str(tx.fee_transfer), str(tx.fee_other),
            tx.note, tx.source_file, tx.source_line, tx.row_hash, now_iso,
        ),
    )


def tx_exists(conn: sqlite3.Connection, row_hash: str) -> bool:
    return conn.execute("SELECT 1 FROM transactions WHERE row_hash=?", (row_hash,)).fetchone() is not None


def insert_import(conn: sqlite3.Connection, account: str, file: str, now_iso: str,
                  rows: int, added: int, skipped: int, errors: int, status: str, message: str = "") -> None:
    conn.execute(
        "INSERT INTO imports(account_name,file,imported_at,rows_read,added,skipped,errors,status,message) VALUES(?,?,?,?,?,?,?,?,?)",
        (account, file, now_iso, rows, added, skipped, errors, status, message[:200]),
    )


def get_account(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM accounts WHERE name=?", (name,)).fetchone()


def all_accounts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM accounts ORDER BY id"))


def transactions_for(conn: sqlite3.Connection, account_id: int, as_of: date):
    return list(conn.execute(
        "SELECT * FROM transactions WHERE account_id=? AND trade_date<=? ORDER BY trade_date,id",
        (account_id, as_of.isoformat()),
    ))


def latest_snapshot_date(conn: sqlite3.Connection, account_id: int) -> str | None:
    row = conn.execute(
        "SELECT as_of_date FROM snapshots WHERE account_id=? ORDER BY as_of_date DESC, id DESC LIMIT 1",
        (account_id,),
    ).fetchone()
    return row["as_of_date"] if row else None


def snapshots_for(conn: sqlite3.Connection, account_id: int, as_of: date):
    return list(conn.execute(
        "SELECT * FROM snapshots WHERE account_id=? AND as_of_date=? ORDER BY id",
        (account_id, as_of.isoformat()),
    ))


def upsert_snapshot(conn: sqlite3.Connection, account_id: int, as_of: date, code: str,
                    name: str, quantity, cost_price, market_value, source_file: str, now_iso: str) -> bool:
    old = conn.execute(
        "SELECT id FROM snapshots WHERE account_id=? AND as_of_date=? AND code=?",
        (account_id, as_of.isoformat(), code),
    ).fetchone()
    if old:
        conn.execute(
            "UPDATE snapshots SET name=?, quantity=?, cost_price=?, market_value=?, source_file=?, created_at=? WHERE id=?",
            (name, str(quantity), None if cost_price is None else str(cost_price),
             None if market_value is None else str(market_value), source_file, now_iso, old["id"]),
        )
        return True
    conn.execute(
        "INSERT INTO snapshots(account_id,as_of_date,code,name,quantity,cost_price,market_value,source_file,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (account_id, as_of.isoformat(), code, name, str(quantity),
         None if cost_price is None else str(cost_price),
         None if market_value is None else str(market_value), source_file, now_iso),
    )
    return False


def price_get(conn: sqlite3.Connection, code: str, as_of: date):
    row = conn.execute(
        "SELECT * FROM prices WHERE code=? AND date<=? ORDER BY date DESC LIMIT 1",
        (code, as_of.isoformat()),
    ).fetchone()
    return row


def price_put(conn: sqlite3.Connection, code: str, at: date, close, source: str, now_iso: str) -> None:
    conn.execute(
        "INSERT INTO prices(code,date,close,source,fetched_at) VALUES(?,?,?,?,?) "
        "ON CONFLICT(code,date) DO UPDATE SET close=excluded.close, source=excluded.source, fetched_at=excluded.fetched_at",
        (code, at.isoformat(), str(close), source, now_iso),
    )


def source_rows(conn: sqlite3.Connection, account_name: str | None = None) -> list[sqlite3.Row]:
    if account_name:
        return list(conn.execute(
            "SELECT file, rows_read, imported_at FROM imports WHERE account_name=? ORDER BY imported_at DESC, id DESC",
            (account_name,),
        ))
    return list(conn.execute(
        "SELECT file, rows_read, imported_at FROM imports ORDER BY imported_at DESC, id DESC"
    ))


def instrument_count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM instruments").fetchone()[0])


def backup_db(conn: sqlite3.Connection, cfg: Config, out: Path) -> int:
    out.parent.mkdir(parents=True, exist_ok=True)
    conn.execute("PRAGMA wal_checkpoint(FULL)")
    p = str(out).replace("'", "''")
    conn.execute(f"VACUUM INTO '{p}'")
    return out.stat().st_size


def reset_db(cfg: Config) -> list[str]:
    deleted = []
    for p in [cfg.db_path, Path(str(cfg.db_path) + "-wal"), Path(str(cfg.db_path) + "-shm")]:
        if p.exists():
            p.unlink()
            deleted.append(str(p))
    return deleted
