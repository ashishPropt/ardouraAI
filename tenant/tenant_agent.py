#!/usr/bin/env python3
"""
tenant/tenant_agent.py
======================
Tenant-aware wrapper around JiraConfluenceAIAgent_mcp.py.
Loads the correct secrets for a given tenant before running the agent.

Usage:
    python tenant/tenant_agent.py --tenant-id <uuid> --issue ADEV-42
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Allow importing from parent directory
sys.path.insert(0, str(Path(__file__).parent.parent))

from tenant.tenant_registry import get_tenant, log_event, update_event_status
from tenant.secrets_manager import write_tenant_env


def main():
    parser = argparse.ArgumentParser(description="Tenant-aware ArdouraAI agent")
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--issue",     required=True)
    args = parser.parse_args()

    tenant = get_tenant(args.tenant_id)
    if not tenant:
        print(f"[TenantAgent] ERROR: tenant {args.tenant_id} not found",
              flush=True)
        sys.exit(1)

    if tenant["status"] != "active":
        print(f"[TenantAgent] ERROR: tenant {args.tenant_id} is {tenant['status']}",
              flush=True)
        sys.exit(1)

    print(f"[TenantAgent] Loading secrets for tenant: {tenant['name']}",
          flush=True)

    # Decrypt and inject tenant secrets into environment
    try:
        secrets = write_tenant_env(args.tenant_id, tenant.get("secrets_path"))
        os.environ.update(secrets)
    except Exception as e:
        print(f"[TenantAgent] ERROR loading secrets: {e}", flush=True)
        sys.exit(1)

    # Log the event
    event_id = log_event(args.tenant_id, "jira_issue_triggered",
                          {"issue": args.issue})

    # Run the agent
    try:
        import asyncio
        from JiraConfluenceAIAgent_mcp import run_agent
        asyncio.run(run_agent(args.issue))
        update_event_status(event_id, "completed")
        print(f"[TenantAgent] Agent completed for {args.issue}", flush=True)
    except Exception as e:
        update_event_status(event_id, "failed")
        print(f"[TenantAgent] Agent FAILED for {args.issue}: {e}", flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
