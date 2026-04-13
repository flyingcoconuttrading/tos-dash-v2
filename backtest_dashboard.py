"""
backtest_dashboard.py — Backtesting Dashboard server for tos-dash-v2.

Serves the backtesting UI and provides REST endpoints for smart test runs,
filter simulation, pattern scanning, and findings history.

Run: python backtest_dashboard.py
URL: http://127.0.0.1:8003/

Requires main api.py running on port 8001 for config proxy.
"""

import asyncio
import json
import time
import uuid
from pathlib import Path
from typing import Optional

import duckdb
import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse

# ── Paths ──────────────────────────────────────────────────────────────────────
THIS_DIR   = Path(__file__).parent
DASH_FILE  = THIS_DIR / "backtest_dashboard.html"
IDEAS_DB   = Path("D:/tos-dash-v2-data/ideas.duckdb")
TICKS_DB   = Path("D:/tos-dash-v2-replay/ticks.duckdb")

# ── Active run queues — run_id -> asyncio.Queue ────────────────────────────────
_active_runs: dict[str, asyncio.Queue] = {}
_run_loop: Optional[asyncio.AbstractEventLoop] = None

# ── Lifespan ────────────────────────────────────────────────────────────────────
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _run_loop
    _run_loop = asyncio.get_event_loop()
    # Schema created by smart_tester on first run — don't open duckdb here
    # since api.py holds an exclusive write lock on ideas.duckdb while running
    yield

# ── App ────────────────────────────────────────────────────────────────────────
app = FastAPI(title="tos-dash-v2 Backtesting Dashboard", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Dashboard HTML ─────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
def serve_dashboard():
    try:
        return HTMLResponse(DASH_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return HTMLResponse("<h1>backtest_dashboard.html not found</h1>", status_code=404)

# ── Metrics endpoint ────────────────────────────────────────────────────────────
@app.get("/metrics")
def get_metrics():
    """Return key trading metrics from ideas.duckdb for the overview tab."""
    try:
        with duckdb.connect(str(IDEAS_DB), read_only=True) as conn:
            # Overall stats
            overall = conn.execute("""
                SELECT
                    COUNT(*)                                                             AS total_trades,
                    ROUND(AVG(paper_pnl_pct), 2)                                        AS avg_pnl,
                    ROUND(SUM(paper_net_dollar_pnl), 2)                                 AS total_net,
                    ROUND(AVG(CASE WHEN paper_pnl_pct > 0 THEN 1.0 ELSE 0.0 END)*100,1) AS win_rate,
                    ROUND(AVG(CASE WHEN paper_exit_reason='STOP' THEN 1.0 ELSE 0.0 END)*100,1) AS stop_rate,
                    COUNT(DISTINCT DATE(surfaced_at))                                    AS trading_days
                FROM ideas
                WHERE paper_exit_reason IS NOT NULL
                    AND EXTRACT(HOUR FROM CAST(surfaced_at AS TIMESTAMP)) BETWEEN 9 AND 16
            """).fetchone()

            # Expectancy: (avg_win * win_rate) - (avg_loss * loss_rate)
            exp = conn.execute("""
                SELECT
                    ROUND(AVG(CASE WHEN paper_pnl_pct > 0 THEN paper_pnl_pct END), 2) AS avg_win,
                    ROUND(AVG(CASE WHEN paper_pnl_pct < 0 THEN ABS(paper_pnl_pct) END), 2) AS avg_loss,
                    ROUND(AVG(CASE WHEN paper_pnl_pct > 0 THEN 1.0 ELSE 0.0 END), 4) AS win_rate
                FROM ideas WHERE paper_exit_reason IS NOT NULL
            """).fetchone()

            # By regime
            by_regime = conn.execute("""
                SELECT
                    entry_regime,
                    COUNT(*)                                     AS n,
                    ROUND(AVG(paper_pnl_pct), 2)                AS avg_pnl,
                    ROUND(SUM(paper_net_dollar_pnl), 2)         AS total_net,
                    ROUND(AVG(CASE WHEN paper_pnl_pct > 0 THEN 1.0 ELSE 0.0 END)*100,1) AS win_rate
                FROM ideas
                WHERE paper_exit_reason IS NOT NULL
                    AND EXTRACT(HOUR FROM CAST(surfaced_at AS TIMESTAMP)) BETWEEN 9 AND 16
                GROUP BY entry_regime ORDER BY n DESC
            """).fetchdf().to_dict(orient="records")

            # By score band
            by_score = conn.execute("""
                SELECT
                    FLOOR(entry_score / 2) * 2  AS band_floor,
                    COUNT(*)                    AS n,
                    ROUND(AVG(paper_pnl_pct), 2) AS avg_pnl,
                    ROUND(SUM(paper_net_dollar_pnl), 2) AS total_net
                FROM ideas
                WHERE paper_exit_reason IS NOT NULL
                    AND EXTRACT(HOUR FROM CAST(surfaced_at AS TIMESTAMP)) BETWEEN 9 AND 16
                GROUP BY band_floor ORDER BY band_floor
            """).fetchdf().to_dict(orient="records")

            # Exit reason breakdown
            by_exit = conn.execute("""
                SELECT
                    paper_exit_reason AS exit_reason,
                    COUNT(*)           AS n,
                    ROUND(AVG(paper_pnl_pct), 2) AS avg_pnl
                FROM ideas
                WHERE paper_exit_reason IS NOT NULL
                GROUP BY paper_exit_reason ORDER BY n DESC
            """).fetchdf().to_dict(orient="records")

            # Running balance (last 50 trades)
            balance = conn.execute("""
                SELECT
                    ROW_NUMBER() OVER (ORDER BY surfaced_at) AS trade_num,
                    surfaced_at,
                    paper_net_dollar_pnl,
                    SUM(paper_net_dollar_pnl) OVER (ORDER BY surfaced_at) AS running_total
                FROM ideas
                WHERE paper_exit_reason IS NOT NULL
                    AND EXTRACT(HOUR FROM CAST(surfaced_at AS TIMESTAMP)) BETWEEN 9 AND 16
                ORDER BY surfaced_at
            """).fetchdf()
            balance_rows = json.loads(balance.to_json(orient="records", date_format="iso"))

            # Compute expectancy
            aw, al, wr = (exp[0] or 0), (exp[1] or 0), (exp[2] or 0)
            expectancy = round((aw * wr) - (al * (1 - wr)), 2)

            # Profit factor
            pf_row = conn.execute("""
                SELECT
                    SUM(CASE WHEN paper_net_dollar_pnl > 0 THEN paper_net_dollar_pnl ELSE 0 END) AS gross_profit,
                    SUM(CASE WHEN paper_net_dollar_pnl < 0 THEN ABS(paper_net_dollar_pnl) ELSE 0 END) AS gross_loss
                FROM ideas WHERE paper_exit_reason IS NOT NULL
            """).fetchone()
            profit_factor = round(pf_row[0] / pf_row[1], 2) if pf_row[1] and pf_row[1] > 0 else None

        return {
            "overall": {
                "total_trades":  int(overall[0] or 0),
                "avg_pnl":       float(overall[1] or 0),
                "total_net":     float(overall[2] or 0),
                "win_rate":      float(overall[3] or 0),
                "stop_rate":     float(overall[4] or 0),
                "trading_days":  int(overall[5] or 0),
                "expectancy":    expectancy,
                "profit_factor": profit_factor,
            },
            "by_regime":  by_regime,
            "by_score":   by_score,
            "by_exit":    by_exit,
            "balance":    balance_rows,
        }
    except Exception as e:
        return {"error": str(e)}

# ── Findings ────────────────────────────────────────────────────────────────────
@app.get("/findings")
def get_findings(limit: int = 50):
    """Return saved findings from backtest_runs table."""
    try:
        with duckdb.connect(str(IDEAS_DB), read_only=True) as conn:
            # Check if table exists
            tables = conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_name='backtest_runs'"
            ).fetchall()
            if not tables:
                return {"findings": [], "total": 0}
            df = conn.execute(f"""
                SELECT id, run_at, hypothesis, verdict, summary, evidence,
                       recommendation, trade_count, date_range
                FROM backtest_runs
                ORDER BY run_at DESC
                LIMIT {limit}
            """).fetchdf()
            rows = json.loads(df.to_json(orient="records", date_format="iso"))
            # Parse evidence JSON strings
            for row in rows:
                if isinstance(row.get("evidence"), str):
                    try:
                        row["evidence"] = json.loads(row["evidence"])
                    except Exception:
                        pass
            total = conn.execute("SELECT COUNT(*) FROM backtest_runs").fetchone()[0]
        return {"findings": rows, "total": int(total)}
    except Exception as e:
        return {"findings": [], "total": 0, "error": str(e)}

# ── Smart Test run ──────────────────────────────────────────────────────────────
@app.post("/run")
async def start_run(request: Request):
    """Start a smart test run. Returns run_id for SSE streaming."""
    body     = await request.json()
    prompt   = body.get("prompt", "")
    max_iter = int(body.get("max_iterations", 30))

    if not prompt.strip():
        return {"error": "prompt is required"}

    # Check for preset hypothesis
    from smart_tester import HYPOTHESIS_PROMPTS
    if prompt.upper() in HYPOTHESIS_PROMPTS:
        prompt = HYPOTHESIS_PROMPTS[prompt.upper()]

    run_id = str(uuid.uuid4())[:8]
    queue  = asyncio.Queue()
    _active_runs[run_id] = queue

    def callback(event_type: str, data: dict):
        if _run_loop:
            _run_loop.call_soon_threadsafe(
                queue.put_nowait,
                {"type": event_type, **data}
            )

    async def run_in_background():
        import smart_tester
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(
                None,
                lambda: smart_tester.run_analysis(prompt, callback, max_iter)
            )
        except Exception as e:
            callback("error", {"message": str(e)})
        finally:
            # Sentinel to close SSE stream
            await queue.put({"type": "done", "findings": []})

    asyncio.create_task(run_in_background())
    return {"run_id": run_id, "prompt": prompt[:100]}

# ── SSE stream ─────────────────────────────────────────────────────────────────
@app.get("/stream/{run_id}")
async def stream_run(run_id: str):
    """Server-Sent Events stream for a smart test run."""
    queue = _active_runs.get(run_id)
    if not queue:
        async def not_found():
            yield f"data: {json.dumps({'type':'error','message':'Run not found'})}\n\n"
        return StreamingResponse(not_found(), media_type="text/event-stream")

    async def generate():
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=120.0)
                except asyncio.TimeoutError:
                    yield "data: {\"type\":\"ping\"}\n\n"
                    continue

                yield f"data: {json.dumps(event, default=str)}\n\n"

                if event.get("type") == "done":
                    break
        finally:
            _active_runs.pop(run_id, None)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":               "no-cache",
            "X-Accel-Buffering":           "no",
            "Access-Control-Allow-Origin": "*",
        },
    )

# ── Direct simulate endpoint ────────────────────────────────────────────────────
@app.post("/simulate")
async def simulate(request: Request):
    """Run a filter simulation directly without the full agent loop."""
    body = await request.json()
    from smart_tester import _simulate_filter
    result = _simulate_filter(
        params    = body.get("params", {}),
        label     = body.get("label", "manual"),
        date_from = body.get("date_from"),
    )
    return result

# ── Direct pattern endpoint ────────────────────────────────────────────────────
@app.post("/pattern")
async def pattern(request: Request):
    """Run a pattern scan directly."""
    body = await request.json()
    from smart_tester import _pattern_scan
    result = _pattern_scan(
        event_sql        = body.get("event_sql", ""),
        instruments      = body.get("instruments", ["spy_price", "qqq_price", "tick_val"]),
        window_before    = int(body.get("window_before", 60)),
        window_after     = int(body.get("window_after", 60)),
    )
    return result

# ── Config proxy to main api ────────────────────────────────────────────────────
@app.get("/config")
async def get_config():
    """Read current config from main api (port 8001) or config.json fallback."""
    try:
        import httpx
        async with httpx.AsyncClient() as client:
            r = await client.get("http://127.0.0.1:8001/config", timeout=2.0)
            return r.json()
    except Exception:
        # Fallback to direct file read
        try:
            cfg_file = THIS_DIR / "config.json"
            return json.loads(cfg_file.read_text())
        except Exception as e:
            return {"error": str(e)}

@app.post("/config")
async def update_config(request: Request):
    """Write config update through main api (port 8001)."""
    body = await request.json()
    try:
        import httpx
        async with httpx.AsyncClient() as client:
            r = await client.post(
                "http://127.0.0.1:8001/config",
                json=body,
                timeout=5.0,
            )
            return r.json()
    except Exception as e:
        return {"error": f"Could not reach main api on port 8001: {e}"}

# ── Export endpoint (for daily_export.py integration) ─────────────────────────
@app.get("/export/today")
async def export_today():
    """Return today's summary as JSON for daily export."""
    from smart_tester import _query_db
    from datetime import date
    today = date.today().isoformat()

    trades   = _query_db(IDEAS_DB, f"""
        SELECT * FROM ideas
        WHERE DATE(surfaced_at) = '{today}'
            AND paper_exit_reason IS NOT NULL
        ORDER BY surfaced_at
    """)
    surfaces = _query_db(IDEAS_DB, f"""
        SELECT * FROM surface_candidates
        WHERE DATE(surfaced_at) = '{today}'
        ORDER BY surfaced_at
    """)

    return {
        "date":     today,
        "trades":   trades,
        "surfaces": surfaces,
    }

# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("Backtesting Dashboard starting on http://127.0.0.1:8003/")
    uvicorn.run(app, host="127.0.0.1", port=8003, log_level="warning")
