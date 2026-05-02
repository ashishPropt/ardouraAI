"""
Jira-Confluence-Claude-GitHub AI Agent
========================================
Workflow:
  1. Receive the newly-created Jira issue key (CLI arg or JIRA_ISSUE_KEY env var)
  2. Fetch ONLY that single ticket — not all open tickets
  3. Pull two Confluence documentation pages
  4. Use Claude as an AI brain to determine the recommended action
  5. If the action requires a code change → fetch the full GitHub repo, apply the change, commit
  6. Create a new Jira ticket in ACR project (approval flow) with the recommended action

Usage:
  python JiraConfluenceAIAgent.py ADEV-42
  # or
  JIRA_ISSUE_KEY=ADEV-42 python JiraConfluenceAIAgent.py
"""

import os
import re
import sys
import base64
import json
import textwrap
from datetime import datetime

import requests
from anthropic import Anthropic

# ─────────────────────────────────────────────
# CREDENTIALS  (extracted from GenerateDocsfromGit1.py + Confluence config)
# ─────────────────────────────────────────────

ANTHROPIC_API_KEY   = "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"

GITHUB_TOKEN        = "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"
GITHUB_OWNER        = "ashishPropt"
GITHUB_REPO         = "Princetondawgs"
GITHUB_BRANCH       = "main"

ATLASSIAN_BASE      = "https://proptxchange.atlassian.net"
ATLASSIAN_EMAIL     = "ashish@proptxchange.com"
ATLASSIAN_API_TOKEN = "ATATT3xFfGF0uJY9cuMSM6d0bU_8KMUm2BSnw2PPvOJ5WXGRBfpKCrgw480FXxkvDBHDvReHjIyCr66XeqdiUoIcqrsGbFvID7WvCMsZTxcn4M5xh5XlX-EdmCTnEPsDlgX1i-ccStCXxFB-_Do5DtPuNcRXvscs6RRpDki6O9mAUesZrMnHB4Q=8F34BC98"

# Jira project key to fetch source tickets from
JIRA_SOURCE_PROJECT_KEY = "ADEV"

# Jira project key for action/approval tickets (approval flow)
# NOTE: ACR project is assumed to already exist in Jira — no existence check is performed.
JIRA_ACTION_PROJECT_KEY = "ACR"

# Confluence page IDs (extracted from the URLs provided)
CONFLUENCE_DOC_PAGE_ID          = "5996545"   # Princetondawgs Documentation
CONFLUENCE_TROUBLESHOOT_PAGE_ID = "6160385"   # Princetondawgs Troubleshooting Guide

ATLASSIAN_AUTH = (ATLASSIAN_EMAIL, ATLASSIAN_API_TOKEN)


# ─────────────────────────────────────────────
# SECTION 1 – JIRA HELPERS
# ─────────────────────────────────────────────

def jira_get(path: str, params: dict = None):
    """Make an authenticated Jira GET request; raises on non-2xx."""
    url = f"{ATLASSIAN_BASE}/rest/api/3{path}"
    r = requests.get(url, auth=ATLASSIAN_AUTH, params=params,
                     headers={"Accept": "application/json"})
    if not r.ok:
        print(f"  [Jira GET] {r.status_code} {r.reason}  →  {url}")
        try:
            print(f"  [Jira GET] body: {r.json()}")
        except Exception:
            print(f"  [Jira GET] body: {r.text[:400]}")
    r.raise_for_status()
    return r.json()


def jira_post(path: str, payload: dict):
    url = f"{ATLASSIAN_BASE}/rest/api/3{path}"
    r = requests.post(url, auth=ATLASSIAN_AUTH, json=payload,
                      headers={"Accept": "application/json",
                               "Content-Type": "application/json"})
    if not r.ok:
        print(f"  [Jira POST] {r.status_code} {r.reason}  →  {url}")
        try:
            print(f"  [Jira POST] body: {r.json()}")
        except Exception:
            print(f"  [Jira POST] body: {r.text[:400]}")
    r.raise_for_status()
    return r.json()


def fetch_single_jira(issue_key: str) -> dict:
    """
    Fetch a single Jira issue by its key (e.g. 'ADEV-42').
    Returns a normalised issue dict identical to what fetch_open_jiras() returns.
    """
    print(f"  [Jira] Fetching single issue: {issue_key} …")
    data = jira_get(f"/issue/{issue_key}")
    fields = data["fields"]
    desc_text = _adf_to_text(fields.get("description") or {})
    return {
        "key":         data["key"],
        "summary":     fields.get("summary", ""),
        "description": desc_text,
        "status":      fields["status"]["name"],
        "priority":    (fields.get("priority") or {}).get("name", ""),
        "issuetype":   fields["issuetype"]["name"],
    }


def _jira_search(jql: str, max_results: int = 50) -> dict:
    """
    Search Jira issues using the current POST /search/jql endpoint.
    Atlassian deprecated GET /search (410 Gone) in favour of POST /search/jql.
    Falls back to GET /search?jql=… for older instances that haven't migrated.
    """
    fields = ["summary", "description", "status", "priority", "issuetype",
              "assignee", "created"]
    payload = {
        "jql":        jql,
        "maxResults": max_results,
        "fields":     fields,
    }
    # ── Primary: POST /search/jql  (current Atlassian Cloud API) ──────
    url = f"{ATLASSIAN_BASE}/rest/api/3/search/jql"
    r = requests.post(url, auth=ATLASSIAN_AUTH, json=payload,
                      headers={"Accept": "application/json",
                               "Content-Type": "application/json"})
    if r.ok:
        return r.json()

    print(f"  [Jira] POST /search/jql returned {r.status_code} – trying GET /search …")

    # ── Fallback: GET /search?jql=… ───────────────────────────────────
    url2 = f"{ATLASSIAN_BASE}/rest/api/3/search"
    r2 = requests.get(url2, auth=ATLASSIAN_AUTH,
                      params={"jql": jql, "maxResults": max_results,
                              "fields": ",".join(fields)},
                      headers={"Accept": "application/json"})
    if r2.ok:
        return r2.json()

    # Both failed – surface the error clearly
    print(f"  [Jira] Both search endpoints failed.")
    print(f"  [Jira] POST body : {r.text[:400]}")
    print(f"  [Jira] GET  body : {r2.text[:400]}")
    r2.raise_for_status()


def _adf_to_text(node: dict, depth: int = 0) -> str:
    """Recursively extract plain text from Atlassian Document Format JSON."""
    if not node:
        return ""
    ntype = node.get("type", "")
    text  = node.get("text", "")
    result = text
    for child in node.get("content", []):
        result += _adf_to_text(child, depth + 1)
    if ntype in ("paragraph", "heading", "listItem"):
        result += "\n"
    return result


def create_jira_ticket(project_key: str, summary: str, description: str,
                       issue_type: str = "Task", priority: str = "Medium",
                       labels: list[str] = None) -> dict:
    """Create a new Jira ticket and return the created issue dict."""
    body = {
        "fields": {
            "project":   {"key": project_key},
            "summary":   summary,
            "issuetype": {"name": issue_type},
            "priority":  {"name": priority},
            "description": {
                "type":    "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": description}]
                    }
                ]
            }
        }
    }
    if labels:
        body["fields"]["labels"] = labels

    return jira_post("/issue", body)


# ─────────────────────────────────────────────
# SECTION 2 – CONFLUENCE HELPERS
# ─────────────────────────────────────────────

def confluence_get_page_content(page_id: str) -> str:
    """Fetch Confluence page body as plain-ish text (storage → strip HTML tags)."""
    url = (f"{ATLASSIAN_BASE}/wiki/rest/api/content/{page_id}"
           f"?expand=body.storage,title")
    r = requests.get(url, auth=ATLASSIAN_AUTH,
                     headers={"Accept": "application/json"})
    r.raise_for_status()
    data = r.json()
    title   = data.get("title", "")
    storage = data["body"]["storage"]["value"]
    plain = re.sub(r"<[^>]+>", " ", storage)
    plain = re.sub(r"\s{2,}", " ", plain).strip()
    return f"=== {title} ===\n\n{plain}"


# ─────────────────────────────────────────────
# SECTION 3 – GITHUB HELPERS
# ─────────────────────────────────────────────

GH_HEADERS = {
    "Authorization": f"token {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
}


def get_repo_tree(owner: str, repo: str, branch: str = "main") -> list[str]:
    url = (f"https://api.github.com/repos/{owner}/{repo}"
           f"/git/trees/{branch}?recursive=1")
    r = requests.get(url, headers=GH_HEADERS)
    r.raise_for_status()
    return [i["path"] for i in r.json().get("tree", []) if i["type"] == "blob"]


def get_file_content(owner: str, repo: str, path: str) -> tuple[str, str]:
    """Return (decoded_content, sha) for a file."""
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}"
    r = requests.get(url, headers=GH_HEADERS)
    r.raise_for_status()
    data = r.json()
    content = base64.b64decode(data["content"]).decode("utf-8")
    return content, data["sha"]


def load_full_codebase(owner: str, repo: str, branch: str = "main") -> dict[str, str]:
    """Load all source files from the repo into a dict {path: content}."""
    print("  [GitHub] Fetching repository tree …")
    paths = get_repo_tree(owner, repo, branch)
    code_exts = (".py", ".js", ".ts", ".java", ".go", ".rb", ".cs",
                 ".yaml", ".yml", ".sql", ".sh", ".tf")
    files = {}
    for p in paths:
        if p.endswith(code_exts):
            try:
                content, _ = get_file_content(owner, repo, p)
                files[p] = content
                print(f"  [GitHub] Loaded {p}")
            except Exception as e:
                print(f"  [GitHub] Skipped {p}: {e}")
    return files


def commit_file_to_github(owner: str, repo: str, path: str,
                           new_content: str, commit_message: str,
                           branch: str = "main") -> dict:
    """Create or update a file in the GitHub repo and commit it."""
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}"

    sha = None
    try:
        r = requests.get(url, headers=GH_HEADERS)
        if r.status_code == 200:
            sha = r.json()["sha"]
    except Exception:
        pass

    encoded = base64.b64encode(new_content.encode("utf-8")).decode("utf-8")
    payload = {
        "message": commit_message,
        "content": encoded,
        "branch":  branch,
    }
    if sha:
        payload["sha"] = sha

    r = requests.put(url, headers=GH_HEADERS, json=payload)
    r.raise_for_status()
    return r.json()


# ─────────────────────────────────────────────
# SECTION 4 – CLAUDE AI ANALYSIS
# ─────────────────────────────────────────────

claude = Anthropic(api_key=ANTHROPIC_API_KEY)


def analyse_jira_with_claude(jira_issue: dict,
                              doc_page: str,
                              troubleshoot_page: str,
                              codebase: dict[str, str] | None = None) -> dict:
    """
    Send the Jira ticket + documentation to Claude.
    Returns a structured dict with action_type, action_summary, action_detail,
    files_to_change, and sql.
    """
    codebase_section = ""
    if codebase:
        codebase_section = "\n\n## FULL CODEBASE\n"
        for path, content in codebase.items():
            codebase_section += f"\n### FILE: {path}\n```\n{content}\n```\n"

    prompt = textwrap.dedent(f"""
        You are an expert SRE and senior software engineer.

        You will be given:
        1. An open Jira ticket describing a problem or task.
        2. Two Confluence documentation pages that describe the system.
        3. Optionally, the full codebase of the application.

        Your job is to determine the EXACT, IMMEDIATELY ACTIONABLE remediation.

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
        The JSON must have these keys:

        {{
          "action_type":    "<one of: sql | code_change | config | manual | unknown>",
          "action_summary": "<concise one-liner suitable for a Jira ticket title>",
          "action_detail":  "<step-by-step, immediately actionable instructions>",
          "files_to_change": [
            {{"path": "<relative file path in repo>", "new_content": "<complete new file content>"}}
          ],
          "sql": "<full SQL statement to execute, or null>"
        }}

        Rules:
        - If a database query is needed, provide the complete SQL in both action_detail and the sql field.
        - If a code change is needed, populate files_to_change with the COMPLETE updated file content (not a diff).
        - If no code change is needed, set files_to_change to [].
        - Set sql to null if no SQL is required.
        - Be specific. Do not say "update the config" — provide the exact values.
    """).strip()

    response = claude.messages.create(
        model="claude-opus-4-5",
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}]
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


# ─────────────────────────────────────────────
# SECTION 5 – ORCHESTRATOR
# ─────────────────────────────────────────────

def process_jira_issue(issue: dict, doc_page: str, troubleshoot_page: str) -> None:
    print(f"\n{'='*60}")
    print(f"  Processing: {issue['key']} – {issue['summary']}")
    print(f"{'='*60}")

    analysis = analyse_jira_with_claude(issue, doc_page, troubleshoot_page)

    if analysis.get("action_type") == "code_change":
        print("  [Agent] Code change detected – loading full codebase …")
        codebase = load_full_codebase(GITHUB_OWNER, GITHUB_REPO, GITHUB_BRANCH)
        analysis  = analyse_jira_with_claude(issue, doc_page, troubleshoot_page,
                                              codebase=codebase)

    print(f"  [Agent] Action type : {analysis.get('action_type')}")
    print(f"  [Agent] Summary     : {analysis.get('action_summary')}")

    # ── Apply code changes to GitHub ──────────────────────────────────
    committed_files = []
    for file_change in analysis.get("files_to_change", []):
        path        = file_change.get("path", "").strip()
        new_content = file_change.get("new_content", "")
        if not path or not new_content:
            continue
        commit_msg = (f"[{issue['key']}] AI-recommended fix: "
                      f"{analysis.get('action_summary', '')[:60]}")
        print(f"  [GitHub] Committing change to {path} …")
        try:
            result = commit_file_to_github(
                GITHUB_OWNER, GITHUB_REPO, path, new_content, commit_msg, GITHUB_BRANCH
            )
            sha = result.get("commit", {}).get("sha", "N/A")
            committed_files.append({"path": path, "sha": sha})
            print(f"  [GitHub] Committed {path} → {sha}")
        except Exception as e:
            print(f"  [GitHub] ERROR committing {path}: {e}")

    # ── Build Jira description ─────────────────────────────────────────
    description_parts = [
        f"AUTO-GENERATED ACTION TICKET — linked to {issue['key']}\n",
        f"Original Jira: {issue['key']} – {issue['summary']}\n",
        f"Source Project: {JIRA_SOURCE_PROJECT_KEY}  |  Approval Project: {JIRA_ACTION_PROJECT_KEY}\n",
        "─" * 50,
        "\nRECOMMENDED ACTION\n",
        analysis.get("action_detail", "See action_summary."),
        "\n\n⚠️  This ticket requires review and approval before any action is taken.",
    ]

    if analysis.get("sql"):
        description_parts += [
            "\n\nSQL TO EXECUTE\n",
            "─" * 50 + "\n",
            analysis["sql"],
        ]

    if committed_files:
        description_parts += [
            "\n\nGITHUB COMMITS\n",
            "─" * 50 + "\n",
        ]
        for cf in committed_files:
            description_parts.append(f"• {cf['path']}  (commit: {cf['sha']})\n")

    full_description = "\n".join(description_parts)

    # ── Create the action Jira ticket in ACR (approval flow) ──────────
    new_summary = (f"[ACTION] {analysis.get('action_summary', issue['summary'])}"
                   )[:250]

    print(f"  [Jira] Creating approval ticket in {JIRA_ACTION_PROJECT_KEY} …")
    try:
        new_ticket = create_jira_ticket(
            project_key  = JIRA_ACTION_PROJECT_KEY,
            summary      = new_summary,
            description  = full_description,
            issue_type   = "Task",
            priority     = issue.get("priority", "Medium") or "Medium",
            labels       = ["ai-recommended", "auto-generated", "pending-approval"],
        )
        print(f"  [Jira] Created {JIRA_ACTION_PROJECT_KEY} ticket: {new_ticket['key']}  →  {new_ticket.get('self','')}")
    except Exception as e:
        print(f"  [Jira] ERROR creating ticket: {e}")


def resolve_issue_key() -> str | None:
    """
    Determine which single Jira issue to process.
    Priority order:
      1. First CLI argument:  python JiraConfluenceAIAgent.py ADEV-42
      2. Environment variable: JIRA_ISSUE_KEY=ADEV-42
    Returns None if neither is provided.
    """
    if len(sys.argv) > 1:
        return sys.argv[1].strip()
    return os.environ.get("JIRA_ISSUE_KEY", "").strip() or None


def main():
    print("\n" + "═" * 60)
    print("  Jira ↔ Confluence ↔ Claude ↔ GitHub Agent")
    print(f"  Source: {JIRA_SOURCE_PROJECT_KEY}  →  Approval: {JIRA_ACTION_PROJECT_KEY}")
    print("═" * 60)

    # ── Resolve which issue to process ────────────────────────────────
    issue_key = resolve_issue_key()
    if not issue_key:
        print("\n[ERROR] No Jira issue key provided.")
        print("  Usage:  python JiraConfluenceAIAgent.py ADEV-42")
        print("    or:   JIRA_ISSUE_KEY=ADEV-42 python JiraConfluenceAIAgent.py")
        sys.exit(1)

    # Validate the key belongs to the expected project
    expected_prefix = f"{JIRA_SOURCE_PROJECT_KEY}-"
    if not issue_key.upper().startswith(expected_prefix):
        print(f"\n[WARNING] Issue key '{issue_key}' does not belong to project "
              f"'{JIRA_SOURCE_PROJECT_KEY}'. Proceeding anyway …")

    # 1. Fetch Confluence documentation
    print("\n[Step 1] Fetching Confluence documentation pages …")
    doc_page          = confluence_get_page_content(CONFLUENCE_DOC_PAGE_ID)
    troubleshoot_page = confluence_get_page_content(CONFLUENCE_TROUBLESHOOT_PAGE_ID)
    print(f"  Loaded doc page       : {len(doc_page):,} chars")
    print(f"  Loaded troubleshoot pg: {len(troubleshoot_page):,} chars")

    # 2. Fetch the single newly-created Jira ticket
    print(f"\n[Step 2] Fetching Jira issue {issue_key} …")
    try:
        issue = fetch_single_jira(issue_key)
    except Exception as e:
        print(f"  [ERROR] Could not fetch issue {issue_key}: {e}")
        sys.exit(1)

    print(f"  Fetched: [{issue['key']}] {issue['summary']} (status: {issue['status']})")

    # 3. Process the single issue → create ACR approval ticket
    print(f"\n[Step 3] Analysing {issue['key']} with Claude → creating {JIRA_ACTION_PROJECT_KEY} approval ticket …")
    try:
        process_jira_issue(issue, doc_page, troubleshoot_page)
    except Exception as e:
        print(f"  ERROR processing {issue['key']}: {e}")

    print("\n" + "═" * 60)
    print("  Agent run complete.")
    print("═" * 60 + "\n")


if __name__ == "__main__":
    main()
