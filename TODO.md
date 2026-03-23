# tos-dash-v2 — Backlog / TODO

## Prompt Inventory

Naming convention: PROMPT-[REPO]-[###] | [title] | Lines: N
Status: DONE = executed and verified, PENDING = not yet run

| ID | Title | Lines | Status |
|---|---|---|---|
| PROMPT-DASH-001 | scalp_advisor.py scoring overhaul | 116 | DONE |
| PROMPT-DASH-002 | market_structure.py DEX bias fix + gamma_chart cleanup | 130 | DONE |
| PROMPT-DASH-003 | add 1/2/3/4 minute outcome tracking | 103 | DONE |
| PROMPT-DASH-004 | debug=True fix + OI investigation | 57 | DONE |
| PROMPT-DASH-005 | verify DASH-003 completed correctly | 63 | DONE |
| PROMPT-DASH-006 | scalp candidates brightness fix | 26 | DONE |
| PROMPT-DASH-007 | complete testing/ folder missing shell scripts | 124 | DONE |
| PROMPT-DASH-008 | testing/ full suite — all 6 files | 255 | DONE |
| PROMPT-DASH-009 | fix test_rtd_schwab.py Schwab auth blocking | 38 | DONE |
| PROMPT-DASH-010 | DB diagnostic queries read only | 47 | DONE |
| PROMPT-DASH-011 | fix UnboundLocalError ms in api.py | 35 | DONE |
| PROMPT-DASH-012 | paper trade + theta fix + font fix | 196 | PENDING |
| PROMPT-DASH-013 | AI triggers + expiry fix + call counter + snap watch | 242 | PENDING |

Weekend run order: DASH-012 first, DASH-013 second.

---


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

## Move Tuning Constants to Settings UI
**Context:** Several hardcoded constants in scalp_advisor.py directly affect
live behavior but are invisible to the user. Changing them requires editing
Python files and restarting the server. They should be in config.json and
exposed in the Settings tab.

**Constants to expose:**

From scalp_advisor.py:
    SMOOTH_TICKS          = 5    → cfg: smooth_ticks
    MIN_TICKS_TO_SURFACE  = 3    → cfg: min_ticks_to_surface
    DROP_THRESHOLD        = 52   → cfg: drop_threshold
    DIRECTION_TICKS       = 6    → cfg: direction_ticks
    DIRECTION_MIN_MOVE    = 0.10 → cfg: direction_min_move
    MAX_DISPLAYED         = 6    → cfg: max_displayed (candidate cap)

**Settings UI section:** Add under Scoring section as "Candidate Surfacing"
group with descriptions for each field so it's self-documenting.

**Priority:** Medium — important for live tuning without restarts.
Do alongside the Settings cleanup task.

---

## Fix 0DTE Theta Penalty in _greeks_score()
**Context:** The monospace font currently used in the dashboard renders
0 with a dot or slash through the middle (see poll_ms field showing
"500"). This is a legibility issue — hard to read quickly during a
live session.

**Fix:** In dashboard.html CSS, find the font-family declaration for
monospace/numeric fields. Replace with a font that renders a clean
open zero. Options:

    font-family: 'Consolas', 'Menlo', 'Monaco', monospace;

Consolas and Menlo both render clean zeros without slashes or dots.
They are system fonts available on Windows and Mac respectively —
no external font load needed.

If the font is applied globally, check that the change doesn't break
the terminal-style aesthetic of the dashboard. If monospace is only
used for specific elements (scores, prices, tick counter), scope the
change to those elements only.

**Priority:** Low — cosmetic but quick fix. Do in next dashboard.html pass.

---

## Settings Tab Cleanup
**Context:** Settings tab has grown organically and is now cluttered.
Needs a proper reorganization before more settings are added (paper trade
config, iv_floor/iv_ceiling, etc. are all coming).

**Proposed section structure:**
    Data & Feed          — symbol, expiry, poll_ms, strike_range, wall_range
    Scoring              — confirm_score, score_decay_threshold, iv_floor,
                           iv_ceiling, vol_surge_mult, open_gate_minutes
    Risk & Invalidation  — stop_pct, risk_cap, idea_cooldown_min
    Paper Trade          — paper_stop_pct, paper_target_1/2/3_pct
    Alerts               — warn_distance, critical_distance
    System               — test_mode, test_date, model_version

Each section as a collapsible card or clearly labeled group.
Descriptions for each setting so it's self-documenting.
Highlight settings that affect live behavior vs analysis-only.

**Priority:** Medium — do after paper trade config is added so settings
are reorganized once, not twice.

---

## AI Status Indicator + Pause Integration
**Context:** Need to know at a glance if AI commentary is active, and
when dashboard is paused the AI should stop generating too.

**Changes needed:**

### 1. AI status light in header
Add a small indicator next to the LIVE dot:
    AI dot — green when actively generating commentary
             amber when idle/waiting
             gray when paused or disabled
             red if API call failed

Position: between LIVE dot and v2.1 version badge, or directly
after the AI CHAT nav button — whichever is less cluttered.

### 2. Pause stops AI
When the PAUSE button is pressed:
- Dashboard already stops updating price/candidates
- AI commentary generation should also halt immediately
- Any in-flight API call should be abandoned or its result discarded
- AI dot goes gray
- On RESUME: AI dot returns to amber, resumes on next snapshot tick

### 3. AI CHAT tab indicator
If AI is generating while user is on a different tab, show a subtle
pulse or badge on the AI CHAT nav button so user knows output is ready.

**Implementation note:** The AI status needs to be driven by actual
API call state — not just a timer. Green only when a streaming response
is actively being received. This requires the frontend to track the
fetch/stream lifecycle, not just assume AI is "active."

**Priority:** Medium — do alongside or after status indicators item above.

---

## Status Indicators — Connection & Mode Display
**Context:** As the platform grows to include RTD, Schwab API, and eventually order
management, a single green dot is insufficient. Need a proper status system.

**Planned changes:**

### 1. Test mode indicator (quick win — do soon)
In dashboard.html, when test_mode=true in snapshot:
- Change "LIVE" label to "TEST"
- Change green dot to amber/yellow
- Prevents confusing test sessions for live ones mid-trade

### 2. RTD connection health (quick win — do soon)
The tick counter in snapshot increments every 500ms. If tick hasn't incremented
in >2-3 seconds, RTD feed has stopped.
- Dot goes red and blinks when stale
- Dot returns to green when ticking resumes

### 3. Full status bar (do when merging platforms)
Replace single dot with a dedicated status row that can grow over time:
- RTD: green/red/blinking (tick-based health check)
- Schwab API: gray (not connected) / green (responding) / red (failing)
- Broker: gray until order management is built
- Mode badge: LIVE / TEST / REPLAY

Design: small pill badges in top bar, each with colored dot + label.
Keeps the UI clean now and expandable later without layout changes.

**Priority:** Items 1 and 2 are quick wins — do in next dashboard.html pass.
Item 3 deferred until platform consolidation begins.

---

## PostgreSQL Migration — Platform Consolidation Prerequisite
**When to do this:** Before adding real-time position management or any multi-process
writes. Must happen before tos-dash-v2 and tos-api are merged into a single platform.
Do NOT do on a trading day — run migration on a weekend.

**Why PostgreSQL over SQLite for the end goal:**
- Real-time position management = multiple concurrent writers (dashboard + order manager
  + risk monitor). SQLite single-writer model will cause locking under this load.
- SaaS/sale readiness — buyers expect a production database, not a file.
- tos-api already runs PostgreSQL — one DB server for the combined platform is cleaner
  than SQLite (ideas) + PostgreSQL (everything else) side by side.
- Row-level locking, better indexing, JSONB support, replication — all matter at scale.

**Migration plan:**
1. Export ideas.db to CSV: sqlite3 ideas.db .mode csv .output ideas_export.csv .dump
2. Create ideas table in PostgreSQL matching current SQLite schema exactly
3. Import CSV — verify row counts match
4. Add indexes: surfaced_at, status, out_5m_correct, entry_regime, option_type
5. Update idea_logger.py connection string to PostgreSQL (use psycopg2 or asyncpg)
6. Update api.py any direct SQLite reads to PostgreSQL
7. Keep ideas.db as read-only archive — do not delete until 2 weeks of PostgreSQL
   operation confirmed clean
8. Add DB_URL to tos-dash-v2 .env (already defined in tos-api config.py)

**Future benefit:** Once on PostgreSQL, tos-api and tos-dash-v2 share one DB server.
Ideas, positions, S/R levels, account data all in one place. Combined platform
becomes a config change, not a data migration.

**Priority:** High — do this BEFORE position management work, not after.

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
