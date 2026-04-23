"""
tos_api/test_core.py — Core endpoint tests for tos-api (port 8002).
Covers: /health /stats /ai/status /settings /watchlist /scan/status
"""

import sys
import argparse
sys.path.insert(0, __file__.rsplit("\\", 2)[0])  # add tests\ to path
from harness import Harness


def run(h: Harness):
    h.run("GET  /health",      lambda: _health(h))
    h.run("GET  /stats",       lambda: _stats(h))
    h.run("GET  /ai/status",   lambda: _ai_status(h))
    h.run("GET  /settings",    lambda: _settings(h))
    h.run("GET  /watchlist",   lambda: _watchlist(h))
    h.run("GET  /scan/status", lambda: _scan_status(h))


def _health(h):
    d = h.get("/health")
    assert d.get("status") == "ok", f"status != ok: {d}"
    assert "version" in d, "missing version"


def _stats(h):
    d = h.get("/stats")
    for k in ("uptime_seconds", "total_calls", "ai_calls", "trade_db"):
        assert k in d, f"missing key: {k}"


def _ai_status(h):
    d = h.get("/ai/status")
    assert "ai_enabled" in d, "missing ai_enabled"
    assert "ai_calls"   in d, "missing ai_calls"


def _settings(h):
    d = h.get("/settings")
    for k in ("moving_averages", "gap_detection", "risk", "ai_enabled"):
        assert k in d, f"missing key: {k}"


def _watchlist(h):
    d = h.get("/watchlist")
    assert "default" in d,              "missing 'default'"
    assert isinstance(d["default"], list), "'default' not a list"
    assert len(d["default"]) > 0,      "'default' is empty"


def _scan_status(h):
    d = h.get("/scan/status")
    for k in ("auto_enabled", "interval_minutes", "score_threshold",
              "default_trade_type"):
        assert k in d, f"missing key: {k}"


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://127.0.0.1:8002")
    args = p.parse_args()
    h = Harness(args.url, "core")
    h.print_header()
    run(h)
    sys.exit(h.print_summary())
