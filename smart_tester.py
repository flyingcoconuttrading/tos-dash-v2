"""
smart_tester.py — Autonomous backtesting agent for tos-dash-v2.

All DuckDB queries route through api.py (port 8001) HTTP endpoints.
No direct DuckDB access — avoids Windows file lock conflicts.

Usage:
    python smart_tester.py                              # interactive
    python smart_tester.py --prompt "analyze H-002"
    python smart_tester.py --hypothesis H-001
"""

import argparse
import json
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

import requests
import anthropic

THIS_DIR    = Path(__file__).parent
CONFIG_FILE = THIS_DIR / "config.json"
API_BASE    = "http://127.0.0.1:8001"


def load_config() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text())
    except Exception:
        return {}


# ── HTTP helpers ───────────────────────────────────────────────────────────────
def _api_query(sql: str, db: str = "ideas") -> dict:
    """POST SQL to api.py and return result dict with 'rows', 'n', optional 'error'."""
    endpoint = "/backtest/ticks-query" if db == "ticks" else "/backtest/query"
    try:
        r = requests.post(f"{API_BASE}{endpoint}", json={"sql": sql}, timeout=15)
        return r.json()
    except Exception as e:
        return {"error": str(e), "rows": [], "n": 0}


def _api_post(endpoint: str, body: dict) -> dict:
    try:
        r = requests.post(f"{API_BASE}{endpoint}", json=body, timeout=60)
        return r.json()
    except Exception as e:
        return {"error": str(e)}


# ── Tool definitions ────────────────────────────────────────────────────────────
TOOLS = [
    {
        "name": "query_ideas_db",
        "description": (
            "Run a read-only SELECT on ideas.duckdb via api.py. "
            "Tables: ideas, surface_candidates, idea_events, idea_tick_history, "
            "backtest_runs, moc_events, market_events, config_history. "
            "Always add paper_exit_reason IS NOT NULL for closed trades. "
            "Exclude OnDemand hours: EXTRACT(HOUR FROM CAST(surfaced_at AS TIMESTAMP)) BETWEEN 9 AND 16. "
            "surface_candidates only from 2026-04-10. Flag n<20 as PRELIMINARY. "
            "EXACT ideas columns: id, symbol, strike, option_type, surfaced_at, entry_regime, "
            "entry_trend, entry_score, entry_tick, entry_vix, entry_spy, paper_pnl_pct, "
            "paper_net_dollar_pnl, paper_entry_ask, paper_exit_bid, paper_exit_reason. "
            "NEVER use: paper_pnl, paper_profit, pnl_pct, net_pnl. "
            "moc_events columns: id, event_date, published_at, direction, sp500_mln, "
            "nasdaq_mln, dow_mln, mag7_mln, total_mln, spy_price_at, spy_close, "
            "price_move, raw_headline, source."
        ),
        "input_schema": {"type": "object",
                         "properties": {"sql": {"type": "string"}},
                         "required": ["sql"]}
    },
    {
        "name": "query_ticks_db",
        "description": (
            "Run a read-only SELECT on ticks.duckdb via api.py. "
            "Tables: spy_ticks, chain_ticks. "
            "EXACT spy_ticks columns: recorded_at, date, spy_price, vix, tick_val, "
            "trin_val, trinq_val, add_val, qqq_price, iwm_price, nq_price. "
            "NEVER use: tick_value, spy_volume, vol_ratio, ntick, ntick_val. "
            "ALWAYS filter market hours: CAST(recorded_at AS TIME) >= '09:30:00'. "
            "chain_ticks columns: recorded_at, date, symbol, bid, ask, last, delta, "
            "gamma, theta, vega, iv, volume, open_interest. "
            "Do NOT use CTEs or window functions in WHERE clauses — use subqueries instead. "
            "Returns error if tick_recorder holds the lock — pause it in Settings first."
        ),
        "input_schema": {"type": "object",
                         "properties": {"sql": {"type": "string"}},
                         "required": ["sql"]}
    },
    {
        "name": "simulate_filter",
        "description": (
            "Replay ideas with hypothetical filter params. Returns baseline vs test vs blocked P&L. "
            "Params: score_ceiling(66), score_floor(0), tick_threshold(500), "
            "vol_ratio_max(999), regime_whitelist(null), option_type('both'), min_mark, max_mark."
        ),
        "input_schema": {"type": "object",
                         "properties": {
                             "params":    {"type": "object"},
                             "label":     {"type": "string"},
                             "date_from": {"type": "string"}
                         },
                         "required": ["params", "label"]}
    },
    {
        "name": "pattern_scan",
        "description": (
            "Find cross-instrument patterns around market events. "
            "Runs server-side on ticks.duckdb. Returns bucketed averages per instrument. "
            "Requires tick_recorder to be paused if it's running."
        ),
        "input_schema": {"type": "object",
                         "properties": {
                             "event_sql":             {"type": "string"},
                             "window_seconds_before": {"type": "integer"},
                             "window_seconds_after":  {"type": "integer"},
                             "instruments":           {"type": "array", "items": {"type": "string"}}
                         },
                         "required": ["event_sql", "instruments"]}
    },
    {
        "name": "save_finding",
        "description": "Save a conclusion to backtest_runs. verdict: SUPPORTS/REFUTES/INCONCLUSIVE/PRELIMINARY.",
        "input_schema": {"type": "object",
                         "properties": {
                             "hypothesis":     {"type": "string"},
                             "verdict":        {"type": "string",
                                               "enum": ["SUPPORTS","REFUTES","INCONCLUSIVE","PRELIMINARY"]},
                             "summary":        {"type": "string"},
                             "evidence":       {"type": "object"},
                             "recommendation": {"type": "string"},
                             "trade_count":    {"type": "integer"},
                             "date_range":     {"type": "string"}
                         },
                         "required": ["hypothesis", "verdict", "summary"]}
    }
]

HYPOTHESIS_PROMPTS = {
    "H-001": "Validate H-001: PINNED regime over-suppression. Query ideas where entry_regime='PINNED'. Split by entry_tick > 150 vs <= 150. Compare avg paper_pnl_pct, stop rate, target rate. Save a finding.",
    "H-002": "Validate H-002: score band 58-62 optimal. Bucket entry_score into <54, 54-58, 58-62, 62-66, 66+. Show n/avg_pnl/win_rate. Simulate score_ceiling=62. Flag n<20. Save a finding.",
    "H-003": "Validate H-003: TICK at entry predicts outcome. Bucket entry_tick. Show P&L. Show what the TICK directional filter is actually blocking. Save a finding.",
    "H-004": "Validate H-004: vol_ratio at entry. Check surface_candidates. Bucket vol_ratio. Simulate vol_ratio_max=2.0. Save a finding.",
    "H-005": "Check H-005: channel position at entry. Check surface_candidates.structure values. Compare P&L by structure. If insufficient data, state when to re-run. Save a finding.",
    "ALL":   "Run H-001 through H-005 in order. Save a finding for each. End with system health summary.",
}

SYSTEM_PROMPT = """You are a quantitative analyst for tos-dash-v2, an intraday SPY options scalp advisor.

Active filters: score ceiling 66, TICK directional ±500, surge protection, PINNED gate, channel block.
Regimes: TRENDING (GEX negative) and PINNED (GEX positive).
Exit types: TARGET_1 +30%, TARGET_2 +50%, TARGET_3 +75%, STOP -30%/-20% PINNED, TIME_EXIT, DROP_EXIT.

Critical data rules:
1. NEVER use trinq_val
2. Market hours: CAST(recorded_at AS TIME) >= '09:30:00'
3. surface_candidates: Apr 10, 2026+ only
4. paper_exit_reason IS NOT NULL = closed trades only
5. n < 20 = PRELIMINARY — always flag this
"""


# ── Tool execution ──────────────────────────────────────────────────────────────
def _simulate_filter(params: dict, label: str, date_from: Optional[str] = None) -> dict:
    score_ceiling  = params.get("score_ceiling", 66)
    score_floor    = params.get("score_floor", 0)
    tick_threshold = params.get("tick_threshold", 500)
    vol_ratio_max  = params.get("vol_ratio_max", 999)
    regime_wl      = params.get("regime_whitelist")
    option_type    = params.get("option_type", "both")
    min_mark       = params.get("min_mark", 0.50)
    max_mark       = params.get("max_mark", 2.00)

    date_clause   = f"AND DATE(surfaced_at) >= '{date_from}'" if date_from else ""
    regime_clause = f"AND entry_regime IN ({','.join(repr(r) for r in regime_wl)})" if regime_wl else ""
    ot_clause     = f"AND option_type = '{option_type}'" if option_type != "both" else ""
    base_where    = f"paper_exit_reason IS NOT NULL AND EXTRACT(HOUR FROM CAST(surfaced_at AS TIMESTAMP)) BETWEEN 9 AND 16 {date_clause}"

    def q(extra=""):
        r = _api_query(f"""
            SELECT COUNT(*) AS n, ROUND(AVG(paper_pnl_pct),2) AS avg_pnl,
                   ROUND(SUM(paper_net_dollar_pnl),2) AS total_net,
                   ROUND(AVG(CASE WHEN paper_pnl_pct>0 THEN 1.0 ELSE 0.0 END)*100,1) AS win_rate,
                   ROUND(AVG(CASE WHEN paper_exit_reason='STOP' THEN 1.0 ELSE 0.0 END)*100,1) AS stop_rate
            FROM ideas WHERE {base_where} {extra}
        """)
        return r["rows"][0] if r.get("rows") else {}

    try:
        bl  = q()
        tst = q(f"AND entry_score <= {score_ceiling} AND entry_score >= {score_floor} "
                f"AND NOT (option_type='Call' AND entry_tick < -{tick_threshold}) "
                f"AND NOT (option_type='Put'  AND entry_tick >  {tick_threshold}) "
                f"AND (paper_entry_ask IS NULL OR (paper_entry_ask >= {min_mark} AND paper_entry_ask <= {max_mark})) "
                f"{regime_clause} {ot_clause}")
        blk = q(f"AND (entry_score > {score_ceiling} OR entry_score < {score_floor} "
                f"OR (option_type='Call' AND entry_tick < -{tick_threshold}) "
                f"OR (option_type='Put'  AND entry_tick >  {tick_threshold}))")

        result = {"label": label, "params": params,
                  "baseline": bl, "test": tst,
                  "blocked": {"n": blk.get("n",0), "avg_pnl": blk.get("avg_pnl"), "total_net": blk.get("total_net")}}
        if bl.get("avg_pnl") and tst.get("avg_pnl"):
            result["delta_avg_pnl"] = round(float(tst["avg_pnl"]) - float(bl["avg_pnl"]), 2)
        return result
    except Exception as e:
        return {"error": str(e), "trace": traceback.format_exc()}


def _dispatch(name: str, inp: dict, prompt: str = "") -> dict:
    if name == "query_ideas_db":
        return _api_query(inp["sql"], "ideas")
    if name == "query_ticks_db":
        return _api_query(inp["sql"], "ticks")
    if name == "simulate_filter":
        return _simulate_filter(inp["params"], inp["label"], inp.get("date_from"))
    if name == "pattern_scan":
        return _api_post("/backtest/pattern", {
            "event_sql":             inp["event_sql"],
            "instruments":           inp["instruments"],
            "window_seconds_before": inp.get("window_seconds_before", 60),
            "window_seconds_after":  inp.get("window_seconds_after", 60),
        })
    if name == "save_finding":
        return _api_post("/backtest/finding", {**inp, "prompt": prompt})
    return {"error": f"Unknown tool: {name}"}


# ── Agent loop ──────────────────────────────────────────────────────────────────
def run_analysis(prompt: str, callback: Optional[Callable] = None,
                 max_iterations: int = 30) -> dict:
    cfg     = load_config()
    api_key = cfg.get("anthropic_api_key", "")
    if not api_key:
        raise ValueError("anthropic_api_key not set in config.json")

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

        text_blocks = [b for b in response.content if b.type == "text"]
        tool_blocks = [b for b in response.content if b.type == "tool_use"]

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
            # Check if a stop was requested via the callback channel
            if callback and hasattr(callback, '_stop_requested') and callback._stop_requested:
                emit("done", {"findings": findings, "iterations": iteration, "stopped": True})
                return {"findings": findings, "iterations": iteration, "stopped": True}
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
    elif t == "thinking" and d.get("text", "").strip():
        print(f"\n[{d.get('iteration','')}] {d['text'].strip()[:500]}")
    elif t == "tool_call":
        print(f"\n  → {d['tool']}({json.dumps(d.get('input',{}))[:150]}...)")
    elif t == "tool_result":
        r = d.get("result", {})
        print(f"     {'✗ '+r['error'] if 'error' in r else '✓ '+str(r.get('n','ok'))+' rows'}")
    elif t == "finding":
        print(f"\n  ✅ [{d.get('hypothesis')}] {d.get('verdict')}: {d.get('summary','')[:200]}")
    elif t == "done":
        print(f"\n{'='*60}\nComplete — {len(d['findings'])} findings.\n{'='*60}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--prompt",     type=str)
    parser.add_argument("--hypothesis", type=str)
    parser.add_argument("--max-iter",   type=int, default=30)
    args = parser.parse_args()

    if args.hypothesis:
        key    = args.hypothesis.upper()
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
            if not line:
                break
            lines.append(line)
        prompt = " ".join(lines)

    if not prompt.strip():
        print("No prompt.")
        sys.exit(1)

    run_analysis(prompt, max_iterations=args.max_iter)
