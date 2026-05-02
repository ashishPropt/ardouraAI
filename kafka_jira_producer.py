from flask import Flask, request
from kafka import KafkaProducer
import json

app = Flask(__name__)

producer = KafkaProducer(
    bootstrap_servers='localhost:9092',
    value_serializer=lambda v: json.dumps(v).encode('utf-8')
)

@app.route('/jira-webhook', methods=['POST'])
def jira_webhook():
    data = request.json
    producer.send('jira-events', data)
    producer.flush()
    print(f"Event sent to Kafka: {data['issue']['key']}")
    return {'status': 'ok'}, 200

@app.route('/health', methods=['GET'])
def health():
    return {'status': 'Flask is running'}, 200

if __name__ == '__main__':
    app.run(port=5000)
