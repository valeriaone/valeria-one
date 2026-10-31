/* ctr01 (v0.24): the signature pad on a contract's signing page — finger, pen or mouse (pointer events), on white
   paper; "Clear" starts again; on Sign the drawing goes in the form as a PNG, and an empty pad is not sent. */
(function () {
  'use strict';
  var form = document.getElementById('ct-sign');
  var pad = document.getElementById('sg-pad');
  if (!form || !pad || !pad.getContext) { return; }
  var ctx = pad.getContext('2d');
  var drawn = false, down = false, last = null;

  function size() {
    var r = pad.getBoundingClientRect();
    var dpr = Math.min(window.devicePixelRatio || 1, 2.5);
    var w = Math.max(200, Math.round(r.width * dpr)), h = Math.max(100, Math.round(r.height * dpr));
    if (pad.width === w && pad.height === h) { return; }
    var keep = drawn ? pad.toDataURL('image/png') : null;
    pad.width = w; pad.height = h;
    paper();
    if (keep) {
      var img = new Image();
      img.onload = function () { ctx.drawImage(img, 0, 0, w, h); };
      img.src = keep;
    }
  }
  function paper() {
    ctx.fillStyle = '#ffffff';
    ctx.fillRect(0, 0, pad.width, pad.height);
    ctx.strokeStyle = '#111111';
    ctx.lineCap = 'round'; ctx.lineJoin = 'round';
    ctx.lineWidth = Math.max(2, pad.width / 220);
  }
  function at(e) {
    var r = pad.getBoundingClientRect();
    return { x: (e.clientX - r.left) * (pad.width / r.width), y: (e.clientY - r.top) * (pad.height / r.height) };
  }
  pad.addEventListener('pointerdown', function (e) {
    e.preventDefault();
    down = true; last = at(e);
    try { pad.setPointerCapture(e.pointerId); } catch (err) { /* older browsers */ }
    ctx.beginPath(); ctx.arc(last.x, last.y, ctx.lineWidth / 2, 0, Math.PI * 2); ctx.fillStyle = '#111111'; ctx.fill();
    drawn = true;
  });
  pad.addEventListener('pointermove', function (e) {
    if (!down) { return; }
    e.preventDefault();
    var p = at(e);
    ctx.beginPath(); ctx.moveTo(last.x, last.y); ctx.lineTo(p.x, p.y); ctx.stroke();
    last = p; drawn = true;
  });
  function up() { down = false; last = null; }
  pad.addEventListener('pointerup', up);
  pad.addEventListener('pointercancel', up);
  pad.addEventListener('pointerleave', up);

  var clear = form.querySelector('[data-sig-clear]');
  if (clear) {
    clear.addEventListener('click', function () { drawn = false; paper(); });
  }
  form.addEventListener('submit', function (e) {
    if (!drawn) {
      e.preventDefault();
      window.alert(form.getAttribute('data-empty') || '');
      return;
    }
    form.querySelector('input[name="signature"]').value = pad.toDataURL('image/png');
  });
  window.addEventListener('resize', size);
  size();
}());
