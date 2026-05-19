#!/usr/bin/env python3
"""
tenant/tenant_registry.py
=========================
Tenant registry - manages tenant records in PostgreSQL.
Each tenant has:
  - tenant_id       : unique UUID
  - name            : company name
  - email           : admin email
  - plan            : free | starter | pro
  - tools           : JSONB list of enabled integrations
  - secrets_path    : path to encrypted secrets.env on disk
  - webhook_secret  : HMAC secret for webhook validation
  - status          : pending | active | suspended
  - created_at      : timestamp
"""
from __future__ import annotations
import json
import os
import uuid
from typing import Optional

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()


def _conn():
    return psycopg2.connect(
        host     = os.environ["DB_HOST"],
        port     = int(os.environ.get("DB_PORT", 5432)),
        user     = os.environ["DB_USER"],
        password = os.environ["DB_PASSWORD"],
        dbname   = os.environ.get("DB_NAME", "ardoura"),
        sslmode  = os.environ.get("DB_SSLMODE", "require"),
    )


def init_schema() -> None:
    """Create tables if they don't exist."""
    sql = """
    CREATE TABLE IF NOT EXISTS tenants (
        tenant_id      VARCHAR(36)  PRIMARY KEY,
        name           VARCHAR(255) NOT NULL,
        email          VARCHAR(255) NOT NULL UNIQUE,
        plan           VARCHAR(50)  NOT NULL DEFAULT 'free',
        tools          JSONB        NOT NULL DEFAULT '[]',
        secrets_path   VARCHAR(500) NOT NULL DEFAULT '',
        webhook_secret VARCHAR(64)  NOT NULL DEFAULT '',
        status         VARCHAR(50)  NOT NULL DEFAULT 'pending',
        created_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
        updated_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS tenant_events (
        id          BIGSERIAL    PRIMARY KEY,
        tenant_id   VARCHAR(36)  NOT NULL,
        event_type  VARCHAR(100) NOT NULL,
        payload     JSONB,
        status      VARCHAR(50)  NOT NULL DEFAULT 'received',
        created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
    );

    CREATE INDEX IF NOT EXISTS idx_tenant_events_tenant
        ON tenant_events (tenant_id);
    CREATE INDEX IF NOT EXISTS idx_tenant_events_status
        ON tenant_events (status);

    -- Auto-update updated_at on tenants
    CREATE OR REPLACE FUNCTION update_updated_at()
    RETURNS TRIGGER AS $$
    BEGIN
        NEW.updated_at = NOW();
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;

    DROP TRIGGER IF EXISTS tenants_updated_at ON tenants;
    CREATE TRIGGER tenants_updated_at
        BEFORE UPDATE ON tenants
        FOR EACH ROW EXECUTE FUNCTION update_updated_at();
    """
    db  = _conn()
    cur = db.cursor()
    cur.execute(sql)
    db.commit()
    cur.close()
    db.close()
    print("[Registry] PostgreSQL schema initialised")


def _row_to_dict(row: dict) -> dict:
    """Normalise a psycopg2 row - ensure tools is a list."""
    if row and isinstance(row.get("tools"), str):
        row["tools"] = json.loads(row["tools"])
    return row


def create_tenant(name: str, email: str, plan: str,
                  tools: list[str]) -> dict:
    tenant_id      = str(uuid.uuid4())
    webhook_secret = uuid.uuid4().hex
    db  = _conn()
    cur = db.cursor()
    cur.execute(
        """
        INSERT INTO tenants
            (tenant_id, name, email, plan, tools, webhook_secret, status)
        VALUES (%s, %s, %s, %s, %s, %s, 'pending')
        """,
        (tenant_id, name, email, plan,
         json.dumps(tools), webhook_secret)
    )
    db.commit()
    cur.close()
    db.close()
    return get_tenant(tenant_id)


def get_tenant(tenant_id: str) -> Optional[dict]:
    db  = _conn()
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        "SELECT * FROM tenants WHERE tenant_id = %s", (tenant_id,)
    )
    row = cur.fetchone()
    cur.close()
    db.close()
    return _row_to_dict(dict(row)) if row else None


def get_tenant_by_email(email: str) -> Optional[dict]:
    db  = _conn()
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        "SELECT * FROM tenants WHERE email = %s", (email,)
    )
    row = cur.fetchone()
    cur.close()
    db.close()
    return _row_to_dict(dict(row)) if row else None


def update_tenant_secrets_path(tenant_id: str, path: str) -> None:
    db  = _conn()
    cur = db.cursor()
    cur.execute(
        """
        UPDATE tenants
        SET secrets_path = %s, status = 'active'
        WHERE tenant_id = %s
        """,
        (path, tenant_id)
    )
    db.commit()
    cur.close()
    db.close()


def list_active_tenants() -> list[dict]:
    db  = _conn()
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM tenants WHERE status = 'active'")
    rows = cur.fetchall()
    cur.close()
    db.close()
    return [_row_to_dict(dict(r)) for r in rows]


def log_event(tenant_id: str, event_type: str, payload: dict) -> int:
    db  = _conn()
    cur = db.cursor()
    cur.execute(
        """
        INSERT INTO tenant_events (tenant_id, event_type, payload)
        VALUES (%s, %s, %s) RETURNING id
        """,
        (tenant_id, event_type, json.dumps(payload))
    )
    event_id = cur.fetchone()[0]
    db.commit()
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
