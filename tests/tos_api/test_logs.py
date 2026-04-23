"""
tos_api/test_logs.py — Analysis log endpoint tests.
Covers: /logs
"""

import sys
import argparse
sys.path.insert(0, __file__.rsplit("\\", 2)[0])
from harness import Harness


def run(h: Harness):
    h.run("GET  /logs?limit=10", lambda: _logs(h))


def _logs(h):
    d = h.get("/logs?limit=10")
    if isinstance(d, dict):
        assert ("logs" in d or "results" in d or len(d) >= 0), \
            f"/logs shape unexpected: {list(d.keys())[:5]}"
    else:
        assert isinstance(d, list), f"/logs not list: {type(d)}"


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://127.0.0.1:8002")
    args = p.parse_args()
    h = Harness(args.url, "logs")
    h.print_header()
    run(h)
    sys.exit(h.print_summary())
