/* help01: Valeria walks the owner through a job step by step (points at the spot on each page), and offers help on her own when
   someone looks stuck — the same «didn't go through» twice, or a minute on a setup page without saving. Never blocks the screen:
   one offer per page per visit, «Not now» = quiet for the day. «Do it for me» hands the job to valAI. */
(function(){
  var cp = document.getElementById('cp'); if(!cp) return;
  var CSRF = cp.dataset.csrf, K = 'vo-guide', cat = null, waiting = [];
  function ls(k, v){ try { if(v === undefined) return localStorage.getItem(k); if(v === null) localStorage.removeItem(k); else localStorage.setItem(k, v); } catch(e){ return null; } }
  function ss(k, v){ try { if(v === undefined) return sessionStorage.getItem(k); sessionStorage.setItem(k, v); } catch(e){ return null; } }
  function load(fn){ if(cat) return fn(cat); waiting.push(fn); if(waiting.length > 1) return;
    fetch('/app/help/catalog', {credentials:'same-origin'}).then(function(r){ return r.json(); })
      .then(function(j){ cat = j; var w = waiting; waiting = []; w.forEach(function(f){ f(j); }); }).catch(function(){ waiting = []; }); }
  function path(p){ return p.split('#')[0].split('?')[0]; }
  function el(tag, cls, txt){ var e = document.createElement(tag); if(cls) e.className = cls; if(txt != null) e.textContent = txt; return e; }
  var css = '.vg-hi{outline:3px solid #25d366!important;outline-offset:4px;border-radius:8px;scroll-margin:120px;transition:outline-color .3s}'
    + '.vg-tip{position:fixed;z-index:2147482500;max-width:min(360px,calc(100vw - 24px));background:#111;color:#fff;border-radius:14px;padding:12px 14px 10px;'
    + 'box-shadow:0 14px 40px rgba(0,0,0,.35);font:14px/1.4 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}'
    + '.vg-tip b{display:block;font-size:12px;opacity:.7;margin-bottom:3px}.vg-tip .vg-row{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px;align-items:center}'
    + '.vg-tip button,.vg-tip a.vg-btn{font:inherit;font-size:13px;border:0;border-radius:9px;padding:7px 11px;cursor:pointer;text-decoration:none}'
    + '.vg-go{background:#25d366;color:#05230f;font-weight:700}.vg-me{background:#2d2d2d;color:#fff}.vg-no{background:none;color:#bbb;padding-left:2px!important}'
    + '.vg-x{position:absolute;top:4px;right:8px;background:none!important;color:#999;font-size:18px!important;padding:2px 4px!important}'
    + '.vg-arrow{position:fixed;z-index:2147482499;font-size:28px;color:#25d366;pointer-events:none;animation:vgb 1s infinite}@keyframes vgb{50%{transform:translateY(-6px)}}'
    + '.cp-g{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}.cp-g button{font:inherit;font-size:12.5px;border:1px solid #25d366;background:transparent;color:inherit;border-radius:999px;padding:4px 10px;cursor:pointer}'
    + '.cp-g button.me{border-color:#888}';
  var st = el('style'); st.textContent = css; document.head.appendChild(st);
  var tip = null, arrow = null;
  function clear(){ if(tip){ tip.remove(); tip = null; } if(arrow){ arrow.remove(); arrow = null; } document.querySelectorAll('.vg-hi').forEach(function(x){ x.classList.remove('vg-hi'); }); }
  function place(target){
    if(!tip) return; var r = target ? target.getBoundingClientRect() : null, W = innerWidth, H = innerHeight, tw = tip.offsetWidth, th = tip.offsetHeight;
    if(!r || r.bottom < 0 || r.top > H){ tip.style.left = Math.max(12, W - tw - 16) + 'px'; tip.style.top = Math.max(12, H - th - 96) + 'px'; if(arrow) arrow.style.display = 'none'; return; }
    var top = r.bottom + 14 + th < H ? r.bottom + 14 : Math.max(12, r.top - th - 14);
    tip.style.top = top + 'px'; tip.style.left = Math.min(Math.max(12, r.left), W - tw - 12) + 'px';
    if(arrow){ arrow.style.display = ''; arrow.textContent = top > r.top ? '⬆' : '⬇'; arrow.style.left = Math.min(Math.max(8, r.left + 12), W - 40) + 'px';
      arrow.style.top = (top > r.top ? r.bottom + 2 : r.top - 34) + 'px'; } }
  function box(head, text, buttons){
    clear(); tip = el('div', 'vg-tip'); tip.setAttribute('role', 'dialog');
    var x = el('button', 'vg-x', '×'); x.type = 'button'; x.setAttribute('aria-label', 'close'); x.onclick = function(){ clear(); ls(K, null); }; tip.appendChild(x);
    if(head) tip.appendChild(el('b', null, head)); tip.appendChild(el('div', null, text));
    var row = el('div', 'vg-row'); buttons.forEach(function(b){ row.appendChild(b); }); tip.appendChild(row); document.body.appendChild(tip); return tip; }
  function btn(cls, label, fn){ var b = el('button', cls, label); b.type = 'button'; b.onclick = fn; return b; }
  function forMe(key){
    fetch('/app/help/forme', {method:'POST', credentials:'same-origin', headers:{'Content-Type':'application/json','X-CSRF':CSRF},
      body:JSON.stringify({task:key || '', page:location.pathname})}).then(function(r){ return r.json(); })
      .then(function(j){ ls(K, null); load(function(c){ box(null, j.say || c.text.asked, [btn('vg-go', c.text.done, clear)]); }); }).catch(function(){}); }
  function show(){
    var s; try { s = JSON.parse(ls(K) || 'null'); } catch(e){ s = null; } if(!s) return;
    load(function(c){
      var g = c.guides[s.key]; if(!g){ ls(K, null); return; } var step = g.steps[s.i]; if(!step){ ls(K, null); return; }
      var T = c.text, head = g.title + ' · ' + T.step + ' ' + (s.i + 1) + '/' + g.steps.length, last = s.i === g.steps.length - 1;
      if(path(step.path) !== location.pathname){
        var a = el('a', 'vg-btn vg-go', T.go_to + ' →'); a.href = step.path;
        box(head, step.text, [a, btn('vg-me', T.forme, function(){ forMe(s.key); })]); place(null); return; }
      var target = null; step.sel.split(',').some(function(q){ try { target = document.querySelector(q.trim()); } catch(e){} return !!target; });
      box(head, step.text, [btn('vg-go', last ? T.done : T.next, function(){
          if(last){ ls(K, null); clear(); return; } s.i += 1; ls(K, JSON.stringify(s)); show(); }),
        btn('vg-me', T.forme, function(){ forMe(s.key); })]);
      if(target){ target.classList.add('vg-hi'); target.scrollIntoView({block:'center', behavior:'smooth'});
        arrow = el('div', 'vg-arrow'); document.body.appendChild(arrow); setTimeout(function(){ place(target); }, 450); } else place(null);
    }); }
  window.VOGuide = {start: function(key){ cp.classList.remove('on'); ls(K, JSON.stringify({key:key, i:0})); show(); }, forMe: forMe,
    chips: function(keys){ var d = el('div', 'cp-g'); load(function(c){
      keys.forEach(function(k){ if(c.guides[k]) d.appendChild(btn('', c.text.go + ': ' + c.guides[k].title, function(){ window.VOGuide.start(k); })); });
      d.appendChild(btn('me', c.text.forme, function(){ forMe(keys[0] || ''); })); }); return d; }};
  addEventListener('resize', function(){ var t = document.querySelector('.vg-hi'); if(tip) place(t); });
  show();
  // proactive: «that didn't go through» twice on this page, or a minute on a setup page without saving → one offer, never more
  function quiet(){ return ls('vo-help-quiet') === new Date().toDateString() || ls(K) || ss('vo-help-' + location.pathname); }
  function offer(kind){
    if(quiet() || cp.classList.contains('on')) return; ss('vo-help-' + location.pathname, '1');
    load(function(c){ var key = null;
      Object.keys(c.guides).some(function(k){ return c.guides[k].steps.some(function(st){ if(path(st.path) === location.pathname){ key = k; return true; } return false; }); });
      var bs = [];
      if(key) bs.push(btn('vg-go', c.text.go + ': ' + c.guides[key].title, function(){ window.VOGuide.start(key); }));
      bs.push(btn('vg-me', c.text.forme, function(){ forMe(key || ''); }));
      bs.push(btn('vg-no', c.text.later, function(){ ls('vo-help-quiet', new Date().toDateString()); clear(); }));
      box(null, kind === 'errors' ? c.text.errors : c.text.stuck, bs); place(null); }); }
  if(document.querySelector('.flash.warn')){ var n = (parseInt(ss('vo-err-' + location.pathname) || '0', 10) || 0) + 1; ss('vo-err-' + location.pathname, String(n));
    if(n >= 2) setTimeout(function(){ offer('errors'); }, 800); }
  var SETUP = ['/app/channels', '/app/calendar', '/app/website', '/app/settings', '/app/projects'], busy = false;
  if(SETUP.indexOf(location.pathname) >= 0){
    ['input', 'change', 'submit'].forEach(function(ev){ document.addEventListener(ev, function(){ busy = true; }, true); });
    setTimeout(function(){ if(!busy) offer('stuck'); }, 60000); }
})();
