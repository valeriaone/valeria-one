"""Secrets a business gives us (tokens, passwords) are stored encrypted with VO_SECRET_KEY (Fernet).
API keys are never stored at all — only their sha256."""
from __future__ import annotations

import hashlib
import hmac
import secrets

from cryptography.fernet import Fernet

from . import config

_SECRET_FIELDS = ("token", "access_token", "page_token", "app_secret", "auth_token", "password", "verify_token", "secret",
                  "bot_token", "pin")


def _f() -> Fernet:
    return Fernet(config.get("VO_SECRET_KEY", required=True).encode())


def seal(cfg: dict) -> dict:
    """Encrypt the secret fields of a channel config before it goes into the database."""
    out = dict(cfg)
    for k in _SECRET_FIELDS:
        if out.get(k):
            out[k] = "enc:" + _f().encrypt(str(out[k]).encode()).decode()
    return out


def open_(cfg: dict) -> dict:
    out = dict(cfg)
    for k in _SECRET_FIELDS:
        v = out.get(k)
        if isinstance(v, str) and v.startswith("enc:"):
            out[k] = _f().decrypt(v[4:].encode()).decode()
    return out


def redact(cfg: dict) -> dict:
    """What the API shows: secrets as '••••'."""
    return {k: ("••••" if k in _SECRET_FIELDS and v else v) for k, v in cfg.items()}


def new_api_key(sandbox: bool) -> tuple[str, str, str]:
    """-> (full key shown once, prefix kept for display, sha256 kept for lookup)."""
    key = ("vo_test_" if sandbox else "vo_live_") + secrets.token_urlsafe(32)
    return key, key[:16], sha256(key)


def sha256(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def hmac_sha256_hex(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def same(a: str, b: str) -> bool:
    return hmac.compare_digest((a or "").encode(), (b or "").encode())
