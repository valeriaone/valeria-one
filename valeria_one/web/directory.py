"""The directory on screen: the public pages (/directory, /directory/<slug>, its sitemap) and the owner's listing
settings (/app/directory). Public pages are server-rendered, cacheable and work without JavaScript."""
from __future__ import annotations

from xml.sax.saxutils import escape

from fastapi import APIRouter, Request
from fastapi.responses import Response

from .. import blocks, config, db, packs
from ..services import Invalid
from ..services import booking as BK
from ..services import directory as DR
from . import _flash, _form, _go, _guard, _lang, _limit, _not_shared_demo, _own, render

router = APIRouter(include_in_schema=False)
_contact_hits: dict = {}


def _public(resp):
    if config.SANDBOX():
        resp.headers["X-Robots-Tag"] = "noindex"
    else:
        resp.headers["Cache-Control"] = "public, max-age=300"
    return resp


@router.get("/directory")
@_guard
async def directory_home(request: Request, q: str = "", type: str = "", city: str = "", country: str = "", page: int = 1):
    lang = _lang(request)
    with db.tx() as conn:
        res = DR.search(conn, q=q, kind=type, city=city, country=country, page=page)
        fac = DR.facets(conn)
    base = f"{config.PUBLIC_URL()}/directory"
    canonical = base + (f"?page={res['page']}" if res["page"] > 1 else "")
    for r in res["rows"]:
        r["what"] = DR.category_of(r, lang)
    ld = {"@context": "https://schema.org", "@type": "ItemList", "itemListElement": [
        {"@type": "ListItem", "position": i + 1, "url": f"{base}/{r['slug']}", "name": r["name"]} for i, r in enumerate(res["rows"])]}
    kinds = [(k, packs.text(packs.industry(k).name, lang)) for k in fac["kinds"]]
    filtered = bool(q or type or city or country)
    return _public(render(request, "directory.html", None, res=res, fac=fac, kinds=kinds, ld=ld, canonical=canonical,
                          f={"q": q, "type": type, "city": city, "country": country}, filtered=filtered, page="directory"))


@router.get("/directory/sitemap.xml")
async def directory_sitemap():
    with db.tx() as conn:
        paths = DR.sitemap_paths(conn)
    base = config.PUBLIC_URL()
    body = ('<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            + "".join(f"<url><loc>{escape(base + p)}</loc></url>" for p in paths) + "</urlset>")
    return Response(body, media_type="application/xml")


@router.get("/directory/{slug}")
@_guard
async def directory_listing(request: Request, slug: str):
    with db.tx() as conn:
        biz = DR.public(conn, slug)
    lang = _lang(request, biz)
    ctx = {"biz": biz, "user": None, "actor": "directory", "role": None, "businesses": []}
    return _public(render(request, "directory_listing.html", ctx, ln=DR.links(biz), what=DR.category_of(biz, lang), bdays=BK.DAYS,
                          ld=DR.json_ld(biz, lang), sent=request.query_params.get("sent") == "1"))


@router.post("/directory/{slug}")
@_guard
async def directory_contact(request: Request, slug: str):
    """A visitor writes from the listing: a new customer (source: directory) and a conversation in the inbox."""
    f = await _form(request)
    if f.get("website"):                                     # honeypot: people never fill it, bots do
        return _go(f"/directory/{slug}?sent=1#contact")
    if not _limit(_contact_hits, request, 10):
        raise Invalid("too many messages from this connection; try later", "rate")
    with db.tx() as conn:
        biz = DR.public(conn, slug)
        DR.contact(conn, biz, name=f.get("name", ""), reach=f.get("reach", ""), message=f.get("message", ""))
    return _go(f"/directory/{slug}?sent=1#contact")


# ---------------------------------------------------------------- the owner's listing
@router.get("/app/directory")
@_guard
async def directory_settings(request: Request):
    ctx = _own(request)
    with db.tx() as conn:
        s = DR.settings(conn, ctx["biz"]["id"])
    lang = _lang(request, ctx["biz"])
    url = f"{config.PUBLIC_URL()}/directory/{ctx['biz']['slug']}" if ctx["biz"].get("slug") else None
    return render(request, "directory_settings.html", ctx, s=s, url=url, default_what=packs.text(packs.industry(ctx["biz"]["industry"]).name, lang),
                  site_on=ctx["biz"]["site_enabled"] and blocks.on(ctx["biz"], "website"), page="directory")


@router.post("/app/directory")
@_guard
async def directory_save(request: Request):
    from ..services import businesses as B
    ctx = _own(request)
    _not_shared_demo(request, ctx)
    f = await _form(request)
    with db.tx() as conn:
        biz = B.get(conn, ctx["biz"]["id"])
        DR.save(conn, biz, ctx["actor"], listed=f.get("listed") == "1", **{k: f.get(k, "") for k in DR.TEXTS},
                **{k: f.get(k) == "1" for k in DR.SHOWS})
    _flash(request, "saved")
    return _go("/app/directory")


