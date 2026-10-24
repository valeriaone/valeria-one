"""The web app for business owners: Today, Inbox, Customers, Pipeline, Calendar, Channels, Settings.

Server-rendered (Jinja), built on the same services as the public API, so the screens and the API can
never disagree. Every form carries a CSRF token; every page is scoped to the signed-in business.
"""
from __future__ import annotations

import secrets
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo, available_timezones

from babel.dates import format_date, format_datetime, format_time
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from .. import __version__, channels, config, db, money, packs
from ..services import Blocked, Invalid, NotFound
from ..services import businesses as B
from ..services import contacts as C
from ..services import deals as D
from ..services import inbox as X
from ..services import users as U
from .i18n import NATIVE, UI_LANGS, from_accept_language, is_rtl, t, ui_lang

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))
router = APIRouter(include_in_schema=False)
_TZS = sorted(available_timezones())


class _Redirect(Exception):
    def __init__(self, url: str):
        self.url = url


# ---------------------------------------------------------------- session helpers
def _csrf(request: Request) -> str:
    tok = request.session.get("csrf")
    if not tok:
        tok = request.session["csrf"] = secrets.token_urlsafe(24)
    return tok


async def _form(request: Request) -> dict[str, Any]:
    f = await request.form()
    data = {k: v for k, v in f.multi_items()}
    if not secrets.compare_digest(str(data.get("csrf", "")), str(request.session.get("csrf", "x"))):
        _flash(request, "expired_form", "warn")
        raise _Redirect(request.headers.get("referer") or "/app")
    return data


def _flash(request: Request, key: str, kind: str = "ok", raw: str | None = None) -> None:
    request.session.setdefault("flash", []).append({"kind": kind, "key": key, "raw": raw})


def _ctx(request: Request) -> dict:
    """The signed-in user's business, or a redirect to sign in."""
    bid = request.session.get("business_id")
    if not bid:
        raise _Redirect("/login")
    with db.tx() as conn:
        try:
            biz = B.get(conn, UUID(bid))
        except (NotFound, ValueError):
            request.session.clear()
            raise _Redirect("/login") from None
        uid = request.session.get("user_id")
        user = U.get(conn, UUID(uid)) if uid else None
        if uid and (not user or not U.membership(conn, user["id"], biz["id"])):
            request.session.clear()
            raise _Redirect("/login")
        if not uid and not (biz["sandbox"] and biz["demo"] and config.SANDBOX()):   # demo visitors: public demos only
            request.session.clear()
            raise _Redirect("/login")
        role = U.membership(conn, user["id"], biz["id"])["role"] if user else "owner"
        mine = U.businesses_of(conn, user["id"]) if user else []
    return {"biz": biz, "user": user, "actor": f"user:{user['id']}" if user else "demo_visitor", "role": role,
            "businesses": mine}


def _own(request: Request) -> dict:
    """Like _ctx, for owner-only pages (settings, channels, keys, AI, booking setup, webhooks, team)."""
    ctx = _ctx(request)
    if ctx["role"] != "owner":
        _flash(request, "owners_only", "warn")
        raise _Redirect("/app")
    return ctx


def _lang(request: Request, biz: dict | None = None) -> str:
    """The screen speaks the PERSON's language: their choice, else their browser, else the business's, else English.
    (The business's language is for what goes out to its customers, not for who reads the screen.)"""
    if request.session.get("lang"):
        return ui_lang(request.session["lang"])
    return from_accept_language(request.headers.get("accept-language")) or ui_lang((biz or {}).get("language"))


def _country_name(code: str, lang: str) -> str:
    """Country names in the reader's language, from the Unicode CLDR (via Babel)."""
    from babel import Locale
    try:
        return Locale.parse(lang).territories.get(code) or packs.country(code).label("en")
    except Exception:
        return packs.country(code).label("en")


def _locale(biz: dict, lang: str) -> str:
    pack = packs.country(biz["country"])
    return pack.locale if pack.locale.split("_")[0] == lang else f"{lang}_{pack.code}"


def render(request: Request, name: str, ctx: dict | None, **kw) -> HTMLResponse:
    biz = ctx["biz"] if ctx else None
    lang = _lang(request, biz)
    loc = _locale(biz, lang) if biz else lang

    def _safe(fn, *a, **k):
        try:
            return fn(*a, **k)
        except Exception:
            return fn(*a, **{**k, "locale": lang})

    def fmt_money(minor):
        return "" if minor is None else _safe(money.fmt, int(minor), biz["currency"], locale=loc)

    def fmt_dt(v, kind="medium"):
        if not v:
            return ""
        tz = ZoneInfo(biz["timezone"]) if biz else timezone.utc
        return _safe(format_datetime, v.astimezone(tz), "short" if kind == "short" else "medium", locale=loc)

    def fmt_day(v):
        tz = ZoneInfo(biz["timezone"])
        return _safe(format_date, v.astimezone(tz) if isinstance(v, datetime) else v, "EEEE d MMMM", locale=loc)

    def fmt_time(v):
        return _safe(format_time, v.astimezone(ZoneInfo(biz["timezone"])), "short", locale=loc)

    flashes = request.session.pop("flash", [])
    data = {"request": request, "ctx": ctx, "biz": biz, "lang": lang, "langs": UI_LANGS, "_": lambda k: t(k, lang),
            "native": NATIVE, "rtl": is_rtl(lang), "country_name": lambda code: _country_name(code, lang),
            "csrf": _csrf(request), "flashes": flashes, "money": fmt_money, "dt": fmt_dt, "day": fmt_day, "hm": fmt_time,
            "label": lambda labels: packs.text(labels, lang), "version": __version__, "sandbox": config.SANDBOX(),
            "words": (lambda w: packs.text(packs.industry(biz["industry"]).words[w], lang)) if biz else (lambda w: w),
            "industry": packs.industry(biz["industry"]) if biz else None,
            "country": packs.country(biz["country"]) if biz else None,
            **kw}
    return templates.TemplateResponse(request, name, data)


def _go(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _guard(fn):
    """Turn our redirects and rule errors into page responses."""
    import functools

    @functools.wraps(fn)
    async def wrap(request: Request, *a, **kw):
        try:
            return await fn(request, *a, **kw)
        except _Redirect as r:
            return _go(r.url)
        except Invalid as e:
            _flash(request, "error", "warn", raw=str(e))
            return _go(request.headers.get("referer") or "/app")
        except NotFound:
            return HTMLResponse("Not found", status_code=404)
    return wrap


# ---------------------------------------------------------------- public pages
@router.get("/")
@_guard
async def home(request: Request):
    if request.session.get("business_id"):
        return _go("/app")
    if config.SANDBOX():                                    # the public demo server opens on a real landing page
        return render(request, "landing.html", None, demos=_demos())
    return render(request, "auth.html", None, mode="home", demos=_demos())


@router.get("/deck")
async def deck():
    """The pitch deck as a PDF anyone can open."""
    return RedirectResponse("/static/valeria-one-deck.pdf", status_code=302)


@router.get("/stats/{token}")
async def sandbox_stats(request: Request, token: str):
    """Sandbox only, for the creator: who looked, from where, and visits from Meta's network."""
    want = config.get("VO_STATS_TOKEN")
    if not (config.SANDBOX() and want and secrets.compare_digest(token, want)):
        return HTMLResponse("Not found", status_code=404)
    from ..sandbox import visits
    import json as _json
    hub_file = Path(config.get("VO_HUB_FILE") or (config.ROOT / "hub.json"))
    hub = _json.loads(hub_file.read_text()) if hub_file.exists() else None
    resp = templates.TemplateResponse(request, "stats.html", {"request": request, "s": visits.summary(), "version": __version__,
                                                              "hub": hub, "gh": visits.github_numbers()})
    resp.headers["X-Robots-Tag"] = "noindex"
    return resp


def _demos() -> list[dict]:
    if not config.SANDBOX():
        return []
    with db.tx() as conn:
        rows = conn.execute(select(db.businesses).where(db.businesses.c.sandbox.is_(True), db.businesses.c.demo.is_(True))
                            .order_by(db.businesses.c.created_at).limit(12)).mappings()
        order = ["US", "PH", "MX", "BR", "JP", "GB", "NG", "DE", "CO"]      # mixed continents, not one language first
        out = [{"id": r["id"], "name": r["name"], "country": r["country"], "industry": r["industry"], "flag": r["country"]}
               for r in rows]
        return sorted(out, key=lambda d: order.index(d["country"]) if d["country"] in order else 99)


@router.get("/login")
@_guard
async def login_page(request: Request):
    return render(request, "auth.html", None, mode="login", demos=_demos())


@router.post("/login")
@_guard
async def login(request: Request):
    f = await _form(request)
    with db.tx() as conn:
        user = U.authenticate(conn, f.get("email", ""), f.get("password", ""))
        bizs = U.businesses_of(conn, user["id"]) if user else []
    if not user or not bizs:
        _flash(request, "bad_login", "warn")
        return _go("/login")
    request.session.clear()
    request.session.update(user_id=str(user["id"]), business_id=str(bizs[0]["id"]))
    return _go("/app")


@router.get("/signup")
@_guard
async def signup_page(request: Request):
    return render(request, "auth.html", None, mode="signup", countries=_countries(request), industries=_industries(request))


def _countries(request):
    lang = _lang(request)
    return sorted(((c.code, _country_name(c.code, lang)) for c in packs.countries().values()), key=lambda x: x[1])


def _industries(request):
    lang = _lang(request)
    return [(i.code, packs.text(i.name, lang)) for i in packs.industries().values()]


@router.post("/signup")
@_guard
async def signup(request: Request):
    f = await _form(request)
    sandbox = config.SANDBOX()
    with db.tx() as conn:
        user, biz = U.register(conn, email=f.get("email", ""), name=f.get("name", ""), password=f.get("password", ""),
                               business_name=f.get("business_name", ""), country=f.get("country", "MX"),
                               industry=f.get("industry", "generic"), sandbox=sandbox)
        if sandbox:
            X.connect(conn, biz, "system", channel="sandbox", name="Sandbox")
            from ..services import ai as AIS
            AIS.save(conn, biz, "system", provider="demo", mode="auto")
    request.session.clear()
    request.session.update(user_id=str(user["id"]), business_id=str(biz["id"]))
    return _go("/app")


@router.get("/demo/{business_id}")
@_guard
async def demo(request: Request, business_id: UUID):
    if not config.SANDBOX():
        raise NotFound("page")
    with db.tx() as conn:
        biz = B.get(conn, business_id)
    if not (biz["sandbox"] and biz["demo"]):          # only the public demos — never a visitor's own business
        raise NotFound("page")
    request.session.clear()
    request.session.update(business_id=str(biz["id"]))
    return _go("/app")


@router.post("/logout")
@_guard
async def logout(request: Request):
    await _form(request)
    request.session.clear()
    return _go("/login")


@router.post("/lang")
@_guard
async def set_lang_form(request: Request):
    f = await _form(request)
    if f.get("code") in UI_LANGS:
        request.session["lang"] = f["code"]
    return _go(request.headers.get("referer") or "/app")


@router.post("/lang/{code}")
@_guard
async def set_lang(request: Request, code: str):
    await _form(request)
    if code in UI_LANGS:
        request.session["lang"] = code
    return _go(request.headers.get("referer") or "/app")


# ---------------------------------------------------------------- Today
@router.get("/app")
@_guard
async def today(request: Request):
    ctx = _ctx(request)
    biz = ctx["biz"]
    tz = ZoneInfo(biz["timezone"])
    start = datetime.combine(datetime.now(tz).date(), time(0), tz)
    with db.tx() as conn:
        convs = X.conversations(conn, biz["id"], status="open", limit=200)
        waiting = [c for c in convs if c["last_inbound_at"] and _last_dir(conn, c["id"]) == "in"]
        appts = D.appointments(conn, biz["id"], start=start, end=start + timedelta(days=1))
        pipe = D.pipeline(conn, biz, _lang(request, biz))
        events = conn.execute(select(db.events).where(db.events.c.business_id == biz["id"])
                              .order_by(db.events.c.id.desc()).limit(12)).mappings().all()
    open_value = sum(s["amount_minor"] for s in pipe if s["outcome"] == "open")
    return render(request, "today.html", ctx, waiting=waiting, open_count=len(convs), appts=appts, pipe=pipe,
                  open_value=open_value, events=events, now_local=datetime.now(tz), page="today")


def _last_dir(conn, conv_id) -> str | None:
    M = db.messages
    return conn.execute(select(M.c.direction).where(M.c.conversation_id == conv_id)
                        .order_by(M.c.created_at.desc()).limit(1)).scalar()


# ---------------------------------------------------------------- Inbox
@router.get("/app/inbox")
@_guard
async def inbox(request: Request, c: UUID | None = None, status: str = "open"):
    ctx = _ctx(request)
    biz = ctx["biz"]
    with db.tx() as conn:
        convs = X.conversations(conn, biz["id"], status=status if status in ("open", "closed") else None, limit=100)
        current = X.conversation(conn, biz["id"], c, limit=200) if c else None
        contact = C.get(conn, biz["id"], current["contact_id"]) if current else None
        templ_ok = bool(current) and channels.adapter(current["channel"]).reply_window_hours is not None
        accs = X.accounts(conn, biz["id"])
    in_window = True
    if current and templ_ok:
        last = current["last_inbound_at"]
        in_window = bool(last) and datetime.now(timezone.utc) - last < timedelta(hours=channels.adapter(current["channel"]).reply_window_hours)
    has_sandbox = any(a["channel"] == "sandbox" for a in accs)
    from ..services import ai as AIS
    with db.tx() as conn:
        ai_s = AIS.public_settings(conn, biz["id"])
    draft = request.session.pop("ai_draft", None) if current else None
    if draft and draft.get("conv") != str(current["id"]):
        draft = None
    return render(request, "inbox.html", ctx, convs=convs, current=current, contact=contact, status=status,
                  templ_ok=templ_ok, in_window=in_window, has_sandbox=has_sandbox and biz["sandbox"], ai=ai_s,
                  draft=draft, page="inbox")


@router.post("/app/inbox/{conv_id}/suggest")
@_guard
async def inbox_suggest(request: Request, conv_id: UUID):
    from ..services import ai as AIS
    ctx = _ctx(request)
    await _form(request)
    with db.tx() as conn:
        X.conversation(conn, ctx["biz"]["id"], conv_id, limit=1)
    try:
        res = AIS.suggest(ctx["biz"], ctx["actor"], conv_id)
        request.session["ai_draft"] = {"conv": str(conv_id), "text": res["text"], "human": res["needs_human"]}
    except AIS.AIError as e:
        _flash(request, "error", "warn", raw=str(e))
    return _go(f"/app/inbox?c={conv_id}")


@router.post("/app/inbox/{conv_id}/resume-ai")
@_guard
async def inbox_resume_ai(request: Request, conv_id: UUID):
    from ..services import ai as AIS
    ctx = _ctx(request)
    await _form(request)
    with db.tx() as conn:
        X.conversation(conn, ctx["biz"]["id"], conv_id, limit=1)
        AIS.clear_human(conn, ctx["biz"]["id"], ctx["actor"], conv_id)
    return _go(f"/app/inbox?c={conv_id}")


@router.post("/app/inbox/{conv_id}/send")
@_guard
async def inbox_send(request: Request, conv_id: UUID):
    ctx = _ctx(request)
    f = await _form(request)
    biz = ctx["biz"]
    with db.tx() as conn:
        conv = X.conversation(conn, biz["id"], conv_id, limit=1)
    tpl = (f.get("template") or "").strip()
    try:
        X.send(biz, ctx["actor"], contact_id=conv["contact_id"], body=(f.get("body") or "").strip(),
               kind="marketing" if f.get("kind") == "marketing" else "service", channel_account_id=conv["channel_account_id"],
               template={"name": tpl, "language": _locale(biz, _lang(request, biz))} if tpl else None)
    except Blocked as b:
        _flash(request, f"r_{b.reason}", "warn")
    return _go(f"/app/inbox?c={conv_id}")


@router.post("/app/inbox/{conv_id}/status")
@_guard
async def inbox_status(request: Request, conv_id: UUID):
    ctx = _ctx(request)
    f = await _form(request)
    with db.tx() as conn:
        X.set_status(conn, ctx["biz"]["id"], ctx["actor"], conv_id, f.get("status", "closed"))
    return _go(f"/app/inbox?c={conv_id}")


@router.post("/app/inbox/simulate")
@_guard
async def inbox_simulate(request: Request):
    ctx = _ctx(request)
    f = await _form(request)
    biz = ctx["biz"]
    if not biz["sandbox"]:
        raise NotFound("page")
    import json
    from ..channels import HookRequest
    with db.tx() as conn:
        acc = X._account(conn, biz["id"], None, "sandbox")
    name = (f.get("name") or "").strip()[:200] or "Customer"
    X.receive(acc, HookRequest("POST", "", {}, {}, json.dumps({"from": "sim-" + name.lower().replace(" ", "-"),
                                                                "name": name, "body": f.get("body") or ""}).encode()))
    with db.tx() as conn:
        I = db.identities
        cid = conn.execute(select(I.c.contact_id).where(I.c.business_id == biz["id"], I.c.channel == "sandbox",
                                                        I.c.address == "sim-" + name.lower().replace(" ", "-"))).scalar()
        conv = conn.execute(select(db.conversations.c.id).where(db.conversations.c.contact_id == cid,
                                                                db.conversations.c.channel_account_id == acc["id"])).scalar()
    from ..services import autoreply
    autoreply.respond(biz["id"], conv)                     # in the sandbox the visitor sees the AI answer right away
    return _go(f"/app/inbox?c={conv}")


# ---------------------------------------------------------------- Comments -> private message
@router.get("/app/comments")
@_guard
async def comments_page(request: Request):
    from ..services import comments as CM
    ctx = _ctx(request)
    with db.tx() as conn:
        rules = CM.rules(conn, ctx["biz"]["id"])
        rows = CM.comments(conn, ctx["biz"]["id"], limit=100)
    return render(request, "comments.html", ctx, rules=rules, rows=rows, page="inbox")


@router.post("/app/comments/rules")
@_guard
async def comments_rule_new(request: Request):
    from ..services import comments as CM
    ctx = _own(request)
    f = await request.form()
    if not secrets.compare_digest(str(f.get("csrf", "")), str(request.session.get("csrf", "x"))):
        _flash(request, "expired_form", "warn")
        return _go("/app/comments")
    chans = [c for c in f.getlist("channels") if c in ("messenger", "instagram")]
    if ctx["biz"]["sandbox"]:
        chans.append("sandbox")
    with db.tx() as conn:
        CM.create_rule(conn, ctx["biz"], ctx["actor"], keywords=str(f.get("keywords", "")), reply=str(f.get("reply", "")),
                       channels=chans)
    _flash(request, "saved")
    return _go("/app/comments")


@router.post("/app/comments/rules/{rule_id}/delete")
@_guard
async def comments_rule_delete(request: Request, rule_id: UUID):
    from ..services import comments as CM
    ctx = _own(request)
    await _form(request)
    with db.tx() as conn:
        CM.delete_rule(conn, ctx["biz"]["id"], ctx["actor"], rule_id)
    return _go("/app/comments")


@router.post("/app/comments/simulate")
@_guard
async def comments_simulate(request: Request):
    import uuid as _uuid
    from ..services import comments as CM
    ctx = _ctx(request)
    f = await _form(request)
    if not ctx["biz"]["sandbox"]:
        raise NotFound("page")
    with db.tx() as conn:
        acc = X._account(conn, ctx["biz"]["id"], None, "sandbox")
    name = (f.get("name") or "").strip()[:200] or "Customer"
    slug = "".join(ch for ch in name.lower() if ch.isalnum())[:40] or "someone"
    res = CM.ingest(acc, "sandbox", {"comment_id": f"sbx_c_{_uuid.uuid4().hex[:12]}", "post_id": "sbx_post",
                                     "author_id": slug, "author_name": name, "text": f.get("text") or ""})
    _flash(request, "c_" + res)
    return _go("/app/comments")


# ---------------------------------------------------------------- Customers
@router.get("/app/contacts")
@_guard
async def contacts_page(request: Request, q: str | None = None):
    ctx = _ctx(request)
    with db.tx() as conn:
        rows = C.search(conn, ctx["biz"]["id"], q=q, limit=200)
    return render(request, "contacts.html", ctx, rows=rows, q=q or "", page="contacts")


def _contact_form(f: dict) -> dict:
    d = {k: f.get(k, "") for k in ("name", "phone", "email", "notes")}
    d["tags"] = [x for x in (f.get("tags") or "").split(",") if x.strip()]
    if "consent" in f:
        d["marketing_consent"] = f.get("consent") == "yes"
        d["marketing_consent_source"] = f.get("consent_source", "")
    return d


@router.post("/app/contacts")
@_guard
async def contacts_new(request: Request):
    ctx = _ctx(request)
    f = await _form(request)
    with db.tx() as conn:
        row = C.create(conn, ctx["biz"], ctx["actor"], {**_contact_form(f), "source": "manual"})
    return _go(f"/app/contacts/{row['id']}")


@router.get("/app/contacts/{contact_id}")
@_guard
async def contact_page(request: Request, contact_id: UUID):
    ctx = _ctx(request)
    biz = ctx["biz"]
    with db.tx() as conn:
        c = C.get(conn, biz["id"], contact_id)
        ids = C.identities(conn, biz["id"], contact_id)
        convs = X.conversations(conn, biz["id"], contact_id=contact_id)
        deals = D.search(conn, biz["id"], contact_id=contact_id)
        appts = [a for a in D.appointments(conn, biz["id"], limit=500) if a["contact_id"] == contact_id]
        accs = X.accounts(conn, biz["id"])
    reach = [a for a in accs if any(i["channel"] == a["channel"] for i in ids)]
    from ..services import payments as PM
    with db.tx() as conn:
        pays = PM.list_(conn, biz["id"], contact_id=contact_id)
        from ..services import quotes as QS
        qts = QS.list_(conn, biz["id"], contact_id=contact_id)
        pay_accs = PM.accounts(conn, biz["id"])
    stages = {s.key: s for s in packs.industry(biz["industry"]).stages}
    return render(request, "contact.html", ctx, c=c, ids=ids, convs=convs, deals=deals, appts=appts, reach=reach,
                  pays=pays, pay_accs=pay_accs, qts=qts, public_url=config.PUBLIC_URL(),
                  stages=stages, page="contacts")


@router.post("/app/contacts/{contact_id}")
@_guard
async def contact_save(request: Request, contact_id: UUID):
    ctx = _ctx(request)
    f = await _form(request)
    with db.tx() as conn:
        C.patch(conn, ctx["biz"], ctx["actor"], contact_id, _contact_form(f))
    _flash(request, "saved")
    return _go(f"/app/contacts/{contact_id}")


@router.post("/app/contacts/{contact_id}/erase")
@_guard
async def contact_erase(request: Request, contact_id: UUID):
    ctx = _ctx(request)
    await _form(request)
    with db.tx() as conn:
        C.delete(conn, ctx["biz"]["id"], ctx["actor"], contact_id)
    _flash(request, "erased")
    return _go("/app/contacts")


@router.post("/app/contacts/{contact_id}/message")
@_guard
async def contact_message(request: Request, contact_id: UUID):
    """Start (or continue) a conversation with this customer on a channel."""
    ctx = _ctx(request)
    f = await _form(request)
    biz = ctx["biz"]
    try:
        acc_id = UUID(f.get("account_id", ""))
    except ValueError:
        raise Invalid("pick a channel", "channel") from None
    with db.tx() as conn:
        C.get(conn, biz["id"], contact_id)
        acc = X._account(conn, biz["id"], acc_id, None)
        conv = X._conversation(conn, biz["id"], acc["id"], contact_id)
    return _go(f"/app/inbox?c={conv['id']}")


# ---------------------------------------------------------------- Pipeline
@router.get("/app/pipeline")
@_guard
async def pipeline_page(request: Request):
    ctx = _ctx(request)
    biz = ctx["biz"]
    with db.tx() as conn:
        pipe = D.pipeline(conn, biz, _lang(request, biz))
        deals = D.search(conn, biz["id"], limit=200)
        names = {r["id"]: r["name"] for r in C.search(conn, biz["id"], limit=200)}
    by_stage: dict[str, list] = {s["key"]: [] for s in pipe}
    for d in deals:
        by_stage.setdefault(d["stage"], []).append(d)
    return render(request, "pipeline.html", ctx, pipe=pipe, by_stage=by_stage, names=names,
                  contacts=sorted(names.items(), key=lambda x: x[1].lower()), page="pipeline")


@router.post("/app/deals")
@_guard
async def deal_new(request: Request):
    ctx = _ctx(request)
    f = await _form(request)
    biz = ctx["biz"]
    amount = None
    if (f.get("amount") or "").strip():
        try:
            amount = money.to_minor(f["amount"].replace(",", "").strip(), biz["currency"])
        except Exception:
            raise Invalid("amount: use digits, e.g. 1500.00", "amount") from None
    with db.tx() as conn:
        D.create(conn, biz, ctx["actor"], {"title": f.get("title", ""), "stage": f.get("stage") or None,
                                           "contact_id": UUID(f["contact_id"]) if f.get("contact_id") else None,
                                           "amount_minor": amount})
    return _go("/app/pipeline")


@router.post("/app/deals/{deal_id}/stage")
@_guard
async def deal_stage(request: Request, deal_id: UUID):
    ctx = _ctx(request)
    f = await _form(request)
    with db.tx() as conn:
        D.patch(conn, ctx["biz"], ctx["actor"], deal_id, {"stage": f.get("stage")})
    return _go(f.get("back") or "/app/pipeline")


# ---------------------------------------------------------------- Calendar
@router.get("/app/calendar")
@_guard
async def calendar_page(request: Request):
    ctx = _ctx(request)
    biz = ctx["biz"]
    tz = ZoneInfo(biz["timezone"])
    start = datetime.combine(datetime.now(tz).date(), time(0), tz)
    with db.tx() as conn:
        appts = D.appointments(conn, biz["id"], start=start, end=start + timedelta(days=60))
        names = {r["id"]: r["name"] for r in C.search(conn, biz["id"], limit=200)}
    days: dict[date, list] = {}
    for a in appts:
        days.setdefault(a["starts_at"].astimezone(tz).date(), []).append(a)
    from ..services import booking as BK
    with db.tx() as conn:
        svcs = BK.services(conn, biz["id"], active_only=True)
        fresh = B.get(conn, biz["id"])
        from ..services import gcal
        glink = gcal.link(conn, biz["id"])
    return render(request, "calendar.html", ctx, days=sorted(days.items()), names=names,
                  contacts=sorted(names.items(), key=lambda x: x[1].lower()),
                  today=datetime.now(tz).date().isoformat(), svcs=svcs, bk=fresh, bdays=BK.DAYS,
                  glink=glink, gcal_on=gcal.configured() and not biz["sandbox"],
                  book_url=f"{config.PUBLIC_URL()}/b/{fresh['slug']}" if fresh["slug"] else None, page="calendar")


@router.post("/app/appointments")
@_guard
async def appt_new(request: Request):
    ctx = _ctx(request)
    f = await _form(request)
    biz = ctx["biz"]
    try:
        d = date.fromisoformat(f.get("date", ""))
        hh, mm = (int(x) for x in (f.get("time") or "").split(":")[:2])
        mins = int(f.get("minutes") or 60)
    except ValueError:
        raise Invalid("pick a date and a time", "starts_at") from None
    if not 5 <= mins <= 24 * 60:
        raise Invalid("minutes: between 5 and 1440", "ends_at")
    st = datetime.combine(d, time(hh, mm), ZoneInfo(biz["timezone"]))
    with db.tx() as conn:
        D.book(conn, biz, ctx["actor"], {"title": f.get("title", ""), "starts_at": st, "ends_at": st + timedelta(minutes=mins),
                                         "contact_id": UUID(f["contact_id"]) if f.get("contact_id") else None,
                                         "location": f.get("location") or None,
                                         "allow_overlap": f.get("allow_overlap") == "1"})
    return _go("/app/calendar")


@router.post("/app/appointments/{appt_id}/status")
@_guard
async def appt_status(request: Request, appt_id: UUID):
    ctx = _ctx(request)
    f = await _form(request)
    with db.tx() as conn:
        D.set_appointment_status(conn, ctx["biz"]["id"], ctx["actor"], appt_id, f.get("status", ""))
    return _go("/app/calendar")


# ---------------------------------------------------------------- Channels
@router.get("/app/channels")
@_guard
async def channels_page(request: Request):
    ctx = _own(request)
    biz = ctx["biz"]
    with db.tx() as conn:
        accs = X.accounts(conn, biz["id"])
    pack = packs.country(biz["country"])
    order = [c for c in pack.channels if c in channels.available()] + \
            [c for c in channels.available() if c not in pack.channels and c != "sandbox"]
    avail = [{"channel": c, "needs": channels.adapter(c).check_config({}), "home": c in pack.channels} for c in order]
    from ..services import meta as MT
    with db.tx() as conn:
        lead_rows = MT.leads(conn, biz["id"], limit=20)
    meta = MT.settings() if MT.configured() and not biz["sandbox"] else None
    return render(request, "channels.html", ctx, accs=accs, avail=avail, meta=meta, leads=lead_rows,
                  graph_version=config.get("VO_GRAPH_VERSION", "v23.0"), page="channels")


@router.post("/app/channels/meta/whatsapp")
@_guard
async def meta_whatsapp(request: Request):
    """The end of Meta's WhatsApp pop-up: the browser posts the code and the ids Meta gave it."""
    from ..services import meta as MT
    ctx = _own(request)
    f = await _form(request)
    try:
        with db.tx() as conn:
            MT.connect_whatsapp(conn, ctx["biz"], ctx["actor"], code=f.get("code", ""), waba_id=f.get("waba_id", ""),
                                phone_number_id=f.get("phone_number_id", ""))
    except MT.MetaError as e:
        raise Invalid(str(e), "meta") from None
    _flash(request, "meta_connected")
    return _go("/app/channels")


@router.post("/app/channels/meta/pages")
@_guard
async def meta_pages(request: Request):
    """The end of Meta's Login pop-up: show the business its Pages to pick from. Tokens wait in the
    session encrypted (10 minutes), never readable in the browser."""
    from ..services import meta as MT
    ctx = _own(request)
    f = await _form(request)
    if not MT.configured() or ctx["biz"]["sandbox"]:
        raise NotFound("page")
    try:
        _, pages = MT.list_pages(f.get("code", ""))
    except MT.MetaError as e:
        raise Invalid(str(e), "meta") from None
    import json as _json
    from ..crypto import _f
    request.session["meta_pages"] = _f().encrypt(_json.dumps(pages).encode()).decode()
    return render(request, "meta_pages.html", ctx, pages=pages, page="channels")


@router.post("/app/channels/meta/pages/connect")
@_guard
async def meta_pages_connect(request: Request):
    from ..services import meta as MT
    ctx = _own(request)
    f = await request.form()
    if not secrets.compare_digest(str(f.get("csrf", "")), str(request.session.get("csrf", "x"))):
        _flash(request, "expired_form", "warn")
        return _go("/app/channels")
    import json as _json
    from cryptography.fernet import InvalidToken
    from ..crypto import _f
    blob = request.session.pop("meta_pages", None)
    try:
        pages = _json.loads(_f().decrypt(blob.encode(), ttl=600)) if blob else None
    except InvalidToken:
        pages = None
    if not pages:
        _flash(request, "expired_form", "warn")
        return _go("/app/channels")
    try:
        with db.tx() as conn:
            MT.connect_pages(conn, ctx["biz"], ctx["actor"], pages, [str(x) for x in f.getlist("page")])
    except MT.MetaError as e:
        raise Invalid(str(e), "meta") from None
    _flash(request, "meta_connected")
    return _go("/app/channels")


@router.post("/app/channels")
@_guard
async def channels_connect(request: Request):
    ctx = _own(request)
    f = await _form(request)
    ch = f.get("channel", "")
    cfg = {k[4:]: v.strip() for k, v in f.items() if k.startswith("cfg_") and isinstance(v, str)}
    with db.tx() as conn:
        X.connect(conn, ctx["biz"], ctx["actor"], channel=ch, name=f.get("name") or ch, address=f.get("address") or None, cfg=cfg)
    _flash(request, "saved")
    return _go("/app/channels")


@router.post("/app/channels/{account_id}/disconnect")
@_guard
async def channels_disconnect(request: Request, account_id: UUID):
    ctx = _own(request)
    await _form(request)
    with db.tx() as conn:
        X.disconnect(conn, ctx["biz"]["id"], ctx["actor"], account_id)
    return _go("/app/channels")


# ---------------------------------------------------------------- Public booking page /b/<slug>
_book_hits: dict = {}


def _book_ok(request: Request) -> bool:
    import time as _t
    ip = request.client.host if request.client else "?"
    q = [x for x in _book_hits.get(ip, []) if _t.time() - x < 3600]
    if len(q) >= 10:
        _book_hits[ip] = q
        return False
    _book_hits[ip] = q + [_t.time()]
    return True


def _pub(request: Request, slug: str):
    from ..services import booking as BK
    with db.tx() as conn:
        biz = BK.public_business(conn, slug)
        svcs = BK.services(conn, biz["id"], active_only=True)
    return {"biz": biz, "user": None, "actor": "booking_page", "public": True}, svcs


def _sig(appt_id) -> str:
    from ..crypto import hmac_sha256_hex
    return hmac_sha256_hex(config.get("VO_SECRET_KEY", required=True), f"ics:{appt_id}".encode())[:24]


@router.get("/b/{slug}")
@_guard
async def book_home(request: Request, slug: str):
    ctx, svcs = _pub(request, slug)
    return render(request, "book.html", ctx, step="service", svcs=svcs, slug=slug)


@router.get("/b/{slug}/{service_id}")
@_guard
async def book_service(request: Request, slug: str, service_id: UUID, d: str | None = None):
    from ..services import booking as BK
    ctx, svcs = _pub(request, slug)
    svc = next((x for x in svcs if x["id"] == service_id), None)
    if not svc:
        raise NotFound("service")
    with db.tx() as conn:
        days = BK.open_days(conn, ctx["biz"], service_id)
        day = None
        if d:
            try:
                day = date.fromisoformat(d)
            except ValueError:
                day = None
        day = day if day in days else (days[0] if days else None)
        times = BK.slots(conn, ctx["biz"], service_id, day) if day else []
    return render(request, "book.html", ctx, step="time", svc=svc, days=days, sel=day, times=times, slug=slug)


@router.post("/b/{slug}/{service_id}")
@_guard
async def book_submit(request: Request, slug: str, service_id: UUID):
    from ..services import booking as BK
    ctx, svcs = _pub(request, slug)
    f = await _form(request)
    if f.get("website"):                                 # honeypot: people never fill it, bots do
        return _go(f"/b/{slug}")
    if not _book_ok(request):
        raise Invalid("too many bookings from this connection; try again later", "rate")
    try:
        st = datetime.fromisoformat(f.get("starts_at", ""))
    except ValueError:
        raise Invalid("pick a time", "starts_at") from None
    with db.tx() as conn:
        res = BK.book(conn, ctx["biz"], service_id=service_id, starts_at=st, name=f.get("name", ""), phone=f.get("phone", ""),
                      email=(f.get("email") or "").strip() or None, marketing_consent=f.get("consent") == "1",
                      notes=f.get("notes", ""))
    a = res["appointment"]
    return render(request, "book.html", ctx, step="done", svc=res["service"], appt=a, slug=slug,
                  ics_url=f"/b/{slug}/ics/{a['id']}/{_sig(a['id'])}")


@router.get("/b/{slug}/ics/{appt_id}/{sig}")
async def book_ics(request: Request, slug: str, appt_id: UUID, sig: str):
    from ..services import booking as BK
    if not secrets.compare_digest(sig, _sig(appt_id)):
        return HTMLResponse("Not found", status_code=404)
    with db.tx() as conn:
        try:
            biz = BK.public_business(conn, slug)
        except NotFound:
            return HTMLResponse("Not found", status_code=404)
        a = conn.execute(select(db.appointments).where(db.appointments.c.id == appt_id,
                                                       db.appointments.c.business_id == biz["id"])).mappings().first()
    if not a:
        return HTMLResponse("Not found", status_code=404)
    return Response(BK.ics(biz, dict(a)), media_type="text/calendar",
                    headers={"Content-Disposition": 'attachment; filename="cita.ics"'})


# ---------------------------------------------------------------- Google Calendar
@router.get("/app/calendar/google")
@_guard
async def google_start(request: Request):
    from ..services import gcal
    ctx = _own(request)
    if not gcal.configured() or ctx["biz"]["sandbox"]:
        raise NotFound("page")
    state = secrets.token_urlsafe(24)
    request.session["g_state"] = state
    return RedirectResponse(gcal.auth_url(state), status_code=303)


@router.get("/app/calendar/google/callback")
@_guard
async def google_callback(request: Request, code: str | None = None, state: str | None = None, error: str | None = None):
    from ..services import gcal
    ctx = _own(request)
    expected = request.session.pop("g_state", None)
    if error or not code or not expected or not secrets.compare_digest(state or "", expected):
        _flash(request, "g_failed", "warn")
        return _go("/app/calendar#google")
    with db.tx() as conn:
        gcal.connect(conn, ctx["biz"], ctx["actor"], code)
    _flash(request, "g_connected")
    return _go("/app/calendar#google")


@router.post("/app/calendar/google/disconnect")
@_guard
async def google_disconnect(request: Request):
    from ..services import gcal
    ctx = _own(request)
    await _form(request)
    with db.tx() as conn:
        gcal.disconnect(conn, ctx["biz"]["id"], ctx["actor"])
    return _go("/app/calendar#google")


# ---------------------------------------------------------------- Website /s/<slug>
_site_hits: dict = {}


@router.get("/s/{slug}")
@_guard
async def site_page(request: Request, slug: str):
    from ..services import booking as BK
    from ..services import site as ST
    with db.tx() as conn:
        biz = ST.public(conn, slug)
        svcs = BK.services(conn, biz["id"], active_only=True)
    url = f"{config.PUBLIC_URL()}/s/{biz['slug']}"
    ctx = {"biz": biz, "user": None, "actor": "website", "role": None, "businesses": []}
    resp = render(request, "site.html", ctx, s=biz["site"] or {}, svcs=svcs, url=url, bdays=BK.DAYS,
                  ld=ST.json_ld(biz, svcs, url), sent=request.query_params.get("sent") == "1")
    if biz["sandbox"]:
        resp.headers["X-Robots-Tag"] = "noindex"
    return resp


@router.post("/s/{slug}")
@_guard
async def site_contact(request: Request, slug: str):
    import time as _t
    from ..services import site as ST
    f = await _form(request)
    if f.get("website"):
        return _go(f"/s/{slug}?sent=1")
    ip = request.client.host if request.client else "?"
    hits = [x for x in _site_hits.get(ip, []) if _t.time() - x < 3600]
    if len(hits) >= 10:
        raise Invalid("too many messages from this connection; try later", "rate")
    _site_hits[ip] = hits + [_t.time()]
    reach = (f.get("reach") or "").strip()
    with db.tx() as conn:
        biz = ST.public(conn, slug)
        ST.contact_form(conn, biz, name=f.get("name", ""), message=f.get("message", ""),
                        email=reach if "@" in reach else "", phone="" if "@" in reach else reach)
    return _go(f"/s/{slug}?sent=1#contact")


@router.get("/app/website")
@_guard
async def website_page(request: Request):
    ctx = _own(request)
    with db.tx() as conn:
        fresh = B.get(conn, ctx["biz"]["id"])
    return render(request, "website.html", ctx, s=fresh["site"] or {}, on=fresh["site_enabled"],
                  url=f"{config.PUBLIC_URL()}/s/{fresh['slug']}" if fresh["slug"] else None, page="website")


@router.post("/app/website")
@_guard
async def website_save(request: Request):
    from ..services import site as ST
    ctx = _own(request)
    f = await _form(request)
    with db.tx() as conn:
        biz = B.get(conn, ctx["biz"]["id"])
        ST.save(conn, biz, ctx["actor"], enabled=f.get("enabled") == "1",
                **{k: f.get(k, "") for k in ST.FIELDS})
    _flash(request, "saved")
    return _go("/app/website")


# ---------------------------------------------------------------- Payments
@router.get("/p/{token}")
@_guard
async def pay_page(request: Request, token: str):
    from ..services import payments as PM
    with db.tx() as conn:
        pay, biz, bank = PM.by_token(conn, token)
    ctx = {"biz": biz, "user": None, "actor": "payment_page", "role": None, "businesses": []}
    return render(request, "pay.html", ctx, pay=pay, bank=bank, done=request.query_params.get("done") == "1")


@router.post("/app/payments/connect")
@_guard
async def pay_connect(request: Request):
    from ..services import payments as PM
    ctx = _own(request)
    f = await _form(request)
    cfg = {k[4:]: v for k, v in f.items() if k.startswith("cfg_") and isinstance(v, str)}
    with db.tx() as conn:
        PM.connect(conn, ctx["biz"], ctx["actor"], f.get("provider", ""), cfg)
    _flash(request, "saved")
    return _go("/app/settings#payments")


@router.post("/app/payments/{account_id}/disconnect")
@_guard
async def pay_disconnect(request: Request, account_id: UUID):
    from ..services import payments as PM
    ctx = _own(request)
    await _form(request)
    with db.tx() as conn:
        PM.disconnect(conn, ctx["biz"]["id"], ctx["actor"], account_id)
    return _go("/app/settings#payments")


@router.post("/app/contacts/{contact_id}/pay")
@_guard
async def pay_request(request: Request, contact_id: UUID):
    from ..services import payments as PM
    ctx = _ctx(request)
    f = await _form(request)
    biz = ctx["biz"]
    try:
        amount = money.to_minor((f.get("amount") or "").replace(",", "").strip(), biz["currency"])
    except Exception:
        raise Invalid("amount: use digits, e.g. 350.00", "amount") from None
    try:
        pay = PM.create(biz, ctx["actor"], amount_minor=amount, description=f.get("description", ""), contact_id=contact_id)
    except PM.PayError as e:
        raise Invalid(str(e), "provider") from None
    if f.get("account_id"):
        try:
            X.send(biz, ctx["actor"], contact_id=contact_id, body=f"{pay['description']} — {money.fmt(amount, biz['currency'], packs.country(biz['country']).locale)}: {pay['url']}",
                   channel_account_id=UUID(f["account_id"]))
        except Blocked as b:
            _flash(request, f"r_{b.reason}", "warn")
    return _go(f"/app/contacts/{contact_id}#payments")


@router.post("/app/payments/{payment_id}/confirm")
@_guard
async def pay_confirm(request: Request, payment_id: UUID):
    from ..services import payments as PM
    ctx = _ctx(request)
    f = await _form(request)
    with db.tx() as conn:
        PM.owner_confirms(conn, ctx["biz"]["id"], ctx["actor"], payment_id)
    return _go(f.get("back") or "/app")


# ---------------------------------------------------------------- Follow-ups + campaigns
@router.get("/app/followups")
@_guard
async def followups_page(request: Request):
    from ..services import followups as FU
    ctx = _own(request)
    tz = ZoneInfo(ctx["biz"]["timezone"])
    with db.tx() as conn:
        rules = FU.automations(conn, ctx["biz"]["id"])
        cps = FU.campaigns(conn, ctx["biz"]["id"])
        box = FU.outbox(conn, ctx["biz"]["id"], limit=30)
    return render(request, "followups.html", ctx, rules=rules, cps=cps, box=box, triggers=FU.TRIGGERS,
                  now_local=datetime.now(tz).strftime("%Y-%m-%dT%H:%M"), page="followups")


@router.post("/app/followups/rules")
@_guard
async def followups_rule_new(request: Request):
    from ..services import followups as FU
    ctx = _own(request)
    f = await _form(request)
    try:
        minutes = int(f.get("minutes") or 0)
    except ValueError:
        raise Invalid("minutes: a whole number", "minutes") from None
    with db.tx() as conn:
        FU.create_automation(conn, ctx["biz"], ctx["actor"], trigger=f.get("trigger", ""), minutes=minutes,
                             message=f.get("message", ""), template=f.get("template"))
    return _go("/app/followups")


@router.post("/app/followups/rules/{rule_id}/delete")
@_guard
async def followups_rule_delete(request: Request, rule_id: UUID):
    from ..services import followups as FU
    ctx = _own(request)
    await _form(request)
    with db.tx() as conn:
        FU.delete_automation(conn, ctx["biz"]["id"], ctx["actor"], rule_id)
    return _go("/app/followups")


@router.post("/app/followups/campaigns")
@_guard
async def followups_campaign_new(request: Request):
    from ..services import followups as FU
    ctx = _own(request)
    f = await _form(request)
    when = None
    if f.get("send_at"):
        try:
            when = datetime.fromisoformat(f["send_at"]).replace(tzinfo=ZoneInfo(ctx["biz"]["timezone"]))
        except ValueError:
            raise Invalid("pick a date and time", "send_at") from None
    with db.tx() as conn:
        FU.create_campaign(conn, ctx["biz"], ctx["actor"], name=f.get("name", ""), message=f.get("message", ""),
                           tag=f.get("tag"), send_at=when, template=f.get("template"))
    return _go("/app/followups")


@router.post("/app/followups/campaigns/{cid}/cancel")
@_guard
async def followups_campaign_cancel(request: Request, cid: UUID):
    from ..services import followups as FU
    ctx = _own(request)
    await _form(request)
    with db.tx() as conn:
        FU.cancel_campaign(conn, ctx["biz"]["id"], ctx["actor"], cid)
    return _go("/app/followups")


# ---------------------------------------------------------------- Quotes /q/<token>
@router.get("/q/{token}")
@_guard
async def quote_page(request: Request, token: str):
    from ..services import quotes as QS
    with db.tx() as conn:
        q, biz = QS.by_token(conn, token)
        contact = conn.execute(select(db.contacts.c.name).where(db.contacts.c.id == q["contact_id"])).scalar() if q["contact_id"] else None
    ctx = {"biz": biz, "user": None, "actor": "quote_page", "role": None, "businesses": []}
    return render(request, "quote.html", ctx, q=q, contact_name=contact, pack=packs.country(biz["country"]),
                  expired=datetime.now(timezone.utc).date() > q["valid_until"])


@router.post("/q/{token}")
@_guard
async def quote_answer(request: Request, token: str):
    from ..services import quotes as QS
    f = await _form(request)
    ip = request.client.host if request.client else ""
    with db.tx() as conn:
        if f.get("act") == "decline":
            QS.decline(conn, token, f.get("reason", ""))
        else:
            QS.accept(conn, token, name=f.get("name", ""), agree=f.get("agree") == "1", ip=ip,
                      ua=request.headers.get("user-agent", ""))
    return _go(f"/q/{token}")


@router.post("/app/contacts/{contact_id}/quote")
@_guard
async def quote_new(request: Request, contact_id: UUID):
    from ..services import quotes as QS
    ctx = _ctx(request)
    f = await request.form()
    if not secrets.compare_digest(str(f.get("csrf", "")), str(request.session.get("csrf", "x"))):
        _flash(request, "expired_form", "warn")
        return _go(f"/app/contacts/{contact_id}")
    biz = ctx["biz"]
    lines = []
    for d, q, u in zip(f.getlist("line_desc"), f.getlist("line_qty"), f.getlist("line_unit")):
        if not str(d).strip():
            continue
        try:
            lines.append({"description": str(d), "qty": int(q or 1), "unit_minor": money.to_minor(str(u).replace(",", "").strip() or "0", biz["currency"])})
        except Exception:
            raise Invalid("check quantities and prices", "lines") from None
    with db.tx() as conn:
        q = QS.create(conn, biz, ctx["actor"], title=str(f.get("title", "")), lines=lines, contact_id=contact_id,
                      notes=str(f.get("notes", "")), valid_days=int(f.get("valid_days") or 15))
    if f.get("account_id"):
        try:
            X.send(biz, ctx["actor"], contact_id=contact_id, channel_account_id=UUID(str(f["account_id"])),
                   body=f"{q['title']}: {config.PUBLIC_URL()}/q/{q['token']}")
        except Blocked as b:
            _flash(request, f"r_{b.reason}", "warn")
    return _go(f"/app/contacts/{contact_id}#quotes")


@router.post("/app/quotes/{quote_id}/cancel")
@_guard
async def quote_cancel(request: Request, quote_id: UUID):
    from ..services import quotes as QS
    ctx = _ctx(request)
    f = await _form(request)
    with db.tx() as conn:
        QS.cancel(conn, ctx["biz"]["id"], ctx["actor"], quote_id)
    return _go(f.get("back") or "/app")


# ---------------------------------------------------------------- Booking settings (owner)
@router.post("/app/booking")
@_guard
async def booking_save(request: Request):
    from ..services import booking as BK
    ctx = _own(request)
    f = await _form(request)
    hours = {}
    for d in BK.DAYS:
        spans = []
        for k in ("1", "2"):
            a, b = (f.get(f"{d}_a{k}") or "").strip(), (f.get(f"{d}_b{k}") or "").strip()
            if a or b:
                spans.append([a, b])
        if spans:
            hours[d] = spans
    try:
        notice = int(f.get("notice") or 120)
        ahead = int(f.get("ahead") or 30)
    except ValueError:
        raise Invalid("use whole numbers", "notice") from None
    with db.tx() as conn:
        BK.save_settings(conn, ctx["biz"], ctx["actor"], enabled=f.get("enabled") == "1", slug=f.get("slug") or None,
                         hours=hours, notice_minutes=notice, days_ahead=ahead)
    _flash(request, "saved")
    return _go("/app/calendar#booking")


@router.post("/app/booking/services")
@_guard
async def booking_service_add(request: Request):
    from ..services import booking as BK
    ctx = _own(request)
    f = await _form(request)
    price = None
    if (f.get("price") or "").strip():
        try:
            price = money.to_minor(f["price"].replace(",", "").strip(), ctx["biz"]["currency"])
        except Exception:
            raise Invalid("price: use digits, e.g. 350.00", "price") from None
    try:
        mins = int(f.get("minutes") or 0)
    except ValueError:
        raise Invalid("minutes: a whole number", "minutes") from None
    with db.tx() as conn:
        BK.add_service(conn, ctx["biz"], ctx["actor"], name=f.get("name", ""), minutes=mins, price_minor=price)
    return _go("/app/calendar#booking")


@router.post("/app/booking/services/{service_id}/remove")
@_guard
async def booking_service_remove(request: Request, service_id: UUID):
    from ..services import booking as BK
    ctx = _own(request)
    await _form(request)
    with db.tx() as conn:
        BK.remove_service(conn, ctx["biz"]["id"], ctx["actor"], service_id)
    return _go("/app/calendar#booking")


# ---------------------------------------------------------------- Settings
@router.get("/app/settings")
@_guard
async def settings_page(request: Request):
    ctx = _own(request)
    return _settings(request, ctx)


def _settings(request: Request, ctx: dict, new_key: str | None = None, invite_link: str | None = None,
              webhook_secret: str | None = None):
    from ..services import team as TM
    from ..services import webhooks as WH
    biz = ctx["biz"]
    with db.tx() as conn:
        keys = B.keys(conn, biz["id"])
        team = TM.members(conn, biz["id"])
        invites = TM.pending(conn, biz["id"])
        hooks = WH.endpoints(conn, biz["id"])
        from ..services import payments as PM
        pay_accs = PM.accounts(conn, biz["id"])
    pack = packs.country(biz["country"])
    from ..services import ai as AIS
    with db.tx() as conn:
        ai_s = AIS.public_settings(conn, biz["id"])
    return render(request, "settings.html", ctx, keys=keys, new_key=new_key, pack=pack, tzs=_TZS, ai=ai_s,
                  team=team, invites=invites, invite_link=invite_link, hooks=hooks, webhook_secret=webhook_secret,
                  pay_accs=pay_accs, pay_fields=PM.PROVIDERS, pay_suggest=[PM.ALIASES.get(x, x) for x in pack.payments],
                  ai_models=AIS.PROVIDERS,
                  industries=_industries(request), page="settings")


@router.post("/app/settings")
@_guard
async def settings_save(request: Request):
    ctx = _own(request)
    f = await _form(request)
    with db.tx() as conn:
        B.patch(conn, ctx["biz"]["id"], ctx["actor"], name=f.get("name"), language=f.get("language"),
                timezone=f.get("timezone"), industry=f.get("industry"))
    _flash(request, "saved")
    return _go("/app/settings")


@router.post("/app/ai")
@_guard
async def ai_save(request: Request):
    from ..services import ai as AIS
    ctx = _own(request)
    f = await _form(request)
    prov = f.get("provider") or None
    if ctx["biz"]["sandbox"] and prov == "demo":
        prov = "demo"
    with db.tx() as conn:
        AIS.save(conn, ctx["biz"], ctx["actor"], provider=prov, api_key=(f.get("api_key") or "").strip() or None,
                 model=f.get("model") or None, mode=f.get("mode") or None, instructions=f.get("instructions", ""))
    _flash(request, "saved")
    return _go("/app/settings#ai")


@router.post("/app/keys")
@_guard
async def key_new(request: Request):
    ctx = _own(request)
    f = await _form(request)
    with db.tx() as conn:
        key, _ = B.new_key(conn, ctx["biz"]["id"], (f.get("name") or "app").strip()[:100])
    return _settings(request, ctx, new_key=key)        # shown once, in this response only — never in a cookie


@router.post("/app/keys/{key_id}/revoke")
@_guard
async def key_revoke(request: Request, key_id: UUID):
    ctx = _own(request)
    await _form(request)
    with db.tx() as conn:
        B.revoke_key(conn, ctx["biz"]["id"], ctx["actor"], key_id)
    return _go("/app/settings#keys")


# ---------------------------------------------------------------- Team
@router.post("/app/team/invite")
@_guard
async def team_invite(request: Request):
    from ..services import team as TM
    ctx = _own(request)
    f = await _form(request)
    with db.tx() as conn:
        _, token = TM.invite(conn, ctx["biz"], ctx["actor"], f.get("email", ""), f.get("role", "staff"))
    return _settings(request, ctx, invite_link=f"{config.PUBLIC_URL()}/join/{token}")   # shown once, never stored


@router.post("/app/team/invites/{inv_id}/revoke")
@_guard
async def team_revoke(request: Request, inv_id: UUID):
    from ..services import team as TM
    ctx = _own(request)
    await _form(request)
    with db.tx() as conn:
        TM.revoke(conn, ctx["biz"]["id"], ctx["actor"], inv_id)
    return _go("/app/settings#team")


@router.post("/app/team/{user_id}/remove")
@_guard
async def team_remove(request: Request, user_id: UUID):
    from ..services import team as TM
    ctx = _own(request)
    await _form(request)
    with db.tx() as conn:
        TM.remove(conn, ctx["biz"]["id"], ctx["actor"], user_id)
    return _go("/app/settings#team")


@router.get("/join/{token}")
@_guard
async def join_page(request: Request, token: str):
    from ..services import team as TM
    with db.tx() as conn:
        inv = TM.find(conn, token)
    signed_in = None
    if request.session.get("user_id"):
        with db.tx() as conn:
            signed_in = U.get(conn, UUID(request.session["user_id"]))
    return render(request, "join.html", None, inv=inv, token=token, signed_in=signed_in)


@router.post("/join/{token}")
@_guard
async def join_submit(request: Request, token: str):
    from ..services import team as TM
    f = await _form(request)
    with db.tx() as conn:
        if f.get("mode") == "accept" and request.session.get("user_id"):
            user = U.get(conn, UUID(request.session["user_id"]))
            inv = TM.accept(conn, token, user)
        elif f.get("mode") == "login":
            inv = TM.find(conn, token)
            user = U.authenticate(conn, inv["email"], f.get("password", ""))
            if not user:
                _flash(request, "bad_login", "warn")
                return _go(f"/join/{token}")
            TM.accept(conn, token, user)
        else:
            user, inv = TM.join_new_user(conn, token, f.get("name", ""), f.get("password", ""))
    request.session.clear()
    request.session.update(user_id=str(user["id"]), business_id=str(inv["business_id"]))
    return _go("/app")


@router.post("/app/switch")
@_guard
async def switch_business(request: Request):
    ctx = _ctx(request)
    f = await _form(request)
    try:
        target = UUID(f.get("business_id", ""))
    except ValueError:
        raise Invalid("pick a business", "business") from None
    if ctx["user"]:
        with db.tx() as conn:
            if U.membership(conn, ctx["user"]["id"], target):
                request.session["business_id"] = str(target)
    return _go("/app")


# ---------------------------------------------------------------- Webhooks (settings card)
@router.post("/app/webhooks")
@_guard
async def webhook_new(request: Request):
    from ..services import webhooks as WH
    ctx = _own(request)
    f = await _form(request)
    evs = [e.strip() for e in (f.get("events") or "*").split(",") if e.strip()]
    with db.tx() as conn:
        _, secret = WH.create(conn, ctx["biz"], ctx["actor"], f.get("url", ""), evs)
    return _settings(request, ctx, webhook_secret=secret)


@router.post("/app/webhooks/{endpoint_id}/delete")
@_guard
async def webhook_delete(request: Request, endpoint_id: UUID):
    from ..services import webhooks as WH
    ctx = _own(request)
    await _form(request)
    with db.tx() as conn:
        WH.delete(conn, ctx["biz"]["id"], ctx["actor"], endpoint_id)
    return _go("/app/settings#webhooks")


@router.get("/favicon.ico")
async def favicon():
    return Response((HERE / "static" / "icon.svg").read_bytes(), media_type="image/svg+xml")
