"""
backtest_dashboard.py — Backtesting Dashboard for tos-dash-v2.
All DuckDB queries route through api.py (port 8001) to avoid file lock conflicts.

Run: python backtest_dashboard.py  (or auto-started by api.py on startup)
URL: http://127.0.0.1:8003/
"""

import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse

THIS_DIR  = Path(__file__).parent
DASH_FILE = THIS_DIR / "backtest_dashboard.html"
API_BASE  = "http://127.0.0.1:8001"

_active_runs: dict[str, asyncio.Queue] = {}
_completed_runs: dict[str, dict] = {}   # run_id -> final event, kept 30s
_run_loop: Optional[asyncio.AbstractEventLoop] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _run_loop
    _run_loop = asyncio.get_event_loop()
    yield


app = FastAPI(title="tos-dash-v2 Backtesting Dashboard", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/", response_class=HTMLResponse)
def serve_dashboard():
    try:
        return HTMLResponse(DASH_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return HTMLResponse("<h1>backtest_dashboard.html not found</h1>", status_code=404)


async def _api_query(sql: str) -> dict:
    """POST a SELECT to api.py /backtest/query and return result."""
    async with httpx.AsyncClient() as client:
        r = await client.post(f"{API_BASE}/backtest/query", json={"sql": sql}, timeout=15.0)
        return r.json()


@app.get("/metrics")
async def get_metrics():
    """Return key trading metrics by querying ideas.duckdb via api.py."""
    try:
        overall_r = await _api_query("""
            SELECT COUNT(*) AS total_trades,
                   ROUND(AVG(paper_pnl_pct),2) AS avg_pnl,
                   ROUND(SUM(paper_net_dollar_pnl),2) AS total_net,
                   ROUND(AVG(CASE WHEN paper_pnl_pct>0 THEN 1.0 ELSE 0.0 END)*100,1) AS win_rate,
                   ROUND(AVG(CASE WHEN paper_exit_reason='STOP' THEN 1.0 ELSE 0.0 END)*100,1) AS stop_rate,
                   COUNT(DISTINCT DATE(surfaced_at)) AS trading_days
            FROM ideas
            WHERE paper_exit_reason IS NOT NULL
              AND EXTRACT(HOUR FROM CAST(surfaced_at AS TIMESTAMP)) BETWEEN 9 AND 16
        """)
        exp_r = await _api_query("""
            SELECT ROUND(AVG(CASE WHEN paper_pnl_pct>0 THEN paper_pnl_pct END),2) AS avg_win,
                   ROUND(AVG(CASE WHEN paper_pnl_pct<0 THEN ABS(paper_pnl_pct) END),2) AS avg_loss,
                   ROUND(AVG(CASE WHEN paper_pnl_pct>0 THEN 1.0 ELSE 0.0 END),4) AS win_rate
            FROM ideas WHERE paper_exit_reason IS NOT NULL
        """)
        regime_r  = await _api_query("""
            SELECT entry_regime, COUNT(*) AS n,
                   ROUND(AVG(paper_pnl_pct),2) AS avg_pnl,
                   ROUND(SUM(paper_net_dollar_pnl),2) AS total_net,
                   ROUND(AVG(CASE WHEN paper_pnl_pct>0 THEN 1.0 ELSE 0.0 END)*100,1) AS win_rate
            FROM ideas WHERE paper_exit_reason IS NOT NULL
              AND EXTRACT(HOUR FROM CAST(surfaced_at AS TIMESTAMP)) BETWEEN 9 AND 16
            GROUP BY entry_regime ORDER BY n DESC
        """)
        score_r   = await _api_query("""
            SELECT FLOOR(entry_score/2)*2 AS band_floor, COUNT(*) AS n,
                   ROUND(AVG(paper_pnl_pct),2) AS avg_pnl,
                   ROUND(SUM(paper_net_dollar_pnl),2) AS total_net
            FROM ideas WHERE paper_exit_reason IS NOT NULL
              AND EXTRACT(HOUR FROM CAST(surfaced_at AS TIMESTAMP)) BETWEEN 9 AND 16
            GROUP BY band_floor ORDER BY band_floor
        """)
        exit_r    = await _api_query("""
            SELECT paper_exit_reason AS exit_reason, COUNT(*) AS n,
                   ROUND(AVG(paper_pnl_pct),2) AS avg_pnl
            FROM ideas WHERE paper_exit_reason IS NOT NULL
            GROUP BY paper_exit_reason ORDER BY n DESC
        """)
        balance_r = await _api_query("""
            SELECT ROW_NUMBER() OVER (ORDER BY surfaced_at) AS trade_num,
                   paper_net_dollar_pnl,
                   SUM(paper_net_dollar_pnl) OVER (ORDER BY surfaced_at) AS running_total
            FROM ideas WHERE paper_exit_reason IS NOT NULL
              AND EXTRACT(HOUR FROM CAST(surfaced_at AS TIMESTAMP)) BETWEEN 9 AND 16
            ORDER BY surfaced_at
        """)
        pf_r = await _api_query("""
            SELECT SUM(CASE WHEN paper_net_dollar_pnl>0 THEN paper_net_dollar_pnl ELSE 0 END) AS gp,
                   SUM(CASE WHEN paper_net_dollar_pnl<0 THEN ABS(paper_net_dollar_pnl) ELSE 0 END) AS gl
            FROM ideas WHERE paper_exit_reason IS NOT NULL
        """)

        if overall_r.get("error"):
            return {"error": overall_r["error"]}

        o  = overall_r["rows"][0] if overall_r["rows"] else {}
        ex = exp_r["rows"][0]    if exp_r.get("rows") else {}
        pf = pf_r["rows"][0]     if pf_r.get("rows") else {}

        aw = float(ex.get("avg_win") or 0)
        al = float(ex.get("avg_loss") or 0)
        wr = float(ex.get("win_rate") or 0)
        expectancy    = round((aw * wr) - (al * (1 - wr)), 2)
        gl            = float(pf.get("gl") or 0)
        profit_factor = round(float(pf.get("gp") or 0) / gl, 2) if gl > 0 else None

        return {
            "overall": {
                "total_trades":  int(o.get("total_trades") or 0),
                "avg_pnl":       float(o.get("avg_pnl") or 0),
                "total_net":     float(o.get("total_net") or 0),
                "win_rate":      float(o.get("win_rate") or 0),
                "stop_rate":     float(o.get("stop_rate") or 0),
                "trading_days":  int(o.get("trading_days") or 0),
                "expectancy":    expectancy,
                "profit_factor": profit_factor,
            },
            "by_regime": regime_r.get("rows", []),
            "by_score":  score_r.get("rows", []),
            "by_exit":   exit_r.get("rows", []),
            "balance":   balance_r.get("rows", []),
        }
    except Exception as e:
        return {"error": str(e)}


@app.get("/findings")
async def get_findings(limit: int = 50):
    try:
        r = await _api_query(f"""
            SELECT id, run_at, hypothesis, verdict, summary, evidence,
                   recommendation, trade_count, date_range
            FROM backtest_runs
            ORDER BY run_at DESC
            LIMIT {limit}
        """)
        if r.get("error"):
            return {"findings": [], "total": 0, "error": r["error"]}
        rows = r.get("rows", [])
        for row in rows:
            if isinstance(row.get("evidence"), str):
                try:
                    row["evidence"] = json.loads(row["evidence"])
                except Exception:
                    pass
        total_r = await _api_query("SELECT COUNT(*) AS n FROM backtest_runs")
        total   = total_r["rows"][0]["n"] if total_r.get("rows") else 0
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

    _stop_flag = {"requested": False}

    def callback(event_type, data):
        if _run_loop:
            _run_loop.call_soon_threadsafe(queue.put_nowait, {"type": event_type, **data})
        # Check if a stop sentinel was placed in the queue
        if event_type == "tool_call" and not _stop_flag["requested"]:
            # Non-blocking check for stop sentinel
            try:
                while not queue.empty():
                    item = queue.get_nowait()
                    if item.get("type") == "stopped":
                        _stop_flag["requested"] = True
                        callback._stop_requested = True
                    else:
                        queue.put_nowait(item)
                    break
            except Exception:
                pass

    callback._stop_requested = False

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
    # Return cached final event if run already completed
    if run_id in _completed_runs:
        cached = _completed_runs[run_id]
        async def replay():
            yield f"data: {json.dumps(cached, default=str)}\n\n"
        return StreamingResponse(replay(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "Access-Control-Allow-Origin": "*"})

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
                    # Cache the final event for 30s in case client reconnects
                    _completed_runs[run_id] = event
                    asyncio.get_event_loop().call_later(
                        30, lambda: _completed_runs.pop(run_id, None)
                    )
                    break
        finally:
            _active_runs.pop(run_id, None)

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no",
                                      "Access-Control-Allow-Origin": "*"})


@app.post("/run/{run_id}/stop")
async def stop_run(run_id: str):
    """Signal a running analysis to stop after its current tool call."""
    queue = _active_runs.get(run_id)
    if not queue:
        return {"stopped": False, "reason": "Run not found or already complete"}
    # Put a stop sentinel into the queue — the background thread checks for it
    await queue.put({"type": "stopped", "message": "Stopped by user"})
    return {"stopped": True, "run_id": run_id}


@app.post("/simulate")
async def simulate(request: Request):
    body = await request.json()
    from smart_tester import _simulate_filter
    loop   = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        None, lambda: _simulate_filter(body.get("params", {}), body.get("label", "manual"), body.get("date_from"))
    )
    return result


@app.get("/moc/events")
async def get_moc_events(limit: int = 50):
    """Proxy to api.py moc events."""
    async with httpx.AsyncClient() as client:
        r = await client.get(f"{API_BASE}/moc/events?limit={limit}", timeout=5.0)
        return r.json()


@app.post("/moc/event")
async def save_moc_event(request: Request):
    """Proxy to api.py moc event save."""
    body = await request.json()
    async with httpx.AsyncClient() as client:
        r = await client.post(f"{API_BASE}/moc/event", json=body, timeout=5.0)
        return r.json()


@app.post("/tick-recorder/pause")
async def pause_tick_recorder():
    """Proxy to api.py tick recorder pause."""
    async with httpx.AsyncClient() as client:
        r = await client.post(f"{API_BASE}/tick-recorder/pause", timeout=5.0)
        return r.json()


@app.post("/tick-recorder/resume")
async def resume_tick_recorder():
    """Proxy to api.py tick recorder resume."""
    async with httpx.AsyncClient() as client:
        r = await client.post(f"{API_BASE}/tick-recorder/resume", timeout=5.0)
        return r.json()


@app.get("/tick-recorder/status")
async def tick_recorder_status():
    """Proxy to api.py tick recorder status."""
    async with httpx.AsyncClient() as client:
        r = await client.get(f"{API_BASE}/tick-recorder/status", timeout=5.0)
        return r.json()


@app.post("/pattern")
async def pattern(request: Request):
    body = await request.json()
    async with httpx.AsyncClient() as client:
        r = await client.post(f"{API_BASE}/backtest/pattern", json=body, timeout=60.0)
        return r.json()


@app.get("/config")
async def get_config():
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(f"{API_BASE}/config", timeout=2.0)
            return r.json()
    except Exception:
        try:
            return json.loads((THIS_DIR / "config.json").read_text())
        except Exception as e:
            return {"error": str(e)}


@app.get("/config/history")
async def get_config_history(limit: int = 15):
    """Proxy to api.py config history."""
    async with httpx.AsyncClient() as client:
        r = await client.get(f"{API_BASE}/config/history?limit={limit}", timeout=5.0)
        return r.json()


@app.post("/config")
async def update_config(request: Request):
    body = await request.json()
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(f"{API_BASE}/config", json=body, timeout=5.0)
            return r.json()
    except Exception as e:
        return {"error": f"Could not reach api on port 8001: {e}"}


if __name__ == "__main__":
    print("Backtesting Dashboard: http://127.0.0.1:8003/")
    uvicorn.run(app, host="127.0.0.1", port=8003, log_level="warning")
