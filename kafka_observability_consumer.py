"""
kafka_observability_consumer.py
================================
Kafka consumer for observability tool alerts.
Topics: dynatrace-alerts, datadog-alerts, grafana-alerts
Severity filtering per tool. Skips resolved alerts.
Triggers JiraConfluenceAIAgent_mcp.py --alert-source --alert-file
"""
import json, os, subprocess, sys, time
from pathlib import Path
from kafka import KafkaConsumer

BOOTSTRAP_SERVERS = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
GROUP_ID = "observability-agent-group"
AGENT_SCRIPT = str(Path(__file__).parent / "JiraConfluenceAIAgent_mcp.py")
TOPICS = ["dynatrace-alerts", "datadog-alerts", "grafana-alerts"]
DYNATRACE_MIN_SEVERITY = os.environ.get("DYNATRACE_MIN_SEVERITY", "ERROR")
DATADOG_MIN_SEVERITY   = os.environ.get("DATADOG_MIN_SEVERITY",   "error")
GRAFANA_MIN_SEVERITY   = os.environ.get("GRAFANA_MIN_SEVERITY",   "warning")
_DT_ORDER = ["RESOURCE","PERFORMANCE","ERROR","AVAILABILITY"]
_DD_ORDER = ["info","warning","error","critical"]
_GF_ORDER = ["info","warning","critical"]

def _should_process_dynatrace(alert):
    sev = (alert.get("severity") or "").upper()
    if not DYNATRACE_MIN_SEVERITY: return True, ""
    min_idx = _DT_ORDER.index(DYNATRACE_MIN_SEVERITY.upper()) if DYNATRACE_MIN_SEVERITY.upper() in _DT_ORDER else 0
    sev_idx = _DT_ORDER.index(sev) if sev in _DT_ORDER else -1
    return (sev_idx >= min_idx, f"severity {sev} below threshold {DYNATRACE_MIN_SEVERITY}" if sev_idx < min_idx else "")

def _should_process_datadog(alert):
    sev = (alert.get("severity") or "").lower()
    if not DATADOG_MIN_SEVERITY: return True, ""
    min_idx = _DD_ORDER.index(DATADOG_MIN_SEVERITY.lower()) if DATADOG_MIN_SEVERITY.lower() in _DD_ORDER else 0
    sev_idx = _DD_ORDER.index(sev) if sev in _DD_ORDER else -1
    return (sev_idx >= min_idx, f"severity {sev} below threshold {DATADOG_MIN_SEVERITY}" if sev_idx < min_idx else "")

def _should_process_grafana(alert):
    sev = (alert.get("severity") or "").lower()
    if not GRAFANA_MIN_SEVERITY: return True, ""
    min_idx = _GF_ORDER.index(GRAFANA_MIN_SEVERITY.lower()) if GRAFANA_MIN_SEVERITY.lower() in _GF_ORDER else 0
    sev_idx = _GF_ORDER.index(sev) if sev in _GF_ORDER else -1
    return (sev_idx >= min_idx, f"severity {sev} below threshold {GRAFANA_MIN_SEVERITY}" if sev_idx < min_idx else "")

TOPIC_GUARDS = {"dynatrace-alerts": _should_process_dynatrace, "datadog-alerts": _should_process_datadog, "grafana-alerts": _should_process_grafana}

def trigger_agent(alert, topic):
    tmp_path = Path(__file__).parent / f"_obs_alert_{int(time.time())}.json"
    try:
        tmp_path.write_text(json.dumps(alert), encoding="utf-8")
        result = subprocess.run([sys.executable, AGENT_SCRIPT, "--alert-source", alert.get("source", topic), "--alert-file", str(tmp_path)], capture_output=False)
        if result.returncode != 0: print(f"[ObsConsumer] WARNING -- agent exited {result.returncode} for '{alert.get('title')}'")
        else: print(f"[ObsConsumer] Agent completed for '{alert.get('title')}'")
    finally:
        try: tmp_path.unlink(missing_ok=True)
        except: pass

def main():
    print(f"[ObsConsumer] Starting -- topics: {TOPICS}")
    print(f"[ObsConsumer] Thresholds: Dynatrace={DYNATRACE_MIN_SEVERITY or 'ALL'} Datadog={DATADOG_MIN_SEVERITY or 'ALL'} Grafana={GRAFANA_MIN_SEVERITY or 'ALL'}")
    consumer = KafkaConsumer(*TOPICS, bootstrap_servers=BOOTSTRAP_SERVERS,
        value_deserializer=lambda v: json.loads(v.decode("utf-8")),
        auto_offset_reset="latest", group_id=GROUP_ID)
    for message in consumer:
        topic = message.topic; alert = message.value
        title = alert.get("title","unknown"); severity = alert.get("severity","UNKNOWN"); status = alert.get("status","unknown")
        print(f"\n[ObsConsumer] [{topic}] {title} | {severity} | {status}")
        guard = TOPIC_GUARDS.get(topic)
        if guard:
            ok, reason = guard(alert)
            if not ok: print(f"[ObsConsumer] Skipped -- {reason}"); continue
        if status in {"resolved","ok","closed","RESOLVED","CLOSED"}: print(f"[ObsConsumer] Skipped -- resolved"); continue
        print(f"[ObsConsumer] Triggering agent for: {alert.get('source',topic)} / {alert.get('alert_id','?')} -- {title}")
        trigger_agent(alert, topic)

if __name__ == "__main__": main()
