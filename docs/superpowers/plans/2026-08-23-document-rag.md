# Document RAG / Document Q&A Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a signed-in user upload a PDF/TXT/MD document and ask questions answered from it, via a new `DocumentQAAgent` in the existing Planner/DAG system, isolated per user.

**Architecture:** Upload → extract (pymupdf4llm for PDF) → normalize → deterministic chunk → Gemini embed → store in Supabase pgvector, scoped by `owner_user_id`. Query time: Planner sees the user's ready documents in `file_manifest` and can route to `DocumentQAAgent`, which calls an MCP tool that vector-searches only that user's chunks — `owner_user_id` is injected server-side into the tool call, never supplied by the LLM.

**Tech Stack:** FastAPI, `google-genai` SDK (`gemini-embedding-001`), Supabase Postgres + pgvector (accessed via raw `httpx` REST/RPC calls, no ORM — matches the existing `SupabaseRunStore` convention), `pymupdf4llm` (already a dependency), MCP (`FastMCP`) tool server over stdio, React/Vite frontend.

**Spec:** `docs/superpowers/specs/2026-08-23-document-rag-design.md`

## Global Constraints

- No new Python or npm dependencies — everything needed (`google-genai`, `pymupdf4llm`, `httpx`, React, Vite) is already in `pyproject.toml` / `frontend/package.json`.
- No LLM calls anywhere in the ingestion pipeline except the one explicit embedding call (`lib/documents.py:embed_texts`) — chunking and text processing are deterministic.
- Embedding model is `gemini-embedding-001` with `output_dimensionality=768` (verified against the live API; `text-embedding-004` does **not** exist for this key/API version). Vectors are L2-normalized after truncation (Google's documented recommendation for non-default output dimensionality).
- Per-user isolation is enforced server-side (`MultiMCP.route_tool_call` injects `owner_user_id`), never trusted to LLM-generated tool-call arguments.
- This project has no pytest — all "tests" in this plan are standalone async scripts run directly with `uv run python <script>.py`, matching the existing `test_run.py`/`test_all_agents.py` convention. Delete each throwaway script after its task's steps confirm it passes; keep only script names explicitly listed as permanent later in this plan (there are none — all verification scripts here are scratch/one-off, run and discarded).
- Document upload/list/delete requires a full (non-guest) account (`require_full_account`), matching existing Mail/Scheduler gating.
- Original uploaded file bytes are **not** persisted — only extracted chunks. Re-upload to reprocess.

---

## File structure

**New files:**
- `db/migrations/007_user_documents.sql` — pgvector schema + RPC search function
- `lib/documents.py` — extraction, normalization, chunking, embedding, ingestion orchestration (pure + Gemini calls; no Supabase, no FastAPI)
- `lib/documents_store.py` — Supabase REST layer for `user_documents`/`document_chunks` (used by both `api_server.py` and the new MCP subprocess)
- `mcp_servers/server_documents.py` — MCP tool server exposing `search_user_documents`
- `prompts/document_qa.md` — DocumentQAAgent prompt

**Deleted files:**
- `mcp_servers/server_rag.py` (dead FAISS/Ollama code, confirmed unused elsewhere)
- `mcp_servers/faiss_index/` and `mcp_servers/documents/` (empty placeholder dirs for the old, never-populated pipeline)

**Modified files:**
- `mcp_servers/models.py` — replace `SearchDocumentsInput` with `SearchUserDocumentsInput`
- `mcp_servers/multi_mcp.py` — swap `"rag"` server config for `"documents"`; `route_tool_call` gains `context` injection
- `core/loop.py` — `AgentLoop4` threads `owner_user_id` down to the tool-call site
- `config/agent_config.yaml` — add `DocumentQAAgent`, remove `"rag"` from other agents
- `prompts/planner.md` — add `DocumentQAAgent` to the node enum + a routing rule
- `prompts/retriever.md` — drop the dead `search_stored_documents_rag` reference
- `api_server.py` — new `/api/documents` endpoints; `AgentLoop4(...)` gets `owner_user_id`; real `file_manifest` from ready documents
- `frontend/src/App.jsx` — nav entry + render branch for the new panel
- `frontend/src/styles.css` — layout rule for `documents-mode` + panel styles

**New frontend file:**
- `frontend/src/DocumentsPanel.jsx` — upload control, document list with status, delete

---

### Task 1: Database migration

**Files:**
- Create: `db/migrations/007_user_documents.sql`

**Interfaces:**
- Produces: tables `public.user_documents`, `public.document_chunks`; RPC function `public.match_document_chunks(query_embedding vector(768), match_owner uuid, match_count int)`.

- [ ] **Step 1: Write the migration file**

```sql
-- 007_user_documents.sql
-- Per-user document RAG: uploaded documents, their chunks + embeddings.

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
-- user_notepad / user_gmail_credentials in prior migrations).

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

- [ ] **Step 2: Commit**

Do not apply this migration yet — it's applied against the live Supabase project in Task 5, once the embedding dimension has been double-checked and the Supabase REST layer exists to test against.

```bash
git add db/migrations/007_user_documents.sql
git commit -m "Add migration for per-user document RAG (pgvector)"
```

---

### Task 2: `lib/documents.py` — text extraction and normalization

**Files:**
- Create: `lib/documents.py`
- Test: `/tmp/test_documents_extract.py` (scratch, delete after passing)

**Interfaces:**
- Produces: `PageText` (dataclass: `page_number: int | None`, `markdown: str`), `extract_text(filename: str, content: bytes) -> list[PageText]`, `normalize_text(text: str) -> str`.

- [ ] **Step 1: Write `lib/documents.py` with extraction + normalization**

```python
"""Document ingestion: extraction, normalization, chunking, embedding."""
from __future__ import annotations

import html
import os
import re
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent.parent / ".env")

SUPPORTED_EXTENSIONS = {".pdf", ".txt", ".md"}


@dataclass
class PageText:
    page_number: int | None
    markdown: str


def extract_text(filename: str, content: bytes) -> list[PageText]:
    """Extract markdown text per page (PDF) or as a single page (TXT/MD)."""
    ext = Path(filename).suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise ValueError(f"Unsupported file type: {ext}")

    if ext == ".pdf":
        return _extract_pdf(content)

    text = content.decode("utf-8", errors="replace")
    return [PageText(page_number=None, markdown=text)]


def _extract_pdf(content: bytes) -> list[PageText]:
    import pymupdf4llm

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(content)
        tmp_path = tmp.name

    try:
        pages = pymupdf4llm.to_markdown(tmp_path, page_chunks=True)
        return [
            PageText(page_number=p["metadata"]["page"], markdown=p["text"])
            for p in pages
        ]
    finally:
        os.unlink(tmp_path)


_ZERO_WIDTH_RE = re.compile(r"[​‎‏﻿]")  # keep U+200D (ZWJ)
_MULTI_SPACE_RE = re.compile(r"[ \t]+")
_MULTI_NEWLINE_RE = re.compile(r"\n{3,}")


def normalize_text(text: str) -> str:
    """Unicode-normalize and strip noise before chunking. No stopword removal,
    no lemmatization — the embedding model handles that internally."""
    text = unicodedata.normalize("NFC", text)
    text = html.unescape(text)
    text = _ZERO_WIDTH_RE.sub("", text)
    text = _MULTI_SPACE_RE.sub(" ", text)
    text = _MULTI_NEWLINE_RE.sub("\n\n", text)
    return text.strip()
```

- [ ] **Step 2: Write the test script**

```python
# /tmp/test_documents_extract.py
import sys
sys.path.insert(0, "/Users/dhruvpatel/Desktop/Projects 1/ArcReactor/Arc-Reactor-Agent-for-Browser")

from lib.documents import extract_text, normalize_text

# TXT extraction
pages = extract_text("notes.txt", b"Hello\tworld  with  spaces")
assert len(pages) == 1
assert pages[0].page_number is None
assert pages[0].markdown == "Hello\tworld  with  spaces"

# PDF extraction (build a tiny real PDF with PyMuPDF)
import fitz
doc = fitz.open()
p1 = doc.new_page()
p1.insert_text((72, 72), "# Heading One\n\nBody text page one.", fontsize=11)
p2 = doc.new_page()
p2.insert_text((72, 72), "Body text page two.", fontsize=11)
doc.save("/tmp/_doctest.pdf")

pages = extract_text("report.pdf", open("/tmp/_doctest.pdf", "rb").read())
assert len(pages) == 2, f"expected 2 pages, got {len(pages)}"
assert pages[0].page_number == 1
assert pages[1].page_number == 2
assert "Heading One" in pages[0].markdown

# Unsupported extension
try:
    extract_text("resume.docx", b"whatever")
    raise AssertionError("expected ValueError for .docx")
except ValueError:
    pass

# Normalization
raw = "Café​ résume﻿  with\t\tspaces\n\n\n\nand HTML &amp; entities"
normalized = normalize_text(raw)
assert "​" not in normalized
assert "﻿" not in normalized
assert "&amp;" not in normalized and "&" in normalized
assert "\n\n\n" not in normalized
assert "  " not in normalized.replace("\n\n", "")

print("ALL EXTRACT/NORMALIZE TESTS PASSED")
```

- [ ] **Step 3: Run it, verify it fails first (module doesn't exist yet if run before Step 1)**

Run: `cd "/Users/dhruvpatel/Desktop/Projects 1/ArcReactor/Arc-Reactor-Agent-for-Browser" && uv run python /tmp/test_documents_extract.py`

Expected (before Step 1 code exists): `ModuleNotFoundError: No module named 'lib.documents'` — confirms the test isn't vacuously passing.

- [ ] **Step 4: Run it again after Step 1's code is in place**

Run: `cd "/Users/dhruvpatel/Desktop/Projects 1/ArcReactor/Arc-Reactor-Agent-for-Browser" && uv run python /tmp/test_documents_extract.py`

Expected: `ALL EXTRACT/NORMALIZE TESTS PASSED`

- [ ] **Step 5: Delete the scratch test and commit**

```bash
rm /tmp/test_documents_extract.py /tmp/_doctest.pdf
cd "/Users/dhruvpatel/Desktop/Projects 1/ArcReactor/Arc-Reactor-Agent-for-Browser"
git add lib/documents.py
git commit -m "Add document text extraction and normalization"
```

---

### Task 3: `lib/documents.py` — deterministic chunking

**Files:**
- Modify: `lib/documents.py` (append)
- Test: `/tmp/test_documents_chunk.py` (scratch)

**Interfaces:**
- Consumes: `PageText` from Task 2.
- Produces: `Chunk` (dataclass: `chunk_index: int`, `content: str`, `section_title: str | None`, `page_number: int | None`), `chunk_document(pages: list[PageText], target_words: int = 400, overlap_words: int = 60) -> list[Chunk]`.

- [ ] **Step 1: Append chunking to `lib/documents.py`**

```python
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$", re.MULTILINE)


@dataclass
class Chunk:
    chunk_index: int
    content: str
    section_title: str | None
    page_number: int | None


def _split_by_headings(markdown: str) -> list[tuple[str | None, str]]:
    """Split markdown into (heading_text_or_None, body) sections."""
    matches = list(_HEADING_RE.finditer(markdown))
    if not matches:
        return [(None, markdown)]

    sections: list[tuple[str | None, str]] = []
    if matches[0].start() > 0:
        preamble = markdown[: matches[0].start()].strip()
        if preamble:
            sections.append((None, preamble))

    for i, m in enumerate(matches):
        heading_text = m.group(2).strip()
        body_start = m.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(markdown)
        body = markdown[body_start:body_end].strip()
        if body:
            sections.append((heading_text, body))
    return sections


def chunk_document(
    pages: list[PageText], target_words: int = 400, overlap_words: int = 60
) -> list[Chunk]:
    """Deterministic markdown-aware chunking: split on headings, then pack
    paragraphs into ~target_words chunks with overlap. No LLM call."""
    chunks: list[Chunk] = []
    chunk_index = 0

    for page in pages:
        for heading, body in _split_by_headings(page.markdown):
            words = body.split()
            if not words:
                continue
            start = 0
            while start < len(words):
                end = min(start + target_words, len(words))
                chunk_text = " ".join(words[start:end])
                chunks.append(
                    Chunk(
                        chunk_index=chunk_index,
                        content=chunk_text,
                        section_title=heading,
                        page_number=page.page_number,
                    )
                )
                chunk_index += 1
                if end == len(words):
                    break
                start = end - overlap_words

    return chunks
```

- [ ] **Step 2: Write the test script**

```python
# /tmp/test_documents_chunk.py
import sys
sys.path.insert(0, "/Users/dhruvpatel/Desktop/Projects 1/ArcReactor/Arc-Reactor-Agent-for-Browser")

from lib.documents import PageText, chunk_document

# No headings, short text -> single chunk
pages = [PageText(page_number=1, markdown="one two three four five")]
chunks = chunk_document(pages, target_words=400, overlap_words=60)
assert len(chunks) == 1
assert chunks[0].content == "one two three four five"
assert chunks[0].section_title is None
assert chunks[0].page_number == 1
assert chunks[0].chunk_index == 0

# Heading-aware splitting
markdown = "# Intro\n\n" + " ".join(["word"] * 10) + "\n\n# Details\n\n" + " ".join(["term"] * 10)
pages = [PageText(page_number=None, markdown=markdown)]
chunks = chunk_document(pages, target_words=400, overlap_words=60)
assert len(chunks) == 2
assert chunks[0].section_title == "Intro"
assert chunks[1].section_title == "Details"
assert "word" in chunks[0].content and "term" not in chunks[0].content

# Long section wraps into multiple overlapping chunks
long_body = " ".join(f"w{i}" for i in range(1000))
pages = [PageText(page_number=3, markdown=long_body)]
chunks = chunk_document(pages, target_words=400, overlap_words=60)
assert len(chunks) == 3, f"expected 3 chunks for 1000 words at 400/60, got {len(chunks)}"
assert all(c.page_number == 3 for c in chunks)
# overlap: last word of chunk 0 should reappear near start of chunk 1
assert chunks[0].content.split()[-1] in chunks[1].content.split()[:overlap := 60]
# chunk_index increments across the whole document
assert [c.chunk_index for c in chunks] == [0, 1, 2]

# Multi-page document: indices keep incrementing, page numbers preserved
pages = [
    PageText(page_number=1, markdown="alpha beta"),
    PageText(page_number=2, markdown="gamma delta"),
]
chunks = chunk_document(pages)
assert [c.page_number for c in chunks] == [1, 2]
assert [c.chunk_index for c in chunks] == [0, 1]

print("ALL CHUNK TESTS PASSED")
```

- [ ] **Step 3: Run it, expect failure before Step 1's code lands**

Run: `cd "/Users/dhruvpatel/Desktop/Projects 1/ArcReactor/Arc-Reactor-Agent-for-Browser" && uv run python /tmp/test_documents_chunk.py`

Expected: `ImportError: cannot import name 'chunk_document'`

- [ ] **Step 4: Run it again after Step 1**

Run: same command.

Expected: `ALL CHUNK TESTS PASSED`

- [ ] **Step 5: Delete scratch test and commit**

```bash
rm /tmp/test_documents_chunk.py
cd "/Users/dhruvpatel/Desktop/Projects 1/ArcReactor/Arc-Reactor-Agent-for-Browser"
git add lib/documents.py
git commit -m "Add deterministic heading-aware document chunking"
```

---

### Task 4: `lib/documents.py` — Gemini embeddings

**Files:**
- Modify: `lib/documents.py` (append)
- Test: `/tmp/test_documents_embed.py` (scratch; needs network + `GEMINI_API_KEY`)

**Interfaces:**
- Produces: `embed_texts(texts: list[str]) -> list[list[float]]` — each inner list has length 768.

- [ ] **Step 1: Append embedding function to `lib/documents.py`**

```python
EMBEDDING_MODEL = "gemini-embedding-001"
EMBEDDING_DIM = 768


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Batch-embed texts via Gemini. Vectors are L2-normalized (required when
    using a non-default output_dimensionality, per Gemini API docs)."""
    if not texts:
        return []

    import numpy as np
    from google import genai
    from google.genai import types

    api_key = (os.getenv("GEMINI_API_KEY") or "").strip()
    client = genai.Client(api_key=api_key)

    response = client.models.embed_content(
        model=EMBEDDING_MODEL,
        contents=texts,
        config=types.EmbedContentConfig(output_dimensionality=EMBEDDING_DIM),
    )

    result = []
    for embedding in response.embeddings:
        vec = np.array(embedding.values, dtype=float)
        norm = np.linalg.norm(vec)
        if norm > 0:
            vec = vec / norm
        result.append(vec.tolist())
    return result
```

- [ ] **Step 2: Write the test script**

```python
# /tmp/test_documents_embed.py
import sys
sys.path.insert(0, "/Users/dhruvpatel/Desktop/Projects 1/ArcReactor/Arc-Reactor-Agent-for-Browser")

from lib.documents import embed_texts, EMBEDDING_DIM
import math

# Empty input
assert embed_texts([]) == []

# Real call against the live Gemini API
vecs = embed_texts(["revenue grew 12% year over year", "unrelated: a recipe for bread"])
assert len(vecs) == 2
for v in vecs:
    assert len(v) == EMBEDDING_DIM, f"expected {EMBEDDING_DIM}-dim, got {len(v)}"
    norm = math.sqrt(sum(x * x for x in v))
    assert abs(norm - 1.0) < 1e-3, f"expected unit-normalized vector, got norm={norm}"

# Similar sentences should be closer than dissimilar ones (basic sanity check)
def cosine(a, b):
    return sum(x * y for x, y in zip(a, b))  # unit vectors -> dot product = cosine

v_revenue_1 = embed_texts(["quarterly revenue increased"])[0]
v_revenue_2 = embed_texts(["sales grew this quarter"])[0]
v_unrelated = embed_texts(["a cat sleeping on a windowsill"])[0]
assert cosine(v_revenue_1, v_revenue_2) > cosine(v_revenue_1, v_unrelated)

print("ALL EMBED TESTS PASSED")
```

- [ ] **Step 3: Run it, expect failure before Step 1's code lands**

Run: `cd "/Users/dhruvpatel/Desktop/Projects 1/ArcReactor/Arc-Reactor-Agent-for-Browser" && uv run python /tmp/test_documents_embed.py`

Expected: `ImportError: cannot import name 'embed_texts'`

- [ ] **Step 4: Run it again after Step 1**

Run: same command.

Expected: `ALL EMBED TESTS PASSED`. If it fails with a 404 on the model name, list available models the same way this plan's authoring did (`client.models.list()`, filter for `embedContent` in `supported_actions`) and update `EMBEDDING_MODEL` accordingly — `gemini-embedding-001` was confirmed available at plan-authoring time but re-verify against the live key being used.

- [ ] **Step 5: Delete scratch test and commit**

```bash
rm /tmp/test_documents_embed.py
cd "/Users/dhruvpatel/Desktop/Projects 1/ArcReactor/Arc-Reactor-Agent-for-Browser"
git add lib/documents.py
git commit -m "Add Gemini embedding generation for document chunks"
```

---

### Task 5: `lib/documents_store.py` — Supabase REST layer + apply migration

**Files:**
- Create: `lib/documents_store.py`
- Test: `/tmp/test_documents_store.py` (scratch; needs live Supabase)

**Interfaces:**
- Produces: `ENABLED: bool`, `insert_document(document_id, owner_user_id, filename, file_type) -> None`, `update_document_status(document_id, status, *, chunk_count=0, error_message=None) -> None`, `list_documents(owner_user_id, *, status=None) -> list[dict]`, `delete_document(document_id, owner_user_id) -> bool`, `insert_document_chunks(rows: list[dict]) -> None`, `search_document_chunks(owner_user_id, query_embedding, match_count=5) -> list[dict]`.
- This module is imported by both `api_server.py` (Task 12) and `mcp_servers/server_documents.py` (Task 7) — kept free of any FastAPI or MCP dependency so both can import it cleanly.

- [ ] **Step 1: Apply the migration to Supabase**

Open the Supabase SQL editor for this project (`SUPABASE_URL` in `.env` — `https://vuxpqhkjyzbgjnjdrrjc.supabase.co`) and run the full contents of `db/migrations/007_user_documents.sql` (written in Task 1). Confirm no errors, and that `pgvector` extension activation succeeded (Supabase has it available by default).

Verify tables exist:

```bash
cd "/Users/dhruvpatel/Desktop/Projects 1/ArcReactor/Arc-Reactor-Agent-for-Browser"
source .env 2>/dev/null || export $(grep -v '^#' .env | xargs)
curl -s "${SUPABASE_URL}/rest/v1/user_documents?select=id&limit=1" \
  -H "apikey: ${SUPABASE_SERVICE_ROLE_KEY}" -H "Authorization: Bearer ${SUPABASE_SERVICE_ROLE_KEY}"
```

Expected: `[]` (empty array, not a 404/error — confirms the table exists and is reachable).

- [ ] **Step 2: Write `lib/documents_store.py`**

```python
"""Supabase REST layer for user_documents / document_chunks. Plain async
functions (not a class) so both api_server.py and the MCP tool subprocess
(server_documents.py) can import this without pulling in FastAPI."""
import os
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent.parent / ".env")

SUPABASE_URL = (os.getenv("SUPABASE_URL") or "").rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = (os.getenv("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
ENABLED = bool(SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY)


def _headers() -> dict[str, str]:
    return {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
    }


async def _request(
    method: str,
    path: str,
    *,
    params: dict[str, str] | None = None,
    payload: Any = None,
    prefer: str | None = None,
) -> Any:
    headers = _headers()
    if prefer:
        headers["Prefer"] = prefer
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.request(
            method, f"{SUPABASE_URL}{path}", params=params, json=payload, headers=headers
        )
        resp.raise_for_status()
        if not resp.content:
            return None
        if "application/json" in resp.headers.get("content-type", ""):
            return resp.json()
        return None


async def insert_document(document_id: str, owner_user_id: str, filename: str, file_type: str) -> None:
    await _request(
        "POST",
        "/rest/v1/user_documents",
        payload=[{
            "id": document_id,
            "owner_user_id": owner_user_id,
            "filename": filename,
            "file_type": file_type,
            "status": "processing",
        }],
        prefer="return=minimal",
    )


async def update_document_status(
    document_id: str, status: str, *, chunk_count: int = 0, error_message: str | None = None
) -> None:
    payload: dict[str, Any] = {"status": status, "chunk_count": chunk_count}
    if error_message is not None:
        payload["error_message"] = error_message
    await _request(
        "PATCH",
        "/rest/v1/user_documents",
        params={"id": f"eq.{document_id}"},
        payload=payload,
        prefer="return=minimal",
    )


async def list_documents(owner_user_id: str, *, status: str | None = None) -> list[dict]:
    params = {
        "select": "id,filename,file_type,status,chunk_count,error_message,created_at",
        "owner_user_id": f"eq.{owner_user_id}",
        "order": "created_at.desc",
    }
    if status:
        params["status"] = f"eq.{status}"
    rows = await _request("GET", "/rest/v1/user_documents", params=params)
    return rows if isinstance(rows, list) else []


async def delete_document(document_id: str, owner_user_id: str) -> bool:
    """Returns True only if a row was actually deleted."""
    headers = _headers()
    headers["Prefer"] = "return=representation"
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.delete(
            f"{SUPABASE_URL}/rest/v1/user_documents",
            params={"id": f"eq.{document_id}", "owner_user_id": f"eq.{owner_user_id}"},
            headers=headers,
        )
        resp.raise_for_status()
        data = resp.json() if resp.content else []
        return isinstance(data, list) and len(data) > 0


async def insert_document_chunks(rows: list[dict]) -> None:
    if not rows:
        return
    await _request("POST", "/rest/v1/document_chunks", payload=rows, prefer="return=minimal")


async def search_document_chunks(
    owner_user_id: str, query_embedding: list[float], match_count: int = 5
) -> list[dict]:
    rows = await _request(
        "POST",
        "/rest/v1/rpc/match_document_chunks",
        payload={
            "query_embedding": query_embedding,
            "match_owner": owner_user_id,
            "match_count": match_count,
        },
    )
    return rows if isinstance(rows, list) else []
```

- [ ] **Step 3: Write the test script**

This test needs a real `auth.users` row to satisfy the foreign key. It reuses the admin user id already present in `.env` (`ADMIN_USER_IDS`), which is a real Supabase auth user in this project.

```python
# /tmp/test_documents_store.py
import asyncio
import os
import sys
import uuid
sys.path.insert(0, "/Users/dhruvpatel/Desktop/Projects 1/ArcReactor/Arc-Reactor-Agent-for-Browser")

from lib import documents_store as store

TEST_USER_ID = (os.getenv("ADMIN_USER_IDS") or "").split(",")[0].strip()
assert TEST_USER_ID, "Set ADMIN_USER_IDS in .env to a real auth.users id to run this test"


async def main():
    assert store.ENABLED, "Supabase not configured in .env"

    doc_id = str(uuid.uuid4())
    await store.insert_document(doc_id, TEST_USER_ID, "test_doc.txt", "txt")

    docs = await store.list_documents(TEST_USER_ID, status="processing")
    assert any(d["id"] == doc_id for d in docs), "inserted document not found in list"

    # Insert chunks with a small real embedding (via lib.documents.embed_texts
    # would need network; here we synthesize a deterministic 768-dim vector to
    # test only the store layer).
    fake_vec = [0.001 * (i % 10) for i in range(768)]
    await store.insert_document_chunks([
        {
            "document_id": doc_id,
            "owner_user_id": TEST_USER_ID,
            "chunk_index": 0,
            "section_title": None,
            "page_number": None,
            "content": "hello world test chunk",
            "embedding": fake_vec,
        }
    ])

    await store.update_document_status(doc_id, "ready", chunk_count=1)
    docs = await store.list_documents(TEST_USER_ID, status="ready")
    match = next((d for d in docs if d["id"] == doc_id), None)
    assert match is not None
    assert match["chunk_count"] == 1

    results = await store.search_document_chunks(TEST_USER_ID, fake_vec, match_count=3)
    assert any(r["content"] == "hello world test chunk" for r in results), results

    deleted = await store.delete_document(doc_id, TEST_USER_ID)
    assert deleted is True

    docs = await store.list_documents(TEST_USER_ID)
    assert not any(d["id"] == doc_id for d in docs), "document should be gone after delete"

    print("ALL DOCUMENTS_STORE TESTS PASSED")


asyncio.run(main())
```

- [ ] **Step 4: Run it, expect failure before Step 2's code lands**

Run: `cd "/Users/dhruvpatel/Desktop/Projects 1/ArcReactor/Arc-Reactor-Agent-for-Browser" && uv run python /tmp/test_documents_store.py`

Expected: `ModuleNotFoundError: No module named 'lib.documents_store'`

- [ ] **Step 5: Run it again after Step 2**

Run: same command.

Expected: `ALL DOCUMENTS_STORE TESTS PASSED`. If the RPC call 404s, confirm the `match_document_chunks` function was actually created (re-check Step 1's SQL editor run for errors on that specific statement).

- [ ] **Step 6: Delete scratch test and commit**

```bash
rm /tmp/test_documents_store.py
cd "/Users/dhruvpatel/Desktop/Projects 1/ArcReactor/Arc-Reactor-Agent-for-Browser"
git add lib/documents_store.py
git commit -m "Add Supabase REST layer for per-user document storage"
```

---

### Task 6: `lib/documents.py` — ingestion orchestration

**Files:**
- Modify: `lib/documents.py` (append)
- Test: `/tmp/test_documents_ingest.py` (scratch; needs network + live Supabase)

**Interfaces:**
- Consumes: `extract_text`, `normalize_text`, `chunk_document`, `embed_texts` (this file); `documents_store.insert_document_chunks`, `documents_store.update_document_status` (Task 5).
- Produces: `async def ingest_document(document_id: str, owner_user_id: str, filename: str, content: bytes) -> None`. Never raises — on any failure it calls `update_document_status(..., status="failed", error_message=...)` and returns.

- [ ] **Step 1: Append orchestration to `lib/documents.py`**

```python
async def ingest_document(document_id: str, owner_user_id: str, filename: str, content: bytes) -> None:
    """Full pipeline: extract -> normalize -> chunk -> embed -> store.
    Never raises; failures are recorded on the document row itself so a bad
    upload never crashes the caller (matches spec section 9)."""
    from lib import documents_store as store

    try:
        pages = extract_text(filename, content)
        pages = [PageText(page_number=p.page_number, markdown=normalize_text(p.markdown)) for p in pages]

        chunks = chunk_document(pages)
        if not chunks:
            await store.update_document_status(
                document_id, "failed", error_message="No extractable text found in this file."
            )
            return

        texts = [c.content for c in chunks]
        vectors = embed_texts(texts)

        rows = [
            {
                "document_id": document_id,
                "owner_user_id": owner_user_id,
                "chunk_index": c.chunk_index,
                "section_title": c.section_title,
                "page_number": c.page_number,
                "content": c.content,
                "embedding": vec,
            }
            for c, vec in zip(chunks, vectors)
        ]
        await store.insert_document_chunks(rows)
        await store.update_document_status(document_id, "ready", chunk_count=len(rows))

    except Exception as exc:  # noqa: BLE001 - deliberately broad: this must never propagate
        from lib import documents_store as store  # re-import in case the first import site failed
        await store.update_document_status(document_id, "failed", error_message=str(exc)[:500])
```

- [ ] **Step 2: Write the test script**

```python
# /tmp/test_documents_ingest.py
import asyncio
import os
import sys
import uuid
sys.path.insert(0, "/Users/dhruvpatel/Desktop/Projects 1/ArcReactor/Arc-Reactor-Agent-for-Browser")

from lib.documents import ingest_document
from lib import documents_store as store

TEST_USER_ID = (os.getenv("ADMIN_USER_IDS") or "").split(",")[0].strip()
assert TEST_USER_ID, "Set ADMIN_USER_IDS in .env to a real auth.users id to run this test"


async def main():
    # Happy path: a real small text document
    doc_id = str(uuid.uuid4())
    await store.insert_document(doc_id, TEST_USER_ID, "sample.txt", "txt")
    content = ("The quarterly report shows revenue of $4.2M, up 12% year over year. "
               "Operating costs remained flat.").encode("utf-8")
    await ingest_document(doc_id, TEST_USER_ID, "sample.txt", content)

    docs = await store.list_documents(TEST_USER_ID, status="ready")
    match = next((d for d in docs if d["id"] == doc_id), None)
    assert match is not None, "document should be status=ready after successful ingest"
    assert match["chunk_count"] >= 1

    results = await store.search_document_chunks(
        TEST_USER_ID,
        (await __import__("lib.documents", fromlist=["embed_texts"]).embed_texts(["revenue this quarter"]))[0],
        match_count=3,
    )
    assert any("revenue" in r["content"].lower() for r in results), results
    await store.delete_document(doc_id, TEST_USER_ID)

    # Failure path: unsupported extension should mark the doc failed, not raise
    doc_id2 = str(uuid.uuid4())
    await store.insert_document(doc_id2, TEST_USER_ID, "bad.docx", "pdf")  # file_type mismatch is fine here, testing extract_text's own extension check via filename
    await ingest_document(doc_id2, TEST_USER_ID, "bad.docx", b"not a real docx")
    docs = await store.list_documents(TEST_USER_ID, status="failed")
    match2 = next((d for d in docs if d["id"] == doc_id2), None)
    assert match2 is not None, "unsupported file should end up status=failed, not raise"
    assert match2["error_message"]
    await store.delete_document(doc_id2, TEST_USER_ID)

    print("ALL INGEST TESTS PASSED")


asyncio.run(main())
```

- [ ] **Step 3: Run it, expect failure before Step 1's code lands**

Run: `cd "/Users/dhruvpatel/Desktop/Projects 1/ArcReactor/Arc-Reactor-Agent-for-Browser" && uv run python /tmp/test_documents_ingest.py`

Expected: `ImportError: cannot import name 'ingest_document'`

- [ ] **Step 4: Run it again after Step 1**

Run: same command.

Expected: `ALL INGEST TESTS PASSED`

- [ ] **Step 5: Delete scratch test and commit**

```bash
rm /tmp/test_documents_ingest.py
cd "/Users/dhruvpatel/Desktop/Projects 1/ArcReactor/Arc-Reactor-Agent-for-Browser"
git add lib/documents.py
git commit -m "Add end-to-end document ingestion orchestration"
```

---

### Task 7: MCP tool — `search_user_documents`

**Files:**
- Modify: `mcp_servers/models.py` (replace `SearchDocumentsInput`)
- Create: `mcp_servers/server_documents.py`
- Delete: `mcp_servers/server_rag.py`, `mcp_servers/faiss_index/`, `mcp_servers/documents/`
- Test: `/tmp/test_server_documents.py` (scratch)

**Interfaces:**
- Consumes: `lib.documents.embed_texts`, `lib.documents_store.search_document_chunks`.
- Produces: `SearchUserDocumentsInput(BaseModel)` with fields `query: str`, `owner_user_id: str`; MCP tool `search_user_documents(input: SearchUserDocumentsInput) -> list[str]`.

- [ ] **Step 1: Replace `SearchDocumentsInput` in `mcp_servers/models.py`**

Find this block (currently present):

```python
class SearchDocumentsInput(BaseModel):
    query: str
```

Replace it with:

```python
class SearchUserDocumentsInput(BaseModel):
    query: str
    owner_user_id: str
```

- [ ] **Step 2: Delete the old dead RAG server and its empty data directories**

```bash
cd "/Users/dhruvpatel/Desktop/Projects 1/ArcReactor/Arc-Reactor-Agent-for-Browser"
git rm mcp_servers/server_rag.py
git rm -r mcp_servers/faiss_index mcp_servers/documents
```

- [ ] **Step 3: Create `mcp_servers/server_documents.py`**

```python
"""MCP server exposing document RAG search over a user's uploaded documents.
Runs as a standalone stdio subprocess (see multi_mcp.py server_configs)."""
import asyncio
import sys
from pathlib import Path

# Allow importing lib/ from the repo root regardless of this subprocess's cwd.
sys.path.insert(0, str(Path(__file__).parent.parent))

from mcp.server.fastmcp import FastMCP
from models import SearchUserDocumentsInput

from lib.documents import embed_texts
from lib.documents_store import search_document_chunks

mcp = FastMCP("Document RAG")


@mcp.tool()
def search_user_documents(input: SearchUserDocumentsInput) -> list[str]:
    """Search the current user's uploaded documents for chunks relevant to
    the query. owner_user_id is injected by MultiMCP.route_tool_call, not
    supplied by the calling agent."""
    try:
        query_vec = embed_texts([input.query])[0]
        results = asyncio.run(
            search_document_chunks(input.owner_user_id, query_vec, match_count=5)
        )
        if not results:
            return ["No relevant content found in your uploaded documents."]

        formatted = []
        for r in results:
            location = f"{r['filename']}"
            if r.get("page_number"):
                location += f", page {r['page_number']}"
            if r.get("section_title"):
                location += f", section: {r['section_title']}"
            formatted.append(f"{r['content']}\n[Source: {location}]")
        return formatted
    except Exception as e:
        return [f"ERROR: Failed to search documents: {str(e)}"]


async def main():
    if len(sys.argv) > 1 and sys.argv[1] == "dev":
        mcp.run()
    else:
        server_thread_target = lambda: mcp.run(transport="stdio")
        import threading

        thread = threading.Thread(target=server_thread_target)
        thread.daemon = True
        thread.start()
        try:
            while True:
                await asyncio.sleep(1)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 4: Write the test script (calls the tool function directly, bypassing MCP stdio)**

```python
# /tmp/test_server_documents.py
import asyncio
import os
import sys
import uuid
sys.path.insert(0, "/Users/dhruvpatel/Desktop/Projects 1/ArcReactor/Arc-Reactor-Agent-for-Browser")
sys.path.insert(0, "/Users/dhruvpatel/Desktop/Projects 1/ArcReactor/Arc-Reactor-Agent-for-Browser/mcp_servers")

from models import SearchUserDocumentsInput
from lib.documents import ingest_document
from lib import documents_store as store

TEST_USER_ID = (os.getenv("ADMIN_USER_IDS") or "").split(",")[0].strip()
assert TEST_USER_ID


async def main():
    from server_documents import search_user_documents

    doc_id = str(uuid.uuid4())
    await store.insert_document(doc_id, TEST_USER_ID, "policy.txt", "txt")
    await ingest_document(
        doc_id, TEST_USER_ID, "policy.txt",
        b"Refunds are processed within 14 business days of the return request.",
    )

    result = search_user_documents(SearchUserDocumentsInput(query="refund timeline", owner_user_id=TEST_USER_ID))
    assert isinstance(result, list) and result, result
    assert any("14 business days" in r for r in result), result
    assert any("policy.txt" in r for r in result), result

    # A different (nonexistent) user must see nothing of this document.
    other_result = search_user_documents(
        SearchUserDocumentsInput(query="refund timeline", owner_user_id=str(uuid.uuid4()))
    )
    assert not any("14 business days" in r for r in other_result), "isolation broken: other user saw this doc's content"

    await store.delete_document(doc_id, TEST_USER_ID)
    print("ALL SERVER_DOCUMENTS TESTS PASSED")


asyncio.run(main())
```

- [ ] **Step 5: Run it, expect failure before Step 3's code lands**

Run: `cd "/Users/dhruvpatel/Desktop/Projects 1/ArcReactor/Arc-Reactor-Agent-for-Browser" && uv run python /tmp/test_server_documents.py`

Expected: `ModuleNotFoundError: No module named 'server_documents'`

- [ ] **Step 6: Run it again after Step 3**

Run: same command.

Expected: `ALL SERVER_DOCUMENTS TESTS PASSED`

- [ ] **Step 7: Delete scratch test and commit**

```bash
rm /tmp/test_server_documents.py
cd "/Users/dhruvpatel/Desktop/Projects 1/ArcReactor/Arc-Reactor-Agent-for-Browser"
git add mcp_servers/models.py mcp_servers/server_documents.py
git commit -m "Replace dead FAISS/Ollama RAG server with per-user document search tool"
```

---

### Task 8: `multi_mcp.py` — server registration + context injection

**Files:**
- Modify: `mcp_servers/multi_mcp.py`
- Test: `/tmp/test_multi_mcp_context.py` (scratch; starts real subprocess servers)

**Interfaces:**
- Consumes: `mcp_servers/server_documents.py` (Task 7).
- Produces: `MultiMCP.route_tool_call(tool_name: str, arguments: dict, context: dict | None = None)` — for tools in `CONTEXT_INJECTED_TOOLS`, merges `context` into `arguments` before dispatch.

- [ ] **Step 1: Swap the server config**

In `mcp_servers/multi_mcp.py`, find:

```python
            "rag": {
                "command": "uv",
                "args": ["run", "mcp_servers/server_rag.py"],
            },
```

Replace with:

```python
            "documents": {
                "command": "uv",
                "args": ["run", "mcp_servers/server_documents.py"],
            },
```

- [ ] **Step 2: Add context injection to `route_tool_call`**

Find:

```python
    # Helper to route tool call by finding which server has it
    async def route_tool_call(self, tool_name: str, arguments: dict):
        breaker = get_breaker(tool_name, failure_threshold=5, recovery_timeout=60.0)
```

Replace with:

```python
    # Tools that receive caller identity injected server-side, never from
    # LLM-generated arguments (see AgentLoop4._execute_step in core/loop.py).
    CONTEXT_INJECTED_TOOLS = {"search_user_documents"}

    # Helper to route tool call by finding which server has it
    async def route_tool_call(self, tool_name: str, arguments: dict, context: dict | None = None):
        if context and tool_name in self.CONTEXT_INJECTED_TOOLS:
            arguments = {**arguments, **context}

        breaker = get_breaker(tool_name, failure_threshold=5, recovery_timeout=60.0)
```

(The rest of the method body is unchanged — `arguments` is now the merged dict by the time it reaches `self.call_tool(name, tool_name, arguments)`.)

- [ ] **Step 3: Write the test script**

```python
# /tmp/test_multi_mcp_context.py
import asyncio
import os
import sys
import uuid
sys.path.insert(0, "/Users/dhruvpatel/Desktop/Projects 1/ArcReactor/Arc-Reactor-Agent-for-Browser")

from mcp_servers.multi_mcp import MultiMCP
from lib.documents import ingest_document
from lib import documents_store as store

TEST_USER_ID = (os.getenv("ADMIN_USER_IDS") or "").split(",")[0].strip()
assert TEST_USER_ID


async def main():
    multi_mcp = MultiMCP()
    assert "documents" in multi_mcp.server_configs
    assert "rag" not in multi_mcp.server_configs
    await multi_mcp.start()

    try:
        doc_id = str(uuid.uuid4())
        await store.insert_document(doc_id, TEST_USER_ID, "handbook.txt", "txt")
        await ingest_document(doc_id, TEST_USER_ID, "handbook.txt", b"Vacation requests need 2 weeks notice.")

        # Simulate what an LLM-generated tool call looks like: it never
        # includes owner_user_id. route_tool_call must inject it.
        result = await multi_mcp.route_tool_call(
            "search_user_documents",
            {"query": "vacation notice period"},
            context={"owner_user_id": TEST_USER_ID},
        )
        text = result.content[0].text if hasattr(result, "content") else str(result)
        assert "2 weeks" in text, text

        await store.delete_document(doc_id, TEST_USER_ID)
        print("ALL MULTI_MCP CONTEXT TESTS PASSED")
    finally:
        await multi_mcp.exit_stack.aclose()


asyncio.run(main())
```

- [ ] **Step 4: Run it, expect failure before Step 1/2's changes land**

Run: `cd "/Users/dhruvpatel/Desktop/Projects 1/ArcReactor/Arc-Reactor-Agent-for-Browser" && uv run python /tmp/test_multi_mcp_context.py`

Expected (against unmodified `multi_mcp.py`): `AssertionError` on `"documents" in multi_mcp.server_configs` (still says `"rag"`).

- [ ] **Step 5: Run it again after Steps 1-2**

Run: same command.

Expected: `ALL MULTI_MCP CONTEXT TESTS PASSED`. This also exercises the full stdio subprocess path for `server_documents.py`, not just direct function calls — a stronger check than Task 7's test.

- [ ] **Step 6: Delete scratch test and commit**

```bash
rm /tmp/test_multi_mcp_context.py
cd "/Users/dhruvpatel/Desktop/Projects 1/ArcReactor/Arc-Reactor-Agent-for-Browser"
git add mcp_servers/multi_mcp.py
git commit -m "Route documents MCP server and inject owner_user_id server-side"
```

---

### Task 9: `core/loop.py` — thread `owner_user_id` to the tool-call site

**Files:**
- Modify: `core/loop.py`
- Test: `/tmp/test_loop_owner_context.py` (scratch)

**Interfaces:**
- Consumes: `MultiMCP.route_tool_call(..., context=...)` (Task 8).
- Produces: `AgentLoop4.__init__(..., owner_user_id: str | None = None)`; `self.owner_user_id` is passed as `context={"owner_user_id": self.owner_user_id}` at the one `route_tool_call` call site in `_execute_step`.

- [ ] **Step 1: Add `owner_user_id` to `AgentLoop4.__init__`**

Find:

```python
    def __init__(
        self,
        multi_mcp,
        strategy="conservative",
        event_callback=None,
        clarification_callback=None,
        progress_callback=None,
    ):
        self.multi_mcp = multi_mcp
        self.strategy = strategy
        self.agent_runner = AgentRunner(multi_mcp)
        self.bootstrap_context = None
        self.event_callback = event_callback
        self.clarification_callback = clarification_callback
        self.progress_callback = progress_callback
```

Replace with:

```python
    def __init__(
        self,
        multi_mcp,
        strategy="conservative",
        event_callback=None,
        clarification_callback=None,
        progress_callback=None,
        owner_user_id: str | None = None,
    ):
        self.multi_mcp = multi_mcp
        self.strategy = strategy
        self.agent_runner = AgentRunner(multi_mcp)
        self.bootstrap_context = None
        self.event_callback = event_callback
        self.clarification_callback = clarification_callback
        self.progress_callback = progress_callback
        self.owner_user_id = owner_user_id
```

- [ ] **Step 2: Pass context at the tool-call site in `_execute_step`**

Find (inside `_execute_step`):

```python
                try:
                    # Execute tool via MultiMCP
                    tool_result = await self.multi_mcp.route_tool_call(tool_name, tool_args)
```

Replace with:

```python
                try:
                    # Execute tool via MultiMCP. owner_user_id is injected
                    # server-side for context-aware tools (e.g. document
                    # search) — never trust the LLM to supply its own id.
                    tool_result = await self.multi_mcp.route_tool_call(
                        tool_name, tool_args, context={"owner_user_id": self.owner_user_id}
                    )
```

- [ ] **Step 3: Write the test script (stub multi_mcp + monkeypatched agent_runner)**

```python
# /tmp/test_loop_owner_context.py
import asyncio
import sys
sys.path.insert(0, "/Users/dhruvpatel/Desktop/Projects 1/ArcReactor/Arc-Reactor-Agent-for-Browser")

from core.loop import AgentLoop4


class FakeToolResult:
    def __init__(self, text):
        class C:
            pass
        c = C()
        c.text = text
        self.content = [c]


class FakeMultiMCP:
    def __init__(self):
        self.calls = []

    async def route_tool_call(self, tool_name, arguments, context=None):
        self.calls.append((tool_name, arguments, context))
        return FakeToolResult("fake tool result")


async def main():
    fake_mcp = FakeMultiMCP()
    loop = AgentLoop4(multi_mcp=fake_mcp, owner_user_id="user-abc-123")
    assert loop.owner_user_id == "user-abc-123"

    # Build a minimal single-node graph and drive _execute_step directly,
    # stubbing agent_runner.run_agent to return: turn 1 = call_tool,
    # turn 2 = final answer (no call_tool/call_self).
    from memory.context import ExecutionContextManager

    graph = {
        "nodes": [{
            "id": "T001",
            "agent": "DocumentQAAgent",
            "description": "Answer from uploaded docs",
            "agent_prompt": "What is the refund policy?",
            "reads": [],
            "writes": ["answer_T001"],
        }],
        "edges": [{"source": "ROOT", "target": "T001"}],
    }
    context = ExecutionContextManager(graph, session_id=None, original_query="refund policy?", file_manifest=[])
    context.multi_mcp = fake_mcp
    context.mark_running("T001")

    call_count = {"n": 0}

    async def fake_run_agent(agent_type, input_data):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return {"success": True, "output": {"call_tool": {"name": "search_user_documents", "arguments": {"query": "refund policy"}}}}
        return {"success": True, "output": {"answer_T001": "Refunds take 14 days.", "call_tool": None, "call_self": False}}

    loop.agent_runner.run_agent = fake_run_agent

    result = await loop._execute_step("T001", context)
    assert result["success"], result

    assert len(fake_mcp.calls) == 1
    tool_name, arguments, ctx = fake_mcp.calls[0]
    assert tool_name == "search_user_documents"
    assert "owner_user_id" not in arguments, "LLM-supplied arguments must never carry owner_user_id"
    assert ctx == {"owner_user_id": "user-abc-123"}, ctx

    print("ALL LOOP OWNER-CONTEXT TESTS PASSED")


asyncio.run(main())
```

- [ ] **Step 4: Run it, expect failure before Steps 1-2's changes land**

Run: `cd "/Users/dhruvpatel/Desktop/Projects 1/ArcReactor/Arc-Reactor-Agent-for-Browser" && uv run python /tmp/test_loop_owner_context.py`

Expected: `TypeError: __init__() got an unexpected keyword argument 'owner_user_id'`

- [ ] **Step 5: Run it again after Steps 1-2**

Run: same command.

Expected: `ALL LOOP OWNER-CONTEXT TESTS PASSED`

- [ ] **Step 6: Delete scratch test and commit**

```bash
rm /tmp/test_loop_owner_context.py
cd "/Users/dhruvpatel/Desktop/Projects 1/ArcReactor/Arc-Reactor-Agent-for-Browser"
git add core/loop.py
git commit -m "Thread owner_user_id through AgentLoop4 to context-aware tool calls"
```

---

### Task 10: DocumentQAAgent registration + Planner routing

**Files:**
- Create: `prompts/document_qa.md`
- Modify: `config/agent_config.yaml`
- Modify: `prompts/planner.md`
- Modify: `prompts/retriever.md`

**Interfaces:**
- Consumes: `search_user_documents` tool (Task 7/8), the `DocumentQAAgent` config entry read by `agents/base_agent.py:run_agent` (existing, unmodified).
- Produces: a new selectable `agent: "DocumentQAAgent"` value the Planner can emit in a plan graph node.

This task has no automated test of its own — prompts and YAML aren't executable in isolation. It's exercised end-to-end in Task 13.

- [ ] **Step 1: Write `prompts/document_qa.md`**

```markdown
# DocumentQAAgent Prompt

You are **DocumentQAAgent**, responsible for answering questions using the
current user's uploaded documents — never the open web, never outside
knowledge.

Return **strict JSON** (no prose) and always use the exact variable names
from `writes`.

## Available Tool
- `search_user_documents(query)` -> list of relevant excerpts, each ending
  with `[Source: filename, page N, section: ...]` when that metadata is
  available.

## Turn 1: Search
Call the tool with a focused query derived from the task. Emit:

```json
{
  "call_tool": {"name": "search_user_documents", "arguments": {"query": "..."}},
  "thought": "Searching the user's documents for relevant excerpts."
}
```

## Turn 2: Answer
Using only the returned excerpts, write the final answer under the exact
`writes` key(s). Rules:
- Answer directly. Cite the source filename (and page/section when given)
  for every claim, like: "Refunds take 14 days [handbook.txt, page 2]."
- If the excerpts don't contain the answer, say so explicitly — do not
  guess or fall back on general knowledge: "I couldn't find information
  about this in your uploaded documents."
- Do not call the tool a second time. Do not set `call_tool` or `call_self`
  on this turn.

## Output Contract
- Turn 1 output: `call_tool` set, `writes` keys absent or empty.
- Turn 2 output: every key from `writes` present, `call_tool` absent/null,
  `call_self` absent/false.
- No markdown wrappers around the JSON.
```

- [ ] **Step 2: Register the agent in `config/agent_config.yaml`**

Find the end of the `RetrieverAgent` block:

```yaml
  RetrieverAgent:
    prompt_file: "prompts/retriever.md"
    model: "gemini"
    mcp_servers: ["rag", "browser", "weather"]
    description: "Searches local documents and web; live weather via Open-Meteo tool."
```

Replace with (drops the dead `"rag"` entry, adds the new agent right after):

```yaml
  RetrieverAgent:
    prompt_file: "prompts/retriever.md"
    model: "gemini"
    mcp_servers: ["browser", "weather"]
    description: "Searches the web; live weather via Open-Meteo tool."

  DocumentQAAgent:
    prompt_file: "prompts/document_qa.md"
    model: "gemini"
    mcp_servers: ["documents"]
    description: "Answers questions using the user's uploaded documents via vector search."
```

Also update `CoderAgent` and `SummarizerAgent`, which both list the now-removed `"rag"` server:

```yaml
  CoderAgent:
    prompt_file: "prompts/coder.md"
    model: "gemini"
    mcp_servers: ["sandbox", "browser", "rag"]
    description: "Writes Python code to analyze data or solve problems."
```
→
```yaml
  CoderAgent:
    prompt_file: "prompts/coder.md"
    model: "gemini"
    mcp_servers: ["sandbox", "browser"]
    description: "Writes Python code to analyze data or solve problems."
```

```yaml
  SummarizerAgent:
    prompt_file: "prompts/summarizer.md"
    model: "gemini"
    mcp_servers: ["browser", "rag"]
    description: "Synthesizes final answers."
```
→
```yaml
  SummarizerAgent:
    prompt_file: "prompts/summarizer.md"
    model: "gemini"
    mcp_servers: ["browser"]
    description: "Synthesizes final answers."
```

- [ ] **Step 3: Add `DocumentQAAgent` to the Planner's node enum and routing rules**

In `prompts/planner.md`, find:

```
  "agent": "RetrieverAgent" | "ThinkerAgent" | "DistillerAgent" | "CoderAgent" | "FormatterAgent" | "QAAgent" | "ClarificationAgent" | "SchedulerAgent" | "PlannerAgent",
```

Replace with:

```
  "agent": "RetrieverAgent" | "DocumentQAAgent" | "ThinkerAgent" | "DistillerAgent" | "CoderAgent" | "FormatterAgent" | "QAAgent" | "ClarificationAgent" | "SchedulerAgent" | "PlannerAgent",
```

In the "🧠 4. Use Role-Based Abstraction" section, find:

```
* **RetrieverAgent**: Gathers raw external or document-based info
```

Replace with:

```
* **RetrieverAgent**: Gathers raw external info from the open web.
* **DocumentQAAgent**: Answers questions using documents the user has
  uploaded (listed in `file_manifest`). Use this — not RetrieverAgent —
  whenever the query should be answered from an uploaded document rather
  than the web. Reference the relevant filename(s) from `file_manifest` in
  the `agent_prompt`.
```

- [ ] **Step 4: Remove the dead tool reference from `prompts/retriever.md`**

Find:

```
- `search_stored_documents_rag(query)` -> internal docs retrieval
```

Delete this line entirely (RetrieverAgent no longer has the `"rag"` server; document search is now DocumentQAAgent's job).

- [ ] **Step 5: Commit**

```bash
cd "/Users/dhruvpatel/Desktop/Projects 1/ArcReactor/Arc-Reactor-Agent-for-Browser"
git add prompts/document_qa.md config/agent_config.yaml prompts/planner.md prompts/retriever.md
git commit -m "Register DocumentQAAgent and route Planner to it for uploaded-document queries"
```

---

### Task 11: `/api/documents` endpoints + wire runs to real documents

**Files:**
- Modify: `api_server.py`

**Interfaces:**
- Consumes: `lib.documents.ingest_document`, `lib.documents_store.{insert_document,list_documents,delete_document,ENABLED}`, `require_full_account` (existing), `AgentLoop4(owner_user_id=...)` (Task 9).
- Produces: `POST /api/documents`, `GET /api/documents`, `DELETE /api/documents/{id}`.

- [ ] **Step 1: Add imports**

Find:

```python
from fastapi import Depends, FastAPI, HTTPException, Request
```

Replace with:

```python
from fastapi import BackgroundTasks, Depends, FastAPI, File, HTTPException, Request, UploadFile
```

Near the other local imports (`from agents.base_agent import AgentRunner`, `from core.loop import AgentLoop4`), add:

```python
from lib import documents_store
from lib.documents import ingest_document
```

- [ ] **Step 2: Add the endpoints**

Insert after `delete_note` (right before the `# ----- Scheduled Jobs API -----` comment):

```python
# ----- Documents API -----

MAX_DOCUMENT_BYTES = 20 * 1024 * 1024  # 20MB
ALLOWED_DOCUMENT_EXTENSIONS = {".pdf", ".txt", ".md"}


@app.post("/api/documents")
async def upload_document(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    current_user: AuthUser = Depends(require_full_account),
) -> dict[str, Any]:
    if not documents_store.ENABLED:
        raise HTTPException(status_code=503, detail="Document storage is not configured.")

    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_DOCUMENT_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type '{ext}'. Allowed: {', '.join(sorted(ALLOWED_DOCUMENT_EXTENSIONS))}",
        )

    content = await file.read()
    if len(content) > MAX_DOCUMENT_BYTES:
        raise HTTPException(status_code=413, detail="File too large (max 20MB).")
    if not content:
        raise HTTPException(status_code=400, detail="File is empty.")

    document_id = str(uuid4())
    file_type = ext.lstrip(".")
    await documents_store.insert_document(document_id, current_user.user_id, file.filename, file_type)

    background_tasks.add_task(
        lambda: asyncio.create_task(
            ingest_document(document_id, current_user.user_id, file.filename, content)
        )
    )

    return {
        "id": document_id,
        "filename": file.filename,
        "file_type": file_type,
        "status": "processing",
    }


@app.get("/api/documents")
async def list_documents(current_user: AuthUser = Depends(require_full_account)) -> list[dict[str, Any]]:
    if not documents_store.ENABLED:
        return []
    return await documents_store.list_documents(current_user.user_id)


@app.delete("/api/documents/{document_id}")
async def delete_document_endpoint(
    document_id: str, current_user: AuthUser = Depends(require_full_account)
) -> dict[str, Any]:
    if not documents_store.ENABLED:
        raise HTTPException(status_code=503, detail="Document storage is not configured.")
    deleted = await documents_store.delete_document(document_id, current_user.user_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Document not found")
    return {"status": "deleted", "id": document_id}
```

Note on `background_tasks.add_task(lambda: asyncio.create_task(...))`: FastAPI's `BackgroundTasks` awaits coroutines directly too, but doing so would block the HTTP response until ingestion finishes (defeating the point of "processing" status). Wrapping in `asyncio.create_task` inside the background-task callback schedules it as a true fire-and-forget task on the running event loop, matching how `_execute_run` is already launched elsewhere in this file (`asyncio.create_task(_execute_run(...))`).

- [ ] **Step 3: Wire `owner_user_id` and the real `file_manifest` into run creation**

In `_execute_run` (around where `AgentLoop4` is instantiated), find:

```python
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
```

Replace with:

```python
        file_manifest = []
        if documents_store.ENABLED and not owner_user_id.startswith("guest:"):
            ready_docs = await documents_store.list_documents(owner_user_id, status="ready")
            file_manifest = [
                {"filename": d["filename"], "type": d["file_type"]} for d in ready_docs
            ]

        loop = AgentLoop4(
            multi_mcp=multi_mcp,
            event_callback=on_agent_activity,
            clarification_callback=on_clarification,
            progress_callback=on_context_progress,
            owner_user_id=owner_user_id,
        )

        run_task = asyncio.create_task(
            loop.run(
                query=query,
                file_manifest=file_manifest,
                globals_schema={},
                uploaded_files=[],
            )
        )
```

- [ ] **Step 4: Manual verification via curl against the running dev server**

Start (or confirm already running from earlier in this session) the backend:

```bash
cd "/Users/dhruvpatel/Desktop/Projects 1/ArcReactor/Arc-Reactor-Agent-for-Browser"
uv run python -m uvicorn api_server:app --host 127.0.0.1 --port 8000 --reload
```

In another terminal, get a guest token (documents require a full account, so this should be rejected) and then a real user's Supabase JWT is needed for the happy path — for a quick smoke test, confirm the guest-rejection behavior, which doesn't require real credentials:

```bash
curl -s http://127.0.0.1:8000/api/auth/guest | tee /tmp/guest.json
GUEST_TOKEN=$(python3 -c "import json;print(json.load(open('/tmp/guest.json'))['access_token'])")
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/api/documents \
  -H "Authorization: Bearer $GUEST_TOKEN"
```

Expected: `403` (guests can't list documents — `require_full_account` working).

Full happy-path upload/list/delete is covered end-to-end through the UI in Task 13, once a real signed-in session is available in the browser.

- [ ] **Step 5: Commit**

```bash
cd "/Users/dhruvpatel/Desktop/Projects 1/ArcReactor/Arc-Reactor-Agent-for-Browser"
git add api_server.py
git commit -m "Add /api/documents endpoints and wire runs to real per-user documents"
```

---

### Task 12: Frontend Documents panel

**Files:**
- Create: `frontend/src/DocumentsPanel.jsx`
- Modify: `frontend/src/App.jsx`
- Modify: `frontend/src/styles.css`

**Interfaces:**
- Consumes from `App.jsx`: `accessToken` (string), `getAuthHeaders()` (returns `{Authorization}` or `{}`), `apiUrl(path)`, `readErrorMessage(res, fallback)`, `isGuest` (bool) — all existing helpers in `App.jsx`.
- `DocumentsPanel` props: `{ accessToken, getAuthHeaders, apiUrl, readErrorMessage, isGuest }`.

- [ ] **Step 1: Create `frontend/src/DocumentsPanel.jsx`**

```jsx
import { useEffect, useRef, useState } from "react";

const STATUS_LABEL = {
  processing: "Processing…",
  ready: "Ready",
  failed: "Failed",
};

export default function DocumentsPanel({ accessToken, getAuthHeaders, apiUrl, readErrorMessage, isGuest }) {
  const [documents, setDocuments] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [uploading, setUploading] = useState(false);
  const fileInputRef = useRef(null);

  async function fetchDocuments() {
    if (!accessToken || isGuest) return;
    setLoading(true);
    setError("");
    try {
      const res = await fetch(apiUrl("/api/documents"), { headers: getAuthHeaders() });
      if (!res.ok) throw new Error(await readErrorMessage(res, "Failed to load documents"));
      const data = await res.json();
      setDocuments(Array.isArray(data) ? data : []);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    fetchDocuments();
  }, [accessToken, isGuest]);

  useEffect(() => {
    const hasProcessing = documents.some((d) => d.status === "processing");
    if (!hasProcessing) return;
    const id = setInterval(fetchDocuments, 3000);
    return () => clearInterval(id);
  }, [documents]);

  async function handleFileSelected(e) {
    const file = e.target.files?.[0];
    if (!file) return;
    e.target.value = "";
    setUploading(true);
    setError("");
    try {
      const form = new FormData();
      form.append("file", file);
      const res = await fetch(apiUrl("/api/documents"), {
        method: "POST",
        headers: getAuthHeaders(),
        body: form,
      });
      if (!res.ok) throw new Error(await readErrorMessage(res, "Upload failed"));
      await fetchDocuments();
    } catch (e) {
      setError(String(e));
    } finally {
      setUploading(false);
    }
  }

  async function handleDelete(id) {
    setError("");
    try {
      const res = await fetch(apiUrl(`/api/documents/${id}`), {
        method: "DELETE",
        headers: getAuthHeaders(),
      });
      if (!res.ok) throw new Error(await readErrorMessage(res, "Delete failed"));
      setDocuments((prev) => prev.filter((d) => d.id !== id));
    } catch (e) {
      setError(String(e));
    }
  }

  if (isGuest) {
    return (
      <main className="documents-panel">
        <div className="empty">Sign in with Google to upload and query documents.</div>
      </main>
    );
  }

  return (
    <main className="documents-panel">
      <div className="documents-header">
        <h2>Documents</h2>
        <button
          className="documents-upload-btn"
          type="button"
          disabled={uploading}
          onClick={() => fileInputRef.current?.click()}
        >
          {uploading ? "Uploading…" : "Upload document"}
        </button>
        <input
          ref={fileInputRef}
          type="file"
          accept=".pdf,.txt,.md"
          style={{ display: "none" }}
          onChange={handleFileSelected}
        />
      </div>
      {error ? <div className="documents-error">{error}</div> : null}
      {loading && documents.length === 0 ? (
        <div className="empty">Loading…</div>
      ) : documents.length === 0 ? (
        <div className="empty">No documents yet. Upload a PDF, TXT, or MD file to ask questions about it.</div>
      ) : (
        <ul className="documents-list">
          {documents.map((doc) => (
            <li key={doc.id} className="documents-list-item">
              <div className="documents-list-item-main">
                <span className="documents-filename">{doc.filename}</span>
                <span className={`documents-status documents-status-${doc.status}`}>
                  {STATUS_LABEL[doc.status] || doc.status}
                </span>
              </div>
              {doc.status === "failed" && doc.error_message ? (
                <div className="documents-error-message">{doc.error_message}</div>
              ) : null}
              {doc.status === "ready" ? (
                <div className="documents-meta">{doc.chunk_count} chunks indexed</div>
              ) : null}
              <button className="documents-delete-btn" type="button" onClick={() => handleDelete(doc.id)}>
                Delete
              </button>
            </li>
          ))}
        </ul>
      )}
    </main>
  );
}
```

- [ ] **Step 2: Wire it into `App.jsx` — import**

Near the top of `frontend/src/App.jsx`, alongside other imports, add:

```jsx
import DocumentsPanel from "./DocumentsPanel.jsx";
```

- [ ] **Step 3: Add the nav rail button**

Find:

```jsx
          <button
            className={`rail-icon-btn ${activeSection === "scheduler" ? "active" : ""}`}
            onClick={() => setActiveSection("scheduler")}
            title="Scheduler"
          >
            ⏰
          </button>
        </aside>
```

Replace with:

```jsx
          <button
            className={`rail-icon-btn ${activeSection === "scheduler" ? "active" : ""}`}
            onClick={() => setActiveSection("scheduler")}
            title="Scheduler"
          >
            ⏰
          </button>
          <button
            className={`rail-icon-btn ${activeSection === "documents" ? "active" : ""}`}
            onClick={() => setActiveSection("documents")}
            title="Documents"
          >
            📄
          </button>
        </aside>
```

- [ ] **Step 4: Add `documents-mode` to the app-shell className**

Find:

```jsx
      <div className={`app-shell ${inspectorExpanded ? "inspector-expanded" : ""} ${activeSection === "notes" ? "notes-mode" : ""} ${activeSection === "mail" ? "mail-mode" : ""} ${activeSection === "scheduler" ? "scheduler-mode" : ""}`}>
```

Replace with:

```jsx
      <div className={`app-shell ${inspectorExpanded ? "inspector-expanded" : ""} ${activeSection === "notes" ? "notes-mode" : ""} ${activeSection === "mail" ? "mail-mode" : ""} ${activeSection === "scheduler" ? "scheduler-mode" : ""} ${activeSection === "documents" ? "documents-mode" : ""}`}>
```

- [ ] **Step 5: Insert the render branch**

Find the boundary between the scheduler block and the final (notes) fallback:

```jsx
        ) : (
```

This exact `) : (` appears once at this indentation level, right after the scheduler section's closing and before the unconditional notes `<main>` block (verify by context — it should be immediately followed by `<div className="notes-shell">` or similar notes markup, not by another `activeSection === ...` check). Replace it with:

```jsx
        ) : activeSection === "documents" ? (
          <DocumentsPanel
            accessToken={accessToken}
            getAuthHeaders={getAuthHeaders}
            apiUrl={apiUrl}
            readErrorMessage={readErrorMessage}
            isGuest={isGuest}
          />
        ) : (
```

- [ ] **Step 6: Add layout CSS**

In `frontend/src/styles.css`, find:

```css
.app-shell.notes-mode,
.app-shell.mail-mode,
.app-shell.scheduler-mode {
  grid-template-columns: 50px 1fr;
}
```

Replace with:

```css
.app-shell.notes-mode,
.app-shell.mail-mode,
.app-shell.scheduler-mode,
.app-shell.documents-mode {
  grid-template-columns: 50px 1fr;
}
```

Append a new block at the end of `styles.css` for the panel itself:

```css
.documents-panel {
  padding: 24px;
  overflow-y: auto;
}

.documents-header {
  display: flex;
  align-items: center;
  gap: 16px;
  margin-bottom: 20px;
}

.documents-upload-btn {
  padding: 8px 16px;
  border-radius: 6px;
  border: 1px solid var(--border-color, #333);
  background: var(--accent-color, #4a6cf7);
  color: #fff;
  cursor: pointer;
}

.documents-upload-btn:disabled {
  opacity: 0.6;
  cursor: default;
}

.documents-error {
  color: #e5484d;
  margin-bottom: 12px;
}

.documents-list {
  list-style: none;
  padding: 0;
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.documents-list-item {
  padding: 12px 16px;
  border: 1px solid var(--border-color, #333);
  border-radius: 8px;
}

.documents-list-item-main {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.documents-filename {
  font-weight: 600;
}

.documents-status {
  font-size: 12px;
  padding: 2px 8px;
  border-radius: 999px;
}

.documents-status-processing {
  background: #4a6cf733;
  color: #4a6cf7;
}

.documents-status-ready {
  background: #22c55e33;
  color: #22c55e;
}

.documents-status-failed {
  background: #e5484d33;
  color: #e5484d;
}

.documents-error-message {
  margin-top: 6px;
  font-size: 13px;
  color: #e5484d;
}

.documents-meta {
  margin-top: 6px;
  font-size: 13px;
  opacity: 0.7;
}

.documents-delete-btn {
  margin-top: 10px;
  padding: 4px 10px;
  border-radius: 6px;
  border: 1px solid var(--border-color, #333);
  background: transparent;
  cursor: pointer;
}
```

- [ ] **Step 7: Verify the frontend builds**

Run: `cd "/Users/dhruvpatel/Desktop/Projects 1/ArcReactor/Arc-Reactor-Agent-for-Browser/frontend" && npm run build`

Expected: build succeeds with no errors (this will catch any JSX syntax mistakes from Steps 2-5, in particular a wrong match on the `) : (` boundary in Step 5 — if the build fails with a JSX structure error, re-check that replacement).

- [ ] **Step 8: Visual check with Playwright**

```bash
cd "/Users/dhruvpatel/Desktop/Projects 1/ArcReactor/Arc-Reactor-Agent-for-Browser/frontend"
npm run dev &
sleep 3
cd "/Users/dhruvpatel/Desktop/Projects 1/ArcReactor/Arc-Reactor-Agent-for-Browser"
uv run python - <<'EOF'
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page()
    page.goto("http://localhost:5173/agent", wait_until="networkidle", timeout=20000)
    page.wait_for_timeout(1000)
    # Not signed in -> should redirect to /login, same as before this change.
    print("URL:", page.url)
    browser.close()
EOF
```

Expected: still redirects to `/login` (this task doesn't change auth gating) with no console errors — full interactive verification of the Documents panel itself happens signed-in, in Task 13.

- [ ] **Step 9: Commit**

```bash
cd "/Users/dhruvpatel/Desktop/Projects 1/ArcReactor/Arc-Reactor-Agent-for-Browser"
git add frontend/src/DocumentsPanel.jsx frontend/src/App.jsx frontend/src/styles.css
git commit -m "Add Documents panel to the frontend"
```

---

### Task 13: End-to-end manual verification

**Files:** none (verification only)

- [ ] **Step 1: Start both servers**

```bash
cd "/Users/dhruvpatel/Desktop/Projects 1/ArcReactor/Arc-Reactor-Agent-for-Browser"
uv run python -m uvicorn api_server:app --host 127.0.0.1 --port 8000 --reload &
cd frontend && npm run dev &
```

- [ ] **Step 2: Sign in with Google in the browser at `http://localhost:5173/agent`**

- [ ] **Step 3: Upload a document**

Go to the new Documents section (📄 icon), upload a small `.txt` file with a distinctive fact, e.g.:

```
The office wifi password is Tungsten-47. Guests should use the visitor network instead.
```

Confirm the status moves from "Processing…" to "Ready" (polling every 3s per Task 12), and shows a chunk count.

- [ ] **Step 4: Ask a question that requires the document**

Go to the Agent Run section, submit a query like: *"What's the office wifi password?"*

Confirm:
- The execution graph includes a `DocumentQAAgent` node (Planner routed correctly using the populated `file_manifest`).
- The final answer contains "Tungsten-47" and cites the uploaded filename.

- [ ] **Step 5: Confirm isolation**

Sign in as a second Google account (or use guest mode, which should be blocked entirely from the Documents panel per Task 12 Step 1's guest-gating). Confirm the second account's document list is empty and a run under that account cannot retrieve the first account's wifi password.

- [ ] **Step 6: Delete the test document**

Confirm delete removes it from the list and a subsequent query about the wifi password gets "I couldn't find information about this in your uploaded documents" instead.

No commit for this task — it's a verification pass over work already committed in Tasks 1-12. If any step fails, fix the responsible task's code, re-run that task's own test script, then re-run this end-to-end pass.
