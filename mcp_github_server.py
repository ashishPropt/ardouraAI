"""
mcp_github_server.py
====================
MCP Server — GitHub Tools

Exposes:
  github_get_repo_tree    list all blob paths in a repo (recursive)
  github_get_file         fetch a single file's content + sha
  github_commit_file      create or update a file and commit it
  github_load_codebase    bulk-load all source-code files from a repo

Run standalone (MCP stdio transport):
    python mcp_github_server.py

Credentials via environment:
    GITHUB_TOKEN   personal access token
"""

import os
import base64
import json
from typing import Any

import requests
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

# ── Credentials ───────────────────────────────────────────────────────────────
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GH_HEADERS = {
    "Authorization": f"token {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
}
CODE_EXTENSIONS = (
    ".py", ".js", ".ts", ".java", ".go", ".rb", ".cs",
    ".yaml", ".yml", ".sql", ".sh", ".tf", ".php", ".html", ".css",
)

# ── Server ────────────────────────────────────────────────────────────────────
app = Server("github-server")


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="github_get_repo_tree",
            description="List all file paths in a GitHub repo recursively.",
            inputSchema={
                "type": "object",
                "properties": {
                    "owner":  {"type": "string", "description": "GitHub user or org"},
                    "repo":   {"type": "string", "description": "Repository name"},
                    "branch": {"type": "string", "default": "main"},
                },
                "required": ["owner", "repo"],
            },
        ),
        Tool(
            name="github_get_file",
            description="Fetch decoded content and SHA of a single file from GitHub.",
            inputSchema={
                "type": "object",
                "properties": {
                    "owner": {"type": "string"},
                    "repo":  {"type": "string"},
                    "path":  {"type": "string", "description": "Repo-relative file path"},
                },
                "required": ["owner", "repo", "path"],
            },
        ),
        Tool(
            name="github_commit_file",
            description="Create or update a file in a GitHub repo with a commit message.",
            inputSchema={
                "type": "object",
                "properties": {
                    "owner":          {"type": "string"},
                    "repo":           {"type": "string"},
                    "path":           {"type": "string"},
                    "content":        {"type": "string", "description": "New file content (plain text)"},
                    "commit_message": {"type": "string"},
                    "branch":         {"type": "string", "default": "main"},
                },
                "required": ["owner", "repo", "path", "content", "commit_message"],
            },
        ),
        Tool(
            name="github_load_codebase",
            description=(
                "Load all source-code files from a GitHub repo. "
                "Returns {files: {path: content}, errors: [], file_count: N}"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "owner":  {"type": "string"},
                    "repo":   {"type": "string"},
                    "branch": {"type": "string", "default": "main"},
                },
                "required": ["owner", "repo"],
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:

    # ── github_get_repo_tree ───────────────────────────────────────────
    if name == "github_get_repo_tree":
        owner  = arguments["owner"]
        repo   = arguments["repo"]
        branch = arguments.get("branch", "main")
        url = (f"https://api.github.com/repos/{owner}/{repo}"
               f"/git/trees/{branch}?recursive=1")
        r = requests.get(url, headers=GH_HEADERS, timeout=30)
        r.raise_for_status()
        paths = [i["path"] for i in r.json().get("tree", []) if i["type"] == "blob"]
        return [TextContent(type="text", text=json.dumps(paths))]

    # ── github_get_file ────────────────────────────────────────────────
    if name == "github_get_file":
        owner = arguments["owner"]
        repo  = arguments["repo"]
        path  = arguments["path"]
        url   = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}"
        r = requests.get(url, headers=GH_HEADERS, timeout=30)
        r.raise_for_status()
        data    = r.json()
        content = base64.b64decode(data["content"]).decode("utf-8")
        return [TextContent(type="text",
                            text=json.dumps({"content": content, "sha": data["sha"],
                                             "path": path}))]

    # ── github_commit_file ─────────────────────────────────────────────
    if name == "github_commit_file":
        owner   = arguments["owner"]
        repo    = arguments["repo"]
        path    = arguments["path"]
        content = arguments["content"]
        message = arguments["commit_message"]
        branch  = arguments.get("branch", "main")
        url     = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}"

        sha = None
        get_r = requests.get(url, headers=GH_HEADERS, timeout=30)
        if get_r.status_code == 200:
            sha = get_r.json().get("sha")

        encoded = base64.b64encode(content.encode("utf-8")).decode("utf-8")
        payload: dict[str, Any] = {
            "message": message,
            "content": encoded,
            "branch":  branch,
        }
        if sha:
            payload["sha"] = sha

        r = requests.put(url, headers=GH_HEADERS, json=payload, timeout=30)
        r.raise_for_status()
        commit_sha = r.json().get("commit", {}).get("sha", "")
        return [TextContent(type="text",
                            text=json.dumps({"committed": True,
                                             "commit_sha": commit_sha,
                                             "path": path}))]

    # ── github_load_codebase ───────────────────────────────────────────
    if name == "github_load_codebase":
        owner  = arguments["owner"]
        repo   = arguments["repo"]
        branch = arguments.get("branch", "main")

        tree_url = (f"https://api.github.com/repos/{owner}/{repo}"
                    f"/git/trees/{branch}?recursive=1")
        r = requests.get(tree_url, headers=GH_HEADERS, timeout=30)
        r.raise_for_status()
        paths = [i["path"] for i in r.json().get("tree", []) if i["type"] == "blob"]

        code_files: dict[str, str] = {}
        errors: list[str] = []
        for p in paths:
            if not any(p.endswith(ext) for ext in CODE_EXTENSIONS):
                continue
            try:
                fu = f"https://api.github.com/repos/{owner}/{repo}/contents/{p}"
                fr = requests.get(fu, headers=GH_HEADERS, timeout=30)
                fr.raise_for_status()
                code_files[p] = base64.b64decode(fr.json()["content"]).decode("utf-8")
            except Exception as exc:
                errors.append(f"{p}: {exc}")

        return [TextContent(type="text",
                            text=json.dumps({"files": code_files,
                                             "errors": errors,
                                             "file_count": len(code_files)}))]

    return [TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]


# ── Entry point ───────────────────────────────────────────────────────────────
async def main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
