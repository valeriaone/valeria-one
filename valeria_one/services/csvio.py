"""Customers and deals as spreadsheet files (CSV): out, with the owner's own fields as columns, and customers back in.

Out: one row per customer (or deal), the owner's fields after the usual columns, headed by their names. In: the
same columns (any order; the owner's fields by their name), a customer already here (same phone or email) is
updated — new tags added, empty details and fields filled — never duplicated. Consent to promotions is never
imported: a file can't prove that someone agreed, so they agree again where the law asks (it is a separate column
on the way out, for the owner's own records). Imported customers aren't new leads, so the "new lead" recipes stay quiet.
"""
from __future__ import annotations

import csv
import io
from sqlalchemy import select
from sqlalchemy.engine import Connection

from .. import db, money, packs, phones
from . import Invalid, log

MAX_ROWS = 5000
MAX_BYTES = 2_000_000
CONTACT_COLS = ("name", "phone", "email", "tags", "notes", "source", "marketing_consent", "created_at")
DEAL_COLS = ("title", "contact", "stage", "amount", "currency", "notes", "created_at")


def _cell(v) -> str:
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@") and not s[1:2].isdigit() else s   # never a formula in the owner's spreadsheet


def contacts_csv(conn: Connection, biz: dict, actor: str) -> str:
    from . import fields as CF
    fl = CF.list_(conn, biz["id"], "contact")
    C = db.contacts
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow([*CONTACT_COLS, *(f["label"] for f in fl)])
    n = 0
    for r in conn.execute(select(C).where(C.c.business_id == biz["id"], C.c.deleted_at.is_(None)).order_by(C.c.created_at)).mappings():
        cu = r["custom"] or {}
        w.writerow([_cell(r["name"]), r["phone"] or "", r["email"] or "", ";".join(r["tags"] or []), _cell(r["notes"]),
                    _cell(r["source"] or ""), "yes" if r["marketing_consent"] else "no", r["created_at"].isoformat(),
                    *(_cell(CF.form_value(biz, f, cu.get(f["key"]))) for f in fl)])
        n += 1
    log(conn, biz["id"], "contacts.exported", actor, biz["id"], {"rows": n})
    return out.getvalue()


def deals_csv(conn: Connection, biz: dict, actor: str) -> str:
    from . import fields as CF
    fl = CF.list_(conn, biz["id"], "deal")
    D, C = db.deals, db.contacts
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow([*DEAL_COLS, *(f["label"] for f in fl)])
    n = 0
    for r in conn.execute(select(D, C.c.name.label("contact_name")).outerjoin(C, C.c.id == D.c.contact_id)
                          .where(D.c.business_id == biz["id"]).order_by(D.c.created_at)).mappings():
        cu = r["custom"] or {}
        w.writerow([_cell(r["title"]), _cell(r["contact_name"] or ""), r["stage"],
                    money.to_major(r["amount_minor"], r["currency"]) if r["amount_minor"] is not None else "", r["currency"],
                    _cell(r["notes"]), r["created_at"].isoformat(), *(_cell(CF.form_value(biz, f, cu.get(f["key"]))) for f in fl)])
        n += 1
    log(conn, biz["id"], "deals.exported", actor, biz["id"], {"rows": n})
    return out.getvalue()


def import_contacts(conn: Connection, biz: dict, actor: str, raw: bytes) -> dict:
    """-> {"added", "updated", "skipped": [(row number, why)]}. Each row on its own: one bad row never stops the rest."""
    from . import contacts as C
    from . import fields as CF
    if len(raw) > MAX_BYTES:
        raise Invalid("the file is too big (2 MB at most)", "file")
    text = raw.decode("utf-8-sig", errors="replace")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.reader(io.StringIO(text), dialect))
    if not rows:
        raise Invalid("the file is empty", "file")
    head = [h.strip().lower() for h in rows[0]]
    if not {"name", "phone", "email"} & set(head):
        raise Invalid("the first row needs the column names: name, phone, email…", "file")
    fl = CF.list_(conn, biz["id"], "contact")
    by_label = {f["label"].lower(): f for f in fl} | {f["key"]: f for f in fl}
    pack = packs.country(biz["country"])
    I = db.identities
    stats = {"added": 0, "updated": 0, "skipped": []}
    for n, row in enumerate(rows[1:MAX_ROWS + 1], start=2):
        d = {head[i]: (row[i] or "").strip() for i in range(min(len(head), len(row)))}
        if not any(d.values()):
            continue
        custom = {by_label[k]["key"]: v for k, v in d.items() if k in by_label and v}
        try:
            with conn.begin_nested():
                e164 = phones.e164(d["phone"], pack) if d.get("phone") else None
                mail = (d.get("email") or "").lower() or None
                cid = None
                for ch, addr in (("whatsapp", e164), ("email", mail)):
                    if addr and not cid:
                        cid = conn.execute(select(I.c.contact_id).where(I.c.business_id == biz["id"], I.c.channel == ch,
                                                                        I.c.address == addr)).scalar()
                tags = [t for t in (d.get("tags") or "").replace(",", ";").split(";") if t.strip()]
                if cid:
                    cur = C.get(conn, biz["id"], cid)
                    upd = {k: v for k, v in (("name", d.get("name")), ("phone", e164), ("email", mail), ("notes", d.get("notes")))
                           if v and not cur.get(k)}
                    if tags:
                        upd["tags"] = sorted({*cur["tags"], *(t.strip().lower() for t in tags)})
                    keep = {k: v for k, v in custom.items() if k not in (cur["custom"] or {})}
                    if keep:
                        upd["custom"] = keep
                    if upd:
                        C.patch(conn, biz, actor, cid, upd)
                    stats["updated"] += 1
                else:
                    C.create(conn, biz, actor, {"name": d.get("name") or "", "phone": e164, "email": mail, "tags": tags,
                                                "notes": d.get("notes") or "", "source": "import", "custom": custom or None})
                    stats["added"] += 1
        except phones.BadPhone:
            stats["skipped"].append((n, "phone"))
        except Invalid as e:
            stats["skipped"].append((n, str(e)[:120]))
    log(conn, biz["id"], "contacts.imported", actor, biz["id"], {"added": stats["added"], "updated": stats["updated"],
                                                                "skipped": len(stats["skipped"])})
    return stats
