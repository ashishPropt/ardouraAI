#!/usr/bin/env python3
"""
tenant/multitenant_consumer.py
==============================
Multi-tenant Kafka consumer.
Dynamically subscribes to all active tenant topics and routes
events to the correct tenant's AI agent.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from threading import Thread

from kafka import KafkaConsumer
from kafka.admin import KafkaAdminClient, NewTopic
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent.parent))
from tenant.tenant_registry import list_active_tenants

load_dotenv()

BOOTSTRAP_SERVERS = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "kafka:9092")
REFRESH_INTERVAL  = int(os.environ.get("TENANT_REFRESH_INTERVAL", 60))


def _topic(tenant_id: str, kind: str) -> str:
    safe = tenant_id.replace("-", "")[:12]
    return f"t-{safe}-{kind}"


def ensure_topics(tenant_ids: list[str]) -> None:
    """Create Kafka topics for new tenants."""
    try:
        admin = KafkaAdminClient(bootstrap_servers=BOOTSTRAP_SERVERS)
        existing = set(admin.list_topics())
        new_topics = []
        for tid in tenant_ids:
            for kind in ["jira-events", "dynatrace-alerts",
                          "datadog-alerts", "grafana-alerts"]:
                topic = _topic(tid, kind)
                if topic not in existing:
                    new_topics.append(NewTopic(
                        name=topic, num_partitions=1, replication_factor=1
                    ))
        if new_topics:
            admin.create_topics(new_topics)
            print(f"[Consumer] Created {len(new_topics)} new topics",
                  flush=True)
        admin.close()
    except Exception as e:
        print(f"[Consumer] WARNING: topic creation failed: {e}", flush=True)


def handle_jira_event(tenant_id: str, event: dict) -> None:
    issue_key = event.get("issue", {}).get("key", "")
    if not issue_key:
        print(f"[Consumer] Tenant={tenant_id}: no issue key in event",
              flush=True)
        return
    print(f"[Consumer] Tenant={tenant_id}: triggering agent for {issue_key}",
          flush=True)
    result = subprocess.run(
        [sys.executable, "tenant/tenant_agent.py",
         "--tenant-id", tenant_id,
         "--issue",     issue_key],
        capture_output=False
    )
    if result.returncode != 0:
        print(f"[Consumer] WARNING: agent failed for {issue_key} "
              f"(tenant={tenant_id})", flush=True)


def consume_loop(topics: list[str],
                 tenant_map: dict[str, str]) -> None:
    """
    Long-running consumer loop for a set of tenant topics.
    tenant_map: {topic -> tenant_id}
    """
    if not topics:
        return
    consumer = KafkaConsumer(
        *topics,
        bootstrap_servers=BOOTSTRAP_SERVERS,
        value_deserializer=lambda v: json.loads(v.decode("utf-8")),
        auto_offset_reset="latest",
        group_id="ardoura-multitenant-consumer",
        consumer_timeout_ms=REFRESH_INTERVAL * 1000,
    )
    for msg in consumer:
        tenant_id = tenant_map.get(msg.topic, "")
        if not tenant_id:
            continue
        event = msg.value
        # Route by topic type
        if "jira-events" in msg.topic:
            handle_jira_event(tenant_id, event)
        else:
            print(f"[Consumer] Tenant={tenant_id}: "
                  f"observability event on {msg.topic}", flush=True)
    consumer.close()


def main() -> None:
    print("[MultiTenantConsumer] Starting ...", flush=True)
    while True:
        tenants = list_active_tenants()
        if not tenants:
            print("[MultiTenantConsumer] No active tenants. Retrying in 30s.",
                  flush=True)
            time.sleep(30)
            continue

        tenant_ids = [t["tenant_id"] for t in tenants]
        ensure_topics(tenant_ids)

        # Build topic -> tenant_id map
        tenant_map: dict[str, str] = {}
        topics: list[str] = []
        for t in tenants:
            tid = t["tenant_id"]
            for kind in ["jira-events", "dynatrace-alerts",
                          "datadog-alerts", "grafana-alerts"]:
                topic = _topic(tid, kind)
                topics.append(topic)
                tenant_map[topic] = tid

        print(f"[MultiTenantConsumer] Consuming {len(topics)} topics "
              f"for {len(tenants)} tenant(s)", flush=True)

        # Run consumer (blocks until consumer_timeout_ms)
        consume_loop(topics, tenant_map)

        # Refresh tenant list
        print("[MultiTenantConsumer] Refreshing tenant list ...", flush=True)


if __name__ == "__main__":
    main()
