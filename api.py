"""
api.py — tos-dash-v2 API + process manager.

Single entry point: python api.py
  - Manages spy_writer.py as a subprocess
  - Serves REST + WebSocket endpoints
  - Handles config read/write + writer restart

Endpoints:
  GET  /price       raw SPY price
  GET  /chain       raw option chain
  GET  /snapshot    computed dashboard snapshot
  GET  /config      current settings
  POST /config      update settings (restarts writer)
  GET  /status      health check
  WS   /ws          streams /snapshot every poll interval
"""

import asyncio
import json
import logging
import subprocess
import sys
import threading
import time
from collections import deque
import uvicorn
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

# ── Paths ─────────────────────────────────────────────────────────────────────
THIS_DIR    = Path(__file__).parent
WRITER_SCRIPT = THIS_DIR / "spy_writer.py"
TICK_RECORDER_SCRIPT = THIS_DIR / "tick_recorder.py"
CONFIG_FILE = THIS_DIR / "config.json"
PRICE_FILE  = THIS_DIR / "spy_price.json"
CHAIN_FILE  = THIS_DIR / "option_chain.json"
DASH_FILE   = THIS_DIR / "dashboard.html"

sys.path.insert(0, str(THIS_DIR))

# ── Logging ───────────────────────────────────────────────────────────────────
from idea_logger import setup_app_logging, IdeaLogger

app_log = setup_app_logging()
logger  = logging.getLogger("tos_dash.api")

# ── Business logic imports ────────────────────────────────────────────────────
from gamma_chart import calculate_max_pain, calculate_walls
import market_structure as ms_mod
from volume_tracker import VolumeTracker
from scalp_advisor import ScalpAdvisor
from channel_advisor import ChannelAdvisor
import news_fetcher

volume_tracker  = VolumeTracker()
scalp_advisor   = ScalpAdvisor()
channel_advisor = ChannelAdvisor()
cfg_snapshot    = {}   # latest config, shared with idea_logger
_last_snapshot: dict = {}   # cached by tick_loop; served by GET /snapshot
_last_tick_seen: int   = -1
_last_tick_time: float = 0.0

# 1DTE next-trading-day cache
_next_1dte_date: str   = ""     # "YYYY-MM-DD"
_next_1dte_time: float = 0.0    # time.time() when cache was set
_dte_mode: str         = "0DTE" # "0DTE" or "1DTE"

# SPY volume tracker — rolling tick volume rate
_spy_vol_history: deque = deque(maxlen=50)
_spy_last_volume: float = 0.0

# SPY price history for surge move detection (last 3 prices)
_spy_price_recent: deque = deque(maxlen=3)
_surge_suppress_until: float = 0.0   # monotonic time — suppress surfaces until this time

# VIX EMA/SMA tracking
_vix_history:  deque = deque(maxlen=21)
_vix_open:     float = 0.0
_vix_open_set: bool  = False

def _compute_vix_signals(vix: float) -> dict:
    """Compute VIX EMA(9)/SMA(21), cross signals, and vs open."""
    global _vix_open, _vix_open_set
    if vix is None:
        return {}
    _vix_history.append(vix)
    if not _vix_open_set and len(_vix_history) >= 3:
        _vix_open     = sorted(list(_vix_history)[:3])[1]  # median of first 3
        _vix_open_set = True
    hist = list(_vix_history)
    # EMA(9)
    ema9 = hist[-1]
    k9 = 2 / (9 + 1)
    for v in hist[-9:]:
        ema9 = v * k9 + ema9 * (1 - k9)
    # SMA(21)
    sma21 = sum(hist) / len(hist)
    cross = "above" if ema9 > sma21 else "below"
    vs_open = vix - _vix_open if _vix_open else 0.0
    return {
        "last":     round(vix, 2),
        "ema9":     round(ema9, 2),
        "sma21":    round(sma21, 2),
        "cross":    cross,
        "vs_open":  round(vs_open, 2),
        "open":     round(_vix_open, 2),
    }

def _get_spy_vol_rate(current_volume: float, cfg: dict) -> tuple[float, float]:
    """Returns (tick_vol_rate, vol_ratio_vs_rolling_avg)."""
    global _spy_last_volume
    tick_vol = max(0.0, (current_volume or 0) - _spy_last_volume)
    if current_volume:
        _spy_last_volume = current_volume
    lookback = cfg.get("vol_lookback", 20)
    _spy_vol_history.append(tick_vol)
    recent = list(_spy_vol_history)[-lookback:]
    avg    = sum(recent) / len(recent) if recent else 1.0
    ratio  = tick_vol / avg if avg > 0 else 1.0
    return tick_vol, ratio

idea_logger = IdeaLogger(cfg=cfg_snapshot)

logger.info("tos-dash-v2 starting up — root=%s", THIS_DIR)

# ── 1DTE next-trading-day helpers ─────────────────────────────────────────────
import time as _time_mod

def _compute_next_trading_day() -> str:
    """Return the next trading day after today as YYYY-MM-DD (skips Sat/Sun)."""
    from datetime import date, timedelta
    d = date.today() + timedelta(days=1)
    while d.weekday() >= 5:  # 5=Sat, 6=Sun
        d += timedelta(days=1)
    return d.isoformat()

def get_next_1dte() -> str:
    """Return cached next trading day, refreshing if >1 hour old."""
    global _next_1dte_date, _next_1dte_time
    if not _next_1dte_date or (_time_mod.time() - _next_1dte_time) > 3600:
        _next_1dte_date = _compute_next_trading_day()
        _next_1dte_time = _time_mod.time()
    return _next_1dte_date

def _reconfigure_advisor(cfg: dict):
    """Apply EMA/SMA tick settings to the ScalpAdvisor instance."""
    from collections import deque
    ema = cfg.get('ema_ticks', 9)
    sma = cfg.get('sma_ticks', 20)
    # EMA ticks -> price history window (direction detection)
    scalp_advisor._price_history = deque(scalp_advisor._price_history, maxlen=ema)
    # SMA ticks -> score smoothing window (rebuild history with new maxlen).
    # Iterate list(keys()) so a concurrent tick adding a new symbol mid-loop
    # cannot raise RuntimeError: dictionary changed size during iteration.
    for sym in list(scalp_advisor._score_history.keys()):
        scalp_advisor._score_history[sym] = deque(scalp_advisor._score_history[sym], maxlen=sma)

# ── Config ────────────────────────────────────────────────────────────────────
DEFAULT_CONFIG = {
    "symbol":         "SPY",
    "strike_range":   10,
    "wall_range":     12,
    "gex_range":      40,
    "strike_spacing": 1.0,
    "expiry_date":    None,
    "warn_distance":  2.0,
    "critical_distance": 1.0,
    "surge_window":   10,
    "surge_threshold":2.0,
    "ema_ticks":      9,
    "sma_ticks":      20,
    "poll_ms":        500,
    "test_mode":             False,
    "test_date":             None,
    "risk_cap":              5.00,
    "stop_pct":              0.50,
    "reentry_window_min":    5,
    "vol_surge_ratio":       1.8,
    "vol_lookback":          20,
    "score_decay_threshold": 45,
    "score_decay_ticks":     10,
    "confirm_score":         60,
    "confirm_ticks":         13,
    "confirm_ticks_surge":   5,
    # Change #1
    "idea_cooldown_min":     15,
    # Change #2
    "vol_surge_mult":        1.5,
    # Change #4
    "iv_cap":                35.0,   # 0 = disabled
    # Change #6
    "open_gate_minutes":     5,
    "rtd_heartbeat_ms":      200,   # Step 2: replaces config.yaml timing.initial_heartbeat
    "alpaca_api_key":        "",
    "alpaca_secret_key":     "",
    "anthropic_api_key":     "",
    "paper_stop_pct":        0.30,
    "paper_target_1_pct":    0.30,
    "paper_target_2_pct":    0.50,
    "paper_target_3_pct":    0.75,
    "drop_threshold":        55,
    "min_delta":             0.25,
    "min_mark":              0.50,
    "max_mark":              2.00,
    "briefing_delta_min":    0.35,
    "briefing_delta_max":    0.50,
    "pinned_allowed_trends":  ["Downtrend"],
    "pinned_max_score":       60,
    "max_surface_score":      66,
    "paper_stop_pct_pinned":  0.20,
    "paper_starting_balance": 10000.0,
    "paper_risk_pct":         2.0,
    "momentum_short_ticks":   120,
    "momentum_medium_ticks":  360,
    "structure_gate_scanner": False,
    "no_trade_gate":          True,
    "gate_hysteresis_ticks":  6,    # consecutive fail ticks before gate closes
    "briefing_eod_hour":      14,   # 2:30 PM ET = hour 14, minute 30
    "iv_floor":              0.0,
    "iv_ceiling":            60.0,
    "commission_per_contract": 0.65,
    "max_surface_candidates":  3,
    "post_stop_cooldown_sec":       30,
    "surge_price_move_threshold":   0.40,
    "surge_vol_ratio_threshold":    3.0,
    "surge_suppress_sec":           60,
    "tick_extreme_threshold":       500,
    "wall_touch_distance":          0.30,
    "wall_touch_cooldown_sec":      120,
    "wall_touch_stop_pct":          0.10,
    "wall_touch_target_pct":        0.15,
    "wall_touch_max_mark":          1.50,
    "breakout_watch_distance":      0.50,
    "breakout_vol_ratio_min":       2.0,
    "pinned_trend_tick_threshold":  150,
    "pinned_trend_tick_min_count":  5,
    "pinned_trend_vol_min":         0.8,
    "pinned_trend_vol_max":         3.0,
    "pinned_trend_max_score":       63,
    "pinned_trend_max_mark":        1.50,
    "channel_block_middle":              True,
    "channel_block_exhaustion":          True,
    "channel_exhaustion_min_profit_pct": 10.0,
    "tick_history_enabled":    True,
    "replay_db_path":          "D:/tos-dash-v2-replay/",
}

def load_config() -> dict:
    try:
        saved = json.loads(CONFIG_FILE.read_text())
        return {**DEFAULT_CONFIG, **saved}
    except Exception:
        return dict(DEFAULT_CONFIG)

def save_config(cfg: dict):
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))

# ── Writer process manager ────────────────────────────────────────────────────
writer_proc: Optional[subprocess.Popen] = None
writer_lock = threading.Lock()

def start_writer():
    global writer_proc, _vix_history, _vix_open, _vix_open_set
    _vix_history.clear()
    _vix_open     = 0.0
    _vix_open_set = False
    with writer_lock:
        # Kill existing
        if writer_proc and writer_proc.poll() is None:
            logger.info("Stopping existing writer process...")
            writer_proc.terminate()
            try:
                writer_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                writer_proc.kill()
            writer_proc = None

        logger.info("Starting spy_writer.py...")
        writer_proc = subprocess.Popen(
            [sys.executable, str(WRITER_SCRIPT)],
            stderr=None,    # stderr flows to our terminal
            stdout=subprocess.DEVNULL,
            cwd=str(THIS_DIR),  # run from tos-dash-v2/ dir
        )
        logger.info(f"Writer started (PID {writer_proc.pid})")

def stop_writer():
    global writer_proc
    with writer_lock:
        if writer_proc and writer_proc.poll() is None:
            writer_proc.terminate()
            try:
                writer_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                writer_proc.kill()
        writer_proc = None

def writer_alive() -> bool:
    return writer_proc is not None and writer_proc.poll() is None

# ── Tick recorder process manager ─────────────────────────────────────────────
tick_recorder_proc: Optional[subprocess.Popen] = None
tick_recorder_lock = threading.Lock()

def start_tick_recorder():
    global tick_recorder_proc
    if not TICK_RECORDER_SCRIPT.exists():
        return
    with tick_recorder_lock:
        if tick_recorder_proc and tick_recorder_proc.poll() is None:
            return  # already running
        try:
            tick_recorder_proc = subprocess.Popen(
                [sys.executable, str(TICK_RECORDER_SCRIPT)],
                stderr=None,
                stdout=subprocess.DEVNULL,
                cwd=str(THIS_DIR),
            )
            logger.info(f"Tick recorder started (PID {tick_recorder_proc.pid})")
        except Exception as e:
            logger.warning(f"Tick recorder start failed: {e}")

def stop_tick_recorder():
    global tick_recorder_proc
    with tick_recorder_lock:
        if tick_recorder_proc and tick_recorder_proc.poll() is None:
            # Write stop file for clean shutdown
            try:
                (THIS_DIR / "tick_recorder.stop").write_text("stop")
            except Exception:
                pass
            try:
                tick_recorder_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                tick_recorder_proc.kill()
        tick_recorder_proc = None
        try:
            (THIS_DIR / "tick_recorder.stop").unlink(missing_ok=True)
        except Exception:
            pass

# ── Helpers ───────────────────────────────────────────────────────────────────
def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception as e:
        return {"error": str(e)}

def build_snapshot() -> dict:
    global _surge_suppress_until
    cfg          = load_config()
    price_data   = read_json(PRICE_FILE)
    chain_data   = read_json(CHAIN_FILE)

    if "error" in price_data:
        return {"error": f"price file: {price_data['error']}"}
    if "error" in chain_data:
        return {"error": f"chain file: {chain_data['error']}"}

    price          = price_data.get("last")
    option_symbols = list(chain_data.get("chain", {}).keys())
    tick           = chain_data.get("tick", 0)
    timestamp      = chain_data.get("timestamp")
    expiry         = chain_data.get("expiry")

    if not price or not option_symbols:
        return {"error": "No data yet"}

    # SPY volume rate
    spy_volume               = price_data.get("volume") or 0
    spy_vol_rate, spy_vol_ratio = _get_spy_vol_rate(spy_volume, cfg)

    es_last   = price_data.get("es_last")
    spx_last  = price_data.get("spx_last")
    vix_raw   = price_data.get("vix_last")
    ntick_raw = price_data.get("ntick_val")
    trin_val  = price_data.get("trin_val")
    trinq_val = price_data.get("trinq_val")
    add_val   = price_data.get("add_val")
    qqq_last  = price_data.get("qqq_last")
    iwm_last  = price_data.get("iwm_last")
    nq_last   = price_data.get("nq_last")
    vix_signals = _compute_vix_signals(vix_raw)
    ntick = int(ntick_raw) if ntick_raw is not None else None

    # Live cross-instrument ratios — recomputed every tick from RTD prices.
    # All three ratios default to None if either price is unavailable.
    def _ratio(a, b):
        try:
            return round(a / b, 4) if a and b and b != 0 else None
        except Exception:
            return None

    es_spy_ratio  = _ratio(es_last,  price)      # ES ÷ SPY
    spx_spy_ratio = _ratio(spx_last, price)      # SPX ÷ SPY
    spx_es_ratio  = _ratio(spx_last, es_last)    # SPX ÷ ES

    # Positions from positions.json
    pos_data  = read_json(THIS_DIR / "positions.json")
    positions = pos_data.get("positions", {}) if "error" not in pos_data else {}

    # Flatten chain → data dict
    data = {}
    for sym, fields in chain_data.get("chain", {}).items():
        for qt_name, val in fields.items():
            data[f"{sym}:{qt_name}"] = val

    sym_root = price_data.get("symbol", "SPY")
    data[f"{sym_root}:LAST"]     = price_data.get("last")
    data[f"{sym_root}:BID"]      = price_data.get("bid")
    data[f"{sym_root}:ASK"]      = price_data.get("ask")
    data[f"{sym_root}:MARK"]     = price_data.get("mark")
    data[f"{sym_root}:VOLUME"]   = spy_volume
    data[f"{sym_root}:BID_SIZE"] = price_data.get("bid_size")
    data[f"{sym_root}:ASK_SIZE"] = price_data.get("ask_size")

    # Extract strikes — use int for whole numbers
    strikes = set()
    for sym in option_symbols:
        try:
            for sep in ("C", "P"):
                if sep in sym[1:]:
                    f = float(sym[1:].split(sep)[-1])
                    strikes.add(int(f) if f == int(f) else f)
                    break
        except (ValueError, IndexError):
            pass
    strikes = sorted(strikes)

    if not strikes:
        return {"error": "Could not extract strikes"}

    price_val  = float(price) if price else 0
    wall_range = cfg.get("wall_range", 12)
    gex_range  = cfg.get("gex_range",  40)

    # GEX strikes — broad range for accurate regime/net_gex detection
    gex_strike_set = set(s for s in strikes if abs(s - price_val) <= gex_range) if price_val else set(strikes)
    gex_option_symbols = []
    for sym in option_symbols:
        try:
            for sep in ("C", "P"):
                if sep in sym[1:]:
                    f = float(sym[1:].split(sep)[-1])
                    s = int(f) if f == int(f) else f
                    if s in gex_strike_set:
                        gex_option_symbols.append(sym)
                    break
        except (ValueError, IndexError):
            pass

    # Wall strikes — tight range for intraday call/put wall detection
    wall_strikes = [s for s in strikes if abs(s - price_val) <= wall_range] if price_val else list(strikes)
    wall_strike_set = set(wall_strikes)
    wall_option_symbols = []
    for sym in option_symbols:
        try:
            for sep in ("C", "P"):
                if sep in sym[1:]:
                    f = float(sym[1:].split(sep)[-1])
                    s = int(f) if f == int(f) else f
                    if s in wall_strike_set:
                        wall_option_symbols.append(sym)
                    break
        except (ValueError, IndexError):
            pass

    # Calculations — walls/max_pain use wall_strikes for accuracy.
    # Guard: OnDemand (test/replay) does not stream OPEN_INT — all OI reads as
    # zero, producing meaningless results. Skip if total OI is zero.
    _total_oi = sum(float(data.get(f"{sym}:OPEN_INT") or 0) for sym in wall_option_symbols)
    if _total_oi == 0:
        max_pain = call_wall = put_wall = None
    else:
        try:
            max_pain = calculate_max_pain(data, wall_strikes, wall_option_symbols, debug=False)
        except Exception:
            max_pain = None

        try:
            call_wall, put_wall = calculate_walls(data, wall_strikes, wall_option_symbols,
                                                  current_price=price_val, debug=False)
        except Exception:
            call_wall = put_wall = None

    try:
        volume_tracker.update(data, option_symbols)
        surge_df   = volume_tracker.get_surge_table(
            threshold_pct = cfg.get("surge_threshold", 50.0),
            ema_span      = cfg.get("ema_ticks", 3),
            sma_window    = cfg.get("sma_ticks", 20),
        )
        surge_syms = set(surge_df["Symbol"].tolist()) if not surge_df.empty else set()
    except Exception:
        surge_syms = set()

    ms = None
    try:
        ms = ms_mod.analyze(
            data              = data,
            strikes           = list(gex_strike_set),
            option_symbols    = gex_option_symbols,
            current_price     = price,
            max_pain          = max_pain or price,
            call_wall         = call_wall,
            put_wall          = put_wall,
            warn_distance     = cfg.get("warn_distance", 2.0),
            critical_distance = cfg.get("critical_distance", 1.0),
            surge_symbols     = surge_syms,
            cfg               = cfg,
        )
        cl = ms.checklist
        ms_dict = {
            "regime":           ms.regime,
            "bias":             ms.bias,
            "bias_reason":      ms.bias_reason,
            "spy_price":        ms.spy_price,
            "gex_anchor":       ms.gex_anchor,
            "max_pain":         ms.max_pain,
            "call_wall":        ms.call_wall,
            "put_wall":         ms.put_wall,
            "snap_level":       ms.snap_level,
            "snap_distance":    ms.snap_distance,
            "snap_direction":   ms.snap_direction,
            "net_gex":          ms.net_gex,
            "net_dex":          ms.net_dex,
            "alert_zone":       ms.alert_zone,
            "alert_message":    ms.alert_message,
            "invalidation":     ms.invalidation,
            "checklist_lean":       cl.lean        if cl else None,
            "checklist_confidence": cl.confidence  if cl else None,
            "checklist_score":      cl.score       if cl else None,
            "checklist_bull_count": cl.bull_count  if cl else None,
            "checklist_bear_count": cl.bear_count  if cl else None,
            "checklist_factors": [
                {
                    "key":    f.key,
                    "label":  f.label,
                    "value":  f.value,
                    "weight": f.weight,
                    "detail": f.detail,
                }
                for f in (cl.factors if cl else [])
            ],
            "checklist_structural_score":      cl.structural_score      if cl else None,
            "checklist_structural_lean":       cl.structural_lean       if cl else None,
            "checklist_structural_confidence": cl.structural_confidence if cl else None,
            "checklist_tactical_score":        cl.tactical_score        if cl else None,
            "checklist_tactical_lean":         cl.tactical_lean         if cl else None,
            "checklist_snap_banner":           cl.snap_banner           if cl else "",
        }
    except Exception as e:
        ms_dict = {"error": str(e)}

    # Track SPY price for surge detection
    import time as _t
    from datetime import datetime as _dt_now
    if price:
        _spy_price_recent.append(float(price))

    # Channel detection — update every tick
    try:
        _channel_state = channel_advisor.update(
            price          = float(price) if price else 0.0,
            spy_vol_ratio  = spy_vol_ratio,
            now            = _dt_now.now(),
        )
    except Exception as _ce:
        logger.debug("Channel advisor error: %s", _ce)
        _channel_state = None

    # Surge protection — suppress all surfaces if SPY moved > $0.40 in 2 ticks AND vol spike
    _surge_suppressed = False
    if len(_spy_price_recent) >= 2:
        _price_move = abs(_spy_price_recent[-1] - _spy_price_recent[-2])
        if _price_move > cfg.get("surge_price_move_threshold", 0.40) and spy_vol_ratio > cfg.get("surge_vol_ratio_threshold", 3.0):
            _surge_suppress_until = _t.monotonic() + cfg.get("surge_suppress_sec", 60)
            logger.info("Surge protection triggered: price_move=%.2f vol_ratio=%.1f suppress=60s",
                        _price_move, spy_vol_ratio)
    if _t.monotonic() < _surge_suppress_until:
        _surge_suppressed = True

    candidates = []
    try:
        if _surge_suppressed:
            candidates = []
        else:
            _cfg_with_ratio = {
                **cfg,
                "_spy_vol_ratio": spy_vol_ratio,
                "_ntick":         ntick or 0,
                "_vix":           vix_raw or 0,
                "_channel":       channel_advisor.to_dict(),
            }
            candidates = scalp_advisor.get_recommendations(
                data           = data,
                strikes        = strikes,
                option_symbols = option_symbols,
                symbol         = cfg.get("symbol", "SPY"),
                max_pain       = max_pain,
                call_wall      = call_wall,
                put_wall       = put_wall,
                surge_symbols  = surge_syms,
                risk_cap       = cfg.get("risk_cap", 5.00),
                cfg            = _cfg_with_ratio,
                ms             = ms,
            )

        # TICK directional filter — remove candidates that chase TICK extremes
        _tick_extreme = cfg.get("tick_extreme_threshold", 500)
        if ntick is not None and abs(ntick) > _tick_extreme and candidates:
            filtered = []
            for _c in candidates:
                if _c.option_type == "Call" and ntick < -_tick_extreme:
                    logger.debug("TICK filter: blocked Call %s (TICK=%d)", _c.symbol, ntick)
                    continue
                if _c.option_type == "Put" and ntick > _tick_extreme:
                    logger.debug("TICK filter: blocked Put %s (TICK=%d)", _c.symbol, ntick)
                    continue
                filtered.append(_c)
            candidates = filtered

        # Log all surfaced candidates for backtesting
        idea_logger.log_surface_candidates(
            candidates    = candidates,
            spy_price     = price_data.get("last", 0),
            ms            = ms,
            vix_signals   = vix_signals,
            ntick         = ntick,
            spy_vol_ratio = spy_vol_ratio,
        )

        # Full lifecycle tracking — process_tick handles all idea logic
        idea_logger.update_cfg(cfg)
        idea_logger.process_tick(
            candidates    = candidates,
            data          = data,
            spy_price     = price_data.get("last", 0),
            spy_vol_rate  = spy_vol_rate,
            spy_vol_ratio = spy_vol_ratio,
            ms            = ms,
            surge_syms    = surge_syms,
            vix_signals   = vix_signals,
            ntick         = ntick,
            channel       = channel_advisor.to_dict(),
        )
        # Position auto-linking
        if positions:
            idea_logger.process_positions(positions)

        # Write active_ideas.json for external tools
        try:
            (THIS_DIR / "active_ideas.json").write_text(
                json.dumps(idea_logger.get_active_ideas(), indent=2)
            )
        except Exception:
            pass

        candidates_list = [
            {
                "symbol":   c.symbol,
                "side":     c.option_type,
                "strike":   c.strike,
                "score":    c.score,
                "reason":   c.reasons[0] if c.reasons else "",
                "bid":      c.bid,
                "ask":      c.ask,
                "delta":    c.delta,
                "gamma":    0,
                "impl_vol": c.iv,
                "mark":     c.mark,
                "trend":    c.underlying_trend,
                "direction":c.direction,
            }
            for c in (candidates or [])
        ]
    except Exception as e:
        candidates_list = []
        logger.warning("Candidates error: %s", e, exc_info=True)

    # ── Trade Briefing ────────────────────────────────────────────────────────
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        _now_et  = datetime.now(ZoneInfo("America/New_York"))
        _eod_hr  = cfg.get("briefing_eod_hour", 14)
        _is_eod  = _now_et.hour >= _eod_hr and _now_et.minute >= 30

        # No-trade determination
        _ms_cl   = ms.checklist if ms else None
        _s_conf  = _ms_cl.structural_confidence if _ms_cl else "Insufficient"
        _s_lean  = _ms_cl.structural_lean       if _ms_cl else "Mixed"
        _regime  = ms.regime if ms else "TRANSITION"
        _trend   = ms.trend  if ms else "Choppy"
        _gex_neg = ms.net_gex < 0 if ms else False

        _gate_pass = (
            _gex_neg
            and _trend not in ("Choppy", "CHOPPY", "choppy")
            and _s_conf in ("Strong", "Moderate")
        )

        _no_trade_reason = ""
        _call_wall_str = f"${ms.call_wall:.0f}" if ms and ms.call_wall else "--"
        _put_wall_str  = f"${ms.put_wall:.0f}"  if ms and ms.put_wall  else "--"
        _price_str     = f"${float(_trend_price):.2f}" if (_trend_price := (ms.spy_price if ms else None)) else "--"
        if not _gex_neg:
            _no_trade_reason = f"Price pinned at {_price_str}. Need break above {_call_wall_str} or below {_put_wall_str} to trade."
        elif _trend in ("Choppy", "CHOPPY", "choppy"):
            _no_trade_reason = f"Choppy price action. Wait for clear direction before trading."
        elif _s_conf not in ("Strong", "Moderate"):
            _no_trade_reason = f"No clear market conviction ({_s_lean}). Wait for structure to develop."

        # Best contract selection from chain_full
        # Criteria: delta 0.38–0.52, mark $0.50–$2.00, pick highest-scored candidate
        # When no candidates (NO TRADE), still find best delta-range contract as watch
        def _best_contract(side):
            """Find best contract for given side ('Call' or 'Put')."""
            # First try from scored candidates
            cands = [c for c in (candidates or []) if c.option_type == side]
            if cands:
                _d_min = cfg.get("briefing_delta_min", 0.35)
                _d_max = cfg.get("briefing_delta_max", 0.50)
                _m_min = cfg.get("min_mark",  0.50)
                _m_max = cfg.get("max_mark",  2.00)
                in_range = [c for c in cands
                            if _d_min <= c.delta <= _d_max
                            and _m_min <= c.mark  <= _m_max]
                pool = in_range if in_range else cands
                best = max(pool, key=lambda c: c.score)
                return {
                    "symbol": best.symbol,
                    "strike": best.strike,
                    "mark":   round(best.mark, 2),
                    "delta":  round(best.delta, 2),
                    "iv":     round(best.iv, 1),
                    "score":  round(best.score, 1),
                    "source": "candidate",
                }
            # Fallback: scan chain_full for best delta-range contract
            chain = chain_data.get("chain", {})
            best_sym, best_fields, best_delta_dist = None, None, 999
            _d_min = cfg.get("briefing_delta_min", 0.35)
            _d_max = cfg.get("briefing_delta_max", 0.50)
            _m_min = cfg.get("min_mark",  0.50)
            _m_max = cfg.get("max_mark",  2.00)
            target_delta = (_d_min + _d_max) / 2
            for sym, fields in chain.items():
                is_call = bool(__import__('re').search(r'\d[C]\d', sym))
                is_put  = bool(__import__('re').search(r'\d[P]\d', sym))
                if side == "Call" and not is_call:
                    continue
                if side == "Put" and not is_put:
                    continue
                d = fields.get("DELTA")
                m = fields.get("MARK")
                if d is None or m is None:
                    continue
                abs_d = abs(float(d))
                abs_m = float(m)
                if _d_min <= abs_d <= _d_max and _m_min <= abs_m <= _m_max:
                    dist = abs(abs_d - target_delta)
                    if dist < best_delta_dist:
                        best_delta_dist = dist
                        best_sym = sym
                        best_fields = fields
            if best_sym:
                abs_d = abs(float(best_fields.get("DELTA", 0)))
                return {
                    "symbol": best_sym,
                    "strike": None,
                    "mark":   round(float(best_fields.get("MARK", 0)), 2),
                    "delta":  round(abs_d, 2),
                    "iv":     round(float(best_fields.get("IMPL_VOL") or 0), 1),
                    "score":  None,
                    "source": "watch",
                }
            return None

        # EOD theta check — warn if theta > 15% of mark on recommended contract
        def _eod_theta_warn(contract_dict):
            if not contract_dict or not _is_eod:
                return ""
            sym = contract_dict.get("symbol")
            if not sym:
                return ""
            fields = chain_data.get("chain", {}).get(sym, {})
            theta = fields.get("THETA")
            mark  = fields.get("MARK")
            if theta and mark and mark > 0:
                ratio = abs(float(theta)) / float(mark)
                if ratio > 0.15:
                    return f"⚠ Theta {ratio:.0%} of mark — consider 1-2 DTE"
            return ""

        # Watch levels
        _watch_up = _watch_dn = ""
        if ms:
            if ms.snap_level and ms.snap_direction == "Above":
                _watch_up = f"Break above ${ms.snap_level:.0f} (snap) → dealers chase upside"
            elif ms.call_wall:
                _watch_up = f"Resistance at call wall ${ms.call_wall:.0f}"
            if ms.snap_level and ms.snap_direction == "Below":
                _watch_dn = f"Break below ${ms.snap_level:.0f} (snap) → dealers chase downside"
            elif ms.put_wall:
                _watch_dn = f"Support at put wall ${ms.put_wall:.0f}"

        _best_call = _best_contract("Call")
        _best_put  = _best_contract("Put")

        # ── Trend/structure conflict detection ──────────────────────────────
        # Conflict = momentum trend direction disagrees with structural lean
        _struct_conflict = False
        _conflict_note   = ""
        if _s_lean == "Bull" and _trend in ("Bearish", "BEARISH", "bearish"):
            _struct_conflict = True
            _conflict_note   = "Trend BEARISH but structure BULL — avoid longs"
        elif _s_lean == "Bear" and _trend in ("Bullish", "BULLISH", "bullish"):
            _struct_conflict = True
            _conflict_note   = "Trend BULLISH but structure BEAR — avoid shorts"

        briefing = {
            "trade_active":    _gate_pass,
            "no_trade_reason": _no_trade_reason,
            "structural_lean": _s_lean,
            "structural_conf": _s_conf,
            "regime":          _regime,
            "trend":           _trend,
            "snap_banner":     _ms_cl.snap_banner if _ms_cl else "",
            "watch_up":        _watch_up,
            "watch_dn":        _watch_dn,
            "best_call":       _best_call,
            "best_put":        _best_put,
            "eod_theta_warn":  _eod_theta_warn(_best_call if _s_lean == "Bull"
                                               else _best_put),
            "is_eod":          _is_eod,
            "invalidation":    ms.invalidation if ms else "",
            "struct_conflict": _struct_conflict,
            "conflict_note":   _conflict_note,
        }
    except Exception as e:
        logger.warning("Briefing error: %s", e)
        briefing = {"trade_active": False, "no_trade_reason": "Error computing briefing"}

    return {
        "tick":             tick,
        "timestamp":        timestamp,
        "symbol":           cfg.get("symbol", "SPY"),
        "price":            price,
        "bid":              price_data.get("bid"),
        "ask":              price_data.get("ask"),
        "mark":             price_data.get("mark"),
        "rtd_stale":        price_data.get("rtd_stale", False),
        "live_fields":      price_data.get("live_fields", 0),
        "expiry":           expiry,
        "test_mode":        price_data.get("test_mode", False),
        "max_pain":         max_pain,
        "call_wall":        call_wall,
        "put_wall":         put_wall,
        "market_structure": ms_dict,
        "candidates":       candidates_list,
        "strike_count":     len(strikes),
        "option_count":     len(option_symbols),
        "spy_vol_rate":     round(spy_vol_rate, 0),
        "spy_vol_ratio":    round(spy_vol_ratio, 2),
        "es_last":          es_last,
        "spx_last":         spx_last,
        "es_spy_ratio":     es_spy_ratio,
        "spx_spy_ratio":    spx_spy_ratio,
        "spx_es_ratio":     spx_es_ratio,
        "briefing":         briefing,
        "dte_mode":         _dte_mode,
        "next_1dte":        get_next_1dte(),
        "vix":              vix_signals,
        "ntick":            ntick,
        "trin":             trin_val,
        "trinq":            trinq_val,
        "add":              add_val,
        "qqq":              qqq_last,
        "iwm":              iwm_last,
        "nq":               nq_last,
        "channel":          channel_advisor.to_dict() if _channel_state else {},
        "paper_stats":      idea_logger.get_paper_stats(),
        "active_ideas":     idea_logger.get_active_ideas(),
        "positions":        positions,
        "chain":            {sym: {"LAST": chain_data.get("chain", {}).get(sym, {}).get("LAST")}
                            for sym in option_symbols},
        "chain_full":       {sym: {
                                "LAST":     fields.get("LAST"),
                                "BID":      fields.get("BID"),
                                "ASK":      fields.get("ASK"),
                                "DELTA":    fields.get("DELTA"),
                                "VOLUME":   fields.get("VOLUME"),
                                "OPEN_INT": fields.get("OPEN_INT"),
                                "IV":       fields.get("IMPL_VOL"),
                                "THETA":    fields.get("THETA"),
                                "GAMMA":    fields.get("GAMMA"),
                            }
                            for sym, fields in chain_data.get("chain", {}).items()},
    }

# ── Pydantic model for config POST ────────────────────────────────────────────
class ConfigUpdate(BaseModel):
    symbol:           Optional[str]   = None
    strike_range:     Optional[int]   = None
    wall_range:       Optional[int]   = None
    gex_range:        Optional[int]   = None
    strike_spacing:   Optional[float] = None
    expiry_date:      Optional[str]   = None
    warn_distance:    Optional[float] = None
    critical_distance:Optional[float] = None
    surge_window:     Optional[int]   = None
    surge_threshold:  Optional[float] = None
    ema_ticks:        Optional[int]   = None
    sma_ticks:        Optional[int]   = None
    poll_ms:          Optional[int]   = None
    test_mode:               Optional[bool]  = None
    test_date:               Optional[str]   = None
    risk_cap:                Optional[float] = None
    stop_pct:                Optional[float] = None
    reentry_window_min:      Optional[int]   = None
    vol_surge_ratio:         Optional[float] = None
    vol_lookback:            Optional[int]   = None
    score_decay_threshold:   Optional[int]   = None
    score_decay_ticks:       Optional[int]   = None
    confirm_score:           Optional[int]   = None
    confirm_ticks:           Optional[int]   = None
    confirm_ticks_surge:     Optional[int]   = None
    idea_cooldown_min:       Optional[int]   = None
    open_gate_minutes:       Optional[int]   = None
    iv_cap:                  Optional[float] = None
    vol_surge_mult:          Optional[float] = None
    alpaca_api_key:          Optional[str]   = None
    alpaca_secret_key:       Optional[str]   = None
    anthropic_api_key:       Optional[str]   = None
    paper_stop_pct:          Optional[float] = None
    paper_target_1_pct:      Optional[float] = None
    paper_target_2_pct:      Optional[float] = None
    paper_target_3_pct:      Optional[float] = None
    drop_threshold:          Optional[float] = None
    min_delta:               Optional[float] = None
    min_mark:                Optional[float] = None
    max_mark:                Optional[float] = None
    briefing_delta_min:      Optional[float] = None
    briefing_delta_max:      Optional[float] = None
    iv_floor:                Optional[float] = None
    iv_ceiling:              Optional[float] = None
    pinned_allowed_trends:   Optional[list]  = None
    pinned_max_score:        Optional[float] = None
    max_surface_score:       Optional[float] = None
    paper_stop_pct_pinned:   Optional[float] = None
    paper_starting_balance:  Optional[float] = None
    paper_risk_pct:          Optional[float] = None
    commission_per_contract: Optional[float] = None
    max_surface_candidates:  Optional[int]   = None
    post_stop_cooldown_sec:  Optional[int]   = None
    tick_history_enabled:    Optional[bool]  = None
    replay_db_path:          Optional[str]   = None
    momentum_short_ticks:    Optional[int]   = None
    momentum_medium_ticks:   Optional[int]   = None
    structure_gate_scanner:  Optional[bool]  = None
    no_trade_gate:            Optional[bool]  = None
    gate_hysteresis_ticks:    Optional[int]   = None
    briefing_eod_hour:        Optional[int]   = None

# ── FastAPI app ───────────────────────────────────────────────────────────────
app = FastAPI(title="tos-dash-v2")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

async def _watchdog():
    global _last_tick_seen, _last_tick_time
    import time
    STALE_FILE_SEC  = 60    # spy_price.json older than this → restart
    STALE_TICK_SEC  = 120   # tick unchanged for this long → restart
    CHECK_INTERVAL  = 30    # check every 30 seconds

    while True:
        await asyncio.sleep(CHECK_INTERVAL)
        try:
            # Check 1 — file timestamp staleness
            price_file = PRICE_FILE
            if price_file.exists():
                age = time.time() - price_file.stat().st_mtime
                if age > STALE_FILE_SEC:
                    logger.warning(
                        f"Watchdog: spy_price.json stale ({age:.0f}s) — restarting writer"
                    )
                    threading.Thread(target=start_writer, daemon=True).start()
                    continue

            # Check 2 — tick counter staleness
            snap = _last_snapshot
            if snap:
                tick = snap.get("tick", -1)
                now  = time.time()
                if tick != _last_tick_seen:
                    _last_tick_seen = tick
                    _last_tick_time = now
                elif _last_tick_time > 0 and (now - _last_tick_time) > STALE_TICK_SEC:
                    logger.warning(
                        f"Watchdog: tick frozen at {tick} for "
                        f"{now - _last_tick_time:.0f}s — restarting writer"
                    )
                    threading.Thread(target=start_writer, daemon=True).start()
                    _last_tick_time = now  # reset to avoid rapid restarts

            # Midnight refresh — reset 1DTE cache so next call recomputes
            from datetime import datetime as _dt
            if _dt.now().hour == 0 and _dt.now().minute < 1:
                global _next_1dte_date, _next_1dte_time
                _next_1dte_date = ""
                _next_1dte_time = 0.0

        except Exception as e:
            logger.error(f"Watchdog error: {e}")


@app.on_event("startup")
async def startup():
    get_next_1dte()   # warm 1DTE cache on boot
    start_writer()
    cfg = load_config()
    if cfg.get("tick_history_enabled", True):
        start_tick_recorder()
    asyncio.create_task(tick_loop())
    asyncio.create_task(_watchdog())
    news_fetcher.start(load_config)

@app.on_event("shutdown")
async def shutdown():
    stop_writer()
    stop_tick_recorder()
    news_fetcher.stop()

# ── REST endpoints ────────────────────────────────────────────────────────────
@app.get("/")
def serve_dashboard():
    try:
        return HTMLResponse(DASH_FILE.read_text(encoding="utf-8"), media_type="text/html; charset=utf-8")
    except Exception:
        return HTMLResponse("<h1>dashboard.html not found</h1>")

@app.get("/price")
def get_price():
    return read_json(PRICE_FILE)

@app.get("/chain")
def get_chain():
    return read_json(CHAIN_FILE)

@app.get("/snapshot")
def get_snapshot():
    # Return the last snapshot built by tick_loop to avoid double-advancing
    # advisor/tracker/logger state on every REST poll.
    return _last_snapshot if _last_snapshot else build_snapshot()

@app.get("/config")
def get_config():
    return load_config()

@app.post("/config")
def update_config(update: ConfigUpdate):
    cfg = load_config()
    for field, val in update.model_dump(exclude_none=True).items():
        cfg[field] = val
    save_config(cfg)
    # Apply EMA/SMA to advisor
    _reconfigure_advisor(cfg)
    # Restart writer with new config
    threading.Thread(target=start_writer, daemon=True).start()
    return {"status": "ok", "config": cfg}

@app.get("/paper/stats")
def get_paper_stats():
    """Return paper trading balance and daily P&L."""
    try:
        return idea_logger.get_paper_stats()
    except Exception as e:
        logger.error("Paper stats error: %s", e)
        return {"balance": 0, "daily_pnl": 0, "daily_count": 0}

@app.get("/writer/restart")
def restart_writer():
    threading.Thread(target=start_writer, daemon=True).start()
    return {"status": "restarting"}

@app.post("/tick-recorder/pause")
def tick_recorder_pause():
    try:
        (THIS_DIR / "tick_recorder.pause").write_text("pause")
    except Exception as e:
        return {"status": "error", "detail": str(e)}
    return {"status": "paused"}

@app.post("/tick-recorder/resume")
def tick_recorder_resume():
    try:
        (THIS_DIR / "tick_recorder.pause").unlink(missing_ok=True)
    except Exception as e:
        return {"status": "error", "detail": str(e)}
    return {"status": "resumed"}

@app.get("/tick-recorder/status")
def tick_recorder_status():
    alive    = tick_recorder_proc is not None and tick_recorder_proc.poll() is None
    paused   = (THIS_DIR / "tick_recorder.pause").exists()
    cfg      = load_config()
    db_dir   = cfg.get("replay_db_path", "D:/tos-dash-v2-replay/")
    db_path  = str(Path(db_dir) / "ticks.duckdb")
    return {"running": alive, "paused": paused, "db_path": db_path}

@app.get("/dte/next")
def dte_next():
    return {"next_1dte": get_next_1dte(), "dte_mode": _dte_mode}

@app.post("/dte/set")
def dte_set(mode: str):
    global _dte_mode
    if mode not in ("0DTE", "1DTE"):
        return {"error": f"Invalid mode '{mode}' — use '0DTE' or '1DTE'"}
    _dte_mode = mode
    new_expiry = get_next_1dte() if mode == "1DTE" else None
    _cfg = load_config()
    _cfg["expiry_date"] = new_expiry
    save_config(_cfg)
    threading.Thread(target=start_writer, daemon=True).start()
    logger.info("DTE switched to %s — expiry_date=%s, writer restarting", mode, new_expiry)
    return {"dte_mode": _dte_mode, "next_1dte": get_next_1dte(), "expiry_date": new_expiry}

@app.get("/status")
def get_status():
    price_ok = PRICE_FILE.exists()
    tick = None
    if price_ok:
        try:
            tick = json.loads(PRICE_FILE.read_text()).get("tick")
        except Exception:
            pass
    return {
        "status":        "ok" if price_ok else "no_data",
        "writer_alive":  writer_alive(),
        "writer_pid":    writer_proc.pid if writer_proc else None,
        "last_tick":     tick,
        "timestamp":     datetime.now().isoformat(),
    }

@app.get("/rtd/test")
def rtd_test(symbol: str):
    """Test if a symbol is returning RTD data. Reads current spy_price.json for known symbols,
    or returns the raw value from latest RTD data for unknown symbols."""
    try:
        price_data = read_json(PRICE_FILE)
        if "error" in price_data:
            return {"symbol": symbol, "value": None, "status": "no_data"}

        # Map common symbols to their spy_price.json keys
        symbol_map = {
            "$TICK":    "ntick_val",
            "TICK":     "ntick_val",
            "$TRIN":    "trin_val",
            "TRIN":     "trin_val",
            "$TRIN/Q":  "trinq_val",
            "TRIN/Q":   "trinq_val",
            "$ADD":     "add_val",
            "ADD":      "add_val",
            "VIX":      "vix_last",
            "$VIX":     "vix_last",
            "QQQ":      "qqq_last",
            "IWM":      "iwm_last",
            "/NQ:XCME": "nq_last",
            "/NQ":      "nq_last",
            "SPY":      "last",
        }
        key = symbol_map.get(symbol.upper().strip())
        if key:
            val = price_data.get(key)
            return {
                "symbol": symbol,
                "value":  val,
                "status": "live" if val is not None else "null",
                "key":    key,
            }
        return {"symbol": symbol, "value": None, "status": "unknown_symbol",
                "hint": f"Known symbols: {list(symbol_map.keys())}"}
    except Exception as e:
        return {"symbol": symbol, "value": None, "status": "error", "detail": str(e)}

# ── WebSocket ─────────────────────────────────────────────────────────────────
subscribers: list[WebSocket] = []

@app.websocket("/ws")
async def websocket_stream(ws: WebSocket):
    await ws.accept()
    subscribers.append(ws)
    logger.info(f"WS client connected ({len(subscribers)} total)")
    try:
        while True:
            await ws.receive_text()  # keep-alive
    except WebSocketDisconnect:
        pass
    finally:
        subscribers.remove(ws)
        logger.info(f"WS client disconnected ({len(subscribers)} total)")

async def tick_loop():
    global _last_snapshot
    last_tick = -1
    last_send = 0
    while True:
        try:
            snap = build_snapshot()
            _last_snapshot = snap   # cache for GET /snapshot
            tick = snap.get("tick", -1)
            now  = asyncio.get_event_loop().time()
            # Always build snapshot (keeps advisor/tracker state warm)
            # But only push to WebSocket every 2 seconds
            if tick != last_tick and not snap.get("error") and (now - last_send) >= 2.0:
                last_tick = tick
                last_send = now
                if subscribers:
                    msg = json.dumps(snap)
                    dead = []
                    for ws in subscribers:
                        try:
                            await ws.send_text(msg)
                        except Exception:
                            dead.append(ws)
                    for ws in dead:
                        subscribers.remove(ws)
        except Exception as e:
            logger.error(f"Tick loop error: {e}")
        await asyncio.sleep(0.5)

# ── Entry point ───────────────────────────────────────────────────────────────

# ── Charts endpoint ───────────────────────────────────────────────────────────
@app.get("/charts")
def get_charts():
    from gamma_chart import GammaChartBuilder, DeltaChartBuilder

    chain_data = read_json(CHAIN_FILE)
    price_data = read_json(PRICE_FILE)

    if "error" in chain_data or "error" in price_data:
        return {"error": "No data"}

    symbol         = price_data.get("symbol", "SPY")
    option_symbols = list(chain_data.get("chain", {}).keys())

    # Flatten data dict
    data = {}
    for sym, fields in chain_data.get("chain", {}).items():
        for qt_name, val in fields.items():
            data[f"{sym}:{qt_name}"] = val
    data[f"{symbol}:LAST"] = price_data.get("last")
    data[f"{symbol}:BID"]  = price_data.get("bid")
    data[f"{symbol}:ASK"]  = price_data.get("ask")
    data[f"{symbol}:MARK"] = price_data.get("mark")

    # Extract int strikes
    strikes = set()
    for sym in option_symbols:
        try:
            for sep in ("C", "P"):
                if sep in sym[1:]:
                    f = float(sym[1:].split(sep)[-1])
                    strikes.add(int(f) if f == int(f) else f)
                    break
        except (ValueError, IndexError):
            pass
    strikes = sorted(strikes)

    cfg        = load_config()
    wall_range = cfg.get("wall_range", 10)
    price_val  = price_data.get("last")

    # Limit chart bars to ±wall_range window — matches Streamlit strike_range behavior
    if price_val:
        price_f = float(price_val)
        chart_strike_set = {s for s in strikes if abs(s - price_f) <= wall_range}
        chart_strikes = sorted(chart_strike_set)
        chart_syms = [sym for sym in option_symbols
                      if any(f'C{s}' in sym or f'P{s}' in sym for s in chart_strike_set)]
    else:
        chart_strikes = strikes
        chart_syms    = option_symbols

    try:
        gex_fig = GammaChartBuilder(symbol).create_chart(
            data, chart_strikes, chart_syms,
            wall_range=wall_range, current_price_override=price_val,
        )
        gex_json = gex_fig.to_json()
    except Exception as e:
        logger.warning("GEX chart error: %s", e, exc_info=True)
        gex_json = None

    try:
        dex_fig = DeltaChartBuilder(symbol).create_chart(
            data, chart_strikes, chart_syms,
            wall_range=wall_range, current_price_override=price_val,
        )
        dex_json = dex_fig.to_json()
    except Exception as e:
        logger.warning("DEX chart error: %s", e, exc_info=True)
        dex_json = None

    return {"gex": gex_json, "dex": dex_json, "tick": chain_data.get("tick")}


@app.get("/ideas")
def get_ideas():
    try:
        return {
            "stats":  idea_logger.get_stats(),
            "rows":   idea_logger.get_all_ideas()[:300],
            "active": idea_logger.get_active_ideas(),
        }
    except Exception as e:
        logger.error("Ideas endpoint error: %s", e)
        return {"stats": {}, "rows": [], "active": []}


@app.post("/ideas/cleanup-hours")
def cleanup_outside_hours():
    """Delete ideas surfaced outside market hours (useful after OnDemand testing)."""
    cfg = load_config()
    dte = _dte_mode
    try:
        count = idea_logger.cleanup_outside_market_hours(dte_mode=dte)
        return {"status": "ok", "deleted": count, "dte_mode": dte}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


@app.get("/news")
def get_news():
    """Return cached market news headlines. Updates every 60s in background."""
    return news_fetcher.get_news()


@app.get("/news/config")
def get_news_config():
    """Return current news feed configuration."""
    return news_fetcher.get_config()


@app.post("/news/config")
async def update_news_config(request: Request):
    """Save news feed configuration. Takes effect on next fetch cycle (no restart needed)."""
    body = await request.json()
    news_fetcher.save_config(body)
    return {"status": "ok"}


@app.get("/log")
def get_log(lines: int = 100):
    """Return last N lines from the application log file."""
    from idea_logger import APP_LOG
    try:
        with open(APP_LOG, "r", encoding="utf-8") as f:
            all_lines = f.readlines()
        tail = [l.rstrip() for l in all_lines[-lines:]]
        return {"lines": tail, "total": len(all_lines)}
    except Exception as e:
        return {"lines": [f"Log unavailable: {e}"], "total": 0}


if __name__ == "__main__":
    logger.info(f"Starting tos-dash-v2 on http://127.0.0.1:8001")
    logger.info(f"Dashboard: http://127.0.0.1:8001/")
    logger.info(f"Config:    http://127.0.0.1:8001/config")
    uvicorn.run(app, host="127.0.0.1", port=8001, log_level="warning")
