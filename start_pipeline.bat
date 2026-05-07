@echo off
REM ardouraAI Pipeline Startup -- 5 processes
REM 1. ngrok  2. Flask producer  3. Jira consumer  4. Obs consumer  5. Topic setup
SET PYTHON_DIR=C:\Users\amath\OneDrive\Documents\python
SET NGROK_PORT=5000
SET NGROK_DOMAIN=
echo Starting ardouraAI pipeline...
start "ngrok" cmd /k "ngrok http %NGROK_PORT% %NGROK_DOMAIN%"
timeout /t 3 /nobreak > nul
start "Kafka Producer" cmd /k "cd /d %PYTHON_DIR% && python kafka_jira_producer.py"
timeout /t 2 /nobreak > nul
start "Kafka Consumer Jira" cmd /k "cd /d %PYTHON_DIR% && python kafka_jira_consumer.py"
timeout /t 1 /nobreak > nul
start "Kafka Consumer Obs" cmd /k "cd /d %PYTHON_DIR% && python kafka_observability_consumer.py"
timeout /t 1 /nobreak > nul
start "Kafka Topics" cmd /k "docker exec -it kafka kafka-topics --create --if-not-exists --topic jira-events --bootstrap-server localhost:9092 --partitions 1 --replication-factor 1 && docker exec -it kafka kafka-topics --create --if-not-exists --topic dynatrace-alerts --bootstrap-server localhost:9092 --partitions 1 --replication-factor 1 && docker exec -it kafka kafka-topics --create --if-not-exists --topic datadog-alerts --bootstrap-server localhost:9092 --partitions 1 --replication-factor 1 && docker exec -it kafka kafka-topics --create --if-not-exists --topic grafana-alerts --bootstrap-server localhost:9092 --partitions 1 --replication-factor 1 && docker exec -it kafka kafka-topics --list --bootstrap-server localhost:9092 && timeout /t 10"
echo All 5 processes started!
pause
