# testing/

Three monitoring scripts for tos-dash-v2. Run during live sessions to validate
that logic fixes held, outcomes are tracking correctly, and RTD data matches
the Schwab feed.

---

## How to run

Open three separate terminals from the project root (`~/tos-dash-v2`):

```
Terminal 1:  python testing/test_invariants.py
Terminal 2:  python testing/test_outcomes.py
Terminal 3:  python testing/test_rtd_schwab.py --tos-api-path ../tos-api
```

---

## What each script does

### test_invariants.py
Polls `data/ideas.db` every 3 seconds for new idea rows and checks each one
against 11 logical invariants. Any failure is a `VIOLATION` — printed to the
terminal and written to `testing/test_violations.log`.

Invariants checked:
- Score in range [40, 80] and soft ceiling applied (≤76.9)
- Mark > $0.01, spread ≤ 50%
- Direction matches option type (Call=Bullish, Put=Bearish)
- Trend filter applied (no Calls on Downtrend, no Puts on Uptrend)
- Regime and bias values are valid known strings
- DEX bias consistency in TRENDING regime (core market_structure.py fix)
- IV ≤ 100% (data quality guard)
- Outcome correct flags match mark direction for all time windows

Prints a `.` heartbeat every 10 polls and a 30-minute summary.

### test_outcomes.py
Queries today's ideas from `data/ideas.db` every 10 minutes and prints a
performance breakdown. No API calls — SQLite only.

Shows hit rates and average PnL for all time windows (1m/2m/3m/4m/5m/10m/15m/30m),
broken down by regime, trend, surge, direction, and score bucket.

Also shows a stop analysis: ideas where 1m correct=0 but 5m correct=1 (stopped
out too early before the trade worked).

### test_rtd_schwab.py
Every 5 minutes, reads the live RTD price snapshot (`spy_price.json`) and
compares it against a fresh Schwab API quote. Logs all comparisons to
`testing/test_rtd_compare.log`.

Also logs Schwab VWAP and day high/low as informational fields (RTD does not
expose these in the snapshot).

Call budget: ~72 Schwab quote calls per 6-hour session — well within limits.

---

## Log files produced

```
testing/test_violations.log    — logic violations from invariant checker
testing/test_rtd_compare.log   — RTD vs Schwab price comparisons per interval
```

Both log files are append-only and should not be committed to git.

---

## What to look for

**test_invariants.py**
- Any `VIOLATION` line means a logic rule failed on a real idea
- `DEX_BIAS_CONSISTENCY` violations mean the market_structure.py fix did not apply
- `TREND_FILTER` violations mean calls/puts are surfacing against the trend

**test_outcomes.py**
- 1m hit rate close to 5m hit rate = setup is working, stops may be too tight
- 1m hit rate significantly lower than 5m = stops are too tight (normal; see stop analysis)
- Regime breakdown shows which regime is most productive

**test_rtd_schwab.py**
- `FAIL` on PRICE = RTD and Schwab diverging — investigate data quality
- Normal drift is < $0.05 due to bid/ask and timing differences

---

## When to be concerned

| Signal | Threshold |
|--------|-----------|
| DEX_BIAS_CONSISTENCY violations | > 2 per session |
| 1m hit rate vs 5m hit rate gap | 1m < 20% while 5m > 35% |
| RTD price divergence | > $0.50 consistently across multiple intervals |
| IV_REASONABLE violations | Any (data error in feed) |
