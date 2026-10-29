"""'Help me set it up': owners who want someone to set Valeria One up for them, and the helpers who do it.

Owner side: one tap ("Want someone to set it all up for you?"), then a screen that says EXACTLY what would be
shared — their name, the business's name, its city, and what they need — and nothing is shared until they agree
(the consent, its words and its time are kept with the request). They pick one of up to 3 helpers matched by
country, then city, then language, with their ratings; after the work they rate the helper (1–5). No helper yet in
their country: "we'll connect you soon", and the request is kept (shared with nobody) until one is.

Helper side: a public sign-up (/helpers) that the server's admin approves (Server › Helpers) before anyone sees
it; a private dashboard link by email (no password; the link is the key, kept only as a sha256) with the leads
sent to them, to accept or decline.

Valeria One stays free. A helper may charge for their own work — the owner decides. What a lead "costs" is only
RECORDED (a per-lead price the admin sets, 0 by default; each helper's first 5 leads are free): there is no
charging code and no payment provider here.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import func, insert, select, update
from sqlalchemy.engine import Connection

from .. import config, db, packs
from . import Invalid, NotFound, log

H, HL = db.helpers, db.helper_leads
FREE_LEADS = 5
NEEDS = ("whatsapp", "payments", "website", "posts", "booking", "ai", "everything")
MAX_MATCHES = 3


def _hash(token: str) -> str:
    return hashlib.sha256((token or "").encode()).hexdigest()


def dashboard_url(token: str) -> str:
    return f"{config.PUBLIC_URL()}/helpers/d/{token}"


# ---------------------------------------------------------------- helpers sign up; the admin approves
def signup(conn: Connection, *, name: str, email: str, country: str, city: str = "", company: str = "", phone: str = "",
           languages: list[str] | None = None, about: str = "") -> tuple[dict, str]:
    """-> (the helper, their private dashboard token). Not shown to any owner until the admin approves."""
    name, email = " ".join((name or "").split())[:200], (email or "").strip().lower()[:320]
    if not name:
        raise Invalid("write your name", "name")
    if "@" not in email or "." not in email.split("@")[-1] or " " in email:
        raise Invalid("check the email", "email")
    try:
        cc = packs.country((country or "").upper()).code
    except KeyError:
        raise Invalid("pick your country", "country") from None
    if conn.execute(select(H.c.id).where(func.lower(H.c.email) == email)).first():
        raise Invalid("this email already signed up — we sent your link to it", "email")
    from ..web.i18n import UI_LANGS
    langs = sorted({lg for lg in (languages or []) if lg in UI_LANGS})
    token = secrets.token_urlsafe(24)
    row = conn.execute(insert(H).values(name=name, email=email, country=cc, city=" ".join((city or "").split())[:100],
                                        company=" ".join((company or "").split())[:200], phone=(phone or "").strip()[:30],
                                        languages=langs, about=" ".join((about or "").split())[:500], token_hash=_hash(token),
                                        currency=packs.country(cc).currency).returning(H)).mappings().one()
    conn.execute(insert(db.instance_events).values(type="helper.signed_up", actor="public", data={"helper_id": str(row["id"]),
                                                                                                   "country": cc}))
    return dict(row), token


def new_link(conn: Connection, email: str) -> tuple[dict, str] | None:
    """'Email me my link again' (a new token: the old link stops working)."""
    row = conn.execute(select(H).where(func.lower(H.c.email) == (email or "").strip().lower(), H.c.active)).mappings().first()
    if not row:
        return None
    token = secrets.token_urlsafe(24)
    conn.execute(update(H).where(H.c.id == row["id"]).values(token_hash=_hash(token)))
    return dict(row), token


def by_token(conn: Connection, token: str) -> dict:
    row = conn.execute(select(H).where(H.c.token_hash == _hash(token), H.c.active)).mappings().first()
    if not row or not token:
        raise NotFound("helper")
    return dict(row)


def all_(conn: Connection) -> list[dict]:
    rows = conn.execute(select(H, func.count(HL.c.id).label("leads")).join(HL, HL.c.helper_id == H.c.id, isouter=True)
                        .group_by(H.c.id).order_by(H.c.approved_at.is_(None).desc(), H.c.created_at.desc())).mappings()
    return [dict(r) for r in rows]


def approve(conn: Connection, helper_id: UUID, actor: str, on: bool = True) -> dict:
    if not conn.execute(update(H).where(H.c.id == helper_id).values(
            approved_at=func.now() if on else None, approved_by=actor if on else None)).rowcount:
        raise NotFound("helper")
    conn.execute(insert(db.instance_events).values(type="helper.approved" if on else "helper.unapproved", actor=actor,
                                                   data={"helper_id": str(helper_id)}))
    return dict(conn.execute(select(H).where(H.c.id == helper_id)).mappings().one())


def set_price(conn: Connection, helper_id: UUID, actor: str, price_minor: int) -> None:
    """What a lead would cost this helper — RECORDED ONLY (nothing is charged anywhere)."""
    if not isinstance(price_minor, int) or price_minor < 0:
        raise Invalid("a price of 0 or more", "price")
    if not conn.execute(update(H).where(H.c.id == helper_id).values(lead_price_minor=price_minor)).rowcount:
        raise NotFound("helper")
    conn.execute(insert(db.instance_events).values(type="helper.price", actor=actor, data={"helper_id": str(helper_id),
                                                                                            "price_minor": price_minor}))


def rating(conn: Connection, helper_id: UUID) -> dict:
    r = conn.execute(select(func.avg(HL.c.rating), func.count(HL.c.rating)).where(HL.c.helper_id == helper_id, HL.c.rating.isnot(None))).one()
    return {"stars": round(float(r[0]), 1) if r[0] is not None else None, "count": r[1]}


# ---------------------------------------------------------------- owners: matched helpers, consent, the request
def matches(conn: Connection, biz: dict, lang: str | None = None, city: str = "") -> list[dict]:
    """Up to 3 approved helpers in the business's country: same city first, then the owner's language, then rating."""
    rows = [dict(r) for r in conn.execute(select(H).where(H.c.active, H.c.approved_at.isnot(None), H.c.country == biz["country"]))
            .mappings()]
    city = (city or "").strip().lower()
    for r in rows:
        r["rating"] = rating(conn, r["id"])
    rows.sort(key=lambda r: (not (city and r["city"].lower() == city), not (lang and lang in (r["languages"] or [])),
                             -(r["rating"]["stars"] or 0), -r["rating"]["count"]))
    return rows[:MAX_MATCHES]


def consent_text(lang: str) -> str:
    from ..web.i18n import t
    return t("hp_consent", lang)


def request(conn: Connection, biz: dict, actor: str, *, consent: bool, name: str, city: str, needs: list[str], note: str = "",
            helper_id: UUID | None = None, lang: str = "en", now: datetime | None = None) -> dict:
    """The owner agreed to share (and picked a helper, or there is none yet). Nothing without the consent."""
    if not consent:
        raise Invalid("tick that you agree to share these details", "consent")
    now = now or datetime.now(timezone.utc)
    shared = {"name": " ".join((name or "").split())[:200], "business": biz["name"], "city": " ".join((city or "").split())[:100],
              "needs": [n for n in (needs or []) if n in NEEDS] or ["everything"], "note": " ".join((note or "").split())[:500],
              "country": biz["country"], "language": lang}
    helper = None
    if helper_id:
        helper = conn.execute(select(H).where(H.c.id == helper_id, H.c.active, H.c.approved_at.isnot(None),
                                              H.c.country == biz["country"])).mappings().first()
        if not helper:
            raise Invalid("that helper isn't available — pick another", "helper")
    free, price, cur = True, 0, (helper or {}).get("currency") or "USD"
    if helper:
        before = conn.execute(select(func.count()).select_from(HL).where(HL.c.helper_id == helper["id"])).scalar()
        free = before < FREE_LEADS
        price = 0 if free else int(helper["lead_price_minor"] or 0)
    row = conn.execute(insert(HL).values(business_id=biz["id"], helper_id=helper["id"] if helper else None,
                                         status="sent" if helper else "waiting", shared=shared, consent_text=consent_text(lang),
                                         consent_at=now, consent_by=actor, free=free, price_minor=price, currency=cur)
                       .returning(HL)).mappings().one()
    log(conn, biz["id"], "helper.requested", actor, row["id"], {"helper": bool(helper)})
    if helper:
        from . import mailer
        from ..web.i18n import t, ui_lang
        hl = ui_lang((helper["languages"] or ["en"])[0])
        mailer.send(helper["email"], t("hp_mail_subject", hl).format(business=biz["name"]),
                    t("hp_mail_body", hl).format(name=shared["name"], business=biz["name"], city=shared["city"] or "—",
                                                 needs=", ".join(shared["needs"]), note=shared["note"] or "—"))
    return dict(row)


def leads_of_business(conn: Connection, business_id: UUID) -> list[dict]:
    rows = conn.execute(select(HL, H.c.name.label("helper_name"), H.c.company.label("helper_company"),
                               H.c.email.label("helper_email"), H.c.phone.label("helper_phone"))
                        .join(H, H.c.id == HL.c.helper_id, isouter=True).where(HL.c.business_id == business_id)
                        .order_by(HL.c.created_at.desc())).mappings()
    return [dict(r) for r in rows]


def leads_of_helper(conn: Connection, helper_id: UUID) -> list[dict]:
    """What a helper sees: only what each owner agreed to share."""
    rows = conn.execute(select(HL.c.id, HL.c.status, HL.c.shared, HL.c.created_at, HL.c.free, HL.c.price_minor, HL.c.currency,
                               HL.c.rating).where(HL.c.helper_id == helper_id).order_by(HL.c.created_at.desc())).mappings()
    return [dict(r) for r in rows]


def decide(conn: Connection, helper: dict, lead_id: UUID, accept: bool) -> dict:
    row = conn.execute(select(HL).where(HL.c.id == lead_id, HL.c.helper_id == helper["id"])).mappings().first()
    if not row:
        raise NotFound("lead")
    if row["status"] != "sent":
        raise Invalid("already answered", "status")
    conn.execute(update(HL).where(HL.c.id == lead_id).values(status="accepted" if accept else "declined", decided_at=func.now()))
    log(conn, row["business_id"], "helper.accepted" if accept else "helper.declined", f"helper:{helper['id']}", lead_id)
    return dict(conn.execute(select(HL).where(HL.c.id == lead_id)).mappings().one())


def rate(conn: Connection, business_id: UUID, actor: str, lead_id: UUID, stars: int) -> dict:
    row = conn.execute(select(HL).where(HL.c.business_id == business_id, HL.c.id == lead_id)).mappings().first()
    if not row:
        raise NotFound("lead")
    if row["status"] not in ("accepted", "done"):
        raise Invalid("you can rate a helper after they took your request", "rating")
    if not isinstance(stars, int) or not 1 <= stars <= 5:
        raise Invalid("1 to 5 stars", "rating")
    conn.execute(update(HL).where(HL.c.id == lead_id).values(rating=stars, rated_at=func.now(), status="done"))
    log(conn, business_id, "helper.rated", actor, lead_id, {"stars": stars})
    return dict(conn.execute(select(HL).where(HL.c.id == lead_id)).mappings().one())


def recorded(conn: Connection) -> dict:
    """For the admin: leads and what they would have cost — recorded, never charged."""
    r = conn.execute(select(func.count(), func.coalesce(func.sum(HL.c.price_minor), 0),
                            func.count().filter(HL.c.helper_id.is_(None))).select_from(HL)).one()
    return {"leads": r[0], "recorded_minor": int(r[1]), "waiting": r[2]}
