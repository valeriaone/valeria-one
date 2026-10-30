/* The public pages (site01): tiles that animate only while on screen, real numbers that count up, the
   "talk to her" voice-note demo and the "watch the video" player. The light/dark switch is app.js ([data-mode]). */
(function () {
  var reduce = window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches;
  var io = 'IntersectionObserver' in window;

  // sections with tiny previews: .live while on screen, so nothing animates off screen
  [].forEach.call(document.querySelectorAll('[data-live]'), function (s) {
    if (!io) { s.classList.add('live'); return; }
    new IntersectionObserver(function (e) { s.classList.toggle('live', e[0].isIntersecting); }, { rootMargin: '80px' }).observe(s);
  });

  // count-ups: the number is already on the page (no JS, crawlers); this only animates it the first time it shows
  if (!reduce && io) [].forEach.call(document.querySelectorAll('[data-count]'), function (n) {
    var to = +n.getAttribute('data-count'), done = false;
    new IntersectionObserver(function (e, o) {
      if (!e[0].isIntersecting || done) return;
      done = true; o.disconnect();
      var t0 = performance.now(), d = 900;
      (function f(t) { var k = Math.min(1, (t - t0) / d); n.textContent = Math.round(to * (1 - Math.pow(1 - k, 3))); if (k < 1) requestAnimationFrame(f); })(t0);
    }, { threshold: 0.6 }).observe(n);
  });

  // talk to her: tap a voice note, see it written down and turned into the action (no audio, nothing sent)
  var talk = document.querySelector('[data-talk]');
  if (talk) {
    var notes = JSON.parse(document.getElementById('talk-data').textContent), i = 0, busy = false;
    var btn = talk.querySelector('.tk-note'), said = talk.querySelector('.tk-said'), did = talk.querySelector('.tk-did'), len = talk.querySelector('.tk-len');
    var show = function (n) { said.textContent = n.said; did.textContent = n.did; len.textContent = n.len; };
    btn.addEventListener('click', function () {
      if (busy) return;
      var n = notes[i]; i = (i + 1) % notes.length;
      if (reduce) { show(n); talk.setAttribute('data-step', '2'); return; }
      busy = true; len.textContent = n.len;
      talk.setAttribute('data-step', '0'); btn.classList.add('playing');
      setTimeout(function () { btn.classList.remove('playing'); said.textContent = n.said; did.textContent = ''; talk.setAttribute('data-step', '1'); }, 1600);
      setTimeout(function () { did.textContent = n.did; talk.setAttribute('data-step', '2'); busy = false; }, 2600);
    });
  }

  // glob02: the top bar's text links step aside by measured fit (French or German words need more room than English)
  var nav = document.querySelector('.mk-nav:not(.pub)');
  if (nav) {
    var fit = function () {
      nav.classList.remove('tight');
      var b = nav.querySelector('.b').getBoundingClientRect(), rtl = getComputedStyle(nav).direction === 'rtl';
      var clash = [].some.call(nav.querySelectorAll('.l > *'), function (e) {
        if (!e.offsetParent) return false;
        var r = e.getBoundingClientRect();
        return rtl ? r.right > b.left - 8 : r.left < b.right + 8;    // a text link would touch the brand: they step aside
      });
      if (clash) nav.classList.add('tight');
    };
    fit(); addEventListener('resize', fit);
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(fit);
  }

  // glob02: the 🌐 language picker is a <details> menu that works without this; here it only closes on Escape or a click outside
  var wps = document.querySelectorAll('[data-wp]');
  if (wps.length) {
    document.addEventListener('click', function (e) { [].forEach.call(wps, function (d) { if (d.open && !d.contains(e.target)) d.open = false; }); });
    document.addEventListener('keydown', function (e) {
      if (e.key !== 'Escape') return;
      [].forEach.call(wps, function (d) { if (d.open) { d.open = false; d.querySelector('summary').focus(); } });
    });
  }

  // watch the video: the MP4 of the chosen language, loaded only when asked for (preload none, no autoplay in the hero)
  var w = document.querySelector('[data-watch]'), dlg = document.getElementById('watch');
  if (w && dlg && dlg.showModal) {
    var box = dlg.querySelector('.wv-box');
    w.addEventListener('click', function (e) {
      e.preventDefault();
      var src = w.getAttribute('href');
      box.innerHTML = '';
      var v = document.createElement('video');
      v.controls = true; v.playsInline = true; v.preload = 'none'; v.src = src; v.poster = src.replace(/\.mp4$/, '.jpg');
      box.appendChild(v);
      dlg.showModal();
      var p = v.play(); if (p && p.catch) p.catch(function () {});
    });
    dlg.addEventListener('close', function () { box.innerHTML = ''; });
    dlg.addEventListener('click', function (e) { if (e.target === dlg || e.target.closest('[data-close]')) dlg.close(); });
  }
})();
