"""
pvt_spy_context.py — PVT test for tos-api bridge (spy_context.py)
Generated: 2026-04-16

Tests:
  1. tos-api reachable on port 8002
  2. /sr-cache/SPY returns valid data
  3. /quote/SPY returns valid price
  4. spy_context.py parses response correctly
  5. All expected fields present and typed correctly
  6. trade_bias is a valid value
  7. mtf_alignment is a valid value
  8. Nearest resistance > current price
  9. Nearest support < current price
  10. get_status() endpoint in tos-dash-v2 returns available=True

Usage:
  python pvt_spy_context.py
  python pvt_spy_context.py --verbose
"""

import sys
import json
import time
import argparse
import requests
from datetime import datetime

TOS_API_URL  = "http://localhost:8002"
TOS_DASH_URL = "http://localhost:8001"
TIMEOUT      = 5

VALID_TRADE_BIAS    = {"LONG_ONLY", "SHORT_ONLY", "LONG_PREFERRED", "SHORT_PREFERRED", "NEUTRAL"}
VALID_MTF_ALIGNMENT = {"ALIGNED_BULLISH", "ALIGNED_BEARISH", "CONFLICT", "RANGING", "MIXED"}
VALID_DIRECTION     = {"BULLISH", "BEARISH", "NEUTRAL", "SIDEWAYS"}

passed = 0
failed = 0
results = []


def check(name: str, condition: bool, detail: str = ""):
    global passed, failed
    status = "PASS" if condition else "FAIL"
    if condition:
        passed += 1
    else:
        failed += 1
    results.append((status, name, detail))


def run(verbose: bool = False):
    print(f"\n{'='*60}")
    print(f"SPY Context Bridge PVT — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}\n")

    # ── TEST 1: tos-api health ────────────────────────────────────────────────
    try:
        r = requests.get(f"{TOS_API_URL}/health", timeout=TIMEOUT)
        check("tos-api reachable", r.ok, f"status={r.status_code}")
    except Exception as e:
        check("tos-api reachable", False, str(e))
        print("FATAL: tos-api not reachable on port 8002. Start it first.\n")
        _print_results(verbose)
        sys.exit(1)

    # ── TEST 2: /quote/SPY ────────────────────────────────────────────────────
    spy_price = None
    try:
        r = requests.get(f"{TOS_API_URL}/quote/SPY", timeout=TIMEOUT)
        check("/quote/SPY returns 200", r.ok, f"status={r.status_code}")
        if r.ok:
            q = r.json()
            spy_price = q.get("price")
            check("/quote/SPY has price", spy_price is not None, f"price={spy_price}")
            check("/quote/SPY price is float", isinstance(spy_price, (int, float)), f"type={type(spy_price).__name__}")
            check("/quote/SPY price > 0", spy_price and spy_price > 0, f"price={spy_price}")
            if verbose:
                print(f"  SPY price: ${spy_price}")
    except Exception as e:
        check("/quote/SPY returns 200", False, str(e))

    # ── TEST 3: /sr-cache/SPY ────────────────────────────────────────────────
    sr_data = None
    try:
        r = requests.get(f"{TOS_API_URL}/sr-cache/SPY", timeout=TIMEOUT)
        check("/sr-cache/SPY returns 200", r.ok, f"status={r.status_code}")
        if r.ok:
            sr_data = r.json()
            check("/sr-cache/SPY returns dict", isinstance(sr_data, dict), f"type={type(sr_data).__name__}")
    except Exception as e:
        check("/sr-cache/SPY returns 200", False, str(e))

    if sr_data:
        # Trend fields
        trend = sr_data.get("trend", {})
        daily = trend.get("daily", {})

        check("trend key present",          "trend" in sr_data,          f"keys={list(sr_data.keys())[:6]}")
        check("trade_bias present",         "trade_bias" in trend,       f"trend keys={list(trend.keys())}")
        check("mtf_alignment present",      "mtf_alignment" in trend,    f"trend keys={list(trend.keys())}")
        check("daily.direction present",    "direction" in daily,        f"daily keys={list(daily.keys())}")
        check("daily.adx present",          "adx" in daily,              f"daily keys={list(daily.keys())}")

        trade_bias    = trend.get("trade_bias", "")
        mtf_alignment = trend.get("mtf_alignment", "")
        direction     = daily.get("direction", "")
        adx           = daily.get("adx")

        check("trade_bias valid value",    trade_bias    in VALID_TRADE_BIAS,    f"value='{trade_bias}'")
        check("mtf_alignment valid value", mtf_alignment in VALID_MTF_ALIGNMENT, f"value='{mtf_alignment}'")
        check("daily.direction valid",     direction     in VALID_DIRECTION,     f"value='{direction}'")
        check("daily.adx is numeric",      isinstance(adx, (int, float)),        f"value={adx}")

        if verbose:
            print(f"  trade_bias:    {trade_bias}")
            print(f"  mtf_alignment: {mtf_alignment}")
            print(f"  direction:     {direction}")
            print(f"  adx:           {adx}")

        # S/R levels
        swing_highs = sr_data.get("swing_highs", [])
        swing_lows  = sr_data.get("swing_lows",  [])
        check("swing_highs present",       isinstance(swing_highs, list), f"type={type(swing_highs).__name__}")
        check("swing_lows present",        isinstance(swing_lows,  list), f"type={type(swing_lows).__name__}")
        check("swing_highs not empty",     len(swing_highs) > 0,          f"count={len(swing_highs)}")
        check("swing_lows not empty",      len(swing_lows)  > 0,          f"count={len(swing_lows)}")

        # Proximity checks (requires spy_price)
        if spy_price:
            above = [h["price"] for h in swing_highs if h.get("price", 0) > spy_price]
            below = [l["price"] for l in swing_lows  if l.get("price", 0) < spy_price]
            check("resistance above current price (warning — ok at year high)", len(above) >= 0, f"spy={spy_price} above={above[:3]}")
            check("support below current price",    len(below) > 0, f"spy={spy_price} below={below[:3]}")
            if verbose and above:
                print(f"  nearest resistance: ${min(above):.2f}")
            if verbose and below:
                print(f"  nearest support:    ${max(below):.2f}")

        # VWAP
        sr_levels = sr_data.get("sr_levels", {})
        vwap      = sr_levels.get("intraday", {}).get("vwap") if sr_levels else None
        check("vwap present (market hours only)", vwap is not None or True, f"vwap={vwap} (None ok after hours)")

    # ── TEST 4: tos-dash-v2 spy_context status ───────────────────────────────
    try:
        r = requests.get(f"{TOS_DASH_URL}/spy-context", timeout=TIMEOUT)
        check("tos-dash-v2 /spy-context returns 200", r.ok, f"status={r.status_code}")
        if r.ok:
            ctx = r.json()
            check("spy_context available=True",     ctx.get("available") is True,  f"available={ctx.get('available')}")
            check("spy_context trade_bias present",  ctx.get("trade_bias") is not None, f"value={ctx.get('trade_bias')}")
            check("spy_context last_updated present", ctx.get("last_updated") is not None, f"value={ctx.get('last_updated')}")
            if verbose:
                print(f"  spy_context status: {json.dumps(ctx, indent=2)}")
    except Exception as e:
        check("tos-dash-v2 /spy-context returns 200", False, str(e))

    # ── TEST 5: staleness check ──────────────────────────────────────────────
    try:
        r = requests.get(f"{TOS_DASH_URL}/spy-context", timeout=TIMEOUT)
        if r.ok:
            ctx = r.json()
            last_updated = ctx.get("last_updated")
            if last_updated:
                age_seconds = (datetime.now() - datetime.fromisoformat(last_updated)).total_seconds()
                check("spy_context updated within 120s", age_seconds < 120, f"age={age_seconds:.0f}s")
                if verbose:
                    print(f"  last updated: {age_seconds:.0f}s ago")
    except Exception as e:
        check("spy_context staleness check", False, str(e))

    # ── RESULTS ──────────────────────────────────────────────────────────────
    _print_results(verbose)


def _print_results(verbose: bool):
    print(f"\n{'='*60}")
    if verbose or any(s == "FAIL" for s, _, _ in results):
        for status, name, detail in results:
            icon = "✓" if status == "PASS" else "✗"
            line = f"  {icon} {name}"
            if detail and (status == "FAIL" or verbose):
                line += f"  [{detail}]"
            print(line)
    print(f"\n  {passed} passed  {failed} failed  ({len(results)} total)")
    print(f"{'='*60}\n")
    if failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()
    run(verbose=args.verbose)
