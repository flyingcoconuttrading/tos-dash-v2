"""
tick_recorder.py — Independent tick history recorder for tos-dash-v2.

Reads spy_price.json and option_chain.json on each poll cycle and appends
rows to a SQLite replay database. Designed to be managed as a subprocess by
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
import sqlite3
from datetime import datetime, date
from pathlib import Path

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

REPLAY_DB_PATH = Path(cfg.get("replay_db_path", "D:/tos-dash-v2-replay/"))
POLL_MS        = cfg.get("poll_ms", 500)
POLL_SEC       = POLL_MS / 1000.0

REPLAY_DB_PATH.mkdir(parents=True, exist_ok=True)

# One replay DB per calendar day
def _db_path_for_today() -> Path:
    return REPLAY_DB_PATH / f"ticks_{date.today().isoformat()}.db"

def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), check_same_thread=False)
    return conn

def _ensure_tables(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS spy_ticks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tick_time TEXT NOT NULL,
            tick INTEGER,
            last REAL,
            bid REAL,
            ask REAL,
            mark REAL,
            volume REAL,
            vix REAL,
            ntick INTEGER,
            es_last REAL,
            spx_last REAL,
            rtd_stale INTEGER,
            frozen_ticks INTEGER,
            trin REAL,
            add_val REAL,
            qqq_last REAL,
            iwm_last REAL,
            nq_last REAL
        )
    """)
    # Migration guard — add new breadth/ETF columns to existing DBs
    existing = {row[1] for row in conn.execute("PRAGMA table_info(spy_ticks)")}
    for col, typedef in [
        ("trin",     "REAL"),
        ("add_val",  "REAL"),
        ("qqq_last", "REAL"),
        ("iwm_last", "REAL"),
        ("nq_last",  "REAL"),
    ]:
        if col not in existing:
            conn.execute(f"ALTER TABLE spy_ticks ADD COLUMN {col} {typedef}")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chain_ticks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tick_time TEXT NOT NULL,
            tick INTEGER,
            symbol TEXT NOT NULL,
            last REAL,
            bid REAL,
            ask REAL,
            mark REAL,
            delta REAL,
            gamma REAL,
            theta REAL,
            iv REAL,
            volume REAL,
            open_int REAL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_spy_time  ON spy_ticks(tick_time)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chain_sym ON chain_ticks(symbol)")
    conn.commit()

PRICE_FILE = THIS_DIR / "spy_price.json"
CHAIN_FILE = THIS_DIR / "option_chain.json"

# ── Main loop ──────────────────────────────────────────────────────────────────
print(f"[tick_recorder] Starting — replay_db={REPLAY_DB_PATH}  poll={POLL_MS}ms", file=sys.stderr)

current_db_date = date.today()
db_conn = _connect(_db_path_for_today())
_ensure_tables(db_conn)

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

        # Day rollover — open new DB
        today = date.today()
        if today != current_db_date:
            db_conn.close()
            current_db_date = today
            db_conn = _connect(_db_path_for_today())
            _ensure_tables(db_conn)
            last_tick = -1

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

        tick_time = datetime.now().isoformat(timespec="milliseconds")

        # Write spy row
        db_conn.execute("""
            INSERT INTO spy_ticks
            (tick_time, tick, last, bid, ask, mark, volume, vix, ntick, es_last, spx_last, rtd_stale, frozen_ticks,
             trin, add_val, qqq_last, iwm_last, nq_last)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            tick_time, tick,
            price_data.get("last"), price_data.get("bid"),
            price_data.get("ask"),  price_data.get("mark"),
            price_data.get("volume"),
            price_data.get("vix_last"), price_data.get("ntick_val"),
            price_data.get("es_last"),  price_data.get("spx_last"),
            1 if price_data.get("rtd_stale") else 0,
            price_data.get("frozen_ticks", 0),
            price_data.get("trin_val"),  price_data.get("add_val"),
            price_data.get("qqq_last"),  price_data.get("iwm_last"),
            price_data.get("nq_last"),
        ))

        # Write chain rows (only symbols with actual data)
        chain = chain_data.get("chain", {})
        rows = []
        for sym, fields in chain.items():
            mark = fields.get("MARK")
            if mark is None:
                continue
            rows.append((
                tick_time, tick, sym,
                fields.get("LAST"), fields.get("BID"), fields.get("ASK"), mark,
                fields.get("DELTA"), fields.get("GAMMA"), fields.get("THETA"),
                fields.get("IMPL_VOL"), fields.get("VOLUME"), fields.get("OPEN_INT"),
            ))

        if rows:
            db_conn.executemany("""
                INSERT INTO chain_ticks
                (tick_time, tick, symbol, last, bid, ask, mark, delta, gamma, theta, iv, volume, open_int)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, rows)

        db_conn.commit()

        time.sleep(max(0.0, POLL_SEC - (time.perf_counter() - t0)))

except KeyboardInterrupt:
    pass
finally:
    db_conn.close()
    PID_FILE.unlink(missing_ok=True)
    print("[tick_recorder] Stopped.", file=sys.stderr)
