from kafka import KafkaConsumer
import json
import subprocess
import sys

consumer = KafkaConsumer(
    'jira-events',
    bootstrap_servers='localhost:9092',
    value_deserializer=lambda v: json.loads(v.decode('utf-8')),
    auto_offset_reset='earliest',
    group_id='jira-agent-group'
)

print("Listening for Jira events...")

for message in consumer:
    event = message.value
    issue_key = event['issue']['key']
    print(f"New Jira issue detected: {issue_key}")
    
    # Trigger your AI agent script
    subprocess.run([
        sys.executable,
        r"C:\Users\amath\OneDrive\Documents\python\JiraConfluenceAIAgent.py",
        "--issue", issue_key
    ])
