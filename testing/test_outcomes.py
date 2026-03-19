"""
testing/test_outcomes.py
Rolling performance monitor — queries ideas.db every 10 minutes and
prints a live hit rate summary broken down by regime, trend, surge,
direction, and score bucket.

Zero API calls — SQLite only.

Run from project root:
    python testing/test_outcomes.py
"""

import os
import sqlite3
import time
from datetime import datetime, date
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────────────────
THIS_DIR  = Path(__file__).parent
ROOT_DIR  = THIS_DIR.parent
DB_PATH   = Path(os.environ.get("IDEAS_DB", ROOT_DIR / "data" / "ideas.db"))

REFRESH_SEC   = 10 * 60   # 10 minutes
MIN_IDEAS     = 5
OUTCOME_WINDOWS = [1, 2, 3, 4, 5, 10, 15, 30]


# ── DB query ──────────────────────────────────────────────────────────────────

def get_columns(conn: sqlite3.Connection) -> set:
    rows = conn.execute("PRAGMA table_info(ideas)").fetchall()
    return {r[1] for r in rows}


def fetch_today(conn: sqlite3.Connection) -> list:
    today = date.today().isoformat()
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM ideas WHERE surfaced_at >= ? ORDER BY id ASC",
        (today,)
    ).fetchall()
    return [dict(r) for r in rows]


# ── Stat helpers ──────────────────────────────────────────────────────────────

def hit_rate(rows, w):
    col_m = f"out_{w}m_mark"
    col_c = f"out_{w}m_correct"
    col_p = f"out_{w}m_pnl_pct"
    filled = [r for r in rows if r.get(col_c) is not None]
    if not filled:
        return None, None, 0
    hits = sum(1 for r in filled if r[col_c] == 1)
    pnls = [r[col_p] for r in filled if r.get(col_p) is not None]
    avg_pnl = sum(pnls) / len(pnls) if pnls else None
    return hits / len(filled) * 100, avg_pnl, len(filled)


def group_hit_rate(rows, group_key, group_val, w=5):
    subset = [r for r in rows if r.get(group_key) == group_val]
    col_c  = f"out_{w}m_correct"
    filled = [r for r in subset if r.get(col_c) is not None]
    if not filled:
        return None, 0
    hits = sum(1 for r in filled if r[col_c] == 1)
    return hits / len(filled) * 100, len(filled)


def score_bucket(score):
    if score is None:
        return None
    if score < 55:
        return "50-55"
    if score < 60:
        return "55-60"
    if score < 65:
        return "60-65"
    if score < 70:
        return "65-70"
    return "70+"


# ── Display ───────────────────────────────────────────────────────────────────

def fmt_rate(rate, n):
    if rate is None:
        return f"  {'n/a':>5}   (n={n})"
    return f"  {rate:>5.1f}%  (n={n})"


def fmt_pnl(pnl):
    if pnl is None:
        return "  n/a"
    sign = "+" if pnl >= 0 else ""
    return f"  avg pnl: {sign}{pnl:.1f}%"


def print_report(rows, columns, start_time):
    now = datetime.now()
    elapsed = now - start_time
    hrs, rem = divmod(int(elapsed.total_seconds()), 3600)
    mins = rem // 60
    print(f"\n{'='*60}")
    print(f"  {now.strftime('%Y-%m-%d %H:%M:%S')}  |  session: {hrs}h {mins}m")
    print(f"  Today's ideas: {len(rows)} total")
    print(f"{'='*60}")

    # Filter to rows with at least one outcome
    has_any_outcome = [
        r for r in rows
        if any(r.get(f"out_{w}m_correct") is not None for w in OUTCOME_WINDOWS)
    ]

    if len(has_any_outcome) < MIN_IDEAS:
        print(f"\n  Insufficient data — need {MIN_IDEAS}+ ideas with outcomes, have {len(has_any_outcome)}")
        return

    print("\nOVERALL HIT RATES (ideas with outcomes today):")
    for w in OUTCOME_WINDOWS:
        # Skip windows whose columns don't exist in older DBs
        col = f"out_{w}m_mark"
        if col not in columns:
            continue
        rate, avg_pnl, n = hit_rate(rows, w)
        label = f"{w}m:"
        print(f"  {label:>4}{fmt_rate(rate, n)}{fmt_pnl(avg_pnl)}")

    print("\nBY REGIME (5m hit rate):")
    for regime in ["TRENDING", "PINNED", "SNAP IMMINENT"]:
        rate, n = group_hit_rate(rows, "entry_regime", regime, w=5)
        print(f"  {regime:<14}{fmt_rate(rate, n)}")

    print("\nBY TREND (5m hit rate):")
    for trend in ["Uptrend", "Downtrend", "Choppy"]:
        rate, n = group_hit_rate(rows, "entry_trend", trend, w=5)
        print(f"  {trend:<12}{fmt_rate(rate, n)}")

    print("\nBY SURGE (5m hit rate):")
    for val, label in [(1, "Surge=1"), (0, "Surge=0")]:
        subset = [r for r in rows if r.get("entry_surge") == val]
        col_c  = "out_5m_correct"
        filled = [r for r in subset if r.get(col_c) is not None]
        rate   = (sum(1 for r in filled if r[col_c] == 1) / len(filled) * 100) if filled else None
        print(f"  {label:<10}{fmt_rate(rate, len(filled))}")

    print("\nBY DIRECTION (5m hit rate):")
    for dirn, label in [("Bullish", "Bullish (Calls)"), ("Bearish", "Bearish (Puts)")]:
        rate, n = group_hit_rate(rows, "direction", dirn, w=5)
        print(f"  {label:<18}{fmt_rate(rate, n)}")

    print("\nBY SCORE BUCKET (5m hit rate):")
    buckets = ["50-55", "55-60", "60-65", "65-70", "70+"]
    bucket_rows: dict = {b: [] for b in buckets}
    for r in rows:
        b = score_bucket(r.get("entry_score"))
        if b:
            bucket_rows[b].append(r)
    for b in buckets:
        brows = bucket_rows[b]
        col_c = "out_5m_correct"
        filled = [r for r in brows if r.get(col_c) is not None]
        rate = (sum(1 for r in filled if r[col_c] == 1) / len(filled) * 100) if filled else None
        print(f"  {b:<8}{fmt_rate(rate, len(filled))}")

    # Stop analysis — 1m correct=0 but 5m correct=1
    if "out_1m_correct" in columns and "out_5m_correct" in columns:
        print("\nSTOP ANALYSIS (1m correct=0 but 5m correct=1):")
        stopped_early = [
            r for r in rows
            if r.get("out_1m_correct") == 0 and r.get("out_5m_correct") == 1
        ]
        one_m_failures = [r for r in rows if r.get("out_1m_correct") == 0]
        if one_m_failures:
            pct = len(stopped_early) / len(one_m_failures) * 100
            pnls_1m = [r.get("out_1m_pnl_pct") for r in stopped_early if r.get("out_1m_pnl_pct") is not None]
            avg_pnl_str = f"{sum(pnls_1m)/len(pnls_1m):.1f}%" if pnls_1m else "n/a"
            print(f"  Stopped out too early: {len(stopped_early)} ideas ({pct:.1f}% of 1m failures)")
            print(f"  Avg 1m pnl when this happens: {avg_pnl_str}")
        else:
            print("  No 1m failures yet")

    print()


# ── Main loop ─────────────────────────────────────────────────────────────────

def main():
    if not DB_PATH.exists():
        print(f"ERROR: DB not found at {DB_PATH}")
        return

    start_time = datetime.now()
    print(f"test_outcomes started — watching {DB_PATH}")
    print(f"Refreshes every {REFRESH_SEC//60} minutes. Ctrl-C to stop.\n")

    while True:
        try:
            with sqlite3.connect(str(DB_PATH)) as conn:
                columns = get_columns(conn)
                rows    = fetch_today(conn)
            print_report(rows, columns, start_time)
        except Exception as e:
            print(f"[error] {e}")

        time.sleep(REFRESH_SEC)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
