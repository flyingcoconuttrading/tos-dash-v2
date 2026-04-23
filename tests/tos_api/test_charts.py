"""
tos_api/test_charts.py — Chart data endpoint tests.
Covers: /chart-data daily/weekly/intraday
"""

import sys
import argparse
sys.path.insert(0, __file__.rsplit("\\", 2)[0])
from harness import Harness


def run(h: Harness):
    h.run("GET  /chart-data/AAPL (daily)",   lambda: _daily(h))
    h.run("GET  /chart-data/AAPL (weekly)",  lambda: _weekly(h))
    h.run("GET  /chart-data/AAPL/intraday",  lambda: _intraday(h))


def _daily(h):
    d = h.get("/chart-data/AAPL")
    assert d.get("timeframe") == "daily", f"timeframe wrong: {d.get('timeframe')}"
    assert len(d.get("bars", [])) > 0, "daily bars empty"


def _weekly(h):
    d = h.get("/chart-data/AAPL?timeframe=weekly")
    assert d.get("timeframe") == "weekly", f"timeframe wrong: {d.get('timeframe')}"
    assert len(d.get("bars", [])) > 0, "weekly bars empty"


def _intraday(h):
    d = h.get("/chart-data/AAPL/intraday")
    assert d.get("timeframe") == "intraday", f"timeframe wrong: {d.get('timeframe')}"
    bars = d.get("bars", [])
    assert len(bars) > 0, "intraday bars empty"
    first_dt = bars[0].get("datetime", "")
    assert first_dt.endswith("-04:00") or first_dt.endswith("-05:00"), \
        f"datetime not ET: {first_dt}"
    vwap_count = sum(1 for b in bars if b.get("vwap") is not None)
    assert vwap_count > 0, "no bars have VWAP"
    levels = d.get("intraday_levels", {})
    assert isinstance(levels, dict), "intraday_levels not a dict"
    assert "today_high" in levels or "prev_day_high" in levels, \
        f"intraday_levels missing expected keys: {levels}"


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://127.0.0.1:8002")
    args = p.parse_args()
    h = Harness(args.url, "charts")
    h.print_header()
    run(h)
    sys.exit(h.print_summary())
