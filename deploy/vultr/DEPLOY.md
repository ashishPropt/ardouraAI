# ArdouraAI – Vultr Cloud Deployment Guide

This folder contains everything needed to provision and run ArdouraAI on
Vultr's cloud infrastructure. Two equivalent paths are provided:

| Path | Best for |
|------|---------|
| **Terraform** (`terraform/`) | Repeatable IaC, team setups, GitOps |
| **Python script** (`vultr_provision.py`) | Quick one-off deploy, no Terraform needed |

---

## Architecture Overview

```
  Internet
     │
     ▼
  Nginx :80/:443  (Vultr instance)
     │
     ▼
  Flask :5000  ← /jira-webhook  /dynatrace-webhook  /datadog-webhook  /grafana-webhook
     │
     ▼
  Kafka  (Docker, single-broker)
     ├─▶ jira-consumer          (JiraConfluenceAIAgent)
     └─▶ obs-consumer           (kafka_observability_consumer)

  MCP servers (Docker, stdio)
     ├─▶ mcp-jira
     ├─▶ mcp-confluence
     ├─▶ mcp-github
     └─▶ mcp-observability

  Vultr Managed MySQL  (external DBaaS)
```

---

## Prerequisites

- Vultr account with a **Personal Access Token** (PAT)
  → <https://my.vultr.com/settings/#settingsapi>
- SSH key uploaded to Vultr (optional but recommended)
  → <https://my.vultr.com/ssh/>
- (Terraform path) Terraform ≥ 1.5 installed locally
- (Python path) Python 3.9+ with `pip install requests python-dotenv`

---

## Option A – Python script (no Terraform)

```bash
# 1. Install deps
pip install requests python-dotenv

# 2. Set your API key
export VULTR_API_KEY="your_vultr_pat_here"
export VULTR_SSH_KEY_ID="your_ssh_key_id"   # optional

# 3. Deploy everything
python deploy/vultr/vultr_provision.py --action create

# 4. Check status at any time
python deploy/vultr/vultr_provision.py --action status

# 5. Tear down (CAREFUL – destroys all resources)
python deploy/vultr/vultr_provision.py --action destroy
```

State is saved to `deploy/vultr/.vultr_state.json` – **don't commit this file**.

---

## Option B – Terraform

```bash
cd deploy/vultr/terraform

# 1. Copy and fill in variables
cp terraform.tfvars.example terraform.tfvars
nano terraform.tfvars

# 2. Init providers
terraform init

# 3. Preview
terraform plan

# 4. Apply
terraform apply

# 5. See outputs
terraform output

# 6. Destroy (when needed)
terraform destroy
```

---

## Post-Deploy: Fill in Secrets

The startup script writes a skeleton secrets file on the instance at:
```
/etc/ardoura/secrets.env
```

SSH in and fill it out:

```bash
ssh root@<INSTANCE_IP>
nano /etc/ardoura/secrets.env
```

Required values:

| Variable | Where to get it |
|----------|-----------------|
| `ANTHROPIC_API_KEY` | <https://console.anthropic.com> |
| `GITHUB_TOKEN` | GitHub → Settings → Developer settings → PAT |
| `ATLASSIAN_BASE` | Your Jira/Confluence domain |
| `ATLASSIAN_EMAIL` | Your Atlassian account email |
| `ATLASSIAN_API_TOKEN` | <https://id.atlassian.com/manage-profile/security/api-tokens> |
| `JIRA_SOURCE_PROJECT_KEY` | Project key that triggers the agent (e.g. `ADEV`) |
| `JIRA_ACTION_PROJECT_KEY` | Project key for approval tickets (e.g. `ACR`) |
| `CONFLUENCE_*` | Page IDs from your Confluence URLs |

After editing:

```bash
systemctl restart ardoura
# Verify:
curl http://localhost:5000/health
docker compose -f /opt/ardoura/deploy/vultr/docker-compose.yml ps
```

---

## Registering Webhooks

Once the instance is running, register your Jira webhook:

1. Go to **Jira → Settings → System → WebHooks**
2. Create a webhook pointing to:
   ```
   http://<INSTANCE_IP>:5000/jira-webhook
   ```
   Events: `Issue Created`  
   JQL filter: `project = ADEV`

For observability tools, point their alert webhooks to:
- Dynatrace → `http://<IP>:5000/dynatrace-webhook`
- Datadog   → `http://<IP>:5000/datadog-webhook`
- Grafana   → `http://<IP>:5000/grafana-webhook`

> 💡 For HTTPS, run `certbot --nginx -d your.domain.com` on the instance
> after configuring DNS.

---

## Useful Commands on the Instance

```bash
# View all running containers
docker compose -f /opt/ardoura/deploy/vultr/docker-compose.yml ps

# Tail logs for a service
docker compose -f /opt/ardoura/deploy/vultr/docker-compose.yml logs -f flask-producer
docker compose -f /opt/ardoura/deploy/vultr/docker-compose.yml logs -f jira-consumer

# Restart a single service
docker compose -f /opt/ardoura/deploy/vultr/docker-compose.yml restart jira-consumer

# Pull latest code and restart
cd /opt/ardoura && git pull && systemctl restart ardoura

# View cloud-init log
cat /var/log/ardoura-init.log

# Kafka CLI – list topics
docker exec -it kafka kafka-topics --bootstrap-server localhost:9092 --list

# Kafka CLI – tail a topic
docker exec -it kafka kafka-console-consumer \
  --bootstrap-server localhost:9092 \
  --topic jira-events --from-beginning
```

---

## Cost Estimate (Newark region)

| Resource | Plan | $/month |
|----------|------|---------|
| Compute instance | vc2-2c-4gb | ~$24 |
| Managed MySQL | startup-cc-1-55-1 | ~$15 |
| **Total** | | **~$39/mo** |

You can reduce cost by using a smaller compute plan (`vc2-1c-2gb` at ~$12)
if you scale back to fewer consumers.
