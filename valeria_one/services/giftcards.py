"""gift01 (v0.55.0): gift cards (Tad 2026-11-08 «close all gaps air tight»).

The owner sells one: the amount, who it's for, a message. Paid at the counter (active at once) or by the business's OWN pay
link (active the moment the link is paid — payment.paid). The recipient gets it on WhatsApp / SMS / email from the business's
own accounts: a page /g/<code> with the card, its balance and the message. At the counter the owner types the code and how
much is used: the balance goes down atomically (two counters can never spend the same balance), each use is kept. A card can
expire (optional, months) and be voided (never deleted). Money only ever moves through the business's own accounts.
"""
from __future__ import annotations

import secrets
from datetime import date, datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import func, insert, select, update
from sqlalchemy.engine import Connection

from .. import config, db
from . import Invalid, NotFound, log

G, U = db.gift_cards, db.gift_card_uses
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"                 # no 0/O, 1/I: easy to read aloud and type


def _code(conn: Connection) -> str:
    while True:
        c = "".join(secrets.choice(ALPHABET) for _ in range(10))
        if not conn.execute(select(G.c.id).where(G.c.code == c)).first():
            return c


def url(card: dict) -> str:
    return f"{config.PUBLIC_URL()}/g/{card['code']}"


def pretty(code: str) -> str:
    return f"{code[:5]}-{code[5:]}"


def create(biz: dict, actor: str, *, amount_minor: int, recipient_name: str = "", recipient_phone: str = "", recipient_email: str = "",
           message: str = "", buyer_contact_id: UUID | None = None, paid_now: bool = True, expires_months: int | None = None,
           http=None) -> dict:
    if not isinstance(amount_minor, int) or isinstance(amount_minor, bool) or amount_minor <= 0:
        raise Invalid("gc_bad_amount", "amount")
    exp = None
    if expires_months:
        if not 1 <= int(expires_months) <= 60:
            raise Invalid("gc_bad_expiry", "expires_months")
        exp = (datetime.now(timezone.utc) + timedelta(days=int(expires_months) * 30)).date()
    vals = {"business_id": biz["id"], "amount_minor": amount_minor, "balance_minor": amount_minor, "currency": biz["currency"],
            "buyer_contact_id": buyer_contact_id, "recipient_name": " ".join((recipient_name or "").split())[:200],
            "recipient_phone": (recipient_phone or "").strip()[:40], "recipient_email": (recipient_email or "").strip().lower()[:320],
            "message": (message or "").strip()[:500], "expires_on": exp}
    pay_id = None
    if not paid_now:
        from . import payments as PM
        pay = PM.create(biz, actor, amount_minor=amount_minor, description=f"Tarjeta de regalo · {biz['name']}"[:300],
                        contact_id=buyer_contact_id, http=http)
        pay_id = pay["id"]
    with db.tx() as conn:
        row = conn.execute(insert(G).values(**vals, code=_code(conn), status="active" if paid_now else "pending",
                                            paid_at=func.now() if paid_now else None, payment_id=pay_id).returning(G)).mappings().one()
        log(conn, biz["id"], "giftcard.created", actor, row["id"], {"amount_minor": amount_minor, "paid": paid_now})
    return dict(row)


def on_event(conn: Connection, business_id: UUID, type_: str, subject_id: UUID | None, data: dict) -> None:
    """Its pay link was paid: the card is active."""
    if type_ != "payment.paid" or not subject_id:
        return
    for (gid,) in conn.execute(update(G).where(G.c.business_id == business_id, G.c.payment_id == subject_id, G.c.status == "pending")
                               .values(status="active", paid_at=func.now(), updated_at=func.now()).returning(G.c.id)).all():
        log(conn, business_id, "giftcard.paid", "payment", gid, {})


def get(conn: Connection, business_id: UUID, card_id: UUID) -> dict:
    r = conn.execute(select(G).where(G.c.business_id == business_id, G.c.id == card_id)).mappings().first()
    if not r:
        raise NotFound("gift card")
    return dict(r)


def by_code(conn: Connection, code: str) -> dict | None:
    c = (code or "").upper().replace("-", "").replace(" ", "")[:16]
    r = conn.execute(select(G).where(G.c.code == c)).mappings().first() if c else None
    return dict(r) if r else None


def list_(conn: Connection, business_id: UUID, limit: int = 200) -> list[dict]:
    return [dict(r) for r in conn.execute(select(G).where(G.c.business_id == business_id).order_by(G.c.created_at.desc()).limit(limit)).mappings()]


def uses(conn: Connection, business_id: UUID, card_id: UUID) -> list[dict]:
    return [dict(r) for r in conn.execute(select(U).where(U.c.business_id == business_id, U.c.gift_card_id == card_id)
                                          .order_by(U.c.created_at)).mappings()]


def redeem(conn: Connection, biz: dict, actor: str, code: str, amount_minor: int, note: str = "", today: date | None = None) -> dict:
    """Use part (or all) of a card at the counter. Atomic: never below zero, never twice the same balance."""
    if not isinstance(amount_minor, int) or isinstance(amount_minor, bool) or amount_minor <= 0:
        raise Invalid("gc_bad_amount", "amount")
    card = by_code(conn, code)
    if not card or card["business_id"] != biz["id"]:
        raise Invalid("gc_not_found", "code")
    if card["status"] == "pending":
        raise Invalid("gc_not_paid", "code")
    if card["status"] == "void":
        raise Invalid("gc_void", "code")
    today = today or datetime.now(timezone.utc).date()
    if card["expires_on"] and card["expires_on"] < today:
        raise Invalid("gc_expired", "code")
    r = conn.execute(update(G).where(G.c.id == card["id"], G.c.status == "active", G.c.balance_minor >= amount_minor)
                     .values(balance_minor=G.c.balance_minor - amount_minor, updated_at=func.now()).returning(G)).mappings().first()
    if not r:
        raise Invalid("gc_not_enough", "amount")
    conn.execute(insert(U).values(business_id=biz["id"], gift_card_id=card["id"], amount_minor=amount_minor, note=(note or "")[:200], by=actor[:100]))
    log(conn, biz["id"], "giftcard.used", actor, card["id"], {"amount_minor": amount_minor, "left": r["balance_minor"]})
    return dict(r)


def void(conn: Connection, biz: dict, actor: str, card_id: UUID) -> None:
    get(conn, biz["id"], card_id)
    conn.execute(update(G).where(G.c.id == card_id).values(status="void", updated_at=func.now()))
    log(conn, biz["id"], "giftcard.void", actor, card_id, {})


def send(biz: dict, actor: str, card_id: UUID, http=None) -> dict:
    """To the recipient, from the business's own account (their phone first, else their email). -> the message."""
    from . import contacts as C
    from . import inbox as X
    from ..web.i18n import t, ui_lang
    with db.tx() as conn:
        card = get(conn, biz["id"], card_id)
        if card["status"] != "active":
            raise Invalid("gc_not_paid", "card")
        if not (card["recipient_phone"] or card["recipient_email"]):
            raise Invalid("gc_need_recipient", "recipient")
        name = card["recipient_name"] or card["recipient_phone"] or card["recipient_email"]
        if card["recipient_phone"]:                               # the same person by their phone, never a duplicate
            from .. import packs, phones
            try:
                ph = phones.e164(card["recipient_phone"], packs.country(biz["country"]))
            except phones.BadPhone:
                raise Invalid("gc_bad_phone", "recipient_phone") from None
            contact, _ = C.find_or_create_by_identity(conn, biz, "sms", ph, name, "gift card")
        else:
            row = conn.execute(select(db.contacts).where(db.contacts.c.business_id == biz["id"], db.contacts.c.deleted_at.is_(None),
                                                         func.lower(db.contacts.c.email) == card["recipient_email"])).mappings().first()
            contact = dict(row) if row else C.create(conn, biz, actor, {"name": name, "email": card["recipient_email"], "source": "gift card"})
    lang = ui_lang(biz["language"])
    body = t("gc_send_text", lang).format(name=(card["recipient_name"] or "").split(" ")[0] or "👋", business=biz["name"],
                                          amount=_money(biz, card["amount_minor"]), link=url(card))
    if card["message"]:
        body = f"{body}\n\n«{card['message']}»"
    m = X.send(biz, actor, contact_id=contact["id"], body=body, kind="service", http=http)
    with db.tx() as conn:
        conn.execute(update(G).where(G.c.id == card_id).values(sent_at=func.now()))
        log(conn, biz["id"], "giftcard.sent", actor, card_id, {"status": m.get("status")})
    return m


def _money(biz: dict, minor: int) -> str:
    from .. import money
    return f"${money.to_major(minor, biz['currency'])} {biz['currency']}"
