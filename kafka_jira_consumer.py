import os, json, subprocess, sys
from pathlib import Path
from kafka import KafkaConsumer
from dotenv import load_dotenv

load_dotenv()

# Reads kafka:9092 when running inside Docker Compose,
# falls back to localhost:9092 for local dev.
BOOTSTRAP_SERVERS    = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
TARGET_PROJECT_KEY   = os.environ.get("KAFKA_TARGET_PROJECT_KEY", "ADEV")
TARGET_WEBHOOK_EVENT = os.environ.get("KAFKA_TARGET_WEBHOOK_EVENT", "jira:issue_created")
GROUP_ID             = os.environ.get("KAFKA_GROUP_ID", "jira-agent-group")

AGENT_SCRIPT = str(Path(__file__).parent / "JiraConfluenceAIAgent_mcp.py")

print(f"[Consumer] Kafka bootstrap: {BOOTSTRAP_SERVERS}")
print(f"[Consumer] Listening for {TARGET_PROJECT_KEY} issue_created events on 'jira-events' ...")

consumer = KafkaConsumer(
    'jira-events',
    bootstrap_servers=BOOTSTRAP_SERVERS,
    value_deserializer=lambda v: json.loads(v.decode('utf-8')),
    auto_offset_reset='earliest',
    group_id=GROUP_ID
)

for message in consumer:
    event = message.value

    webhook_event = event.get('webhookEvent', '')
    issue         = event.get('issue', {})
    issue_key     = issue.get('key', '')
    project_key   = issue.get('fields', {}).get('project', {}).get('key', '')

    if webhook_event != TARGET_WEBHOOK_EVENT:
        print(f"[Consumer] Skipped -- event type '{webhook_event}' is not issue_created")
        continue

    if project_key != TARGET_PROJECT_KEY:
        print(f"[Consumer] Skipped -- issue {issue_key} belongs to project '{project_key}', not '{TARGET_PROJECT_KEY}'")
        continue

    if not issue_key:
        print("[Consumer] Skipped -- could not determine issue key from event payload")
        continue

    print(f"[Consumer] New {TARGET_PROJECT_KEY} issue detected: {issue_key} -- triggering MCP AI agent ...")

    result = subprocess.run(
        [sys.executable, AGENT_SCRIPT, "--issue", issue_key],
        capture_output=False
    )

    if result.returncode != 0:
        print(f"[Consumer] WARNING -- agent exited with code {result.returncode} for {issue_key}")
    else:
        print(f"[Consumer] Agent completed successfully for {issue_key}")
