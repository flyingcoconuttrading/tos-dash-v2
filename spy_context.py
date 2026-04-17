"""
spy_context.py
--------------
Polls tos-api GET /sr-cache/SPY every 60 seconds.
Caches result in memory. Exposes get_spy_context() for use in scalp_advisor.py.
Fails silently — returns available=False if tos-api is unreachable.
Version: v2.52.0
"""

import threading
import requests
from datetime import datetime

TOS_API_URL   = "http://localhost:8002"
POLL_INTERVAL = 60   # seconds
REQUEST_TIMEOUT = 5  # seconds

_cache: dict = {"available": False}
_lock         = threading.Lock()
_thread: threading.Thread | None = None


def _fetch() -> dict:
    """Fetch and parse /sr-cache/SPY from tos-api."""
    try:
        resp = requests.get(
            f"{TOS_API_URL}/sr-cache/SPY",
            timeout=REQUEST_TIMEOUT,
        )
        if not resp.ok:
            return {"available": False}

        data  = resp.json()
        trend = data.get("trend", {})
        daily = trend.get("daily", {})
        sr    = data.get("swing_highs", [])
        sl    = data.get("swing_lows",  [])

        # Get current SPY price for proximity calc
        quote_resp = requests.get(
            f"{TOS_API_URL}/quote/SPY", timeout=REQUEST_TIMEOUT
        )
        current_price = None
        if quote_resp.ok:
            current_price = quote_resp.json().get("price")

        # Nearest resistance (lowest swing_high above current price)
        nearest_resistance = None
        if current_price and sr:
            above = [h["price"] for h in sr if h["price"] > current_price]
            nearest_resistance = min(above) if above else None

        # Nearest support (highest swing_low below current price)
        nearest_support = None
        if current_price and sl:
            below = [l["price"] for l in sl if l["price"] < current_price]
            nearest_support = max(below) if below else None

        # VWAP from sr_levels intraday
        sr_levels = data.get("sr_levels", {})
        vwap      = sr_levels.get("intraday", {}).get("vwap")

        return {
            "available":          True,
            "trade_bias":         trend.get("trade_bias",    "NEUTRAL"),
            "mtf_alignment":      trend.get("mtf_alignment", "RANGING"),
            "daily_direction":    daily.get("direction",     "SIDEWAYS"),
            "daily_adx":          daily.get("adx",           25.0),
            "daily_strength":     daily.get("strength",      "WEAK"),
            "daily_momentum":     daily.get("momentum",      "STEADY"),
            "weekly_direction":   trend.get("weekly", {}).get("direction", "SIDEWAYS"),
            "bias_reason":        trend.get("bias_reason",   ""),
            "nearest_resistance": nearest_resistance,
            "nearest_support":    nearest_support,
            "vwap":               vwap,
            "spy_price":          current_price,
            "last_updated":       datetime.now().isoformat(),
        }

    except Exception as e:
        print(f"[SPYContext] fetch error: {e}")
        return {"available": False}


def _poll_loop():
    """Background thread — polls tos-api every POLL_INTERVAL seconds."""
    global _cache
    while True:
        result = _fetch()
        with _lock:
            _cache = result
        threading.Event().wait(POLL_INTERVAL)


def start():
    """Start background polling thread. Safe to call multiple times."""
    global _thread
    if _thread is None or not _thread.is_alive():
        _thread = threading.Thread(target=_poll_loop, daemon=True, name="SPYContextPoller")
        _thread.start()
        print(f"[SPYContext] polling tos-api every {POLL_INTERVAL}s")


def get_spy_context() -> dict:
    """
    Return cached SPY context. Always returns a dict.
    Check result["available"] before using values.
    """
    with _lock:
        return dict(_cache)


def get_status() -> dict:
    ctx = get_spy_context()
    return {
        "available":     ctx.get("available", False),
        "trade_bias":    ctx.get("trade_bias"),
        "mtf_alignment": ctx.get("mtf_alignment"),
        "daily_adx":     ctx.get("daily_adx"),
        "last_updated":  ctx.get("last_updated"),
    }
