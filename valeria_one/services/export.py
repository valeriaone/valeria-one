"""Everything a business has here, as one JSON file its owner can download (GET /app/export, GET /v1/export).

The business's data is the business's: a privacy law (GDPR, the Philippines' Data Privacy Act, India's DPDP Act…)
lets its customers ask what is kept about them, and the owner must be able to leave with all of it. Secrets
never leave: connection settings, API keys, password hashes and AI keys are not in the file.
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.engine import Connection

from .. import __version__, db
from . import log

# what goes in, by table (each filtered to the business); secrets and the server's own records stay out
TABLES = ("contacts", "contact_identities", "conversations", "messages", "deals", "appointments", "booking_services", "quotes",
          "payments", "automations", "campaigns", "scheduled_messages", "review_requests", "growth_settings", "referral_links",
          "referrals", "meta_leads", "social_comments", "comment_rules", "billing_payments", "directory_listings", "reviews",
          "posts", "events")
HIDDEN = {"businesses": ("stripe_customer_id", "stripe_subscription_id")}


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
        out[name] = [dict(r) for r in conn.execute(select(t).where(t.c.business_id == business_id)
                                                   .order_by(t.c.created_at if "created_at" in t.c else t.c.id)).mappings()]
    log(conn, business_id, "business.exported", actor, business_id, {"rows": sum(len(out[n]) for n in TABLES)})
    return out
