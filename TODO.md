# tos-dash-v2 — Backlog / TODO

## Standard Claude Code Prompt Header (prepend to every prompt)
```
## Environment
Working directory: ~/tos-dash-v2
Before making ANY changes, read ALL files in the project — including files you think are unrelated.
Do not edit, create, or delete anything until you have a complete picture of the codebase.
```

---

## SPY Underlying Volume vs Options Volume
**Context:** Real SPY directional moves come with underlying share volume expansion, not just options volume surges. A vol surge on a specific contract is meaningful, but it's more meaningful when the underlying itself is seeing elevated tape activity — this separates conviction moves from noise/positioning.

**What to explore:**
- Pull SPY tick volume (or 1-min volume bar) from RTD alongside price
- Compute a rolling baseline of SPY share volume (e.g. 5-min avg)
- When current SPY volume > N × baseline AND a directional options surge is present, treat this as a "confirmed volume expansion" signal — higher confidence than options surge alone
- This would add a new confirmatory gate, likely surfacing as a score bonus (similar to `underlying_confirms` candle logic but volume-based)
- Consider: SPY volume spike opposing the trade direction could be a *negative* signal (distribution vs accumulation)

**Data capture needed:**
- Add `entry_spy_vol_delta` to `ideas.db` — the per-tick SPY volume delta at time of surface
- After N weeks of data, run correlation vs `out_5m_correct` to validate before weighting

**Priority:** Medium — validate with data before building

---

## Codebase Optimization Pass
**When to do this:** After 2–3 weeks of clean live data post-fixes. The system needs to
be correct before it's optimized. Do not start this while architectural changes are
still in flight.

**The readiness signal:**
- DEX/scoring fixes are live and stable
- ideas.db hit rate is trending in the right direction
- No more logic-level changes planned — tuning only
- You're no longer changing function signatures or data shapes

**Highest priority wins:**

### 1. Eliminate redundant GEX/DEX computation (HIGH)
market_structure.py, scalp_advisor.py, and gamma_chart.py all independently compute
GEX and DEX from raw RTD data on every tick. This is the same math run 3x per cycle.
Fix: compute once per tick in api.py build_snapshot(), cache the result, pass it down
to all consumers. Single source of truth, one third the computation.

### 2. Replace spy_writer.py JSON file I/O with in-process shared state (HIGH)
spy_writer.py writes JSON to disk every 500ms. api.py reads it back. This file I/O
pattern will degrade as the option chain widens (more strikes = bigger JSON = slower
disk round-trip). Fix: move to a shared in-process dict or asyncio Queue so RTD data
is passed in memory, not via filesystem.

### 3. ideas.db size cap + indexing (MEDIUM)
As the DB grows past ~50k rows, queries will slow without indexes. Also needs a max
row cap or date-based archiving strategy so it doesn't grow unbounded.
Fix: add indexes on surfaced_at, status, out_5m_correct. Add a config key
max_idea_rows (default 10000) that archives or deletes oldest rows when exceeded.

### 4. Score history and tick count dicts in scalp_advisor.py (MEDIUM)
_score_history and _tick_count grow one entry per option symbol seen. With a wide chain
(±25 strikes × 2 sides = 100 symbols) these dicts accumulate stale entries across the
session. Fix: add a periodic cleanup pass that evicts symbols not seen in the last N ticks.

### 5. GEX/DEX chart recalculation on every render (LOW)
gamma_chart.py recalculates all GEX and DEX values from scratch on every chart render
call. Fix: cache the last computed chart figure and only rebuild when the underlying
data tick count has advanced. Especially useful for the DEX chart which is purely
visual and changes slowly.

### 6. General code hygiene (LOW — do last)
- Consolidate duplicate _sf() / safe_float() helper patterns across files into a
  single shared util module
- Standardize logging (currently mix of print() and no logging)
- Add type hints to all public function signatures
- Remove dead code / commented-out blocks that accumulated during bug fixes

**Priority:** Low urgency now — schedule after data confirms fixes are working

---

## Multi-DTE Support + Session Phase Auto-Switch
**Context:** 0DTE gamma dominates the morning session but theta decay accelerates past
the point of no return in the afternoon. Rolling to 1-2DTE after ~2pm reduces decay
cost while keeping directional exposure. The system needs to support this workflow
natively rather than requiring a manual chain swap.

**Complexity rating:** Moderate — spread across more files than expected, but not a
rewrite. Do this AFTER 0DTE scoring is stable and validated.

**What needs to change:**

### 1. Expiry config + auto-switch logic (api.py)
Add config keys:
    preferred_dte: "auto" | "0" | "1" | "2"   # default "auto"
    dte_switch_hour: 14                         # 2:00pm ET cutoff for auto mode

Auto mode logic:
    before dte_switch_hour ET  → load today's chain (0DTE)
    after  dte_switch_hour ET  → load next trading day's chain (1DTE)
Next-day expiry must account for weekends and holidays — use a trading calendar
lookup, not just date + 1.

### 2. Theta scoring recalibration (scalp_advisor.py)
Current theta penalty thresholds were tuned for 0DTE decay rates (extreme).
1DTE theta ≈ half of 0DTE, 2DTE ≈ one third. The same formula under-penalizes
those contracts at current thresholds.

Fix: make theta penalty scale-aware. Pass DTE into _greeks_score():
    0DTE: current thresholds (theta_ratio > 0.20 = very bad)
    1DTE: loosen to (theta_ratio > 0.12 = very bad)
    2DTE: loosen to (theta_ratio > 0.08 = very bad)

### 3. Scoring weight shifts at DTE boundary (scalp_advisor.py)
On 0DTE: max pain gravity is strong, snap urgency is high, gamma dominates
On 1-2DTE: max pain weaker, walls softer, theta matters more, wider delta range ok

When DTE >= 1:
    - Reduce W_SPY_LEVEL weight slightly (snap effects less sharp)
    - Increase W_GREEKS weight (theta cost is now meaningful over the hold)
    - Accept wider delta range: DELTA_MIN = 0.25, DELTA_MAX = 0.75

### 4. GEX/DEX regime context note (market_structure.py)
On 1-2DTE, the chain being traded is no longer the dominant gamma force on dealers.
Add a DTE-aware note to bias_reason when DTE >= 1:
    "Note: trading 1DTE — GEX regime reflects 0DTE chain dominance, use for
     structural context only."

### 5. ideas.db capture (idea_logger.py)
Add dte_at_surface column (int: 0, 1, or 2) to ideas table so future analysis
can segment hit rates by DTE and validate the 2pm switch timing empirically.

**Data-driven validation before building:**
Query ideas.db for hit rate by hour of day on 0DTE ideas:
    SELECT strftime('%H', surfaced_at) as hour,
           AVG(out_5m_correct) as hit_rate,
           COUNT(*) as n
    FROM ideas
    WHERE entry_regime != 'ACTIVE'
    GROUP BY hour ORDER BY hour;

This will show you the exact hour where 0DTE performance degrades and give you
a data-driven dte_switch_hour rather than a gut-feel cutoff.

**Priority:** Medium — natural evolution once 0DTE scoring is solid and validated.
Do not start until ideas.db has 3+ weeks of clean post-fix data.

---

## Hull Ch.19 — Vanna: IV Change Rate as Directional Multiplier
**Source:** Hull Chapter 19 (Greek Letters) — Vanna = dDelta/dIV

**Context:** Vanna measures how dealer deltas shift when implied volatility changes.
On 0DTE, when IV spikes intraday AND price is moving directionally, dealers are forced
to re-hedge both their gamma AND their vanna exposure simultaneously. This creates
amplified, faster moves — the "clean trend" days that are easiest to scalp.

Your system scores IV level (good) but not IV change rate (missing). A rising IV
environment on a directional move is a fundamentally different setup from a flat or
falling IV environment at the same IV level.

**What to add:**
- Track IV tick-by-tick for each candidate in _vol_history equivalent (per-option IV
  deque, similar to how vol_surge is tracked)
- Compute iv_delta = current_iv - avg(prior 3 ticks IV)
- Add vanna_bonus to score:
    iv_delta > +1.5%  AND aligned direction → +5 bonus (rising IV amplifies move)
    iv_delta < -1.5%  AND in position     → -3 penalty (IV crush = option losing value
                                             even if direction is right)
- Capture entry_iv_delta in ideas.db for later regression analysis

**Priority:** Medium — validate with data first, but high theoretical grounding

---

## Hull Ch.19 — Charm: Delta Decay from Time, Not Price
**Source:** Hull Chapter 19 (Greek Letters) — Charm = dDelta/dTime

**Context:** Charm measures how an option's delta drifts purely from time passing,
even with zero price movement. On 0DTE this is enormous — by 2pm, ATM options are
losing delta sensitivity rapidly. This creates the mechanical drift seen into close,
NOT driven by price but by charm-induced dealer rehedging unwinding.

Your current candle confirmation logic (price above/below last 1-min close) is a weak
proxy for charm. The actual mechanism is time-driven delta decay forcing dealer
position adjustments regardless of price action.

**What to add:**
- Charm is implicitly handled by the time-of-day max pain weighting already in TODO
- Extend that to a session_phase concept used across scoring:
    PHASE_OPEN:    first 30 min  — high vol, low charm impact, open gate active
    PHASE_MORNING: 10am–12pm    — most stable, charm low, best scoring reliability
    PHASE_MIDDAY:  12pm–2pm     — charm building, reduce confidence on stale ideas
    PHASE_EOD:     2pm–close    — charm dominant, max pain gravity high, DTE switch
- Use session_phase to scale MIN_TICKS_TO_SURFACE and DROP_THRESHOLD:
    EOD phase: raise DROP_THRESHOLD by 5pts (harder to stay in list as charm kills delta)
- Connects to Multi-DTE switching — charm is the theoretical reason for the 2pm switch

**Priority:** Low-Medium — implement alongside DTE switching work

---

## Hull Ch.20 — Volatility Skew Adjustment for IV Scoring
**Source:** Hull Chapter 20 (Volatility Smiles) — equity index put skew

**Context:** SPY/SPX options have a persistent volatility skew — put IV is structurally
higher than call IV at the same distance from ATM. This is not because puts have better
edge — it reflects demand for downside protection. Scoring raw IV equally for puts and
calls overstates the quality of put setups at any given IV level.

Example: Put IV = 38%, Call IV = 32% at equidistant strikes. Raw IV scoring gives
the put a higher score, but the put's IV is inflated by structural skew, not by
genuine uncertainty about direction. The call's 32% represents more "real" volatility
premium relative to its structural baseline.

**What to add:**
- Track ATM IV (the at-the-money strike IV) each tick
- Compute iv_skew_adjusted = iv - (atm_iv × skew_factor)
    skew_factor: calls = 0.0 (no adjustment), puts = -0.05 (puts get 5% IV haircut)
    These are starting values — tune from data
- Use iv_skew_adjusted in _iv_score() instead of raw iv
- Add entry_atm_iv to ideas.db for regression validation

**Data needed to tune:** After N weeks, compare iv_skew_adjusted vs out_5m_correct
for calls vs puts separately to find the right skew_factor

**Priority:** Low — theoretical refinement, implement after vanna and charm work
