"""
JiraConfluenceAIAgent_mcp.py  (MCP Edition)
fix: max_tokens raised to 16000 — see _call_claude_raw
"""

import os, sys, re, argparse, json, textwrap
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env", override=True)

from anthropic import Anthropic
from mcp_client import GitHubMCP, JiraMCP, ConfluenceMCP, MySQLMCP
from audit_logger import AuditLogger

ANTHROPIC_API_KEY       = os.environ["ANTHROPIC_API_KEY"]
_k = ANTHROPIC_API_KEY
print(f"[Auth] ANTHROPIC_API_KEY = {_k[:20]}...{_k[-6:]}")
if not _k.startswith("sk-ant-"):
    print("[Auth] WARNING: key does not look like a valid Anthropic key")
_gh = os.environ.get("GITHUB_TOKEN","")
if _gh: print(f"[Auth] GITHUB_TOKEN      = {_gh[:15]}...{_gh[-4:]}")
else:   print("[Auth] WARNING: GITHUB_TOKEN not set")
_db = os.environ.get("DB_HOST","")
if _db: print(f"[Auth] DB_HOST           = {_db}")
else:   print("[Auth] INFO: DB_HOST not set — MySQL skipped")

APP_GITHUB_OWNER        = os.environ.get("APP_GITHUB_OWNER",  "ashishPropt")
APP_GITHUB_REPO         = os.environ.get("APP_GITHUB_REPO",   "Princetondawgs")
APP_GITHUB_BRANCH       = os.environ.get("APP_GITHUB_BRANCH", "main")
JIRA_SOURCE_PROJECT_KEY = os.environ.get("JIRA_SOURCE_PROJECT_KEY", "ADEV")
JIRA_ACTION_PROJECT_KEY = os.environ.get("JIRA_ACTION_PROJECT_KEY", "ACR")
CONFLUENCE_DOC_ID       = os.environ["CONFLUENCE_DOC_PAGE_ID"]
CONFLUENCE_TS_ID        = os.environ["CONFLUENCE_TROUBLESHOOT_PAGE_ID"]
CONFLUENCE_SPACE_KEY    = os.environ.get("CONFLUENCE_SPACE_KEY", "~default")
DB_ENABLED              = bool(os.environ.get("DB_HOST",""))

app_github = GitHubMCP()
jira       = JiraMCP()
confluence = ConfluenceMCP()
db         = MySQLMCP() if DB_ENABLED else None
claude     = Anthropic(api_key=ANTHROPIC_API_KEY)


def resolve_issue_key() -> str | None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--issue", metavar="ISSUE_KEY", default=None)
    args, _ = parser.parse_known_args()
    if args.issue: return args.issue.strip()
    return os.environ.get("JIRA_ISSUE_KEY","").strip() or None


# ── Prompts ───────────────────────────────────────────────────────────────────

_JSON_RULES = """
## OUTPUT RULES
You MUST respond ONLY with a single valid JSON object.
- NO text, explanation, or commentary before or after the JSON.
- NO markdown code fences (no ```json or ```).
- Response MUST start with { and end with }.
- All string values must use proper JSON escaping.
- For code_change: provide FULL file content (not diffs).
"""

def analyse_with_confluence(jira_issue, doc_page, troubleshoot_page, db_schema=""):
    schema_section = f"\n\n## DATABASE SCHEMA\n{db_schema}" if db_schema else ""
    prompt = textwrap.dedent(f"""
        You are an expert SRE and senior software engineer.
        Analyse the Jira ticket. Set confidence="low" if you cannot produce a
        complete, immediately-actionable solution.

        ## JIRA TICKET
        Key: {jira_issue['key']} | Type: {jira_issue['issuetype']} | Priority: {jira_issue['priority']}
        Summary: {jira_issue['summary']}
        Description: {jira_issue['description'] or '(none)'}

        ## CONFLUENCE DOCS
        {doc_page}

        ## TROUBLESHOOTING GUIDE
        {troubleshoot_page}
        {schema_section}
        {_JSON_RULES}

        Required JSON:
        {{
          "confidence": "<high|low>",
          "action_type": "<sql|code_change|config|manual|unknown>",
          "action_summary": "<one-liner>",
          "action_detail": "<step-by-step>",
          "files_to_change": [{{"path":"<path>","new_content":"<full content>"}}],
          "sql": "<SQL or null>",
          "sql_tables_affected": ["<table>"],
          "knowledge_gaps": "<gaps or null>"
        }}
    """).strip()
    return _call_claude(prompt)


def analyse_with_confluence_and_github(jira_issue, doc_page, troubleshoot_page, codebase):
    codebase_section = "\n\n## APPLICATION CODEBASE\n"
    for path, content in codebase.items():
        codebase_section += f"\n### {path}\n```\n{content}\n```\n"
    prompt = textwrap.dedent(f"""
        You are an expert SRE and senior software engineer.
        Analyse the Jira ticket using Confluence docs AND the full codebase.

        ## JIRA TICKET
        Key: {jira_issue['key']} | Type: {jira_issue['issuetype']} | Priority: {jira_issue['priority']}
        Summary: {jira_issue['summary']}
        Description: {jira_issue['description'] or '(none)'}

        ## CONFLUENCE DOCS
        {doc_page}

        ## TROUBLESHOOTING GUIDE
        {troubleshoot_page}
        {codebase_section}
        {_JSON_RULES}

        Required JSON:
        {{
          "confidence": "<high|low>",
          "action_type": "<sql|code_change|config|manual|unknown>",
          "action_summary": "<one-liner>",
          "action_detail": "<step-by-step>",
          "files_to_change": [{{"path":"<path>","new_content":"<full content>"}}],
          "sql": "<SQL or null>",
          "sql_tables_affected": ["<table>"],
          "new_confluence_knowledge": "<markdown or null>"
        }}
    """).strip()
    return _call_claude(prompt)


def build_confluence_update_for_code_change(jira_issue, analysis, committed_files):
    if not committed_files: return None
    files_list = "\n".join(f"  • {cf['path']} (commit: {cf['sha']})" for cf in committed_files)
    prompt = textwrap.dedent(f"""
        Write a concise Confluence wiki section (Markdown) for this code commit.
        JIRA: {jira_issue['key']} — {jira_issue['summary']}
        SUMMARY: {analysis.get('action_summary','')}
        FILES:\n{files_list}
        DETAIL: {analysis.get('action_detail','')}
        Output ONLY the Markdown — no preamble, no fences.
        Heading: ## Fix: <short title> ({jira_issue['key']})
    """).strip()
    result = _call_claude_raw(prompt)
    return result.strip() if result else None


# ── Claude call helpers ───────────────────────────────────────────────────────

def _call_claude(prompt: str) -> dict:
    raw = _call_claude_raw(prompt)
    raw = re.sub(r"^```[a-z]*\n?", "", raw)
    raw = re.sub(r"\n?```$", "", raw)

    preview = raw[:600].replace("\n"," ")
    print(f"  [Claude Raw] ({len(raw)} chars): {preview}{'...' if len(raw)>600 else ''}")

    if not raw.strip().startswith("{"):
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            print("  [Claude] Preamble detected — extracting JSON …")
            raw = m.group(0)

    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        pos     = e.pos if hasattr(e,"pos") else 0
        snippet = raw[max(0,pos-40):pos+40]
        print(f"  [Claude Parse ERROR] {e}")
        print(f"  [Claude Parse ERROR] pos={pos}, context: ...{snippet}...")
        try:
            (Path(__file__).parent/"claude_parse_error.txt").write_text(raw, encoding="utf-8")
            print("  [Claude Parse ERROR] Full response saved to claude_parse_error.txt")
        except Exception:
            pass
        return {
            "confidence":"low","action_type":"unknown",
            "action_summary":"AI analysis (parse error)","action_detail":raw,
            "files_to_change":[],"sql":None,"sql_tables_affected":[],
        }


def _call_claude_raw(prompt: str) -> str:
    response = claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=16000,   # Raised from 4000: code_change responses with full file content
                            # were hitting the limit and truncating JSON mid-string.
        messages=[{"role":"user","content":prompt}],
    )
    if response.stop_reason == "max_tokens":
        print("  [Claude WARNING] Response TRUNCATED even at 16000 tokens!")
        print("  [Claude WARNING] Consider splitting large code_change tickets.")
    return response.content[0].text.strip()


# ── Confluence updater ────────────────────────────────────────────────────────

def append_to_confluence(title, new_section, log=None):
    try:
        existing = confluence.get_page_by_title(CONFLUENCE_SPACE_KEY, title) or ""
        confluence.create_or_update(
            space_key=CONFLUENCE_SPACE_KEY, title=title,
            content=existing+"\n\n"+new_section,
        )
        print(f"  [Confluence-MCP] Updated: '{title}'")
        if log: log.confluence_updated(title)
    except Exception as exc:
        print(f"  [Confluence-MCP] WARNING — '{title}': {exc}")
        if log: log.warning("confluence_updated", str(exc))


# ── Per-issue orchestration ───────────────────────────────────────────────────

def process_jira_issue(issue, doc_page, troubleshoot_page, log):
    print(f"\n{'='*60}\n  Processing: {issue['key']} – {issue['summary']}\n{'='*60}")

    github_was_loaded = False
    codebase = {}

    # DB schema
    db_schema = ""
    if db and DB_ENABLED:
        try:
            print("  [MySQL-MCP] Loading schema …")
            sr = db.get_schema()
            if isinstance(sr,dict) and "tables" in sr:
                lines = [f"Database: {sr.get('database','?')}"]
                for tbl,cols in sr["tables"].items():
                    lines.append("  "+tbl+": "+", ".join(
                        f"{c['column']} {c['type']}{' PK' if c['key']=='PRI' else ''}"
                        for c in cols))
                db_schema = "\n".join(lines)
                tc = sr.get("table_count",0)
                print(f"  [MySQL-MCP] Schema: {tc} tables")
                log.db_schema_loaded(tc)
        except Exception as exc:
            print(f"  [MySQL-MCP] WARNING: {exc}")
            log.warning("db_schema_loaded",str(exc))

    # Pass 1
    print("  [Agent] Pass 1: Confluence + DB schema …")
    analysis = analyse_with_confluence(issue, doc_page, troubleshoot_page, db_schema)
    log.claude_analysis(analysis, pass_number=1)
    print(f"  [Agent] Confidence  : {analysis.get('confidence')}")
    print(f"  [Agent] Action type : {analysis.get('action_type')}")

    needs_github = (
        analysis.get("confidence")=="low"
        or analysis.get("action_type") in ("unknown","code_change")
    )

    # Pass 2
    if needs_github:
        print(f"  [Agent] Pass 2: Loading GitHub {APP_GITHUB_OWNER}/{APP_GITHUB_REPO} …")
        codebase = app_github.load_codebase(APP_GITHUB_OWNER,APP_GITHUB_REPO,APP_GITHUB_BRANCH)
        print(f"  [Agent] Loaded {len(codebase)} files")
        github_was_loaded = True
        log.github_codebase_loaded(APP_GITHUB_OWNER,APP_GITHUB_REPO,len(codebase))

        analysis = analyse_with_confluence_and_github(issue,doc_page,troubleshoot_page,codebase)
        log.claude_analysis(analysis, pass_number=2)
        print(f"  [Agent] (Pass 2) Confidence  : {analysis.get('confidence')}")
        print(f"  [Agent] (Pass 2) Action type : {analysis.get('action_type')}")

        nk = analysis.get("new_confluence_knowledge")
        if nk:
            print("  [Agent] New knowledge → Confluence …")
            append_to_confluence(
                "AI-Discovered Knowledge from Application Codebase",
                f"### Findings for {issue['key']} – {issue['summary']}\n\n{nk}",
                log=log,
            )

    print(f"  [Agent] Final summary: {analysis.get('action_summary')}")

    # SQL execution
    sql_results = []
    if analysis.get("action_type")=="sql" and analysis.get("sql") and db and DB_ENABLED:
        sql_raw = analysis["sql"].strip()
        tables  = analysis.get("sql_tables_affected",[])
        snapshot={}; total_snapped=0
        if tables:
            try:
                snap = db.begin_snapshot(tables)
                snapshot = snap.get("snapshots",{})
                total_snapped = sum(len(v) for v in snapshot.values())
                print(f"  [MySQL-MCP] Snapshot: {total_snapped} rows")
                log.sql_snapshot(tables,total_snapped)
            except Exception as exc:
                print(f"  [MySQL-MCP] Snapshot WARNING: {exc}")
                log.warning("sql_snapshot",str(exc))
        try:
            dry = db.execute_update(sql_raw, dry_run=True)
            if dry.get("error"):
                msg=dry["error"]; print(f"  [MySQL-MCP] Dry-run FAILED: {msg}")
                log.sql_dry_run(sql_raw,passed=False,error=msg)
                sql_results.append({"sql":sql_raw,"status":"dry_run_failed","error":msg})
            else:
                log.sql_dry_run(sql_raw,passed=True)
                result = db.execute_update(sql_raw)
                if result.get("success"):
                    aff=result.get("affected_rows",0)
                    print(f"  [MySQL-MCP] SUCCESS: {aff} row(s)")
                    log.sql_executed(sql_raw,aff,tables)
                    sql_results.append({"sql":sql_raw,"status":"executed","affected_rows":aff,"snapshot":snapshot})
                    append_to_confluence("Application Changelog (AI-Managed)",
                        f"## DB Change: {analysis.get('action_summary')} ({issue['key']})\n\n"
                        f"**SQL:**\n```sql\n{sql_raw}\n```\n\n**Rows:** {aff}\n\n"
                        f"**Tables:** {', '.join(tables)}\n\n**Snapshot rows:** {total_snapped}",log=log)
                else:
                    err=result.get("error","unknown")
                    print(f"  [MySQL-MCP] FAILED: {err}")
                    log.sql_failed(sql_raw,err)
                    sql_results.append({"sql":sql_raw,"status":"failed","error":err})
        except Exception as exc:
            print(f"  [MySQL-MCP] ERROR: {exc}")
            log.error("sql_executed",str(exc))
            sql_results.append({"sql":sql_raw,"status":"error","error":str(exc)})
    elif analysis.get("action_type")=="sql" and not DB_ENABLED:
        print("  [MySQL-MCP] DB not configured — SQL in ticket only")
        log.warning("sql_executed","DB not configured",{"sql_preview":(analysis.get("sql") or "")[:200]})

    # Code commits
    committed_files=[]
    if analysis.get("action_type")=="code_change":
        if not github_was_loaded:
            codebase=app_github.load_codebase(APP_GITHUB_OWNER,APP_GITHUB_REPO,APP_GITHUB_BRANCH)
        for fc in analysis.get("files_to_change",[]):
            path=(fc.get("path") or "").strip(); nc=fc.get("new_content","")
            if not path or not nc: continue
            msg=f"[{issue['key']}] AI-recommended fix: {analysis.get('action_summary','')[:60]}"
            print(f"  [AppGitHub-MCP] Committing {path} …")
            try:
                r=app_github.commit_file(APP_GITHUB_OWNER,APP_GITHUB_REPO,path,nc,msg,APP_GITHUB_BRANCH)
                sha=r.get("commit_sha","")
                committed_files.append({"path":path,"sha":sha})
                print(f"  [AppGitHub-MCP] → {sha or 'N/A'}")
                log.github_committed(path,sha,APP_GITHUB_OWNER,APP_GITHUB_REPO)
            except Exception as exc:
                print(f"  [AppGitHub-MCP] ERROR: {exc}")
                log.github_commit_failed(path,str(exc))
        if committed_files:
            cd=build_confluence_update_for_code_change(issue,analysis,committed_files)
            if cd: append_to_confluence("Application Changelog (AI-Managed)",cd,log=log)

    # ACR ticket
    parts=[
        f"AUTO-GENERATED — linked to {issue['key']}\n",
        f"Original: {issue['key']} – {issue['summary']}\n",
        f"Source: {JIRA_SOURCE_PROJECT_KEY}  |  Approval: {JIRA_ACTION_PROJECT_KEY}\n",
        "─"*50,"\nRECOMMENDED ACTION\n",
        analysis.get("action_detail","See action_summary."),
        "\n\n⚠️  Requires review and approval before any action is taken.",
    ]
    if sql_results:
        parts+=["\\n\\nSQL RESULTS\n","─"*50+"\n"]
        for sr in sql_results:
            parts.append(f"Status: {sr['status']} | SQL: {sr['sql'][:200]}\n"
                +(f"Rows: {sr.get('affected_rows')}\n" if sr["status"]=="executed" else "")
                +(f"Error: {sr.get('error')}\n" if sr.get("error") else ""))
    elif analysis.get("sql"):
        parts+=["\n\nSQL TO EXECUTE (run manually)\n","─"*50+"\n",analysis["sql"]]
    if committed_files:
        parts+=["\n\nGITHUB COMMITS\n","─"*50+"\n"]
        for cf in committed_files: parts.append(f"• {cf['path']} (commit: {cf['sha']})\n")
        parts.append(f"\nRepo: {APP_GITHUB_OWNER}/{APP_GITHUB_REPO}@{APP_GITHUB_BRANCH}\n")

    summary = f"[ACTION] {analysis.get('action_summary',issue['summary'])}"[:250]
    print(f"  [Jira-MCP] Creating ACR ticket …")
    try:
        t=jira.create_ticket(project_key=JIRA_ACTION_PROJECT_KEY,summary=summary,
            description="\n".join(parts),issue_type="Task",
            priority=issue.get("priority","Medium") or "Medium",
            labels=["ai-recommended","auto-generated","pending-approval"])
        print(f"  [Jira-MCP] Created: {t['key']}  → {t.get('self','')}")
        log.jira_ticket_created(t["key"],JIRA_ACTION_PROJECT_KEY,summary)
    except Exception as exc:
        print(f"  [Jira-MCP] ERROR: {exc}")
        log.error("jira_ticket_created",str(exc))


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("\n"+"═"*60)
    print("  Jira ↔ Confluence ↔ Claude ↔ GitHub ↔ MySQL Agent  (MCP Edition)")
    print(f"  Source: {JIRA_SOURCE_PROJECT_KEY}  →  Approval: {JIRA_ACTION_PROJECT_KEY}")
    print(f"  App repo: {APP_GITHUB_OWNER}/{APP_GITHUB_REPO}@{APP_GITHUB_BRANCH}")
    print(f"  DB: {'enabled ('+os.environ.get('DB_HOST','')+')' if DB_ENABLED else 'not configured'}")
    print("═"*60)

    issue_key = resolve_issue_key()
    if not issue_key:
        print("\n[ERROR] No issue key. Usage: python JiraConfluenceAIAgent_mcp.py --issue ADEV-42")
        sys.exit(1)
    if not issue_key.upper().startswith(f"{JIRA_SOURCE_PROJECT_KEY}-"):
        print(f"\n[WARNING] '{issue_key}' not in project '{JIRA_SOURCE_PROJECT_KEY}'. Proceeding …")

    log = AuditLogger(issue_key=issue_key)
    try:
        print("\n[Step 1] Fetching Confluence pages …")
        doc_page          = confluence.get_page(CONFLUENCE_DOC_ID)
        troubleshoot_page = confluence.get_page(CONFLUENCE_TS_ID)
        log.confluence_fetched(CONFLUENCE_DOC_ID, len(doc_page))
        log.confluence_fetched(CONFLUENCE_TS_ID,  len(troubleshoot_page))
        print(f"  doc: {len(doc_page):,} chars | troubleshoot: {len(troubleshoot_page):,} chars")

        print(f"\n[Step 2] Fetching {issue_key} …")
        try:
            issue = jira.get_issue(issue_key)
            log.jira_fetched(issue)
        except Exception as exc:
            log.error("jira_fetched",str(exc))
            print(f"  [ERROR] {exc}")
            sys.exit(1)
        print(f"  [{issue['key']}] {issue['summary']} ({issue['status']})")

        print(f"\n[Step 3] Analysing {issue['key']} …")
        process_jira_issue(issue, doc_page, troubleshoot_page, log)
        log.agent_end("success")

    except Exception as exc:
        log.error("agent_end",str(exc))
        print(f"\n[ERROR] {exc}")
        raise

    print("\n"+"═"*60)
    print("  Agent run complete.")
    print(f"  Audit log: {Path(__file__).parent/'ardoura_audit.log'}")
    print("═"*60+"\n")


if __name__ == "__main__":
    main()
