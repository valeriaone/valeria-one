"""A simple website for businesses that have none (or only a Facebook page): /s/<slug>.

Built from what the business already keeps in Valeria One — services and prices, opening hours, booking
link, channels — plus a few lines the owner writes. A contact form drops the message straight into the
inbox. The page speaks the visitor's language for its own words; the owner's text stays as written.
"""
from __future__ import annotations

import re
import secrets
from datetime import datetime, timezone

from sqlalchemy import func, insert, select, update
from sqlalchemy.engine import Connection

from .. import blocks, db, packs, phones
from . import Invalid, NotFound, contacts, log

FIELDS = {"headline": 120, "about": 2000, "address": 300, "map_url": 500, "whatsapp": 40, "instagram": 200,
          "facebook": 200, "tiktok": 200, "email": 320, "phone": 40, "color": 7, "photo_url": 500,
          "preview": 1,                                      # "see your space in our style": visitors try the AI visualizer
          "assistant": 1}                                    # the "ask us" chat bubble, on this site and on any site by script tag
_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


def save(conn: Connection, biz: dict, actor: str, *, enabled: bool | None = None, **fields) -> dict:
    cur = dict(biz.get("site") or {})
    pack = packs.country(biz["country"])
    for k, v in fields.items():
        if k not in FIELDS or v is None:
            continue
        v = str(v).strip()[:FIELDS[k]]
        if not v:
            cur.pop(k, None)
            continue
        if k in ("preview", "assistant") and v != "1":
            raise Invalid(f"{k}: on or off", k)
        if k == "color" and not _COLOR.match(v):
            raise Invalid("color like #4d5bff", "color")
        if k in ("map_url", "photo_url") and (not v.startswith("https://") or re.search(r"[\s\"'()<>\\]", v)):
            raise Invalid(f"{k}: an https:// address", k)
        if k in ("whatsapp", "phone"):
            try:
                v = phones.e164(v, pack)
            except phones.BadPhone:
                raise Invalid(f"{k}: check the number", k) from None
        if k == "instagram":
            v = v.lstrip("@").split("/")[-1] or v
            if not re.fullmatch(r"[A-Za-z0-9._]{1,30}", v):
                raise Invalid("instagram: just the @username", k)
        if k == "tiktok":
            v = v.split("?")[0].rstrip("/").split("/")[-1].lstrip("@")
            if not re.fullmatch(r"[A-Za-z0-9._]{2,24}", v):
                raise Invalid("tiktok: just the @username", k)
        if k == "facebook" and not v.startswith("https://"):
            v = "https://facebook.com/" + v.lstrip("@/")
        if k == "email" and ("@" not in v or " " in v):
            raise Invalid("email: check it", k)
        cur[k] = v
    vals = {"site": cur, "updated_at": func.now()}
    if enabled is not None:
        vals["site_enabled"] = enabled
    conn.execute(update(db.businesses).where(db.businesses.c.id == biz["id"]).values(**vals))
    row = dict(conn.execute(select(db.businesses).where(db.businesses.c.id == biz["id"])).mappings().one())
    if row["site_enabled"] and not row["slug"]:
        from . import booking as BK
        row = BK.save_settings(conn, row, actor, slug=BK._free_slug(conn, row["name"]))
    log(conn, biz["id"], "site.updated", actor, biz["id"], {"enabled": row["site_enabled"]})
    return row


def public(conn: Connection, slug: str) -> dict:
    row = conn.execute(select(db.businesses).where(db.businesses.c.slug == (slug or "").lower(),
                                                   db.businesses.c.site_enabled)).mappings().first()
    if not row or not blocks.on(dict(row), "website"):
        raise NotFound("site")
    return dict(row)


def _webchat_account(conn: Connection, biz: dict):
    A = db.channel_accounts
    acc = conn.execute(select(A).where(A.c.business_id == biz["id"], A.c.channel == "webchat", A.c.active)).mappings().first()
    if acc:
        return acc
    return conn.execute(insert(A).values(business_id=biz["id"], channel="webchat", name="Website", config={},
                                         hook_id=secrets.token_urlsafe(18)).returning(A)).mappings().one()


def contact_form(conn: Connection, biz: dict, *, name: str, phone: str = "", email: str = "", message: str = "") -> dict:
    name, message = (name or "").strip()[:200], (message or "").strip()[:2000]
    if not name:
        raise Invalid("tell us your name", "name")
    if not message:
        raise Invalid("write your message", "message")
    pack = packs.country(biz["country"])
    e164 = None
    if (phone or "").strip():
        try:
            e164 = phones.e164(phone, pack)
        except phones.BadPhone:
            raise Invalid("check the phone number", "phone") from None
    mail = (email or "").strip().lower() or None
    if mail and ("@" not in mail or " " in mail):
        raise Invalid("check the email", "email")
    if not (e164 or mail):
        raise Invalid("leave a phone or an email so they can answer you", "phone")
    I = db.identities
    cid = None
    for ch, addr in (("whatsapp", e164), ("email", mail)):
        if addr and not cid:
            cid = conn.execute(select(I.c.contact_id).where(I.c.business_id == biz["id"], I.c.channel == ch, I.c.address == addr)).scalar()
    if cid:
        contact = contacts.get(conn, biz["id"], cid)
        upd = {k: v for k, v in (("phone", e164), ("email", mail)) if v and not contact.get(k)}
        if upd:
            contact = contacts.patch(conn, biz, "website", cid, upd)
    else:
        data = {"name": name, "source": "website"}
        if e164:
            data["phone"] = e164
        if mail:
            data["email"] = mail
        contact = contacts.create(conn, biz, "website", data)
    acc = _webchat_account(conn, biz)
    contacts.link(conn, biz["id"], contact["id"], "webchat", f"web:{contact['id']}")
    from . import inbox as X
    conv = X._conversation(conn, biz["id"], acc["id"], contact["id"])
    now = datetime.now(timezone.utc)
    mid = conn.execute(insert(db.messages).values(business_id=biz["id"], conversation_id=conv["id"], direction="in",
                                                  body=message, status="received").returning(db.messages.c.id)).scalar()
    conn.execute(update(db.conversations).where(db.conversations.c.id == conv["id"]).values(last_inbound_at=now, last_message_at=now,
                                                                                         status="open"))
    log(conn, biz["id"], "lead.website", "website", contact["id"], {"message_id": str(mid)})
    return {"contact": contact, "conversation_id": conv["id"]}


def json_ld(biz: dict, services: list[dict], url: str) -> dict:
    """schema.org LocalBusiness, so search engines show the business properly."""
    site = biz.get("site") or {}
    d = {"@context": "https://schema.org", "@type": "LocalBusiness", "name": biz["name"], "url": url}
    if site.get("about"):
        d["description"] = site["about"][:300]
    if site.get("phone") or site.get("whatsapp"):
        d["telephone"] = site.get("phone") or site.get("whatsapp")
    if site.get("address"):
        d["address"] = site["address"]
    if site.get("photo_url"):
        d["image"] = site["photo_url"]
    days = {"mon": "Mo", "tue": "Tu", "wed": "We", "thu": "Th", "fri": "Fr", "sat": "Sa", "sun": "Su"}
    hrs = [f"{days[k]} {a}-{b}" for k, v in (biz.get("hours") or {}).items() for a, b in v]
    if hrs:
        d["openingHours"] = hrs
    sameas = [x for x in (site.get("facebook"), f"https://instagram.com/{site['instagram']}" if site.get("instagram") else None,
                          f"https://www.tiktok.com/@{site['tiktok']}" if site.get("tiktok") else None) if x]
    if sameas:
        d["sameAs"] = sameas
    return d
