"""cfdi01 (v0.52.0): Mexican CFDI 4.0 e-invoices through the business's OWN Facturapi account (Tad 2026-11-08 «close all gaps
air tight»). $0 to us: their key, their account, their stamps.

* Connect: the owner pastes their Facturapi secret key (sk_test_… to try — test invoices never reach the SAT — or sk_live_…).
  We read their organization (GET /organizations/me) and show what Facturapi still needs before live stamping (their CSD
  «sello digital», fiscal data, the manifiesto) — never a password of ours or theirs, only their API key, kept encrypted.
* Stamp: one tap on an invoice → POST /invoices (Ingreso) with the invoice's own lines, prices and IVA; the customer's fiscal
  data (RFC, razón social without «S.A. de C.V.», régimen, CP, uso) or «público en general» (XAXX010101000, régimen 616, uso
  S01, the global period). The same invoice can never be stamped twice (idempotency key = our invoice id). If Facturapi's
  total differs from ours by more than one cent, it is flagged on the invoice — nothing silently off.
* After: the PDF and XML (fetched from their account), sent by email from Facturapi, cancellation with the SAT motive
  (01 needs the substituting invoice), status refreshed.
Facts from https://docs.facturapi.io (verified 2026-11-08).
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from uuid import UUID

import httpx
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection

from .. import crypto, db
from . import Invalid, NotFound, log

BASE = "https://www.facturapi.io/v2"
A = db.cfdi_accounts
RFC = re.compile(r"^[A-ZÑ&]{3,4}\d{6}[A-Z0-9]{3}$")
GENERIC = "XAXX010101000"
PAYMENT_FORMS = {"01": "Efectivo", "03": "Transferencia", "04": "Tarjeta de crédito", "28": "Tarjeta de débito", "99": "Por definir"}
USES = {"G01": "Adquisición de mercancías", "G03": "Gastos en general", "I04": "Equipo de cómputo", "D01": "Honorarios médicos",
        "CP01": "Pagos", "S01": "Sin efectos fiscales"}
TAX_SYSTEMS = {"601": "General de Ley Personas Morales", "603": "Personas Morales con Fines no Lucrativos", "605": "Sueldos y Salarios",
               "606": "Arrendamiento", "612": "Personas Físicas con Actividades Empresariales y Profesionales", "616": "Sin obligaciones fiscales",
               "621": "Incorporación Fiscal", "626": "Régimen Simplificado de Confianza"}
MOTIVES = ("01", "02", "03", "04")


def _key(conn: Connection, business_id: UUID) -> tuple[str, dict]:
    row = conn.execute(select(A).where(A.c.business_id == business_id)).mappings().first()
    if not row:
        raise Invalid("cfdi_not_connected", "cfdi")
    return crypto.open_({"secret": row["secret"]})["secret"], dict(row)


def _call(key: str, method: str, path: str, *, json: dict | None = None, http: httpx.Client | None = None,
          raw: bool = False, headers: dict | None = None):
    cl = http or httpx.Client(timeout=40)
    try:
        r = cl.request(method, BASE + path, json=json, headers={"Authorization": f"Bearer {key}", "Accept-Language": "es", **(headers or {})})
    except httpx.HTTPError:
        raise Invalid("cfdi_unreachable", "cfdi") from None
    if r.status_code >= 400:
        try:
            j = r.json()
        except ValueError:
            j = {}
        raise Invalid(f"Facturapi: {str(j.get('message') or r.status_code)[:300]}", str(j.get("path") or "cfdi")[:60])
    return r if raw else (r.json() if r.content else {})


def account(conn: Connection, business_id: UUID) -> dict | None:
    """What the screens show (never the key)."""
    row = conn.execute(select(A).where(A.c.business_id == business_id)).mappings().first()
    if not row:
        return None
    return {"livemode": row["livemode"], "org": row["org"], "defaults": row["defaults"], "checked_at": row["checked_at"]}


def connect(conn: Connection, biz: dict, actor: str, key: str, http=None) -> dict:
    """Their own Facturapi key -> checked against their organization, kept encrypted."""
    key = (key or "").strip()
    if not re.fullmatch(r"sk_(test|live)_[A-Za-z0-9_-]{8,200}", key):
        raise Invalid("cfdi_bad_key", "key")
    org = _org(key, http)
    vals = {"secret": crypto.seal({"secret": key})["secret"], "livemode": key.startswith("sk_live_"), "org": org,
            "checked_at": datetime.now(timezone.utc), "updated_at": datetime.now(timezone.utc)}
    conn.execute(pg_insert(A).values(business_id=biz["id"], **vals).on_conflict_do_update(index_elements=[A.c.business_id], set_=vals))
    log(conn, biz["id"], "cfdi.connected", actor, biz["id"], {"live": vals["livemode"], "ready": org.get("ready")})
    return account(conn, biz["id"])


def _org(key: str, http=None) -> dict:
    j = _call(key, "GET", "/organizations/me", http=http)
    legal = j.get("legal") or {}
    return {"name": str(legal.get("name") or legal.get("legal_name") or j.get("name") or "")[:200],
            "tax_id": str(legal.get("tax_id") or "")[:13], "zip": str((legal.get("address") or {}).get("zip") or "")[:5],
            "ready": bool(j.get("is_production_ready")),
            "pending": [str(p.get("description") or p.get("type") or "")[:200] for p in (j.get("pending_steps") or []) if isinstance(p, dict)][:10]}


def refresh_org(conn: Connection, biz: dict, http=None) -> dict:
    key, _ = _key(conn, biz["id"])
    org = _org(key, http)
    conn.execute(update(A).where(A.c.business_id == biz["id"]).values(org=org, checked_at=datetime.now(timezone.utc)))
    return account(conn, biz["id"])


def disconnect(conn: Connection, biz: dict, actor: str) -> None:
    conn.execute(A.delete().where(A.c.business_id == biz["id"]))
    log(conn, biz["id"], "cfdi.disconnected", actor, biz["id"])


def save_defaults(conn: Connection, biz: dict, actor: str, **d) -> dict:
    _key(conn, biz["id"])
    pk = re.sub(r"\D", "", str(d.get("product_key") or ""))[:8] or "01010101"      # 01010101 «No existe en el catálogo»
    uk = str(d.get("unit_key") or "E48").strip().upper()[:3] or "E48"              # E48 = unidad de servicio
    pf = str(d.get("payment_form") or "03")
    zp = re.sub(r"\D", "", str(d.get("zip") or ""))[:5]
    if pf not in PAYMENT_FORMS:
        raise Invalid("cfdi_bad_payment_form", "payment_form")
    vals = {"product_key": pk, "unit_key": uk, "payment_form": pf, "series": re.sub(r"[^A-Za-z0-9]", "", str(d.get("series") or ""))[:25],
            "zip": zp}
    conn.execute(update(A).where(A.c.business_id == biz["id"]).values(defaults=vals, updated_at=datetime.now(timezone.utc)))
    log(conn, biz["id"], "cfdi.defaults", actor, biz["id"])
    return account(conn, biz["id"])


def receiver(form: dict) -> dict:
    """The customer's fiscal data as typed (checked) — or público en general."""
    if form.get("publico"):
        return {"publico": True}
    rfc = re.sub(r"\s", "", str(form.get("tax_id") or "")).upper()
    if not RFC.match(rfc) or rfc == GENERIC:
        raise Invalid("cfdi_bad_rfc", "tax_id")
    name = " ".join(str(form.get("legal_name") or "").split()).upper()[:254]
    name = re.sub(r",?\s+(S\.?\s?A\.?\s?P?\.?\s?I?\.?\s?(DE\s+)?C\.?\s?V\.?|S\.?\s?DE\s+R\.?\s?L\.?(\s+DE\s+C\.?\s?V\.?)?|S\.?\s?C\.?|A\.?\s?C\.?)$", "", name).strip()
    if not name:
        raise Invalid("cfdi_need_name", "legal_name")
    ts = str(form.get("tax_system") or "")
    if ts not in TAX_SYSTEMS:
        raise Invalid("cfdi_bad_tax_system", "tax_system")
    zp = re.sub(r"\D", "", str(form.get("zip") or ""))
    if len(zp) != 5:
        raise Invalid("cfdi_bad_zip", "zip")
    use = str(form.get("use") or "G03")
    if use not in USES:
        raise Invalid("cfdi_bad_use", "use")
    em = str(form.get("email") or "").strip().lower()[:200]
    return {"tax_id": rfc, "legal_name": name, "tax_system": ts, "zip": zp, "use": use, **({"email": em} if "@" in em else {})}


def _body(inv: dict, acc: dict, rec: dict, payment_form: str, payment_method: str) -> dict:
    if inv["currency"] != "MXN":
        raise Invalid("cfdi_mxn_only", "currency")
    d = acc.get("defaults") or {}
    rate = (inv["tax_rate_bp"] or 0) / 10000 if inv["tax_minor"] is not None else None
    items = []
    for ln in inv["lines"] or []:
        prod = {"description": str(ln.get("description") or inv["title"])[:1000], "product_key": d.get("product_key") or "01010101",
                "unit_key": d.get("unit_key") or "E48", "price": round(int(ln.get("unit_minor") or 0) / 100, 2), "tax_included": False}
        prod["taxes"] = [{"type": "IVA", "rate": rate}] if rate is not None else []
        items.append({"quantity": ln.get("qty") or 1, "product": prod})
    if not items:
        raise Invalid("cfdi_no_lines", "lines")
    if payment_form not in PAYMENT_FORMS:
        raise Invalid("cfdi_bad_payment_form", "payment_form")
    if payment_method not in ("PUE", "PPD"):
        raise Invalid("cfdi_bad_payment_method", "payment_method")
    if payment_method == "PPD":
        payment_form = "99"                                     # SAT: paid later = «por definir»
    body = {"items": items, "payment_form": payment_form, "payment_method": payment_method, "currency": "MXN",
            **({"series": d["series"]} if d.get("series") else {})}
    if rec.get("publico"):
        zp = d.get("zip") or (acc.get("org") or {}).get("zip")
        if not zp:
            raise Invalid("cfdi_need_issuer_zip", "zip")
        issued = inv["issued_on"]
        body.update(customer={"legal_name": "PUBLICO EN GENERAL", "tax_id": GENERIC, "tax_system": "616", "address": {"zip": zp}},
                    use="S01", **{"global": {"periodicity": "month", "months": f"{issued.month:02d}", "year": issued.year}})
    else:
        body.update(customer={"legal_name": rec["legal_name"], "tax_id": rec["tax_id"], "tax_system": rec["tax_system"],
                              "address": {"zip": rec["zip"]}, **({"email": rec["email"]} if rec.get("email") else {})}, use=rec["use"])
    return body


def stamp(biz: dict, actor: str, invoice_id: UUID, form: dict, http=None) -> dict:
    """One invoice -> its CFDI (once). -> the invoice's cfdi facts."""
    from . import invoices as IVS
    rec = receiver(form)
    with db.tx() as conn:
        key, acc = _key(conn, biz["id"])
        inv = IVS.get(conn, biz["id"], invoice_id)
        if inv["status"] == "void":
            raise Invalid("cfdi_void_invoice", "invoice")
        if (inv.get("cfdi") or {}).get("uuid") and (inv["cfdi"].get("status") != "canceled"):
            raise Invalid("cfdi_already", "invoice")
        pf = str(form.get("payment_form") or (acc.get("defaults") or {}).get("payment_form") or "03")
        pm = str(form.get("payment_method") or "PUE")
        body = _body(inv, acc, rec, pf, pm)
    old = inv.get("cfdi") or {}
    history = (old.get("history") or []) + ([{k: v for k, v in old.items() if k != "history"}] if old.get("uuid") else [])
    j = _call(key, "POST", "/invoices", json={**body, "idempotency_key": f"vo-{invoice_id}-{len(history)}"}, http=http)
    total = j.get("total")
    mismatch = total is not None and abs(round(float(total) * 100) - int(inv["total_minor"])) > 1
    facts = {"id": j.get("id"), "uuid": j.get("uuid"), "status": j.get("status"), "livemode": bool(j.get("livemode")),
             "series": j.get("series"), "folio": j.get("folio_number"), "total": total, "verification_url": j.get("verification_url"),
             "receiver": rec if not rec.get("publico") else {"publico": True}, "payment_form": body["payment_form"],
             "payment_method": body["payment_method"], "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
             "total_mismatch": mismatch, "history": history}
    with db.tx() as conn:
        conn.execute(update(db.invoices).where(db.invoices.c.id == invoice_id, db.invoices.c.business_id == biz["id"]).values(cfdi=facts))
        if inv["contact_id"] and not rec.get("publico"):
            conn.execute(update(db.contacts).where(db.contacts.c.id == inv["contact_id"], db.contacts.c.business_id == biz["id"])
                         .values(fiscal={k: rec[k] for k in ("tax_id", "legal_name", "tax_system", "zip", "use") if k in rec}))
        log(conn, biz["id"], "cfdi.stamped", actor, invoice_id, {"uuid": facts["uuid"], "live": facts["livemode"], "mismatch": mismatch})
    return facts


def _facts(conn: Connection, biz: dict, invoice_id: UUID) -> tuple[str, dict]:
    key, _ = _key(conn, biz["id"])
    inv = conn.execute(select(db.invoices.c.cfdi).where(db.invoices.c.id == invoice_id, db.invoices.c.business_id == biz["id"])).scalar()
    if not inv or not inv.get("id"):
        raise NotFound("cfdi")
    return key, inv


def file(biz: dict, invoice_id: UUID, fmt: str, http=None) -> tuple[bytes, str]:
    """The PDF or XML, from their own account."""
    if fmt not in ("pdf", "xml"):
        raise NotFound("format")
    with db.tx() as conn:
        key, f = _facts(conn, biz, invoice_id)
    r = _call(key, "GET", f"/invoices/{f['id']}/{fmt}", http=http, raw=True)
    return r.content, ("application/pdf" if fmt == "pdf" else "application/xml")


def email(biz: dict, actor: str, invoice_id: UUID, to: str | None = None, http=None) -> None:
    """Facturapi emails the PDF + XML (to the given address, else the customer's)."""
    with db.tx() as conn:
        key, f = _facts(conn, biz, invoice_id)
    to = (to or "").strip().lower()
    _call(key, "POST", f"/invoices/{f['id']}/email", json={"email": to} if "@" in to else None, http=http)
    with db.tx() as conn:
        log(conn, biz["id"], "cfdi.emailed", actor, invoice_id, {})


def cancel(biz: dict, actor: str, invoice_id: UUID, motive: str, substitution: str = "", http=None) -> dict:
    """SAT cancellation: 01 (errors, replaced — give the new invoice's UUID), 02 (errors), 03 (didn't happen), 04 (in a global)."""
    if motive not in MOTIVES:
        raise Invalid("cfdi_bad_motive", "motive")
    sub = (substitution or "").strip()
    if motive in ("01", "04") and not sub:
        raise Invalid("cfdi_need_substitution", "substitution")
    with db.tx() as conn:
        key, f = _facts(conn, biz, invoice_id)
    q = f"?motive={motive}" + (f"&substitution={sub}" if sub else "")
    j = _call(key, "DELETE", f"/invoices/{f['id']}{q}", http=http)
    f.update(status=j.get("status") or f.get("status"), cancellation_status=j.get("cancellation_status"), cancel_motive=motive)
    with db.tx() as conn:
        conn.execute(update(db.invoices).where(db.invoices.c.id == invoice_id, db.invoices.c.business_id == biz["id"]).values(cfdi=f))
        log(conn, biz["id"], "cfdi.cancel", actor, invoice_id, {"motive": motive, "status": f.get("cancellation_status")})
    return f


def refresh(biz: dict, invoice_id: UUID, http=None) -> dict:
    with db.tx() as conn:
        key, f = _facts(conn, biz, invoice_id)
    j = _call(key, "GET", f"/invoices/{f['id']}", http=http)
    f.update(status=j.get("status") or f.get("status"), cancellation_status=j.get("cancellation_status") or f.get("cancellation_status"))
    with db.tx() as conn:
        conn.execute(update(db.invoices).where(db.invoices.c.id == invoice_id, db.invoices.c.business_id == biz["id"]).values(cfdi=f))
    return f
