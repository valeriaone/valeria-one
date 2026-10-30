"""Sign up and sign in with the accounts a business already has: "Continue with Facebook" / "Continue with Google".

No form, no password to invent. Facebook also hands over the business's Pages (and the Instagram accounts linked
to them), so the new business arrives already named, with its phone, email, address, about and opening hours
filled from its Page, and its Messenger + Instagram already in the inbox.

Same apps as the rest of the server, nothing new to pay for:
* Facebook: the server's Meta app (VO_META_APP_ID / VO_META_APP_SECRET, see docs/META_SETUP.md), with
  https://YOUR-SERVER/auth/facebook/callback as a valid OAuth redirect URI. A Business app signs people in with
  a Login for Business configuration: VO_META_SIGNUP_CONFIG_ID, else the Pages one (VO_META_LOGIN_CONFIG_ID);
  without either, the classic scope list. A configuration without `email` still works: the person types it.
* Google: the server's Google OAuth client (VO_GOOGLE_CLIENT_ID / VO_GOOGLE_CLIENT_SECRET, as for Calendar), with
  https://YOUR-SERVER/auth/google/callback as an authorised redirect URI. Sign-in only: name and email.
Without them the buttons don't show and email + password works as before.
"""
from __future__ import annotations

import base64
import json
import secrets
from datetime import timedelta
from urllib.parse import urlencode

import httpx
from sqlalchemy import delete, func, insert, select
from sqlalchemy.engine import Connection

from .. import config, crypto, db, packs
from ..channels.meta import graph
from . import Invalid, log
from . import users as U

UL = db.user_logins
PROVIDERS = ("facebook", "google")
FB_SCOPES = "email,public_profile," + "pages_show_list,pages_messaging,pages_manage_metadata,pages_read_engagement," \
            "instagram_basic,instagram_manage_messages,instagram_manage_comments,leads_retrieval,business_management," \
            "pages_manage_posts,instagram_content_publish"
PAGE_INFO = ("id,name,access_token,category,phone,emails,website,about,single_line_address,location{country},hours,"
             "instagram_business_account{id,username}")
G_AUTH, G_TOKEN = "https://accounts.google.com/o/oauth2/v2/auth", "https://oauth2.googleapis.com/token"
MAX_PAGES = 25
PENDING_MINUTES = 15


class SocialError(Exception):
    pass


def configured(provider: str) -> bool:
    if config.SANDBOX():
        return False
    if provider == "facebook":
        return bool(config.get("VO_META_APP_ID") and config.get("VO_META_APP_SECRET"))
    if provider == "google":
        return bool(config.get("VO_GOOGLE_CLIENT_ID") and config.get("VO_GOOGLE_CLIENT_SECRET"))
    return False


def available() -> list[str]:
    return [p for p in PROVIDERS if configured(p)]


def redirect_uri(provider: str) -> str:
    return f"{config.PUBLIC_URL()}/auth/{provider}/callback"


def auth_url(provider: str, state: str) -> str:
    if provider == "facebook":
        q = {"client_id": config.get("VO_META_APP_ID"), "redirect_uri": redirect_uri(provider), "state": state,
             "response_type": "code"}
        cfg = config.get("VO_META_SIGNUP_CONFIG_ID") or config.get("VO_META_LOGIN_CONFIG_ID")
        if cfg:                                    # a Business app: Login for Business, by configuration
            q["config_id"] = cfg
        else:                                      # a consumer app: classic Facebook Login, by scope
            q["scope"] = FB_SCOPES
        return f"https://www.facebook.com/{config.get('VO_GRAPH_VERSION', 'v23.0')}/dialog/oauth?" + urlencode(q)
    if provider == "google":
        return G_AUTH + "?" + urlencode({"client_id": config.get("VO_GOOGLE_CLIENT_ID"), "redirect_uri": redirect_uri(provider),
                                         "response_type": "code", "scope": "openid email profile", "state": state,
                                         "prompt": "select_account"})
    raise SocialError("unknown provider")


def _http(http: httpx.Client | None = None) -> httpx.Client:
    return http or httpx.Client(timeout=20)


def _json(r: httpx.Response) -> dict:
    try:
        j = r.json()
    except ValueError:
        raise SocialError(f"answered {r.status_code}") from None
    if r.status_code >= 300 or "error" in j:
        e = j.get("error")
        raise SocialError((e.get("message") if isinstance(e, dict) else e) or f"answered {r.status_code}")
    return j


# ---------------------------------------------------------------- who is this person (and, on Facebook, their Pages)
def identify(provider: str, code: str, http: httpx.Client | None = None) -> dict:
    """The one-time code from the provider -> {provider, subject, email, name, pages}."""
    if not configured(provider) or not code:
        raise SocialError("not available")
    h = _http(http)
    if provider == "facebook":
        app_id, secret = config.get("VO_META_APP_ID"), config.get("VO_META_APP_SECRET")
        short = _json(h.get(f"{graph()}/oauth/access_token", params={
            "client_id": app_id, "client_secret": secret, "redirect_uri": redirect_uri(provider), "code": code})).get("access_token")
        if not short:
            raise SocialError("Facebook did not return a token")
        long_ = _json(h.get(f"{graph()}/oauth/access_token", params={
            "grant_type": "fb_exchange_token", "client_id": app_id, "client_secret": secret,
            "fb_exchange_token": short})).get("access_token") or short
        me = _json(h.get(f"{graph()}/me", params={"access_token": long_, "fields": "id,name,email"}))
        if not me.get("id"):
            raise SocialError("Facebook did not say who you are")
        try:                                               # Pages are a bonus: a person without any still signs up
            data = _json(h.get(f"{graph()}/me/accounts", params={"access_token": long_, "fields": PAGE_INFO,
                                                                  "limit": MAX_PAGES})).get("data", [])
        except SocialError:
            data = []
        pages = [_page(p) for p in data if p.get("access_token")][:MAX_PAGES]
        if pages:                                          # what they allowed (posting needs two of them, v0.12)
            from .meta import granted
            scopes = granted(long_, h)
            for pg in pages:
                pg["scopes"] = scopes
        return {"provider": "facebook", "subject": str(me["id"]), "email": (me.get("email") or "").strip().lower() or None,
                "name": (me.get("name") or "").strip()[:200], "pages": pages}
    tok = _json(h.post(G_TOKEN, data={"code": code, "client_id": config.get("VO_GOOGLE_CLIENT_ID"),
                                      "client_secret": config.get("VO_GOOGLE_CLIENT_SECRET"),
                                      "redirect_uri": redirect_uri(provider), "grant_type": "authorization_code"}))
    claims = _claims(tok.get("id_token"))           # straight from Google's token endpoint over TLS
    if not claims.get("sub") or claims.get("aud") != config.get("VO_GOOGLE_CLIENT_ID"):
        raise SocialError("Google did not say who you are")
    verified = claims.get("email_verified") in (True, "true")
    return {"provider": "google", "subject": str(claims["sub"]),
            "email": (claims.get("email") or "").strip().lower() if verified else None,
            "name": (claims.get("name") or "").strip()[:200], "pages": []}


def _claims(id_token: str | None) -> dict:
    try:
        part = (id_token or "").split(".")[1]
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except Exception:
        return {}


def _page(p: dict) -> dict:
    """A Page as we keep it while the person finishes: what connects it, and what fills the business in."""
    loc = p.get("location") or {}
    return {"id": str(p["id"]), "name": (p.get("name") or "")[:200], "token": p["access_token"],
            "ig": p.get("instagram_business_account"), "category": (p.get("category") or "")[:100],
            "phone": (p.get("phone") or "")[:40], "email": ((p.get("emails") or [None])[0] or "")[:320],
            "about": (p.get("about") or "")[:2000], "address": (p.get("single_line_address") or "")[:300],
            "country": country_code(loc.get("country") or ""), "hours": p.get("hours") or {}}


def country_code(name: str) -> str:
    """'Philippines' -> 'PH' (Facebook writes the country's English name); '' when unknown or closed."""
    from babel import Locale
    n = (name or "").strip().lower()
    if not n:
        return ""
    if len(n) == 2 and n.upper() in packs.open_codes():
        return n.upper()
    for code, label in Locale.parse("en").territories.items():
        if len(code) == 2 and label.lower() == n and code in packs.open_codes():
            return code
    return ""


_KINDS = (("salon", ("salon", "spa", "beauty", "nail", "barber", "hair", "lash", "brow", "makeup", "massage", "cosmetic", "tattoo")),
          ("fitness", ("gym", "fitness", "yoga", "pilates", "crossfit", "martial", "boxing", "dance", "sport", "coach")),
          ("clinic", ("clinic", "doctor", "dentist", "dental", "medical", "health", "hospital", "physio", "therap", "pharmac",
                      "veterinar", "optic", "psycholog", "nutrition")),
          ("restaurant", ("restaurant", "food", "cafe", "café", "coffee", "bakery", "bar", "pizza", "catering", "grill",
                          "kitchen", "eatery", "dessert", "tea")),
          ("realtor", ("real estate", "realtor", "property", "realty", "broker")),
          ("contractor", ("contractor", "plumb", "electric", "construction", "repair", "cleaning", "roof", "landscap",
                          "pest", "hvac", "carpent", "painter", "handyman", "mover", "auto")),
          ("professional", ("lawyer", "attorney", "legal", "accountant", "accounting", "consult", "agency", "insurance",
                            "financ", "tutor", "school", "education", "photograph", "design", "marketing")),
          ("florist", ("florist", "flower")),
          ("shop", ("shop", "store", "retail", "boutique", "clothing", "jewel", "market", "gift")))


def industry_guess(category: str) -> str:
    """A Page's category ('Beauty Salon', 'Dentist & Dental Office') -> the industry pack to preselect."""
    c = (category or "").lower()
    for code, words in _KINDS:
        if any(w in c for w in words) and code in packs.industries():
            return code
    return "generic"


# ---------------------------------------------------------------- the few minutes between the provider and "Start"
def hold(conn: Connection, ident: dict) -> str:
    """Keep what the provider told us (Page tokens included) server-side and encrypted; the browser only gets a key."""
    conn.execute(delete(db.instance_kv).where(db.instance_kv.c.key.like("social:%"),
                                              db.instance_kv.c.updated_at < func.now() - timedelta(hours=1)))
    key = "social:" + secrets.token_urlsafe(24)
    conn.execute(insert(db.instance_kv).values(key=key, value={"blob": crypto._f().encrypt(json.dumps(ident).encode()).decode()}))
    return key


def held(conn: Connection, key: str | None, *, take: bool = False) -> dict | None:
    from cryptography.fernet import InvalidToken
    if not key or not key.startswith("social:"):
        return None
    v = conn.execute(select(db.instance_kv.c.value).where(db.instance_kv.c.key == key)).scalar()
    if take:
        conn.execute(delete(db.instance_kv).where(db.instance_kv.c.key == key))
    try:
        return json.loads(crypto._f().decrypt(v["blob"].encode(), ttl=PENDING_MINUTES * 60)) if v else None
    except (InvalidToken, KeyError, TypeError, ValueError):
        return None


# ---------------------------------------------------------------- sign in, or make the account
def find_user(conn: Connection, ident: dict) -> dict | None:
    """The person's account here: by this provider's id, else by the same (provider-verified) email, which links it."""
    uid = conn.execute(select(UL.c.user_id).where(UL.c.provider == ident["provider"], UL.c.subject == ident["subject"])).scalar()
    if uid:
        return U.get(conn, uid)
    if ident.get("email"):
        u = conn.execute(select(db.users).where(func.lower(db.users.c.email) == ident["email"])).mappings().first()
        if u:
            link(conn, u["id"], ident)
            return dict(u)
    return None


def link(conn: Connection, user_id, ident: dict) -> None:
    conn.execute(insert(UL).values(user_id=user_id, provider=ident["provider"], subject=ident["subject"], email=ident.get("email")))
    for (bid,) in conn.execute(select(db.memberships.c.business_id).where(db.memberships.c.user_id == user_id)):
        log(conn, bid, "user.login_linked", f"user:{user_id}", user_id, {"provider": ident["provider"]})


def create(conn: Connection, ident: dict, *, business_name: str, country: str, industry: str, email: str | None = None,
           pages: list[str] | None = None, sandbox: bool = False, http=None) -> tuple[dict, dict, list[str]]:
    """New owner + business from a provider account -> (user, business, problems). The password is a long random one
    nobody knows ("forgot my password" sets a real one). Chosen Pages fill the business in and join the inbox; a Page
    that won't connect is reported, it never stops the sign-up."""
    e = ident.get("email") or (email or "").strip().lower()
    if not e:
        raise Invalid("enter your email (your account didn't share one)", "email")
    user, biz = U.register(conn, email=e, name=ident.get("name") or "", password=secrets.token_urlsafe(32),
                           business_name=business_name, country=country, industry=industry, sandbox=sandbox)
    link(conn, user["id"], ident)
    chosen = [p for p in ident.get("pages") or [] if p["id"] in (pages or [])]
    problems = []
    if chosen:
        biz = prefill(conn, biz, f"user:{user['id']}", chosen[0])
        from . import meta as MT
        try:
            with conn.begin_nested():
                MT.connect_pages(conn, biz, f"user:{user['id']}", chosen, [p["id"] for p in chosen], http=http)
        except (MT.MetaError, Invalid, httpx.HTTPError) as ex:
            problems.append(str(ex)[:200])
    log(conn, biz["id"], "user.signed_up_with", f"user:{user['id']}", user["id"],
        {"provider": ident["provider"], "pages": len(chosen)})
    return user, biz, problems


def prefill(conn: Connection, biz: dict, actor: str, page: dict) -> dict:
    """Fill the website and opening hours from the Page. Each field on its own: one odd value is skipped, not fatal."""
    from . import booking as BK
    from . import site as S
    fields = {"phone": page.get("phone"), "email": page.get("email"), "about": page.get("about"),
              "address": page.get("address"), "facebook": f"https://facebook.com/{page['id']}",
              "instagram": (page.get("ig") or {}).get("username")}
    for k, v in fields.items():
        if not v:
            continue
        try:
            with conn.begin_nested():
                biz = S.save(conn, biz, actor, **{k: v})
        except Invalid:
            pass
    hours = fb_hours(page.get("hours") or {})
    if hours:
        try:
            with conn.begin_nested():
                biz = BK.save_settings(conn, biz, actor, hours=hours)
        except Invalid:
            pass
    return biz


def fb_hours(h: dict) -> dict:
    """Facebook's {"mon_1_open": "09:00", "mon_1_close": "18:00", …} -> {"mon": [["09:00", "18:00"]], …}."""
    out: dict[str, list] = {}
    for key, v in h.items():
        bits = key.split("_")
        if len(bits) != 3 or bits[2] != "open" or not isinstance(v, str):
            continue
        close = h.get(f"{bits[0]}_{bits[1]}_close")
        if isinstance(close, str):
            out.setdefault(bits[0], []).append([v, "23:59" if close in ("00:00", "24:00") else close])
    return {d: sorted(s) for d, s in out.items()}


def logins(conn: Connection, user_id) -> list[str]:
    return [r[0] for r in conn.execute(select(UL.c.provider).where(UL.c.user_id == user_id).order_by(UL.c.provider))]
