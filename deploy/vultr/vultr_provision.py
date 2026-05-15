#!/usr/bin/env python3
"""
vultr_provision.py
==================
Standalone Python script that uses the Vultr REST API v2 to provision
all ArdouraAI infrastructure components.

Prerequisites:
    pip install requests python-dotenv

Usage:
    # Windows PowerShell
    $env:VULTR_API_KEY = "your_key_here"

    python deploy/vultr/vultr_provision.py --action list-db-plans   # see available DB plans
    python deploy/vultr/vultr_provision.py --action create
    python deploy/vultr/vultr_provision.py --action status
    python deploy/vultr/vultr_provision.py --action destroy          # CAREFUL!
"""
from __future__ import annotations
import argparse
import base64
import json
import os
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

API_KEY  = os.environ.get("VULTR_API_KEY", "")
BASE_URL = "https://api.vultr.com/v2"
HEADERS  = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type":  "application/json",
}

# ── Infrastructure configuration ────────────────────────────────────────────
CONFIG = {
    "region":          "ewr",          # Newark – closest to Princeton NJ
    "plan":            "vc2-2c-4gb",   # 2 vCPU / 4 GB RAM
    "os_id":           1743,           # Ubuntu 22.04 LTS x64
    "app_label":       "ardoura-ai",
    "app_hostname":    "ardoura-ai",
    "vpc_description": "ardouraAI private network",
    "vpc_subnet":      "10.10.0.0",
    "vpc_subnet_mask": 24,
    "fw_description":  "ardouraAI firewall",
    "db_label":        "ardoura-mysql",
    "db_engine":       "mysql",
    "db_version":      "8",
    # db_plan is resolved at runtime via resolve_db_plan() – do not hardcode
    "db_plan":         "",
    "state_file":      Path(__file__).parent / ".vultr_state.json",
}


# ── Helpers ──────────────────────────────────────────────────────────────────
def v(path: str, method: str = "GET", body: dict | None = None) -> dict:
    """Thin wrapper around the Vultr REST API with descriptive error output."""
    if not API_KEY:
        sys.exit("ERROR: VULTR_API_KEY is not set.\n"
                 "  PowerShell: $env:VULTR_API_KEY = 'your_key'\n"
                 "  CMD:        set VULTR_API_KEY=your_key")
    url  = f"{BASE_URL}/{path.lstrip('/')}"
    resp = requests.request(method, url, headers=HEADERS, json=body, timeout=30)
    if not resp.ok:
        print(f"  API error {resp.status_code}: {resp.text[:600]}")
        resp.raise_for_status()
    return resp.json() if resp.text else {}


def save_state(state: dict) -> None:
    CONFIG["state_file"].write_text(json.dumps(state, indent=2))
    print(f"  State saved → {CONFIG['state_file']}")


def load_state() -> dict:
    sf = CONFIG["state_file"]
    return json.loads(sf.read_text()) if sf.exists() else {}


def wait_for(resource_type: str, resource_id: str, field: str, target: str,
             timeout: int = 600, poll: int = 15) -> None:
    """Poll until a Vultr resource reaches a target status."""
    deadline = time.time() + timeout
    print(f"  Waiting for {resource_type}/{resource_id} {field}={target} ...",
          end="", flush=True)
    while time.time() < deadline:
        data    = v(f"{resource_type}/{resource_id}")
        obj     = next(iter(data.values())) if data else {}
        current = obj.get(field, "")
        if current == target:
            print(" done")
            return
        print(".", end="", flush=True)
        time.sleep(poll)
    raise TimeoutError(
        f"{resource_type}/{resource_id} did not reach {field}={target} within {timeout}s"
    )


# ── DB plan auto-discovery ───────────────────────────────────────────────────
def fetch_db_plans(region: str, engine: str = "mysql") -> list[dict]:
    """Return all managed-DB plans available for a given region and engine."""
    data  = v(f"databases/plans?engine={engine}")
    plans = data.get("plans", [])
    # Filter to plans that include our target region
    return [p for p in plans if region in p.get("supported_engines", {}).get(engine, {}).get("regions", [])]


def resolve_db_plan(region: str, engine: str = "mysql") -> str:
    """
    Pick the cheapest available managed-DB plan for region+engine.
    Raises a clear error listing valid plans if none found.
    """
    plans = fetch_db_plans(region, engine)
    if not plans:
        # Fallback: fetch all plans without region filter and show them
        all_plans = v(f"databases/plans?engine={engine}").get("plans", [])
        ids = [p["id"] for p in all_plans]
        sys.exit(
            f"ERROR: No managed MySQL plans found for region '{region}'.\n"
            f"All available plan IDs: {ids}\n"
            f"Re-run with --action list-db-plans to see full details."
        )

    # Sort by monthly cost ascending, pick the cheapest
    plans_sorted = sorted(plans, key=lambda p: p.get("monthly_cost", 9999))
    chosen = plans_sorted[0]["id"]
    print(f"  Auto-selected DB plan: {chosen}  "
          f"(${plans_sorted[0].get('monthly_cost', '?')}/mo, "
          f"{plans_sorted[0].get('vcpu_count', '?')} vCPU, "
          f"{plans_sorted[0].get('ram', '?')} MB RAM)")
    return chosen


# ── Startup script ───────────────────────────────────────────────────────────
def build_startup_script_payload() -> str:
    script_path = Path(__file__).parent / "cloud-init" / "startup.sh"
    if not script_path.exists():
        sys.exit(f"ERROR: startup.sh not found at {script_path}")
    return base64.b64encode(script_path.read_bytes()).decode()


# ── Create actions ────────────────────────────────────────────────────────────
def create_vpc(state: dict) -> dict:
    if state.get("vpc_id"):
        print(f"  VPC already exists: {state['vpc_id']}")
        return state
    print("Creating VPC ...")
    r = v("vpcs", "POST", {
        "region":        CONFIG["region"],
        "description":   CONFIG["vpc_description"],
        "v4_subnet":     CONFIG["vpc_subnet"],
        "v4_subnet_mask": CONFIG["vpc_subnet_mask"],
    })
    state["vpc_id"] = r["vpc"]["id"]
    print(f"  VPC created: {state['vpc_id']}")
    save_state(state)
    return state


def create_firewall(state: dict) -> dict:
    if state.get("fw_id"):
        print(f"  Firewall already exists: {state['fw_id']}")
        return state
    print("Creating firewall group ...")
    r     = v("firewalls", "POST", {"description": CONFIG["fw_description"]})
    fw_id = r["firewall_group"]["id"]
    state["fw_id"] = fw_id
    save_state(state)

    rules = [
        {"ip_type": "v4", "protocol": "tcp",  "subnet": "0.0.0.0", "subnet_size": 0,
         "port": "22",   "notes": "SSH"},
        {"ip_type": "v4", "protocol": "tcp",  "subnet": "0.0.0.0", "subnet_size": 0,
         "port": "5000", "notes": "Flask webhook"},
        {"ip_type": "v4", "protocol": "tcp",  "subnet": "0.0.0.0", "subnet_size": 0,
         "port": "80",   "notes": "HTTP"},
        {"ip_type": "v4", "protocol": "tcp",  "subnet": "0.0.0.0", "subnet_size": 0,
         "port": "443",  "notes": "HTTPS"},
        {"ip_type": "v4", "protocol": "icmp", "subnet": "0.0.0.0", "subnet_size": 0,
         "notes": "ICMP ping"},
    ]
    for rule in rules:
        v(f"firewalls/{fw_id}/rules", "POST", rule)
    print(f"  Firewall created with {len(rules)} rules: {fw_id}")
    return state


def create_database(state: dict) -> dict:
    if state.get("db_id"):
        print(f"  Database already exists: {state['db_id']}")
        return state

    # Auto-discover the correct plan slug for this account + region
    db_plan = resolve_db_plan(CONFIG["region"], CONFIG["db_engine"])

    print("Creating managed MySQL cluster (this takes 5-10 min) ...")
    r  = v("databases", "POST", {
        "database_engine":         CONFIG["db_engine"],
        "database_engine_version": CONFIG["db_version"],
        "region":                  CONFIG["region"],
        "plan":                    db_plan,
        "label":                   CONFIG["db_label"],
        "cluster_time_zone":       "America/New_York",
    })
    db = r["database"]
    state.update({
        "db_id":       db["id"],
        "db_host":     db.get("host",     ""),
        "db_port":     db.get("port",     3306),
        "db_user":     db.get("user",     ""),
        "db_password": db.get("password", ""),
    })
    save_state(state)
    print(f"  DB cluster provisioning: {state['db_id']}")
    wait_for("databases", state["db_id"], "status", "Running", timeout=900)

    # Refresh credentials after Running state
    db_fresh = v(f"databases/{state['db_id']}")["database"]
    state.update({
        "db_host":     db_fresh.get("host",     state["db_host"]),
        "db_port":     db_fresh.get("port",     state["db_port"]),
        "db_user":     db_fresh.get("user",     state["db_user"]),
        "db_password": db_fresh.get("password", state["db_password"]),
    })
    save_state(state)
    print(f"  MySQL ready: {state['db_host']}:{state['db_port']}")
    return state


def create_startup_script(state: dict) -> dict:
    if state.get("script_id"):
        print(f"  Startup script already exists: {state['script_id']}")
        return state
    print("Uploading startup script ...")
    r = v("startup-scripts", "POST", {
        "name":   "ardoura-cloud-init",
        "type":   "boot",
        "script": build_startup_script_payload(),
    })
    state["script_id"] = r["startup_script"]["id"]
    save_state(state)
    print(f"  Script uploaded: {state['script_id']}")
    return state


def create_instance(state: dict) -> dict:
    if state.get("instance_id"):
        print(f"  Instance already exists: {state['instance_id']}")
        return state
    print("Creating compute instance ...")
    user_data = json.dumps({
        "db_host":     state.get("db_host",     ""),
        "db_port":     str(state.get("db_port", 3306)),
        "db_user":     state.get("db_user",     ""),
        "db_password": state.get("db_password", ""),
        "db_name":     "ardoura",
    })
    payload = {
        "region":           CONFIG["region"],
        "plan":             CONFIG["plan"],
        "os_id":            CONFIG["os_id"],
        "label":            CONFIG["app_label"],
        "hostname":         CONFIG["app_hostname"],
        "firewall_group_id": state["fw_id"],
        "vpc_ids":          [state["vpc_id"]],
        "script_id":        state["script_id"],
        "user_data":        base64.b64encode(user_data.encode()).decode(),
        "backups":          "disabled",
        "ddos_protection":  False,
        "activation_email": False,
    }
    ssh_key = os.environ.get("VULTR_SSH_KEY_ID", "")
    if ssh_key:
        payload["sshkey_id"] = [ssh_key]

    r    = v("instances", "POST", payload)
    inst = r["instance"]
    state.update({
        "instance_id": inst["id"],
        "instance_ip": inst.get("main_ip", ""),
    })
    save_state(state)
    print(f"  Instance provisioning: {state['instance_id']}")
    wait_for("instances", state["instance_id"], "status", "active")

    # Refresh IP after active
    inst_fresh       = v(f"instances/{state['instance_id']}")["instance"]
    state["instance_ip"] = inst_fresh.get("main_ip", state["instance_ip"])
    save_state(state)
    print(f"  Instance active: {state['instance_ip']}")
    return state


# ── Top-level actions ─────────────────────────────────────────────────────────
def action_list_db_plans() -> None:
    """Print all managed MySQL plans available in the configured region."""
    region = CONFIG["region"]
    print(f"\nFetching MySQL plans available in region '{region}' ...\n")
    plans = fetch_db_plans(region, "mysql")
    if not plans:
        print(f"  No MySQL plans found for region '{region}'.")
        print("  Try a different region, or check https://www.vultr.com/databases/")
        return
    print(f"  {'ID':<40} {'vCPU':>5} {'RAM MB':>8} {'Disk GB':>8} {'$/mo':>8}")
    print("  " + "-" * 75)
    for p in sorted(plans, key=lambda x: x.get("monthly_cost", 9999)):
        print(f"  {p['id']:<40} {p.get('vcpu_count',0):>5} "
              f"{p.get('ram',0):>8} {p.get('disk',0):>8} "
              f"{p.get('monthly_cost',0):>8.2f}")
    print()


def action_create() -> None:
    state = load_state()
    state = create_vpc(state)
    state = create_firewall(state)
    state = create_database(state)
    state = create_startup_script(state)
    state = create_instance(state)

    ip = state.get("instance_ip", "IP")
    print("\n" + "=" * 60)
    print("ArdouraAI deployment complete!")
    print("=" * 60)
    print(f"  App IP:       {ip}")
    print(f"  Webhook URL:  http://{ip}:5000/jira-webhook")
    print(f"  Health:       http://{ip}:5000/health")
    print(f"  MySQL host:   {state.get('db_host')}:{state.get('db_port')}")
    print()
    print("Next steps:")
    print(f"  1. SSH:    ssh root@{ip}")
    print("  2. Fill:   nano /etc/ardoura/secrets.env")
    print("  3. Reload: systemctl restart ardoura")
    print(f"  4. Verify: curl http://{ip}:5000/health")


def action_status() -> None:
    state = load_state()
    if not state:
        print("No state file found. Run --action create first.")
        return
    print("\n=== ArdouraAI Vultr Status ===")
    for key, val in state.items():
        if "password" in key.lower():
            val = "********"
        print(f"  {key:20s}: {val}")
    if state.get("instance_id"):
        inst = v(f"instances/{state['instance_id']}")["instance"]
        print(f"\n  Instance status : {inst.get('status')}")
        print(f"  Server status   : {inst.get('server_status')}")
        print(f"  Power status    : {inst.get('power_status')}")


def action_destroy() -> None:
    state = load_state()
    if not state:
        print("No state file found – nothing to destroy.")
        return
    confirm = input("Type 'destroy' to confirm deletion of ALL ArdouraAI Vultr resources: ")
    if confirm.strip() != "destroy":
        print("Aborted.")
        return

    if state.get("instance_id"):
        print(f"Deleting instance {state['instance_id']} ...")
        v(f"instances/{state['instance_id']}", "DELETE")
        time.sleep(5)
    if state.get("db_id"):
        print(f"Deleting database cluster {state['db_id']} ...")
        v(f"databases/{state['db_id']}", "DELETE")
    if state.get("script_id"):
        print(f"Deleting startup script {state['script_id']} ...")
        v(f"startup-scripts/{state['script_id']}", "DELETE")
    if state.get("fw_id"):
        print(f"Deleting firewall {state['fw_id']} ...")
        v(f"firewalls/{state['fw_id']}", "DELETE")
    if state.get("vpc_id"):
        print(f"Deleting VPC {state['vpc_id']} ...")
        v(f"vpcs/{state['vpc_id']}", "DELETE")

    CONFIG["state_file"].unlink(missing_ok=True)
    print("All resources deleted.")


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ArdouraAI Vultr provisioner")
    parser.add_argument(
        "--action",
        choices=["create", "status", "destroy", "list-db-plans"],
        default="status",
        help="Action to perform",
    )
    args = parser.parse_args()

    actions = {
        "create":        action_create,
        "status":        action_status,
        "destroy":       action_destroy,
        "list-db-plans": action_list_db_plans,
    }
    actions[args.action]()
