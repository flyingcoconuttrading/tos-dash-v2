"""
tick_recorder.py — Independent tick history recorder for tos-dash-v2.

Reads spy_price.json and option_chain.json on each poll cycle and appends
rows to a DuckDB replay database. Designed to be managed as a subprocess by
api.py — do not run directly in production.

Control files (written by api.py):
  tick_recorder.stop  — clean shutdown requested
  tick_recorder.pause — recording paused (file exists = paused)

PID file: tick_recorder.pid (prevents duplicate processes)
"""

import json
import os
import sys
import time
import duckdb
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

THIS_DIR = Path(__file__).parent

# ── PID guard ─────────────────────────────────────────────────────────────────
PID_FILE  = THIS_DIR / "tick_recorder.pid"
STOP_FILE = THIS_DIR / "tick_recorder.stop"
PAUSE_FILE = THIS_DIR / "tick_recorder.pause"

def _check_pid():
    """Return True if another instance is already running."""
    if PID_FILE.exists():
        try:
            old_pid = int(PID_FILE.read_text().strip())
            # On Windows, check if PID is alive
            import ctypes
            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, old_pid)
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True  # process alive
        except Exception:
            pass
    return False

if _check_pid():
    print("[tick_recorder] Another instance running — exiting.", file=sys.stderr)
    sys.exit(0)

PID_FILE.write_text(str(os.getpid()))

# ── Load config ────────────────────────────────────────────────────────────────
def load_config() -> dict:
    try:
        return json.loads((THIS_DIR / "config.json").read_text())
    except Exception:
        return {}

cfg = load_config()

REPLAY_DB_DIR = Path(cfg.get("replay_db_path", "D:/tos-dash-v2-replay/"))
POLL_MS       = cfg.get("poll_ms", 500)
POLL_SEC      = POLL_MS / 1000.0

REPLAY_DB_DIR.mkdir(parents=True, exist_ok=True)

DUCKDB_PATH = REPLAY_DB_DIR / "ticks.duckdb"

# ── DuckDB connection and table creation ───────────────────────────────────────
def _open_db() -> duckdb.DuckDBPyConnection:
    conn = duckdb.connect(str(DUCKDB_PATH))
    conn.execute("""
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
    conn.execute("""
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
    return conn

PRICE_FILE = THIS_DIR / "spy_price.json"
CHAIN_FILE = THIS_DIR / "option_chain.json"

# ── Market hours check ─────────────────────────────────────────────────────────
def _is_market_hours() -> bool:
    """Return True if current ET time is within 9:30–16:00."""
    now_et = datetime.now(ZoneInfo("America/New_York"))
    market_open  = now_et.hour > 9 or (now_et.hour == 9 and now_et.minute >= 30)
    market_close = now_et.hour < 16
    return market_open and market_close

# ── Main loop ──────────────────────────────────────────────────────────────────
print(f"[tick_recorder] Starting — db={DUCKDB_PATH}  poll={POLL_MS}ms", file=sys.stderr)

db_conn  = _open_db()
last_tick = -1

try:
    while True:
        t0 = time.perf_counter()

        # Clean shutdown
        if STOP_FILE.exists():
            print("[tick_recorder] Stop file detected — shutting down.", file=sys.stderr)
            break

        # Paused
        if PAUSE_FILE.exists():
            time.sleep(max(0.0, POLL_SEC - (time.perf_counter() - t0)))
            continue

        # Market hours gate: 9:30–16:00 ET only
        if not _is_market_hours():
            time.sleep(max(0.0, POLL_SEC - (time.perf_counter() - t0)))
            continue

        try:
            price_data = json.loads(PRICE_FILE.read_text())
            chain_data = json.loads(CHAIN_FILE.read_text())
        except Exception:
            time.sleep(max(0.0, POLL_SEC - (time.perf_counter() - t0)))
            continue

        tick = price_data.get("tick", -1)
        if tick == last_tick:
            time.sleep(max(0.0, POLL_SEC - (time.perf_counter() - t0)))
            continue
        last_tick = tick

        now          = datetime.now()
        recorded_at  = now.isoformat(timespec="milliseconds")
        date_str     = now.date().isoformat()

        try:
            # Write spy row
            db_conn.execute("""
                INSERT INTO spy_ticks
                (recorded_at, date, spy_price, vix, tick_val, trin_val, trinq_val,
                 add_val, qqq_price, iwm_price, nq_price)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                recorded_at, date_str,
                price_data.get("last"),
                price_data.get("vix_last"),
                price_data.get("ntick_val"),
                price_data.get("trin_val"),
                price_data.get("trinq_val"),
                price_data.get("add_val"),
                price_data.get("qqq_last"),
                price_data.get("iwm_last"),
                price_data.get("nq_last"),
            ))

            # Write chain rows (only symbols with a bid or last price)
            chain = chain_data.get("chain", {})
            chain_rows = []
            for sym, fields in chain.items():
                if fields.get("BID") is None and fields.get("LAST") is None:
                    continue
                chain_rows.append((
                    recorded_at, date_str, sym,
                    fields.get("BID"),
                    fields.get("ASK"),
                    fields.get("LAST"),
                    fields.get("DELTA"),
                    fields.get("GAMMA"),
                    fields.get("THETA"),
                    fields.get("VEGA"),
                    fields.get("IMPL_VOL"),
                    fields.get("VOLUME"),
                    fields.get("OPEN_INT"),
                ))

            if chain_rows:
                db_conn.executemany("""
                    INSERT INTO chain_ticks
                    (recorded_at, date, symbol, bid, ask, last, delta, gamma, theta,
                     vega, iv, volume, open_interest)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, chain_rows)

        except Exception as e:
            print(f"[tick_recorder] Write error: {e}", file=sys.stderr)

        time.sleep(max(0.0, POLL_SEC - (time.perf_counter() - t0)))

except KeyboardInterrupt:
    pass
finally:
    db_conn.close()
    PID_FILE.unlink(missing_ok=True)
    print("[tick_recorder] Stopped.", file=sys.stderr)
