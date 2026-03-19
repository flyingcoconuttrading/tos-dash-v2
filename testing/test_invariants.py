"""
testing/test_invariants.py
Live invariant monitor for ideas.db.

Polls every 3 seconds for new idea rows and checks each against a set of
logical invariants. Violations are written to testing/test_violations.log
and printed to stdout.

Run from project root:
    python testing/test_invariants.py
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
LOG_PATH  = THIS_DIR / "test_violations.log"

POLL_SEC        = 3
SUMMARY_SEC     = 30 * 60   # print summary every 30 minutes
HEARTBEAT_POLLS = 10

VALID_REGIMES = {"TRENDING", "PINNED", "SNAP IMMINENT", "TRANSITION"}
VALID_BIASES  = {"Bullish", "Bearish", "Neutral"}
OUTCOME_WINDOWS = [1, 2, 3, 4, 5, 10, 15, 30]


# ── Logging ───────────────────────────────────────────────────────────────────

def _log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _violation(check: str, row: dict, detail: str):
    idea_id     = row.get("id")
    symbol      = row.get("symbol", "?")
    opt_type    = row.get("option_type", "?")
    trend       = row.get("entry_trend", "?")
    bias        = row.get("entry_bias", "?")
    regime      = row.get("entry_regime", "?")
    score       = row.get("entry_score")
    surfaced    = row.get("surfaced_at", "?")
    score_str   = f"{score:.1f}" if score is not None else "?"
    _log(
        f"VIOLATION: {check}\n"
        f"  idea_id={idea_id} symbol={symbol} option_type={opt_type}\n"
        f"  entry_trend={trend} entry_bias={bias} entry_regime={regime}\n"
        f"  entry_score={score_str} surfaced_at={surfaced}\n"
        f"  detail: {detail}"
    )


# ── Invariant checks ──────────────────────────────────────────────────────────

def check_row(row: dict) -> int:
    """Run all invariants on a single row. Returns number of violations found."""
    violations = 0

    score  = row.get("entry_score")
    mark   = row.get("entry_mark")
    bid    = row.get("entry_bid")
    ask    = row.get("entry_ask")
    iv     = row.get("entry_iv")
    opt    = row.get("option_type")
    dirn   = row.get("direction")
    trend  = row.get("entry_trend")
    regime = row.get("entry_regime")
    bias   = row.get("entry_bias")

    # 1. SCORE_RANGE
    if score is not None:
        if score < 40.0 or score > 80.0:
            _violation("SCORE_RANGE", row, f"entry_score={score:.2f} outside [40, 80]")
            violations += 1

    # 2. SCORE_COMPRESSION
    if score is not None and score > 76.9:
        _violation("SCORE_COMPRESSION", row,
                   f"entry_score={score:.2f} > 76.9 — soft ceiling may not be applying")
        violations += 1

    # 3. MARK_POSITIVE
    if mark is not None and mark <= 0.01:
        _violation("MARK_POSITIVE", row, f"entry_mark={mark} <= 0.01")
        violations += 1

    # 4. SPREAD_REASONABLE
    if mark and mark > 0 and bid is not None and ask is not None:
        spread_pct = (ask - bid) / mark * 100
        if spread_pct > 50:
            _violation("SPREAD_REASONABLE", row,
                       f"spread={spread_pct:.1f}% > 50%  bid={bid} ask={ask} mark={mark}")
            violations += 1

    # 5. DIRECTION_MATCHES_TYPE
    if opt and dirn:
        expected = "Bullish" if opt == "Call" else "Bearish"
        if dirn != expected:
            _violation("DIRECTION_MATCHES_TYPE", row,
                       f"option_type={opt} but direction={dirn} (expected {expected})")
            violations += 1

    # 6. TREND_FILTER
    if opt and trend:
        if opt == "Call" and trend == "Downtrend":
            _violation("TREND_FILTER", row,
                       "Call surfaced during Downtrend — trend filter not applied")
            violations += 1
        if opt == "Put" and trend == "Uptrend":
            _violation("TREND_FILTER", row,
                       "Put surfaced during Uptrend — trend filter not applied")
            violations += 1

    # 7. REGIME_VALID
    if regime and regime not in VALID_REGIMES:
        _violation("REGIME_VALID", row, f"unknown regime='{regime}'")
        violations += 1

    # 8. BIAS_VALID
    if bias and bias not in VALID_BIASES:
        _violation("BIAS_VALID", row, f"unknown bias='{bias}'")
        violations += 1

    # 9. DEX_BIAS_CONSISTENCY
    if regime == "TRENDING" and trend and bias:
        if trend == "Downtrend" and bias == "Bullish":
            _violation("DEX_BIAS_CONSISTENCY", row,
                       "TRENDING + Downtrend but bias=Bullish — market_structure.py fix not applied")
            violations += 1
        elif trend == "Uptrend" and bias == "Bearish":
            _violation("DEX_BIAS_CONSISTENCY", row,
                       "TRENDING + Uptrend but bias=Bearish — market_structure.py fix not applied")
            violations += 1

    # 10. IV_REASONABLE
    if iv is not None and iv > 100:
        _violation("IV_REASONABLE", row,
                   f"entry_iv={iv:.1f}% > 100 — likely a data error")
        violations += 1

    # 11. OUTCOME_CORRECT_LOGIC
    entry_mark = row.get("entry_mark")
    if entry_mark and entry_mark > 0:
        for w in OUTCOME_WINDOWS:
            out_mark    = row.get(f"out_{w}m_mark")
            out_correct = row.get(f"out_{w}m_correct")
            if out_mark is None or out_correct is None:
                continue
            expected_correct = 1 if out_mark > entry_mark else 0
            if out_correct != expected_correct:
                _violation("OUTCOME_CORRECT_LOGIC", row,
                           f"out_{w}m_mark={out_mark:.2f} entry_mark={entry_mark:.2f} "
                           f"out_{w}m_correct={out_correct} expected={expected_correct}")
                violations += 1

    return violations


# ── DB helpers ────────────────────────────────────────────────────────────────

def get_columns(conn: sqlite3.Connection) -> set:
    rows = conn.execute("PRAGMA table_info(ideas)").fetchall()
    return {r[1] for r in rows}


def fetch_new_rows(conn: sqlite3.Connection, after_id: int, columns: set) -> list:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM ideas WHERE id > ? ORDER BY id ASC", (after_id,)
    ).fetchall()
    return [dict(r) for r in rows]


# ── Main loop ─────────────────────────────────────────────────────────────────

def main():
    if not DB_PATH.exists():
        print(f"ERROR: DB not found at {DB_PATH}")
        return

    _log(f"test_invariants started — watching {DB_PATH}")
    print(f"Violations logged to: {LOG_PATH}")

    last_id          = 0
    poll_count       = 0
    session_ideas    = 0
    session_viols    = 0
    window_ideas     = 0
    window_viols     = 0
    window_start     = datetime.now()
    last_summary     = time.monotonic()

    # Seed last_id to current max — don't re-check historical rows
    with sqlite3.connect(str(DB_PATH)) as conn:
        row = conn.execute("SELECT MAX(id) FROM ideas").fetchone()
        last_id = row[0] or 0
    print(f"Starting from idea id={last_id}. Waiting for new ideas...")

    while True:
        time.sleep(POLL_SEC)
        poll_count += 1

        if poll_count % HEARTBEAT_POLLS == 0:
            print(".", end="", flush=True)

        try:
            with sqlite3.connect(str(DB_PATH)) as conn:
                columns    = get_columns(conn)
                new_rows   = fetch_new_rows(conn, last_id, columns)
        except Exception as e:
            print(f"\n[poll error] {e}")
            continue

        for row in new_rows:
            viols = check_row(row)
            session_ideas += 1
            window_ideas  += 1
            session_viols += viols
            window_viols  += viols
            if last_id < row["id"]:
                last_id = row["id"]

        # 30-min summary
        now_mono = time.monotonic()
        if now_mono - last_summary >= SUMMARY_SEC:
            ws = window_start.strftime("%H:%M")
            we = datetime.now().strftime("%H:%M")
            viol_note = f"{window_viols} violations (see log)" if window_viols else "0 violations"
            print(f"\n[{ws}-{we}] {window_ideas} ideas checked, {viol_note}")
            window_ideas  = 0
            window_viols  = 0
            window_start  = datetime.now()
            last_summary  = now_mono


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
