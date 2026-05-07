# Full content committed locally to C:\Users\amath\OneDrive\Documents\python\JiraConfluenceAIAgent_mcp.py
# Key additions in this commit:
#   - --alert-source and --alert-file CLI args (observability mode)
#   - parse_args() / resolve_alert() functions
#   - analyse_alert_with_confluence() and analyse_alert_with_codebase() (obs analysis)
#   - process_alert() orchestrator for observability path
#   - _commit_changes() shared helper (used by both Jira and alert paths)
#   - Severity -> Jira priority mapping for alert tickets
#   - Alert tickets labelled with obs-dynatrace / obs-datadog / obs-grafana
