"""Claim kits (claim01, v0.32): a business we already set up — its sample site, its demo — waiting for its owner.

The operator prepares a KIT for a business that does not have an account yet (the gym we made a sample site for, the
car service we showed a demo to) and sends its owner one link, /claim/<code>. The owner signs up on that page as anyone
signs up (their own email and password; nothing about the business is verified by the kit), and the new account starts
with what the kit holds instead of empty:

    {"business_name": "Malik Hasnain", "country": "US", "industry": "generic", "timezone": "America/Los_Angeles",
     "site":     {"headline": "...", "about": "...", "address": "...", "color": "#111111", "whatsapp": "+14243944457"},
     "services": [{"name": "Airport transfer · LAX", "minutes": 90, "price_minor": 19500}, ...],
     "hours":    {"mon": [["06:00", "22:00"]], ...},                    # the booking page's hours (optional)
     "tools":    {"staff": true, "pos": true, ...}}                     # tools switched on beyond the trade's kit

A link works once: the claim and the sign-up happen in one transaction, so two people opening the same link cannot both
get it. Nothing is sent to anyone by a claim; it only fills the new account.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import insert, select, update
from sqlalchemy.engine import Connection

from .. import db, kits, packs
from . import Invalid, log
from . import booking as BK
from . import businesses as B
from . import site as ST

CK = db.claim_kits
MAX_SERVICES = 30


def clean(kit: dict) -> dict:
    """The kit as stored: only the known parts, each checked the way the screens that edit them check it."""
    if not isinstance(kit, dict):
        raise Invalid("a kit is a JSON object", "kit")
    name = " ".join(str(kit.get("business_name") or "").split())[:200]
    if not name:
        raise Invalid("the kit needs the business name", "business_name")
    try:
        country = packs.country(str(kit.get("country") or "")).code
    except KeyError:
        raise Invalid("the kit needs a known country", "country") from None
    try:
        industry = packs.industry(str(kit.get("industry") or "generic")).code
    except KeyError:
        raise Invalid("unknown industry in the kit", "industry") from None
    out = {"business_name": name, "country": country, "industry": industry}
    if kit.get("timezone"):
        from zoneinfo import ZoneInfo
        try:
            ZoneInfo(str(kit["timezone"]))
        except Exception:
            raise Invalid("unknown timezone in the kit", "timezone") from None
        out["timezone"] = str(kit["timezone"])
    site = kit.get("site") or {}
    if not isinstance(site, dict):
        raise Invalid("site: an object", "site")
    out["site"] = {k: str(v)[:ST.FIELDS[k]] for k, v in site.items()
                   if k in ST.FIELDS and k not in ("assistant_photo", "hero_video", "preview", "assistant", "lang_fixed", "brand") and v}
    svcs = []
    for s in (kit.get("services") or [])[:MAX_SERVICES]:
        n = " ".join(str((s or {}).get("name") or "").split())[:120]
        if not n:
            continue
        mins = int(s.get("minutes") or 60)
        price = s.get("price_minor")
        svcs.append({"name": n, "minutes": max(5, min(mins, 24 * 60)), "price_minor": None if price is None else max(0, int(price))})
    out["services"] = svcs
    if kit.get("hours"):
        out["hours"] = BK.clean_hours(kit["hours"])
    tools = kit.get("tools") or {}
    if not isinstance(tools, dict):
        raise Invalid("tools: an object", "tools")
    out["tools"] = {k: bool(v) for k, v in tools.items() if k in kits.OPTIONAL}
    return out


def create(conn: Connection, kit: dict, note: str = "") -> dict:
    """A new kit and its one-use code. Returns the row."""
    row = conn.execute(insert(CK).values(code=secrets.token_urlsafe(18)[:24], kit=clean(kit), note=(note or "").strip()[:500] or None)
                       .returning(CK)).mappings().one()
    return dict(row)


def find(conn: Connection, code: str) -> dict | None:
    """The kit behind a claim link, while it has not been used; None otherwise (unknown and used look the same)."""
    code = (code or "").strip()
    if not code or len(code) > 32:
        return None
    row = conn.execute(select(CK).where(CK.c.code == code, CK.c.claimed_at.is_(None))).mappings().first()
    return dict(row) if row else None


def take(conn: Connection, code: str, business_id: UUID) -> dict:
    """Mark the kit used by this new business — atomically, so one link makes one account. Returns the kit."""
    row = conn.execute(update(CK).where(CK.c.code == (code or "").strip(), CK.c.claimed_at.is_(None))
                       .values(claimed_at=datetime.now(timezone.utc), business_id=business_id)
                       .returning(CK)).mappings().first()
    if not row:
        raise Invalid("this claim link was already used — sign in, or sign up without it", "claim")
    return dict(row)


def apply(conn: Connection, biz: dict, actor: str, kit: dict) -> dict:
    """Fill a brand-new business from its kit: timezone, tools, website, booking page and services. Returns the business."""
    if kit.get("timezone"):
        B.patch(conn, biz["id"], actor, timezone=kit["timezone"])
    if kit.get("tools"):
        row = conn.execute(select(db.businesses.c.tools).where(db.businesses.c.id == biz["id"])).scalar() or {}
        conn.execute(update(db.businesses).where(db.businesses.c.id == biz["id"]).values(tools={**row, **kit["tools"]}))
    fresh = lambda: dict(conn.execute(select(db.businesses).where(db.businesses.c.id == biz["id"])).mappings().one())  # noqa: E731
    if kit.get("site"):
        ST.save(conn, fresh(), actor, enabled=True, **kit["site"])
    if kit.get("services") or kit.get("hours"):
        BK.save_settings(conn, fresh(), actor, enabled=True, slug=BK.slugify(kit["business_name"]),
                         hours=kit.get("hours"), notice_minutes=60, days_ahead=30)
        for s in kit.get("services") or []:
            BK.add_service(conn, fresh(), actor, name=s["name"], minutes=s["minutes"], price_minor=s["price_minor"])
    log(conn, biz["id"], "business.claimed", actor, biz["id"], {"services": len(kit.get("services") or []),
                                                                  "site": bool(kit.get("site")), "tools": sorted(kit.get("tools") or {})})
    return fresh()


def main() -> None:
    """python -m valeria_one.services.claims new < kit.json   → prints the claim path (operator only)."""
    import json
    import sys
    if sys.argv[1:2] != ["new"]:
        raise SystemExit("usage: python -m valeria_one.services.claims new [note] < kit.json")
    kit = json.load(sys.stdin)
    with db.tx() as conn:
        row = create(conn, kit, " ".join(sys.argv[2:]))
    print(f"/claim/{row['code']}")


if __name__ == "__main__":
    main()
