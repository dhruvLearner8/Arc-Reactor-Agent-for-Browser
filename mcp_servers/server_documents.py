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
async def search_user_documents(input: SearchUserDocumentsInput) -> list[str]:
    """Search the current user's uploaded documents for chunks relevant to
    the query. owner_user_id is injected by MultiMCP.route_tool_call, not
    supplied by the calling agent."""
    try:
        query_vec = embed_texts([input.query])[0]
        results = await search_document_chunks(input.owner_user_id, query_vec, match_count=5)
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
