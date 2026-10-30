"""Valeria inside the office and the studio: an assistant for the people who run the business.

It knows the business (clients, projects, money, agenda, suppliers, library) by looking it up with tools, answers
anything else a professional would ask (design, writing, maths, translations), and can DO things. Reading is
free; anything that changes data or sends something comes back as a proposed action the person taps to confirm
(human in the loop). It works with whichever AI the business connected (Gemini, Claude or OpenAI) because tools
are a plain JSON protocol, not one provider's function-calling dialect. The business's own key pays.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from uuid import UUID
from zoneinfo import ZoneInfo

import httpx
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy import func, select

from .. import blocks, config, db, money, packs
from . import Invalid, NotFound
from . import ai as AI

MAX_TURNS, MAX_CHARS, MAX_ROUNDS = 16, 4000, 4
READ = ("today", "find_clients", "client", "projects", "project", "late_orders", "open_tasks", "money", "library", "suppliers")
WRITE = ("create_task", "add_note", "create_project", "add_piece", "set_stage", "book_visit", "message_client")


def _fmt(biz):
    loc = packs.country(biz["country"]).locale
    return lambda v: "—" if v is None else money.fmt(int(v), biz["currency"], loc)


def _id(v):
    try:
        return UUID(str(v))
    except (TypeError, ValueError):
        raise Invalid("that id isn't right", "id") from None


# ---------------------------------------------------------------- what it can look up
def run_read(biz: dict, tool: str, args: dict) -> object:
    from . import contacts as C
    from . import deals as D
    from . import projects as PR
    fm = _fmt(biz)
    tz = ZoneInfo(biz["timezone"])
    with db.tx() as conn:
        if tool == "today":
            now = datetime.now(tz)
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            appts = D.appointments(conn, biz["id"], start=start, end=start + timedelta(days=int(args.get("days") or 1)))
            CV = db.conversations
            waiting = conn.execute(select(func.count()).select_from(CV).where(CV.c.business_id == biz["id"], CV.c.status == "open",
                                                                              CV.c.last_inbound_at.is_not(None))).scalar()
            return {"now": now.isoformat(timespec="minutes"), "appointments": [
                {"title": a["title"], "starts": a["starts_at"].astimezone(tz).isoformat(timespec="minutes"), "status": a["status"]} for a in appts],
                "open_conversations": waiting}
        if tool == "find_clients":
            return [{"id": str(c["id"]), "name": c["name"], "phone": c["phone"], "email": c["email"]}
                    for c in C.search(conn, biz["id"], q=str(args.get("q") or "")[:80] or None, limit=10)]
        if tool == "client":
            from . import fields as CF
            c = C.get(conn, biz["id"], _id(args.get("id")))
            ps = PR.search(conn, biz["id"], status=None, contact_id=c["id"])
            ds = D.search(conn, biz["id"], contact_id=c["id"], limit=10)
            return {"id": str(c["id"]), "name": c["name"], "phone": c["phone"], "email": c["email"], "notes": c["notes"][:500],
                    "tags": c["tags"], "fields": CF.readable(conn, biz, "contact", c["custom"]),     # the owner's own fields
                    "deals": [{"title": d["title"], "stage": d["stage"], "amount": fm(d["amount_minor"]),
                               "fields": CF.readable(conn, biz, "deal", d["custom"])} for d in ds],
                    "projects": [{"id": str(p["id"]), "name": p["name"], "stage": p["stage"], "status": p["status"]} for p in ps]}
        if tool == "projects":
            st = args.get("status") or "active"
            return [{"id": str(p["id"]), "name": p["name"], "client": p["contact_name"], "stage": p["stage"]}
                    for p in PR.search(conn, biz["id"], status=st if st in ("active", "done", "cancelled") else "active", limit=50)]
        if tool == "project":
            p = PR.get(conn, biz["id"], _id(args.get("id")))
            t = PR.totals(conn, biz, p)
            return {"id": str(p["id"]), "name": p["name"], "stage": p["stage"], "stages": PR.stage_keys(biz), "status": p["status"],
                    "style": p["style"], "brief": p["brief"][:1500], "address": p["address"],
                    "budget": fm(t["budget"]), "approved": fm(t["approved"]), "waiting_for_client": fm(t["proposed"]),
                    "fee": fm(t["fee"]), "margin": fm(t["margin"]), "paid": fm(t["paid"]),
                    "rooms": [{"id": str(r["id"]), "name": r["name"], "cm": [r["width_cm"], r["length_cm"], r["height_cm"]]}
                              for r in PR.rooms(conn, biz["id"], p["id"])],
                    "pieces": [{"name": i["name"], "status": i["status"], "price": fm(i["price_minor"]), "qty": i["qty"]}
                               for i in PR.items(conn, biz["id"], p["id"])][:60],
                    "recent": [{"kind": s["kind"], "text": s["body"][:300], "at": s["created_at"].astimezone(tz).date().isoformat()}
                               for s in PR.story(conn, biz["id"], p["id"], limit=12)],
                    "portal": f"{config.PUBLIC_URL()}/portal/{p['token']}"}
        if tool == "late_orders":
            return [{"po": o["number"], "supplier": o["supplier_name"], "project": o["project_name"], "expected": o["expected_on"].isoformat()}
                    for o in PR.late_orders(conn, biz["id"])]
        if tool == "open_tasks":
            return [{"title": t["title"], "project": t["project_name"], "who": t["assignee_name"], "due": t["due_on"].isoformat() if t["due_on"] else None}
                    for t in PR.tasks(conn, biz["id"])][:40]
        if tool == "money":
            PY, Q = db.payments, db.quotes
            month = datetime.now(tz).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            paid = conn.execute(select(func.coalesce(func.sum(PY.c.amount_minor), 0)).where(
                PY.c.business_id == biz["id"], PY.c.status == "paid", PY.c.paid_at >= month)).scalar()
            pend = conn.execute(select(PY.c.description, PY.c.amount_minor).where(PY.c.business_id == biz["id"], PY.c.status == "pending")
                                .order_by(PY.c.created_at.desc()).limit(15)).all()
            qs = conn.execute(select(Q.c.number, Q.c.title, Q.c.total_minor, Q.c.status).where(Q.c.business_id == biz["id"])
                              .order_by(Q.c.created_at.desc()).limit(10)).all()
            return {"paid_this_month": fm(paid), "pending_payments": [{"for": d, "amount": fm(a)} for d, a in pend],
                    "recent_quotes": [{"n": n, "title": t, "total": fm(v), "status": s} for n, t, v, s in qs]}
        if tool == "library":
            return [{"id": str(c["id"]), "name": c["name"], "category": c["category"], "price": fm(c["price_minor"]),
                     "supplier": c["supplier_name"]} for c in PR.catalog(conn, biz["id"], q=str(args.get("q") or "")[:60], limit=30)]
        if tool == "suppliers":
            return [{"name": s["name"], "kind": s["kind"], "phone": s["phone"], "email": s["email"], "lead_days": s["lead_days"]}
                    for s in PR.suppliers(conn, biz["id"])][:40]
    raise Invalid("unknown tool", "tool")


# ---------------------------------------------------------------- what it can do (after the person confirms)
def _signer():
    return URLSafeTimedSerializer(config.get("VO_SECRET_KEY", required=True), salt="copilot-action")


def describe(biz: dict, tool: str, args: dict) -> str:
    """A plain line for the button: what will happen."""
    a = {k: str(v)[:80] for k, v in (args or {}).items()}
    return {"create_task": "Tarea: {title}", "add_note": "Nota en el proyecto: {text}", "create_project": "Proyecto nuevo: {name}",
            "add_piece": "Pieza: {name}", "set_stage": "Etapa → {stage}", "book_visit": "Cita: {title} · {starts}",
            "message_client": "Mensaje al cliente: {text}"}[tool].format_map(_Blank(a))


class _Blank(dict):
    def __missing__(self, k):
        return "—"


def sign(biz: dict, user_id, tool: str, args: dict) -> str:
    return _signer().dumps({"b": str(biz["id"]), "u": str(user_id) if user_id else None, "t": tool, "a": args})


def run_write(biz: dict, actor: str, user_id, token: str) -> str:
    """Do a confirmed action. -> what was done (for the chat)."""
    from . import contacts as C
    from . import deals as D
    from . import projects as PR
    try:
        d = _signer().loads(token, max_age=1800)
    except BadSignature:
        raise Invalid("that button expired; ask again", "action") from None
    if d.get("b") != str(biz["id"]) or d.get("u") != (str(user_id) if user_id else None) or d.get("t") not in WRITE:
        raise Invalid("that action isn't yours", "action")
    tool, a = d["t"], d.get("a") or {}
    with db.tx() as conn:
        if tool == "create_task":
            t = PR.add_task(conn, biz, actor, {"title": a.get("title"), "project_id": _id(a["project_id"]) if a.get("project_id") else None,
                                                "due_on": a.get("due") or None})
            return f"✓ {t['title']}"
        if tool == "add_note":
            PR.note(conn, biz, actor, _id(a.get("project_id")), str(a.get("text") or ""), shared=bool(a.get("client_sees")))
            return "✓"
        if tool == "create_project":
            p = PR.create(conn, biz, actor, {"name": a.get("name"), "contact_id": _id(a["client_id"]) if a.get("client_id") else None,
                                              "style": a.get("style"), "brief": a.get("brief"),
                                              "budget_minor": _amount(biz, a.get("budget"))})
            return f"✓ {p['name']} → /app/projects/{p['id']}"
        if tool == "add_piece":
            it = PR.add_item(conn, biz, actor, _id(a.get("project_id")), {"name": a.get("name"), "category": a.get("category") or "other",
                                                                          "price_minor": _amount(biz, a.get("price")), "qty": a.get("qty") or 1,
                                                                          "room_id": _id(a["room_id"]) if a.get("room_id") else None})
            return f"✓ {it['name']}"
        if tool == "set_stage":
            p = PR.patch(conn, biz, actor, _id(a.get("project_id")), {"stage": a.get("stage")})
            return f"✓ {p['name']} · {p['stage']}"
        if tool == "book_visit":
            tz = ZoneInfo(biz["timezone"])
            try:
                st = datetime.fromisoformat(str(a.get("starts"))).replace(tzinfo=None).replace(tzinfo=tz)
            except ValueError:
                raise Invalid("the date and time aren't clear", "starts") from None
            ap = D.book(conn, biz, actor, {"title": a.get("title") or "Visita", "starts_at": st,
                                           "ends_at": st + timedelta(minutes=int(a.get("minutes") or 60)),
                                           "contact_id": _id(a["client_id"]) if a.get("client_id") else None})
            return f"✓ {ap['title']} · {st.strftime('%Y-%m-%d %H:%M')}"
    if tool == "message_client":
        from . import inbox as X
        cid = _id(a.get("client_id"))
        with db.tx() as conn:
            C.get(conn, biz["id"], cid)
        X.send(biz, actor, contact_id=cid, body=str(a.get("text") or "")[:2000])
        return "✓ enviado"
    raise Invalid("unknown action", "action")


def _amount(biz, v):
    if v in (None, ""):
        return None
    try:
        return money.to_minor(str(v).replace(",", "").replace("$", "").strip(), biz["currency"])
    except Exception:
        raise Invalid("check the amount", "amount") from None


# ---------------------------------------------------------------- the conversation
def _system(biz: dict, user_name: str, page: str) -> str:
    ind = packs.industry(biz["industry"])
    now = datetime.now(ZoneInfo(biz["timezone"])).strftime("%A %Y-%m-%d %H:%M")
    return f"""You are Valeria, the assistant inside {biz['name']}'s office software ({packs.text(ind.name, 'en')}, {biz['country']}).
You work for {user_name or 'the owner'}, not for their customers. Now: {now} ({biz['timezone']}). Currency {biz['currency']}. They are on: {page}.
Be a sharp, warm colleague: short answers, practical, in the language they write in. You may help with anything a professional
asks (design advice, writing proposals, captions, emails, maths, translations, planning).

To know their business, LOOK IT UP with tools; never invent clients, projects, prices or dates. To change anything, PROPOSE an action:
the person taps a button to confirm (you never claim something is done until they confirm).

Reply with ONE JSON object and nothing else:
{{"say": "text for the person", "read": [{{"tool": "...", "args": {{}}}}], "do": [{{"tool": "...", "args": {{}}}}]}}
Use "read" when you need data (you'll get the results and answer again); leave "say" short then. Use "do" to propose actions.

Read tools: today {{days}}; find_clients {{q}}; client {{id}}; projects {{status: active|done|cancelled}}; project {{id}};
late_orders {{}}; open_tasks {{}}; money {{}}; library {{q}}; suppliers {{}}.
Do tools: create_task {{title, project_id?, due? YYYY-MM-DD}}; add_note {{project_id, text, client_sees? bool}};
create_project {{name, client_id?, style?, brief?, budget?}}; add_piece {{project_id, name, category?, price?, qty?, room_id?}};
set_stage {{project_id, stage}}; book_visit {{title, starts YYYY-MM-DDTHH:MM local, minutes?, client_id?}};
message_client {{client_id, text}} (sends on their connected WhatsApp/channel — always show the exact text in "say").
Ids come from read results. Money amounts are plain numbers in {biz['currency']}."""


def _parse(text: str) -> dict:
    t = (text or "").strip()
    m = re.search(r"\{.*\}", t, re.S)
    if m:
        try:
            d = json.loads(m.group(0))
            if isinstance(d, dict):
                return d
        except ValueError:
            pass
    return {"say": t}


def chat(biz: dict, user: dict | None, turns: list, page: str = "", http: httpx.Client | None = None) -> dict:
    """-> {"say": str, "actions": [{"label", "token"}]} or {"say": None, "reason": "no_ai"}."""
    clean = []
    for t in (turns or [])[-MAX_TURNS:]:
        if isinstance(t, dict) and t.get("r") in ("user", "assistant") and str(t.get("t") or "").strip():
            clean.append(("user" if t["r"] == "user" else "assistant", str(t["t"]).strip()[:MAX_CHARS]))
    merged = []
    for r, t in clean:                                         # providers want turns that alternate
        if merged and merged[-1][0] == r:
            merged[-1] = (r, merged[-1][1] + "\n\n" + t)
        else:
            merged.append((r, t))
    clean = merged
    while clean and clean[0][0] != "user":
        clean = clean[1:]
    if not clean or clean[-1][0] != "user":
        raise Invalid("write something", "turns")
    with db.tx() as conn:
        s = AI.settings(conn, biz["id"])
        if not blocks.on(biz, "ai") or not s or not (s["api_key"] or s["provider"] == "demo"):
            return {"say": None, "reason": "no_ai"}
        prov, key, model = AI._credentials(conn, biz, s)
    system = _system(biz, (user or {}).get("name") or "", page[:80])
    h = AI._http(http)
    turns_ = list(clean)
    for _round in range(MAX_ROUNDS):
        out = None
        for m in [x.strip() for x in model.split(",") if x.strip()]:
            try:
                out = AI._call(prov, key, m, system, turns_, h, max_tokens=1800)
                break
            except (httpx.HTTPError, AI.AIError) as e:
                if isinstance(e, AI.AIError) and not e.busy:
                    raise
        if out is None:
            raise AI.AIError("the AI is busy; try again in a moment", busy=True)
        d = _parse(out)
        reads = [r for r in (d.get("read") or []) if isinstance(r, dict) and r.get("tool") in READ][:5]
        if reads and _round < MAX_ROUNDS - 1:
            results = []
            for r in reads:
                try:
                    results.append({"tool": r["tool"], "result": run_read(biz, r["tool"], r.get("args") or {})})
                except (Invalid, NotFound) as e:
                    results.append({"tool": r["tool"], "error": str(e) or "not found"})
            turns_ += [("assistant", out[:MAX_CHARS]),
                       ("user", "TOOL RESULTS (not from the person): " + json.dumps(results, ensure_ascii=False, default=str)[:12000])]
            continue
        acts = []
        for a in (d.get("do") or [])[:5]:
            if isinstance(a, dict) and a.get("tool") in WRITE:
                args = a.get("args") if isinstance(a.get("args"), dict) else {}
                acts.append({"label": describe(biz, a["tool"], args), "token": sign(biz, (user or {}).get("id"), a["tool"], args)})
        with db.tx() as conn:
            from . import log
            log(conn, biz["id"], "copilot.answered", f"user:{user['id']}" if user else "demo_visitor", None, {"provider": prov, "actions": len(acts)})
        return {"say": str(d.get("say") or "").strip()[:6000] or "…", "actions": acts}
    return {"say": "…", "actions": []}
