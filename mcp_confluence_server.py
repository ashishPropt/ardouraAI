"""
mcp_confluence_server.py
========================
MCP Server — Confluence Tools

Exposes:
  confluence_get_page            fetch page body as plain text by page ID
  confluence_create_or_update    create or update a page by space+title
  confluence_find_page_id        look up a page ID by space key + title

Run standalone (MCP stdio transport):
    python mcp_confluence_server.py

Credentials via environment:
    ATLASSIAN_BASE        e.g. https://yourorg.atlassian.net
    ATLASSIAN_EMAIL       your Atlassian account email
    ATLASSIAN_API_TOKEN   Atlassian API token
"""

import os
import re
import json
from typing import Any

import requests
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

# ── Credentials ───────────────────────────────────────────────────────────────
ATLASSIAN_BASE  = os.environ.get("ATLASSIAN_BASE", "")
ATLASSIAN_EMAIL = os.environ.get("ATLASSIAN_EMAIL", "")
ATLASSIAN_TOKEN = os.environ.get("ATLASSIAN_API_TOKEN", "")
ATLASSIAN_AUTH  = (ATLASSIAN_EMAIL, ATLASSIAN_TOKEN)

WIKI_BASE = f"{ATLASSIAN_BASE}/wiki"

# ── Server ────────────────────────────────────────────────────────────────────
app = Server("confluence-server")


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="confluence_get_page",
            description=(
                "Fetch a Confluence page's content as plain text by page ID. "
                "Returns {title, text}."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "page_id": {"type": "string", "description": "Confluence page ID"},
                },
                "required": ["page_id"],
            },
        ),
        Tool(
            name="confluence_find_page_id",
            description="Look up a Confluence page ID by space key and title.",
            inputSchema={
                "type": "object",
                "properties": {
                    "space_key": {"type": "string"},
                    "title":     {"type": "string"},
                },
                "required": ["space_key", "title"],
            },
        ),
        Tool(
            name="confluence_create_or_update",
            description=(
                "Create or update a Confluence page in a given space. "
                "Content is plain text (newlines are converted to <br/>)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "space_key": {"type": "string"},
                    "title":     {"type": "string"},
                    "content":   {"type": "string", "description": "Plain-text page body"},
                },
                "required": ["space_key", "title", "content"],
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:

    # ── confluence_get_page ────────────────────────────────────────────
    if name == "confluence_get_page":
        page_id = arguments["page_id"]
        url     = f"{WIKI_BASE}/rest/api/content/{page_id}?expand=body.storage,title"
        r       = requests.get(url, auth=ATLASSIAN_AUTH,
                               headers={"Accept": "application/json"}, timeout=30)
        r.raise_for_status()
        data    = r.json()
        title   = data.get("title", "")
        storage = data["body"]["storage"]["value"]
        plain   = re.sub(r"<[^>]+>", " ", storage)
        plain   = re.sub(r"\s{2,}", " ", plain).strip()
        return [TextContent(type="text",
                            text=json.dumps({"title": title,
                                             "text": f"=== {title} ===\n\n{plain}"}))]

    # ── confluence_find_page_id ────────────────────────────────────────
    if name == "confluence_find_page_id":
        space_key = arguments["space_key"]
        title     = arguments["title"]
        url       = f"{WIKI_BASE}/rest/api/content"
        r = requests.get(url, auth=ATLASSIAN_AUTH,
                         params={"title": title, "spaceKey": space_key},
                         headers={"Accept": "application/json"}, timeout=30)
        r.raise_for_status()
        results = r.json().get("results", [])
        if results:
            page_id = results[0]["id"]
            return [TextContent(type="text",
                                text=json.dumps({"page_id": page_id, "found": True}))]
        return [TextContent(type="text", text=json.dumps({"page_id": None, "found": False}))]

    # ── confluence_create_or_update ────────────────────────────────────
    if name == "confluence_create_or_update":
        space_key = arguments["space_key"]
        title     = arguments["title"]
        content   = arguments["content"]
        html_body = content.replace("\n", "<br/>")

        # Check if page exists
        find_url = f"{WIKI_BASE}/rest/api/content"
        fr = requests.get(find_url, auth=ATLASSIAN_AUTH,
                          params={"title": title, "spaceKey": space_key},
                          headers={"Accept": "application/json"}, timeout=30)
        fr.raise_for_status()
        results = fr.json().get("results", [])

        if results:
            # Update existing page
            page_id = results[0]["id"]
            get_url = f"{WIKI_BASE}/rest/api/content/{page_id}"
            gr      = requests.get(get_url, auth=ATLASSIAN_AUTH,
                                   headers={"Accept": "application/json"}, timeout=30)
            gr.raise_for_status()
            version = gr.json()["version"]["number"] + 1

            data = {
                "id":      page_id,
                "type":    "page",
                "title":   title,
                "space":   {"key": space_key},
                "version": {"number": version},
                "body": {
                    "storage": {
                        "value": html_body,
                        "representation": "storage",
                    }
                },
            }
            r = requests.put(get_url, auth=ATLASSIAN_AUTH, json=data,
                             headers={"Accept": "application/json",
                                      "Content-Type": "application/json"},
                             timeout=30)
            r.raise_for_status()
            return [TextContent(type="text",
                                text=json.dumps({"action": "updated", "page_id": page_id,
                                                 "status_code": r.status_code}))]
        else:
            # Create new page
            data = {
                "type":  "page",
                "title": title,
                "space": {"key": space_key},
                "body": {
                    "storage": {
                        "value": html_body,
                        "representation": "storage",
                    }
                },
            }
            r = requests.post(find_url, auth=ATLASSIAN_AUTH, json=data,
                              headers={"Accept": "application/json",
                                       "Content-Type": "application/json"},
                              timeout=30)
            r.raise_for_status()
            new_id = r.json().get("id", "")
            return [TextContent(type="text",
                                text=json.dumps({"action": "created", "page_id": new_id,
                                                 "status_code": r.status_code}))]

    return [TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]


# ── Entry point ───────────────────────────────────────────────────────────────
async def main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
