"""
Standalone Gmail MCP server — NOT wired into Arc Reactor agents.
Use for CLI / external MCP clients. S8 Share Mail UI uses api_server routes + gmail_api directly.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Ensure project root on path
_root = Path(__file__).parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from lib.gmail_api import (
    get_gmail_service,
    list_messages,
    get_message,
    send_message,
    reply_to_message,
)
from mcp.server.fastmcp import FastMCP
from mcp.types import TextContent

mcp = FastMCP("gmail-standalone")


@mcp.tool()
def gmail_list_emails(max_results: int = 20, query: str = "") -> str:
    """
    List recent Gmail messages. Returns JSON with id, subject, from, date, snippet.
    query: optional Gmail search (e.g. 'is:unread', 'from:someone@example.com').
    """
    try:
        service = get_gmail_service()
        msgs, _ = list_messages(service, max_results=max_results, query=query)
        return json.dumps(msgs, indent=2)
    except FileNotFoundError as e:
        return json.dumps({"error": "gmail_not_configured", "message": str(e)})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gmail_get_email(message_id: str) -> str:
    """Get full email by ID: subject, from, to, date, body."""
    try:
        service = get_gmail_service()
        msg = get_message(service, message_id)
        return json.dumps(msg, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gmail_send_email(to: str, subject: str, body: str) -> str:
    """Send a new email via Gmail."""
    try:
        service = get_gmail_service()
        sent = send_message(service, to=to, subject=subject, body=body)
        return json.dumps({"status": "sent", "id": sent.get("id")})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gmail_reply_to_email(message_id: str, body: str) -> str:
    """Reply to an existing email. Keeps the thread."""
    try:
        service = get_gmail_service()
        sent = reply_to_message(service, original_id=message_id, body=body)
        return json.dumps({"status": "sent", "id": sent.get("id")})
    except Exception as e:
        return json.dumps({"error": str(e)})


if __name__ == "__main__":
    mcp.run(transport="stdio")
