"""
tos_api/test_plans.py — Plan validity endpoint tests (API-010).
Covers: /plans/summary /plans
"""

import sys
import argparse
sys.path.insert(0, __file__.rsplit("\\", 2)[0])
from harness import Harness


def run(h: Harness):
    h.run("GET  /plans/summary", lambda: _summary(h))
    h.run("GET  /plans?limit=10", lambda: _list(h))


def _summary(h):
    d = h.get("/plans/summary")
    for k in ("pending", "waiting", "triggered", "invalidated", "expired", "total"):
        assert k in d, f"plans/summary missing key: {k}"


def _list(h):
    d = h.get("/plans?limit=10")
    assert isinstance(d, list), f"/plans not list: {type(d)}"


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://127.0.0.1:8002")
    args = p.parse_args()
    h = Harness(args.url, "plans")
    h.print_header()
    run(h)
    sys.exit(h.print_summary())
