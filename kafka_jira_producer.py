from flask import Flask, request
from kafka import KafkaProducer
import json

app = Flask(__name__)

producer = KafkaProducer(
    bootstrap_servers='localhost:9092',
    value_serializer=lambda v: json.dumps(v).encode('utf-8')
)

# Only fire the agent for issues created in this project
TARGET_PROJECT_KEY = "ADEV"
TARGET_WEBHOOK_EVENT = "jira:issue_created"


@app.route('/jira-webhook', methods=['POST'])
def jira_webhook():
    data = request.json

    webhook_event  = data.get('webhookEvent', '')
    issue          = data.get('issue', {})
    issue_key      = issue.get('key', '')
    project_key    = issue.get('fields', {}).get('project', {}).get('key', '')

    # ── Filter: only forward ADEV issue_created events ────────────────
    if webhook_event != TARGET_WEBHOOK_EVENT:
        print(f"[Producer] Ignored event '{webhook_event}' (not an issue_created)")
        return {'status': 'ignored', 'reason': 'not issue_created'}, 200

    if project_key != TARGET_PROJECT_KEY:
        print(f"[Producer] Ignored issue {issue_key} – project '{project_key}' is not '{TARGET_PROJECT_KEY}'")
        return {'status': 'ignored', 'reason': f'project is {project_key}'}, 200

    # ── Forward to Kafka ───────────────────────────────────────────────
    producer.send('jira-events', data)
    producer.flush()
    print(f"[Producer] Forwarded to Kafka: {issue_key} ({webhook_event})")
    return {'status': 'ok', 'issue': issue_key}, 200


@app.route('/health', methods=['GET'])
def health():
    return {'status': 'Flask is running'}, 200


if __name__ == '__main__':
    app.run(port=5000)
