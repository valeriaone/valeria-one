"""realty01 (Tad 2026-11-16 «every realtor gets their website in valeriaone and the property listed there and also pushed to
valeria.valai.mx»): a realtor's properties on their Valeria One site.

One source: the realtor keeps their properties on their portal (valeria.valai.mx — EasyBroker import, feed, Drive, by hand);
the portal sends the whole active set here (partner API). `replace` makes this business's list exactly that set: new ones
added, changed ones updated, ones no longer sent removed (the portal still keeps them). Nothing else writes here.
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection

from .. import db
from . import Invalid, log

MAX = 500
PHOTOS = 12
OPS = ("sale", "rent")


def _int(v, field: str) -> int | None:
    if v in (None, ""):
        return None
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise Invalid(f"{field} must be a whole number") from None
    if n < 0:
        raise Invalid(f"{field} can't be negative")
    return n


def _row(it: dict, pos: int) -> dict:
    ref = str(it.get("ref") or "").strip()[:80]
    title = str(it.get("title") or "").strip()[:300]
    if not ref or not title:
        raise Invalid("each listing needs ref and title")
    op = str(it.get("operation") or "sale").strip()
    if op not in OPS:
        raise Invalid("operation must be sale or rent")
    photos = [str(p).strip()[:500] for p in (it.get("photos") or []) if str(p).strip().startswith("https://")][:PHOTOS]
    url = str(it.get("url") or "").strip()[:500]
    return {"ext_ref": ref, "title": title, "operation": op, "kind": str(it.get("kind") or "").strip()[:40],
            "price_minor": _int(it.get("price_minor"), "price_minor"),
            "currency": (str(it.get("currency") or "MXN").strip().upper() or "MXN")[:3],
            "beds": _int(it.get("beds"), "beds"), "baths": str(it.get("baths") or "").strip()[:6],
            "area_m2": _int(it.get("area_m2"), "area_m2"), "lot_m2": _int(it.get("lot_m2"), "lot_m2"),
            "place": str(it.get("place") or "").strip()[:200], "photos": photos,
            "url": url if url.startswith("https://") else "", "position": pos}


def replace(conn: Connection, actor: str, business_id: UUID, items: list[dict]) -> dict:
    """This business's properties become exactly `items` (in that order). Returns counts."""
    if len(items) > MAX:
        raise Invalid(f"at most {MAX} listings")
    rows = [_row(it, i) for i, it in enumerate(items)]
    refs = [r["ext_ref"] for r in rows]
    if len(set(refs)) != len(refs):
        raise Invalid("each ref only once")
    t = db.listings
    before = {r.ext_ref for r in conn.execute(select(t.c.ext_ref).where(t.c.business_id == business_id))}
    now = datetime.now(timezone.utc)
    for r in rows:
        stmt = pg_insert(t).values(business_id=business_id, **r)
        conn.execute(stmt.on_conflict_do_update(constraint="uq_listings_ref",
                                                set_={**{k: stmt.excluded[k] for k in r if k != "ext_ref"}, "updated_at": now}))
    gone = before - set(refs)
    if gone:
        conn.execute(delete(t).where(t.c.business_id == business_id, t.c.ext_ref.in_(gone)))
    out = {"listings": len(rows), "added": len(set(refs) - before), "removed": len(gone)}
    log(conn, business_id, "listings.replaced", actor, data=out)
    return out


def public(conn: Connection, business_id: UUID) -> list[dict]:
    t = db.listings
    return [dict(r) for r in conn.execute(select(t).where(t.c.business_id == business_id)
                                          .order_by(t.c.position, t.c.created_at)).mappings()]
