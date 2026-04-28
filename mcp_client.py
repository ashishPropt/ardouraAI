"""
mcp_client.py
=============
Lightweight synchronous MCP client helper.

Wraps the three MCP servers as simple Python callables so the agent
scripts don't need to know anything about MCP transport details.

Usage:
    from mcp_client import GitHubMCP, JiraMCP, ConfluenceMCP

    github = GitHubMCP()
    files  = github.load_codebase("ashishPropt", "ArdouraAI")

Each MCP class spawns its server as a subprocess and communicates via stdio.
"""

import json
import subprocess
import sys
import os
from typing import Any


class _MCPClient:
    """
    Minimal synchronous MCP stdio client.
    Sends a single tools/call request and reads the response.
    """

    def __init__(self, server_script: str):
        self._script = server_script

    def call(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        """Call a tool on the server; returns the parsed JSON result."""
        # Build a minimal JSON-RPC 2.0 request as the MCP host would
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        }
        req_bytes = (json.dumps(request) + "\n").encode("utf-8")

        # Spawn server, pass request, read response
        env = {**os.environ}  # inherit all env vars (includes credentials)
        proc = subprocess.run(
            [sys.executable, self._script],
            input=req_bytes,
            capture_output=True,
            env=env,
            timeout=120,
        )

        # The MCP server writes multiple newline-delimited JSON frames.
        # The response to our call is the frame with matching id.
        for line in proc.stdout.splitlines():
            if not line.strip():
                continue
            try:
                frame = json.loads(line)
            except json.JSONDecodeError:
                continue
            if frame.get("id") == 1 and "result" in frame:
                # result.content is a list of TextContent objects
                content_list = frame["result"].get("content", [])
                if content_list:
                    text = content_list[0].get("text", "")
                    try:
                        return json.loads(text)
                    except json.JSONDecodeError:
                        return text

        # If we get here the server may have errored
        stderr = proc.stderr.decode("utf-8", errors="replace")
        raise RuntimeError(
            f"MCP call to '{tool_name}' failed.\n"
            f"stderr: {stderr[:400]}"
        )


# ── Resolve server paths relative to this file ────────────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))


class GitHubMCP:
    """Wrapper around mcp_github_server.py."""

    def __init__(self):
        self._client = _MCPClient(os.path.join(_HERE, "mcp_github_server.py"))

    def get_repo_tree(self, owner: str, repo: str, branch: str = "main") -> list[str]:
        return self._client.call("github_get_repo_tree",
                                 {"owner": owner, "repo": repo, "branch": branch})

    def get_file(self, owner: str, repo: str, path: str) -> dict:
        """Returns {content, sha, path}."""
        return self._client.call("github_get_file",
                                 {"owner": owner, "repo": repo, "path": path})

    def commit_file(self, owner: str, repo: str, path: str,
                    content: str, commit_message: str,
                    branch: str = "main") -> dict:
        """Returns {committed, commit_sha, path}."""
        return self._client.call("github_commit_file", {
            "owner": owner, "repo": repo, "path": path,
            "content": content, "commit_message": commit_message, "branch": branch,
        })

    def load_codebase(self, owner: str, repo: str,
                      branch: str = "main") -> dict[str, str]:
        """Returns {path: content} dict of all source files."""
        result = self._client.call("github_load_codebase",
                                   {"owner": owner, "repo": repo, "branch": branch})
        return result.get("files", {})


class JiraMCP:
    """Wrapper around mcp_jira_server.py."""

    def __init__(self):
        self._client = _MCPClient(os.path.join(_HERE, "mcp_jira_server.py"))

    def search_issues(self, jql: str, max_results: int = 50) -> list[dict]:
        return self._client.call("jira_search_issues",
                                 {"jql": jql, "max_results": max_results})

    def create_ticket(self, project_key: str, summary: str, description: str,
                      issue_type: str = "Task", priority: str = "Medium",
                      labels: list[str] = None) -> dict:
        """Returns {key, id, self}."""
        return self._client.call("jira_create_ticket", {
            "project_key": project_key,
            "summary":     summary,
            "description": description,
            "issue_type":  issue_type,
            "priority":    priority,
            "labels":      labels or [],
        })

    def get_issue(self, issue_key: str) -> dict:
        return self._client.call("jira_get_issue", {"issue_key": issue_key})

    def fetch_open_issues(self, project_key: str = None,
                          max_results: int = 50) -> list[dict]:
        jql = "status != Done AND status != Closed ORDER BY created DESC"
        if project_key:
            jql = f"project = {project_key} AND {jql}"
        return self.search_issues(jql, max_results)


class ConfluenceMCP:
    """Wrapper around mcp_confluence_server.py."""

    def __init__(self):
        self._client = _MCPClient(os.path.join(_HERE, "mcp_confluence_server.py"))

    def get_page(self, page_id: str) -> str:
        """Returns the plain-text content of the page (with title header)."""
        result = self._client.call("confluence_get_page", {"page_id": page_id})
        return result.get("text", "")

    def find_page_id(self, space_key: str, title: str) -> str | None:
        result = self._client.call("confluence_find_page_id",
                                   {"space_key": space_key, "title": title})
        return result.get("page_id")

    def create_or_update(self, space_key: str, title: str, content: str) -> dict:
        """Returns {action, page_id, status_code}."""
        return self._client.call("confluence_create_or_update", {
            "space_key": space_key,
            "title":     title,
            "content":   content,
        })
