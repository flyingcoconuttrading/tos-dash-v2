"""
tos_api/test_trades.py — Trade tracker endpoint tests.
Covers: /trades list
"""

import sys
import argparse
sys.path.insert(0, __file__.rsplit("\\", 2)[0])
from harness import Harness


def run(h: Harness):
    h.run("GET  /trades?limit=10", lambda: _trades_list(h))


def _trades_list(h):
    d = h.get("/trades?limit=10")
    assert isinstance(d, list), f"/trades did not return list: {type(d)}"


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://127.0.0.1:8002")
    args = p.parse_args()
    h = Harness(args.url, "trades")
    h.print_header()
    run(h)
    sys.exit(h.print_summary())
