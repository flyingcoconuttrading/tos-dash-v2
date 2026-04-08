"""
migrate_ticks.py — One-time migration from per-day SQLite files to ticks.duckdb.

Finds all ticks_YYYY-MM-DD.db files in D:/tos-dash-v2-replay/, reads spy_ticks
and chain_ticks from each, and inserts rows into ticks.duckdb using the new schema.

Uses DuckDB's sqlite extension for fast bulk loading (no row-by-row INSERT).
Handles schema evolution — older files with missing columns get NULL for those fields.
Dates already present in ticks.duckdb are skipped (idempotent).
Source SQLite files are NOT deleted — remove them manually after verifying.

Usage:
    python migrate_ticks.py
"""

import sqlite3
import sys
from datetime import date
from pathlib import Path

try:
    import duckdb
except ImportError:
    print("ERROR: duckdb not installed. Run: pip install duckdb")
    sys.exit(1)

REPLAY_DIR  = Path("D:/tos-dash-v2-replay")
DUCKDB_PATH = REPLAY_DIR / "ticks.duckdb"


def sqlite_columns(db_path: str, table: str) -> set:
    """Return set of column names present in a SQLite table."""
    try:
        conn = sqlite3.connect(db_path)
        cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        conn.close()
        return cols
    except Exception:
        return set()


def col_or_null(cols: set, col: str, alias: str, cast: str = "DOUBLE") -> str:
    """Return 'CAST(col AS type) AS alias' if col exists, else 'NULL AS alias'."""
    if col in cols:
        return f"CAST({col} AS {cast}) AS {alias}"
    return f"NULL AS {alias}"


def main():
    if not REPLAY_DIR.exists():
        print(f"ERROR: replay dir not found: {REPLAY_DIR}")
        sys.exit(1)

    sqlite_files = sorted(REPLAY_DIR.glob("ticks_????-??-??.db"))
    if not sqlite_files:
        print("No ticks_YYYY-MM-DD.db files found — nothing to migrate.")
        return

    print(f"Opening DuckDB: {DUCKDB_PATH}")
    duck = duckdb.connect(str(DUCKDB_PATH))

    # Install and load sqlite extension for fast bulk reads
    duck.execute("INSTALL sqlite; LOAD sqlite")

    # Ensure target tables exist (same schema as tick_recorder.py)
    duck.execute("""
        CREATE TABLE IF NOT EXISTS spy_ticks (
            recorded_at  TIMESTAMP NOT NULL,
            date         DATE NOT NULL,
            spy_price    DOUBLE,
            vix          DOUBLE,
            tick_val     INTEGER,
            trin_val     DOUBLE,
            trinq_val    DOUBLE,
            add_val      INTEGER,
            qqq_price    DOUBLE,
            iwm_price    DOUBLE,
            nq_price     DOUBLE
        )
    """)
    duck.execute("""
        CREATE TABLE IF NOT EXISTS chain_ticks (
            recorded_at   TIMESTAMP NOT NULL,
            date          DATE NOT NULL,
            symbol        TEXT NOT NULL,
            bid           DOUBLE,
            ask           DOUBLE,
            last          DOUBLE,
            delta         DOUBLE,
            gamma         DOUBLE,
            theta         DOUBLE,
            vega          DOUBLE,
            iv            DOUBLE,
            volume        INTEGER,
            open_interest INTEGER
        )
    """)

    # Dates already in DuckDB
    existing_dates = {
        row[0].strftime("%Y-%m-%d") if hasattr(row[0], "strftime") else str(row[0])
        for row in duck.execute("SELECT DISTINCT date FROM spy_ticks").fetchall()
    }
    if existing_dates:
        print(f"Already migrated dates: {sorted(existing_dates)}")

    total_spy   = 0
    total_chain = 0

    for sqlite_path in sqlite_files:
        stem     = sqlite_path.stem          # ticks_2026-04-07
        date_str = stem.replace("ticks_", "")
        try:
            date.fromisoformat(date_str)
        except ValueError:
            print(f"  SKIP {sqlite_path.name} — cannot parse date")
            continue

        if date_str in existing_dates:
            print(f"  SKIP {sqlite_path.name} — date {date_str} already in DuckDB")
            continue

        size_mb  = sqlite_path.stat().st_size / 1_000_000
        src_path = str(sqlite_path).replace("\\", "/")
        print(f"\nMigrating {sqlite_path.name} ({size_mb:.0f} MB, date={date_str}) ...", flush=True)

        # Inspect source columns so we can handle schema evolution
        spy_cols   = sqlite_columns(src_path, "spy_ticks")
        chain_cols = sqlite_columns(src_path, "chain_ticks")
        if not spy_cols:
            print(f"  ERROR: could not read spy_ticks schema from {sqlite_path.name}")
            continue

        try:
            duck.execute(f"ATTACH '{src_path}' AS src (TYPE sqlite, READ_ONLY)")

            # ── spy_ticks ─────────────────────────────────────────────────────
            # Always-present columns: tick_time, last, vix, ntick
            # Conditionally-present: trin, trinq, add_val, qqq_last, iwm_last, nq_last
            spy_select = f"""
                INSERT INTO spy_ticks
                    (recorded_at, date, spy_price, vix, tick_val, trin_val, trinq_val,
                     add_val, qqq_price, iwm_price, nq_price)
                SELECT
                    CAST(tick_time AS TIMESTAMP),
                    CAST('{date_str}' AS DATE),
                    {col_or_null(spy_cols, 'last',     'spy_price')},
                    {col_or_null(spy_cols, 'vix',      'vix')},
                    {col_or_null(spy_cols, 'ntick',    'tick_val', 'INTEGER')},
                    {col_or_null(spy_cols, 'trin',     'trin_val')},
                    {col_or_null(spy_cols, 'trinq',    'trinq_val')},
                    {col_or_null(spy_cols, 'add_val',  'add_val', 'INTEGER')},
                    {col_or_null(spy_cols, 'qqq_last', 'qqq_price')},
                    {col_or_null(spy_cols, 'iwm_last', 'iwm_price')},
                    {col_or_null(spy_cols, 'nq_last',  'nq_price')}
                FROM src.spy_ticks
            """
            duck.execute(spy_select)
            n_spy = duck.execute(
                "SELECT COUNT(*) FROM spy_ticks WHERE date = CAST(? AS DATE)", [date_str]
            ).fetchone()[0]
            print(f"  spy_ticks:   {n_spy:,} rows")

            # ── chain_ticks ───────────────────────────────────────────────────
            # open_int was renamed to open_interest in new schema
            # vega was added in DASH-028 — may be missing in older files
            oi_col = "open_int" if "open_int" in chain_cols else "open_interest"
            chain_select = f"""
                INSERT INTO chain_ticks
                    (recorded_at, date, symbol, bid, ask, last, delta, gamma, theta,
                     vega, iv, volume, open_interest)
                SELECT
                    CAST(tick_time AS TIMESTAMP),
                    CAST('{date_str}' AS DATE),
                    symbol,
                    {col_or_null(chain_cols, 'bid',   'bid')},
                    {col_or_null(chain_cols, 'ask',   'ask')},
                    {col_or_null(chain_cols, 'last',  'last')},
                    {col_or_null(chain_cols, 'delta', 'delta')},
                    {col_or_null(chain_cols, 'gamma', 'gamma')},
                    {col_or_null(chain_cols, 'theta', 'theta')},
                    {col_or_null(chain_cols, 'vega',  'vega')},
                    {col_or_null(chain_cols, 'iv',    'iv')},
                    {col_or_null(chain_cols, 'volume','volume', 'INTEGER')},
                    CAST({oi_col} AS INTEGER) AS open_interest
                FROM src.chain_ticks
            """
            duck.execute(chain_select)
            n_chain = duck.execute(
                "SELECT COUNT(*) FROM chain_ticks WHERE date = CAST(? AS DATE)", [date_str]
            ).fetchone()[0]
            print(f"  chain_ticks: {n_chain:,} rows")

            duck.execute("DETACH src")

            total_spy   += n_spy
            total_chain += n_chain
            print(f"  Done: {date_str}")

        except Exception as e:
            print(f"  ERROR migrating {sqlite_path.name}: {e}")
            try:
                duck.execute("DETACH src")
            except Exception:
                pass

    duck.close()
    print(f"\nMigration complete.")
    print(f"  spy_ticks   total: {total_spy:,} rows")
    print(f"  chain_ticks total: {total_chain:,} rows")
    print("Source SQLite files NOT deleted. Remove them manually after verifying.")


if __name__ == "__main__":
    main()
