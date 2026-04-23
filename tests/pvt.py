"""
pvt.py — PVT wrapper for all tos-api and tos-dash-v2 test modules.

Usage:
    python pvt.py                       # all non-AI tests
    python pvt.py --analyze             # include AI /analyze test
    python pvt.py --only charts         # run one module
    python pvt.py --skip plans          # skip one module
    python pvt.py --api-url http://...  # override tos-api URL
    python pvt.py --dash-url http://... # override tos-dash URL

Run individual modules directly:
    python tos_api/test_charts.py
    python tos_api/test_analyze.py
"""

import argparse
import sys
from pathlib import Path

# Ensure tests\ is on path for harness import inside modules
sys.path.insert(0, str(Path(__file__).parent))

from harness import Harness

from tos_api import (
    test_core,
    test_charts,
    test_market,
    test_trades,
    test_logs,
    test_scan,
    test_plans,
    test_analyze,
)
from tos_dash import test_backtest

_GREEN = "\033[92m"
_RED   = "\033[91m"
_RESET = "\033[0m"

_TOS_API_MODULES = {
    "core":     test_core,
    "charts":   test_charts,
    "market":   test_market,
    "trades":   test_trades,
    "logs":     test_logs,
    "scan":     test_scan,
    "plans":    test_plans,
}

_TOS_DASH_MODULES = {
    "backtest": test_backtest,
}


def main():
    p = argparse.ArgumentParser(description="PVT wrapper — tos-api + tos-dash-v2")
    p.add_argument("--api-url",  default="http://127.0.0.1:8002",
                   help="tos-api base URL")
    p.add_argument("--dash-url", default="http://127.0.0.1:8001",
                   help="tos-dash-v2 base URL")
    p.add_argument("--analyze",  action="store_true",
                   help="Include /analyze AAPL test (costs 4 AI calls)")
    p.add_argument("--only",     metavar="MODULE",
                   help="Run only this module (core/charts/market/trades/logs/scan/plans/backtest/analyze)")
    p.add_argument("--skip",     metavar="MODULE",
                   help="Skip this module")
    args = p.parse_args()

    total_passed  = 0
    total_failed  = 0
    total_skipped = 0

    def _run_module(name: str, module, url: str):
        nonlocal total_passed, total_failed, total_skipped
        if args.only and args.only != name:
            return
        if args.skip and args.skip == name:
            print(f"\n  [SKIP] {name} (--skip)")
            return
        h = Harness(url, name)
        h.print_header()
        module.run(h)
        total_passed  += h.passed
        total_failed  += h.failed
        total_skipped += h.skipped

    # tos-api modules
    for name, mod in _TOS_API_MODULES.items():
        _run_module(name, mod, args.api_url)

    # analyze (opt-in)
    if args.analyze:
        _run_module("analyze", test_analyze, args.api_url)
    else:
        if not args.only or args.only == "analyze":
            print(f"\n  [SKIP] analyze (use --analyze to enable)")
            total_skipped += 1

    # tos-dash modules
    for name, mod in _TOS_DASH_MODULES.items():
        _run_module(name, mod, args.dash_url)

    # Grand summary
    total = total_passed + total_failed + total_skipped
    print(f"\n{'='*60}")
    if total_failed == 0:
        print(f"{_GREEN}ALL PASS{_RESET}  "
              f"{total_passed}/{total}  (skipped: {total_skipped})")
        sys.exit(0)
    else:
        print(f"{_RED}FAILURES{_RESET}  "
              f"{total_passed} pass / {total_failed} fail / "
              f"{total_skipped} skip / {total} total")
        sys.exit(1)


if __name__ == "__main__":
    main()
