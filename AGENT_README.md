# Agent / developer onboarding — Arc Reactor (S8 Share)

**Audience:** Another AI agent or engineer who needs to understand **what the system does** and **where logic lives** without reading the whole repo.

**Companion doc:** The human-facing product README is [`README.md`](./README.md) (demos, Render/Supabase, full API list). **This file is the “navigation map” for code changes.**

**Filename:** This doc is **`AGENT_README.md`** (Markdown). There is no `AGENT_README.py` — hand this `.md` file to another agent as context.

---

## 1. Mental model (30 seconds)

1. User sends a **query** → FastAPI **`api_server.py`** creates a **run** and starts async execution.
2. **`AgentLoop4`** (`core/loop.py`) asks **`PlannerAgent`** for a **DAG** (nodes + edges + `reads`/`writes` keys).
3. **`ExecutionContextManager`** (`memory/context.py`) holds a **NetworkX** graph; **ready** nodes run (often in parallel).
4. Each node is an **agent** (see `config/agent_config.yaml`): model + prompt file + optional **MCP tools**.
5. Agent output can be **final**, or a **control** signal (`call_tool` / `call_self`) → loop continues until the node completes.
6. Completed outputs are written into **`globals_schema`** under the node’s **`writes`** keys so **downstream `reads`** get isolated inputs.
7. The UI gets **live updates** via **SSE** (`run_update` / `run_complete` / `run_error`) using a short-lived **stream ticket**.

**Typical pipeline (not fixed — planner decides):** plan → retrieve (tools) → think → distill → QA → format → summarize.

---

## 2. Where to start reading (ordered)

| Order | File | Why |
|------:|------|-----|
| 1 | `api_server.py` | All HTTP routes, `RUNS` state, SSE, clarification & formatter follow-up, **scheduled jobs**, persistence |
| 2 | `core/loop.py` | `AgentLoop4`, DAG scheduling, per-step execution, progress hooks |
| 3 | `memory/context.py` | Graph lifecycle, `get_ready_steps`, `mark_done`, extraction into `globals_schema` |
| 4 | `agents/base_agent.py` | `AgentRunner`: builds prompts, calls model, parses JSON, dispatches MCP tools |
| 5 | `frontend/src/App.jsx` | Run list, graph (ReactFlow), inspector (**Preview / Rendered / JSON / Web URLs**), **Scheduler** UI, Mail, Notepad, SSE + clarification / follow-up |
| — | `scheduler_service.py` | `build_query(subject, params)` → natural-language query for jobs / weather / stocks / news / custom |

Then drill into **`prompts/*.md`** and **`mcp_servers/multi_mcp.py`** when changing behavior vs. infrastructure.

---

## 3. Two ways to run the backend

| Entry | Purpose |
|-------|---------|
| **`api_server.py`** (uvicorn) | **Production path:** web UI, auth, SSE, persistence, clarification / follow-up APIs. |
| **`app.py`** | **CLI / dev:** calls `AgentLoop4.run(...)` directly without HTTP; useful for quick local tests. |

Do **not** assume `app.py` and `api_server.py` stay in perfect feature parity — **API server is source of truth** for web behavior.

---

## 4. HTTP flow (minimal trace)

```
POST /api/runs  →  creates RUNS[run_id], seeds snapshot, asyncio task _execute_run
GET  /api/runs/{id}  →  snapshot (+ pending_clarification, activity tail) for UI
POST /api/runs/{id}/stream-ticket  →  one-time ticket for SSE
GET  /api/runs/{id}/events?ticket=...  →  SSE stream (immediate snapshot, then events)
```

**Auth:** `auth.py` — JWT bearer (Supabase-style). Most `/api/*` routes require a logged-in user.

**Other notable routes:**

- `POST /api/runs/{id}/clarification` — user answers a pending clarification (see §6).
- `POST /api/runs/{id}/formatter-followup` — appends a **new subgraph** after the last formatter and resumes execution in-process (see §7).

**Scheduled jobs (Scheduler):**

- `GET /api/scheduled-jobs` — list current user’s jobs (each item includes computed **`built_query`**).
- `POST /api/scheduled-jobs` — create job (`name`, `subject`, `params`, `schedule_type`, `cron_expr` or `interval_minutes`).
- `GET /api/scheduled-jobs/{job_id}` — get one job.
- `PATCH /api/scheduled-jobs/{job_id}` — update fields / `enabled`.
- `DELETE /api/scheduled-jobs/{job_id}` — delete (local file + Supabase when enabled).
- `POST /api/scheduled-jobs/{job_id}/run-now` — start **`_execute_run`** immediately with that job’s built query; returns `{ run_id, query, job }`.

See **§14** for persistence, APScheduler, and UI behavior.

---

## 5. In-memory run state (`RUNS`)

`RUNS[run_id]` in `api_server.py` is the **live** source of truth while a run is active. Important keys:

- **`query`**, **`status`**, **`summary`**, **`session_id`**, **`owner_user_id`**
- **`snapshot`** — what the UI renders: `nodes`, `links`, `globals_schema`, `status`, …
- **`subscribers`** — `asyncio.Queue` set for SSE fan-out
- **`pending_clarification`** — when set, orchestration waits for user input (see §6)
- **`activity`** — short human-readable log lines for the inspector
- **`scheduled_job_id`** (optional) — set when the run was started by a scheduled job or **run-now** from the Scheduler.

Persistence: **local JSON** under `memory/local_runs/` when `LOCAL_RUN_STORE=1`, else **Supabase** when configured. Session graph files under `memory/session_summaries_index/` when `SAVE_LOCAL_SESSIONS` is on (see env table).

---

## 6. Clarification pause / resume

When the DAG needs user input, the backend sets **`pending_clarification`** on the run (and mirrors into `snapshot`). The execution path **waits** until:

- `POST /api/runs/{id}/clarification` fills `response` on that pending object, then the loop continues.

**Find the logic:** search `pending_clarification` in `api_server.py` and the wait/resume helpers around `_execute_run` / loop integration.

**UI:** `App.jsx` reads `runDetail.pending_clarification` and posts the user’s answer.

---

## 7. Formatter “research follow-up” (same run, extra nodes)

`POST /api/runs/{id}/formatter-followup`:

1. Requires run **not** `running`, valid **snapshot**, and a **prior FormatterAgent** node with extractable text.
2. Builds a **synthetic mini-DAG** (Planner → Retriever → Thinker → Distiller → QA → Formatter) with fresh ids (prefix `FUPR…`).
3. **Splices** nodes/links into the existing snapshot, wires an edge from the **parent formatter** to the new planner.
4. Pre-seeds `globals_schema` with the previous answer + follow-up prompt.
5. Spawns **`_run_research_followup_in_run`** (async task) to execute only that subgraph via **`AgentRunner`** / loop helpers.

**Intent:** User asks “tell me more” **without** starting a brand-new run; the graph grows in place.

---

## 8. `AgentLoop4` and the DAG (`core/loop.py`)

- Main scheduling loop: **`_execute_dag`** — repeatedly **`get_ready_steps()`**, mark running, **`asyncio.gather`** (or equivalent) for parallel steps.
- **Safety:** `DAG_MAX_ITERATIONS` env (default **500**) caps the outer while-loop to avoid infinite spin if the graph never settles.
- **Failure handling:** `_handle_failures` can attempt recovery paths before marking the run failed.
- **Retriever / QA behavior:** contains guards and retries around weak retrieval (discovery-before-extraction, quality gate + retry) — search for retriever / QA / quality in this file when debugging empty outputs.

Progress and snapshots are pushed back to `api_server` via callbacks/hooks (search `_emit_progress`, snapshot builders in `api_server.py`).

**Concurrency (multiple runs):** Each `POST /api/runs` (and each scheduler **run-now** / cron fire) uses **`asyncio.create_task(_execute_run(...))`**. Separate runs execute **concurrently** on the same event loop (not serialized). Inside **one** run, steps with satisfied dependencies are executed in parallel via **`asyncio.gather`** in `_execute_dag` (`core/loop.py`).

---

## 9. Context graph (`memory/context.py`)

- **`ExecutionContextManager`** wraps **NetworkX** `DiGraph`.
- **`reads` / `writes`** on each node declare **dataflow**; completion copies structured output into **`globals_schema[write_key]`**.
- **`mark_done` / `mark_failed` / `mark_running`** drive status transitions and persistence side effects.
- Autosave of session snapshots is gated by **`SAVE_LOCAL_SESSIONS`**.

If downstream nodes see empty inputs, verify: planner **`writes`** keys match retriever **`reads`**, and extraction didn’t fall back to empty payloads.

---

## 10. Agents and prompts

- **Registry:** `config/agent_config.yaml` — agent name → `prompt_file`, `model`, `mcp_servers`.
- **Prompts:** `prompts/<agent>.md` — edit here for tone/steps; keep JSON/output contracts stable or update parsers in `agents/base_agent.py` / `core/json_parser.py`.
- **Models:** `core/model_manager.py` — provider routing (e.g. Gemini).

---

## 11. MCP and tools

- **Router:** `mcp_servers/multi_mcp.py` — starts subprocess MCP servers per config, routes **`call_tool`** from agents.
- **Config:** `config/mcp_server_config.yaml` + **`mcp_servers/*.py`** (browser, rag, sandbox, etc.).
- **Web / search helpers:** `mcp_servers/tools/web_tools_async.py` (async HTTP; used by browser/web tooling paths).
- **Code execution:** `tools/sandbox.py` — constrained execution for `CoderAgent` / `call_self` flows.

When tools return odd shapes, **`multi_mcp`** may coerce types so **`AgentRunner`** and retriever gates get list/dict-safe data.

---

## 12. Frontend (`frontend/`)

- **Vite + React;** main surface: `src/App.jsx` (large file — search by feature: `EventSource`, `stream-ticket`, `clarification`, `formatter`, `scheduled-jobs`, `extractUrlsFromRun`, `formatterMarkdownFromRunData`).
- **Auth:** Supabase client + `LoginPage.jsx` / `HomePage.jsx`.
- **Graph:** ReactFlow for nodes/edges from `snapshot`.
- **Left rail:** **Agent Run** (default), **Notepad**, **Mail** (Gmail), **Scheduler** (clock icon).
- **Inspector (Agent Run):** tabs **Preview** / **Rendered** / **JSON** / **Web URLs**. Output markdown uses `renderNodeOutput(selectedNode)` + `ReactMarkdown` + `remarkGfm`.
- **Web URLs tab:** `extractUrlsFromRun(runDetail)` walks **`nodes[].output`** and **`globals_schema`** for strings matching `http(s)://…`, deduped and sorted; shown as external links.
- **Scheduler panel:** card grid; each card shows **`built_query`**, **Run now** (`POST …/run-now` + poll `GET /api/runs/{id}`), **Load last run** (uses `last_run_id`). Results markdown uses **`formatterMarkdownFromRunData`**, which calls **`renderNodeOutput`** on the Formatter node — same pipeline as Inspector Preview (HTML→Markdown via Turndown, table normalization, `objectToMarkdown` fallback).
- **`VITE_API_BASE_URL`:** point the browser at the API host (e.g. `http://127.0.0.1:8000`); if unset, relative `/api/…` hits the Vite dev server and breaks.

---

## 13. Environment variables (common)

| Variable | Effect |
|----------|--------|
| `LOCAL_RUN_STORE` | `1` → persist run list/detail to `memory/local_runs/*.json` |
| `SAVE_LOCAL_SESSIONS` | `0` / `false` → skip writing session graph files under `memory/session_summaries_index/` |
| `DAG_MAX_ITERATIONS` | Cap for DAG scheduler loop in `core/loop.py` (default 500) |
| `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` | Cloud persistence + auth alignment |
| `CORS_ORIGINS` | Comma-separated allowed origins (defaults include Vite `5173`) |
| `SCHEDULER_TIMEZONE` | IANA tz for APScheduler cron (default **`America/Regina`**) |
| `apscheduler` | Declared in `pyproject.toml`; scheduler starts in FastAPI **lifespan** in `api_server.py` |

See also `README.md` and `docs/RENDER_SUPABASE_LOCAL_FRONTEND_SETUP.md` for deployment.

---

## 14. Scheduled jobs — backend detail (`api_server.py` + `scheduler_service.py`)

- **Query text:** `scheduler_service.build_query(subject, params)` builds the string passed to **`AgentLoop4`** (subjects: `jobs`, `weather`, `stocks`, `news`, `custom`).
- **Persistence (dual):** Every create/update/delete touches **`memory/scheduled_jobs/jobs.json`** via **`_upsert_job_in_local_store`** / **`_remove_job_from_local_store`**. If **`_use_supabase_store()`** is true, Supabase table **`scheduled_jobs`** is also updated (see migrations).
- **List / APScheduler reload:** **`_list_scheduled_jobs_merged`** and **`_all_enabled_scheduled_jobs_merged`** merge **cloud + local** by job `id` (local wins on conflict). This avoids “empty list” when Supabase upsert fails but the job exists locally.
- **Owner matching:** **`_owner_ids_match`** normalizes UUID strings (case/spacing) for list/delete.
- **APScheduler:** **`AsyncIOScheduler`** in app **lifespan**; **`_reload_scheduled_jobs`** registers cron or interval jobs; firing calls **`_on_scheduled_job_fire`** → **`_start_agent_run_for_scheduled_job`** + **`_after_scheduled_run_started`** (persist **`last_run_id`** locally always; cloud when configured).
- **Supabase migrations:** `db/migrations/003_scheduled_jobs.sql` (table), `004_scheduled_jobs_last_run.sql` (`last_run_id`, `last_run_at`). Not required for pure local file mode if `LOCAL_RUN_STORE=1` disables cloud store (see `_use_supabase_store()`).

---

## 15. Tests and benchmarks

- **`test_*.py`** at repo root — agent isolation, planner acid, flow tests; run with your usual `pytest` / `uv run` workflow.
- **`benchmarks/research_smoke/`** — smoke runner for retrieval-style scenarios.

---

## 16. Safe extension points (summary)

| Goal | Likely touch points |
|------|---------------------|
| New agent type | `config/agent_config.yaml`, `prompts/new.md`, planner prompt to emit node `agent` name, optional MCP list |
| New tool | MCP server module + `mcp_server_config.yaml`, wire server name in agent config |
| Change DAG rules / retries | `core/loop.py`, `memory/context.py` |
| New API or event | `api_server.py`, then `App.jsx` if UI-facing |
| Stricter output parsing | `agents/base_agent.py`, `core/json_parser.py` |
| New scheduled subject / query template | `scheduler_service.py` + Scheduler form in `App.jsx` |
| Inspector output tabs | `App.jsx` (`viewMode`), `styles.css` |

---

## 17. Glossary

| Term | Meaning |
|------|---------|
| **DAG** | Directed acyclic graph of agent steps |
| **globals_schema** | Shared key/value bag for inter-node data (via declared reads/writes) |
| **Snapshot** | JSON-serializable graph + schema the UI and persistence use |
| **SSE** | Server-Sent Events for streaming run updates |
| **MCP** | Model Context Protocol tool servers |
| **built_query** | Server-computed agent prompt for a scheduled job (returned on list/create/get) |
| **run-now** | Immediate `POST /api/scheduled-jobs/{id}/run-now` without waiting for cron |

---

*Last aligned with repo layout: S8 Share (Scheduler, Web URLs tab, dual scheduled-job persistence). If something disagrees with code, trust the code and update this file.*
