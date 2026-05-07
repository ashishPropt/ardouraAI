"""
JiraConfluenceAIAgent_mcp.py
=============================
Jira <-> Confluence <-> Claude <-> Application-GitHub <-> MySQL AI Agent (MCP Edition)

Fixes applied:
  - max_tokens raised to 16000 (prevents JSON truncation on code_change)
  - Retry with exponential backoff on 429 rate limit errors
  - Codebase filtered to relevant files only (reduces input tokens)
  - Confluence docs truncated to 8000 chars each (reduces input tokens)
  - Jira smart-commit tag in GitHub commit message (links commit to Jira ticket)

Usage:
  python JiraConfluenceAIAgent_mcp.py --issue ADEV-42
  JIRA_ISSUE_KEY=ADEV-42 python JiraConfluenceAIAgent_mcp.py
"""

import os
import sys
import time
import re
import argparse
import json
import textwrap
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env", override=True)

from anthropic import Anthropic, RateLimitError
from mcp_client import GitHubMCP, JiraMCP, ConfluenceMCP, MySQLMCP
from audit_logger import AuditLogger

ANTHROPIC_API_KEY        = os.environ["ANTHROPIC_API_KEY"]
_k = ANTHROPIC_API_KEY
print(f"[Auth] ANTHROPIC_API_KEY = {_k[:20]}...{_k[-6:]}")
if not _k.startswith("sk-ant-"):
    print("[Auth] WARNING: key does not look like a valid Anthropic key -- expect 401s")
_gh = os.environ.get("GITHUB_TOKEN", "")
if _gh: print(f"[Auth] GITHUB_TOKEN      = {_gh[:15]}...{_gh[-4:]}")
else:   print("[Auth] WARNING: GITHUB_TOKEN not set -- GitHub calls will get 401s")
_db_host = os.environ.get("DB_HOST", "")
if _db_host: print(f"[Auth] DB_HOST           = {_db_host}")
else:        print("[Auth] INFO: DB_HOST not set -- MySQL features skipped")

APP_GITHUB_OWNER         = os.environ.get("APP_GITHUB_OWNER",  "ashishPropt")
APP_GITHUB_REPO          = os.environ.get("APP_GITHUB_REPO",   "Princetondawgs")
APP_GITHUB_BRANCH        = os.environ.get("APP_GITHUB_BRANCH", "main")
JIRA_SOURCE_PROJECT_KEY  = os.environ.get("JIRA_SOURCE_PROJECT_KEY", "ADEV")
JIRA_ACTION_PROJECT_KEY  = os.environ.get("JIRA_ACTION_PROJECT_KEY", "ACR")
CONFLUENCE_DOC_ID        = os.environ["CONFLUENCE_DOC_PAGE_ID"]
CONFLUENCE_TS_ID         = os.environ["CONFLUENCE_TROUBLESHOOT_PAGE_ID"]
CONFLUENCE_SPACE_KEY     = os.environ.get("CONFLUENCE_SPACE_KEY", "~default")
DB_ENABLED               = bool(os.environ.get("DB_HOST", ""))

MAX_DOC_CHARS      = 8_000
MAX_CODEBASE_CHARS = 15_000
MAX_RETRIES        = 4
RETRY_BASE_SECS    = 65

app_github = GitHubMCP()
jira       = JiraMCP()
confluence = ConfluenceMCP()
db         = MySQLMCP() if DB_ENABLED else None
claude     = Anthropic(api_key=ANTHROPIC_API_KEY)


def resolve_issue_key() -> str | None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--issue", metavar="ISSUE_KEY", default=None)
    args, _ = parser.parse_known_args()
    if args.issue:
        return args.issue.strip()
    return os.environ.get("JIRA_ISSUE_KEY", "").strip() or None


def _truncate(text: str, max_chars: int, label: str = "") -> str:
    if len(text) <= max_chars:
        return text
    suffix = f"\n\n... [TRUNCATED -- original {len(text):,} chars, showing first {max_chars:,}]"
    if label:
        print(f"  [TokenBudget] {label} truncated {len(text):,} -> {max_chars:,} chars")
    return text[:max_chars] + suffix


def _filter_codebase(codebase: dict[str, str], issue_summary: str,
                     max_chars: int = MAX_CODEBASE_CHARS) -> dict[str, str]:
    keywords = set(re.findall(r"\w+", issue_summary.lower()))
    ui_keywords = {"css", "style", "logo", "image", "ui", "layout", "design",
                   "colour", "color", "font", "size", "width", "height", "nav",
                   "header", "footer", "button", "menu"}
    is_ui_ticket = bool(keywords & ui_keywords)

    def score(path: str, content: str) -> int:
        s = 0
        path_lower = path.lower()
        content_lower = content.lower()
        for kw in keywords:
            if kw in path_lower:    s += 3
            if kw in content_lower: s += 1
        if is_ui_ticket and path_lower.endswith((".css", ".js")):
            s += 10
        return s

    scored = sorted(
        [(score(p, c), p, c) for p, c in codebase.items()],
        key=lambda x: x[0], reverse=True
    )
    selected: dict[str, str] = {}
    total = 0
    for _, path, content in scored:
        if total + len(content) > max_chars:
            print(f"  [TokenBudget] Codebase capped -- skipping {path}")
            continue
        selected[path] = content
        total += len(content)
    print(f"  [TokenBudget] Codebase: {len(selected)}/{len(codebase)} files, {total:,} chars")
    return selected


def _build_commit_message(issue_key: str, action_summary: str) -> str:
    """
    Build a GitHub commit message with a Jira smart-commit tag.

    Jira's GitHub integration recognises the format:
        <ISSUE-KEY> #comment <text>

    This causes Jira to:
      - Link the commit to the issue (shows in Development panel)
      - Post the comment text on the issue timeline

    Example:
        ADEV-42 #comment AI fix: decrease logo size to 58px

        Resolves: ADEV-42
        AI-generated commit via ArdouraAI agent.
    """
    summary_short = action_summary[:72]   # keep subject line short
    return (
        f"{issue_key} #comment AI fix: {summary_short}\n\n"
        f"Resolves: {issue_key}\n"
        f"AI-generated commit via ArdouraAI agent."
    )


def _call_claude_raw(prompt: str) -> str:
    wait = RETRY_BASE_SECS
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = claude.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=16000,
                messages=[{"role": "user", "content": prompt}],
            )
            if response.stop_reason == "max_tokens":
                print("  [Claude WARNING] Response TRUNCATED at 16000 tokens!")
            return response.content[0].text.strip()
        except RateLimitError:
            if attempt == MAX_RETRIES:
                print(f"  [Claude] Rate limit -- all {MAX_RETRIES} retries exhausted.")
                raise
            print(f"  [Claude] 429 on attempt {attempt}/{MAX_RETRIES}. Waiting {wait}s ...")
            time.sleep(wait)
            wait = min(wait * 2, 300)
    raise RuntimeError("_call_claude_raw: unreachable")


def _call_claude(prompt: str) -> dict:
    raw = _call_claude_raw(prompt)
    raw = re.sub(r"^```[a-z]*\n?", "", raw)
    raw = re.sub(r"\n?```$", "", raw)
    preview = raw[:600].replace("\n", " ")
    print(f"  [Claude Raw] ({len(raw)} chars): {preview}{'...' if len(raw) > 600 else ''}")
    if not raw.strip().startswith("{"):
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            print("  [Claude] Preamble -- extracting JSON ...")
            raw = m.group(0)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        pos = e.pos if hasattr(e, "pos") else 0
        print(f"  [Claude Parse ERROR] {e} pos={pos}")
        try:
            (Path(__file__).parent / "claude_parse_error.txt").write_text(raw, encoding="utf-8")
        except Exception:
            pass
        return {
            "confidence": "low", "action_type": "unknown",
            "action_summary": "AI analysis (parse error)", "action_detail": raw,
            "files_to_change": [], "sql": None, "sql_tables_affected": [],
        }


def analyse_with_confluence(
    jira_issue: dict, doc_page: str, troubleshoot_page: str, db_schema: str = ""
) -> dict:
    schema_section = f"\n\n## DATABASE SCHEMA\n{db_schema}" if db_schema else ""
    doc_t = _truncate(doc_page,          MAX_DOC_CHARS, "Confluence doc")
    ts_t  = _truncate(troubleshoot_page, MAX_DOC_CHARS, "Confluence troubleshoot")
    prompt = textwrap.dedent(f"""
        You are an expert SRE and senior software engineer.
        Analyse the Jira ticket. Set confidence="low" if incomplete.

        ## JIRA TICKET
        Key: {jira_issue['key']} | Type: {jira_issue['issuetype']} | Priority: {jira_issue['priority']}
        Summary: {jira_issue['summary']}
        Description: {jira_issue['description'] or '(none)'}

        ## CONFLUENCE - SYSTEM DOCUMENTATION
        {doc_t}

        ## CONFLUENCE - TROUBLESHOOTING GUIDE
        {ts_t}
        {schema_section}

        ## OUTPUT RULES
        Respond ONLY with a single valid JSON object.
        No text before or after. No markdown fences. Start with {{ end with }}.
        Proper JSON escaping.

        {{"confidence":"<high|low>","action_type":"<sql|code_change|config|manual|unknown>",
         "action_summary":"<one-liner>","action_detail":"<step-by-step>",
         "files_to_change":[{{"path":"<path>","new_content":"<full file>"}}],
         "sql":"<SQL or null>","sql_tables_affected":["<table>"],"knowledge_gaps":"<or null>"}}
    """).strip()
    return _call_claude(prompt)


def analyse_with_confluence_and_github(
    jira_issue: dict, doc_page: str, troubleshoot_page: str, codebase: dict[str, str]
) -> dict:
    doc_t    = _truncate(doc_page,          MAX_DOC_CHARS, "Confluence doc")
    ts_t     = _truncate(troubleshoot_page, MAX_DOC_CHARS, "Confluence troubleshoot")
    filtered = _filter_codebase(codebase, jira_issue.get("summary", ""))
    codebase_section = "\n\n## APPLICATION CODEBASE (relevant files)\n"
    for path, content in filtered.items():
        codebase_section += f"\n### {path}\n```\n{content}\n```\n"
    prompt = textwrap.dedent(f"""
        You are an expert SRE and senior software engineer.
        Analyse using Confluence docs AND the application source code.

        ## JIRA TICKET
        Key: {jira_issue['key']} | Type: {jira_issue['issuetype']} | Priority: {jira_issue['priority']}
        Summary: {jira_issue['summary']}
        Description: {jira_issue['description'] or '(none)'}

        ## CONFLUENCE - SYSTEM DOCUMENTATION
        {doc_t}

        ## CONFLUENCE - TROUBLESHOOTING GUIDE
        {ts_t}
        {codebase_section}

        ## OUTPUT RULES
        Respond ONLY with a single valid JSON object.
        No text before or after. No markdown fences. Start with {{ end with }}.
        files_to_change must have FULL file content (not diffs).

        {{"confidence":"<high|low>","action_type":"<sql|code_change|config|manual|unknown>",
         "action_summary":"<one-liner>","action_detail":"<step-by-step>",
         "files_to_change":[{{"path":"<path>","new_content":"<full file>"}}],
         "sql":"<SQL or null>","sql_tables_affected":["<table>"],
         "new_confluence_knowledge":"<markdown or null>"}}
    """).strip()
    return _call_claude(prompt)


def build_confluence_update_for_code_change(
    jira_issue: dict, analysis: dict, committed_files: list[dict]
) -> str | None:
    if not committed_files:
        return None
    files_list = "\n".join(f"  * {cf['path']} (commit: {cf['sha']})" for cf in committed_files)
    prompt = textwrap.dedent(f"""
        Write a concise Confluence wiki section (Markdown) for this commit.
        JIRA: {jira_issue['key']} -- {jira_issue['summary']}
        SUMMARY: {analysis.get('action_summary', '')}
        FILES:\n{files_list}
        DETAIL: {analysis.get('action_detail', '')[:1000]}
        Output ONLY Markdown. Heading: ## Fix: <title> ({jira_issue['key']})
    """).strip()
    result = _call_claude_raw(prompt)
    return result.strip() if result else None


def append_to_confluence(title: str, new_section: str, log=None) -> None:
    try:
        existing = confluence.get_page_by_title(CONFLUENCE_SPACE_KEY, title) or ""
        confluence.create_or_update(
            space_key=CONFLUENCE_SPACE_KEY, title=title,
            content=existing + "\n\n" + new_section,
        )
        print(f"  [Confluence-MCP] Updated: '{title}'")
        if log: log.confluence_updated(title)
    except Exception as exc:
        print(f"  [Confluence-MCP] WARNING -- '{title}': {exc}")
        if log: log.warning("confluence_updated", str(exc))


def process_jira_issue(issue: dict, doc_page: str, troubleshoot_page: str,
                       log: AuditLogger) -> None:
    print(f"\n{'='*60}\n  Processing: {issue['key']} - {issue['summary']}\n{'='*60}")

    github_was_loaded = False
    codebase: dict[str, str] = {}
    db_schema = ""

    if db and DB_ENABLED:
        try:
            print("  [MySQL-MCP] Loading schema ...")
            sr = db.get_schema()
            if isinstance(sr, dict) and "tables" in sr:
                lines = [f"Database: {sr.get('database', '?')}"]
                for tbl, cols in sr["tables"].items():
                    lines.append("  " + tbl + ": " + ", ".join(
                        f"{c['column']} {c['type']}{' PK' if c['key']=='PRI' else ''}"
                        for c in cols))
                db_schema = "\n".join(lines)
                tc = sr.get("table_count", 0)
                print(f"  [MySQL-MCP] Schema: {tc} tables")
                log.db_schema_loaded(tc)
        except Exception as exc:
            print(f"  [MySQL-MCP] WARNING: {exc}")
            log.warning("db_schema_loaded", str(exc))

    print("  [Agent] Pass 1: Confluence + DB schema ...")
    analysis = analyse_with_confluence(issue, doc_page, troubleshoot_page, db_schema)
    log.claude_analysis(analysis, pass_number=1)
    print(f"  [Agent] Confidence  : {analysis.get('confidence')}")
    print(f"  [Agent] Action type : {analysis.get('action_type')}")

    needs_github = (
        analysis.get("confidence") == "low"
        or analysis.get("action_type") in ("unknown", "code_change")
    )

    if needs_github:
        print(f"  [Agent] Pass 2: Loading GitHub {APP_GITHUB_OWNER}/{APP_GITHUB_REPO} ...")
        codebase = app_github.load_codebase(APP_GITHUB_OWNER, APP_GITHUB_REPO, APP_GITHUB_BRANCH)
        print(f"  [Agent] Loaded {len(codebase)} source files")
        github_was_loaded = True
        log.github_codebase_loaded(APP_GITHUB_OWNER, APP_GITHUB_REPO, len(codebase))
        analysis = analyse_with_confluence_and_github(issue, doc_page, troubleshoot_page, codebase)
        log.claude_analysis(analysis, pass_number=2)
        print(f"  [Agent] (Pass 2) Confidence  : {analysis.get('confidence')}")
        print(f"  [Agent] (Pass 2) Action type : {analysis.get('action_type')}")
        nk = analysis.get("new_confluence_knowledge")
        if nk:
            append_to_confluence(
                "AI-Discovered Knowledge from Application Codebase",
                f"### Findings for {issue['key']} - {issue['summary']}\n\n{nk}",
                log=log,
            )

    print(f"  [Agent] Final summary: {analysis.get('action_summary')}")

    sql_results: list[dict] = []
    if analysis.get("action_type") == "sql" and analysis.get("sql") and db and DB_ENABLED:
        sql_raw = analysis["sql"].strip()
        tables  = analysis.get("sql_tables_affected", [])
        snapshot = {}; total_snapped = 0
        if tables:
            try:
                snap = db.begin_snapshot(tables)
                snapshot = snap.get("snapshots", {})
                total_snapped = sum(len(v) for v in snapshot.values())
                log.sql_snapshot(tables, total_snapped)
            except Exception as exc:
                log.warning("sql_snapshot", str(exc))
        try:
            dry = db.execute_update(sql_raw, dry_run=True)
            if dry.get("error"):
                msg = dry["error"]
                log.sql_dry_run(sql_raw, passed=False, error=msg)
                sql_results.append({"sql": sql_raw, "status": "dry_run_failed", "error": msg})
            else:
                log.sql_dry_run(sql_raw, passed=True)
                result = db.execute_update(sql_raw)
                if result.get("success"):
                    aff = result.get("affected_rows", 0)
                    log.sql_executed(sql_raw, aff, tables)
                    sql_results.append({"sql": sql_raw, "status": "executed",
                                        "affected_rows": aff, "snapshot": snapshot})
                else:
                    err = result.get("error", "unknown")
                    log.sql_failed(sql_raw, err)
                    sql_results.append({"sql": sql_raw, "status": "failed", "error": err})
        except Exception as exc:
            log.error("sql_executed", str(exc))
            sql_results.append({"sql": sql_raw, "status": "error", "error": str(exc)})
    elif analysis.get("action_type") == "sql" and not DB_ENABLED:
        log.warning("sql_executed", "DB not configured",
                    {"sql_preview": (analysis.get("sql") or "")[:200]})

    committed_files: list[dict] = []
    if analysis.get("action_type") == "code_change":
        if not github_was_loaded:
            codebase = app_github.load_codebase(APP_GITHUB_OWNER, APP_GITHUB_REPO, APP_GITHUB_BRANCH)
        for fc in analysis.get("files_to_change", []):
            path = (fc.get("path") or "").strip()
            nc   = fc.get("new_content", "")
            if not path or not nc: continue

            # Smart-commit: Jira key tag links this commit to the issue in Jira's
            # Development panel and posts a comment on the ticket timeline.
            commit_msg = _build_commit_message(issue["key"], analysis.get("action_summary", ""))

            print(f"  [AppGitHub-MCP] Committing {path} ...")
            try:
                r   = app_github.commit_file(APP_GITHUB_OWNER, APP_GITHUB_REPO,
                                             path, nc, commit_msg, APP_GITHUB_BRANCH)
                sha = r.get("commit_sha", "")
                committed_files.append({"path": path, "sha": sha})
                print(f"  [AppGitHub-MCP] -> {sha or 'N/A'}")
                log.github_committed(path, sha, APP_GITHUB_OWNER, APP_GITHUB_REPO)
            except Exception as exc:
                print(f"  [AppGitHub-MCP] ERROR: {exc}")
                log.github_commit_failed(path, str(exc))
        if committed_files:
            cd = build_confluence_update_for_code_change(issue, analysis, committed_files)
            if cd:
                append_to_confluence("Application Changelog (AI-Managed)", cd, log=log)

    parts = [
        f"AUTO-GENERATED -- linked to {issue['key']}\n",
        f"Original: {issue['key']} - {issue['summary']}\n",
        f"Source: {JIRA_SOURCE_PROJECT_KEY}  |  Approval: {JIRA_ACTION_PROJECT_KEY}\n",
        "-" * 50, "\nRECOMMENDED ACTION\n",
        analysis.get("action_detail", "See action_summary."),
        "\n\nThis ticket requires review and approval before any action is taken.",
    ]
    if sql_results:
        parts += ["\n\nSQL RESULTS\n", "-" * 50 + "\n"]
        for sr in sql_results:
            parts.append(f"Status: {sr['status']} | SQL: {sr['sql'][:200]}\n"
                + (f"Rows: {sr.get('affected_rows')}\n" if sr["status"] == "executed" else "")
                + (f"Error: {sr.get('error')}\n" if sr.get("error") else ""))
    elif analysis.get("sql"):
        parts += ["\n\nSQL TO EXECUTE (run manually)\n", "-" * 50 + "\n", analysis["sql"]]
    if committed_files:
        parts += ["\n\nGITHUB COMMITS\n", "-" * 50 + "\n"]
        for cf in committed_files:
            parts.append(f"* {cf['path']}  (commit: {cf['sha']})\n")
        parts.append(f"\nRepo: {APP_GITHUB_OWNER}/{APP_GITHUB_REPO}@{APP_GITHUB_BRANCH}\n")

    summary = f"[ACTION] {analysis.get('action_summary', issue['summary'])}"[:250]
    print(f"  [Jira-MCP] Creating ACR ticket ...")
    try:
        t = jira.create_ticket(
            project_key=JIRA_ACTION_PROJECT_KEY, summary=summary,
            description="\n".join(parts), issue_type="Task",
            priority=issue.get("priority", "Medium") or "Medium",
            labels=["ai-recommended", "auto-generated", "pending-approval"],
        )
        print(f"  [Jira-MCP] Created: {t['key']}  -> {t.get('self','')}")
        log.jira_ticket_created(t["key"], JIRA_ACTION_PROJECT_KEY, summary)
    except Exception as exc:
        print(f"  [Jira-MCP] ERROR: {exc}")
        log.error("jira_ticket_created", str(exc))


def main():
    print("\n" + "=" * 60)
    print("  Jira <-> Confluence <-> Claude <-> GitHub <-> MySQL Agent  (MCP Edition)")
    print(f"  Source: {JIRA_SOURCE_PROJECT_KEY}  ->  Approval: {JIRA_ACTION_PROJECT_KEY}")
    print(f"  App repo: {APP_GITHUB_OWNER}/{APP_GITHUB_REPO}@{APP_GITHUB_BRANCH}")
    print(f"  DB: {'enabled (' + os.environ.get('DB_HOST','') + ')' if DB_ENABLED else 'not configured'}")
    print("=" * 60)

    issue_key = resolve_issue_key()
    if not issue_key:
        print("\n[ERROR] No issue key. Usage: python JiraConfluenceAIAgent_mcp.py --issue ADEV-42")
        sys.exit(1)
    if not issue_key.upper().startswith(f"{JIRA_SOURCE_PROJECT_KEY}-"):
        print(f"\n[WARNING] '{issue_key}' not in project '{JIRA_SOURCE_PROJECT_KEY}'. Proceeding ...")

    log = AuditLogger(issue_key=issue_key)
    try:
        print("\n[Step 1] Fetching Confluence pages ...")
        doc_page          = confluence.get_page(CONFLUENCE_DOC_ID)
        troubleshoot_page = confluence.get_page(CONFLUENCE_TS_ID)
        log.confluence_fetched(CONFLUENCE_DOC_ID, len(doc_page))
        log.confluence_fetched(CONFLUENCE_TS_ID,  len(troubleshoot_page))
        print(f"  doc: {len(doc_page):,} chars  |  troubleshoot: {len(troubleshoot_page):,} chars")

        print(f"\n[Step 2] Fetching {issue_key} ...")
        try:
            issue = jira.get_issue(issue_key)
            log.jira_fetched(issue)
        except Exception as exc:
            log.error("jira_fetched", str(exc))
            print(f"  [ERROR] {exc}")
            sys.exit(1)
        print(f"  [{issue['key']}] {issue['summary']} ({issue['status']})")

        print(f"\n[Step 3] Analysing {issue['key']} ...")
        process_jira_issue(issue, doc_page, troubleshoot_page, log)
        log.agent_end("success")

    except Exception as exc:
        log.error("agent_end", str(exc))
        print(f"\n[ERROR] Unhandled exception: {exc}")
        raise

    print("\n" + "=" * 60)
    print("  Agent run complete.")
    print(f"  Audit log: {Path(__file__).parent / 'ardoura_audit.log'}")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    main()
