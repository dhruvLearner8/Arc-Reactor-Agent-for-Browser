"""Persistent guest run quota (local JSON)."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

BASE_DIR = Path(__file__).parent
GUEST_USAGE_PATH = BASE_DIR / "memory" / "guest_usage.json"

_guest_lock = asyncio.Lock()


def guest_run_limit() -> int:
    try:
        return max(1, int((os.getenv("GUEST_RUN_LIMIT") or "7").strip()))
    except ValueError:
        return 7


def _load_raw() -> dict[str, int]:
    if not GUEST_USAGE_PATH.exists():
        return {}
    try:
        data = json.loads(GUEST_USAGE_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        out: dict[str, int] = {}
        for k, v in data.items():
            if isinstance(k, str) and isinstance(v, (int, float)):
                out[k] = int(v)
        return out
    except Exception:
        return {}


def _save_raw(data: dict[str, int]) -> None:
    GUEST_USAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
    GUEST_USAGE_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def guest_runs_used(session_key: str) -> int:
    return max(0, _load_raw().get(session_key, 0))


async def guest_try_consume_run(session_key: str, limit: int) -> None:
    """Raises ValueError if guest is at or over limit."""
    async with _guest_lock:
        data = _load_raw()
        used = int(data.get(session_key, 0))
        if used >= limit:
            raise ValueError("guest_run_limit")
        data[session_key] = used + 1
        _save_raw(data)
