"""
tos_api/test_market.py — Market data endpoint tests.
Covers: /quote /sr-cache
"""

import sys
import json
import argparse
sys.path.insert(0, __file__.rsplit("\\", 2)[0])
from harness import Harness


def run(h: Harness):
    h.run("GET  /quote/SPY",    lambda: _quote(h))
    h.run("GET  /sr-cache/SPY", lambda: _sr_cache(h))


def _quote(h):
    d    = h.get("/quote/SPY")
    flat = json.dumps(d)
    assert ("price" in flat.lower() or "last" in flat.lower()
            or "mark" in flat.lower()), \
        f"quote lacks price field: {flat[:200]}"


def _sr_cache(h):
    d = h.get("/sr-cache/SPY")
    assert "trend" in d, "sr-cache missing 'trend'"
    trend = d["trend"]
    for k in ("daily", "weekly", "mtf_alignment", "trade_bias"):
        assert k in trend, f"trend missing key: {k}"


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://127.0.0.1:8002")
    args = p.parse_args()
    h = Harness(args.url, "market")
    h.print_header()
    run(h)
    sys.exit(h.print_summary())
