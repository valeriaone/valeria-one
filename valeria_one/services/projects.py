"""Projects: work a business delivers to a customer in stages (a home, a kitchen, an event, a build).

A project has rooms (or areas), selections (the pieces and materials proposed for each room, which the
customer approves or turns down from their phone), suppliers and purchase orders, tasks and punch lists,
hours, and a story the customer can follow in their portal at /portal/<token> — no password, the link is the key.
Money is kept in the smallest currency unit, like everywhere else.
"""
from __future__ import annotations

import secrets
from datetime import date, datetime, timezone
from uuid import UUID

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.engine import Connection

from .. import db, packs, phones
from . import Invalid, NotFound, log

P, R, IT, UP = db.projects, db.project_rooms, db.project_items, db.project_updates
S, CAT, PO, T, H = db.suppliers, db.catalog_items, db.purchase_orders, db.tasks, db.time_entries
CATEGORIES = ("furniture", "lighting", "textile", "finish", "decor", "appliance", "fixture", "art", "service", "other")
ITEM_STATUS = ("proposed", "approved", "rejected", "ordered", "delivered", "installed")
PLAN_MAX_ITEMS = 200


# ---------------------------------------------------------------- small checks
def _money(v, field: str) -> int | None:
    if v in (None, ""):
        return None
    if not isinstance(v, int) or isinstance(v, bool) or v < 0:
        raise Invalid("a whole number in the smallest currency unit, 0 or more", field)
    return v


def _cm(v, field: str) -> int | None:
    if v in (None, ""):
        return None
    try:
        v = int(v)
    except (TypeError, ValueError):
        raise Invalid("centimetres, a whole number", field) from None
    if not 1 <= v <= 100_000:
        raise Invalid("centimetres, from 1 to 100000", field)
    return v


def _days(v, field: str = "lead_days") -> int | None:
    if v in (None, ""):
        return None
    try:
        v = int(v)
    except (TypeError, ValueError):
        raise Invalid("days, a whole number", field) from None
    if not 0 <= v <= 730:
        raise Invalid("days, from 0 to 730", field)
    return v


def _date(v, field: str) -> date | None:
    if v in (None, ""):
        return None
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v))
    except ValueError:
        raise Invalid("a date, YYYY-MM-DD", field) from None


def _txt(v, n: int) -> str:
    return str(v or "").strip()[:n]


def _url(v) -> str:
    v = _txt(v, 500)
    if v and not v.startswith(("https://", "http://")):
        raise Invalid("a web address starting with https://", "url")
    return v


def _stages(biz: dict) -> list:
    return packs.industry(biz["industry"]).project_stages


def stage_keys(biz: dict) -> list[str]:
    return [s.key for s in _stages(biz)]


# ---------------------------------------------------------------- projects
def create(conn: Connection, biz: dict, actor: str, data: dict) -> dict:
    name = _txt(data.get("name"), 200)
    if not name:
        raise Invalid("give the project a name", "name")
    contact_id = data.get("contact_id") or None
    if contact_id:
        from . import contacts
        contacts.get(conn, biz["id"], contact_id)
    if data.get("deal_id"):
        from . import deals
        deals.get(conn, biz["id"], data["deal_id"])
    stage = data.get("stage") or stage_keys(biz)[0]
    if stage not in stage_keys(biz):
        raise Invalid("unknown stage", "stage")
    vals = {"business_id": biz["id"], "contact_id": contact_id, "deal_id": data.get("deal_id") or None, "name": name,
            "stage": stage, "address": _txt(data.get("address"), 300), "brief": _txt(data.get("brief"), 5000),
            "style": _txt(data.get("style"), 200), "budget_minor": _money(data.get("budget_minor"), "budget"),
            "fee_minor": _money(data.get("fee_minor"), "fee"), "markup_bp": _markup(data.get("markup_bp")),
            "currency": biz["currency"], "starts_on": _date(data.get("starts_on"), "starts_on"),
            "due_on": _date(data.get("due_on"), "due_on"), "notes": _txt(data.get("notes"), 5000),
            "token": secrets.token_urlsafe(18)}
    row = dict(conn.execute(insert(P).values(**vals).returning(P)).mappings().one())
    log(conn, biz["id"], "project.created", actor, row["id"], {"name": name, "contact_id": str(contact_id) if contact_id else None})
    _story(conn, biz["id"], row["id"], "stage", stage, actor, shared=True)
    return row


def _markup(v) -> int:
    if v in (None, ""):
        return 0
    try:
        v = int(v)
    except (TypeError, ValueError):
        raise Invalid("margin: a whole number of basis points (1500 = 15 %)", "markup") from None
    if not 0 <= v <= 100_000:
        raise Invalid("margin: from 0 to 1000 %", "markup")
    return v


def get(conn: Connection, business_id: UUID, project_id: UUID) -> dict:
    row = conn.execute(select(P).where(P.c.business_id == business_id, P.c.id == project_id)).mappings().first()
    if not row:
        raise NotFound("project")
    return dict(row)


def by_token(conn: Connection, token: str) -> tuple[dict, dict]:
    """The customer's portal: the project and its business, by the link's token."""
    row = conn.execute(select(P).where(P.c.token == (token or ""), P.c.status != "cancelled")).mappings().first() if token else None
    if not row:
        raise NotFound("project")
    biz = dict(conn.execute(select(db.businesses).where(db.businesses.c.id == row["business_id"])).mappings().one())
    if biz["suspended_at"]:
        raise NotFound("project")
    return dict(row), biz


def search(conn: Connection, business_id: UUID, *, status: str | None = "active", contact_id: UUID | None = None,
           limit: int = 200) -> list[dict]:
    q = select(P, db.contacts.c.name.label("contact_name")).outerjoin(db.contacts, db.contacts.c.id == P.c.contact_id) \
        .where(P.c.business_id == business_id)
    if status:
        q = q.where(P.c.status == status)
    if contact_id:
        q = q.where(P.c.contact_id == contact_id)
    return [dict(r) for r in conn.execute(q.order_by(P.c.updated_at.desc()).limit(limit)).mappings()]


def patch(conn: Connection, biz: dict, actor: str, project_id: UUID, data: dict) -> dict:
    p = get(conn, biz["id"], project_id)
    vals: dict = {}
    for k, n in (("name", 200), ("address", 300), ("brief", 5000), ("style", 200), ("notes", 5000)):
        if k in data:
            vals[k] = _txt(data[k], n)
    if "name" in vals and not vals["name"]:
        raise Invalid("give the project a name", "name")
    for k in ("budget_minor", "fee_minor"):
        if k in data:
            vals[k] = _money(data[k], k)
    if "markup_bp" in data:
        vals["markup_bp"] = _markup(data["markup_bp"])
    for k in ("starts_on", "due_on"):
        if k in data:
            vals[k] = _date(data[k], k)
    if "contact_id" in data:
        if data["contact_id"]:
            from . import contacts
            contacts.get(conn, biz["id"], data["contact_id"])
        vals["contact_id"] = data["contact_id"] or None
    if "published" in data:
        vals["published"] = bool(data["published"])
    if "status" in data:
        if data["status"] not in ("active", "done", "cancelled"):
            raise Invalid("active, done or cancelled", "status")
        vals["status"] = data["status"]
    if "stage" in data and data["stage"] != p["stage"]:
        if data["stage"] not in stage_keys(biz):
            raise Invalid("unknown stage", "stage")
        vals["stage"] = data["stage"]
    if not vals:
        return p
    vals["updated_at"] = func.now()
    conn.execute(update(P).where(P.c.id == project_id).values(**vals))
    if "stage" in vals:
        _story(conn, biz["id"], project_id, "stage", vals["stage"], actor, shared=True)
        log(conn, biz["id"], "project.stage", actor, project_id, {"from": p["stage"], "to": vals["stage"]})
    if vals.get("status") and vals["status"] != p["status"]:
        log(conn, biz["id"], f"project.{vals['status']}", actor, project_id)
    log(conn, biz["id"], "project.updated", actor, project_id, {"fields": sorted(k for k in vals if k != "updated_at")})
    return get(conn, biz["id"], project_id)


def new_link(conn: Connection, biz: dict, actor: str, project_id: UUID) -> dict:
    """A fresh portal link: the old one stops working (shared with the wrong person, say)."""
    get(conn, biz["id"], project_id)
    conn.execute(update(P).where(P.c.id == project_id).values(token=secrets.token_urlsafe(18), updated_at=func.now()))
    log(conn, biz["id"], "project.link_renewed", actor, project_id)
    return get(conn, biz["id"], project_id)


def touch(conn: Connection, project_id: UUID) -> None:
    conn.execute(update(P).where(P.c.id == project_id).values(updated_at=func.now()))


# ---------------------------------------------------------------- the project's story
def _story(conn: Connection, business_id: UUID, project_id: UUID, kind: str, body: str, actor: str, *,
           shared: bool = False, file_id: UUID | None = None) -> dict:
    return dict(conn.execute(insert(UP).values(business_id=business_id, project_id=project_id, kind=kind, body=body[:5000],
                                               shared=shared, by=actor, file_id=file_id).returning(UP)).mappings().one())


def note(conn: Connection, biz: dict, actor: str, project_id: UUID, body: str, *, shared: bool = False,
         file_id: UUID | None = None, kind: str = "note") -> dict:
    get(conn, biz["id"], project_id)
    body = _txt(body, 5000)
    if not body and not file_id:
        raise Invalid("write something", "body")
    row = _story(conn, biz["id"], project_id, "update" if shared and kind == "note" else kind, body, actor,
                 shared=shared, file_id=file_id)
    touch(conn, project_id)
    log(conn, biz["id"], "project.note", actor, project_id, {"shared": shared})
    return row


def story(conn: Connection, business_id: UUID, project_id: UUID, *, shared_only: bool = False, limit: int = 200) -> list[dict]:
    q = select(UP).where(UP.c.business_id == business_id, UP.c.project_id == project_id)
    if shared_only:
        q = q.where(UP.c.shared.is_(True))
    return [dict(r) for r in conn.execute(q.order_by(UP.c.created_at.desc()).limit(limit)).mappings()]


# ---------------------------------------------------------------- rooms
def add_room(conn: Connection, biz: dict, actor: str, project_id: UUID, data: dict) -> dict:
    get(conn, biz["id"], project_id)
    name = _txt(data.get("name"), 120)
    if not name:
        raise Invalid("name the room", "name")
    pos = conn.execute(select(func.coalesce(func.max(R.c.position), 0) + 1).where(R.c.project_id == project_id)).scalar()
    row = dict(conn.execute(insert(R).values(business_id=biz["id"], project_id=project_id, name=name,
                                             width_cm=_cm(data.get("width_cm"), "width"), length_cm=_cm(data.get("length_cm"), "length"),
                                             height_cm=_cm(data.get("height_cm"), "height"), notes=_txt(data.get("notes"), 5000),
                                             position=pos).returning(R)).mappings().one())
    touch(conn, project_id)
    log(conn, biz["id"], "room.added", actor, row["id"], {"project_id": str(project_id)})
    return row


def room(conn: Connection, business_id: UUID, room_id: UUID) -> dict:
    row = conn.execute(select(R).where(R.c.business_id == business_id, R.c.id == room_id)).mappings().first()
    if not row:
        raise NotFound("room")
    return dict(row)


def rooms(conn: Connection, business_id: UUID, project_id: UUID) -> list[dict]:
    return [dict(r) for r in conn.execute(select(R).where(R.c.business_id == business_id, R.c.project_id == project_id)
                                          .order_by(R.c.position, R.c.created_at)).mappings()]


def patch_room(conn: Connection, biz: dict, actor: str, room_id: UUID, data: dict) -> dict:
    r = room(conn, biz["id"], room_id)
    vals: dict = {}
    if "name" in data:
        vals["name"] = _txt(data["name"], 120)
        if not vals["name"]:
            raise Invalid("name the room", "name")
    for k in ("width_cm", "length_cm", "height_cm"):
        if k in data:
            vals[k] = _cm(data[k], k)
    if "notes" in data:
        vals["notes"] = _txt(data["notes"], 5000)
    if "plan" in data:
        vals["plan"] = _plan(data["plan"])
    if "palette" in data:
        vals["palette"] = _palette(data["palette"])
    if vals:
        conn.execute(update(R).where(R.c.id == room_id).values(**vals))
        touch(conn, r["project_id"])
        log(conn, biz["id"], "room.updated", actor, room_id, {"fields": sorted(vals)})
    return room(conn, biz["id"], room_id)


def remove_room(conn: Connection, biz: dict, actor: str, room_id: UUID) -> None:
    r = room(conn, biz["id"], room_id)
    conn.execute(delete(R).where(R.c.id == room_id))
    touch(conn, r["project_id"])
    log(conn, biz["id"], "room.deleted", actor, room_id, {"name": r["name"]})


def _num(v, lo: float, hi: float) -> float:
    try:
        v = float(v)
    except (TypeError, ValueError):
        raise Invalid("the plan has a number we can't read", "plan") from None
    if v != v or not lo <= v <= hi:
        raise Invalid("the plan has a number out of range", "plan")
    return round(v, 1)


def _plan(plan) -> dict:
    """A 2D layout in centimetres: pieces (rectangles or circles) placed in the room, nothing else kept."""
    if not isinstance(plan, dict) or not isinstance(plan.get("items", []), list):
        raise Invalid("the plan is not in the right shape", "plan")
    items = []
    for it in plan.get("items", [])[:PLAN_MAX_ITEMS]:
        if not isinstance(it, dict):
            raise Invalid("the plan is not in the right shape", "plan")
        shape = it.get("shape", "rect")
        if shape not in ("rect", "circle", "door", "window"):
            raise Invalid("unknown shape in the plan", "plan")
        color = str(it.get("color") or "")
        items.append({"label": _txt(it.get("label"), 60), "shape": shape,
                      "x": _num(it.get("x", 0), -100_000, 100_000), "y": _num(it.get("y", 0), -100_000, 100_000),
                      "w": _num(it.get("w", 50), 1, 100_000), "h": _num(it.get("h", 50), 1, 100_000),
                      "r": _num(it.get("r", 0), -360, 360),
                      "color": color if len(color) == 7 and color.startswith("#") and _hex(color) else ""})
    return {"items": items}


def _hex(s: str) -> bool:
    try:
        int(s[1:], 16)
        return True
    except ValueError:
        return False


def _palette(pal) -> list:
    if not isinstance(pal, list):
        raise Invalid("the palette is a list of colours", "palette")
    out = []
    for c in pal[:24]:
        h = str((c or {}).get("hex") or "").lower() if isinstance(c, dict) else ""
        if not (len(h) == 7 and h.startswith("#") and _hex(h)):
            raise Invalid("colours look like #d8c3a5", "palette")
        out.append({"hex": h, "name": _txt(c.get("name"), 80)})
    return out


# ---------------------------------------------------------------- selections
def _item_vals(conn: Connection, biz: dict, data: dict, partial: bool) -> dict:
    vals: dict = {}
    if not partial or "name" in data:
        vals["name"] = _txt(data.get("name"), 200)
        if not vals["name"]:
            raise Invalid("name the piece", "name")
    if not partial or "category" in data:
        cat = data.get("category") or "other"
        if cat not in CATEGORIES:
            raise Invalid("unknown category", "category")
        vals["category"] = cat
    for k, n in (("description", 2000), ("sku", 100), ("finish", 200)):
        if not partial or k in data:
            vals[k] = _txt(data.get(k), n)
    if not partial or "url" in data:
        vals["url"] = _url(data.get("url"))
    for k in ("width_cm", "depth_cm", "height_cm"):
        if not partial or k in data:
            vals[k] = _cm(data.get(k), k)
    for k in ("cost_minor", "price_minor"):
        if not partial or k in data:
            vals[k] = _money(data.get(k), k)
    if not partial or "lead_days" in data:
        vals["lead_days"] = _days(data.get("lead_days"))
    if not partial or "supplier_id" in data:
        sid = data.get("supplier_id") or None
        if sid:
            supplier(conn, biz["id"], sid)
        vals["supplier_id"] = sid
    if not partial or "file_id" in data:
        fid = data.get("file_id") or None
        if fid:
            from . import files
            files.get(conn, biz["id"], fid)
        vals["file_id"] = fid
    return vals


def add_item(conn: Connection, biz: dict, actor: str, project_id: UUID, data: dict) -> dict:
    p = get(conn, biz["id"], project_id)
    if data.get("catalog_id"):                                # from the library: copy it, then let this project change it
        c = catalog_item(conn, biz["id"], data["catalog_id"])
        base = {k: c[k] for k in ("category", "name", "description", "sku", "url", "finish", "width_cm", "depth_cm",
                                  "height_cm", "cost_minor", "price_minor", "lead_days", "supplier_id", "file_id")}
        data = {**base, **{k: v for k, v in data.items() if v not in (None, "")}}
    vals = _item_vals(conn, biz, data, partial=False)
    if vals.get("price_minor") is None and vals.get("cost_minor") is not None and p["markup_bp"]:
        vals["price_minor"] = vals["cost_minor"] + (vals["cost_minor"] * p["markup_bp"] + 5000) // 10_000
    room_id = data.get("room_id") or None
    if room_id and room(conn, biz["id"], room_id)["project_id"] != project_id:
        raise NotFound("room")
    qty = data.get("qty") or 1
    try:
        qty = int(qty)
    except (TypeError, ValueError):
        raise Invalid("quantity: a whole number from 1", "qty") from None
    if not 1 <= qty <= 100_000:
        raise Invalid("quantity: a whole number from 1", "qty")
    pos = conn.execute(select(func.coalesce(func.max(IT.c.position), 0) + 1).where(IT.c.project_id == project_id)).scalar()
    row = dict(conn.execute(insert(IT).values(business_id=biz["id"], project_id=project_id, room_id=room_id, qty=qty,
                                              catalog_id=data.get("catalog_id") or None, position=pos, **vals)
                            .returning(IT)).mappings().one())
    touch(conn, project_id)
    log(conn, biz["id"], "selection.added", actor, row["id"], {"project_id": str(project_id), "name": row["name"]})
    return row


def item(conn: Connection, business_id: UUID, item_id: UUID) -> dict:
    row = conn.execute(select(IT).where(IT.c.business_id == business_id, IT.c.id == item_id)).mappings().first()
    if not row:
        raise NotFound("selection")
    return dict(row)


def items(conn: Connection, business_id: UUID, project_id: UUID, *, for_client: bool = False) -> list[dict]:
    q = select(IT, S.c.name.label("supplier_name")).outerjoin(S, S.c.id == IT.c.supplier_id) \
        .where(IT.c.business_id == business_id, IT.c.project_id == project_id)
    rows = [dict(r) for r in conn.execute(q.order_by(IT.c.position, IT.c.created_at)).mappings()]
    if for_client:                                            # what the business pays its suppliers is its own business
        for r in rows:
            r.pop("cost_minor", None)
            r.pop("supplier_name", None)
            r.pop("supplier_id", None)
    return rows


def patch_item(conn: Connection, biz: dict, actor: str, item_id: UUID, data: dict) -> dict:
    it = item(conn, biz["id"], item_id)
    vals = _item_vals(conn, biz, data, partial=True)
    if "room_id" in data:
        rid = data["room_id"] or None
        if rid and room(conn, biz["id"], rid)["project_id"] != it["project_id"]:
            raise NotFound("room")
        vals["room_id"] = rid
    if "qty" in data:
        try:
            q = int(data["qty"])
        except (TypeError, ValueError):
            raise Invalid("quantity: a whole number from 1", "qty") from None
        if not 1 <= q <= 100_000:
            raise Invalid("quantity: a whole number from 1", "qty")
        vals["qty"] = q
    if "status" in data and data["status"] != it["status"]:
        if data["status"] not in ITEM_STATUS:
            raise Invalid("unknown status", "status")
        vals["status"] = data["status"]
        if data["status"] in ("approved", "rejected"):
            vals["decided_at"] = func.now()
        elif data["status"] == "proposed":
            vals["decided_at"] = None
    if not vals:
        return it
    vals["updated_at"] = func.now()
    conn.execute(update(IT).where(IT.c.id == item_id).values(**vals))
    touch(conn, it["project_id"])
    log(conn, biz["id"], "selection.updated", actor, item_id, {"fields": sorted(k for k in vals if k != "updated_at"),
                                                                **({"status": vals["status"]} if "status" in vals else {})})
    return item(conn, biz["id"], item_id)


def remove_item(conn: Connection, biz: dict, actor: str, item_id: UUID) -> None:
    it = item(conn, biz["id"], item_id)
    conn.execute(delete(IT).where(IT.c.id == item_id))
    touch(conn, it["project_id"])
    log(conn, biz["id"], "selection.deleted", actor, item_id, {"name": it["name"]})


def decide(conn: Connection, token: str, item_id: UUID, answer: str, note_: str = "") -> dict:
    """The customer, from their portal (or a WhatsApp button): yes or no to one piece."""
    p, biz = by_token(conn, token)
    it = conn.execute(select(IT).where(IT.c.business_id == biz["id"], IT.c.project_id == p["id"], IT.c.id == item_id)).mappings().first()
    if not it:
        raise NotFound("selection")
    if answer not in ("approved", "rejected"):
        raise Invalid("approve or decline", "answer")
    if it["status"] not in ("proposed", "approved", "rejected"):
        raise Invalid("this one is already ordered; talk to us to change it", "answer")
    conn.execute(update(IT).where(IT.c.id == item_id).values(status=answer, client_note=_txt(note_, 2000),
                                                             decided_at=func.now(), updated_at=func.now()))
    body = it["name"] + (f" — {_txt(note_, 2000)}" if note_ else "")
    _story(conn, biz["id"], p["id"], answer, body, "customer", shared=True)
    touch(conn, p["id"])
    log(conn, biz["id"], f"selection.{answer}", "customer", item_id, {"project_id": str(p["id"]), "name": it["name"]})
    return dict(conn.execute(select(IT).where(IT.c.id == item_id)).mappings().one())


def comment(conn: Connection, token: str, body: str) -> dict:
    p, biz = by_token(conn, token)
    body = _txt(body, 2000)
    if not body:
        raise Invalid("write something", "body")
    row = _story(conn, biz["id"], p["id"], "comment", body, "customer", shared=True)
    touch(conn, p["id"])
    log(conn, biz["id"], "project.comment", "customer", p["id"])
    return row


def react(conn: Connection, token: str, what: str, obj_id: UUID, reaction: str | None, comment_: str = "") -> None:
    """The customer likes or dislikes a picture or a board, with a word if they want."""
    p, biz = by_token(conn, token)
    if reaction not in (None, "like", "dislike"):
        raise Invalid("like or dislike", "reaction")
    tbl = {"file": db.files, "board": db.boards}.get(what)
    if tbl is None:
        raise Invalid("unknown thing", "what")
    n = conn.execute(update(tbl).where(tbl.c.business_id == biz["id"], tbl.c.project_id == p["id"], tbl.c.id == obj_id,
                                       tbl.c.shared.is_(True))
                     .values(reaction=reaction, comment=_txt(comment_, 2000))).rowcount
    if not n:
        raise NotFound(what)
    label = {"like": "👍", "dislike": "👎", None: "·"}[reaction]
    _story(conn, biz["id"], p["id"], "comment", f"{label} {_txt(comment_, 2000)}".strip(), "customer", shared=True,
           file_id=obj_id if what == "file" else None)
    touch(conn, p["id"])
    log(conn, biz["id"], f"{what}.reaction", "customer", obj_id, {"reaction": reaction})


# ---------------------------------------------------------------- money
def totals(conn: Connection, biz: dict, project: dict) -> dict:
    """What the selections add up to, against the budget; and what the business makes on them."""
    rows = conn.execute(select(IT.c.status, IT.c.qty, IT.c.price_minor, IT.c.cost_minor)
                        .where(IT.c.business_id == biz["id"], IT.c.project_id == project["id"])).all()
    t = {"proposed": 0, "approved": 0, "rejected": 0, "cost": 0, "unpriced": 0}
    for st, qty, price, cost in rows:
        if price is None:
            t["unpriced"] += 1
            continue
        if st == "rejected":
            t["rejected"] += qty * price
            continue
        t["approved" if st != "proposed" else "proposed"] += qty * price
        if cost is not None:
            t["cost"] += qty * cost
    committed = t["approved"] + (project["fee_minor"] or 0)
    t["fee"] = project["fee_minor"] or 0
    t["committed"] = committed
    t["all_in"] = committed + t["proposed"]
    t["margin"] = t["approved"] - t["cost"] + t["fee"]
    b = project["budget_minor"]
    t["budget"] = b
    t["left"] = None if b is None else b - t["all_in"]
    t["pct"] = None if not b else min(999, round(100 * t["all_in"] / b))
    P_ = db.payments
    t["paid"] = conn.execute(select(func.coalesce(func.sum(P_.c.amount_minor), 0)).where(
        P_.c.business_id == biz["id"], P_.c.status == "paid", P_.c.project_id == project["id"])).scalar()
    return t


def quote_from_selections(conn: Connection, biz: dict, actor: str, project_id: UUID, *, include: str = "approved",
                          title: str | None = None, valid_days: int = 15) -> dict:
    """One quote (accepted by e-signature, like any other) from the project's pieces, plus the business's fee."""
    from . import quotes
    p = get(conn, biz["id"], project_id)
    want = ("approved",) if include == "approved" else ("proposed", "approved")
    rows = conn.execute(select(IT, R.c.name.label("room_name")).outerjoin(R, R.c.id == IT.c.room_id)
                        .where(IT.c.business_id == biz["id"], IT.c.project_id == project_id, IT.c.status.in_(want))
                        .order_by(R.c.position, IT.c.position)).mappings().all()
    lines = [{"description": (f"{r['room_name']} · " if r["room_name"] else "") + r["name"], "qty": r["qty"],
              "unit_minor": r["price_minor"]} for r in rows if r["price_minor"] is not None]
    if p["fee_minor"]:
        lines.insert(0, {"description": title or p["name"], "qty": 1, "unit_minor": p["fee_minor"]})
    if not lines:
        raise Invalid("nothing with a price to quote yet", "lines")
    q = quotes.create(conn, biz, actor, title=title or p["name"], lines=lines[:quotes.MAX_LINES], contact_id=p["contact_id"],
                      deal_id=p["deal_id"], valid_days=valid_days, project_id=project_id)
    _story(conn, biz["id"], project_id, "quote", f"#{q['number']} · {q['title']}", actor, shared=True)
    return q


# ---------------------------------------------------------------- suppliers and the library
def _phone(raw, biz: dict) -> str:
    try:
        return phones.e164(str(raw), packs.country(biz["country"]))
    except phones.BadPhone:
        raise Invalid("that phone number doesn't look right", "phone") from None


def add_supplier(conn: Connection, biz: dict, actor: str, data: dict) -> dict:
    name = _txt(data.get("name"), 200)
    if not name:
        raise Invalid("name the supplier", "name")
    phone = None
    if data.get("phone"):
        phone = _phone(data["phone"], biz)
    email = _txt(data.get("email"), 320) or None
    if email and "@" not in email:
        raise Invalid("that email doesn't look right", "email")
    row = dict(conn.execute(insert(S).values(business_id=biz["id"], name=name, kind=_txt(data.get("kind"), 60),
                                             contact_name=_txt(data.get("contact_name"), 200), phone=phone, email=email,
                                             website=_url(data.get("website")), lead_days=_days(data.get("lead_days")),
                                             notes=_txt(data.get("notes"), 5000)).returning(S)).mappings().one())
    log(conn, biz["id"], "supplier.added", actor, row["id"], {"name": name})
    return row


def supplier(conn: Connection, business_id: UUID, supplier_id: UUID) -> dict:
    row = conn.execute(select(S).where(S.c.business_id == business_id, S.c.id == supplier_id)).mappings().first()
    if not row:
        raise NotFound("supplier")
    return dict(row)


def suppliers(conn: Connection, business_id: UUID, *, archived: bool = False) -> list[dict]:
    return [dict(r) for r in conn.execute(select(S).where(S.c.business_id == business_id, S.c.archived.is_(archived))
                                          .order_by(func.lower(S.c.name))).mappings()]


def patch_supplier(conn: Connection, biz: dict, actor: str, supplier_id: UUID, data: dict) -> dict:
    supplier(conn, biz["id"], supplier_id)
    vals: dict = {}
    for k, n in (("name", 200), ("kind", 60), ("contact_name", 200), ("notes", 5000)):
        if k in data:
            vals[k] = _txt(data[k], n)
    if "name" in vals and not vals["name"]:
        raise Invalid("name the supplier", "name")
    if "phone" in data:
        vals["phone"] = _phone(data["phone"], biz) if data["phone"] else None
    if "email" in data:
        vals["email"] = _txt(data["email"], 320) or None
    if "website" in data:
        vals["website"] = _url(data["website"])
    if "lead_days" in data:
        vals["lead_days"] = _days(data["lead_days"])
    if "archived" in data:
        vals["archived"] = bool(data["archived"])
    if vals:
        conn.execute(update(S).where(S.c.id == supplier_id).values(**vals))
        log(conn, biz["id"], "supplier.updated", actor, supplier_id, {"fields": sorted(vals)})
    return supplier(conn, biz["id"], supplier_id)


def add_catalog(conn: Connection, biz: dict, actor: str, data: dict) -> dict:
    vals = _item_vals(conn, biz, data, partial=False)
    row = dict(conn.execute(insert(CAT).values(business_id=biz["id"], **vals).returning(CAT)).mappings().one())
    log(conn, biz["id"], "library.added", actor, row["id"], {"name": row["name"]})
    return row


def catalog_item(conn: Connection, business_id: UUID, cat_id: UUID) -> dict:
    row = conn.execute(select(CAT).where(CAT.c.business_id == business_id, CAT.c.id == cat_id)).mappings().first()
    if not row:
        raise NotFound("library item")
    return dict(row)


def catalog(conn: Connection, business_id: UUID, *, q: str = "", category: str | None = None, limit: int = 300) -> list[dict]:
    st = select(CAT, S.c.name.label("supplier_name")).outerjoin(S, S.c.id == CAT.c.supplier_id) \
        .where(CAT.c.business_id == business_id, CAT.c.archived.is_(False))
    if q:
        like = f"%{q.strip()[:60]}%"
        st = st.where(CAT.c.name.ilike(like) | CAT.c.description.ilike(like) | CAT.c.finish.ilike(like) | CAT.c.sku.ilike(like))
    if category:
        st = st.where(CAT.c.category == category)
    return [dict(r) for r in conn.execute(st.order_by(CAT.c.category, func.lower(CAT.c.name)).limit(limit)).mappings()]


def patch_catalog(conn: Connection, biz: dict, actor: str, cat_id: UUID, data: dict) -> dict:
    catalog_item(conn, biz["id"], cat_id)
    vals = _item_vals(conn, biz, data, partial=True)
    if "archived" in data:
        vals["archived"] = bool(data["archived"])
    if vals:
        conn.execute(update(CAT).where(CAT.c.id == cat_id).values(**vals))
        log(conn, biz["id"], "library.updated", actor, cat_id, {"fields": sorted(vals)})
    return catalog_item(conn, biz["id"], cat_id)


def save_to_catalog(conn: Connection, biz: dict, actor: str, item_id: UUID) -> dict:
    it = item(conn, biz["id"], item_id)
    data = {k: it[k] for k in ("category", "name", "description", "sku", "url", "finish", "width_cm", "depth_cm", "height_cm",
                               "cost_minor", "price_minor", "lead_days", "supplier_id", "file_id")}
    row = add_catalog(conn, biz, actor, data)
    conn.execute(update(IT).where(IT.c.id == item_id).values(catalog_id=row["id"]))
    return row


# ---------------------------------------------------------------- purchase orders
def order(conn: Connection, biz: dict, actor: str, project_id: UUID, supplier_id: UUID | None, item_ids: list[UUID],
          expected_on=None, notes: str = "") -> dict:
    """Approved pieces from one supplier become one purchase order; the pieces are marked ordered."""
    get(conn, biz["id"], project_id)
    if supplier_id:
        supplier(conn, biz["id"], supplier_id)
    rows = conn.execute(select(IT).where(IT.c.business_id == biz["id"], IT.c.project_id == project_id,
                                         IT.c.id.in_(item_ids or []))).mappings().all()
    if not rows:
        raise Invalid("pick the pieces to order", "items")
    bad = [r["name"] for r in rows if r["status"] != "approved"]
    if bad:
        raise Invalid("only approved pieces can be ordered: " + ", ".join(bad[:3]), "items")
    total = sum(r["qty"] * (r["cost_minor"] if r["cost_minor"] is not None else (r["price_minor"] or 0)) for r in rows)
    conn.execute(select(func.pg_advisory_xact_lock(func.hashtext("po:" + str(biz["id"])))))
    number = conn.execute(select(func.coalesce(func.max(PO.c.number), 0) + 1).where(PO.c.business_id == biz["id"])).scalar()
    po = dict(conn.execute(insert(PO).values(business_id=biz["id"], project_id=project_id, supplier_id=supplier_id, number=number,
                                             status="draft", total_minor=total, currency=biz["currency"],
                                             expected_on=_date(expected_on, "expected_on"), notes=_txt(notes, 2000))
                           .returning(PO)).mappings().one())
    conn.execute(update(IT).where(IT.c.id.in_([r["id"] for r in rows])).values(po_id=po["id"], status="ordered", updated_at=func.now()))
    touch(conn, project_id)
    log(conn, biz["id"], "po.created", actor, po["id"], {"number": number, "total_minor": total, "items": len(rows)})
    return po


def po(conn: Connection, business_id: UUID, po_id: UUID) -> dict:
    row = conn.execute(select(PO, S.c.name.label("supplier_name"), S.c.email.label("supplier_email"),
                              S.c.phone.label("supplier_phone")).outerjoin(S, S.c.id == PO.c.supplier_id)
                       .where(PO.c.business_id == business_id, PO.c.id == po_id)).mappings().first()
    if not row:
        raise NotFound("purchase order")
    return dict(row)


def pos(conn: Connection, business_id: UUID, *, project_id: UUID | None = None, open_only: bool = False) -> list[dict]:
    q = select(PO, S.c.name.label("supplier_name"), P.c.name.label("project_name")) \
        .outerjoin(S, S.c.id == PO.c.supplier_id).join(P, P.c.id == PO.c.project_id).where(PO.c.business_id == business_id)
    if project_id:
        q = q.where(PO.c.project_id == project_id)
    if open_only:
        q = q.where(PO.c.status.in_(("draft", "sent", "confirmed")))
    return [dict(r) for r in conn.execute(q.order_by(PO.c.number.desc())).mappings()]


def po_items(conn: Connection, business_id: UUID, po_id: UUID) -> list[dict]:
    return [dict(r) for r in conn.execute(select(IT, R.c.name.label("room_name")).outerjoin(R, R.c.id == IT.c.room_id)
                                          .where(IT.c.business_id == business_id, IT.c.po_id == po_id)
                                          .order_by(IT.c.position)).mappings()]


def patch_po(conn: Connection, biz: dict, actor: str, po_id: UUID, data: dict) -> dict:
    o = po(conn, biz["id"], po_id)
    vals: dict = {}
    if "status" in data and data["status"] != o["status"]:
        if data["status"] not in ("draft", "sent", "confirmed", "delivered", "cancelled"):
            raise Invalid("unknown status", "status")
        vals["status"] = data["status"]
        if data["status"] == "delivered":
            vals["delivered_on"] = _date(data.get("delivered_on"), "delivered_on") or datetime.now(timezone.utc).date()
            conn.execute(update(IT).where(IT.c.po_id == po_id, IT.c.status == "ordered").values(status="delivered", updated_at=func.now()))
            _story(conn, biz["id"], o["project_id"], "update", f"PO #{o['number']}", actor, shared=False)
        if data["status"] == "cancelled":
            conn.execute(update(IT).where(IT.c.po_id == po_id, IT.c.status == "ordered").values(status="approved", po_id=None,
                                                                                               updated_at=func.now()))
    if "expected_on" in data:
        vals["expected_on"] = _date(data["expected_on"], "expected_on")
    if "paid_minor" in data:
        vals["paid_minor"] = _money(data["paid_minor"], "paid") or 0
    if "notes" in data:
        vals["notes"] = _txt(data["notes"], 2000)
    if vals:
        vals["updated_at"] = func.now()
        conn.execute(update(PO).where(PO.c.id == po_id).values(**vals))
        touch(conn, o["project_id"])
        log(conn, biz["id"], "po.updated", actor, po_id, {k: str(v) for k, v in vals.items() if k != "updated_at"})
    return po(conn, biz["id"], po_id)


def late_orders(conn: Connection, business_id: UUID, today: date | None = None) -> list[dict]:
    today = today or datetime.now(timezone.utc).date()
    return [o for o in pos(conn, business_id, open_only=True) if o["expected_on"] and o["expected_on"] < today]


# ---------------------------------------------------------------- tasks and hours
def add_task(conn: Connection, biz: dict, actor: str, data: dict) -> dict:
    title = _txt(data.get("title"), 300)
    if not title:
        raise Invalid("what needs doing?", "title")
    kind = data.get("kind") or "task"
    if kind not in ("task", "install", "punch"):
        raise Invalid("task, install or punch", "kind")
    pid = data.get("project_id") or None
    if pid:
        get(conn, biz["id"], pid)
    rid = data.get("room_id") or None
    if rid and (not pid or room(conn, biz["id"], rid)["project_id"] != pid):
        raise NotFound("room")
    who = data.get("assignee_id") or None
    if who and not conn.execute(select(db.memberships.c.user_id).where(db.memberships.c.business_id == biz["id"],
                                                                       db.memberships.c.user_id == who)).scalar():
        raise Invalid("that person isn't on the team", "assignee_id")
    row = dict(conn.execute(insert(T).values(business_id=biz["id"], project_id=pid, room_id=rid, kind=kind, title=title,
                                             assignee_id=who, due_on=_date(data.get("due_on"), "due_on"),
                                             file_id=data.get("file_id") or None, created_by=actor).returning(T)).mappings().one())
    if pid:
        touch(conn, pid)
    log(conn, biz["id"], "task.added", actor, row["id"], {"kind": kind})
    return row


def tasks(conn: Connection, business_id: UUID, *, project_id: UUID | None = None, open_only: bool = True,
          assignee_id: UUID | None = None, kind: str | None = None) -> list[dict]:
    q = select(T, db.users.c.name.label("assignee_name"), P.c.name.label("project_name"), R.c.name.label("room_name")) \
        .outerjoin(db.users, db.users.c.id == T.c.assignee_id).outerjoin(P, P.c.id == T.c.project_id) \
        .outerjoin(R, R.c.id == T.c.room_id).where(T.c.business_id == business_id)
    if project_id:
        q = q.where(T.c.project_id == project_id)
    if open_only:
        q = q.where(T.c.done_at.is_(None))
    if assignee_id:
        q = q.where(T.c.assignee_id == assignee_id)
    if kind:
        q = q.where(T.c.kind == kind)
    return [dict(r) for r in conn.execute(q.order_by(T.c.done_at.is_not(None), T.c.due_on.nulls_last(), T.c.created_at)).mappings()]


def done_task(conn: Connection, biz: dict, actor: str, task_id: UUID, done: bool = True) -> None:
    n = conn.execute(update(T).where(T.c.business_id == biz["id"], T.c.id == task_id)
                     .values(done_at=func.now() if done else None)).rowcount
    if not n:
        raise NotFound("task")
    log(conn, biz["id"], "task.done" if done else "task.reopened", actor, task_id)


def remove_task(conn: Connection, biz: dict, actor: str, task_id: UUID) -> None:
    if not conn.execute(delete(T).where(T.c.business_id == biz["id"], T.c.id == task_id)).rowcount:
        raise NotFound("task")
    log(conn, biz["id"], "task.deleted", actor, task_id)


def log_hours(conn: Connection, biz: dict, actor: str, project_id: UUID, user_id: UUID | None, minutes, on_date=None,
              note_: str = "") -> dict:
    get(conn, biz["id"], project_id)
    try:
        minutes = int(minutes)
    except (TypeError, ValueError):
        raise Invalid("minutes, a whole number", "minutes") from None
    if not 1 <= minutes <= 1440:
        raise Invalid("from 1 minute to 24 hours", "minutes")
    row = dict(conn.execute(insert(H).values(business_id=biz["id"], project_id=project_id, user_id=user_id, minutes=minutes,
                                             on_date=_date(on_date, "on_date") or datetime.now(timezone.utc).date(),
                                             note=_txt(note_, 300)).returning(H)).mappings().one())
    log(conn, biz["id"], "hours.logged", actor, row["id"], {"minutes": minutes, "project_id": str(project_id)})
    return row


def hours(conn: Connection, business_id: UUID, project_id: UUID) -> dict:
    rows = [dict(r) for r in conn.execute(select(H, db.users.c.name.label("user_name")).outerjoin(db.users, db.users.c.id == H.c.user_id)
                                          .where(H.c.business_id == business_id, H.c.project_id == project_id)
                                          .order_by(H.c.on_date.desc(), H.c.created_at.desc())).mappings()]
    by: dict = {}
    for r in rows:
        by[r["user_name"] or "—"] = by.get(r["user_name"] or "—", 0) + r["minutes"]
    return {"entries": rows, "total": sum(r["minutes"] for r in rows), "by_person": by}


def remove_hours(conn: Connection, biz: dict, actor: str, entry_id: UUID) -> None:
    if not conn.execute(delete(H).where(H.c.business_id == biz["id"], H.c.id == entry_id)).rowcount:
        raise NotFound("hours")
    log(conn, biz["id"], "hours.deleted", actor, entry_id)


def published(conn: Connection, business_id: UUID) -> list[dict]:
    """Finished work on the website: published projects with their public pictures (cover first)."""
    rows = [dict(r) for r in conn.execute(select(P.c.id, P.c.name, P.c.style, P.c.brief, P.c.updated_at).where(
        P.c.business_id == business_id, P.c.published.is_(True)).order_by(P.c.updated_at.desc()).limit(60)).mappings()]
    F = db.files
    for r in rows:
        r["pictures"] = [str(x) for x in conn.execute(select(F.c.id).where(F.c.business_id == business_id, F.c.project_id == r["id"],
                                                                         F.c.public.is_(True)).order_by(F.c.created_at)).scalars()]
    return [r for r in rows if r["pictures"]]
