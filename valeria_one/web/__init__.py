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

from .. import __version__, blocks, channels, config, db, kits, money, packs
from ..services import Blocked, Invalid, NotFound, log
from ..services import businesses as B
from ..services import contacts as C
from ..services import billing as BL
from ..services import instance as I
from ..services import deals as D
from ..services import inbox as X
from ..services import users as U
from . import world
from .i18n import NATIVE, UI_LANGS, from_accept_language, is_rtl, t, ui_lang

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))
# glob02: the public website's words and its 🌐 picker, for every public template
def _pwa_face():
    from .pwa import default_face
    return default_face()


templates.env.globals.update(default_face=lambda: bool(_pwa_face()), mkcopy=world.copy, world_picker=world.picker, world_homes=world.HOMES, native_names=NATIVE, t_=t)
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
        if biz["suspended_at"]:                              # paused by whoever hosts it
            other = next((b for b in (U.businesses_of(conn, user["id"]) if user else []) if not b["suspended_at"]), None)
            if other:
                request.session["business_id"] = str(other["id"])
                raise _Redirect("/app")
            request.session.clear()
            _flash(request, "business_paused", "warn")
            raise _Redirect("/login")
        role = U.membership(conn, user["id"], biz["id"])["role"] if user else "owner"
        mine = U.businesses_of(conn, user["id"]) if user else []
    if user and user.get("must_change_password") and request.url.path not in ("/app/new-password", "/logout"):
        raise _Redirect("/app/new-password")               # an account someone else set up: the person picks their own password
    if BL.paused(biz) and not request.url.path.startswith(_PAUSED_OPEN):
        _flash(request, "bill_paused_banner", "warn")      # launch months over, not paid: Billing, Settings, the export
        raise _Redirect("/app/billing")
    return {"biz": biz, "user": user, "actor": f"user:{user['id']}" if user else "demo_visitor", "role": role,
            "businesses": mine, "admin": I.is_admin(user)}


_PAUSED_OPEN = ("/app/billing", "/app/settings", "/app/export", "/app/switch", "/logout")


# bank numbers by the name banks print on them (the same in every language)
BANK_IDS = [("iban", "IBAN"), ("swift", "SWIFT / BIC"), ("routing", "Routing (ABA)"), ("sort_code", "Sort code"),
            ("ifsc", "IFSC"), ("bsb", "BSB"), ("clabe", "CLABE"), ("branch", "Branch / bank code")]


def _own(request: Request) -> dict:
    """Like _ctx, for owner-only pages (settings, channels, keys, AI, booking setup, webhooks, team)."""
    ctx = _ctx(request)
    if ctx["role"] != "owner":
        _flash(request, "owners_only", "warn")
        raise _Redirect("/app")
    return ctx


def _need(request: Request, ctx: dict, block: str) -> None:
    """Pages of a block the business switched off send the owner to Start, where it can be switched back on."""
    if not blocks.on(ctx["biz"], block):
        _flash(request, "block_off", "warn")
        raise _Redirect("/app/start" if ctx["role"] == "owner" else "/app")


def _admin(request: Request) -> dict:
    """The Server page and its actions: only the account that runs this server."""
    ctx = _ctx(request)
    if not ctx["admin"]:
        raise NotFound("page")
    return ctx


def _lang(request: Request, biz: dict | None = None) -> str:
    """The screen speaks the PERSON's language: their choice, else their browser, else the business's, else English.
    (The business's language is for what goes out to its customers, not for who reads the screen.)
    glob02: ?hl=<code> (the website's 🌐 picker) is that choice, made on any public page; it is kept for the next pages."""
    world.remember(request)
    if request.session.get("lang"):
        return ui_lang(request.session["lang"])
    if not biz and getattr(request.state, "studio_biz", None):   # a studio address: its business, even before signing in
        with db.tx() as conn:
            biz = B.get(conn, request.state.studio_biz)
    if biz and (biz.get("site") or {}).get("lang_fixed"):       # the business asked: every page opens in its language
        return ui_lang(biz.get("language"))
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


# the design studio: these pages wear the studio's own look (and are all a studio address serves)
STUDIO_PATHS = ("/studio", "/app/projects", "/app/rooms", "/app/boards", "/app/items", "/app/files", "/app/library",
                "/app/suppliers", "/app/orders", "/app/tasks", "/app/apps", "/app/canva")


def _in_studio(request: Request, biz: dict | None) -> bool:
    """app01: the studio wears its own look only at the business's own studio address; at the office address (PUBLIC_URL)
    the same pages sit in Valeria One's office, like every other page there."""
    return (bool(biz) and blocks.on(biz, "studio") and bool(getattr(request.state, "studio_biz", None))
            and request.url.path.startswith(STUDIO_PATHS))


def _office(name: str) -> bool:
    """The page is drawn inside the office shell (base.html's own body): Valeria One's look, never a business's.
    Pages that draw their own body — a business's site, booking, quote, portal, sign-in; the public pages — decide for themselves."""
    return "body" not in templates.env.get_template(name).blocks


def _brand(request: Request, biz: dict | None) -> dict | None:
    """The business whose own look this page wears: the one on screen when it asked for it (Website › its own look),
    or — before anyone signs in — the business whose address this is."""
    if not biz and getattr(request.state, "studio_biz", None):
        with db.tx() as conn:
            try:
                biz = B.get(conn, request.state.studio_biz)
            except NotFound:
                return None
    return biz if biz and (biz.get("site") or {}).get("brand") else None


def render(request: Request, name: str, ctx: dict | None, **kw) -> HTMLResponse:
    biz = ctx["biz"] if ctx else None
    lang = _lang(request, biz)
    loc = _locale(biz, lang) if biz else lang

    def _safe(fn, *a, **k):
        try:
            return fn(*a, **k)
        except Exception:
            return fn(*a, **{**k, "locale": lang})

    def fmt_money(minor, currency=None):              # a record keeps the currency it was made in
        return "" if minor is None else _safe(money.fmt, int(minor), currency or biz["currency"], locale=loc)

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
    office = _office(name)
    owner = bool(ctx) and ctx.get("role") == "owner"
    page = kw.get("page")
    data = {"request": request, "ctx": ctx, "biz": biz, "lang": lang, "langs": UI_LANGS, "_": lambda k: t(k, lang),
            "native": NATIVE, "rtl": is_rtl(lang), "country_name": lambda code: _country_name(code, lang),
            "bank_ids": BANK_IDS, "csrf": _csrf(request), "flashes": flashes, "money": fmt_money, "major": (lambda v: money.to_major(v, biz["currency"])) if biz else (lambda v: ""), "dt": fmt_dt, "day": fmt_day, "hm": fmt_time,
            "label": lambda labels: packs.text(labels, lang), "version": __version__, "sandbox": config.SANDBOX(),
            "words": (lambda w: blocks.word(biz, w, lang)) if biz else (lambda w: w),
            "blk": (lambda name: blocks.on(biz, name)) if biz else (lambda name: True),
            # kit01: the menu is the registry + this business's kit + its own choices; tool() = shown in its menu
            "tool": (lambda key: kits.visible(biz, key)) if biz else (lambda key: True),
            "nav": kits.menu(biz, role=ctx.get("role"), admin=bool(ctx.get("admin")), billing=BL.enabled(),
                             signed_in=bool(ctx.get("user"))) if office and biz and ctx else [],
            "hidden_here": page if owner and page in kits.OPTIONAL and not kits.is_on(biz, page) else None,
            "plan_allows": (lambda name: BL.allows(biz, name)) if biz else (lambda name: True),
            "billing_on": BL.enabled(), "terms_on": _terms_on(), "badge": _badge(biz),
            "signups_open": I.signup_open(),
            "industry": packs.industry(biz["industry"]) if biz else None,
            "layout": "base_studio.html" if _in_studio(request, biz) else "base.html",
            "office_url": "" if getattr(request.state, "studio_biz", None) else config.PUBLIC_URL(),
            "brand": None if office else _brand(request, biz),     # app01: a business's own look stays on its own pages
            "office": office,
            "signed_in": bool(request.session.get("business_id")),   # app01: the public pages offer "Open my app"
            "country": packs.country(biz["country"]) if biz else None,
            **kw}
    return templates.TemplateResponse(request, name, data)


def _terms_on() -> bool:
    """The server publishes Terms and Privacy (its public pages are on), so sign-up links to them."""
    from . import marketing
    return marketing.on()


def _badge(biz: dict | None) -> str | None:
    """'Made with Valeria One' on public pages: on a server that charges, it carries the business's referral code."""
    if not biz or (config.get("VO_BADGE") or "on").lower() == "off" or biz.get("sandbox"):
        return None
    if BL.enabled() and biz.get("billing_ref"):
        return f"{config.PUBLIC_URL()}/signup?ref={biz['billing_ref']}"
    return "https://github.com/valeriaone/valeria-one"


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
    """app01: the home page always shows the website, signed in or not (signed in, it offers "Open my app").
    A business's own address never gets here: studio_host_guard sends its "/" to its office first.
    Only a server with no public website (self-hosted, no marketing pages) still sends a signed-in person to the app,
    because its home is the sign-in screen."""
    if config.SANDBOX():                                    # the public demo server opens on a real landing page
        return render(request, "landing.html", None, demos=_demos())
    with db.tx() as conn:
        if I.needs_setup(conn):                             # a brand-new server: its first account comes first
            return _go("/setup")
    from . import marketing
    if marketing.on():                                      # a server that sells Valeria One opens on its home page
        # glob02: the visitor's language picks the home page — their own choice first (🌐 picker or the app's picker,
        # session "lang"), then the browser's Accept-Language. ?hl= is a choice made right now; ?c= keeps English.
        # Crawlers and link previews are never redirected: they get the page they asked for.
        picked = world.remember(request)
        if picked:
            return _go(world.HOMES[picked])
        if not request.query_params.get("c") and not world.is_bot(request):
            want = world.wanted(request)
            if want != "en":
                return _go(world.HOMES[want])
        return marketing.home(request)
    if request.session.get("business_id"):
        return _go("/app")
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
        order = ["US", "PH", "MX", "BR", "UG", "JP", "GB", "NG", "DE", "CO"]      # mixed continents, not one language first
        out = [{"id": r["id"], "name": r["name"], "country": r["country"], "industry": r["industry"], "flag": r["country"]}
               for r in rows]
        return sorted(out, key=lambda d: order.index(d["country"]) if d["country"] in order else 99)


@router.get("/login")
@_guard
async def login_page(request: Request):
    from ..services import social as SO
    return render(request, "auth.html", None, mode="login", demos=_demos(), social=SO.available())


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
    live = [b for b in bizs if not b["suspended_at"]]
    if not live:
        _flash(request, "business_paused", "warn")
        return _go("/login")
    request.session.clear()
    sb = getattr(request.state, "studio_biz", None)              # signing in at a studio address: that business, its studio
    pick = next((b for b in live if str(b["id"]) == str(sb)), None) if sb else live[0]
    if not pick:
        _flash(request, "bad_login", "warn")
        return _go("/login")
    request.session.update(user_id=str(user["id"]), business_id=str(pick["id"]))
    return _go("/app")


@router.get("/signup")
@_guard
async def signup_page(request: Request, ref: str | None = None):
    if ref:
        request.session["ref"] = ref.strip().upper()[:12]
    if not I.signup_open():
        _flash(request, "signup_closed", "warn")
        return _go("/login")
    from ..services import social as SO
    return render(request, "auth.html", None, mode="signup", countries=_countries(request), industries=_industries(request),
                  country_guess=_country_guess(request), social=SO.available(), **_launch_offer(request))


def _launch_offer(request) -> dict:
    """While the launch window is open, sign-up says plainly what is free and what comes after."""
    months = BL.launch_months() if BL.enabled() else 0
    if not months:
        return {"launch_months": 0, "launch_price": ""}
    usd = plan_price_words("")
    local = {c: w for c in packs.open_codes() if (w := plan_price_words(c)) != usd}     # where a local price is set
    return {"launch_months": months, "launch_price": plan_price_words(_country_guess(request)), "launch_usd": usd,
            "launch_local": local}


def plan_price_words(country: str) -> str:
    """The monthly price as the visitor from `country` would pay it: its local price where one is set
    (₱799, ₹999, $249 MXN + IVA), else US$39."""
    fake = {"country": country or "US", "currency": ""}
    cur = BL.currency_for(fake)
    q = BL.quote(fake, "starter")
    price = q["price_minor"]
    if cur == "USD":
        return f"US${price // 100}" if price % 100 == 0 else money.fmt(price, "USD", "en_US")
    words = money.fmt(price, cur, packs.country(country).locale).replace(".00", "").replace(",00", "")
    if cur == "MXN":
        words = f"${price // 100} MXN"
    return words + (f" + {q['tax_name']}" if q["tax_name"] else "")


_signup_hits: dict = {}
_reset_hits: dict = {}


def _limit(store: dict, request: Request, n: int, window: int = 3600) -> bool:
    import time as _t
    ip = request.client.host if request.client else "?"
    q = [x for x in store.get(ip, []) if _t.time() - x < window]
    if len(q) >= n:
        store[ip] = q
        return False
    store[ip] = q + [_t.time()]
    return True


# ---------------------------------------------------------------- forgot my password
@router.get("/forgot")
@_guard
async def forgot_page(request: Request):
    return render(request, "auth.html", None, mode="forgot")


@router.post("/forgot")
@_guard
async def forgot_submit(request: Request):
    from ..services import mailer
    f = await _form(request)
    if _limit(_reset_hits, request, 5):
        lang = _lang(request)
        with db.tx() as conn:
            r = U.start_reset(conn, f.get("email", ""))
        if r:
            user, token = r
            mailer.send(user["email"], t("mail_reset_subject", lang),
                        t("mail_reset_body", lang).format(link=f"{config.PUBLIC_URL()}/reset/{token}"))
    _flash(request, "reset_sent")                            # the same answer whether or not the email has an account
    return _go("/login")


@router.get("/reset/{token}")
@_guard
async def reset_page(request: Request, token: str):
    with db.tx() as conn:
        u = U.reset_user(conn, token)
    if not u:
        _flash(request, "reset_expired", "warn")
        return _go("/forgot")
    return render(request, "auth.html", None, mode="reset", token=token)


@router.post("/reset/{token}")
@_guard
async def reset_submit(request: Request, token: str):
    f = await _form(request)
    with db.tx() as conn:
        u = U.finish_reset(conn, token, f.get("password", ""))
        live = [b for b in U.businesses_of(conn, u["id"]) if not b["suspended_at"]]
    request.session.clear()
    if not live:
        _flash(request, "password_changed")
        return _go("/login")
    request.session.update(user_id=str(u["id"]), business_id=str(live[0]["id"]))
    _flash(request, "password_changed")
    return _go("/app")


@router.get("/app/new-password")
@_guard
async def new_password_page(request: Request):
    ctx = _ctx(request)
    if not (ctx["user"] or {}).get("must_change_password"):
        return _go("/app")
    return render(request, "auth.html", None, mode="newpass")


@router.post("/app/new-password")
@_guard
async def new_password_save(request: Request):
    ctx = _ctx(request)
    f = await _form(request)
    if not ctx["user"]:
        return _go("/app")
    with db.tx() as conn:
        U.set_password(conn, ctx["user"]["id"], f.get("password", ""))
        log(conn, ctx["biz"]["id"], "user.password_set", ctx["actor"], ctx["user"]["id"])
    _flash(request, "password_changed")
    return _go("/studio" if getattr(request.state, "studio_biz", None) else "/app")


# ---------------------------------------------------------------- plans and prices (servers that charge), or free
@router.get("/pricing")
@_guard
async def pricing_page(request: Request):
    if not BL.enabled():                                # a server that doesn't charge: the page says it's free
        return render(request, "free.html", None)
    pr = BL.prices()
    seller = (config.get("VO_SELLER_COUNTRY") or "").upper()
    tax = packs.country(seller).tax if seller else None
    return render(request, "pricing.html", None, prices=pr, plans=BL.PLANS, trial_days=BL.TRIAL_DAYS,
                  tax_name=tax.name if tax and tax.mode == "fixed" else None, seller=seller,
                  fmt=BL.fmt)


# ---------------------------------------------------------------- first-time setup of a new server
@router.get("/setup")
@_guard
async def setup_page(request: Request, k: str | None = None):
    if config.SANDBOX():
        raise NotFound("page")
    with db.tx() as conn:
        if not I.needs_setup(conn):
            return _go("/login")
    ok = bool(k) and secrets.compare_digest(k, I.setup_token())
    return render(request, "setup.html", None, ok=ok, k=k if ok else "", countries=_countries(request),
                  industries=_industries(request))


@router.post("/setup")
@_guard
async def setup_submit(request: Request):
    if config.SANDBOX():
        raise NotFound("page")
    f = await _form(request)
    with db.tx() as conn:
        user, biz = I.setup(conn, token=f.get("k", ""), email=f.get("email", ""), name=f.get("name", ""),
                            password=f.get("password", ""), business_name=f.get("business_name", ""),
                            country=f.get("country", ""), industry=f.get("industry", "generic"))
    request.session.clear()
    request.session.update(user_id=str(user["id"]), business_id=str(biz["id"]))
    return _go("/app/start")


def _countries(request):
    """Every country a business can sign up from, named in the reader's language."""
    lang = _lang(request)
    return sorted(((c, _country_name(c, lang)) for c in packs.open_codes()), key=lambda x: x[1])


def _country_guess(request) -> str:
    """The country the browser's language points at (en-PH -> PH), to preselect; '' = make them choose."""
    for part in (request.headers.get("accept-language") or "").split(","):
        bits = part.split(";")[0].strip().replace("_", "-").split("-")
        if len(bits) > 1 and len(bits[-1]) == 2 and bits[-1].upper() in packs.open_codes():
            return bits[-1].upper()
    return ""


def _industries(request):
    lang = _lang(request)
    return [(i.code, packs.text(i.name, lang)) for i in packs.industries().values()]


@router.post("/signup")
@_guard
async def signup(request: Request):
    f = await _form(request)
    if not I.signup_open():
        _flash(request, "signup_closed", "warn")
        return _go("/login")
    if not _limit(_signup_hits, request, 10):
        raise Invalid("too many new accounts from this connection; try again later", "rate")
    sandbox = config.SANDBOX()
    with db.tx() as conn:
        user, biz = U.register(conn, email=f.get("email", ""), name=f.get("name", ""), password=f.get("password", ""),
                               business_name=f.get("business_name", ""), country=f.get("country", ""),
                               industry=f.get("industry", "generic"), sandbox=sandbox)
        ref_biz = BL.referrer_by_ref(conn, request.session.get("ref"))
        if ref_biz and ref_biz != biz["id"]:
            conn.execute(db.businesses.update().where(db.businesses.c.id == biz["id"]).values(referred_by=ref_biz))
        if sandbox:
            X.connect(conn, biz, "system", channel="sandbox", name="Sandbox")
            from ..services import ai as AIS
            AIS.save(conn, biz, "system", provider="demo", mode="auto")
    request.session.clear()
    request.session.update(user_id=str(user["id"]), business_id=str(biz["id"]))
    return _go("/app" if sandbox else "/app/connect")


# ---------------------------------------------------------------- continue with Facebook / Google
@router.get("/auth/{provider}")
@_guard
async def social_start(request: Request, provider: str):
    from ..services import social as SO
    if provider not in SO.available():
        raise NotFound("page")
    state = secrets.token_urlsafe(24)
    request.session["so_state"] = f"{provider}:{state}"
    return RedirectResponse(SO.auth_url(provider, state), status_code=303)


@router.get("/auth/{provider}/callback")
@_guard
async def social_callback(request: Request, provider: str, code: str | None = None, state: str | None = None,
                          error: str | None = None):
    """Back from Facebook / Google: an account here -> signed in; none -> one short screen to finish."""
    import httpx
    from ..services import social as SO
    expected = request.session.pop("so_state", None)
    if (provider not in SO.available() or error or not code or not expected
            or not secrets.compare_digest(expected, f"{provider}:{state or ''}")):
        _flash(request, "social_failed", "warn")
        return _go("/login")
    try:
        ident = SO.identify(provider, code)
    except (SO.SocialError, httpx.HTTPError):
        _flash(request, "social_failed", "warn")
        return _go("/login")
    with db.tx() as conn:
        user = SO.find_user(conn, ident)
        mine = U.businesses_of(conn, user["id"]) if user else []
        live = [b for b in mine if not b["suspended_at"]]
        key = None if user else SO.hold(conn, ident)
    if user:
        if not live:
            _flash(request, "business_paused" if mine else "bad_login", "warn")
            return _go("/login")
        request.session.clear()
        request.session.update(user_id=str(user["id"]), business_id=str(live[0]["id"]))
        return _go("/app")
    if not I.signup_open():
        _flash(request, "signup_closed", "warn")
        return _go("/login")
    request.session["so_key"] = key
    return _go("/signup/finish")


def _finish_ctx(request: Request, ident: dict) -> dict:
    from ..services import social as SO
    pages = ident.get("pages") or []
    first = pages[0] if pages else {}
    return {"ident": {"provider": ident["provider"], "name": ident.get("name"), "email": ident.get("email")},
            "pages": [{"id": p["id"], "name": p["name"], "ig": (p.get("ig") or {}).get("username"), "category": p.get("category")}
                      for p in pages],
            "suggest_name": first.get("name") or "", "industry_guess": SO.industry_guess(first.get("category", "")),
            "country_guess": next((p["country"] for p in pages if p.get("country")), "") or _country_guess(request)}


@router.get("/signup/finish")
@_guard
async def social_finish_page(request: Request):
    from ..services import social as SO
    with db.tx() as conn:
        ident = SO.held(conn, request.session.get("so_key"))
    if not ident:
        _flash(request, "expired_form", "warn")
        return _go("/signup")
    return render(request, "auth.html", None, mode="finish", countries=_countries(request), industries=_industries(request),
                  **_finish_ctx(request, ident), **_launch_offer(request))


@router.post("/signup/finish")
@_guard
async def social_finish(request: Request):
    from ..services import social as SO
    f = await _multi_form(request)
    if not I.signup_open():
        _flash(request, "signup_closed", "warn")
        return _go("/login")
    if not _limit(_signup_hits, request, 10):
        raise Invalid("too many new accounts from this connection; try again later", "rate")
    with db.tx() as conn:
        ident = SO.held(conn, request.session.get("so_key"), take=True)
        if not ident:
            _flash(request, "expired_form", "warn")
            return _go("/signup")
        user, biz, problems = SO.create(conn, ident, business_name=str(f.get("business_name", "")),
                                        country=str(f.get("country", "")), industry=str(f.get("industry") or "generic"),
                                        email=str(f.get("email", "")), pages=[str(x) for x in f.getlist("page")])
        ref_biz = BL.referrer_by_ref(conn, request.session.get("ref"))
        if ref_biz and ref_biz != biz["id"]:
            conn.execute(db.businesses.update().where(db.businesses.c.id == biz["id"]).values(referred_by=ref_biz))
    request.session.clear()
    request.session.update(user_id=str(user["id"]), business_id=str(biz["id"]))
    _flash(request, "social_pages_failed" if problems else "welcome_connect", "warn" if problems else "ok")
    return _go("/app/connect")


@router.get("/i/{ref}")
async def referral_link(ref: str):
    """The short link on the "— Valeria One" line and on invites: sign-up, with the referrer's code."""
    code = "".join(ch for ch in ref.upper() if ch.isalnum() or ch == "-")[:12]
    return RedirectResponse(f"/signup?ref={code}" if code else "/signup", status_code=302)


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
        from .i18n import has as _has                          # only what a person wants to see: internal saves stay out
        events = [e for e in conn.execute(select(db.events).where(db.events.c.business_id == biz["id"])
                                          .order_by(db.events.c.id.desc()).limit(200)).mappings() if _has("ev_" + e["type"])][:12]
        steps = _start_steps(conn, biz) if ctx["role"] == "owner" else []
        alerts = _alerts(conn, biz) if ctx["role"] == "owner" else []
        from ..services import owner as OW
        from ..services import reminders as RMD
        from ..services import projects as PRJ
        end_today = start + timedelta(days=1)
        due = [x for x in PRJ.tasks(conn, biz["id"], assignee_id=ctx["user"]["id"] if ctx["user"] and ctx["role"] != "owner" else None)
               if (x["due_at"] and x["due_at"] < end_today) or (not x["due_at"] and x["due_on"] and x["due_on"] <= start.date())]
        hint_recipes = ctx["role"] == "owner" and kits.visible(biz, "recipes") and not conn.execute(
            select(db.recipes.c.id).where(db.recipes.c.business_id == biz["id"], db.recipes.c.active).limit(1)).first()
        from ..services import invoices as IVS                  # v0.18: monthly recurring revenue; one-tap setups; campaigns
        mrr = IVS.mrr(conn, biz["id"]) if blocks.on(biz, "payments") else 0
        hint_setup = ctx["role"] == "owner" and not conn.execute(select(db.snapshot_applies.c.id).where(
            db.snapshot_applies.c.business_id == biz["id"]).limit(1)).first()
        hint_campaign = ctx["role"] == "owner" and kits.visible(biz, "campaigns") and not hint_recipes and not conn.execute(
            select(db.campaigns.c.id).where(db.campaigns.c.business_id == biz["id"], db.campaigns.c.channel.isnot(None)).limit(1)).first() \
            and conn.execute(select(db.contacts.c.id).where(db.contacts.c.business_id == biz["id"], db.contacts.c.marketing_consent)
                             .limit(1)).first() is not None
        tip = kits.suggestion(conn, biz) if ctx["role"] == "owner" else None      # kit01: Valeria's one suggestion
        from ..services import inventory as INV                 # v0.20: what is running low; what the register took today
        from ..services import pos as POS
        low = INV.low(conn, biz["id"]) if kits.visible(biz, "inventory") else []
        sold = POS.day_summary(conn, biz, POS.today_of(biz)) if kits.visible(biz, "pos") else None
        from ..services import posts as PO                      # v0.21: posts whose time came, to post by hand
        hand_due = PO.hand_due(conn, biz["id"]) if ctx["role"] == "owner" and blocks.on(biz, "posts") else []
        va = {"questions": len(OW.waiting(conn, biz["id"])) if ctx["role"] == "owner" else 0,
              "linked": any(n["channel"] for n in OW.numbers(conn, biz["id"])), "paused": OW.paused(conn, biz["id"]),
              "unconfirmed": RMD.unconfirmed(conn, biz, datetime.now(tz).date() + timedelta(days=1))} if blocks.on(biz, "assistant") else None
    open_value = sum(s["amount_minor"] for s in pipe if s["outcome"] == "open")
    need = [s for s in steps if not s["optional"]]
    bill = BL.status(biz) if BL.enabled() and ctx["role"] == "owner" else None
    return render(request, "today.html", ctx, waiting=waiting, open_count=len(convs), appts=appts, pipe=pipe,
                  open_value=open_value, events=events, now_local=datetime.now(tz), page="today",
                  start=None if all(s["done"] for s in need) else {"done": sum(s["done"] for s in need), "total": len(need)},
                  bill=bill, alerts=alerts, va=va, due=due, hint_recipes=hint_recipes, mrr=mrr, hint_setup=hint_setup,
                  hint_campaign=hint_campaign, tip=tip, low=low, sold=sold, hand_due=hand_due)


def _alerts(conn, biz: dict) -> list[dict]:
    """What needs the owner's tap today: Google or TikTok to reconnect, posts that didn't go out."""
    from ..services import gbp as GB
    out = []
    rem = GB.reminder(GB.link(conn, biz["id"]))
    if rem:
        out.append({"key": "al_google_" + rem, "href": "/app/google"})
    A = db.channel_accounts
    if conn.execute(select(A.c.id).where(A.c.business_id == biz["id"], A.c.channel == "tiktok", A.c.active,
                                         A.c.config["error"].astext.isnot(None), A.c.config["error"].astext != "null")).first():
        out.append({"key": "al_tiktok", "href": "/app/tiktok"})
    if blocks.on(biz, "posts") and conn.execute(select(db.posts.c.id).where(db.posts.c.business_id == biz["id"],
                                                                            db.posts.c.status == "failed").limit(1)).first():
        out.append({"key": "al_posts", "href": "/app/posts"})
    return out


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
    from ..services import replies as SRP                      # v0.18: saved replies, one tap into the reply box
    with db.tx() as conn:
        saved = SRP.list_(conn, biz["id"]) if current else []
    draft = request.session.pop("ai_draft", None) if current else None
    if draft and draft.get("conv") != str(current["id"]):
        draft = None
    return render(request, "inbox.html", ctx, convs=convs, current=current, contact=contact, status=status,
                  templ_ok=templ_ok, in_window=in_window, has_sandbox=has_sandbox and biz["sandbox"], ai=ai_s,
                  draft=draft, saved=saved, page="inbox")


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
    if f.get("ai_draft") and (f.get("body") or "").strip():      # the AI's draft, changed by a person: Valeria learns their voice
        from ..services import style as STY
        with db.tx() as conn:
            STY.record(conn, biz["id"], "reply", f["ai_draft"], f["body"])
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
    st = X.receive(acc, HookRequest("POST", "", {}, {}, json.dumps({"from": "sim-" + name.lower().replace(" ", "-"),
                                                                     "name": name, "body": f.get("body") or ""}).encode()))
    if st.get("owner_ids"):                                # the owner's own test line: Valeria answers on /app/valeria
        from ..services import autoreply
        for mid in st["owner_ids"]:
            autoreply.respond_owner(biz["id"], mid)
        return _go("/app/valeria#chat")
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
    _need(request, ctx, "comments")
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
async def contacts_page(request: Request, q: str | None = None, field: str | None = None, value: str | None = None):
    from ..services import fields as CF
    ctx = _ctx(request)
    biz = ctx["biz"]
    lang = _lang(request, biz)
    with db.tx() as conn:
        rows = C.search(conn, biz["id"], q=q, limit=200, field=field or None, value=value)
        fl = CF.list_(conn, biz["id"], "contact")
    shown = [f for f in fl if f["show_in_list"]]
    for r in rows:
        r["shown_fields"] = [(f["label"], CF.display(biz, f, (r["custom"] or {}).get(f["key"]), lang)) for f in shown
                             if (r["custom"] or {}).get(f["key"]) not in (None, "")]
    return render(request, "contacts.html", ctx, rows=rows, q=q or "", page="contacts", fl=fl, field=field or "", value=value or "",
                  filtered=bool(q or field))


def _contact_form(f: dict) -> dict:
    d = {k: f.get(k, "") for k in ("name", "phone", "email", "notes")}
    d["tags"] = [x for x in (f.get("tags") or "").split(",") if x.strip()]
    if "assignee_id" in f:
        d["assignee_id"] = f.get("assignee_id") or None
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
    from ..services import reviews as RV
    from ..services import growth as GR
    with db.tx() as conn:
        asked = RV.last_asked(conn, biz["id"], contact_id)
        friend = {"link": GR.link_of(conn, biz["id"], contact_id), "via": GR.came_through(conn, biz["id"], contact_id),
                  "brought": GR.brought(conn, biz["id"], contact_id),
                  "rewards": any(GR.settings(conn, biz["id"])[k] for k in ("reward_friend", "reward_referrer"))}
    from ..services import loyalty as LY
    from ..services import fields as CF
    from ..services import projects as PRJ
    from ..services import team as TM
    from .crm import field_rows
    with db.tx() as conn:
        loyal = {"visits": LY.count(conn, biz["id"], contact_id), "rule": LY.rule(conn, biz["id"]),
                 "rewards": LY.rewards(conn, biz["id"], contact_id=contact_id, limit=10)}
        cf_rows = field_rows(biz, CF.list_(conn, biz["id"], "contact"), c["custom"], _lang(request, biz))
        c_tasks = PRJ.tasks(conn, biz["id"], contact_id=contact_id)
        people = TM.members(conn, biz["id"])
        from ..services import invoices as IVS                # v0.18: invoices, subscriptions, calls, drips, workflows
        from ..services import sequences as SQ
        from ..services import voice_agent as VAG
        from ..services import workflows as WF
        v18 = {"invoices": IVS.list_(conn, biz["id"], contact_id=contact_id, limit=20),
               "subs": IVS.subscriptions(conn, biz["id"], contact_id=contact_id), "plans": IVS.plans(conn, biz["id"]),
               "calls": VAG.calls_of(conn, biz["id"], contact_id=contact_id, limit=10),
               "enrolled": SQ.of_contact(conn, biz["id"], contact_id),
               "seqs": [q for q in SQ.list_(conn, biz["id"]) if q["active"]],
               "runs": WF.runs(conn, biz["id"], contact_id=contact_id, limit=10),
               "wfs": [w for w in WF.list_(conn, biz["id"]) if w["active"]]}
    lang = _lang(request, biz)
    for r in v18["runs"]:
        r["words"] = [WF.line_text(x, lang) for x in (r.get("history") or [])]
    return render(request, "contact.html", ctx, c=c, ids=ids, convs=convs, deals=deals, appts=appts, reach=reach, asked=asked,
                  pays=pays, pay_accs=pay_accs, qts=qts, public_url=config.PUBLIC_URL(),
                  stages=stages, friend=friend, loyal=loyal, page="contacts", cf_rows=cf_rows, c_tasks=c_tasks, people=people,
                  v18=v18, today_iso=datetime.now(ZoneInfo(biz["timezone"])).date().isoformat())


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


# ---------------------------------------------------------------- Pipeline (the board and the deal page: web/crm.py)
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
    back = f.get("back") or ""
    return _go(back if back.startswith("/app/") else "/app/pipeline")


# ---------------------------------------------------------------- Calendar
@router.get("/app/calendar")
@_guard
async def calendar_page(request: Request):
    ctx = _ctx(request)
    _need(request, ctx, "calendar")
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
        from ..services import chatcal
        feed = chatcal.feed_url(conn, biz["id"], ctx["user"]["id"]) if ctx["user"] else None   # sch01: their phone's calendar
    return render(request, "calendar.html", ctx, days=sorted(days.items()), names=names, feed=feed,
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


# ---------------------------------------------------------------- Connect: every account the business already has, one screen
@router.get("/app/connect")
@_guard
async def connect_page(request: Request):
    """One screen, one row per account: WhatsApp, Facebook + Instagram, email, payments, calendar, website, TikTok.
    Each shows a green tick once live; what isn't connected yet is one tap (or one short form) away.
    Everything runs on the business's own accounts."""
    from ..services import directory as DR
    from ..services import gbp as GB
    from ..services import gcal
    from ..services import meta as MT
    from ..services import payments as PM
    from ..services import tiktok as TT
    ctx = _own(request)
    biz = ctx["biz"]
    with db.tx() as conn:
        accs = [a for a in X.accounts(conn, biz["id"]) if biz["sandbox"] or a["channel"] != "sandbox"]
        pays = PM.accounts(conn, biz["id"])
        cal = gcal.link(conn, biz["id"])
        gb = GB.link(conn, biz["id"])
        lst = DR.settings(conn, biz["id"])
    by = lambda *chs: [a["name"] for a in accs if a["channel"] in chs]    # noqa: E731
    site = biz["site"] or {}
    rows = [
        {"key": "whatsapp", "done": by("whatsapp", "whatsapp_twilio"), "href": "/app/channels#manual", "meta": "wa"},
        {"key": "facebook", "done": by("messenger", "instagram"), "href": "/app/channels#manual", "meta": "pages"},
        {"key": "email", "done": by("email"), "href": "/app/channels?c=email#manual"},
        {"key": "more", "done": by("sms", "telegram", "viber", "webhook"), "href": "/app/channels#manual"},
        {"key": "payments", "done": [p["provider"] for p in pays], "href": "/app/settings#payments"},
        {"key": "calendar", "done": [cal.get("account_email") or "Google"] if cal else [],
         "href": "/app/calendar/google" if gcal.configured() and not biz["sandbox"] else "/app/calendar#booking"},
        {"key": "website", "done": [f"/s/{biz['slug']}"] if biz["site_enabled"] and biz["slug"] else [], "href": "/app/website"},
        {"key": "tiktok", "done": by("tiktok") or (["@" + site["tiktok"]] if site.get("tiktok") else []),
         "href": "/app/tiktok" if TT.configured() and not biz["sandbox"] else "/app/website#links"},
        {"key": "google", "done": [gb.get("location_title") or "Google"] if gb and gb.get("location") and gb["status"] == "ok" else [],
         "href": "/app/google"},
        {"key": "directory", "done": [f"/directory/{biz['slug']}"] if lst["listed"] and biz.get("slug") else [], "href": "/app/directory"},
    ]
    meta = MT.settings() if MT.configured() and not biz["sandbox"] else None
    invite = f"{config.PUBLIC_URL()}/i/{biz['billing_ref']}" if biz.get("billing_ref") and not biz["sandbox"] else None
    return render(request, "connect.html", ctx, rows=rows, meta=meta, invite=invite,
                  done_n=sum(1 for r in rows if r["done"]), graph_version=config.get("VO_GRAPH_VERSION", "v23.0"),
                  page="connect")


# ---------------------------------------------------------------- Channels
@router.get("/app/channels")
@_guard
async def channels_page(request: Request):
    ctx = _own(request)
    biz = ctx["biz"]
    with db.tx() as conn:
        accs = X.accounts(conn, biz["id"])
    pack = packs.country(biz["country"])
    home = []
    for c in pack.channels:                                   # WhatsApp through Twilio sits right next to WhatsApp
        home += [x for x in ([c, "whatsapp_twilio"] if c == "whatsapp" else [c]) if x in channels.available()]
    order = [c for c in home + [c for c in channels.available() if c not in home and c != "sandbox"] if channels.adapter(c).manual]
    avail = [{"channel": c, "needs": channels.adapter(c).check_config({}), "home": c in home} for c in order]
    from ..services import meta as MT
    with db.tx() as conn:
        lead_rows = MT.leads(conn, biz["id"], limit=20)
    meta = MT.settings() if MT.configured() and not biz["sandbox"] else None
    return render(request, "channels.html", ctx, accs=accs, avail=avail, meta=meta, leads=lead_rows,
                  graph_version=config.get("VO_GRAPH_VERSION", "v23.0"), page="channels", pick=request.query_params.get("c"))


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
    return _go(_back(f))


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
    return render(request, "meta_pages.html", ctx, pages=pages, page="channels", back=_back(f))


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
        return _go(_back(f))
    try:
        with db.tx() as conn:
            MT.connect_pages(conn, ctx["biz"], ctx["actor"], pages, [str(x) for x in f.getlist("page")])
    except MT.MetaError as e:
        raise Invalid(str(e), "meta") from None
    _flash(request, "meta_connected")
    return _go(_back(f))


def _back(f) -> str:
    """Where a one-tap connection returns: the page it was started from (only our own two)."""
    return "/app/connect" if str(f.get("back", "")) == "/app/connect" else "/app/channels"


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
    from ..services import chatcal
    return chatcal.sig(appt_id)


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
        _friend_came(request, conn, ctx["biz"], res["contact"], "booking")
    a = res["appointment"]
    return render(request, "book.html", ctx, step="done", svc=res["service"], appt=a, slug=slug,
                  ics_url=f"/b/{slug}/ics/{a['id']}/{_sig(a['id'])}", gcal_url=f"/b/{slug}/gcal/{a['id']}/{_sig(a['id'])}")


@router.get("/b/{slug}/ics/{appt_id}/{sig}")
async def book_ics(request: Request, slug: str, appt_id: UUID, sig: str):
    """A customer's booking as a calendar file (signed link; sch01: also for bookings made in a chat, and after the
    owner switches the booking page off — the same UID with a higher SEQUENCE once it moved or was cancelled)."""
    from ..services import booking as BK
    from ..services import chatcal as CC
    with db.tx() as conn:
        got = CC.public_appointment(conn, slug, appt_id, sig)
    if not got:
        return HTMLResponse("Not found", status_code=404)
    biz, a = got
    return Response(BK.ics(biz, a), media_type="text/calendar",
                    headers={"Content-Disposition": 'attachment; filename="cita.ics"', "Cache-Control": "no-store"})


@router.get("/b/{slug}/gcal/{appt_id}/{sig}")
async def book_gcal(request: Request, slug: str, appt_id: UUID, sig: str):
    """sch01: "Add to Google Calendar" — Google's own add-event page, filled in with the booking as it is now."""
    from ..services import chatcal as CC
    with db.tx() as conn:
        got = CC.public_appointment(conn, slug, appt_id, sig)
    if not got or got[1]["status"] == "cancelled":
        return HTMLResponse("Not found", status_code=404)
    return RedirectResponse(CC.google_url(*got), status_code=303)


@router.get("/cal/{business_id}/{user_id}/{sig}.ics")
async def calendar_feed(request: Request, business_id: UUID, user_id: UUID, sig: str):
    """sch01: the business's calendar for one person's phone (a private link per person; a new one kills the old)."""
    from ..services import chatcal as CC
    with db.tx() as conn:
        body = CC.feed(conn, business_id, user_id, sig)
    if body is None:
        return HTMLResponse("Not found", status_code=404)
    return Response(body, media_type="text/calendar", headers={"Cache-Control": "no-store"})


@router.post("/app/calendar/feed/new")
@_guard
async def calendar_feed_new(request: Request):
    from ..services import chatcal as CC
    ctx = _ctx(request)
    await _form(request)
    if not ctx["user"]:
        raise NotFound("page")
    with db.tx() as conn:
        CC.new_feed(conn, ctx["biz"]["id"], ctx["user"]["id"])
    _flash(request, "sc_feed_newed")
    return _go("/app/calendar#feed")


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


@router.get("/privacy/{slug}")
@_guard
async def business_privacy(request: Request, slug: str):
    """Each business's own privacy notice, in the visitor's language: what it keeps, why, and how to ask for it,
    with its country's law. Linked from its website, booking and payment pages."""
    with db.tx() as conn:
        row = conn.execute(select(db.businesses).where(db.businesses.c.slug == slug)).mappings().first()
    if not row or row["suspended_at"]:
        raise NotFound("page")
    biz = dict(row)
    ctx = {"biz": biz, "user": None, "actor": "website", "role": None, "businesses": []}
    resp = render(request, "privacy_biz.html", ctx, s=biz["site"] or {}, law=packs.country(biz["country"]).privacy_law.name)
    resp.headers["X-Robots-Tag"] = "noindex"
    return resp


@router.get("/s/{slug}")
@_guard
async def site_page(request: Request, slug: str):
    from ..services import booking as BK
    from ..services import site as ST
    with db.tx() as conn:
        biz = ST.public(conn, slug)
        svcs = BK.services(conn, biz["id"], active_only=True)
        from ..services import projects as PRJ
        from ..services import studio as STU
        work = PRJ.published(conn, biz["id"]) if blocks.on(biz, "projects") else []
    url = f"{config.PUBLIC_URL()}/s/{biz['slug']}"
    ctx = {"biz": biz, "user": None, "actor": "website", "role": None, "businesses": []}
    from .crm import site_forms
    resp = render(request, "site.html", ctx, s=biz["site"] or {}, svcs=svcs, url=url, bdays=BK.DAYS, work=work, styles=STU.STYLES,
                  ld=ST.json_ld(biz, svcs, url), sent=request.query_params.get("sent") == "1", forms=site_forms(biz))
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
        res = ST.contact_form(conn, biz, name=f.get("name", ""), message=f.get("message", ""),
                              email=reach if "@" in reach else "", phone="" if "@" in reach else reach)
        _friend_came(request, conn, biz, res["contact"], "website")
    return _go(f"/s/{slug}?sent=1#contact")


@router.get("/app/website")
@_guard
async def website_page(request: Request):
    ctx = _own(request)
    _need(request, ctx, "website")
    with db.tx() as conn:
        fresh = B.get(conn, ctx["biz"]["id"])
    return render(request, "website.html", ctx, s=fresh["site"] or {}, on=fresh["site_enabled"],
                  url=f"{config.PUBLIC_URL()}/s/{fresh['slug']}" if fresh["slug"] else None, page="website", public_url=config.PUBLIC_URL())


@router.post("/app/website")
@_guard
async def website_save(request: Request):
    from ..services import site as ST
    ctx = _own(request)
    f = await _form(request)
    with db.tx() as conn:
        biz = B.get(conn, ctx["biz"]["id"])
        ST.save(conn, biz, ctx["actor"], enabled=f.get("enabled") == "1",
                **{k: f.get(k, "") for k in ST.FIELDS if k != "assistant_photo"})
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
    phone = None
    if pay["provider"] in PM.PUSH and pay["contact_id"]:
        with db.tx() as conn:
            phone = conn.execute(select(db.contacts.c.phone).where(db.contacts.c.id == pay["contact_id"])).scalar()
    return render(request, "pay.html", ctx, pay=pay, bank=bank, done=request.query_params.get("done") == "1",
                  push=pay["provider"] in PM.PUSH, phone=phone, prompted=request.query_params.get("prompted") == "1")


@router.post("/p/{token}/push")
@_guard
async def pay_push(request: Request, token: str):
    from ..services import payments as PM
    f = await _form(request)
    try:
        PM.push(token, f.get("phone", ""))
    except PM.PayError as e:
        raise Invalid(str(e), "provider") from None
    return _go(f"/p/{token}?prompted=1")


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


@router.post("/app/contacts/{contact_id}/received")
@_guard
async def pay_received(request: Request, contact_id: UUID):
    from ..services import payments as PM
    ctx = _ctx(request)
    f = await _form(request)
    biz = ctx["biz"]
    try:
        amount = money.to_minor((f.get("amount") or "").replace(",", "").strip(), biz["currency"])
    except Exception:
        raise Invalid("amount: use digits, e.g. 350.00", "amount") from None
    PM.record_received(biz, ctx["actor"], amount_minor=amount, description=f.get("description", ""), contact_id=contact_id)
    _flash(request, "saved")
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
    _need(request, ctx, "followups")
    tz = ZoneInfo(ctx["biz"]["timezone"])
    with db.tx() as conn:
        rules = FU.automations(conn, ctx["biz"]["id"])
        cps = [c for c in FU.campaigns(conn, ctx["biz"]["id"]) if not c["channel"]]   # v0.18 campaigns live on /app/campaigns
        box = FU.outbox(conn, ctx["biz"]["id"], limit=30)
    from ..services import reviews as RV
    with db.tx() as conn:
        rstats = RV.stats(conn, ctx["biz"]["id"])
    return render(request, "followups.html", ctx, rules=rules, cps=cps, box=box, triggers=FU.TRIGGERS, rstats=rstats,
                  has_review_preset=any("{review_link}" in (r["message"] or "") for r in rules),
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
        from ..services import directory as DR
        listed = DR.settings(conn, biz["id"])["listed"]
        from ..services import push as PSH                       # v0.18: this person's devices that get notifications
        devices = len(PSH.devices(conn, biz["id"], ctx["user"]["id"])) if ctx["user"] else 0
    pack = packs.country(biz["country"])
    from ..services import ai as AIS
    with db.tx() as conn:
        ai_s = AIS.public_settings(conn, biz["id"])
    return render(request, "settings.html", ctx, keys=keys, new_key=new_key, pack=pack, tzs=_TZS, ai=ai_s, devices=devices,
                  team=team, invites=invites, invite_link=invite_link, hooks=hooks, webhook_secret=webhook_secret,
                  pay_accs=pay_accs, pay_fields=PM.PROVIDERS, pay_suggest=[PM.ALIASES.get(x, x) for x in pack.payments],
                  ai_models=AIS.PROVIDERS, ui_langs=[(c, NATIVE.get(c, c)) for c in UI_LANGS],
                  currencies=_currencies(_lang(request)),
                  industries=_industries(request), listed=listed, page="settings")


def _currencies(lang: str) -> list[tuple[str, str]]:
    """Every ISO currency, named in the reader's language (CLDR), sorted by code."""
    from babel.numbers import get_currency_name, list_currencies
    out = []
    for c in sorted(list_currencies()):
        try:
            out.append((c, get_currency_name(c, locale=lang)))
        except Exception:                                  # noqa: BLE001
            out.append((c, c))
    return out


@router.get("/app/export")
@_guard
async def export_download(request: Request):
    """The owner downloads everything (customers, messages, deals, quotes, payments…) as one JSON file."""
    import json as _json
    from fastapi.encoders import jsonable_encoder
    from ..services import export as EX
    ctx = _own(request)
    with db.tx() as conn:
        data = EX.business_data(conn, ctx["biz"]["id"], ctx["actor"])
    name = f"valeria-one-{ctx['biz']['slug'] or str(ctx['biz']['id'])[:8]}-{date.today().isoformat()}.json"
    return Response(_json.dumps(jsonable_encoder(data), ensure_ascii=False, indent=1), media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})


@router.post("/app/settings")
@_guard
async def settings_save(request: Request):
    ctx = _own(request)
    f = await _form(request)
    with db.tx() as conn:
        B.patch(conn, ctx["biz"]["id"], ctx["actor"], name=f.get("name"), language=f.get("language") or None,
                timezone=f.get("timezone"), industry=f.get("industry"), currency=f.get("currency") or None,
                tax_id=f.get("tax_id") if "tax_id" in f else None)
        if "review_url" in f:
            from ..services import reviews as RV
            RV.set_url(conn, ctx["biz"]["id"], ctx["actor"], f.get("review_url", ""))
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
        if f.get("powered_shown") == "1":
            B.set_powered_tag(conn, ctx["biz"]["id"], ctx["actor"], f.get("powered_tag") == "1")
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


# ---------------------------------------------------------------- Review requests
@router.get("/r/{token}")
async def review_click(token: str):
    from ..services import reviews as RV
    try:
        return RedirectResponse(RV.click(token), status_code=302)
    except NotFound:
        return HTMLResponse("Not found", status_code=404)


@router.post("/app/contacts/{contact_id}/review")
@_guard
async def review_ask(request: Request, contact_id: UUID):
    from ..services import reviews as RV
    ctx = _ctx(request)
    f = await _form(request)
    with db.tx() as conn:
        biz = B.get(conn, ctx["biz"]["id"])
    try:
        RV.ask(biz, ctx["actor"], contact_id, UUID(f["account_id"]) if f.get("account_id") else None)
        _flash(request, "review_sent")
    except Blocked as b:
        _flash(request, f"r_{b.reason}", "warn")
    return _go(f"/app/contacts/{contact_id}#review")


@router.post("/app/followups/review-preset")
@_guard
async def review_preset(request: Request):
    from ..services import followups as FU
    ctx = _own(request)
    await _form(request)
    lang = ui_lang(ctx["biz"]["language"])
    with db.tx() as conn:
        FU.create_automation(conn, ctx["biz"], ctx["actor"], trigger="appointment.done", minutes=120,
                             message=t("review_ask", lang).replace("{link}", "{review_link}"), kind="marketing")
    _flash(request, "saved")
    return _go("/app/followups")


# ---------------------------------------------------------------- Grow: win-back + bring a friend
def _friend_came(request: Request, conn, biz: dict, contact: dict, how: str) -> None:
    """A new customer arrived (booking, website, chat, the friend page): if a friend's link brought them, record it."""
    from ..services import growth as GR
    if GR.claim(conn, biz, request.session.get("friend"), contact, how):
        request.session.pop("friend", None)
    _attribute(request, conn, biz, contact)


def _attribute(request: Request, conn, biz: dict, contact: dict) -> None:
    """ars02: who else brought a new customer — an affiliate's link (/a/<code>) or one of the business's ads (/go/<code>)
    opened on this phone. Each is recorded once, for that business only, and only for a customer who is new since."""
    from ..services import ads as ADS
    from ..services import affiliates as AF
    if AF.claim(conn, biz, request.session.get("aff"), contact):
        request.session.pop("aff", None)
    if ADS.claim(conn, biz, request.session.get("ad"), contact):
        request.session.pop("ad", None)


@router.get("/app/grow")
@_guard
async def grow_page(request: Request):
    from ..services import growth as GR
    ctx = _own(request)
    _need(request, ctx, "grow")
    with db.tx() as conn:
        biz = B.get(conn, ctx["biz"]["id"])
        ov = GR.overview(conn, biz)
        refs = GR.referrals(conn, biz["id"])
        pv = GR.preview(conn, biz, ov["winback"]["message"], ov["quiet"]["rows"])
    return render(request, "grow.html", {**ctx, "biz": biz}, ov=ov, refs=refs, preview=pv, page="grow")


@router.post("/app/grow/settings")
@_guard
async def grow_settings(request: Request):
    from ..services import growth as GR
    ctx = _own(request)
    _not_shared_demo(request, ctx)
    f = await _form(request)
    nums = {k: f[k] for k in GR.LIMITS if (f.get(k) or "").strip()}          # a number left empty stays as it was
    words = {k: f[k] for k in GR.TEXTS if k in f}                            # a text left empty is cleared
    with db.tx() as conn:
        GR.save_settings(conn, ctx["biz"], ctx["actor"], **nums, **words)
    _flash(request, "saved")
    return _go("/app/grow#" + ("friends" if "reward_friend" in f else "winback"))


@router.post("/app/grow/winback")
@_guard
async def grow_winback(request: Request):
    from ..services import growth as GR
    ctx = _own(request)
    _not_shared_demo(request, ctx)
    f = await _form(request)
    with db.tx() as conn:
        GR.set_winback(conn, ctx["biz"], ctx["actor"], f.get("on") == "1", message=f.get("message"),
                       template=f.get("template") if "template" in f else None)
    _flash(request, "saved")
    return _go("/app/grow#winback")


@router.post("/app/grow/share")
@_guard
async def grow_share(request: Request):
    from ..services import growth as GR
    ctx = _own(request)
    _not_shared_demo(request, ctx)
    f = await _form(request)
    with db.tx() as conn:
        GR.set_share(conn, ctx["biz"], ctx["actor"], f.get("on") == "1", when=f.get("when") or None, message=f.get("message"))
    _flash(request, "saved")
    return _go("/app/grow#friends")


@router.post("/app/grow/referrals/{rid}/given")
@_guard
async def grow_given(request: Request, rid: UUID):
    from ..services import growth as GR
    ctx = _own(request)
    _not_shared_demo(request, ctx)
    f = await _form(request)
    with db.tx() as conn:
        GR.mark_given(conn, ctx["biz"]["id"], ctx["actor"], rid, f.get("who", ""))
    return _go("/app/grow#referrals")


@router.post("/app/contacts/{contact_id}/friend-link")
@_guard
async def friend_link(request: Request, contact_id: UUID):
    """A customer's own bring-a-friend link: make it (to copy), or send it to them on their channel."""
    from ..services import growth as GR
    ctx = _ctx(request)
    _need(request, ctx, "grow")
    f = await _form(request)
    if f.get("act") == "send":
        try:
            msg = GR.send_link(ctx["biz"], ctx["actor"], contact_id, UUID(f["account_id"]) if f.get("account_id") else None)
            ok = msg.get("status") not in ("failed", "blocked")
            _flash(request, "gw_link_sent" if ok else "msg_failed", "ok" if ok else "warn")
        except Blocked as b:
            _flash(request, f"r_{b.reason}", "warn")
    else:
        with db.tx() as conn:
            GR.link_for(conn, ctx["biz"], contact_id)
    return _go(f"/app/contacts/{contact_id}#friend")


_friend_hits: dict = {}


def _friend_page(request: Request, opened: tuple, sent: bool):
    from ..services import growth as GR
    link, biz, who = opened
    ctx = {"biz": biz, "user": None, "actor": "friend_page", "role": None, "businesses": []}
    with db.tx() as conn:
        s = GR.settings(conn, biz["id"])
    book = f"/b/{biz['slug']}" if biz.get("slug") and biz["booking_enabled"] and blocks.on(biz, "calendar") else None
    site = f"/s/{biz['slug']}" if biz.get("slug") and biz["site_enabled"] and blocks.on(biz, "website") else None
    resp = render(request, "friend.html", ctx, who=who, reward=s["reward_friend"] or s["reward_referrer"], book=book, site=site,
                  code=link["code"], sent=sent)
    resp.headers["X-Robots-Tag"] = "noindex"
    return resp


@router.get("/f/{code}")
@_guard
async def friend_open(request: Request, code: str):
    """A friend opened a customer's link: show the business and remember the link on this phone."""
    from ..services import growth as GR
    opened = GR.open_link(code)
    mine = request.session.get("friend")
    if not (isinstance(mine, dict) and mine.get("c") == opened[0]["code"]):
        request.session["friend"] = GR.remember(opened[0])
    return _friend_page(request, opened, request.query_params.get("sent") == "1")


@router.post("/f/{code}")
@_guard
async def friend_join(request: Request, code: str):
    """The friend leaves their name and number: a new customer, brought by a friend."""
    from ..services import growth as GR
    from ..services import site as ST
    f = await _form(request)
    if f.get("website"):                                     # honeypot
        return _go(f"/f/{code}?sent=1")
    if not _limit(_friend_hits, request, 10):
        raise Invalid("too many messages from this connection; try later", "rate")
    link, biz, who = GR.open_link(code, count=False)
    if not (isinstance(request.session.get("friend"), dict) and request.session["friend"].get("c") == link["code"]):
        request.session["friend"] = GR.remember(link)
    reach = (f.get("reach") or "").strip()
    lang = ui_lang(biz["language"])
    with db.tx() as conn:
        res = ST.contact_form(conn, biz, name=f.get("name", ""), email=reach if "@" in reach else "",
                              phone="" if "@" in reach else reach,
                              message=(f.get("message") or "").strip() or t("gw_friend_inbox", lang).format(name=who or "—"))
        _friend_came(request, conn, biz, res["contact"], "form")
    return _go(f"/f/{code}?sent=1")


# ---------------------------------------------------------------- Start: pick blocks, own words, next steps
def _start_steps(conn, biz: dict) -> list[dict]:
    """What is left to set up, worked out from the business itself (nothing to tick by hand)."""
    from ..services import ai as AIS
    from ..services import booking as BK
    from ..services import payments as PM
    from ..services import team as TM
    accs = [a for a in X.accounts(conn, biz["id"]) if biz["sandbox"] or a["channel"] != "sandbox"]
    steps = [{"key": "blocks", "done": blocks.chosen(biz), "href": "/app/start#blocks", "optional": False},
             {"key": "channel", "done": bool(accs), "href": "/app/connect", "optional": False}]
    if blocks.on(biz, "calendar"):
        steps.append({"key": "calendar", "done": bool(biz["hours"]) and bool(BK.services(conn, biz["id"], active_only=True)),
                      "href": "/app/calendar#booking", "optional": False})
    if blocks.on(biz, "payments"):
        steps.append({"key": "payments", "done": bool(PM.accounts(conn, biz["id"])), "href": "/app/settings#payments",
                      "optional": False})
    if blocks.on(biz, "website"):
        steps.append({"key": "website", "done": bool(biz["site_enabled"]), "href": "/app/website", "optional": False})
    steps.append({"key": "words", "done": bool(biz["words"]), "href": "/app/start#words", "optional": True})
    if blocks.on(biz, "followups"):                          # v0.16: one tap on a ready-made "when this, do that"
        steps.append({"key": "recipes", "done": bool(conn.execute(select(db.recipes.c.id).where(
            db.recipes.c.business_id == biz["id"], db.recipes.c.active).limit(1)).first()), "href": "/app/recipes", "optional": True})
    steps.append({"key": "setups", "done": bool(conn.execute(select(db.snapshot_applies.c.id).where(        # v0.18
        db.snapshot_applies.c.business_id == biz["id"]).limit(1)).first()), "href": "/app/setups", "optional": True})
    if blocks.on(biz, "followups"):
        steps.append({"key": "campaigns", "done": bool(conn.execute(select(db.campaigns.c.id).where(
            db.campaigns.c.business_id == biz["id"], db.campaigns.c.channel.isnot(None), db.campaigns.c.status != "draft")
            .limit(1)).first()), "href": "/app/campaigns", "optional": True})
    if blocks.on(biz, "payments"):
        steps.append({"key": "invoices", "done": bool(conn.execute(select(db.invoices.c.id).where(
            db.invoices.c.business_id == biz["id"], db.invoices.c.status != "draft").limit(1)).first()), "href": "/app/invoices",
            "optional": True})
    if blocks.on(biz, "ai") and not biz["sandbox"]:
        from ..services import voice_agent as VAG
        steps.append({"key": "voice", "done": VAG.settings(conn, biz["id"])["enabled"], "href": "/app/voice-agent", "optional": True})
    steps.append({"key": "fields", "done": bool(conn.execute(select(db.custom_fields.c.id).where(
        db.custom_fields.c.business_id == biz["id"]).limit(1)).first()), "href": "/app/settings/fields", "optional": True})
    steps.append({"key": "forms", "done": bool(conn.execute(select(db.forms.c.id).where(
        db.forms.c.business_id == biz["id"]).limit(1)).first()), "href": "/app/forms", "optional": True})
    from ..services import directory as DR
    from ..services import gbp as GB
    steps.append({"key": "directory", "done": DR.settings(conn, biz["id"])["listed"], "href": "/app/directory", "optional": True})
    if GB.configured() and not biz["sandbox"]:
        gl = GB.link(conn, biz["id"])
        steps.append({"key": "google", "done": bool(gl and gl["location"]), "href": "/app/google", "optional": True})
    if blocks.on(biz, "posts"):
        steps.append({"key": "posts", "done": bool(conn.execute(select(db.posts.c.id).where(
            db.posts.c.business_id == biz["id"], db.posts.c.status == "published").limit(1)).first()), "href": "/app/posts", "optional": True})
    if blocks.on(biz, "grow"):
        from ..services import growth as GR
        steps.append({"key": "grow", "done": bool(GR.ready_made(conn, biz["id"], "winback") or GR.ready_made(conn, biz["id"], "referral")),
                      "href": "/app/grow", "optional": True})
    if blocks.on(biz, "ai"):
        steps.append({"key": "ai", "done": AIS.public_settings(conn, biz["id"])["mode"] != "off", "href": "/app/settings#ai",
                      "optional": True})
    steps.append({"key": "team", "done": len(TM.members(conn, biz["id"])) > 1 or bool(TM.pending(conn, biz["id"])),
                  "href": "/app/settings#team", "optional": True})
    if blocks.on(biz, "assistant"):
        from ..services import owner as OW
        steps.append({"key": "valeria", "done": any(n["channel"] for n in OW.numbers(conn, biz["id"])), "href": "/app/valeria/setup",
                      "optional": True})
    steps.append({"key": "helpers", "done": bool(conn.execute(select(db.helper_leads.c.id).where(
        db.helper_leads.c.business_id == biz["id"]).limit(1)).first()), "href": "/app/helpers", "optional": True})
    return steps


def _not_shared_demo(request: Request, ctx: dict) -> None:
    """A public demo is shared by every visitor: what it shows stays as it is (a sign-up gives you your own)."""
    if not ctx["user"] and ctx["biz"]["demo"]:
        _flash(request, "demo_shared", "warn")
        raise _Redirect("/app/start")


@router.get("/app/start")
@_guard
async def start_page(request: Request):
    ctx = _own(request)
    lang = _lang(request, ctx["biz"])
    with db.tx() as conn:
        biz = B.get(conn, ctx["biz"]["id"])
        steps = _start_steps(conn, biz)
    pack_words = {**biz, "words": {}}                       # the pack's own word, shown as the default
    words = [{"key": k, "default": blocks.word(pack_words, k, lang), "own": (biz["words"] or {}).get(k, ""),
              "stage": k.startswith("stage:")}
             for k in blocks.word_keys(biz["industry"])]
    return render(request, "start.html", {**ctx, "biz": biz}, all_blocks=blocks.BLOCKS, steps=steps, words_list=words,
                  page="start")


async def _multi_form(request: Request):
    f = await request.form()
    if not secrets.compare_digest(str(f.get("csrf", "")), str(request.session.get("csrf", "x"))):
        _flash(request, "expired_form", "warn")
        raise _Redirect(request.headers.get("referer") or "/app")
    return f


@router.post("/app/start/blocks")
@_guard
async def start_blocks(request: Request):
    ctx = _own(request)
    _not_shared_demo(request, ctx)
    f = await _multi_form(request)
    with db.tx() as conn:
        B.set_blocks(conn, ctx["biz"]["id"], ctx["actor"], [str(b) for b in f.getlist("block")])
    _flash(request, "saved")
    return _go("/app/start#words")


@router.post("/app/start/words")
@_guard
async def start_words(request: Request):
    ctx = _own(request)
    _not_shared_demo(request, ctx)
    f = await _form(request)
    with db.tx() as conn:
        B.set_words(conn, ctx["biz"]["id"], ctx["actor"], {k[3:]: v for k, v in f.items() if k.startswith("w__")})
    _flash(request, "saved")
    return _go("/app/start#next")


# ---------------------------------------------------------------- Server (the account that runs this server)
@router.get("/app/server")
@_guard
async def server_page(request: Request):
    ctx = _admin(request)
    return _server(request, ctx)


def _when(v) -> datetime | None:
    try:
        d = datetime.fromisoformat(str(v)) if v else None
    except ValueError:
        return None
    return d.replace(tzinfo=timezone.utc) if d and d.tzinfo is None else d


def _server(request: Request, ctx: dict, invite_link: str | None = None, invited: str | None = None,
            new_partner_key: str | None = None):
    from ..services import partner as PT
    with db.tx() as conn:
        st = I.status(conn)
        rows = I.hosted(conn)
        ctry = I.countries_summary(conn)
        pkeys = PT.keys(conn)
        audit = PT.audit_log(conn, limit=12)
        bill = {"account": BL.account(conn), "pending": BL.pending(conn), "report": BL.report(conn)} if BL.enabled() else None
        exempt = {r[0] for r in conn.execute(select(db.businesses.c.id).where(db.businesses.c.billing_exempt.is_(True)))}
    for k in ("last_backup", "last_update"):                 # the host writes ISO times; show them in local time
        if isinstance(st[k], dict):
            st[k]["at"] = _when(st[k].get("at"))
    st["checked_at"] = _when(st["checked_at"])
    jobs_at = _when((st["jobs"] or {}).get("at"))
    fresh = bool(jobs_at) and datetime.now(timezone.utc) - jobs_at < timedelta(minutes=5)
    return render(request, "server.html", ctx, st=st, rows=rows, countries=_countries(request), jobs_at=jobs_at, ctry=ctry,
                  jobs_fresh=fresh, industries=_industries(request), invite_link=invite_link, invited=invited,
                  pkeys=pkeys, new_partner_key=new_partner_key, audit=audit, bill=bill, exempt=exempt, fmt=BL.fmt,
                  page="server")


@router.post("/app/server-keys")
@_guard
async def server_partner_key(request: Request):
    from ..services import partner as PT
    ctx = _admin(request)
    f = await _form(request)
    with db.tx() as conn:
        key, _ = PT.new_key(conn, f.get("name", ""), ctx["actor"])
    return _server(request, ctx, new_partner_key=key)                      # shown once, in this response only


@router.post("/app/server-keys/{key_id}/revoke")
@_guard
async def server_partner_key_revoke(request: Request, key_id: UUID):
    from ..services import partner as PT
    ctx = _admin(request)
    await _form(request)
    with db.tx() as conn:
        PT.revoke_key(conn, key_id, ctx["actor"])
    return _go("/app/server#partners")


@router.post("/app/server-businesses/{business_id}/{act}")
@_guard
async def server_business_pause(request: Request, business_id: UUID, act: str):
    from ..services import partner as PT
    ctx = _admin(request)
    f = await _form(request)
    if act not in ("suspend", "resume"):
        raise NotFound("page")
    with db.tx() as conn:
        if act == "suspend":
            PT.suspend(conn, ctx["actor"], business_id, f.get("reason", ""))
        else:
            PT.resume(conn, ctx["actor"], business_id)
    _flash(request, "saved")
    return _go("/app/server#businesses")


@router.post("/app/server/{op}")
@_guard
async def server_request(request: Request, op: str):
    ctx = _admin(request)
    await _form(request)
    if op not in I.OPS:
        raise NotFound("page")
    from ..services import partner as PT
    with db.tx() as conn:
        PT.request(conn, op, ctx["actor"])
    _flash(request, "srv_requested")
    return _go("/app/server")


@router.post("/app/server-businesses")
@_guard
async def server_add_business(request: Request):
    ctx = _admin(request)
    f = await _form(request)
    with db.tx() as conn:
        biz, token = I.add_business(conn, ctx["user"], name=f.get("name", ""), country=f.get("country", ""),
                                    industry=f.get("industry", "generic"), owner_email=f.get("email", ""))
        from ..services import partner as PT
        PT.audit(conn, "business.created", ctx["actor"], {"business_id": str(biz["id"]), "name": biz["name"]})
    return _server(request, ctx, invite_link=f"{config.PUBLIC_URL()}/join/{token}", invited=biz["name"])   # shown once


@router.get("/app/server/backups/{name}")
@_guard
async def server_backup_download(request: Request, name: str):
    import logging
    from fastapi.responses import FileResponse
    ctx = _admin(request)
    path = I.backup_path(name)
    logging.getLogger("valeria_one").info("backup %s downloaded by %s", name, ctx["actor"])
    return FileResponse(path, media_type="application/gzip", filename=name,
                        headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"})


# ---------------------------------------------------------------- Billing (owners, on a server that charges)
def _billing_ctx(request: Request) -> dict:
    ctx = _own(request)
    if not BL.enabled():
        raise NotFound("page")
    return ctx


def _admins_emails(conn) -> list[str]:
    return [r[0] for r in conn.execute(select(db.users.c.email).where(db.users.c.is_admin.is_(True)))]


@router.get("/app/billing")
@_guard
async def billing_page(request: Request):
    ctx = _billing_ctx(request)
    with db.tx() as conn:
        biz = B.get(conn, ctx["biz"]["id"])
        acc = BL.account(conn)
        hist = BL.history(conn, biz["id"])
    quotes = {p: {m: BL.quote(biz, p, m) for m in BL.MONTHS} for p in BL.PAID}
    cur = BL.currency_for(biz)
    return render(request, "billing.html", {**ctx, "biz": biz}, st=BL.status(biz), quotes=quotes, plans=BL.PLANS,
                  account=acc if acc and acc["currency"] == cur else None, card=BL.stripe_on(), hist=hist,
                  pending=next((h for h in hist if h["status"] == "pending"), None), fmt=BL.fmt,
                  paid=request.query_params.get("paid") == "1", page="billing")


@router.post("/app/billing/transfer")
@_guard
async def billing_transfer(request: Request):
    from ..services import mailer
    ctx = _billing_ctx(request)
    f = await _form(request)
    try:
        months = int(f.get("months") or 1)
    except ValueError:
        raise Invalid("pay for 1 or 12 months", "months") from None
    with db.tx() as conn:
        biz = B.get(conn, ctx["biz"]["id"])
        pay = BL.request_transfer(conn, biz, ctx["actor"], f.get("plan", ""), months, f.get("note", ""))
        admins = _admins_emails(conn)
    for a in admins:
        mailer.send(a, t("mail_transfer_subject", "en").format(business=biz["name"]),
                    t("mail_transfer_body", "en").format(business=biz["name"], amount=BL.fmt(pay["amount_minor"], pay["currency"]),
                                                         reference=pay["reference"], link=f"{config.PUBLIC_URL()}/app/server#billing"))
    _flash(request, "billing_transfer_sent")
    return _go("/app/billing")


@router.post("/app/billing/card")
@_guard
async def billing_card(request: Request):
    ctx = _billing_ctx(request)
    f = await _form(request)
    with db.tx() as conn:
        biz = B.get(conn, ctx["biz"]["id"])
    try:
        url = BL.checkout(biz, (ctx["user"] or {}).get("email"), f.get("plan", ""), f"{config.PUBLIC_URL()}/app/billing")
    except BL.BillingError as e:
        raise Invalid(str(e), "card") from None
    return RedirectResponse(url, status_code=303)


@router.post("/app/billing/portal")
@_guard
async def billing_portal(request: Request):
    ctx = _billing_ctx(request)
    await _form(request)
    with db.tx() as conn:
        biz = B.get(conn, ctx["biz"]["id"])
    try:
        url = BL.portal(biz, f"{config.PUBLIC_URL()}/app/billing")
    except BL.BillingError as e:
        raise Invalid(str(e), "card") from None
    return RedirectResponse(url, status_code=303)


# ---------------------------------------------------------------- Billing on the Server page (admin)
@router.post("/app/server-billing/account")
@_guard
async def server_billing_account(request: Request):
    ctx = _admin(request)
    f = await _form(request)
    with db.tx() as conn:
        BL.set_account(conn, ctx["user"], f.get("password", ""), f)
    _flash(request, "saved")
    return _go("/app/server#billing")


@router.post("/app/server-billing/{payment_id}/{act}")
@_guard
async def server_billing_decide(request: Request, payment_id: UUID, act: str):
    from ..services import mailer
    ctx = _admin(request)
    await _form(request)
    if act not in ("approve", "reject"):
        raise NotFound("page")
    with db.tx() as conn:
        p = BL.decide(conn, payment_id, ctx["actor"], act == "approve")
        biz = B.get(conn, p["business_id"])
        owners = [r[0] for r in conn.execute(select(db.users.c.email).join(db.memberships, db.memberships.c.user_id == db.users.c.id)
                                             .where(db.memberships.c.business_id == biz["id"], db.memberships.c.role == "owner"))]
    if act == "approve" and p["status"] == "paid":
        lang = ui_lang(biz["language"])
        for o in owners:
            mailer.send(o, t("mail_paid_subject", lang).format(business=biz["name"]),
                        t("mail_paid_body", lang).format(business=biz["name"], plan=p["plan"].title(),
                                                         until=biz["paid_until"].date().isoformat(), link=f"{config.PUBLIC_URL()}/app/billing"))
    _flash(request, "saved")
    return _go("/app/server#billing")


@router.post("/app/server-businesses/{business_id}/billing/{act}")
@_guard
async def server_business_exempt(request: Request, business_id: UUID, act: str):
    ctx = _admin(request)
    await _form(request)
    if act not in ("exempt", "charge"):
        raise NotFound("page")
    with db.tx() as conn:
        BL.set_exempt(conn, business_id, ctx["actor"], act == "exempt")
    _flash(request, "saved")
    return _go("/app/server#businesses")


@router.get("/favicon.ico")
async def favicon():
    return Response((HERE / "static" / "icon.svg").read_bytes(), media_type="image/svg+xml")
