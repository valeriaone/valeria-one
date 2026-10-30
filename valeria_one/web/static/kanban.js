/* Valeria One — boards (mon01, v0.16): the pipeline and the tasks. Plain JavaScript, no library, for cheap phones.
   - List / board: the choice is remembered on this phone (localStorage), ?view= in the address wins.
   - Drag a card to another column with the mouse (anywhere on the card) or a finger (from its ⠿ grip, so the page still
     scrolls). Arrow keys on a focused card move it left/right; up/down go to the next card. "Move to…" works everywhere,
     also without JavaScript. Each move is the same POST the "Move to…" buttons make, so the recipes fire the same way;
     if the server says no, the card goes back where it was. */
(function () {
  'use strict';
  function all(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }
  function one(sel, root) { return (root || document).querySelector(sel); }

  /* 1. list or board */
  all('[data-view-toggle]').forEach(function (group) {
    var key = 'vo-view:' + group.getAttribute('data-view-toggle');
    var panes = all('[data-view-pane]');
    function show(v, keep) {
      if (!panes.some(function (p) { return p.getAttribute('data-view-pane') === v; })) return;
      panes.forEach(function (p) { p.hidden = p.getAttribute('data-view-pane') !== v; });
      all('[data-view]', group).forEach(function (b) {
        var on = b.getAttribute('data-view') === v;
        b.classList.toggle('on', on); b.setAttribute('aria-pressed', on ? 'true' : 'false');
      });
      if (keep) { try { localStorage.setItem(key, v); } catch (e) { /* private mode: fine */ } }
    }
    var asked = null;
    try { asked = new URLSearchParams(location.search).get('view'); } catch (e) { asked = null; }
    var saved = null;
    try { saved = localStorage.getItem(key); } catch (e) { saved = null; }
    if (asked) show(asked, true); else if (saved) show(saved, false);
    group.addEventListener('click', function (e) {
      var b = e.target.closest('[data-view]');
      if (!b) return;
      e.preventDefault();
      show(b.getAttribute('data-view'), true);
    });
  });

  /* 2. boards */
  all('[data-kb]').forEach(function (kb) {
    var url = kb.getAttribute('data-move-url'), csrf = kb.getAttribute('data-csrf'), field = kb.getAttribute('data-field') || 'stage';
    var live = one('[data-kb-live]'), rtl = document.documentElement.getAttribute('dir') === 'rtl';
    var drag = null;

    function cols() { return all('[data-col]', kb); }
    function colOf(el) { return el && el.closest('[data-col]'); }
    function list(col) { return one('[data-drop]', col); }
    function name(col) { var s = one('h3 span', col); return s ? s.textContent : ''; }
    function say(text) { if (!live) return; live.textContent = ''; setTimeout(function () { live.textContent = text; }, 40); }
    function counts() { cols().forEach(function (c) { var n = one('[data-count]', c); if (n) n.textContent = all('[data-card]', c).length; }); }
    function menu(card, col) {
      all('[data-move-to]', card).forEach(function (b) { b.disabled = b.value === col.getAttribute('data-col'); });
      card.setAttribute('aria-label', (card.getAttribute('aria-label') || '').replace(/ · [^·]*$/, '') + ' · ' + name(col));
    }
    function put(card, col, before) {
      var l = list(col), empty = one('.kb-empty', l);
      if (before && before.parentNode === l) l.insertBefore(card, before); else l.insertBefore(card, empty);
    }
    function send(card, col, from, before) {
      var body = new FormData();
      body.append('csrf', csrf); body.append(field, col.getAttribute('data-col'));
      card.classList.add('busy');
      fetch(url.replace('{id}', card.getAttribute('data-card')), {method: 'POST', body: body, credentials: 'same-origin',
                                                                 headers: {'X-Requested-With': 'fetch'}})
        .then(function (r) { return r.json().catch(function () { return {ok: false}; }); })
        .then(function (j) {
          card.classList.remove('busy');
          if (!j || !j.ok) throw new Error((j && j.error) || '');
          menu(card, col);
          say((kb.getAttribute('data-moved') || '').replace('{col}', name(col)));
        })
        .catch(function (e) {
          card.classList.remove('busy');
          put(card, from, before); counts(); menu(card, from);
          say((kb.getAttribute('data-err') || '') + (e && e.message ? ' ' + e.message : ''));
        });
    }
    function move(card, col) {
      var from = colOf(card);
      if (!col || col === from) return;
      var before = card.nextElementSibling;
      put(card, col, null); counts(); send(card, col, from, before);
      card.focus();
    }

    /* "Move to…": no page reload when JavaScript is on */
    kb.addEventListener('click', function (e) {
      var b = e.target.closest('[data-move-to]');
      if (!b) return;
      var card = b.closest('[data-card]'), col = null;
      cols().forEach(function (c) { if (c.getAttribute('data-col') === b.value) col = c; });
      if (!card || !col) return;
      e.preventDefault();
      var d = b.closest('details'); if (d) d.open = false;
      move(card, col);
    });

    /* the keyboard */
    kb.addEventListener('keydown', function (e) {
      var card = e.target;
      if (!card.hasAttribute || !card.hasAttribute('data-card')) return;
      var cs = cols(), i = cs.indexOf(colOf(card)), k = e.key;
      var step = k === 'ArrowRight' ? 1 : (k === 'ArrowLeft' ? -1 : 0);
      if (rtl) step = -step;
      if (step) { e.preventDefault(); if (cs[i + step]) move(card, cs[i + step]); return; }
      if (k === 'ArrowDown' || k === 'ArrowUp') {
        e.preventDefault();
        var s = k === 'ArrowDown' ? card.nextElementSibling : card.previousElementSibling;
        while (s && !s.hasAttribute('data-card')) s = k === 'ArrowDown' ? s.nextElementSibling : s.previousElementSibling;
        if (s) s.focus();
        return;
      }
      if (k === 'Enter') { var a = one('a', card); if (a) a.click(); }
    });

    /* drag: pointer events = mouse, finger and pen alike */
    kb.addEventListener('pointerdown', function (e) {
      var card = e.target.closest('[data-card]');
      if (!card || e.button > 0) return;
      var grip = e.target.closest('.kb-grip');
      if (e.pointerType !== 'mouse' && !grip) return;
      if (!grip && e.target.closest('button,summary,select,input,textarea,label,form')) return;   /* the title link may start a drag */
      drag = {card: card, id: e.pointerId, x: e.clientX, y: e.clientY, on: false, from: colOf(card), before: card.nextElementSibling};
    });
    kb.addEventListener('dragstart', function (e) { if (e.target.closest && e.target.closest('[data-card]')) e.preventDefault(); });   /* no native link/image drag */
    function start(e) {
      var r = drag.card.getBoundingClientRect();
      drag.on = true; drag.dx = e.clientX - r.left; drag.dy = e.clientY - r.top;
      var g = drag.card.cloneNode(true);
      g.removeAttribute('data-card'); g.removeAttribute('tabindex'); g.classList.add('kb-ghost');
      g.style.width = r.width + 'px';
      document.body.appendChild(g); drag.ghost = g;
      drag.card.classList.add('kb-placeholder');
      kb.classList.add('dragging');
    }
    document.addEventListener('pointermove', function (e) {
      if (!drag || e.pointerId !== drag.id) return;
      if (!drag.on) {
        if (Math.abs(e.clientX - drag.x) + Math.abs(e.clientY - drag.y) < 6) return;
        start(e);
      }
      e.preventDefault();
      drag.ghost.style.transform = 'translate(' + (e.clientX - drag.dx) + 'px,' + (e.clientY - drag.dy) + 'px)';
      var el = document.elementFromPoint(e.clientX, e.clientY), col = colOf(el);
      cols().forEach(function (c) { c.classList.toggle('over', c === col); });
      if (col && kb.contains(col)) {
        var after = null;
        all('[data-card]', list(col)).forEach(function (c) {
          if (c === drag.card || after) return;
          var r = c.getBoundingClientRect();
          if (e.clientY < r.top + r.height / 2) after = c;
        });
        put(drag.card, col, after);
      }
      var b = kb.getBoundingClientRect();                     /* near an edge: the board scrolls along */
      if (e.clientX > b.right - 40) kb.scrollLeft += 14; else if (e.clientX < b.left + 40) kb.scrollLeft -= 14;
    }, {passive: false});
    function end(e, cancel) {
      if (!drag || e.pointerId !== drag.id) return;
      var d = drag; drag = null;
      if (!d.on) return;
      d.ghost.parentNode.removeChild(d.ghost);
      d.card.classList.remove('kb-placeholder'); kb.classList.remove('dragging');
      cols().forEach(function (c) { c.classList.remove('over'); });
      var col = colOf(d.card);
      if (cancel || col === d.from) { put(d.card, d.from, d.before); counts(); return; }   /* the order inside a column isn't kept */
      counts(); send(d.card, col, d.from, d.before);
      var stop = function (ev) { ev.preventDefault(); ev.stopPropagation(); document.removeEventListener('click', stop, true); };
      document.addEventListener('click', stop, true);          /* the click that ends a drag doesn't open the card */
      setTimeout(function () { document.removeEventListener('click', stop, true); }, 400);
      setTimeout(function () { d.card.focus(); }, 0);
    }
    document.addEventListener('pointerup', function (e) { end(e, false); });
    document.addEventListener('pointercancel', function (e) { end(e, true); });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && drag && drag.on) end({pointerId: drag.id}, true);
    });
  });
})();
