"""
backtest_dashboard.py — Backtesting Dashboard for tos-dash-v2.
Run: python backtest_dashboard.py
URL: http://127.0.0.1:8003/
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

THIS_DIR  = Path(__file__).parent
DASH_FILE = THIS_DIR / "backtest_dashboard.html"
IDEAS_DB  = Path("D:/tos-dash-v2-data/ideas.duckdb")
TICKS_DB  = Path("D:/tos-dash-v2-replay/ticks.duckdb")

app = FastAPI(title="tos-dash-v2 Backtesting Dashboard")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

_active_runs: dict[str, asyncio.Queue] = {}
_run_loop: Optional[asyncio.AbstractEventLoop] = None

@app.on_event("startup")
async def startup():
    global _run_loop
    _run_loop = asyncio.get_event_loop()
    try:
        import smart_tester
        smart_tester.ensure_schema()
    except Exception as e:
        print(f"[startup] schema warning: {e}")

@app.get("/", response_class=HTMLResponse)
def serve_dashboard():
    try:
        return HTMLResponse(DASH_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return HTMLResponse("<h1>backtest_dashboard.html not found</h1>", status_code=404)

@app.get("/metrics")
def get_metrics():
    try:
        with duckdb.connect(str(IDEAS_DB), read_only=True) as conn:
            overall = conn.execute("""
                SELECT COUNT(*) AS total_trades,
                       ROUND(AVG(paper_pnl_pct),2) AS avg_pnl,
                       ROUND(SUM(paper_net_dollar_pnl),2) AS total_net,
                       ROUND(AVG(CASE WHEN paper_pnl_pct>0 THEN 1.0 ELSE 0.0 END)*100,1) AS win_rate,
                       ROUND(AVG(CASE WHEN paper_exit_reason='STOP' THEN 1.0 ELSE 0.0 END)*100,1) AS stop_rate,
                       COUNT(DISTINCT DATE(surfaced_at)) AS trading_days
                FROM ideas WHERE paper_exit_reason IS NOT NULL
                  AND EXTRACT(HOUR FROM CAST(surfaced_at AS TIMESTAMP)) BETWEEN 9 AND 16
            """).fetchone()

            exp = conn.execute("""
                SELECT ROUND(AVG(CASE WHEN paper_pnl_pct>0 THEN paper_pnl_pct END),2) AS avg_win,
                       ROUND(AVG(CASE WHEN paper_pnl_pct<0 THEN ABS(paper_pnl_pct) END),2) AS avg_loss,
                       ROUND(AVG(CASE WHEN paper_pnl_pct>0 THEN 1.0 ELSE 0.0 END),4) AS win_rate
                FROM ideas WHERE paper_exit_reason IS NOT NULL
            """).fetchone()

            by_regime = conn.execute("""
                SELECT entry_regime, COUNT(*) AS n,
                       ROUND(AVG(paper_pnl_pct),2) AS avg_pnl,
                       ROUND(SUM(paper_net_dollar_pnl),2) AS total_net,
                       ROUND(AVG(CASE WHEN paper_pnl_pct>0 THEN 1.0 ELSE 0.0 END)*100,1) AS win_rate
                FROM ideas WHERE paper_exit_reason IS NOT NULL
                  AND EXTRACT(HOUR FROM CAST(surfaced_at AS TIMESTAMP)) BETWEEN 9 AND 16
                GROUP BY entry_regime ORDER BY n DESC
            """).fetchdf().to_dict(orient="records")

            by_score = conn.execute("""
                SELECT FLOOR(entry_score/2)*2 AS band_floor, COUNT(*) AS n,
                       ROUND(AVG(paper_pnl_pct),2) AS avg_pnl,
                       ROUND(SUM(paper_net_dollar_pnl),2) AS total_net
                FROM ideas WHERE paper_exit_reason IS NOT NULL
                  AND EXTRACT(HOUR FROM CAST(surfaced_at AS TIMESTAMP)) BETWEEN 9 AND 16
                GROUP BY band_floor ORDER BY band_floor
            """).fetchdf().to_dict(orient="records")

            by_exit = conn.execute("""
                SELECT paper_exit_reason AS exit_reason, COUNT(*) AS n,
                       ROUND(AVG(paper_pnl_pct),2) AS avg_pnl
                FROM ideas WHERE paper_exit_reason IS NOT NULL
                GROUP BY paper_exit_reason ORDER BY n DESC
            """).fetchdf().to_dict(orient="records")

            balance = conn.execute("""
                SELECT ROW_NUMBER() OVER (ORDER BY surfaced_at) AS trade_num,
                       paper_net_dollar_pnl,
                       SUM(paper_net_dollar_pnl) OVER (ORDER BY surfaced_at) AS running_total
                FROM ideas WHERE paper_exit_reason IS NOT NULL
                  AND EXTRACT(HOUR FROM CAST(surfaced_at AS TIMESTAMP)) BETWEEN 9 AND 16
                ORDER BY surfaced_at
            """).fetchdf()
            balance_rows = json.loads(balance.to_json(orient="records", date_format="iso"))

            aw, al, wr = (exp[0] or 0), (exp[1] or 0), (exp[2] or 0)
            expectancy = round((aw * wr) - (al * (1 - wr)), 2)
            pf = conn.execute("""
                SELECT SUM(CASE WHEN paper_net_dollar_pnl>0 THEN paper_net_dollar_pnl ELSE 0 END),
                       SUM(CASE WHEN paper_net_dollar_pnl<0 THEN ABS(paper_net_dollar_pnl) ELSE 0 END)
                FROM ideas WHERE paper_exit_reason IS NOT NULL
            """).fetchone()
            profit_factor = round(pf[0]/pf[1], 2) if pf[1] and pf[1] > 0 else None

        return {
            "overall": {"total_trades": int(overall[0] or 0), "avg_pnl": float(overall[1] or 0),
                        "total_net": float(overall[2] or 0), "win_rate": float(overall[3] or 0),
                        "stop_rate": float(overall[4] or 0), "trading_days": int(overall[5] or 0),
                        "expectancy": expectancy, "profit_factor": profit_factor},
            "by_regime": by_regime, "by_score": by_score, "by_exit": by_exit, "balance": balance_rows,
        }
    except Exception as e:
        return {"error": str(e)}

@app.get("/findings")
def get_findings(limit: int = 50):
    try:
        with duckdb.connect(str(IDEAS_DB), read_only=True) as conn:
            tables = conn.execute("SELECT table_name FROM information_schema.tables WHERE table_name='backtest_runs'").fetchall()
            if not tables:
                return {"findings": [], "total": 0}
            df = conn.execute(f"""
                SELECT id, run_at, hypothesis, verdict, summary, evidence, recommendation, trade_count, date_range
                FROM backtest_runs ORDER BY run_at DESC LIMIT {limit}
            """).fetchdf()
            rows = json.loads(df.to_json(orient="records", date_format="iso"))
            for row in rows:
                if isinstance(row.get("evidence"), str):
                    try: row["evidence"] = json.loads(row["evidence"])
                    except: pass
            total = conn.execute("SELECT COUNT(*) FROM backtest_runs").fetchone()[0]
        return {"findings": rows, "total": int(total)}
    except Exception as e:
        return {"findings": [], "total": 0, "error": str(e)}

@app.post("/run")
async def start_run(request: Request):
    body     = await request.json()
    prompt   = body.get("prompt", "")
    max_iter = int(body.get("max_iterations", 30))
    if not prompt.strip():
        return {"error": "prompt is required"}

    from smart_tester import HYPOTHESIS_PROMPTS
    if prompt.upper() in HYPOTHESIS_PROMPTS:
        prompt = HYPOTHESIS_PROMPTS[prompt.upper()]

    run_id = str(uuid.uuid4())[:8]
    queue  = asyncio.Queue()
    _active_runs[run_id] = queue

    def callback(event_type, data):
        if _run_loop:
            _run_loop.call_soon_threadsafe(queue.put_nowait, {"type": event_type, **data})

    async def run_in_background():
        import smart_tester
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, lambda: smart_tester.run_analysis(prompt, callback, max_iter))
        except Exception as e:
            callback("error", {"message": str(e)})
        finally:
            await queue.put({"type": "done", "findings": []})

    asyncio.create_task(run_in_background())
    return {"run_id": run_id, "prompt": prompt[:100]}

@app.get("/stream/{run_id}")
async def stream_run(run_id: str):
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

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                                      "Access-Control-Allow-Origin": "*"})

@app.post("/simulate")
async def simulate(request: Request):
    body = await request.json()
    from smart_tester import _simulate_filter
    return _simulate_filter(body.get("params", {}), body.get("label", "manual"), body.get("date_from"))

@app.post("/pattern")
async def pattern(request: Request):
    body = await request.json()
    from smart_tester import _pattern_scan
    return _pattern_scan(body.get("event_sql",""), body.get("instruments",["spy_price","tick_val"]),
                         int(body.get("window_before", 60)), int(body.get("window_after", 60)))

@app.get("/config")
async def get_config():
    try:
        import httpx
        async with httpx.AsyncClient() as client:
            r = await client.get("http://127.0.0.1:8001/config", timeout=2.0)
            return r.json()
    except Exception:
        try:
            return json.loads((THIS_DIR / "config.json").read_text())
        except Exception as e:
            return {"error": str(e)}

@app.post("/config")
async def update_config(request: Request):
    body = await request.json()
    try:
        import httpx
        async with httpx.AsyncClient() as client:
            r = await client.post("http://127.0.0.1:8001/config", json=body, timeout=5.0)
            return r.json()
    except Exception as e:
        return {"error": f"Could not reach main api on port 8001: {e}"}

@app.get("/export/today")
async def export_today():
    from smart_tester import _query_db
    from datetime import date
    today = date.today().isoformat()
    return {
        "date":     today,
        "trades":   _query_db(IDEAS_DB, f"SELECT * FROM ideas WHERE DATE(surfaced_at)='{today}' AND paper_exit_reason IS NOT NULL ORDER BY surfaced_at"),
        "surfaces": _query_db(IDEAS_DB, f"SELECT * FROM surface_candidates WHERE DATE(surfaced_at)='{today}' ORDER BY surfaced_at"),
    }

if __name__ == "__main__":
    print("Backtesting Dashboard: http://127.0.0.1:8003/")
    uvicorn.run(app, host="127.0.0.1", port=8003, log_level="warning")
