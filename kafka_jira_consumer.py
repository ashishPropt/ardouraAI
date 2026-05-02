from kafka import KafkaConsumer
import json
import subprocess
import sys
import os

# Only process issues from this project (defence-in-depth — producer already filters)
TARGET_PROJECT_KEY   = "ADEV"
TARGET_WEBHOOK_EVENT = "jira:issue_created"

# Path to the MCP AI agent script — resolved relative to this file's location
AGENT_SCRIPT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "JiraConfluenceAIAgent_mcp.py"
)

consumer = KafkaConsumer(
    'jira-events',
    bootstrap_servers='localhost:9092',
    value_deserializer=lambda v: json.loads(v.decode('utf-8')),
    auto_offset_reset='earliest',
    group_id='jira-agent-group'
)

print(f"[Consumer] Listening for {TARGET_PROJECT_KEY} issue_created events on Kafka topic 'jira-events' ...")

for message in consumer:
    event = message.value

    webhook_event = event.get('webhookEvent', '')
    issue         = event.get('issue', {})
    issue_key     = issue.get('key', '')
    project_key   = issue.get('fields', {}).get('project', {}).get('key', '')

    # ── Guard 1: must be an issue_created event ───────────────────────
    if webhook_event != TARGET_WEBHOOK_EVENT:
        print(f"[Consumer] Skipped – event type '{webhook_event}' is not issue_created")
        continue

    # ── Guard 2: must be in the ADEV project ──────────────────────────
    if project_key != TARGET_PROJECT_KEY:
        print(f"[Consumer] Skipped – issue {issue_key} belongs to project '{project_key}', not '{TARGET_PROJECT_KEY}'")
        continue

    if not issue_key:
        print("[Consumer] Skipped – could not determine issue key from event payload")
        continue

    print(f"[Consumer] New {TARGET_PROJECT_KEY} issue detected: {issue_key} – triggering MCP AI agent ...")

    result = subprocess.run(
        [
            sys.executable,
            AGENT_SCRIPT,
            "--issue", issue_key
        ],
        capture_output=False   # let agent output flow to console
    )

    if result.returncode != 0:
        print(f"[Consumer] WARNING – agent exited with code {result.returncode} for {issue_key}")
    else:
        print(f"[Consumer] Agent completed successfully for {issue_key}")
