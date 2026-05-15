#!/usr/bin/env python3
"""
JiraConfluenceAIAgent_mcp.py
============================
AI agent that:
  1. Reads an ADEV Jira issue
  2. Checks Confluence docs for context
  3. Checks GitHub codebase for impact
  4. Uses Claude to analyse and plan the fix
  5. Creates an ACR ticket linked to the ADEV as 'fixes'
  6. If Claude deems execution safe -> transitions ACR to In Progress
     Otherwise -> leaves ACR Open and assigns to the operator
  7. Adds a detailed comment to both ADEV and ACR

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
APP_GITHUB_REPO     = os.environ.get("APP_GITHUB_REPO", "ardouraAI")
OPERATOR_ACCOUNT_ID = os.environ.get("JIRA_ASSIGNEE_ACCOUNT_ID", "")

# Directory where MCP server scripts live
BASE_DIR = str(Path(__file__).parent)

# Pass the FULL current environment to MCP subprocesses so they inherit
# all secrets already loaded from .env by the consumer
FULL_ENV = {**os.environ}


def _server(script: str) -> StdioServerParameters:
    """Build MCP server params passing the full environment."""
    return StdioServerParameters(
        command="python",
        args=[script],
        cwd=BASE_DIR,
        env=FULL_ENV,
    )


async def call_tool(session: ClientSession, tool: str, args: dict) -> str:
    """Call an MCP tool and return its text result with error logging."""
    result = await session.call_tool(tool, args)
    if not result.content:
        print(f"[Agent] WARNING: tool '{tool}' returned empty content", flush=True)
        return "{}"
    text = result.content[0].text
    if not text or not text.strip():
        print(f"[Agent] WARNING: tool '{tool}' returned blank text", flush=True)
        return "{}"
    return text


async def run_agent(issue_key: str) -> None:
    print(f"[Agent] Starting for {issue_key}", flush=True)
    print(f"[Agent] ATLASSIAN_BASE={ATLASSIAN_BASE}", flush=True)
    print(f"[Agent] ATLASSIAN_EMAIL={ATLASSIAN_EMAIL}", flush=True)
    print(f"[Agent] TOKEN set={'yes' if ATLASSIAN_API_TOKEN else 'NO - MISSING'}",
          flush=True)

    if not ATLASSIAN_BASE or not ATLASSIAN_EMAIL or not ATLASSIAN_API_TOKEN:
        print("[Agent] ERROR: Atlassian credentials missing - check /etc/ardoura/secrets.env",
              flush=True)
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
        print(f"[Agent] Raw issue response: {issue_raw[:200]}", flush=True)
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

    # ── Step 3: Read GitHub codebase tree ────────────────────────────────────
    print("[Agent] Reading GitHub codebase ...", flush=True)
    github_context = ""
    try:
        async with stdio_client(_server("mcp_github_server.py")) as (r, w):
            async with ClientSession(r, w) as gh:
                await gh.initialize()
                tree = await call_tool(gh, "get_repo_tree",
                                        {"owner": APP_GITHUB_OWNER,
                                         "repo":  APP_GITHUB_REPO})
                github_context = f"Repository file tree:\n{tree[:4000]}"
    except Exception as e:
        print(f"[Agent] WARNING: GitHub read failed: {e}", flush=True)
        github_context = f"(GitHub read failed: {e})"

    # ── Step 4: Claude analysis ──────────────────────────────────────────────
    print("[Agent] Sending to Claude for analysis ...", flush=True)

    prompt = f"""You are an expert software engineer and change manager.

You have been given a new Jira issue that needs to be analysed, planned, and actioned.

## ADEV Issue
Key: {issue.get('key')}
Summary: {issue.get('summary')}
Description: {issue.get('description', '(no description)')}
Status: {issue.get('status')}
Priority: {issue.get('priority')}
Type: {issue.get('issuetype')}

## Confluence Documentation
{confluence_context}

## GitHub Codebase
{github_context}

## Your Task
Analyse this issue thoroughly and produce a structured JSON response with these exact fields:

{{
  "acr_summary": "One-line summary for the ACR ticket (max 100 chars)",
  "acr_description": "Full detailed description of what will be done to fix/implement this. Include: root cause analysis, files to change, steps to execute, risks, rollback plan.",
  "execution_safe": true or false,
  "execution_rationale": "Why it is or isn't safe to auto-execute",
  "risk_level": "LOW | MEDIUM | HIGH",
  "estimated_effort": "e.g. 2 hours, 1 day",
  "steps": ["step 1", "step 2", ...]
}}

Set execution_safe=true ONLY if ALL of these are true:
- Risk level is LOW
- No database schema changes
- No destructive operations
- No external service configuration changes
- Change is fully reversible

Otherwise set execution_safe=false and leave it for human review.

Respond with ONLY the JSON object, no markdown, no explanation."""

    response = claude.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}]
    )

    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()

    try:
        analysis = json.loads(raw)
    except json.JSONDecodeError:
        print(f"[Agent] WARNING: Claude response not valid JSON:\n{raw}", flush=True)
        analysis = {
            "acr_summary":         f"Fix for {issue_key}: {issue.get('summary', '')[:60]}",
            "acr_description":     raw,
            "execution_safe":      False,
            "execution_rationale": "Could not parse Claude response - manual review required",
            "risk_level":          "HIGH",
            "estimated_effort":    "Unknown",
            "steps":               ["Manual review required"],
        }

    print(f"[Agent] Analysis: Risk={analysis.get('risk_level')} "
          f"Safe={analysis.get('execution_safe')}", flush=True)

    # ── Step 5: Create ACR + link + comments ─────────────────────────────────
    print(f"[Agent] Creating ACR in {JIRA_ACTION_PROJECT} ...", flush=True)

    steps_text = "\n".join(
        f"{i+1}. {s}" for i, s in enumerate(analysis.get("steps", []))
    )

    acr_description = (
        f"AUTO-GENERATED by ArdouraAI Agent\n\n"
        f"Linked ADEV issue: {issue_key}\n"
        f"Original summary: {issue.get('summary')}\n\n"
        f"--- ANALYSIS ---\n{analysis.get('acr_description', '')}\n\n"
        f"--- EXECUTION STEPS ---\n{steps_text}\n\n"
        f"--- RISK ASSESSMENT ---\n"
        f"Risk Level: {analysis.get('risk_level')}\n"
        f"Estimated Effort: {analysis.get('estimated_effort')}\n"
        f"Auto-Execution Safe: {analysis.get('execution_safe')}\n"
        f"Rationale: {analysis.get('execution_rationale')}\n"
    )

    async with stdio_client(_server("mcp_jira_server.py")) as (r, w):
        async with ClientSession(r, w) as jira:
            await jira.initialize()

            # Create ACR ticket
            acr_raw = await call_tool(jira, "jira_create_ticket", {
                "project_key": JIRA_ACTION_PROJECT,
                "summary":     analysis.get("acr_summary",
                               f"ACR: {issue.get('summary', '')[:80]}"),
                "description": acr_description,
                "issue_type":  "Task",
                "priority":    "High" if analysis.get("risk_level") == "HIGH" else "Medium",
                "labels":      ["auto-generated", "ardoura-ai",
                                f"risk-{analysis.get('risk_level', 'unknown').lower()}"],
            })
            acr = json.loads(acr_raw)
            acr_key = acr["key"]
            print(f"[Agent] ACR created: {acr_key}", flush=True)

            # Link ACR -> ADEV
            link_result = await call_tool(jira, "jira_link_issues", {
                "link_type":   "Fixes",
                "inward_key":  acr_key,
                "outward_key": issue_key,
            })
            print(f"[Agent] Linked {acr_key} -> {issue_key}: {link_result}", flush=True)

            # Comment on ADEV
            auto_exec = analysis.get("execution_safe")
            adev_comment = (
                f"ArdouraAI has analysed this issue and created ACR ticket *{acr_key}*.\n\n"
                f"*Risk Level:* {analysis.get('risk_level')}\n"
                f"*Estimated Effort:* {analysis.get('estimated_effort')}\n"
                f"*Auto-Execution:* "
                f"{'Yes - ticket transitioned to In Progress' if auto_exec else 'No - assigned for manual review'}\n"
                f"*Rationale:* {analysis.get('execution_rationale')}\n\n"
                f"View ACR: {ATLASSIAN_BASE}/browse/{acr_key}"
            )
            await call_tool(jira, "jira_add_comment", {
                "issue_key": issue_key,
                "comment":   adev_comment,
            })
            print(f"[Agent] Comment added to {issue_key}", flush=True)

            # Assign ACR to operator
            if OPERATOR_ACCOUNT_ID:
                await call_tool(jira, "jira_assign_issue", {
                    "issue_key":  acr_key,
                    "account_id": OPERATOR_ACCOUNT_ID,
                })
                print(f"[Agent] ACR assigned to {OPERATOR_ACCOUNT_ID}", flush=True)

            # Transition or leave open
            if auto_exec:
                print(f"[Agent] Safe to execute - transitioning {acr_key} to In Progress",
                      flush=True)
                await call_tool(jira, "jira_transition_issue", {
                    "issue_key":       acr_key,
                    "transition_name": "In Progress",
                })
                await call_tool(jira, "jira_add_comment", {
                    "issue_key": acr_key,
                    "comment":   (
                        f"ArdouraAI auto-executing this change (risk: LOW).\n\n"
                        f"Steps being executed:\n{steps_text}\n\n"
                        f"Original issue: {ATLASSIAN_BASE}/browse/{issue_key}"
                    ),
                })
            else:
                print(f"[Agent] NOT safe - leaving {acr_key} open for manual review",
                      flush=True)
                await call_tool(jira, "jira_add_comment", {
                    "issue_key": acr_key,
                    "comment":   (
                        f"ArdouraAI has flagged this for manual review.\n\n"
                        f"Reason: {analysis.get('execution_rationale')}\n"
                        f"Risk Level: {analysis.get('risk_level')}\n\n"
                        f"Please review the execution steps and transition to "
                        f"In Progress when ready.\n\n"
                        f"Original issue: {ATLASSIAN_BASE}/browse/{issue_key}"
                    ),
                })

    print(f"[Agent] DONE. ADEV={issue_key} ACR={acr_key} "
          f"Safe={auto_exec}", flush=True)


def main():
    parser = argparse.ArgumentParser(description="ArdouraAI Jira Agent")
    parser.add_argument("--issue", required=True, help="e.g. ADEV-42")
    args = parser.parse_args()
    asyncio.run(run_agent(args.issue))


if __name__ == "__main__":
    main()
