"""«Valeria sets it up» (setup28, v0.28): one talk that asks which accounts a business already has — and sets up each one.

Right after sign-up (and from Connect and Today) Valeria asks one question at a time: WhatsApp (personal, the Business app
or the official Business API), the email address and who hosts it, the websites, the Facebook Page, Instagram, TikTok,
YouTube, the Google profile, the calendar, the payments it already takes, other chat apps of its country, and the apps its
trade usually has (the industry pack's `setup.tools`, filtered by country — a realtor's listing portals, a salon's
booking app…). Every question allows «I don't have it» and «Later»; answers are kept per business, so it picks up where
it stopped, and any answer can be changed.

For each answer she does the setup herself, as far as it can go, with what already exists here (nothing is duplicated):
  * WhatsApp number -> the business's contact number (phones.e164: Mexico's old 52-1 handled) on its website and in every
    click-to-chat link — a customer can write in one tap at once, and the owner answers from THEIR OWN number — plus a
    tracked source (leads, v0.27) whose short link opens that chat with its "(ref <code>)". "Valeria answers for me" ->
    the official account: Meta's one-tap sign-up where the server has it (meta.py), else the business's own Cloud API
    settings in Channels — said plainly, never faked.
  * Email -> the provider read from the domain (or asked, for the business's own domain), the email channel's settings
    filled in (the same presets as Channels), the provider's own app-password page linked, then a sign-in test BEFORE
    anything is saved (channels/email_smtp.py check: nothing is sent); the app password goes into the existing encrypted
    channel account. Microsoft no longer lets other apps sign in with a password: said so, the address kept.
  * Each website -> a tracked source + the lead form snippet for any website + its tracked link.
  * Facebook, Instagram, TikTok, YouTube, Google -> the profile kept (website links, Messenger click-to-chat), a tracked
    link per network to put in the bio; the official connections (Meta, TikTok, Google) where the server has them.
  * Calendar, payments, other chats, the trade's apps -> their existing connect screens; where none exists, "has an
    account" + its link is kept and the summary says what happens once it can connect.
The end: «Ready: N of M», each pending item with one clear step; Today nudges until nothing is left.

NEVER a password. Only the email's app password is taken (shape-checked for providers whose app passwords have one), and
only into the encrypted channel flow; every other field refuses anything that looks like a password (or is named one).
$0 to us: the business's own accounts and own number, nothing sent from ours.

answer(dry_run=True) (and VO_SETUP_DRY_RUN=1) says what Valeria would do and writes, connects and tests nothing at all.
"""
from __future__ import annotations

import re
import secrets
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit
from uuid import UUID

from sqlalchemy import cast, func, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection

from .. import channels, config, db, packs, phones
from . import Invalid, log

OB = db.onboarding
ORDER = ("whatsapp", "email", "prices", "website", "facebook", "instagram", "tiktok", "youtube", "google", "calendar", "payments", "chats",
         "tools")
HAS = ("yes", "no", "later")
WA_KINDS = ("personal", "business_app", "business_api")
CAL_KINDS = ("google", "outlook", "apple", "other")
CHATS = ("sms", "telegram", "viber", "line")                  # other chat apps a country's pack may list (WhatsApp, Meta: own questions)
CHAT_NAMES = {"sms": "SMS", "telegram": "Telegram", "viber": "Viber", "line": "LINE"}
MAX_SITES = 10
DONE = ("ready", "none")                                     # nothing left to do for this one

# who hosts the mailbox: the sending server, where the provider makes an app password, and its shape
MAIL = {
    "gmail": {"name": "Gmail", "host": "smtp.gmail.com", "app": "https://myaccount.google.com/apppasswords",
              "twostep": "https://myaccount.google.com/signinoptions/two-step-verification", "shape": "16"},
    "workspace": {"name": "Google Workspace", "host": "smtp.gmail.com", "app": "https://myaccount.google.com/apppasswords",
                  "twostep": "https://myaccount.google.com/signinoptions/two-step-verification", "shape": "16"},
    "yahoo": {"name": "Yahoo", "host": "smtp.mail.yahoo.com", "app": "https://login.yahoo.com/myaccount/security/app-password",
              "twostep": "https://login.yahoo.com/myaccount/security/", "shape": "16"},
    "icloud": {"name": "iCloud", "host": "smtp.mail.me.com", "app": "https://account.apple.com/account/manage/section/security",
               "twostep": "https://account.apple.com/account/manage/section/security", "shape": "apple"},
    "zoho": {"name": "Zoho", "host": "smtp.zoho.com", "app": "https://accounts.zoho.com/home#security/app_password",
             "twostep": "https://accounts.zoho.com/home#multiTFA/modes", "shape": "zoho"},
    "zoho_org": {"name": "Zoho Mail", "host": "smtppro.zoho.com", "app": "https://accounts.zoho.com/home#security/app_password",
                 "twostep": "https://accounts.zoho.com/home#multiTFA/modes", "shape": "zoho"},
    "microsoft": {"name": "Outlook / Hotmail", "host": None},                    # no app sign-in with a password any more
    "m365": {"name": "Microsoft 365", "host": None},
    "other": {"name": "", "host": None},                                         # their host: the Channels form, by hand
}
_DOMAINS = {"gmail": ("gmail.com", "googlemail.com"),
            "yahoo": ("yahoo.com", "ymail.com", "rocketmail.com", "yahoo.com.mx", "yahoo.es", "yahoo.co.uk", "yahoo.fr", "yahoo.de",
                      "yahoo.com.br", "yahoo.co.in", "yahoo.co.jp", "yahoo.com.ar", "yahoo.com.ph", "yahoo.co.id", "yahoo.com.au"),
            "icloud": ("icloud.com", "me.com", "mac.com"),
            "zoho": ("zoho.com", "zohomail.com", "zoho.eu", "zohomail.eu", "zoho.in", "zohomail.in"),
            "microsoft": ("outlook.com", "hotmail.com", "live.com", "msn.com", "outlook.es", "hotmail.es", "outlook.com.mx",
                          "live.com.mx", "hotmail.co.uk", "live.co.uk", "hotmail.fr", "live.fr", "hotmail.de", "outlook.de",
                          "hotmail.it", "outlook.com.br", "hotmail.com.br", "outlook.jp", "outlook.in", "outlook.ph")}
CUSTOM_HOSTS = ("workspace", "zoho_org", "m365", "other")    # the business's own domain: who hosts it?
# Zoho's data centres (its sign-in and sending servers follow the account's region)
_ZOHO_DC = {"IN": "in", "AU": "com.au", "JP": "jp",
            **{c: "eu" for c in "AT BE BG HR CY CZ DK EE FI FR DE GR HU IE IT LV LT LU MT NL PL PT RO SK SI ES SE IS LI NO CH".split()}}

_PW_NAME = re.compile(r"pass|pwd|contrase|senha|kennwort|clave|parola|sandi|katasandi", re.I)
_PW_SYM = re.compile(r"[!#$%^&*()+=\[\]{}|\\;:'\",<>?~`]")
_EMAIL = re.compile(r"^[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+\.)+[A-Za-z]{2,}$")
_HOST = re.compile(r"^([A-Za-z0-9-]+\.)+[A-Za-z]{2,}$")
_IG = re.compile(r"^[A-Za-z0-9._]{1,30}$")
_TT = re.compile(r"^[A-Za-z0-9._]{2,24}$")
_FB_USER = re.compile(r"^[A-Za-z0-9.]{2,100}$")
_YT = re.compile(r"^(@[A-Za-z0-9._-]{3,30}|channel/UC[A-Za-z0-9_-]{20,24}|c/[A-Za-z0-9._-]{1,100}|user/[A-Za-z0-9._-]{1,100})$")


class EmailFailed(Invalid):
    """The sign-in test didn't pass: nothing was saved."""


# Every refusal here carries a word key (ob_…) as its message: the screen says it in the reader's language (web/onboard.py).


def dry() -> bool:
    return config.get("VO_SETUP_DRY_RUN", "0") == "1"


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------- never a password
def looks_like_password(v) -> bool:
    """A value someone typed that looks like a password (letters + digits + a symbol a link, handle, number or email
    wouldn't have, no spaces), so it is refused and never kept."""
    s = str(v or "").strip()
    if len(s) < 6 or len(s) > 128 or any(c.isspace() for c in s) or "/" in s:
        return False
    if _EMAIL.match(s) or re.fullmatch(r"[+\d().\-]+", s):
        return False
    return bool(re.search(r"[A-Za-z]", s) and re.search(r"\d", s) and _PW_SYM.search(s))


def refuse_passwords(form: dict, allow: tuple[str, ...] = ()) -> None:
    """Raise when a form carries a password: a field named like one (except the app password Valeria asked for) or a value
    that looks like one."""
    for k, v in form.items():
        if k in ("csrf",) + allow or not isinstance(v, str):
            continue
        if _PW_NAME.search(k) or looks_like_password(v):
            raise Invalid("ob_no_passwords", k)


def _t(key: str, lang: str | None) -> str:
    from ..web.i18n import t, ui_lang
    return t(key, ui_lang(lang))


# ---------------------------------------------------------------- the questions (country + trade)
def tools(biz: dict) -> list[packs.SetupTool]:
    return packs.industry(biz["industry"]).setup_tools(biz["country"])


def chat_options(biz: dict) -> list[str]:
    """Other chat apps for this country (its pack's list) — Telegram everywhere, since anyone can make a bot."""
    pack = packs.country(biz["country"])
    out = [c for c in pack.channels if c in CHATS]
    return out + [c for c in ("telegram",) if c not in out]


def payment_options(biz: dict) -> list[str]:
    from . import payments as PM
    pack = packs.country(biz["country"])
    out = list(dict.fromkeys(pack.payments))
    out += [p for p in ("stripe", "bank_transfer") if p not in out and p in PM.PROVIDERS]
    return out + ["cash"]


def questions(biz: dict) -> list[str]:
    """The talk for this business, in order. WhatsApp first where the country lives on it (its pack lists it), else after
    the socials; the trade's apps only when its pack has some for this country."""
    pack = packs.country(biz["country"])
    order = list(ORDER)
    if "whatsapp" not in pack.channels[:3]:
        order.remove("whatsapp")
        order.insert(order.index("tiktok"), "whatsapp")
    if not tools(biz):
        order.remove("tools")
    return order


# ---------------------------------------------------------------- answers
def answers(conn: Connection, business_id: UUID) -> dict:
    r = conn.execute(select(OB.c.answers).where(OB.c.business_id == business_id)).scalar()
    return dict(r or {})


def _save_answer(conn: Connection, biz: dict, actor: str, key: str, value: dict) -> None:
    new = {key: value}
    conn.execute(pg_insert(OB).values(business_id=biz["id"], answers=new)
                 .on_conflict_do_update(index_elements=[OB.c.business_id],
                                        set_={"answers": OB.c.answers.op("||")(cast(new, JSONB)), "updated_at": func.now()}))
    got = answers(conn, biz["id"])
    if all(q in got for q in questions(biz)):
        conn.execute(update(OB).where(OB.c.business_id == biz["id"], OB.c.finished_at.is_(None)).values(finished_at=func.now()))
    log(conn, biz["id"], "setup.answered", actor, biz["id"], {"question": key, "has": value.get("has")})


def reset(conn: Connection, biz: dict, actor: str) -> None:
    """Start the talk again: the answers go; what she set up (sources, connections, profile links) stays."""
    conn.execute(update(OB).where(OB.c.business_id == biz["id"]).values(answers={}, finished_at=None, updated_at=func.now()))
    log(conn, biz["id"], "setup.restarted", actor, biz["id"])


# ---------------------------------------------------------------- reading what people type
def _clean(v, n: int = 300) -> str:
    return " ".join(str(v or "").split())[:n]


def _url(v, field: str) -> str:
    """"www.x.com", "x.com/page", "https://x.com" -> https://…; http:// and anything odd are refused."""
    from .funnels import https
    s = _clean(v, 500).strip().rstrip(".,;")
    if not s:
        return ""
    if s.lower().startswith("http://"):
        raise Invalid("ob_bad_http", field)
    if "://" not in s:
        s = "https://" + s
    host = urlsplit(s).hostname or ""
    if not _HOST.match(host) or "@" in urlsplit(s).netloc:
        raise Invalid("ob_bad_url", field)
    try:
        return https(s, field)
    except Invalid:
        raise Invalid("ob_bad_url", field) from None


def websites(raw) -> list[str]:
    parts = [p for p in re.split(r"[\s,;]+", str(raw or "")) if p.strip()]
    out = list(dict.fromkeys(_url(p, "sites") for p in parts))
    if len(out) > MAX_SITES:
        raise Invalid("ob_too_many_sites", "sites")
    return out


def facebook(raw) -> tuple[str, str | None]:
    """A Page by its address, its @username or its name in the address -> (its https address, its username for m.me or None)."""
    s = _clean(raw, 300).strip()
    if not s:
        return "", None
    if re.match(r"^(https?://)?([a-z0-9-]+\.)?(facebook\.com|fb\.com|fb\.me)/", s, re.I):
        s = re.sub(r"^(https?://)?([a-z0-9-]+\.)?(facebook\.com|fb\.com|fb\.me)/", "", s, flags=re.I)
        if s.lower().startswith("profile.php"):
            m = re.search(r"[?&]id=(\d{5,20})", s)
            if not m:
                raise Invalid("ob_bad_profile", "value")
            return f"https://www.facebook.com/profile.php?id={m.group(1)}", None
        s = s.split("?")[0].strip("/")
        if s.lower().startswith("pages/"):
            parts = s.split("/")
            if len(parts) >= 3 and parts[-1].isdigit():
                return f"https://www.facebook.com/{parts[-1]}", parts[-1]
            raise Invalid("ob_bad_profile", "value")
        s = s.split("/")[0]
    s = s.lstrip("@")
    if not _FB_USER.match(s):
        raise Invalid("ob_bad_profile", "value")
    return f"https://www.facebook.com/{s}", s


def handle(raw, net: str) -> str:
    """@name, name or the profile's address -> the username (Instagram, TikTok)."""
    s = _clean(raw, 300).strip()
    if not s:
        return ""
    s = re.sub(r"^(https?://)?(www\.|m\.|vm\.)?(instagram\.com|instagr\.am|tiktok\.com)/", "", s, flags=re.I)
    s = s.split("?")[0].strip("/").split("/")[0].lstrip("@")
    if not (_IG if net == "instagram" else _TT).match(s):
        raise Invalid("ob_bad_profile", "value")
    return s


def youtube(raw) -> str:
    s = _clean(raw, 300).strip()
    if not s:
        return ""
    s = re.sub(r"^(https?://)?(www\.|m\.)?youtube\.com/", "", s, flags=re.I).split("?")[0].strip("/")
    if not s.startswith(("@", "channel/", "c/", "user/")):
        s = "@" + s.lstrip("@")
    if not _YT.match(s):
        raise Invalid("ob_bad_profile", "value")
    return f"https://www.youtube.com/{s}"


def mail_provider(email: str) -> str | None:
    """The provider by the address's domain; None = the business's own domain (who hosts it is asked)."""
    dom = email.rsplit("@", 1)[-1].lower()
    return next((p for p, ds in _DOMAINS.items() if dom in ds), None)


def mail_host(provider: str, country: str, email: str = "") -> str | None:
    """The sending server for this provider (Zoho follows its region: the address's own .eu / .in, else the country's)."""
    h = MAIL.get(provider, {}).get("host")
    if h and provider in ("zoho", "zoho_org"):
        dom = email.rsplit("@", 1)[-1].lower()
        dc = next((x for x in ("eu", "in") if dom.endswith("." + x) and "zoho" in dom), None) or _ZOHO_DC.get(country)
        if dc:
            h = h.replace(".com", "." + dc)
    return h


def app_password(raw, provider: str) -> str:
    """The provider's app password, never the normal one: Google and Yahoo make 16 letters, Apple 4 groups of 4 with dashes;
    anything else for them is refused (it is probably the real password)."""
    s = str(raw or "").strip()
    shape = MAIL.get(provider, {}).get("shape")
    letters = re.sub(r"[\s-]", "", s).lower()
    if shape == "16":
        if not re.fullmatch(r"[a-z]{16}", letters):
            raise Invalid("ob_not_app_password", "app_password")
        return letters
    if shape == "apple":
        if not re.fullmatch(r"[a-z]{16}", letters):
            raise Invalid("ob_not_app_password", "app_password")
        return "-".join(letters[i:i + 4] for i in range(0, 16, 4))
    if shape == "zoho":
        if not re.fullmatch(r"[A-Za-z0-9]{12,24}", s):
            raise Invalid("ob_not_app_password", "app_password")
        return s
    raise Invalid("ob_not_app_password", "app_password")


# ---------------------------------------------------------------- what Valeria does with an answer (planned, then done)
def plan(conn: Connection, biz: dict, key: str, form: dict) -> tuple[dict, list[dict]]:
    """(the answer to keep, the steps to set it up). Reads only — nothing is written here."""
    if key not in questions(biz):
        raise Invalid("?", "q")
    act = str(form.get("act") or "answer")
    refuse_passwords(form, allow=("app_password",) if key == "email" else ())
    prev = answers(conn, biz["id"]).get(key) or {}
    if act in ("no", "later"):
        return {"has": act, "at": _now().isoformat()}, []
    if act == "added" and key == "calendar":
        return {**prev, "has": "yes", "added": True, "at": _now().isoformat()}, []
    ans: dict = {"has": "yes", "at": _now().isoformat()}
    steps: list[dict] = []
    pack = packs.country(biz["country"])
    if key == "whatsapp":
        kind = str(form.get("kind") or "")
        if kind not in WA_KINDS:
            raise Invalid("ob_pick_one", "kind")
        raw = _clean(form.get("number"), 40)
        if not raw:
            raise Invalid("ob_need_number", "number")
        try:
            num = phones.e164(raw, pack)
        except phones.BadPhone:
            raise Invalid("ob_bad_number", "number") from None
        ans.update(kind=kind, value=num, assist=bool(form.get("assist")) or kind == "business_api")
        steps.append({"op": "site", "fields": {"whatsapp": num}})
        from . import leads as LD
        if LD.settings(conn, biz["id"]).get("chat_number") not in (None, num):     # the click-to-chat number follows the answer
            steps.append({"op": "lead_settings", "fields": {"chat_number": num}})
        steps += [
                  {"op": "source", "slot": "chat", "kind": "other", "name": "WhatsApp", "dest": "", "chat": True}]
    elif key == "email":
        addr = _clean(form.get("email"), 320).lower()
        if not _EMAIL.match(addr):
            raise Invalid("ob_bad_email", "email")
        prov = mail_provider(addr) or str(form.get("host") or "")
        if prov not in MAIL or (mail_provider(addr) is None and prov not in CUSTOM_HOSTS):
            ans.update(value=addr, provider=None)                 # their own domain: who hosts it is the next tap
            return ans, []
        ans.update(value=addr, provider=prov)
        host = mail_host(prov, biz["country"], addr)
        pw = str(form.get("app_password") or "")
        from . import inbox as X
        if any(x["channel"] == "email" and x.get("address") == addr for x in X.accounts(conn, biz["id"])):
            return ans, []                                       # already connected: nothing to change (Channels changes it)
        if host and pw.strip():
            if MAIL[prov]["shape"] == "zoho" and not form.get("made_app"):
                raise Invalid("ob_zoho_confirm", "made_app")
            cfg = {"smtp_host": host, "smtp_port": "587", "username": addr, "password": app_password(pw, prov), "from_email": addr,
                   "from_name": biz["name"][:100], "secret": secrets.token_urlsafe(24)}
            steps.append({"op": "email", "cfg": cfg})
    elif key == "website":
        urls = websites(form.get("sites"))
        if not urls:
            raise Invalid("ob_need_site", "sites")
        ans.update(value=urls)
        steps += [{"op": "source", "slot": u, "kind": "website", "name": (urlsplit(u).hostname or u)[:120], "dest": u} for u in urls]
    elif key == "facebook":
        page, user = facebook(form.get("value"))
        if not page:
            raise Invalid("ob_need_profile", "value")
        ans.update(value=page, username=user)
        steps.append({"op": "site", "fields": {"facebook": page}})
        if user:
            steps.append({"op": "lead_settings", "fields": {"messenger": user}})
        steps.append({"op": "source", "slot": "bio", "kind": "facebook", "name": "Facebook", "dest": ""})
    elif key in ("instagram", "tiktok"):
        user = handle(form.get("value"), key)
        if not user:
            raise Invalid("ob_need_profile", "value")
        ans.update(value=user)
        steps += [{"op": "site", "fields": {key: user}},
                  {"op": "source", "slot": "bio", "kind": "instagram" if key == "instagram" else "other",
                   "name": "Instagram" if key == "instagram" else "TikTok", "dest": ""}]
    elif key == "youtube":
        url = youtube(form.get("value"))
        if not url:
            raise Invalid("ob_need_profile", "value")
        ans.update(value=url)
        steps.append({"op": "source", "slot": "bio", "kind": "other", "name": "YouTube", "dest": ""})
    elif key == "google":
        url = _url(form.get("value"), "value")
        ans.update(value=url)
        steps.append({"op": "source", "slot": "bio", "kind": "google", "name": "Google", "dest": ""})
    elif key == "calendar":
        kind = str(form.get("kind") or "")
        if kind not in CAL_KINDS:
            raise Invalid("ob_pick_one", "kind")
        ans.update(kind=kind, added=bool(prev.get("added")) and prev.get("kind") == kind)
    elif key in ("payments", "chats"):
        opts = payment_options(biz) if key == "payments" else chat_options(biz)
        got = [x for x in _list(form, "pick") if x in opts]
        if not got:
            raise Invalid("ob_pick_some", "pick")
        ans.update(value=got)
    elif key == "prices":                                      # cfg01: «tell me your prices — I load them for you»
        text_ = str(form.get("prices") or "").replace("\r\n", "\n").strip()[:4000]
        photo = str(form.get("photo_id") or "") or (prev.get("photo") if prev.get("has") == "yes" else "")
        if not text_ and not photo:
            raise Invalid("ob_need_prices", "prices")
        ans.update(value=text_, hours=_clean(form.get("hours"), 300), photo=photo or None)
    elif key == "tools":
        by = {x.key: x for x in tools(biz)}
        got = [x for x in _list(form, "pick") if x in by]
        if not got:
            raise Invalid("ob_pick_some", "pick")
        links = {}
        for k in got:
            raw = form.get(f"link_{k}")
            if raw and str(raw).strip():
                links[k] = _url(raw, f"link_{k}")
        ans.update(value=got, links=links)
        steps += [{"op": "source", "slot": k, "kind": "marketplace", "name": by[k].name, "dest": ""} for k in got]
    return ans, steps


def _list(form: dict, name: str) -> list[str]:
    v = form.get(name)
    if isinstance(v, (list, tuple)):
        return [str(x) for x in v]
    return [x for x in str(v or "").split(",") if x]


def answer(biz: dict, actor: str, key: str, form: dict, *, dry_run: bool | None = None) -> dict:
    """One answer: planned, then (not in a dry run) the email's sign-in tested BEFORE anything is saved, then everything
    done in one transaction. -> {"key", "has", "steps": [op names], "dry_run"}. Raises Invalid / EmailFailed (nothing saved)."""
    if dry_run is None:
        dry_run = dry()
    with db.tx() as conn:
        ans, steps = plan(conn, biz, key, form)
    out = {"key": key, "has": ans["has"], "steps": [s["op"] for s in steps], "dry_run": dry_run}
    if dry_run:
        return out
    for s in steps:
        if s["op"] == "email":
            if biz["sandbox"] or config.SANDBOX():
                raise Invalid("sandbox_only", "email")
            why = channels.adapter("email").check(s["cfg"])
            if why:
                raise EmailFailed(f"ob_mail_{why}", "app_password")
    with db.tx() as conn:
        from . import businesses as B
        cur = B.get(conn, biz["id"])
        prev = answers(conn, biz["id"]).get(key) or {}
        made = dict(prev.get("sources") or {}) if ans["has"] == "yes" else {}
        for s in steps:
            cur = _do(conn, cur, actor, s, made)
        if ans["has"] == "yes" and made:
            keep = {s["slot"] for s in steps if s["op"] == "source"}
            ans["sources"] = {k: v for k, v in made.items() if k in keep}
        _save_answer(conn, cur, actor, key, ans)
    return out


def _do(conn: Connection, biz: dict, actor: str, step: dict, made: dict) -> dict:
    from . import inbox as X
    from . import leads as LD
    from . import site as ST
    op = step["op"]
    if op == "site":
        return ST.save(conn, biz, actor, **step["fields"])
    if op == "lead_settings":
        LD.save_settings(conn, biz, actor, **step["fields"])
        return biz
    if op == "email":
        X.connect(conn, biz, actor, channel="email", name=step["cfg"]["from_email"], address=step["cfg"]["from_email"],
                  cfg=step["cfg"])
        return biz
    if op == "source":
        src = _source(conn, biz, actor, step, made.get(step["slot"]))
        if step.get("chat"):                                 # its short link opens the business's own WhatsApp, "(ref <code>)"
            st = LD.settings(conn, biz["id"])
            link = LD.chat_links(biz, st, src)["whatsapp"]
            if link and src["dest"] != link:
                src = LD.save_source(conn, biz, actor, {"kind": src["kind"], "name": src["name"], "dest": link}, src["id"])
        made[step["slot"]] = str(src["id"])
        return biz
    raise ValueError(op)


def _source(conn: Connection, biz: dict, actor: str, step: dict, known: str | None) -> dict:
    """The source this answer made before (kept, switched back on), else one with the same place, else a new one —
    answering twice never makes two."""
    from . import leads as LD
    from . import NotFound
    data = {"kind": step["kind"], "name": step["name"], "dest": step["dest"]}

    def same(s: dict) -> bool:
        if s["kind"] != data["kind"]:
            return False
        if step.get("chat"):                                 # her WhatsApp source: its address is the chat link itself
            return s["name"] == data["name"] and (not s["dest"] or s["dest"].startswith("https://wa.me/"))
        if data["dest"]:
            return s["dest"].lower() == data["dest"].lower()
        return s["name"] == data["name"] and not s["dest"]

    cands = []
    if known:
        try:
            cands.append(LD.source(conn, biz["id"], known))
        except NotFound:
            pass
    for s in cands + LD.sources(conn, biz["id"]):
        if same(s):
            if not s["active"]:
                LD.set_active(conn, biz, actor, s["id"], True)
            return LD.source(conn, biz["id"], s["id"])
    return LD.save_source(conn, biz, actor, data)


# ---------------------------------------------------------------- where each one stands (live, from what is really connected)
def _line(ok, k: str, **a) -> dict:
    return {"ok": ok, "k": k, "a": a}


def _link(k: str, url: str, copy: bool = True, **a) -> dict:
    return {"k": k, "url": url, "copy": copy, "a": a}


def meta_tap(biz: dict) -> dict:
    """Meta's one-tap sign-ups this server offers (its own Meta app): {"wa": bool, "pages": bool}."""
    from . import meta as MT
    if biz["sandbox"] or not MT.configured():
        return {"wa": False, "pages": False}
    s = MT.settings()
    return {"wa": bool(s["wa_config_id"]), "pages": bool(s["login_config_id"])}


def state(conn: Connection, biz: dict, user_id: UUID | None = None, *, links: bool = True, lang: str | None = None) -> dict:
    """Every question with its live green light, what was set up, and the one next step; «Ready: N of M»."""
    from . import gbp as GB
    from . import gcal
    from . import inbox as X
    from . import leads as LD
    from . import payments as PM
    from . import tiktok as TT
    ans = answers(conn, biz["id"])
    accs = [a for a in X.accounts(conn, biz["id"]) if a["channel"] != "sandbox"]
    chans = {a["channel"] for a in accs}
    site = biz.get("site") or {}
    st = LD.settings(conn, biz["id"])
    srcs = {str(s["id"]): s for s in LD.sources(conn, biz["id"])}
    tap = meta_tap(biz)
    pays = {p["provider"] for p in PM.accounts(conn, biz["id"])}
    items = {}
    for key in questions(biz):
        a = ans.get(key) or {}
        it = {"key": key, "has": a.get("has"), "answer": a, "lines": [], "links": [], "step": None, "extra": [], "status": "new"}
        mine = [srcs[i] for i in (a.get("sources") or {}).values() if i in srcs and srcs[i]["active"]]
        if links:
            for s in mine:
                if key == "website":
                    it["links"] += [_link("ob_k_snippet", LD.embed(s), name=s["name"]), _link("ob_k_tracked", LD.short_link(s), name=s["name"])]
                elif key != "whatsapp":
                    it["links"].append(_link("ob_k_bio", LD.short_link(s), name=s["name"]))
        status = None
        if key == "whatsapp":
            num = LD.chat_number(biz, st)
            api = bool(chans & {"whatsapp", "whatsapp_twilio"})
            if num:
                it["lines"].append(_line(True, "ob_l_wa_number", number=num))
                if links:
                    chat = next((LD.chat_links(biz, st, s)["whatsapp"] for s in mine), None) or LD.chat_links(biz, st, None)["whatsapp"]
                    it["links"].append(_link("ob_k_wa_chat", chat))
                    it["links"] += [_link("ob_k_wa_short", LD.short_link(s)) for s in mine]
            it["lines"].append(_line(True if api else None, "ob_l_wa_api_on" if api else "ob_l_wa_api_off"))
            if a.get("has") == "yes":
                if not num:
                    status, it["step"] = "todo", {"k": "ob_s_wa_number", "href": "/app/onboard?q=whatsapp"}
                elif a.get("assist") and not api:
                    status = "todo"
                    it["step"] = ({"k": "ob_s_wa_tap", "href": "/app/onboard?q=whatsapp#meta"} if tap["wa"] else
                                  {"k": "ob_s_wa_paste", "href": "/app/channels?c=whatsapp#manual"})
                    if not tap["wa"]:
                        it["lines"].append(_line(None, "ob_w_wa_tap_off"))
                else:
                    status = "ready"
            elif num or api:
                status = "ready"
        elif key == "prices":                                  # cfg01: green once their real prices are loaded
            priced = conn.execute(select(func.count()).select_from(db.booking_services).where(
                db.booking_services.c.business_id == biz["id"], db.booking_services.c.active,
                db.booking_services.c.price_minor > 0)).scalar()
            if priced:
                it["lines"].append(_line(True, "ob_l_prices_on", n=priced))
                status = "ready"
            elif a.get("has") == "yes":
                it["lines"].append(_line(None, "ob_l_prices_valeria"))
                status = "todo"
        elif key == "email":
            mails = [x["address"] or x["name"] for x in accs if x["channel"] == "email"]
            for m in mails:
                it["lines"].append(_line(True, "ob_l_mail_on", email=m))
            prov = a.get("provider")
            if mails:
                status = "ready"
            elif a.get("has") == "yes":
                if not prov:
                    status, it["step"] = "todo", {"k": "ob_s_mail_host", "href": "/app/onboard?q=email"}
                elif prov in ("microsoft", "m365"):
                    status = "waiting"
                    it["lines"].append(_line(None, "ob_w_mail_microsoft", email=a.get("value", "")))
                elif prov == "other":
                    status, it["step"] = "todo", {"k": "ob_s_mail_other", "href": "/app/channels?c=email#manual"}
                else:
                    status, it["step"] = "todo", {"k": "ob_s_mail_app", "href": "/app/onboard?q=email", "a": {"provider": MAIL[prov]["name"]}}
        elif key == "website":
            for s in mine:
                it["lines"].append(_line(True, "ob_l_site", name=s["dest"] or s["name"]))
            if mine:
                status = "ready"
                it["extra"].append({"k": "ob_x_paste_snippet", "href": "/app/leads?tab=sources"})
            elif a.get("has") == "yes":
                status, it["step"] = "todo", {"k": "ob_s_site", "href": "/app/onboard?q=website"}
            if a.get("has") == "no" and not biz.get("site_enabled"):
                it["extra"].append({"k": "ob_x_own_site", "href": "/app/website"})
        elif key in ("facebook", "instagram", "tiktok", "youtube", "google"):
            prof = a.get("value") or (site.get(key) if key in ("facebook", "instagram", "tiktok") else None)
            if prof:
                it["lines"].append(_line(True, "ob_l_profile", value=("@" + prof) if key in ("instagram", "tiktok") else prof))
            if key == "facebook" and st.get("messenger") and links:
                it["links"] += [_link("ob_k_messenger", LD.chat_links(biz, st, s)["messenger"]) for s in mine[:1]]
            on = None
            if key in ("facebook", "instagram"):
                on = ("messenger" if key == "facebook" else "instagram") in chans
                it["lines"].append(_line(True if on else None, f"ob_l_{key}_dm_{'on' if on else 'off'}"))
                if not on and tap["pages"]:
                    it["extra"].append({"k": "ob_x_meta_tap", "href": f"/app/onboard?q={key}#meta"})
            elif key == "tiktok":
                on = bool(TT.account(conn, biz["id"]))
                if on:
                    it["lines"].append(_line(True, "ob_l_tiktok_dm_on"))
                elif TT.configured() and TT.available_in(biz["country"]) and not biz["sandbox"]:
                    it["extra"].append({"k": "ob_x_tiktok", "href": "/app/tiktok"})
            elif key == "google":
                lk = GB.link(conn, biz["id"])
                on = bool(lk and lk.get("location") and lk["status"] == "ok")
                if on:
                    it["lines"].append(_line(True, "ob_l_google_on", name=lk.get("location_title") or "Google"))
                elif GB.configured() and not biz["sandbox"]:
                    it["extra"].append({"k": "ob_x_google", "href": "/app/google"})
                else:
                    it["lines"].append(_line(None, "ob_w_google_off"))
            if mine or on:
                status = "ready"
            elif a.get("has") == "yes":
                status, it["step"] = "todo", {"k": "ob_s_profile", "href": f"/app/onboard?q={key}"}
            elif prof:
                status = "ready"
        elif key == "calendar":
            lk = gcal.link(conn, biz["id"])
            if lk:
                it["lines"].append(_line(True, "ob_l_gcal_on", email=lk.get("account_email") or "Google"))
                status = "ready"
            elif a.get("has") == "yes":
                g_ok = a.get("kind") == "google" and gcal.configured() and not biz["sandbox"]
                if a.get("added"):
                    it["lines"].append(_line(True, "ob_l_feed_added"))
                    status = "ready"
                elif g_ok:
                    status, it["step"] = "todo", {"k": "ob_s_gcal", "href": "/app/calendar/google"}
                else:
                    status, it["step"] = "todo", {"k": "ob_s_feed", "href": "/app/onboard?q=calendar"}
            if links and user_id and a.get("has") == "yes" and not lk:
                from . import chatcal
                feed = chatcal.feed_url(conn, biz["id"], user_id)
                if feed:
                    it["links"].append(_link("ob_k_feed", feed))
        elif key == "payments":
            got = list(a.get("value") or [])
            todo = False
            for p in got:
                prov = PM.ALIASES.get(p, p)
                if p == "cash":
                    it["lines"].append(_line(True, "ob_l_cash"))
                elif prov in pays:
                    it["lines"].append(_line(True, "ob_l_pay_on", name=pay_name(p, lang)))
                elif prov in PM.PROVIDERS:
                    it["lines"].append(_line(False, "ob_l_pay_todo", name=pay_name(p, lang)))
                    todo = True
                else:
                    it["lines"].append(_line(None, "ob_w_pay_cant", name=pay_name(p, lang)))
            if a.get("has") == "yes":
                if todo:
                    status, it["step"] = "todo", {"k": "ob_s_pay", "href": "/app/settings#payments"}
                else:
                    status = "ready" if any(p == "cash" or PM.ALIASES.get(p, p) in pays for p in got) else "waiting"
            elif pays:
                status = "ready"
        elif key == "chats":
            got = list(a.get("value") or [])
            todo = []
            for c in got:
                if c in chans:
                    it["lines"].append(_line(True, "ob_l_chat_on", name=CHAT_NAMES[c]))
                elif c in channels.available() and channels.adapter(c).manual:
                    it["lines"].append(_line(False, "ob_l_chat_todo", name=CHAT_NAMES[c]))
                    todo.append(c)
                else:
                    it["lines"].append(_line(None, "ob_w_chat_cant", name=CHAT_NAMES[c]))
            if a.get("has") == "yes":
                if todo:
                    status = "todo"
                    it["step"] = {"k": "ob_s_chat", "href": f"/app/channels?c={todo[0]}#manual", "a": {"name": CHAT_NAMES[todo[0]]}}
                else:
                    status = "ready" if any(c in chans for c in got) else "waiting"
        elif key == "tools":
            by = {x.key: x for x in tools(biz)}
            for k in a.get("value") or []:
                if k in by:
                    it["lines"].append(_line(True, "ob_l_tool", name=by[k].name, url=(a.get("links") or {}).get(k) or by[k].url))
            if a.get("has") == "yes":
                status = "ready"
                it["extra"].append({"k": "ob_x_tools", "href": "/app/connect-apps#in"})
        if a.get("has") == "no":
            status = "ready" if status == "ready" else "none"
        elif a.get("has") == "later":
            status = "ready" if status == "ready" else "later"
            if status == "later":
                it["step"] = {"k": "ob_s_answer", "href": f"/app/onboard?q={key}"}
        it["status"] = status or ("new" if not a else "none")
        items[key] = it
    order = questions(biz)
    counted = [items[k] for k in order if items[k]["status"] not in ("none", "new")]
    nxt = next((k for k in order if not ans.get(k)), None)
    return {"qs": items, "order": order, "ready": sum(1 for i in counted if i["status"] == "ready"), "total": len(counted),
            "asked": sum(1 for k in order if ans.get(k) and ans[k].get("has") != "later"), "count": len(order), "next": nxt,
            "pending": [i for i in counted if i["status"] in ("todo", "later", "waiting")], "started": bool(ans),
            "done": nxt is None and all(i["status"] in ("ready", "waiting") for i in counted)}


def pay_name(p: str, lang: str | None = None) -> str:
    return {"bank_transfer": _t("bank_transfer", lang), "cash": _t("ob_cash", lang), "mercadopago": "Mercado Pago", "mpesa": "M-Pesa",
            "mtn_momo": "MTN MoMo", "pix": "Pix", "upi": "UPI", "sepa": "SEPA"}.get(p, p.replace("_", " ").title())


def nudge(conn: Connection, biz: dict) -> dict | None:
    """Today's card until the talk is over and nothing is left to do: {"ready", "total", "asked", "count", "step"}."""
    s = state(conn, biz, links=False)
    if s["done"]:
        return None
    first = next((i for i in s["pending"] if i["step"]), None)
    return {"ready": s["ready"], "total": s["total"], "asked": s["asked"], "count": s["count"], "next": s["next"],
            "step": first["step"] if first and not s["next"] else None, "item": first["key"] if first and not s["next"] else None}
