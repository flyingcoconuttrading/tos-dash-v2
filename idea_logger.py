# tos-dash-v2/idea_logger.py
"""
IdeaLogger - full lifecycle tracking for scalp advisor ideas.
ACTIVE -> WEAKENING -> CONFIRMED -> INVALIDATED / EXPIRED

Invalidation rules (priority order):
  1. LEVEL_CROSS        - SPY crosses nearest wall (immediate)
  2. VOL_CONFIRMED_MOVE - delta-adjusted SPY move WITH volume surge (immediate)  
  3. UNCONFIRMED_MOVE   - same move, low volume -> WEAKENING first
  4. SCORE_DECAY        - score < threshold for N consecutive ticks
  5. TIME_EXPIRED       - idea > 30 min, not confirmed

Re-entry: same symbol within reentry_window_min -> same idea ID, new event logged
Position auto-link: RTD POSITION_QTY/AV_TRADE_PRICE auto-linked to matching active idea

Files written:
  data/ideas.db     - SQLite (ideas + idea_events tables)
  data/ideas.csv    - one row per idea, all columns
  data/events.csv   - full event audit trail
  data/alerts.csv   - snap/warning alerts
  logs/tos_dash.log - rotating app log
"""

import csv
import logging
import logging.handlers
import sqlite3
import threading
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Optional

THIS_DIR   = Path(__file__).parent
DATA_DIR   = THIS_DIR / "data"
LOG_DIR    = THIS_DIR / "logs"
DB_PATH    = DATA_DIR / "ideas.db"
IDEAS_CSV  = DATA_DIR / "ideas.csv"
EVENTS_CSV = DATA_DIR / "events.csv"
ALERTS_CSV = DATA_DIR / "alerts.csv"
APP_LOG    = LOG_DIR  / "tos_dash.log"

MODEL_VERSION = "v2.1"

IDEA_HEADERS = [
    "id", "model_version",
    "symbol", "strike", "option_type", "direction",
    "surfaced_at",
    "entry_score", "entry_mark", "entry_bid", "entry_ask",
    "entry_delta", "entry_theta", "entry_iv", "entry_volume",
    "entry_spy", "entry_spy_vol_rate", "entry_spy_vol_ratio",
    "entry_trend", "entry_regime", "entry_bias",
    "entry_net_gex", "entry_net_dex",
    "entry_call_wall", "entry_put_wall", "entry_max_pain", "entry_gex_anchor",
    "entry_surge",
    "status",
    "confirmed_at", "confirmed_score", "confirmed_spy",
    "invalidated_at", "invalidation_reason",
    "invalidation_spy", "invalidation_mark",
    "spy_move_at_invalidation", "vol_confirmed",
    "reentry_count", "last_reentry_at",
    "out_5m_mark",  "out_5m_pnl_pct",  "out_5m_correct",
    "out_10m_mark", "out_10m_pnl_pct", "out_10m_correct",
    "out_15m_mark", "out_15m_pnl_pct", "out_15m_correct",
    "out_30m_mark", "out_30m_pnl_pct", "out_30m_correct",
    "out_1m_mark",  "out_1m_pnl_pct",  "out_1m_correct",
    "out_2m_mark",  "out_2m_pnl_pct",  "out_2m_correct",
    "out_3m_mark",  "out_3m_pnl_pct",  "out_3m_correct",
    "out_4m_mark",  "out_4m_pnl_pct",  "out_4m_correct",
    "traded", "trade_fill_price", "trade_qty", "trade_pnl_pct",
]
EVENT_HEADERS = ["id","idea_id","event_time","event_type","score","mark","spy","spy_move","vol_ratio","detail"]
ALERT_HEADERS = ["id","logged_at","alert_zone","alert_message","spy_price","snap_level","snap_distance","snap_direction","regime","bias","net_gex","net_dex"]

STATUS_ACTIVE      = "ACTIVE"
STATUS_WEAKENING   = "WEAKENING"
STATUS_CONFIRMED   = "CONFIRMED"
STATUS_INVALIDATED = "INVALIDATED"
STATUS_EXPIRED     = "EXPIRED"

INVALIDATION_LEVEL_CROSS   = "LEVEL_CROSS"
INVALIDATION_VOL_CONFIRMED = "VOL_CONFIRMED_MOVE"
INVALIDATION_SCORE_DECAY   = "SCORE_DECAY"
INVALIDATION_TIME          = "TIME_EXPIRED"
INVALIDATION_MANUAL        = "MANUAL"


def setup_app_logging() -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(pathname)s:%(lineno)d - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    log = logging.getLogger("tos_dash")
    log.setLevel(logging.DEBUG)
    if not log.handlers:
        fh = logging.handlers.RotatingFileHandler(APP_LOG, maxBytes=5*1024*1024, backupCount=5, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        log.addHandler(fh)
        ch = logging.StreamHandler()
        ch.setLevel(logging.INFO)
        ch.setFormatter(fmt)
        log.addHandler(ch)
    return log


class IdeaState:
    __slots__ = ["idea_id","symbol","strike","option_type","direction","status",
                 "surfaced_at","entry_spy","entry_mark","entry_delta",
                 "entry_call_wall","entry_put_wall","score_history",
                 "decay_ticks","confirm_ticks_count","confirm_ticks_target",
                 "last_seen_at","reentry_count"]

    def __init__(self, idea_id, candidate, spy_price, call_wall, put_wall):
        self.idea_id              = idea_id
        self.symbol               = candidate.symbol
        self.strike               = float(candidate.strike)
        self.option_type          = candidate.option_type
        self.direction            = getattr(candidate, "direction", "")
        self.status               = STATUS_ACTIVE
        self.surfaced_at          = datetime.now()
        self.entry_spy            = spy_price
        self.entry_mark           = float(candidate.mark)
        self.entry_delta          = abs(float(candidate.delta))
        self.entry_call_wall      = call_wall
        self.entry_put_wall       = put_wall
        self.score_history        = deque(maxlen=30)
        self.decay_ticks          = 0
        self.confirm_ticks_count  = 0
        self.confirm_ticks_target = 10   # overwritten immediately after construction
        self.last_seen_at         = datetime.now()
        self.reentry_count        = 0


class IdeaLogger:
    """Full lifecycle logger for scalp advisor ideas. Thread-safe. Call on every tick."""

    def __init__(self, cfg: dict = None):
        self._lock     = threading.Lock()
        self._log      = logging.getLogger("tos_dash.idea_logger")
        self._cfg      = cfg or {}
        self._alert_id = 0
        self._event_id = 0
        self._active: dict[str, IdeaState] = {}
        self._recently_invalidated: dict[str, tuple] = {}
        self._known_positions: dict[str, float] = {}  # symbol -> qty
        self._ensure_storage()
        self._log.info("IdeaLogger %s initialised — %s", MODEL_VERSION, DB_PATH)

        # Alert dedup — zone-level and per-snap-level cooldowns
        self._last_alert_zone: str   = "normal"
        self._last_alert_time: float = 0.0
        self._alert_cooldown_s: int  = 60    # min seconds between same-zone alerts
        self._alert_snap_times: dict = {}    # snap_level -> last fire monotonic time
        self._snap_alert_cooldown_s  = 300   # 5 min per snap level

    def update_cfg(self, cfg: dict):
        with self._lock:
            self._cfg = cfg

    # ── Main tick entry point ─────────────────────────────────────────────────

    def process_tick(self, candidates, data, spy_price, spy_vol_rate, spy_vol_ratio, ms, surge_syms):
        now       = datetime.now()
        cand_map  = {c.symbol: c for c in (candidates or [])}
        call_wall = getattr(ms, "call_wall", None)
        put_wall  = getattr(ms, "put_wall", None)

        # 1. New ideas + re-entries
        for sym, c in cand_map.items():
            key = self._key(c)
            if key in self._active:
                self._active[key].last_seen_at = now
                continue
            prev = self._recently_invalidated.get(key)
            if prev:
                idea_id, inv_at = prev
                window_sec = self._cfg.get("reentry_window_min", 5) * 60
                if (now - inv_at).total_seconds() < window_sec:
                    self._handle_reentry(idea_id, key, c, spy_price, now, spy_vol_ratio)
                    continue
                else:
                    del self._recently_invalidated[key]
            self._surface_new_idea(key, c, spy_price, spy_vol_rate, spy_vol_ratio, ms, surge_syms, now)

        # 2. Update lifecycle for all active ideas
        for key in list(self._active.keys()):
            state = self._active[key]
            c     = cand_map.get(state.symbol)
            score = float(c.score) if c else None
            mark  = float(c.mark)  if c else None
            if score is not None:
                state.score_history.append(score)
            self._update_lifecycle(key, state, c, score, mark, spy_price,
                                   spy_vol_rate, spy_vol_ratio, call_wall, put_wall, now, data)

        # 3. Outcome filling
        self._fill_outcomes(data, now)

        # 4. Alerts — two-level dedup:
        #    - zone level:      same zone suppressed for alert_cooldown_s (60s); zone change has 10s floor
        #    - snap level:      same snap_level suppressed for snap_alert_cooldown_s (300s = 5 min)
        #                       repeat snap fires are logged at DEBUG, not WARNING, and not written to CSV
        if ms and getattr(ms, "alert_zone", "normal") != "normal":
            import time as _time
            now_ts       = _time.monotonic()
            new_zone     = ms.alert_zone
            snap_lv      = getattr(ms, "snap_level", "") or ""
            elapsed      = now_ts - self._last_alert_time
            zone_changed = new_zone != self._last_alert_zone
            zone_cooldown = 10 if zone_changed else self._alert_cooldown_s
            if elapsed >= zone_cooldown:
                snap_last    = self._alert_snap_times.get(snap_lv, 0.0)
                snap_elapsed = now_ts - snap_last
                if snap_elapsed >= self._snap_alert_cooldown_s:
                    self._log_alert(ms)
                    self._alert_snap_times[snap_lv] = now_ts
                else:
                    self._log.debug(
                        "ALERT repeat suppressed [%s] snap=%s (%.0fs / %ds cooldown)",
                        new_zone, snap_lv, snap_elapsed, self._snap_alert_cooldown_s,
                    )
                self._last_alert_zone = new_zone
                self._last_alert_time = now_ts

    # ── Position monitoring ───────────────────────────────────────────────────

    def process_positions(self, positions: dict):
        """
        Called every tick with positions dict from positions.json.
        positions = {symbol: {qty, av_trade_price, mark, ...}}
        Auto-links new positions to active ideas, detects closures.
        """
        current_syms = set(positions.keys())
        prev_syms    = set(self._known_positions.keys())

        # New positions
        for sym in current_syms - prev_syms:
            pos = positions[sym]
            qty   = pos.get("qty", 0)
            price = pos.get("av_trade_price")
            if qty and price:
                self._link_position(sym, qty, price)

        # Closed positions
        for sym in prev_syms - current_syms:
            prev_qty = self._known_positions.get(sym, 0)
            if prev_qty:
                # Get last known mark
                mark = None
                for key, state in self._active.items():
                    if state.symbol == sym:
                        mark = state.entry_mark
                        break
                self._close_position(sym, mark or 0)

        self._known_positions = {s: positions[s].get("qty", 0) for s in current_syms}

    def _link_position(self, symbol: str, qty: float, fill_price: float):
        idea_id = self._find_idea_for_symbol(symbol)
        if idea_id:
            with self._connect() as conn:
                conn.execute(
                    "UPDATE ideas SET traded=1, trade_fill_price=?, trade_qty=? WHERE id=?",
                    (fill_price, qty, idea_id)
                )
            self._log_event(idea_id, "TRADE_LINKED", mark=fill_price,
                            detail=f"qty={qty}  fill={fill_price:.2f}")
            self._log.info("IDEA #%d TRADE_LINKED  %s  qty=%s  fill=%.2f",
                           idea_id, symbol, qty, fill_price)
        else:
            self._log.debug("Position detected, no active idea: %s qty=%s fill=%.2f",
                            symbol, qty, fill_price)

    def _close_position(self, symbol: str, exit_price: float):
        idea_id = self._find_idea_for_symbol(symbol)
        if not idea_id:
            self._log.info("POSITION_CLOSED (no idea)  %s  exit=%.2f", symbol, exit_price)
            return
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT trade_fill_price FROM ideas WHERE id=?",
                               (idea_id,)).fetchone()
            fill    = row["trade_fill_price"] if row else None
            pnl_pct = ((exit_price - fill) / fill * 100) if fill and fill > 0 else None
            conn.execute("UPDATE ideas SET trade_pnl_pct=? WHERE id=?", (pnl_pct, idea_id))
        # Remove from _active so it no longer shows as an open idea
        keys_to_remove = [k for k, s in self._active.items() if s.symbol == symbol]
        for k in keys_to_remove:
            del self._active[k]
        self._log_event(idea_id, "POSITION_CLOSED", mark=exit_price,
                        detail=f"exit={exit_price:.2f}  pnl={pnl_pct:.1f}%" if pnl_pct else f"exit={exit_price:.2f}")
        self._log.info("IDEA #%d POSITION_CLOSED  %s  exit=%.2f  pnl=%s",
                       idea_id, symbol, exit_price,
                       f"{pnl_pct:.1f}%" if pnl_pct else "unknown")

    def _find_idea_for_symbol(self, symbol: str) -> Optional[int]:
        for key, state in self._active.items():
            if state.symbol == symbol:
                return state.idea_id
        for key, (iid, _) in self._recently_invalidated.items():
            if key.startswith(symbol + ":"):
                return iid
        return None

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _conf_ticks(self, spy_vol_ratio: float) -> int:
        """Return the confirmation tick target locked at surface/re-entry time."""
        vol_surge = spy_vol_ratio >= self._cfg.get("vol_surge_ratio", 1.8)
        return (self._cfg.get("confirm_ticks_surge", 5)
                if vol_surge else self._cfg.get("confirm_ticks", 10))

    # ── Surfacing ─────────────────────────────────────────────────────────────

    def _surface_new_idea(self, key, candidate, spy_price, spy_vol_rate, spy_vol_ratio, ms, surge_syms, now):
        call_wall = getattr(ms, "call_wall", None)
        put_wall  = getattr(ms, "put_wall", None)
        row = {
            "model_version":       MODEL_VERSION,
            "symbol":              candidate.symbol,
            "strike":              float(candidate.strike),
            "option_type":         candidate.option_type,
            "direction":           getattr(candidate, "direction", None),
            "surfaced_at":         now.isoformat(timespec="seconds"),
            "entry_score":         float(candidate.score),
            "entry_mark":          float(candidate.mark),
            "entry_bid":           float(candidate.bid),
            "entry_ask":           float(candidate.ask),
            "entry_delta":         float(candidate.delta),
            "entry_theta":         float(candidate.theta),
            "entry_iv":            float(candidate.iv),
            "entry_volume":        float(candidate.volume),
            "entry_spy":           spy_price,
            "entry_spy_vol_rate":  spy_vol_rate,
            "entry_spy_vol_ratio": spy_vol_ratio,
            "entry_trend":         getattr(candidate, "underlying_trend", None),
            "entry_regime":        getattr(ms, "regime", None),
            "entry_bias":          getattr(ms, "bias", None),
            "entry_net_gex":       getattr(ms, "net_gex", None),
            "entry_net_dex":       getattr(ms, "net_dex", None),
            "entry_call_wall":     call_wall,
            "entry_put_wall":      put_wall,
            "entry_max_pain":      getattr(ms, "max_pain", None),
            "entry_gex_anchor":    getattr(ms, "gex_anchor", None),
            "entry_surge":         1 if candidate.symbol in surge_syms else 0,
            "status":              STATUS_ACTIVE,
            "reentry_count":       0,
            "traded":              0,
        }
        idea_id = self._db_insert_idea(row)
        self._csv_append_idea({**{k: "" for k in IDEA_HEADERS}, **row, "id": idea_id})
        state = IdeaState(idea_id, candidate, spy_price, call_wall, put_wall)
        state.confirm_ticks_target = self._conf_ticks(spy_vol_ratio)
        self._active[key] = state
        self._log_event(idea_id, "SURFACED", score=candidate.score, mark=candidate.mark,
                        spy=spy_price, vol_ratio=spy_vol_ratio,
                        detail=f"score={candidate.score:.1f} mark={candidate.mark:.2f} "
                               f"delta={candidate.delta:.2f} trend={getattr(candidate,'underlying_trend','')} "
                               f"conf_ticks_target={state.confirm_ticks_target}")
        _surge_at_surface = spy_vol_ratio >= self._cfg.get("vol_surge_ratio", 1.8)
        self._log.info(
            "IDEA #%d SURFACED  %s  score=%.1f  mark=%.2f  spy=%.2f  delta=%.2f"
            "  conf_ticks=%d(%s)  vol_ratio=%.1f",
            idea_id, candidate.symbol, candidate.score,
            candidate.mark, spy_price, candidate.delta,
            state.confirm_ticks_target,
            "surge" if _surge_at_surface else "normal",
            spy_vol_ratio,
        )

    def _handle_reentry(self, idea_id, key, candidate, spy_price, now, spy_vol_ratio=0.0):
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM ideas WHERE id=?", (idea_id,)).fetchone()
            if not row:
                return
            reentry_count = (row["reentry_count"] or 0) + 1
            conn.execute(
                "UPDATE ideas SET reentry_count=?, last_reentry_at=?, status=? WHERE id=?",
                (reentry_count, now.isoformat(timespec="seconds"), STATUS_ACTIVE, idea_id)
            )
        state = IdeaState(idea_id, candidate, spy_price, row["entry_call_wall"], row["entry_put_wall"])
        state.reentry_count        = reentry_count
        state.entry_mark           = row["entry_mark"]
        state.entry_spy            = row["entry_spy"]
        state.confirm_ticks_target = self._conf_ticks(spy_vol_ratio)
        self._active[key]          = state
        del self._recently_invalidated[key]
        _surge_at_reentry = spy_vol_ratio >= self._cfg.get("vol_surge_ratio", 1.8)
        self._log_event(idea_id, "REENTRY", score=candidate.score, mark=candidate.mark,
                        spy=spy_price,
                        detail=f"reentry #{reentry_count}  score={candidate.score:.1f}"
                               f"  conf_ticks={state.confirm_ticks_target}"
                               f"({'surge' if _surge_at_reentry else 'normal'})")
        self._log.info(
            "IDEA #%d REENTRY #%d  %s  score=%.1f  spy=%.2f"
            "  conf_ticks=%d(%s)  vol_ratio=%.1f",
            idea_id, reentry_count, candidate.symbol, candidate.score, spy_price,
            state.confirm_ticks_target,
            "surge" if _surge_at_reentry else "normal",
            spy_vol_ratio,
        )

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def _update_lifecycle(self, key, state, candidate, score, mark,
                          spy_price, spy_vol_rate, spy_vol_ratio,
                          call_wall, put_wall, now, data):
        cfg = self._cfg

        # Time expiry — unconfirmed ideas expire at 30 min; confirmed ideas get
        # a 60-min hard ceiling so stale entries don't accumulate indefinitely.
        age_min = (now - state.surfaced_at).total_seconds() / 60
        if age_min > 60 or (age_min > 30 and state.status not in (STATUS_CONFIRMED,)):
            self._invalidate(key, state, INVALIDATION_TIME, spy_price,
                             mark or state.entry_mark, spy_vol_ratio, now,
                             detail=f"age={age_min:.1f}min > {'60' if age_min > 60 else '30'}min limit")
            return

        # Level cross — use ENTRY walls (walls at time of surface), not current walls.
        # Current walls can shift and cause false invalidations.
        ew_call = state.entry_call_wall
        ew_put  = state.entry_put_wall
        if ew_call and ew_put and state.option_type == "Call" and spy_price < ew_put:
            self._invalidate(key, state, INVALIDATION_LEVEL_CROSS, spy_price,
                             mark or state.entry_mark, spy_vol_ratio, now,
                             detail=f"SPY={spy_price:.2f} crossed entry put_wall={ew_put:.0f}")
            return
        if ew_call and ew_put and state.option_type == "Put" and spy_price > ew_call:
            self._invalidate(key, state, INVALIDATION_LEVEL_CROSS, spy_price,
                             mark or state.entry_mark, spy_vol_ratio, now,
                             detail=f"SPY={spy_price:.2f} crossed entry call_wall={ew_call:.0f}")
            return

        # Delta-adjusted SPY move
        stop_pct  = cfg.get("stop_pct", 0.50)
        delta     = state.entry_delta or 0.40
        threshold = (state.entry_mark * stop_pct) / delta
        spy_move  = spy_price - state.entry_spy
        move_against = -spy_move if state.option_type == "Call" else spy_move
        vol_surge = spy_vol_ratio >= cfg.get("vol_surge_ratio", 1.8)

        if move_against > threshold:
            if vol_surge:
                self._invalidate(key, state, INVALIDATION_VOL_CONFIRMED, spy_price,
                                 mark or state.entry_mark, spy_vol_ratio, now,
                                 detail=f"move_against={move_against:.2f} threshold={threshold:.2f} "
                                        f"vol_ratio={spy_vol_ratio:.1f} confirmed")
                return
            elif state.status == STATUS_ACTIVE:
                state.status = STATUS_WEAKENING
                self._db_update_status(state.idea_id, STATUS_WEAKENING)
                self._log_event(state.idea_id, "WEAKENING", score=score, mark=mark,
                                spy=spy_price, spy_move=spy_move, vol_ratio=spy_vol_ratio,
                                detail=f"move_against={move_against:.2f} threshold={threshold:.2f} unconfirmed")
                self._log.debug("IDEA #%d WEAKENING  %s  move_against=%.2f  vol_ratio=%.1f",
                                state.idea_id, state.symbol, move_against, spy_vol_ratio)

        # Score decay
        if score is not None:
            decay_thr  = cfg.get("score_decay_threshold", 45)
            decay_max  = cfg.get("score_decay_ticks", 10)
            state.decay_ticks = state.decay_ticks + 1 if score < decay_thr else 0
            if state.decay_ticks >= decay_max:
                self._invalidate(key, state, INVALIDATION_SCORE_DECAY, spy_price,
                                 mark or state.entry_mark, spy_vol_ratio, now,
                                 detail=f"score={score:.1f} < {decay_thr} for {state.decay_ticks} ticks")
                return

        # Confirmation — conf_ticks_target is locked at surface/re-entry time,
        # never recalculated per tick, so the bar doesn't shift under an idea mid-life.
        if score is not None and state.status in (STATUS_ACTIVE, STATUS_WEAKENING):
            conf_score = cfg.get("confirm_score", 52)
            conf_ticks = state.confirm_ticks_target   # fixed at surface/re-entry
            if score >= conf_score:
                state.confirm_ticks_count += 1
                self._log.debug(
                    "IDEA #%d CONFIRM_PROGRESS  %s  score=%.1f >= %.0f  ticks=%d/%d",
                    state.idea_id, state.symbol, score, conf_score,
                    state.confirm_ticks_count, conf_ticks,
                )
            else:
                if state.confirm_ticks_count > 0:
                    self._log.debug(
                        "IDEA #%d CONFIRM_RESET  %s  score=%.1f < %.0f  was=%d ticks",
                        state.idea_id, state.symbol, score, conf_score,
                        state.confirm_ticks_count,
                    )
                state.confirm_ticks_count = 0
            if state.confirm_ticks_count >= conf_ticks and state.status != STATUS_CONFIRMED:
                state.status = STATUS_CONFIRMED
                self._db_confirm(state.idea_id, score, spy_price, now)
                self._log_event(state.idea_id, "CONFIRMED", score=score, mark=mark,
                                spy=spy_price,
                                detail=f"score={score:.1f} >= {conf_score} for {conf_ticks} ticks")
                self._log.info("IDEA #%d CONFIRMED (score)  %s  score=%.1f >= %.0f  ticks=%d  spy=%.2f",
                               state.idea_id, state.symbol, score, conf_score,
                               conf_ticks, spy_price)

    def _invalidate(self, key, state, reason, spy_price, mark, vol_ratio, now, detail=""):
        spy_move = spy_price - state.entry_spy
        vol_conf = 1 if vol_ratio >= self._cfg.get("vol_surge_ratio", 1.8) else 0
        with self._connect() as conn:
            conn.execute("""
                UPDATE ideas SET status=?, invalidated_at=?, invalidation_reason=?,
                    invalidation_spy=?, invalidation_mark=?,
                    spy_move_at_invalidation=?, vol_confirmed=?
                WHERE id=?
            """, (STATUS_INVALIDATED, now.isoformat(timespec="seconds"), reason,
                  spy_price, mark, spy_move, vol_conf, state.idea_id))
        self._log_event(state.idea_id, "INVALIDATED", mark=mark, spy=spy_price,
                        spy_move=spy_move, vol_ratio=vol_ratio, detail=f"{reason} - {detail}")
        self._log.info("IDEA #%d INVALIDATED  %s  reason=%s  spy_move=%.2f  vol=%d  %s",
                       state.idea_id, state.symbol, reason, spy_move, vol_conf, detail)
        self._recently_invalidated[key] = (state.idea_id, now)
        del self._active[key]

    # ── Outcome filling ───────────────────────────────────────────────────────

    def _fill_outcomes(self, data: dict, now: datetime):
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            pending = conn.execute("""
                SELECT id, symbol, surfaced_at, entry_mark, option_type
                FROM ideas
                WHERE surfaced_at >= datetime('now', '-35 minutes')
                  AND (out_1m_mark IS NULL OR out_2m_mark IS NULL
                    OR out_3m_mark IS NULL OR out_4m_mark IS NULL
                    OR out_5m_mark IS NULL OR out_10m_mark IS NULL
                    OR out_15m_mark IS NULL OR out_30m_mark IS NULL)
            """).fetchall()

        for row in pending:
            logged_at   = datetime.fromisoformat(row["surfaced_at"])
            elapsed_min = (now - logged_at).total_seconds() / 60
            mark_raw    = data.get(f"{row['symbol']}:MARK") or data.get(f"{row['symbol']}:LAST")
            try:
                current = float(mark_raw) if mark_raw else None
            except (ValueError, TypeError):
                current = None
            if current is None:
                continue
            entry   = row["entry_mark"]
            pnl_pct = ((current - entry) / entry * 100) if entry and entry > 0 else None
            correct = None
            if pnl_pct is not None:
                # Both calls and puts gain value (pnl_pct > 0) when the underlying
                # moves in the predicted direction, so correct = option appreciated.
                correct = 1 if pnl_pct > 0 else 0
            for w in [1, 2, 3, 4, 5, 10, 15, 30]:
                if elapsed_min >= w:
                    col = f"out_{w}m_mark"
                    with self._connect() as conn:
                        conn.row_factory = sqlite3.Row
                        existing = conn.execute(f"SELECT {col} FROM ideas WHERE id=?",
                                                (row["id"],)).fetchone()
                        if existing and existing[col] is None:
                            conn.execute(f"""
                                UPDATE ideas SET {col}=?, out_{w}m_pnl_pct=?, out_{w}m_correct=?
                                WHERE id=?
                            """, (current, pnl_pct, correct, row["id"]))
                            self._log_event(row["id"], "OUTCOME_FILLED", mark=current,
                                            detail=f"{w}m mark={current:.2f} pnl={pnl_pct:.1f}% correct={correct}",
                                            conn=conn)

    # ── Public read API ───────────────────────────────────────────────────────

    def get_active_ideas(self) -> list:
        with self._lock:
            return [{
                "idea_id":       state.idea_id,
                "symbol":        state.symbol,
                "strike":        state.strike,
                "option_type":   state.option_type,
                "direction":     state.direction,
                "status":        state.status,
                "surfaced_at":   state.surfaced_at.isoformat(timespec="seconds"),
                "entry_mark":    state.entry_mark,
                "entry_spy":     state.entry_spy,
                "decay_ticks":   state.decay_ticks,
                "reentry_count": state.reentry_count,
                "age_min":       round((datetime.now() - state.surfaced_at).total_seconds()/60, 1),
            } for state in self._active.values()]

    def get_all_ideas(self) -> list:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM ideas ORDER BY surfaced_at DESC LIMIT 500").fetchall()
        return [dict(r) for r in rows]

    def get_stats(self) -> dict:
        rows  = self.get_all_ideas()
        stats = {}
        for w in [5, 10, 15, 30]:
            filled = [r for r in rows if r.get(f"out_{w}m_correct") is not None]
            if not filled:
                stats[f"{w}m"] = {"hit_rate": None, "avg_pnl": None, "count": 0}
                continue
            hits    = sum(1 for r in filled if r[f"out_{w}m_correct"])
            pnls    = [r[f"out_{w}m_pnl_pct"] for r in filled if r.get(f"out_{w}m_pnl_pct") is not None]
            stats[f"{w}m"] = {
                "hit_rate": hits / len(filled) * 100,
                "avg_pnl":  sum(pnls)/len(pnls) if pnls else None,
                "count":    len(filled),
            }
        inv_stats: dict = {}
        for r in rows:
            reason = r.get("invalidation_reason") or "ACTIVE"
            if reason not in inv_stats:
                inv_stats[reason] = {"count": 0, "vol_confirmed": 0}
            inv_stats[reason]["count"] += 1
            if r.get("vol_confirmed"):
                inv_stats[reason]["vol_confirmed"] += 1
        stats["by_invalidation"] = inv_stats
        trend_stats: dict = {}
        for r in rows:
            t = r.get("entry_trend") or "Unknown"
            if t not in trend_stats:
                trend_stats[t] = {"hits": 0, "total": 0, "pnl": []}
            correct = r.get("out_15m_correct")
            pnl     = r.get("out_15m_pnl_pct")
            if correct is not None:
                trend_stats[t]["total"] += 1
                if correct: trend_stats[t]["hits"] += 1
                if pnl is not None: trend_stats[t]["pnl"].append(pnl)
        for t, s in trend_stats.items():
            s["hit_rate"] = s["hits"]/s["total"]*100 if s["total"] else None
            s["avg_pnl"]  = sum(s["pnl"])/len(s["pnl"]) if s["pnl"] else None
        stats["by_trend"]          = trend_stats
        stats["traded_count"]      = sum(1 for r in rows if r.get("traded"))
        stats["not_traded_count"]  = sum(1 for r in rows if not r.get("traded"))
        return stats

    def get_alerts(self) -> list:
        if not ALERTS_CSV.exists():
            return []
        try:
            with open(ALERTS_CSV, "r", newline="", encoding="utf-8") as f:
                return list(csv.DictReader(f))
        except Exception:
            return []

    # ── Private DB helpers ────────────────────────────────────────────────────

    def _ensure_storage(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS ideas (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    model_version TEXT, symbol TEXT NOT NULL, strike REAL,
                    option_type TEXT, direction TEXT, surfaced_at TEXT NOT NULL,
                    entry_score REAL, entry_mark REAL, entry_bid REAL, entry_ask REAL,
                    entry_delta REAL, entry_theta REAL, entry_iv REAL, entry_volume REAL,
                    entry_spy REAL, entry_spy_vol_rate REAL, entry_spy_vol_ratio REAL,
                    entry_trend TEXT, entry_regime TEXT, entry_bias TEXT,
                    entry_net_gex REAL, entry_net_dex REAL,
                    entry_call_wall REAL, entry_put_wall REAL,
                    entry_max_pain REAL, entry_gex_anchor REAL, entry_surge INTEGER,
                    status TEXT DEFAULT 'ACTIVE',
                    confirmed_at TEXT, confirmed_score REAL, confirmed_spy REAL,
                    invalidated_at TEXT, invalidation_reason TEXT,
                    invalidation_spy REAL, invalidation_mark REAL,
                    spy_move_at_invalidation REAL, vol_confirmed INTEGER,
                    reentry_count INTEGER DEFAULT 0, last_reentry_at TEXT,
                    out_5m_mark REAL, out_5m_pnl_pct REAL, out_5m_correct INTEGER,
                    out_10m_mark REAL, out_10m_pnl_pct REAL, out_10m_correct INTEGER,
                    out_15m_mark REAL, out_15m_pnl_pct REAL, out_15m_correct INTEGER,
                    out_30m_mark REAL, out_30m_pnl_pct REAL, out_30m_correct INTEGER,
                    traded INTEGER DEFAULT 0, trade_fill_price REAL,
                    trade_qty REAL, trade_pnl_pct REAL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS idea_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    idea_id INTEGER NOT NULL, event_time TEXT NOT NULL,
                    event_type TEXT NOT NULL, score REAL, mark REAL,
                    spy REAL, spy_move REAL, vol_ratio REAL, detail TEXT,
                    FOREIGN KEY(idea_id) REFERENCES ideas(id)
                )
            """)
        # Migration guard — add new columns to existing DB without recreating table
        for col_def in [
            "out_1m_mark REAL", "out_1m_pnl_pct REAL", "out_1m_correct INTEGER",
            "out_2m_mark REAL", "out_2m_pnl_pct REAL", "out_2m_correct INTEGER",
            "out_3m_mark REAL", "out_3m_pnl_pct REAL", "out_3m_correct INTEGER",
            "out_4m_mark REAL", "out_4m_pnl_pct REAL", "out_4m_correct INTEGER",
        ]:
            try:
                with self._connect() as conn:
                    conn.execute(f"ALTER TABLE ideas ADD COLUMN {col_def}")
            except Exception:
                pass  # already exists

        for path, headers in [(IDEAS_CSV, IDEA_HEADERS), (EVENTS_CSV, EVENT_HEADERS), (ALERTS_CSV, ALERT_HEADERS)]:
            if not path.exists():
                with open(path, "w", newline="", encoding="utf-8") as f:
                    csv.DictWriter(f, fieldnames=headers).writeheader()

    def _connect(self):
        conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _db_insert_idea(self, row: dict) -> int:
        cols = [k for k in IDEA_HEADERS if k != "id" and k in row]
        with self._lock:
            with self._connect() as conn:
                cur = conn.execute(
                    f"INSERT INTO ideas ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})",
                    [row.get(c) for c in cols]
                )
                return cur.lastrowid

    def _db_update_status(self, idea_id, status):
        with self._connect() as conn:
            conn.execute("UPDATE ideas SET status=? WHERE id=?", (status, idea_id))

    def _db_confirm(self, idea_id, score, spy, now):
        with self._connect() as conn:
            conn.execute(
                "UPDATE ideas SET status=?, confirmed_at=?, confirmed_score=?, confirmed_spy=? WHERE id=?",
                (STATUS_CONFIRMED, now.isoformat(timespec="seconds"), score, spy, idea_id)
            )

    def _log_event(self, idea_id, event_type, score=None, mark=None,
                   spy=None, spy_move=None, vol_ratio=None, detail="",
                   conn=None):
        now = datetime.now().isoformat(timespec="seconds")
        with self._lock:
            self._event_id += 1
            eid = self._event_id
        row = {"id": eid, "idea_id": idea_id, "event_time": now, "event_type": event_type,
               "score": score, "mark": mark, "spy": spy, "spy_move": spy_move,
               "vol_ratio": vol_ratio, "detail": detail}
        vals = [row[k] for k in ["idea_id","event_time","event_type","score","mark","spy","spy_move","vol_ratio","detail"]]
        sql  = """
                INSERT INTO idea_events (idea_id,event_time,event_type,score,mark,spy,spy_move,vol_ratio,detail)
                VALUES (?,?,?,?,?,?,?,?,?)
            """
        if conn is not None:
            conn.execute(sql, vals)
        else:
            with self._connect() as _conn:
                _conn.execute(sql, vals)
        with open(EVENTS_CSV, "a", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=EVENT_HEADERS).writerow(row)

    def _csv_append_idea(self, row: dict):
        with open(IDEAS_CSV, "a", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=IDEA_HEADERS).writerow(row)

    def _log_alert(self, ms):
        now = datetime.now().isoformat(timespec="seconds")
        self._alert_id += 1
        row = {
            "id": self._alert_id, "logged_at": now,
            "alert_zone": ms.alert_zone, "alert_message": getattr(ms,"alert_message",""),
            "spy_price": getattr(ms,"spy_price",""), "snap_level": getattr(ms,"snap_level",""),
            "snap_distance": getattr(ms,"snap_distance",""), "snap_direction": getattr(ms,"snap_direction",""),
            "regime": getattr(ms,"regime",""), "bias": getattr(ms,"bias",""),
            "net_gex": getattr(ms,"net_gex",""), "net_dex": getattr(ms,"net_dex",""),
        }
        with open(ALERTS_CSV, "a", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=ALERT_HEADERS).writerow(row)
        self._log.warning("ALERT [%s] spy=%.2f  %s", ms.alert_zone,
                          float(ms.spy_price or 0), getattr(ms,"alert_message",""))

    @staticmethod
    def _key(candidate) -> str:
        return f"{candidate.symbol}:{candidate.strike}:{candidate.option_type}"
