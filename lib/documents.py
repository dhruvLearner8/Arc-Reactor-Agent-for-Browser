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
