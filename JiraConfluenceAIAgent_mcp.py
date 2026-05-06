"""
JiraConfluenceAIAgent_mcp.py
=============================
Jira ↔ Confluence ↔ Claude ↔ Application-GitHub ↔ MySQL AI Agent — MCP Edition

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
  4. Load DB schema from application MySQL      → via MySQLMCP server
  5. Claude analyses ticket (Confluence+schema) → direct Anthropic SDK call
  6. If answer INCOMPLETE / UNKNOWN             → load Application GitHub + re-analyse
  7. If action_type = "sql"                     → snapshot tables, dry-run, execute
  8. If action_type = "code_change"             → commit to Application GitHub
  9. Create an action/approval ticket in ACR   → via JiraMCP server
 10. Every step is written to ardoura_audit.log → searchable via audit_log_viewer.py

All credentials come from environment variables (or .env file).

Usage:
  python JiraConfluenceAIAgent_mcp.py --issue ADEV-42
  # or via env var fallback:
  JIRA_ISSUE_KEY=ADEV-42 python JiraConfluenceAIAgent_mcp.py
"""

import os
import sys
from pathlib import Path

# ── Load .env FIRST — before any other imports that might snapshot os.environ ─
from dotenv import load_dotenv
_env_path = Path(__file__).parent / ".env"
load_dotenv(_env_path, override=True)

import re
import argparse
import json
import textwrap

from anthropic import Anthropic
from mcp_client import GitHubMCP, JiraMCP, ConfluenceMCP, MySQLMCP
from audit_logger import AuditLogger

# ── Config from environment ───────────────────────────────────────────────────
ANTHROPIC_API_KEY        = os.environ["ANTHROPIC_API_KEY"]

_k = ANTHROPIC_API_KEY
print(f"[Auth] ANTHROPIC_API_KEY = {_k[:20]}...{_k[-6:]}")
if not _k.startswith("sk-ant-"):
    print("[Auth] WARNING: key does not look like a valid Anthropic key — expect 401s")
_gh = os.environ.get("GITHUB_TOKEN", "")
if _gh:
    print(f"[Auth] GITHUB_TOKEN      = {_gh[:15]}...{_gh[-4:]}")
else:
    print("[Auth] WARNING: GITHUB_TOKEN is not set — GitHub API calls will get 401s")
_db_host = os.environ.get("DB_HOST", "")
if _db_host:
    print(f"[Auth] DB_HOST           = {_db_host}")
else:
    print("[Auth] INFO: DB_HOST not set — MySQL features will be skipped gracefully")

APP_GITHUB_OWNER         = os.environ.get("APP_GITHUB_OWNER",  "ashishPropt")
APP_GITHUB_REPO          = os.environ.get("APP_GITHUB_REPO",   "Princetondawgs")
APP_GITHUB_BRANCH        = os.environ.get("APP_GITHUB_BRANCH", "main")

JIRA_SOURCE_PROJECT_KEY  = os.environ.get("JIRA_SOURCE_PROJECT_KEY", "ADEV")
JIRA_ACTION_PROJECT_KEY  = os.environ.get("JIRA_ACTION_PROJECT_KEY", "ACR")
CONFLUENCE_DOC_ID        = os.environ["CONFLUENCE_DOC_PAGE_ID"]
CONFLUENCE_TS_ID         = os.environ["CONFLUENCE_TROUBLESHOOT_PAGE_ID"]
CONFLUENCE_SPACE_KEY     = os.environ.get("CONFLUENCE_SPACE_KEY", "~default")

DB_ENABLED = bool(os.environ.get("DB_HOST", ""))

# ── MCP clients ───────────────────────────────────────────────────────────────
app_github = GitHubMCP()
jira       = JiraMCP()
confluence = ConfluenceMCP()
db         = MySQLMCP() if DB_ENABLED else None

# ── Claude client ─────────────────────────────────────────────────────────────
claude = Anthropic(api_key=ANTHROPIC_API_KEY)


# ── Argument parsing ──────────────────────────────────────────────────────────

def resolve_issue_key() -> str | None:
    parser = argparse.ArgumentParser(
        description="Jira ↔ Confluence ↔ Claude ↔ Application-GitHub ↔ MySQL Agent"
    )
    parser.add_argument("--issue", metavar="ISSUE_KEY", default=None)
    args, _ = parser.parse_known_args()
    if args.issue:
        return args.issue.strip()
    return os.environ.get("JIRA_ISSUE_KEY", "").strip() or None


# ── Claude analysis ───────────────────────────────────────────────────────────

def analyse_with_confluence(
    jira_issue: dict, doc_page: str, troubleshoot_page: str, db_schema: str = ""
) -> dict:
    schema_section = f"\n\n## APPLICATION DATABASE SCHEMA\n{db_schema}" if db_schema else ""
    prompt = textwrap.dedent(f"""
        You are an expert SRE and senior software engineer.

        Analyse the Jira ticket using the Confluence documentation and database schema.
        Set confidence = "low" if you cannot produce a complete, immediately-actionable
        solution — the agent will then load the full application source code.

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
        {schema_section}

        ## INSTRUCTIONS
        You MUST respond ONLY with a single valid JSON object.
        - Do NOT include any text, explanation, or commentary before or after the JSON.
        - Do NOT wrap the JSON in markdown code fences (no ```json or ```).
        - The response MUST start with the character {{ and end with the character }}.
        - All string values must use proper JSON escaping for special characters.
        - If you are unsure, set confidence to "low" rather than guessing.

        Required JSON structure:
        {{
          "confidence":           "<high | low>",
          "action_type":          "<sql | code_change | config | manual | unknown>",
          "action_summary":       "<concise one-liner>",
          "action_detail":        "<step-by-step instructions>",
          "files_to_change":      [{{"path": "<path>", "new_content": "<full content>"}}],
          "sql":                  "<full SQL or null>",
          "sql_tables_affected":  ["<table1>"],
          "knowledge_gaps":       "<what Confluence was missing, or null>"
        }}

        Rules:
        - action_type = "sql" when a database change is needed.
        - If action_type = "sql", always populate sql AND sql_tables_affected.
        - files_to_change = [] if no code change needed.
    """).strip()
    return _call_claude(prompt)


def analyse_with_confluence_and_github(
    jira_issue: dict, doc_page: str, troubleshoot_page: str, codebase: dict[str, str]
) -> dict:
    codebase_section = "\n\n## APPLICATION CODEBASE (source of truth)\n"
    for path, content in codebase.items():
        codebase_section += f"\n### FILE: {path}\n```\n{content}\n```\n"

    prompt = textwrap.dedent(f"""
        You are an expert SRE and senior software engineer.

        Analyse the Jira ticket using Confluence documentation AND the full
        application source code.

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
        You MUST respond ONLY with a single valid JSON object.
        - Do NOT include any text, explanation, or commentary before or after the JSON.
        - Do NOT wrap the JSON in markdown code fences (no ```json or ```).
        - The response MUST start with the character {{ and end with the character }}.
        - All string values must use proper JSON escaping for special characters.
        - files_to_change must contain FULL file content, never diffs or partial content.

        Required JSON structure:
        {{
          "confidence":               "<high | low>",
          "action_type":              "<sql | code_change | config | manual | unknown>",
          "action_summary":           "<concise one-liner>",
          "action_detail":            "<step-by-step instructions>",
          "files_to_change":          [{{"path": "<path>", "new_content": "<full content>"}}],
          "sql":                      "<full SQL or null>",
          "sql_tables_affected":      ["<table1>"],
          "new_confluence_knowledge": "<markdown to add to Confluence, or null>"
        }}

        Rules:
        - files_to_change: include FULL file content (not diffs).
        - new_confluence_knowledge: fill if codebase revealed missing doc.
        - action_type = "sql" when a database change is needed.
    """).strip()
    return _call_claude(prompt)


def build_confluence_update_for_code_change(
    jira_issue: dict, analysis: dict, committed_files: list[dict]
) -> str | None:
    if not committed_files:
        return None
    files_list = "\n".join(
        f"  • {cf['path']}  (commit: {cf['sha']})" for cf in committed_files
    )
    prompt = textwrap.dedent(f"""
        You are a technical writer in an SRE team.
        Write a concise Confluence wiki section (Markdown) documenting WHAT changed
        and WHY for this code commit. Start with a ## heading.

        JIRA: {jira_issue['key']} — {jira_issue['summary']}
        SUMMARY: {analysis.get('action_summary', '')}
        FILES:\n{files_list}
        DETAIL: {analysis.get('action_detail', '')}

        Output ONLY the Markdown section — no preamble, no fences.
        Heading: ## Fix: <short title> ({jira_issue['key']})
    """).strip()
    result = _call_claude_raw(prompt)
    return result.strip() if result else None


def _call_claude(prompt: str) -> dict:
    raw = _call_claude_raw(prompt)

    # Strip accidental markdown fences
    raw = re.sub(r"^```[a-z]*\n?", "", raw)
    raw = re.sub(r"\n?```$", "", raw)

    # ── DEBUG: always log what Claude actually returned ───────────────
    preview = raw[:600].replace("\n", " ")
    print(f"  [Claude Raw Response] ({len(raw)} chars): {preview}{'...' if len(raw) > 600 else ''}")

    # Attempt to extract JSON if Claude added preamble text
    json_match = re.search(r"\{.*\}", raw, re.DOTALL)
    if json_match and not raw.strip().startswith("{"):
        print("  [Claude] Preamble detected — extracting JSON block …")
        raw = json_match.group(0)

    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        # ── DEBUG: log exact parse error position ─────────────────────
        error_pos = e.pos if hasattr(e, "pos") else "unknown"
        snippet   = raw[max(0, int(error_pos or 0) - 40): int(error_pos or 0) + 40] if error_pos != "unknown" else ""
        print(f"  [Claude Parse ERROR] {e}")
        print(f"  [Claude Parse ERROR] Position {error_pos}, context: ...{snippet}...")
        print(f"  [Claude Parse ERROR] Full raw response saved to claude_parse_error.txt")

        # Save full response to file for inspection
        try:
            error_file = Path(__file__).parent / "claude_parse_error.txt"
            error_file.write_text(raw, encoding="utf-8")
        except Exception:
            pass

        return {
            "confidence": "low", "action_type": "unknown",
            "action_summary": "AI analysis (parse error)", "action_detail": raw,
            "files_to_change": [], "sql": None, "sql_tables_affected": [],
        }


def _call_claude_raw(prompt: str) -> str:
    response = claude.messages.create(
        model="claude-sonnet-4-6", max_tokens=4000,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip()


# ── Confluence updater ────────────────────────────────────────────────────────

def append_to_confluence(title: str, new_section: str,
                         log: AuditLogger | None = None) -> None:
    try:
        existing = confluence.get_page_by_title(CONFLUENCE_SPACE_KEY, title) or ""
        updated  = existing + "\n\n" + new_section
        confluence.create_or_update(
            space_key=CONFLUENCE_SPACE_KEY, title=title, content=updated,
        )
        print(f"  [Confluence-MCP] Updated page: '{title}'")
        if log:
            log.confluence_updated(title)
    except Exception as exc:
        print(f"  [Confluence-MCP] WARNING — could not update '{title}': {exc}")
        if log:
            log.warning("confluence_updated",
                        f"Could not update '{title}': {exc}")


# ── Per-issue orchestration ───────────────────────────────────────────────────

def process_jira_issue(issue: dict, doc_page: str, troubleshoot_page: str,
                       log: AuditLogger) -> None:
    print(f"\n{'='*60}")
    print(f"  Processing: {issue['key']} – {issue['summary']}")
    print(f"{'='*60}")

    github_was_loaded = False
    codebase: dict[str, str] = {}

    # ── Load DB schema ────────────────────────────────────────────────
    db_schema = ""
    if db and DB_ENABLED:
        try:
            print("  [MySQL-MCP] Loading database schema …")
            schema_result = db.get_schema()
            if isinstance(schema_result, dict) and "tables" in schema_result:
                lines = [f"Database: {schema_result.get('database', 'unknown')}"]
                for table, cols in schema_result["tables"].items():
                    col_desc = ", ".join(
                        f"{c['column']} {c['type']}{' PK' if c['key'] == 'PRI' else ''}"
                        for c in cols
                    )
                    lines.append(f"  {table}: {col_desc}")
                db_schema = "\n".join(lines)
                table_count = schema_result.get("table_count", 0)
                print(f"  [MySQL-MCP] Schema loaded: {table_count} tables")
                log.db_schema_loaded(table_count)
            else:
                print("  [MySQL-MCP] WARNING: unexpected schema response")
                log.warning("db_schema_loaded", "Unexpected schema response")
        except Exception as exc:
            print(f"  [MySQL-MCP] WARNING: schema load failed ({exc}) — continuing")
            log.warning("db_schema_loaded", str(exc))

    # ── Pass 1: Confluence + schema ───────────────────────────────────
    print("  [Agent] Pass 1: Confluence + DB schema analysis …")
    analysis = analyse_with_confluence(issue, doc_page, troubleshoot_page, db_schema)
    log.claude_analysis(analysis, pass_number=1)
    print(f"  [Agent] Confidence  : {analysis.get('confidence')}")
    print(f"  [Agent] Action type : {analysis.get('action_type')}")

    needs_github = (
        analysis.get("confidence") == "low"
        or analysis.get("action_type") in ("unknown", "code_change")
    )

    # ── Pass 2: Confluence + GitHub ───────────────────────────────────
    if needs_github:
        print(f"  [Agent] Pass 2: Loading Application GitHub "
              f"({APP_GITHUB_OWNER}/{APP_GITHUB_REPO}) …")
        codebase = app_github.load_codebase(
            APP_GITHUB_OWNER, APP_GITHUB_REPO, APP_GITHUB_BRANCH
        )
        print(f"  [Agent] Loaded {len(codebase)} source files")
        github_was_loaded = True
        log.github_codebase_loaded(APP_GITHUB_OWNER, APP_GITHUB_REPO, len(codebase))

        analysis = analyse_with_confluence_and_github(
            issue, doc_page, troubleshoot_page, codebase
        )
        log.claude_analysis(analysis, pass_number=2)
        print(f"  [Agent] (Pass 2) Confidence  : {analysis.get('confidence')}")
        print(f"  [Agent] (Pass 2) Action type : {analysis.get('action_type')}")

        new_knowledge = analysis.get("new_confluence_knowledge")
        if new_knowledge:
            print("  [Agent] New knowledge → updating Confluence …")
            append_to_confluence(
                title="AI-Discovered Knowledge from Application Codebase",
                new_section=(
                    f"### Findings for {issue['key']} – {issue['summary']}\n\n"
                    + new_knowledge
                ),
                log=log,
            )

    print(f"  [Agent] Final summary: {analysis.get('action_summary')}")

    # ── Execute SQL ───────────────────────────────────────────────────
    sql_results: list[dict] = []
    if analysis.get("action_type") == "sql" and analysis.get("sql") and db and DB_ENABLED:
        sql_raw         = analysis["sql"].strip()
        affected_tables = analysis.get("sql_tables_affected", [])

        snapshot      = {}
        total_snapped = 0
        if affected_tables:
            print(f"  [MySQL-MCP] Snapshotting {affected_tables} …")
            try:
                snap_result  = db.begin_snapshot(affected_tables)
                snapshot     = snap_result.get("snapshots", {})
                total_snapped = sum(len(v) for v in snapshot.values())
                print(f"  [MySQL-MCP] Snapshot: {total_snapped} rows")
                log.sql_snapshot(affected_tables, total_snapped)
            except Exception as exc:
                print(f"  [MySQL-MCP] WARNING: snapshot failed ({exc})")
                log.warning("sql_snapshot", str(exc))

        print("  [MySQL-MCP] Dry-run validating SQL …")
        try:
            dry = db.execute_update(sql_raw, dry_run=True)
            if dry.get("error"):
                msg = dry["error"]
                print(f"  [MySQL-MCP] Dry-run FAILED: {msg} — skipping")
                log.sql_dry_run(sql_raw, passed=False, error=msg)
                sql_results.append({"sql": sql_raw, "status": "dry_run_failed",
                                    "error": msg})
            else:
                log.sql_dry_run(sql_raw, passed=True)
                print("  [MySQL-MCP] Dry-run OK — executing …")
                result = db.execute_update(sql_raw)
                if result.get("success"):
                    affected = result.get("affected_rows", 0)
                    print(f"  [MySQL-MCP] SUCCESS: {affected} row(s) affected")
                    log.sql_executed(sql_raw, affected, affected_tables)
                    sql_results.append({
                        "sql": sql_raw, "status": "executed",
                        "affected_rows": affected, "snapshot": snapshot,
                    })
                    append_to_confluence(
                        title="Application Changelog (AI-Managed)",
                        new_section=(
                            f"## DB Change: {analysis.get('action_summary')} "
                            f"({issue['key']})\n\n"
                            f"**SQL executed:**\n```sql\n{sql_raw}\n```\n\n"
                            f"**Rows affected:** {affected}\n\n"
                            f"**Tables:** {', '.join(affected_tables)}\n\n"
                            f"**Pre-change snapshot rows:** {total_snapped}"
                        ),
                        log=log,
                    )
                else:
                    err = result.get("error", "unknown error")
                    print(f"  [MySQL-MCP] FAILED (rolled back): {err}")
                    log.sql_failed(sql_raw, err)
                    sql_results.append({"sql": sql_raw, "status": "failed",
                                        "error": err})
        except Exception as exc:
            print(f"  [MySQL-MCP] ERROR: {exc}")
            log.error("sql_executed", str(exc))
            sql_results.append({"sql": sql_raw, "status": "error", "error": str(exc)})

    elif analysis.get("action_type") == "sql" and not DB_ENABLED:
        print("  [MySQL-MCP] DB not configured — SQL in ACR ticket only")
        log.warning("sql_executed", "DB not configured — SQL skipped",
                    {"sql_preview": (analysis.get("sql") or "")[:200]})

    # ── Commit code changes ───────────────────────────────────────────
    committed_files: list[dict] = []
    if analysis.get("action_type") == "code_change":
        if not github_was_loaded:
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
                sha = result.get("commit_sha", "")
                committed_files.append({"path": path, "sha": sha})
                print(f"  [AppGitHub-MCP] Committed → {sha or 'N/A'}")
                log.github_committed(path, sha,
                                     APP_GITHUB_OWNER, APP_GITHUB_REPO)
            except Exception as exc:
                print(f"  [AppGitHub-MCP] ERROR: {exc}")
                log.github_commit_failed(path, str(exc))

        if committed_files:
            print("  [Agent] Code committed → updating Confluence …")
            change_doc = build_confluence_update_for_code_change(
                issue, analysis, committed_files
            )
            if change_doc:
                append_to_confluence(
                    title="Application Changelog (AI-Managed)",
                    new_section=change_doc,
                    log=log,
                )

    # ── Build ACR ticket ──────────────────────────────────────────────
    parts = [
        f"AUTO-GENERATED ACTION TICKET — linked to {issue['key']}\n",
        f"Original: {issue['key']} – {issue['summary']}\n",
        f"Source: {JIRA_SOURCE_PROJECT_KEY}  |  Approval: {JIRA_ACTION_PROJECT_KEY}\n",
        "─" * 50,
        "\nRECOMMENDED ACTION\n",
        analysis.get("action_detail", "See action_summary."),
        "\n\n⚠️  This ticket requires review and approval before any action is taken.",
    ]
    if sql_results:
        parts += ["\n\nSQL EXECUTION RESULTS\n", "─" * 50 + "\n"]
        for sr in sql_results:
            parts.append(
                f"Status : {sr['status']}\n"
                f"SQL    : {sr['sql'][:200]}\n"
                + (f"Rows   : {sr.get('affected_rows')}\n"
                   if sr["status"] == "executed" else "")
                + (f"Error  : {sr.get('error')}\n" if sr.get("error") else "")
            )
    elif analysis.get("sql"):
        parts += ["\n\nSQL TO EXECUTE (DB not configured — run manually)\n",
                  "─" * 50 + "\n", analysis["sql"]]
    if committed_files:
        parts += ["\n\nAPPLICATION GITHUB COMMITS\n", "─" * 50 + "\n"]
        for cf in committed_files:
            parts.append(f"• {cf['path']}  (commit: {cf['sha']})\n")
        parts.append(
            f"\nRepo: {APP_GITHUB_OWNER}/{APP_GITHUB_REPO}@{APP_GITHUB_BRANCH}\n"
        )

    full_desc   = "\n".join(parts)
    new_summary = f"[ACTION] {analysis.get('action_summary', issue['summary'])}"[:250]

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
        key = new_ticket["key"]
        print(f"  [Jira-MCP] Created: {key}  → {new_ticket.get('self', '')}")
        log.jira_ticket_created(key, JIRA_ACTION_PROJECT_KEY, new_summary)
    except Exception as exc:
        print(f"  [Jira-MCP] ERROR: {exc}")
        log.error("jira_ticket_created", str(exc))


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("\n" + "═" * 60)
    print("  Jira ↔ Confluence ↔ Claude ↔ GitHub ↔ MySQL Agent  (MCP Edition)")
    print(f"  Source: {JIRA_SOURCE_PROJECT_KEY}  →  Approval: {JIRA_ACTION_PROJECT_KEY}")
    print(f"  App repo: {APP_GITHUB_OWNER}/{APP_GITHUB_REPO}@{APP_GITHUB_BRANCH}")
    print(f"  DB: {'enabled (' + os.environ.get('DB_HOST','') + ')' if DB_ENABLED else 'not configured'}")
    print("═" * 60)

    issue_key = resolve_issue_key()
    if not issue_key:
        print("\n[ERROR] No Jira issue key provided.")
        print("  Usage: python JiraConfluenceAIAgent_mcp.py --issue ADEV-42")
        sys.exit(1)

    if not issue_key.upper().startswith(f"{JIRA_SOURCE_PROJECT_KEY}-"):
        print(f"\n[WARNING] Issue key '{issue_key}' not in project "
              f"'{JIRA_SOURCE_PROJECT_KEY}'. Proceeding anyway …")

    log = AuditLogger(issue_key=issue_key)

    try:
        print("\n[Step 1] Fetching Confluence pages …")
        doc_page          = confluence.get_page(CONFLUENCE_DOC_ID)
        troubleshoot_page = confluence.get_page(CONFLUENCE_TS_ID)
        log.confluence_fetched(CONFLUENCE_DOC_ID, len(doc_page))
        log.confluence_fetched(CONFLUENCE_TS_ID,  len(troubleshoot_page))
        print(f"  doc: {len(doc_page):,} chars  |  "
              f"troubleshoot: {len(troubleshoot_page):,} chars")

        print(f"\n[Step 2] Fetching Jira issue {issue_key} …")
        try:
            issue = jira.get_issue(issue_key)
            log.jira_fetched(issue)
        except Exception as exc:
            log.error("jira_fetched", str(exc))
            print(f"  [ERROR] Could not fetch {issue_key}: {exc}")
            sys.exit(1)
        print(f"  [{issue['key']}] {issue['summary']} (status: {issue['status']})")

        print(f"\n[Step 3] Analysing {issue['key']} …")
        process_jira_issue(issue, doc_page, troubleshoot_page, log)

        log.agent_end("success")

    except Exception as exc:
        log.error("agent_end", str(exc))
        print(f"\n[ERROR] Unhandled exception: {exc}")
        raise

    print("\n" + "═" * 60)
    print("  Agent run complete.")
    print(f"  Audit log: {Path(__file__).parent / 'ardoura_audit.log'}")
    print("═" * 60 + "\n")


if __name__ == "__main__":
    main()
