"""
mcp_jira_server.py
==================
MCP Server — Jira Tools

Exposes:
  jira_search_issues   search issues via JQL (POST /search/jql with GET fallback)
  jira_create_ticket   create a new Jira issue
  jira_get_issue       fetch a single issue by key

Run standalone (MCP stdio transport):
    python mcp_jira_server.py

Credentials via environment:
    ATLASSIAN_BASE        e.g. https://yourorg.atlassian.net
    ATLASSIAN_EMAIL       your Atlassian account email
    ATLASSIAN_API_TOKEN   Atlassian API token
"""

import os
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
JSON_HEADERS    = {"Accept": "application/json", "Content-Type": "application/json"}

# ── Server ────────────────────────────────────────────────────────────────────
app = Server("jira-server")


# ── Internal helpers ──────────────────────────────────────────────────────────
def _adf_to_text(node: dict, depth: int = 0) -> str:
    """Recursively extract plain text from Atlassian Document Format."""
    if not node:
        return ""
    text   = node.get("text", "")
    result = text
    for child in node.get("content", []):
        result += _adf_to_text(child, depth + 1)
    if node.get("type") in ("paragraph", "heading", "listItem"):
        result += "\n"
    return result


def _jira_search(jql: str, max_results: int = 50) -> dict:
    """Search Jira; tries POST /search/jql first, falls back to GET /search."""
    fields  = ["summary", "description", "status", "priority", "issuetype",
                "assignee", "created"]
    payload = {"jql": jql, "maxResults": max_results, "fields": fields}

    # Primary: POST /search/jql (current Atlassian Cloud)
    url = f"{ATLASSIAN_BASE}/rest/api/3/search/jql"
    r   = requests.post(url, auth=ATLASSIAN_AUTH, json=payload,
                        headers=JSON_HEADERS, timeout=30)
    if r.ok:
        return r.json()

    # Fallback: GET /search
    url2 = f"{ATLASSIAN_BASE}/rest/api/3/search"
    r2   = requests.get(url2, auth=ATLASSIAN_AUTH,
                        params={"jql": jql, "maxResults": max_results,
                                "fields": ",".join(fields)},
                        headers={"Accept": "application/json"}, timeout=30)
    if r2.ok:
        return r2.json()

    r2.raise_for_status()


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="jira_search_issues",
            description="Search Jira issues using JQL. Returns list of simplified issue dicts.",
            inputSchema={
                "type": "object",
                "properties": {
                    "jql":         {"type": "string", "description": "JQL query string"},
                    "max_results": {"type": "integer", "default": 50},
                },
                "required": ["jql"],
            },
        ),
        Tool(
            name="jira_create_ticket",
            description="Create a new Jira issue and return the created issue key.",
            inputSchema={
                "type": "object",
                "properties": {
                    "project_key": {"type": "string", "description": "Jira project key, e.g. ADEV"},
                    "summary":     {"type": "string"},
                    "description": {"type": "string", "description": "Plain-text description"},
                    "issue_type":  {"type": "string", "default": "Task"},
                    "priority":    {"type": "string", "default": "Medium"},
                    "labels":      {"type": "array", "items": {"type": "string"}, "default": []},
                },
                "required": ["project_key", "summary", "description"],
            },
        ),
        Tool(
            name="jira_get_issue",
            description="Fetch a single Jira issue by key (e.g. ADEV-42).",
            inputSchema={
                "type": "object",
                "properties": {
                    "issue_key": {"type": "string"},
                },
                "required": ["issue_key"],
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:

    # ── jira_search_issues ─────────────────────────────────────────────
    if name == "jira_search_issues":
        jql         = arguments["jql"]
        max_results = arguments.get("max_results", 50)
        data        = _jira_search(jql, max_results)

        issues = []
        for item in data.get("issues", []):
            fields = item["fields"]
            issues.append({
                "key":         item["key"],
                "summary":     fields.get("summary", ""),
                "description": _adf_to_text(fields.get("description") or {}),
                "status":      fields["status"]["name"],
                "priority":    (fields.get("priority") or {}).get("name", ""),
                "issuetype":   fields["issuetype"]["name"],
            })
        return [TextContent(type="text", text=json.dumps(issues))]

    # ── jira_create_ticket ─────────────────────────────────────────────
    if name == "jira_create_ticket":
        project_key = arguments["project_key"]
        summary     = arguments["summary"]
        description = arguments["description"]
        issue_type  = arguments.get("issue_type", "Task")
        priority    = arguments.get("priority", "Medium")
        labels      = arguments.get("labels", [])

        body: dict[str, Any] = {
            "fields": {
                "project":    {"key": project_key},
                "summary":    summary,
                "issuetype":  {"name": issue_type},
                "priority":   {"name": priority},
                "description": {
                    "type":    "doc",
                    "version": 1,
                    "content": [
                        {"type": "paragraph",
                         "content": [{"type": "text", "text": description}]}
                    ],
                },
            }
        }
        if labels:
            body["fields"]["labels"] = labels

        url = f"{ATLASSIAN_BASE}/rest/api/3/issue"
        r   = requests.post(url, auth=ATLASSIAN_AUTH, json=body,
                            headers=JSON_HEADERS, timeout=30)
        r.raise_for_status()
        data = r.json()
        return [TextContent(type="text",
                            text=json.dumps({"key": data["key"], "id": data["id"],
                                             "self": data.get("self", "")}))]

    # ── jira_get_issue ─────────────────────────────────────────────────
    if name == "jira_get_issue":
        key = arguments["issue_key"]
        url = f"{ATLASSIAN_BASE}/rest/api/3/issue/{key}"
        r   = requests.get(url, auth=ATLASSIAN_AUTH,
                           headers={"Accept": "application/json"}, timeout=30)
        r.raise_for_status()
        item   = r.json()
        fields = item["fields"]
        result = {
            "key":         item["key"],
            "summary":     fields.get("summary", ""),
            "description": _adf_to_text(fields.get("description") or {}),
            "status":      fields["status"]["name"],
            "priority":    (fields.get("priority") or {}).get("name", ""),
            "issuetype":   fields["issuetype"]["name"],
        }
        return [TextContent(type="text", text=json.dumps(result))]

    return [TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]


# ── Entry point ───────────────────────────────────────────────────────────────
async def main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
