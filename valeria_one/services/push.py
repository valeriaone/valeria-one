"""Notifications on the owner's phone or computer (v0.18), with the standard Web Push protocol — no Firebase, no
third party of ours: the browser's own push service delivers them.

  * VAPID (RFC 8292): one key pair per server, made the first time it is needed and kept in instance_kv with the
    private key encrypted (VO_SECRET_KEY, like every other secret). The public key goes to the browser.
  * Encryption (RFC 8291, aes128gcm): every message is encrypted for that one device (its p256dh key and auth
    secret); the push service can't read it.
  * What is sent: a title, one line and the page to open — Valeria's questions and notices ("Ana is waiting",
    "Call back +52…"). Never a customer's message in full.
A device that is gone (404/410) is forgotten; one that keeps failing is dropped after MAX_FAILURES.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import struct
import time
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection

from .. import config, crypto, db
from . import Invalid, log

logger = logging.getLogger("valeria_one.push")
PS, KV = db.push_subscriptions, db.instance_kv
MAX_FAILURES = 5
TTL = 24 * 3600
RS = 4096


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def unb64u(s: str) -> bytes:
    s = (s or "").strip()
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _pub_bytes(key: ec.EllipticCurvePublicKey) -> bytes:
    return key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


# ---------------------------------------------------------------- this server's VAPID keys
def vapid(conn: Connection | None = None) -> tuple[str, ec.EllipticCurvePrivateKey]:
    """-> (public key, base64url, for the browser; the private key). Made once per server."""
    def get(c):
        return c.execute(select(KV.c.value).where(KV.c.key == "vapid")).scalar()
    if conn is None:
        with db.tx() as c:
            return vapid(c)
    v = get(conn)
    if not v:
        key = ec.generate_private_key(ec.SECP256R1())
        d = key.private_numbers().private_value.to_bytes(32, "big")
        sealed = crypto.seal({"secret": b64u(d)})["secret"]
        conn.execute(pg_insert(KV).values(key="vapid", value={"public": b64u(_pub_bytes(key.public_key())), "private": sealed})
                     .on_conflict_do_nothing(index_elements=[KV.c.key]))
        v = get(conn)
    d = unb64u(crypto.open_({"secret": v["private"]})["secret"])
    return v["public"], ec.derive_private_key(int.from_bytes(d, "big"), ec.SECP256R1())


def _jwt(endpoint: str, key: ec.EllipticCurvePrivateKey, now: float | None = None) -> str:
    u = urlsplit(endpoint)
    claims = {"aud": f"{u.scheme}://{u.netloc}", "exp": int((now or time.time()) + 12 * 3600),
              "sub": config.get("VO_PUSH_CONTACT") or f"mailto:admin@{urlsplit(config.PUBLIC_URL()).hostname or 'localhost'}"}
    head = b64u(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
    body = b64u(json.dumps(claims, separators=(",", ":")).encode())
    der = key.sign(f"{head}.{body}".encode(), ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    return f"{head}.{body}.{b64u(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"


# ---------------------------------------------------------------- RFC 8291 encryption
def _hkdf(salt: bytes, ikm: bytes, info: bytes, n: int) -> bytes:
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    return hmac.new(prk, info + b"\x01", hashlib.sha256).digest()[:n]


def encrypt(payload: bytes, p256dh: str, auth: str, *, salt: bytes | None = None,
            server_key: ec.EllipticCurvePrivateKey | None = None) -> bytes:
    """aes128gcm content coding for one device: header (salt, record size, our public key) + one encrypted record."""
    ua_pub = unb64u(p256dh)
    secret = unb64u(auth)
    if len(ua_pub) != 65 or len(secret) < 16:
        raise Invalid("not a push subscription", "keys")
    as_key = server_key or ec.generate_private_key(ec.SECP256R1())
    as_pub = _pub_bytes(as_key.public_key())
    ua_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_pub)
    shared = as_key.exchange(ec.ECDH(), ua_key)
    ikm = _hkdf(secret, shared, b"WebPush: info\x00" + ua_pub + as_pub, 32)
    salt = salt or os.urandom(16)
    cek = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    record = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)
    return salt + struct.pack("!IB", RS, len(as_pub)) + as_pub + record


def decrypt(body: bytes, ua_key: ec.EllipticCurvePrivateKey, auth: bytes) -> bytes:
    """The browser's side (tests prove encrypt() is right with it)."""
    salt, rs, idlen = body[:16], struct.unpack("!I", body[16:20])[0], body[20]
    as_pub = body[21:21 + idlen]
    record = body[21 + idlen:]
    ua_pub = _pub_bytes(ua_key.public_key())
    shared = ua_key.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_pub))
    ikm = _hkdf(auth, shared, b"WebPush: info\x00" + ua_pub + as_pub, 32)
    cek = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    plain = AESGCM(cek).decrypt(nonce, record, None)
    assert rs == RS
    return plain.rstrip(b"\x00")[:-1]


# ---------------------------------------------------------------- subscriptions
def subscribe(conn: Connection, biz: dict, user_id: UUID, sub: dict, ua: str = "") -> dict:
    endpoint = str((sub or {}).get("endpoint") or "")
    keys = (sub or {}).get("keys") or {}
    if not endpoint.startswith("https://") or len(endpoint) > 2000:
        raise Invalid("not a push subscription", "endpoint")
    if len(unb64u(keys.get("p256dh") or "")) != 65 or len(unb64u(keys.get("auth") or "")) < 16:
        raise Invalid("not a push subscription", "keys")
    vals = {"business_id": biz["id"], "user_id": user_id, "p256dh": keys["p256dh"][:200], "auth": keys["auth"][:100],
            "ua": (ua or "")[:200], "failures": 0}
    row = conn.execute(pg_insert(PS).values(endpoint=endpoint, **vals)
                       .on_conflict_do_update(index_elements=[PS.c.endpoint], set_=vals).returning(PS)).mappings().one()
    log(conn, biz["id"], "push.subscribed", f"user:{user_id}", row["id"])
    return dict(row)


def unsubscribe(conn: Connection, user_id: UUID, endpoint: str) -> int:
    return conn.execute(PS.delete().where(PS.c.user_id == user_id, PS.c.endpoint == endpoint)).rowcount


def devices(conn: Connection, business_id: UUID, user_id: UUID | None = None) -> list[dict]:
    q = select(PS).where(PS.c.business_id == business_id)
    if user_id:
        q = q.where(PS.c.user_id == user_id)
    return [dict(r) for r in conn.execute(q.order_by(PS.c.created_at)).mappings()]


def notify(business_id: UUID, title: str, body: str, url: str = "/app", http=None, user_id: UUID | None = None) -> int:
    """Send one notification to every device that asked for this business's. -> how many took it. Never raises."""
    try:
        with db.tx() as conn:
            subs = devices(conn, business_id, user_id)
            if not subs:
                return 0
            pub, key = vapid(conn)
    except Exception:                                          # noqa: BLE001 — a notification is never worth an error
        logger.exception("push: could not prepare")
        return 0
    payload = json.dumps({"title": title[:80], "body": body[:180], "url": url if url.startswith("/") else "/app"}).encode()
    h = http or httpx.Client(timeout=10)
    ok = 0
    for s in subs:
        try:
            r = h.post(s["endpoint"], content=encrypt(payload, s["p256dh"], s["auth"]),
                       headers={"Content-Encoding": "aes128gcm", "TTL": str(TTL), "Urgency": "normal",
                                "Authorization": f"vapid t={_jwt(s['endpoint'], key)}, k={pub}",
                                "Content-Type": "application/octet-stream"})
            status = r.status_code
        except Exception:                                      # noqa: BLE001
            status = 0
        with db.tx() as conn:
            if status in (200, 201, 202):
                ok += 1
                conn.execute(update(PS).where(PS.c.id == s["id"]).values(failures=0, last_ok_at=func.now()))
            elif status in (404, 410):
                conn.execute(PS.delete().where(PS.c.id == s["id"]))
            else:
                n = s["failures"] + 1
                if n >= MAX_FAILURES:
                    conn.execute(PS.delete().where(PS.c.id == s["id"]))
                else:
                    conn.execute(update(PS).where(PS.c.id == s["id"]).values(failures=n))
    return ok
