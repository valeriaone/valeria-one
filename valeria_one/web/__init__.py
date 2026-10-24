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
from .i18n import UI_LANGS, t, ui_lang

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
    return {"biz": biz, "user": user, "actor": f"user:{user['id']}" if user else "demo_visitor"}


def _lang(request: Request, biz: dict | None = None) -> str:
    return ui_lang(request.session.get("lang") or (biz or {}).get("language") or _accept(request))


def _accept(request: Request) -> str:
    al = (request.headers.get("accept-language") or "en").split(",")[0]
    return al.split("-")[0]


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
    return render(request, "auth.html", None, mode="home", demos=_demos())


def _demos() -> list[dict]:
    if not config.SANDBOX():
        return []
    with db.tx() as conn:
        rows = conn.execute(select(db.businesses).where(db.businesses.c.sandbox.is_(True), db.businesses.c.demo.is_(True))
                            .order_by(db.businesses.c.created_at).limit(12)).mappings()
        return [{"id": r["id"], "name": r["name"], "country": r["country"], "industry": r["industry"],
                 "flag": packs.country(r["country"]).label("en")} for r in rows]


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
    return sorted(((c.code, c.label(lang)) for c in packs.countries().values()), key=lambda x: x[1])


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
        pipe = D.pipeline(conn, biz)
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
    return render(request, "inbox.html", ctx, convs=convs, current=current, contact=contact, status=status,
                  templ_ok=templ_ok, in_window=in_window, has_sandbox=has_sandbox and biz["sandbox"], page="inbox")


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
    return _go(f"/app/inbox?c={conv}")


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
    stages = {s.key: s for s in packs.industry(biz["industry"]).stages}
    return render(request, "contact.html", ctx, c=c, ids=ids, convs=convs, deals=deals, appts=appts, reach=reach,
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
        pipe = D.pipeline(conn, biz)
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
    return render(request, "calendar.html", ctx, days=sorted(days.items()), names=names,
                  contacts=sorted(names.items(), key=lambda x: x[1].lower()),
                  today=datetime.now(tz).date().isoformat(), page="calendar")


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
    ctx = _ctx(request)
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
    ctx = _ctx(request)
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
    ctx = _ctx(request)
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
    ctx = _ctx(request)
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
    ctx = _ctx(request)
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
    ctx = _ctx(request)
    await _form(request)
    with db.tx() as conn:
        X.disconnect(conn, ctx["biz"]["id"], ctx["actor"], account_id)
    return _go("/app/channels")


# ---------------------------------------------------------------- Settings
@router.get("/app/settings")
@_guard
async def settings_page(request: Request):
    ctx = _ctx(request)
    return _settings(request, ctx)


def _settings(request: Request, ctx: dict, new_key: str | None = None):
    biz = ctx["biz"]
    with db.tx() as conn:
        keys = B.keys(conn, biz["id"])
    pack = packs.country(biz["country"])
    return render(request, "settings.html", ctx, keys=keys, new_key=new_key, pack=pack, tzs=_TZS,
                  industries=_industries(request), page="settings")


@router.post("/app/settings")
@_guard
async def settings_save(request: Request):
    ctx = _ctx(request)
    f = await _form(request)
    with db.tx() as conn:
        B.patch(conn, ctx["biz"]["id"], ctx["actor"], name=f.get("name"), language=f.get("language"),
                timezone=f.get("timezone"), industry=f.get("industry"))
    request.session.pop("lang", None)
    _flash(request, "saved")
    return _go("/app/settings")


@router.post("/app/keys")
@_guard
async def key_new(request: Request):
    ctx = _ctx(request)
    f = await _form(request)
    with db.tx() as conn:
        key, _ = B.new_key(conn, ctx["biz"]["id"], (f.get("name") or "app").strip()[:100])
    return _settings(request, ctx, new_key=key)        # shown once, in this response only — never in a cookie


@router.post("/app/keys/{key_id}/revoke")
@_guard
async def key_revoke(request: Request, key_id: UUID):
    ctx = _ctx(request)
    await _form(request)
    with db.tx() as conn:
        B.revoke_key(conn, ctx["biz"]["id"], ctx["actor"], key_id)
    return _go("/app/settings#keys")


@router.get("/favicon.ico")
async def favicon():
    return Response((HERE / "static" / "icon.svg").read_bytes(), media_type="image/svg+xml")
