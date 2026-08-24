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
