"""
JiraConfluenceAIAgent_mcp.py
=============================
Jira ↔ Confluence ↔ Claude ↔ GitHub AI Agent — MCP Edition

Workflow (unchanged from original):
  1. Fetch open Jira tickets         → via JiraMCP server
  2. Pull two Confluence doc pages   → via ConfluenceMCP server
  3. Claude analyses each ticket     → direct Anthropic SDK call
  4. If code change needed, load repo and re-analyse → via GitHubMCP server
  5. Commit code changes to GitHub   → via GitHubMCP server
  6. Create action Jira ticket       → via JiraMCP server

All hard-coded credentials have been REMOVED.
Set the following environment variables (or populate .env and load with python-dotenv):

    ANTHROPIC_API_KEY
    GITHUB_TOKEN
    GITHUB_OWNER               (default: ashishPropt)
    GITHUB_REPO                (default: Princetondawgs)
    GITHUB_BRANCH              (default: main)
    ATLASSIAN_BASE             (e.g. https://yourorg.atlassian.net)
    ATLASSIAN_EMAIL
    ATLASSIAN_API_TOKEN
    JIRA_PROJECT_KEY           (e.g. ADEV)
    CONFLUENCE_DOC_PAGE_ID
    CONFLUENCE_TROUBLESHOOT_PAGE_ID
"""

import os
import re
import json
import textwrap
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv  # pip install python-dotenv

# ── Load .env if present (safe — does not override existing env vars) ─────────
load_dotenv(Path(__file__).parent / ".env")

# ── Import MCP wrappers ───────────────────────────────────────────────────────
from mcp_client import GitHubMCP, JiraMCP, ConfluenceMCP

# ── Config from environment ───────────────────────────────────────────────────
ANTHROPIC_API_KEY   = os.environ["ANTHROPIC_API_KEY"]
GITHUB_OWNER        = os.environ.get("GITHUB_OWNER",  "ashishPropt")
GITHUB_REPO         = os.environ.get("GITHUB_REPO",   "Princetondawgs")
GITHUB_BRANCH       = os.environ.get("GITHUB_BRANCH", "main")
JIRA_PROJECT_KEY    = os.environ.get("JIRA_PROJECT_KEY", "ADEV")
CONFLUENCE_DOC_ID   = os.environ["CONFLUENCE_DOC_PAGE_ID"]
CONFLUENCE_TS_ID    = os.environ["CONFLUENCE_TROUBLESHOOT_PAGE_ID"]

# ── MCP clients (one instance each — reused across all calls) ─────────────────
github    = GitHubMCP()
jira      = JiraMCP()
confluence = ConfluenceMCP()

# ── Claude client ─────────────────────────────────────────────────────────────
claude = Anthropic(api_key=ANTHROPIC_API_KEY)


# ── Claude analysis ───────────────────────────────────────────────────────────

def analyse_jira_with_claude(
    jira_issue:       dict,
    doc_page:         str,
    troubleshoot_page: str,
    codebase:         dict[str, str] | None = None,
) -> dict:
    """
    Send Jira ticket + Confluence docs (+ optional codebase) to Claude.

    Returns:
    {
      "action_type":    "sql|code_change|config|manual|unknown",
      "action_summary": str,
      "action_detail":  str,
      "files_to_change": [{"path": str, "new_content": str}],
      "sql":            str | None,
    }
    """
    codebase_section = ""
    if codebase:
        codebase_section = "\n\n## FULL CODEBASE\n"
        for path, content in codebase.items():
            codebase_section += f"\n### FILE: {path}\n```\n{content}\n```\n"

    prompt = textwrap.dedent(f"""
        You are an expert SRE and senior software engineer.

        Given an open Jira ticket, Confluence docs, and optionally the full codebase,
        determine the EXACT, IMMEDIATELY ACTIONABLE remediation.

        ## JIRA TICKET
        Key:         {jira_issue['key']}
        Type:        {jira_issue['issuetype']}
        Priority:    {jira_issue['priority']}
        Summary:     {jira_issue['summary']}
        Description:
        {jira_issue['description'] or '(no description)'}

        ## CONFLUENCE – SYSTEM DOCUMENTATION
        {doc_page}

        ## CONFLUENCE – TROUBLESHOOTING GUIDE
        {troubleshoot_page}
        {codebase_section}

        ## INSTRUCTIONS

        Respond ONLY with a valid JSON object (no markdown fences, no preamble).

        {{
          "action_type":    "<sql | code_change | config | manual | unknown>",
          "action_summary": "<concise one-liner for a Jira ticket title>",
          "action_detail":  "<step-by-step immediately actionable instructions>",
          "files_to_change": [
            {{"path": "<relative path>", "new_content": "<complete file content>"}}
          ],
          "sql": "<full SQL or null>"
        }}

        Rules:
        - Provide complete SQL in both action_detail and sql field (or null).
        - For code changes, populate files_to_change with FULL file content (not diffs).
        - Set files_to_change to [] if no code change needed.
        - Be specific — no vague instructions.
    """).strip()

    response = claude.messages.create(
        model="claude-opus-4-5",
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = response.content[0].text.strip()
    raw = re.sub(r"^```[a-z]*\n?", "", raw)
    raw = re.sub(r"\n?```$", "", raw)

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {
            "action_type":    "unknown",
            "action_summary": f"AI analysis for {jira_issue['key']}",
            "action_detail":  raw,
            "files_to_change": [],
            "sql":            None,
        }


# ── Per-issue orchestration ───────────────────────────────────────────────────

def process_jira_issue(issue: dict, doc_page: str, troubleshoot_page: str) -> None:
    print(f"\n{'='*60}")
    print(f"  Processing: {issue['key']} – {issue['summary']}")
    print(f"{'='*60}")

    # First pass — analyse without codebase (faster)
    analysis = analyse_jira_with_claude(issue, doc_page, troubleshoot_page)

    # If Claude needs the code, load it via MCP and re-analyse
    if analysis.get("action_type") == "code_change":
        print("  [Agent] Code change detected — loading codebase via MCP …")
        codebase = github.load_codebase(GITHUB_OWNER, GITHUB_REPO, GITHUB_BRANCH)
        print(f"  [Agent] Loaded {len(codebase)} files")
        analysis  = analyse_jira_with_claude(issue, doc_page, troubleshoot_page,
                                              codebase=codebase)

    print(f"  [Agent] Action type : {analysis.get('action_type')}")
    print(f"  [Agent] Summary     : {analysis.get('action_summary')}")

    # ── Commit code changes via MCP ───────────────────────────────────
    committed_files: list[dict] = []
    for file_change in analysis.get("files_to_change", []):
        path        = (file_change.get("path") or "").strip()
        new_content = file_change.get("new_content", "")
        if not path or not new_content:
            continue
        commit_msg = (f"[{issue['key']}] AI-recommended fix: "
                      f"{analysis.get('action_summary', '')[:60]}")
        print(f"  [GitHub-MCP] Committing {path} …")
        try:
            result = github.commit_file(
                GITHUB_OWNER, GITHUB_REPO, path, new_content, commit_msg, GITHUB_BRANCH
            )
            committed_files.append({"path": path, "sha": result.get("commit_sha", "")})
            print(f"  [GitHub-MCP] Committed → {result.get('commit_sha', 'N/A')}")
        except Exception as exc:
            print(f"  [GitHub-MCP] ERROR: {exc}")

    # ── Build Jira description ────────────────────────────────────────
    parts = [
        f"AUTO-GENERATED ACTION TICKET — linked to {issue['key']}\n",
        f"Original: {issue['key']} – {issue['summary']}\n",
        "─" * 50,
        "\nRECOMMENDED ACTION\n",
        analysis.get("action_detail", "See action_summary."),
    ]
    if analysis.get("sql"):
        parts += ["\n\nSQL TO EXECUTE\n", "─" * 50 + "\n", analysis["sql"]]
    if committed_files:
        parts += ["\n\nGITHUB COMMITS\n", "─" * 50 + "\n"]
        for cf in committed_files:
            parts.append(f"• {cf['path']}  (commit: {cf['sha']})\n")

    full_desc   = "\n".join(parts)
    new_summary = f"[ACTION] {analysis.get('action_summary', issue['summary'])}"[:250]

    # ── Create action ticket via MCP ──────────────────────────────────
    print("  [Jira-MCP] Creating action ticket …")
    try:
        new_ticket = jira.create_ticket(
            project_key=JIRA_PROJECT_KEY,
            summary=new_summary,
            description=full_desc,
            issue_type="Task",
            priority=issue.get("priority", "Medium") or "Medium",
            labels=["ai-recommended", "auto-generated"],
        )
        print(f"  [Jira-MCP] Created: {new_ticket['key']}  → {new_ticket.get('self', '')}")
    except Exception as exc:
        print(f"  [Jira-MCP] ERROR: {exc}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("\n" + "═" * 60)
    print("  Jira ↔ Confluence ↔ Claude ↔ GitHub Agent  (MCP Edition)")
    print("═" * 60)

    # 1. Confluence docs via MCP
    print("\n[Step 1] Fetching Confluence pages via MCP …")
    doc_page          = confluence.get_page(CONFLUENCE_DOC_ID)
    troubleshoot_page = confluence.get_page(CONFLUENCE_TS_ID)
    print(f"  doc page       : {len(doc_page):,} chars")
    print(f"  troubleshoot pg: {len(troubleshoot_page):,} chars")

    # 2. Open Jira tickets via MCP
    print("\n[Step 2] Fetching open Jira tickets via MCP …")
    issues: list[dict] = []
    try:
        issues = jira.fetch_open_issues(project_key=JIRA_PROJECT_KEY)
    except Exception as exc:
        print(f"  Project key failed ({exc}), trying without filter …")
    if not issues:
        try:
            issues = jira.fetch_open_issues()
        except Exception as exc:
            print(f"  Could not fetch issues: {exc}")

    print(f"  Found {len(issues)} open ticket(s)")
    if not issues:
        print("  Nothing to process. Exiting.")
        return

    # 3. Process each issue
    print("\n[Step 3] Analysing tickets with Claude …")
    for issue in issues:
        try:
            process_jira_issue(issue, doc_page, troubleshoot_page)
        except Exception as exc:
            print(f"  ERROR processing {issue['key']}: {exc}")

    print("\n" + "═" * 60)
    print("  Agent run complete.")
    print("═" * 60 + "\n")


if __name__ == "__main__":
    main()
