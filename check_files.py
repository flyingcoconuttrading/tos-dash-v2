"""
check_files.py — tos-dash-v2 version checker
Run from C:\\Users\\randy\\tos-dash-v2\\
    python check_files.py
"""

import hashlib
import os
import sys
from pathlib import Path

# ── Expected checksums (MD5) ──────────────────────────────────────────────────
# Updated: 2026-03-13
EXPECTED = {
    "api.py":              ("9d0ddb1b171607e1dd221e27a7de1fbd",  661),
    "gamma_chart.py":      ("030d07dac2c0c6728c0de6f14c63e06c",  517),
    "idea_logger.py":      ("d8cc52159b3b55fb11d30d6e69fab9e7",  691),
    "market_structure.py": ("feb05f11089a1ccb6b48c1242a0c4674",  497),
    "scalp_advisor.py":    ("6e289ddd08e44f49c5e744dd89ed8fda",  655),
    "spy_writer.py":       ("0de3994a604c8e84470b0a4a2789083f",  263),
    "volume_tracker.py":   ("6e6c19ff3cc5c4922d07fbfaad0a1a8c",  209),
    "dashboard.html":      ("156a4ae5e57c4b4368b0438914456901", 2269),
}

HERE = Path(__file__).parent

GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
RESET  = "\033[0m"
BOLD   = "\033[1m"

def md5(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()

def line_count(path: Path) -> int:
    with open(path, encoding="utf-8", errors="ignore") as f:
        return sum(1 for _ in f)

def main():
    print(f"\n{BOLD}tos-dash-v2 version checker{RESET}  ({HERE})\n")
    print(f"{'FILE':<22} {'STATUS':<10} {'LINES':>6}  {'EXPECTED':>6}  HASH")
    print("─" * 72)

    all_ok   = True
    missing  = []
    modified = []
    ok_files = []

    for filename, (expected_hash, expected_lines) in EXPECTED.items():
        path = HERE / filename

        if not path.exists():
            print(f"{RED}{filename:<22} MISSING{RESET}")
            missing.append(filename)
            all_ok = False
            continue

        actual_hash  = md5(path)
        actual_lines = line_count(path)

        if actual_hash == expected_hash:
            status = f"{GREEN}OK{RESET}"
            line_info = f"{actual_lines:>6}  {expected_lines:>6}"
            print(f"{filename:<22} {status:<19} {line_info}  {actual_hash[:12]}…")
            ok_files.append(filename)
        else:
            delta = actual_lines - expected_lines
            delta_str = f"{'+' if delta >= 0 else ''}{delta}"
            status = f"{YELLOW}MODIFIED{RESET}"
            line_info = f"{actual_lines:>6}  {expected_lines:>6}  ({delta_str} lines)"
            print(f"{filename:<22} {status:<19} {line_info}")
            print(f"  {CYAN}expected:{RESET} {expected_hash}")
            print(f"  {CYAN}actual:  {RESET} {actual_hash}")
            modified.append(filename)
            all_ok = False

    print("─" * 72)

    total_lines = sum(line_count(HERE / f) for f in EXPECTED if (HERE / f).exists())
    print(f"\n{'Total lines:':<22} {total_lines:>6}")
    print(f"{'Files checked:':<22} {len(EXPECTED):>6}")
    print(f"{'OK:':<22} {len(ok_files):>6}  {GREEN}{', '.join(ok_files) if ok_files else '—'}{RESET}")

    if modified:
        print(f"{'Modified:':<22} {len(modified):>6}  {YELLOW}{', '.join(modified)}{RESET}")
    if missing:
        print(f"{'Missing:':<22} {len(missing):>6}  {RED}{', '.join(missing)}{RESET}")

    if all_ok:
        print(f"\n{GREEN}{BOLD}✓ All files match expected checksums.{RESET}\n")
    else:
        print(f"\n{YELLOW}{BOLD}⚠  Some files differ from expected.{RESET}")
        print(f"  Modified files have been changed since last baseline update.")
        print(f"  Run this after deploying new files to confirm deployment.\n")

    return 0 if all_ok else 1

if __name__ == "__main__":
    sys.exit(main())
