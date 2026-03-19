#!/usr/bin/env bash
# Launches all three test scripts and streams output to terminal.
# Usage: bash testing/run_tests.sh [--tos-api-path ../tos-api]

# ── Detect Python ──────────────────────────────────────────────────────────────
if command -v python &>/dev/null; then
    PYTHON=python
elif command -v python3 &>/dev/null; then
    PYTHON=python3
else
    echo "ERROR: Python not found."
    exit 1
fi

# ── Parse arguments ────────────────────────────────────────────────────────────
TOS_API_PATH="../tos-api"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --tos-api-path)
            TOS_API_PATH="$2"
            shift 2
            ;;
        *)
            echo "Unknown argument: $1"
            shift
            ;;
    esac
done

# ── Pre-flight checks ──────────────────────────────────────────────────────────
if [ ! -f "data/ideas.db" ]; then
    echo "ERROR: data/ideas.db not found."
    echo "Run from project root: bash testing/run_tests.sh"
    exit 1
fi

# ── Create log directory ───────────────────────────────────────────────────────
mkdir -p testing/logs

# ── Launch scripts ─────────────────────────────────────────────────────────────
$PYTHON testing/test_invariants.py \
    > testing/logs/invariants.out 2>&1 &
PID1=$!

$PYTHON testing/test_outcomes.py \
    > testing/logs/outcomes.out 2>&1 &
PID2=$!

$PYTHON testing/test_rtd_schwab.py \
    --tos-api-path "$TOS_API_PATH" \
    > testing/logs/rtd_schwab.out 2>&1 &
PID3=$!

echo "$PID1 $PID2 $PID3" > testing/logs/test_pids.txt

echo ""
echo "  ✓ test_invariants  PID=$PID1"
echo "  ✓ test_outcomes    PID=$PID2"
echo "  ✓ test_rtd_schwab  PID=$PID3"
echo ""
echo "  Logs:        testing/logs/"
echo "  Violations:  testing/test_violations.log"
echo "  RTD compare: testing/test_rtd_compare.log"
echo ""
echo "  Press Ctrl+C to stop all scripts."
echo ""

# ── Cleanup on exit ────────────────────────────────────────────────────────────
cleanup() {
    echo ""
    echo "Stopping test scripts..."
    kill $PID1 $PID2 $PID3 2>/dev/null
    rm -f testing/logs/test_pids.txt
    echo "All test scripts stopped."
    exit 0
}
trap cleanup INT TERM

# ── Stream all three logs simultaneously ───────────────────────────────────────
tail -f testing/logs/invariants.out \
         testing/logs/outcomes.out \
         testing/logs/rtd_schwab.out
