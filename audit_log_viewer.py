"""
audit_log_viewer.py
===================
Searchable CLI viewer for the ArdouraAI audit log.

Usage examples:
  # Show all log entries
  python audit_log_viewer.py

  # Filter by Jira issue key
  python audit_log_viewer.py --issue ADEV-42

  # Filter by action type
  python audit_log_viewer.py --action github_committed
  python audit_log_viewer.py --action sql_executed

  # Filter by status
  python audit_log_viewer.py --status failed
  python audit_log_viewer.py --status success

  # Filter by date range
  python audit_log_viewer.py --from 2025-01-01
  python audit_log_viewer.py --from 2025-01-01 --to 2025-01-31

  # Filter by run ID
  python audit_log_viewer.py --run abc123

  # Free-text search across all fields
  python audit_log_viewer.py --search "register.php"

  # Combine filters
  python audit_log_viewer.py --issue ADEV-42 --status failed
  python audit_log_viewer.py --action sql_executed --from 2025-01-01

  # Show a summary (counts per action/status)
  python audit_log_viewer.py --summary

  # Output as raw JSON (for piping)
  python audit_log_viewer.py --issue ADEV-42 --json

  # Show last N entries
  python audit_log_viewer.py --last 20

Available --action values:
  agent_start, agent_end,
  jira_fetched, confluence_fetched, db_schema_loaded,
  claude_analysis, github_codebase_loaded,
  github_committed, sql_dry_run, sql_executed, sql_snapshot,
  jira_ticket_created, confluence_updated,
  error, warning
"""

import json
import argparse
import sys
from pathlib import Path
from datetime import datetime, timezone
from collections import defaultdict

LOG_FILE = Path(__file__).parent / "ardoura_audit.log"

# ── ANSI colours (disabled on Windows if not supported) ──────────────────────
COLOURS = {
    "success": "\033[32m",   # green
    "failed":  "\033[31m",   # red
    "warning": "\033[33m",   # yellow
    "skipped": "\033[36m",   # cyan
    "reset":   "\033[0m",
    "bold":    "\033[1m",
    "dim":     "\033[2m",
}

# Disable colours on Windows cmd (unless ANSI is enabled)
if sys.platform == "win32":
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleMode(
            ctypes.windll.kernel32.GetStdHandle(-11), 7
        )
    except Exception:
        COLOURS = {k: "" for k in COLOURS}


def _c(colour: str, text: str) -> str:
    return f"{COLOURS.get(colour, '')}{text}{COLOURS['reset']}"


def load_entries() -> list[dict]:
    if not LOG_FILE.exists():
        print(f"[viewer] Log file not found: {LOG_FILE}")
        return []
    entries = []
    with open(LOG_FILE, encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                print(f"[viewer] WARNING: invalid JSON on line {lineno} — skipped")
    return entries


def parse_ts(ts: str) -> datetime:
    try:
        return datetime.fromisoformat(ts)
    except Exception:
        return datetime.min.replace(tzinfo=timezone.utc)


def apply_filters(entries: list[dict], args: argparse.Namespace) -> list[dict]:
    result = entries

    if args.issue:
        q = args.issue.upper()
        result = [e for e in result if q in e.get("issue_key", "").upper()]

    if args.action:
        q = args.action.lower()
        result = [e for e in result if q in e.get("action", "").lower()]

    if args.status:
        q = args.status.lower()
        result = [e for e in result if e.get("status", "").lower() == q]

    if args.run:
        q = args.run.lower()
        result = [e for e in result if q in e.get("run_id", "").lower()]

    if args.search:
        q = args.search.lower()
        result = [
            e for e in result
            if q in json.dumps(e).lower()
        ]

    if getattr(args, "from", None):
        try:
            from_dt = datetime.fromisoformat(getattr(args, "from")).replace(
                tzinfo=timezone.utc)
            result = [e for e in result if parse_ts(e["timestamp"]) >= from_dt]
        except ValueError:
            print(f"[viewer] WARNING: invalid --from date, ignoring filter")

    if args.to:
        try:
            to_dt = datetime.fromisoformat(args.to).replace(tzinfo=timezone.utc)
            result = [e for e in result if parse_ts(e["timestamp"]) <= to_dt]
        except ValueError:
            print(f"[viewer] WARNING: invalid --to date, ignoring filter")

    if args.last:
        result = result[-args.last:]

    return result


def print_summary(entries: list[dict]) -> None:
    by_action: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for e in entries:
        by_action[e.get("action", "unknown")][e.get("status", "unknown")] += 1

    print(_c("bold", f"\n{'─'*56}"))
    print(_c("bold", f"  ArdouraAI Audit Log — Summary ({len(entries)} entries)"))
    print(_c("bold", f"{'─'*56}"))
    print(f"  {'ACTION':<30}  {'SUCCESS':>7}  {'FAILED':>7}  {'WARNING':>7}")
    print(f"  {'─'*30}  {'─'*7}  {'─'*7}  {'─'*7}")
    for action in sorted(by_action):
        counts = by_action[action]
        s = counts.get("success", 0)
        f = counts.get("failed", 0)
        w = counts.get("warning", 0)
        s_str = _c("success", str(s)) if s else _c("dim", "0")
        f_str = _c("failed",  str(f)) if f else _c("dim", "0")
        w_str = _c("warning", str(w)) if w else _c("dim", "0")
        print(f"  {action:<30}  {s_str:>7}  {f_str:>7}  {w_str:>7}")
    print(_c("bold", f"{'─'*56}\n"))


def print_entries(entries: list[dict]) -> None:
    if not entries:
        print("  (no matching entries)")
        return

    print(_c("bold", f"\n{'─'*70}"))
    print(_c("bold", f"  ArdouraAI Audit Log ({len(entries)} entries)"))
    print(_c("bold", f"{'─'*70}"))

    for e in entries:
        ts        = e.get("timestamp", "")[:19].replace("T", " ")
        run_short = e.get("run_id", "")[:8]
        issue     = e.get("issue_key", "-")
        action    = e.get("action", "-")
        status    = e.get("status", "-")
        detail    = e.get("detail", {})

        status_str = _c(status, f"[{status.upper():<7}]")
        action_str = _c("bold", f"{action:<30}")

        # Build a concise one-line detail string
        detail_parts = []
        if isinstance(detail, dict):
            priority_keys = [
                "key", "path", "sha", "sql_preview", "summary",
                "page_title", "page_id", "affected_rows", "file_count",
                "table_count", "tables", "error", "message",
                "action_type", "confidence", "action_summary",
            ]
            for k in priority_keys:
                if k in detail and detail[k] not in (None, "", [], {}):
                    val = detail[k]
                    if isinstance(val, str) and len(val) > 80:
                        val = val[:77] + "..."
                    detail_parts.append(f"{k}={val}")
                    if len(detail_parts) >= 4:
                        break
        detail_str = "  ".join(str(p) for p in detail_parts)

        print(
            f"  {_c('dim', ts)}  "
            f"{_c('dim', run_short)}  "
            f"{issue:<10}  "
            f"{status_str}  "
            f"{action_str}  "
            f"{_c('dim', detail_str)}"
        )

    print(_c("bold", f"{'─'*70}\n"))


def main():
    parser = argparse.ArgumentParser(
        description="ArdouraAI Audit Log Viewer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--issue",   help="Filter by Jira issue key (partial match)")
    parser.add_argument("--action",  help="Filter by action type")
    parser.add_argument("--status",  help="Filter by status: success | failed | warning")
    parser.add_argument("--run",     help="Filter by run ID (partial match)")
    parser.add_argument("--search",  help="Free-text search across all fields")
    parser.add_argument("--from",    dest="from", metavar="DATE",
                        help="Start date (ISO format: 2025-01-01)")
    parser.add_argument("--to",      metavar="DATE",
                        help="End date   (ISO format: 2025-01-31)")
    parser.add_argument("--last",    type=int, metavar="N",
                        help="Show last N entries")
    parser.add_argument("--summary", action="store_true",
                        help="Show a count summary grouped by action/status")
    parser.add_argument("--json",    action="store_true",
                        help="Output raw JSON (one entry per line)")
    parser.add_argument("--log",     help="Path to a different log file")

    args = parser.parse_args()

    global LOG_FILE
    if args.log:
        LOG_FILE = Path(args.log)

    entries = load_entries()
    if not entries:
        sys.exit(0)

    filtered = apply_filters(entries, args)

    if args.json:
        for e in filtered:
            print(json.dumps(e))
        return

    if args.summary:
        print_summary(filtered)
        return

    print_entries(filtered)


if __name__ == "__main__":
    main()
