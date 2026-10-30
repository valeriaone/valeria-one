"""The owner's own forms: "Ask for a quote", "Join the waiting list", "Book a viewing".

The owner picks the fields (name always; phone, email, a message, and any of their own customer fields), which are
required, the thank-you words, and where a lead lands (a tag, a deal in a stage). Each form gets a public page at
/form/<slug> (the friend links already use /f/<code>), an embed snippet for the business's own website, and can sit as
a button on its Valeria One website and directory listing. The public page wears the business's own look.

A form sent = a customer (the one they already are when the phone or email matches), a conversation in the inbox
(source "form:<name>") with what they wrote, the owner's fields filled in, an optional deal, and the "form sent"
event for the recipes. Two separate boxes: "you may contact me about this request" (needed to send) and "send me
offers and news" (optional, never ticked for them) — agreeing to be answered is not agreeing to promotions.
"""
from __future__ import annotations

import re
import secrets
import unicodedata
from uuid import UUID

from sqlalchemy import func, insert, select, update
from sqlalchemy.engine import Connection

from .. import config, db, packs
from . import Invalid, NotFound, log

FM, FS = db.forms, db.form_submissions
BASICS = ("name", "phone", "email", "message")              # what every business has; custom fields come as cf:<key>
MAX_FORMS = 30


def _slugify(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")[:40] or "form"


def _free_slug(conn: Connection, biz: dict, name: str) -> str:
    base = "-".join(x for x in (_slugify(biz.get("slug") or biz["name"])[:30], _slugify(name)) if x)[:70]
    for i in range(20):
        slug = base if i == 0 else f"{base}-{secrets.token_hex(2)}"
        if not conn.execute(select(FM.c.id).where(FM.c.slug == slug)).first():
            return slug
    return f"{base[:60]}-{secrets.token_hex(4)}"


def list_(conn: Connection, business_id: UUID, *, on_site: bool = False) -> list[dict]:
    q = select(FM).where(FM.c.business_id == business_id)
    if on_site:
        q = q.where(FM.c.on_site, FM.c.active)
    return [dict(r) for r in conn.execute(q.order_by(FM.c.created_at)).mappings()]


def get(conn: Connection, business_id: UUID, form_id: UUID) -> dict:
    r = conn.execute(select(FM).where(FM.c.business_id == business_id, FM.c.id == form_id)).mappings().first()
    if not r:
        raise NotFound("form")
    return dict(r)


def public(conn: Connection, slug: str) -> tuple[dict, dict]:
    """The form and its business, for the public page. NotFound when it's off or the business is paused."""
    r = conn.execute(select(FM).where(FM.c.slug == (slug or "").lower()[:80], FM.c.active)).mappings().first()
    if not r:
        raise NotFound("form")
    biz = dict(conn.execute(select(db.businesses).where(db.businesses.c.id == r["business_id"])).mappings().one())
    if biz["suspended_at"]:
        raise NotFound("form")
    return dict(r), biz


def url(form: dict) -> str:
    return f"{config.PUBLIC_URL()}/form/{form['slug']}"


def embed(form: dict, title: str = "") -> str:
    """What the owner pastes into their own website."""
    t = (title or form["name"]).replace('"', "'")
    return (f'<iframe src="{url(form)}?embed=1" title="{t}" loading="lazy" '
            f'style="width:100%;max-width:560px;min-height:640px;border:0"></iframe>')


def _clean(conn: Connection, biz: dict, data: dict) -> dict:
    from . import fields as CF
    name = " ".join(str(data.get("name") or "").split())[:120]
    if not name:
        raise Invalid("give the form a name", "name")
    custom = {f["key"]: f for f in CF.list_(conn, biz["id"], "contact")}
    picked, seen = [], set()
    for x in data.get("fields") or []:
        k = str((x or {}).get("key") or "")
        if k in seen or not (k in BASICS or (k.startswith("cf:") and k[3:] in custom)):
            continue
        seen.add(k)
        picked.append({"key": k, "required": bool(x.get("required")) or k == "name"})
    if "name" not in seen:
        picked.insert(0, {"key": "name", "required": True})
    if not ({"phone", "email"} & seen):
        raise Invalid("ask for a phone or an email, so you can answer", "fields")
    stage = (data.get("stage") or "").strip() or None
    if stage and not packs.industry(biz["industry"]).stage(stage):
        raise Invalid("pick a stage", "stage")
    tag = str(data.get("tag") or "").strip().lower()[:60] or None
    return {"name": name, "intro": str(data.get("intro") or "").strip()[:500], "fields": picked,
            "thanks": str(data.get("thanks") or "").strip()[:500], "stage": stage, "tag": tag,
            "on_site": bool(data.get("on_site")), "active": bool(data.get("active", True))}


def save(conn: Connection, biz: dict, actor: str, data: dict, form_id: UUID | None = None) -> dict:
    vals = _clean(conn, biz, data)
    if form_id:
        get(conn, biz["id"], form_id)
        conn.execute(update(FM).where(FM.c.business_id == biz["id"], FM.c.id == form_id).values(**vals, updated_at=func.now()))
        log(conn, biz["id"], "form.updated", actor, form_id)
        return get(conn, biz["id"], form_id)
    if len(list_(conn, biz["id"])) >= MAX_FORMS:
        raise Invalid(f"at most {MAX_FORMS} forms", "name")
    row = conn.execute(insert(FM).values(business_id=biz["id"], slug=_free_slug(conn, biz, vals["name"]), **vals)
                       .returning(FM)).mappings().one()
    log(conn, biz["id"], "form.created", actor, row["id"])
    return dict(row)


def delete(conn: Connection, biz: dict, actor: str, form_id: UUID) -> None:
    get(conn, biz["id"], form_id)
    conn.execute(FM.delete().where(FM.c.business_id == biz["id"], FM.c.id == form_id))
    log(conn, biz["id"], "form.deleted", actor, form_id)


def shown(conn: Connection, biz: dict, form: dict) -> list[dict]:
    """The form's fields as the public page draws them: {key, label?, kind, required, options}."""
    from . import fields as CF
    custom = {f["key"]: f for f in CF.list_(conn, biz["id"], "contact")}
    out = []
    for x in form["fields"]:
        k = x["key"]
        if k in BASICS:
            out.append({"key": k, "kind": k, "required": x["required"], "options": []})
        elif k.startswith("cf:") and k[3:] in custom:
            f = custom[k[3:]]
            out.append({"key": k, "label": f["label"], "kind": f["kind"], "required": x["required"], "options": f["options"]})
    return out


def submit(conn: Connection, biz: dict, form: dict, answers: dict, *, request_consent: bool, marketing_consent: bool,
           lang: str = "en") -> dict:
    """Someone sent the form: the customer (deduped by phone/email), the inbox, their fields, a deal, the event."""
    from . import contacts as C
    from . import deals as D
    from . import fields as CF
    from . import site as ST
    from ..web.i18n import t
    if not request_consent:
        raise Invalid(t("fm_need_consent", lang), "consent")
    shown_fields = shown(conn, biz, form)
    custom, lines = {}, []
    for f in shown_fields:
        v = str(answers.get(f["key"]) or "").strip()
        if f["required"] and not v:
            raise Invalid(t("fm_required", lang).format(field=f.get("label") or t(f["key"], lang)), f["key"])
        if f["key"].startswith("cf:") and v:
            cf = next(x for x in CF.list_(conn, biz["id"], "contact") if x["key"] == f["key"][3:])
            custom[cf["key"]] = CF.clean_value(biz, cf, v)            # checked by its type before anything is kept
            lines.append(f"{cf['label']}: {CF.display(biz, cf, custom[cf['key']], lang)}")
    msg = str(answers.get("message") or "").strip()[:2000]
    body = "\n".join([f"📝 {form['name']}", *([msg] if msg else []), *lines, "✓ " + t("fm_consent_line", ui(biz))]).strip()
    source = f"form:{form['name']}"[:60]
    res = ST.contact_form(conn, biz, name=str(answers.get("name") or ""), phone=str(answers.get("phone") or ""),
                          email=str(answers.get("email") or ""), message=body, source=source, event="lead.form")
    contact = res["contact"]
    patch: dict = {}
    if custom:
        patch["custom"] = custom
    if form["tag"] and form["tag"] not in (contact["tags"] or []):
        patch["tags"] = [*(contact["tags"] or []), form["tag"]]
    if marketing_consent and not contact["marketing_consent"]:
        patch.update(marketing_consent=True, marketing_consent_source=f"{source} ({url(form)})"[:200])
    if patch:
        contact = C.patch(conn, biz, source, contact["id"], patch)
    deal = None
    if form["stage"]:
        deal = D.create(conn, biz, source, {"title": f"{form['name']} · {contact['name'] or contact['phone'] or contact['email']}"[:200],
                                            "contact_id": contact["id"], "stage": form["stage"]})
    sub = conn.execute(insert(FS).values(business_id=biz["id"], form_id=form["id"], contact_id=contact["id"],
                                         deal_id=deal["id"] if deal else None, request_consent=True,
                                         marketing_consent=bool(marketing_consent),
                                         data={k: str(answers.get(k) or "")[:2000] for k in (f["key"] for f in shown_fields)})
                       .returning(FS.c.id)).scalar()
    conn.execute(update(FM).where(FM.c.id == form["id"]).values(submissions=FM.c.submissions + 1))
    log(conn, biz["id"], "form.submitted", source, contact["id"],
        {"form_id": str(form["id"]), "form": form["name"], "submission_id": str(sub), "deal_id": str(deal["id"]) if deal else None,
         "conversation_id": str(res["conversation_id"]), "request_consent": True, "marketing_consent": bool(marketing_consent)})
    return {"contact": contact, "deal": deal, "conversation_id": res["conversation_id"], "submission_id": sub}


def ui(biz: dict) -> str:
    from ..web.i18n import ui_lang
    return ui_lang(biz["language"])


def submissions(conn: Connection, business_id: UUID, form_id: UUID, limit: int = 20) -> list[dict]:
    C = db.contacts
    return [dict(r) for r in conn.execute(select(FS, C.c.name.label("contact_name")).outerjoin(C, C.c.id == FS.c.contact_id)
                                          .where(FS.c.business_id == business_id, FS.c.form_id == form_id)
                                          .order_by(FS.c.created_at.desc()).limit(limit)).mappings()]
