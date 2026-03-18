# src/ui/scalp_advisor.py
"""
Scalp Advisor — ranks option contracts by scalp suitability using live RTD data.

Scoring factors (each 0–100, weighted):
  1. GEX regime        — PINNED (pos GEX) vs TRENDING (neg GEX) regime            (15%)
  2. Direction         — underlying price trend + DEX dealer flow alignment        (20%)
  3. Level proximity   — near a meaningful level (max pain, call/put wall)         (15%)
  4. Volume surge      — unusual activity on this specific contract                (15%)
  5. Greeks quality    — delta 0.35–0.65, theta not too destructive                (10%)
  6. IV                — implied volatility as movement predictor                  (15%)
  7. SPY intraday level— position in day's range (near LOD = better for calls)    (10%)

Stability mechanisms:
  - Score smoothing    — each contract's score is a rolling average over SMOOTH_TICKS ticks
  - Hysteresis         — a contract must hold a high score for MIN_TICKS_TO_SURFACE ticks
                         before appearing; once shown it stays until score drops below
                         DROP_THRESHOLD

Hard filters (contract excluded entirely if any fail):
  - mark > 0.01
  - mark <= risk_cap   (user-configurable, default $2.00)
  - spread_pct <= 50%

Underlying direction is derived from a rolling price history (last DIRECTION_TICKS prices).
"""

from collections import deque
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Tuning constants
# ---------------------------------------------------------------------------
SMOOTH_TICKS          = 5    # ticks to average score over
MIN_TICKS_TO_SURFACE  = 3    # must score well for this many ticks before showing
DROP_THRESHOLD        = 58   # if smoothed score falls below this, remove from display
MAX_DISPLAYED         = 6    # hard cap on simultaneously shown candidates
DIRECTION_TICKS       = 6    # price history lookback for underlying trend (~1 min at 10s)
DIRECTION_MIN_MOVE    = 0.10 # underlying must move at least $0.10 to be called trending

# Change #1 — idea cooldown: suppresses re-surfacing same symbol for N minutes
DEFAULT_IDEA_COOLDOWN_MIN = 15

# Change #2 — relative volume surge: multiplier vs rolling per-option vol baseline
DEFAULT_VOL_SURGE_MULT    = 1.5   # option vol >= this × its 3-tick rolling avg = surge
DEFAULT_VOL_TICKS         = 3     # rolling window for per-option vol baseline
CANDLE_MINUTES            = 1     # candle size in minutes for underlying close confirmation

# Change #4 — IV floor/ceiling filters (0 = disabled)
DEFAULT_IV_FLOOR          = 0.0   # % — exclude contracts with IV below this
DEFAULT_IV_CEILING        = 60.0  # % — exclude contracts with IV above this

# Change #5 — confirm score threshold
DEFAULT_CONFIRM_SCORE     = 55

# Change #6 — first-N-minutes gate after market open (9:30 ET)
DEFAULT_OPEN_GATE_MINUTES = 30


@dataclass
class ScalpCandidate:
    symbol: str
    strike: float
    option_type: str       # 'Call' or 'Put'
    direction: str         # 'Bullish' or 'Bearish'
    underlying_trend: str  # 'Uptrend' | 'Downtrend' | 'Choppy'
    mark: float
    bid: float
    ask: float
    spread_pct: float
    delta: float
    theta: float
    iv: float
    volume: int
    score: float           # smoothed 0–100
    gex_negative: bool     # GEX regime at time of recommendation
    dex_bias: str          # 'Bullish' or 'Bearish' at time of recommendation
    reasons: list[str] = field(default_factory=list)

    @property
    def spread_str(self) -> str:
        return f"${self.bid:.2f} / ${self.ask:.2f}  ({self.spread_pct:.1f}%)"

    @property
    def type_emoji(self) -> str:
        return "🟢" if self.option_type == "Call" else "🔴"

    @property
    def trend_emoji(self) -> str:
        return {"Uptrend": "📈", "Downtrend": "📉", "Choppy": "↔️"}.get(self.underlying_trend, "")


class ScalpAdvisor:
    # Score weights (must sum to 1.0)
    W_GEX       = 0.15
    W_DIRECTION = 0.20
    W_LEVEL     = 0.15
    W_SURGE     = 0.15
    W_GREEKS    = 0.10
    W_IV        = 0.15
    W_SPY_LEVEL = 0.10

    MAX_SPREAD_PCT = 15.0
    DELTA_MIN      = 0.30
    DELTA_MAX      = 0.70

    def __init__(self):
        # score_history[sym] = deque of raw scores, maxlen=SMOOTH_TICKS
        self._score_history: dict[str, deque] = {}
        # tick_count[sym] = how many consecutive ticks this contract has scored well
        self._tick_count: dict[str, int] = {}
        # currently displayed contracts (for hysteresis)
        self._displayed: set[str] = set()
        # price history for underlying direction
        self._price_history: deque = deque(maxlen=DIRECTION_TICKS)

        # Change #1 — cooldown: sym -> datetime first surfaced
        self._idea_cooldown: dict[str, object] = {}   # sym -> datetime when idea was surfaced

        # Change #2 — per-option rolling volume history for relative surge detection
        self._vol_history: dict[str, deque] = {}      # sym -> deque(maxlen=DEFAULT_VOL_TICKS)
        # 1-minute candle close tracking for underlying confirmation
        self._last_candle_close: float = 0.0          # previous completed candle's close price
        self._candle_minute: int       = -1            # wall-clock minute of current open candle
        self._candle_last: float       = 0.0           # last price seen in current candle

        # SPY intraday range tracking
        self._day_high: float = 0.0
        self._day_low:  float = float('inf')

        # runtime config — updated each tick from api.py
        self._cfg: dict = {}

    def reset(self):
        self._score_history.clear()
        self._tick_count.clear()
        self._displayed.clear()
        self._price_history.clear()
        self._idea_cooldown.clear()
        self._vol_history.clear()
        self._last_candle_close = 0.0
        self._candle_minute     = -1
        self._candle_last       = 0.0
        self._day_high          = 0.0
        self._day_low           = float('inf')
        self._cfg.clear()

    def get_recommendations(
        self,
        data: dict,
        strikes: list,
        option_symbols: list,
        symbol: str,
        max_pain: float | None,
        call_wall: float | None,
        put_wall: float | None,
        surge_symbols: set | None = None,
        top_n: int = 6,
        risk_cap: float = 2.00,
        cfg: dict | None = None,
    ) -> list[ScalpCandidate]:

        from datetime import datetime, timezone


        if not strikes or not option_symbols:
            return []

        # Merge runtime config
        self._cfg = cfg or {}
        cooldown_min    = self._cfg.get("idea_cooldown_min",   DEFAULT_IDEA_COOLDOWN_MIN)
        iv_floor        = self._cfg.get("iv_floor",            DEFAULT_IV_FLOOR)
        iv_ceiling      = self._cfg.get("iv_ceiling",          DEFAULT_IV_CEILING)
        vol_surge_mult  = self._cfg.get("vol_surge_mult",      DEFAULT_VOL_SURGE_MULT)
        open_gate_min   = self._cfg.get("open_gate_minutes",   DEFAULT_OPEN_GATE_MINUTES)

        now       = datetime.now()
        today     = now.date()
        import re as _re
        _EXPIRY_RE = _re.compile(r'(\d{6})[CP]')

        # ------------------------------------------------------------------
        # Change #6 — first-N-minutes gate (9:30 ET = 13:30 UTC)
        # ------------------------------------------------------------------
        try:
            from zoneinfo import ZoneInfo
            now_et    = datetime.now(ZoneInfo("America/New_York"))
            open_et   = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
            mins_open = (now_et - open_et).total_seconds() / 60
            in_gate   = 0 < mins_open < open_gate_min
        except Exception:
            in_gate = False

        current_price = self._sf(data, f"{symbol}:LAST")
        if current_price == 0:
            return []

        # Track price history for direction
        self._price_history.append(current_price)

        # Track intraday range for SPY level scoring
        if current_price > self._day_high:
            self._day_high = current_price
        if 0 < current_price < self._day_low:
            self._day_low = current_price

        # ------------------------------------------------------------------
        # Change #2 — track 1-minute candle closes for underlying confirmation
        # ------------------------------------------------------------------
        self._update_candle(current_price, now)

        surge_symbols = surge_symbols or set()

        # Underlying direction
        trend = self._get_trend()

        # GEX regime
        net_gex = self._net_gex_at_price(data, strikes, option_symbols, current_price)
        gex_negative = net_gex < 0

        # DEX bias
        net_dex = self._net_dex_near_price(data, strikes, option_symbols, current_price)
        dex_bias = "Bearish" if net_dex > 0 else "Bullish"

        # Key levels
        levels = {}
        if max_pain is not None:
            levels["Max Pain"] = max_pain
        if call_wall is not None:
            levels["Call Wall"] = call_wall
        if put_wall is not None:
            levels["Put Wall"] = put_wall

        # ------------------------------------------------------------------
        # Expire cooldowns
        # ------------------------------------------------------------------
        expired = [sym for sym, t in self._idea_cooldown.items()
                   if (now - t).total_seconds() / 60 >= cooldown_min]
        for sym in expired:
            del self._idea_cooldown[sym]

        # ------------------------------------------------------------------
        # Score every contract
        # ------------------------------------------------------------------
        this_tick_scores: dict[str, tuple] = {}

        for strike in strikes:
            for opt_type in ("Call", "Put"):
                marker = "C" if opt_type == "Call" else "P"
                try:
                    sym = next(s for s in option_symbols if s.endswith(f'{marker}{strike}'))
                except StopIteration:
                    continue

                # ── Expired-expiry guard ──────────────────────────────────
                # Parse YYMMDD from symbol (e.g. .SPY260320C678 → 2026-03-20).
                # Skip entirely if expiry is before today — stale chain data
                # can linger after roll and produce garbage deltas/marks.
                _em = _EXPIRY_RE.search(sym)
                if _em:
                    try:
                        _exp = datetime.strptime('20' + _em.group(1), '%Y%m%d').date()
                        if _exp < today:
                            continue
                    except ValueError:
                        pass

                bid   = self._sf(data, f"{sym}:BID")
                ask   = self._sf(data, f"{sym}:ASK")
                mark  = self._sf(data, f"{sym}:MARK")
                delta = self._sf(data, f"{sym}:DELTA")
                theta = self._sf(data, f"{sym}:THETA")
                iv    = self._sf(data, f"{sym}:IMPL_VOL")
                vol   = int(self._sf(data, f"{sym}:VOLUME"))

                # ── Hard filters ──────────────────────────────────────────
                if mark <= 0.01:
                    continue
                if mark > risk_cap:
                    continue
                spread_pct = ((ask - bid) / mark * 100) if mark > 0 else 999
                if spread_pct > 50:
                    continue

                # Change #4 — IV floor/ceiling filters
                if iv_floor > 0 and iv < iv_floor:
                    continue
                if iv_ceiling > 0 and iv > iv_ceiling:
                    continue

                # Change #3 — trend-side filter: suppress low-confidence counter-trend ideas
                if opt_type == "Put"  and trend == "Uptrend":
                    continue   # no puts on uptrend
                if opt_type == "Call" and trend == "Downtrend":
                    continue   # no calls on downtrend

                # Wall gate — calls only when price is ABOVE put wall,
                # puts only when price is BELOW call wall.
                # Prevents ideas that would be immediately invalidated.
                if call_wall and put_wall:
                    if opt_type == "Call" and current_price < put_wall:
                        continue
                    if opt_type == "Put"  and current_price > call_wall:
                        continue

                # Change #6 — gate first N minutes
                if in_gate:
                    continue

                # Change #1 — cooldown: skip if this sym recently surfaced
                if sym in self._idea_cooldown:
                    # still update vol history so baseline stays accurate
                    self._update_vol_history(sym, vol)
                    continue

                abs_delta = abs(delta)

                # Change #2 — relative volume surge
                vol_surging, rel_vol_ratio = self._check_rel_vol_surge(
                    sym, vol, opt_type, vol_surge_mult
                )
                # If we have a legacy surge_symbols set, honour it too
                is_surging = vol_surging or (sym in surge_symbols)

                # ── Factor scores ─────────────────────────────────────────
                gex_score       = 65 if gex_negative else 75
                direction_score = self._direction_score(opt_type, trend, dex_bias)
                level_score     = self._level_score(strike, levels, current_price)
                surge_score     = self._surge_score(is_surging, rel_vol_ratio)
                greeks_score    = self._greeks_score(abs_delta, theta, mark)
                iv_score        = self._iv_score(iv)
                spy_level_score, position_in_range = self._spy_level_score(current_price, opt_type)

                # Change #2 — candle-close confirmation: price must be above (calls)
                # or below (puts) the last completed 1-min candle close
                underlying_confirms = self._candle_confirms(current_price, opt_type)
                if vol_surging and underlying_confirms:
                    surge_score = min(surge_score + 8, 100)   # +8 bonus for confirmed surge

                raw_score = (
                    self.W_GEX       * gex_score       +
                    self.W_DIRECTION * direction_score  +
                    self.W_LEVEL     * level_score      +
                    self.W_SURGE     * surge_score      +
                    self.W_GREEKS    * greeks_score     +
                    self.W_IV        * iv_score         +
                    self.W_SPY_LEVEL * spy_level_score
                )

                # Soft ceiling — scores above 68 are compressed
                if raw_score > 68:
                    raw_score = 68 + (raw_score - 68) * 0.3

                # Spread penalty
                if spread_pct > self.MAX_SPREAD_PCT:
                    penalty = min((spread_pct - self.MAX_SPREAD_PCT) / self.MAX_SPREAD_PCT, 1.0)
                    raw_score *= (1 - penalty * 0.5)

                this_tick_scores[sym] = (raw_score, {
                    "symbol": sym, "strike": strike, "option_type": opt_type,
                    "direction": "Bullish" if opt_type == "Call" else "Bearish",
                    "underlying_trend": trend,
                    "mark": mark, "bid": bid, "ask": ask,
                    "spread_pct": spread_pct, "delta": abs_delta,
                    "theta": theta, "iv": iv, "volume": vol,
                    "gex_negative": gex_negative, "dex_bias": dex_bias,
                    # internal
                    "_gex_negative": gex_negative, "_dex_bias": dex_bias,
                    "_levels": levels, "_current_price": current_price,
                    "_is_surging": is_surging,
                    "_rel_vol_ratio": rel_vol_ratio,
                    "_underlying_confirms": underlying_confirms,
                    "_position_in_range": position_in_range,
                })

        # ------------------------------------------------------------------
        # Update score history and tick counts
        # ------------------------------------------------------------------
        for sym, (raw_score, _) in this_tick_scores.items():
            if sym not in self._score_history:
                self._score_history[sym] = deque(maxlen=SMOOTH_TICKS)
                self._tick_count[sym] = 0
            self._score_history[sym].append(raw_score)
            self._tick_count[sym] += 1

        # Decay tick counts for contracts not seen this tick
        for sym in list(self._tick_count.keys()):
            if sym not in this_tick_scores:
                self._tick_count[sym] = max(0, self._tick_count[sym] - 1)

        # ------------------------------------------------------------------
        # Build smoothed candidates with hysteresis
        # ------------------------------------------------------------------
        smoothed: list[tuple[float, ScalpCandidate]] = []

        for sym, (_, kwargs) in this_tick_scores.items():
            history = self._score_history.get(sym)
            if not history:
                continue
            smoothed_score = sum(history) / len(history)
            tick_count = self._tick_count.get(sym, 0)

            # Hysteresis — must qualify for MIN_TICKS_TO_SURFACE before surfacing
            if sym not in self._displayed:
                if tick_count < MIN_TICKS_TO_SURFACE:
                    continue
                if smoothed_score < DROP_THRESHOLD:
                    continue
                self._displayed.add(sym)
            else:
                # Already displayed — remove only if score drops too low
                if smoothed_score < DROP_THRESHOLD:
                    self._displayed.discard(sym)
                    continue

            # Change #1 — register cooldown on first surface
            if sym not in self._idea_cooldown:
                self._idea_cooldown[sym] = now

            reasons = self._build_reasons(
                kwargs["option_type"], kwargs["direction"], kwargs["_dex_bias"],
                kwargs["_gex_negative"], kwargs["delta"],
                kwargs["theta"], kwargs["mark"], kwargs["spread_pct"],
                kwargs["_is_surging"], kwargs["strike"], kwargs["_levels"],
                kwargs["_current_price"], kwargs["iv"], trend,
                kwargs["_position_in_range"],
            )

            # Build dataclass — strip internal underscore keys
            dc_kwargs = {k: v for k, v in kwargs.items() if not k.startswith("_")}
            smoothed.append((smoothed_score, ScalpCandidate(
                **dc_kwargs,
                score=round(smoothed_score, 1),
                reasons=reasons,
            )))

        smoothed.sort(key=lambda x: x[0], reverse=True)

        # Evict symbols that fell out of top MAX_DISPLAYED and aren't in this tick's list
        top_syms = {c.symbol for _, c in smoothed[:MAX_DISPLAYED]}
        all_syms = {c.symbol for _, c in smoothed}
        for sym in list(self._displayed):
            if sym not in top_syms and sym not in all_syms:
                self._displayed.discard(sym)

        return [c for _, c in smoothed[:top_n]]

    # ------------------------------------------------------------------
    # Change #2 helpers — per-option relative volume surge
    # ------------------------------------------------------------------

    def _update_vol_history(self, sym: str, vol: int):
        """Record latest volume tick for a symbol."""
        if sym not in self._vol_history:
            self._vol_history[sym] = deque(maxlen=DEFAULT_VOL_TICKS)
        self._vol_history[sym].append(vol)

    def _check_rel_vol_surge(
        self, sym: str, vol: int, opt_type: str, mult: float
    ) -> tuple[bool, float]:
        """
        Returns (is_surging, ratio).
        TOS VOLUME is cumulative (total contracts today), so we compute
        per-tick deltas before comparing current activity to baseline.
        is_surging = True if current tick delta >= mult × avg of prior deltas.
        ratio = current_delta / baseline_avg (1.0 if no baseline).
        """
        self._update_vol_history(sym, vol)
        history = self._vol_history.get(sym)
        if not history or len(history) < 2:
            return False, 1.0
        items = list(history)
        # Convert cumulative volumes to per-tick deltas; EOD resets → 0
        deltas = [max(items[i] - items[i - 1], 0) for i in range(1, len(items))]
        if len(deltas) < 2:
            return False, 1.0
        current_delta = deltas[-1]
        baseline = sum(deltas[:-1]) / len(deltas[:-1])
        if baseline <= 0:
            return False, 1.0
        ratio = current_delta / baseline
        return ratio >= mult, round(ratio, 2)

    def _update_candle(self, price: float, now: object):
        """
        Maintain a 1-minute candle for the underlying.
        When the wall-clock minute rolls over, the previous candle's last
        price becomes _last_candle_close.
        """
        current_minute = now.hour * 60 + now.minute
        if self._candle_minute == -1:
            # First tick ever
            self._candle_minute = current_minute
            self._candle_last   = price
        elif current_minute != self._candle_minute:
            # Minute rolled — completed candle's close is its last price
            self._last_candle_close = self._candle_last
            self._candle_minute     = current_minute
            self._candle_last       = price
        else:
            # Same candle — update running last
            self._candle_last = price

    def _candle_confirms(self, current_price: float, opt_type: str) -> bool:
        """
        True if the underlying is positioned correctly vs the last completed 1-min candle close.
        Calls: current price > last candle close  (bullish — price above prior close)
        Puts:  current price < last candle close  (bearish — price below prior close)
        Returns False if no completed candle exists yet (first minute of session).
        """
        if self._last_candle_close <= 0:
            return False
        if opt_type == "Call":
            return current_price > self._last_candle_close
        else:
            return current_price < self._last_candle_close
    # ------------------------------------------------------------------

    def _get_trend(self) -> str:
        if len(self._price_history) < 3:
            return "Choppy"
        oldest = self._price_history[0]
        newest = self._price_history[-1]
        move = newest - oldest
        if move >= DIRECTION_MIN_MOVE:
            return "Uptrend"
        elif move <= -DIRECTION_MIN_MOVE:
            return "Downtrend"
        return "Choppy"

    def _direction_score(self, opt_type: str, trend: str, dex_bias: str) -> float:
        """
        Combines price trend and DEX bias.
        Call in Uptrend + Bullish DEX = 100
        Put in Downtrend + Bearish DEX = 100
        Mixed signals = partial credit
        Choppy = neutral 40 for both sides
        """
        if trend == "Choppy":
            # Use DEX bias as tiebreaker to distinguish calls from puts
            if opt_type == "Call":
                return 60 if dex_bias == "Bullish" else 30
            else:
                return 60 if dex_bias == "Bearish" else 30

        # Price trend alignment
        if opt_type == "Call":
            trend_aligned = trend == "Uptrend"
        else:
            trend_aligned = trend == "Downtrend"

        # DEX alignment
        if opt_type == "Call":
            dex_aligned = dex_bias == "Bullish"
        else:
            dex_aligned = dex_bias == "Bearish"

        if trend_aligned and dex_aligned:
            return 100
        elif trend_aligned and not dex_aligned:
            return 65   # trend confirms but dealers hedging against
        elif not trend_aligned and dex_aligned:
            return 50   # dealers aligned but price moving against
        else:
            return 15   # both against

    # ------------------------------------------------------------------
    # GEX / DEX helpers
    # ------------------------------------------------------------------

    def _net_gex_at_price(self, data, strikes, option_symbols, price) -> float:
        # Use all strikes for consistency with market_structure.analyze()
        total = 0.0
        for strike in strikes:
            try:
                cs = next(s for s in option_symbols if s.endswith(f'C{strike}'))
                ps = next(s for s in option_symbols if s.endswith(f'P{strike}'))
                total += ((self._sf(data, f"{cs}:OPEN_INT") * self._sf(data, f"{cs}:GAMMA")) -
                          (self._sf(data, f"{ps}:OPEN_INT") * self._sf(data, f"{ps}:GAMMA"))) \
                         * 100 * price * price * 0.01
            except StopIteration:
                continue
        return total

    def _net_dex_near_price(self, data, strikes, option_symbols, price) -> float:
        # Use all strikes for consistency with market_structure.analyze()
        total = 0.0
        for strike in strikes:
            try:
                cs = next(s for s in option_symbols if s.endswith(f'C{strike}'))
                ps = next(s for s in option_symbols if s.endswith(f'P{strike}'))
                total += (self._sf(data, f"{cs}:OPEN_INT") * self._sf(data, f"{cs}:DELTA") +
                          self._sf(data, f"{ps}:OPEN_INT") * self._sf(data, f"{ps}:DELTA")) \
                         * 100 * price
            except StopIteration:
                continue
        return total

    # ------------------------------------------------------------------
    # Scoring helpers
    # ------------------------------------------------------------------

    def _level_score(self, strike, levels: dict, current_price: float) -> float:
        if not levels:
            return 40
        best = 0.0
        price_range = max(abs(current_price - v) for v in levels.values()) or 1
        for _, level in levels.items():
            dist = abs(strike - level)
            if dist <= 0.5:
                best = max(best, 95)
            elif self._between(strike, current_price, level):
                best = max(best, 50 + (1 - dist / price_range) * 40)
            else:
                best = max(best, max(0, 1 - dist / price_range) * 40)
        return best

    def _greeks_score(self, abs_delta: float, theta: float, mark: float) -> float:
        """Score delta quality and theta cost.
        TOS THETA is already per-day (e.g. -1.12 means lose $1.12/day per contract).
        For a $1.18 mark, theta of -1.12 is extremely high — penalise hard.
        We compare theta to mark directly (theta_pct = abs(theta)/mark).
        Above 20% of mark per day = very expensive to hold.
        """
        if self.DELTA_MIN <= abs_delta <= self.DELTA_MAX:
            delta_score = 100
        elif abs_delta < self.DELTA_MIN:
            delta_score = max(0, (abs_delta / self.DELTA_MIN) * 70)
        else:
            delta_score = max(0, 100 - ((abs_delta - self.DELTA_MAX) / (1 - self.DELTA_MAX)) * 60)

        # theta as fraction of mark — 0.05 (5%) = neutral, 0.20 (20%) = very bad
        theta_ratio = abs(theta) / mark if mark > 0 else 0
        theta_score = max(0, 100 - (theta_ratio / 0.20) * 100)

        return delta_score * 0.7 + theta_score * 0.3

    def _surge_score(self, is_surging: bool, rel_vol_ratio: float) -> float:
        """Graduated surge score using relative volume ratio."""
        if rel_vol_ratio <= 1.0:
            return 15
        if rel_vol_ratio < 1.5:
            return 15 + ((rel_vol_ratio - 1.0) / 0.5) * 25   # 15→40
        capped = min(rel_vol_ratio, 5.0)
        return 55 + ((capped - 1.5) / 3.5) * 40               # 55→95

    def _iv_score(self, iv: float) -> float:
        """Score implied volatility — higher IV = better movement potential."""
        if iv <= 0:
            return 20
        if iv < 25:
            return max(0, (iv / 25) * 30)
        if iv < 32:
            return 30 + ((iv - 25) / 7) * 25    # 30→55
        if iv < 38:
            return 55 + ((iv - 32) / 6) * 25    # 55→80
        if iv < 45:
            return 80 + ((iv - 38) / 7) * 15    # 80→95
        return 95

    def _spy_level_score(self, current_price: float, opt_type: str) -> tuple[float, float]:
        """
        Score based on SPY's position in today's high/low range.
        Returns (score, position) where position is 0.0 (LOD) to 1.0 (HOD).
        Calls near LOD = good; Puts near HOD = good.
        """
        day_range = self._day_high - self._day_low
        if day_range < 0.50:
            return 50.0, 0.5  # not enough range established yet
        position = (current_price - self._day_low) / day_range
        position = max(0.0, min(1.0, position))
        if opt_type == "Call":
            return 20 + (1.0 - position) * 75, position   # near LOD = best
        else:
            return 20 + position * 75, position            # near HOD = best

    # ------------------------------------------------------------------
    # Reasons
    # ------------------------------------------------------------------

    def _build_reasons(
        self, opt_type, direction, dex_bias, gex_negative,
        abs_delta, theta, mark, spread_pct, is_surging,
        strike, levels, current_price, iv, trend,
        position_in_range: float = 0.5,
    ) -> list[str]:
        reasons = []

        # Trend
        trend_emoji = {"Uptrend": "📈", "Downtrend": "📉", "Choppy": "↔️"}.get(trend, "")
        if trend == "Choppy":
            reasons.append(f"{trend_emoji} Underlying is choppy — lower conviction for directional scalp")
        elif (opt_type == "Call" and trend == "Uptrend") or (opt_type == "Put" and trend == "Downtrend"):
            reasons.append(f"{trend_emoji} Price trending in direction of this trade")
        else:
            reasons.append(f"{trend_emoji} Price trending against this trade — lower confidence")

        # DEX
        if (opt_type == "Call" and dex_bias == "Bullish") or (opt_type == "Put" and dex_bias == "Bearish"):
            reasons.append("DEX dealer flow aligns with trade direction")
        else:
            reasons.append(f"DEX dealer flow ({dex_bias}) is against this trade")

        # GEX
        if gex_negative:
            reasons.append("Negative GEX — TRENDING regime, moves tend to extend")
        else:
            reasons.append("Positive GEX — PINNED regime, mean-reversion setup")

        # Level
        if levels:
            nearest_name, nearest_level = min(levels.items(), key=lambda x: abs(strike - x[1]))
            dist = abs(strike - nearest_level)
            if dist <= 0.5:
                reasons.append(f"Strike is AT {nearest_name} (${nearest_level})")
            elif self._between(strike, current_price, nearest_level):
                reasons.append(f"In path toward {nearest_name} (${nearest_level})")
            else:
                reasons.append(f"Nearest level: {nearest_name} at ${nearest_level} ({dist:.1f} pts)")

        # Surge
        if is_surging:
            reasons.append("🔥 Volume surge on this contract")

        # Greeks
        if 0.35 <= abs_delta <= 0.65:
            reasons.append(f"Δ {abs_delta:.2f} — near ATM, good leverage")
        elif abs_delta < 0.35:
            reasons.append(f"Δ {abs_delta:.2f} — OTM, needs larger underlying move")
        else:
            reasons.append(f"Δ {abs_delta:.2f} — deep ITM")

        # Theta — show if it's notable (>10% of mark per day)
        theta_ratio = abs(theta) / mark if mark > 0 else 0
        if theta_ratio > 0.10:
            theta_pct_str = f"{theta_ratio*100:.0f}%"
            reasons.append(f"θ ${abs(theta):.2f}/day — {theta_pct_str} of mark, decay is significant")

        if spread_pct > self.MAX_SPREAD_PCT:
            reasons.append(f"⚠️ Wide spread ({spread_pct:.1f}%)")

        # IV — tiered message
        if iv > 0:
            if iv < 32:
                reasons.append(f"IV {iv:.0f}% — low, option may not move enough")
            elif iv < 38:
                reasons.append(f"IV {iv:.0f}% — average")
            else:
                reasons.append(f"IV {iv:.0f}% — elevated, good movement potential")

        # SPY level context
        if opt_type == "Call" and position_in_range > 0.80:
            reasons.append("SPY near day high — limited upside room")
        elif opt_type == "Put" and position_in_range < 0.20:
            reasons.append("SPY near day low — limited downside room")

        return reasons

    @staticmethod
    def _between(value, a, b) -> bool:
        return min(a, b) <= value <= max(a, b)

    @staticmethod
    def _sf(data: dict, key: str, default: float = 0.0) -> float:
        try:
            v = data.get(key)
            return float(v) if v is not None else default
        except (ValueError, TypeError):
            return default
