<div align="center">
  <img src="docs/logo.svg" width="72" height="72" alt="Holdbook">
  <h1>Holdbook</h1>
  <p>Turn broker CSV exports into a line-by-line reconcilable cost ledger.</p>
  <p>English · <a href="README.md">简体中文</a></p>
  <p>
    <img src="https://img.shields.io/badge/python-3.11%2B-2f6f4e" alt="Python 3.11+">
    <img src="https://img.shields.io/badge/tests-96%20passing-2f6f4e" alt="96 tests passing">
    <img src="https://img.shields.io/badge/version-0.1.0-2f6f4e" alt="version 0.1.0">
    <img src="https://img.shields.io/badge/license-MIT-2f6f4e" alt="MIT">
  </p>
</div>

![Holdbook report](docs/screenshots/report-light.png)

## What it is

Holdbook is a local-first reconciliation tool for Chinese A-share investors. It imports broker trade CSVs and a position snapshot, replays cost basis, checks quantity/cost differences, and writes a single-file HTML report.

It is for a specific job: **one investor, one to three brokerage accounts, the same stocks/ETFs across accounts, and a monthly ten-minute reconciliation.** It is not a trading app, a market predictor, a fund look-through tool, or a tax tool.

- Fully offline by default. The only network call is the optional Tencent quote endpoint, and only with `--market live`.
- No account, cloud sync, telemetry, or upload.
- Data lives in `~/.local/share/holdbook` with `0700` directory and `0600` database permissions.

## Before you use it

- CSV only; no broker API, PDF, or Excel parsing.
- A-share stocks, ETFs, and convertible bonds only; no funds, options, futures, margin, HK/US, or multi-currency.
- Quotes are optional: `off` (default), `mock` (built-in table), or `live` (Tencent public quote, may fail).
- Reports contain your real positions. Check before sharing them.
- Not investment, tax, or audit advice.

### Applies

1. You have two or three brokerage accounts and want quantities, costs, and realized P&L to line up.
2. You export trades and positions monthly and want a repeatable check.
3. You want an offline HTML report instead of an app's total assets.
4. You want an XIRR based on real cash flows including fees and dividends.

### Does not apply

1. Automatic broker sync, intraday valuation, or order placement.
2. Fund look-through, factor exposure, or portfolio optimization.
3. Options, futures, margin, HK/US stocks, or multiple currencies.
4. Broker-grade clearing, tax filing, or audit workpapers.

## Features

- Import trades: buy, sell, dividend, bonus shares, rights issue, IPO allotment, fees, interest, transfers.
- Import snapshots: code, quantity, optional cost and market value; overwrite by account/date/code.
- Reconcile with seven statuses and actionable hints.
- Replay moving weighted-average cost with 12-decimal internal precision; buy fees are capitalized.
- Report as text, single-file HTML (light/dark/375px), or JSON: market value, cost, unrealized/realized P&L, dividends, interest, fees, fee ratio, concentration, XIRR.
- `explain CODE` replays one instrument trade by trade.
- `backup` uses `VACUUM INTO`; `reset --yes` deletes only the database files.

See [docs/features.md](docs/features.md) for rules and CSV aliases, and [SPEC.md](SPEC.md) for the contract.

## Quick start

Requirements: Python 3.11+; no database server, Node, Docker, or API key.

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install .
holdbook --version
```

Offline demo:

```bash
holdbook demo --dir holdbook-demo
```

This creates sample CSVs and `holdbook-demo/report.html`. The sample snapshot is deliberately 10 shares short to show a reconciliation difference; all quotes come from the built-in `mock` table.

With your own data:

```bash
holdbook init
holdbook import trades.csv --account "Huatai" --broker "Huatai Securities"
holdbook snapshot positions.csv --account "Huatai" --as-of 2026-10-29
holdbook check
holdbook report --market mock
```

The minimal trade CSV needs `date,action`; the minimal snapshot CSV needs `code,quantity`. See [docs/features.md](docs/features.md#交割单-csv) for aliases and validation.

## Commands

| Command | Purpose |
|---|---|
| `holdbook init` | Create database and report directory; idempotent |
| `holdbook import FILE... --account NAME [--broker NAME] [--dry-run]` | Import trades |
| `holdbook snapshot FILE --account NAME [--as-of DATE] [--dry-run]` | Import positions |
| `holdbook check [--account NAME] [--as-of DATE] [--strict]` | Reconcile; `--strict` exits 3 on differences |
| `holdbook report [--account NAME] [--as-of DATE] [--market off\|live\|mock] [--prices FILE] [--format text\|html] [--out PATH] [--open]` | Build report |
| `holdbook explain CODE [--account NAME] [--as-of DATE]` | Replay one instrument |
| `holdbook status` | Database, accounts, imports, snapshots, instruments |
| `holdbook backup [--out PATH]` | Consistent copy via `VACUUM INTO` |
| `holdbook reset --yes` | Delete the database (keep other files) |
| `holdbook demo [--dir PATH]` | Offline demo |
| `holdbook version` / `--version` | Version |

Global flags: `--data-dir PATH`, `--json`, `--today YYYY-MM-DD`, `--quiet`, `-v/--verbose`. Exit codes: `0` success, `1` runtime/data, `2` input/config, `3` strict reconciliation difference, `4` market unavailable.

## Data and recovery

- Default data directory: `~/.local/share/holdbook`; override with `HOLDBOOK_DATA_DIR` or `--data-dir`.
- Database: `<data_dir>/holdbook.db`; override with `HOLDBOOK_DB_PATH`.
- Reports: `<data_dir>/reports`.
- `holdbook backup` writes `<data_dir>/holdbook-backup-<time>.db`; it never overwrites.
- `holdbook reset --yes` deletes only `holdbook.db`, `-wal`, and `-shm`.
- Back up before upgrading. The schema version is 1; a newer version is refused rather than guessed.

## Performance (measured)

macOS arm64, Python 3.13.5, SQLite 3.49.1, SSD; 10,000 rows, 50 instruments, one account, no network, `report --market off`:

- `import`: 0.63 s; database 3.5 MB.
- `report --json`: 0.42 s.

These are order-of-magnitude numbers, not guarantees. Limits: 100 MiB and 200,000 rows per file; larger files are refused.

## Development

```bash
python3 -m unittest discover -s tests -v
```

96 tests, all offline: temporary directories, temporary SQLite databases, a local fake HTTP server, and a fixed `HOLDBOOK_NOW` clock. No fixed sleeps. See [docs/development.md](docs/development.md).

## Docs

- [Features and CSV rules](docs/features.md)
- [Development and testing](docs/development.md)
- [External APIs](docs/external-apis.md)
- [Deviations](docs/deviations.md)
- [Deployment](docs/deploy.md)
- [SPEC](SPEC.md) / [DESIGN](DESIGN.md)
- [Changelog](CHANGELOG.md)

## License

MIT, see [LICENSE](LICENSE).

## Disclaimer

The ledger replays exactly what you import. It does not guarantee a broker's or tax authority's exact basis; quotes come from a third-party public endpoint and may be delayed, missing, or wrong. Reports are for personal reconciliation only, not investment advice. Keep your original CSV exports.
