# See local copy at C:\Users\amath\OneDrive\Documents\python\mcp_observability_server.py
# MCP server for Dynatrace, Datadog, Grafana, Prometheus
# Gracefully skips any tool whose API key is not configured
# Tools: obs_get_dynatrace_problems, obs_get_dynatrace_events,
#        obs_get_datadog_monitors, obs_get_datadog_events,
#        obs_get_grafana_alerts, obs_prometheus_query,
#        obs_prometheus_alerts, obs_status
