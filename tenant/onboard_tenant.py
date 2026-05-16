#!/usr/bin/env python3
"""
tenant/onboard_tenant.py
========================
CLI tool to onboard a new tenant manually.
Used by the ArdouraAI ops team after a client signs up.

Usage:
    python tenant/onboard_tenant.py \
        --name "Acme Corp" \
        --email admin@acme.com \
        --plan starter \
        --tools jira confluence github \
        --secrets-file /path/to/acme_secrets.env
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from tenant.tenant_registry import create_tenant, update_tenant_secrets_path
from tenant.secrets_manager import encrypt_secrets

AVAILABLE_TOOLS = [
    "jira", "confluence", "github",
    "dynatrace", "datadog", "grafana", "prometheus",
]


def parse_secrets_file(path: str) -> dict:
    """Parse a plain-text .env file into a dict."""
    secrets = {}
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, _, v = line.partition("=")
            secrets[k.strip()] = v.strip()
    return secrets


def main():
    parser = argparse.ArgumentParser(description="Onboard a new ArdouraAI tenant")
    parser.add_argument("--name",         required=True)
    parser.add_argument("--email",        required=True)
    parser.add_argument("--plan",         default="starter",
                        choices=["free", "starter", "pro"])
    parser.add_argument("--tools",        nargs="+", default=["jira"],
                        choices=AVAILABLE_TOOLS)
    parser.add_argument("--secrets-file", required=True,
                        help="Path to plain-text secrets.env for this tenant")
    args = parser.parse_args()

    print(f"Onboarding tenant: {args.name} ({args.email})")

    # 1. Create tenant record
    tenant = create_tenant(
        name  = args.name,
        email = args.email,
        plan  = args.plan,
        tools = args.tools,
    )
    tenant_id = tenant["tenant_id"]
    print(f"  Tenant ID:      {tenant_id}")
    print(f"  Webhook secret: {tenant['webhook_secret']}")

    # 2. Parse and encrypt secrets
    secrets = parse_secrets_file(args.secrets_file)
    enc_path = encrypt_secrets(tenant_id, secrets)
    print(f"  Secrets encrypted at: {enc_path}")

    # 3. Update registry with secrets path
    update_tenant_secrets_path(tenant_id, str(enc_path))
    print(f"  Status: active")

    print()
    print("=" * 60)
    print("Tenant onboarded successfully!")
    print("=" * 60)
    print(f"  Tenant ID : {tenant_id}")
    print(f"  Jira webhook URL:")
    print(f"    https://<ARDOURA_HOST>/webhook/{tenant_id}/jira")
    print(f"  Dynatrace webhook URL:")
    print(f"    https://<ARDOURA_HOST>/webhook/{tenant_id}/dynatrace")
    print(f"  Register the Jira webhook secret: {tenant['webhook_secret']}")
    print()


if __name__ == "__main__":
    main()
