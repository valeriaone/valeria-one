"""Outgoing webhooks: other apps (a website, Zapier/Make, an ERP, Meta's own tools…) hear about everything
that happens in a business — a new customer, a message, a booking, a deal moving.

Every event is written to an outbox in the SAME transaction as the event itself, so nothing is lost and
nothing is announced that didn't happen. A job (`python -m valeria_one.jobs webhooks`) delivers them,
signed, and retries with back-off for a day before giving up.

Each delivery: POST JSON {"id", "type", "business_id", "subject_id", "data", "created_at"} with headers
X-VO-Event, X-VO-Delivery, X-VO-Signature (hex HMAC-SHA256 of the body with the endpoint's secret).
"""
from __future__ import annotations

import ipaddress
import json
import secrets
import socket
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from uuid import UUID

import httpx
from sqlalchemy import func, insert, select, update
from sqlalchemy.engine import Connection

from .. import config, crypto, db
from . import Invalid, NotFound

W, WD = db.webhook_endpoints, db.webhook_deliveries
BACKOFF = [60, 300, 1800, 7200, 21600, 43200, 86400]      # seconds after attempt 1..7, then give up
MAX_PER_BUSINESS = 10
# v0.21: the events the owner picks from on "Connect other apps" (any event type, a "<kind>.*" or "*" still works too)
EVENTS = ("contact.created", "deal.created", "deal.stage_changed", "invoice.paid", "payment.paid", "form.submitted", "pos.sale",
          "booking.made", "appointment.booked", "member.joined")


def _check_url(url: str, resolve: bool = True) -> str:
    """https only, and never a private / local address (no reaching into the server's own network)."""
    u = urlparse((url or "").strip())
    if u.scheme != "https" or not u.hostname:
        raise Invalid("the webhook address must start with https://", "url")
    if u.username or u.password:
        raise Invalid("no user or password inside the address", "url")
    if resolve and config.get("VO_WEBHOOKS_ALLOW_PRIVATE", "0") != "1":
        try:
            addrs = {ai[4][0] for ai in socket.getaddrinfo(u.hostname, u.port or 443, type=socket.SOCK_STREAM)}
        except socket.gaierror:
            raise Invalid("that address doesn't exist (DNS)", "url") from None
        for a in addrs:
            if not ipaddress.ip_address(a.split("%")[0]).is_global:
                raise Invalid("that address points inside a private network", "url")
    return url.strip()


def create(conn: Connection, biz: dict, actor: str, url: str, events: list[str] | None = None) -> tuple[dict, str]:
    """-> (endpoint, secret shown once)."""
    url = _check_url(url)
    n = conn.execute(select(func.count()).select_from(W).where(W.c.business_id == biz["id"], W.c.active)).scalar()
    if n >= MAX_PER_BUSINESS:
        raise Invalid(f"at most {MAX_PER_BUSINESS} webhooks per business", "url")
    evs = sorted({e.strip() for e in (events or ["*"]) if e.strip()}) or ["*"]
    secret = "whsec_" + secrets.token_urlsafe(24)
    row = conn.execute(insert(W).values(business_id=biz["id"], url=url, events=evs,
                                        secret=crypto.seal({"secret": secret})["secret"]).returning(W)).mappings().one()
    from . import log
    log(conn, biz["id"], "webhook.created", actor, row["id"], {"url": url})
    return public(row), secret


def public(row) -> dict:
    d = dict(row)
    d.pop("secret", None)
    return d


def endpoints(conn: Connection, business_id: UUID) -> list[dict]:
    return [public(r) for r in conn.execute(select(W).where(W.c.business_id == business_id, W.c.active)
                                            .order_by(W.c.created_at)).mappings()]


def delete(conn: Connection, business_id: UUID, actor: str, endpoint_id: UUID) -> None:
    if not conn.execute(update(W).where(W.c.business_id == business_id, W.c.id == endpoint_id, W.c.active)
                        .values(active=False)).rowcount:
        raise NotFound("webhook")
    from . import log
    log(conn, business_id, "webhook.deleted", actor, endpoint_id)


def deliveries(conn: Connection, business_id: UUID, endpoint_id: UUID, limit: int = 50) -> list[dict]:
    return [dict(r) for r in conn.execute(select(WD).where(WD.c.business_id == business_id, WD.c.endpoint_id == endpoint_id)
                                          .order_by(WD.c.id.desc()).limit(max(1, min(limit, 200)))).mappings()]


def enqueue(conn: Connection, business_id: UUID, event: dict) -> None:
    """Called by services.log() inside the event's own transaction."""
    eps = conn.execute(select(W.c.id, W.c.events).where(W.c.business_id == business_id, W.c.active)).all()
    for ep_id, evs in eps:
        if "*" in evs or event["type"] in evs or event["type"].split(".")[0] + ".*" in evs:
            conn.execute(insert(WD).values(business_id=business_id, endpoint_id=ep_id, event_id=event["id"],
                                           payload=json.loads(json.dumps(event, default=str))))


def deliver_due(http: httpx.Client | None = None, now: datetime | None = None, limit: int = 200) -> dict:
    now = now or datetime.now(timezone.utc)
    h = http or httpx.Client(timeout=10, follow_redirects=False)
    stats = {"delivered": 0, "retry": 0, "gave_up": 0}
    with db.tx() as conn:
        due = conn.execute(select(WD, W.c.url, W.c.secret, W.c.active).join(W, W.c.id == WD.c.endpoint_id)
                           .where(WD.c.delivered_at.is_(None), WD.c.given_up_at.is_(None), WD.c.next_at <= now)
                           .order_by(WD.c.id).limit(limit).with_for_update(skip_locked=True, of=WD)).mappings().all()
    for d in due:
        if not d["active"]:
            _finish(d["id"], d["endpoint_id"], gave_up=True, error="webhook deleted")
            stats["gave_up"] += 1
            continue
        body = json.dumps(d["payload"], ensure_ascii=False, separators=(",", ":")).encode()
        secret = crypto.open_({"secret": d["secret"]})["secret"]
        status, err = None, None
        try:
            _check_url(d["url"])                                  # DNS may have changed since it was saved
            r = h.post(d["url"], content=body, headers={
                "Content-Type": "application/json", "User-Agent": "ValeriaOne-Webhooks/1",
                "X-VO-Event": d["payload"].get("type", ""), "X-VO-Delivery": str(d["id"]),
                "X-VO-Signature": crypto.hmac_sha256_hex(secret, body)})
            status = r.status_code
            if 200 <= status < 300:
                _finish(d["id"], d["endpoint_id"], status=status)
                stats["delivered"] += 1
                continue
            err = f"answered {status}"
        except Invalid as e:
            err = str(e)
        except httpx.HTTPError as e:
            err = type(e).__name__
        attempts = d["attempts"] + 1
        if attempts > len(BACKOFF):
            _finish(d["id"], d["endpoint_id"], status=status, error=err, gave_up=True, attempts=attempts)
            stats["gave_up"] += 1
        else:
            with db.tx() as conn:
                conn.execute(update(WD).where(WD.c.id == d["id"]).values(
                    attempts=attempts, next_at=now + timedelta(seconds=BACKOFF[attempts - 1]), last_status=status, last_error=err))
                conn.execute(update(W).where(W.c.id == d["endpoint_id"]).values(last_status=status, last_error=err))
            stats["retry"] += 1
    return stats


def _finish(did, endpoint_id, status=None, error=None, gave_up=False, attempts=None) -> None:
    with db.tx() as conn:
        vals = {"last_status": status, "last_error": error}
        if attempts is not None:
            vals["attempts"] = attempts
        vals["given_up_at" if gave_up else "delivered_at"] = func.now()
        conn.execute(update(WD).where(WD.c.id == did).values(**vals))
        conn.execute(update(W).where(W.c.id == endpoint_id).values(last_status=status, last_error=error))


def test(conn: Connection, biz: dict, actor: str, endpoint_id: UUID) -> None:
    """Queue a 'webhook.test' event for this endpoint only."""
    ep = conn.execute(select(W).where(W.c.business_id == biz["id"], W.c.id == endpoint_id, W.c.active)).mappings().first()
    if not ep:
        raise NotFound("webhook")
    ev = {"id": 0, "type": "webhook.test", "business_id": str(biz["id"]), "subject_id": str(endpoint_id),
          "data": {"hello": "from Valeria One"}, "created_at": datetime.now(timezone.utc).isoformat()}
    conn.execute(insert(WD).values(business_id=biz["id"], endpoint_id=endpoint_id, event_id=0, payload=ev))


def redeliver(conn: Connection, business_id: UUID, actor: str, delivery_id: int) -> None:
    """v0.21: one that was given up on goes again (its endpoint must still be there): from attempt 1, next round."""
    d = conn.execute(select(WD).join(W, W.c.id == WD.c.endpoint_id).where(
        WD.c.business_id == business_id, WD.c.id == delivery_id, WD.c.given_up_at.isnot(None), W.c.active)).mappings().first()
    if not d:
        raise NotFound("delivery")
    conn.execute(update(WD).where(WD.c.id == delivery_id).values(given_up_at=None, attempts=0, next_at=func.now(), last_error=None))
    from . import log
    log(conn, business_id, "webhook.redelivered", actor, d["endpoint_id"], {"delivery_id": delivery_id})
