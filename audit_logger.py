"""
audit_logger.py
===============
Centralised audit logger for ArdouraAI.

Every action the agent takes is appended as a single JSON line to
`ardoura_audit.log` in the same directory as this file.

Each log entry contains:
  timestamp     ISO-8601 UTC
  run_id        UUID shared across all events in one agent run
  issue_key     Jira issue being processed (e.g. ADEV-42)
  action        what happened (see ACTION_* constants below)
  status        success | failed | skipped | warning
  detail        free-text or structured payload
  actor         always "ArdouraAI"

Usage:
    from audit_logger import AuditLogger
    log = AuditLogger(issue_key="ADEV-42")
    log.jira_fetched(issue)
    log.claude_analysis(analysis)
    log.github_committed(path, sha)
    log.jira_ticket_created(key)
    log.sql_executed(sql, affected_rows)
    log.error(action, message)
"""

import json
import uuid
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ── Log file path ─────────────────────────────────────────────────────────────
LOG_FILE = Path(__file__).parent / "ardoura_audit.log"

# ── Action type constants ─────────────────────────────────────────────────────
ACTION_AGENT_START          = "agent_start"
ACTION_AGENT_END            = "agent_end"
ACTION_JIRA_FETCHED         = "jira_fetched"
ACTION_CONFLUENCE_FETCHED   = "confluence_fetched"
ACTION_DB_SCHEMA_LOADED     = "db_schema_loaded"
ACTION_CLAUDE_ANALYSIS      = "claude_analysis"
ACTION_GITHUB_LOADED        = "github_codebase_loaded"
ACTION_GITHUB_COMMITTED     = "github_committed"
ACTION_SQL_DRY_RUN          = "sql_dry_run"
ACTION_SQL_EXECUTED         = "sql_executed"
ACTION_SQL_SNAPSHOT         = "sql_snapshot"
ACTION_JIRA_TICKET_CREATED  = "jira_ticket_created"
ACTION_CONFLUENCE_UPDATED   = "confluence_updated"
ACTION_ERROR                = "error"
ACTION_WARNING              = "warning"


class AuditLogger:
    """
    One instance per agent run.  All events share the same run_id so
    you can reconstruct the full history of a single run.
    """

    def __init__(self, issue_key: str = "unknown"):
        self.run_id    = str(uuid.uuid4())
        self.issue_key = issue_key
        self._write(ACTION_AGENT_START, "success",
                    {"issue_key": issue_key, "pid": os.getpid()})

    # ── Public helpers ────────────────────────────────────────────────

    def jira_fetched(self, issue: dict) -> None:
        self._write(ACTION_JIRA_FETCHED, "success", {
            "key":       issue.get("key"),
            "summary":   issue.get("summary"),
            "status":    issue.get("status"),
            "priority":  issue.get("priority"),
            "issuetype": issue.get("issuetype"),
        })

    def confluence_fetched(self, page_id: str, char_count: int) -> None:
        self._write(ACTION_CONFLUENCE_FETCHED, "success",
                    {"page_id": page_id, "char_count": char_count})

    def db_schema_loaded(self, table_count: int) -> None:
        self._write(ACTION_DB_SCHEMA_LOADED, "success",
                    {"table_count": table_count})

    def claude_analysis(self, analysis: dict, pass_number: int = 1) -> None:
        self._write(ACTION_CLAUDE_ANALYSIS, "success", {
            "pass":           pass_number,
            "confidence":     analysis.get("confidence"),
            "action_type":    analysis.get("action_type"),
            "action_summary": analysis.get("action_summary"),
            "files_count":    len(analysis.get("files_to_change", [])),
            "has_sql":        bool(analysis.get("sql")),
        })

    def github_codebase_loaded(self, owner: str, repo: str,
                               file_count: int) -> None:
        self._write(ACTION_GITHUB_LOADED, "success",
                    {"owner": owner, "repo": repo, "file_count": file_count})

    def github_committed(self, path: str, sha: str,
                         owner: str, repo: str) -> None:
        self._write(ACTION_GITHUB_COMMITTED, "success",
                    {"path": path, "sha": sha,
                     "owner": owner, "repo": repo})

    def github_commit_failed(self, path: str, error: str) -> None:
        self._write(ACTION_GITHUB_COMMITTED, "failed",
                    {"path": path, "error": error})

    def sql_snapshot(self, tables: list[str], row_count: int) -> None:
        self._write(ACTION_SQL_SNAPSHOT, "success",
                    {"tables": tables, "total_rows_snapped": row_count})

    def sql_dry_run(self, sql: str, passed: bool, error: str = "") -> None:
        self._write(ACTION_SQL_DRY_RUN,
                    "success" if passed else "failed",
                    {"sql_preview": sql[:300], "error": error})

    def sql_executed(self, sql: str, affected_rows: int,
                     tables: list[str]) -> None:
        self._write(ACTION_SQL_EXECUTED, "success", {
            "sql_preview":   sql[:300],
            "affected_rows": affected_rows,
            "tables":        tables,
        })

    def sql_failed(self, sql: str, error: str) -> None:
        self._write(ACTION_SQL_EXECUTED, "failed",
                    {"sql_preview": sql[:300], "error": error})

    def jira_ticket_created(self, key: str, project: str,
                            summary: str) -> None:
        self._write(ACTION_JIRA_TICKET_CREATED, "success",
                    {"key": key, "project": project, "summary": summary})

    def confluence_updated(self, page_title: str) -> None:
        self._write(ACTION_CONFLUENCE_UPDATED, "success",
                    {"page_title": page_title})

    def warning(self, action: str, message: str,
                detail: dict | None = None) -> None:
        self._write(action, "warning",
                    {"message": message, **(detail or {})})

    def error(self, action: str, message: str,
              detail: dict | None = None) -> None:
        self._write(action, "failed",
                    {"message": message, **(detail or {})})

    def agent_end(self, status: str = "success") -> None:
        self._write(ACTION_AGENT_END, status, {})

    # ── Internal ──────────────────────────────────────────────────────

    def _write(self, action: str, status: str, detail: Any) -> None:
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "run_id":    self.run_id,
            "issue_key": self.issue_key,
            "actor":     "ArdouraAI",
            "action":    action,
            "status":    status,
            "detail":    detail,
        }
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception as exc:
            # Never let logging crash the main process
            print(f"[AuditLogger] WARNING: could not write to log: {exc}")
