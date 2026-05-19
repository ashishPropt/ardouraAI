#!/usr/bin/env python3
"""
deploy/vultr/migrate_mysql_to_postgres.py
=========================================
Migrates all data from the Vultr managed MySQL cluster
to the new Vultr managed PostgreSQL cluster.

Run this ONCE after provisioning PostgreSQL and before
switching the app to use PostgreSQL.

Usage:
    python deploy/vultr/migrate_mysql_to_postgres.py

Requires both DB_* (postgres) and MYSQL_* env vars to be set.
"""
from __future__ import annotations

import json
import os
import sys
from dotenv import load_dotenv

load_dotenv()


def get_mysql_conn():
    import mysql.connector
    return mysql.connector.connect(
        host     = os.environ["MYSQL_HOST"],
        port     = int(os.environ.get("MYSQL_PORT", 3306)),
        user     = os.environ["MYSQL_USER"],
        password = os.environ["MYSQL_PASSWORD"],
        database = os.environ.get("MYSQL_DB", "ardoura"),
    )


def get_pg_conn():
    import psycopg2
    return psycopg2.connect(
        host     = os.environ["DB_HOST"],
        port     = int(os.environ.get("DB_PORT", 5432)),
        user     = os.environ["DB_USER"],
        password = os.environ["DB_PASSWORD"],
        dbname   = os.environ.get("DB_NAME", "ardoura"),
        sslmode  = os.environ.get("DB_SSLMODE", "require"),
    )


def migrate() -> None:
    print("=" * 60)
    print("ArdouraAI MySQL -> PostgreSQL Migration")
    print("=" * 60)

    # ── Connect to both DBs ───────────────────────────────────────
    print("\nConnecting to MySQL ...")
    try:
        my_db  = get_mysql_conn()
        my_cur = my_db.cursor(dictionary=True)
        print("  MySQL connected")
    except Exception as e:
        print(f"  MySQL connection FAILED: {e}")
        sys.exit(1)

    print("Connecting to PostgreSQL ...")
    try:
        import psycopg2.extras
        pg_db  = get_pg_conn()
        pg_cur = pg_db.cursor()
        print("  PostgreSQL connected")
    except Exception as e:
        print(f"  PostgreSQL connection FAILED: {e}")
        sys.exit(1)

    # ── Init PostgreSQL schema ───────────────────────────────────
    print("\nInitialising PostgreSQL schema ...")
    from tenant.tenant_registry import init_schema
    init_schema()

    # ── Migrate tenants ─────────────────────────────────────────
    print("\nMigrating tenants ...")
    my_cur.execute("SELECT * FROM tenants")
    tenants = my_cur.fetchall()
    print(f"  Found {len(tenants)} tenant(s) in MySQL")

    migrated = 0
    for t in tenants:
        tools = t["tools"]
        if isinstance(tools, str):
            tools = tools  # keep as JSON string for pg
        elif isinstance(tools, (list, dict)):
            tools = json.dumps(tools)

        try:
            pg_cur.execute(
                """
                INSERT INTO tenants
                    (tenant_id, name, email, plan, tools,
                     secrets_path, webhook_secret, status,
                     created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (tenant_id) DO UPDATE SET
                    name           = EXCLUDED.name,
                    email          = EXCLUDED.email,
                    plan           = EXCLUDED.plan,
                    tools          = EXCLUDED.tools,
                    secrets_path   = EXCLUDED.secrets_path,
                    webhook_secret = EXCLUDED.webhook_secret,
                    status         = EXCLUDED.status,
                    updated_at     = EXCLUDED.updated_at
                """,
                (
                    t["tenant_id"], t["name"], t["email"],
                    t["plan"], tools,
                    t.get("secrets_path", ""),
                    t.get("webhook_secret", ""),
                    t["status"],
                    t["created_at"], t["updated_at"],
                )
            )
            migrated += 1
            print(f"  Migrated tenant: {t['name']} ({t['email']})")
        except Exception as e:
            print(f"  ERROR migrating tenant {t['email']}: {e}")

    pg_db.commit()
    print(f"  Tenants migrated: {migrated}/{len(tenants)}")

    # ── Migrate tenant_events ──────────────────────────────────
    print("\nMigrating tenant_events ...")
    my_cur.execute("SELECT * FROM tenant_events ORDER BY id")
    events = my_cur.fetchall()
    print(f"  Found {len(events)} event(s) in MySQL")

    migrated_events = 0
    for e in events:
        payload = e.get("payload")
        if isinstance(payload, str):
            pass  # already JSON string
        elif payload is not None:
            payload = json.dumps(payload)

        try:
            pg_cur.execute(
                """
                INSERT INTO tenant_events
                    (tenant_id, event_type, payload, status, created_at)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                (
                    e["tenant_id"], e["event_type"],
                    payload, e["status"], e["created_at"],
                )
            )
            migrated_events += 1
        except Exception as ex:
            print(f"  ERROR migrating event {e['id']}: {ex}")

    pg_db.commit()
    print(f"  Events migrated: {migrated_events}/{len(events)}")

    # ── Verify ─────────────────────────────────────────────────
    print("\nVerifying migration ...")
    pg_cur.execute("SELECT COUNT(*) FROM tenants")
    pg_tenant_count = pg_cur.fetchone()[0]
    pg_cur.execute("SELECT COUNT(*) FROM tenant_events")
    pg_event_count = pg_cur.fetchone()[0]
    print(f"  PostgreSQL tenants: {pg_tenant_count}")
    print(f"  PostgreSQL events:  {pg_event_count}")

    # ── Cleanup ──────────────────────────────────────────────────
    my_cur.close(); my_db.close()
    pg_cur.close(); pg_db.close()

    print("\n" + "=" * 60)
    print("Migration complete!")
    print("=" * 60)
    print("\nNext steps:")
    print("  1. Update .env: replace DB_* with PostgreSQL credentials")
    print("  2. Remove MYSQL_* vars from .env")
    print("  3. Restart Docker stack")
    print("  4. Delete MySQL cluster from Vultr console")


if __name__ == "__main__":
    migrate()
