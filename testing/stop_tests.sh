#!/usr/bin/env bash
# Stops all running test scripts.
# Use if run_tests.sh was closed without Ctrl+C.

if [ ! -f "testing/logs/test_pids.txt" ]; then
    echo "No PID file found. Scripts may not be running."
    exit 0
fi

read -r PID1 PID2 PID3 < testing/logs/test_pids.txt

for PID in $PID1 $PID2 $PID3; do
    if kill -0 $PID 2>/dev/null; then
        kill $PID
        echo "  Stopped PID=$PID"
    else
        echo "  PID=$PID already stopped"
    fi
done

rm -f testing/logs/test_pids.txt
echo "Done."
