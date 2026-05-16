#!/usr/bin/env python3
"""
tenant/secrets_manager.py
=========================
Encrypts and decrypts tenant secrets.env files.

Design:
  - Master key lives in /etc/ardoura/credentials.json  (never in git)
  - Each tenant gets their own Fernet key derived from master + tenant_id
  - Encrypted secrets stored at /etc/ardoura/tenants/{tenant_id}/secrets.enc
  - At runtime: decrypt -> parse -> return dict of env vars

credentials.json format:
  {
    "master_key": "base64-encoded-32-byte-key",
    "version": 1
  }
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet

CREDENTIALS_FILE = Path(os.environ.get(
    "ARDOURA_CREDENTIALS", "/etc/ardoura/credentials.json"
))
TENANTS_SECRETS_DIR = Path(os.environ.get(
    "ARDOURA_SECRETS_DIR", "/etc/ardoura/tenants"
))


def init_master_key() -> None:
    """Generate master key if it doesn't exist. Run once at setup."""
    CREDENTIALS_FILE.parent.mkdir(parents=True, exist_ok=True)
    TENANTS_SECRETS_DIR.mkdir(parents=True, exist_ok=True)

    if CREDENTIALS_FILE.exists():
        print(f"[Secrets] Master key already exists at {CREDENTIALS_FILE}")
        return

    master_key = base64.urlsafe_b64encode(os.urandom(32)).decode()
    CREDENTIALS_FILE.write_text(json.dumps({
        "master_key": master_key,
        "version": 1
    }, indent=2))
    CREDENTIALS_FILE.chmod(0o600)
    print(f"[Secrets] Master key generated at {CREDENTIALS_FILE}")


def _load_master_key() -> bytes:
    if not CREDENTIALS_FILE.exists():
        raise FileNotFoundError(
            f"credentials.json not found at {CREDENTIALS_FILE}. "
            "Run secrets_manager.init_master_key() first."
        )
    data = json.loads(CREDENTIALS_FILE.read_text())
    return base64.urlsafe_b64decode(data["master_key"])


def _tenant_fernet(tenant_id: str) -> Fernet:
    """Derive a unique Fernet key per tenant from master key + tenant_id."""
    master = _load_master_key()
    # HKDF-like derivation: SHA256(master || tenant_id)
    derived = hashlib.sha256(master + tenant_id.encode()).digest()
    key = base64.urlsafe_b64encode(derived)
    return Fernet(key)


def encrypt_secrets(tenant_id: str, secrets: dict) -> Path:
    """
    Encrypt a dict of secrets and save to disk.
    Returns the path to the encrypted file.
    """
    tenant_dir = TENANTS_SECRETS_DIR / tenant_id
    tenant_dir.mkdir(parents=True, exist_ok=True)
    tenant_dir.chmod(0o700)

    # Serialize as .env format
    env_text = "\n".join(
        f"{k}={v}" for k, v in secrets.items()
    ).encode()

    fernet   = _tenant_fernet(tenant_id)
    encrypted = fernet.encrypt(env_text)

    enc_path = tenant_dir / "secrets.enc"
    enc_path.write_bytes(encrypted)
    enc_path.chmod(0o600)
    print(f"[Secrets] Encrypted secrets saved for tenant {tenant_id}")
    return enc_path


def decrypt_secrets(tenant_id: str,
                    secrets_path: Optional[str] = None) -> dict:
    """
    Decrypt and return secrets as a dict for a given tenant.
    """
    if secrets_path:
        enc_path = Path(secrets_path)
    else:
        enc_path = TENANTS_SECRETS_DIR / tenant_id / "secrets.enc"

    if not enc_path.exists():
        raise FileNotFoundError(
            f"Encrypted secrets not found at {enc_path}. "
            "Has this tenant been onboarded?"
        )

    fernet    = _tenant_fernet(tenant_id)
    decrypted = fernet.decrypt(enc_path.read_bytes()).decode()

    # Parse .env format into dict
    secrets = {}
    for line in decrypted.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, _, v = line.partition("=")
            secrets[k.strip()] = v.strip()
    return secrets


def write_tenant_env(tenant_id: str,
                     secrets_path: Optional[str] = None) -> dict:
    """
    Decrypt secrets and return as dict ready for os.environ injection.
    Also writes a temp .env to /tmp/ardoura_{tenant_id}.env for subprocess use.
    """
    secrets  = decrypt_secrets(tenant_id, secrets_path)
    tmp_path = Path(f"/tmp/ardoura_{tenant_id}.env")
    tmp_path.write_text("\n".join(f"{k}={v}" for k, v in secrets.items()))
    tmp_path.chmod(0o600)
    return secrets
