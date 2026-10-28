"""The website assistant on screen: one script tag on any website, a chat bubble in the corner.

  <script src="https://<server>/w/<slug>/assistant.js" defer></script>

Public and cookieless: no session, no CSRF (nothing here changes the business's data except a new lead), open to
any origin (CORS *), rate-limited per visitor. The answers come from services.assistant.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from .. import blocks, config, db
from ..services import Invalid, NotFound
from ..services import assistant as AS
from ..services import site as SI
from .i18n import UI_LANGS, t
from . import _limit

router = APIRouter(include_in_schema=False)
_ask_hits: dict = {}
_lead_hits: dict = {}
CORS = {"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Max-Age": "86400"}
KEYS = ("as_title", "as_hello", "as_ph", "as_send", "as_book", "as_work", "as_preview", "as_wa", "as_leave", "as_name",
        "as_phone", "as_leave_go", "as_thanks", "as_offline", "as_close", "as_busy", "as_powered")


def _biz(slug: str) -> dict:
    with db.tx() as conn:
        biz = SI.public(conn, slug)
    if not (biz.get("site") or {}).get("assistant"):
        raise NotFound("assistant")
    return biz


def _json(data, status=200):
    return JSONResponse(data, status_code=status, headers=CORS)


@router.get("/w/{slug}/assistant.js")
async def assistant_js(slug: str):
    try:
        biz = _biz(slug)
    except NotFound:
        return Response("/* not found */", status_code=404, media_type="application/javascript")
    s = biz.get("site") or {}
    base = config.PUBLIC_URL()
    cfg = {"api": f"{base}/w/{biz['slug']}", "name": biz["name"], "color": s.get("color") or "#4d5bff",
           "lang": biz["language"], "book": f"{base}/b/{biz['slug']}" if biz["booking_enabled"] and blocks.on(biz, "calendar") else None,
           "work": f"{base}/s/{biz['slug']}#work" if blocks.on(biz, "projects") else None,
           "preview": f"{base}/s/{biz['slug']}#preview" if s.get("preview") and blocks.on(biz, "studio") else None,
           "wa": f"https://wa.me/{s['whatsapp'].lstrip('+')}" if s.get("whatsapp") else None,
           "photo": f"{base}/s/{biz['slug']}/f/{s['assistant_photo']}?t=1" if s.get("assistant_photo") else None,
           "text": {lang: {k: t(k, lang) for k in KEYS} for lang in UI_LANGS}}
    js = _WIDGET.replace("__CFG__", json.dumps(cfg, ensure_ascii=False).replace("</", "<\\/"))
    return Response(js, media_type="application/javascript; charset=utf-8",
                    headers={"Cache-Control": "public, max-age=300", "Access-Control-Allow-Origin": "*"})


@router.options("/w/{slug}/ask")
@router.options("/w/{slug}/lead")
async def assistant_preflight(slug: str):
    return Response(status_code=204, headers=CORS)


async def _body(request: Request) -> dict:
    try:
        raw = await request.body()
        if len(raw) > 20_000:
            raise ValueError
        d = json.loads(raw or b"{}")
        return d if isinstance(d, dict) else {}
    except ValueError:
        return {}


@router.post("/w/{slug}/ask")
async def assistant_ask(request: Request, slug: str):
    try:
        biz = _biz(slug)
    except NotFound:
        return _json({"error": "not_found"}, 404)
    if not _limit(_ask_hits, request, 40):
        return _json({"text": None, "reason": "busy"})
    d = await _body(request)
    try:
        text = AS.ask(biz, d.get("turns") or [])
    except Invalid as e:
        return _json({"error": str(e)}, 400)
    return _json({"text": text, "reason": None if text else "offline"})


@router.post("/w/{slug}/lead")
async def assistant_lead(request: Request, slug: str):
    try:
        biz = _biz(slug)
    except NotFound:
        return _json({"error": "not_found"}, 404)
    if not _limit(_lead_hits, request, 6):
        return _json({"error": "busy"}, 429)
    d = await _body(request)
    msg = str(d.get("message") or "").strip()[:1800] or "—"
    contact = str(d.get("contact") or "").strip()[:200]
    phone, email = ("", contact) if "@" in contact else (contact, "")
    try:
        with db.tx() as conn:
            SI.contact_form(conn, biz, name=str(d.get("name") or "").strip()[:200], phone=phone, email=email,
                            message="[chat] " + msg)
    except Invalid as e:
        return _json({"error": str(e)}, 400)
    return _json({"ok": True})


_WIDGET = r"""/* Valeria One · website assistant */
(function(){
  if(window.__voAssistant) return; window.__voAssistant = 1;
  var C = __CFG__;
  var L = (navigator.language || C.lang || 'en').toLowerCase(), T = C.text[L] || C.text[L.split('-')[0]] || C.text[C.lang] || C.text.en;
  Object.keys(T).forEach(function(k){ T[k] = T[k].split('{business}').join(C.name); });
  var turns = [], open = false;
  try { turns = JSON.parse(sessionStorage.getItem('vo-as-' + C.api) || '[]'); } catch(e) {}
  function save(){ try { sessionStorage.setItem('vo-as-' + C.api, JSON.stringify(turns.slice(-20))); } catch(e) {} }
  var css = '#vo-as{position:fixed;right:18px;bottom:18px;z-index:2147483000;font:15px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;color:#1d1b19}'
    + '#vo-as .b{display:flex;align-items:center;gap:10px;border:0;cursor:pointer;background:' + C.color + ';color:#fff;padding:12px 18px 12px 12px;border-radius:999px;box-shadow:0 8px 28px rgba(0,0,0,.28);font:inherit;font-weight:600}'
    + '#vo-as .b i{width:34px;height:34px;border-radius:50%;background:rgba(255,255,255,.16);display:flex;align-items:center;justify-content:center;font-style:normal;font-family:Georgia,serif;font-size:17px}'
    + '#vo-as .p{position:absolute;right:0;bottom:64px;width:min(370px,calc(100vw - 28px));height:min(560px,calc(100vh - 110px));background:#fff;border-radius:18px;box-shadow:0 18px 60px rgba(0,0,0,.3);display:none;flex-direction:column;overflow:hidden}'
    + '#vo-as.on .p{display:flex} #vo-as .h{background:' + C.color + ';color:#fff;padding:14px 16px;display:flex;justify-content:space-between;align-items:center}'
    + '#vo-as .b i img,#vo-as .hp{width:34px;height:34px;border-radius:50%;object-fit:cover;display:block} #vo-as .b i{overflow:hidden;padding:0}'
    + '#vo-as .hp{width:38px;height:38px;margin-inline-end:10px;border:2px solid rgba(255,255,255,.5)} #vo-as .h>div{flex:1}'
    + '#vo-as .h b{font-size:15px} #vo-as .h small{display:block;opacity:.75;font-weight:400} #vo-as .x{background:none;border:0;color:#fff;font-size:22px;cursor:pointer;line-height:1}'
    + '#vo-as .m{flex:1;overflow:auto;padding:14px;display:flex;flex-direction:column;gap:8px;background:#f7f5f2}'
    + '#vo-as .q,#vo-as .a{max-width:85%;padding:9px 12px;border-radius:14px;white-space:pre-wrap;overflow-wrap:anywhere}'
    + '#vo-as .q{align-self:flex-end;background:' + C.color + ';color:#fff} #vo-as .a{align-self:flex-start;background:#fff;border:1px solid #e7e2dc}'
    + '#vo-as .a a{color:inherit;text-decoration:underline} #vo-as .c{display:flex;flex-wrap:wrap;gap:6px;padding:8px 12px;border-top:1px solid #eee;background:#fff}'
    + '#vo-as .c a,#vo-as .c button{font:inherit;font-size:13px;border:1px solid #ddd6ce;background:#fff;color:#1d1b19;border-radius:999px;padding:5px 11px;cursor:pointer;text-decoration:none}'
    + '#vo-as form{display:flex;gap:6px;padding:10px 12px;border-top:1px solid #eee;background:#fff} #vo-as input{flex:1;min-width:0;font:inherit;border:1px solid #ddd6ce;border-radius:10px;padding:9px 11px;color:#1d1b19;background:#fff}'
    + '#vo-as form button{font:inherit;font-weight:600;border:0;border-radius:10px;padding:0 14px;background:' + C.color + ';color:#fff;cursor:pointer}'
    + '#vo-as .f{font-size:11px;color:#8a837b;text-align:center;padding:0 0 8px;background:#fff} #vo-as .lead{display:flex;flex-direction:column;gap:6px;padding:10px;background:#fff;border:1px solid #e7e2dc;border-radius:14px}'
    + '#vo-as .lead input{flex:none} #vo-as .lead button{font:inherit;font-weight:600;border:0;border-radius:10px;padding:9px;background:' + C.color + ';color:#fff;cursor:pointer}'
    + '@media (prefers-color-scheme:dark){#vo-as .p{background:#1b1a19;color:#eee}#vo-as .m{background:#141312}#vo-as .a,#vo-as .lead{background:#24221f;border-color:#34302c;color:#eee}'
    + '#vo-as .c,#vo-as form,#vo-as .f{background:#1b1a19;border-color:#2c2926}#vo-as .c a,#vo-as .c button,#vo-as input{background:#24221f;color:#eee;border-color:#3a3530}}';
  var st = document.createElement('style'); st.textContent = css; document.head.appendChild(st);
  var root = document.createElement('div'); root.id = 'vo-as';
  root.innerHTML = '<div class="p" role="dialog" aria-label=""><div class="h"><div><b></b><small></small></div><button class="x" type="button">×</button></div>'
    + '<div class="m" aria-live="polite"></div><div class="c"></div><form><input maxlength="600" autocomplete="off"><button></button></form><div class="f"></div></div>'
    + '<button class="b" type="button"><i>V</i><span></span></button>';
  document.body.appendChild(root);
  var $ = function(s){ return root.querySelector(s); }, M = $('.m');
  if(C.photo){ ['.b i', '.h'].forEach(function(sel, n){ var im = document.createElement('img'); im.src = C.photo; im.alt = '';
    im.className = n ? 'hp' : 'bp'; if(n) $('.h').insertBefore(im, $('.h').firstChild); else { $('.b i').textContent = ''; $('.b i').appendChild(im); } }); }
  $('.h b').textContent = T.as_title; $('.h small').textContent = C.name; $('.b span').textContent = T.as_title;
  $('.p').setAttribute('aria-label', T.as_title); $('.x').setAttribute('aria-label', T.as_close);
  $('input').placeholder = T.as_ph; $('form button').textContent = T.as_send; $('.f').textContent = T.as_powered;
  function link(text){                                          // answers may carry links: make https ones clickable, nothing else
    var d = document.createElement('div'), re = /(https:\/\/[^\s)]+)/g, last = 0, m;
    while((m = re.exec(text))){ d.appendChild(document.createTextNode(text.slice(last, m.index)));
      var a = document.createElement('a'); a.href = m[1]; a.target = '_blank'; a.rel = 'noopener'; a.textContent = m[1].replace(/^https:\/\//, ''); d.appendChild(a); last = re.lastIndex; }
    d.appendChild(document.createTextNode(text.slice(last))); return d;
  }
  function say(r, text){ var d = document.createElement('div'); d.className = r === 'user' ? 'q' : 'a';
    if(r === 'user') d.textContent = text; else d.appendChild(link(text)); M.appendChild(d); M.scrollTop = M.scrollHeight; }
  function chips(){
    var c = $('.c'); c.innerHTML = '';
    [[C.book, T.as_book], [C.work, T.as_work], [C.preview, T.as_preview], [C.wa, T.as_wa]].forEach(function(x){
      if(!x[0]) return; var a = document.createElement('a'); a.href = x[0]; a.target = '_blank'; a.rel = 'noopener'; a.textContent = x[1]; c.appendChild(a); });
    var b = document.createElement('button'); b.type = 'button'; b.textContent = T.as_leave; b.onclick = leadForm; c.appendChild(b);
  }
  function leadForm(){
    if(M.querySelector('.lead')) return;
    var f = document.createElement('div'); f.className = 'lead';
    var n = document.createElement('input'); n.placeholder = T.as_name; n.maxLength = 200;
    var p = document.createElement('input'); p.placeholder = T.as_phone; p.maxLength = 200; p.inputMode = 'tel';
    var go = document.createElement('button'); go.type = 'button'; go.textContent = T.as_leave_go;
    go.onclick = function(){
      if(!n.value.trim() || !p.value.trim()) return; go.disabled = true;
      var msg = turns.filter(function(x){ return x.r === 'user'; }).map(function(x){ return x.t; }).join('\n') || '—';
      fetch(C.api + '/lead', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name:n.value, contact:p.value, message:msg})})
        .then(function(r){ return r.json(); }).then(function(j){
          if(j.ok){ f.remove(); say('assistant', T.as_thanks); turns.push({r:'assistant', t:T.as_thanks}); save(); }
          else { go.disabled = false; go.textContent = j.error || T.as_busy; } })
        .catch(function(){ go.disabled = false; go.textContent = T.as_busy; });
    };
    f.appendChild(n); f.appendChild(p); f.appendChild(go); M.appendChild(f); n.focus(); M.scrollTop = M.scrollHeight;
  }
  function start(){ M.innerHTML = ''; if(!turns.length) say('assistant', T.as_hello); turns.forEach(function(x){ say(x.r, x.t); }); chips(); }
  $('.b').onclick = function(){ open = !open; root.classList.toggle('on', open); if(open){ start(); setTimeout(function(){ $('input').focus(); }, 50); } };
  $('.x').onclick = function(){ open = false; root.classList.remove('on'); };
  $('form').onsubmit = function(e){
    e.preventDefault(); var i = $('input'), q = i.value.trim(); if(!q) return; i.value = '';
    say('user', q); turns.push({r:'user', t:q}); save();
    var w = document.createElement('div'); w.className = 'a'; w.textContent = '…'; M.appendChild(w);
    fetch(C.api + '/ask', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({turns:turns})})
      .then(function(r){ return r.json(); }).then(function(j){
        w.remove();
        var a = j.text || (j.reason === 'busy' ? T.as_busy : T.as_offline);
        say('assistant', a); turns.push({r:'assistant', t:a}); save();
        if(!j.text) leadForm(); })
      .catch(function(){ w.remove(); say('assistant', T.as_offline); leadForm(); });
  };
})();
"""
