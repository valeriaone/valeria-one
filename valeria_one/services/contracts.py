"""Contracts (ctr01, v0.24): a business sends its customer a written agreement, and the customer signs it on the phone.

One base contract every business gets — who (the business and the client), what, when, how much (price, deposit,
balance, when it is due), cancellation and refunds, changes and extra charges, liability and damage, the data-privacy
consent and the signatures — in plain words, in the business's language, every word the owner's to change. On top of it
the clause blocks (BLOCKS, a few lines each): the trade's kit switches some on (packs/industries/<code>.yaml, kit:
contracts), the owner switches any on or off. An unusual business describes what it does in its own words and Valeria
picks the blocks that fit with the business's OWN AI (and may add one clause of its own); with no AI, the base + the kit's
blocks (deterministic). The owner may also upload their own contract (a PDF, or the words typed or pasted).

A contract is one copy for one customer: filled in from the customer and the booking / quote / invoice it started from,
changed by the owner if they like, then SENT — from then on it is frozen: its words (and the PDF's bytes) have a SHA-256
fingerprint, the link carries a signature made from it, and any change makes a NEW version (the old one is voided, so it
can never be signed). The customer opens the link, reads, ticks the privacy consent and the e-signature consent, types
their full name and draws their signature; the record keeps the name, the drawing, the time, the IP, the device and the
fingerprint checked again at that moment. The owner hears it; a booking can wait for it (and the deposit) before it is
confirmed; unsigned after 24 h = one reminder (the jobs round); the owner can void a sent one, the customer can decline
with a reason; a printable copy has a blank notary acknowledgment.

Sample wording only: the screens tell the owner to have a lawyer in their country review it. The words never cite a law.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection

from .. import config, db, money, packs
from . import Blocked, Invalid, NotFound, contacts, log

_log = logging.getLogger("valeria_one.contracts")

T = db.contract_templates
K = db.contracts
S = db.contract_settings

BASE = ("parties", "what", "when", "price", "cancel", "changes", "liability", "privacy", "signatures")
BLOCKS = ("deposit_kept", "installments", "no_show", "rental", "delivery", "site", "client_materials", "photos", "health",
          "allergies", "minors", "membership", "warranty", "confidentiality", "pets", "travel", "weather", "overtime")
AFTER = "liability"                                       # the blocks go after this base section, before privacy
STATUSES = ("draft", "sent", "viewed", "signed", "declined", "voided")
OPEN = ("sent", "viewed")                                 # waiting for the customer
FIELDS = ("business", "client", "client_contact", "service", "date", "price", "deposit", "balance", "due", "today")
BLANK = "________"
SUMMARY = ("parties", "what", "when", "price")           # an uploaded PDF: what goes on the page Valeria adds to it
MAX_SECTIONS = 60
MAX_TEXT = 6000                                           # one section's words
MAX_BODY = 60000
MAX_SIGNATURE = 300 * 1024                                # the drawn signature, as a PNG
REMIND_AFTER = timedelta(hours=24)
_FIELD = re.compile(r"\{(" + "|".join(FIELDS) + r")\}")


def _t(key: str, lang: str | None) -> str:
    from .owner import t
    return t(key, lang)


def _lang(lang: str | None) -> str:
    from ..web.i18n import ui_lang
    return ui_lang(lang)


# ---------------------------------------------------------------- the words: base + blocks
def section(key: str, lang: str) -> dict:
    """One built-in section in a language: {"key", "title", "text"} — base ("parties") or a block ("k:rental")."""
    name = f"ct_k_{key[2:]}" if key.startswith("k:") else f"ct_s_{key}"
    return {"key": key, "title": _t(name, lang), "text": _t(name + "_t", lang)}


def compose(lang: str, blocks: list[str]) -> list[dict]:
    """The base contract with these clause blocks in it (after liability, before privacy), in BLOCKS order."""
    lang = _lang(lang)
    chosen = [b for b in BLOCKS if b in set(blocks or [])]
    out = []
    for key in BASE:
        out.append(section(key, lang))
        if key == AFTER:
            out += [section(f"k:{b}", lang) for b in chosen]
    return out


def kit_blocks(industry: str | None) -> list[str]:
    from .. import kits
    return list(kits.kit(industry).contracts)


def blocks_of(sections: list[dict]) -> list[str]:
    return [s["key"][2:] for s in sections or [] if str(s.get("key", "")).startswith("k:")]


def text_of(sections: list[dict]) -> str:
    """Sections -> the contract's words: '## Title' then its text, a blank line between sections."""
    parts = []
    for s in sections or []:
        title, body = (s.get("title") or "").strip(), (s.get("text") or "").strip()
        if title or body:
            parts.append((f"## {title}\n" if title else "") + body)
    return "\n\n".join(parts).strip()


def sections_of_text(text: str) -> list[dict]:
    """The owner's own words -> sections ('## ' or '# ' lines start one); everything is kept, in order."""
    text = (text or "").replace("\r\n", "\n").strip()[:MAX_BODY]
    out, cur = [], {"key": "own:1", "title": "", "text": ""}
    for line in text.split("\n"):
        m = re.match(r"^\s*#{1,3}\s+(.*)$", line)
        if m:
            if cur["title"] or cur["text"].strip():
                out.append(cur)
            cur = {"key": f"own:{len(out) + 1}", "title": m.group(1).strip()[:200], "text": ""}
        else:
            cur["text"] += line + "\n"
    if cur["title"] or cur["text"].strip():
        out.append(cur)
    for s in out:
        s["text"] = s["text"].strip()[:MAX_TEXT]
    return out[:MAX_SECTIONS]


def fill(text: str, values: dict) -> str:
    """Put the facts in: only the known {fields}; one not known yet stays a blank line to write on."""
    return _FIELD.sub(lambda m: str(values.get(m.group(1)) or "").strip() or BLANK, text or "")


def paragraphs(body: str) -> list[dict]:
    """For the screens: [{"h": heading} | {"p": paragraph}] (plain text, escaped by the template)."""
    out = []
    for block in re.split(r"\n\s*\n", (body or "").strip()):
        lines = block.split("\n")
        if lines and lines[0].startswith("## "):
            out.append({"h": lines[0][3:].strip()})
            lines = lines[1:]
        rest = "\n".join(lines).strip()
        if rest:
            out.append({"p": rest})
    return out


# ---------------------------------------------------------------- templates
def _clean_sections(raw) -> list[dict]:
    out = []
    for s in (raw or [])[:MAX_SECTIONS]:
        key = str(s.get("key") or "").strip()[:40] or f"own:{len(out) + 1}"
        title, text = str(s.get("title") or "").strip()[:200], str(s.get("text") or "").replace("\r\n", "\n").strip()[:MAX_TEXT]
        if title or text:
            out.append({"key": key, "title": title, "text": text})
    return out


def templates(conn: Connection, business_id: UUID, archived: bool = False) -> list[dict]:
    q = select(T).where(T.c.business_id == business_id, T.c.archived.is_(archived)).order_by(T.c.created_at)
    return [dict(r) for r in conn.execute(q).mappings()]


def template(conn: Connection, business_id: UUID, template_id) -> dict:
    tid = _id(template_id, "template")
    r = conn.execute(select(T).where(T.c.business_id == business_id, T.c.id == tid)).mappings().first()
    if not r:
        raise NotFound("template")
    return dict(r)


def _id(v, what: str) -> UUID:
    try:
        return v if isinstance(v, UUID) else UUID(str(v))
    except (ValueError, TypeError, AttributeError):
        raise NotFound(what) from None


def _new_template(conn: Connection, biz: dict, actor: str, *, name: str, source: str, sections: list[dict],
                  file_id: UUID | None = None, lang: str | None = None) -> dict:
    name = (name or "").strip()[:200] or _t("ct_default_title", lang or biz["language"])
    row = conn.execute(insert(T).values(business_id=biz["id"], name=name, source=source, lang=_lang(lang or biz["language"]),
                                        sections=_clean_sections(sections), file_id=file_id, created_by=actor)
                       .returning(T)).mappings().one()
    log(conn, biz["id"], "contract_template.created", actor, row["id"], {"source": source})
    return dict(row)


def from_kit(conn: Connection, biz: dict, actor: str, name: str = "", blocks: list[str] | None = None) -> dict:
    """The base contract + the clause blocks the trade's kit switches on (or the ones given)."""
    lang = _lang(biz["language"])
    chosen = kit_blocks(biz["industry"]) if blocks is None else [b for b in blocks if b in BLOCKS]
    return _new_template(conn, biz, actor, name=name, source="kit", sections=compose(lang, chosen), lang=lang)


def from_text(conn: Connection, biz: dict, actor: str, name: str, words: str) -> dict:
    """The owner's own contract, typed or pasted (or a .txt file)."""
    secs = sections_of_text(words)
    if not secs:
        raise Invalid(_t("ct_need_words", biz["language"]), "text")
    return _new_template(conn, biz, actor, name=name, source="own", sections=secs)


def from_upload(conn: Connection, biz: dict, actor: str, name: str, raw: bytes, mime: str, filename: str = "") -> dict:
    """The owner's own contract as a file: a PDF is kept as it is (Valeria adds the summary + signature page);
    a text file becomes their own words."""
    from . import files as FL
    mime = (mime or "").split(";")[0].strip().lower()
    if mime.startswith("text/") or (filename or "").lower().endswith(".txt"):
        try:
            words = raw.decode("utf-8")
        except UnicodeDecodeError:
            words = raw.decode("latin-1")
        return from_text(conn, biz, actor, name or (filename or "").rsplit(".", 1)[0], words)
    if mime != "application/pdf" or raw[:5] != b"%PDF-":
        raise Invalid(_t("ct_pdf_or_text", biz["language"]), "file")
    f = FL.save(conn, biz, actor, raw=raw, mime=mime, name=filename or "contract.pdf", kind="doc")
    lang = _lang(biz["language"])
    return _new_template(conn, biz, actor, name=name or (filename or "").rsplit(".", 1)[0], source="upload",
                         sections=[section(k, lang) for k in SUMMARY], file_id=f["id"], lang=lang)


AI_SYSTEM = ("You help a small business write the customer agreement it gives every customer. The base contract (parties, "
             "what, when, price, cancellation and refunds, changes and extra charges, liability, data privacy, signatures) "
             "is already written. From the owner's description of the business, pick the clause blocks that fit, from this "
             "list only: {blocks}. If something important for this kind of business is not covered by the base or the "
             "blocks, write ONE extra clause of at most 6 short sentences in {lang}, in plain words, country-neutral: never "
             "cite or name a law or statute, never promise a legal effect, never invent prices, dates or numbers. "
             "Answer with JSON only: {{\"blocks\": [keys], \"title\": a short title for the agreement in {lang}, "
             "\"extra_title\": a short heading or \"\", \"extra\": the clause or \"\"}}.\n"
             "What each block covers: {about}")


def from_words(biz: dict, actor: str, words: str, http=None) -> tuple[dict, bool]:
    """"What does your business do?" -> a template: base + the blocks that fit (+ one clause of its own), with the
    business's OWN AI; no AI (or no usable answer) = base + the kit's blocks. -> (template, by_ai)."""
    from . import ai as AI
    words = " ".join((words or "").split())[:1500]
    if not words:
        raise Invalid(_t("ct_need_describe", biz["language"]), "words")
    lang = _lang(biz["language"])
    with db.tx() as conn:
        creds = AI.own_key(conn, biz)
    chosen, extra, title = None, None, ""
    if creds:
        prov, key, model = creds
        about = "; ".join(f"{b} = {_t('ct_k_' + b, 'en')}" for b in BLOCKS)
        system = AI_SYSTEM.format(blocks=", ".join(BLOCKS), lang=AI._LANG.get(lang, "the business's language"), about=about)
        try:
            raw = AI._call(prov, key, model.split(",")[0].strip(), system, [("user", words)], AI._http(http), max_tokens=700)
            m = re.search(r"\{.*\}", raw or "", re.S)
            j = json.loads(m.group(0)) if m else {}
            got = [str(b).strip() for b in (j.get("blocks") or []) if str(b).strip() in BLOCKS]
            if isinstance(j.get("blocks"), list):
                chosen = got
                title = str(j.get("title") or "").strip()[:200]
                if str(j.get("extra") or "").strip():
                    extra = {"key": "own:ai", "title": str(j.get("extra_title") or "").strip()[:200] or _t("ct_special", lang),
                             "text": str(j["extra"]).strip()[:1500]}
        except (AI.AIError, ValueError, AttributeError, TypeError):
            chosen = None
    by_ai = chosen is not None
    secs = compose(lang, chosen if by_ai else kit_blocks(biz["industry"]))
    if extra:
        at = next(i for i, s in enumerate(secs) if s["key"] == "privacy")
        secs.insert(at, extra)
    with db.tx() as conn:
        tpl = _new_template(conn, biz, actor, name=title or words[:80], source="ai" if by_ai else "kit", sections=secs, lang=lang)
    return tpl, by_ai


def save_template(conn: Connection, biz: dict, actor: str, template_id, *, name: str | None = None,
                  sections: list[dict] | None = None) -> dict:
    tpl = template(conn, biz["id"], template_id)
    vals = {"updated_at": func.now()}
    if name is not None:
        vals["name"] = name.strip()[:200] or tpl["name"]
    if sections is not None:
        vals["sections"] = _clean_sections(sections)
    conn.execute(update(T).where(T.c.id == tpl["id"]).values(**vals))
    log(conn, biz["id"], "contract_template.saved", actor, tpl["id"])
    return template(conn, biz["id"], tpl["id"])


def set_blocks(conn: Connection, biz: dict, actor: str, template_id, keys: list[str]) -> dict:
    """Switch clause blocks on or off: a block switched off leaves; one switched on comes in its place (before privacy);
    every other section — and the owner's own changes to them — stays as it is."""
    tpl = template(conn, biz["id"], template_id)
    want = [b for b in BLOCKS if b in set(keys or [])]
    secs = [s for s in tpl["sections"] if not s["key"].startswith("k:") or s["key"][2:] in want]
    have = set(blocks_of(secs))
    for b in want:
        if b in have:
            continue
        new = section(f"k:{b}", tpl["lang"])
        later = [i for i, s in enumerate(secs) if s["key"].startswith("k:") and BLOCKS.index(s["key"][2:]) > BLOCKS.index(b)]
        at = later[0] if later else next((i for i, s in enumerate(secs) if s["key"] in ("privacy", "signatures")), len(secs))
        secs.insert(at, new)
    conn.execute(update(T).where(T.c.id == tpl["id"]).values(sections=secs, updated_at=func.now()))
    log(conn, biz["id"], "contract_template.blocks", actor, tpl["id"], {"blocks": want})
    return template(conn, biz["id"], tpl["id"])


def archive_template(conn: Connection, biz: dict, actor: str, template_id, on: bool = True) -> None:
    tpl = template(conn, biz["id"], template_id)
    conn.execute(update(T).where(T.c.id == tpl["id"]).values(archived=bool(on), updated_at=func.now()))
    log(conn, biz["id"], "contract_template.archived" if on else "contract_template.restored", actor, tpl["id"])


# ---------------------------------------------------------------- one contract: filled in
def _money(biz: dict, minor) -> str:
    return "" if minor is None else money.fmt(int(minor), biz["currency"], packs.country(biz["country"]).locale)


def values(biz: dict, fields: dict, lang: str) -> dict:
    """The words that go in the {fields}."""
    from .reminders import when_text
    f = fields or {}
    price, dep = f.get("price_minor"), f.get("deposit_minor")
    bal = price - dep if price is not None and dep is not None else (price if price is not None and dep is None else None)
    from zoneinfo import ZoneInfo
    from babel.dates import format_date
    pack = packs.country(biz["country"])
    loc = pack.locale if pack.locale.split("_")[0] == lang else lang
    try:
        today = format_date(datetime.now(ZoneInfo(biz["timezone"])).date(), "long", locale=loc)
    except Exception:                                          # noqa: BLE001 — an odd locale: the plain date
        today = datetime.now(ZoneInfo(biz["timezone"])).date().isoformat()
    date = f.get("date") or ""
    if not date and f.get("starts_at"):
        try:
            date = when_text(biz, datetime.fromisoformat(f["starts_at"]), lang)
        except ValueError:
            date = ""
    return {"business": biz["name"], "client": f.get("client") or "", "client_contact": f.get("client_contact") or "",
            "service": f.get("service") or "", "date": date, "price": _money(biz, price), "deposit": _money(biz, dep),
            "balance": _money(biz, bal), "due": f.get("due") or "", "today": today}


def prefill(conn: Connection, biz: dict, *, contact_id=None, appointment_id=None, invoice_id=None, quote_id=None) -> dict:
    """The facts Valeria already knows: the customer, and the booking / quote / invoice it starts from.
    -> {"fields", "contact_id", "appointment_id", "invoice_id", "quote_id"} (each one checked to be this business's)."""
    from .reminders import when_text
    out = {"fields": {}, "contact_id": None, "appointment_id": None, "invoice_id": None, "quote_id": None}
    f = out["fields"]
    lang = _lang(biz["language"])
    if appointment_id:
        ap = conn.execute(select(db.appointments).where(db.appointments.c.business_id == biz["id"],
                                                        db.appointments.c.id == _id(appointment_id, "booking"))).mappings().first()
        if not ap:
            raise NotFound("booking")
        out["appointment_id"] = ap["id"]
        f["service"], f["date"] = ap["title"], when_text(biz, ap["starts_at"], lang)
        contact_id = contact_id or ap["contact_id"]
        if ap["location"]:
            f["service"] = f"{ap['title']} — {ap['location']}"
    for key, table, what in (("quote_id", db.quotes, "quote"), ("invoice_id", db.invoices, "invoice")):
        ref = quote_id if key == "quote_id" else invoice_id
        if not ref:
            continue
        r = conn.execute(select(table).where(table.c.business_id == biz["id"], table.c.id == _id(ref, what))).mappings().first()
        if not r:
            raise NotFound(what)
        out[key] = r["id"]
        f.setdefault("service", r["title"])
        f["price_minor"] = r["total_minor"]
        contact_id = contact_id or r["contact_id"]
    if contact_id:
        c = contacts.get(conn, biz["id"], _id(contact_id, "contact"))
        out["contact_id"] = c["id"]
        f["client"] = c["name"] or ""
        f["client_contact"] = " · ".join(x for x in (c["phone"], c["email"]) if x)
    return out


def _next_number(conn: Connection, business_id: UUID) -> int:
    conn.execute(select(func.pg_advisory_xact_lock(func.hashtext("contract:" + str(business_id)))))
    return conn.execute(select(func.coalesce(func.max(K.c.number), 0) + 1).where(K.c.business_id == business_id)).scalar()


def _file_sha(conn: Connection, business_id: UUID, file_id) -> str | None:
    if not file_id:
        return None
    from . import files as FL
    data, _ = FL.content(conn, business_id, file_id)
    return hashlib.sha256(data).hexdigest()


def create(conn: Connection, biz: dict, actor: str, *, template_id, contact_id=None, appointment_id=None, invoice_id=None,
           quote_id=None) -> dict:
    """A draft for one customer from a template, the facts filled in (the owner checks it before sending)."""
    tpl = template(conn, biz["id"], template_id)
    pre = prefill(conn, biz, contact_id=contact_id, appointment_id=appointment_id, invoice_id=invoice_id, quote_id=quote_id)
    lang = tpl["lang"]
    body = fill(text_of(tpl["sections"]), values(biz, pre["fields"], lang))
    row = conn.execute(insert(K).values(
        business_id=biz["id"], number=_next_number(conn, biz["id"]), template_id=tpl["id"], contact_id=pre["contact_id"],
        appointment_id=pre["appointment_id"], invoice_id=pre["invoice_id"], quote_id=pre["quote_id"], title=tpl["name"],
        body=body[:MAX_BODY], fields=pre["fields"], file_id=tpl["file_id"], file_sha256=_file_sha(conn, biz["id"], tpl["file_id"]),
        lang=lang, currency=biz["currency"], created_by=actor).returning(K)).mappings().one()
    log(conn, biz["id"], "contract.created", actor, row["id"], {"number": row["number"], "template_id": str(tpl["id"])})
    return dict(row)


def get(conn: Connection, business_id: UUID, contract_id) -> dict:
    cid = _id(contract_id, "contract")
    r = conn.execute(select(K).where(K.c.business_id == business_id, K.c.id == cid)).mappings().first()
    if not r:
        raise NotFound("contract")
    return dict(r)


def list_(conn: Connection, business_id: UUID, *, status: str | None = None, contact_id=None, limit: int = 200) -> list[dict]:
    C = db.contacts
    q = select(K, C.c.name.label("contact_name")).outerjoin(C, C.c.id == K.c.contact_id).where(K.c.business_id == business_id)
    if status in STATUSES:
        q = q.where(K.c.status == status)
    if contact_id:
        q = q.where(K.c.contact_id == contact_id)
    return [dict(r) for r in conn.execute(q.order_by(K.c.number.desc(), K.c.version.desc()).limit(max(1, min(limit, 500)))).mappings()]


def counts(conn: Connection, business_id: UUID) -> dict:
    return {s: n for s, n in conn.execute(select(K.c.status, func.count()).where(K.c.business_id == business_id)
                                          .group_by(K.c.status)).all()}


def versions(conn: Connection, business_id: UUID, number: int) -> list[dict]:
    return [dict(r) for r in conn.execute(select(K.c.id, K.c.version, K.c.status, K.c.sent_at, K.c.signed_at).where(
        K.c.business_id == business_id, K.c.number == number).order_by(K.c.version)).mappings()]


def _draft(conn: Connection, biz: dict, contract_id) -> dict:
    c = get(conn, biz["id"], contract_id)
    if c["status"] != "draft":
        raise Invalid(_t("ct_frozen", biz["language"]), "status")
    return c


def save(conn: Connection, biz: dict, actor: str, contract_id, *, title: str | None = None, body: str | None = None,
         fields: dict | None = None, refill: bool = False) -> dict:
    """Change this one copy while it is a draft: its facts (refill = the template's words again, with the new facts),
    or its words as the owner typed them."""
    c = _draft(conn, biz, contract_id)
    vals = {"updated_at": func.now()}
    f = dict(c["fields"] or {})
    if fields is not None:
        for k in ("client", "client_contact", "service", "date", "due"):
            if k in fields:
                f[k] = str(fields.get(k) or "").strip()[:300]
        for k in ("price_minor", "deposit_minor"):
            if k in fields:
                v = fields.get(k)
                if v is not None and (not isinstance(v, int) or isinstance(v, bool) or v < 0):
                    raise Invalid("check the amount", k)
                f[k] = v
        vals["fields"] = f
        vals["deposit_minor"] = f.get("deposit_minor")
    if title is not None:
        vals["title"] = title.strip()[:200] or c["title"]
    if refill:
        tpl = template(conn, biz["id"], c["template_id"]) if c["template_id"] else None
        if tpl:
            vals["body"] = fill(text_of(tpl["sections"]), values(biz, f, c["lang"]))[:MAX_BODY]
    elif body is not None:
        vals["body"] = body.replace("\r\n", "\n").strip()[:MAX_BODY]
    conn.execute(update(K).where(K.c.id == c["id"]).values(**vals))
    log(conn, biz["id"], "contract.saved", actor, c["id"], {"refill": refill})
    return get(conn, biz["id"], c["id"])


def delete_draft(conn: Connection, biz: dict, actor: str, contract_id) -> None:
    c = _draft(conn, biz, contract_id)
    conn.execute(K.delete().where(K.c.id == c["id"]))
    log(conn, biz["id"], "contract.deleted", actor, c["id"], {"number": c["number"]})


# ---------------------------------------------------------------- frozen: the fingerprint and the link
def canonical(c: dict) -> str:
    keep = {"business_id": str(c["business_id"]), "number": c["number"], "version": c["version"], "title": c["title"],
            "body": c["body"], "file_sha256": c.get("file_sha256"), "lang": c["lang"]}
    return json.dumps(keep, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def fingerprint(c: dict) -> str:
    """SHA-256 of exactly what the customer signs: the words, the uploaded file's own fingerprint, which copy it is."""
    return hashlib.sha256(canonical(c).encode()).hexdigest()


def sig(c: dict) -> str:
    """The signature in the customer's link: made from the contract AND its fingerprint, so a link only ever opens the
    exact copy that was sent."""
    from ..crypto import hmac_sha256_hex
    return hmac_sha256_hex(config.get("VO_SECRET_KEY", required=True), f"contract:{c['id']}:{c['doc_hash']}".encode())[:32]


def link(c: dict) -> str | None:
    return f"{config.PUBLIC_URL()}/sign/{c['id']}/{sig(c)}" if c.get("doc_hash") else None


def public(conn: Connection, contract_id, s: str) -> tuple[dict, dict] | None:
    """The contract and its business behind a customer's link — None (= 404) for anything else."""
    try:
        cid = UUID(str(contract_id))
    except ValueError:
        return None
    c = conn.execute(select(K).where(K.c.id == cid)).mappings().first()
    if not c or not c["doc_hash"] or not secrets.compare_digest(s or "", sig(dict(c))):
        return None
    biz = conn.execute(select(db.businesses).where(db.businesses.c.id == c["business_id"])).mappings().one()
    return dict(c), dict(biz)


def file_bytes(conn: Connection, c: dict) -> tuple[bytes, str] | None:
    if not c.get("file_id"):
        return None
    from . import files as FL
    return FL.content(conn, c["business_id"], c["file_id"])


def _contact(conn: Connection, business_id: UUID, contact_id) -> dict | None:
    if not contact_id:
        return None
    r = conn.execute(select(db.contacts).where(db.contacts.c.business_id == business_id, db.contacts.c.id == contact_id)).mappings().first()
    return dict(r) if r and not r["deleted_at"] else None


def _first(name: str | None) -> str:
    return ((name or "").strip().split(" ") or [""])[0]


def _deliver(biz: dict, actor: str, contact_id: UUID, body: str, *, account_id=None, http=None,
             now: datetime | None = None) -> tuple[str | None, str | None]:
    """A service message to the customer on the channel they use; when that channel's rules stop it (its 24 h window,
    no address, a failure) another channel they can be reached on (email, SMS…). -> (channel it went on, why it couldn't)."""
    from . import inbox as X
    from .followups import _pick_account
    avoid, why = set(), None
    for _ in range(5):
        with db.tx() as conn:
            acc = account_id if account_id and not avoid else _pick_account(conn, biz, contact_id, frozenset(avoid))
            ch = conn.execute(select(db.channel_accounts.c.channel).where(db.channel_accounts.c.business_id == biz["id"],
                                                                          db.channel_accounts.c.id == acc)).scalar() if acc else None
        if not ch:
            return None, why or "no_channel"
        try:
            m = X.send(biz, actor, contact_id=contact_id, channel_account_id=acc, body=body, kind="service", http=http, now=now)
        except Blocked as b:
            why = b.reason
            if b.reason in ("suspended", "paused", "erased"):
                return None, why
            avoid.add(ch)
            continue
        if m["status"] == "failed":
            why = m.get("error") or "failed"
            avoid.add(ch)
            continue
        return ch, None
    return None, why


def send(biz: dict, actor: str, contract_id, *, account_id=None, http=None, message: bool = True,
         now: datetime | None = None) -> dict:
    """Freeze it (the fingerprint), then the link to the customer by the channel they use. It counts as sent even when
    no message can go (the owner shares the link): -> {**contract, "link", "problem"}."""
    with db.tx() as conn:
        c = dict(conn.execute(select(K).where(K.c.business_id == biz["id"], K.c.id == _id(contract_id, "contract"))
                              .with_for_update()).mappings().first() or {})
        if not c:
            raise NotFound("contract")
        if c["status"] != "draft":
            raise Invalid(_t("ct_frozen", biz["language"]), "status")
        if not (c["body"] or "").strip() and not c["file_id"]:
            raise Invalid(_t("ct_need_words", biz["language"]), "body")
        if c["file_id"]:
            c["file_sha256"] = _file_sha(conn, biz["id"], c["file_id"])
        c["doc_hash"] = fingerprint(c)
        conn.execute(update(K).where(K.c.id == c["id"]).values(status="sent", doc_hash=c["doc_hash"], file_sha256=c["file_sha256"],
                                                               sent_at=func.now(), updated_at=func.now()))
        log(conn, biz["id"], "contract.sent", actor, c["id"], {"number": c["number"], "version": c["version"], "doc_hash": c["doc_hash"]})
        c = get(conn, biz["id"], c["id"])
        contact = _contact(conn, biz["id"], c["contact_id"])
    url = link(c)
    channel, why = None, None
    if message and contact:
        text = _t("ct_msg", c["lang"]).format(name=_first(contact["name"]), business=biz["name"], title=c["title"], link=url)
        channel, why = _deliver(biz, actor, contact["id"], " ".join(text.split(" ")).strip(), account_id=account_id, http=http, now=now)
        if channel:
            with db.tx() as conn:
                conn.execute(update(K).where(K.c.id == c["id"]).values(channel=channel))
            c["channel"] = channel
    elif message:
        why = "no_contact"
    return {**c, "link": url, "problem": why}


def viewed(conn: Connection, c: dict) -> None:
    if c["status"] == "sent":
        conn.execute(update(K).where(K.c.id == c["id"], K.c.status == "sent").values(status="viewed", viewed_at=func.now()))
        log(conn, c["business_id"], "contract.viewed", "customer", c["id"], {"number": c["number"]})


def check_signature(data_url: str) -> str:
    """The drawn signature: a PNG data URL, not too big, not empty. -> the clean data URL."""
    from PIL import Image, UnidentifiedImageError
    m = re.match(r"^data:image/png;base64,([A-Za-z0-9+/=\s]+)$", (data_url or "").strip())
    if not m:
        raise Invalid("draw your signature", "signature")
    try:
        raw = base64.b64decode(m.group(1), validate=False)
    except ValueError:
        raise Invalid("draw your signature", "signature") from None
    if len(raw) > MAX_SIGNATURE or raw[:8] != b"\x89PNG\r\n\x1a\n":
        raise Invalid("draw your signature again (smaller)", "signature")
    try:
        im = Image.open(io.BytesIO(raw))
        w, h = im.size
        if w > 3000 or h > 2000:
            raise Invalid("draw your signature again (smaller)", "signature")
        im = im.convert("RGBA")
        paper = Image.new("RGBA", im.size, (255, 255, 255, 255))
        paper.alpha_composite(im)                              # on white paper: is any ink on it?
        ink = paper.convert("L").point(lambda p: 255 if p < 160 else 0).getbbox()
    except (UnidentifiedImageError, OSError, ValueError):
        raise Invalid("draw your signature", "signature") from None
    if not ink:
        raise Invalid("draw your signature", "signature")
    return "data:image/png;base64," + base64.b64encode(raw).decode()


def sign(contract_id, s: str, *, name: str, privacy: bool, agree: bool, signature: str, ip: str, ua: str,
         http=None) -> dict:
    """The customer signs the exact copy they were sent. Only an open copy (sent / viewed) whose fingerprint still matches
    can be signed — a voided one, an older version, a changed one never."""
    with db.tx() as conn:
        got = public(conn, contract_id, s)
        if not got:
            raise NotFound("contract")
        c, biz = got
        lang = c["lang"]
        if c["status"] not in OPEN:
            raise Invalid(_t("ct_pub_cant_sign", lang), "status")
        name = " ".join((name or "").split())
        if len(name) < 3:
            raise Invalid(_t("ct_pub_need_name", lang), "name")
        if not privacy:
            raise Invalid(_t("ct_pub_need_privacy", lang), "privacy")
        if not agree:
            raise Invalid(_t("ct_pub_need_agree", lang), "agree")
        drawn = check_signature(signature)
        check = dict(c)
        if c["file_id"]:
            check["file_sha256"] = _file_sha(conn, c["business_id"], c["file_id"])
        now_hash = fingerprint(check)
        if now_hash != c["doc_hash"]:                           # the words must be exactly what was sent
            raise Invalid(_t("ct_pub_changed", lang), "doc_hash")
        row = conn.execute(update(K).where(K.c.id == c["id"], K.c.status.in_(OPEN), K.c.doc_hash == now_hash).values(
            status="signed", signed_at=func.now(), signed_name=name[:200], signed_ip=(ip or "")[:64], signed_ua=(ua or "")[:300],
            signature=drawn, signed_hash=now_hash, updated_at=func.now()).returning(K)).mappings().first()
        if not row:
            raise Invalid(_t("ct_pub_cant_sign", lang), "status")
        row = dict(row)
        log(conn, c["business_id"], "contract.signed", "customer", c["id"],
            {"number": c["number"], "version": c["version"], "signed_name": name[:200], "doc_hash": now_hash})
        contact = _contact(conn, biz["id"], c["contact_id"])
    _after_sign(biz, row, contact, http)
    return row


def _after_sign(biz: dict, c: dict, contact: dict | None, http=None) -> None:
    """The owner hears it; the deposit's pay link (the business's own account) when there is one to pay; the booking it
    is for is confirmed when nothing else is missing; the customer gets the link to their signed copy. The signature is
    already on record: none of this can undo it, and a failure here is logged, never shown to the customer."""
    for step in (_tell_signed, _deposit_link, _confirm_booking, _send_copy):
        try:
            step(biz, c, contact, http)
        except Exception:                                      # noqa: BLE001 — see above
            _log.exception("contract %s: after signing, %s failed", c["id"], step.__name__)


def _tell_signed(biz: dict, c: dict, contact: dict | None, http=None) -> None:
    tell_owner(biz, "ct_ping_signed", c, dedupe=f"ctr:signed:{c['id']}", name=(contact or {}).get("name") or c["signed_name"], http=http)


def _deposit_link(biz: dict, c: dict, contact: dict | None, http=None) -> None:
    from . import payments as PM
    if (c["deposit_minor"] or 0) > 0 and not c["deposit_paid_at"] and not c["invoice_id"] and not c["payment_id"]:
        with db.tx() as conn:
            has_pay = bool(PM.accounts(conn, biz["id"]))
        if has_pay:
            try:
                pay = PM.create(biz, "contract", amount_minor=int(c["deposit_minor"]), contact_id=c["contact_id"], http=http,
                                description=f"{_t('ct_deposit', c['lang'])} · {c['title']}"[:300])
                with db.tx() as conn:
                    conn.execute(update(K).where(K.c.id == c["id"]).values(payment_id=pay["id"]))
            except (Invalid, PM.PayError):
                pass                                           # no link today: the owner asks for it as usual


def _confirm_booking(biz: dict, c: dict, contact: dict | None, http=None) -> None:
    with db.tx() as conn:
        confirm_ready(conn, biz, get(conn, biz["id"], c["id"]))


def _send_copy(biz: dict, c: dict, contact: dict | None, http=None) -> None:
    if contact:                                                # they have the signed page open anyway
        text = _t("ct_signed_msg", c["lang"]).format(name=_first(contact["name"]), title=c["title"], link=link(c))
        _deliver(biz, "contract", contact["id"], text, http=http)


def decline(contract_id, s: str, reason: str = "", http=None) -> dict:
    with db.tx() as conn:
        got = public(conn, contract_id, s)
        if not got:
            raise NotFound("contract")
        c, biz = got
        if c["status"] not in OPEN:
            raise Invalid(_t("ct_pub_cant_sign", c["lang"]), "status")
        reason = (reason or "").strip()[:1000]
        row = dict(conn.execute(update(K).where(K.c.id == c["id"], K.c.status.in_(OPEN)).values(
            status="declined", declined_at=func.now(), decline_reason=reason, updated_at=func.now()).returning(K)).mappings().one())
        log(conn, c["business_id"], "contract.declined", "customer", c["id"], {"number": c["number"]})
        who = conn.execute(select(db.contacts.c.name).where(db.contacts.c.id == c["contact_id"])).scalar() if c["contact_id"] else None
    tell_owner(biz, "ct_ping_declined", row, dedupe=f"ctr:declined:{c['id']}", name=who or (c["fields"] or {}).get("client") or "—",
               reason=reason or "—", http=http)
    return row


def void(conn: Connection, biz: dict, actor: str, contract_id, reason: str = "") -> dict:
    """The owner cancels a copy that was sent (or declined): its link stops working for signing. A signed one stays."""
    c = get(conn, biz["id"], contract_id)
    if c["status"] not in (*OPEN, "declined"):
        raise Invalid(_t("ct_cant_void", biz["language"]), "status")
    conn.execute(update(K).where(K.c.id == c["id"]).values(status="voided", voided_at=func.now(),
                                                           void_reason=(reason or "").strip()[:1000], updated_at=func.now()))
    log(conn, biz["id"], "contract.voided", actor, c["id"], {"number": c["number"], "version": c["version"]})
    return get(conn, biz["id"], c["id"])


def new_version(conn: Connection, biz: dict, actor: str, contract_id) -> dict:
    """A change to a sent copy: a new draft (version + 1) with the same words to change; the copy that was sent is voided
    at once, so it can never be signed."""
    c = get(conn, biz["id"], contract_id)
    if c["status"] not in (*OPEN, "declined"):
        raise Invalid(_t("ct_cant_change", biz["language"]), "status")
    conn.execute(select(func.pg_advisory_xact_lock(func.hashtext("contract:" + str(biz["id"])))))
    ver = conn.execute(select(func.max(K.c.version)).where(K.c.business_id == biz["id"], K.c.number == c["number"])).scalar() + 1
    keep = ("template_id", "contact_id", "appointment_id", "invoice_id", "quote_id", "title", "body", "fields", "file_id",
            "file_sha256", "lang", "currency", "deposit_minor")
    row = dict(conn.execute(insert(K).values(business_id=biz["id"], number=c["number"], version=ver, created_by=actor,
                                             **{k: c[k] for k in keep}).returning(K)).mappings().one())
    conn.execute(update(K).where(K.c.id == c["id"]).values(status="voided", voided_at=func.now(),
                                                           void_reason=f"version {ver}", updated_at=func.now()))
    log(conn, biz["id"], "contract.voided", actor, c["id"], {"number": c["number"], "version": c["version"], "replaced_by": ver})
    log(conn, biz["id"], "contract.created", actor, row["id"], {"number": c["number"], "version": ver})
    return row


# ---------------------------------------------------------------- the owner hears it
def tell_owner(biz: dict, key: str, c: dict, *, dedupe: str, name: str, http=None, **more) -> bool:
    """A note on the owner's chat and the installed app, once (dedupe)."""
    from . import owner as OW
    lang = OW.lang_of(biz)
    text = _t(key, lang).format(name=_first(name) or "—", title=c["title"], number=c["number"], **more)
    with db.tx() as conn:
        a = OW.ask(conn, biz, "notice", text, {"text": text, "contract_id": str(c["id"])}, contact_id=c.get("contact_id"),
                   by="valeria", dedupe=dedupe, owner_says_go=True)
    if not a:
        return False
    OW.run_now(biz, a["id"], http=http)
    return True


# ---------------------------------------------------------------- a booking waits for it (and the deposit)
def settings(conn: Connection, business_id: UUID) -> dict:
    r = conn.execute(select(S).where(S.c.business_id == business_id)).mappings().first()
    return dict(r) if r else {"business_id": business_id, "require_signed": False, "require_deposit": False}


def save_settings(conn: Connection, biz: dict, actor: str, *, require_signed: bool, require_deposit: bool) -> dict:
    vals = {"require_signed": bool(require_signed), "require_deposit": bool(require_signed and require_deposit)}
    conn.execute(pg_insert(S).values(business_id=biz["id"], **vals)
                 .on_conflict_do_update(index_elements=[S.c.business_id], set_={**vals, "updated_at": func.now()}))
    log(conn, biz["id"], "contract.settings", actor, biz["id"], vals)
    return settings(conn, biz["id"])


def deposit_paid(conn: Connection, c: dict) -> bool:
    """The deposit is in: marked by the owner, its pay link paid, or the invoice it came from paid."""
    if c.get("deposit_paid_at") or not (c.get("deposit_minor") or 0):
        return True
    if c.get("payment_id") and conn.execute(select(db.payments.c.status).where(db.payments.c.id == c["payment_id"])).scalar() == "paid":
        return True
    return bool(c.get("invoice_id")) and conn.execute(select(db.invoices.c.status).where(db.invoices.c.id == c["invoice_id"])).scalar() == "paid"


def gate(conn: Connection, business_id: UUID, appointment_id) -> str | None:
    """What still stops this booking from being confirmed: None, "unsigned" or "deposit" (when the owner asked for it)."""
    st = settings(conn, business_id)
    if not st["require_signed"]:
        return None
    rows = [dict(r) for r in conn.execute(select(K).where(K.c.business_id == business_id, K.c.appointment_id == appointment_id,
                                                         K.c.status == "signed")).mappings()]
    if not rows:
        return "unsigned"
    if st["require_deposit"] and not any(deposit_paid(conn, r) for r in rows):
        return "deposit"
    return None


def gate_problem(conn: Connection, business_id: UUID, appointment_id, lang: str) -> str | None:
    why = gate(conn, business_id, appointment_id)
    return _t(f"ct_gate_why_{why}", lang) if why else None


def confirm_ready(conn: Connection, biz: dict, c: dict) -> bool:
    """A signed contract for a booking (and its deposit, when asked for): the booking is confirmed."""
    if c["status"] != "signed" or not c["appointment_id"] or not settings(conn, biz["id"])["require_signed"]:
        return False
    AP = db.appointments
    if conn.execute(select(AP.c.status).where(AP.c.id == c["appointment_id"])).scalar() != "booked":
        return False
    if gate(conn, biz["id"], c["appointment_id"]):
        return False
    conn.execute(update(AP).where(AP.c.id == c["appointment_id"], AP.c.status == "booked").values(status="confirmed",
                                                                                               updated_at=func.now()))
    log(conn, biz["id"], "appointment.confirmed", "contract", c["appointment_id"], {"via": "contract", "contract_id": str(c["id"])})
    return True


def mark_deposit(conn: Connection, biz: dict, actor: str, contract_id) -> dict:
    """The owner got the deposit another way (cash, a transfer)."""
    c = get(conn, biz["id"], contract_id)
    if c["status"] == "voided":
        raise Invalid(_t("ct_cant_change", biz["language"]), "status")
    conn.execute(update(K).where(K.c.id == c["id"]).values(deposit_paid_at=func.now(), updated_at=func.now()))
    log(conn, biz["id"], "contract.deposit_paid", actor, c["id"], {"how": "marked"})
    c = get(conn, biz["id"], c["id"])
    confirm_ready(conn, biz, c)
    return c


def on_event(conn: Connection, business_id: UUID, type_: str, subject_id: UUID | None, data: dict) -> None:
    """A pay link or an invoice paid: the deposit of the contract it belongs to is in (and its booking may be confirmed)."""
    if type_ not in ("payment.paid", "invoice.paid") or not subject_id:
        return
    col = K.c.payment_id if type_ == "payment.paid" else K.c.invoice_id
    rows = [dict(r) for r in conn.execute(update(K).where(K.c.business_id == business_id, col == subject_id,
                                                         K.c.deposit_paid_at.is_(None)).values(deposit_paid_at=func.now())
                                          .returning(K)).mappings()]
    if not rows:
        return
    biz = dict(conn.execute(select(db.businesses).where(db.businesses.c.id == business_id)).mappings().one())
    for c in rows:
        confirm_ready(conn, biz, c)


# ---------------------------------------------------------------- the jobs round: one reminder after 24 h
def round_(now: datetime | None = None, http=None) -> dict:
    """Unsigned 24 h after it was sent: one reminder to the customer (the same channel rules, another channel when that
    one's window is closed); when none can go, the owner is told once, with the link to share."""
    now = now or datetime.now(timezone.utc)
    stats = {"reminded": 0, "told_owner": 0}
    with db.tx() as conn:
        due = [dict(r) for r in conn.execute(select(K).where(K.c.status.in_(OPEN), K.c.reminded_at.is_(None),
                                                             K.c.sent_at <= now - REMIND_AFTER).limit(200)).mappings()]
        for c in due:                                          # claimed first: a reminder never goes twice
            conn.execute(update(K).where(K.c.id == c["id"]).values(reminded_at=now))
        bizs = {r["id"]: dict(r) for r in conn.execute(select(db.businesses).where(
            db.businesses.c.id.in_({c["business_id"] for c in due}))).mappings()} if due else {}
    for c in due:
        biz = bizs[c["business_id"]]
        if biz.get("suspended_at"):
            continue
        with db.tx() as conn:
            contact = _contact(conn, biz["id"], c["contact_id"])
        why = "no_contact"
        if contact and not contact.get("deleted_at"):
            text = _t("ct_remind", c["lang"]).format(name=_first(contact["name"]), business=biz["name"], title=c["title"], link=link(c))
            ch, why = _deliver(biz, "contract", contact["id"], text, http=http, now=now)
            if ch:
                stats["reminded"] += 1
                with db.tx() as conn:
                    log(conn, biz["id"], "contract.reminded", "contract", c["id"], {"channel": ch})
                continue
        from . import owner as OW
        reason = _t(f"r_{why}", OW.lang_of(biz)) if why and why.isidentifier() and _t(f"r_{why}", "en") != f"r_{why}" else (why or "—")
        if tell_owner(biz, "ct_ping_remind_failed", c, dedupe=f"ctr:remind:{c['id']}",
                      name=(contact or {}).get("name") or (c["fields"] or {}).get("client") or "—", why=reason, link=link(c), http=http):
            stats["told_owner"] += 1
    return stats
