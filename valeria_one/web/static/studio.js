/* The design studio's two editors: the 2D floor plan (centimetres, to scale) and the free-form mood board.
   No libraries: pointer events + SVG / absolutely placed boxes. Saves with fetch; works on a phone. */
(function(){
  function post(url, csrf, data){
    var fd = new FormData(); fd.append('csrf', csrf);
    Object.keys(data).forEach(function(k){ fd.append(k, data[k]); });
    return fetch(url, {method:'POST', body:fd, headers:{'X-Requested-With':'fetch'}, credentials:'same-origin'})
      .then(function(r){ if(!r.ok) throw new Error(r.status); return r.json(); });
  }
  function toast(msg){
    var t = document.createElement('div'); t.className = 'toast'; t.textContent = msg; document.body.appendChild(t);
    setTimeout(function(){ t.remove(); }, 1800);
  }

  /* busy forms (the AI visualizer takes a while): disable the button, say so */
  document.addEventListener('submit', function(e){
    var f = e.target, msg = f.getAttribute('data-busy'); if(!msg) return;
    var pick = f.querySelector('input[name=pick]:checked'); if(pick) f.action = pick.getAttribute('data-action');
    var b = f.querySelector('button.primary'); if(b){ b.disabled = true; b.textContent = msg; }
  });
  document.addEventListener('click', function(e){             /* copy an AI draft */
    var b = e.target.closest('[data-copy-text]'); if(!b || !navigator.clipboard) return;
    var t = b.parentNode.querySelector('.txt'); navigator.clipboard.writeText(t.textContent.trim()).then(function(){ b.classList.add('ok'); });
  });

  /* ---------------------------------------------------------------- floor plan */
  var svg = document.getElementById('plan');
  if(svg){
    var NS = 'http://www.w3.org/2000/svg', W = +svg.dataset.w, H = +svg.dataset.h, pad = Math.max(W, H) * 0.06;
    var data = JSON.parse(document.getElementById('plan-data').textContent || '{}'); var items = data.items || [];
    var sel = -1, drag = null, dirty = false, edit = document.querySelector('.plan-edit');
    svg.setAttribute('viewBox', (-pad) + ' ' + (-pad) + ' ' + (W + 2 * pad) + ' ' + (H + 2 * pad));
    function el(n, a){ var x = document.createElementNS(NS, n); for(var k in a) x.setAttribute(k, a[k]); return x; }
    function pt(ev){ var p = svg.createSVGPoint(); p.x = ev.clientX; p.y = ev.clientY; return p.matrixTransform(svg.getScreenCTM().inverse()); }
    function snap(v){ return Math.round(v / 5) * 5; }
    function draw(){
      while(svg.firstChild) svg.removeChild(svg.firstChild);
      for(var gx = 0; gx <= W; gx += 50) svg.appendChild(el('line', {x1:gx, y1:0, x2:gx, y2:H, class:'grid' + (gx % 100 ? '' : ' m')}));
      for(var gy = 0; gy <= H; gy += 50) svg.appendChild(el('line', {x1:0, y1:gy, x2:W, y2:gy, class:'grid' + (gy % 100 ? '' : ' m')}));
      svg.appendChild(el('rect', {x:0, y:0, width:W, height:H, class:'room'}));
      var tw = el('text', {x:W / 2, y:-pad / 3, class:'dim'}); tw.textContent = W + ' cm'; svg.appendChild(tw);
      var th = el('text', {x:-pad / 3, y:H / 2, class:'dim', transform:'rotate(-90 ' + (-pad / 3) + ' ' + (H / 2) + ')'}); th.textContent = H + ' cm'; svg.appendChild(th);
      items.forEach(function(it, i){
        var g = el('g', {transform:'translate(' + it.x + ' ' + it.y + ') rotate(' + (it.r || 0) + ' ' + it.w / 2 + ' ' + it.h / 2 + ')', class:'pc ' + it.shape + (i === sel ? ' on' : ''), 'data-i':i});
        var shape = it.shape === 'circle' ? el('ellipse', {cx:it.w / 2, cy:it.h / 2, rx:it.w / 2, ry:it.h / 2}) : el('rect', {width:it.w, height:it.h, rx:it.shape === 'rect' ? 3 : 0});
        if(it.color) shape.setAttribute('style', 'fill:' + it.color);
        g.appendChild(shape);
        var t = el('text', {x:it.w / 2, y:it.h / 2}); t.textContent = it.label; g.appendChild(t);
        if(i === sel){ g.appendChild(el('rect', {x:it.w - 8, y:it.h - 8, width:16, height:16, class:'grip', 'data-grip':i})); }
        svg.appendChild(g);
      });
      fill();
    }
    function fill(){
      edit.style.visibility = sel < 0 ? 'hidden' : 'visible'; if(sel < 0) return;
      var it = items[sel];
      edit.querySelectorAll('[data-f]').forEach(function(inp){ var k = inp.dataset.f; inp.value = k === 'color' ? (it.color || '#dddddd') : (it[k] === undefined ? '' : Math.round(it[k] * 10) / 10); });
    }
    edit.addEventListener('input', function(e){
      var k = e.target.dataset.f; if(sel < 0 || !k) return; var it = items[sel];
      if(k === 'label' || k === 'color') it[k] = e.target.value; else { var v = parseFloat(e.target.value); if(!isNaN(v)) it[k] = (k === 'r' ? v : Math.max(1, v)); }
      dirty = true; draw();
    });
    edit.querySelector('[data-del]').addEventListener('click', function(){ if(sel < 0) return; items.splice(sel, 1); sel = -1; dirty = true; draw(); });
    document.querySelectorAll('[data-add]').forEach(function(b){
      b.addEventListener('click', function(){
        var w = +b.dataset.w, h = +b.dataset.h;
        items.push({label:b.dataset.add, shape:b.dataset.shape, x:snap(W / 2 - w / 2), y:snap(H / 2 - h / 2), w:w, h:h, r:0, color:''});
        sel = items.length - 1; dirty = true; draw();
      });
    });
    svg.addEventListener('pointerdown', function(e){
      var grip = e.target.getAttribute('data-grip'), g = e.target.closest('g.pc');
      if(grip !== null){ sel = +grip; var p0 = pt(e); drag = {mode:'size', p:p0, w:items[sel].w, h:items[sel].h}; }
      else if(g){ sel = +g.getAttribute('data-i'); var p1 = pt(e); drag = {mode:'move', p:p1, x:items[sel].x, y:items[sel].y}; }
      else { sel = -1; drag = null; }
      if(drag) svg.setPointerCapture(e.pointerId);
      draw();
    });
    svg.addEventListener('pointermove', function(e){
      if(!drag || sel < 0) return; var p = pt(e), it = items[sel];
      if(drag.mode === 'move'){ it.x = snap(drag.x + p.x - drag.p.x); it.y = snap(drag.y + p.y - drag.p.y); }
      else { it.w = Math.max(5, snap(drag.w + p.x - drag.p.x)); it.h = Math.max(5, snap(drag.h + p.y - drag.p.y)); }
      dirty = true; draw();
    });
    svg.addEventListener('pointerup', function(){ drag = null; });
    document.addEventListener('keydown', function(e){
      if(sel < 0 || /INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)) return;
      var it = items[sel], step = e.shiftKey ? 50 : 5;
      if(e.key === 'Delete' || e.key === 'Backspace'){ items.splice(sel, 1); sel = -1; }
      else if(e.key === 'r'){ it.r = ((it.r || 0) + 90) % 360; }
      else if(e.key === 'ArrowLeft') it.x -= step; else if(e.key === 'ArrowRight') it.x += step;
      else if(e.key === 'ArrowUp') it.y -= step; else if(e.key === 'ArrowDown') it.y += step;
      else if(e.key === 'd' && (e.ctrlKey || e.metaKey)){ items.push(Object.assign({}, it, {x:it.x + 20, y:it.y + 20})); sel = items.length - 1; }
      else return;
      e.preventDefault(); dirty = true; draw();
    });
    document.querySelector('[data-plan-save]').addEventListener('click', function(){
      post(svg.dataset.url, svg.dataset.csrf, {plan:JSON.stringify({items:items})}).then(function(){ dirty = false; toast(svg.dataset.saved); })
        .catch(function(){ toast('!'); });
    });
    window.addEventListener('beforeunload', function(e){ if(dirty){ e.preventDefault(); e.returnValue = ''; } });
    draw();
  }

  /* ---------------------------------------------------------------- mood board */
  var board = document.getElementById('board');
  if(board){
    var bitems = JSON.parse(document.getElementById('board-data').textContent || '[]'), bsel = -1, bdrag = null, bdirty = false;
    function brender(){
      board.innerHTML = '';
      bitems.forEach(function(it, i){
        var d = document.createElement('div'); d.className = 'bi ' + it.t + (i === bsel ? ' on' : ''); d.dataset.i = i;
        d.style.left = it.x + '%'; d.style.top = it.y + '%'; d.style.width = it.w + '%'; d.style.height = it.h + '%';
        if(it.t === 'img'){ var im = document.createElement('img'); im.src = board.dataset.src + it.file + '?t=1'; im.draggable = false; d.appendChild(im); }
        else if(it.t === 'color'){ var s = document.createElement('span'); s.className = 'swatch'; s.style.background = it.hex; d.appendChild(s);
          var sm = document.createElement('small'); sm.textContent = it.name || it.hex; d.appendChild(sm); }
        else if(it.t === 'text'){ var p = document.createElement('p'); p.textContent = it.text; p.dir = 'auto'; d.appendChild(p); }
        else { var a = document.createElement('small'); a.textContent = it.text || it.url; d.appendChild(a); }
        if(i === bsel){ var g = document.createElement('i'); g.className = 'grip'; g.dataset.grip = i; d.appendChild(g); }
        board.appendChild(d);
      });
    }
    function pct(e){ var r = board.getBoundingClientRect(); return {x:(e.clientX - r.left) / r.width * 100, y:(e.clientY - r.top) / r.height * 100}; }
    function addItem(it){ bitems.push(Object.assign({x:4 + (bitems.length % 4) * 24, y:4 + (Math.floor(bitems.length / 4) * 30) % 90, w:22, h:30}, it)); bsel = bitems.length - 1; bdirty = true; brender(); }
    document.querySelectorAll('[data-add-img]').forEach(function(b){ b.addEventListener('click', function(){ addItem({t:'img', file:b.dataset.addImg}); }); });
    var q = function(s){ return document.querySelector(s); };
    q('[data-add-color]').addEventListener('click', function(){ addItem({t:'color', hex:q('#bd-color').value, name:q('#bd-cname').value, w:10, h:18}); q('#bd-cname').value = ''; });
    q('[data-add-text]').addEventListener('click', function(){ var v = q('#bd-text').value.trim(); if(v){ addItem({t:'text', text:v, w:22, h:12}); q('#bd-text').value = ''; } });
    q('[data-add-link]').addEventListener('click', function(){ var v = q('#bd-url').value.trim(); if(/^https?:\/\//.test(v)){ addItem({t:'link', url:v, text:'', w:22, h:10}); q('#bd-url').value = ''; } });
    board.addEventListener('pointerdown', function(e){
      var grip = e.target.dataset ? e.target.dataset.grip : undefined, d = e.target.closest('.bi');
      var p = pct(e);
      if(grip !== undefined){ bsel = +grip; bdrag = {mode:'size', p:p, w:bitems[bsel].w, h:bitems[bsel].h}; }
      else if(d){ bsel = +d.dataset.i; bdrag = {mode:'move', p:p, x:bitems[bsel].x, y:bitems[bsel].y}; }
      else { bsel = -1; bdrag = null; }
      if(bdrag) board.setPointerCapture(e.pointerId);
      brender();
    });
    board.addEventListener('pointermove', function(e){
      if(!bdrag || bsel < 0) return; var p = pct(e), it = bitems[bsel];
      if(bdrag.mode === 'move'){ it.x = Math.min(98, Math.max(0, bdrag.x + p.x - bdrag.p.x)); it.y = Math.min(98, Math.max(0, bdrag.y + p.y - bdrag.p.y)); }
      else { it.w = Math.min(100, Math.max(3, bdrag.w + p.x - bdrag.p.x)); it.h = Math.min(100, Math.max(3, bdrag.h + p.y - bdrag.p.y)); }
      bdirty = true; brender();
    });
    board.addEventListener('pointerup', function(){ bdrag = null; });
    document.addEventListener('keydown', function(e){
      if(bsel < 0 || /INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)) return;
      if(e.key === 'Delete' || e.key === 'Backspace'){ bitems.splice(bsel, 1); bsel = -1; bdirty = true; brender(); e.preventDefault(); }
      else if(e.key === 'f'){ var it = bitems.splice(bsel, 1)[0]; bitems.push(it); bsel = bitems.length - 1; bdirty = true; brender(); }
    });
    q('[data-board-save]').addEventListener('click', function(){
      bitems.forEach(function(it){ ['x','y','w','h'].forEach(function(k){ it[k] = Math.round(it[k] * 10) / 10; }); });
      post(board.dataset.url, board.dataset.csrf, {items:JSON.stringify(bitems)}).then(function(){ bdirty = false; toast(board.dataset.saved); })
        .catch(function(){ toast('!'); });
    });
    window.addEventListener('beforeunload', function(e){ if(bdirty){ e.preventDefault(); e.returnValue = ''; } });
    brender();
  }
})();
