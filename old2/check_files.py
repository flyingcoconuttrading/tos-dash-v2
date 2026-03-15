#!/usr/bin/env python3
"""
tos-dash-v2 File Integrity Checker
Run this from your C:\\Users\\randy\\tos-dash-v2\\ directory:

    cd C:\\Users\\randy\\tos-dash-v2
    python check_files.py

Updated: 2026-03-11 (post V2-decoupling + checklist rebuild session)
"""

import hashlib
import re
import sys
from pathlib import Path

EXPECTED = {
    'api.py':              ('ac402cb1f821036ff4823b3e83147315', 24025),
    'scalp_advisor.py':    ('f344ad93f227af36e072171e391ba4bf', 27485),
    'dashboard.html':      ('75fcd58119b633936390b77d2d7c1e33', 86670),
    'idea_logger.py':      ('7b6a988b0a7f0a8cccf9488ba6ba1c48', 33315),
    'spy_writer.py':       ('b23aff04efeda775c973a8e3229fdd2f', 10854),
    'market_structure.py': ('feb05f11089a1ccb6b48c1242a0c4674', 20236),
    'gamma_chart.py':      ('432968fbf8498dd774a4f90031efadb7', 19951),
    'volume_tracker.py':   ('6e6c19ff3cc5c4922d07fbfaad0a1a8c',  7360),
}

MARKERS = {
    'api.py': [
        (r'idea_cooldown_min',          'Change #1 cooldown config'),
        (r'vol_surge_mult',             'Change #2 rel vol surge config'),
        (r'iv_cap',                     'Change #4 IV cap config'),
        (r'open_gate_minutes',          'Change #6 open gate config'),
        (r'confirm_score.*55',          'Change #5 confirm score default=55'),
        (r'rtd_heartbeat_ms',           'Step1: rtd_heartbeat_ms in config'),
        (r'from gamma_chart import',    'Step1: gamma_chart local import'),
        (r'import market_structure',    'Step1: market_structure local import'),
        (r'from volume_tracker import', 'Step1: volume_tracker local import'),
        (r'from scalp_advisor import',  'Step1: scalp_advisor local import'),
        (r'checklist_score',            'checklist score field in snapshot'),
        (r'"weight": f\.weight',        'factor weight in snapshot'),
        (r'cfg=cfg',                    'cfg passed to get_recommendations'),
    ],
    'scalp_advisor.py': [
        (r'_update_candle',             'Change #2 candle tracking'),
        (r'_candle_confirms',           'Change #2 candle confirmation'),
        (r'_idea_cooldown',             'Change #1 cooldown dict'),
        (r'_check_rel_vol_surge',       'Change #2 rel vol surge'),
        (r'in_gate',                    'Change #6 open gate logic'),
        (r'iv_cap',                     'Change #4 IV cap filter'),
        (r'opt_type.*Put.*Uptrend',     'Change #3 trend filter puts'),
        (r'opt_type.*Call.*Downtrend',  'Change #3 trend filter calls'),
        (r'zoneinfo',                   'zoneinfo (not pytz)'),
        (r'DEFAULT_CONFIRM_SCORE\s*=\s*55', 'Change #5 confirm score=55'),
    ],
    'market_structure.py': [
        (r'_build_checklist',           'checklist builder'),
        (r'DirectionalChecklist',       'checklist dataclass'),
        (r'ChecklistFactor',            'factor dataclass'),
        (r'FACTOR_WEIGHTS',             'weight dict'),
        (r'CL_MOMENTUM',                'momentum factor'),
        (r'CL_WALLS',                   'walls factor'),
        (r'CL_CANDLE',                  'candle factor'),
        (r'CL_REGIME',                  'regime factor'),
        (r'_price_history',             'momentum price history'),
        (r'_update_candle',             'candle tracker'),
        (r'scored_w',                   'weighted scoring'),
        (r'checklist=checklist',        'checklist in return'),
    ],
    'dashboard.html': [
        (r'cfg_idea_cooldown_min',      'Change #1 cooldown in settings'),
        (r'cfg_open_gate_minutes',      'Change #6 open gate in settings'),
        (r'cfg_iv_cap',                 'Change #4 IV cap in settings'),
        (r'cfg_vol_surge_mult',         'Change #2 vol surge mult in settings'),
        (r'Idea Filters',               'Idea Filters settings card'),
        (r'clBarFill',                  'checklist score bar'),
        (r'clScoreVal',                 'checklist score value'),
        (r'cl-score-bar-track',         'score bar CSS'),
        (r'cl-weight',                  'factor weight badge'),
        (r'checklist_score',            'score field in renderChecklist'),
        (r'renderChain',                'chain tab renderChain function'),
        (r'pageChain',                  'chain tab page'),
    ],
    'idea_logger.py': [
        (r'process_tick',               'process_tick method'),
        (r'_surface_new_idea',          'idea surfacing logic'),
        (r'LEVEL_CROSS',                'invalidation: level cross'),
        (r'SCORE_DECAY',                'invalidation: score decay'),
        (r'process_positions',          'RTD position auto-link'),
    ],
    'spy_writer.py': [
        (r'POSITION_QTY',               'position qty RTD field'),
        (r'AV_TRADE_PRICE',             'avg trade price RTD field'),
        (r'positions\.json',            'positions.json output'),
        (r'IS_FUTURES',                 'futures symbol support'),
        (r'rtd_heartbeat_ms',           'Step1: SETTINGS replaced by config.json'),
    ],
    'gamma_chart.py': [
        (r'calculate_max_pain',         'calculate_max_pain function'),
        (r'calculate_walls',            'calculate_walls function'),
    ],
    'volume_tracker.py': [
        (r'VolumeTracker',              'VolumeTracker class'),
        (r'get_surge_table',            'get_surge_table method'),
    ],
}

BANNED = {
    'api.py': [
        (r'from src\.ui',               'src.ui import (should be local now)'),
        (r'tos-streamlit-dashboard',    'V2 folder reference'),
    ],
    'spy_writer.py': [
        (r'from src\.core\.settings import SETTINGS', 'SETTINGS import (replaced by config.json)'),
    ],
}

GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
RESET  = "\033[0m"
BOLD   = "\033[1m"

def md5(path):
    return hashlib.md5(open(path, 'rb').read()).hexdigest()

def main():
    base = Path(__file__).parent
    print(f"\n{BOLD}{'='*62}{RESET}")
    print(f"{BOLD}  tos-dash-v2 File Integrity Check{RESET}")
    print(f"  Directory: {base}")
    print(f"{BOLD}{'='*62}{RESET}\n")

    all_ok   = True
    problems = []

    for filename, (expected_md5, expected_size) in EXPECTED.items():
        path = base / filename
        print(f"{BOLD}{filename}{RESET}")

        if not path.exists():
            print(f"  {RED}✗ MISSING{RESET}")
            problems.append((filename, 'missing'))
            all_ok = False
            print()
            continue

        actual_md5  = md5(path)
        actual_size = path.stat().st_size
        content     = path.read_text(encoding='utf-8', errors='replace')

        if actual_md5 == expected_md5:
            print(f"  {GREEN}✓ Checksum OK{RESET}  ({actual_size:,} bytes)")
        else:
            diff = actual_size - expected_size
            print(f"  {YELLOW}⚠ Checksum mismatch{RESET}  "
                  f"(yours {actual_size:,}b  expected {expected_size:,}b  {diff:+,}b)")
            problems.append((filename, 'checksum'))
            all_ok = False

        fails = [(p, d) for p, d in MARKERS.get(filename, [])
                 if not re.search(p, content)]
        if not fails:
            print(f"  {GREEN}✓ All {len(MARKERS.get(filename, []))} feature markers present{RESET}")
        else:
            for _, desc in fails:
                print(f"  {RED}✗ MISSING: {desc}{RESET}")
            all_ok = False

        bans = [(p, d) for p, d in BANNED.get(filename, [])
                if re.search(p, content)]
        if bans:
            for _, desc in bans:
                print(f"  {RED}✗ BANNED PATTERN: {desc}{RESET}")
            all_ok = False
        elif BANNED.get(filename):
            print(f"  {GREEN}✓ No banned V2 references{RESET}")

        print()

    print(f"{BOLD}{'='*62}{RESET}")
    if all_ok:
        print(f"{GREEN}{BOLD}  ✓ ALL FILES OK — ready to run{RESET}")
    else:
        print(f"{RED}{BOLD}  ✗ ISSUES FOUND{RESET}\n")
        for filename, reason in problems:
            tag = f'{RED}MISSING{RESET}' if reason == 'missing' else f'{YELLOW}WRONG VERSION{RESET}'
            print(f"  → {filename}  ({tag})")
        print(f"\n  Copy the latest versions from the Claude outputs folder.")
    print(f"{BOLD}{'='*62}{RESET}\n")

if __name__ == '__main__':
    main()
