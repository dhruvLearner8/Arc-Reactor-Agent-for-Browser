"""
Gmail API helper: list, get, send, reply. Used by Mail API routes and Gmail MCP.
Requires credentials.json + token.json (run generate_gmail_token.py once).
"""
from __future__ import annotations

import base64
from email.mime.text import MIMEText
from pathlib import Path

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import BatchHttpRequest

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
]

_ROOT = Path(__file__).parent.parent
_CREDENTIALS_PATH = _ROOT / "credentials.json"
_TOKEN_PATH = _ROOT / "token.json"


def _ensure_token():
    """Generate token.json from credentials.json if missing."""
    if _TOKEN_PATH.exists():
        return
    if not _CREDENTIALS_PATH.exists():
        raise FileNotFoundError(
            f"Put credentials.json in {_ROOT}. Get it from Google Cloud Console with Gmail API enabled."
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(_CREDENTIALS_PATH), SCOPES)
    creds = flow.run_local_server(port=0)
    with open(_TOKEN_PATH, "w") as f:
        f.write(creds.to_json())


def get_gmail_service(credentials_path: Path | None = None, token_path: Path | None = None):
    """Return an authorized Gmail API service."""
    creds_path = credentials_path or _CREDENTIALS_PATH
    tok_path = token_path or _TOKEN_PATH
    if not tok_path.exists() and creds_path.exists():
        flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
        creds = flow.run_local_server(port=0)
        with open(tok_path, "w") as f:
            f.write(creds.to_json())
    creds = Credentials.from_authorized_user_file(str(tok_path), SCOPES)
    return build("gmail", "v1", credentials=creds)


def list_threads(
    service,
    max_results: int = 20,
    query: str = "",
    page_token: str | None = None,
) -> tuple[list[dict], str | None]:
    """List threads (conversations) with full message content. Returns (threads, next_page_token)."""
    list_kw: dict = {"userId": "me", "maxResults": max_results}
    if query:
        list_kw["q"] = query
    if page_token:
        list_kw["pageToken"] = page_token
    resp = service.users().threads().list(**list_kw).execute()
    thread_list = resp.get("threads") or []
    next_page_token = resp.get("nextPageToken")
    if not thread_list:
        return [], next_page_token

    result: list[dict | None] = [None] * len(thread_list)

    def _callback(req_id: str, response, exception):
        idx = int(req_id)
        tid = thread_list[idx].get("id") if idx < len(thread_list) else ""
        if exception:
            result[idx] = {"id": tid, "messages": [], "snippet": ""}
        else:
            msgs = response.get("messages") or []
            parsed = []
            for m in msgs:
                headers = {h["name"].lower(): h["value"] for h in m.get("payload", {}).get("headers", [])}
                parsed.append({
                    "id": m.get("id"),
                    "threadId": m.get("threadId"),
                    "subject": headers.get("subject", "(no subject)"),
                    "from": headers.get("from", ""),
                    "to": headers.get("to", ""),
                    "date": headers.get("date", ""),
                    "snippet": m.get("snippet", ""),
                    "body": _extract_body(m.get("payload")),
                })
            parsed.sort(key=lambda x: (x.get("date") or ""))
            result[idx] = {
                "id": tid,
                "messages": parsed,
                "snippet": response.get("snippet", ""),
            }

    batch = BatchHttpRequest(
        callback=_callback,
        batch_uri="https://www.googleapis.com/batch/gmail/v1",
    )
    for i, t in enumerate(thread_list):
        tid = t.get("id")
        if tid:
            batch.add(
                service.users().threads().get(userId="me", id=tid, format="full"),
                request_id=str(i),
            )
    batch.execute()
    return [r for r in result if r is not None], next_page_token


def list_messages(
    service,
    max_results: int = 20,
    query: str = "",
    page_token: str | None = None,
) -> tuple[list[dict], str | None]:
    """List messages with full content. Returns (messages, next_page_token)."""
    list_kw: dict = {"userId": "me", "maxResults": max_results}
    if query:
        list_kw["q"] = query
    if page_token:
        list_kw["pageToken"] = page_token
    resp = service.users().messages().list(**list_kw).execute()
    msgs = resp.get("messages") or []
    next_page_token = resp.get("nextPageToken")
    if not msgs:
        return [], next_page_token

    result: list[dict | None] = [None] * len(msgs)

    def _callback(req_id: str, response, exception):
        idx = int(req_id)
        mid = msgs[idx].get("id") if idx < len(msgs) else ""
        if exception:
            result[idx] = {"id": mid, "subject": "(unable to fetch)", "from": "", "date": "", "snippet": "", "body": ""}
        else:
            result[idx] = _message_full_to_simple(response)

    batch = BatchHttpRequest(
        callback=_callback,
        batch_uri="https://www.googleapis.com/batch/gmail/v1",
    )
    for i, m in enumerate(msgs):
        mid = m.get("id")
        if mid:
            batch.add(
                service.users().messages().get(userId="me", id=mid, format="full"),
                request_id=str(i),
            )
    batch.execute()
    return [r for r in result if r is not None], next_page_token


def _message_meta_to_simple(meta: dict) -> dict:
    headers = {h["name"].lower(): h["value"] for h in meta.get("payload", {}).get("headers", [])}
    return {
        "id": meta.get("id"),
        "threadId": meta.get("threadId"),
        "subject": headers.get("subject", "(no subject)"),
        "from": headers.get("from", ""),
        "to": headers.get("to", ""),
        "date": headers.get("date", ""),
        "snippet": meta.get("snippet", ""),
    }


def _message_full_to_simple(meta: dict) -> dict:
    """Like _message_meta_to_simple but includes body (from format=full)."""
    out = _message_meta_to_simple(meta)
    out["body"] = _extract_body(meta.get("payload"))
    return out


def get_thread(service, thread_id: str) -> list[dict]:
    """Get all messages in a thread, oldest first."""
    thread = service.users().threads().get(userId="me", id=thread_id, format="full").execute()
    messages = thread.get("messages") or []
    result = []
    for m in messages:
        headers = {h["name"].lower(): h["value"] for h in m.get("payload", {}).get("headers", [])}
        result.append({
            "id": m.get("id"),
            "threadId": m.get("threadId"),
            "subject": headers.get("subject", "(no subject)"),
            "from": headers.get("from", ""),
            "to": headers.get("to", ""),
            "date": headers.get("date", ""),
            "snippet": m.get("snippet", ""),
            "body": _extract_body(m.get("payload")),
        })
    result.sort(key=lambda x: (x.get("date") or ""))
    return result


def get_message(service, message_id: str) -> dict:
    """Get full message (subject, from, to, date, body)."""
    meta = service.users().messages().get(userId="me", id=message_id, format="full").execute()
    headers = {h["name"].lower(): h["value"] for h in meta.get("payload", {}).get("headers", [])}
    body_text = _extract_body(meta.get("payload"))
    return {
        "id": meta.get("id"),
        "threadId": meta.get("threadId"),
        "subject": headers.get("subject", "(no subject)"),
        "from": headers.get("from", ""),
        "to": headers.get("to", ""),
        "date": headers.get("date", ""),
        "snippet": meta.get("snippet", ""),
        "body": body_text,
    }


def _extract_body(payload: dict | None) -> str:
    if not payload:
        return ""
    mime = (payload.get("mimeType") or "").lower()
    data = payload.get("body", {}).get("data")
    if data and mime in ("text/plain", "text/html"):
        return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
    plain, html = "", ""
    for part in payload.get("parts") or []:
        pt = (part.get("mimeType") or "").lower()
        d = part.get("body", {}).get("data")
        if not d:
            continue
        decoded = base64.urlsafe_b64decode(d).decode("utf-8", errors="replace")
        if pt == "text/plain":
            plain = decoded
        elif pt == "text/html":
            html = decoded
    return plain or html


def create_message(to: str, subject: str, body: str, in_reply_to: str | None = None, references: str | None = None) -> dict:
    """Create a Gmail message dict (for send or reply)."""
    msg = MIMEText(body, "plain", "utf-8")
    msg["to"] = to
    msg["subject"] = subject
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    return {"raw": raw}


def send_message(service, to: str, subject: str, body: str) -> dict:
    """Send a new email."""
    message = create_message(to=to, subject=subject, body=body)
    return service.users().messages().send(userId="me", body=message).execute()


def reply_to_message(service, original_id: str, body: str) -> dict:
    """Reply to an existing message. Keeps thread intact."""
    orig = service.users().messages().get(userId="me", id=original_id, format="metadata").execute()
    headers = {h["name"].lower(): h["value"] for h in orig.get("payload", {}).get("headers", [])}
    to = headers.get("reply-to") or headers.get("from", "")
    subject = headers.get("subject", "")
    if subject and not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"
    msg_id = headers.get("message-id", "")
    refs = headers.get("references", "")
    if msg_id and (not refs or msg_id not in refs):
        refs = f"{refs} {msg_id}".strip() if refs else msg_id
    message = create_message(to=to, subject=subject, body=body, in_reply_to=msg_id or None, references=refs or None)
    thread_id = orig.get("threadId")
    body_send = dict(message)
    if thread_id:
        body_send["threadId"] = thread_id
    return service.users().messages().send(userId="me", body=body_send).execute()
