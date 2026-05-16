#!/usr/bin/env python3
"""
website/app.py
==============
ArdouraAI marketing website + client signup.
Serves the landing page and handles signup form submissions.

Routes:
  GET  /              - Landing page
  POST /signup        - Handle signup form
  GET  /signup/success - Confirmation page
  GET  /health        - Health check
"""
from __future__ import annotations

import json
import os
import smtplib
import sys
from email.mime.text import MIMEText
from pathlib import Path
from datetime import datetime

from flask import Flask, request, jsonify, render_template, redirect, url_for
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent.parent))

load_dotenv()

app = Flask(__name__, template_folder="templates", static_folder="static")

SMTP_HOST     = os.environ.get("SMTP_HOST", "")
SMTP_PORT     = int(os.environ.get("SMTP_PORT", 587))
SMTP_USER     = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
NOTIFY_EMAIL  = os.environ.get("NOTIFY_EMAIL", SMTP_USER)

SIGNUPS_FILE  = Path("/etc/ardoura/signups.jsonl")

AVAILABLE_TOOLS = [
    {"id": "jira",        "name": "Jira",        "icon": "📋",
     "desc": "Auto-triage issues, create ACRs, link tickets"},
    {"id": "confluence",  "name": "Confluence",  "icon": "📚",
     "desc": "Read docs for context, update runbooks"},
    {"id": "github",      "name": "GitHub",      "icon": "🐙",
     "desc": "Read & commit code changes automatically"},
    {"id": "dynatrace",   "name": "Dynatrace",   "icon": "🔍",
     "desc": "AI-powered alert triage and remediation"},
    {"id": "datadog",     "name": "Datadog",     "icon": "🐶",
     "desc": "Monitor alerts, auto-create incidents"},
    {"id": "grafana",     "name": "Grafana",     "icon": "📊",
     "desc": "Alert routing and dashboard insights"},
    {"id": "prometheus",  "name": "Prometheus",  "icon": "🔥",
     "desc": "Metrics-driven auto-remediation"},
]

PLANS = [
    {
        "id": "free",
        "name": "Free",
        "price": "$0",
        "period": "/month",
        "features": [
            "1 application",
            "Jira integration",
            "100 events/month",
            "Community support",
        ],
        "cta": "Get Started Free",
    },
    {
        "id": "starter",
        "name": "Starter",
        "price": "$49",
        "period": "/month",
        "highlight": True,
        "features": [
            "3 applications",
            "All integrations",
            "5,000 events/month",
            "GitHub auto-commit",
            "Email support",
        ],
        "cta": "Start Free Trial",
    },
    {
        "id": "pro",
        "name": "Pro",
        "price": "$199",
        "period": "/month",
        "features": [
            "Unlimited applications",
            "All integrations",
            "Unlimited events",
            "Priority support",
            "Custom AI rules",
            "SSO / SAML",
        ],
        "cta": "Contact Sales",
    },
]


@app.route("/")
def index():
    return render_template("index.html",
                           tools=AVAILABLE_TOOLS,
                           plans=PLANS)


@app.route("/signup", methods=["POST"])
def signup():
    name    = request.form.get("name", "").strip()
    email   = request.form.get("email", "").strip()
    company = request.form.get("company", "").strip()
    plan    = request.form.get("plan", "free")
    tools   = request.form.getlist("tools")

    if not name or not email:
        return jsonify({"error": "Name and email are required"}), 400

    # Save signup record
    record = {
        "name":       name,
        "email":      email,
        "company":    company,
        "plan":       plan,
        "tools":      tools,
        "created_at": datetime.utcnow().isoformat(),
        "status":     "pending_onboarding",
    }
    SIGNUPS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(SIGNUPS_FILE, "a") as f:
        f.write(json.dumps(record) + "\n")

    # Also write to MySQL if available
    try:
        from tenant.tenant_registry import create_tenant
        create_tenant(name=name, email=email, plan=plan, tools=tools)
    except Exception:
        pass  # MySQL not available in marketing-only mode

    # Send notification email
    _notify_signup(record)

    return redirect("/signup/success")


@app.route("/signup/success")
def signup_success():
    return render_template("success.html")


@app.route("/health")
def health():
    return jsonify({"status": "running", "service": "ardoura-website"})


def _notify_signup(record: dict) -> None:
    if not SMTP_HOST or not SMTP_USER:
        print(f"[Website] New signup: {record['email']} (no SMTP configured)",
              flush=True)
        return
    try:
        body = (
            f"New ArdouraAI signup!\n\n"
            f"Name:    {record['name']}\n"
            f"Email:   {record['email']}\n"
            f"Company: {record['company']}\n"
            f"Plan:    {record['plan']}\n"
            f"Tools:   {', '.join(record['tools'])}\n\n"
            f"Run onboarding:\n"
            f"  python tenant/onboard_tenant.py "
            f"--name '{record['name']}' "
            f"--email {record['email']} "
            f"--plan {record['plan']} "
            f"--tools {' '.join(record['tools'])} "
            f"--secrets-file /path/to/secrets.env"
        )
        msg            = MIMEText(body)
        msg["Subject"] = f"New ArdouraAI Signup: {record['name']}"
        msg["From"]    = SMTP_USER
        msg["To"]      = NOTIFY_EMAIL
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASSWORD)
            s.send_message(msg)
    except Exception as e:
        print(f"[Website] Email notification failed: {e}", flush=True)


if __name__ == "__main__":
    port = int(os.environ.get("WEBSITE_PORT", 8080))
    print(f"[Website] Starting on port {port}", flush=True)
    app.run(host="0.0.0.0", port=port)
