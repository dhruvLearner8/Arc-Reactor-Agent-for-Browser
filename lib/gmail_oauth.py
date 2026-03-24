"""Per-user Gmail OAuth (web flow) and Credentials → Gmail API service."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

from lib.gmail_api import SCOPES, _CREDENTIALS_PATH

_ROOT = Path(__file__).parent.parent


def gmail_credentials_file_exists() -> bool:
    return _CREDENTIALS_PATH.exists()


def create_oauth_flow(redirect_uri: str) -> Flow:
    if not _CREDENTIALS_PATH.exists():
        raise FileNotFoundError(f"Missing {_CREDENTIALS_PATH}")
    return Flow.from_client_secrets_file(
        str(_CREDENTIALS_PATH),
        scopes=SCOPES,
        redirect_uri=redirect_uri,
    )


def credentials_from_stored_dict(data: dict[str, Any]) -> Credentials:
    return Credentials.from_authorized_user_info(data, SCOPES)


def credentials_to_storable_dict(creds: Credentials) -> dict[str, Any]:
    return json.loads(creds.to_json())


def refresh_credentials_if_needed(creds: Credentials) -> bool:
    """Refresh expired access token. Returns True if credentials were refreshed."""
    if not creds.expired:
        return False
    if not creds.refresh_token:
        return False
    creds.refresh(Request())
    return True


def build_gmail_service(creds: Credentials):
    return build("gmail", "v1", credentials=creds)


def fetch_google_email(creds: Credentials) -> str | None:
    try:
        service = build_gmail_service(creds)
        prof = service.users().getProfile(userId="me").execute()
        return prof.get("emailAddress")
    except Exception:
        return None
