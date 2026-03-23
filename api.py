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
import news_fetcher

volume_tracker  = VolumeTracker()
scalp_advisor   = ScalpAdvisor()
cfg_snapshot    = {}   # latest config, shared with idea_logger
_last_snapshot: dict = {}   # cached by tick_loop; served by GET /snapshot

# SPY volume tracker — rolling tick volume rate
_spy_vol_history: deque = deque(maxlen=50)
_spy_last_volume: float = 0.0

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
    "wall_range":     10,
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
    "confirm_score":         55,    # change #5: lowered from 65
    "confirm_ticks":         10,
    "confirm_ticks_surge":   5,
    # Change #1
    "idea_cooldown_min":     15,
    # Change #2
    "vol_surge_mult":        1.5,
    # Change #4
    "iv_cap":                35.0,   # 0 = disabled
    # Change #6
    "open_gate_minutes":     30,
    "rtd_heartbeat_ms":      200,   # Step 2: replaces config.yaml timing.initial_heartbeat
    "alpaca_api_key":        "",
    "alpaca_secret_key":     "",
    "paper_stop_pct":        0.30,
    "paper_target_1_pct":    0.30,
    "paper_target_2_pct":    0.50,
    "paper_target_3_pct":    0.75,
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
    global writer_proc
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

# ── Helpers ───────────────────────────────────────────────────────────────────
def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception as e:
        return {"error": str(e)}

def build_snapshot() -> dict:
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

    # Wall strikes — wider range for accurate wall/max_pain calculation
    # Uses all symbols in chain (spy_writer subscribes to wall_range)
    wall_strikes = strikes  # same set — spy_writer controls what's in chain
    price_val = float(price) if price else 0
    wall_range = cfg.get("wall_range", 25)
    if price_val:
        wall_strikes = [s for s in strikes
                        if abs(s - price_val) <= wall_range]
    # Build wall_option_symbols using exact strike matching (not substring)
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
            call_wall, put_wall = calculate_walls(data, wall_strikes, wall_option_symbols, debug=False)
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
            strikes           = strikes,
            option_symbols    = option_symbols,
            current_price     = price,
            max_pain          = max_pain or price,
            call_wall         = call_wall,
            put_wall          = put_wall,
            warn_distance     = cfg.get("warn_distance", 2.0),
            critical_distance = cfg.get("critical_distance", 1.0),
            surge_symbols     = surge_syms,
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
        }
    except Exception as e:
        ms_dict = {"error": str(e)}

    try:
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
            cfg            = cfg,
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
        )
        # Position auto-linking
        if positions:
            idea_logger.process_positions(positions)

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

    return {
        "tick":             tick,
        "timestamp":        timestamp,
        "symbol":           cfg.get("symbol", "SPY"),
        "price":            price,
        "bid":              price_data.get("bid"),
        "ask":              price_data.get("ask"),
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
    paper_stop_pct:          Optional[float] = None
    paper_target_1_pct:      Optional[float] = None
    paper_target_2_pct:      Optional[float] = None
    paper_target_3_pct:      Optional[float] = None

# ── FastAPI app ───────────────────────────────────────────────────────────────
app = FastAPI(title="tos-dash-v2")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
async def startup():
    start_writer()
    asyncio.create_task(tick_loop())
    news_fetcher.start(load_config)

@app.on_event("shutdown")
async def shutdown():
    stop_writer()
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

@app.get("/writer/restart")
def restart_writer():
    threading.Thread(target=start_writer, daemon=True).start()
    return {"status": "restarting"}

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
