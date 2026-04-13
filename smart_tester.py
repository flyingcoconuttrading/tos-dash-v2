"""
smart_tester.py — Autonomous backtesting agent for tos-dash-v2.

Uses Claude tool-use to autonomously query DuckDB, simulate filter changes,
and find cross-instrument patterns. Can run standalone or be imported by
backtest_dashboard.py.

Usage:
    python smart_tester.py                              # interactive prompt
    python smart_tester.py --prompt "analyze H-002"    # direct
    python smart_tester.py --hypothesis H-001          # preset hypothesis
    python smart_tester.py --all                       # run all hypotheses
"""

import argparse
import json
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

import duckdb
import anthropic

# ── Paths ──────────────────────────────────────────────────────────────────────
THIS_DIR    = Path(__file__).parent
CONFIG_FILE = THIS_DIR / "config.json"
IDEAS_DB    = Path("D:/tos-dash-v2-data/ideas.duckdb")
TICKS_DB    = Path("D:/tos-dash-v2-replay/ticks.duckdb")

# ── Config ─────────────────────────────────────────────────────────────────────
def load_config() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text())
    except Exception:
        return {}

# ── Schema bootstrap ───────────────────────────────────────────────────────────
def ensure_schema():
    try:
        with duckdb.connect(str(IDEAS_DB), read_only=False) as conn:
            conn.execute("CREATE SEQUENCE IF NOT EXISTS backtest_runs_seq START 1")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS backtest_runs (
                    id             INTEGER DEFAULT nextval('backtest_runs_seq') PRIMARY KEY,
                    run_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    hypothesis     VARCHAR,
                    verdict        VARCHAR,
                    summary        TEXT,
                    evidence       JSON,
                    recommendation TEXT,
                    trade_count    INTEGER,
                    date_range     VARCHAR,
                    run_params     JSON,
                    prompt         TEXT
                )
            """)
    except Exception as e:
        print(f"[schema] Warning: {e}", file=sys.stderr)

# ── Tool definitions ────────────────────────────────────────────────────────────
TOOLS = [
    {
        "name": "query_ideas_db",
        "description": (
            "Run a read-only SQL query against ideas.duckdb. "
            "Tables available: ideas, surface_candidates, idea_events, idea_tick_history, backtest_runs. "
            "Always add paper_exit_reason IS NOT NULL to filter closed trades only. "
            "Exclude OnDemand test trades: EXTRACT(HOUR FROM CAST(surfaced_at AS TIMESTAMP)) BETWEEN 9 AND 16. "
            "surface_candidates only has data from 2026-04-10 onward. "
            "Flag any finding with n < 20 as PRELIMINARY."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sql": {"type": "string", "description": "SQL SELECT query — read-only"}
            },
            "required": ["sql"]
        }
    },
    {
        "name": "query_ticks_db",
        "description": (
            "Run a read-only SQL query against ticks.duckdb. "
            "Tables: spy_ticks, chain_ticks. "
            "spy_ticks columns: recorded_at, spy_price, vix, tick_val, trin_val, trinq_val, "
            "add_val, qqq_price, iwm_price, nq_price. "
            "es_price may be present if tick_recorder was recently updated — check before using. "
            "CRITICAL: NEVER use trinq_val — NULL until 2026-04-10. "
            "ALWAYS filter: CAST(recorded_at AS TIME) >= '09:30:00'"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sql": {"type": "string", "description": "SQL SELECT query — read-only"}
            },
            "required": ["sql"]
        }
    },
    {
        "name": "simulate_filter",
        "description": (
            "Replay ideas with hypothetical filter parameters and compare P&L vs baseline. "
            "Supported params: score_ceiling (default 66), score_floor (default 0), "
            "tick_threshold (default 500), vol_ratio_max (default 999), vol_ratio_min (default 0), "
            "regime_whitelist (list), option_type ('both'/'Call'/'Put'), min_mark, max_mark."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "params": {"type": "object"},
                "label":  {"type": "string"},
                "date_from": {"type": "string"}
            },
            "required": ["params", "label"]
        }
    },
    {
        "name": "pattern_scan",
        "description": (
            "Find cross-instrument patterns around a defined market event. "
            "Returns average behavior of instruments in a window before/after the event."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "event_sql":              {"type": "string"},
                "window_seconds_before":  {"type": "integer"},
                "window_seconds_after":   {"type": "integer"},
                "instruments":            {"type": "array", "items": {"type": "string"}}
            },
            "required": ["event_sql", "instruments"]
        }
    },
    {
        "name": "save_finding",
        "description": "Save a conclusion to backtest_runs. verdict: SUPPORTS/REFUTES/INCONCLUSIVE/PRELIMINARY.",
        "input_schema": {
            "type": "object",
            "properties": {
                "hypothesis":     {"type": "string"},
                "verdict":        {"type": "string", "enum": ["SUPPORTS","REFUTES","INCONCLUSIVE","PRELIMINARY"]},
                "summary":        {"type": "string"},
                "evidence":       {"type": "object"},
                "recommendation": {"type": "string"},
                "trade_count":    {"type": "integer"},
                "date_range":     {"type": "string"}
            },
            "required": ["hypothesis", "verdict", "summary"]
        }
    }
]

HYPOTHESIS_PROMPTS = {
    "H-001": "Validate H-001: PINNED regime over-suppression. Query ideas where entry_regime='PINNED'. Split by entry_tick > 150 vs <= 150. Compare avg paper_pnl_pct, stop rate, target rate. Conclude whether PINNED+high-TICK outperforms random PINNED surfaces.",
    "H-002": "Validate H-002: score band 58-62 optimal. Bucket entry_score into <54, 54-58, 58-62, 62-66, 66+. Show n, avg_pnl_pct, win_rate, total_net. Simulate score_ceiling=62. Flag n<20 as preliminary.",
    "H-003": "Validate H-003: TICK at entry predicts outcome. Bucket entry_tick: <-500, -500 to -300, -300 to +300, +300 to +500, >+500. Show n, avg_pnl_pct, stop rate. Show actual P&L of trades blocked by TICK directional filter.",
    "H-004": "Validate H-004: vol_ratio at entry. Check surface_candidates data (Apr 10+). Bucket vol_ratio <0.8, 0.8-2.0, 2.0-2.5, >2.5. Simulate vol_ratio_max=2.0.",
    "H-005": "Check H-005: channel position at entry. Check surface_candidates.structure column values. If sufficient data, compare P&L by structure value. If not, state when to re-run.",
    "ALL":   "Run full analysis of H-001 through H-005 in order. Save a finding for each. End with overall system health summary.",
}

SYSTEM_PROMPT = """You are a quantitative analyst for tos-dash-v2, an intraday SPY options scalp advisor.

## System Overview
- Scores and surfaces 0DTE/1DTE SPY option candidates every 500ms
- Active filters: score ceiling (66), TICK directional (±500), surge protection, PINNED gate, channel block
- Regimes: TRENDING (GEX negative) and PINNED (GEX positive)
- Exit types: TARGET_1 (+30%), TARGET_2 (+50%), TARGET_3 (+75%), STOP (-30%/-20% PINNED), TIME_EXIT, DROP_EXIT

## Critical Data Rules
1. NEVER use trinq_val
2. Market hours: CAST(recorded_at AS TIME) >= '09:30:00'
3. surface_candidates: Apr 10, 2026+ only
4. paper_exit_reason IS NOT NULL = closed trades only
5. n < 20 = PRELIMINARY
"""

def _query_db(db_path: Path, sql: str) -> dict:
    if not sql.strip().upper().startswith("SELECT"):
        return {"error": "Only SELECT queries are permitted"}
    try:
        with duckdb.connect(str(db_path), read_only=True) as conn:
            df = conn.execute(sql).fetchdf()
            truncated = len(df) > 200
            if truncated:
                df = df.head(200)
            rows = json.loads(df.to_json(orient="records", date_format="iso"))
            return {"columns": list(df.columns), "rows": rows, "n": len(rows), "truncated": truncated}
    except Exception as e:
        return {"error": str(e)}

def _simulate_filter(params: dict, label: str, date_from: Optional[str] = None) -> dict:
    score_ceiling   = params.get("score_ceiling", 66)
    score_floor     = params.get("score_floor", 0)
    tick_threshold  = params.get("tick_threshold", 500)
    vol_ratio_max   = params.get("vol_ratio_max", 999)
    vol_ratio_min   = params.get("vol_ratio_min", 0.0)
    regime_wl       = params.get("regime_whitelist")
    option_type     = params.get("option_type", "both")
    min_mark        = params.get("min_mark", 0.50)
    max_mark        = params.get("max_mark", 2.00)

    date_clause   = f"AND DATE(surfaced_at) >= '{date_from}'" if date_from else ""
    regime_clause = f"AND entry_regime IN ({','.join(repr(r) for r in regime_wl)})" if regime_wl else ""
    ot_clause     = f"AND option_type = '{option_type}'" if option_type != "both" else ""

    try:
        with duckdb.connect(str(IDEAS_DB), read_only=True) as conn:
            def q(where_extra=""):
                return conn.execute(f"""
                    SELECT COUNT(*) AS n,
                           ROUND(AVG(paper_pnl_pct),2) AS avg_pnl,
                           ROUND(SUM(paper_net_dollar_pnl),2) AS total_net,
                           ROUND(AVG(CASE WHEN paper_pnl_pct>0 THEN 1.0 ELSE 0.0 END)*100,1) AS win_rate,
                           ROUND(AVG(CASE WHEN paper_exit_reason='STOP' THEN 1.0 ELSE 0.0 END)*100,1) AS stop_rate
                    FROM ideas
                    WHERE paper_exit_reason IS NOT NULL
                      AND EXTRACT(HOUR FROM CAST(surfaced_at AS TIMESTAMP)) BETWEEN 9 AND 16
                      {date_clause} {where_extra}
                """).fetchone()

            bl   = q()
            test = q(f"AND entry_score <= {score_ceiling} AND entry_score >= {score_floor} "
                     f"AND NOT (option_type='Call' AND entry_tick < -{tick_threshold}) "
                     f"AND NOT (option_type='Put'  AND entry_tick >  {tick_threshold}) "
                     f"AND (paper_entry_ask IS NULL OR (paper_entry_ask >= {min_mark} AND paper_entry_ask <= {max_mark})) "
                     f"{regime_clause} {ot_clause}")
            blocked = q(f"AND (entry_score > {score_ceiling} OR entry_score < {score_floor} "
                        f"OR (option_type='Call' AND entry_tick < -{tick_threshold}) "
                        f"OR (option_type='Put'  AND entry_tick >  {tick_threshold}))")

            def row(r):
                return {"n": int(r[0] or 0), "avg_pnl": float(r[1] or 0),
                        "total_net": float(r[2] or 0), "win_rate": float(r[3] or 0),
                        "stop_rate": float(r[4] or 0)}

            result = {"label": label, "params": params,
                      "baseline": row(bl), "test": row(test),
                      "blocked": {"n": int(blocked[0] or 0), "avg_pnl": float(blocked[1] or 0),
                                  "total_net": float(blocked[2] or 0)}}
            if result["baseline"]["avg_pnl"] and result["test"]["avg_pnl"]:
                result["delta_avg_pnl"] = round(result["test"]["avg_pnl"] - result["baseline"]["avg_pnl"], 2)
            return result
    except Exception as e:
        return {"error": str(e), "trace": traceback.format_exc()}

def _pattern_scan(event_sql: str, instruments: list,
                  window_before: int = 60, window_after: int = 60) -> dict:
    try:
        import pandas as pd
        with duckdb.connect(str(TICKS_DB), read_only=True) as conn:
            sample = conn.execute("SELECT * FROM spy_ticks LIMIT 1").fetchdf()
            valid  = [i for i in instruments if i in sample.columns]
            if not valid:
                return {"error": f"None of {instruments} found in spy_ticks"}
            events = conn.execute(event_sql).fetchdf()
            if events.empty or "recorded_at" not in events.columns:
                return {"error": "Query must return a 'recorded_at' column", "n_events": 0}
            if len(events) > 500:
                return {"error": f"Too many events ({len(events)}). Add LIMIT.", "n_events": len(events)}
            inst_cols = ", ".join(valid)
            frames = []
            for _, row in events.iterrows():
                ts = str(row["recorded_at"])[:19]
                try:
                    wdf = conn.execute(f"""
                        SELECT EPOCH(recorded_at) - EPOCH(TIMESTAMP '{ts}') AS offset_sec, {inst_cols}
                        FROM spy_ticks
                        WHERE recorded_at BETWEEN
                            TIMESTAMP '{ts}' - INTERVAL '{window_before} seconds'
                            AND TIMESTAMP '{ts}' + INTERVAL '{window_after} seconds'
                          AND CAST(recorded_at AS TIME) >= '09:30:00'
                        ORDER BY recorded_at
                    """).fetchdf()
                    if not wdf.empty:
                        frames.append(wdf)
                except Exception:
                    continue
            if not frames:
                return {"error": "No tick data found in windows", "n_events": len(events)}
            combined = pd.concat(frames, ignore_index=True)
            combined["bucket"] = ((combined["offset_sec"] // 10) * 10).astype(int)
            summary = combined.groupby("bucket")[valid].agg(["mean","std","count"]).round(4).reset_index()
            summary.columns = ["_".join(c).strip("_") for c in summary.columns]
            return {"n_events": len(events), "frames_used": len(frames),
                    "instruments": valid, "buckets": json.loads(summary.to_json(orient="records"))}
    except ImportError:
        return {"error": "pandas required — pip install pandas"}
    except Exception as e:
        return {"error": str(e)}

def _save_finding(hypothesis: str, verdict: str, summary: str,
                  evidence: dict = None, recommendation: str = "",
                  trade_count: int = None, date_range: str = None, prompt: str = "") -> dict:
    try:
        with duckdb.connect(str(IDEAS_DB), read_only=False) as conn:
            conn.execute("""
                INSERT INTO backtest_runs
                    (run_at, hypothesis, verdict, summary, evidence, recommendation, trade_count, date_range, prompt)
                VALUES (CURRENT_TIMESTAMP, ?, ?, ?, ?, ?, ?, ?, ?)
            """, [hypothesis, verdict, summary, json.dumps(evidence or {}),
                  recommendation or "", trade_count, date_range, prompt])
        return {"saved": True, "hypothesis": hypothesis, "verdict": verdict}
    except Exception as e:
        return {"error": str(e)}

def _dispatch(name: str, inp: dict, prompt: str = "") -> dict:
    if name == "query_ideas_db":   return _query_db(IDEAS_DB, inp["sql"])
    if name == "query_ticks_db":   return _query_db(TICKS_DB, inp["sql"])
    if name == "simulate_filter":  return _simulate_filter(inp["params"], inp["label"], inp.get("date_from"))
    if name == "pattern_scan":     return _pattern_scan(inp["event_sql"], inp["instruments"],
                                                        inp.get("window_seconds_before", 60),
                                                        inp.get("window_seconds_after", 60))
    if name == "save_finding":     return _save_finding(prompt=prompt, **inp)
    return {"error": f"Unknown tool: {name}"}

def run_analysis(prompt: str, callback: Optional[Callable] = None, max_iterations: int = 30) -> dict:
    cfg = load_config()
    api_key = cfg.get("anthropic_api_key", "")
    if not api_key:
        raise ValueError("anthropic_api_key not set in config.json")
    ensure_schema()
    client   = anthropic.Anthropic(api_key=api_key)
    messages = [{"role": "user", "content": prompt}]
    findings = []

    def emit(t, d):
        if callback:
            callback(t, d)
        else:
            _print_event(t, d)

    emit("start", {"prompt": prompt, "timestamp": datetime.now().isoformat()})

    for iteration in range(1, max_iterations + 1):
        response = client.messages.create(
            model="claude-sonnet-4-20250514", max_tokens=4096,
            system=SYSTEM_PROMPT, tools=TOOLS, messages=messages)

        text_blocks    = [b for b in response.content if b.type == "text"]
        tool_blocks    = [b for b in response.content if b.type == "tool_use"]

        for b in text_blocks:
            if b.text.strip():
                emit("thinking", {"text": b.text, "iteration": iteration})

        assistant_msg = []
        for b in response.content:
            if b.type == "text":
                assistant_msg.append({"type": "text", "text": b.text})
            elif b.type == "tool_use":
                assistant_msg.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
        messages.append({"role": "assistant", "content": assistant_msg})

        if not tool_blocks:
            if response.stop_reason == "end_turn":
                emit("done", {"findings": findings, "iterations": iteration})
                break
            continue

        tool_results = []
        for block in tool_blocks:
            emit("tool_call", {"tool": block.name, "input": block.input, "iteration": iteration})
            result = _dispatch(block.name, block.input, prompt)
            emit("tool_result", {"tool": block.name, "result": result, "iteration": iteration})
            if block.name == "save_finding":
                findings.append(block.input)
                emit("finding", block.input)
            tool_results.append({"type": "tool_result", "tool_use_id": block.id,
                                  "content": json.dumps(result, default=str)})
        messages.append({"role": "user", "content": tool_results})

    return {"findings": findings, "iterations": iteration}

def _print_event(t, d):
    if t == "start":
        print(f"\n{'='*60}\nSmart Tester — {d['timestamp']}\n{d['prompt'][:120]}\n{'='*60}")
    elif t == "thinking" and d.get("text","").strip():
        print(f"\n[{d.get('iteration','')}] {d['text'].strip()[:500]}")
    elif t == "tool_call":
        print(f"\n  → {d['tool']}({json.dumps(d.get('input',{}))[:150]}...)")
    elif t == "tool_result":
        r = d.get("result", {})
        print(f"     {'✗ '+r['error'] if 'error' in r else '✓ '+str(r.get('n','ok'))+' rows'}")
    elif t == "finding":
        print(f"\n  ✅ [{d.get('hypothesis')}] {d.get('verdict')}: {(d.get('summary',''))[:200]}")
    elif t == "done":
        print(f"\n{'='*60}\nComplete — {len(d['findings'])} findings saved.\n{'='*60}\n")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--prompt",     type=str)
    parser.add_argument("--hypothesis", type=str)
    parser.add_argument("--max-iter",   type=int, default=30)
    args = parser.parse_args()

    if args.hypothesis:
        key = args.hypothesis.upper()
        prompt = HYPOTHESIS_PROMPTS.get(key)
        if not prompt:
            print(f"Unknown: {key}. Options: {list(HYPOTHESIS_PROMPTS.keys())}")
            sys.exit(1)
    elif args.prompt:
        prompt = args.prompt
    else:
        print("Enter analysis question (blank line to finish):")
        lines = []
        while True:
            line = input()
            if not line: break
            lines.append(line)
        prompt = " ".join(lines)

    if not prompt.strip():
        print("No prompt."); sys.exit(1)

    run_analysis(prompt, max_iterations=args.max_iter)
