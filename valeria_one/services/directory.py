"""The public directory: "find a business near you" (/directory, one page per business at /directory/<slug>).

Every business is OFF the directory until its owner lists it (Website › Directory). The owner writes what it is,
the city and one or two lines; everything else comes from what the business already keeps here: opening hours,
its website and booking page when they are on, its rating from the Google reviews it connected. Nothing personal
is shown unless the owner ticks it (phone, WhatsApp, address). A visitor who writes from the listing becomes a
customer in the business's inbox, with "directory" as where they came from.

Sandbox businesses (and every business on a sandbox server) are never listed; a business paused by its host
disappears from it while paused.
"""
from __future__ import annotations

import re
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection

from .. import blocks, config, db, packs
from . import Invalid, NotFound, log

DL, B = db.directory_listings, db.businesses
DEFAULTS = {"listed": False, "category": "", "city": "", "area": "", "summary": "", "show_hours": True, "show_phone": False,
            "show_whatsapp": False, "show_address": False}
TEXTS = {"category": 60, "city": 100, "area": 100, "summary": 300}
SHOWS = ("show_hours", "show_phone", "show_whatsapp", "show_address")
PER_PAGE = 24


def settings(conn: Connection, business_id: UUID) -> dict:
    row = conn.execute(select(DL).where(DL.c.business_id == business_id)).mappings().first()
    return {**DEFAULTS, **(dict(row) if row else {}), "business_id": business_id}


def save(conn: Connection, biz: dict, actor: str, *, listed: bool | None = None, **fields) -> dict:
    """The owner's listing. Listing it needs a city (so people nearby find it) and gives the business its address
    (/directory/<slug>) if it has none yet."""
    vals: dict = {}
    for k, n in TEXTS.items():
        if fields.get(k) is not None:
            v = " ".join(str(fields[k]).split())
            if len(v) > n:
                raise Invalid(f"{k}: at most {n} characters", k)
            if re.search(r"[<>]", v):
                raise Invalid(f"{k}: plain words only", k)
            vals[k] = v
    for k in SHOWS:
        if fields.get(k) is not None:
            vals[k] = bool(fields[k])
    if listed is not None:
        vals["listed"] = bool(listed)
    cur = {**settings(conn, biz["id"]), **vals}
    if cur["listed"] and not cur["city"]:
        raise Invalid("write your city first, so people nearby find you", "city")
    if not vals:
        return cur
    conn.execute(pg_insert(DL).values(business_id=biz["id"], **vals)
                 .on_conflict_do_update(index_elements=[DL.c.business_id], set_={**vals, "updated_at": func.now()}))
    if cur["listed"] and not biz.get("slug"):
        from . import booking as BK
        BK.save_settings(conn, biz, actor, slug=BK._free_slug(conn, biz["name"]))
    log(conn, biz["id"], "directory.updated", actor, biz["id"], {"listed": cur["listed"], "fields": sorted(vals)})
    return settings(conn, biz["id"])


def visible() -> bool:
    """A sandbox server lists nobody: its businesses are all practice ones."""
    return not config.SANDBOX()


def _listed():
    """Listed by its owner, real (not sandbox), not paused, with an address."""
    return (DL.c.listed.is_(True) & B.c.sandbox.is_(False) & B.c.suspended_at.is_(None) & B.c.slug.isnot(None))


def search(conn: Connection, *, q: str = "", kind: str = "", city: str = "", country: str = "", page: int = 1) -> dict:
    """Listed businesses matching the words (name, what it is, the lines it wrote), the type (industry), the city
    and the country. Newest listings last, so early ones keep their place."""
    if not visible():
        return {"rows": [], "count": 0, "page": 1, "pages": 1}
    w = [_listed()]
    q = " ".join((q or "").split())[:100]
    if q:
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"     # a visitor's % or _ is just a letter
        w.append(or_(*(c.ilike(like, escape="\\") for c in (B.c.name, DL.c.category, DL.c.summary, DL.c.area))))
    if kind and kind in packs.industries():
        w.append(B.c.industry == kind)
    if city:
        w.append(func.lower(DL.c.city) == " ".join(city.split()).lower()[:100])
    if country and re.fullmatch(r"[A-Za-z]{2}", country):
        w.append(B.c.country == country.upper())
    base = select(B, DL.c.category, DL.c.city, DL.c.area, DL.c.summary, DL.c.show_hours).join(DL, DL.c.business_id == B.c.id).where(*w)
    count = conn.execute(select(func.count()).select_from(base.subquery())).scalar()
    pages = max(1, -(-count // PER_PAGE))
    page = min(max(1, int(page or 1)), pages)
    rows = [dict(r) for r in conn.execute(base.order_by(DL.c.created_at, B.c.name).limit(PER_PAGE).offset((page - 1) * PER_PAGE)).mappings()]
    stars = ratings(conn, [r["id"] for r in rows])
    for r in rows:
        r["rating"] = stars.get(r["id"])
    return {"rows": rows, "count": count, "page": page, "pages": pages}


def facets(conn: Connection) -> dict:
    """What the filters offer: the countries, cities and types that have someone listed."""
    if not visible():
        return {"countries": [], "cities": [], "kinds": []}
    j = DL.join(B, B.c.id == DL.c.business_id)
    countries = [r[0] for r in conn.execute(select(B.c.country).select_from(j).where(_listed()).distinct().order_by(B.c.country))]
    cities = [r[0] for r in conn.execute(select(DL.c.city).select_from(j).where(_listed()).distinct().order_by(DL.c.city).limit(300))]
    kinds = [r[0] for r in conn.execute(select(B.c.industry).select_from(j).where(_listed()).distinct().order_by(B.c.industry))]
    return {"countries": countries, "cities": cities, "kinds": kinds}


def public(conn: Connection, slug: str) -> dict:
    """One listing: the business with its listing fields. NotFound unless it is listed right now."""
    if not visible():
        raise NotFound("listing")
    row = conn.execute(select(B, DL.c.category, DL.c.city, DL.c.area, DL.c.summary, *[DL.c[k] for k in SHOWS])
                       .join(DL, DL.c.business_id == B.c.id).where(_listed(), B.c.slug == (slug or "").lower()[:60])).mappings().first()
    if not row:
        raise NotFound("listing")
    d = dict(row)
    d["rating"] = ratings(conn, [d["id"]]).get(d["id"])
    return d


def ratings(conn: Connection, business_ids: list) -> dict:
    """{business_id: {"avg": 4.6, "count": 12}} from the reviews the business brought in (Google)."""
    if not business_ids:
        return {}
    R = db.reviews
    rows = conn.execute(select(R.c.business_id, func.avg(R.c.rating), func.count()).where(
        R.c.business_id.in_(business_ids), R.c.rating.isnot(None)).group_by(R.c.business_id)).all()
    return {r[0]: {"avg": round(float(r[1]), 1), "count": r[2]} for r in rows if r[2]}


def links(biz: dict) -> dict:
    """Where the listing's buttons go: its website, its booking page and WhatsApp — only what is on and chosen."""
    base = config.PUBLIC_URL()
    s = biz.get("site") or {}
    return {"site": f"{base}/s/{biz['slug']}" if biz.get("site_enabled") and blocks.on(biz, "website") else None,
            "book": f"{base}/b/{biz['slug']}" if biz.get("booking_enabled") and blocks.on(biz, "calendar") else None,
            "whatsapp": f"https://wa.me/{s['whatsapp'].lstrip('+')}" if biz.get("show_whatsapp") and s.get("whatsapp") else None,
            "phone": s.get("phone") if biz.get("show_phone") else None,
            "address": s.get("address") if biz.get("show_address") else None,
            "url": f"{base}/directory/{biz['slug']}"}


def category_of(biz: dict, lang: str) -> str:
    return biz.get("category") or packs.text(packs.industry(biz["industry"]).name, lang)


def json_ld(biz: dict, lang: str) -> dict:
    """schema.org LocalBusiness for the listing page (with its rating when it has reviews)."""
    ln = links(biz)
    d = {"@context": "https://schema.org", "@type": "LocalBusiness", "name": biz["name"], "url": ln["url"],
         "description": (biz.get("summary") or category_of(biz, lang))[:300],
         "address": {"@type": "PostalAddress", "addressLocality": biz.get("city") or "", "addressCountry": biz["country"],
                     **({"addressRegion": biz["area"]} if biz.get("area") else {}),
                     **({"streetAddress": ln["address"]} if ln["address"] else {})}}
    if ln["phone"]:
        d["telephone"] = ln["phone"]
    if ln["site"]:
        d["sameAs"] = [ln["site"]]
    days = {"mon": "Mo", "tue": "Tu", "wed": "We", "thu": "Th", "fri": "Fr", "sat": "Sa", "sun": "Su"}
    hrs = [f"{days[k]} {a}-{b}" for k, v in (biz.get("hours") or {}).items() if k in days for a, b in v]
    if hrs and biz.get("show_hours", True):
        d["openingHours"] = hrs
    if biz.get("rating"):
        d["aggregateRating"] = {"@type": "AggregateRating", "ratingValue": biz["rating"]["avg"], "reviewCount": biz["rating"]["count"],
                                "bestRating": 5, "worstRating": 1}
    return d


def sitemap_paths(conn: Connection, limit: int = 5000) -> list[str]:
    if not visible():
        return []
    rows = conn.execute(select(B.c.slug).join(DL, DL.c.business_id == B.c.id).where(_listed()).order_by(DL.c.created_at).limit(limit))
    return ["/directory"] + [f"/directory/{r[0]}" for r in rows]


def contact(conn: Connection, biz: dict, *, name: str, reach: str, message: str) -> dict:
    """A visitor wrote from the listing: a customer (source "directory") and a conversation in the inbox."""
    from . import site as SI
    reach = (reach or "").strip()
    return SI.contact_form(conn, biz, name=name, message=message, email=reach if "@" in reach else "",
                           phone="" if "@" in reach else reach, source="directory")
