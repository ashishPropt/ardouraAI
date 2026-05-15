#!/usr/bin/env python3
"""
JiraConfluenceAIAgent_mcp.py
============================
AI agent that:
  1. Reads an ADEV Jira issue
  2. Checks Confluence docs for context
  3. Gets GitHub repo tree -> asks Claude which files are relevant
     -> reads those files' ACTUAL CONTENTS before generating the fix
  4. Uses Claude to generate exact search/replace code changes
  5. Creates an ACR ticket linked to the ADEV as 'fixes'
  6. If execution_safe=True (LOW blast radius):
       - Commits the code changes to GitHub
       - Transitions ACR to In Progress then Done
     Otherwise:
       - Leaves ACR Open, assigns to operator for manual review
  7. Adds detailed comments to both ADEV and ACR

Usage:
    python JiraConfluenceAIAgent_mcp.py --issue ADEV-42
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

import anthropic
from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY   = os.environ.get("ANTHROPIC_API_KEY", "")
ATLASSIAN_BASE      = os.environ.get("ATLASSIAN_BASE", "")
ATLASSIAN_EMAIL     = os.environ.get("ATLASSIAN_EMAIL", "")
ATLASSIAN_API_TOKEN = os.environ.get("ATLASSIAN_API_TOKEN", "")
JIRA_ACTION_PROJECT = os.environ.get("JIRA_ACTION_PROJECT_KEY", "ACR")
CONFLUENCE_DOC_PAGE = os.environ.get("CONFLUENCE_DOC_PAGE_ID", "")
CONFLUENCE_TS_PAGE  = os.environ.get("CONFLUENCE_TROUBLESHOOT_PAGE_ID", "")
APP_GITHUB_OWNER    = os.environ.get("APP_GITHUB_OWNER", "ashishPropt")
APP_GITHUB_REPO     = os.environ.get("APP_GITHUB_REPO", "Princetondawgs")
APP_GITHUB_BRANCH   = os.environ.get("APP_GITHUB_BRANCH", "main")
OPERATOR_ACCOUNT_ID = os.environ.get("JIRA_ASSIGNEE_ACCOUNT_ID", "")

# Max chars to include per file so we don't blow the context window
MAX_FILE_CHARS = 8000

BASE_DIR = str(Path(__file__).parent)
FULL_ENV = {**os.environ}


def _server(script: str) -> StdioServerParameters:
    return StdioServerParameters(
        command="python", args=[script], cwd=BASE_DIR, env=FULL_ENV
    )


async def call_tool(session: ClientSession, tool: str, args: dict) -> str:
    result = await session.call_tool(tool, args)
    if not result.content:
        print(f"[Agent] WARNING: tool '{tool}' returned empty content", flush=True)
        return "{}"
    text = result.content[0].text
    if not text or not text.strip():
        print(f"[Agent] WARNING: tool '{tool}' returned blank text", flush=True)
        return "{}"
    return text


def _ask_claude_which_files(claude: anthropic.Anthropic,
                             issue: dict, repo_tree: list[str]) -> list[str]:
    """Ask Claude to identify which files from the tree are relevant to this issue."""
    tree_text = "\n".join(repo_tree)
    prompt = f"""You are a senior software engineer.

Given this Jira issue:
Summary: {issue.get('summary')}
Description: {issue.get('description', '')}

And this GitHub repository file tree:
{tree_text}

Identify the files that would need to be READ to understand and fix this issue.
Return ONLY a JSON array of file paths (max 5 files), e.g.:
["css/style.css", "includes/header.php"]

Choose the most relevant files. For UI/styling issues pick CSS and template files.
For logic issues pick PHP/JS files. Respond with ONLY the JSON array."""

    r = claude.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}]
    )
    raw = r.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    try:
        files = json.loads(raw)
        # Only return files that actually exist in the tree
        return [f for f in files if f in repo_tree][:5]
    except Exception:
        return []


async def run_agent(issue_key: str) -> None:
    print(f"[Agent] Starting for {issue_key}", flush=True)
    print(f"[Agent] Target repo: {APP_GITHUB_OWNER}/{APP_GITHUB_REPO}", flush=True)
    print(f"[Agent] ATLASSIAN_BASE={ATLASSIAN_BASE}", flush=True)
    print(f"[Agent] TOKEN set={'yes' if ATLASSIAN_API_TOKEN else 'NO - MISSING'}",
          flush=True)

    if not ATLASSIAN_BASE or not ATLASSIAN_EMAIL or not ATLASSIAN_API_TOKEN:
        print("[Agent] ERROR: Atlassian credentials missing", flush=True)
        sys.exit(1)

    claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # ── Step 1: Read the ADEV issue ──────────────────────────────────────────
    print(f"[Agent] Fetching {issue_key} from Jira ...", flush=True)
    try:
        async with stdio_client(_server("mcp_jira_server.py")) as (r, w):
            async with ClientSession(r, w) as jira:
                await jira.initialize()
                issue_raw = await call_tool(jira, "jira_get_issue",
                                            {"issue_key": issue_key})
        issue = json.loads(issue_raw)
    except Exception as e:
        print(f"[Agent] ERROR fetching issue: {e}", flush=True)
        raise
    print(f"[Agent] Issue: {issue.get('summary')}", flush=True)

    # ── Step 2: Read Confluence docs ─────────────────────────────────────────
    confluence_context = ""
    if CONFLUENCE_DOC_PAGE or CONFLUENCE_TS_PAGE:
        print("[Agent] Reading Confluence docs ...", flush=True)
        try:
            async with stdio_client(_server("mcp_confluence_server.py")) as (r, w):
                async with ClientSession(r, w) as conf:
                    await conf.initialize()
                    if CONFLUENCE_DOC_PAGE:
                        doc = await call_tool(conf, "confluence_get_page",
                                              {"page_id": CONFLUENCE_DOC_PAGE})
                        confluence_context += f"\n\n=== Documentation ===\n{doc[:3000]}"
                    if CONFLUENCE_TS_PAGE:
                        ts = await call_tool(conf, "confluence_get_page",
                                             {"page_id": CONFLUENCE_TS_PAGE})
                        confluence_context += f"\n\n=== Troubleshooting ===\n{ts[:3000]}"
        except Exception as e:
            print(f"[Agent] WARNING: Confluence read failed: {e}", flush=True)
            confluence_context = f"(Confluence read failed: {e})"
    else:
        confluence_context = "(No Confluence pages configured)"

    # ── Step 3a: Get repo tree ──────────────────────────────────────────────────
    print("[Agent] Reading GitHub repo tree ...", flush=True)
    repo_tree = []
    try:
        async with stdio_client(_server("mcp_github_server.py")) as (r, w):
            async with ClientSession(r, w) as gh:
                await gh.initialize()
                tree_raw = await call_tool(gh, "github_get_repo_tree", {
                    "owner":  APP_GITHUB_OWNER,
                    "repo":   APP_GITHUB_REPO,
                    "branch": APP_GITHUB_BRANCH,
                })
                repo_tree = json.loads(tree_raw)
        print(f"[Agent] Repo tree: {len(repo_tree)} files", flush=True)
    except Exception as e:
        print(f"[Agent] WARNING: GitHub tree read failed: {e}", flush=True)

    # ── Step 3b: Ask Claude which files are relevant ──────────────────────────
    relevant_files = []
    if repo_tree:
        print("[Agent] Asking Claude which files to read ...", flush=True)
        relevant_files = _ask_claude_which_files(claude, issue, repo_tree)
        print(f"[Agent] Relevant files identified: {relevant_files}", flush=True)

    # ── Step 3c: Read actual file contents ────────────────────────────────────
    file_contents: dict[str, str] = {}
    if relevant_files:
        print(f"[Agent] Reading {len(relevant_files)} file(s) from GitHub ...",
              flush=True)
        try:
            async with stdio_client(_server("mcp_github_server.py")) as (r, w):
                async with ClientSession(r, w) as gh:
                    await gh.initialize()
                    for file_path in relevant_files:
                        try:
                            file_raw = await call_tool(gh, "github_get_file", {
                                "owner": APP_GITHUB_OWNER,
                                "repo":  APP_GITHUB_REPO,
                                "path":  file_path,
                            })
                            file_data = json.loads(file_raw)
                            content   = file_data.get("content", "")
                            file_contents[file_path] = content[:MAX_FILE_CHARS]
                            print(f"[Agent] Read {file_path} "
                                  f"({len(content)} chars)", flush=True)
                        except Exception as fe:
                            print(f"[Agent] WARNING: could not read {file_path}: {fe}",
                                  flush=True)
        except Exception as e:
            print(f"[Agent] WARNING: GitHub file read failed: {e}", flush=True)

    # Build github context with actual file contents
    github_context = (
        f"Repository: {APP_GITHUB_OWNER}/{APP_GITHUB_REPO} "
        f"(branch: {APP_GITHUB_BRANCH})\n"
        f"Total files: {len(repo_tree)}\n\n"
    )
    if file_contents:
        github_context += "=== FILE CONTENTS (actual source code) ===\n"
        for path, content in file_contents.items():
            github_context += f"\n--- {path} ---\n{content}\n"
    else:
        github_context += "File tree:\n" + "\n".join(repo_tree[:100])

    # ── Step 4: Claude analysis + code generation ───────────────────────────
    print("[Agent] Sending to Claude for analysis + code generation ...",
          flush=True)

    prompt = f"""You are an expert software engineer and change manager.

You have been given a Jira issue AND the actual source code of the relevant files.
Analyse the issue, then generate the EXACT code changes needed.

## ADEV Issue
Key: {issue.get('key')}
Summary: {issue.get('summary')}
Description: {issue.get('description', '(no description)')}
Status: {issue.get('status')}
Priority: {issue.get('priority')}

## Confluence Documentation
{confluence_context}

## GitHub Repository + File Contents
{github_context}

## Your Task
Produce a JSON response with these exact fields:

{{
  "acr_summary": "One-line summary for the ACR ticket (max 100 chars)",
  "acr_description": "Full description: root cause, files to change, steps, risks, rollback plan",
  "execution_safe": true or false,
  "execution_rationale": "Why it is or isn't safe to auto-execute",
  "risk_level": "LOW | MEDIUM | HIGH",
  "estimated_effort": "e.g. 30 mins, 2 hours",
  "steps": ["step 1", "step 2", ...],
  "code_changes": [
    {{
      "file_path": "relative/path/to/file.ext",
      "description": "what change to make in this file",
      "search": "exact string from the file content above to find (must be unique)",
      "replace": "exact replacement string"
    }}
  ]
}}

CRITICAL rules for code_changes:
- You HAVE the actual file contents above - use them
- search must be copied EXACTLY from the file content (including whitespace)
- search must be unique within the file
- replace is the complete new version of that string
- File paths must match exactly as shown above
- If the file content was not provided, set execution_safe=false

Rules for execution_safe=true (ALL must be true):
- risk_level is LOW
- No database schema changes
- No destructive operations
- No external service configuration changes
- Change is fully reversible via a revert commit
- code_changes is non-empty with valid search/replace pairs from actual file content

Respond with ONLY the JSON object, no markdown fences, no explanation."""

    response = claude.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}]
    )

    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()

    try:
        analysis = json.loads(raw)
    except json.JSONDecodeError:
        print(f"[Agent] WARNING: Claude response not valid JSON:\n{raw[:300]}",
              flush=True)
        analysis = {
            "acr_summary":         f"Fix for {issue_key}: {issue.get('summary', '')[:60]}",
            "acr_description":     raw,
            "execution_safe":      False,
            "execution_rationale": "Could not parse Claude response - manual review required",
            "risk_level":          "HIGH",
            "estimated_effort":    "Unknown",
            "steps":               ["Manual review required"],
            "code_changes":        [],
        }

    print(f"[Agent] Analysis: Risk={analysis.get('risk_level')} "
          f"Safe={analysis.get('execution_safe')} "
          f"Changes={len(analysis.get('code_changes', []))}",
          flush=True)

    # ── Step 5: Execute code changes if safe ─────────────────────────────────
    execution_log = []
    code_changes  = analysis.get("code_changes", [])
    auto_exec     = analysis.get("execution_safe", False) and bool(code_changes)

    if auto_exec:
        print(f"[Agent] Executing {len(code_changes)} code change(s) on GitHub ...",
              flush=True)
        try:
            async with stdio_client(_server("mcp_github_server.py")) as (r, w):
                async with ClientSession(r, w) as gh:
                    await gh.initialize()
                    for change in code_changes:
                        file_path   = change["file_path"]
                        search_str  = change["search"]
                        replace_str = change["replace"]
                        description = change.get("description", "")

                        # Use cached content if we already read it
                        if file_path in file_contents:
                            old_content = file_contents[file_path]
                        else:
                            print(f"[Agent] Reading {file_path} ...", flush=True)
                            file_raw = await call_tool(gh, "github_get_file", {
                                "owner": APP_GITHUB_OWNER,
                                "repo":  APP_GITHUB_REPO,
                                "path":  file_path,
                            })
                            file_data   = json.loads(file_raw)
                            old_content = file_data["content"]

                        if search_str not in old_content:
                            msg = (f"SKIPPED {file_path}: search string not found "
                                   f"- marking for manual review")
                            print(f"[Agent] WARNING: {msg}", flush=True)
                            execution_log.append(msg)
                            auto_exec = False
                            continue

                        new_content = old_content.replace(search_str, replace_str, 1)
                        commit_msg  = (
                            f"fix({issue_key}): "
                            f"{description or analysis.get('acr_summary', '')}\n\n"
                            f"Auto-executed by ArdouraAI agent\n"
                            f"ADEV: {issue_key}"
                        )

                        print(f"[Agent] Committing {file_path} ...", flush=True)
                        commit_raw = await call_tool(gh, "github_commit_file", {
                            "owner":          APP_GITHUB_OWNER,
                            "repo":           APP_GITHUB_REPO,
                            "path":           file_path,
                            "content":        new_content,
                            "commit_message": commit_msg,
                            "branch":         APP_GITHUB_BRANCH,
                        })
                        commit_data = json.loads(commit_raw)
                        sha = commit_data.get("commit_sha", "")[:10]
                        msg = f"Committed {file_path} -> {sha}"
                        print(f"[Agent] {msg}", flush=True)
                        execution_log.append(msg)

        except Exception as e:
            print(f"[Agent] ERROR during code execution: {e}", flush=True)
            execution_log.append(f"Execution failed: {e}")
            auto_exec = False
    else:
        if analysis.get("execution_safe") and not code_changes:
            analysis["execution_safe"] = False
            analysis["execution_rationale"] += " (No code changes generated)"

    # ── Step 6: Create ACR + link + comments ────────────────────────────────
    print(f"[Agent] Creating ACR in {JIRA_ACTION_PROJECT} ...", flush=True)

    steps_text    = "\n".join(
        f"{i+1}. {s}" for i, s in enumerate(analysis.get("steps", []))
    )
    exec_log_text = "\n".join(execution_log) if execution_log else "(no changes executed)"
    changes_text  = "\n".join(
        f"- {c.get('file_path')}: {c.get('description')}"
        for c in code_changes
    ) if code_changes else "(none)"

    acr_description = (
        f"AUTO-GENERATED by ArdouraAI Agent\n\n"
        f"Linked ADEV: {issue_key}\n"
        f"Original summary: {issue.get('summary')}\n\n"
        f"--- ANALYSIS ---\n{analysis.get('acr_description', '')}\n\n"
        f"--- CODE CHANGES ---\n{changes_text}\n\n"
        f"--- EXECUTION LOG ---\n{exec_log_text}\n\n"
        f"--- STEPS ---\n{steps_text}\n\n"
        f"--- RISK ASSESSMENT ---\n"
        f"Risk Level: {analysis.get('risk_level')}\n"
        f"Estimated Effort: {analysis.get('estimated_effort')}\n"
        f"Auto-Executed: {auto_exec}\n"
        f"Rationale: {analysis.get('execution_rationale')}\n"
    )

    async with stdio_client(_server("mcp_jira_server.py")) as (r, w):
        async with ClientSession(r, w) as jira:
            await jira.initialize()

            acr_raw = await call_tool(jira, "jira_create_ticket", {
                "project_key": JIRA_ACTION_PROJECT,
                "summary":     analysis.get("acr_summary",
                               f"ACR: {issue.get('summary', '')[:80]}"),
                "description": acr_description,
                "issue_type":  "Task",
                "priority":    "High" if analysis.get("risk_level") == "HIGH" else "Medium",
                "labels":      ["auto-generated", "ardoura-ai",
                                f"risk-{analysis.get('risk_level', 'unknown').lower()}",
                                "executed" if auto_exec else "needs-review"],
            })
            acr     = json.loads(acr_raw)
            acr_key = acr["key"]
            print(f"[Agent] ACR created: {acr_key}", flush=True)

            await call_tool(jira, "jira_link_issues", {
                "link_type":   "Fixes",
                "inward_key":  acr_key,
                "outward_key": issue_key,
            })
            print(f"[Agent] Linked {acr_key} -> {issue_key}", flush=True)

            adev_comment = (
                f"ArdouraAI analysed this issue and created ACR *{acr_key}*.\n\n"
                f"*Target Repo:* {APP_GITHUB_OWNER}/{APP_GITHUB_REPO}\n"
                f"*Files Read:* {', '.join(file_contents.keys()) or 'none'}\n"
                f"*Risk Level:* {analysis.get('risk_level')}\n"
                f"*Estimated Effort:* {analysis.get('estimated_effort')}\n"
                f"*Auto-Executed:* "
                f"{'Yes - code committed to GitHub' if auto_exec else 'No - manual review needed'}\n"
                f"*Rationale:* {analysis.get('execution_rationale')}\n\n"
                + (f"*Commits:*\n{exec_log_text}\n\n" if auto_exec else "")
                + f"View ACR: {ATLASSIAN_BASE}/browse/{acr_key}"
            )
            await call_tool(jira, "jira_add_comment", {
                "issue_key": issue_key,
                "comment":   adev_comment,
            })
            print(f"[Agent] Comment added to {issue_key}", flush=True)

            if OPERATOR_ACCOUNT_ID:
                await call_tool(jira, "jira_assign_issue", {
                    "issue_key":  acr_key,
                    "account_id": OPERATOR_ACCOUNT_ID,
                })

            if auto_exec:
                print(f"[Agent] Transitioning {acr_key} In Progress -> Done ...",
                      flush=True)
                await call_tool(jira, "jira_transition_issue", {
                    "issue_key": acr_key, "transition_name": "In Progress"
                })
                await call_tool(jira, "jira_transition_issue", {
                    "issue_key": acr_key, "transition_name": "Done"
                })
                await call_tool(jira, "jira_add_comment", {
                    "issue_key": acr_key,
                    "comment":   (
                        f"ArdouraAI auto-executed this change.\n\n"
                        f"*Commits:*\n{exec_log_text}\n\n"
                        f"*Files changed:*\n{changes_text}\n\n"
                        f"Original issue: {ATLASSIAN_BASE}/browse/{issue_key}"
                    ),
                })
            else:
                await call_tool(jira, "jira_add_comment", {
                    "issue_key": acr_key,
                    "comment":   (
                        f"ArdouraAI flagged for manual review.\n\n"
                        f"*Reason:* {analysis.get('execution_rationale')}\n"
                        f"*Risk:* {analysis.get('risk_level')}\n\n"
                        + (f"*Execution errors:*\n{exec_log_text}\n\n"
                           if execution_log else "")
                        + f"Review steps and transition to In Progress when ready.\n\n"
                        f"Original issue: {ATLASSIAN_BASE}/browse/{issue_key}"
                    ),
                })

    status = "EXECUTED" if auto_exec else "NEEDS REVIEW"
    print(f"[Agent] DONE. ADEV={issue_key} ACR={acr_key} Status={status}",
          flush=True)


def main():
    parser = argparse.ArgumentParser(description="ArdouraAI Jira Agent")
    parser.add_argument("--issue", required=True, help="e.g. ADEV-42")
    args = parser.parse_args()
    asyncio.run(run_agent(args.issue))


if __name__ == "__main__":
    main()
