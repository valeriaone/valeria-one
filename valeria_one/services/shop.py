"""shop01 (v0.56.0): the online store (Tad 2026-11-08 «close all gaps air tight»).

The business switches «Tienda en línea» on in Website: its priced items (Inventory — what the register sells) are shown at
/shop/<slug> with photo, price and stock; the customer picks quantities, leaves name + WhatsApp (or email) and pickup or
delivery (a flat shipping cost, free above an amount — the owner's numbers). Checkout is a real sale through the register:
a pay link from the business's OWN account (Mercado Pago / Stripe…), stock goes down, nothing half-made if the link fails.
Without a payment account the order is «pay on pickup / delivery». The owner gets a push; the customer gets a page with
their order, the pay button and the business's WhatsApp. Money and the customer's data stay with the business.
"""
from __future__ import annotations

from uuid import UUID

from sqlalchemy.engine import Connection

from .. import db, money, packs, phones
from . import Invalid, log

MAX_LINES = 50


def settings(biz: dict) -> dict:
    s = biz.get("site") or {}
    cur = biz["currency"]

    def amt(k):
        try:
            return money.to_minor(s[k], cur) if s.get(k) else None
        except Exception:                                      # noqa: BLE001
            return None
    return {"on": s.get("shop") == "1", "pickup": s.get("shop_pickup") == "1", "ship": s.get("shop_ship") == "1",
            "ship_minor": amt("shop_ship_cost") or 0, "free_over_minor": amt("shop_free_over"), "note": s.get("shop_note") or ""}


def items(conn: Connection, biz: dict) -> list[dict]:
    """What the store shows: priced, not archived; sold out (counted stock 0) shown as sold out."""
    from . import inventory as INV
    return [i for i in INV.items(conn, biz["id"], for_sale=True) if i.get("price_minor")]


def shipping(st: dict, subtotal_minor: int, how: str) -> int:
    if how != "ship":
        return 0
    if st["free_over_minor"] and subtotal_minor >= st["free_over_minor"]:
        return 0
    return st["ship_minor"]


def checkout(biz: dict, form: dict, http=None) -> dict:
    """The customer's order -> a sale (pay link from the business's own account when it has one). -> {"sale", "pay_url"}."""
    from . import contacts as C
    from . import inventory as INV
    from . import payments as PM
    from . import pos
    from . import push
    st = settings(biz)
    if not st["on"]:
        raise Invalid("shop_off", "shop")
    how = str(form.get("how") or "")
    if how not in ("pickup", "ship") or (how == "pickup" and not st["pickup"]) or (how == "ship" and not st["ship"]):
        raise Invalid("shop_pick_how", "how")
    name = " ".join(str(form.get("name") or "").split())[:200]
    if not name:
        raise Invalid("shop_need_name", "name")
    raw_phone, email = str(form.get("phone") or "").strip(), str(form.get("email") or "").strip().lower()[:320]
    phone = None
    if raw_phone:
        try:
            phone = phones.e164(raw_phone, packs.country(biz["country"]))
        except phones.BadPhone:
            raise Invalid("shop_bad_phone", "phone") from None
    if not phone and "@" not in email:
        raise Invalid("shop_need_reach", "phone")
    address = " ".join(str(form.get("address") or "").split())[:400]
    if how == "ship" and len(address) < 8:
        raise Invalid("shop_need_address", "address")
    lines = []
    with db.tx() as conn:
        allowed = {str(i["id"]): i for i in items(conn, biz)}
        for k, v in form.items():
            if not str(k).startswith("qty_"):
                continue
            iid = str(k)[4:]
            try:
                q = int(v or 0)
            except ValueError:
                continue
            if q <= 0 or iid not in allowed:
                continue
            it = allowed[iid]
            if it.get("stock") is not None and q > it["stock"]:
                raise Invalid("shop_not_enough", "lines")
            lines.append({"item_id": iid, "qty": min(q, 99)})
        if not lines:
            raise Invalid("shop_empty", "lines")
        if len(lines) > MAX_LINES:
            raise Invalid("shop_empty", "lines")
        sub = sum(int(allowed[ln["item_id"]]["price_minor"]) * ln["qty"] for ln in lines)
        ship = shipping(st, sub, how)
        if phone:
            contact, _ = C.find_or_create_by_identity(conn, biz, "sms", phone, name, "online store")
        else:
            from sqlalchemy import func, select
            row = conn.execute(select(db.contacts).where(db.contacts.c.business_id == biz["id"], db.contacts.c.deleted_at.is_(None),
                                                         func.lower(db.contacts.c.email) == email)).mappings().first()
            contact = dict(row) if row else C.create(conn, biz, "shop", {"name": name, "email": email, "source": "online store"})
        can_link = PM.can_link(conn, biz["id"])
    if ship:
        from ..web.i18n import t, ui_lang
        lines.append({"description": t("shop_ship_line", ui_lang(biz["language"])), "qty": 1, "unit_minor": ship})
    note = ("Recoge en tienda" if how == "pickup" else f"Envío a: {address}") + (f" · {form.get('note')}"[:200] if form.get("note") else "")
    sale = pos.sell(biz, "shop", lines=lines, method="link" if can_link else "other", contact_id=contact["id"], note=note[:300], http=http)
    with db.tx() as conn:
        pay_url = pos.pay_url(conn, sale) if can_link else None
        log(conn, biz["id"], "shop.order", "shop", sale["id"], {"total_minor": sale["total_minor"], "how": how})
    push.notify(biz["id"], biz["name"], f"🛍 {name}: {money.to_major(sale['total_minor'], biz['currency'])} {biz['currency']}",
                f"/app/pos/sales/{sale['id']}")
    return {"sale": sale, "pay_url": pay_url, "contact": contact}
