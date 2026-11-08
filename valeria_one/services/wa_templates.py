"""WhatsApp message templates (wati01, v0.29): written here, approved by Meta, picked from a list.

Outside the 24-hour window WhatsApp only lets a business send a template Meta approved. Until now the owner typed the
template's name by hand (and had to make it in Meta's own tool). Now, on the business's OWN WhatsApp account (the one it
connected — Embedded Signup or its own app; never ours):
  * write it here: name (Meta's rule: lowercase, digits, _), language (es_MX, en_US …), kind (MARKETING / UTILITY /
    AUTHENTICATION) and the text, with {{1}} for the customer's first name; a sample for each {{n}} (Meta asks for it);
  * "Send to Meta" → POST /{waba_id}/message_templates; Meta answers PENDING / APPROVED / REJECTED;
  * "Check with Meta" (and the jobs timer) reads every template on the account back: status, why it was rejected, and
    the ones made in Meta's own tool, so the list here is the whole truth;
  * campaigns and the inbox pick from the APPROVED ones, and the template goes out in the language Meta approved
    (a template sent in another language is refused by Meta).
Each screen offers the approved templates it can fill: campaigns, drips, leads, win-backs and the inbox know the
customer's first name ({{1}}); reminders and follow-ups also know the time ({{2}}). The sandbox has no Meta: a template sent there is approved at once, so the demo works end to end.
Twilio WhatsApp keeps its templates in Twilio's own console (said on the screen).
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection

from .. import crypto, db
from . import Invalid, NotFound, log

T, A = db.wa_templates, db.channel_accounts
NAME_RE = re.compile(r"^[a-z0-9_]{1,100}$")
LANG_RE = re.compile(r"^[a-z]{2,3}(_[A-Z]{2})?$")
CATEGORIES = ("UTILITY", "MARKETING", "AUTHENTICATION")
STATUSES = ("DRAFT", "PENDING", "APPROVED", "REJECTED", "PAUSED", "DISABLED", "ERROR")
_PH = re.compile(r"\{\{(\d+)\}\}")
MAX_BODY = 1024                                               # Meta's limit for a template's body
SYNC_EVERY = 5                                                # minutes between reading a PENDING template back from Meta
CHANNELS = ("whatsapp", "sandbox", "whatsapp_twilio")         # gap05 (v0.42.0): Twilio's WhatsApp templates are made here too
CONTENT = "https://content.twilio.com/v1/Content"             # Twilio's Content API (templates + WhatsApp approval)
MAX_BUTTONS, MAX_SHORT = 3, 60


def clean_extras(header: str = "", footer: str = "", buttons: str = "") -> dict:
    """gap05: a header and a footer (≤60 characters each), and up to 3 buttons — «Sí | No | Ver más» (quick replies) or one
    link button «Ver catálogo | https://…»."""
    out: dict = {}
    if (header or "").strip():
        if len(header.strip()) > MAX_SHORT or _PH.search(header):
            raise Invalid(f"header: up to {MAX_SHORT} characters, no {{{{n}}}}", "header")
        out["header"] = header.strip()
    if (footer or "").strip():
        if len(footer.strip()) > MAX_SHORT:
            raise Invalid(f"footer: up to {MAX_SHORT} characters", "footer")
        out["footer"] = footer.strip()
    parts = [x.strip() for x in (buttons or "").split("|") if x.strip()]
    if parts:
        if len(parts) == 2 and parts[1].startswith("https://"):
            out["buttons"] = [{"type": "url", "text": parts[0][:25], "url": parts[1][:2000]}]
        else:
            if len(parts) > MAX_BUTTONS:
                raise Invalid(f"buttons: up to {MAX_BUTTONS}, separated by |", "buttons")
            out["buttons"] = [{"type": "quick", "text": x[:25]} for x in parts]
    return out


def _meta_components(t: dict) -> list[dict]:
    ex = t.get("extras") or {}
    comps = []
    if ex.get("header"):
        comps.append({"type": "HEADER", "format": "TEXT", "text": ex["header"]})
    body = {"type": "BODY", "text": t["body"]}
    if t["example"]:
        body["example"] = {"body_text": [list(t["example"])]}
    comps.append(body)
    if ex.get("footer"):
        comps.append({"type": "FOOTER", "text": ex["footer"]})
    if ex.get("buttons"):
        comps.append({"type": "BUTTONS", "buttons": [
            {"type": "URL", "text": b["text"], "url": b["url"]} if b["type"] == "url" else {"type": "QUICK_REPLY", "text": b["text"]}
            for b in ex["buttons"]]})
    return comps


def _twilio_body(t: dict) -> str:
    return _PH.sub(lambda m: "{{" + m.group(1) + "}}", t["body"])


def _twilio_submit(acc: dict, t: dict, http) -> tuple[str, str | None, str | None]:
    """gap05: create the template in the owner's own Twilio (Content API) and ask WhatsApp to approve it.
    -> (status, content SID «HX…» or None, reason)."""
    import httpx
    cfg = crypto.open_(acc["config"] or {})
    auth = (cfg["account_sid"], cfg["auth_token"])
    h = http or httpx.Client(timeout=20)
    ex = t.get("extras") or {}
    body = (ex.get("header") + "\n\n" if ex.get("header") else "") + _twilio_body(t) + ("\n\n" + ex["footer"] if ex.get("footer") else "")
    if ex.get("buttons") and ex["buttons"][0]["type"] == "quick":
        types = {"twilio/quick-reply": {"body": body, "actions": [{"title": b["text"], "id": b["text"]} for b in ex["buttons"]]}}
    elif ex.get("buttons"):
        b = ex["buttons"][0]
        types = {"twilio/call-to-action": {"body": body, "actions": [{"type": "URL", "title": b["text"], "url": b["url"]}]}}
    else:
        types = {"twilio/text": {"body": body}}
    variables = {str(i + 1): v for i, v in enumerate(t["example"] or [])}
    r = h.post(CONTENT, auth=auth, json={"friendly_name": t["name"], "language": t["language"].split("_")[0],
                                         "variables": variables, "types": types})
    if r.status_code >= 300:
        return "ERROR", None, _twilio_err(r)
    sid = r.json().get("sid")
    a = h.post(f"{CONTENT}/{sid}/ApprovalRequests/whatsapp", auth=auth, json={"name": t["name"], "category": t["category"]})
    if a.status_code >= 300:
        return "ERROR", sid, _twilio_err(a)
    return "PENDING", sid, None


def _twilio_err(r) -> str:
    try:
        j = r.json()
        return str(j.get("message") or f"Twilio answered {r.status_code}")[:300]
    except ValueError:
        return f"Twilio answered {r.status_code}"


def _twilio_sync(biz: dict, acc: dict, http, now: datetime) -> dict:
    """gap05: each Twilio template we made, read back: WhatsApp's approval status and why it was rejected."""
    import httpx
    cfg = crypto.open_(acc["config"] or {})
    auth = (cfg["account_sid"], cfg["auth_token"])
    h = http or httpx.Client(timeout=20)
    out = {"updated": 0, "added": 0}
    with db.tx() as conn:
        rows = [dict(r) for r in conn.execute(select(T).where(T.c.channel_account_id == acc["id"], T.c.meta_id.isnot(None))).mappings()]
    for t in rows:
        r = h.get(f"{CONTENT}/{t['meta_id']}/ApprovalRequests", auth=auth)
        if r.status_code >= 300:
            continue
        wa = (r.json() or {}).get("whatsapp") or {}
        st = {"approved": "APPROVED", "rejected": "REJECTED", "pending": "PENDING", "received": "PENDING", "paused": "PAUSED",
              "disabled": "DISABLED"}.get(str(wa.get("status") or "").lower(), t["status"])
        why = wa.get("rejection_reason") or None
        with db.tx() as conn:
            conn.execute(update(T).where(T.c.id == t["id"]).values(status=st, reason=(str(why)[:300] if why else None),
                                                                    synced_at=now, updated_at=now))
        out["updated"] += 1
    with db.tx() as conn:
        log(conn, biz["id"], "wa_template.synced", "valeria", acc["id"], out)
    return out


def quality(biz: dict, account_id: UUID, http=None) -> dict:
    """gap05: the WhatsApp number's standing with Meta — quality rating (GREEN / YELLOW / RED) and how many people it may
    start chats with per day (the messaging tier). Official Cloud API numbers only."""
    from ..channels.meta import graph
    from .meta import _http
    with db.tx() as conn:
        acc = _account(conn, biz["id"], account_id)
    if acc["channel"] != "whatsapp":
        raise TemplateError("the quality rating is read from Meta for official WhatsApp (Cloud API) numbers")
    cfg = crypto.open_(acc["config"] or {})
    r = _http(http).get(f"{graph()}/{cfg['phone_number_id']}", headers={"Authorization": f"Bearer {cfg['token']}"},
                        params={"fields": "quality_rating,messaging_limit_tier,display_phone_number,verified_name"})
    if r.status_code >= 300:
        raise TemplateError(_meta_err(r))
    j = r.json()
    return {"quality": j.get("quality_rating") or "UNKNOWN", "tier": j.get("messaging_limit_tier") or "—",
            "number": j.get("display_phone_number") or "", "name": j.get("verified_name") or ""}


class TemplateError(Exception):
    pass


def placeholders(body: str) -> int:
    """How many {{n}} the text has; they must be {{1}}, {{2}} … with none skipped."""
    nums = sorted({int(n) for n in _PH.findall(body or "")})
    if nums and nums != list(range(1, len(nums) + 1)):
        raise Invalid("number the parts that change {{1}}, {{2}} … without skipping one", "body")
    return len(nums)


def default_language(biz: dict) -> str:
    lang = (biz.get("language") or "en").split("-")[0].split("_")[0].lower()
    country = (biz.get("country") or "").upper()
    return f"{lang}_{country}" if len(country) == 2 else lang


def accounts(conn: Connection, business_id: UUID) -> list[dict]:
    """The business's WhatsApp accounts that can hold templates here (official Cloud API with its WhatsApp account id, or
    the sandbox)."""
    out = []
    for r in conn.execute(select(A).where(A.c.business_id == business_id, A.c.active,
                                          A.c.channel.in_(CHANNELS)).order_by(A.c.created_at)).mappings():
        cfg = r["config"] or {}
        out.append({"id": r["id"], "name": r["name"], "channel": r["channel"], "waba_id": cfg.get("waba_id"),
                    "ready": r["channel"] in ("sandbox", "whatsapp_twilio") or bool(cfg.get("waba_id"))})   # gap05: Twilio too
    return out


def _account(conn: Connection, business_id: UUID, account_id: UUID) -> dict:
    r = conn.execute(select(A).where(A.c.business_id == business_id, A.c.id == account_id, A.c.active,
                                     A.c.channel.in_(CHANNELS))).mappings().first()
    if not r:
        raise NotFound("WhatsApp account")
    return dict(r)


def list_(conn: Connection, business_id: UUID, account_id: UUID | None = None) -> list[dict]:
    q = select(T).where(T.c.business_id == business_id)
    if account_id:
        q = q.where(T.c.channel_account_id == account_id)
    rows = [dict(r) for r in conn.execute(q.order_by(T.c.name, T.c.language)).mappings()]
    for r in rows:
        r["params"] = len({*_PH.findall(r["body"] or "")})
    return rows


def get(conn: Connection, business_id: UUID, tid: UUID) -> dict:
    r = conn.execute(select(T).where(T.c.business_id == business_id, T.c.id == tid)).mappings().first()
    if not r:
        raise NotFound("template")
    return dict(r)


def approved(conn: Connection, business_id: UUID, account_id: UUID | None = None, max_params: int = 1) -> list[dict]:
    """What a screen may send: approved, with no more {{n}} than that screen can fill."""
    return [r for r in list_(conn, business_id, account_id) if r["status"] == "APPROVED" and r["params"] <= max_params]


def for_sending(conn: Connection, business_id: UUID, account_id: UUID | None, name: str, fallback_lang: str,
                first_name: str | None, *, legacy_params: bool = True, more: list | None = None) -> dict:
    """The template dict the WhatsApp adapter sends: the language Meta approved, and exactly as many parameters as the
    template has (Meta refuses a different count). A name not in the list (typed by hand before v0.29) keeps the old way
    of its screen: campaigns sent the first name as {{1}} (legacy_params), the inbox sent none. `more` = what else the screen
    knows, in order ({{2}}, {{3}} …)."""
    q = select(T).where(T.c.business_id == business_id, T.c.name == name, T.c.status == "APPROVED")
    if account_id:
        q = q.where(T.c.channel_account_id == account_id)
    row = conn.execute(q.order_by(T.c.updated_at.desc()).limit(1)).mappings().first()
    if not row:
        return {"name": name, "language": fallback_lang,
                "params": [p for p in (first_name, *(more or [])) if p] if legacy_params else []}
    n = placeholders(row["body"])
    values = [first_name or "👋", *[str(v) for v in (more or []) if v]]
    ch = conn.execute(select(A.c.channel).where(A.c.id == row["channel_account_id"])).scalar()
    send_as = row["meta_id"] if ch == "whatsapp_twilio" and row["meta_id"] else name   # gap05: Twilio sends by its HX… SID
    return {"name": send_as, "language": row["language"], "params": values[:n]}


def save(conn: Connection, biz: dict, actor: str, *, account_id: UUID, name: str, language: str, category: str, body: str,
         example: str = "", tid: UUID | None = None, header: str = "", footer: str = "", buttons: str = "") -> dict:
    """Write or change a template that Meta hasn't approved yet (an approved one is changed in Meta, then read back)."""
    acc = _account(conn, biz["id"], account_id)
    name, language, category, body = (name or "").strip().lower(), (language or "").strip(), (category or "").strip().upper(), (body or "").strip()
    if not NAME_RE.match(name):
        raise Invalid("name: lowercase letters, numbers and _ only (for example: class_reminder)", "name")
    if not LANG_RE.match(language):
        raise Invalid("language: like es_MX, en_US or pt_BR", "language")
    if category not in CATEGORIES:
        raise Invalid("kind: UTILITY, MARKETING or AUTHENTICATION", "category")
    if not body or len(body) > MAX_BODY:
        raise Invalid(f"the text: 1 to {MAX_BODY} characters", "body")
    n = placeholders(body)
    samples = [s.strip() for s in (example or "").split("|")] if example else []
    samples = [s for s in samples if s][:n]
    if n and len(samples) < n:
        raise Invalid("Meta asks for an example for each {{n}} — separate them with |", "example")
    vals = {"channel_account_id": acc["id"], "name": name, "language": language, "category": category, "body": body,
            "example": samples, "extras": clean_extras(header, footer, buttons), "updated_at": datetime.now(timezone.utc)}
    if tid:
        cur = get(conn, biz["id"], tid)
        if cur["status"] in ("PENDING", "APPROVED", "PAUSED", "DISABLED"):
            raise Invalid("Meta already has this one; change it in Meta and tap «Check with Meta»", "body")
        row = conn.execute(update(T).where(T.c.id == tid).values(**vals, status="DRAFT", reason=None).returning(T)).mappings().one()
    else:
        if conn.execute(select(T.c.id).where(T.c.channel_account_id == acc["id"], T.c.name == name, T.c.language == language)).first():
            raise Invalid("there is already a template with this name and language", "name")
        row = conn.execute(pg_insert(T).values(business_id=biz["id"], **vals).returning(T)).mappings().one()
    log(conn, biz["id"], "wa_template.saved", actor, row["id"], {"name": name, "language": language})
    return dict(row)


def delete(conn: Connection, biz: dict, actor: str, tid: UUID) -> None:
    """Only one Meta doesn't have (a draft, a rejected one, an error) — an approved one is removed in Meta, then read back."""
    cur = get(conn, biz["id"], tid)
    if cur["status"] in ("PENDING", "APPROVED", "PAUSED", "DISABLED"):
        raise Invalid("Meta has this one; delete it in Meta and tap «Check with Meta»", "status")
    conn.execute(T.delete().where(T.c.id == tid))
    log(conn, biz["id"], "wa_template.deleted", actor, tid, {"name": cur["name"]})


# ---------------------------------------------------------------- Meta
def _graph(acc: dict, http):
    from ..channels.meta import graph
    from .meta import _http
    cfg = crypto.open_(acc["config"] or {})
    if not cfg.get("waba_id") or not cfg.get("token"):
        raise TemplateError("this WhatsApp has no WhatsApp account id (waba_id): reconnect it with Meta's button, "
                            "or add the id from Meta's WhatsApp Manager")
    return graph(), cfg["waba_id"], {"Authorization": f"Bearer {cfg['token']}"}, _http(http)


def _meta_err(r) -> str:
    try:
        e = r.json().get("error", {})
        return (e.get("error_user_msg") or e.get("message") or f"Meta answered {r.status_code}")[:300]
    except ValueError:
        return f"Meta answered {r.status_code}"


def submit(biz: dict, actor: str, tid: UUID, http=None, now: datetime | None = None) -> dict:
    """Send one template to Meta for approval. -> the row (status PENDING / APPROVED / REJECTED / ERROR)."""
    now = now or datetime.now(timezone.utc)
    with db.tx() as conn:
        t = get(conn, biz["id"], tid)
        if t["status"] in ("PENDING", "APPROVED"):
            return t
        acc = _account(conn, biz["id"], t["channel_account_id"])
    if acc["channel"] == "sandbox":
        status, meta_id, reason = "APPROVED", f"sandbox-{str(tid)[:8]}", None
    elif acc["channel"] == "whatsapp_twilio":                  # gap05: the owner's own Twilio (Content API)
        status, meta_id, reason = _twilio_submit(acc, t, http)
    else:
        base, waba, auth, h = _graph(acc, http)
        r = h.post(f"{base}/{waba}/message_templates", headers=auth,       # gap05: header, body, footer, buttons
                   json={"name": t["name"], "language": t["language"], "category": t["category"], "components": _meta_components(t)})
        if r.status_code < 300:
            j = r.json()
            status, meta_id, reason = (j.get("status") or "PENDING").upper(), str(j.get("id") or "") or None, None
            if status not in STATUSES:
                status = "PENDING"
        else:
            status, meta_id, reason = "ERROR", None, _meta_err(r)
    with db.tx() as conn:
        row = conn.execute(update(T).where(T.c.id == tid).values(status=status, meta_id=meta_id, reason=reason, submitted_at=now,
                                                                 synced_at=now, updated_at=now).returning(T)).mappings().one()
        log(conn, biz["id"], "wa_template.submitted", actor, tid, {"name": t["name"], "status": status})
    return dict(row)


def sync(biz: dict, account_id: UUID, http=None, now: datetime | None = None) -> dict:
    """Read every template on the account back from Meta: status, why rejected, and the ones made in Meta's own tool."""
    now = now or datetime.now(timezone.utc)
    with db.tx() as conn:
        acc = _account(conn, biz["id"], account_id)
    if acc["channel"] == "sandbox":
        return {"updated": 0, "added": 0}
    if acc["channel"] == "whatsapp_twilio":                    # gap05
        return _twilio_sync(biz, acc, http, now)
    base, waba, auth, h = _graph(acc, http)
    seen, after = [], None
    for _ in range(10):                                        # pages of 200 (Meta's maximum)
        params = {"fields": "id,name,language,status,category,rejected_reason,components", "limit": 200}
        if after:
            params["after"] = after
        r = h.get(f"{base}/{waba}/message_templates", headers=auth, params=params)
        if r.status_code >= 300:
            raise TemplateError(_meta_err(r))
        j = r.json()
        seen += j.get("data") or []
        paging = j.get("paging") or {}
        after = (paging.get("cursors") or {}).get("after") if paging.get("next") else None
        if not after:
            break
    out = {"updated": 0, "added": 0}
    with db.tx() as conn:
        for m in seen:
            name, lang = str(m.get("name") or ""), str(m.get("language") or "")
            if not NAME_RE.match(name) or not LANG_RE.match(lang):
                continue
            status = str(m.get("status") or "PENDING").upper()
            status = status if status in STATUSES else "PENDING"
            why = m.get("rejected_reason")
            why = None if why in (None, "", "NONE") else str(why)[:300]
            body = next((c.get("text") for c in m.get("components") or [] if str(c.get("type")).upper() == "BODY"), None) or ""
            cat = str(m.get("category") or "UTILITY").upper()
            cat = cat if cat in CATEGORIES else "UTILITY"
            cur = conn.execute(select(T).where(T.c.channel_account_id == account_id, T.c.name == name, T.c.language == lang)).mappings().first()
            if cur:
                conn.execute(update(T).where(T.c.id == cur["id"]).values(status=status, reason=why, meta_id=str(m.get("id") or cur["meta_id"] or "") or None,
                                                                          body=body or cur["body"], category=cat, synced_at=now, updated_at=now))
                out["updated"] += 1
            elif body:
                conn.execute(pg_insert(T).values(business_id=biz["id"], channel_account_id=account_id, name=name, language=lang,
                                                 category=cat, body=body[:MAX_BODY], status=status, reason=why,
                                                 meta_id=str(m.get("id") or "") or None, synced_at=now, updated_at=now))
                out["added"] += 1
        log(conn, biz["id"], "wa_template.synced", "valeria", account_id, out)
    return out


def sync_pending(http=None) -> int:
    """The jobs timer: every account with a template waiting for Meta is read back (Meta usually decides in minutes),
    at most every SYNC_EVERY minutes per template."""
    from datetime import timedelta
    from sqlalchemy import or_
    since = datetime.now(timezone.utc) - timedelta(minutes=SYNC_EVERY)
    with db.tx() as conn:
        rows = conn.execute(select(T.c.business_id, T.c.channel_account_id).where(
            T.c.status == "PENDING", or_(T.c.synced_at.is_(None), T.c.synced_at < since)).distinct()).all()
    n = 0
    for bid, aid in rows:
        with db.tx() as conn:
            biz = dict(conn.execute(select(db.businesses).where(db.businesses.c.id == bid)).mappings().one())
        try:
            sync(biz, aid, http)
            n += 1
        except Exception:                                      # noqa: BLE001 — one account's trouble never stops the others
            continue
    return n
