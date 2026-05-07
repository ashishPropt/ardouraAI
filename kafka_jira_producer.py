"""
kafka_jira_producer.py
======================
Flask webhook receiver and Kafka producer.
Endpoints: /jira-webhook, /dynatrace-webhook, /datadog-webhook, /grafana-webhook, /health
Topics:    jira-events, dynatrace-alerts, datadog-alerts, grafana-alerts
"""
from flask import Flask, request, jsonify
from kafka import KafkaProducer
import json, time

app = Flask(__name__)
producer = KafkaProducer(bootstrap_servers='localhost:9092', value_serializer=lambda v: json.dumps(v).encode('utf-8'))
TARGET_PROJECT_KEY = "ADEV"
TARGET_WEBHOOK_EVENT = "jira:issue_created"

@app.route('/jira-webhook', methods=['POST'])
def jira_webhook():
    data = request.json or {}
    webhook_event = data.get('webhookEvent','')
    issue = data.get('issue',{})
    issue_key = issue.get('key','')
    project_key = issue.get('fields',{}).get('project',{}).get('key','')
    if webhook_event != TARGET_WEBHOOK_EVENT: return jsonify({'status':'ignored','reason':'not issue_created'}),200
    if project_key != TARGET_PROJECT_KEY: return jsonify({'status':'ignored','reason':f'project is {project_key}'}),200
    producer.send('jira-events', data); producer.flush()
    print(f"[Producer] Jira -> Kafka 'jira-events': {issue_key}")
    return jsonify({'status':'ok','issue':issue_key}),200

@app.route('/dynatrace-webhook', methods=['POST'])
def dynatrace_webhook():
    data = request.json or {}
    pid = data.get('PID', data.get('pid', data.get('problemId','unknown')))
    state = data.get('State', data.get('state','OPEN'))
    alert = {"source":"dynatrace","alert_id":pid,"title":data.get('ProblemTitle',data.get('title','Dynatrace Alert')),
             "severity":data.get('ProblemSeverity',data.get('severity','UNKNOWN')),"status":state,
             "impact":data.get('ProblemImpact',data.get('impact','')),"tags":data.get('Tags',data.get('tags',[])),
             "affected":data.get('ImpactedEntities',data.get('affectedEntities',[])),
             "problem_url":data.get('ProblemURL',data.get('problemUrl','')),"timestamp":int(time.time()),"raw":data}
    producer.send('dynatrace-alerts', alert); producer.flush()
    print(f"[Producer] Dynatrace -> Kafka 'dynatrace-alerts': {pid} ({state})")
    return jsonify({'status':'ok','alert_id':pid}),200

@app.route('/datadog-webhook', methods=['POST'])
def datadog_webhook():
    data = request.json or {}
    alert_id = str(data.get('id',data.get('monitor_id','unknown')))
    alert_type = data.get('alert_type',data.get('type','error'))
    alert = {"source":"datadog","alert_id":alert_id,"title":data.get('title',data.get('monitor_name','Datadog Alert')),
             "severity":alert_type,"status":data.get('alert_status',data.get('status','triggered')),
             "body":data.get('body',data.get('text',''))[:500],"tags":data.get('tags',[]),
             "url":data.get('url',data.get('monitor_url','')),"host":data.get('hostname',data.get('host','')),
             "timestamp":int(time.time()),"raw":data}
    producer.send('datadog-alerts', alert); producer.flush()
    print(f"[Producer] Datadog -> Kafka 'datadog-alerts': {alert_id} ({alert_type})")
    return jsonify({'status':'ok','alert_id':alert_id}),200

@app.route('/grafana-webhook', methods=['POST'])
def grafana_webhook():
    data = request.json or {}
    state = data.get('state',data.get('status','alerting'))
    title = data.get('title','Grafana Alert')
    message = data.get('message',data.get('body',''))[:500]
    sub_alerts = data.get('alerts',[])
    if sub_alerts:
        for a in sub_alerts:
            labels = a.get('labels',{}); annots = a.get('annotations',{})
            alert = {"source":"grafana","alert_id":a.get('fingerprint',a.get('id','unknown')),
                     "title":labels.get('alertname',title),"severity":labels.get('severity','UNKNOWN'),
                     "status":a.get('status',state),"summary":annots.get('summary',message),
                     "description":annots.get('description','')[:300],"labels":labels,
                     "generator_url":a.get('generatorURL',''),"timestamp":int(time.time()),"raw":a}
            producer.send('grafana-alerts', alert)
        producer.flush()
        print(f"[Producer] Grafana -> Kafka 'grafana-alerts': {len(sub_alerts)} alert(s) ({state})")
    else:
        alert = {"source":"grafana","alert_id":data.get('groupKey','unknown'),"title":title,
                 "severity":"UNKNOWN","status":state,"summary":message,"description":message,
                 "labels":{},"timestamp":int(time.time()),"raw":data}
        producer.send('grafana-alerts', alert); producer.flush()
        print(f"[Producer] Grafana -> Kafka 'grafana-alerts': {title} ({state})")
    return jsonify({'status':'ok'}),200

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status':'running','endpoints':['/jira-webhook','/dynatrace-webhook','/datadog-webhook','/grafana-webhook','/health']}),200

if __name__ == '__main__':
    print("[Producer] Flask starting on port 5000 ...")
    app.run(port=5000)
