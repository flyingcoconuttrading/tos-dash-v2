"""
testing/test_rtd_schwab.py
Compares RTD snapshot data (spy_price.json written by spy_writer.py) against
live Schwab API data (via tos-api data/collector.py). Runs every 5 minutes.

Run from project root:
    python testing/test_rtd_schwab.py --tos-api-path ../tos-api

Call budget note:
    _get_quote() has a 15-second TTL cache in tos-api.
    At 5-minute intervals this script issues ~72 quote calls per 6-hour session.
    _get_intraday() has a 60-second cache; same budget applies.
    Both are within acceptable limits for the Schwab API tier.
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────────────────
THIS_DIR    = Path(__file__).parent
ROOT_DIR    = THIS_DIR.parent
LOG_PATH    = THIS_DIR / "test_rtd_compare.log"

# RTD snapshot is written by spy_writer.py to the project root
RTD_PRICE_FILE = ROOT_DIR / "spy_price.json"
RTD_STALE_SEC  = 10    # snapshot older than this = stale

COMPARE_SEC    = 5 * 60   # 5 minutes between comparisons
PRICE_TOL      = 0.10
BIDASK_TOL     = 0.05


# ── Logging ───────────────────────────────────────────────────────────────────

def _log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


# ── RTD helpers ───────────────────────────────────────────────────────────────

def read_rtd_snapshot() -> dict | None:
    """Read spy_price.json written by spy_writer.py. Returns None if stale."""
    if not RTD_PRICE_FILE.exists():
        _log("RTD snapshot not found — is spy_writer.py running?")
        return None
    age = time.time() - RTD_PRICE_FILE.stat().st_mtime
    if age > RTD_STALE_SEC:
        _log(f"RTD snapshot stale ({age:.0f}s old, limit={RTD_STALE_SEC}s) — skipping interval")
        return None
    try:
        return json.loads(RTD_PRICE_FILE.read_text())
    except Exception as e:
        _log(f"RTD snapshot read error: {e}")
        return None


# ── Schwab helpers ────────────────────────────────────────────────────────────

def load_schwab(tos_api_path: str):
    """
    Import _get_quote and _get_intraday from tos-api.
    Returns (get_quote_fn, get_intraday_fn) or (None, None) on failure.
    """
    api_path = Path(tos_api_path).resolve()
    if not api_path.exists():
        print(f"ERROR: tos-api path not found: {api_path}")
        print(f"  Expected: {api_path}")
        print("  Run with: --tos-api-path ../tos-api")
        sys.exit(1)

    sys.path.insert(0, str(api_path))
    try:
        from data.collector import _get_quote, _get_intraday
        return _get_quote, _get_intraday
    except ImportError as e:
        _log(f"Schwab import failed: {e}")
        _log(f"  Looked in: {api_path}/data/collector.py")
        return None, None
    except Exception as e:
        _log(f"Schwab import error: {e}")
        return None, None


# ── Comparison ────────────────────────────────────────────────────────────────

def compare(get_quote, get_intraday):
    # Read RTD snapshot
    rtd = read_rtd_snapshot()
    if rtd is None:
        return

    sym = rtd.get("symbol", "SPY")

    # Read Schwab quote
    try:
        sch_quote = get_quote(sym)
    except Exception as e:
        _log(f"Schwab _get_quote error: {e}")
        sch_quote = {}

    # ── PRICE ──────────────────────────────────────────────────────────────
    rtd_price = rtd.get("last")
    sch_price = sch_quote.get("last") if sch_quote else None
    if rtd_price is not None and sch_price is not None:
        diff = abs(rtd_price - sch_price)
        status = "PASS" if diff <= PRICE_TOL else "FAIL"
        flag = " <-- divergence" if status == "FAIL" else ""
        _log(f"PRICE    RTD={rtd_price:.2f} SCH={sch_price:.2f} diff=${diff:.2f} {status}{flag}")
    else:
        _log(f"PRICE    RTD={rtd_price} SCH={sch_price} (one side unavailable)")

    # ── BID ─────────────────────────────────────────────────────────────────
    rtd_bid = rtd.get("bid")
    sch_bid = sch_quote.get("bid") if sch_quote else None
    if rtd_bid is not None and sch_bid is not None:
        diff = abs(rtd_bid - sch_bid)
        status = "PASS" if diff <= BIDASK_TOL else "FAIL"
        _log(f"BID      RTD={rtd_bid:.2f} SCH={sch_bid:.2f} diff=${diff:.2f} {status}")
    else:
        _log(f"BID      RTD={rtd_bid} SCH={sch_bid} (one side unavailable)")

    # ── ASK ─────────────────────────────────────────────────────────────────
    rtd_ask = rtd.get("ask")
    sch_ask = sch_quote.get("ask") if sch_quote else None
    if rtd_ask is not None and sch_ask is not None:
        diff = abs(rtd_ask - sch_ask)
        status = "PASS" if diff <= BIDASK_TOL else "FAIL"
        _log(f"ASK      RTD={rtd_ask:.2f} SCH={sch_ask:.2f} diff=${diff:.2f} {status}")
    else:
        _log(f"ASK      RTD={rtd_ask} SCH={sch_ask} (one side unavailable)")

    # ── DAY HIGH/LOW ─────────────────────────────────────────────────────────
    # RTD day high/low is tracked internally in scalp_advisor._day_high/_day_low
    # and is not written to spy_price.json snapshot. Log this gap.
    _log("DAY_RANGE  RTD day high/low not in snapshot — gap noted for future VWAP integration")

    # Schwab intraday for day range + VWAP
    try:
        intraday = get_intraday(sym)
        if intraday:
            bars = intraday.get("candles") or intraday.get("bars") or []
            if bars:
                day_high = max(b.get("high", 0) for b in bars if b.get("high"))
                day_low  = min(b.get("low", float("inf")) for b in bars if b.get("low"))
                _log(f"DAY_RANGE  SCH day_high={day_high:.2f} day_low={day_low:.2f} (informational)")

            # VWAP — Schwab intraday provides this; RTD does not
            vwap = intraday.get("vwap")
            if vwap is not None:
                _log(f"VWAP       SCH={vwap:.2f} (informational)")
            else:
                # Compute VWAP from bars if available
                if bars:
                    tp_vol = sum(
                        ((b.get("high", 0) + b.get("low", 0) + b.get("close", 0)) / 3) * b.get("volume", 0)
                        for b in bars
                    )
                    total_vol = sum(b.get("volume", 0) for b in bars)
                    if total_vol > 0:
                        vwap_calc = tp_vol / total_vol
                        _log(f"VWAP       SCH={vwap_calc:.2f} (computed from bars, informational)")
    except Exception as e:
        _log(f"Schwab _get_intraday error: {e}")


# ── Main loop ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="RTD vs Schwab price comparison")
    parser.add_argument("--tos-api-path", default="../tos-api",
                        help="Path to tos-api directory (default: ../tos-api)")
    args = parser.parse_args()

    get_quote, get_intraday = load_schwab(args.tos_api_path)
    if get_quote is None:
        print("Could not load Schwab collector. Exiting.")
        sys.exit(1)

    print(f"test_rtd_schwab started")
    print(f"  RTD source:    {RTD_PRICE_FILE}")
    print(f"  Schwab source: {args.tos_api_path}/data/collector.py")
    print(f"  Log:           {LOG_PATH}")
    print(f"  Interval:      {COMPARE_SEC//60} minutes\n")
    _log("test_rtd_schwab session started")

    while True:
        try:
            compare(get_quote, get_intraday)
        except Exception as e:
            _log(f"compare() error: {e}")
        time.sleep(COMPARE_SEC)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
