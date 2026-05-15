"""
mcp_jira_server.py
==================
MCP Server - Jira Tools

Exposes:
  jira_search_issues    search issues via JQL
  jira_create_ticket    create a new Jira issue
  jira_get_issue        fetch a single issue by key
  jira_link_issues      link two issues (e.g. Fixes, Blocks)
  jira_add_comment      add a comment to an issue
  jira_transition_issue transition an issue to a new status
  jira_assign_issue     assign an issue to a user

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

ATLASSIAN_BASE  = os.environ.get("ATLASSIAN_BASE", "")
ATLASSIAN_EMAIL = os.environ.get("ATLASSIAN_EMAIL", "")
ATLASSIAN_TOKEN = os.environ.get("ATLASSIAN_API_TOKEN", "")
ATLASSIAN_AUTH  = (ATLASSIAN_EMAIL, ATLASSIAN_TOKEN)
JSON_HEADERS    = {"Accept": "application/json", "Content-Type": "application/json"}

app = Server("jira-server")


def _adf_to_text(node: dict, depth: int = 0) -> str:
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
    fields  = ["summary", "description", "status", "priority",
                "issuetype", "assignee", "created"]
    payload = {"jql": jql, "maxResults": max_results, "fields": fields}
    url  = f"{ATLASSIAN_BASE}/rest/api/3/search/jql"
    r    = requests.post(url, auth=ATLASSIAN_AUTH, json=payload,
                         headers=JSON_HEADERS, timeout=30)
    if r.ok:
        return r.json()
    url2 = f"{ATLASSIAN_BASE}/rest/api/3/search"
    r2   = requests.get(url2, auth=ATLASSIAN_AUTH,
                        params={"jql": jql, "maxResults": max_results,
                                "fields": ",".join(fields)},
                        headers={"Accept": "application/json"}, timeout=30)
    r2.raise_for_status()
    return r2.json()


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="jira_search_issues",
            description="Search Jira issues using JQL.",
            inputSchema={
                "type": "object",
                "properties": {
                    "jql":         {"type": "string"},
                    "max_results": {"type": "integer", "default": 50},
                },
                "required": ["jql"],
            },
        ),
        Tool(
            name="jira_create_ticket",
            description="Create a new Jira issue.",
            inputSchema={
                "type": "object",
                "properties": {
                    "project_key": {"type": "string"},
                    "summary":     {"type": "string"},
                    "description": {"type": "string"},
                    "issue_type":  {"type": "string", "default": "Task"},
                    "priority":    {"type": "string", "default": "Medium"},
                    "labels":      {"type": "array", "items": {"type": "string"}, "default": []},
                },
                "required": ["project_key", "summary", "description"],
            },
        ),
        Tool(
            name="jira_get_issue",
            description="Fetch a single Jira issue by key.",
            inputSchema={
                "type": "object",
                "properties": {"issue_key": {"type": "string"}},
                "required": ["issue_key"],
            },
        ),
        Tool(
            name="jira_link_issues",
            description="Link two Jira issues. link_type examples: Fixes, Blocks, Clones, Relates.",
            inputSchema={
                "type": "object",
                "properties": {
                    "link_type":   {"type": "string", "description": "e.g. Fixes, Blocks, Relates"},
                    "inward_key":  {"type": "string", "description": "Issue that IS the link subject (e.g. ACR-1)"},
                    "outward_key": {"type": "string", "description": "Issue being linked to (e.g. ADEV-42)"},
                },
                "required": ["link_type", "inward_key", "outward_key"],
            },
        ),
        Tool(
            name="jira_add_comment",
            description="Add a plain-text comment to a Jira issue.",
            inputSchema={
                "type": "object",
                "properties": {
                    "issue_key": {"type": "string"},
                    "comment":   {"type": "string"},
                },
                "required": ["issue_key", "comment"],
            },
        ),
        Tool(
            name="jira_transition_issue",
            description="Transition a Jira issue to a new status by transition name.",
            inputSchema={
                "type": "object",
                "properties": {
                    "issue_key":       {"type": "string"},
                    "transition_name": {"type": "string", "description": "e.g. In Progress, Done, Open"},
                },
                "required": ["issue_key", "transition_name"],
            },
        ),
        Tool(
            name="jira_assign_issue",
            description="Assign a Jira issue to a user by their accountId.",
            inputSchema={
                "type": "object",
                "properties": {
                    "issue_key":  {"type": "string"},
                    "account_id": {"type": "string", "description": "Atlassian accountId"},
                },
                "required": ["issue_key", "account_id"],
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:

    # ── jira_search_issues ────────────────────────────────────────────────────
    if name == "jira_search_issues":
        data   = _jira_search(arguments["jql"], arguments.get("max_results", 50))
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

    # ── jira_create_ticket ────────────────────────────────────────────────────
    if name == "jira_create_ticket":
        body: dict[str, Any] = {
            "fields": {
                "project":   {"key": arguments["project_key"]},
                "summary":   arguments["summary"],
                "issuetype": {"name": arguments.get("issue_type", "Task")},
                "priority":  {"name": arguments.get("priority", "Medium")},
                "description": {
                    "type": "doc", "version": 1,
                    "content": [{"type": "paragraph",
                                 "content": [{"type": "text",
                                              "text": arguments["description"]}]}],
                },
            }
        }
        if arguments.get("labels"):
            body["fields"]["labels"] = arguments["labels"]
        r = requests.post(f"{ATLASSIAN_BASE}/rest/api/3/issue",
                          auth=ATLASSIAN_AUTH, json=body,
                          headers=JSON_HEADERS, timeout=30)
        r.raise_for_status()
        d = r.json()
        return [TextContent(type="text",
                            text=json.dumps({"key": d["key"], "id": d["id"],
                                             "self": d.get("self", "")}))]

    # ── jira_get_issue ────────────────────────────────────────────────────────
    if name == "jira_get_issue":
        key = arguments["issue_key"]
        r   = requests.get(f"{ATLASSIAN_BASE}/rest/api/3/issue/{key}",
                           auth=ATLASSIAN_AUTH,
                           headers={"Accept": "application/json"}, timeout=30)
        r.raise_for_status()
        item   = r.json()
        fields = item["fields"]
        return [TextContent(type="text", text=json.dumps({
            "key":         item["key"],
            "summary":     fields.get("summary", ""),
            "description": _adf_to_text(fields.get("description") or {}),
            "status":      fields["status"]["name"],
            "priority":    (fields.get("priority") or {}).get("name", ""),
            "issuetype":   fields["issuetype"]["name"],
        }))]

    # ── jira_link_issues ──────────────────────────────────────────────────────
    if name == "jira_link_issues":
        payload = {
            "type":         {"name": arguments["link_type"]},
            "inwardIssue":  {"key": arguments["inward_key"]},
            "outwardIssue": {"key": arguments["outward_key"]},
        }
        r = requests.post(f"{ATLASSIAN_BASE}/rest/api/3/issueLink",
                          auth=ATLASSIAN_AUTH, json=payload,
                          headers=JSON_HEADERS, timeout=30)
        # 201 = created, 204 = no content — both are success
        if r.status_code not in (200, 201, 204):
            # Try fallback link type names
            for fallback in ["Relates", "relates to", "Duplicate"]:
                payload["type"] = {"name": fallback}
                r2 = requests.post(f"{ATLASSIAN_BASE}/rest/api/3/issueLink",
                                   auth=ATLASSIAN_AUTH, json=payload,
                                   headers=JSON_HEADERS, timeout=30)
                if r2.status_code in (200, 201, 204):
                    return [TextContent(type="text",
                                        text=json.dumps({"status": "linked",
                                                         "type": fallback}))]
            r.raise_for_status()
        return [TextContent(type="text",
                            text=json.dumps({"status": "linked",
                                             "type": arguments["link_type"]}))]

    # ── jira_add_comment ──────────────────────────────────────────────────────
    if name == "jira_add_comment":
        key     = arguments["issue_key"]
        comment = arguments["comment"]
        body    = {
            "body": {
                "type": "doc", "version": 1,
                "content": [{"type": "paragraph",
                              "content": [{"type": "text", "text": comment}]}],
            }
        }
        r = requests.post(f"{ATLASSIAN_BASE}/rest/api/3/issue/{key}/comment",
                          auth=ATLASSIAN_AUTH, json=body,
                          headers=JSON_HEADERS, timeout=30)
        r.raise_for_status()
        return [TextContent(type="text", text=json.dumps({"status": "commented"}))]

    # ── jira_transition_issue ─────────────────────────────────────────────────
    if name == "jira_transition_issue":
        key             = arguments["issue_key"]
        transition_name = arguments["transition_name"].lower()

        # Get available transitions
        r = requests.get(f"{ATLASSIAN_BASE}/rest/api/3/issue/{key}/transitions",
                         auth=ATLASSIAN_AUTH,
                         headers={"Accept": "application/json"}, timeout=30)
        r.raise_for_status()
        transitions = r.json().get("transitions", [])

        # Find best match
        matched = None
        for t in transitions:
            if transition_name in t["name"].lower():
                matched = t
                break

        if not matched:
            available = [t["name"] for t in transitions]
            return [TextContent(type="text",
                                text=json.dumps({"error": f"Transition '{arguments['transition_name']}' not found",
                                                 "available": available}))]

        r2 = requests.post(f"{ATLASSIAN_BASE}/rest/api/3/issue/{key}/transitions",
                           auth=ATLASSIAN_AUTH,
                           json={"transition": {"id": matched["id"]}},
                           headers=JSON_HEADERS, timeout=30)
        r2.raise_for_status()
        return [TextContent(type="text",
                            text=json.dumps({"status": "transitioned",
                                             "to": matched["name"]}))]

    # ── jira_assign_issue ─────────────────────────────────────────────────────
    if name == "jira_assign_issue":
        key        = arguments["issue_key"]
        account_id = arguments["account_id"]
        r = requests.put(f"{ATLASSIAN_BASE}/rest/api/3/issue/{key}/assignee",
                         auth=ATLASSIAN_AUTH,
                         json={"accountId": account_id},
                         headers=JSON_HEADERS, timeout=30)
        r.raise_for_status()
        return [TextContent(type="text",
                            text=json.dumps({"status": "assigned",
                                             "account_id": account_id}))]

    return [TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]


async def main():
    async with stdio_server() as (r, w):
        await app.run(r, w, app.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
