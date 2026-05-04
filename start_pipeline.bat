@echo off
REM ============================================================
REM  ardouraAI Pipeline Startup
REM  Opens 3 terminals in order:
REM    1. ngrok         — public tunnel to localhost:5000
REM    2. Kafka Producer — Flask webhook receiver
REM    3. Kafka Consumer — triggers AI agent on new ADEV tickets
REM
REM  IMPORTANT: Update NGROK_DOMAIN below if you have a fixed
REM  ngrok domain (paid plan). Otherwise leave as-is and update
REM  the Jira webhook URL manually after ngrok starts.
REM ============================================================

SET PYTHON_DIR=C:\Users\amath\OneDrive\Documents\python
SET NGROK_PORT=5000

REM ── Optional: set your fixed ngrok domain here (paid plan) ──
REM SET NGROK_DOMAIN=--domain=yourname.ngrok-free.app
SET NGROK_DOMAIN=

echo.
echo ============================================================
echo   ardouraAI Pipeline Startup
echo ============================================================
echo.
echo Starting 3 processes in separate windows...
echo.

REM ── 1. Start ngrok ──────────────────────────────────────────
echo [1/3] Starting ngrok tunnel on port %NGROK_PORT%...
start "ngrok - Public Tunnel" cmd /k "ngrok http %NGROK_PORT% %NGROK_DOMAIN%"

REM Wait 3 seconds for ngrok to establish the tunnel
timeout /t 3 /nobreak > nul

REM ── 2. Start Flask Kafka Producer ───────────────────────────
echo [2/3] Starting Kafka Producer (Flask webhook server)...
start "Kafka Producer - Flask :5000" cmd /k "cd /d %PYTHON_DIR% && python kafka_jira_producer.py"

REM Wait 2 seconds for Flask to start up
timeout /t 2 /nobreak > nul

REM ── 3. Start Kafka Consumer ─────────────────────────────────
echo [3/3] Starting Kafka Consumer (agent trigger)...
start "Kafka Consumer - Agent Trigger" cmd /k "cd /d %PYTHON_DIR% && python kafka_jira_consumer.py"

echo.
echo ============================================================
echo   All 3 processes started!
echo.
echo   NEXT STEP: Copy the ngrok URL from the ngrok window
echo   (looks like: https://xxxx.ngrok-free.app)
echo   and update your Jira webhook to:
echo   https://xxxx.ngrok-free.app/jira-webhook
echo.
echo   Jira: Settings -> System -> Webhooks
echo   JQL filter: project = ADEV
echo   Event:      Issue -> created
echo ============================================================
echo.
pause
