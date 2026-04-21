# src/ui/market_structure.py
__version__ = "1.2.0"  # Bug 4: max pain checklist weight scaled by time-of-day
"""
Market Structure Analyzer — interprets GEX/DEX data into a human-readable
regime summary with bias, snap level detection, flip zone alerts,
and a 7-factor weighted directional checklist.

Rubber band model:
  GEX  = anchor     — positive GEX pins price, dealers sell rallies/buy dips
  DEX  = elasticity — measures how stretched dealer positioning is
  Snap = the price level where DEX flips dealers from hedging to chasing

Checklist factors (weighted 0–100 score):
  1. Regime Type         25%  — TRENDING/SNAP IMMINENT vs PINNED/TRANSITION
  2. Snap Direction      20%  — which side dealers get forced toward
  3. Price vs GEX Anchor 15%  — below anchor = dealer support, above = resistance
  4. Price Momentum      15%  — EMA trend direction over recent ticks
  5. Price vs Max Pain   10%  — gravitational pull toward max pain
  6. Price vs Walls      10%  — call wall = ceiling, put wall = floor
  7. Candle Structure     5%  — price above/below last 1-min close
"""

import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

_log = logging.getLogger("tos_dash.market_structure")


# ── Regime labels ─────────────────────────────────────────────────────────────
REGIME_PINNED           = "PINNED"
REGIME_TRENDING         = "TRENDING"
REGIME_TRANSITION       = "TRANSITION"
REGIME_SNAP_IMMINENT    = "SNAP IMMINENT"

ZONE_NORMAL   = "normal"
ZONE_WARNING  = "warning"
ZONE_CRITICAL = "critical"

# Checklist factor keys
CL_REGIME    = "regime_type"
CL_SNAP_DIR  = "snap_direction"
CL_ANCHOR    = "price_vs_anchor"
CL_MOMENTUM  = "price_momentum"
CL_MAX_PAIN  = "price_vs_maxpain"
CL_WALLS     = "price_vs_walls"
CL_CANDLE    = "candle_structure"
CL_DEX_BIAS  = "dex_bias"

ALL_FACTORS = [CL_REGIME, CL_SNAP_DIR, CL_ANCHOR, CL_DEX_BIAS,
               CL_MOMENTUM, CL_MAX_PAIN, CL_WALLS, CL_CANDLE]

FACTOR_LABELS = {
    CL_REGIME:   "Regime",
    CL_SNAP_DIR: "Snap Dir",
    CL_ANCHOR:   "GEX Anchor",
    CL_DEX_BIAS: "DEX Bias",
    CL_MOMENTUM: "Momentum",
    CL_MAX_PAIN: "Max Pain",
    CL_WALLS:    "Walls",
    CL_CANDLE:   "Candle",
}

FACTOR_WEIGHTS = {
    CL_REGIME:   0.25,
    CL_SNAP_DIR: 0.20,
    CL_ANCHOR:   0.15,
    CL_DEX_BIAS: 0.10,
    CL_MAX_PAIN: 0.10,
    CL_WALLS:    0.10,
    CL_MOMENTUM: 0.08,
    CL_CANDLE:   0.02,
}

MOMENTUM_MIN_MOVE   = 0.08   # keep — used in bias/trend calc
MOMENTUM_SHORT_DEF  = 120    # 1 min at 500ms — short-term momentum
MOMENTUM_MEDIUM_DEF = 360    # 3 min at 500ms — medium-term confirmation


@dataclass
class ChecklistFactor:
    key:    str
    label:  str
    value:  str    # "Bull" | "Bear" | "Neutral"
    weight: float
    detail: str


@dataclass
class DirectionalChecklist:
    factors:       list
    bull_count:    int
    bear_count:    int
    neutral_count: int
    score:         float   # 0–100 weighted bull%
    lean:          str     # "Bull" | "Bear" | "Mixed"
    confidence:    str     # "Strong" | "Moderate" | "Weak" | "Insufficient"
    structural_score:      float = 0.0
    structural_lean:       str   = "Mixed"
    structural_confidence: str   = "Insufficient"
    tactical_score:        float = 50.0
    tactical_lean:         str   = "Mixed"
    snap_banner:           str   = ""


@dataclass
class MarketStructure:
    regime:         str
    bias:           str
    bias_reason:    str
    spy_price:      float
    gex_anchor:     float
    max_pain:       float
    call_wall:      float
    put_wall:       float
    snap_level:     Optional[float]
    snap_distance:  Optional[float]
    snap_direction: str
    net_gex:        float
    net_dex:        float
    alert_zone:     str
    alert_message:  str
    invalidation:   str
    checklist:      Optional[DirectionalChecklist] = None
    trend:          str                            = "Choppy"


# ── Module-level state (persists across ticks) ───────────────────────────────
_momentum_short:    deque = deque(maxlen=MOMENTUM_SHORT_DEF)
_momentum_medium:   deque = deque(maxlen=MOMENTUM_MEDIUM_DEF)
_current_trend:     str   = "Choppy"   # exported for scalp_advisor
_last_candle_close: float = 0.0
_candle_minute:     int   = -1
_candle_last:       float = 0.0
_momentum_short_cfg:  int = MOMENTUM_SHORT_DEF   # last applied config value
_momentum_medium_cfg: int = MOMENTUM_MEDIUM_DEF  # last applied config value


def _resize_momentum_deques(short_ticks: int, medium_ticks: int):
    """Resize momentum deques if config values have changed.
    Preserves existing history up to the new maxlen."""
    global _momentum_short, _momentum_medium
    global _momentum_short_cfg, _momentum_medium_cfg
    if short_ticks != _momentum_short_cfg and short_ticks > 0:
        existing = list(_momentum_short)
        _momentum_short = deque(existing[-short_ticks:], maxlen=short_ticks)
        _momentum_short_cfg = short_ticks
    if medium_ticks != _momentum_medium_cfg and medium_ticks > 0:
        existing = list(_momentum_medium)
        _momentum_medium = deque(existing[-medium_ticks:], maxlen=medium_ticks)
        _momentum_medium_cfg = medium_ticks


def _update_candle(price: float) -> float:
    global _last_candle_close, _candle_minute, _candle_last
    from datetime import datetime
    now = datetime.now()
    minute = now.hour * 60 + now.minute
    if _candle_minute == -1:
        _candle_minute = minute
        _candle_last   = price
    elif minute != _candle_minute:
        _last_candle_close = _candle_last
        _candle_minute     = minute
        _candle_last       = price
    else:
        _candle_last = price
    return _last_candle_close


def get_current_trend() -> str:
    """Return the most recently computed 1-min/3-min unified trend.
    Called by scalp_advisor each tick to get the shared trend state."""
    return _current_trend


def _build_checklist(
    current_price: float,
    gex_anchor:    float,
    max_pain:      float,
    call_wall:     float,
    put_wall:      float,
    regime:        str,
    snap_direction: str,
    snap_level:    Optional[float],
    snap_distance: Optional[float],
    last_candle_close: float,
    net_dex:           float = 0.0,
    cfg_momentum_short:  int = 120,
    cfg_momentum_medium: int = 360,
) -> DirectionalChecklist:

    factors = []

    def add(key, value, detail, weight=None):
        factors.append(ChecklistFactor(
            key=key, label=FACTOR_LABELS[key],
            value=value, weight=weight if weight is not None else FACTOR_WEIGHTS[key], detail=detail,
        ))

    # ── 1. Regime (25%) ───────────────────────────────────────────────────────
    if regime == REGIME_SNAP_IMMINENT:
        val = "Bull" if snap_direction == "Above" else "Bear"
        add(CL_REGIME, val,
            f"SNAP IMMINENT — explosive move toward {'upside' if val=='Bull' else 'downside'}")
    elif regime == REGIME_TRENDING:
        # snap_direction=="Above" means net_dex > 0 (customers long delta = dealers short
        # = bullish dealer hedging pressure, amplified by negative GEX in TRENDING).
        val = "Bull" if snap_direction == "Above" else "Bear"
        add(CL_REGIME, val,
            f"TRENDING — negative GEX, directional move likely {'up' if val=='Bull' else 'down'}")
    elif regime == REGIME_PINNED:
        add(CL_REGIME, "Neutral",
            "PINNED — positive GEX anchoring price, mean reversion dominant")
    else:
        add(CL_REGIME, "Neutral",
            "TRANSITION — GEX near zero, unstable, no directional edge")

    # ── 2. Snap Direction (20%) ───────────────────────────────────────────────
    if snap_level is None:
        add(CL_SNAP_DIR, "Neutral", "No snap level in loaded strike range")
    else:
        val = "Bull" if snap_direction == "Above" else "Bear"
        dist = f"${snap_distance:.2f} away" if snap_distance is not None else ""
        add(CL_SNAP_DIR, val,
            f"Snap ${snap_level:.0f} {'above' if val=='Bull' else 'below'} — "
            f"{'squeeze' if val=='Bull' else 'flush'} potential {dist}")

    # ── 3. Price vs GEX Anchor (15%) ─────────────────────────────────────────
    if gex_anchor <= 0:
        add(CL_ANCHOR, "Neutral", "No positive GEX — anchor unavailable")
    else:
        diff = current_price - gex_anchor
        if abs(diff) < 0.50:
            add(CL_ANCHOR, "Neutral", f"At GEX anchor ${gex_anchor:.0f} — no pull")
        elif diff < 0:
            add(CL_ANCHOR, "Bull",
                f"${abs(diff):.2f} below GEX anchor ${gex_anchor:.0f} — dealer support beneath")
        else:
            add(CL_ANCHOR, "Bear",
                f"${diff:.2f} above GEX anchor ${gex_anchor:.0f} — dealer resistance above")

    # ── 4. DEX Bias (10%) ────────────────────────────────────────────────────────
    if net_dex > 0:
        add(CL_DEX_BIAS, "Bull",
            "Net DEX positive — customers net long delta, dealers buying to hedge")
    elif net_dex < 0:
        add(CL_DEX_BIAS, "Bear",
            "Net DEX negative — customers net short delta, dealers selling to hedge")
    else:
        add(CL_DEX_BIAS, "Neutral", "Net DEX near zero — no clear dealer positioning")

    # ── 5. Momentum (8%) — requires 1-min AND 3-min agreement ────────────────
    def _get_momentum(history: deque, min_move: float) -> str:
        if len(history) < 4:
            return "Choppy"
        move = history[-1] - history[0]
        if abs(move) < min_move:
            return "Choppy"
        mid = history[len(history) // 2]
        if move > 0:
            return "Uptrend" if mid <= history[-1] else "Choppy"
        else:
            return "Downtrend" if mid >= history[-1] else "Choppy"

    trend_short  = _get_momentum(_momentum_short,  MOMENTUM_MIN_MOVE)
    trend_medium = _get_momentum(_momentum_medium, MOMENTUM_MIN_MOVE)

    if trend_short == trend_medium and trend_short != "Choppy":
        mom_val = "Bull" if trend_short == "Uptrend" else "Bear"
        add(CL_MOMENTUM, mom_val,
            f"1-min: {trend_short} / 3-min: {trend_medium} — confirmed direction")
    else:
        add(CL_MOMENTUM, "Neutral",
            f"1-min: {trend_short} / 3-min: {trend_medium} — no confirmed trend")

    # ── 5. Price vs Max Pain (10% base, time-weighted) ────────────────────────
    # Max pain pin only becomes meaningfully active in the final ~90 min of session.
    # Weight ramps linearly from 0 (before noon) to full (after 2:30pm ET).
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        _now_et = datetime.now(ZoneInfo("America/New_York"))
        _mins_to_close = (16 * 60) - (_now_et.hour * 60 + _now_et.minute)
        if _mins_to_close <= 90:
            mp_weight_mult = 1.0
        elif _now_et.hour < 12:
            mp_weight_mult = 0.0
        else:
            _total_window = (4 * 60) - 90   # noon to 2:30pm = 150 mins
            _elapsed = (_now_et.hour * 60 + _now_et.minute) - (12 * 60)
            mp_weight_mult = min(_elapsed / _total_window, 1.0)
    except Exception:
        mp_weight_mult = 0.5   # safe fallback if zoneinfo unavailable

    mp_eff_weight = FACTOR_WEIGHTS[CL_MAX_PAIN] * mp_weight_mult
    mp_eff_pct    = round(mp_eff_weight * 100)
    mp_weight_note = (
        f"weight: {mp_eff_pct}%"
        if mp_weight_mult >= 1.0
        else f"weight: {mp_eff_pct}% — pre-activation, <2:30pm ET"
    )

    mp_diff = current_price - max_pain
    if abs(mp_diff) < 0.75:
        add(CL_MAX_PAIN, "Neutral",
            f"At max pain ${max_pain:.0f} — no gravity ({mp_weight_note})",
            weight=mp_eff_weight)
    elif mp_diff < 0:
        add(CL_MAX_PAIN, "Bull",
            f"${abs(mp_diff):.2f} below max pain ${max_pain:.0f} — upward gravity ({mp_weight_note})",
            weight=mp_eff_weight)
    else:
        add(CL_MAX_PAIN, "Bear",
            f"${mp_diff:.2f} above max pain ${max_pain:.0f} — downward gravity ({mp_weight_note})",
            weight=mp_eff_weight)

    # ── 6. Price vs Walls (10%) ───────────────────────────────────────────────
    near_call = abs(current_price - call_wall) <= 1.50
    near_put  = abs(current_price - put_wall)  <= 1.50

    if near_call:
        add(CL_WALLS, "Bear",
            f"Testing call wall ${call_wall:.0f} — strong resistance")
    elif near_put:
        add(CL_WALLS, "Bull",
            f"Testing put wall ${put_wall:.0f} — strong support")
    else:
        dist_call = abs(current_price - call_wall)
        dist_put  = abs(current_price - put_wall)
        if dist_put < dist_call * 0.40:
            add(CL_WALLS, "Bull",
                f"Closer to put wall floor ${put_wall:.0f} (${dist_put:.2f} away)")
        elif dist_call < dist_put * 0.40:
            add(CL_WALLS, "Bear",
                f"Closer to call wall ceiling ${call_wall:.0f} (${dist_call:.2f} away)")
        else:
            add(CL_WALLS, "Neutral",
                f"Midrange — put wall ${put_wall:.0f} / call wall ${call_wall:.0f}")

    # ── 7. Candle Structure (5%) ──────────────────────────────────────────────
    if last_candle_close <= 0:
        add(CL_CANDLE, "Neutral", "No completed 1-min candle yet")
    else:
        diff = current_price - last_candle_close
        if abs(diff) < 0.05:
            add(CL_CANDLE, "Neutral",
                f"Flat vs last close ${last_candle_close:.2f}")
        elif diff > 0:
            add(CL_CANDLE, "Bull",
                f"${diff:+.2f} above last candle close ${last_candle_close:.2f}")
        else:
            add(CL_CANDLE, "Bear",
                f"${diff:+.2f} below last candle close ${last_candle_close:.2f}")

    # Redistribute zeroed max pain weight proportionally to other structural factors
    # so morning structural scores aren't deflated by the inactive max pain factor.
    if mp_weight_mult < 1.0:
        _zeroed      = FACTOR_WEIGHTS[CL_MAX_PAIN] * (1.0 - mp_weight_mult)
        _redist_keys = {CL_REGIME, CL_SNAP_DIR, CL_ANCHOR, CL_DEX_BIAS, CL_WALLS}
        _redist_base = sum(FACTOR_WEIGHTS[k] for k in _redist_keys)
        for f in factors:
            if f.key in _redist_keys:
                f.weight = FACTOR_WEIGHTS[f.key] + _zeroed * (FACTOR_WEIGHTS[f.key] / _redist_base)

    # ── Weighted score ────────────────────────────────────────────────────────
    bull_w    = sum(f.weight for f in factors if f.value == "Bull")
    bear_w    = sum(f.weight for f in factors if f.value == "Bear")
    scored_w  = bull_w + bear_w

    bull_count    = sum(1 for f in factors if f.value == "Bull")
    bear_count    = sum(1 for f in factors if f.value == "Bear")
    neutral_count = sum(1 for f in factors if f.value == "Neutral")

    if scored_w < 0.20:
        score, lean, confidence = 50.0, "Mixed", "Insufficient"
    else:
        score = (bull_w / scored_w) * 100
        if score >= 75:
            lean, confidence = "Bull", "Strong"
        elif score >= 62:
            lean, confidence = "Bull", "Moderate"
        elif score <= 25:
            lean, confidence = "Bear", "Strong"
        elif score <= 38:
            lean, confidence = "Bear", "Moderate"
        else:
            lean, confidence = "Mixed", "Weak"

    # ── Structural score (slow factors only) ─────────────────────────────────
    structural_keys = {CL_REGIME, CL_SNAP_DIR, CL_ANCHOR,
                       CL_DEX_BIAS, CL_MAX_PAIN, CL_WALLS}
    s_bull = sum(f.weight for f in factors
                 if f.value == "Bull" and f.key in structural_keys)
    s_bear = sum(f.weight for f in factors
                 if f.value == "Bear" and f.key in structural_keys)
    s_scored = s_bull + s_bear
    if s_scored < 0.15:
        structural_score, structural_lean, structural_confidence = \
            50.0, "Mixed", "Insufficient"
    else:
        structural_score = (s_bull / s_scored) * 100
        if structural_score >= 75:
            structural_lean, structural_confidence = "Bull", "Strong"
        elif structural_score >= 62:
            structural_lean, structural_confidence = "Bull", "Moderate"
        elif structural_score <= 25:
            structural_lean, structural_confidence = "Bear", "Strong"
        elif structural_score <= 38:
            structural_lean, structural_confidence = "Bear", "Moderate"
        else:
            structural_lean, structural_confidence = "Mixed", "Weak"

    # ── Tactical score (momentum + candle only) ───────────────────────────────
    t_bull = sum(f.weight for f in factors
                 if f.value == "Bull" and f.key in {CL_MOMENTUM, CL_CANDLE})
    t_bear = sum(f.weight for f in factors
                 if f.value == "Bear" and f.key in {CL_MOMENTUM, CL_CANDLE})
    t_scored = t_bull + t_bear
    if t_scored < 0.01:
        tactical_score, tactical_lean = 50.0, "Mixed"
    else:
        tactical_score = (t_bull / t_scored) * 100
        tactical_lean = ("Bull" if tactical_score >= 60
                         else "Bear" if tactical_score <= 40 else "Mixed")

    # ── Snap banner (plain-English trade impact) ──────────────────────────────
    snap_banner = ""
    if snap_level is not None and snap_distance is not None:
        side = "above" if snap_direction == "Above" else "below"
        dist_str = f"${snap_distance:.2f}"
        if snap_distance <= 1.0:
            snap_banner = (
                f"⚡ GAMMA FLIP at ${snap_level:.0f} — {dist_str} away · "
                f"dealers about to {'chase upside' if snap_direction == 'Above' else 'chase downside'}"
            )
        elif snap_direction == "Above" and current_price > snap_level:
            snap_banner = (
                f"✅ Snap ${snap_level:.0f} behind price — "
                f"dealers are TAILWIND (buying to hedge)"
            )
        elif snap_direction == "Below" and current_price < snap_level:
            snap_banner = (
                f"✅ Snap ${snap_level:.0f} behind price — "
                f"dealers are TAILWIND (selling to hedge)"
            )
        else:
            snap_banner = (
                f"⚠️ Snap ${snap_level:.0f} {side} — {dist_str} away · "
                f"dealer HEADWIND until crossed"
            )

    return DirectionalChecklist(
        factors=factors,
        bull_count=bull_count, bear_count=bear_count, neutral_count=neutral_count,
        score=round(score, 1), lean=lean, confidence=confidence,
        structural_score=round(structural_score, 1),
        structural_lean=structural_lean,
        structural_confidence=structural_confidence,
        tactical_score=round(tactical_score, 1),
        tactical_lean=tactical_lean,
        snap_banner=snap_banner,
    )


def analyze(
    data: dict,
    strikes: list,
    option_symbols: list,
    current_price: float,
    max_pain: float,
    call_wall: float,
    put_wall: float,
    warn_distance: float = 2.00,
    critical_distance: float = 1.00,
    surge_symbols: set = None,
    cfg: dict = None,
) -> MarketStructure:

    def _sf(key: str) -> float:
        try:
            v = data.get(key)
            return float(v) if v is not None else 0.0
        except (ValueError, TypeError):
            return 0.0

    surge_symbols = surge_symbols or set()

    # ── Per-strike GEX and DEX ────────────────────────────────────────────────
    gex_by_strike: dict[float, float] = {}
    dex_by_strike: dict[float, float] = {}

    for strike in strikes:
        try:
            cs = next(s for s in option_symbols if s.endswith(f'C{strike}'))
            ps = next(s for s in option_symbols if s.endswith(f'P{strike}'))
        except StopIteration:
            continue

        c_oi    = _sf(f"{cs}:OPEN_INT")
        c_gamma = _sf(f"{cs}:GAMMA")
        p_oi    = _sf(f"{ps}:OPEN_INT")
        p_gamma = _sf(f"{ps}:GAMMA")
        c_delta = _sf(f"{cs}:DELTA")
        p_delta = _sf(f"{ps}:DELTA")

        gex = (c_oi * c_gamma - p_oi * p_gamma) * 100 * current_price ** 2 * 0.01
        dex = (c_oi * c_delta + p_oi * p_delta) * 100 * current_price

        gex_by_strike[strike] = gex
        dex_by_strike[strike] = dex

    if not gex_by_strike:
        return _empty_structure(current_price, max_pain, call_wall, put_wall)

    net_gex = sum(gex_by_strike.values())
    net_dex = sum(dex_by_strike.values())

    pos_gex    = {s: v for s, v in gex_by_strike.items() if v > 0}
    gex_anchor = max(pos_gex, key=pos_gex.get) if pos_gex else current_price

    above = sorted([s for s in strikes if s > current_price])
    below = sorted([s for s in strikes if s <= current_price], reverse=True)

    snap_level     = None
    snap_direction = "Above"

    if net_dex > 0:
        snap_direction = "Above"
        # Start with only below-price DEX; net_dex already includes above strikes
        # so using net_dex as seed would double-count them.
        running = sum(dex_by_strike.get(s, 0) for s in below)
        for s in above:
            running += dex_by_strike.get(s, 0)
            if running <= 0:
                snap_level = s
                break
    else:
        snap_direction = "Below"
        # Start with only above-price DEX; accumulate downward.
        running = sum(dex_by_strike.get(s, 0) for s in above)
        for s in below:
            running += dex_by_strike.get(s, 0)
            if running >= 0:
                snap_level = s
                break

    snap_distance = abs(current_price - snap_level) if snap_level else None

    # ── Regime ────────────────────────────────────────────────────────────────
    # TRANSITION when net GEX is near zero (dealers not meaningfully anchored).
    # Fixed absolute threshold; the old `abs(net_gex) * 0.10` was self-referential
    # and always False (abs(x) < abs(x)*0.10 ≡ 1 < 0.10).
    GEX_TRANSITION_THRESHOLD = 5_000_000   # $5 M — tune via observation
    if snap_distance is not None and snap_distance <= critical_distance:
        regime = REGIME_SNAP_IMMINENT
    elif abs(net_gex) < GEX_TRANSITION_THRESHOLD:
        regime = REGIME_TRANSITION
    elif net_gex > 0:
        regime = REGIME_PINNED
    else:
        regime = REGIME_TRENDING
    _log.debug("REGIME: %s (net_gex=%.0f net_dex=%.0f snap_lvl=%s snap_dist=%s)",
               regime, net_gex, net_dex,
               f"{snap_level:.2f}" if snap_level is not None else "None",
               f"{snap_distance:.2f}" if snap_distance is not None else "None")

    # ── Bias ──────────────────────────────────────────────────────────────────
    mp_distance    = current_price - max_pain
    above_mp       = mp_distance > 0
    near_call_wall = abs(current_price - call_wall) <= 2.0
    near_put_wall  = abs(current_price - put_wall) <= 2.0

    # ── Feed momentum deques + compute unified trend ──────────────────────────
    global _current_trend
    # Resize deques if config values differ from current maxlen
    _cfg_short  = cfg.get("momentum_short_ticks",  MOMENTUM_SHORT_DEF)  if cfg else MOMENTUM_SHORT_DEF
    _cfg_medium = cfg.get("momentum_medium_ticks", MOMENTUM_MEDIUM_DEF) if cfg else MOMENTUM_MEDIUM_DEF
    _resize_momentum_deques(_cfg_short, _cfg_medium)
    _momentum_short.append(current_price)
    _momentum_medium.append(current_price)
    if len(_momentum_short) < 4:
        _current_trend = "Choppy"
    else:
        _move = _momentum_short[-1] - _momentum_short[0]
        if _move < -MOMENTUM_MIN_MOVE:
            _current_trend = "Downtrend"
        elif _move > MOMENTUM_MIN_MOVE:
            _current_trend = "Uptrend"
        else:
            _current_trend = "Choppy"
    trend = _current_trend

    dex_bias = "Bullish" if net_dex > 0 else "Bearish"

    if regime == REGIME_SNAP_IMMINENT:
        bias        = "Bullish" if snap_direction == "Above" else "Bearish"
        bias_reason = (
            f"Snap imminent at ${snap_level:.0f} — dealers forced to chase "
            f"{'upside' if snap_direction == 'Above' else 'downside'}"
        )
    elif regime == REGIME_TRENDING:
        if trend == "Downtrend" and dex_bias == "Bearish":
            bias = "Bearish"
            bias_reason = "Trending regime — price falling, bearish positioning aligned. Dealers amplify move."
        elif trend == "Downtrend" and dex_bias == "Bullish":
            bias = "Bearish"
            bias_reason = "Trending regime — price falling. DEX diverging (watch for reversal)."
        elif trend == "Uptrend" and dex_bias == "Bullish":
            bias = "Bullish"
            bias_reason = "Trending regime — price rising, bullish positioning aligned. Dealers amplify move."
        elif trend == "Uptrend" and dex_bias == "Bearish":
            bias = "Bullish"
            bias_reason = "Trending regime — price rising. DEX diverging (watch for reversal)."
        else:  # Choppy — no momentum confirmation
            bias = "Neutral"
            bias_reason = "Trending regime — no clear momentum. Wait for price direction to establish."
    elif regime == REGIME_TRANSITION:
        bias        = "Neutral"
        bias_reason = "GEX near zero — regime unstable, no clear anchor"
    else:
        if abs(mp_distance) <= 1.0:
            bias, bias_reason = "Neutral", f"Pinned at max pain ${max_pain:.0f}"
        elif above_mp and net_dex > 0:
            bias        = "Bearish"
            bias_reason = f"Above max pain +${mp_distance:.2f} — gravitating toward ${max_pain:.0f}, DEX {'confirms' if dex_bias == 'Bearish' else 'diverges, watch for hold'}"
        elif not above_mp and net_dex < 0:
            bias        = "Bullish"
            bias_reason = f"Below max pain -${abs(mp_distance):.2f} — gravitating toward ${max_pain:.0f}, DEX {'confirms' if dex_bias == 'Bullish' else 'diverges, watch for hold'}"
        elif near_call_wall and above_mp:
            bias, bias_reason = "Bearish", f"Resistance at call wall ${call_wall:.0f}"
        elif near_put_wall and not above_mp:
            bias, bias_reason = "Bullish", f"Support at put wall ${put_wall:.0f}"
        else:
            bias, bias_reason = "Neutral", "Mixed signals — GEX pinning dominant"

    # ── Invalidation ─────────────────────────────────────────────────────────
    if bias == "Bearish":
        invalidation = f"Bullish above ${snap_level:.0f}" if snap_level else f"Bullish above ${call_wall:.0f}"
    elif bias == "Bullish":
        invalidation = f"Bearish below ${snap_level:.0f}" if snap_level else f"Bearish below ${put_wall:.0f}"
    else:
        invalidation = f"Watch ${call_wall:.0f} / ${put_wall:.0f}"

    # ── Alert zone ────────────────────────────────────────────────────────────
    if snap_distance is None:
        alert_zone, alert_message = ZONE_NORMAL, ""
    elif snap_distance <= critical_distance:
        alert_zone    = ZONE_CRITICAL
        alert_message = (
            f"⚡ SNAP CRITICAL — ${snap_distance:.2f} from flip at ${snap_level:.0f}! "
            f"Dealers about to reverse {'bullish' if snap_direction == 'Above' else 'bearish'}"
        )
    elif snap_distance <= warn_distance:
        alert_zone    = ZONE_WARNING
        alert_message = (
            f"⚠️ SNAP WARNING — ${snap_distance:.2f} from ${snap_level:.0f}. "
            f"Watch for {'upside' if snap_direction == 'Above' else 'downside'} acceleration"
        )
    else:
        alert_zone, alert_message = ZONE_NORMAL, ""

    # ── Directional checklist ─────────────────────────────────────────────────
    last_candle_close = _update_candle(current_price)
    checklist = _build_checklist(
        current_price       = current_price,
        gex_anchor          = gex_anchor,
        max_pain            = max_pain,
        call_wall           = call_wall,
        put_wall            = put_wall,
        regime              = regime,
        snap_direction      = snap_direction,
        snap_level          = snap_level,
        snap_distance       = snap_distance,
        last_candle_close   = last_candle_close,
        net_dex             = net_dex,
        cfg_momentum_short  = cfg.get("momentum_short_ticks",  MOMENTUM_SHORT_DEF)  if cfg else MOMENTUM_SHORT_DEF,
        cfg_momentum_medium = cfg.get("momentum_medium_ticks", MOMENTUM_MEDIUM_DEF) if cfg else MOMENTUM_MEDIUM_DEF,
    )

    return MarketStructure(
        regime=regime, bias=bias, bias_reason=bias_reason,
        spy_price=current_price, gex_anchor=gex_anchor,
        max_pain=max_pain, call_wall=call_wall, put_wall=put_wall,
        snap_level=snap_level, snap_distance=snap_distance,
        snap_direction=snap_direction,
        net_gex=net_gex, net_dex=net_dex,
        alert_zone=alert_zone, alert_message=alert_message,
        invalidation=invalidation,
        checklist=checklist,
        trend=trend,
    )


def _empty_structure(price, max_pain, call_wall, put_wall) -> MarketStructure:
    return MarketStructure(
        regime=REGIME_TRANSITION, bias="Neutral",
        bias_reason="Insufficient data",
        spy_price=price, gex_anchor=price,
        max_pain=max_pain, call_wall=call_wall, put_wall=put_wall,
        snap_level=None, snap_distance=None, snap_direction="Above",
        net_gex=0.0, net_dex=0.0,
        alert_zone=ZONE_NORMAL, alert_message="",
        invalidation="—", checklist=None,
    )
