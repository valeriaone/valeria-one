"""Everything a business has here, as one JSON file its owner can download (GET /app/export, GET /v1/export).

The business's data is the business's: a privacy law (GDPR, the Philippines' Data Privacy Act, India's DPDP Act…)
lets its customers ask what is kept about them, and the owner must be able to leave with all of it. Secrets
never leave: connection settings, API keys, password hashes and AI keys are not in the file.

v0.21: the file is made from the database's own list of tables — EVERY table that holds a business's rows goes in,
except the few that are only secrets (SKIP, each with its reason) — so a table added later can't be forgotten
(tests/test_ars02.py checks it). Inside a table, the secret columns stay out (HIDDEN). Files: their details are in the
file; the pictures and documents themselves are not (they can be big) — each opens from the app as before.
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.engine import Connection

from .. import __version__, db
from . import log

# whole tables that stay out, and why (anything else with a business_id goes in)
SKIP = {
    "memberships": "in the file as 'people' (who is on the team, without passwords)",
    "channel_accounts": "in the file as 'channels' (without their passwords and tokens)",
    "api_keys": "credentials (only a fingerprint is kept anyway)",
    "invitations": "credentials (a pending sign-in invitation)",
    "push_subscriptions": "credentials (a phone's notification keys)",
    "member_links": "credentials (one-time sign-in links)",
}
# columns that stay out of a table that goes in
HIDDEN = {
    "businesses": ("stripe_customer_id", "stripe_subscription_id"),
    "ai_settings": ("api_key",),
    "app_links": ("tokens",),
    "calendar_links": ("refresh_token",),
    "google_links": ("tokens",),
    "payment_accounts": ("config",),
    "webhook_endpoints": ("secret",),
    "inbound_hooks": ("token_hash",),
    "owner_numbers": ("pair_code",),
    "files": ("data", "thumb"),
}
_FIRST = ("contacts", "contact_identities", "conversations", "messages", "deals", "appointments")   # the file reads well top-down


def tables() -> list[str]:
    """Every table that holds a business's rows and goes in the file (from the database's own list)."""
    names = sorted(n for n, t in db.metadata.tables.items() if "business_id" in t.c and n not in SKIP and n != "businesses")
    return [n for n in _FIRST if n in names] + [n for n in names if n not in _FIRST]


TABLES = tuple(tables())


def business_data(conn: Connection, business_id: UUID, actor: str) -> dict:
    biz = dict(conn.execute(select(db.businesses).where(db.businesses.c.id == business_id)).mappings().one())
    for k in HIDDEN["businesses"]:
        biz.pop(k, None)
    out = {"format": "valeria-one-export", "version": __version__, "exported_at": datetime.now(timezone.utc),
           "business": biz}
    M, U = db.memberships, db.users
    out["people"] = [dict(r) for r in conn.execute(select(U.c.email, U.c.name, M.c.role, M.c.created_at)
                                                  .join(M, M.c.user_id == U.c.id).where(M.c.business_id == business_id)).mappings()]
    out["channels"] = [dict(r) for r in conn.execute(select(db.channel_accounts.c.id, db.channel_accounts.c.channel,
                                                            db.channel_accounts.c.name, db.channel_accounts.c.active)
                                                     .where(db.channel_accounts.c.business_id == business_id)).mappings()]
    for name in TABLES:
        t = db.metadata.tables[name]
        cols = [c for c in t.c if c.name not in HIDDEN.get(name, ())]
        order = t.c.created_at if "created_at" in t.c else (t.c.id if "id" in t.c else t.c.business_id)
        out[name] = [dict(r) for r in conn.execute(select(*cols).where(t.c.business_id == business_id).order_by(order)).mappings()]
    log(conn, business_id, "business.exported", actor, business_id, {"rows": sum(len(out[n]) for n in TABLES)})
    return out
