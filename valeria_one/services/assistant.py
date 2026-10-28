"""The website assistant: a chat bubble a business puts on its own website (any website, one script tag).

It answers visitors from the business's own facts (services, hours, address, finished work, its notes for the AI),
with the business's OWN AI key — and without a key it still works: it books a visit, shows the work, opens
WhatsApp and takes the visitor's details, which land in the inbox like a contact form. Nothing is kept from the
chat itself except a count; a visitor who leaves their details becomes a contact.
"""
from __future__ import annotations

from datetime import timedelta

import httpx
from sqlalchemy import func, select

from .. import blocks, config, db
from . import Invalid, log
from . import ai as AI

MAX_TURNS, MAX_CHARS = 12, 600
DAILY = 400                                        # answers per business per day: a bot can't empty the owner's AI credit


def facts(conn, biz: dict) -> str:
    s = biz.get("site") or {}
    lines = [AI._facts(conn, biz)]
    for k, label in (("headline", "About"), ("about", "About"), ("address", "Address"), ("phone", "Phone"), ("email", "Email")):
        if s.get(k):
            lines.append(f"{label}: {s[k]}")
    if s.get("whatsapp"):
        lines.append(f"WhatsApp: https://wa.me/{s['whatsapp'].lstrip('+')}")
    if s.get("preview") and blocks.on(biz, "studio"):
        lines.append(f"Visitors can upload a photo of their room and see it redesigned: {config.PUBLIC_URL()}/s/{biz['slug']}#preview")
    if blocks.on(biz, "projects"):
        from . import projects as PR
        work = PR.published(conn, biz["id"])
        if work:
            lines.append("Finished work: " + "; ".join(f"{w['name']}" + (f" ({w['style']})" if w["style"] else "") for w in work[:12])
                         + f" — {config.PUBLIC_URL()}/s/{biz['slug']}#work")
    return "\n".join(lines)


def answered_today(conn, business_id) -> int:
    E = db.events
    return conn.execute(select(func.count()).select_from(E).where(
        E.c.business_id == business_id, E.c.type == "assistant.answered", E.c.created_at > func.now() - timedelta(days=1))).scalar()


def ask(biz: dict, turns: list, http: httpx.Client | None = None) -> str | None:
    """-> the answer, or None when this business has no AI to ask (the widget then offers its buttons)."""
    clean = []
    for t in (turns or [])[-MAX_TURNS:]:
        if not isinstance(t, dict) or t.get("r") not in ("user", "assistant"):
            raise Invalid("bad message", "turns")
        txt = str(t.get("t") or "").strip()[:MAX_CHARS]
        if txt:
            clean.append(("user" if t["r"] == "user" else "assistant", txt))
    if not clean or clean[-1][0] != "user":
        raise Invalid("write a question", "turns")
    with db.tx() as conn:
        s = AI.settings(conn, biz["id"])
        if not blocks.on(biz, "ai") or not s or not (s["api_key"] or s["provider"] == "demo"):
            return None
        if answered_today(conn, biz["id"]) >= DAILY:
            return None
        try:
            prov, key, model = AI._credentials(conn, biz, s)
        except AI.AIError:
            return None
        system = (AI._system(conn, biz, s["instructions"], AI.detect_language(clean[-1][1]))
                  .replace("in a chat app", "in the chat on its website")
                  .replace("FACTS:\n" + AI._facts(conn, biz), "FACTS:\n" + facts(conn, biz))
                  + "\n\nWhen the visitor wants to go ahead or needs a person, invite them to book a visit or leave their WhatsApp "
                    "number in this chat. Never ask for card numbers or passwords.")
    out = None
    for m in [x.strip() for x in model.split(",") if x.strip()]:
        try:
            out = AI._call(prov, key, m, system, clean, AI._http(http))
            break
        except (httpx.HTTPError, AI.AIError) as e:
            if isinstance(e, AI.AIError) and not e.busy:
                break
    if not out:
        return None
    with db.tx() as conn:
        log(conn, biz["id"], "assistant.answered", "website", None, {"provider": prov})
    return out.replace(AI.HANDOFF, "").strip()[:1500]
