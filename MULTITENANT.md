# ArdouraAI Multi-Tenant Architecture

## Overview

```
Internet
   |
   v
Nginx :80/:443
   |
   |-- /webhook/{tenant_id}/*  --> Flask (port 5000)  Multi-tenant producer
   |-- /*                      --> Website (port 8080) Marketing + signup
   |
Kafka  (per-tenant topics: t-{tenant12}-jira-events etc.)
   |
   v
Multi-tenant consumer
   |
   v
tenant_agent.py  (decrypts secrets, runs AI agent per tenant)
   |
   +-- MCP: Jira    (tenant's Atlassian)
   +-- MCP: Confluence
   +-- MCP: GitHub  (tenant's app repo)
   +-- Claude AI
   |
   v
ACR created, code committed, Jira updated
```

---

## Secret Management

### Credential chain

```
/etc/ardoura/credentials.json      <- master key (on Vultr host, never in git)
        |
        | SHA256(master_key + tenant_id) -> per-tenant Fernet key
        |
        v
/etc/ardoura/tenants/{tenant_id}/secrets.enc   <- encrypted secrets
        |
        | decrypt at runtime
        |
        v
dict of env vars injected into agent subprocess
```

### credentials.json format
```json
{
  "master_key": "base64-encoded-32-byte-key",
  "version": 1
}
```
Generated once via:
```bash
python -c "from tenant.secrets_manager import init_master_key; init_master_key()"
```

---

## Onboarding a New Tenant

### 1. Client signs up on the website
They fill in the form at `https://ardoura.yourdomain.com` selecting their tools and plan.
You receive an email notification with their details.

### 2. Client provides their secrets
Send them `tenant/secrets.env.template`. They fill it in and send it back securely
(via encrypted email, 1Password share, or your onboarding portal).

### 3. Run the onboarding script
```bash
python tenant/onboard_tenant.py \
  --name "Acme Corp" \
  --email admin@acme.com \
  --plan starter \
  --tools jira confluence github \
  --secrets-file /tmp/acme_secrets.env

# Delete the plain-text file immediately after!
rm /tmp/acme_secrets.env
```

Output:
```
Tenant ID : abc123-...
Jira webhook URL:
  https://ardoura.yourdomain.com/webhook/abc123-.../jira
```

### 4. Client registers the webhook
They add the webhook URL to Jira → Settings → System → WebHooks.

---

## Adding a New Tenant (Vultr setup)

```bash
# Initialize master key (first time only)
python -c "from tenant.secrets_manager import init_master_key; init_master_key()"

# Initialize database schema (first time only)
python -c "from tenant.tenant_registry import init_schema; init_schema()"

# Onboard a tenant
python tenant/onboard_tenant.py --name ... --email ... --secrets-file ...
```

---

## Webhook URLs per Tenant

| Tool | URL |
|------|-----|
| Jira | `https://host/webhook/{tenant_id}/jira` |
| Dynatrace | `https://host/webhook/{tenant_id}/dynatrace` |
| Datadog | `https://host/webhook/{tenant_id}/datadog` |
| Grafana | `https://host/webhook/{tenant_id}/grafana` |

---

## Website

The marketing website runs on port 8080 and is proxied by Nginx.

- Landing page with integration showcase
- Pricing plans (Free / Starter $49 / Pro $199)
- Signup form that captures name, email, company, plan, tools
- Signup records saved to `/etc/ardoura/signups.jsonl`
- Optional email notification to ops team on new signup

---

## Environment Variables for Website

Add to `/etc/ardoura/secrets.env`:
```
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=your@gmail.com
SMTP_PASSWORD=your_app_password
NOTIFY_EMAIL=ops@yourcompany.com
WEBSITE_PORT=8080
```
