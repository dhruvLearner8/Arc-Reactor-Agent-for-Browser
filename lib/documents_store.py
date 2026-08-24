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
