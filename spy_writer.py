"""
spy_writer.py — RTD data writer for tos-dash-v2.
Reads config.json for all settings.
Managed as a subprocess by api.py — do not run directly in production.

Writes every tick:
  spy_price.json    — SPY price + bid/ask/mark
  option_chain.json — full option chain greeks
"""

import json
import sys
import time
import pythoncom
from datetime import date, datetime, timedelta
from pathlib import Path

# ── Path setup ────────────────────────────────────────────────────────────────
THIS_DIR = Path(__file__).parent

sys.path.insert(0, str(THIS_DIR))

from rtd.client import RTDClient
from rtd.quote_types import QuoteType
from rtd.option_symbol_builder import OptionSymbolBuilder

# ── Load config ───────────────────────────────────────────────────────────────
def load_config() -> dict:
    try:
        return json.loads((THIS_DIR / "config.json").read_text())
    except Exception as e:
        print(f"[writer] Config load error: {e}", file=sys.stderr)
        return {}

cfg = load_config()

SYMBOL_RAW    = cfg.get("symbol", "SPY")
# RTD underlying uses the symbol exactly as entered (e.g. /ES, /CL, SPY)
# Option symbol builder needs the slash stripped (ES, CL, SPY)
SYMBOL        = SYMBOL_RAW                                    # used for RTD subscriptions
OPTION_BASE   = SYMBOL_RAW.lstrip("/")                       # used for option chain e.g. .ES250321C5800
IS_FUTURES    = SYMBOL_RAW.startswith("/")
STRIKE_RANGE  = cfg.get("strike_range", 10)
WALL_RANGE    = cfg.get("wall_range", 25)   # wider range for wall/max_pain accuracy
_SUB_RANGE    = max(STRIKE_RANGE, WALL_RANGE)  # subscribe to the larger
STRIKE_SPACING= cfg.get("strike_spacing", 1.0)
POLL_MS       = cfg.get("poll_ms", 500)
TEST_MODE     = cfg.get("test_mode", False)
TEST_DATE     = cfg.get("test_date", None)

ES_SYMBOL     = "/ES:XCME"
SPX_SYMBOL    = "$SPX"
VIX_SYMBOL    = "$VIX.X"
NTICK_SYMBOL  = "$TICK"
COMPANION_QTS = [QuoteType.LAST]   # only need LAST for ratio tracking

OPTION_QTS = [
    QuoteType.LAST, QuoteType.BID, QuoteType.ASK, QuoteType.MARK,
    QuoteType.VOLUME, QuoteType.OPEN_INT,
    QuoteType.DELTA, QuoteType.GAMMA, QuoteType.THETA, QuoteType.IMPL_VOL,
    QuoteType.POSITION_QTY, QuoteType.AV_TRADE_PRICE,
]

SPY_QTS = [
    QuoteType.LAST, QuoteType.MARK, QuoteType.BID, QuoteType.ASK,
    QuoteType.VOLUME, QuoteType.BID_SIZE, QuoteType.ASK_SIZE,
]

print(f"[writer] Config: symbol={SYMBOL} (option_base={OPTION_BASE} futures={IS_FUTURES}) range=±{STRIKE_RANGE} spacing={STRIKE_SPACING} poll={POLL_MS}ms test={TEST_MODE}", file=sys.stderr)

# ── Helpers ───────────────────────────────────────────────────────────────────
def next_friday(from_date=None):
    d = from_date or date.today()
    days_ahead = (4 - d.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    return d + timedelta(days=days_ahead)


def third_friday(year: int, month: int) -> date:
    """Return the third Friday of the given month — standard futures/index option expiry."""
    first = date(year, month, 1)
    first_friday = first + timedelta(days=(4 - first.weekday()) % 7)
    return first_friday + timedelta(weeks=2)


def next_futures_expiry(from_date=None) -> date:
    """
    Returns the next quarterly futures options expiry (3rd Friday of Mar/Jun/Sep/Dec).
    ES, NQ, CL, GC, etc. all use quarterly expirations.
    """
    d = from_date or date.today()
    quarterly_months = [3, 6, 9, 12]
    for year in [d.year, d.year + 1]:
        for month in quarterly_months:
            exp = third_friday(year, month)
            if exp >= d:
                return exp
    return third_friday(d.year + 1, 3)  # fallback


def get_expiry() -> date:
    # Test mode: use override date if set
    if TEST_MODE and TEST_DATE:
        try:
            return date.fromisoformat(TEST_DATE)
        except Exception:
            pass
    # Use configured expiry if set (always takes priority)
    exp = cfg.get("expiry_date")
    if exp:
        try:
            return date.fromisoformat(exp)
        except Exception:
            pass
    # Futures: next quarterly expiry (3rd Friday of Mar/Jun/Sep/Dec)
    if IS_FUTURES:
        return next_futures_expiry()
    # Equities: today (0DTE)
    return date.today()


def safe_float(val):
    try:
        return float(val) if val is not None else None
    except (ValueError, TypeError):
        return None


# ── Initialize COM + RTDClient ────────────────────────────────────────────────
print(f"[writer] Starting RTDClient for {SYMBOL}...", file=sys.stderr)
pythoncom.CoInitialize()
client = RTDClient(heartbeat_ms=cfg.get('rtd_heartbeat_ms', 200))
client.initialize()
print("[writer] RTDClient initialized", file=sys.stderr)

# ── Subscribe to underlying ───────────────────────────────────────────────────
for qt in SPY_QTS:
    client.subscribe(qt, SYMBOL)
print(f"[writer] Subscribed to {SYMBOL} underlying", file=sys.stderr)

# ── Subscribe to ES and SPX for ratio converter ──────────────────────────────
for sym in (ES_SYMBOL, SPX_SYMBOL):
    for qt in COMPANION_QTS:
        try:
            client.subscribe(qt, sym)
        except Exception as e:
            print(f"[writer] Warning: could not subscribe {sym}: {e}",
                  file=sys.stderr)
print(f"[writer] Subscribed to {ES_SYMBOL} and {SPX_SYMBOL} (LAST only)",
      file=sys.stderr)

# ── Subscribe to VIX and NYSE TICK ───────────────────────────────────────────
for sym in (VIX_SYMBOL, NTICK_SYMBOL):
    for qt in COMPANION_QTS:
        try:
            client.subscribe(qt, sym)
        except Exception as e:
            print(f"[writer] Warning: could not subscribe {sym}: {e}",
                  file=sys.stderr)
print(f"[writer] Subscribed to {VIX_SYMBOL} and {NTICK_SYMBOL} (LAST only)",
      file=sys.stderr)

# ── Wait for initial price ────────────────────────────────────────────────────
initial_price = None
for _ in range(30):
    pythoncom.PumpWaitingMessages()
    with client._value_lock:
        q = client._latest_values.get((SYMBOL, "LAST"))
    if q and q.value is not None:
        initial_price = safe_float(q.value)
        if initial_price:
            break
    time.sleep(0.5)

if not initial_price:
    print("[writer] ERROR: No price received. Is TOS/OnDemand running?", file=sys.stderr)
    sys.exit(1)

print(f"[writer] {SYMBOL} price: ${initial_price:.2f}  option_base={OPTION_BASE}", file=sys.stderr)

# ── Build option chain ────────────────────────────────────────────────────────
expiry         = get_expiry()
option_symbols = OptionSymbolBuilder.build_symbols(
    base_symbol    = OPTION_BASE,   # strip slash for option symbol format e.g. .ES250321C5800
    expiry         = expiry,
    current_price  = initial_price,
    strike_range   = _SUB_RANGE,
    strike_spacing = STRIKE_SPACING,
)
expiry_type = "quarterly" if IS_FUTURES else "0DTE"
print(f"[writer] Expiry: {expiry} ({expiry_type}) | {len(option_symbols)} option symbols", file=sys.stderr)

# ── Subscribe to all option greeks ────────────────────────────────────────────
subscribed = 0
for sym in option_symbols:
    for qt in OPTION_QTS:
        try:
            if client.subscribe(qt, sym) is not None:
                subscribed += 1
        except Exception:
            pass
print(f"[writer] Subscribed to {subscribed} option fields — poll loop starting", file=sys.stderr)
time.sleep(0.3)

# ── Write loop ────────────────────────────────────────────────────────────────
tick     = 0
poll_sec = POLL_MS / 1000.0

while True:
    t0 = time.perf_counter()
    pythoncom.PumpWaitingMessages()

    with client._value_lock:
        raw = dict(client._latest_values)

    if raw:
        spy_last     = safe_float(getattr(raw.get((SYMBOL, "LAST")),     "value", None))
        spy_bid      = safe_float(getattr(raw.get((SYMBOL, "BID")),      "value", None))
        spy_ask      = safe_float(getattr(raw.get((SYMBOL, "ASK")),      "value", None))
        spy_mark     = safe_float(getattr(raw.get((SYMBOL, "MARK")),     "value", None))
        spy_volume   = safe_float(getattr(raw.get((SYMBOL, "VOLUME")),   "value", None))
        spy_bid_size = safe_float(getattr(raw.get((SYMBOL, "BID_SIZE")), "value", None))
        spy_ask_size = safe_float(getattr(raw.get((SYMBOL, "ASK_SIZE")), "value", None))
        es_last   = safe_float(getattr(raw.get((ES_SYMBOL,    "LAST")), "value", None))
        spx_last  = safe_float(getattr(raw.get((SPX_SYMBOL,  "LAST")), "value", None))
        vix_last  = safe_float(getattr(raw.get((VIX_SYMBOL,  "LAST")), "value", None))
        ntick_val = safe_float(getattr(raw.get((NTICK_SYMBOL,"LAST")), "value", None))

        price_payload = {
            "symbol":    SYMBOL,
            "last":      spy_last,
            "bid":       spy_bid,
            "ask":       spy_ask,
            "mark":      spy_mark,
            "volume":    spy_volume,
            "bid_size":  spy_bid_size,
            "ask_size":  spy_ask_size,
            "tick":      tick,
            "timestamp": datetime.now().isoformat(timespec="milliseconds"),
            "expiry":    expiry.strftime("%Y-%m-%d"),
            "test_mode": TEST_MODE,
            "es_last":   es_last,
            "spx_last":  spx_last,
            "vix_last":  vix_last,
            "ntick_val": ntick_val,
        }
        (THIS_DIR / "spy_price.json").write_text(json.dumps(price_payload, indent=2))

        chain = {}
        positions = {}
        for sym in option_symbols:
            entry = {}
            for qt in OPTION_QTS:
                quote_obj = raw.get((sym, qt.value))
                val = safe_float(getattr(quote_obj, "value", None)) if quote_obj else None
                entry[qt.value] = val
            chain[sym] = entry
            # Extract position data separately for easy access
            qty   = entry.get("POSITION_QTY")
            price = entry.get("AV_TRADE_PRICE")
            if qty and qty != 0:
                positions[sym] = {
                    "qty":             qty,
                    "av_trade_price":  price,
                    "mark":            entry.get("MARK"),
                    "last":            entry.get("LAST"),
                    "delta":           entry.get("DELTA"),
                    "iv":              entry.get("IMPL_VOL"),
                }

        chain_payload = {
            "symbol":        SYMBOL,
            "tick":          tick,
            "timestamp":     datetime.now().isoformat(timespec="milliseconds"),
            "expiry":        expiry.strftime("%Y-%m-%d"),
            "strike_range":  STRIKE_RANGE,
            "strike_spacing":STRIKE_SPACING,
            "option_count":  len(option_symbols),
            "chain":         chain,
        }
        (THIS_DIR / "option_chain.json").write_text(json.dumps(chain_payload, indent=2))

        # Write positions separately for fast access
        (THIS_DIR / "positions.json").write_text(json.dumps({
            "tick":      tick,
            "timestamp": datetime.now().isoformat(timespec="milliseconds"),
            "positions": positions,
        }, indent=2))

        if tick % 60 == 0:
            live = sum(1 for e in chain.values() for v in e.values() if v is not None)
            print(f"[writer] tick={tick} {SYMBOL}=${spy_last} live={live}", file=sys.stderr)

        tick += 1

    time.sleep(max(0.0, poll_sec - (time.perf_counter() - t0)))
