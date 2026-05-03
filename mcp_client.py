"""
mcp_client.py
=============
Lightweight synchronous MCP client helper.

Wraps the three MCP servers as simple Python callables so the agent
scripts don't need to know anything about MCP transport details.

NOTE ON GITHUB
--------------
GitHubMCP connects to the APPLICATION GitHub repo (e.g. Princetondawgs).
It NEVER touches the ardouraAI repo (the agent code itself).
Keep APP_GITHUB_OWNER / APP_GITHUB_REPO / APP_GITHUB_BRANCH in .env.

Usage:
    from mcp_client import GitHubMCP, JiraMCP, ConfluenceMCP

    app_github = GitHubMCP()
    files      = app_github.load_codebase("ashishPropt", "Princetondawgs")

Each MCP class spawns its server as a subprocess and communicates via stdio.

FIX: Performs the full MCP handshake before every tool call:
  1. initialize  (request  id=0)
  2. initialized (notification — no id)
  3. tools/call  (request  id=1)
The server rejects any tool call received before initialization is complete.
"""

import json
import subprocess
import sys
import os
from typing import Any


class _MCPClient:
    """
    Minimal synchronous MCP stdio client.

    Sends the full MCP handshake then a single tools/call request,
    all via subprocess stdin/stdout.
    """

    def __init__(self, server_script: str):
        self._script = server_script

    # ── helpers ───────────────────────────────────────────────────────
    @staticmethod
    def _frame(obj: dict) -> bytes:
        """Encode a JSON-RPC object as a newline-terminated UTF-8 line."""
        return (json.dumps(obj) + "\n").encode("utf-8")

    # ── public API ────────────────────────────────────────────────────
    def call(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        """
        Perform the MCP handshake, call one tool, return the parsed result.

        Stdin to the server consists of three newline-delimited JSON frames:
          1. initialize request   (id=0)
          2. initialized notify   (no id — notification)
          3. tools/call request   (id=1)
        """

        # ── 1. initialize ─────────────────────────────────────────────
        init_req = {
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "mcp_client.py", "version": "1.0"},
            },
        }

        # ── 2. initialized notification (no id → server won't reply) ──
        init_notify = {
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
        }

        # ── 3. tools/call ─────────────────────────────────────────────
        tool_req = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        }

        stdin_bytes = (
            self._frame(init_req)
            + self._frame(init_notify)
            + self._frame(tool_req)
        )

        env = {**os.environ}  # inherit all env vars (credentials etc.)
        proc = subprocess.run(
            [sys.executable, self._script],
            input=stdin_bytes,
            capture_output=True,
            env=env,
            timeout=120,
        )

        # ── Parse newline-delimited JSON frames from stdout ────────────
        # We want the frame whose id == 1 (our tools/call response).
        # Frame id=0 is the initialize response — we skip it.
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                frame = json.loads(line)
            except json.JSONDecodeError:
                continue

            if frame.get("id") == 1:
                # Check for JSON-RPC error
                if "error" in frame:
                    err = frame["error"]
                    raise RuntimeError(
                        f"MCP tool '{tool_name}' returned error "
                        f"{err.get('code')}: {err.get('message')}"
                    )
                # Success — extract TextContent
                content_list = frame.get("result", {}).get("content", [])
                if content_list:
                    text = content_list[0].get("text", "")
                    try:
                        return json.loads(text)
                    except json.JSONDecodeError:
                        return text

        # Nothing matched — surface stderr for debugging
        stderr = proc.stderr.decode("utf-8", errors="replace")
        stdout = proc.stdout.decode("utf-8", errors="replace")
        raise RuntimeError(
            f"MCP call to '{tool_name}' failed — no id=1 response found.\n"
            f"stdout: {stdout[:600]}\n"
            f"stderr: {stderr[:400]}"
        )


# ── Resolve server paths relative to this file ────────────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))


class GitHubMCP:
    """
    Wrapper around mcp_github_server.py.

    Connects to the APPLICATION GitHub repo — never to the ardouraAI repo.
    Configure via APP_GITHUB_OWNER / APP_GITHUB_REPO / APP_GITHUB_BRANCH in .env.
    """

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
        """Returns {path: content} dict of all source files in the APPLICATION repo."""
        result = self._client.call("github_load_codebase",
                                   {"owner": owner, "repo": repo, "branch": branch})
        # Guard: if the MCP server returned a raw string (e.g. truncated/error
        # response) instead of a parsed dict, raise clearly rather than letting
        # a confusing 'str has no attribute get' bubble up in the agent.
        if isinstance(result, str):
            raise RuntimeError(
                f"github_load_codebase returned a string instead of a dict. "
                f"MCP server error or stdout overflow. Response: {result[:300]}"
            )
        errors = result.get("errors", [])
        if errors:
            for e in errors:
                print(f"  [AppGitHub-MCP] {e}")
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

    def get_page_by_title(self, space_key: str, title: str) -> str | None:
        """
        Look up a page by space + title and return its text content.
        Returns None if the page does not exist.
        """
        page_id = self.find_page_id(space_key, title)
        if not page_id:
            return None
        return self.get_page(page_id)

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
