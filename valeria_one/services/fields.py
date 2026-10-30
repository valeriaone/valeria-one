"""The owner's own fields on customers and deals: "Birthday", "Budget", "Size", "Came from the fair? yes/no".

The owner adds them in Settings (one question per screen) and picks a type; each value is checked against its type
before it is kept, so a "money" field always holds an amount and a "date" field a real date. Values live on the row
itself (contacts.custom / deals.custom, keyed by the field's own key, never by its name — renaming a field keeps every
value). They show on the customer's and the deal's page, in the lists (when the owner says so), on the pipeline
board's cards, in the CSV files, in the API (`custom`) and to Valeria in the office.

Deleting a field asks first, and only hides it: its values stay 30 days so "undo" brings everything back; after
that the job removes them for good (purge_due).
"""
from __future__ import annotations

import re
import secrets
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from uuid import UUID

from sqlalchemy import func, insert, select, update
from sqlalchemy.engine import Connection

from .. import db, money, packs, phones
from . import Invalid, NotFound, log

F = db.custom_fields
OBJECTS = ("contact", "deal")
KINDS = ("text", "number", "money", "date", "yesno", "choice", "phone", "link")
MAX_FIELDS = 20                                            # per object: enough for a small business, short on a phone
MAX_OPTIONS = 20
KEEP_DAYS = 30                                             # a deleted field's values wait this long for "undo"
TABLE = {"contact": db.contacts, "deal": db.deals}
_URL = re.compile(r"^https?://[^\s<>\"']+\.[^\s<>\"']+$", re.I)


def _key() -> str:
    return "f_" + secrets.token_hex(3)


def list_(conn: Connection, business_id: UUID, obj: str | None = None, *, deleted: bool = False) -> list[dict]:
    """The fields in their order (deleted ones only when asked: the "undo" list)."""
    q = select(F).where(F.c.business_id == business_id)
    if obj:
        q = q.where(F.c.object == obj)
    q = q.where(F.c.deleted_at.isnot(None) if deleted else F.c.deleted_at.is_(None))
    return [dict(r) for r in conn.execute(q.order_by(F.c.object, F.c.position, F.c.created_at)).mappings()]


def get(conn: Connection, business_id: UUID, field_id: UUID) -> dict:
    r = conn.execute(select(F).where(F.c.business_id == business_id, F.c.id == field_id)).mappings().first()
    if not r:
        raise NotFound("field")
    return dict(r)


def _options(kind: str, options) -> list[str]:
    if kind != "choice":
        return []
    if isinstance(options, str):
        options = re.split(r"[\n,]", options)
    out: list[str] = []
    for o in options or []:
        o = str(o).strip()[:60]
        if o and o.lower() not in {x.lower() for x in out}:
            out.append(o)
    if len(out) < 2:
        raise Invalid("write at least two choices", "options")
    if len(out) > MAX_OPTIONS:
        raise Invalid(f"at most {MAX_OPTIONS} choices", "options")
    return out


def create(conn: Connection, biz: dict, actor: str, *, obj: str, label: str, kind: str, options=None,
           show_in_list: bool = False, show_on_card: bool = False) -> dict:
    if obj not in OBJECTS:
        raise Invalid("contact or deal", "object")
    if kind not in KINDS:
        raise Invalid(f"type: one of {', '.join(KINDS)}", "kind")
    label = " ".join((label or "").split())[:60]
    if not label:
        raise Invalid("give the field a name", "label")
    have = list_(conn, biz["id"], obj)
    if len(have) >= MAX_FIELDS:
        raise Invalid(f"at most {MAX_FIELDS} fields here", "label")
    if any(f["label"].lower() == label.lower() for f in have):
        raise Invalid("there is already a field with that name", "label")
    row = conn.execute(insert(F).values(business_id=biz["id"], object=obj, key=_key(), label=label, kind=kind,
                                        options=_options(kind, options), show_in_list=bool(show_in_list),
                                        show_on_card=bool(show_on_card) and obj == "deal",
                                        position=(max((f["position"] for f in have), default=0) + 1))
                       .returning(F)).mappings().one()
    log(conn, biz["id"], "field.created", actor, row["id"], {"object": obj, "kind": kind})
    return dict(row)


def patch(conn: Connection, biz: dict, actor: str, field_id: UUID, **data) -> dict:
    """Rename, change the choices, show in the list / on the card, move up. The type never changes (values stay valid)."""
    cur = get(conn, biz["id"], field_id)
    if cur["deleted_at"]:
        raise NotFound("field")
    vals: dict = {}
    if data.get("label") is not None:
        label = " ".join(str(data["label"]).split())[:60]
        if not label:
            raise Invalid("give the field a name", "label")
        if any(f["label"].lower() == label.lower() and f["id"] != cur["id"] for f in list_(conn, biz["id"], cur["object"])):
            raise Invalid("there is already a field with that name", "label")
        vals["label"] = label
    if data.get("options") is not None and cur["kind"] == "choice":
        vals["options"] = _options("choice", data["options"])
    for k in ("show_in_list", "show_on_card"):
        if data.get(k) is not None:
            vals[k] = bool(data[k]) and (k != "show_on_card" or cur["object"] == "deal")
    if data.get("up"):
        have = list_(conn, biz["id"], cur["object"])
        i = next(n for n, f in enumerate(have) if f["id"] == cur["id"])
        if i > 0:
            prev = have[i - 1]
            conn.execute(update(F).where(F.c.id == prev["id"]).values(position=cur["position"]))
            vals["position"] = prev["position"] if prev["position"] != cur["position"] else cur["position"] - 1
    if vals:
        conn.execute(update(F).where(F.c.id == cur["id"]).values(**vals))
        log(conn, biz["id"], "field.updated", actor, cur["id"], {"fields": sorted(vals)})
    return get(conn, biz["id"], field_id)


def delete(conn: Connection, biz: dict, actor: str, field_id: UUID) -> dict:
    """Hidden now; its values stay KEEP_DAYS days so it can be undone."""
    cur = get(conn, biz["id"], field_id)
    if not cur["deleted_at"]:
        conn.execute(update(F).where(F.c.id == cur["id"]).values(deleted_at=func.now()))
        log(conn, biz["id"], "field.deleted", actor, cur["id"], {"object": cur["object"]})
    return get(conn, biz["id"], field_id)


def restore(conn: Connection, biz: dict, actor: str, field_id: UUID) -> dict:
    cur = get(conn, biz["id"], field_id)
    if cur["deleted_at"]:
        if len(list_(conn, biz["id"], cur["object"])) >= MAX_FIELDS:
            raise Invalid(f"at most {MAX_FIELDS} fields here", "label")
        if any(f["label"].lower() == cur["label"].lower() for f in list_(conn, biz["id"], cur["object"])):
            raise Invalid("there is already a field with that name", "label")
        conn.execute(update(F).where(F.c.id == cur["id"]).values(deleted_at=None))
        log(conn, biz["id"], "field.restored", actor, cur["id"])
    return get(conn, biz["id"], field_id)


def purge_due(now: datetime | None = None) -> int:
    """The job: fields deleted more than KEEP_DAYS ago go for good, with their values."""
    now = now or datetime.now(timezone.utc)
    n = 0
    with db.tx() as conn:
        rows = conn.execute(select(F).where(F.c.deleted_at.isnot(None), F.c.deleted_at < now - timedelta(days=KEEP_DAYS))
                            .limit(200).with_for_update(skip_locked=True)).mappings().all()
        for f in rows:
            T = TABLE[f["object"]]
            conn.execute(update(T).where(T.c.business_id == f["business_id"], T.c.custom.has_key(f["key"]))
                         .values(custom=T.c.custom.op("-")(f["key"])))
            conn.execute(F.delete().where(F.c.id == f["id"]))
            log(conn, f["business_id"], "field.purged", "system", f["id"], {"object": f["object"]})
            n += 1
    return n


# ---------------------------------------------------------------- values
def clean_value(biz: dict, f: dict, v):
    """One value, checked against its field's type. None = empty (the value is removed)."""
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    k, lab = f["kind"], f["label"]
    s = v.strip() if isinstance(v, str) else v
    if k == "text":
        return str(s)[:500]
    if k == "number":
        if isinstance(s, bool):
            raise Invalid(f"{lab}: a number", f["key"])
        try:
            d = Decimal(str(s).replace(" ", "").replace(",", "." if str(s).count(",") == 1 and "." not in str(s) else ""))
        except InvalidOperation:
            raise Invalid(f"{lab}: a number, like 12 or 3.5", f["key"]) from None
        if not d.is_finite() or abs(d) >= Decimal("1e15"):
            raise Invalid(f"{lab}: a number, like 12 or 3.5", f["key"])
        return int(d) if d == d.to_integral_value() else float(d)
    if k == "money":
        if isinstance(s, int) and not isinstance(s, bool):
            minor = s                                      # the API sends the smallest unit, like amount_minor
        else:
            try:
                minor = money.to_minor(str(s).replace(",", "").replace(" ", ""), biz["currency"])
            except Exception:
                raise Invalid(f"{lab}: an amount, like 1500.00", f["key"]) from None
        if minor < 0 or minor >= 10 ** 15:
            raise Invalid(f"{lab}: an amount, like 1500.00", f["key"])
        return int(minor)
    if k == "date":
        try:
            return (s if isinstance(s, date) else date.fromisoformat(str(s)[:10])).isoformat()
        except ValueError:
            raise Invalid(f"{lab}: a date (year-month-day)", f["key"]) from None
    if k == "yesno":
        if isinstance(s, bool):
            return s
        t = str(s).lower()
        if t in ("yes", "y", "1", "true", "on", "si", "sí", "oo", "ja", "oui", "sim", "ya", "はい", "是", "نعم", "हाँ"):
            return True
        if t in ("no", "n", "0", "false", "off", "hindi", "nein", "non", "não", "nao", "tidak", "いいえ", "否", "لا", "नहीं"):
            return False
        raise Invalid(f"{lab}: yes or no", f["key"])
    if k == "choice":
        for o in f["options"]:
            if str(s).lower() == o.lower():
                return o
        raise Invalid(f"{lab}: pick one of {', '.join(f['options'])}", f["key"])
    if k == "phone":
        try:
            return phones.e164(str(s), packs.country(biz["country"]))
        except phones.BadPhone:
            raise Invalid(f"{lab}: check the number", f["key"]) from None
    if k == "link":
        u = str(s)
        if u.lower().startswith("www."):
            u = "https://" + u
        if not _URL.match(u) or len(u) > 500:
            raise Invalid(f"{lab}: a web address, like https://…", f["key"])
        return u
    raise Invalid("unknown field type", f["key"])


def merge(conn: Connection, biz: dict, obj: str, current: dict | None, given: dict | None) -> tuple[dict, dict]:
    """New values (by field key, or by the field's name) on top of the current ones. -> (all values, what changed).
    Values of fields that are deleted (waiting for undo) are kept as they are."""
    if given is None:
        return dict(current or {}), {}
    if not isinstance(given, dict):
        raise Invalid("custom: an object of field key -> value", "custom")
    fields = list_(conn, biz["id"], obj)
    by_key = {f["key"]: f for f in fields}
    by_label = {f["label"].lower(): f for f in fields}
    out, changed = dict(current or {}), {}
    for k, v in given.items():
        f = by_key.get(str(k)) or by_label.get(str(k).strip().lower())
        if not f:
            raise Invalid(f"no field called {k!r}", "custom")
        val = clean_value(biz, f, v)
        if val is None:
            if f["key"] in out:
                out.pop(f["key"])
                changed[f["key"]] = None
        elif out.get(f["key"]) != val:
            out[f["key"]] = val
            changed[f["key"]] = val
    return out, changed


def log_changes(conn: Connection, business_id: UUID, actor: str, obj: str, subject_id: UUID, changed: dict) -> None:
    """One event per changed field ("field.changed"): the recipes and the webhooks hear about it."""
    for key, val in changed.items():
        log(conn, business_id, "field.changed", actor, subject_id, {"object": obj, "key": key, "value": as_text(val)})


def as_text(val) -> str:
    """How a stored value compares with what the owner typed in a recipe (yes/no as yes|no)."""
    if val is None:
        return ""
    if isinstance(val, bool):
        return "yes" if val else "no"
    return str(val)


def display(biz: dict, f: dict, val, lang: str = "en") -> str:
    """A value as people read it: money with its currency, yes/no in their words."""
    if val is None or val == "":
        return ""
    if f["kind"] == "money":
        try:
            return money.fmt(int(val), biz["currency"], packs.country(biz["country"]).locale)
        except Exception:
            return str(val)
    if f["kind"] == "yesno":
        from ..web.i18n import t
        return t("yes" if val else "no", lang)
    return str(val)


def form_value(biz: dict, f: dict, val) -> str:
    """A value as an input shows it (money in major units, yes/no as yes|no)."""
    if val is None:
        return ""
    if f["kind"] == "money":
        try:
            return str(money.to_major(int(val), biz["currency"]))
        except Exception:
            return str(val)
    return as_text(val)


def readable(conn: Connection, biz: dict, obj: str, values: dict | None, lang: str = "en") -> dict:
    """{label: value as people read it} — what Valeria sees in the office, and the CSV files."""
    out = {}
    for f in list_(conn, biz["id"], obj):
        if f["key"] in (values or {}):
            out[f["label"]] = display(biz, f, values[f["key"]], lang)
    return out
