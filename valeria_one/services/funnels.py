"""Funnels (ars02, v0.21): landing pages in steps — page 1 (the offer) -> page 2 (the form) -> thank you — built from
blocks, public at the business's own address /s/<business>/p/<page>, wearing the business's look.

Blocks: hero, text, image, video (YouTube / Vimeo, by the address the owner pastes), testimonial, offer (a price), form
(one of the business's own forms: services/forms.py), booking button (its booking page), pay button (its OWN pay link,
any https address), countdown, FAQ, button. A page is only ever drawn from these blocks: every word is escaped where
it is drawn, every address is https and checked here, a video is kept as its provider + id (never as HTML), so nothing
the owner types can run on a visitor's phone.

Counting, without cookies and without keeping an IP: a visitor is a hash of the IP with the server's secret and the
day (like the sandbox's own visit counter), one "view" per page per visitor per day, one "lead" when they send the
page's form. Per step: visitors, leads, and how many went on to the next step.

A funnel switched off (the default while it is being made) is seen only by the business's own people (a preview).
"""
from __future__ import annotations

import hashlib
import re
import secrets
import unicodedata
from datetime import date, datetime, timezone
from uuid import UUID

from sqlalchemy import and_, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection

from .. import config, db
from . import Invalid, NotFound, log

FN, FP, FH = db.funnels, db.funnel_pages, db.funnel_hits
BLOCK_TYPES = ("hero", "text", "image", "video", "testimonial", "offer", "form", "booking", "pay", "countdown", "faq", "button")
GOES = ("next", "book", "url")               # where a button goes: the next step, the booking page, an address
MAX_BLOCKS = 30
MAX_PAGES = 10
MAX_FUNNELS = 30
MAX_LIST = 15                                # FAQ questions, offer lines
_URL_BAD = re.compile(r"[\s\"'<>\\`]")
_YT = re.compile(r"^(?:https://)?(?:www\.|m\.)?(?:youtube\.com/(?:watch\?(?:.*&)?v=|embed/|shorts/)|youtu\.be/)([A-Za-z0-9_-]{11})")
_VIMEO = re.compile(r"^(?:https://)?(?:www\.|player\.)?vimeo\.com/(?:video/)?(\d{5,12})")


def _t(key: str, lang: str | None) -> str:
    from ..web.i18n import t, ui_lang
    return t(key, ui_lang(lang))


# ---------------------------------------------------------------- what a block may hold
def https(v, field: str = "url") -> str:
    """An https address, nothing else (no javascript:, no data:, no quotes or spaces that could break out)."""
    v = str(v or "").strip()[:500]
    if not v:
        return ""
    if not v.lower().startswith("https://") or _URL_BAD.search(v) or len(v) < 12:
        raise Invalid(f"{field}: an address that starts with https://", field)
    return v


def video(v) -> dict | None:
    """A YouTube or Vimeo address -> {"p": "yt"|"vimeo", "id"}; anything else is refused."""
    v = str(v or "").strip()
    if not v:
        return None
    m = _YT.match(v)
    if m:
        return {"p": "yt", "id": m.group(1)}
    m = _VIMEO.match(v)
    if m:
        return {"p": "vimeo", "id": m.group(1)}
    raise Invalid("video: a YouTube or Vimeo address", "video")


def video_src(b: dict) -> str:
    """The embed address of a cleaned video block (the id is checked, so it is safe in an iframe src)."""
    if b.get("p") == "yt" and re.fullmatch(r"[A-Za-z0-9_-]{11}", b.get("id") or ""):
        return f"https://www.youtube-nocookie.com/embed/{b['id']}"
    if b.get("p") == "vimeo" and re.fullmatch(r"\d{5,12}", b.get("id") or ""):
        return f"https://player.vimeo.com/video/{b['id']}"
    return ""


def _s(v, n: int) -> str:
    return str(v or "").strip()[:n]


def _uuid(v) -> str | None:
    try:
        return str(UUID(str(v))) if v else None
    except ValueError:
        return None


def _minor(v) -> int | None:
    if v in (None, ""):
        return None
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise Invalid("price: a number", "price") from None
    if n < 0:
        raise Invalid("price: a number", "price")
    return n


def _go(b: dict, raw: dict) -> None:
    to = raw.get("to") if raw.get("to") in GOES else "next"
    b["to"] = to
    if to == "url":
        b["url"] = https(raw.get("url"), "button")
        if not b["url"]:
            raise Invalid("button: the address it opens", "url")


def clean_blocks(conn: Connection, business_id: UUID, raw) -> list[dict]:
    """The blocks as they may be kept: known kinds, known fields, lengths, https addresses, the business's own forms
    and pictures only. Empty blocks are dropped."""
    from . import forms as FM
    out = []
    for r in list(raw or [])[:MAX_BLOCKS]:
        r = r or {}
        t = r.get("t")
        if t not in BLOCK_TYPES:
            continue
        b: dict = {"t": t}
        if t == "hero":
            b.update(title=_s(r.get("title"), 160), text=_s(r.get("text"), 600), button=_s(r.get("button"), 60))
            if r.get("file"):
                b["file"] = _own_file(conn, business_id, r.get("file"))
            if b["button"]:
                _go(b, r)
            if not (b["title"] or b["text"]):
                continue
        elif t == "text":
            b.update(title=_s(r.get("title"), 160), text=_s(r.get("text"), 4000))
            if not (b["title"] or b["text"]):
                continue
        elif t == "image":
            b.update(alt=_s(r.get("alt"), 200))
            if r.get("file"):
                b["file"] = _own_file(conn, business_id, r.get("file"))
            else:
                b["url"] = https(r.get("url"), "image")
            if not (b.get("file") or b.get("url")):
                continue
        elif t == "video":
            v = r if r.get("p") else video(r.get("url"))
            if not v or not video_src(v):
                continue
            b.update(p=v["p"], id=v["id"], title=_s(r.get("title"), 160))
        elif t == "testimonial":
            b.update(quote=_s(r.get("quote"), 600), name=_s(r.get("name"), 120))
            if not b["quote"]:
                continue
        elif t == "offer":
            items = r.get("items")
            if isinstance(items, str):
                items = items.splitlines()
            b.update(title=_s(r.get("title"), 160), price=_minor(r.get("price")), was=_minor(r.get("was")),
                     items=[_s(x, 160) for x in (items or []) if _s(x, 160)][:MAX_LIST], button=_s(r.get("button"), 60))
            if b["button"]:
                _go(b, r)
            if not (b["title"] or b["price"] is not None):
                continue
        elif t == "form":
            fid = _uuid(r.get("form"))
            if not fid:
                continue
            FM.get(conn, business_id, UUID(fid))                  # only the business's own forms
            b["form"] = fid
        elif t == "booking":
            b["button"] = _s(r.get("button"), 60)
        elif t == "pay":
            b.update(button=_s(r.get("button"), 60), url=https(r.get("url"), "pay"))
            if not b["url"]:
                continue
        elif t == "countdown":
            until = _when(r.get("until"))
            if not until:
                continue
            b.update(until=until, text=_s(r.get("text"), 160))
        elif t == "faq":
            qa = r.get("items")
            if isinstance(qa, str):                               # "Question? | Answer" one per line
                qa = [dict(zip(("q", "a"), (x.split("|", 1) + [""])[:2])) for x in qa.splitlines() if x.strip()]
            b["items"] = [{"q": _s(x.get("q"), 200), "a": _s(x.get("a"), 1000)} for x in (qa or []) if _s((x or {}).get("q"), 200)][:MAX_LIST]
            if not b["items"]:
                continue
        elif t == "button":
            b["button"] = _s(r.get("button"), 60)
            if not b["button"]:
                continue
            _go(b, r)
        out.append(b)
    return out


def _when(v) -> str | None:
    """An aware ISO time (kept in UTC) or nothing."""
    if isinstance(v, datetime):
        d = v
    else:
        try:
            d = datetime.fromisoformat(str(v or "").strip())
        except ValueError:
            return None
    if d.tzinfo is None:
        return None
    return d.astimezone(timezone.utc).isoformat()


def _own_file(conn: Connection, business_id: UUID, fid) -> str:
    f = _uuid(fid)
    if not f or not conn.execute(select(db.files.c.id).where(db.files.c.business_id == business_id, db.files.c.id == UUID(f))).first():
        raise NotFound("file")
    return f


# ---------------------------------------------------------------- funnels and their pages
def _slugify(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")[:40] or "page"


def _free_slug(conn: Connection, business_id: UUID, title: str, but: UUID | None = None) -> str:
    base = _slugify(title)
    for i in range(30):
        slug = base if i == 0 else f"{base}-{i + 1}"
        q = select(FP.c.id).where(FP.c.business_id == business_id, FP.c.slug == slug)
        if but:
            q = q.where(FP.c.id != but)
        if not conn.execute(q).first():
            return slug
    return f"{base[:30]}-{secrets.token_hex(3)}"


def list_(conn: Connection, business_id: UUID) -> list[dict]:
    rows = [dict(r) for r in conn.execute(select(FN).where(FN.c.business_id == business_id).order_by(FN.c.created_at.desc()))
            .mappings()]
    firsts = {}
    for p in conn.execute(select(FP.c.funnel_id, FP.c.slug, FP.c.position).where(FP.c.business_id == business_id)
                          .order_by(FP.c.funnel_id, FP.c.position)).mappings():
        firsts.setdefault(p["funnel_id"], p["slug"])
    for r in rows:
        r["first"] = firsts.get(r["id"])
    return rows


def get(conn: Connection, business_id: UUID, funnel_id: UUID) -> dict:
    r = conn.execute(select(FN).where(FN.c.business_id == business_id, FN.c.id == funnel_id)).mappings().first()
    if not r:
        raise NotFound("funnel")
    return dict(r)


def pages(conn: Connection, business_id: UUID, funnel_id: UUID) -> list[dict]:
    return [dict(r) for r in conn.execute(select(FP).where(FP.c.business_id == business_id, FP.c.funnel_id == funnel_id)
                                          .order_by(FP.c.position, FP.c.created_at)).mappings()]


def page(conn: Connection, business_id: UUID, page_id: UUID) -> dict:
    r = conn.execute(select(FP).where(FP.c.business_id == business_id, FP.c.id == page_id)).mappings().first()
    if not r:
        raise NotFound("page")
    return dict(r)


def _need_slug(conn: Connection, biz: dict, actor: str) -> dict:
    """The business's public address part (/s/<slug>): made from its name the first time it is needed."""
    if biz.get("slug"):
        return biz
    from . import booking as BK
    return BK.save_settings(conn, biz, actor, slug=BK._free_slug(conn, biz["name"]))


def create(conn: Connection, biz: dict, actor: str, name: str, template: str | None = None, lang: str | None = None) -> dict:
    name = " ".join(str(name or "").split())[:120]
    if not name:
        raise Invalid("give the funnel a name", "name")
    if conn.execute(select(func.count()).select_from(FN).where(FN.c.business_id == biz["id"])).scalar() >= MAX_FUNNELS:
        raise Invalid(f"at most {MAX_FUNNELS} funnels", "name")
    _need_slug(conn, biz, actor)
    row = dict(conn.execute(insert(FN).values(business_id=biz["id"], name=name, created_by=actor).returning(FN)).mappings().one())
    steps = starter(conn, biz, actor, template, name, lang or biz["language"]) if template else [(name, [])]
    for title, blocks in steps:
        add_page(conn, biz, actor, row["id"], title, blocks)
    log(conn, biz["id"], "funnel.created", actor, row["id"], {"template": template})
    return row


def save(conn: Connection, biz: dict, actor: str, funnel_id: UUID, *, name: str | None = None, active: bool | None = None) -> dict:
    fn = get(conn, biz["id"], funnel_id)
    vals: dict = {}
    if name is not None:
        vals["name"] = " ".join(str(name).split())[:120] or fn["name"]
    if active is not None:
        if active and not pages(conn, biz["id"], funnel_id):
            raise Invalid("add a page first", "active")
        vals["active"] = bool(active)
    if vals:
        conn.execute(update(FN).where(FN.c.id == funnel_id).values(**vals, updated_at=func.now()))
        log(conn, biz["id"], "funnel.updated", actor, funnel_id, {k: v for k, v in vals.items() if k == "active"})
    return get(conn, biz["id"], funnel_id)


def delete(conn: Connection, biz: dict, actor: str, funnel_id: UUID) -> None:
    """Deleting a funnel takes its pages and their counts with it (the leads it brought stay: they are customers)."""
    get(conn, biz["id"], funnel_id)
    conn.execute(FN.delete().where(FN.c.business_id == biz["id"], FN.c.id == funnel_id))
    log(conn, biz["id"], "funnel.deleted", actor, funnel_id)


def add_page(conn: Connection, biz: dict, actor: str, funnel_id: UUID, title: str, blocks: list | None = None) -> dict:
    get(conn, biz["id"], funnel_id)
    have = pages(conn, biz["id"], funnel_id)
    if len(have) >= MAX_PAGES:
        raise Invalid(f"at most {MAX_PAGES} steps", "title")
    title = " ".join(str(title or "").split())[:120] or f"{len(have) + 1}"
    row = conn.execute(insert(FP).values(business_id=biz["id"], funnel_id=funnel_id, title=title,
                                         slug=_free_slug(conn, biz["id"], title),
                                         position=(max((p["position"] for p in have), default=-1) + 1),
                                         blocks=clean_blocks(conn, biz["id"], blocks or [])).returning(FP)).mappings().one()
    return dict(row)


def save_page(conn: Connection, biz: dict, actor: str, page_id: UUID, *, title: str | None = None, slug: str | None = None,
              blocks: list | None = None) -> dict:
    p = page(conn, biz["id"], page_id)
    vals: dict = {}
    if title is not None:
        vals["title"] = " ".join(str(title).split())[:120] or p["title"]
    if slug is not None and _slugify(slug) != p["slug"]:
        vals["slug"] = _free_slug(conn, biz["id"], slug, but=page_id)
    if blocks is not None:
        vals["blocks"] = clean_blocks(conn, biz["id"], blocks)
    if vals:
        conn.execute(update(FP).where(FP.c.id == page_id).values(**vals, updated_at=func.now()))
    return page(conn, biz["id"], page_id)


def move_page(conn: Connection, biz: dict, actor: str, page_id: UUID, step: int) -> None:
    p = page(conn, biz["id"], page_id)
    ps = pages(conn, biz["id"], p["funnel_id"])
    i = next(n for n, x in enumerate(ps) if x["id"] == page_id)
    j = i + (1 if step > 0 else -1)
    if not 0 <= j < len(ps):
        return
    ps[i], ps[j] = ps[j], ps[i]
    for n, x in enumerate(ps):
        conn.execute(update(FP).where(FP.c.id == x["id"]).values(position=n))


def delete_page(conn: Connection, biz: dict, actor: str, page_id: UUID) -> None:
    p = page(conn, biz["id"], page_id)
    conn.execute(FP.delete().where(FP.c.business_id == biz["id"], FP.c.id == page_id))
    if not pages(conn, biz["id"], p["funnel_id"]):
        conn.execute(update(FN).where(FN.c.id == p["funnel_id"]).values(active=False))


def block_edit(blocks: list[dict], act: str, i: int) -> list[dict]:
    """The editor's buttons on one block: move it up or down, remove it. -> the new list (not saved yet)."""
    out = [dict(b) for b in blocks]
    if 0 <= i < len(out):
        if act == "up" and i > 0:
            out[i - 1], out[i] = out[i], out[i - 1]
        elif act == "down" and i < len(out) - 1:
            out[i + 1], out[i] = out[i], out[i + 1]
        elif act == "remove":
            out.pop(i)
    return out


# ---------------------------------------------------------------- public
def public(conn: Connection, slug: str, page_slug: str) -> tuple[dict, dict, dict, dict | None]:
    """-> (business, funnel, page, next page or None). NotFound when unknown or the business is paused."""
    biz = conn.execute(select(db.businesses).where(db.businesses.c.slug == (slug or "").lower()[:60])).mappings().first()
    if not biz or biz["suspended_at"]:
        raise NotFound("page")
    p = conn.execute(select(FP).where(FP.c.business_id == biz["id"], FP.c.slug == (page_slug or "").lower()[:60])).mappings().first()
    if not p:
        raise NotFound("page")
    fn = get(conn, biz["id"], p["funnel_id"])
    ps = pages(conn, biz["id"], fn["id"])
    i = next(n for n, x in enumerate(ps) if x["id"] == p["id"])
    return dict(biz), fn, dict(p), (ps[i + 1] if i + 1 < len(ps) else None)


def file_ok(conn: Connection, business_id: UUID, file_id: UUID) -> bool:
    """A picture may be shown publicly only while an active funnel's page uses it."""
    return bool(conn.execute(select(FP.c.id).join(FN, FN.c.id == FP.c.funnel_id).where(
        FP.c.business_id == business_id, FN.c.active, FP.c.blocks.contains([{"file": str(file_id)}])).limit(1)).first())


def visitor(ip: str, day: date) -> str:
    """The same daily hash the sandbox counter uses: people can be counted, never identified or followed."""
    return hashlib.sha256(f"{day.isoformat()}:{config.get('VO_SECRET_KEY', '')}:{ip}".encode()).hexdigest()[:16]


def hit(conn: Connection, biz: dict, page_id: UUID, ip: str, kind: str = "view", now: datetime | None = None) -> None:
    from zoneinfo import ZoneInfo
    day = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(biz["timezone"])).date()
    conn.execute(pg_insert(FH).values(business_id=biz["id"], page_id=page_id, day=day, visitor=visitor(ip, day), kind=kind)
                 .on_conflict_do_nothing(constraint="uq_funnel_hit"))


def stats(conn: Connection, business_id: UUID, funnel_id: UUID, since: date | None = None) -> list[dict]:
    """Per step, in order: views (visitors), leads (forms sent), went_on (visitors of this step seen on the next one the same
    day) and the share of the first step's visitors still here."""
    ps = pages(conn, business_id, funnel_id)
    out = []
    first = None
    for n, p in enumerate(ps):
        w = [FH.c.business_id == business_id, FH.c.page_id == p["id"]]
        if since:
            w.append(FH.c.day >= since)
        views = conn.execute(select(func.count()).where(*w, FH.c.kind == "view")).scalar()
        leads = conn.execute(select(func.count()).where(*w, FH.c.kind == "lead")).scalar()
        went = None
        if n + 1 < len(ps):
            nx = FH.alias("nx")
            went = conn.execute(select(func.count()).select_from(FH.join(nx, and_(
                nx.c.page_id == ps[n + 1]["id"], nx.c.day == FH.c.day, nx.c.visitor == FH.c.visitor, nx.c.kind == "view")))
                .where(*w, FH.c.kind == "view")).scalar()
        first = views if first is None else first
        out.append({"page": p, "views": views, "leads": leads, "went_on": went,
                    "kept_pct": round(100 * views / first) if first else None})
    return out


# ---------------------------------------------------------------- starter funnels (a few, universal)
TEMPLATES = ("leads", "book", "sell")
SUGGEST = {"salon": ("book", "sell", "leads"), "clinic": ("book", "leads", "sell"), "fitness": ("book", "sell", "leads"),
           "shop": ("sell", "leads", "book"), "florist": ("sell", "leads", "book"), "restaurant": ("book", "sell", "leads")}


def suggested(industry: str | None) -> tuple[str, ...]:
    """The starter funnels in the order that fits the trade (every trade gets all three)."""
    return SUGGEST.get(industry or "", ("leads", "book", "sell"))


def starter(conn: Connection, biz: dict, actor: str, template: str, name: str, lang: str) -> list[tuple[str, list]]:
    """A ready funnel in the owner's language: [(step title, blocks)]. The words are a start; the owner changes them."""
    if template not in TEMPLATES:
        raise Invalid("pick a starter", "template")
    w = lambda k: _t(f"fn_tpl_{k}", lang).replace("{business}", biz["name"])       # noqa: E731
    thanks = (w("thanks_title"), [{"t": "text", "title": w("thanks_title"), "text": w("thanks_text")}])
    faq = {"t": "faq", "items": [{"q": w("faq_q1"), "a": w("faq_a1")}, {"q": w("faq_q2"), "a": w("faq_a2")}]}
    proof = {"t": "testimonial", "quote": w("quote"), "name": w("quote_name")}
    if template == "leads":
        from . import forms as FM
        form = next((f for f in FM.list_(conn, biz["id"]) if f["active"]), None) or FM.save(conn, biz, actor, {
            "name": name, "fields": [{"key": "name", "required": True}, {"key": "phone"}, {"key": "email"}, {"key": "message"}],
            "thanks": w("thanks_text")})
        return [(name, [{"t": "hero", "title": w("leads_title"), "text": w("leads_text")},
                        {"t": "text", "text": w("leads_why")}, {"t": "form", "form": str(form["id"])}, proof, faq]), thanks]
    if template == "book":
        return [(name, [{"t": "hero", "title": w("book_title"), "text": w("book_text"), "button": w("book_button"), "to": "book"},
                        {"t": "offer", "title": w("book_offer"), "items": w("book_items").split("\n"), "button": w("book_button"),
                         "to": "book"}, proof, faq])]
    return [(name, [{"t": "hero", "title": w("sell_title"), "text": w("sell_text"), "button": w("sell_button"), "to": "next"},
                    {"t": "offer", "title": w("sell_offer"), "items": w("sell_items").split("\n")}, proof, faq,
                    {"t": "button", "button": w("sell_button"), "to": "next"}]),
            (w("sell_step2"), [{"t": "text", "title": w("sell_step2"), "text": w("sell_pay_text")}]), thanks]
