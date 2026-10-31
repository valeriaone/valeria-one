"""Our agreement with every business (agr01, v0.25): the provider — whoever runs this server — and each business that uses
it sign one written agreement, in plain words, for every trade in every country.

Valeria One is self-hosted (AGPL): the provider is each server's operator, never a name written in the code. The operator
fills in who they are (legal name, address, country, contact email), the law and courts for disputes, how many days after an
account closes its data is deleted, the other companies that handle data for them (sub-processors) and any extra terms, on
the Server page (/app/server-agreement). Until those are filled in AND a version is published, the agreement is DORMANT:
nobody is asked anything. Publishing freezes the words in every language and their SHA-256 (a database trigger refuses any
change); new details mean a new version.

The agreement: the service and its price (free, unless this server charges — then the Billing page's prices), the
business's own accounts (WhatsApp, Meta, email, payments, AI: theirs, and so are those costs), their data is theirs (export
any time, deleted after the stated days, never sold), a data processing annex (the business is the controller of its
customers' personal data, the provider the processor acting only on its documented instructions; confidentiality, security,
sub-processors, help with customers' requests, breach notice without undue delay, deletion or return at the end, checks on
reasonable request, data in other countries), no guarantees and a liability limit, ending at any time (export first), the
law and courts from the operator's settings, electronic signature, and changes (a new version is asked again; old ones are
kept). English is the canonical text; each language has its own display text; what is signed is exactly the text shown, in
its language, with its own SHA-256.

The owner signs (ticks "I agree, for <business>", types their full name, draws the signature — or the typed name only when
the pad can't be used). A business made after a version was published is asked at once; an existing one gets `grace_days`
("Later") from the first time its owner is asked, then its owner's /app pages wait for it. Team members are never blocked.
Customers, the public site, bookings, signing links, calendars, webhooks, the API and the background jobs never see any of it.

Sample wording only: the operator's screen says to have a lawyer review it. The words never cite a law.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import func, insert, select, update
from sqlalchemy import text as sql
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection

from .. import config, db
from . import Invalid, NotFound, log

A, ACC, ASK = db.provider_agreements, db.agreement_acceptances, db.agreement_asks
KV_KEY = "agreement"
REQUIRED = ("legal_name", "address", "country", "email", "law", "delete_days", "subprocessors")
DEFAULTS = {"legal_name": "", "address": "", "country": "", "email": "", "law": "", "courts": "", "delete_days": 30,
            "subprocessors": [], "extra": "", "grace_days": 14}
SECTIONS = ("parties", "service", "accounts", "data", "noguarantee", "ending", "law", "esign", "changes",
            "annex", "roles", "staff", "security", "subs", "requests", "breach", "end_data", "audit", "transfer")
MAX_SUBS = 30
METHODS = ("drawn", "typed")
BLANK = "________"
_FIELD = re.compile(r"\{(provider|provider_address|provider_country|provider_email|price|export|days|subprocessors|law|courts|"
                    r"version|date)\}")


def _t(key: str, lang: str | None) -> str:
    from .owner import t
    return t(key, lang)


def _langs() -> tuple[str, ...]:
    from ..web.i18n import UI_LANGS
    return UI_LANGS


def _ui(lang: str | None) -> str:
    from ..web.i18n import ui_lang
    return ui_lang(lang)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------- the operator's details (instance_kv)
def settings(conn: Connection) -> dict:
    from . import instance
    return {**DEFAULTS, **(instance.kv_get(conn, KV_KEY) or {})}


def missing(s: dict) -> list[str]:
    """The details still to fill in before a version can be published."""
    return [k for k in REQUIRED if not s.get(k)]


def _int(v, lo: int, hi: int, field: str) -> int:
    try:
        n = int(str(v).strip())
    except (TypeError, ValueError):
        raise Invalid(f"{field}: a whole number from {lo} to {hi}", field) from None
    if not lo <= n <= hi:
        raise Invalid(f"{field}: a whole number from {lo} to {hi}", field)
    return n


def save_settings(conn: Connection, actor: str, f: dict) -> dict:
    """The operator's details. Saving never changes a published version (that takes publishing a new one)."""
    from .. import packs
    from . import instance, partner
    s = settings(conn)
    one = lambda k, n: " ".join(str(f.get(k) or "").split())[:n]            # noqa: E731
    s["legal_name"], s["courts"], s["law"] = one("legal_name", 200), one("courts", 300), one("law", 300)
    s["address"] = str(f.get("address") or "").replace("\r\n", "\n").strip()[:500]
    country = str(f.get("country") or "").strip().upper()
    if country and country not in packs.open_codes():
        raise Invalid("country: pick one from the list", "country")
    s["country"] = country
    email = str(f.get("email") or "").strip()[:320]
    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise Invalid("contact email: an email address", "email")
    s["email"] = email
    s["delete_days"] = _int(f.get("delete_days") or DEFAULTS["delete_days"], 1, 3650, "delete_days")
    s["grace_days"] = _int(f.get("grace_days") if str(f.get("grace_days") or "").strip() else DEFAULTS["grace_days"], 0, 365, "grace_days")
    subs = [" ".join(x.split())[:300] for x in str(f.get("subprocessors") or "").splitlines()]
    s["subprocessors"] = [x for x in subs if x][:MAX_SUBS]
    s["extra"] = str(f.get("extra") or "").replace("\r\n", "\n").strip()[:6000]
    instance.kv_set(conn, KV_KEY, s)
    partner.audit(conn, "agreement.settings", actor, {"missing": missing(s)})
    return s


# ---------------------------------------------------------------- the words
def _country_name(code: str, lang: str) -> str:
    from babel import Locale
    try:
        return Locale.parse(lang).territories.get(code) or code
    except Exception:                                      # noqa: BLE001
        return code


def _section(key: str, lang: str) -> dict:
    return {"key": key, "title": _t(f"ag_s_{key}", lang), "text": _t(f"ag_s_{key}_t", lang)}


def compose(s: dict, lang: str, *, version: int, date: str, paid: bool) -> dict:
    """One language's text of a version: {"title", "body"} — every detail filled in except {business}, which is the
    signing business's own name, put in when it is shown to it."""
    from .contracts import text_of
    lang = _ui(lang)
    secs = [_section(k, lang) for k in SECTIONS]
    if s.get("extra"):
        secs.insert(SECTIONS.index("changes") + 1, {"key": "extra", "title": _t("ag_s_extra", lang), "text": s["extra"]})
    vals = {"provider": s.get("legal_name"), "provider_address": ", ".join(" ".join(x.split()) for x in (s.get("address") or "").splitlines() if x.strip()),
            "provider_country": _country_name(s["country"], lang) if s.get("country") else "",
            "provider_email": s.get("email"), "export": f"{config.PUBLIC_URL()}/app/export",
            "days": str(s.get("delete_days") or ""), "law": s.get("law"),
            "courts": s.get("courts") or _t("ag_courts_default", lang),
            "subprocessors": "\n".join(f"- {x}" for x in s.get("subprocessors") or []),
            "price": _t("ag_price_paid" if paid else "ag_price_free", lang), "version": str(version), "date": date}
    fill = lambda w: _FIELD.sub(lambda m: str(vals.get(m.group(1)) or "").strip() or BLANK, w or "")   # noqa: E731
    body = fill(_t("ag_doc_meta", lang)) + "\n\n" + text_of([{**x, "title": fill(x["title"]), "text": fill(x["text"])} for x in secs])
    return {"title": fill(_t("ag_doc_title", lang)), "body": body}


def build(s: dict, *, version: int, date: str, paid: bool) -> dict:
    return {lang: compose(s, lang, version=version, date=date, paid=paid) for lang in _langs()}


def _provider(s: dict) -> dict:
    return {**{k: s.get(k) for k in ("legal_name", "address", "country", "email", "law", "courts", "delete_days",
                                     "subprocessors", "extra")}, "price": "paid" if _paid() else "free"}


def canonical(version: int, provider: dict, texts: dict) -> str:
    return json.dumps({"version": version, "provider": provider, "texts": texts}, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))


def fingerprint(a: dict) -> str:
    """SHA-256 of a version: its number, the operator's details and the words in every language."""
    return hashlib.sha256(canonical(a["version"], a["provider"], a["texts"]).encode()).hexdigest()


def text_for(a: dict, lang: str | None, business_name: str) -> tuple[str, str]:
    """Exactly what a business is shown and signs: (language, text) — its own name in it."""
    lang = _ui(lang)
    texts = a["texts"] or {}
    if lang not in texts:
        lang = "en"
    put = lambda w: (w or "").replace("{business}", business_name or BLANK)  # noqa: E731
    one = texts[lang]
    return lang, f"# {put(one['title'])}\n\n{put(one['body'])}"


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def paragraphs(text: str) -> tuple[str, list[dict]]:
    """For the screens: (title, [{"h"} | {"p"}]) from a text made by text_for()."""
    from .contracts import paragraphs as paras
    first, _, rest = (text or "").partition("\n\n")
    return (first[2:] if first.startswith("# ") else first), paras(rest)


# ---------------------------------------------------------------- versions
def _paid() -> bool:
    from . import billing
    return billing.enabled()


def current(conn: Connection) -> dict | None:
    """The latest published version (None = dormant: nothing published on this server)."""
    r = conn.execute(select(A).order_by(A.c.version.desc()).limit(1)).mappings().first()
    return dict(r) if r else None


def current_meta(conn: Connection) -> dict | None:
    r = conn.execute(select(A.c.id, A.c.version, A.c.published_at, A.c.doc_hash).order_by(A.c.version.desc()).limit(1)).mappings().first()
    return dict(r) if r else None


def versions(conn: Connection) -> list[dict]:
    n = select(func.count()).select_from(ACC).where(ACC.c.agreement_id == A.c.id).scalar_subquery()
    return [dict(r) for r in conn.execute(select(A.c.id, A.c.version, A.c.doc_hash, A.c.published_by, A.c.published_at,
                                                 n.label("signed")).order_by(A.c.version.desc())).mappings()]


def version(conn: Connection, n) -> dict:
    try:
        n = int(n)
    except (TypeError, ValueError):
        raise NotFound("agreement") from None
    r = conn.execute(select(A).where(A.c.version == n)).mappings().first()
    if not r:
        raise NotFound("agreement")
    return dict(r)


def preview(conn: Connection) -> dict:
    """What the next version would say with today's details (gaps = a line to fill)."""
    cur = current_meta(conn)
    return build(settings(conn), version=(cur["version"] + 1) if cur else 1, date=_now().date().isoformat(), paid=_paid())


def changed(conn: Connection) -> bool:
    """Would publishing now say anything different from the published version (the details, the price, the words)?"""
    cur = current(conn)
    if not cur:
        return True
    s = settings(conn)
    return _provider(s) != cur["provider"] or build(s, version=cur["version"], date=cur["published_at"].date().isoformat(),
                                                     paid=_paid()) != cur["texts"]


def publish(conn: Connection, actor: str) -> dict:
    """Freeze the words in every language + their SHA-256 as the next version. Every business is then asked to sign it."""
    from . import partner
    conn.execute(sql("SELECT pg_advisory_xact_lock(hashtext('valeria-one:agreement'))"))   # two tabs, one new version
    s = settings(conn)
    if missing(s):
        raise Invalid("fill in the provider's details first: " + ", ".join(missing(s)), "missing")
    cur = current(conn)
    if cur and not changed(conn):
        raise Invalid(f"nothing changed since version {cur['version']}", "version")
    n = (cur["version"] + 1) if cur else 1
    provider, now = _provider(s), _now()
    texts = build(s, version=n, date=now.date().isoformat(), paid=_paid())
    row = conn.execute(insert(A).values(version=n, provider=provider, texts=texts, published_by=actor[:100], published_at=now,
                                        doc_hash=hashlib.sha256(canonical(n, provider, texts).encode()).hexdigest())
                       .returning(A)).mappings().one()
    partner.audit(conn, "agreement.published", actor, {"version": n, "doc_hash": row["doc_hash"]})
    return dict(row)


# ---------------------------------------------------------------- a business and the current version
def acceptance(conn: Connection, business_id: UUID, agreement_id: UUID) -> dict | None:
    r = conn.execute(select(ACC).where(ACC.c.business_id == business_id, ACC.c.agreement_id == agreement_id)).mappings().first()
    return dict(r) if r else None


def acceptances(conn: Connection, business_id: UUID) -> list[dict]:
    """Every version this business signed, newest first (kept for good)."""
    return [dict(r) for r in conn.execute(select(ACC).where(ACC.c.business_id == business_id)
                                          .order_by(ACC.c.version.desc())).mappings()]


def signed(conn: Connection, business_id: UUID, acceptance_id) -> dict:
    """One signed copy — only this business's own."""
    try:
        aid = UUID(str(acceptance_id))
    except ValueError:
        raise NotFound("agreement") from None
    r = conn.execute(select(ACC).where(ACC.c.id == aid, ACC.c.business_id == business_id)).mappings().first()
    if not r:
        raise NotFound("agreement")
    return dict(r)


def state(conn: Connection, biz: dict, now: datetime | None = None) -> dict | None:
    """Where this business stands with the current version — None while the agreement is dormant.
    need = not signed yet; new = made after it was published (asked at once, no "Later"); deadline = when "Later" ends
    (grace_days from the first time the owner was asked); blocked = past it."""
    if biz.get("sandbox"):
        return None
    cur = current_meta(conn)
    if not cur:
        return None
    now = now or _now()
    acc = conn.execute(select(ACC.c.id, ACC.c.accepted_at, ACC.c.signed_name).where(
        ACC.c.business_id == biz["id"], ACC.c.agreement_id == cur["id"])).mappings().first()
    if acc:
        return {**cur, "need": False, "accepted": dict(acc), "new": False, "ask": None, "deadline": None, "blocked": False,
                "can_later": False}
    ask = conn.execute(select(ASK).where(ASK.c.business_id == biz["id"], ASK.c.agreement_id == cur["id"])).mappings().first()
    new = bool(biz.get("created_at")) and biz["created_at"] >= cur["published_at"]
    if new:
        deadline = biz["created_at"]
    elif ask:
        deadline = ask["first_asked_at"] + timedelta(days=int(settings(conn)["grace_days"]))
    else:
        deadline = None                                     # the owner hasn't been asked yet: the grace starts then
    blocked = deadline is not None and now >= deadline
    return {**cur, "need": True, "accepted": None, "new": new, "ask": dict(ask) if ask else None, "deadline": deadline,
            "blocked": blocked, "can_later": not new and not blocked}


def asked(conn: Connection, biz: dict, agreement_id: UUID) -> None:
    """The owner saw it: the grace starts now (once per version)."""
    conn.execute(pg_insert(ASK).values(business_id=biz["id"], agreement_id=agreement_id).on_conflict_do_nothing())


def _current_for(conn: Connection, biz: dict, agreement_id) -> dict:
    cur = current(conn)
    if not cur:
        raise NotFound("agreement")
    if str(agreement_id or "") != str(cur["id"]):
        raise Invalid(_t("ag_newer", biz.get("language")), "agreement")
    return cur


def later(conn: Connection, biz: dict, actor: str, agreement_id) -> dict:
    cur = _current_for(conn, biz, agreement_id)
    st = state(conn, biz)
    if not st or not st["need"]:
        return st or {}
    if not st["can_later"]:
        raise Invalid(_t("ag_no_later", biz.get("language")), "later")
    asked(conn, biz, cur["id"])
    conn.execute(update(ASK).where(ASK.c.business_id == biz["id"], ASK.c.agreement_id == cur["id"])
                 .values(later_count=ASK.c.later_count + 1, last_later_at=func.now()))
    log(conn, biz["id"], "agreement.later", actor, cur["id"], {"version": cur["version"]})
    return state(conn, biz)


def decline(conn: Connection, biz: dict, actor: str, agreement_id) -> None:
    """'I don't agree': kept on record; nothing is deleted — the owner is shown how to take their data and leave."""
    cur = _current_for(conn, biz, agreement_id)
    asked(conn, biz, cur["id"])
    conn.execute(update(ASK).where(ASK.c.business_id == biz["id"], ASK.c.agreement_id == cur["id"]).values(declined_at=func.now()))
    log(conn, biz["id"], "agreement.declined", actor, cur["id"], {"version": cur["version"]})


def accept(conn: Connection, biz: dict, user: dict, *, agreement_id, lang: str, shown: str, name: str, agree: bool,
           signature: str, ip: str, ua: str) -> dict:
    """The owner signs the current version for their business: the exact text they were shown (its SHA-256 must still be
    the same), their typed full name, the drawn signature (or 'typed' when the pad couldn't be used). Signing twice = the
    first record."""
    from . import contracts
    cur = _current_for(conn, biz, agreement_id)
    old = acceptance(conn, biz["id"], cur["id"])
    if old:
        return old
    lang, text = text_for(cur, lang, biz["name"])
    h = sha(text)
    if shown and not secrets_eq(shown, h):
        raise Invalid(_t("ag_newer", lang), "agreement")
    if not agree:
        raise Invalid(_t("ag_need_agree", lang), "agree")
    name = " ".join((name or "").split())
    if len(name) < 3:
        raise Invalid(_t("ag_need_name", lang), "name")
    drawn = contracts.check_signature(signature) if (signature or "").strip() else None
    row = conn.execute(pg_insert(ACC).values(
        business_id=biz["id"], agreement_id=cur["id"], version=cur["version"], user_id=user["id"],
        signer_email=(user.get("email") or "")[:320], business_name=(biz["name"] or "")[:200], signed_name=name[:200],
        lang=lang, text=text, text_sha256=h, doc_hash=cur["doc_hash"], method="drawn" if drawn else "typed",
        signature=drawn, ip=(ip or "")[:64], ua=(ua or "")[:300])
        .on_conflict_do_nothing(constraint="uq_agreement_acceptance").returning(ACC)).mappings().first()
    if not row:                                            # two taps at once: the first one counts
        return acceptance(conn, biz["id"], cur["id"])
    row = dict(row)
    log(conn, biz["id"], "agreement.accepted", f"user:{user['id']}", row["id"],
        {"version": cur["version"], "signed_name": row["signed_name"], "text_sha256": h, "method": row["method"]})
    return row


def secrets_eq(a: str, b: str) -> bool:
    import secrets
    return secrets.compare_digest(str(a or ""), str(b or ""))


def mail_copy(row: dict, provider: dict) -> bool:
    """The signed copy to the person who signed (when the server's mail is set up; else it is only kept in the outbox)."""
    from . import mailer
    if not row.get("signer_email"):
        return False
    lang = row["lang"]
    rec = _t("ag_mail_record", lang).format(name=row["signed_name"], business=row["business_name"],
                                            at=row["accepted_at"].astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                                            sha=row["text_sha256"])
    try:
        return mailer.send(row["signer_email"], _t("ag_mail_subject", lang).format(provider=provider.get("legal_name") or ""),
                           row["text"].replace("\n## ", "\n") + "\n\n— \n" + rec + f"\n{config.PUBLIC_URL()}/app/agreement",
                           reply_to=provider.get("email") or None)
    except Exception:                                      # noqa: BLE001 — the signature is on record; mail never breaks it
        return False


# ---------------------------------------------------------------- the operator's list: who signed which version
def report(conn: Connection, limit: int = 1000) -> dict:
    """Every business on this server (demos left out) and where it stands with the current version."""
    B = db.businesses
    cur = current_meta(conn)
    rows = [dict(r) for r in conn.execute(select(B.c.id, B.c.name, B.c.country, B.c.created_at).where(B.c.sandbox.is_(False))
                                          .order_by(B.c.created_at.desc()).limit(limit)).mappings()]
    accs, asks = {}, {}
    if rows:
        ids = [r["id"] for r in rows]
        for a in conn.execute(select(ACC.c.business_id, ACC.c.version, ACC.c.signed_name, ACC.c.signer_email, ACC.c.accepted_at,
                                     ACC.c.method, ACC.c.id).where(ACC.c.business_id.in_(ids))
                              .order_by(ACC.c.version)).mappings():
            accs.setdefault(a["business_id"], []).append(dict(a))
        if cur:
            asks = {a["business_id"]: dict(a) for a in conn.execute(select(ASK).where(ASK.c.agreement_id == cur["id"],
                                                                                      ASK.c.business_id.in_(ids))).mappings()}
    grace = int(settings(conn)["grace_days"])
    for r in rows:
        mine = accs.get(r["id"], [])
        r["signed"] = [a["version"] for a in mine]
        r["current"] = next((a for a in mine if cur and a["version"] == cur["version"]), None)
        r["last"] = mine[-1] if mine else None
        r["ask"] = asks.get(r["id"])
        if cur and not r["current"]:
            new = r["created_at"] >= cur["published_at"]
            r["deadline"] = r["created_at"] if new else (r["ask"]["first_asked_at"] + timedelta(days=grace) if r["ask"] else None)
        else:
            r["deadline"] = None
    done = sum(1 for r in rows if r["current"])
    return {"current": cur, "rows": rows, "signed": done, "waiting": len(rows) - done if cur else 0}
