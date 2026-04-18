# tos-dash-v2/tests/pvt_gex_recorder.py
"""
PVT test for DASH-055 gex_recorder + baseline infrastructure.
Run: python tests/pvt_gex_recorder.py
Requires api.py running on port 8001.
"""

import json
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

API = "http://127.0.0.1:8001"
PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = ""):
    global PASS, FAIL
    mark = "✓" if cond else "✗"
    print(f"  {mark} {name}" + (f" — {detail}" if detail else ""))
    if cond:
        PASS += 1
    else:
        FAIL += 1


def test_recorder_status():
    print("\n[1] gex_recorder status endpoint")
    r = httpx.get(f"{API}/gex-recorder/status", timeout=3)
    check("status endpoint 200", r.status_code == 200)
    d = r.json()
    check("has 'running' key", "running" in d)
    check("has 'paused' key",  "paused"  in d)
    check("running is true",   d.get("running") is True,
          detail="set gex_recorder_enabled=true in config.json and restart api.py")


def test_write_endpoint():
    print("\n[2] /backtest/gex-snapshot-write accepts rows")
    now_et = datetime.now(ZoneInfo("America/New_York"))
    row = {
        "recorded_at":   now_et.replace(tzinfo=None).isoformat(timespec="seconds"),
        "date":          now_et.date().isoformat(),
        "minute_of_day": now_et.hour * 60 + now_et.minute,
        "symbol":        "PVT_TEST",
        "spy_price":     500.0,
        "vix":           18.0,
        "net_gex":       1.23e8,
        "net_dex":       -4.56e7,
        "gex_anchor":    500.0,
        "max_pain":      499.0,
        "call_wall":     502.0,
        "put_wall":      498.0,
        "regime":        "PINNED",
        "trend":         "Choppy",
    }
    r = httpx.post(f"{API}/backtest/gex-snapshot-write", json=row, timeout=5)
    check("write returns 200", r.status_code == 200)
    check("saved=True",        r.json().get("saved") is True)


def test_snapshot_persisted():
    print("\n[3] snapshot persisted in gex_snapshots table")
    r = httpx.post(f"{API}/backtest/query",
                   json={"sql": "SELECT COUNT(*) AS n FROM gex_snapshots WHERE symbol='PVT_TEST'"},
                   timeout=5)
    n = (r.json().get("rows") or [{}])[0].get("n", 0)
    check("at least 1 PVT_TEST row present", int(n or 0) >= 1, detail=f"n={n}")


def test_baseline_endpoint():
    print("\n[4] /gex/baseline endpoint")
    r = httpx.get(f"{API}/gex/baseline?symbol=PVT_TEST&minute_of_day=600&window_minutes=1440&days=90",
                  timeout=5)
    check("baseline 200",            r.status_code == 200)
    d = r.json()
    check("has 'baseline' key",      "baseline" in d)
    n = (d.get("baseline") or {}).get("n", 0)
    check("baseline n >= 1",         int(n or 0) >= 1, detail=f"n={n}")


def test_live_snapshot_market_hours():
    print("\n[5] live SPY snapshot capture (market hours only)")
    now_et = datetime.now(ZoneInfo("America/New_York"))
    mins = now_et.hour * 60 + now_et.minute
    if now_et.weekday() >= 5 or not (570 <= mins <= 960):
        print("  — skipped (outside market hours)")
        return
    print("  waiting 70s for gex_recorder cycle...")
    time.sleep(70)
    r = httpx.post(f"{API}/backtest/query",
                   json={"sql":
                         "SELECT COUNT(*) AS n FROM gex_snapshots "
                         "WHERE symbol='SPY' AND recorded_at >= NOW() - INTERVAL 3 MINUTE"},
                   timeout=5)
    n = (r.json().get("rows") or [{}])[0].get("n", 0)
    check("SPY snapshot written in last 3 min", int(n or 0) >= 1, detail=f"n={n}")


def test_cleanup():
    print("\n[6] cleanup PVT_TEST rows")
    r = httpx.post(f"{API}/backtest/query",
                   json={"sql": "DELETE FROM gex_snapshots WHERE symbol='PVT_TEST'"},
                   timeout=5)
    # /backtest/query is SELECT-only — cleanup must be manual if needed
    print("  — cleanup skipped (SELECT-only endpoint); PVT_TEST rows harmless to leave")


if __name__ == "__main__":
    print("=" * 60)
    print("PVT: DASH-055 gex_recorder + baseline infrastructure")
    print("=" * 60)
    test_recorder_status()
    test_write_endpoint()
    test_snapshot_persisted()
    test_baseline_endpoint()
    test_live_snapshot_market_hours()
    test_cleanup()
    print()
    print(f"{PASS} passed, {FAIL} failed")
    sys.exit(0 if FAIL == 0 else 1)
