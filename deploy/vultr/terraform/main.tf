# =============================================================================
# ArdouraAI – Vultr Cloud Infrastructure
# Terraform >= 1.5  |  vultr/vultr provider >= 2.19
#
# Resources provisioned:
#   • VPC (private network)
#   • Firewall group  (SSH + Flask webhook + ephemeral)
#   • Managed MySQL database cluster  (Vultr DBaaS)
#   • App compute instance  (runs Docker Compose: Flask, Kafka, ZK, consumers)
#   • Startup script (cloud-init) injected at instance creation
#   • DNS A-record (optional – set var.dns_domain to enable)
# =============================================================================

terraform {
  required_version = ">= 1.5"
  required_providers {
    vultr = {
      source  = "vultr/vultr"
      version = "~> 2.19"
    }
  }
}

provider "vultr" {
  api_key = var.vultr_api_key
  # Rate-limit retries – keeps noisy plans from tripping the 30 req/s cap
  rate_limit  = 100
  retry_limit = 3
}

# ---------------------------------------------------------------------------
# Variables
# ---------------------------------------------------------------------------
variable "vultr_api_key" {
  description = "Vultr Personal Access Token (PAT)"
  type        = string
  sensitive   = true
}

variable "region" {
  description = "Vultr region slug (vultr regions list)"
  type        = string
  default     = "ewr"  # Newark – closest to Princeton NJ
}

variable "plan" {
  description = "Vultr compute plan slug"
  type        = string
  default     = "vc2-2c-4gb"  # 2 vCPU / 4 GB – min for Kafka + Flask
}

variable "os_id" {
  description = "Vultr OS image ID – Ubuntu 22.04 LTS x64"
  type        = number
  default     = 1743
}

variable "ssh_key_id" {
  description = "Vultr SSH key ID to inject (create via API or console first)"
  type        = string
  default     = ""
}

variable "app_hostname" {
  description = "Hostname for the app instance"
  type        = string
  default     = "ardoura-ai"
}

variable "db_plan" {
  description = "Vultr managed DB plan"
  type        = string
  default     = "vultr-dbaas-startup-cc-1-55-1"  # 1 vCPU / 1 GB MySQL starter
}

variable "db_label" {
  description = "Label for the managed DB cluster"
  type        = string
  default     = "ardoura-mysql"
}

variable "dns_domain" {
  description = "Domain name for DNS A-record. Leave blank to skip DNS."
  type        = string
  default     = ""
}

variable "dns_subdomain" {
  description = "Subdomain prefix (e.g. 'app' → app.yourdomain.com)"
  type        = string
  default     = "ardoura"
}

# ---------------------------------------------------------------------------
# VPC – isolated private network
# ---------------------------------------------------------------------------
resource "vultr_vpc" "ardoura" {
  region      = var.region
  description = "ardouraAI private network"
  v4_subnet     = "10.10.0.0"
  v4_subnet_mask = 24
}

# ---------------------------------------------------------------------------
# Firewall group
# ---------------------------------------------------------------------------
resource "vultr_firewall_group" "ardoura" {
  description = "ardouraAI firewall rules"
}

# SSH from anywhere (restrict to your IP in production!)
resource "vultr_firewall_rule" "ssh" {
  firewall_group_id = vultr_firewall_group.ardoura.id
  protocol          = "tcp"
  ip_type           = "v4"
  subnet            = "0.0.0.0"
  subnet_size       = 0
  port              = "22"
  notes             = "SSH"
}

# Flask webhook port (HTTP)
resource "vultr_firewall_rule" "flask" {
  firewall_group_id = vultr_firewall_group.ardoura.id
  protocol          = "tcp"
  ip_type           = "v4"
  subnet            = "0.0.0.0"
  subnet_size       = 0
  port              = "5000"
  notes             = "Flask webhook receiver"
}

# HTTPS (443) for optional reverse-proxy / ngrok replacement
resource "vultr_firewall_rule" "https" {
  firewall_group_id = vultr_firewall_group.ardoura.id
  protocol          = "tcp"
  ip_type           = "v4"
  subnet            = "0.0.0.0"
  subnet_size       = 0
  port              = "443"
  notes             = "HTTPS"
}

# HTTP
resource "vultr_firewall_rule" "http" {
  firewall_group_id = vultr_firewall_group.ardoura.id
  protocol          = "tcp"
  ip_type           = "v4"
  subnet            = "0.0.0.0"
  subnet_size       = 0
  port              = "80"
  notes             = "HTTP"
}

# ---------------------------------------------------------------------------
# Startup script (cloud-init) – installs Docker, clones repo, launches stack
# ---------------------------------------------------------------------------
resource "vultr_startup_script" "ardoura" {
  name   = "ardoura-cloud-init"
  type   = "boot"
  script = base64encode(file("${path.module}/../cloud-init/startup.sh"))
}

# ---------------------------------------------------------------------------
# Compute instance
# ---------------------------------------------------------------------------
resource "vultr_instance" "ardoura" {
  region              = var.region
  plan                = var.plan
  os_id               = var.os_id
  label               = var.app_hostname
  hostname            = var.app_hostname
  firewall_group_id   = vultr_firewall_group.ardoura.id
  vpc_ids             = [vultr_vpc.ardoura.id]
  script_id           = vultr_startup_script.ardoura.id
  ssh_key_ids         = var.ssh_key_id != "" ? [var.ssh_key_id] : []
  backups             = "disabled"
  ddos_protection     = false
  activation_email    = false

  # Pass DB connection as user-data so startup.sh can write it to .env
  user_data = jsonencode({
    db_host     = vultr_database.ardoura.host
    db_port     = vultr_database.ardoura.port
    db_user     = vultr_database.ardoura.user
    db_password = vultr_database.ardoura.password
    db_name     = "ardoura"
  })

  depends_on = [
    vultr_database.ardoura,
    vultr_startup_script.ardoura
  ]
}

# ---------------------------------------------------------------------------
# Managed MySQL database cluster
# ---------------------------------------------------------------------------
resource "vultr_database" "ardoura" {
  database_engine      = "mysql"
  database_engine_version = "8"
  region               = var.region
  plan                 = var.db_plan
  label                = var.db_label
  cluster_time_zone    = "America/New_York"
}

# ---------------------------------------------------------------------------
# Optional DNS A-record
# ---------------------------------------------------------------------------
resource "vultr_dns_record" "ardoura" {
  count  = var.dns_domain != "" ? 1 : 0
  domain = var.dns_domain
  name   = var.dns_subdomain
  type   = "A"
  data   = vultr_instance.ardoura.main_ip
  ttl    = 300
}

# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------
output "app_public_ip" {
  description = "Public IP of the ArdouraAI app instance"
  value       = vultr_instance.ardoura.main_ip
}

output "app_private_ip" {
  description = "Private VPC IP of the app instance"
  value       = vultr_instance.ardoura.internal_ip
}

output "mysql_host" {
  description = "Managed MySQL host"
  value       = vultr_database.ardoura.host
  sensitive   = true
}

output "mysql_port" {
  description = "Managed MySQL port"
  value       = vultr_database.ardoura.port
}

output "mysql_user" {
  description = "Managed MySQL username"
  value       = vultr_database.ardoura.user
  sensitive   = true
}

output "mysql_password" {
  description = "Managed MySQL password"
  value       = vultr_database.ardoura.password
  sensitive   = true
}

output "webhook_url" {
  description = "Jira webhook endpoint to register"
  value       = "http://${vultr_instance.ardoura.main_ip}:5000/jira-webhook"
}
