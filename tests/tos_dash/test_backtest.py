"""
tos_dash/test_backtest.py — tos-dash-v2 backtest endpoint tests (placeholder).
Port 8001. Tests to be added when tos-dash-v2 PVT scope is defined.
"""

import sys
import argparse
sys.path.insert(0, __file__.rsplit("\\", 2)[0])
from harness import Harness


def run(h: Harness):
    h.run("GET  /spy-context", lambda: _spy_context(h))


def _spy_context(h):
    d = h.get("/spy-context")
    assert "available" in d, "spy-context missing 'available'"


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://127.0.0.1:8001")
    args = p.parse_args()
    h = Harness(args.url, "tos-dash")
    h.print_header()
    run(h)
    sys.exit(h.print_summary())
