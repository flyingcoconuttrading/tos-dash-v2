"""
tos_api/test_analyze.py — /analyze endpoint test.
Costs 4 AI calls. Run explicitly: python test_analyze.py
or via wrapper: python pvt.py --analyze
"""

import sys
import argparse
sys.path.insert(0, __file__.rsplit("\\", 2)[0])
from harness import Harness


def run(h: Harness):
    h.run("POST /analyze AAPL scalp (AI)", lambda: _analyze(h))


def _analyze(h):
    d = h.post("/analyze", {
        "ticker":       "AAPL",
        "account_size": 5000,
        "risk_percent": 2.0,
        "trade_type":   "scalp",
    }, timeout=120)
    assert "trade_plan"     in d, "missing trade_plan"
    assert "agent_verdicts" in d, "missing agent_verdicts"
    assert "sr_cache"       in d, "missing sr_cache"
    assert "trend"          in d, "missing trend"
    assert d["trade_plan"].get("verdict") in ("TRADE", "NO_TRADE", "TRADE_WAIT"), \
        f"unexpected verdict: {d['trade_plan'].get('verdict')}"


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://127.0.0.1:8002")
    args = p.parse_args()
    h = Harness(args.url, "analyze")
    h.print_header()
    run(h)
    sys.exit(h.print_summary())
