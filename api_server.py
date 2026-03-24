import asyncio
import json
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from dotenv import load_dotenv

from auth import AuthUser, get_current_user
from agents.base_agent import AgentRunner
from core.loop import AgentLoop4
from mcp_servers.multi_mcp import MultiMCP

from scheduler_service import build_query


BASE_DIR = Path(__file__).parent
SESSIONS_DIR = BASE_DIR / "memory" / "session_summaries_index"
LOCAL_RUNS_DIR = BASE_DIR / "memory" / "local_runs"
LOCAL_SCHEDULED_JOBS_DIR = BASE_DIR / "memory" / "scheduled_jobs"
load_dotenv(BASE_DIR / ".env")
LOCAL_RUN_STORE = (os.getenv("LOCAL_RUN_STORE") or "0").strip().lower() in {"1", "true", "yes", "on"}


class RunCreateRequest(BaseModel):
    query: str = Field(min_length=1, description="User query to execute")


class ClarificationSubmitRequest(BaseModel):
    response: str = Field(min_length=1, max_length=2000)
    clarification_id: str | None = None


class FormatterFollowupRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=4000)


class NoteCreateRequest(BaseModel):
    content: str = Field(default="", max_length=100000)
    name: str = Field(default="untitled.txt", min_length=1, max_length=200)
    kind: str = Field(default="file")
    parent_id: str | None = None


class NoteUpdateRequest(BaseModel):
    content: str | None = Field(default=None, max_length=100000)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    kind: str | None = None
    parent_id: str | None = None


class RunCreateResponse(BaseModel):
    run_id: str
    status: str
    summary: dict[str, Any] = {}


class MailSendRequest(BaseModel):
    to: str = Field(min_length=1)
    subject: str = Field(default="")
    body: str = Field(default="")


class MailReplyRequest(BaseModel):
    message_id: str = Field(min_length=1)
    body: str = Field(min_length=1)


class MailDraftRequest(BaseModel):
    message_id: str = Field(min_length=1)


class ScheduledJobCreateRequest(BaseModel):
    name: str = Field(default="Scheduled Job", min_length=1, max_length=200)
    subject: str = Field(..., description="jobs | weather | stocks | news | custom")
    params: dict[str, Any] = Field(default_factory=dict)
    schedule_type: str = Field(..., description="cron | interval")
    cron_expr: str | None = Field(default=None, description='e.g. "0 7 * * *" for 7am daily')
    interval_minutes: int | None = Field(default=None, ge=1, le=10080)  # max 1 week


class ScheduledJobUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    subject: str | None = None
    params: dict[str, Any] | None = None
    schedule_type: str | None = None
    cron_expr: str | None = None
    interval_minutes: int | None = Field(default=None, ge=1, le=10080)
    enabled: bool | None = None


JOB_SCHEDULER: AsyncIOScheduler | None = None


def _scheduler_timezone() -> ZoneInfo:
    tz_name = (os.getenv("SCHEDULER_TIMEZONE") or "America/Regina").strip()
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return ZoneInfo("America/Regina")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global JOB_SCHEDULER
    JOB_SCHEDULER = AsyncIOScheduler(timezone=_scheduler_timezone())
    JOB_SCHEDULER.start()
    await _reload_scheduled_jobs()
    yield
    if JOB_SCHEDULER:
        JOB_SCHEDULER.shutdown(wait=False)
        JOB_SCHEDULER = None


app = FastAPI(title="S15 NewArch API", version="0.1.0", lifespan=lifespan)

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

    async def list_scheduled_jobs(self, user_id: str) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        try:
            rows = await self._request(
                "GET",
                "/rest/v1/scheduled_jobs",
                params={
                    "select": "id,owner_user_id,owner_email,name,subject,params,schedule_type,cron_expr,interval_minutes,enabled,created_at,updated_at,last_run_id,last_run_at",
                    "owner_user_id": f"eq.{user_id}",
                    "order": "created_at.desc",
                },
            )
            return rows if isinstance(rows, list) else []
        except Exception:
            return []

    async def get_scheduled_job(self, job_id: str, user_id: str) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        try:
            rows = await self._request(
                "GET",
                "/rest/v1/scheduled_jobs",
                params={
                    "select": "*",
                    "id": f"eq.{job_id}",
                    "owner_user_id": f"eq.{user_id}",
                    "limit": "1",
                },
            )
            return rows[0] if isinstance(rows, list) and rows else None
        except Exception:
            return None

    async def upsert_scheduled_job(self, job: dict[str, Any]) -> None:
        if not self.enabled:
            return
        row: dict[str, Any] = {
            "id": job["id"],
            "owner_user_id": job["owner_user_id"],
            "owner_email": job.get("owner_email"),
            "name": job.get("name", "Scheduled Job"),
            "subject": job.get("subject", "custom"),
            "params": job.get("params", {}),
            "schedule_type": job.get("schedule_type", "cron"),
            "cron_expr": job.get("cron_expr"),
            "interval_minutes": job.get("interval_minutes"),
            "enabled": job.get("enabled", True),
            "updated_at": datetime.utcnow().isoformat(),
        }
        if job.get("last_run_id") is not None:
            row["last_run_id"] = job.get("last_run_id")
        if job.get("last_run_at") is not None:
            row["last_run_at"] = job.get("last_run_at")
        payload = [row]
        try:
            await self._request(
                "POST",
                "/rest/v1/scheduled_jobs",
                params={"on_conflict": "id"},
                payload=payload,
                prefer="resolution=merge-duplicates,return=minimal",
            )
        except Exception as exc:
            print(f"[scheduled_jobs] Supabase upsert failed: {exc}")

    async def delete_scheduled_job(self, job_id: str, user_id: str) -> bool:
        """Returns True only if Supabase reports at least one deleted row."""
        if not self.enabled:
            return False
        try:
            headers = dict(self._headers)
            headers["Prefer"] = "return=representation"
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.delete(
                    f"{self.url}/rest/v1/scheduled_jobs",
                    params={"id": f"eq.{job_id}", "owner_user_id": f"eq.{user_id}"},
                    headers=headers,
                )
                resp.raise_for_status()
                if not resp.content:
                    return False
                data = resp.json()
                return isinstance(data, list) and len(data) > 0
        except Exception as exc:
            print(f"[scheduled_jobs] Supabase delete: {exc}")
            return False

    async def list_all_enabled_scheduled_jobs(self) -> list[dict[str, Any]]:
        """All enabled jobs across users, for scheduler reload."""
        if not self.enabled:
            return []
        try:
            rows = await self._request(
                "GET",
                "/rest/v1/scheduled_jobs",
                params={
                    "select": "id,owner_user_id,owner_email,name,subject,params,schedule_type,cron_expr,interval_minutes,enabled",
                    "enabled": "eq.true",
                    "order": "created_at.desc",
                },
            )
            return rows if isinstance(rows, list) else []
        except Exception:
            return []

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


def _notes_dir() -> Path:
    path = BASE_DIR / "memory" / "notes"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _notes_path(user_id: str) -> Path:
    safe_user = user_id.replace("/", "_")
    return _notes_dir() / f"{safe_user}.json"


def _load_notes(user_id: str) -> list[dict[str, Any]]:
    path = _notes_path(user_id)
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8") as f:
            payload = json.load(f)
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
    except Exception:
        return []
    return []


def _save_notes(user_id: str, notes: list[dict[str, Any]]) -> None:
    path = _notes_path(user_id)
    with path.open("w", encoding="utf-8") as f:
        json.dump(notes, f, ensure_ascii=False, indent=2)


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


def _local_scheduled_jobs_path() -> Path:
    LOCAL_SCHEDULED_JOBS_DIR.mkdir(parents=True, exist_ok=True)
    return LOCAL_SCHEDULED_JOBS_DIR / "jobs.json"


def _load_all_local_scheduled_jobs() -> list[dict[str, Any]]:
    return [j for j in _load_local_scheduled_jobs_raw() if j.get("enabled", True)]


def _save_local_scheduled_jobs(jobs: list[dict[str, Any]]) -> None:
    path = _local_scheduled_jobs_path()
    with path.open("w", encoding="utf-8") as f:
        json.dump({"jobs": jobs}, f, ensure_ascii=False, indent=2)


def _load_local_scheduled_jobs_raw() -> list[dict[str, Any]]:
    """All jobs from local file (including disabled)."""
    path = _local_scheduled_jobs_path()
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        jobs = data.get("jobs", data) if isinstance(data, dict) else (data if isinstance(data, list) else [])
        return [j for j in jobs if isinstance(j, dict)]
    except Exception:
        return []


def _upsert_job_in_local_store(job: dict[str, Any]) -> None:
    """Always persist one job to memory/scheduled_jobs/jobs.json (survives missing Supabase table)."""
    jobs = _load_local_scheduled_jobs_raw()
    jid = job.get("id")
    if not jid:
        return
    idx = next((i for i, j in enumerate(jobs) if j.get("id") == jid), None)
    if idx is not None:
        jobs[idx] = dict(job)
    else:
        jobs.insert(0, dict(job))
    _save_local_scheduled_jobs(jobs)


def _owner_ids_match(stored: Any, current: str) -> bool:
    """JWT sub and JSON may differ only by case/spacing for UUIDs."""
    a = str(stored or "").strip().lower()
    b = str(current or "").strip().lower()
    return bool(a) and a == b


def _remove_job_from_local_store(job_id: str, owner_user_id: str) -> bool:
    jobs = _load_local_scheduled_jobs_raw()
    if not jobs:
        return False
    uid = str(owner_user_id)
    # Match job_id as string (path params are always str)
    new_jobs = [
        j
        for j in jobs
        if not (str(j.get("id")) == str(job_id) and _owner_ids_match(j.get("owner_user_id"), uid))
    ]
    if len(new_jobs) == len(jobs):
        return False
    _save_local_scheduled_jobs(new_jobs)
    return True


async def _list_scheduled_jobs_merged(user_id: str) -> list[dict[str, Any]]:
    """Supabase rows + local file, keyed by job id (local wins on conflict)."""
    uid = str(user_id)
    by_id: dict[str, dict[str, Any]] = {}
    if _use_supabase_store():
        try:
            for j in await RUN_STORE.list_scheduled_jobs(user_id):
                if isinstance(j, dict) and j.get("id"):
                    by_id[str(j["id"])] = dict(j)
        except Exception as exc:
            print(f"[scheduled_jobs] Supabase list failed, using local only: {exc}")
    for j in _load_local_scheduled_jobs_raw():
        if not _owner_ids_match(j.get("owner_user_id"), uid):
            continue
        if j.get("id"):
            by_id[str(j["id"])] = dict(j)
    return list(by_id.values())


async def _all_enabled_scheduled_jobs_merged() -> list[dict[str, Any]]:
    """For APScheduler: every enabled job from cloud + local (local wins on id clash)."""
    by_id: dict[str, dict[str, Any]] = {}
    if _use_supabase_store():
        try:
            for j in await RUN_STORE.list_all_enabled_scheduled_jobs():
                if isinstance(j, dict) and j.get("id"):
                    by_id[str(j["id"])] = dict(j)
        except Exception as exc:
            print(f"[scheduled_jobs] list_all_enabled failed: {exc}")
    for j in _load_all_local_scheduled_jobs():
        if j.get("id"):
            by_id[str(j["id"])] = dict(j)
    return list(by_id.values())


def _get_scheduled_job_from_local(job_id: str, owner_user_id: str) -> dict[str, Any] | None:
    for j in _load_local_scheduled_jobs_raw():
        if str(j.get("id")) == str(job_id) and _owner_ids_match(j.get("owner_user_id"), str(owner_user_id)):
            return dict(j)
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


def _extract_best_formatter_text(output: Any) -> str:
    if isinstance(output, str):
        text = output.strip()
        return text
    if not isinstance(output, dict):
        return ""

    preferred_keys = ["formatted_report", "formatted_output", "final_answer", "summary", "fallback_markdown"]
    for key in preferred_keys:
        value = output.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    for key, value in output.items():
        if not isinstance(key, str):
            continue
        if not isinstance(value, str):
            continue
        lower = key.lower()
        if lower.startswith("formatted_report") and value.strip():
            return value.strip()

    for _, value in output.items():
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _last_formatter_node(snapshot: dict[str, Any]) -> dict[str, Any] | None:
    nodes = snapshot.get("nodes", []) if isinstance(snapshot, dict) else []
    for node in reversed(nodes):
        if not isinstance(node, dict):
            continue
        agent = str(node.get("agent", "")).lower()
        if "formatter" in agent:
            return node
    return None


def _next_followup_step_ids(snapshot: dict[str, Any], count: int, prefix: str = "FUPR") -> list[str]:
    nodes = snapshot.get("nodes", []) if isinstance(snapshot, dict) else []
    existing = {str(node.get("id")) for node in nodes if isinstance(node, dict) and node.get("id")}
    ids: list[str] = []
    counter = 1
    while len(ids) < count:
        candidate = f"{prefix}{counter:03d}"
        if candidate not in existing and candidate not in ids:
            ids.append(candidate)
        counter += 1
    return ids


def _build_research_followup_query(
    *,
    base_query: str,
    followup_prompt: str,
    previous_answer: str,
) -> str:
    return (
        f"{base_query}\n\n"
        "---- Previous answer context ----\n"
        f"{previous_answer}\n\n"
        "---- Follow-up request ----\n"
        f"{followup_prompt}\n\n"
        "Use the follow-up request to do additional research and provide an improved answer. "
        "Do not merely rephrase; gather stronger evidence and deeper detail."
    )


def _coerce_output_value(output: Any, write_key: str) -> Any:
    if isinstance(output, dict):
        if write_key in output:
            return output[write_key]
        extracted = _extract_best_formatter_text(output)
        if extracted:
            return extracted
        return output
    if isinstance(output, str):
        return output.strip()
    return output


async def _run_research_followup_in_run(
    *,
    run_id: str,
    node_ids: dict[str, str],
    read_keys: dict[str, str],
    write_keys: dict[str, str],
    followup_query: str,
    owner_user_id: str | None,
) -> None:
    run = RUNS.get(run_id)
    if not run:
        return

    multi_mcp = MultiMCP()
    runner = AgentRunner(multi_mcp)
    try:
        await multi_mcp.start()
        run = RUNS.get(run_id)
        if not run:
            return
        snapshot = run.get("snapshot")
        if not isinstance(snapshot, dict):
            raise RuntimeError("Run snapshot unavailable for follow-up execution")
        nodes = snapshot.get("nodes", [])
        if not isinstance(nodes, list):
            raise RuntimeError("Invalid node list in run snapshot")
        globals_schema = snapshot.setdefault("globals_schema", {})
        if not isinstance(globals_schema, dict):
            raise RuntimeError("Invalid globals_schema in run snapshot")

        async def execute_step(
            step_id: str,
            agent: str,
            *,
            reads: list[str],
            writes: list[str],
            agent_prompt: str,
        ) -> dict[str, Any]:
            node_ref = next((n for n in nodes if isinstance(n, dict) and n.get("id") == step_id), None)
            if not isinstance(node_ref, dict):
                raise RuntimeError(f"Follow-up node not found: {step_id}")

            node_ref["status"] = "running"
            node_ref["start_time"] = datetime.utcnow().isoformat()
            run["status"] = "running"
            snapshot["status"] = "running"
            _publish_event(run_id, {"snapshot": snapshot}, event="run_update")

            inputs = {key: globals_schema.get(key) for key in reads if key in globals_schema}
            payload: dict[str, Any] = {
                "step_id": step_id,
                "agent_prompt": agent_prompt,
                "reads": reads,
                "writes": writes,
                "inputs": inputs,
            }
            if agent == "FormatterAgent":
                payload["all_globals_schema"] = dict(globals_schema)
                payload["original_query"] = snapshot.get("query", run.get("query", ""))
                payload["session_context"] = {
                    "session_id": snapshot.get("session_id", run.get("session_id")),
                    "created_at": snapshot.get("created_at", run.get("created_at")),
                    "file_manifest": [],
                }

            result = await runner.run_agent(agent, payload)
            if not result.get("success"):
                raise RuntimeError(str(result.get("error") or f"{agent} failed"))
            output = result.get("output", {}) or {}

            for write_key in writes:
                globals_schema[write_key] = _coerce_output_value(output, write_key)

            node_ref["status"] = "completed"
            node_ref["end_time"] = datetime.utcnow().isoformat()
            node_ref["output"] = output
            node_ref["error"] = None
            _publish_event(run_id, {"snapshot": snapshot}, event="run_update")
            return output if isinstance(output, dict) else {"output": output}

        planner_output = await execute_step(
            node_ids["planner"],
            "PlannerAgent",
            reads=[read_keys["previous_answer"], read_keys["followup_prompt"]],
            writes=[write_keys["planner"]],
            agent_prompt=(
                "Create a focused follow-up research plan for this user request. "
                "The plan should identify what facts to gather, what comparisons to make, and what answer structure to produce.\n\n"
                f"Follow-up context query:\n{followup_query}"
            ),
        )
        retriever_output = await execute_step(
            node_ids["retriever"],
            "RetrieverAgent",
            reads=[read_keys["previous_answer"], read_keys["followup_prompt"], write_keys["planner"]],
            writes=[write_keys["retriever"]],
            agent_prompt=(
                "Gather evidence and concrete data needed by the follow-up plan. "
                "Prioritize current, source-backed details."
            ),
        )
        thinker_output = await execute_step(
            node_ids["thinker"],
            "ThinkerAgent",
            reads=[write_keys["planner"], write_keys["retriever"]],
            writes=[write_keys["thinker"]],
            agent_prompt="Reason over retrieved evidence and produce clear conclusions, trade-offs, and recommendations.",
        )
        await execute_step(
            node_ids["distiller"],
            "DistillerAgent",
            reads=[write_keys["thinker"]],
            writes=[write_keys["distiller"]],
            agent_prompt="Distill follow-up analysis into concise, high-signal bullet points and structured themes.",
        )
        await execute_step(
            node_ids["qa"],
            "QAAgent",
            reads=[write_keys["distiller"], write_keys["retriever"]],
            writes=[write_keys["qa"]],
            agent_prompt=(
                "Validate follow-up answer quality for coverage, factual grounding, and clarity. "
                "Return verdict and concrete issues if any."
            ),
        )
        await execute_step(
            node_ids["formatter"],
            "FormatterAgent",
            reads=[write_keys["planner"], write_keys["retriever"], write_keys["thinker"], write_keys["distiller"], write_keys["qa"]],
            writes=[write_keys["formatter"]],
            agent_prompt=(
                "Produce a polished follow-up answer for the user. "
                "Be practical, detailed, and directly aligned with the follow-up request."
            ),
        )

        run["status"] = "completed"
        snapshot["status"] = "completed"
        run["error"] = None
        _publish_event(run_id, {"snapshot": snapshot}, event="run_update")
        _publish_event(run_id, {"status": "completed", "snapshot": snapshot}, event="run_complete")
        run_record = _build_run_record(run_id)
        if run_record and _use_supabase_store():
            await RUN_STORE.upsert_run(run_record)
        if _use_supabase_store():
            await RUN_STORE.insert_log(
                level="INFO",
                event_type="run_followup_completed",
                message="In-run research follow-up completed",
                owner_user_id=owner_user_id,
                run_id=run_id,
                payload={"node_ids": node_ids, "retriever_keys": list(retriever_output.keys()), "thinker_keys": list(thinker_output.keys()), "planner_keys": list(planner_output.keys())},
            )
    except Exception as exc:
        run = RUNS.get(run_id)
        if run and isinstance(run.get("snapshot"), dict):
            snapshot = run["snapshot"]
            nodes = snapshot.get("nodes", [])
            for sid in node_ids.values():
                node_ref = next((n for n in nodes if isinstance(n, dict) and n.get("id") == sid), None)
                if isinstance(node_ref, dict) and node_ref.get("status") in {"pending", "running"}:
                    node_ref["status"] = "failed"
                    node_ref["error"] = str(exc)
                    node_ref["end_time"] = datetime.utcnow().isoformat()
            run["status"] = "failed"
            run["error"] = str(exc)
            snapshot["status"] = "failed"
            snapshot["error"] = str(exc)
            _publish_event(run_id, {"snapshot": snapshot}, event="run_update")
            _publish_event(run_id, {"status": "failed", "error": str(exc)}, event="run_error")
        if _use_supabase_store():
            await RUN_STORE.insert_log(
                level="ERROR",
                event_type="run_followup_failed",
                message="In-run research follow-up failed",
                owner_user_id=owner_user_id,
                run_id=run_id,
                payload={"error": str(exc), "node_ids": node_ids},
            )
    finally:
        await multi_mcp.stop()


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


def _enrich_scheduled_job(job: dict[str, Any]) -> dict[str, Any]:
    out = dict(job)
    out["built_query"] = build_query(job.get("subject", "custom"), job.get("params") or {})
    return out


async def _persist_scheduled_job_last_run(job_id: str, owner_user_id: str, run_id: str) -> None:
    now_iso = datetime.utcnow().isoformat()
    uid = str(owner_user_id)
    jobs = _load_local_scheduled_jobs_raw()
    for i, j in enumerate(jobs):
        if j.get("id") == job_id and str(j.get("owner_user_id")) == uid:
            jobs[i] = {**j, "last_run_id": run_id, "last_run_at": now_iso}
            _save_local_scheduled_jobs(jobs)
            break
    if _use_supabase_store():
        job = await RUN_STORE.get_scheduled_job(job_id, owner_user_id)
        if job:
            job = dict(job)
            job["last_run_id"] = run_id
            job["last_run_at"] = now_iso
            try:
                await RUN_STORE.upsert_scheduled_job(job)
            except Exception as exc:
                print(f"[scheduled_jobs] persist last_run cloud failed: {exc}")


def _start_agent_run_for_scheduled_job(job: dict[str, Any]) -> tuple[str, str]:
    """Create RUNS entry and spawn _execute_run. Returns (run_id, query)."""
    job_id = job.get("id") or ""
    owner_user_id = job.get("owner_user_id") or ""
    owner_email = job.get("owner_email") or ""
    query = build_query(job.get("subject", "custom"), job.get("params") or {})
    if not query.strip():
        raise ValueError("empty query for scheduled job")
    run_id = f"run_{uuid4().hex[:10]}"
    RUNS[run_id] = {
        "query": query,
        "created_at": datetime.utcnow().isoformat(),
        "status": "queued",
        "summary": {},
        "snapshot": {
            "run_id": run_id,
            "query": query,
            "status": "queued",
            "nodes": [
                {
                    "id": "BOOTSTRAP_PLANNING",
                    "agent": "PlannerAgent",
                    "description": f"Scheduled: {job.get('name', job_id)}",
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
        "owner_user_id": owner_user_id,
        "owner_email": owner_email,
        "activity": [],
        "pending_clarification": None,
        "scheduled_job_id": job_id,
    }
    _save_local_run_record(run_id)
    asyncio.create_task(_execute_run(run_id, query, owner_user_id, owner_email))
    return run_id, query


async def _after_scheduled_run_started(
    job: dict[str, Any],
    run_id: str,
    query: str,
    *,
    event_type: str = "scheduled_job_fired",
    log_message: str = "Scheduled job executed",
) -> None:
    job_id = job.get("id") or ""
    owner_user_id = job.get("owner_user_id") or ""
    owner_email = job.get("owner_email") or ""
    if _use_supabase_store():
        await RUN_STORE.upsert_user(owner_user_id, owner_email)
    run_record = _build_run_record(run_id)
    if run_record and _use_supabase_store():
        await RUN_STORE.upsert_run(run_record)
    if _use_supabase_store():
        await RUN_STORE.insert_log(
            level="INFO",
            event_type=event_type,
            message=log_message,
            owner_user_id=owner_user_id,
            run_id=run_id,
            payload={"query": query, "scheduled_job_id": job_id},
        )
    await _persist_scheduled_job_last_run(job_id, owner_user_id, run_id)


async def _on_scheduled_job_fire(job_id: str) -> None:
    """Called by APScheduler when a scheduled job fires."""
    jobs = await _all_enabled_scheduled_jobs_merged()
    job = next((j for j in jobs if str(j.get("id")) == str(job_id)), None)
    if not job:
        print(f"[scheduler] job {job_id} not found, skipping")
        return
    try:
        run_id, query = _start_agent_run_for_scheduled_job(job)
    except ValueError as e:
        print(f"[scheduler] job {job_id}: {e}")
        return
    await _after_scheduled_run_started(job, run_id, query)
    print(f"[scheduler] job {job_id} started run {run_id}")


async def _reload_scheduled_jobs() -> None:
    """Load all enabled jobs and register with APScheduler."""
    if not JOB_SCHEDULER:
        return
    for j in JOB_SCHEDULER.get_jobs():
        try:
            j.remove()
        except Exception:
            pass
    jobs = await _all_enabled_scheduled_jobs_merged()
    for job in jobs:
        job_id = job.get("id")
        if not job_id:
            continue
        schedule_type = (job.get("schedule_type") or "cron").lower()
        try:
            if schedule_type == "interval":
                mins = job.get("interval_minutes") or 60
                JOB_SCHEDULER.add_job(
                    _on_scheduled_job_fire,
                    "interval",
                    minutes=mins,
                    id=job_id,
                    args=[job_id],
                    replace_existing=True,
                )
            else:
                cron = job.get("cron_expr") or "0 7 * * *"  # default 7am daily
                JOB_SCHEDULER.add_job(
                    _on_scheduled_job_fire,
                    "cron",
                    **{k: v for k, v in _cron_to_kwargs(cron).items() if v is not None},
                    id=job_id,
                    args=[job_id],
                    replace_existing=True,
                )
        except Exception as e:
            print(f"[scheduler] failed to add job {job_id}: {e}")


def _cron_to_kwargs(cron_expr: str) -> dict[str, Any]:
    """Convert cron 'min hour day month dow' to APScheduler cron trigger kwargs."""
    parts = (cron_expr or "0 7 * * *").strip().split()
    kwargs = {}
    if len(parts) > 0:
        kwargs["minute"] = int(parts[0]) if parts[0] != "*" else 0
    if len(parts) > 1 and parts[1] != "*":
        kwargs["hour"] = int(parts[1])
    if len(parts) > 2 and parts[2] != "*":
        try:
            kwargs["day"] = int(parts[2])
        except ValueError:
            kwargs["day"] = parts[2]
    if len(parts) > 3 and parts[3] != "*":
        try:
            kwargs["month"] = int(parts[3])
        except ValueError:
            kwargs["month"] = parts[3]
    if len(parts) > 4 and parts[4] != "*":
        kwargs["day_of_week"] = parts[4]
    return kwargs


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

        async def on_context_progress(context, _event: dict[str, Any]) -> None:
            run = RUNS.get(run_id)
            if not run:
                return
            snapshot = _context_to_run_detail(context, status=run.get("status", "running"))
            snapshot["run_id"] = run_id
            snapshot["session_id"] = run.get("session_id") or snapshot.get("run_id")
            _publish_event(run_id, {"snapshot": snapshot}, event="run_update")

            # Keep persisted run snapshots reasonably fresh without writing on every tick.
            now = time.time()
            last_persist_at = float(run.get("last_progress_persist_at") or 0.0)
            if now - last_persist_at < 1.0:
                return
            run["last_progress_persist_at"] = now
            run_record = _build_run_record(run_id)
            if run_record and _use_supabase_store():
                await RUN_STORE.upsert_run(run_record)

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
            progress_callback=on_context_progress,
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


@app.get("/api/notes")
async def list_notes(current_user: AuthUser = Depends(get_current_user)) -> list[dict[str, Any]]:
    notes = _load_notes(current_user.user_id)
    for note in notes:
        note["kind"] = (note.get("kind") or "file").strip().lower()
        note["name"] = (note.get("name") or "untitled.txt").strip() or "untitled.txt"
        if "parent_id" not in note:
            note["parent_id"] = None
        if note["kind"] == "folder":
            note["content"] = ""
        else:
            note["content"] = str(note.get("content") or "")
    notes.sort(key=lambda n: (n.get("updated_at") or n.get("created_at") or ""), reverse=True)
    return notes


@app.post("/api/notes")
async def create_note(
    request: NoteCreateRequest, current_user: AuthUser = Depends(get_current_user)
) -> dict[str, Any]:
    notes = _load_notes(current_user.user_id)
    kind = (request.kind or "file").strip().lower()
    if kind not in {"file", "folder"}:
        raise HTTPException(status_code=400, detail="Invalid note kind")
    name = (request.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Name is required")
    if kind == "file" and not name.lower().endswith(".md"):
        name = f"{name}.md"
    if request.parent_id:
        parent = next((n for n in notes if n.get("id") == request.parent_id), None)
        if not parent:
            raise HTTPException(status_code=400, detail="Parent folder not found")
        if (parent.get("kind") or "file").strip().lower() != "folder":
            raise HTTPException(status_code=400, detail="Parent must be a folder")
    now = datetime.utcnow().isoformat()
    note = {
        "id": f"note_{uuid4().hex[:10]}",
        "name": name,
        "kind": kind,
        "parent_id": request.parent_id,
        "content": "" if kind == "folder" else (request.content or ""),
        "created_at": now,
        "updated_at": now,
    }
    notes.insert(0, note)
    _save_notes(current_user.user_id, notes)
    return note


@app.put("/api/notes/{note_id}")
async def update_note(
    note_id: str, request: NoteUpdateRequest, current_user: AuthUser = Depends(get_current_user)
) -> dict[str, Any]:
    notes = _load_notes(current_user.user_id)
    for note in notes:
        if note.get("id") == note_id:
            if request.name is not None:
                name = request.name.strip()
                if not name:
                    raise HTTPException(status_code=400, detail="Name cannot be empty")
                note["name"] = name
            if request.kind is not None:
                next_kind = request.kind.strip().lower()
                if next_kind not in {"file", "folder"}:
                    raise HTTPException(status_code=400, detail="Invalid note kind")
                note["kind"] = next_kind
                if next_kind == "folder":
                    note["content"] = ""
            if request.parent_id is not None:
                if request.parent_id == note_id:
                    raise HTTPException(status_code=400, detail="Cannot set item as its own parent")
                parent = next((n for n in notes if n.get("id") == request.parent_id), None)
                if not parent:
                    raise HTTPException(status_code=400, detail="Parent folder not found")
                if (parent.get("kind") or "file").strip().lower() != "folder":
                    raise HTTPException(status_code=400, detail="Parent must be a folder")
                note["parent_id"] = request.parent_id
            if request.content is not None and (note.get("kind") or "file") != "folder":
                note["content"] = request.content
            note["updated_at"] = datetime.utcnow().isoformat()
            _save_notes(current_user.user_id, notes)
            return note
    raise HTTPException(status_code=404, detail="Note not found")


@app.delete("/api/notes/{note_id}")
async def delete_note(note_id: str, current_user: AuthUser = Depends(get_current_user)) -> dict[str, Any]:
    notes = _load_notes(current_user.user_id)
    note_ids = {n.get("id") for n in notes}
    if note_id not in note_ids:
        raise HTTPException(status_code=404, detail="Note not found")
    to_delete = {note_id}
    changed = True
    while changed:
        changed = False
        for n in notes:
            nid = n.get("id")
            if nid in to_delete:
                continue
            if n.get("parent_id") in to_delete:
                to_delete.add(nid)
                changed = True
    filtered = [n for n in notes if n.get("id") not in to_delete]
    _save_notes(current_user.user_id, filtered)
    return {"status": "deleted", "id": note_id, "deleted_count": len(to_delete)}


# ----- Scheduled Jobs API -----
@app.get("/api/scheduled-jobs")
async def list_scheduled_jobs(current_user: AuthUser = Depends(get_current_user)) -> list[dict[str, Any]]:
    jobs = await _list_scheduled_jobs_merged(current_user.user_id)
    jobs.sort(key=lambda j: j.get("created_at", ""), reverse=True)
    return [_enrich_scheduled_job(j) for j in jobs]


@app.post("/api/scheduled-jobs")
async def create_scheduled_job(
    request: ScheduledJobCreateRequest,
    current_user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    if (request.schedule_type or "").lower() == "cron" and not (request.cron_expr or "").strip():
        raise HTTPException(status_code=400, detail="cron_expr required for cron schedule")
    if (request.schedule_type or "").lower() == "interval" and not request.interval_minutes:
        raise HTTPException(status_code=400, detail="interval_minutes required for interval schedule")
    job_id = f"job_{uuid4().hex[:10]}"
    job = {
        "id": job_id,
        "owner_user_id": current_user.user_id,
        "owner_email": current_user.email,
        "name": (request.name or "Scheduled Job").strip(),
        "subject": (request.subject or "custom").strip().lower(),
        "params": request.params or {},
        "schedule_type": (request.schedule_type or "cron").strip().lower(),
        "cron_expr": (request.cron_expr or "").strip() or None,
        "interval_minutes": request.interval_minutes,
        "enabled": True,
        "created_at": datetime.utcnow().isoformat(),
        "updated_at": datetime.utcnow().isoformat(),
    }
    if _use_supabase_store():
        try:
            await RUN_STORE.upsert_scheduled_job(job)
        except Exception as exc:
            print(f"[scheduled_jobs] create cloud save failed (local still saved): {exc}")
    _upsert_job_in_local_store(job)
    await _reload_scheduled_jobs()
    return _enrich_scheduled_job(job)


@app.post("/api/scheduled-jobs/{job_id}/run-now")
async def run_scheduled_job_now(
    job_id: str,
    current_user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Start the agent run for this job immediately (same query as scheduled / built_query)."""
    job = None
    if _use_supabase_store():
        job = await RUN_STORE.get_scheduled_job(job_id, current_user.user_id)
    if not job:
        job = _get_scheduled_job_from_local(job_id, current_user.user_id)
    if not job:
        raise HTTPException(status_code=404, detail="Scheduled job not found")
    try:
        run_id, query = _start_agent_run_for_scheduled_job(job)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    await _after_scheduled_run_started(
        job,
        run_id,
        query,
        event_type="scheduled_job_run_now",
        log_message="Scheduled job run-now started",
    )
    job = dict(job)
    job["last_run_id"] = run_id
    job["last_run_at"] = datetime.utcnow().isoformat()
    return {"run_id": run_id, "query": query, "job": _enrich_scheduled_job(job)}


@app.get("/api/scheduled-jobs/{job_id}")
async def get_scheduled_job(
    job_id: str,
    current_user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    job = None
    if _use_supabase_store():
        job = await RUN_STORE.get_scheduled_job(job_id, current_user.user_id)
    if not job:
        job = _get_scheduled_job_from_local(job_id, current_user.user_id)
    if not job:
        raise HTTPException(status_code=404, detail="Scheduled job not found")
    return _enrich_scheduled_job(job)


@app.patch("/api/scheduled-jobs/{job_id}")
async def update_scheduled_job(
    job_id: str,
    request: ScheduledJobUpdateRequest,
    current_user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    job = None
    if _use_supabase_store():
        job = await RUN_STORE.get_scheduled_job(job_id, current_user.user_id)
    if not job:
        job = _get_scheduled_job_from_local(job_id, current_user.user_id)
    if not job:
        raise HTTPException(status_code=404, detail="Scheduled job not found")
    if request.name is not None:
        job["name"] = request.name.strip()
    if request.subject is not None:
        job["subject"] = request.subject.strip().lower()
    if request.params is not None:
        job["params"] = request.params
    if request.schedule_type is not None:
        job["schedule_type"] = request.schedule_type.strip().lower()
    if request.cron_expr is not None:
        job["cron_expr"] = request.cron_expr.strip() or None
    if request.interval_minutes is not None:
        job["interval_minutes"] = request.interval_minutes
    if request.enabled is not None:
        job["enabled"] = request.enabled
    job["updated_at"] = datetime.utcnow().isoformat()
    if _use_supabase_store():
        try:
            await RUN_STORE.upsert_scheduled_job(job)
        except Exception as exc:
            print(f"[scheduled_jobs] patch cloud save failed (local still saved): {exc}")
    _upsert_job_in_local_store(job)
    await _reload_scheduled_jobs()
    return _enrich_scheduled_job(job)


@app.delete("/api/scheduled-jobs/{job_id}")
async def delete_scheduled_job(
    job_id: str,
    current_user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    # Local file is source of truth for merged UI; remove first so OneDrive/local always updates.
    ok_local = _remove_job_from_local_store(job_id, current_user.user_id)
    if _use_supabase_store():
        try:
            await RUN_STORE.delete_scheduled_job(job_id, current_user.user_id)
        except Exception as exc:
            print(f"[scheduled_jobs] Supabase delete failed: {exc}")
    if not ok_local:
        merged = await _list_scheduled_jobs_merged(current_user.user_id)
        if not any(str(j.get("id")) == str(job_id) for j in merged):
            await _reload_scheduled_jobs()
            return {"status": "deleted", "id": job_id}
        raise HTTPException(status_code=404, detail="Scheduled job not found or access denied")
    await _reload_scheduled_jobs()
    return {"status": "deleted", "id": job_id}


# ----- Mail (Gmail) API - separate from Arc Reactor agents -----
def _gmail_available() -> bool:
    tok = BASE_DIR / "token.json"
    creds = BASE_DIR / "credentials.json"
    return creds.exists() and (tok.exists() or True)


@app.get("/api/mail/messages")
async def mail_list(
    max_results: int = 20,
    q: str = "",
    page_token: str | None = None,
    current_user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    """List Gmail threads (conversations) with pagination. Returns {threads, nextPageToken}."""
    if not _gmail_available():
        raise HTTPException(status_code=503, detail="Gmail not configured. Add credentials.json and run generate_gmail_token.py")
    try:
        from lib.gmail_api import get_gmail_service, list_threads
        service = get_gmail_service()
        threads, next_tok = await asyncio.to_thread(
            list_threads,
            service,
            max_results=min(max_results, 50),
            query=q,
            page_token=page_token,
        )
        return {"threads": threads, "nextPageToken": next_tok}
    except FileNotFoundError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/mail/messages/{message_id}")
async def mail_get(
    message_id: str,
    current_user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Get full email by ID."""
    if not _gmail_available():
        raise HTTPException(status_code=503, detail="Gmail not configured")
    try:
        from lib.gmail_api import get_gmail_service, get_message
        service = get_gmail_service()
        msg = await asyncio.to_thread(get_message, service, message_id)
        return msg
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/mail/send")
async def mail_send(
    req: MailSendRequest,
    current_user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Send a new email."""
    if not _gmail_available():
        raise HTTPException(status_code=503, detail="Gmail not configured")
    try:
        from lib.gmail_api import get_gmail_service, send_message
        service = get_gmail_service()
        sent = await asyncio.to_thread(send_message, service, to=req.to, subject=req.subject, body=req.body)
        return {"status": "sent", "id": sent.get("id")}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/mail/reply")
async def mail_reply(
    req: MailReplyRequest,
    current_user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Reply to an existing email."""
    if not _gmail_available():
        raise HTTPException(status_code=503, detail="Gmail not configured")
    try:
        from lib.gmail_api import get_gmail_service, reply_to_message
        service = get_gmail_service()
        sent = await asyncio.to_thread(reply_to_message, service, original_id=req.message_id, body=req.body)
        return {"status": "sent", "id": sent.get("id")}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/mail/draft-with-ai")
async def mail_draft_with_ai(
    req: MailDraftRequest,
    current_user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Generate an AI draft reply for an email. Returns { draft: string }."""
    if not _gmail_available():
        raise HTTPException(status_code=503, detail="Gmail not configured")
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise HTTPException(status_code=503, detail="GEMINI_API_KEY required for AI draft")
    try:
        from lib.gmail_api import get_gmail_service, get_message
        from google import genai
        service = get_gmail_service()
        msg = await asyncio.to_thread(get_message, service, req.message_id)
        from_addr = msg.get("from", "")
        subject = msg.get("subject", "")
        body = (msg.get("body") or "")[:8000]
        prompt = f"""You are helping reply to an email. Write a concise, professional reply.

From: {from_addr}
Subject: {subject}

Email body:
---
{body}
---

Write only the reply body (plain text, no subject/headers). Keep it brief and actionable. Sign off appropriately if it's to a client or colleague."""
        client = genai.Client(api_key=api_key)
        response = await asyncio.to_thread(
            lambda: client.models.generate_content(model="gemini-2.0-flash", contents=prompt)
        )
        draft = (getattr(response, "text", None) or "").strip()
        if not draft and response and getattr(response, "candidates", None):
            for c in response.candidates or []:
                if getattr(c, "content", None) and getattr(c.content, "parts", None):
                    for p in c.content.parts or []:
                        if getattr(p, "text", None):
                            draft = (p.text or "").strip()
                            break
                    if draft:
                        break
        return {"draft": draft}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


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


@app.post("/api/runs/{run_id}/formatter-followup")
async def submit_formatter_followup(
    run_id: str,
    request: FormatterFollowupRequest,
    current_user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    run = RUNS.get(run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not active")
    if not _run_belongs_to_user(run, current_user.user_id):
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")
    snapshot: dict[str, Any] | None = run.get("snapshot") if isinstance(run.get("snapshot"), dict) else None
    base_query = str(run.get("query") or "")

    if not snapshot:
        raise HTTPException(status_code=409, detail="Run snapshot unavailable for follow-up")

    prompt = request.prompt.strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="Follow-up prompt cannot be empty")

    parent_formatter = _last_formatter_node(snapshot)
    if not parent_formatter:
        raise HTTPException(status_code=409, detail="No formatter output available for follow-up")
    previous_answer = _extract_best_formatter_text(parent_formatter.get("output"))
    if not previous_answer:
        previous_answer = "Previous answer unavailable."

    if run and run.get("status") == "running":
        raise HTTPException(status_code=409, detail="Run is currently busy. Try again in a moment.")

    followup_query = _build_research_followup_query(
        base_query=base_query or str(snapshot.get("query") or ""),
        followup_prompt=prompt,
        previous_answer=previous_answer,
    )
    step_ids = _next_followup_step_ids(snapshot, 6, prefix="FUPR")
    planner_id, retriever_id, thinker_id, distiller_id, qa_id, formatter_id = step_ids

    nodes = snapshot.setdefault("nodes", [])
    links = snapshot.setdefault("links", [])
    globals_schema = snapshot.setdefault("globals_schema", {})
    if not isinstance(nodes, list) or not isinstance(links, list) or not isinstance(globals_schema, dict):
        raise HTTPException(status_code=500, detail="Corrupt run snapshot structure")

    read_previous_answer = f"followup_previous_answer_{planner_id}"
    read_followup_prompt = f"followup_prompt_{planner_id}"
    write_planner = f"followup_plan_{planner_id}"
    write_retriever = f"followup_retrieval_{retriever_id}"
    write_thinker = f"followup_reasoning_{thinker_id}"
    write_distiller = f"followup_distilled_{distiller_id}"
    write_qa = f"followup_qa_{qa_id}"
    write_formatter = f"followup_final_{formatter_id}"

    globals_schema[read_previous_answer] = previous_answer
    globals_schema[read_followup_prompt] = prompt

    new_nodes = [
        {
            "id": planner_id,
            "agent": "PlannerAgent",
            "description": "Follow-up planning for deeper research",
            "agent_prompt": "Plan the deeper follow-up research steps.",
            "reads": [read_previous_answer, read_followup_prompt],
            "writes": [write_planner],
            "status": "pending",
            "output": None,
            "error": None,
            "cost": 0.0,
            "start_time": None,
            "end_time": None,
            "execution_time": 0.0,
        },
        {
            "id": retriever_id,
            "agent": "RetrieverAgent",
            "description": "Retrieve follow-up evidence",
            "agent_prompt": "Collect follow-up evidence with sources.",
            "reads": [read_previous_answer, read_followup_prompt, write_planner],
            "writes": [write_retriever],
            "status": "pending",
            "output": None,
            "error": None,
            "cost": 0.0,
            "start_time": None,
            "end_time": None,
            "execution_time": 0.0,
        },
        {
            "id": thinker_id,
            "agent": "ThinkerAgent",
            "description": "Analyze follow-up evidence",
            "agent_prompt": "Analyze follow-up evidence and trade-offs.",
            "reads": [write_planner, write_retriever],
            "writes": [write_thinker],
            "status": "pending",
            "output": None,
            "error": None,
            "cost": 0.0,
            "start_time": None,
            "end_time": None,
            "execution_time": 0.0,
        },
        {
            "id": distiller_id,
            "agent": "DistillerAgent",
            "description": "Distill follow-up findings",
            "agent_prompt": "Distill follow-up findings into concise insights.",
            "reads": [write_thinker],
            "writes": [write_distiller],
            "status": "pending",
            "output": None,
            "error": None,
            "cost": 0.0,
            "start_time": None,
            "end_time": None,
            "execution_time": 0.0,
        },
        {
            "id": qa_id,
            "agent": "QAAgent",
            "description": "Validate follow-up quality",
            "agent_prompt": "Validate follow-up quality and identify issues.",
            "reads": [write_distiller, write_retriever],
            "writes": [write_qa],
            "status": "pending",
            "output": None,
            "error": None,
            "cost": 0.0,
            "start_time": None,
            "end_time": None,
            "execution_time": 0.0,
        },
        {
            "id": formatter_id,
            "agent": "FormatterAgent",
            "description": "Format final follow-up answer",
            "agent_prompt": "Generate final follow-up answer for the user.",
            "reads": [write_planner, write_retriever, write_thinker, write_distiller, write_qa],
            "writes": [write_formatter],
            "status": "pending",
            "output": None,
            "error": None,
            "cost": 0.0,
            "start_time": None,
            "end_time": None,
            "execution_time": 0.0,
        },
    ]
    nodes.extend(new_nodes)
    links.extend(
        [
            {"source": str(parent_formatter.get("id")), "target": planner_id},
            {"source": planner_id, "target": retriever_id},
            {"source": retriever_id, "target": thinker_id},
            {"source": thinker_id, "target": distiller_id},
            {"source": distiller_id, "target": qa_id},
            {"source": qa_id, "target": formatter_id},
        ]
    )

    if run:
        run["status"] = "running"
        run["error"] = None
        snapshot["status"] = "running"
        _publish_event(run_id, {"snapshot": snapshot}, event="run_update")
        run_record = _build_run_record(run_id)
        if run_record and _use_supabase_store():
            await RUN_STORE.upsert_run(run_record)

    if _use_supabase_store():
        await RUN_STORE.insert_log(
            level="INFO",
            event_type="run_followup_created",
            message="In-run research follow-up graph created",
            owner_user_id=current_user.user_id,
            run_id=run_id,
            payload={"followup_prompt": prompt, "node_ids": step_ids},
        )

    asyncio.create_task(
        _run_research_followup_in_run(
            run_id=run_id,
            node_ids={
                "planner": planner_id,
                "retriever": retriever_id,
                "thinker": thinker_id,
                "distiller": distiller_id,
                "qa": qa_id,
                "formatter": formatter_id,
            },
            read_keys={
                "previous_answer": read_previous_answer,
                "followup_prompt": read_followup_prompt,
            },
            write_keys={
                "planner": write_planner,
                "retriever": write_retriever,
                "thinker": write_thinker,
                "distiller": write_distiller,
                "qa": write_qa,
                "formatter": write_formatter,
            },
            followup_query=followup_query,
            owner_user_id=current_user.user_id,
        )
    )
    return {"status": "accepted", "run_id": run_id, "mode": "research_in_run", "added_nodes": step_ids}


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

