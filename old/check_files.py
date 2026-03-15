#!/usr/bin/env python3
"""
tos-dash-v2 File Integrity Checker
Run this from your C:\\Users\\randy\\tos-dash-v2\\ directory:

    cd C:\\Users\\randy\\tos-dash-v2
    python check_files.py

It checks each file against known-good checksums and reports
which files are correct, wrong version, or missing.
"""

import hashlib
import os
import sys
from pathlib import Path

# ── Known-good checksums from the dev session ─────────────────────────────────
EXPECTED = {
    "api.py":            ("14339b6cf2d9cd4a9be97f307100bd07", 22601),
    "scalp_advisor.py":  ("8a6199bcf4c5646cb7f0c59f48348865", 27180),
    "dashboard.html":    ("fce7f9d21243ec8150e42e16d226e66b", 82533),
    "idea_logger.py":    ("7b6a988b0a7f0a8cccf9488ba6ba1c48", 32517),
    "spy_writer.py":     ("e3788dd81260629c261a21ea4bad5743",  9804),
}

# ── Key markers that MUST exist inside each file ───────────────────────────────
MARKERS = {
    "api.py": [
        ("idea_cooldown_min",      "Change #1 cooldown config"),
        ("vol_surge_mult",         "Change #2 relative vol surge config"),
        ("iv_cap",                 "Change #4 IV cap config"),
        ("open_gate_minutes",      "Change #6 open gate config"),
        ("confirm_score.*55",      "Change #5 confirm score default=55"),
        ("chain_full",             "chain_full in snapshot"),
        ("positions",              "positions in snapshot"),
        ("cfg=cfg",                "cfg passed to get_recommendations"),
    ],
    "scalp_advisor.py": [
        ("_update_candle",         "Change #2 candle tracking"),
        ("_candle_confirms",       "Change #2 candle confirmation"),
        ("_last_candle_close",     "Change #2 candle close state"),
        ("_check_rel_vol_surge",   "Change #2 relative vol surge"),
        ("_idea_cooldown",         "Change #1 cooldown dict"),
        ("idea_cooldown_min",      "Change #1 cooldown config key"),
        ("in_gate",                "Change #6 open gate logic"),
        ("iv_cap",                 "Change #4 IV cap filter"),
        ("opt_type.*Put.*Uptrend", "Change #3 trend-side filter puts"),
        ("opt_type.*Call.*Downtrend", "Change #3 trend-side filter calls"),
        ("DEFAULT_CONFIRM_SCORE.*55", "Change #5 confirm score default"),
    ],
    "dashboard.html": [
        ("cfg_idea_cooldown_min",  "Change #1 cooldown in settings"),
        ("cfg_open_gate_minutes",  "Change #6 open gate in settings"),
        ("cfg_iv_cap",             "Change #4 IV cap in settings"),
        ("cfg_vol_surge_mult",     "Change #2 vol surge mult in settings"),
        ("Idea Filters",           "Idea Filters settings card"),
        ("chain-hide-bidask",      "Chain bid/ask toggle"),
        ("applyChainBidAsk",       "Chain bid/ask function"),
        ("chainRowClick.*lastC",   "Chain row click with last price"),
        ("pageChain",              "Chain page exists"),
        ("renderChain",            "renderChain function"),
    ],
    "idea_logger.py": [
        ("process_tick",           "process_tick method"),
        ("process_positions",      "RTD position auto-link"),
        ("_surface_new_idea",      "idea surfacing logic"),
        ("LEVEL_CROSS",            "invalidation: level cross"),
        ("SCORE_DECAY",            "invalidation: score decay"),
    ],
    "spy_writer.py": [
        ("POSITION_QTY",           "position qty RTD field"),
        ("AV_TRADE_PRICE",         "avg trade price RTD field"),
        ("positions.json",         "positions.json output"),
        ("IS_FUTURES",             "futures symbol support"),
    ],
}

# ── Helpers ────────────────────────────────────────────────────────────────────
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
RESET  = "\033[0m"
BOLD   = "\033[1m"

def md5(path):
    return hashlib.md5(open(path, "rb").read()).hexdigest()

def check_marker(content, pattern):
    import re
    return bool(re.search(pattern, content))

# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    base = Path(__file__).parent
    print(f"\n{BOLD}{'='*60}{RESET}")
    print(f"{BOLD}  tos-dash-v2 File Integrity Check{RESET}")
    print(f"  Checking: {base}")
    print(f"{BOLD}{'='*60}{RESET}\n")

    all_ok = True

    for filename, (expected_md5, expected_size) in EXPECTED.items():
        path = base / filename
        print(f"{BOLD}{filename}{RESET}")

        if not path.exists():
            print(f"  {RED}✗ MISSING — file not found{RESET}")
            print(f"    → Copy the latest version from the Claude outputs\n")
            all_ok = False
            continue

        actual_md5  = md5(path)
        actual_size = path.stat().st_size
        content     = path.read_text(encoding="utf-8", errors="replace")

        # Checksum match
        if actual_md5 == expected_md5:
            print(f"  {GREEN}✓ Checksum match{RESET}  ({actual_size:,} bytes)")
        else:
            size_diff = actual_size - expected_size
            print(f"  {YELLOW}⚠ Checksum mismatch{RESET}  "
                  f"(yours: {actual_size:,} bytes  expected: {expected_size:,}  diff: {size_diff:+,})")
            all_ok = False

        # Content markers
        markers = MARKERS.get(filename, [])
        marker_fails = []
        for pattern, description in markers:
            if not check_marker(content, pattern):
                marker_fails.append((pattern, description))

        if not marker_fails:
            print(f"  {GREEN}✓ All {len(markers)} feature markers present{RESET}")
        else:
            for pattern, desc in marker_fails:
                print(f"  {RED}✗ MISSING: {desc}{RESET}")
                print(f"    pattern: {pattern}")
            all_ok = False

        print()

    # Summary
    print(f"{BOLD}{'='*60}{RESET}")
    if all_ok:
        print(f"{GREEN}{BOLD}  ✓ ALL FILES OK — you're good to go{RESET}")
    else:
        print(f"{RED}{BOLD}  ✗ ISSUES FOUND — see above{RESET}")
        print(f"\n  Files to re-copy from Claude outputs:")
        for filename in EXPECTED:
            path = base / filename
            if not path.exists():
                print(f"    {RED}→ {filename}  (missing){RESET}")
            elif md5(path) != EXPECTED[filename][0]:
                print(f"    {YELLOW}→ {filename}  (wrong version){RESET}")
    print(f"{BOLD}{'='*60}{RESET}\n")

if __name__ == "__main__":
    main()
