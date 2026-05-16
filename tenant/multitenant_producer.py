#!/usr/bin/env python3
"""
tenant/multitenant_producer.py
==============================
Multi-tenant webhook receiver.
Routes incoming webhooks to the correct tenant's Kafka topic.

Endpoints:
  POST /webhook/{tenant_id}/jira
  POST /webhook/{tenant_id}/dynatrace
  POST /webhook/{tenant_id}/datadog
  POST /webhook/{tenant_id}/grafana
  GET  /health
  GET  /tenant/{tenant_id}/status
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import time
from pathlib import Path

from flask import Flask, request, jsonify, abort
from kafka import KafkaProducer
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent.parent))
from tenant.tenant_registry import get_tenant, log_event

load_dotenv()

BOOTSTRAP_SERVERS = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "kafka:9092")
FLASK_PORT        = int(os.environ.get("FLASK_PORT", 5000))

app = Flask(__name__)
producer = KafkaProducer(
    bootstrap_servers=BOOTSTRAP_SERVERS,
    value_serializer=lambda v: json.dumps(v).encode("utf-8")
)

# Topic names per tenant: {tenant_id}-jira-events, {tenant_id}-dynatrace-alerts
def _topic(tenant_id: str, kind: str) -> str:
    safe = tenant_id.replace("-", "")[:12]
    return f"t-{safe}-{kind}"


def _verify_webhook_secret(tenant: dict, payload: bytes,
                            signature_header: str) -> bool:
    """Validate Jira/generic HMAC signature if provided."""
    expected = tenant.get("webhook_secret", "")
    if not expected or not signature_header:
        return True  # No secret configured - allow
    mac = hmac.new(expected.encode(), payload, hashlib.sha256)
    return hmac.compare_digest(f"sha256={mac.hexdigest()}", signature_header)


def _get_active_tenant(tenant_id: str) -> dict:
    tenant = get_tenant(tenant_id)
    if not tenant:
        abort(404, description=f"Tenant {tenant_id} not found")
    if tenant["status"] != "active":
        abort(403, description=f"Tenant {tenant_id} is {tenant['status']}")
    return tenant


# ── Jira webhook ──────────────────────────────────────────────────────────────
@app.route("/webhook/<tenant_id>/jira", methods=["POST"])
def jira_webhook(tenant_id: str):
    tenant = _get_active_tenant(tenant_id)
    data   = request.json or {}

    webhook_event = data.get("webhookEvent", "")
    issue         = data.get("issue", {})
    issue_key     = issue.get("key", "")
    project_key   = issue.get("fields", {}).get("project", {}).get("key", "")

    # Tenant-specific filter from their secrets
    target_event   = "jira:issue_created"
    target_project = data.get("_ardoura_project", project_key)  # use actual project

    if webhook_event != target_event:
        return jsonify({"status": "ignored", "reason": "not issue_created"}), 200

    # Enrich event with tenant context
    event = {
        **data,
        "_ardoura_tenant_id": tenant_id,
        "_ardoura_tenant_name": tenant["name"],
        "_ardoura_tools": tenant["tools"],
    }

    topic = _topic(tenant_id, "jira-events")
    producer.send(topic, event)
    producer.flush()
    log_event(tenant_id, "jira_webhook_received",
               {"issue": issue_key, "topic": topic})
    print(f"[Producer] Tenant={tenant['name']} -> Kafka '{topic}': {issue_key}",
          flush=True)
    return jsonify({"status": "ok", "issue": issue_key, "topic": topic}), 200


# ── Dynatrace webhook ────────────────────────────────────────────────────────
@app.route("/webhook/<tenant_id>/dynatrace", methods=["POST"])
def dynatrace_webhook(tenant_id: str):
    tenant = _get_active_tenant(tenant_id)
    data   = request.json or {}
    alert  = {
        "source":      "dynatrace",
        "alert_id":    data.get("PID", "unknown"),
        "title":       data.get("ProblemTitle", "Dynatrace Alert"),
        "severity":    data.get("ProblemSeverity", "UNKNOWN"),
        "status":      data.get("State", "OPEN"),
        "timestamp":   int(time.time()),
        "raw":         data,
        "_ardoura_tenant_id": tenant_id,
    }
    topic = _topic(tenant_id, "dynatrace-alerts")
    producer.send(topic, alert)
    producer.flush()
    return jsonify({"status": "ok"}), 200


# ── Datadog webhook ────────────────────────────────────────────────────────────
@app.route("/webhook/<tenant_id>/datadog", methods=["POST"])
def datadog_webhook(tenant_id: str):
    tenant = _get_active_tenant(tenant_id)
    data   = request.json or {}
    alert  = {
        "source":    "datadog",
        "alert_id":  str(data.get("id", "unknown")),
        "title":     data.get("title", "Datadog Alert"),
        "severity":  data.get("alert_type", "error"),
        "status":    data.get("alert_status", "triggered"),
        "timestamp": int(time.time()),
        "raw":       data,
        "_ardoura_tenant_id": tenant_id,
    }
    topic = _topic(tenant_id, "datadog-alerts")
    producer.send(topic, alert)
    producer.flush()
    return jsonify({"status": "ok"}), 200


# ── Grafana webhook ────────────────────────────────────────────────────────────
@app.route("/webhook/<tenant_id>/grafana", methods=["POST"])
def grafana_webhook(tenant_id: str):
    tenant = _get_active_tenant(tenant_id)
    data   = request.json or {}
    alert  = {
        "source":    "grafana",
        "alert_id":  data.get("groupKey", "unknown"),
        "title":     data.get("title", "Grafana Alert"),
        "severity":  "UNKNOWN",
        "status":    data.get("state", "alerting"),
        "timestamp": int(time.time()),
        "raw":       data,
        "_ardoura_tenant_id": tenant_id,
    }
    topic = _topic(tenant_id, "grafana-alerts")
    producer.send(topic, alert)
    producer.flush()
    return jsonify({"status": "ok"}), 200


# ── Health + status ──────────────────────────────────────────────────────────
@app.route("/health")
def health():
    return jsonify({
        "status":  "running",
        "kafka":   BOOTSTRAP_SERVERS,
        "mode":    "multi-tenant",
    }), 200


@app.route("/tenant/<tenant_id>/status")
def tenant_status(tenant_id: str):
    tenant = get_tenant(tenant_id)
    if not tenant:
        return jsonify({"error": "not found"}), 404
    return jsonify({
        "tenant_id": tenant["tenant_id"],
        "name":      tenant["name"],
        "status":    tenant["status"],
        "plan":      tenant["plan"],
        "tools":     tenant["tools"],
        "webhook_base": f"/webhook/{tenant_id}/",
    }), 200


if __name__ == "__main__":
    print(f"[MultiTenant] Starting on port {FLASK_PORT}", flush=True)
    app.run(host="0.0.0.0", port=FLASK_PORT)
