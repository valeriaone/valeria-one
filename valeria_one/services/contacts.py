"""Customers (contacts) and how to reach them on each channel."""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import Text, cast, func, insert, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection

from .. import db, packs, phones
from . import Invalid, NotFound, log

C = db.contacts
FIELDS = ("name", "email", "phone", "language", "source", "tags", "notes")


def _clean(biz: dict, d: dict) -> dict:
    out = {k: d[k] for k in FIELDS if k in d}
    if "phone" in out:
        out["phone"] = phones.e164(out["phone"], packs.country(biz["country"])) if out["phone"] else None
    if "email" in out:
        e = (out["email"] or "").strip().lower() or None
        if e and ("@" not in e or "." not in e.split("@")[-1] or " " in e):
            raise Invalid("not an email address", "email")
        out["email"] = e
    if "name" in out:
        out["name"] = (out["name"] or "").strip()[:200]
    if "tags" in out:
        out["tags"] = sorted({str(t).strip().lower()[:60] for t in (out["tags"] or []) if str(t).strip()})
    return out


def _assignee(conn: Connection, business_id: UUID, user_id) -> UUID | None:
    """Who on the team looks after this customer: someone of this business, or nobody."""
    if not user_id:
        return None
    try:
        uid = UUID(str(user_id))
    except ValueError:
        raise Invalid("that person isn't on the team", "assignee_id") from None
    if not conn.execute(select(db.memberships.c.user_id).where(db.memberships.c.business_id == business_id,
                                                               db.memberships.c.user_id == uid)).scalar():
        raise Invalid("that person isn't on the team", "assignee_id")
    return uid


def create(conn: Connection, biz: dict, actor: str, data: dict) -> dict:
    from . import fields as CF
    try:
        vals = _clean(biz, data)
    except phones.BadPhone as e:
        raise Invalid(f"phone: {e}", "phone") from None
    if not (vals.get("name") or vals.get("phone") or vals.get("email")):
        raise Invalid("give at least a name, a phone or an email")
    changed = {}
    if data.get("custom"):
        vals["custom"], changed = CF.merge(conn, biz, "contact", {}, data["custom"])
    if data.get("assignee_id"):
        vals["assignee_id"] = _assignee(conn, biz["id"], data["assignee_id"])
    if data.get("marketing_consent"):
        src = (data.get("marketing_consent_source") or "").strip()
        if not src:
            raise Invalid("say how they agreed (marketing_consent_source)", "marketing_consent_source")
        vals.update(marketing_consent=True, marketing_consent_at=datetime.now(timezone.utc), marketing_consent_source=src[:200])
    row = conn.execute(insert(C).values(business_id=biz["id"], **vals).returning(C)).mappings().one()
    _sync_identities(conn, biz["id"], row)
    log(conn, biz["id"], "contact.created", actor, row["id"])
    CF.log_changes(conn, biz["id"], actor, "contact", row["id"], changed)
    return get(conn, biz["id"], row["id"])


PHONE_CHANNELS = ("whatsapp", "sms", "whatsapp_twilio")      # channels whose address IS the phone number


def _sync_identities(conn: Connection, business_id: UUID, row) -> None:
    """A phone reaches the contact on every phone channel (WhatsApp, SMS, WhatsApp through Twilio); an email on email."""
    pairs = []
    if row["phone"]:
        pairs += [(ch, row["phone"]) for ch in PHONE_CHANNELS]
    if row["email"]:
        pairs.append(("email", row["email"]))
    for ch, addr in pairs:
        link(conn, business_id, row["id"], ch, addr)


def link(conn: Connection, business_id: UUID, contact_id: UUID, channel: str, address: str) -> None:
    conn.execute(pg_insert(db.identities).values(business_id=business_id, contact_id=contact_id, channel=channel,
                                                 address=address).on_conflict_do_nothing(constraint="uq_identity"))


def get(conn: Connection, business_id: UUID, contact_id: UUID) -> dict:
    row = conn.execute(select(C).where(C.c.business_id == business_id, C.c.id == contact_id,
                                       C.c.deleted_at.is_(None))).mappings().first()
    if not row:
        raise NotFound("contact")
    return dict(row)


def identities(conn: Connection, business_id: UUID, contact_id: UUID) -> list[dict]:
    I = db.identities
    return [dict(r) for r in conn.execute(select(I.c.channel, I.c.address).where(
        I.c.business_id == business_id, I.c.contact_id == contact_id).order_by(I.c.channel)).mappings()]


def field_filter(conn: Connection, business_id: UUID, T, obj: str, field: str | None, value: str | None):
    """`WHERE` for "only the ones whose <field> is <value>" (the lists' filter and the API's field= & value=)."""
    if not field:
        return None
    from . import fields as CF
    f = next((x for x in CF.list_(conn, business_id, obj) if field in (x["key"], x["label"])), None)
    if not f:
        raise Invalid(f"no field called {field!r}", "field")
    v = (value or "").strip()
    if not v:
        return T.c.custom.has_key(f["key"])
    if f["kind"] == "yesno":
        yes = v.lower() in ("yes", "true", "1")
        return T.c.custom[f["key"]].astext == ("true" if yes else "false")
    if f["kind"] in ("text", "link"):
        return T.c.custom[f["key"]].astext.ilike(f"%{v}%")
    return func.lower(T.c.custom[f["key"]].astext) == v.lower()


def search(conn: Connection, business_id: UUID, *, q: str | None = None, tag: str | None = None,
           limit: int = 50, after: datetime | None = None, field: str | None = None, value: str | None = None,
           assignee_id: UUID | None = None) -> list[dict]:
    s = select(C).where(C.c.business_id == business_id, C.c.deleted_at.is_(None))
    if q:
        like = f"%{q.strip()}%"
        s = s.where(or_(C.c.name.ilike(like), C.c.email.ilike(like), C.c.phone.ilike(like), cast(C.c.custom, Text).ilike(like)))
    w = field_filter(conn, business_id, C, "contact", field, value)
    if w is not None:
        s = s.where(w)
    if assignee_id:
        s = s.where(C.c.assignee_id == assignee_id)
    if tag:
        s = s.where(C.c.tags.any(tag.strip().lower()))
    if after:
        s = s.where(C.c.created_at < after)
    s = s.order_by(C.c.created_at.desc()).limit(max(1, min(limit, 200)))
    return [dict(r) for r in conn.execute(s).mappings()]


def patch(conn: Connection, biz: dict, actor: str, contact_id: UUID, data: dict) -> dict:
    from . import fields as CF
    cur = get(conn, biz["id"], contact_id)
    try:
        vals = _clean(biz, data)
    except phones.BadPhone as e:
        raise Invalid(f"phone: {e}", "phone") from None
    changed = {}
    if "custom" in data:
        new, changed = CF.merge(conn, biz, "contact", cur["custom"], data["custom"])
        if changed:
            vals["custom"] = new
    if "assignee_id" in data:
        who = _assignee(conn, biz["id"], data["assignee_id"])
        if who != cur["assignee_id"]:
            vals["assignee_id"] = who
    if "marketing_consent" in data:
        if data["marketing_consent"]:
            src = (data.get("marketing_consent_source") or "").strip()
            if not src:
                raise Invalid("say how they agreed (marketing_consent_source)", "marketing_consent_source")
            vals.update(marketing_consent=True, marketing_consent_at=datetime.now(timezone.utc),
                        marketing_consent_source=src[:200], unsubscribed_at=None)
        else:
            vals.update(marketing_consent=False)
    if vals:
        row = conn.execute(update(C).where(C.c.business_id == biz["id"], C.c.id == contact_id)
                           .values(**vals, updated_at=func.now()).returning(C)).mappings().one()
        _sync_identities(conn, biz["id"], row)
        log(conn, biz["id"], "contact.updated", actor, contact_id, {"fields": sorted(vals)})
        CF.log_changes(conn, biz["id"], actor, "contact", contact_id, changed)
    return get(conn, biz["id"], contact_id)


def delete(conn: Connection, business_id: UUID, actor: str, contact_id: UUID) -> None:
    """Privacy-law erasure: personal fields are wiped, the row stays only as an anonymous shell for the audit trail."""
    get(conn, business_id, contact_id)
    conn.execute(update(C).where(C.c.business_id == business_id, C.c.id == contact_id).values(
        name="", email=None, phone=None, notes="", tags=[], custom={}, marketing_consent=False, deleted_at=func.now(),
        updated_at=func.now()))
    conn.execute(db.identities.delete().where(db.identities.c.business_id == business_id,
                                              db.identities.c.contact_id == contact_id))
    conn.execute(update(db.messages).where(db.messages.c.business_id == business_id, db.messages.c.conversation_id.in_(
        select(db.conversations.c.id).where(db.conversations.c.contact_id == contact_id))).values(body="", media=[]))
    log(conn, business_id, "contact.erased", actor, contact_id)


def find_or_create_by_identity(conn: Connection, biz: dict, channel: str, address: str, name: str | None,
                               source: str) -> tuple[dict, bool]:
    """Inbound message from someone: the contact we know, or a new one."""
    I = db.identities
    cid = conn.execute(select(I.c.contact_id).where(I.c.business_id == biz["id"], I.c.channel == channel,
                                                    I.c.address == address)).scalar()
    if cid:
        row = conn.execute(select(C).where(C.c.id == cid)).mappings().one()
        if name and not row["name"] and row["deleted_at"] is None:
            conn.execute(update(C).where(C.c.id == cid).values(name=name[:200], updated_at=func.now()))
            row = conn.execute(select(C).where(C.c.id == cid)).mappings().one()
        return dict(row), False
    vals = {"name": (name or "")[:200], "source": source}
    if channel in PHONE_CHANNELS:
        vals["phone"] = address
    elif channel == "email":
        vals["email"] = address
    row = conn.execute(insert(C).values(business_id=biz["id"], **vals).returning(C)).mappings().one()
    link(conn, biz["id"], row["id"], channel, address)
    if channel in (*PHONE_CHANNELS, "email"):
        _sync_identities(conn, biz["id"], row)
    log(conn, biz["id"], "contact.created", f"webhook:{channel}", row["id"], {"source": source})
    return dict(row), True


# the whole message, alone, in the languages people write in (a word inside a sentence never counts)
STOP_WORDS = {"stop", "stopall", "unsubscribe", "unsub", "optout", "opt-out", "opt out",          # en
              "baja", "alto", "cancelar", "parar", "darme de baja",                               # es
              "sair", "descadastrar",                                                              # pt
              "arret", "arrêt", "désabonner", "desabonner", "désinscrire",                          # fr
              "stopp", "abmelden", "abbestellen",                                                  # de
              "tigil", "hinto", "itigil",                                                          # fil
              "berhenti", "henti",                                                                 # id / ms
              "बंद", "रोकें", "रुको", "बंद करो",                                                    # hi
              "توقف", "إيقاف", "الغاء", "إلغاء",                                                    # ar
              "停止", "配信停止", "退订", "取消订阅",                                                 # ja / zh
              "중지", "수신거부",                                                                    # ko
              "dừng", "huỷ", "hủy",                                                                 # vi
              "acha", "sitisha",                                                                   # sw
              "dur", "iptal",                                                                      # tr
              "basta", "disiscrivi", "rimuovi"}                                                    # it


def is_stop(body: str) -> bool:
    return (body or "").strip().lower().strip(".!¡ ") in STOP_WORDS


def unsubscribe(conn: Connection, business_id: UUID, contact_id: UUID, actor: str) -> None:
    conn.execute(update(C).where(C.c.business_id == business_id, C.c.id == contact_id).values(
        marketing_consent=False, unsubscribed_at=func.now(), updated_at=func.now()))
    log(conn, business_id, "contact.unsubscribed", actor, contact_id)
