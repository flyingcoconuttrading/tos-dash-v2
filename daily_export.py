"""
daily_export.py — End-of-day export utility for tos-dash-v2.

Usage:
    python daily_export.py                  # exports today
    python daily_export.py --date 2024-01-15
    python daily_export.py --days 7         # last 7 days

Writes JSON + CSV summaries to D:/tos-dash-v2-data/exports/
"""

import argparse
import json
import csv
from datetime import date, datetime, timedelta
from pathlib import Path
import duckdb

IDEAS_DB   = Path("D:/tos-dash-v2-data/ideas.duckdb")
EXPORT_DIR = Path("D:/tos-dash-v2-data/exports")


def _connect():
    return duckdb.connect(str(IDEAS_DB), read_only=True)


def export_day(target_date: str) -> dict:
    """Export all data for a single trading day. Returns summary dict."""
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)

    with _connect() as conn:
        # All closed ideas for the day
        ideas_df = conn.execute(f"""
            SELECT
                id, surfaced_at, symbol, direction, entry_regime,
                entry_score, paper_entry_mark, paper_exit_mark,
                paper_pnl_pct, paper_net_dollar_pnl,
                paper_exit_reason, paper_exit_minute,
                invalidation_reason, entry_notes, exit_notes,
                paper_qty, paper_cost_basis
            FROM ideas
            WHERE DATE(surfaced_at) = '{target_date}'
              AND paper_exit_reason IS NOT NULL
            ORDER BY surfaced_at
        """).fetchdf()

        # Surface candidates (all surfaced, including rejected)
        try:
            cands_df = conn.execute(f"""
                SELECT *
                FROM surface_candidates
                WHERE DATE(surfaced_at) = '{target_date}'
                ORDER BY surfaced_at
            """).fetchdf()
        except Exception:
            cands_df = None

        # Daily stats
        stats = conn.execute(f"""
            SELECT
                COUNT(*)                                                        AS total_trades,
                ROUND(AVG(paper_pnl_pct), 2)                                   AS avg_pnl_pct,
                ROUND(SUM(paper_net_dollar_pnl), 2)                            AS total_net,
                ROUND(AVG(CASE WHEN paper_pnl_pct > 0 THEN 1.0 ELSE 0.0 END) * 100, 1) AS win_rate,
                ROUND(AVG(CASE WHEN paper_exit_reason='STOP' THEN 1.0 ELSE 0.0 END) * 100, 1) AS stop_rate,
                ROUND(MAX(paper_net_dollar_pnl), 2)                            AS best_trade,
                ROUND(MIN(paper_net_dollar_pnl), 2)                            AS worst_trade
            FROM ideas
            WHERE DATE(surfaced_at) = '{target_date}'
              AND paper_exit_reason IS NOT NULL
        """).fetchone()

        by_regime = conn.execute(f"""
            SELECT entry_regime, COUNT(*) AS n,
                   ROUND(AVG(paper_pnl_pct), 2) AS avg_pnl,
                   ROUND(SUM(paper_net_dollar_pnl), 2) AS total_net,
                   ROUND(AVG(CASE WHEN paper_pnl_pct > 0 THEN 1.0 ELSE 0.0 END) * 100, 1) AS win_rate
            FROM ideas
            WHERE DATE(surfaced_at) = '{target_date}'
              AND paper_exit_reason IS NOT NULL
            GROUP BY entry_regime ORDER BY n DESC
        """).fetchdf().to_dict(orient="records")

        by_exit = conn.execute(f"""
            SELECT paper_exit_reason AS exit_reason, COUNT(*) AS n,
                   ROUND(AVG(paper_pnl_pct), 2) AS avg_pnl
            FROM ideas
            WHERE DATE(surfaced_at) = '{target_date}'
              AND paper_exit_reason IS NOT NULL
            GROUP BY paper_exit_reason ORDER BY n DESC
        """).fetchdf().to_dict(orient="records")

    summary = {
        "date":         target_date,
        "exported_at":  datetime.utcnow().isoformat() + "Z",
        "stats": {
            "total_trades": int(stats[0] or 0),
            "avg_pnl_pct":  float(stats[1] or 0),
            "total_net":    float(stats[2] or 0),
            "win_rate":     float(stats[3] or 0),
            "stop_rate":    float(stats[4] or 0),
            "best_trade":   float(stats[5] or 0),
            "worst_trade":  float(stats[6] or 0),
        },
        "by_regime": by_regime,
        "by_exit":   by_exit,
        "trades":    json.loads(ideas_df.to_json(orient="records", date_format="iso")),
        "candidates": json.loads(cands_df.to_json(orient="records", date_format="iso")) if cands_df is not None else [],
    }

    # Write JSON
    json_path = EXPORT_DIR / f"{target_date}.json"
    json_path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")

    # Write trades CSV
    csv_path = EXPORT_DIR / f"{target_date}_trades.csv"
    if not ideas_df.empty:
        ideas_df.to_csv(str(csv_path), index=False)

    # Write candidates CSV
    if cands_df is not None and not cands_df.empty:
        cands_csv = EXPORT_DIR / f"{target_date}_candidates.csv"
        cands_df.to_csv(str(cands_csv), index=False)

    print(f"[export] {target_date}: {summary['stats']['total_trades']} trades, "
          f"net ${summary['stats']['total_net']:+.2f}, "
          f"win {summary['stats']['win_rate']:.0f}%  → {json_path}")

    return summary


def export_range(start: str, end: str) -> list[dict]:
    """Export all days from start to end (inclusive)."""
    results = []
    d = date.fromisoformat(start)
    e = date.fromisoformat(end)
    while d <= e:
        try:
            results.append(export_day(d.isoformat()))
        except Exception as ex:
            print(f"[export] {d.isoformat()} error: {ex}")
        d += timedelta(days=1)
    return results


def print_summary(summaries: list[dict]):
    if not summaries:
        return
    total_trades = sum(s["stats"]["total_trades"] for s in summaries)
    total_net    = sum(s["stats"]["total_net"]    for s in summaries)
    days_with_trades = sum(1 for s in summaries if s["stats"]["total_trades"] > 0)
    all_pnls = [t["paper_pnl_pct"] for s in summaries for t in s["trades"] if t.get("paper_pnl_pct") is not None]
    win_rate = (sum(1 for p in all_pnls if p > 0) / len(all_pnls) * 100) if all_pnls else 0

    print(f"\n{'='*50}")
    print(f"  EXPORT SUMMARY")
    print(f"  Days exported:  {len(summaries)} ({days_with_trades} with trades)")
    print(f"  Total trades:   {total_trades}")
    print(f"  Win rate:       {win_rate:.1f}%")
    print(f"  Total P&L:      ${total_net:+.2f}")
    print(f"{'='*50}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="tos-dash-v2 daily export utility")
    parser.add_argument("--date",  default=None, help="Export single date (YYYY-MM-DD)")
    parser.add_argument("--days",  type=int, default=None, help="Export last N days")
    parser.add_argument("--start", default=None, help="Range start (YYYY-MM-DD)")
    parser.add_argument("--end",   default=None, help="Range end (YYYY-MM-DD)")
    args = parser.parse_args()

    if args.date:
        export_day(args.date)
    elif args.days:
        end_d   = date.today()
        start_d = end_d - timedelta(days=args.days - 1)
        results = export_range(start_d.isoformat(), end_d.isoformat())
        print_summary(results)
    elif args.start and args.end:
        results = export_range(args.start, args.end)
        print_summary(results)
    else:
        # Default: today
        export_day(date.today().isoformat())
