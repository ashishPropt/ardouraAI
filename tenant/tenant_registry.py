#!/usr/bin/env python3
"""
tenant/tenant_registry.py
=========================
Tenant registry - manages tenant records in MySQL.
Each tenant has:
  - tenant_id       : unique UUID
  - name            : company name
  - email           : admin email
  - plan            : free | starter | pro
  - tools           : JSON list of enabled integrations
  - secrets_path    : path to encrypted secrets.env on disk
  - webhook_secret  : HMAC secret for webhook validation
  - status          : pending | active | suspended
  - created_at      : timestamp
"""
from __future__ import annotations
import json
import os
import uuid
from datetime import datetime
from typing import Optional

import mysql.connector
from dotenv import load_dotenv

load_dotenv()


def _conn():
    return mysql.connector.connect(
        host     = os.environ["DB_HOST"],
        port     = int(os.environ.get("DB_PORT", 3306)),
        user     = os.environ["DB_USER"],
        password = os.environ["DB_PASSWORD"],
        database = os.environ.get("DB_NAME", "ardoura"),
    )


def init_schema() -> None:
    """Create tables if they don't exist."""
    sql = """
    CREATE TABLE IF NOT EXISTS tenants (
        tenant_id      VARCHAR(36)  PRIMARY KEY,
        name           VARCHAR(255) NOT NULL,
        email          VARCHAR(255) NOT NULL UNIQUE,
        plan           VARCHAR(50)  NOT NULL DEFAULT 'free',
        tools          JSON         NOT NULL DEFAULT ('[]'),
        secrets_path   VARCHAR(500) NOT NULL DEFAULT '',
        webhook_secret VARCHAR(64)  NOT NULL DEFAULT '',
        status         VARCHAR(50)  NOT NULL DEFAULT 'pending',
        created_at     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP
                                    ON UPDATE CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS tenant_events (
        id          BIGINT AUTO_INCREMENT PRIMARY KEY,
        tenant_id   VARCHAR(36)  NOT NULL,
        event_type  VARCHAR(100) NOT NULL,
        payload     JSON,
        status      VARCHAR(50)  NOT NULL DEFAULT 'received',
        created_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_tenant (tenant_id),
        INDEX idx_status (status)
    );
    """
    db = _conn()
    cur = db.cursor()
    for stmt in sql.strip().split(";"):
        stmt = stmt.strip()
        if stmt:
            cur.execute(stmt)
    db.commit()
    cur.close()
    db.close()
    print("[Registry] Schema initialised")


def create_tenant(name: str, email: str, plan: str,
                  tools: list[str]) -> dict:
    tenant_id      = str(uuid.uuid4())
    webhook_secret = uuid.uuid4().hex
    db  = _conn()
    cur = db.cursor()
    cur.execute(
        """INSERT INTO tenants
           (tenant_id, name, email, plan, tools, webhook_secret, status)
           VALUES (%s, %s, %s, %s, %s, %s, 'pending')""",
        (tenant_id, name, email, plan, json.dumps(tools), webhook_secret)
    )
    db.commit()
    cur.close()
    db.close()
    return get_tenant(tenant_id)


def get_tenant(tenant_id: str) -> Optional[dict]:
    db  = _conn()
    cur = db.cursor(dictionary=True)
    cur.execute("SELECT * FROM tenants WHERE tenant_id = %s", (tenant_id,))
    row = cur.fetchone()
    cur.close()
    db.close()
    if row and isinstance(row.get("tools"), str):
        row["tools"] = json.loads(row["tools"])
    return row


def get_tenant_by_email(email: str) -> Optional[dict]:
    db  = _conn()
    cur = db.cursor(dictionary=True)
    cur.execute("SELECT * FROM tenants WHERE email = %s", (email,))
    row = cur.fetchone()
    cur.close()
    db.close()
    if row and isinstance(row.get("tools"), str):
        row["tools"] = json.loads(row["tools"])
    return row


def update_tenant_secrets_path(tenant_id: str, path: str) -> None:
    db  = _conn()
    cur = db.cursor()
    cur.execute(
        "UPDATE tenants SET secrets_path = %s, status = 'active' WHERE tenant_id = %s",
        (path, tenant_id)
    )
    db.commit()
    cur.close()
    db.close()


def list_active_tenants() -> list[dict]:
    db  = _conn()
    cur = db.cursor(dictionary=True)
    cur.execute("SELECT * FROM tenants WHERE status = 'active'")
    rows = cur.fetchall()
    cur.close()
    db.close()
    for row in rows:
        if isinstance(row.get("tools"), str):
            row["tools"] = json.loads(row["tools"])
    return rows


def log_event(tenant_id: str, event_type: str, payload: dict) -> int:
    db  = _conn()
    cur = db.cursor()
    cur.execute(
        "INSERT INTO tenant_events (tenant_id, event_type, payload) VALUES (%s, %s, %s)",
        (tenant_id, event_type, json.dumps(payload))
    )
    db.commit()
    event_id = cur.lastrowid
    cur.close()
    db.close()
    return event_id


def update_event_status(event_id: int, status: str) -> None:
    db  = _conn()
    cur = db.cursor()
    cur.execute(
        "UPDATE tenant_events SET status = %s WHERE id = %s",
        (status, event_id)
    )
    db.commit()
    cur.close()
    db.close()
