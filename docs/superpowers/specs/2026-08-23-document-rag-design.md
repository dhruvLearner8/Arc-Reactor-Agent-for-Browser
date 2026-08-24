# Document RAG / Document Q&A — Design Spec

Date: 2026-08-23
Branch: `feature/document-rag`
Status: Approved for implementation planning

## 1. Problem

Arc Reactor's `RetrieverAgent` can research the open web but has no way to
answer questions grounded in a document the user brings themselves (a
contract, a report, personal notes, etc.). The codebase already contains a
partial, never-finished attempt at this (`mcp_servers/server_rag.py`): a
FAISS index, an Ollama-based embedder and chunker, and an MCP tool
(`search_stored_documents_rag`) wired onto `RetrieverAgent`/`CoderAgent`/
`SummarizerAgent`. None of it is reachable in practice — there is no upload
endpoint, so the index is never populated, and both the embedder and
chunker depend on a local Ollama server that does not exist in the Render
production deployment.

This spec replaces that dead code with a working, production-viable
document ingestion + RAG Q&A pipeline, isolated per user, integrated into
the existing Planner/DAG architecture as a new agent.

## 2. Goals / non-goals

**Goals (v1):**
- User uploads a PDF, TXT, or MD file; it becomes queryable via natural-
  language questions.
- Documents and their vector data are private to the uploading user.
- Works identically in local dev and on Render (no local-only services).
- Integrates with the existing Planner → DAG → Agent execution model,
  not a side-channel feature.
- Answers are grounded in retrieved chunks and cite the source filename.

**Explicitly out of scope for v1** (may become a fast-follow):
- DOCX / other office formats (PDF + TXT/MD only).
- Embedded image captioning inside PDFs.
- Hierarchical (multi-level) chunking, parent-chunk expansion.
- Hybrid BM25 + semantic retrieval, reciprocal rank fusion.
- Cross-encoder re-ranking.
- Query rewriting pass.
- Post-hoc faithfulness/hallucination scoring.
- A precision/recall evaluation harness with a golden test set.
- Retaining the original uploaded file after processing (re-upload to
  reprocess; no "download original" feature).
- Editing/re-processing an existing document in place.

These were considered (a fuller pipeline was sketched using OpenAI
embeddings, 3-level hierarchical chunking, hybrid retrieval + re-ranking,
and an eval harness) and deliberately deferred: no OpenAI key is
available, and the added complexity (new ML dependencies, multiple extra
LLM calls per query, a golden dataset to maintain) isn't justified for a
first version. The schema keeps a few cheap-to-add fields (`chunk_index`,
`section_title`, `page_number`) so a hierarchical/parent-child upgrade
later doesn't require another migration, but v1 only ever writes a single
flat chunk level.

## 3. Architecture overview

```
Upload:  UI → POST /api/documents → extract text → normalize → chunk
         → embed (Gemini) → store rows (Supabase pgvector) → status: ready

Query:   User query → Planner sees file_manifest (user's ready docs)
         → adds DocumentQAAgent step → tool call → route_tool_call()
         injects owner_user_id → pgvector similarity search (RPC,
         filtered by owner_user_id) → agent composes cited answer
```

Per-user isolation is enforced at the tool-execution layer, not trusted to
the LLM: `MultiMCP.route_tool_call()` currently has no concept of caller
identity. A `run_context` (containing `owner_user_id`) is threaded from
the authenticated request in `api_server.py`, through
`AgentLoop4.run()` → `AgentRunner` → `MultiMCP.route_tool_call()`, which
injects `owner_user_id` into the arguments of context-aware tools before
dispatch. The LLM-generated tool call never carries a user id itself.

## 4. Data model

New migration `db/migrations/007_user_documents.sql`:

```sql
create extension if not exists vector;

create table if not exists public.user_documents (
  id uuid primary key default gen_random_uuid(),
  owner_user_id uuid not null references auth.users (id) on delete cascade,
  filename text not null,
  file_type text not null check (file_type in ('pdf', 'txt', 'md')),
  status text not null default 'processing'
    check (status in ('processing', 'ready', 'failed')),
  chunk_count int not null default 0,
  error_message text,
  created_at timestamptz not null default now()
);

create index if not exists user_documents_owner_idx
  on public.user_documents (owner_user_id);

create table if not exists public.document_chunks (
  id uuid primary key default gen_random_uuid(),
  document_id uuid not null references public.user_documents (id) on delete cascade,
  owner_user_id uuid not null references auth.users (id) on delete cascade,
  chunk_index int not null,
  section_title text,
  page_number int,
  content text not null,
  embedding vector(768) not null,
  created_at timestamptz not null default now()
);

create index if not exists document_chunks_owner_idx
  on public.document_chunks (owner_user_id);

create index if not exists document_chunks_embedding_idx
  on public.document_chunks using hnsw (embedding vector_cosine_ops);

alter table public.user_documents enable row level security;
alter table public.document_chunks enable row level security;
-- No policies: backend uses service role only (same pattern as
-- user_notepad / user_gmail_credentials).

create or replace function public.match_document_chunks(
  query_embedding vector(768),
  match_owner uuid,
  match_count int default 5
)
returns table (
  chunk_id uuid,
  document_id uuid,
  filename text,
  section_title text,
  page_number int,
  content text,
  similarity float
)
language sql stable
as $$
  select
    c.id, c.document_id, d.filename, c.section_title, c.page_number,
    c.content,
    1 - (c.embedding <=> query_embedding) as similarity
  from public.document_chunks c
  join public.user_documents d on d.id = c.document_id
  where c.owner_user_id = match_owner
  order by c.embedding <=> query_embedding
  limit match_count
$$;
```

`match_document_chunks` is called via Supabase's PostgREST RPC endpoint
(`POST /rest/v1/rpc/match_document_chunks`), consistent with how
`SupabaseRunStore` already talks to Postgres (raw `httpx` REST calls, no
ORM).

Embedding dimension (768) assumes Gemini's `text-embedding-004`; confirmed
during implementation against the actual `google-genai` SDK response.

## 5. Backend components

**`lib/documents.py`** (new, pure functions, no LLM calls except the
explicit embedding call):
- `extract_text(filename, content: bytes) -> ExtractedDoc` — PDF via
  `pymupdf4llm.to_markdown(..., page_chunks=True)` (already a dependency)
  to get per-page markdown + page numbers; TXT/MD via UTF-8 decode.
- `normalize_text(text) -> str` — Unicode NFC normalization, strip
  zero-width spaces/BOM (keep zero-width joiners, for Indic-script
  correctness), unescape HTML entities, collapse repeated whitespace.
  Applied before chunking.
- `chunk_document(pages: list[PageText]) -> list[Chunk]` — deterministic,
  no LLM: split each page's markdown on headings, pack paragraphs into
  ~400-word chunks with ~60-word overlap; falls back to a plain sliding
  window if no headings are present. Each `Chunk` carries `chunk_index`,
  `section_title` (nearest preceding heading, if any), `page_number`
  (from the source page, PDF only).
- `embed_texts(texts: list[str]) -> list[list[float]]` — batched calls
  through the existing `ModelManager`'s `google-genai` client
  (`client.models.embed_content(model="text-embedding-004", ...)`),
  matching the SDK already used in `core/model_manager.py`.

**`SupabaseRunStore`** (extended, `api_server.py`) — new methods
following the class's existing `_request()` pattern:
`insert_document`, `update_document_status`, `list_documents`,
`delete_document`, `insert_document_chunks` (batched), and
`search_document_chunks` (calls the `match_document_chunks` RPC).

**`mcp_servers/server_documents.py`** (new) replaces
`mcp_servers/server_rag.py` (deleted — confirmed unused anywhere else
in the codebase). Exposes one MCP tool:
`search_user_documents(query: str) -> list[str]`. `owner_user_id` is not
part of its declared input schema; it's injected by `MultiMCP` per
§3.

`mcp_servers/multi_mcp.py`: swap the hardcoded `"rag"` entry in
`server_configs` for `"documents"`; `route_tool_call` gains a
`context: dict | None` parameter and, for tools flagged as
context-aware, merges `context["owner_user_id"]` into the call
arguments before dispatch.

`core/loop.py`: `AgentLoop4.run()` gains an `owner_user_id` parameter,
stored on the instance and passed down to `AgentRunner`, which passes it
to `multi_mcp.route_tool_call(...)` as `context`.

## 6. DocumentQAAgent + Planner integration

- New prompt `prompts/document_qa.md`: instructs the agent to call
  `search_user_documents(query)`, then answer strictly from the returned
  chunks, citing `[filename]` (and section/page when available) per
  claim, and to say explicitly when the excerpts don't contain the
  answer — same anti-hallucination framing as the reference pipeline,
  without a separate verification LLM pass.
- `config/agent_config.yaml`: add
  ```yaml
  DocumentQAAgent:
    prompt_file: "prompts/document_qa.md"
    model: "gemini"
    mcp_servers: ["documents"]
    description: "Answers questions using the user's uploaded documents via vector search."
  ```
  Remove `"rag"` from `RetrieverAgent`, `CoderAgent`, `SummarizerAgent`'s
  `mcp_servers` lists.
- `prompts/planner.md`: add `"DocumentQAAgent"` to the node `agent` enum
  and a routing bullet under the role-based abstraction section
  ("use DocumentQAAgent when the query should be answered from a file in
  `file_manifest`, not the open web").
- `api_server.py` run-creation path (`POST /api/runs`): replace the
  hardcoded `file_manifest=[]` with the current user's `ready` documents
  (`filename`, `file_type`) fetched via `SupabaseRunStore.list_documents`.
  This activates planner logic that already exists in `prompts/planner.md`
  but has never received real data.

## 7. API endpoints

All gated by `require_full_account` (guests don't get persistent cloud
features — consistent with existing Mail/Scheduler gating and the
login screen's own copy: *"Gmail, scheduler, and cloud sync need a
Google sign-in"*).

- `POST /api/documents` — multipart upload. Validates extension
  (`.pdf`/`.txt`/`.md`) and size (20MB cap). Inserts a `user_documents`
  row with `status=processing`, schedules ingestion as a
  `BackgroundTasks` job (same pattern as `_execute_run`), returns the row
  immediately.
- `GET /api/documents` — list current user's documents + status.
- `DELETE /api/documents/{id}` — deletes the document row; chunks cascade
  via FK.

Background ingestion job: extract → normalize → chunk → embed → insert
chunks → set `status=ready` (with `chunk_count`) or `status=failed` (with
`error_message`).

## 8. Frontend

New "Documents" panel in `frontend/src/`, structurally modeled on the
existing Notepad component (upload control, list with status badges,
delete). While any document is `processing`, the panel polls
`GET /api/documents` every few seconds (no new SSE channel — not
justified for this). Nav entry added alongside Notepad/Mail/Scheduler.

## 9. Error handling

- Size/extension validation rejected client-side and server-side.
- Per-document failure (bad PDF, no extractable text, embedding API
  error) sets that document's `status=failed` with a human-readable
  `error_message` — never fails or crashes an unrelated agent run.
- Scanned/image-only PDFs with no extractable text produce a clear
  "no extractable text found in this file" failure rather than silently
  indexing nothing.

## 10. Testing

- Unit tests for `lib/documents.py`: normalization edge cases (BOM,
  zero-width spaces, HTML entities), chunk boundaries/overlap, heading
  detection.
- Integration test for the ingestion background job against a real (or
  fixture) small PDF and a `.md` file, asserting rows land in
  `document_chunks` with correct `owner_user_id` scoping.
- Integration test for `search_document_chunks` isolation: seed chunks
  for two different `owner_user_id`s, assert a query for user A never
  returns user B's chunks.
- Manual end-to-end pass: upload a doc through the UI, ask a question in
  a run, confirm the Planner routes to `DocumentQAAgent` and the answer
  cites the uploaded filename.
