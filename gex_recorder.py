# tos-dash-v2/gex_recorder.py
"""
GEX Recorder — captures SPY (and future tickers) GEX/DEX snapshots every 60s
during market hours for multi-day baseline analysis.
Version: v2.55.0

Writes to ideas.duckdb table `gex_snapshots` via api.py /backtest/query proxy
to avoid DuckDB file-lock conflicts. Pulls snapshot data from api.py /snapshot.

Control files (same pattern as tick_recorder.py):
  gex_recorder.stop  — graceful shutdown sentinel
  gex_recorder.pause — pause writes (sleeps, keeps process alive)

Market hours: 09:30–16:00 ET. Outside market hours the process idles
with 60s sleeps but does not write.
"""

import json
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

THIS_DIR  = Path(__file__).parent
API_BASE  = "http://127.0.0.1:8001"
STOP_FILE  = THIS_DIR / "gex_recorder.stop"
PAUSE_FILE = THIS_DIR / "gex_recorder.pause"
INTERVAL_SEC = 60

ET = ZoneInfo("America/New_York")


def _in_market_hours(now_et: datetime) -> bool:
    if now_et.weekday() >= 5:
        return False
    mins = now_et.hour * 60 + now_et.minute
    return 9 * 60 + 30 <= mins <= 16 * 60


def _fetch_snapshot() -> dict | None:
    try:
        r = httpx.get(f"{API_BASE}/snapshot", timeout=5.0)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


def _write_snapshot(row: dict) -> bool:
    """Insert via api.py /backtest/write endpoint (parameterized)."""
    try:
        r = httpx.post(f"{API_BASE}/backtest/gex-snapshot-write",
                       json=row, timeout=5.0)
        return r.status_code == 200 and r.json().get("saved") is True
    except Exception:
        return False


def _minute_of_day(now_et: datetime) -> int:
    return now_et.hour * 60 + now_et.minute


def run():
    print(f"[gex_recorder] started pid={__import__('os').getpid()}", flush=True)
    while True:
        if STOP_FILE.exists():
            print("[gex_recorder] stop file present — exiting", flush=True)
            return
        if PAUSE_FILE.exists():
            time.sleep(2)
            continue

        now_et = datetime.now(ET)
        if not _in_market_hours(now_et):
            time.sleep(INTERVAL_SEC)
            continue

        snap = _fetch_snapshot()
        if not snap:
            time.sleep(INTERVAL_SEC)
            continue

        ms = snap.get("market_structure") or {}
        row = {
            "recorded_at":    now_et.replace(tzinfo=None).isoformat(timespec="seconds"),
            "date":           now_et.date().isoformat(),
            "minute_of_day":  _minute_of_day(now_et),
            "symbol":         "SPY",
            "spy_price":      float(snap.get("spy_price") or 0.0),
            "vix":            float(snap.get("vix") or 0.0),
            "net_gex":        float(ms.get("net_gex") or 0.0),
            "net_dex":        float(ms.get("net_dex") or 0.0),
            "gex_anchor":     float(ms.get("gex_anchor") or 0.0),
            "max_pain":       float(ms.get("max_pain") or 0.0),
            "call_wall":      float(ms.get("call_wall") or 0.0),
            "put_wall":       float(ms.get("put_wall") or 0.0),
            "regime":         ms.get("regime") or "",
            "trend":          ms.get("trend") or "",
        }
        ok = _write_snapshot(row)
        if not ok:
            print(f"[gex_recorder] write failed at {row['recorded_at']}", flush=True)

        time.sleep(INTERVAL_SEC)


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        sys.exit(0)
