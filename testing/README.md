# tos-dash-v2 Testing Suite

## Quick start
    cd ~/tos-dash-v2
    bash testing/run_tests.sh --tos-api-path ../tos-api

## Stop all scripts
    bash testing/stop_tests.sh
    OR press Ctrl+C in the run_tests.sh terminal

## What each script does

test_invariants.py — watches ideas.db every 3 seconds and checks every
new idea for logic violations. Catches bugs the moment they surface.

test_outcomes.py — prints a live hit rate summary every 10 minutes
broken down by regime, trend, surge, direction, and score bucket.
Also shows stop analysis: how often 1m is wrong but 5m is correct.

test_rtd_schwab.py — compares RTD snapshot data against Schwab API
every 5 minutes. Validates data quality and seeds the RTD/Schwab
comparison for eventual platform merge.

## Log files
    testing/logs/invariants.out    live invariant checker output
    testing/logs/outcomes.out      live hit rate output
    testing/logs/rtd_schwab.out    live RTD vs Schwab output
    testing/test_violations.log    violation records (persistent)
    testing/test_rtd_compare.log   comparison records (persistent)

## What to watch for

test_invariants:
    DEX_BIAS_CONSISTENCY violations → market_structure fix not applying
    TREND_FILTER violations → scalp_advisor filter not working
    OUTCOME_CORRECT_LOGIC → put inversion bug re-appeared

test_outcomes:
    1m hit rate much lower than 5m → stops too tight
    Puts outperforming calls in downtrend → bias fix working
    PINNED hit rate > TRENDING → regime scoring fix working

test_rtd_schwab:
    FAIL on price consistently → RTD data quality issue
    Price diff > $0.50 → investigate RTD feed immediately

## When to be concerned
    More than 2 DEX_BIAS_CONSISTENCY violations per session
    1m hit rate < 20% while 5m hit rate > 35%
    RTD price diverges > $0.50 from Schwab consistently
