"""lead27 (v0.27) on screen: Leads (/app/leads) — Today (the next best thing per lead, one tap), every lead with its source,
the sources (short link, chat links, QR, the website snippet, the forwarding address), source -> result, and how Valeria
works them (Suggest / Autopilot, the plan, quiet, voice). One lead: /app/leads/<id> — write (Valeria's draft, cards, the
owner's own voice or hers), send from the business's own account or open the owner's own app with it filled in.

Public, cookieless where it runs on other people's websites: /l/<code> (the short link), /l/<code>/hi (a small page for
a source with nowhere else to go), /l/<code>/embed.js + POST /l/<code>/lead (the snippet: CORS *, honeypot, time check,
rate limit), /lv/<token> (a voice note the customer's app fetches), POST /lmail (the forwarding address, signed)."""
from __future__ import annotations

import json
import secrets

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import select

from .. import config, crypto, db, packs, qr
from ..services import Invalid, NotFound
from ..services import leads as LD
from ..services import leadvoice as LV
from . import _ctx, _flash, _form, _go, _guard, _lang, _limit, _own, render
from .i18n import UI_LANGS, has, t

router = APIRouter(include_in_schema=False)
TABS = ("today", "all", "sources", "results", "settings")
_lead_hits: dict = {}
_src_hits: dict = {}
CORS = {"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Max-Age": "86400"}
WIDGET_KEYS = ("fm_name", "fm_phone", "fm_email", "fm_message", "fm_send", "fm_consent_request", "fm_consent_marketing",
               "privacy_notice", "fm_thanks_default", "fm_need_consent", "optional", "ld_w_title", "ld_w_contact", "ld_w_or_chat",
               "ld_w_error", "ld_w_busy", "as_wa", "ld_w_messenger")


def _view(conn, biz: dict, items: list[dict]) -> list[dict]:
    """Each suggestion with its plain draft (her words, in their language) and whether the business has an account to send it."""
    st = LD.settings(conn, biz["id"])
    C = db.contacts
    out = []
    for s in items:
        contact = dict(conn.execute(select(C).where(C.c.id == s["contact_id"])).mappings().one())
        lang = LD.lang_for(conn, biz, contact)
        text = LD.template_text(conn, biz, contact, s["do"], LD.step_key(st, s["step"]), lang)
        aid, ch = LD._account(conn, biz, contact["id"])
        blk = f"ld_blk_{s.get('blocked')}"
        out.append({**s, "text": text, "channel": ch, "links": LD.deeplinks(contact, text, biz["name"]),
                    "blk": blk if has(blk) else "ld_blk_other"})
    return out


@router.get("/app/leads")
@_guard
async def leads_page(request: Request, tab: str = "today", status: str | None = None, src: str | None = None, days: int = 90,
                     q: str | None = None):
    ctx = _ctx(request)
    biz = ctx["biz"]
    lang = _lang(request, biz)
    tab = tab if tab in TABS else "today"
    if tab in ("sources", "settings") and ctx["role"] != "owner":
        tab = "today"
    days = days if days in (7, 30, 90, 365, 0) else 90
    data: dict = {}
    with db.tx() as conn:
        st = LD.settings(conn, biz["id"])
        data["counts"] = {"week": LD.counts(conn, biz)["week"]}
        if tab == "today":
            data["sugg"] = _view(conn, biz, LD.suggestions(conn, biz, limit=40))
        elif tab == "all":
            data["rows"] = LD.list_(conn, biz["id"], status=status if status in ("open", *LD.OPEN, "booked", "won", "lost", "quiet",
                                                                                    "stopped") else None,
                                    source_id=_uuid(src), q=q, limit=300)
            data["all_sources"] = LD.sources(conn, biz["id"])
        elif tab == "sources":
            data["sources"] = [{**s, "link": LD.short_link(s), "chat": LD.chat_links(biz, st, s, lang), "embed": LD.embed(s),
                                "catch": LD.embed_catch(s), "mail": LD.mail_address(s)} for s in LD.sources(conn, biz["id"])]
        elif tab == "results":
            data["report"] = LD.report(conn, biz, days or None, lang)
        else:
            data["preview"] = request.session.pop("ld_preview", None)
    return render(request, "leads.html", ctx, page="leads", tab=tab, st=st, status=status, src=src, days=days, q=q or "",
                  kinds=LD.KINDS, mail_on=bool(LD.mail_domain()), label=lambda r: LD.label(r, lang),
                  chat_number=LD.chat_number(biz, st), **data)


def _uuid(v):
    from uuid import UUID
    try:
        return UUID(str(v)) if v else None
    except ValueError:
        return None


@router.get("/app/leads/{lid}")
@_guard
async def lead_page(request: Request, lid: str):
    from ..services import ai as AI
    ctx = _ctx(request)
    biz = ctx["biz"]
    lang = _lang(request, biz)
    with db.tx() as conn:
        lead = LD.get(conn, biz["id"], lid)
        C = db.contacts
        contact = dict(conn.execute(select(C).where(C.c.id == lead["contact_id"])).mappings().one())
        st = LD.settings(conn, biz["id"])
        their = LD.lang_for(conn, biz, contact)
        sugg = next((s for s in LD.suggestions(conn, biz) if s["id"] == lead["id"]), None)
        do = sugg["do"] if sugg else "followup"
        plain = LD.template_text(conn, biz, contact, do, LD.step_key(st, lead["step"]), their)
        cards = LD.cards(conn, biz, their)
        aid, ch = LD._account(conn, biz, contact["id"])
        conv = conn.execute(select(db.conversations.c.id).where(db.conversations.c.business_id == biz["id"],
                                                                db.conversations.c.contact_id == contact["id"])
                            .order_by(db.conversations.c.last_message_at.desc().nulls_last()).limit(1)).scalar()
        creds = AI.own_key(conn, biz)
    d = request.session.pop("ld_draft", None)
    text = d["text"] if d and d.get("lead") == str(lead["id"]) else plain
    by_ai = bool(d and d.get("lead") == str(lead["id"]) and d.get("by_ai"))
    links = request.session.pop("ld_links", None)
    links = links if links and links.get("lead") == str(lead["id"]) else None
    if links:
        links["blk"] = f"ld_blk_{links.get('why')}" if has(f"ld_blk_{links.get('why')}") else "ld_blk_other"
    return render(request, "lead.html", ctx, page="leads", lead=lead, contact=contact, st=st, do=do, sugg=sugg, text=text,
                  by_ai=by_ai, cards=cards, channel=ch, conv=conv, links=links, deep=LD.deeplinks(contact, text, biz["name"]),
                  can_listen=bool(creds and creds[0] in ("gemini", "openai")), can_speak=bool(creds and creds[0] in LV.STOCK),
                  src_label=LD.label(lead, lang), nurture=LD.is_nurture(st, lead["step"]),
                  day=(st["days"][lead["step"]] if not LD.is_nurture(st, lead["step"]) else None))


@router.post("/app/leads/{lid}/draft")
@_guard
async def lead_draft(request: Request, lid: str):
    ctx = _ctx(request)
    f = await _form(request)
    got = LD.draft(ctx["biz"], lid, f.get("do") or "followup")
    request.session["ld_draft"] = {"lead": lid, "text": got["text"], "by_ai": got["by_ai"]}
    if not got["by_ai"]:
        _flash(request, "ld_draft_plain", "warn")
    return _go(f"/app/leads/{lid}#send")


@router.post("/app/leads/{lid}/send")
@_guard
async def lead_send(request: Request, lid: str):
    ctx = _ctx(request)
    f = await _form(request)
    do = f.get("do") if f.get("do") in ("answer", "followup", "checkin") else "followup"
    res = LD.send(ctx["biz"], ctx["actor"], lid, body=f.get("body") or "", do=do, voice_id=f.get("voice_id") or None)
    back = _back(f)
    if res["status"] == "sent":
        _flash(request, "ld_sent")
        return _go(back or f"/app/leads/{lid}")
    if res["status"] == "failed":
        _flash(request, "ld_failed", "warn")
        return _go(f"/app/leads/{lid}")
    request.session["ld_links"] = {"lead": lid, "why": res["why"], "links": res["links"]}
    request.session["ld_draft"] = {"lead": lid, "text": (f.get("body") or "")[:1600], "by_ai": False}
    return _go(f"/app/leads/{lid}#send")


@router.post("/app/leads/{lid}/open")
@_guard
async def lead_open(request: Request, lid: str):
    """"Open in my WhatsApp / SMS / email": on record as sent by the owner, then their own app with the message in it."""
    ctx = _ctx(request)
    f = await _form(request)
    via = f.get("via") if f.get("via") in ("whatsapp", "sms", "email") else None
    if not via:
        raise Invalid("pick an app", "via")
    with db.tx() as conn:
        lead = LD.get(conn, ctx["biz"]["id"], lid)
        contact = dict(conn.execute(select(db.contacts).where(db.contacts.c.id == lead["contact_id"])).mappings().one())
        link = next((x["url"] for x in LD.deeplinks(contact, (f.get("body") or "")[:1600], ctx["biz"]["name"]) if x["kind"] == via), None)
        if not link:
            raise Invalid("no address for that app", "via")
        LD.opened_app(conn, ctx["biz"], ctx["actor"], lid, f.get("do") or "followup", via)
    return RedirectResponse(link, status_code=303)


@router.post("/app/leads/{lid}/act")
@_guard
async def lead_act(request: Request, lid: str):
    ctx = _ctx(request)
    f = await _form(request)
    with db.tx() as conn:
        LD.owner_action(conn, ctx["biz"], ctx["actor"], lid, str(f.get("act") or ""))
    _flash(request, "saved")
    back = _back(f)
    return _go(back or f"/app/leads/{lid}")


def _back(f: dict) -> str | None:
    """Only back into this app (never another site): /app, /app#…, /app/…"""
    b = str(f.get("back") or "")
    return b if (b == "/app" or b.startswith(("/app/", "/app#"))) and "//" not in b and "\\" not in b else None


def _json(data, status=200):
    return JSONResponse(data, status_code=status)


@router.post("/app/leads/{lid}/shared")
@_guard
async def lead_shared(request: Request, lid: str):
    """The owner's phone shared the voice note into their own WhatsApp (or it downloaded and the chat opened): on record."""
    ctx = _ctx(request)
    f = await request.form()
    if not secrets.compare_digest(str(f.get("csrf", "")), str(request.session.get("csrf", "x"))):
        return _json({"error": "expired"}, 403)
    how = "share" if f.get("how") == "share" else "download"
    do = f.get("do") if f.get("do") in ("answer", "followup", "checkin") else "followup"
    with db.tx() as conn:
        LD.opened_app(conn, ctx["biz"], ctx["actor"], lid, do, f"whatsapp_voice_{how}"[:20], voice=True)
    return _json({"ok": True})


@router.post("/app/leads/{lid}/voice")
@_guard
async def lead_voice(request: Request, lid: str):
    """The owner's recording (the page's microphone button): kept, written down with their own AI when it can."""
    ctx = _ctx(request)
    try:
        n = int(request.headers.get("content-length") or 0)
    except ValueError:
        n = 0
    if n > LV.MAX_BYTES + 64 * 1024:
        return _json({"error": t("ld_voice_big", _lang(request, ctx["biz"]))}, 413)
    f = await request.form()
    if not secrets.compare_digest(str(f.get("csrf", "")), str(request.session.get("csrf", "x"))):
        return _json({"error": "expired"}, 403)
    up = f.get("audio")
    if not hasattr(up, "read"):
        return _json({"error": "no audio"}, 400)
    raw = await up.read()
    try:
        got = LV.record(ctx["biz"], ctx["actor"], lid, raw, up.content_type or "")
    except Invalid as e:
        return _json({"error": str(e)}, 400)
    return _json(got)


@router.post("/app/leads/{lid}/valeria-voice")
@_guard
async def lead_valeria_voice(request: Request, lid: str):
    """Valeria says the message in her own voice (a stock voice of the business's own AI account; never a copy)."""
    from ..services import ai as AI
    ctx = _ctx(request)
    f = await request.form()
    if not secrets.compare_digest(str(f.get("csrf", "")), str(request.session.get("csrf", "x"))):
        return _json({"error": "expired"}, 403)
    biz = ctx["biz"]
    with db.tx() as conn:
        lead = LD.get(conn, biz["id"], lid)
        contact = dict(conn.execute(select(db.contacts).where(db.contacts.c.id == lead["contact_id"])).mappings().one())
        lang = LD.lang_for(conn, biz, contact)
    try:
        got = LV.valeria_note(biz, contact, str(f.get("text") or ""), lang)
    except AI.AIError as e:
        return _json({"error": str(e)}, 400)
    return _json(got)


# ---------------------------------------------------------------- sources and settings (owner)
@router.post("/app/leads/sources")
@_guard
async def source_new(request: Request):
    ctx = _own(request)
    f = await _form(request)
    with db.tx() as conn:
        s = LD.save_source(conn, ctx["biz"], ctx["actor"], {"kind": f.get("kind"), "name": f.get("name"), "dest": f.get("dest")})
    _flash(request, "ld_source_made")
    return _go(f"/app/leads?tab=sources#s-{s['id']}")


@router.post("/app/leads/sources/{sid}")
@_guard
async def source_save(request: Request, sid: str):
    ctx = _own(request)
    f = await _form(request)
    with db.tx() as conn:
        if f.get("act") in ("on", "off"):
            LD.set_active(conn, ctx["biz"], ctx["actor"], sid, f["act"] == "on")
        else:
            cur = LD.source(conn, ctx["biz"]["id"], sid)
            LD.save_source(conn, ctx["biz"], ctx["actor"], {"kind": f.get("kind") or cur["kind"], "name": f.get("name") or cur["name"],
                                                            "dest": f.get("dest", cur["dest"])}, sid)
    _flash(request, "saved")
    return _go(f"/app/leads?tab=sources#s-{sid}")


@router.get("/app/leads/sources/{sid}/qr.svg")
@_guard
async def source_qr(request: Request, sid: str):
    ctx = _ctx(request)
    with db.tx() as conn:
        s = LD.source(conn, ctx["biz"]["id"], sid)
    svg = qr.svg(LD.short_link(s), size=600)
    return Response(svg, media_type="image/svg+xml", headers={
        "Content-Disposition": f'attachment; filename="qr-{s["code"]}.svg"' if request.query_params.get("download") else "inline",
        "Cache-Control": "private, max-age=300"})


@router.post("/app/leads/settings")
@_guard
async def leads_settings(request: Request):
    ctx = _own(request)
    f = await _form(request)
    vals = {k: f.get(k) for k in ("mode", "days", "nurture_days", "daily_cap", "template", "chat_number", "messenger", "greeting")
            if k in f}
    for k in ("followup", "nurture", "voice", "paused"):
        vals[k] = bool(f.get(k))
    with db.tx() as conn:
        LD.save_settings(conn, ctx["biz"], ctx["actor"], **vals)
    _flash(request, "saved")
    return _go("/app/leads?tab=settings")


@router.post("/app/leads/preview")
@_guard
async def leads_preview(request: Request):
    """"What would Valeria do now?" — the job, as a dry run for this business: nothing is written, nothing is sent."""
    ctx = _own(request)
    await _form(request)
    got = LD.round_(dry_run=True, business_id=ctx["biz"]["id"])
    request.session["ld_preview"] = [{k: p.get(k) for k in ("lead_id", "do", "why", "channel")} for p in got.get("plan", [])][:50]
    return _go("/app/leads?tab=settings#preview")


# ---------------------------------------------------------------- public: the short link, its page, the snippet
@router.get("/l/{code}")
async def short_link(request: Request, code: str):
    try:
        s, biz, where = LD.open_link(code)
    except (NotFound, Invalid):
        return HTMLResponse("Not found", status_code=404)
    request.session["src"] = LD.remember(s)
    return RedirectResponse(where, status_code=302)


@router.get("/l/{code}/hi")
@_guard
async def source_page(request: Request, code: str):
    """A source with nowhere else to go (no website, no booking page): the business's name, its chat buttons, the form."""
    with db.tx() as conn:
        s, biz = LD.by_code(conn, code)
    ctx = {"biz": biz, "user": None, "actor": "website", "role": None, "businesses": []}
    resp = render(request, "lead_landing.html", ctx, src=s, site=biz.get("site") or {}, script=f"{LD.short_link(s)}/embed.js")
    resp.headers["X-Robots-Tag"] = "noindex"
    return resp


@router.get("/l/{code}/embed.js")
async def embed_js(code: str):
    try:
        with db.tx() as conn:
            s, biz = LD.by_code(conn, code)
            st = LD.settings(conn, biz["id"])
    except NotFound:
        return Response("/* not found */", status_code=404, media_type="application/javascript")
    site = biz.get("site") or {}
    law = packs.country(biz["country"]).privacy_law
    cfg = {"api": f"{LD.short_link(s)}/lead", "name": biz["name"], "color": site.get("color") or "#4d5bff", "lang": biz["language"],
           "privacy": f"{config.PUBLIC_URL()}/privacy/{biz['slug']}" if biz.get("slug") else None, "law": law.code,
           "chat": LD.chat_links(biz, st, s),
           "text": {lg: {k: t(k, lg) for k in WIDGET_KEYS} for lg in UI_LANGS},
           "chat_lang": {lg: LD.chat_links(biz, st, s, lg) for lg in UI_LANGS}}
    js = _WIDGET.replace("__CFG__", json.dumps(cfg, ensure_ascii=False).replace("</", "<\\/"))
    return Response(js, media_type="application/javascript; charset=utf-8",
                    headers={"Cache-Control": "public, max-age=300", "Access-Control-Allow-Origin": "*",
                             "X-Content-Type-Options": "nosniff"})


@router.options("/l/{code}/lead")
async def lead_preflight(code: str):
    return Response(status_code=204, headers=CORS)


@router.post("/l/{code}/lead")
async def lead_in(request: Request, code: str):
    if not _limit(_lead_hits, request, 6):
        return JSONResponse({"error": "busy"}, status_code=429, headers=CORS)
    try:
        raw = await request.body()
        if len(raw) > 20_000:
            raise ValueError
        d = json.loads(raw or b"{}")
        if not isinstance(d, dict):
            raise ValueError
    except ValueError:
        return JSONResponse({"error": "bad"}, status_code=400, headers=CORS)
    if str(d.get("hp") or "").strip():                       # the honeypot: a robot filled the field no person sees
        return JSONResponse({"ok": True}, headers=CORS)
    try:
        if int(d.get("ms") or 0) < 2500:                     # sent faster than a person types: not kept
            return JSONResponse({"ok": True}, headers=CORS)
    except (TypeError, ValueError):
        return JSONResponse({"ok": True}, headers=CORS)
    if not _limit(_src_hits, _Key(code), 60):
        return JSONResponse({"error": "busy"}, status_code=429, headers=CORS)
    try:
        LD.widget_in(code, d, origin=request.headers.get("origin") or "")
    except NotFound:
        return JSONResponse({"error": "not_found"}, status_code=404, headers=CORS)
    except Invalid as e:
        return JSONResponse({"error": str(e), "field": e.field}, status_code=400, headers=CORS)
    return JSONResponse({"ok": True}, headers=CORS)


class _Key:                                                   # one source's own limit, whoever sends (a flood from many places)
    def __init__(self, code: str):
        self.client = type("c", (), {"host": "src:" + (code or "")[:12]})()


@router.get("/lv/{token}")
async def voice_file(token: str):
    try:
        raw, mime = LV.serve(token)
    except NotFound:
        return Response("Not found", status_code=404)
    return Response(raw, media_type=mime, headers={"Cache-Control": "private, max-age=86400", "X-Content-Type-Options": "nosniff",
                                                   "Content-Disposition": "inline"})


@router.post("/lmail")
async def lead_mail(request: Request):
    """The forwarding address: a mail service (the server operator's own) posts each email as JSON, signed with
    VO_LEADS_MAIL_SECRET: {"to", "from", "reply_to", "name", "subject", "text", "message_id"}."""
    secret = config.get("VO_LEADS_MAIL_SECRET")
    if not LD.mail_domain() or not secret:
        return JSONResponse({"error": "off"}, status_code=404)
    raw = await request.body()
    if len(raw) > 512 * 1024 or not crypto.same(request.headers.get("x-vo-signature", ""), crypto.hmac_sha256_hex(secret, raw)):
        return JSONResponse({"error": "bad signature"}, status_code=401)
    try:
        d = json.loads(raw or b"{}")
    except ValueError:
        return JSONResponse({"error": "bad"}, status_code=400)
    try:
        got = LD.mail_in(d if isinstance(d, dict) else {})
    except Invalid as e:
        return JSONResponse({"ok": False, "why": str(e)}, status_code=200)
    return JSONResponse(got)


_WIDGET = r"""/* Valeria One · lead form for any website */
(function(){
  var C=__CFG__, d=document;
  if(window['__vo_lead_'+C.api]) return; window['__vo_lead_'+C.api]=1;
  var L=(navigator.language||C.lang||'en').toLowerCase().replace('_','-').split('-')[0]; if(L==='tl') L='fil';
  var T=C.text[L]||C.text[(C.lang||'en').split('-')[0]]||C.text.en, CH=C.chat_lang[L]||C.chat;
  function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});}
  function f(k,b){return (T[k]||'').replace('{business}',b||C.name);}
  var cur=d.currentScript, host=d.createElement('div'); host.setAttribute('data-vo-lead','');
  var ASK=((cur&&cur.getAttribute('data-ask'))||'').toLowerCase().split(',').map(function(x){return x.trim();}).filter(Boolean);  // catch01
  var TITLE=(cur&&cur.getAttribute('data-title'))||'';
  function has(k){return ASK.indexOf(k)>=0;}
  if(cur&&cur.parentNode){cur.parentNode.insertBefore(host,cur);} else {d.body.appendChild(host);}
  var root=host.attachShadow?host.attachShadow({mode:'open'}):host, t0=Date.now();
  var css='*{box-sizing:border-box;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif}'+
    '.b{max-width:520px;margin:12px auto;padding:16px;border:1px solid #ddd;border-radius:14px;background:#fff;color:#111}'+
    'h3{margin:0 0 10px;font-size:18px}label{display:block;font-size:14px;margin:10px 0 4px}'+
    'input,textarea{width:100%;padding:10px;border:1px solid #ccc;border-radius:10px;font-size:16px;background:#fff;color:#111}'+
    'textarea{min-height:84px}.k{display:flex;gap:8px;align-items:flex-start;font-size:13px;margin:10px 0 0}.k input{width:auto;margin-top:2px}'+
    'button,.c{display:inline-block;border:0;border-radius:10px;padding:11px 16px;font-size:16px;cursor:pointer;text-decoration:none;text-align:center}'+
    'button{width:100%;margin-top:14px;background:'+C.color+';color:#fff}.c{background:#f2f2f2;color:#111;margin:4px 6px 0 0}'+
    '.m{font-size:12px;color:#666;margin-top:10px}.m a{color:#666}.e{color:#b00020;font-size:14px;margin-top:8px}.h{position:absolute;left:-5000px}'+
    '.r{display:flex;flex-wrap:wrap;margin-top:8px}';
  var chat='';
  if(CH&&CH.whatsapp) chat+='<a class="c" target="_blank" rel="noopener" href="'+esc(CH.whatsapp)+'">'+esc(T.as_wa)+'</a>';
  if(CH&&CH.messenger) chat+='<a class="c" target="_blank" rel="noopener" href="'+esc(CH.messenger)+'">'+esc(T.ld_w_messenger)+'</a>';
  root.innerHTML='<style>'+css+'</style><form class="b" novalidate>'+
    '<h3 dir="auto">'+esc(TITLE||f('ld_w_title'))+'</h3>'+
    (chat?'<div class="m">'+esc(T.ld_w_or_chat)+'</div><div class="r">'+chat+'</div>':'')+
    '<label>'+esc(T.fm_name)+' *</label><input name="name" maxlength="200" autocomplete="name" required>'+
    (ASK.length?
      (has('whatsapp')?'<label>WhatsApp *</label><input name="whatsapp" type="tel" maxlength="40" autocomplete="tel" inputmode="tel" required>':'')+
      (has('email')?'<label>'+esc(T.fm_email)+' *</label><input name="email" type="email" maxlength="200" autocomplete="email" inputmode="email" required>':'')+
      (has('instagram')?'<label>Instagram ('+esc(T.optional)+')</label><input name="instagram" maxlength="120" placeholder="@">':'')+
      (has('tiktok')?'<label>TikTok ('+esc(T.optional)+')</label><input name="tiktok" maxlength="120" placeholder="@">':'')+
      (has('facebook')?'<label>Facebook ('+esc(T.optional)+')</label><input name="facebook" maxlength="120">':'')
     :'<label>'+esc(T.ld_w_contact)+' *</label><input name="contact" maxlength="200" autocomplete="tel" required>')+
    '<label>'+esc(T.fm_message)+'</label><textarea name="message" maxlength="1800" dir="auto"></textarea>'+
    '<input class="h" name="hp" tabindex="-1" autocomplete="off" aria-hidden="true">'+
    '<label class="k"><input type="checkbox" name="consent" value="1" required> <span>'+esc(f('fm_consent_request'))+' *</span></label>'+
    '<label class="k"><input type="checkbox" name="offers" value="1"> <span>'+esc(f('fm_consent_marketing'))+' ('+esc(T.optional)+')</span></label>'+
    '<div class="e" role="alert" hidden></div><button type="submit">'+esc(T.fm_send)+'</button>'+
    '<div class="m">'+(C.privacy?'<a target="_blank" rel="noopener" href="'+esc(C.privacy)+'">'+esc(T.privacy_notice)+'</a> · ':'')+esc(C.law)+' · '+esc(C.name)+'</div></form>';
  var form=root.querySelector('form'), err=root.querySelector('.e');
  form.addEventListener('submit',function(ev){
    ev.preventDefault(); err.hidden=true;
    var g=function(n){var el=form.querySelector('[name='+n+']'); return !el?'':(el.type==='checkbox'?el.checked:el.value.trim());};
    var need=ASK.length?ASK.filter(function(k){return k==='whatsapp'||k==='email';}):['contact'];
    if(!g('name')||need.some(function(k){return !g(k);})){err.textContent=T.ld_w_error; err.hidden=false; return;}
    if(has('email')&&g('email').indexOf('@')<1){err.textContent=T.ld_w_error; err.hidden=false; return;}
    if(!g('consent')){err.textContent=T.fm_need_consent; err.hidden=false; return;}
    var btn=form.querySelector('button'); btn.disabled=true;
    var x=new XMLHttpRequest(); x.open('POST',C.api); x.setRequestHeader('Content-Type','application/json');
    x.onload=function(){
      var ok=false, j={}; try{j=JSON.parse(x.responseText||'{}'); ok=x.status<300&&j.ok;}catch(e){}
      if(ok){form.innerHTML='<h3 dir="auto">'+esc(T.fm_thanks_default)+'</h3>'+(chat?'<div class="r">'+chat+'</div>':'');}
      else {btn.disabled=false; err.textContent=x.status===429?T.ld_w_busy:(j.error&&x.status===400?j.error:T.ld_w_error); err.hidden=false;}
    };
    x.onerror=function(){btn.disabled=false; err.textContent=T.ld_w_error; err.hidden=false;};
    x.send(JSON.stringify({name:g('name'),contact:g('contact'),whatsapp:g('whatsapp'),email:g('email'),instagram:g('instagram'),
                           tiktok:g('tiktok'),facebook:g('facebook'),message:g('message'),consent:g('consent'),offers:g('offers'),hp:g('hp'),
                           ms:Date.now()-t0,page:String(location.href).slice(0,300)}));
  });
})();
"""
