import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from core.loop import AgentLoop4
from mcp_servers.multi_mcp import MultiMCP


BASE_DIR = Path(__file__).parent
SESSIONS_DIR = BASE_DIR / "memory" / "session_summaries_index"


class RunCreateRequest(BaseModel):
    query: str = Field(min_length=1, description="User query to execute")


class RunCreateResponse(BaseModel):
    run_id: str
    status: str
    summary: dict[str, Any] = {}


app = FastAPI(title="S15 NewArch API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

RUNS: dict[str, dict[str, Any]] = {}


def _load_session_file(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _find_run_file(run_id: str) -> Path | None:
    if not SESSIONS_DIR.exists():
        return None
    matches = list(SESSIONS_DIR.rglob(f"session_{run_id}.json"))
    return matches[0] if matches else None


def _session_to_run_list_item(path: Path) -> dict[str, Any]:
    payload = _load_session_file(path)
    graph_meta = payload.get("graph", {})
    nodes = payload.get("nodes", [])

    completed = sum(1 for n in nodes if n.get("id") != "ROOT" and n.get("status") == "completed")
    failed = sum(1 for n in nodes if n.get("id") != "ROOT" and n.get("status") == "failed")
    total = sum(1 for n in nodes if n.get("id") != "ROOT")

    return {
        "run_id": graph_meta.get("session_id"),
        "query": graph_meta.get("original_query", ""),
        "created_at": graph_meta.get("created_at"),
        "status": graph_meta.get("status", "unknown"),
        "completed_steps": completed,
        "failed_steps": failed,
        "total_steps": total,
        "path": str(path),
    }


def _session_to_run_detail(payload: dict[str, Any]) -> dict[str, Any]:
    graph_meta = payload.get("graph", {})
    nodes = payload.get("nodes", [])
    links = payload.get("links", payload.get("edges", []))
    globals_schema = graph_meta.get("globals_schema", {})

    return {
        "run_id": graph_meta.get("session_id"),
        "query": graph_meta.get("original_query", ""),
        "created_at": graph_meta.get("created_at"),
        "status": graph_meta.get("status", "unknown"),
        "nodes": nodes,
        "links": links,
        "globals_schema": globals_schema,
    }


def _context_to_run_detail(context, status: str = "running") -> dict[str, Any]:
    graph = context.plan_graph
    nodes = [{"id": node_id, **node_data} for node_id, node_data in graph.nodes(data=True)]
    links = [{"source": source, "target": target} for source, target in graph.edges()]
    return {
        "run_id": graph.graph.get("session_id"),
        "query": graph.graph.get("original_query", ""),
        "created_at": graph.graph.get("created_at"),
        "status": status,
        "nodes": nodes,
        "links": links,
        "globals_schema": graph.graph.get("globals_schema", {}),
    }


def _serialize_sse(data: dict[str, Any], event: str = "message") -> str:
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"


def _publish_event(run_id: str, payload: dict[str, Any], event: str = "message") -> None:
    run = RUNS.get(run_id)
    if not run:
        return
    if payload.get("snapshot"):
        run["snapshot"] = payload["snapshot"]
    subscribers = run.get("subscribers", set())
    for queue in list(subscribers):
        try:
            queue.put_nowait((event, payload))
        except Exception:
            pass


async def _watch_session_file(run_id: str, stop_event: asyncio.Event) -> None:
    last_mtime = None
    while not stop_event.is_set():
        run = RUNS.get(run_id)
        if not run:
            return
        session_id = run.get("session_id")
        if not session_id:
            await asyncio.sleep(0.2)
            continue

        session_file = _find_run_file(session_id)
        if session_file and session_file.exists():
            mtime = session_file.stat().st_mtime
            if last_mtime is None or mtime > last_mtime:
                last_mtime = mtime
                payload = _load_session_file(session_file)
                snapshot = _session_to_run_detail(payload)
                snapshot["run_id"] = run_id
                snapshot["session_id"] = session_id
                snapshot["status"] = run.get("status", snapshot.get("status", "running"))
                _publish_event(run_id, {"snapshot": snapshot}, event="run_update")
        await asyncio.sleep(0.6)


async def _execute_run(run_id: str, query: str) -> None:
    multi_mcp = MultiMCP()
    stop_event = asyncio.Event()
    watcher_task = None
    RUNS[run_id]["status"] = "starting"

    try:
        await multi_mcp.start()
        loop = AgentLoop4(multi_mcp=multi_mcp)

        run_task = asyncio.create_task(
            loop.run(
                query=query,
                file_manifest=[],
                globals_schema={},
                uploaded_files=[],
            )
        )

        # Wait briefly for bootstrap context creation and publish immediately.
        for _ in range(20):
            if loop.bootstrap_context:
                session_id = loop.bootstrap_context.plan_graph.graph.get("session_id")
                RUNS[run_id]["session_id"] = session_id
                RUNS[run_id]["status"] = "running"
                bootstrap_snapshot = _context_to_run_detail(loop.bootstrap_context, status="running")
                bootstrap_snapshot["run_id"] = run_id
                bootstrap_snapshot["session_id"] = session_id
                _publish_event(run_id, {"snapshot": bootstrap_snapshot}, event="run_update")
                break
            if run_task.done():
                break
            await asyncio.sleep(0.1)

        watcher_task = asyncio.create_task(_watch_session_file(run_id, stop_event))

        context = await run_task
        summary = context.get_execution_summary()
        session_id = str(summary.get("session_id"))
        RUNS[run_id]["session_id"] = session_id
        RUNS[run_id]["status"] = "completed"
        RUNS[run_id]["summary"] = summary

        final_snapshot = _context_to_run_detail(context, status="completed")
        final_snapshot["run_id"] = run_id
        final_snapshot["session_id"] = session_id
        _publish_event(
            run_id,
            {"status": "completed", "summary": summary, "snapshot": final_snapshot},
            event="run_complete",
        )
    except Exception as exc:
        RUNS[run_id]["status"] = "failed"
        RUNS[run_id]["error"] = str(exc)
        _publish_event(run_id, {"status": "failed", "error": str(exc)}, event="run_error")
    finally:
        stop_event.set()
        if watcher_task:
            watcher_task.cancel()
            try:
                await watcher_task
            except Exception:
                pass
        await multi_mcp.stop()


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/runs")
async def list_runs() -> list[dict[str, Any]]:
    active = []
    for run_id, run in RUNS.items():
        snapshot = run.get("snapshot", {})
        active.append(
            {
                "run_id": run_id,
                "query": run.get("query", ""),
                "created_at": run.get("created_at"),
                "status": run.get("status", "running"),
                "completed_steps": sum(
                    1
                    for n in snapshot.get("nodes", [])
                    if n.get("id") != "ROOT" and n.get("status") == "completed"
                ),
                "failed_steps": sum(
                    1
                    for n in snapshot.get("nodes", [])
                    if n.get("id") != "ROOT" and n.get("status") == "failed"
                ),
                "total_steps": sum(1 for n in snapshot.get("nodes", []) if n.get("id") != "ROOT"),
                "session_id": run.get("session_id"),
            }
        )

    persisted = []
    if SESSIONS_DIR.exists():
        files = sorted(SESSIONS_DIR.rglob("session_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        persisted = [_session_to_run_list_item(path) for path in files]

    return sorted(active, key=lambda r: (r.get("created_at") or ""), reverse=True) + persisted


@app.get("/api/runs/{run_id}")
async def get_run(run_id: str) -> dict[str, Any]:
    if run_id in RUNS and RUNS[run_id].get("snapshot"):
        return RUNS[run_id]["snapshot"]

    # Accept session id for persisted runs too.
    run_file = _find_run_file(run_id)
    if run_file:
        payload = _load_session_file(run_file)
        return _session_to_run_detail(payload)

    # Fallback: if temporary run id has mapped session id, try that.
    run = RUNS.get(run_id)
    if run and run.get("session_id"):
        run_file = _find_run_file(run["session_id"])
        if run_file:
            payload = _load_session_file(run_file)
            detail = _session_to_run_detail(payload)
            detail["run_id"] = run_id
            detail["session_id"] = run["session_id"]
            detail["status"] = run.get("status", detail.get("status", "unknown"))
            return detail

    raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")


@app.get("/api/runs/{run_id}/events")
async def stream_run_events(run_id: str, request: Request) -> StreamingResponse:
    if run_id not in RUNS:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not active")

    queue: asyncio.Queue = asyncio.Queue()
    RUNS[run_id].setdefault("subscribers", set()).add(queue)

    async def event_generator():
        try:
            # Push latest snapshot immediately for fast UI hydration.
            latest = RUNS[run_id].get("snapshot")
            if latest:
                yield _serialize_sse({"snapshot": latest}, event="run_update")

            while True:
                if await request.is_disconnected():
                    break
                try:
                    event, payload = await asyncio.wait_for(queue.get(), timeout=15)
                    yield _serialize_sse(payload, event=event)
                    if event in {"run_complete", "run_error"}:
                        break
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            run = RUNS.get(run_id)
            if run:
                run.get("subscribers", set()).discard(queue)
    response = StreamingResponse(event_generator(), media_type="text/event-stream")
    response.headers["Cache-Control"] = "no-cache"
    response.headers["Connection"] = "keep-alive"
    return response


@app.post("/api/runs", response_model=RunCreateResponse)
async def create_run(request: RunCreateRequest) -> RunCreateResponse:
    run_id = f"run_{uuid4().hex[:10]}"
    RUNS[run_id] = {
        "query": request.query,
        "created_at": datetime.utcnow().isoformat(),
        "status": "queued",
        "summary": {},
        "snapshot": {
            "run_id": run_id,
            "query": request.query,
            "status": "queued",
            "nodes": [
                {
                    "id": "BOOTSTRAP_PLANNING",
                    "agent": "PlannerAgent",
                    "description": "Phase 1: Planning...",
                    "status": "running",
                    "reads": [],
                    "writes": [],
                }
            ],
            "links": [],
            "globals_schema": {},
        },
        "subscribers": set(),
        "session_id": None,
    }
    asyncio.create_task(_execute_run(run_id, request.query))
    return RunCreateResponse(run_id=run_id, status="running", summary={})

