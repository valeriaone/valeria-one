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
          "assistant": 1,
          "assistant_photo": 36,
          "lang_fixed": 1,
          "brand": 1, "shop": 1, "shop_pickup": 1, "shop_ship": 1, "shop_ship_cost": 12, "shop_free_over": 12, "shop_note": 300}                                        # every page (login, office, portal, quotes…) wears the business's own look                                   # every page (site, portal, quotes, studio…) opens in the business's language                             # a file id: the face on the chat bubble                                    # the "ask us" chat bubble, on this site and on any site by script tag
_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
# page01 (Tad 2026-11-09 «edit their websites … upload videos, pics … the look and feel»): the page's look, its parts in the order the
# owner wants them, a video link (YouTube / TikTok / Vimeo / Facebook) and an uploaded hero video (a file id, set by its own upload).
FIELDS.update({"look": 12, "sections": 80, "video_url": 300, "hero_video": 36})
LOOKS = ("classic", "bold", "warm", "minimal", "elegant")
SECTIONS = ("about", "services", "work", "video", "contact")
_VIDEO_HOSTS = re.compile(r"^https://(www\.|m\.)?(youtube\.com|youtu\.be|tiktok\.com|vimeo\.com|facebook\.com|fb\.watch)/", re.I)


def sections(site: dict) -> list[str]:
    """The page's parts in the owner's order; parts they never placed follow in the usual order."""
    got = [x for x in (site.get("sections") or "").split(",") if x in SECTIONS]
    return got + [x for x in SECTIONS if x not in got]


def youtube_id(url: str) -> str | None:
    m = re.search(r"(?:youtube\.com/(?:watch\?(?:.*&)?v=|shorts/|embed/|live/)|youtu\.be/)([A-Za-z0-9_-]{11})", url or "")
    return m.group(1) if m else None


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
        if k in ("assistant_photo", "hero_video"):
            import uuid
            try:
                v = str(uuid.UUID(v))
            except ValueError:
                raise Invalid(f"{k}: a file", k) from None
        if k == "look" and v not in LOOKS:
            raise Invalid("look: " + ", ".join(LOOKS), k)
        if k == "sections":
            got = [x for x in v.split(",") if x]
            if any(x not in SECTIONS for x in got) or len(set(got)) != len(got):
                raise Invalid("sections: " + ", ".join(SECTIONS), k)
            v = ",".join(got)
        if k == "video_url" and (not _VIDEO_HOSTS.match(v) or re.search(r"[\s\"'<>\\]", v)):
            raise Invalid("video_url: a YouTube, TikTok, Vimeo or Facebook link", k)
        if k in ("preview", "assistant", "lang_fixed", "brand", "shop", "shop_pickup", "shop_ship") and v != "1":
            raise Invalid(f"{k}: on or off", k)
        if k in ("shop_ship_cost", "shop_free_over") and not re.fullmatch(r"\d{1,9}(\.\d{1,2})?", v.replace(",", "")):
            raise Invalid(f"{k}: an amount like 120 or 99.50", k)          # shop01: the store's shipping, as typed
        if k in ("shop_ship_cost", "shop_free_over"):
            v = v.replace(",", "")
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
        if k == "facebook" and not v.startswith("https://"):     # catch01: «facebook.com/x», «www.fb.com/x» → x (never doubled)
            m = re.search(r"(?:facebook\.com|fb\.com|fb\.me)/(.+)", v, re.I)
            v = "https://facebook.com/" + (m.group(1) if m else v).lstrip("@/")
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


def contact_form(conn: Connection, biz: dict, *, name: str, phone: str = "", email: str = "", message: str = "",
                 source: str = "website", event: str | None = None) -> dict:
    """A visitor wrote from a public page (the website, the directory, a form…): a customer + a conversation in the inbox.
    `source` is where they found the business; it is kept on a new customer and on the event (`event`, else lead.<source>)."""
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
            contact = contacts.patch(conn, biz, source, cid, upd)
    else:
        data = {"name": name, "source": source}
        if e164:
            data["phone"] = e164
        if mail:
            data["email"] = mail
        contact = contacts.create(conn, biz, source, data)
    acc = _webchat_account(conn, biz)
    contacts.link(conn, biz["id"], contact["id"], "webchat", f"web:{contact['id']}")
    from . import inbox as X
    conv = X._conversation(conn, biz["id"], acc["id"], contact["id"])
    now = datetime.now(timezone.utc)
    mid = conn.execute(insert(db.messages).values(business_id=biz["id"], conversation_id=conv["id"], direction="in",
                                                  body=message, status="received").returning(db.messages.c.id)).scalar()
    conn.execute(update(db.conversations).where(db.conversations.c.id == conv["id"]).values(last_inbound_at=now, last_message_at=now,
                                                                                         status="open"))
    log(conn, biz["id"], event or f"lead.{source}", source, contact["id"], {"message_id": str(mid), "source": source})
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


# ---------------------------------------------------------------- page01/ponla01: photos and a video straight onto the page
GALLERY = {"es": "Nuestro trabajo", "en": "Our work"}
PAGE_WORDS = re.compile(r"(mi|su|tu|my|our|the)\s+(p[aá]gina|page|sitio|web|website)|en\s+(la\s+)?(p[aá]gina|web)|on\s+(the\s+)?(page|website|site)"
                        r"|\b(a|para)\s+(la\s+)?(p[aá]gina|web)\b|galer[ií]a|gallery", re.I)


def wants_page(text: str) -> bool:
    """«ponla en mi página», «add it to my page», «súbela a la web» — the owner wants it on their page (not a social post)."""
    return bool(PAGE_WORDS.search(text or ""))


def gallery(conn: Connection, biz: dict, actor: str) -> dict:
    """The page's own gallery project (published, so its public photos show in «Our work»); made on first use."""
    from . import projects as PR
    row = conn.execute(select(db.projects).where(db.projects.c.business_id == biz["id"], db.projects.c.name.in_(list(GALLERY.values())))
                       .order_by(db.projects.c.created_at)).mappings().first()
    if row:
        return dict(row)
    p = PR.create(conn, biz, actor, {"name": GALLERY.get(biz.get("language"), GALLERY["en"])})
    return PR.patch(conn, biz, actor, p["id"], {"published": True})


def add_media(conn: Connection, biz: dict, actor: str, raw: bytes, mime: str, name: str = "") -> tuple[str, dict]:
    """One photo → the page's gallery (public); one video → the page's top video (the old one goes).
    -> ("photo" | "video", the business as it is now). Switches Projects on so «Our work» shows."""
    from uuid import UUID
    from . import files as FL
    tools = dict(biz.get("tools") or {})
    if not tools.get("projects"):
        tools["projects"] = True
        conn.execute(update(db.businesses).where(db.businesses.c.id == biz["id"]).values(tools=tools))
        biz = dict(conn.execute(select(db.businesses).where(db.businesses.c.id == biz["id"])).mappings().one())
    if (mime or "").startswith("video/") or FL._video_ok(raw):
        old = (biz.get("site") or {}).get("hero_video")
        fl = FL.save(conn, biz, actor, raw=raw, mime=mime or "video/mp4", name=name or "video.mp4", kind="site", caption="hero video")
        FL.patch(conn, biz["id"], actor, fl["id"], public=True)
        biz = save(conn, biz, actor, hero_video=str(fl["id"]))
        if old:
            try:
                FL.remove(conn, biz["id"], actor, UUID(old))
            except (NotFound, ValueError):
                pass
        return "video", biz
    g = gallery(conn, biz, actor)
    fl = FL.save(conn, biz, actor, raw=raw, mime=mime, name=name or "photo.jpg", kind="photo", project_id=g["id"])
    FL.patch(conn, biz["id"], actor, fl["id"], public=True)
    return "photo", biz
