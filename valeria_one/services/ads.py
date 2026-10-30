"""Ads helper (ars02, v0.21): the words for an ad and a link that says which ad brought whom — $0 to us.

Valeria One never runs ads, never spends money and holds no ad account or key of ours: the owner places the ad in their
OWN Meta Ads Manager, Google Ads or TikTok Ads Manager (one tap opens it; one tap copies each piece). Here:
  * a draft — headlines, the ad text and who to show it to — written by the business's OWN AI key in its style (like
    the post writer), or, with no key, a plain draft from the owner's own words and the business's facts; never sent
    or published anywhere;
  * a tracked link /go/<code> to put in the ad: it counts the click, remembers it on the visitor's phone (14 days, like
    the friend links) and opens the business's page with utm_source / utm_medium / utm_campaign / utm_content added
    (their own website's analytics see it too). A visitor who then becomes a NEW customer (booking, website, form,
    funnel) is marked with the ad as their source (contacts.source = "ad:<code>"), so campaigns can find them by it.
"""
from __future__ import annotations

import json
import re
import secrets
import unicodedata
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import UUID

from sqlalchemy import func, insert, select, update
from sqlalchemy.engine import Connection

from .. import config, db, packs
from . import Invalid, NotFound, log
from .funnels import https

AD, C = db.ads, db.contacts
PLATFORMS = ("meta", "google", "tiktok", "other")
# where each platform's own ad tool opens (the business signs in with its own account there)
MANAGERS = {"meta": "https://adsmanager.facebook.com/adsmanager/", "google": "https://ads.google.com/aw/campaigns/new",
            "tiktok": "https://ads.tiktok.com/i18n/creation", "other": None}
MEDIUM = {"meta": "paid_social", "google": "cpc", "tiktok": "paid_social", "other": "paid"}
# each platform's own limits (characters): headline, text
LIMITS = {"meta": (40, 125), "google": (30, 90), "tiktok": (40, 100), "other": (60, 300)}
HEADLINES = 5
REMEMBER_DAYS = 14
_CODE = re.compile(r"^[A-Za-z0-9_-]{8,16}$")


def _t(key: str, lang: str | None) -> str:
    from ..web.i18n import t, ui_lang
    return t(key, ui_lang(lang))


def _slug(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")[:60] or "ad"


# ---------------------------------------------------------------- the ads
def list_(conn: Connection, business_id: UUID) -> list[dict]:
    rows = [dict(r) for r in conn.execute(select(AD).where(AD.c.business_id == business_id).order_by(AD.c.created_at.desc()))
            .mappings()]
    leads = dict(conn.execute(select(C.c.source, func.count()).where(C.c.business_id == business_id, C.c.source.like("ad:%"),
                                                                     C.c.deleted_at.is_(None)).group_by(C.c.source)).all())
    for r in rows:
        r["leads"] = leads.get(f"ad:{r['code']}", 0)
    return rows


def get(conn: Connection, business_id: UUID, ad_id: UUID) -> dict:
    r = conn.execute(select(AD).where(AD.c.business_id == business_id, AD.c.id == ad_id)).mappings().first()
    if not r:
        raise NotFound("ad")
    d = dict(r)
    d["leads"] = conn.execute(select(func.count()).where(C.c.business_id == business_id, C.c.source == f"ad:{d['code']}",
                                                         C.c.deleted_at.is_(None))).scalar()
    return d


def destinations(conn: Connection, biz: dict) -> list[tuple[str, str]]:
    """The business's own pages an ad can open: (key for the screen's words, address)."""
    from . import forms as FM
    from . import funnels as FU
    base = config.PUBLIC_URL()
    out = []
    if biz.get("slug") and biz.get("site_enabled"):
        out.append(("ad_dest_site", f"{base}/s/{biz['slug']}"))
    if biz.get("slug") and biz.get("booking_enabled"):
        out.append(("ad_dest_book", f"{base}/b/{biz['slug']}"))
    for f in FU.list_(conn, biz["id"]):
        if f["active"] and f["first"] and biz.get("slug"):
            out.append((f["name"], f"{base}/s/{biz['slug']}/p/{f['first']}"))
    for f in FM.list_(conn, biz["id"]):
        if f["active"]:
            out.append((f["name"], FM.url(f)))
    return out


def save(conn: Connection, biz: dict, actor: str, data: dict, ad_id: UUID | None = None) -> dict:
    name = " ".join(str(data.get("name") or "").split())[:120]
    if not name:
        raise Invalid("give the ad a name (only you see it)", "name")
    platform = data.get("platform") if data.get("platform") in PLATFORMS else "meta"
    dest = https(data.get("dest"), "dest")
    if not dest:
        raise Invalid("where the ad sends people (an https:// address)", "dest")
    hmax, tmax = LIMITS[platform]
    heads = data.get("headlines")
    if isinstance(heads, str):
        heads = heads.splitlines()
    vals = {"name": name, "platform": platform, "dest": dest, "idea": str(data.get("idea") or "").strip()[:2000],
            "headline": "\n".join(h.strip()[:hmax] for h in (heads or []) if h.strip())[: (hmax + 1) * HEADLINES * 3],
            "body": str(data.get("body") or "").strip()[:2000], "audience": str(data.get("audience") or "").strip()[:2000]}
    if "by_ai" in data:
        vals["by_ai"] = bool(data["by_ai"])
    if ad_id:
        get(conn, biz["id"], ad_id)
        conn.execute(update(AD).where(AD.c.business_id == biz["id"], AD.c.id == ad_id).values(**vals, updated_at=func.now()))
        return get(conn, biz["id"], ad_id)
    row = conn.execute(insert(AD).values(business_id=biz["id"], code=secrets.token_urlsafe(9), created_by=actor, **vals)
                       .returning(AD.c.id)).scalar()
    log(conn, biz["id"], "ad.created", actor, row, {"platform": platform})
    return get(conn, biz["id"], row)


def delete(conn: Connection, biz: dict, actor: str, ad_id: UUID) -> None:
    """Deleting an ad stops its link; the customers it brought keep their source."""
    get(conn, biz["id"], ad_id)
    conn.execute(AD.delete().where(AD.c.business_id == biz["id"], AD.c.id == ad_id))
    log(conn, biz["id"], "ad.deleted", actor, ad_id)


def warnings(ad: dict) -> list[dict]:
    """Pieces longer than the platform takes: [{"what", "n", "max"}] (the screen says it plainly)."""
    hmax, tmax = LIMITS[ad["platform"]]
    out = [{"what": "headline", "n": len(h), "max": hmax} for h in (ad["headline"] or "").splitlines() if len(h) > hmax]
    if len(ad["body"] or "") > tmax:
        out.append({"what": "text", "n": len(ad["body"]), "max": tmax})
    return out


# ---------------------------------------------------------------- the words
def draft(biz: dict, *, idea: str, platform: str, http=None) -> dict:
    """{"headlines": [..], "body", "audience", "by_ai"} — the business's own AI key in its style, else a plain draft."""
    from . import ai as AI
    from . import style as STY
    idea = " ".join((idea or "").split())[:500]
    if not idea:
        raise Invalid(_t("ad_need_idea", biz["language"]), "idea")
    platform = platform if platform in PLATFORMS else "meta"
    hmax, tmax = LIMITS[platform]
    with db.tx() as conn:
        creds = AI.own_key(conn, biz)
        prof = STY.get(conn, biz["id"])
        facts = AI._facts(conn, biz)
    if creds:
        prov, key, model = creds
        lang = AI._LANG.get(biz["language"], "the business's language")
        system = (f"You write ad copy for {biz['name']}, a small business, for {platform} ads. Write in {lang}. "
                  "Use ONLY the owner's idea and the facts below; never invent prices, discounts, dates, results or promises. "
                  "No health, money or personal-attribute claims, nothing that names who the reader is. "
                  f"Answer with JSON only: {{\"headlines\": [{HEADLINES} headlines, each at most {hmax} characters], "
                  f"\"text\": the ad text at most {tmax} characters, \"audience\": who to show it to (place, age range, "
                  "interests) in 1-3 short lines}.\n" + STY.prompt(prof) + f"\n\nFACTS:\n{facts}")
        try:
            raw = AI._call(prov, key, model, system, [("user", idea)], AI._http(http), max_tokens=700)
            m = re.search(r"\{.*\}", raw or "", re.S)
            j = json.loads(m.group(0)) if m else {}
            heads = [str(h).strip()[:hmax] for h in (j.get("headlines") or []) if str(h).strip()][:HEADLINES]
            if heads and str(j.get("text") or "").strip():
                return {"headlines": heads, "body": str(j["text"]).strip()[:tmax], "audience": str(j.get("audience") or "").strip()[:600],
                        "by_ai": True}
        except (AI.AIError, ValueError, AttributeError):
            pass
    lang = biz["language"]
    pack = packs.country(biz["country"])
    city = (biz.get("site") or {}).get("address") or pack.label(lang.split("-")[0])
    heads = [h[:hmax] for h in (idea[:hmax], biz["name"][:hmax], _t("ad_plain_h3", lang).format(business=biz["name"])[:hmax]) if h]
    body = STY.apply(prof, _t("ad_plain_text", lang).replace("{idea}", idea).replace("{business}", biz["name"]))[:tmax]
    ind = packs.text(packs.industry(biz["industry"]).name, lang.split("-")[0])
    aud = _t("ad_plain_audience", lang).format(place=city, trade=ind)
    return {"headlines": heads, "body": body, "audience": aud, "by_ai": False}


# ---------------------------------------------------------------- the tracked link
def link(ad: dict) -> str:
    return f"{config.PUBLIC_URL()}/go/{ad['code']}"


def landing(ad: dict) -> str:
    """The ad's own page with the utm_* the owner's analytics read (any utm_* already there is replaced)."""
    u = urlsplit(ad["dest"])
    q = [(k, v) for k, v in parse_qsl(u.query, keep_blank_values=True) if not k.startswith("utm_")]
    q += [("utm_source", ad["platform"]), ("utm_medium", MEDIUM[ad["platform"]]), ("utm_campaign", _slug(ad["name"])),
          ("utm_content", ad["code"])]
    return urlunsplit((u.scheme, u.netloc, u.path, urlencode(q), u.fragment))


def open_link(code: str) -> tuple[dict, dict]:
    """/go/<code> opened: (the ad, the business), counted. NotFound when unknown or the business is paused."""
    if not _CODE.match(code or ""):
        raise NotFound("link")
    with db.tx() as conn:
        a = conn.execute(select(AD).where(AD.c.code == code)).mappings().first()
        if not a:
            raise NotFound("link")
        biz = dict(conn.execute(select(db.businesses).where(db.businesses.c.id == a["business_id"])).mappings().one())
        if biz["suspended_at"]:
            raise NotFound("link")
        conn.execute(update(AD).where(AD.c.id == a["id"]).values(clicks=AD.c.clicks + 1))
    return dict(a), biz


def remember(ad: dict, now: datetime | None = None) -> dict:
    return {"c": ad["code"], "b": str(ad["business_id"]), "at": (now or datetime.now(timezone.utc)).isoformat()}


def claim(conn: Connection, biz: dict, ref: dict | None, contact: dict) -> bool:
    """A NEW customer on a phone that came from an ad: the ad is their source. Never over an older customer."""
    if not isinstance(ref, dict) or ref.get("b") != str(biz["id"]):
        return False
    try:
        opened = datetime.fromisoformat(str(ref.get("at")))
    except ValueError:
        return False
    if datetime.now(timezone.utc) - opened > timedelta(days=REMEMBER_DAYS):
        return False
    if contact.get("created_at") and contact["created_at"] < opened - timedelta(minutes=1):
        return False
    code = str(ref.get("c") or "")[:16]
    a = conn.execute(select(AD.c.id).where(AD.c.business_id == biz["id"], AD.c.code == code)).scalar()
    if not a:
        return False
    conn.execute(update(C).where(C.c.business_id == biz["id"], C.c.id == contact["id"]).values(source=f"ad:{code}"))
    log(conn, biz["id"], "ad.lead", "customer", contact["id"], {"ad_id": str(a)})
    return True
