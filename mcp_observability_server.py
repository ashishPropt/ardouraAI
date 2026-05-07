"""
mcp_observability_server.py
============================
MCP server for observability tool integrations:
  - Dynatrace
  - Datadog
  - Grafana / Prometheus

IMPORTANT: This server starts successfully even when API keys are
not configured.  Every tool checks its own credentials at call-time
and returns a graceful error payload instead of raising an exception.
This means the agent pipeline never crashes due to a missing key.

Environment variables (all optional -- missing = tool skipped gracefully):
    DYNATRACE_URL          e.g. https://abc12345.live.dynatrace.com
    DYNATRACE_API_TOKEN    Dynatrace API v2 token (scopes: problems.read, events.read)
    DATADOG_API_KEY        Datadog API key
    DATADOG_APP_KEY        Datadog application key
    DATADOG_SITE           e.g. datadoghq.com (default) or datadoghq.eu
    GRAFANA_URL            e.g. http://localhost:3000
    GRAFANA_API_KEY        Grafana service account token
    PROMETHEUS_URL         e.g. http://localhost:9090
"""

import json
import os
import sys
from datetime import datetime, timezone

try:
    import requests as _requests
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False

def _send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

def _read_line():
    line = sys.stdin.readline()
    if not line: return None
    try: return json.loads(line.strip())
    except: return None

def _missing(name): return {"error": f"NOT_CONFIGURED: {name} is not set", "configured": False}
def _creds_dynatrace():
    url = os.environ.get("DYNATRACE_URL","").rstrip("/")
    token = os.environ.get("DYNATRACE_API_TOKEN","")
    return (url, token) if url and token else None
def _creds_datadog():
    api_key = os.environ.get("DATADOG_API_KEY","")
    app_key = os.environ.get("DATADOG_APP_KEY","")
    site = os.environ.get("DATADOG_SITE","datadoghq.com")
    return (api_key, app_key, site) if api_key and app_key else None
def _creds_grafana():
    url = os.environ.get("GRAFANA_URL","").rstrip("/")
    key = os.environ.get("GRAFANA_API_KEY","")
    return (url, key) if url and key else None
def _creds_prometheus():
    url = os.environ.get("PROMETHEUS_URL","").rstrip("/")
    return url if url else None

def _tool_get_dynatrace_problems(args):
    creds = _creds_dynatrace()
    if not creds: return _missing("DYNATRACE_URL / DYNATRACE_API_TOKEN")
    if not _HAS_REQUESTS: return {"error": "requests library not installed"}
    url, token = creds
    try:
        r = _requests.get(f"{url}/api/v2/problems",
            headers={"Authorization": f"Api-Token {token}"},
            params={"problemSelector": args.get("problem_selector","status(\"OPEN\")"), "pageSize": args.get("page_size",20)}, timeout=15)
        r.raise_for_status()
        problems = [{"id": p.get("problemId"), "title": p.get("title"), "severity": p.get("severityLevel"),
                     "status": p.get("status"), "impact": p.get("impactLevel"), "start_time": p.get("startTime"),
                     "affected": [e.get("name") for e in p.get("affectedEntities",[])],
                     "root_cause": p.get("rootCauseEntity",{}).get("name")} for p in r.json().get("problems",[])]
        return {"source": "dynatrace", "problems": problems, "count": len(problems)}
    except Exception as e: return {"error": str(e), "source": "dynatrace"}

def _tool_get_dynatrace_events(args):
    creds = _creds_dynatrace()
    if not creds: return _missing("DYNATRACE_URL / DYNATRACE_API_TOKEN")
    if not _HAS_REQUESTS: return {"error": "requests library not installed"}
    url, token = creds
    try:
        params = {"pageSize": args.get("page_size",20)}
        if args.get("event_type"): params["eventTypeSelector"] = args["event_type"]
        r = _requests.get(f"{url}/api/v2/events", headers={"Authorization": f"Api-Token {token}"}, params=params, timeout=15)
        r.raise_for_status()
        events = [{"id": e.get("eventId"), "title": e.get("title"), "type": e.get("eventType"),
                   "entity": e.get("entityId",{}).get("name"), "start": e.get("startTime")} for e in r.json().get("events",[])]
        return {"source": "dynatrace", "events": events, "count": len(events)}
    except Exception as e: return {"error": str(e), "source": "dynatrace"}

def _tool_get_datadog_monitors(args):
    creds = _creds_datadog()
    if not creds: return _missing("DATADOG_API_KEY / DATADOG_APP_KEY")
    if not _HAS_REQUESTS: return {"error": "requests library not installed"}
    api_key, app_key, site = creds
    try:
        r = _requests.get(f"https://api.{site}/api/v1/monitor/groups/search",
            headers={"DD-API-KEY": api_key, "DD-APPLICATION-KEY": app_key},
            params={"monitor_states": args.get("states","Alert,Warn,No Data")}, timeout=15)
        r.raise_for_status()
        alerts = [{"monitor_id": g.get("monitor_id"), "monitor_name": g.get("monitor_name"),
                   "status": g.get("status"), "group": g.get("group"),
                   "last_triggered": g.get("last_triggered_ts")} for g in r.json().get("groups",[])]
        return {"source": "datadog", "alerts": alerts, "count": len(alerts)}
    except Exception as e: return {"error": str(e), "source": "datadog"}

def _tool_get_datadog_events(args):
    creds = _creds_datadog()
    if not creds: return _missing("DATADOG_API_KEY / DATADOG_APP_KEY")
    if not _HAS_REQUESTS: return {"error": "requests library not installed"}
    api_key, app_key, site = creds
    now = int(datetime.now(timezone.utc).timestamp())
    try:
        r = _requests.get(f"https://api.{site}/api/v1/events",
            headers={"DD-API-KEY": api_key, "DD-APPLICATION-KEY": app_key},
            params={"start": args.get("start",now-3600), "end": args.get("end",now), "priority": args.get("priority","normal")}, timeout=15)
        r.raise_for_status()
        events = [{"id": e.get("id"), "title": e.get("title"), "text": e.get("text","")[:200],
                   "tags": e.get("tags",[]), "alert_type": e.get("alert_type"),
                   "date_happened": e.get("date_happened")} for e in r.json().get("events",[])]
        return {"source": "datadog", "events": events, "count": len(events)}
    except Exception as e: return {"error": str(e), "source": "datadog"}

def _tool_get_grafana_alerts(args):
    creds = _creds_grafana()
    if not creds: return _missing("GRAFANA_URL / GRAFANA_API_KEY")
    if not _HAS_REQUESTS: return {"error": "requests library not installed"}
    url, key = creds
    try:
        r = _requests.get(f"{url}/api/alertmanager/grafana/api/v2/alerts",
            headers={"Authorization": f"Bearer {key}"}, params={"state": args.get("state","firing")}, timeout=15)
        r.raise_for_status()
        alerts = []
        for a in r.json():
            labels = a.get("labels",{}); annots = a.get("annotations",{})
            alerts.append({"fingerprint": a.get("fingerprint"), "status": a.get("status",{}).get("state"),
                           "name": labels.get("alertname"), "severity": labels.get("severity"),
                           "summary": annots.get("summary"), "description": annots.get("description","")[:300],
                           "starts_at": a.get("startsAt"), "labels": labels})
        return {"source": "grafana", "alerts": alerts, "count": len(alerts)}
    except Exception as e: return {"error": str(e), "source": "grafana"}

def _tool_prometheus_query(args):
    url = _creds_prometheus()
    if not url: return _missing("PROMETHEUS_URL")
    if not _HAS_REQUESTS: return {"error": "requests library not installed"}
    query = args.get("query","")
    if not query: return {"error": "query parameter is required"}
    try:
        r = _requests.get(f"{url}/api/v1/query", params={"query": query}, timeout=15)
        r.raise_for_status()
        data = r.json().get("data",{})
        return {"source": "prometheus", "query": query, "result_type": data.get("resultType"), "result": data.get("result",[])}
    except Exception as e: return {"error": str(e), "source": "prometheus"}

def _tool_prometheus_alerts(args):
    url = _creds_prometheus()
    if not url: return _missing("PROMETHEUS_URL")
    if not _HAS_REQUESTS: return {"error": "requests library not installed"}
    try:
        r = _requests.get(f"{url}/api/v1/alerts", timeout=15)
        r.raise_for_status()
        alerts = r.json().get("data",{}).get("alerts",[])
        firing = [a for a in alerts if a.get("state")=="firing"] if not args.get("all") else alerts
        return {"source": "prometheus", "count": len(firing),
                "alerts": [{"name": a["labels"].get("alertname"), "severity": a["labels"].get("severity"),
                             "state": a.get("state"), "summary": a.get("annotations",{}).get("summary"),
                             "labels": a.get("labels",{}), "active_at": a.get("activeAt")} for a in firing]}
    except Exception as e: return {"error": str(e), "source": "prometheus"}

def _tool_observability_status(args):
    return {"dynatrace": _creds_dynatrace() is not None, "datadog": _creds_datadog() is not None,
            "grafana": _creds_grafana() is not None, "prometheus": _creds_prometheus() is not None}

TOOLS = {
    "obs_get_dynatrace_problems": _tool_get_dynatrace_problems,
    "obs_get_dynatrace_events":   _tool_get_dynatrace_events,
    "obs_get_datadog_monitors":   _tool_get_datadog_monitors,
    "obs_get_datadog_events":     _tool_get_datadog_events,
    "obs_get_grafana_alerts":     _tool_get_grafana_alerts,
    "obs_prometheus_query":       _tool_prometheus_query,
    "obs_prometheus_alerts":      _tool_prometheus_alerts,
    "obs_status":                 _tool_observability_status,
}

TOOL_SCHEMAS = [
    {"name": "obs_get_dynatrace_problems", "description": "Fetch open Dynatrace problems. Graceful if not configured.", "inputSchema": {"type": "object", "properties": {"problem_selector": {"type": "string"}, "page_size": {"type": "integer"}}}},
    {"name": "obs_get_dynatrace_events",   "description": "Fetch recent Dynatrace events.",   "inputSchema": {"type": "object", "properties": {"event_type": {"type": "string"}, "page_size": {"type": "integer"}}}},
    {"name": "obs_get_datadog_monitors",   "description": "Fetch triggered Datadog monitors.", "inputSchema": {"type": "object", "properties": {"states": {"type": "string"}}}},
    {"name": "obs_get_datadog_events",     "description": "Fetch Datadog events stream.",      "inputSchema": {"type": "object", "properties": {"start": {"type": "integer"}, "end": {"type": "integer"}, "priority": {"type": "string"}}}},
    {"name": "obs_get_grafana_alerts",     "description": "Fetch firing Grafana alerts.",      "inputSchema": {"type": "object", "properties": {"state": {"type": "string"}}}},
    {"name": "obs_prometheus_query",       "description": "Run a PromQL instant query.",        "inputSchema": {"type": "object", "required": ["query"], "properties": {"query": {"type": "string"}}}},
    {"name": "obs_prometheus_alerts",      "description": "Fetch firing Prometheus alerts.",    "inputSchema": {"type": "object", "properties": {"all": {"type": "boolean"}}}},
    {"name": "obs_status",                 "description": "Return which tools are configured.", "inputSchema": {"type": "object", "properties": {}}},
]

def main():
    while True:
        msg = _read_line()
        if msg is None: break
        method = msg.get("method",""); msg_id = msg.get("id")
        if method == "initialize":
            _send({"jsonrpc":"2.0","id":msg_id,"result":{"protocolVersion":"2024-11-05","capabilities":{"tools":{}},"serverInfo":{"name":"mcp_observability_server","version":"1.0"}}})
        elif method == "notifications/initialized": pass
        elif method == "tools/list":
            _send({"jsonrpc":"2.0","id":msg_id,"result":{"tools":TOOL_SCHEMAS}})
        elif method == "tools/call":
            params = msg.get("params",{}); tool_name = params.get("name",""); arguments = params.get("arguments",{})
            handler = TOOLS.get(tool_name)
            try: result = handler(arguments) if handler else {"error": f"Unknown tool: {tool_name}"}
            except Exception as exc: result = {"error": str(exc)}
            _send({"jsonrpc":"2.0","id":msg_id,"result":{"content":[{"type":"text","text":json.dumps(result)}]}})
        else:
            if msg_id is not None: _send({"jsonrpc":"2.0","id":msg_id,"error":{"code":-32601,"message":f"Method not found: {method}"}})

if __name__ == "__main__": main()
