# Arc Reactor - Multi-Agent DAG Architecture

Arc Reactor is a production-oriented multi-agent system built around a directed acyclic graph (DAG) execution model.  
It combines a FastAPI backend, a React/ReactFlow frontend, MCP tool servers, and session-aware persistence for replayable run histories.

This README documents the **current architecture** , with emphasis on how planning, execution, streaming, and storage work together.

---
# Demo - 1

https://drive.google.com/file/d/1vt172Q0vzhBNi5NEmVYDxuZcMlvBef92/view?usp=sharing

# Demo - 2

https://youtu.be/wYQjWFYDFOg

## Table of Contents

- [What This System Does](#what-this-system-does)
- [Architecture Overview](#architecture-overview)
- [Execution Lifecycle (Step-by-Step)](#execution-lifecycle-step-by-step)
- [Core Data Contracts](#core-data-contracts)
- [Storage and Persistence Modes](#storage-and-persistence-modes)
- [Frontend Architecture](#frontend-architecture)
- [Authentication Model](#authentication-model)
- [API Endpoints](#api-endpoints)
- [Configuration](#configuration)
- [Run Locally](#run-locally)
- [Render + Supabase Setup](#render--supabase-setup)
- [Project Structure](#project-structure)
- [Troubleshooting](#troubleshooting)

---

## What This System Does

- Converts a user query into a DAG of agent tasks (Planner-first).
- Executes independent DAG nodes in parallel when dependencies are satisfied.
- Allows iterative tool usage (`call_tool`) and self-reasoning/code execution (`call_self`) per node.
- Streams live run snapshots to the UI via SSE.
- Persists run history and node outputs for replay/debug (local and/or Supabase, based on config).

---

## Architecture Overview

### High-level flow

1. User submits query (`POST /api/runs`).
2. Backend creates a run record and spawns async execution.
3. `PlannerAgent` produces a plan graph (`nodes` + `edges` + reads/writes contracts).
4. `ExecutionContextManager` materializes a NetworkX DAG.
5. Ready nodes execute via `AgentLoop4`, with tool/call-self loops as needed.
6. Node outputs are merged into `globals_schema` for downstream dependencies.
7. Snapshots are streamed to clients and persisted.
8. Run completes with summary, final snapshot, and status.

### Architecture flow diagram

```text
┌────────────────────────────────────────────────────────────────────────────┐
│                              Frontend (React)                              │
│                                                                            │
│  New Run Query Input  ->  POST /api/runs                                  │
│  Run Graph + Inspector  <-  SSE: run_update / run_complete / run_error    │
└────────────────────────────────────────────────────────────────────────────┘
                                   │
                                   ▼
┌────────────────────────────────────────────────────────────────────────────┐
│                         FastAPI Backend (api_server.py)                    │
│                                                                            │
│  1) Auth check (auth.py)                                                   │
│  2) Create run state + async _execute_run                                  │
│  3) Stream snapshots to UI                                                 │
│  4) Persist run/session history                                            │
└────────────────────────────────────────────────────────────────────────────┘
                                   │
                                   ▼
┌────────────────────────────────────────────────────────────────────────────┐
│                 Agent Orchestration (AgentLoop4 + NetworkX DAG)            │
│                                                                            │
│  PlannerAgent -> builds DAG (nodes + dependencies)                         │
│                                                                            │
│  Core execution path:                                                      │
│    Planner -> Retriever -> Thinker -> Distiller -> Formatter -> Summarizer│
│                                                                            │
│  Validation loop:                                                          │
│    if retrieved data is weak/missing -> Retriever retries with tools       │
│                                                                            │
│  Node loop behavior:                                                       │
│    - call_tool  -> query MCP tools                                         │
│    - call_self  -> self-iterate / run generated code                       │
└────────────────────────────────────────────────────────────────────────────┘
                                   │
                                   ▼
┌────────────────────────────────────────────────────────────────────────────┐
│                              Tool Layer (MCP)                              │
│                                                                            │
│  Browser tools | RAG/Search tools | Sandbox execution tools                │
│  (via MultiMCP router)                                                     │
└────────────────────────────────────────────────────────────────────────────┘
                                   │
                                   ▼
┌────────────────────────────────────────────────────────────────────────────┐
│                                Data Stores                                 │
│                                                                            │
│  memory/local_runs/                     (run index/history)                │
│  memory/session_summaries_index/YYYY/MM/DD/ (full DAG snapshots)           │
│  Supabase chat_runs/app_users/app_logs   (optional cloud persistence)      │
└────────────────────────────────────────────────────────────────────────────┘
```

Simple mental model: **Plan -> Retrieve (loop if needed) -> Think -> Distill -> Format -> Summarize**.

### Main backend components

| Component | File | Responsibility |
|---|---|---|
| API layer | `api_server.py` | Run creation/list/detail, stream tickets, SSE events, persistence routing |
| Orchestrator | `core/loop.py` | `AgentLoop4` bootstrap, planning, DAG scheduling, step execution loops |
| Context graph | `memory/context.py` | NetworkX DAG state, `mark_running/done/failed`, extraction, autosave |
| Agent runner | `agents/base_agent.py` | Prompt/model execution, MCP tool schema injection, JSON parsing/retry |
| Tool router | `mcp_servers/multi_mcp.py` | Starts MCP servers and routes tool calls |
| Sandbox execution | `tools/sandbox.py` | Executes generated code safely with constrained globals |
| Auth | `auth.py` | Bearer JWT verification (Supabase-compatible) |

### Tooling layer (MCP)

The system integrates multiple tool servers (for retrieval, browser/web tasks, and sandbox-like execution), with `MultiMCP` as the coordinator.

---

## Execution Lifecycle (Step-by-Step)

### 1) Run creation

- `POST /api/runs`:
  - validates auth user
  - creates `run_id`
  - initializes in-memory `RUNS[run_id]` state
  - persists initial run metadata (local/Supabase)
  - starts `_execute_run(...)` as background task

### 2) Bootstrap + planning

- `_execute_run(...)` initializes MCP and `AgentLoop4`.
- `AgentLoop4.run(...)` creates a bootstrap context and asks `PlannerAgent` for plan graph.
- Graph is converted to NetworkX in `ExecutionContextManager`.

### 3) DAG execution

- Scheduler repeatedly calls `get_ready_steps()`.
- Ready nodes become `running` and execute concurrently.
- Each node may:
  - return final output,
  - request `call_tool` (invoke MCP tool),
  - request `call_self` (iterate with new instruction / code result).

### 4) Output extraction and dependency propagation

- `mark_done(...)` writes outputs into `globals_schema` using declared `writes`.
- Downstream nodes read only declared dependencies (`reads`), preserving isolation.
- If extraction fails, fallback payloads are inserted so pipeline remains debuggable.

### 5) Live streaming

- UI requests a short-lived stream ticket (`/api/runs/{id}/stream-ticket`).
- UI opens `EventSource` on `/api/runs/{id}/events?ticket=...`.
- Backend emits `run_update`, `run_complete`, `run_error`.
- Latest snapshot is sent immediately on stream connect for fast hydration.

### 6) Completion/failure

- On success: status set to `completed`, summary persisted, completion event published.
- On failure/timeout: status set to `failed`, error persisted and streamed.

---

## Core Data Contracts

### Run record (conceptual)

`RUNS[run_id]` typically contains:

- `query`, `created_at`, `status`
- `summary`
- `snapshot` (latest graph state for UI)
- `session_id`
- `owner_user_id`, `owner_email`
- `subscribers` (SSE queues)
- optional `error`

### Snapshot contract (used by API/UI)

- `run_id`
- `query`
- `created_at`
- `status`
- `nodes[]` (each with id/agent/status/reads/writes/output metadata)
- `links[]` (`source`, `target`)
- `globals_schema` (cross-node data)
- `session_id` (when available)

### Node output contract (flexible)

Node `output` is model-dependent JSON and may include:

- content keys (`final_answer`, `formatted_report_*`, etc.)
- control keys (`call_tool`, `call_self`, tool params)
- telemetry (`cost`, token counts)
- execution results (`execution_result`, `execution_error`)

---

## Storage and Persistence Modes

### Local files

- Run index files: `memory/local_runs/run_*.json`
- Full session graph snapshots: `memory/session_summaries_index/YYYY/MM/DD/session_*.json`

### Supabase mode

- Uses `chat_runs`, `app_users`, and optional logs tables when enabled.

### Mode selection

- `LOCAL_RUN_STORE=1` -> prefer local run store.
- Supabase used when configured and local-run mode is disabled.
- Session-file persistence controlled via `SAVE_LOCAL_SESSIONS`.

---

## Frontend Architecture

Frontend lives in `frontend/` and is built with Vite + React.

### Key behavior

- Auth gate via Supabase session.
- Run list + run detail loading from backend.
- Live status updates through SSE ticket + EventSource.
- Graph visualization through ReactFlow.
- Inspector panel supports:
  - rendered markdown mode
  - raw JSON mode

### Important rendering note

Formatter outputs can contain both metadata fields and content fields.  
Rendered mode should select content keys (e.g., rich report fields) rather than format metadata values.

---

## Authentication Model

- Frontend obtains Supabase access token.
- Backend validates JWT (`Authorization: Bearer ...`) in `auth.py`.
- User-scoped access enforced for run history and run detail APIs.
- Admin-only logs endpoint guarded by configured allowlist.

---

## API Endpoints

### Health/Auth

- `GET /api/health`
- `GET /api/me`

### Runs

- `GET /api/runs` - list user runs (active + persisted)
- `GET /api/runs/{run_id}` - run snapshot/detail
- `POST /api/runs` - create run (`{ "query": "..." }`)

### Streaming

- `POST /api/runs/{run_id}/stream-ticket`
- `GET /api/runs/{run_id}/events?ticket=...` (SSE)

### Admin

- `GET /api/admin/logs` (admin users only)

---

## Configuration

### Core config files

- `config/agent_config.yaml` - agent definitions, prompts, model mapping
- `config/models.json` - model inventory and defaults
- `config/profiles.yaml` - profile-level behavior toggles
- `config/mcp_server_config.yaml` - MCP server configuration
- `prompts/*.md` - per-agent prompting strategy

### Common environment variables

- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`
- `SUPABASE_JWT_SECRET` (or JWKS-based verification path)
- `SUPABASE_JWT_ISSUER`
- `SUPABASE_JWT_AUDIENCE`
- `LOCAL_RUN_STORE`
- `SAVE_LOCAL_SESSIONS`
- `RUN_TIMEOUT_SEC`
- `CORS_ORIGINS`
- `ADMIN_USER_IDS`
- model/provider keys as needed (for example Gemini-related keys)
- frontend vars: `VITE_SUPABASE_URL`, `VITE_SUPABASE_ANON_KEY`, `VITE_API_BASE_URL`

---

## Run Locally

### 1) Backend

```bash
uv run python -m uvicorn api_server:app --host 127.0.0.1 --port 8000 --reload
```

### 2) Frontend

```bash
cd frontend
npm install
npm run dev
```

Open `http://localhost:5173`.

---

## Render + Supabase Setup

For a hybrid setup (local frontend + Render backend + Supabase persistence), follow:

- `docs/RENDER_SUPABASE_LOCAL_FRONTEND_SETUP.md`

---

## Project Structure

```text
S8 Share/
├── agents/                 # Agent runner and agent execution interfaces
├── config/                 # Agent, model, profile, MCP config
├── core/                   # Main DAG loop, model manager, utilities
├── db/migrations/          # Supabase schema migrations
├── frontend/               # React app (graph + inspector + auth)
├── memory/                 # Session graph + local run persistence
├── mcp_servers/            # MCP servers and tool implementations
├── prompts/                # Prompt templates per agent role
├── tools/                  # Sandbox and helper tooling
├── api_server.py           # FastAPI API + SSE + run orchestration entry
├── auth.py                 # JWT auth for API
└── app.py                  # CLI/dev entry path
```

---

## Troubleshooting

### `401/403` from API

- Verify frontend token exists and backend JWT settings match your Supabase project.
- Confirm `SUPABASE_URL` and JWT-related env vars are set correctly.

### Stream not connecting

- Ensure run is active (`queued`/`running`) before requesting stream ticket.
- Confirm frontend is calling backend host/port correctly (`VITE_API_BASE_URL` or proxy).

### Run history missing

- Check whether you are in local or Supabase persistence mode.
- Inspect `memory/local_runs/` and `memory/session_summaries_index/` when local persistence is enabled.

### Formatter output looks too short

- Inspect node `JSON` tab to verify which key contains full content.
- If rendered view shows only summary text, verify renderer key-selection prioritizes report content keys over metadata/fallback fields.
