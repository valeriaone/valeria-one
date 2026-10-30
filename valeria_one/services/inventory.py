"""Inventory (ars01, v0.20): what the business has on its shelves, how many, and when it runs low.

The items are the business's own (catalog_items — the same table as the Library; there is never a second products
table). An item in Inventory carries in_inventory, a stock count (NULL = not counted: a service, a custom job), a
"running low" mark and, optionally, the item it is a variant of (one level: "T-shirt" → "Red / L"). Every change to a
count is a stock_moves row — why (received, damaged, counted, sold, refunded, voided, imported), by whom, from which
sale — so the number on screen can always be explained. A sale at the counter takes stock out; a refund or a void puts
it back. Counts may go below zero (the shelf had more than the count said): the next count fixes it, and the history shows it.
"""
from __future__ import annotations

import csv
import io
from uuid import UUID

from sqlalchemy import func, insert, select, update
from sqlalchemy.engine import Connection

from .. import db, money
from . import Invalid, NotFound, log

CAT, MV, S = db.catalog_items, db.stock_moves, db.suppliers
ADJUST = ("received", "damaged", "count")                    # what the owner or staff do by hand
MAX_QTY = 1_000_000
CSV_COLS = ("name", "variant", "sku", "price", "cost", "stock", "low_at", "supplier")
MAX_ROWS = 5000
MAX_BYTES = 2_000_000


def _count(v, field: str, *, lo: int = 0) -> int | None:
    if v in (None, ""):
        return None
    try:
        n = int(str(v).strip())
    except (TypeError, ValueError):
        raise Invalid("a whole number", field) from None
    if not lo <= n <= MAX_QTY:
        raise Invalid(f"from {lo} to {MAX_QTY}", field)
    return n


def label(it: dict) -> str:
    """What the item is called on a receipt: "T-shirt · Red / L"."""
    return f"{it['name']} · {it['variant']}" if it.get("variant") else it["name"]


def _q(business_id: UUID):
    P = CAT.alias("parent")
    return (select(CAT, S.c.name.label("supplier_name"), P.c.name.label("parent_name"))
            .outerjoin(S, S.c.id == CAT.c.supplier_id).outerjoin(P, P.c.id == CAT.c.parent_id)
            .where(CAT.c.business_id == business_id, CAT.c.in_inventory))


def items(conn: Connection, business_id: UUID, *, q: str = "", low: bool = False, archived: bool = False,
          for_sale: bool = False, limit: int = 1000) -> list[dict]:
    st = _q(business_id).where(CAT.c.archived.is_(archived))
    if q:
        like = f"%{q.strip()[:60]}%"
        st = st.where(CAT.c.name.ilike(like) | CAT.c.sku.ilike(like) | CAT.c.variant.ilike(like))
    if low:
        st = st.where(CAT.c.stock.isnot(None), CAT.c.low_at.isnot(None), CAT.c.stock <= CAT.c.low_at)
    if for_sale:
        st = st.where(CAT.c.price_minor.isnot(None))
    rows = [dict(r) for r in conn.execute(st.order_by(func.lower(CAT.c.name), CAT.c.id).limit(limit)).mappings()]
    # a variant sits right under the item it belongs to
    return sorted(rows, key=lambda r: ((r["parent_name"] or r["name"]).lower(), bool(r["parent_id"]), r["name"].lower(),
                                       r["variant"].lower()))


def item(conn: Connection, business_id: UUID, item_id: UUID) -> dict:
    r = conn.execute(_q(business_id).where(CAT.c.id == item_id)).mappings().first()
    if not r:
        raise NotFound("item")
    return dict(r)


def low(conn: Connection, business_id: UUID) -> list[dict]:
    """Items at or under their "running low" mark (Today, Valeria's suggestion, the morning message)."""
    return items(conn, business_id, low=True, limit=200)


def _vals(conn: Connection, biz: dict, data: dict, partial: bool) -> dict:
    from . import projects as PR
    base = {k: data[k] for k in ("name", "sku", "cost_minor", "price_minor", "supplier_id", "file_id", "description") if k in data}
    if not partial:
        base.setdefault("category", "other")
    vals = PR._item_vals(conn, biz, base, partial=partial)    # the Library's own checks: one set of rules for one table
    if not partial or "low_at" in data:
        vals["low_at"] = _count(data.get("low_at"), "low_at")
    if not partial or "variant" in data:
        vals["variant"] = str(data.get("variant") or "").strip()[:100]
    if not partial or "parent_id" in data:
        pid = data.get("parent_id") or None
        if pid:
            p = item(conn, biz["id"], pid)
            if p["parent_id"]:
                raise Invalid("a variant of a variant: pick the main item", "parent_id")
        vals["parent_id"] = pid
    return vals


def add(conn: Connection, biz: dict, actor: str, data: dict) -> dict:
    """A new item on the shelf. stock = how many there are now (blank: not counted)."""
    vals = _vals(conn, biz, data, partial=False)
    start = _count(data.get("stock"), "stock")
    row = dict(conn.execute(insert(CAT).values(business_id=biz["id"], in_inventory=True, stock=None, **vals).returning(CAT))
               .mappings().one())
    log(conn, biz["id"], "inventory.added", actor, row["id"], {"name": row["name"]})
    if start is not None:
        move(conn, biz["id"], actor, row["id"], start, "start")
    return item(conn, biz["id"], row["id"])


def patch(conn: Connection, biz: dict, actor: str, item_id: UUID, data: dict) -> dict:
    """Change what it is called and costs (not the count: counts change by adjust(), with a reason)."""
    cur = item(conn, biz["id"], item_id)
    vals = _vals(conn, biz, data, partial=True)
    if vals.get("parent_id") and str(vals["parent_id"]) == str(item_id):
        raise Invalid("an item can't be a variant of itself", "parent_id")
    if vals.get("parent_id") and conn.execute(select(CAT.c.id).where(CAT.c.business_id == biz["id"], CAT.c.parent_id == item_id)
                                              .limit(1)).first():
        raise Invalid("this item has variants of its own", "parent_id")
    if "archived" in data:
        vals["archived"] = bool(data["archived"])
    if vals:
        conn.execute(update(CAT).where(CAT.c.id == cur["id"]).values(**vals))
        log(conn, biz["id"], "inventory.updated", actor, item_id, {"fields": sorted(vals)})
    return item(conn, biz["id"], item_id)


def move(conn: Connection, business_id: UUID, actor: str, item_id: UUID, delta: int, reason: str, *,
         sale_id: UUID | None = None, note: str = "") -> int | None:
    """Change one count by delta, and write down why. The row is locked, so two tills selling the last one
    count right. An item that isn't counted (stock NULL) is left alone, except when counting starts."""
    cur = conn.execute(select(CAT.c.stock).where(CAT.c.business_id == business_id, CAT.c.id == item_id).with_for_update()).first()
    if cur is None:
        raise NotFound("item")
    if cur.stock is None and reason not in ("start", "received", "count", "import"):
        return None
    before = cur.stock or 0
    after = before + int(delta)
    if delta == 0 and cur.stock is not None:
        return after
    conn.execute(update(CAT).where(CAT.c.id == item_id).values(stock=after))
    conn.execute(insert(MV).values(business_id=business_id, item_id=item_id, delta=int(delta), stock_after=after, reason=reason,
                                   sale_id=sale_id, note=(note or "").strip()[:300], created_by=actor[:100]))
    return after


def adjust(conn: Connection, biz: dict, actor: str, item_id: UUID, reason: str, qty, note: str = "") -> int:
    """By hand: received N (+N), damaged or lost N (−N), or counted: there are N now."""
    if reason not in ADJUST:
        raise Invalid("received, damaged or counted", "reason")
    n = _count(qty, "qty")
    if n is None:
        raise Invalid("how many?", "qty")
    it = item(conn, biz["id"], item_id)
    if reason in ("received", "damaged") and n == 0:
        raise Invalid("how many? (more than 0)", "qty")
    if reason == "damaged" and it["stock"] is None:
        raise Invalid("count it first", "qty")
    if reason == "count":
        locked = conn.execute(select(CAT.c.stock).where(CAT.c.id == item_id).with_for_update()).scalar()
        delta = n - (locked or 0)
        if locked is not None and delta == 0:
            return n
    else:
        delta = n if reason == "received" else -n
    after = move(conn, biz["id"], actor, item_id, delta, reason, note=note)
    log(conn, biz["id"], "inventory.adjusted", actor, item_id, {"reason": reason, "delta": delta, "stock": after})
    return after


def moves(conn: Connection, business_id: UUID, item_id: UUID, limit: int = 100) -> list[dict]:
    return [dict(r) for r in conn.execute(select(MV).where(MV.c.business_id == business_id, MV.c.item_id == item_id)
                                          .order_by(MV.c.created_at.desc(), MV.c.id).limit(limit)).mappings()]


# ---------------------------------------------------------------- spreadsheet out and in
def _cell(v) -> str:
    from .csvio import _cell as c
    return c(v)


def csv_out(conn: Connection, biz: dict, actor: str) -> str:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(CSV_COLS)
    cur = biz["currency"]
    rows = items(conn, business_id=biz["id"], limit=100_000)
    for r in rows:
        w.writerow([_cell(r["name"]), _cell(r["variant"]), _cell(r["sku"]), money.to_major(r["price_minor"], cur),
                    money.to_major(r["cost_minor"], cur), "" if r["stock"] is None else r["stock"],
                    "" if r["low_at"] is None else r["low_at"], _cell(r["supplier_name"] or "")])
    log(conn, biz["id"], "inventory.exported", actor, biz["id"], {"rows": len(rows)})
    return out.getvalue()


def import_csv(conn: Connection, biz: dict, actor: str, raw: bytes) -> dict:
    """The same columns back (any order; name is the only one needed). An item already here — the same SKU, or the
    same name and variant — is updated (a different stock = a count, written down); a new one is added. Nothing is
    ever deleted or archived by a file."""
    if len(raw) > MAX_BYTES:
        raise Invalid("files up to 2 MB", "file")
    try:
        text_ = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text_ = raw.decode("latin-1")
    rd = csv.DictReader(io.StringIO(text_))
    cols = {(c or "").strip().lower(): c for c in (rd.fieldnames or [])}
    if "name" not in cols:
        raise Invalid("the first row needs a 'name' column", "file")
    cur = biz["currency"]
    have = items(conn, biz["id"], limit=100_000) + items(conn, biz["id"], archived=True, limit=100_000)
    by_sku = {r["sku"].strip().lower(): r for r in have if r["sku"].strip()}
    by_name = {(r["name"].strip().lower(), r["variant"].strip().lower()): r for r in have}
    sups = {s["name"].strip().lower(): s["id"] for s in conn.execute(select(S.c.id, S.c.name).where(S.c.business_id == biz["id"])).mappings()}
    stats = {"added": 0, "updated": 0, "skipped": 0}
    for i, row in enumerate(rd):
        if i >= MAX_ROWS:
            break
        g = {k: str(row.get(c) or "").strip() for k, c in cols.items()}
        name = g.get("name", "")
        if not name:
            stats["skipped"] += 1
            continue
        try:
            data: dict = {"name": name}
            if "variant" in cols:
                data["variant"] = g.get("variant", "")
            if g.get("sku"):
                data["sku"] = g["sku"]
            for k in ("price", "cost"):
                if g.get(k):
                    data[f"{k}_minor"] = money.to_minor(g[k].replace(",", ""), cur)
            if g.get("low_at"):
                data["low_at"] = g["low_at"]
            if g.get("supplier") and g["supplier"].lower() in sups:
                data["supplier_id"] = sups[g["supplier"].lower()]
            stock = _count(g.get("stock"), "stock") if g.get("stock") else None
            hit = (by_sku.get(g["sku"].lower()) if g.get("sku") else None) or by_name.get((name.lower(), g.get("variant", "").lower()))
            with conn.begin_nested():
                if hit:
                    patch(conn, biz, actor, hit["id"], data)
                    if stock is not None and stock != hit["stock"]:
                        move(conn, biz["id"], actor, hit["id"], stock - (hit["stock"] or 0), "import")
                    stats["updated"] += 1
                else:
                    new = add(conn, biz, actor, data)
                    if stock is not None:
                        move(conn, biz["id"], actor, new["id"], stock, "import")
                    stats["added"] += 1
        except (Invalid, ValueError, ArithmeticError):
            stats["skipped"] += 1
    log(conn, biz["id"], "inventory.imported", actor, biz["id"], stats)
    return stats
