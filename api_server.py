import asyncio
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from dotenv import load_dotenv

from auth import AuthUser, get_current_user
from core.loop import AgentLoop4
from mcp_servers.multi_mcp import MultiMCP


BASE_DIR = Path(__file__).parent
SESSIONS_DIR = BASE_DIR / "memory" / "session_summaries_index"
LOCAL_RUNS_DIR = BASE_DIR / "memory" / "local_runs"
load_dotenv(BASE_DIR / ".env")
LOCAL_RUN_STORE = (os.getenv("LOCAL_RUN_STORE") or "0").strip().lower() in {"1", "true", "yes", "on"}


class RunCreateRequest(BaseModel):
    query: str = Field(min_length=1, description="User query to execute")


class ClarificationSubmitRequest(BaseModel):
    response: str = Field(min_length=1, max_length=2000)
    clarification_id: str | None = None


class RunCreateResponse(BaseModel):
    run_id: str
    status: str
    summary: dict[str, Any] = {}


app = FastAPI(title="S15 NewArch API", version="0.1.0")

cors_origins_env = os.getenv("CORS_ORIGINS", "")
cors_origins = [origin.strip() for origin in cors_origins_env.split(",") if origin.strip()]
if not cors_origins:
    cors_origins = ["http://localhost:5173", "http://127.0.0.1:5173"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

RUNS: dict[str, dict[str, Any]] = {}
STREAM_TICKETS: dict[str, dict[str, Any]] = {}


class SupabaseRunStore:
    def __init__(self):
        self.url = (os.getenv("SUPABASE_URL") or "").rstrip("/")
        self.service_key = (os.getenv("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
        self.enabled = bool(self.url and self.service_key)

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "apikey": self.service_key,
            "Authorization": f"Bearer {self.service_key}",
            "Content-Type": "application/json",
        }

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        payload: dict[str, Any] | list[dict[str, Any]] | None = None,
        prefer: str | None = None,
    ) -> Any:
        if not self.enabled:
            return None
        headers = dict(self._headers)
        if prefer:
            headers["Prefer"] = prefer
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.request(
                method,
                f"{self.url}{path}",
                params=params,
                json=payload,
                headers=headers,
            )
            resp.raise_for_status()
            if not resp.content:
                return None
            ctype = resp.headers.get("content-type", "")
            if "application/json" in ctype:
                return resp.json()
            return None

    async def upsert_user(self, user_id: str, email: str | None) -> None:
        if not self.enabled:
            return
        payload = [{
            "id": user_id,
            "email": email,
            "last_seen_at": datetime.utcnow().isoformat(),
        }]
        await self._request(
            "POST",
            "/rest/v1/app_users",
            params={"on_conflict": "id"},
            payload=payload,
            prefer="resolution=merge-duplicates,return=minimal",
        )

    async def upsert_run(self, run_data: dict[str, Any]) -> None:
        if not self.enabled:
            return
        payload = [{
            "run_id": run_data["run_id"],
            "owner_user_id": run_data["owner_user_id"],
            "owner_email": run_data.get("owner_email"),
            "query": run_data.get("query", ""),
            "status": run_data.get("status", "running"),
            "session_id": run_data.get("session_id"),
            "summary": run_data.get("summary", {}) or {},
            "latest_snapshot": run_data.get("latest_snapshot", {}) or {},
            "created_at": run_data.get("created_at") or datetime.utcnow().isoformat(),
            "updated_at": datetime.utcnow().isoformat(),
        }]
        await self._request(
            "POST",
            "/rest/v1/chat_runs",
            params={"on_conflict": "run_id"},
            payload=payload,
            prefer="resolution=merge-duplicates,return=minimal",
        )

    async def list_runs(self, user_id: str, limit: int = 200) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        rows = await self._request(
            "GET",
            "/rest/v1/chat_runs",
            params={
                "select": "run_id,query,status,created_at,session_id,latest_snapshot",
                "owner_user_id": f"eq.{user_id}",
                "order": "created_at.desc",
                "limit": str(limit),
            },
        )
        return rows if isinstance(rows, list) else []

    async def get_run(self, run_id: str, user_id: str) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        rows = await self._request(
            "GET",
            "/rest/v1/chat_runs",
            params={
                "select": "run_id,query,status,created_at,session_id,latest_snapshot,summary",
                "run_id": f"eq.{run_id}",
                "owner_user_id": f"eq.{user_id}",
                "limit": "1",
            },
        )
        if isinstance(rows, list) and rows:
            return rows[0]
        return None

    async def insert_log(
        self,
        *,
        level: str,
        event_type: str,
        message: str,
        owner_user_id: str | None = None,
        run_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        if not self.enabled:
            return
        log_payload = [{
            "level": level,
            "event_type": event_type,
            "message": message,
            "owner_user_id": owner_user_id,
            "run_id": run_id,
            "payload": payload or {},
            "created_at": datetime.utcnow().isoformat(),
        }]
        await self._request(
            "POST",
            "/rest/v1/app_logs",
            payload=log_payload,
            prefer="return=minimal",
        )

    async def list_logs(self, limit: int = 500) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        rows = await self._request(
            "GET",
            "/rest/v1/app_logs",
            params={
                "select": "id,level,event_type,message,owner_user_id,run_id,payload,created_at",
                "order": "created_at.desc",
                "limit": str(limit),
            },
        )
        return rows if isinstance(rows, list) else []


RUN_STORE = SupabaseRunStore()


def _load_session_file(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _local_run_path(run_id: str) -> Path:
    LOCAL_RUNS_DIR.mkdir(parents=True, exist_ok=True)
    return LOCAL_RUNS_DIR / f"{run_id}.json"


def _save_local_run_record(run_id: str) -> None:
    run = RUNS.get(run_id)
    if not run:
        return
    snapshot = run.get("snapshot") if isinstance(run.get("snapshot"), dict) else {}
    payload = {
        "run_id": run_id,
        "owner_user_id": run.get("owner_user_id"),
        "owner_email": run.get("owner_email"),
        "query": run.get("query", ""),
        "created_at": run.get("created_at"),
        "status": run.get("status", "running"),
        "session_id": run.get("session_id"),
        "summary": run.get("summary", {}) if isinstance(run.get("summary"), dict) else {},
        "latest_snapshot": snapshot,
        "updated_at": datetime.utcnow().isoformat(),
    }
    path = _local_run_path(run_id)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)


def _load_local_run_record(run_id: str) -> dict[str, Any] | None:
    path = _local_run_path(run_id)
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            payload = json.load(f)
        return payload if isinstance(payload, dict) else None
    except Exception:
        return None


def _list_local_run_records(user_id: str, limit: int = 200) -> list[dict[str, Any]]:
    if not LOCAL_RUNS_DIR.exists():
        return []
    rows: list[dict[str, Any]] = []
    for path in LOCAL_RUNS_DIR.glob("run_*.json"):
        try:
            with path.open("r", encoding="utf-8") as f:
                payload = json.load(f)
            if not isinstance(payload, dict):
                continue
            if payload.get("owner_user_id") != user_id:
                continue
            rows.append(payload)
        except Exception:
            continue
    rows.sort(key=lambda r: (r.get("created_at") or ""), reverse=True)
    return rows[:limit]


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


def _session_owner_id(payload: dict[str, Any]) -> str | None:
    graph_meta = payload.get("graph", {})
    owner = graph_meta.get("owner_user_id")
    return str(owner) if owner else None


def _run_belongs_to_user(run: dict[str, Any] | None, user_id: str) -> bool:
    if not run:
        return False
    owner = run.get("owner_user_id")
    return bool(owner and owner == user_id)


def _use_supabase_store() -> bool:
    return (not LOCAL_RUN_STORE) and RUN_STORE.enabled


def _is_admin_user(user_id: str) -> bool:
    raw = (os.getenv("ADMIN_USER_IDS") or "").strip()
    if not raw:
        return False
    allowed = {item.strip() for item in raw.split(",") if item.strip()}
    return user_id in allowed


def _prune_stream_tickets() -> None:
    now = time.time()
    expired = [k for k, v in STREAM_TICKETS.items() if v.get("expires_at", 0) < now]
    for k in expired:
        STREAM_TICKETS.pop(k, None)


def _issue_stream_ticket(run_id: str, user_id: str, ttl_seconds: int = 120) -> str:
    _prune_stream_tickets()
    ticket = f"st_{uuid4().hex}"
    STREAM_TICKETS[ticket] = {
        "run_id": run_id,
        "user_id": user_id,
        "expires_at": time.time() + ttl_seconds,
    }
    return ticket


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


def _db_row_to_run_list_item(row: dict[str, Any]) -> dict[str, Any]:
    snapshot = row.get("latest_snapshot") if isinstance(row.get("latest_snapshot"), dict) else {}
    nodes = snapshot.get("nodes", []) if isinstance(snapshot, dict) else []
    return {
        "run_id": row.get("run_id"),
        "query": row.get("query", ""),
        "created_at": row.get("created_at"),
        "status": row.get("status", "unknown"),
        "completed_steps": sum(1 for n in nodes if n.get("id") != "ROOT" and n.get("status") == "completed"),
        "failed_steps": sum(1 for n in nodes if n.get("id") != "ROOT" and n.get("status") == "failed"),
        "total_steps": sum(1 for n in nodes if n.get("id") != "ROOT"),
        "session_id": row.get("session_id"),
    }


def _db_row_to_run_detail(row: dict[str, Any]) -> dict[str, Any]:
    snapshot = row.get("latest_snapshot") if isinstance(row.get("latest_snapshot"), dict) else {}
    detail = snapshot.copy() if isinstance(snapshot, dict) else {}
    detail["run_id"] = row.get("run_id")
    detail["query"] = row.get("query", detail.get("query", ""))
    detail["created_at"] = row.get("created_at", detail.get("created_at"))
    detail["status"] = row.get("status", detail.get("status", "unknown"))
    detail["session_id"] = row.get("session_id", detail.get("session_id"))
    if "nodes" not in detail:
        detail["nodes"] = []
    if "links" not in detail:
        detail["links"] = []
    if "globals_schema" not in detail:
        detail["globals_schema"] = {}
    return detail


def _build_run_record(run_id: str) -> dict[str, Any] | None:
    run = RUNS.get(run_id)
    if not run:
        return None
    return {
        "run_id": run_id,
        "owner_user_id": run.get("owner_user_id"),
        "owner_email": run.get("owner_email"),
        "query": run.get("query", ""),
        "status": run.get("status", "running"),
        "session_id": run.get("session_id"),
        "summary": run.get("summary", {}),
        "latest_snapshot": run.get("snapshot", {}),
        "created_at": run.get("created_at"),
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
        if isinstance(payload["snapshot"], dict):
            payload["snapshot"]["activity"] = run.get("activity", [])[:50]
            payload["snapshot"]["pending_clarification"] = run.get("pending_clarification")
        run["snapshot"] = payload["snapshot"]
        _save_local_run_record(run_id)
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
                run_record = _build_run_record(run_id)
                if run_record and _use_supabase_store():
                    await RUN_STORE.upsert_run(run_record)
        await asyncio.sleep(0.6)


async def _execute_run(run_id: str, query: str, owner_user_id: str, owner_email: str | None) -> None:
    multi_mcp = MultiMCP()
    stop_event = asyncio.Event()
    watcher_task = None
    RUNS[run_id]["status"] = "starting"
    _save_local_run_record(run_id)
    run_record = _build_run_record(run_id)
    if run_record and _use_supabase_store():
        await RUN_STORE.upsert_run(run_record)
    if _use_supabase_store():
        await RUN_STORE.insert_log(
            level="INFO",
            event_type="run_started",
            message="Run execution started",
            owner_user_id=owner_user_id,
            run_id=run_id,
            payload={"query": query},
        )

    try:
        print(f"[run:{run_id}] execution_bootstrap_started")
        await multi_mcp.start()
        print(f"[run:{run_id}] multi_mcp_started")
        
        async def on_agent_activity(event: dict[str, Any]) -> None:
            run = RUNS.get(run_id)
            if not run:
                return
            enriched = {"run_id": run_id, **event}
            activity = run.setdefault("activity", [])
            activity.insert(0, enriched)
            del activity[200:]
            _publish_event(run_id, {"activity": enriched}, event="run_activity")

        async def on_clarification(step_id: str, output: dict[str, Any]) -> str:
            run = RUNS.get(run_id)
            if not run:
                raise RuntimeError("Run not found while waiting for clarification")

            clarification_id = f"cq_{uuid4().hex[:10]}"
            pending = {
                "id": clarification_id,
                "step_id": step_id,
                "agent": "ClarificationAgent",
                "message": output.get("clarificationMessage", "Please provide additional input."),
                "options": output.get("options", []),
                "writes_to": output.get("writes_to", "user_response"),
                "created_at": datetime.utcnow().isoformat(),
            }
            run["pending_clarification"] = pending
            if isinstance(run.get("snapshot"), dict):
                run["snapshot"]["pending_clarification"] = pending
            _publish_event(run_id, {"clarification": pending, "snapshot": run.get("snapshot")}, event="run_clarification")

            timeout_sec = int((os.getenv("CLARIFICATION_TIMEOUT_SEC") or "900").strip())
            waited = 0.0
            while waited < timeout_sec:
                current = RUNS.get(run_id)
                if not current:
                    raise RuntimeError("Run disappeared while waiting for clarification")
                active = current.get("pending_clarification")
                if not active:
                    raise RuntimeError("Clarification state lost before response")
                response = active.get("response")
                if isinstance(response, str) and response.strip():
                    resolved = {**active, "resolved_at": datetime.utcnow().isoformat()}
                    current["pending_clarification"] = None
                    if isinstance(current.get("snapshot"), dict):
                        current["snapshot"]["pending_clarification"] = None
                    _publish_event(
                        run_id,
                        {"clarification": resolved, "snapshot": current.get("snapshot")},
                        event="run_clarification_resolved",
                    )
                    return response.strip()
                await asyncio.sleep(0.5)
                waited += 0.5

            if RUNS.get(run_id):
                RUNS[run_id]["pending_clarification"] = None
                if isinstance(RUNS[run_id].get("snapshot"), dict):
                    RUNS[run_id]["snapshot"]["pending_clarification"] = None
                _publish_event(
                    run_id,
                    {"error": "Timed out waiting for clarification response"},
                    event="run_clarification_timeout",
                )
            raise RuntimeError("Timed out waiting for clarification response")

        loop = AgentLoop4(
            multi_mcp=multi_mcp,
            event_callback=on_agent_activity,
            clarification_callback=on_clarification,
        )

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
                # Stamp owner onto graph metadata as early as possible so
                # persisted session files are user-scoped.
                loop.bootstrap_context.plan_graph.graph["owner_user_id"] = owner_user_id
                if owner_email:
                    loop.bootstrap_context.plan_graph.graph["owner_email"] = owner_email
                RUNS[run_id]["session_id"] = session_id
                RUNS[run_id]["status"] = "running"
                bootstrap_snapshot = _context_to_run_detail(loop.bootstrap_context, status="running")
                bootstrap_snapshot["run_id"] = run_id
                bootstrap_snapshot["session_id"] = session_id
                bootstrap_snapshot["activity"] = RUNS[run_id].get("activity", [])[:50]
                bootstrap_snapshot["pending_clarification"] = RUNS[run_id].get("pending_clarification")
                _publish_event(run_id, {"snapshot": bootstrap_snapshot}, event="run_update")
                run_record = _build_run_record(run_id)
                if run_record and _use_supabase_store():
                    await RUN_STORE.upsert_run(run_record)
                break
            if run_task.done():
                break
            await asyncio.sleep(0.1)

        save_local_sessions = (os.getenv("SAVE_LOCAL_SESSIONS") or "1").strip().lower() not in {
            "0", "false", "off", "no"
        }
        if save_local_sessions:
            watcher_task = asyncio.create_task(_watch_session_file(run_id, stop_event))

        run_timeout_sec = int((os.getenv("RUN_TIMEOUT_SEC") or "900").strip())
        print(f"[run:{run_id}] awaiting_agent_loop timeout_sec={run_timeout_sec}")
        context = await asyncio.wait_for(run_task, timeout=run_timeout_sec)
        print(f"[run:{run_id}] agent_loop_completed")
        context.plan_graph.graph["owner_user_id"] = owner_user_id
        if owner_email:
            context.plan_graph.graph["owner_email"] = owner_email
        # Persist owner metadata to session file only when local saving enabled.
        if save_local_sessions:
            try:
                context._auto_save()
            except Exception:
                pass
        summary = context.get_execution_summary()
        session_id = str(summary.get("session_id"))
        RUNS[run_id]["session_id"] = session_id
        RUNS[run_id]["status"] = "completed"
        RUNS[run_id]["summary"] = summary
        _save_local_run_record(run_id)

        final_snapshot = _context_to_run_detail(context, status="completed")
        final_snapshot["run_id"] = run_id
        final_snapshot["session_id"] = session_id
        final_snapshot["activity"] = RUNS[run_id].get("activity", [])[:50]
        final_snapshot["pending_clarification"] = RUNS[run_id].get("pending_clarification")
        _publish_event(
            run_id,
            {"status": "completed", "summary": summary, "snapshot": final_snapshot},
            event="run_complete",
        )
        if _use_supabase_store():
            await RUN_STORE.insert_log(
                level="INFO",
                event_type="run_completed",
                message="Run execution completed",
                owner_user_id=owner_user_id,
                run_id=run_id,
                payload={"session_id": session_id, "status": "completed"},
            )
        run_record = _build_run_record(run_id)
        if run_record and _use_supabase_store():
            await RUN_STORE.upsert_run(run_record)
    except asyncio.TimeoutError:
        RUNS[run_id]["status"] = "failed"
        timeout_sec = int((os.getenv("RUN_TIMEOUT_SEC") or "900").strip())
        err_msg = f"Run timed out after {timeout_sec}s"
        RUNS[run_id]["error"] = err_msg
        _save_local_run_record(run_id)
        _publish_event(run_id, {"status": "failed", "error": err_msg}, event="run_error")
        if _use_supabase_store():
            await RUN_STORE.insert_log(
                level="ERROR",
                event_type="run_failed",
                message="Run execution timed out",
                owner_user_id=owner_user_id,
                run_id=run_id,
                payload={"error": err_msg, "timeout_sec": timeout_sec},
            )
        run_record = _build_run_record(run_id)
        if run_record and _use_supabase_store():
            await RUN_STORE.upsert_run(run_record)
    except Exception as exc:
        RUNS[run_id]["status"] = "failed"
        RUNS[run_id]["error"] = str(exc)
        _save_local_run_record(run_id)
        _publish_event(run_id, {"status": "failed", "error": str(exc)}, event="run_error")
        if _use_supabase_store():
            await RUN_STORE.insert_log(
                level="ERROR",
                event_type="run_failed",
                message="Run execution failed",
                owner_user_id=owner_user_id,
                run_id=run_id,
                payload={"error": str(exc)},
            )
        run_record = _build_run_record(run_id)
        if run_record and _use_supabase_store():
            await RUN_STORE.upsert_run(run_record)
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


@app.get("/api/me")
async def me(current_user: AuthUser = Depends(get_current_user)) -> dict[str, Any]:
    return {"user_id": current_user.user_id, "email": current_user.email}


@app.get("/api/admin/logs")
async def admin_logs(
    limit: int = 500,
    current_user: AuthUser = Depends(get_current_user),
) -> list[dict[str, Any]]:
    if not _is_admin_user(current_user.user_id):
        raise HTTPException(status_code=403, detail="Admin access required")
    if LOCAL_RUN_STORE:
        return []
    return await RUN_STORE.list_logs(limit=max(1, min(limit, 2000)))


@app.get("/api/runs")
async def list_runs(current_user: AuthUser = Depends(get_current_user)) -> list[dict[str, Any]]:
    active = []
    for run_id, run in RUNS.items():
        if not _run_belongs_to_user(run, current_user.user_id):
            continue
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

    persisted: list[dict[str, Any]] = []
    if LOCAL_RUN_STORE:
        local_rows = _list_local_run_records(current_user.user_id)
        persisted.extend(_db_row_to_run_list_item(row) for row in local_rows)
    else:
        db_rows = await RUN_STORE.list_runs(current_user.user_id)
        persisted.extend(_db_row_to_run_list_item(row) for row in db_rows)

    by_id: dict[str, dict[str, Any]] = {}
    for item in persisted:
        rid = item.get("run_id")
        if rid:
            by_id[rid] = item
    for item in active:
        rid = item.get("run_id")
        if rid:
            by_id[rid] = item

    return sorted(by_id.values(), key=lambda r: (r.get("created_at") or ""), reverse=True)


@app.get("/api/runs/{run_id}")
async def get_run(run_id: str, current_user: AuthUser = Depends(get_current_user)) -> dict[str, Any]:
    if run_id in RUNS and RUNS[run_id].get("snapshot"):
        if not _run_belongs_to_user(RUNS.get(run_id), current_user.user_id):
            raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")
        detail = RUNS[run_id]["snapshot"]
        if isinstance(detail, dict):
            detail = detail.copy()
            detail["pending_clarification"] = RUNS[run_id].get("pending_clarification")
            detail["activity"] = RUNS[run_id].get("activity", [])[:50]
        return detail

    # Persisted history.
    if LOCAL_RUN_STORE:
        local_row = _load_local_run_record(run_id)
        if local_row and local_row.get("owner_user_id") == current_user.user_id:
            return _db_row_to_run_detail(local_row)
    else:
        db_row = await RUN_STORE.get_run(run_id, current_user.user_id)
        if db_row:
            return _db_row_to_run_detail(db_row)

    raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")


@app.post("/api/runs/{run_id}/stream-ticket")
async def create_stream_ticket(
    run_id: str,
    current_user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    if run_id not in RUNS:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not active")
    if not _run_belongs_to_user(RUNS.get(run_id), current_user.user_id):
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")
    ticket = _issue_stream_ticket(run_id, current_user.user_id)
    return {"ticket": ticket, "expires_in": 120}


@app.post("/api/runs/{run_id}/clarification")
async def submit_clarification(
    run_id: str,
    request: ClarificationSubmitRequest,
    current_user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    run = RUNS.get(run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not active")
    if not _run_belongs_to_user(run, current_user.user_id):
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")
    pending = run.get("pending_clarification")
    if not pending:
        raise HTTPException(status_code=409, detail="No pending clarification for this run")
    if request.clarification_id and pending.get("id") != request.clarification_id:
        raise HTTPException(status_code=409, detail="Clarification request no longer active")
    response = request.response.strip()
    if not response:
        raise HTTPException(status_code=400, detail="Clarification response cannot be empty")
    pending["response"] = response
    pending["responded_at"] = datetime.utcnow().isoformat()
    return {"status": "accepted", "run_id": run_id, "clarification_id": pending.get("id")}


@app.get("/api/runs/{run_id}/events")
async def stream_run_events(
    run_id: str,
    request: Request,
    ticket: str | None = None,
) -> StreamingResponse:
    _prune_stream_tickets()
    if not ticket:
        raise HTTPException(status_code=401, detail="Missing stream ticket")
    ticket_payload = STREAM_TICKETS.pop(ticket, None)
    if not ticket_payload:
        raise HTTPException(status_code=401, detail="Invalid or expired stream ticket")
    if ticket_payload.get("run_id") != run_id:
        raise HTTPException(status_code=401, detail="Stream ticket does not match run")
    user_id = ticket_payload.get("user_id")

    if run_id not in RUNS:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not active")
    if not _run_belongs_to_user(RUNS.get(run_id), user_id):
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")

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
async def create_run(
    request: RunCreateRequest,
    current_user: AuthUser = Depends(get_current_user),
) -> RunCreateResponse:
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
            "activity": [],
        },
        "subscribers": set(),
        "session_id": None,
        "owner_user_id": current_user.user_id,
        "owner_email": current_user.email,
        "activity": [],
        "pending_clarification": None,
    }
    _save_local_run_record(run_id)
    if _use_supabase_store():
        await RUN_STORE.upsert_user(current_user.user_id, current_user.email)
    run_record = _build_run_record(run_id)
    if run_record and _use_supabase_store():
        await RUN_STORE.upsert_run(run_record)
    if _use_supabase_store():
        await RUN_STORE.insert_log(
            level="INFO",
            event_type="run_created",
            message="Run created",
            owner_user_id=current_user.user_id,
            run_id=run_id,
            payload={"query": request.query},
        )
    asyncio.create_task(_execute_run(run_id, request.query, current_user.user_id, current_user.email))
    return RunCreateResponse(run_id=run_id, status="running", summary={})

