"""
tos_api/test_scan.py — Scanner endpoint tests.
Covers: /scan/status
"""

import sys
import argparse
sys.path.insert(0, __file__.rsplit("\\", 2)[0])
from harness import Harness


def run(h: Harness):
    h.run("GET  /scan/status", lambda: _scan_status(h))


def _scan_status(h):
    d = h.get("/scan/status")
    for k in ("auto_enabled", "interval_minutes", "score_threshold",
              "default_trade_type"):
        assert k in d, f"missing key: {k}"


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://127.0.0.1:8002")
    args = p.parse_args()
    h = Harness(args.url, "scan")
    h.print_header()
    run(h)
    sys.exit(h.print_summary())
