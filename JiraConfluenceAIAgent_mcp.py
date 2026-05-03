"""
JiraConfluenceAIAgent_mcp.py
=============================
Jira ↔ Confluence ↔ Claude ↔ Application-GitHub AI Agent — MCP Edition

NOTE ON GITHUB REPOS
--------------------
  • APPLICATION GitHub  = the repo the agent acts ON (e.g. Princetondawgs).
    Controlled by APP_GITHUB_OWNER / APP_GITHUB_REPO / APP_GITHUB_BRANCH.
  • ARDOURA-AI GitHub   = THIS repo (the agent code itself).
    The agent NEVER reads or writes its own source repo.

Workflow:
  1. Accept a single Jira issue key via --issue argument
  2. Fetch ONLY that ticket                     → via JiraMCP server
  3. Pull two Confluence documentation pages    → via ConfluenceMCP server
  4. Claude analyses ticket using Confluence    → direct Anthropic SDK call
  5. If answer is INCOMPLETE / UNKNOWN          → load Application GitHub
       re-analyse with codebase                 → update Confluence with new knowledge
  6. If code change needed (either pass)        → commit to Application GitHub
       update Confluence to document the change
  7. Create an action/approval ticket in ACR   → via JiraMCP server

All credentials come from environment variables (or .env file).
See .env for the full list of required variables.

Usage:
  python JiraConfluenceAIAgent_mcp.py --issue ADEV-42
  # or via env var fallback:
  JIRA_ISSUE_KEY=ADEV-42 python JiraConfluenceAIAgent_mcp.py
"""

import os
import re
import sys
import argparse
import json
import textwrap
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

# Load .env (safe — does not override existing env vars)
load_dotenv(Path(__file__).parent / ".env")

from mcp_client import GitHubMCP, JiraMCP, ConfluenceMCP

# ── Config from environment ───────────────────────────────────────────────────
ANTHROPIC_API_KEY        = os.environ["ANTHROPIC_API_KEY"]

# APPLICATION GitHub — the repo the agent acts ON (e.g. Princetondawgs)
# DO NOT point these at the ardouraAI repo.
APP_GITHUB_OWNER         = os.environ.get("APP_GITHUB_OWNER",  "ashishPropt")
APP_GITHUB_REPO          = os.environ.get("APP_GITHUB_REPO",   "Princetondawgs")
APP_GITHUB_BRANCH        = os.environ.get("APP_GITHUB_BRANCH", "main")

JIRA_SOURCE_PROJECT_KEY  = os.environ.get("JIRA_SOURCE_PROJECT_KEY", "ADEV")
JIRA_ACTION_PROJECT_KEY  = os.environ.get("JIRA_ACTION_PROJECT_KEY", "ACR")
CONFLUENCE_DOC_ID        = os.environ["CONFLUENCE_DOC_PAGE_ID"]
CONFLUENCE_TS_ID         = os.environ["CONFLUENCE_TROUBLESHOOT_PAGE_ID"]
CONFLUENCE_SPACE_KEY     = os.environ.get("CONFLUENCE_SPACE_KEY", "~default")

# ── MCP clients ───────────────────────────────────────────────────────────────
app_github = GitHubMCP()      # Application GitHub (Princetondawgs, etc.)
jira       = JiraMCP()
confluence = ConfluenceMCP()

# ── Claude client ─────────────────────────────────────────────────────────────
claude = Anthropic(api_key=ANTHROPIC_API_KEY)


# ── Argument parsing ──────────────────────────────────────────────────────────

def resolve_issue_key() -> str | None:
    parser = argparse.ArgumentParser(
        description="Jira ↔ Confluence ↔ Claude ↔ Application-GitHub AI Agent (MCP Edition)"
    )
    parser.add_argument(
        "--issue",
        metavar="ISSUE_KEY",
        help="Jira issue key to process (e.g. ADEV-42)",
        default=None,
    )
    args, _ = parser.parse_known_args()
    if args.issue:
        return args.issue.strip()
    return os.environ.get("JIRA_ISSUE_KEY", "").strip() or None


# ── Claude: primary analysis ──────────────────────────────────────────────────

def analyse_with_confluence(
    jira_issue:        dict,
    doc_page:          str,
    troubleshoot_page: str,
) -> dict:
    """
    First-pass analysis using only Confluence documentation.
    Returns a structured action dict that also includes a 'confidence' field:
      - 'high'   : Confluence contained enough info to fully resolve the ticket.
      - 'low'    : Confluence was insufficient; Application GitHub should be consulted.
    """
    prompt = textwrap.dedent(f"""
        You are an expert SRE and senior software engineer.

        Analyse the Jira ticket using ONLY the Confluence documentation provided.
        If the documentation does not contain enough information to produce a
        complete, specific, immediately-actionable solution, set confidence to "low"
        so the agent knows to consult the application's source code next.

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

        ## INSTRUCTIONS
        Respond ONLY with a valid JSON object (no markdown fences, no preamble).

        {{
          "confidence":      "<high | low>",
          "action_type":     "<sql | code_change | config | manual | unknown>",
          "action_summary":  "<concise one-liner for a Jira ticket title>",
          "action_detail":   "<step-by-step immediately actionable instructions>",
          "files_to_change": [
            {{"path": "<relative path>", "new_content": "<complete file content>"}}
          ],
          "sql": "<full SQL or null>",
          "knowledge_gaps":  "<brief description of what was missing from Confluence, or null>"
        }}

        Rules:
        - confidence = "low" if documentation does not fully answer the ticket.
        - Set files_to_change to [] if no code change is needed.
        - Be specific — no vague instructions.
    """).strip()

    return _call_claude(prompt)


def analyse_with_confluence_and_github(
    jira_issue:        dict,
    doc_page:          str,
    troubleshoot_page: str,
    codebase:          dict[str, str],
) -> dict:
    """
    Second-pass analysis: Confluence + full Application GitHub codebase.
    Used when (a) first pass had low confidence, or (b) a code change is needed.
    """
    codebase_section = "\n\n## APPLICATION CODEBASE (source of truth)\n"
    for path, content in codebase.items():
        codebase_section += f"\n### FILE: {path}\n```\n{content}\n```\n"

    prompt = textwrap.dedent(f"""
        You are an expert SRE and senior software engineer.

        Analyse the Jira ticket using the Confluence documentation AND the full
        application source code provided below.

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
          "confidence":        "<high | low>",
          "action_type":       "<sql | code_change | config | manual | unknown>",
          "action_summary":    "<concise one-liner for a Jira ticket title>",
          "action_detail":     "<step-by-step immediately actionable instructions>",
          "files_to_change":   [
            {{"path": "<relative path>", "new_content": "<complete file content>"}}
          ],
          "sql":               "<full SQL or null>",
          "new_confluence_knowledge": "<markdown section to ADD to Confluence docs based on what
                                        you learned from the codebase that was missing, or null>"
        }}

        Rules:
        - For code changes, populate files_to_change with FULL file content (not diffs).
        - Set files_to_change to [] if no code change is needed.
        - new_confluence_knowledge: always populate this with any architectural or
          operational knowledge found in the codebase that was absent from Confluence.
    """).strip()

    return _call_claude(prompt)


def build_confluence_update_for_code_change(
    jira_issue:      dict,
    analysis:        dict,
    committed_files: list[dict],
) -> str | None:
    """
    Ask Claude to produce a Confluence snippet that documents code changes
    that were just committed to the Application GitHub repo.
    Returns a markdown string or None.
    """
    if not committed_files:
        return None

    files_list = "\n".join(
        f"  • {cf['path']}  (commit: {cf['sha']})" for cf in committed_files
    )

    prompt = textwrap.dedent(f"""
        You are a technical writer embedded in an SRE team.

        The following code changes were just committed to the application GitHub repo
        as part of resolving a Jira ticket.  Write a concise Confluence wiki section
        (in Markdown) that documents WHAT changed and WHY, so the documentation
        stays in sync with the codebase.

        ## JIRA TICKET
        {jira_issue['key']} — {jira_issue['summary']}

        ## CHANGE SUMMARY
        {analysis.get('action_summary', '')}

        ## CHANGED FILES
        {files_list}

        ## ACTION DETAIL
        {analysis.get('action_detail', '')}

        Output ONLY the Markdown wiki section — no preamble, no fences.
        Start with a ## heading like "## Fix: <short title> ({jira_issue['key']})"
    """).strip()

    result = _call_claude_raw(prompt)
    return result.strip() if result else None


def _call_claude(prompt: str) -> dict:
    """Call Claude and parse a JSON response. Returns raw dict on parse failure."""
    raw = _call_claude_raw(prompt)
    raw = re.sub(r"^```[a-z]*\n?", "", raw)
    raw = re.sub(r"\n?```$", "", raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {
            "confidence":    "low",
            "action_type":   "unknown",
            "action_summary": "AI analysis (parse error)",
            "action_detail":  raw,
            "files_to_change": [],
            "sql":            None,
        }


def _call_claude_raw(prompt: str) -> str:
    """Call Claude and return the raw text response."""
    response = claude.messages.create(
        model="claude-opus-4-5",
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip()


# ── Confluence updater ────────────────────────────────────────────────────────

def append_to_confluence(title: str, new_section: str) -> None:
    """
    Read the existing Confluence page with `title`, append `new_section`,
    and write it back.  Creates the page if it does not exist.
    """
    try:
        existing = confluence.get_page_by_title(CONFLUENCE_SPACE_KEY, title) or ""
        updated  = existing + "\n\n" + new_section
        confluence.create_or_update(
            space_key=CONFLUENCE_SPACE_KEY,
            title=title,
            content=updated,
        )
        print(f"  [Confluence-MCP] Updated page: '{title}'")
    except Exception as exc:
        print(f"  [Confluence-MCP] WARNING — could not update '{title}': {exc}")


# ── Per-issue orchestration ───────────────────────────────────────────────────

def process_jira_issue(issue: dict, doc_page: str, troubleshoot_page: str) -> None:
    print(f"\n{'='*60}")
    print(f"  Processing: {issue['key']} – {issue['summary']}")
    print(f"{'='*60}")

    github_was_loaded  = False
    codebase: dict[str, str] = {}

    # ── PASS 1: Confluence-only analysis ──────────────────────────────
    print("  [Agent] Pass 1: Confluence-only analysis …")
    analysis = analyse_with_confluence(issue, doc_page, troubleshoot_page)
    print(f"  [Agent] Confidence  : {analysis.get('confidence')}")
    print(f"  [Agent] Action type : {analysis.get('action_type')}")

    needs_github = (
        analysis.get("confidence") == "low"
        or analysis.get("action_type") in ("unknown", "code_change")
    )

    # ── PASS 2 (if needed): Load Application GitHub + re-analyse ──────
    if needs_github:
        print(f"  [Agent] Pass 2: Loading Application GitHub "
              f"({APP_GITHUB_OWNER}/{APP_GITHUB_REPO}) …")
        codebase = app_github.load_codebase(
            APP_GITHUB_OWNER, APP_GITHUB_REPO, APP_GITHUB_BRANCH
        )
        print(f"  [Agent] Loaded {len(codebase)} source files")
        github_was_loaded = True

        analysis = analyse_with_confluence_and_github(
            issue, doc_page, troubleshoot_page, codebase
        )
        print(f"  [Agent] (Pass 2) Confidence  : {analysis.get('confidence')}")
        print(f"  [Agent] (Pass 2) Action type : {analysis.get('action_type')}")

        # Backfill Confluence with knowledge found in the codebase
        new_knowledge = analysis.get("new_confluence_knowledge")
        if new_knowledge:
            print("  [Agent] New knowledge from GitHub → updating Confluence …")
            append_to_confluence(
                title="AI-Discovered Knowledge from Application Codebase",
                new_section=(
                    f"### Findings for {issue['key']} – {issue['summary']}\n\n"
                    + new_knowledge
                ),
            )

    print(f"  [Agent] Final summary: {analysis.get('action_summary')}")

    # ── Commit code changes to Application GitHub ─────────────────────
    committed_files: list[dict] = []
    if analysis.get("action_type") == "code_change":
        if not github_was_loaded:
            # Shouldn't happen after Pass-2 logic above, but be safe
            codebase = app_github.load_codebase(
                APP_GITHUB_OWNER, APP_GITHUB_REPO, APP_GITHUB_BRANCH
            )

        for file_change in analysis.get("files_to_change", []):
            path        = (file_change.get("path") or "").strip()
            new_content = file_change.get("new_content", "")
            if not path or not new_content:
                continue
            commit_msg = (f"[{issue['key']}] AI-recommended fix: "
                          f"{analysis.get('action_summary', '')[:60]}")
            print(f"  [AppGitHub-MCP] Committing {path} …")
            try:
                result = app_github.commit_file(
                    APP_GITHUB_OWNER, APP_GITHUB_REPO,
                    path, new_content, commit_msg, APP_GITHUB_BRANCH,
                )
                committed_files.append({
                    "path": path,
                    "sha":  result.get("commit_sha", ""),
                })
                print(f"  [AppGitHub-MCP] Committed → {result.get('commit_sha', 'N/A')}")
            except Exception as exc:
                print(f"  [AppGitHub-MCP] ERROR: {exc}")

        # ── Update Confluence to document what changed in GitHub ───────
        if committed_files:
            print("  [Agent] Code committed → updating Confluence documentation …")
            change_doc = build_confluence_update_for_code_change(
                issue, analysis, committed_files
            )
            if change_doc:
                append_to_confluence(
                    title="Application Changelog (AI-Managed)",
                    new_section=change_doc,
                )

    # ── Build action ticket description ───────────────────────────────
    parts = [
        f"AUTO-GENERATED ACTION TICKET — linked to {issue['key']}\n",
        f"Original: {issue['key']} – {issue['summary']}\n",
        f"Source Project: {JIRA_SOURCE_PROJECT_KEY}  |  Approval Project: {JIRA_ACTION_PROJECT_KEY}\n",
        "─" * 50,
        "\nRECOMMENDED ACTION\n",
        analysis.get("action_detail", "See action_summary."),
        "\n\n⚠️  This ticket requires review and approval before any action is taken.",
    ]
    if analysis.get("sql"):
        parts += ["\n\nSQL TO EXECUTE\n", "─" * 50 + "\n", analysis["sql"]]
    if committed_files:
        parts += ["\n\nAPPLICATION GITHUB COMMITS\n", "─" * 50 + "\n"]
        for cf in committed_files:
            parts.append(f"• {cf['path']}  (commit: {cf['sha']})\n")
        parts.append(
            f"\nCommitted to: {APP_GITHUB_OWNER}/{APP_GITHUB_REPO}@{APP_GITHUB_BRANCH}\n"
        )

    full_desc   = "\n".join(parts)
    new_summary = f"[ACTION] {analysis.get('action_summary', issue['summary'])}"[:250]

    # ── Create action ticket via Jira MCP ─────────────────────────────
    print(f"  [Jira-MCP] Creating approval ticket in {JIRA_ACTION_PROJECT_KEY} …")
    try:
        new_ticket = jira.create_ticket(
            project_key=JIRA_ACTION_PROJECT_KEY,
            summary=new_summary,
            description=full_desc,
            issue_type="Task",
            priority=issue.get("priority", "Medium") or "Medium",
            labels=["ai-recommended", "auto-generated", "pending-approval"],
        )
        print(f"  [Jira-MCP] Created: {new_ticket['key']}  → {new_ticket.get('self', '')}")
    except Exception as exc:
        print(f"  [Jira-MCP] ERROR: {exc}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("\n" + "═" * 60)
    print("  Jira ↔ Confluence ↔ Claude ↔ Application-GitHub Agent  (MCP Edition)")
    print(f"  Source: {JIRA_SOURCE_PROJECT_KEY}  →  Approval: {JIRA_ACTION_PROJECT_KEY}")
    print(f"  Application repo: {APP_GITHUB_OWNER}/{APP_GITHUB_REPO}@{APP_GITHUB_BRANCH}")
    print("═" * 60)

    issue_key = resolve_issue_key()
    if not issue_key:
        print("\n[ERROR] No Jira issue key provided.")
        print("  Usage:  python JiraConfluenceAIAgent_mcp.py --issue ADEV-42")
        print("    or:   JIRA_ISSUE_KEY=ADEV-42 python JiraConfluenceAIAgent_mcp.py")
        sys.exit(1)

    expected_prefix = f"{JIRA_SOURCE_PROJECT_KEY}-"
    if not issue_key.upper().startswith(expected_prefix):
        print(f"\n[WARNING] Issue key '{issue_key}' does not belong to project "
              f"'{JIRA_SOURCE_PROJECT_KEY}'. Proceeding anyway …")

    # Step 1 — Confluence docs
    print("\n[Step 1] Fetching Confluence pages via Confluence MCP …")
    doc_page          = confluence.get_page(CONFLUENCE_DOC_ID)
    troubleshoot_page = confluence.get_page(CONFLUENCE_TS_ID)
    print(f"  doc page       : {len(doc_page):,} chars")
    print(f"  troubleshoot pg: {len(troubleshoot_page):,} chars")

    # Step 2 — Fetch Jira ticket
    print(f"\n[Step 2] Fetching Jira issue {issue_key} via Jira MCP …")
    try:
        issue = jira.get_issue(issue_key)
    except Exception as exc:
        print(f"  [ERROR] Could not fetch issue {issue_key}: {exc}")
        sys.exit(1)
    print(f"  Fetched: [{issue['key']}] {issue['summary']} (status: {issue['status']})")

    # Step 3 — Process
    print(f"\n[Step 3] Analysing {issue['key']} with Claude …")
    try:
        process_jira_issue(issue, doc_page, troubleshoot_page)
    except Exception as exc:
        print(f"  ERROR processing {issue['key']}: {exc}")

    print("\n" + "═" * 60)
    print("  Agent run complete.")
    print("═" * 60 + "\n")


if __name__ == "__main__":
    main()
