"""
check_files.py — tos-dash-v2 file version checker
Run: python check_files.py
"""
__version__ = "2.0.0"  # rewrite: version-string based (replaces MD5 checksum approach)

import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent

GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
RESET  = "\033[0m"
BOLD   = "\033[1m"
DIM    = "\033[2m"


def get_version(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
        m = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', text, re.MULTILINE)
        return m.group(1) if m else "—"
    except Exception:
        return "?"


def line_count(path: Path) -> int:
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            return sum(1 for _ in f)
    except Exception:
        return 0


def git_log(path: Path) -> tuple:
    """Returns (short_hash, relative_age, subject_truncated)."""
    try:
        rel = str(path.relative_to(HERE)).replace("\\", "/")
        result = subprocess.run(
            ["git", "log", "-1", "--format=%h|%ar|%s", "--", rel],
            capture_output=True, text=True, cwd=HERE,
        )
        if result.returncode == 0 and result.stdout.strip():
            parts = result.stdout.strip().split("|", 2)
            hash_ = parts[0] if len(parts) > 0 else "?"
            age   = parts[1] if len(parts) > 1 else "?"
            subj  = parts[2][:48] if len(parts) > 2 else ""
            return hash_, age, subj
    except Exception:
        pass
    return "?", "?", ""


def collect_files() -> list:
    files = []
    for p in sorted(HERE.glob("*.py")):
        if p.name != "__init__.py":
            files.append(p)
    rtd = HERE / "rtd"
    if rtd.exists():
        for p in sorted(rtd.glob("*.py")):
            if p.name != "__init__.py":
                files.append(p)
    html = HERE / "dashboard.html"
    if html.exists():
        files.append(html)
    return files


def main():
    files = collect_files()

    print(f"\n{BOLD}tos-dash-v2 version checker{RESET}  ({HERE})\n")
    print(f"{'FILE':<30} {'VER':<12} {'LINES':>6}  {'HASH':<9}  {'WHEN':<20}  LAST COMMIT")
    print("-" * 108)

    versioned   = []
    unversioned = []

    for path in files:
        rel   = str(path.relative_to(HERE)).replace("\\", "/")
        ver   = get_version(path) if path.suffix == ".py" else "—"
        lines = line_count(path)
        hash_, age, subj = git_log(path)

        if ver not in ("—", "?"):
            versioned.append(rel)
            ver_col = f"{GREEN}{ver:<12}{RESET}"
        else:
            unversioned.append(rel)
            ver_col = f"{DIM}{'-':<12}{RESET}"

        print(f"{rel:<30} {ver_col} {lines:>6}  {hash_:<9}  {age:<20}  {DIM}{subj}{RESET}")

    print("-" * 108)
    print(f"\n{BOLD}Summary{RESET}")
    print(f"  Files found:      {len(files)}")
    if versioned:
        print(f"  {GREEN}Versioned:{RESET}        {len(versioned)}  ->  {', '.join(versioned)}")
    if unversioned:
        print(f"  {YELLOW}No version yet:{RESET}   {len(unversioned)}  ->  add __version__ when you next touch these")
    print()
    print(f"  {DIM}Convention: __version__ = \"MAJOR.MINOR.PATCH\"")
    print(f"  MAJOR = breaking change   MINOR = new feature/significant fix   PATCH = small fix{RESET}\n")


if __name__ == "__main__":
    sys.exit(main())
