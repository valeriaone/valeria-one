/* «A day with Valeria» — the phone in the hero (site01). Data: /static/day/day.json (script + words per language).
   The server already drew the finished still frame (web/day.py); this plays the day from the start, in the visitor's
   language, and only while the phone is on screen. Reduced motion: the still frame, nothing moves. */
(function () {
  var root = document.querySelector('[data-day]');
  if (!root || !window.fetch) return;
  var $ = function (s) { return root.querySelector(s); };
  var chat = $('.dv-chat'), clock = $('.dv-clock'), who = $('.dv-who span'), input = $('.dv-input'), screen = $('.dv-screen'),
      end = $('.dv-end'), cap = $('.dv-cap'), pauseBtn = $('[data-dv-pause]'), watch = document.querySelector('[data-watch]');
  var reduce = window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches;
  var free = root.getAttribute('data-free') === '1';
  var D = null, W = null, lang = root.getAttribute('data-lang') || 'en';
  var timers = [], idx = 0, paused = false, away = false, typingNode = null, started = false;

  function el(cls, html) { var e = document.createElement('div'); e.className = cls; if (html != null) e.innerHTML = html; return e; }
  function wave(n) {
    var s = '';
    for (var i = 0; i < n; i++) s += '<i style="height:' + (6 + Math.round(Math.abs(Math.sin(i * 1.7)) * 14)) + 'px;animation-delay:' + ((i % 6) * 0.08).toFixed(2) + 's"></i>';
    return '<div class="dv-wave">' + s + '</div>';
  }
  function btns(list) { return '<div class="dv-btns">' + list.map(function (b) { return '<span class="dv-btn" data-b="' + b + '">' + W[b] + '</span>'; }).join('') + '</div>'; }
  // the chat never grows: a bubble that has scrolled fully out of the top is removed (the phone's size is fixed)
  function trim() {
    var top = chat.getBoundingClientRect().top;
    while (chat.firstElementChild && chat.children.length > 1 && chat.firstElementChild.getBoundingClientRect().bottom <= top + 1) chat.removeChild(chat.firstElementChild);
  }
  function add(n) { chat.appendChild(n); trim(); return n; }
  function untype() { if (typingNode) { typingNode.remove(); typingNode = null; } }
  function moment(m) {
    var t = ['8:00', '11:20', '15:45', W.mon][m];
    cap.innerHTML = '<b>' + t + '</b> ' + W['m' + m];
    root.setAttribute('data-moment', m);
  }

  function step(s, instant) {
    var op = s[1], a = s[2];
    if (a.clock || a.clockk) clock.textContent = a.clock || W[a.clockk];
    if (a.m != null) moment(a.m);
    if (op === 'day') { untype(); add(el('dv-day', W[a.k])); }
    else if (op === 'typing') { if (!instant) typingNode = add(el('dv-typing', '<i></i><i></i><i></i>')); }
    else if (op === 'in') { untype(); add(el('dv-b in', W[a.k] + (a.btns ? btns(a.btns) : ''))); }
    else if (op === 'out') { add(el('dv-b out', '<small>' + W[a.k] + '</small>')); }
    else if (op === 'voice') {
      var v = add(el('dv-b out', '<div class="dv-voice' + (instant ? '' : ' playing') + '"><span class="dv-play"></span>' + wave(22) + '<small>' + a.len + '</small></div>'));
      if (!instant) timers.push(setTimeout(function () { var x = v.querySelector('.dv-voice'); if (x) x.classList.remove('playing'); }, 2000));
    }
    else if (op === 'tap') {
      var all = chat.querySelectorAll('.dv-btn[data-b="' + a.b + '"]');
      if (all.length) all[all.length - 1].classList.add('tap');
    }
    else if (op === 'end') { if (!instant) end.classList.add('show'); }
    else if (op === 'loop') { if (!instant) play(); }
  }

  function words() {
    W = D.strings[lang] || D.strings.en;
    var rtl = (D.meta[lang] || {}).dir === 'rtl';
    screen.setAttribute('dir', rtl ? 'rtl' : 'ltr');
    screen.setAttribute('lang', lang);
    root.classList.toggle('dv-ur', lang === 'ur');
    who.innerHTML = W.who; input.innerHTML = W.input;
    end.querySelector('b').innerHTML = W.end1;
    end.querySelector('span').innerHTML = free ? W.end2 : '';
    if (rtl && D.meta[lang].font && !document.getElementById('dv-font')) {        // the Urdu face loads only when asked for
      var l = document.createElement('link'); l.id = 'dv-font'; l.rel = 'stylesheet';
      l.href = 'https://fonts.googleapis.com/css2?family=Noto+Nastaliq+Urdu:wght@400;700&display=swap';
      document.head.appendChild(l);
    }
    if (watch) watch.href = '/static/video/valeria-one-day-' + lang + '.mp4';
  }
  function clear() { timers.forEach(clearTimeout); timers = []; untype(); chat.innerHTML = ''; end.classList.remove('show'); }
  function still() {                                    // the finished frame, as the server draws it
    clear();
    var last = D.script.length - 2;
    for (var i = 0; i < last; i++) step(D.script[i], true);
    while (chat.children.length > 5) chat.removeChild(chat.firstElementChild);
  }
  function run() {
    if (paused || away || idx >= D.script.length) return;
    var s = D.script[idx];
    timers.push(setTimeout(function () { if (paused || away) return; idx++; step(s, false); run(); }, s[0]));
  }
  function play() { clear(); idx = 0; started = true; run(); }
  function setLang(c) {
    lang = D.strings[c] ? c : 'en';
    [].forEach.call(root.querySelectorAll('[data-dv-lang]'), function (b) { b.setAttribute('aria-pressed', b.getAttribute('data-dv-lang') === lang ? 'true' : 'false'); });
    words();
    if (reduce) { still(); return; }
    paused = false; pauseBtn.textContent = pauseBtn.getAttribute('data-t-pause');
    if (!away) play(); else { still(); started = false; }
  }

  fetch(root.getAttribute('data-src')).then(function (r) { return r.json(); }).then(function (d) {
    D = d;
    root.classList.add('dv-js');
    [].forEach.call(root.querySelectorAll('[data-dv-lang]'), function (b) { b.addEventListener('click', function () { setLang(b.getAttribute('data-dv-lang')); }); });
    $('[data-dv-replay]').addEventListener('click', function () { if (reduce) { still(); return; } paused = false; pauseBtn.textContent = pauseBtn.getAttribute('data-t-pause'); play(); });
    pauseBtn.addEventListener('click', function () {
      if (reduce) return;
      paused = !paused;
      pauseBtn.textContent = pauseBtn.getAttribute(paused ? 'data-t-play' : 'data-t-pause');
      if (paused) { timers.forEach(clearTimeout); timers = []; } else if (!started) play(); else run();
    });
    var c = lang;
    if (root.getAttribute('data-auto') === '1') {     // the English page speaks Taglish, Hinglish or Urdu to those browsers
      var l = (navigator.language || '').toLowerCase();
      if (/^(fil|tl)/.test(l)) c = 'tl'; else if (/^hi/.test(l)) c = 'hi'; else if (/^ur/.test(l)) c = 'ur';
    }
    W = D.strings[c] || D.strings.en;
    if (reduce) { setLang(c); return; }
    lang = c; words();
    [].forEach.call(root.querySelectorAll('[data-dv-lang]'), function (b) { b.setAttribute('aria-pressed', b.getAttribute('data-dv-lang') === lang ? 'true' : 'false'); });
    if (c !== root.getAttribute('data-lang')) still();
    // play only while the phone is on screen and the tab is visible (cheap phones, batteries)
    function wake(on) {
      var was = away; away = !on;
      if (away) { timers.forEach(clearTimeout); timers = []; }
      else if (was && !paused) { if (!started) play(); else run(); }
    }
    away = true;
    if ('IntersectionObserver' in window) new IntersectionObserver(function (e) { wake(e[0].isIntersecting && !document.hidden); }, { threshold: 0.25 }).observe(root.querySelector('.dv-phone'));
    else wake(true);
    document.addEventListener('visibilitychange', function () { if (document.hidden) wake(false); else { var r = root.querySelector('.dv-phone').getBoundingClientRect(); wake(r.bottom > 0 && r.top < innerHeight); } });
  }).catch(function () {});
})();
