/* One-click Meta connection: Meta's own pop-ups (Embedded Signup for WhatsApp, Login for Business for
   Pages + Instagram). The browser only relays the one-time code to our server. */
(function () {
  var box = document.getElementById('meta-connect');
  if (!box) return;
  var waInfo = {};
  window.addEventListener('message', function (ev) {
    if (!/(^|\.)facebook\.com$/.test(new URL(ev.origin).hostname)) return;
    try {
      var d = typeof ev.data === 'string' ? JSON.parse(ev.data) : ev.data;
      if (d && d.type === 'WA_EMBEDDED_SIGNUP' && d.event && d.event.indexOf('FINISH') === 0) {
        waInfo = { waba_id: d.data.waba_id, phone_number_id: d.data.phone_number_id };
        tryWa();
      }
    } catch (e) {}
  });
  var waCode = null;
  function tryWa() {
    if (!waCode || !waInfo.waba_id) return;
    var f = document.getElementById('meta-wa-form');
    f.code.value = waCode; f.waba_id.value = waInfo.waba_id; f.phone_number_id.value = waInfo.phone_number_id;
    f.submit();
  }
  window.fbAsyncInit = function () {
    FB.init({ appId: box.dataset.app, autoLogAppEvents: true, xfbml: false, version: box.dataset.v });
  };
  var s = document.createElement('script');
  s.src = 'https://connect.facebook.net/en_US/sdk.js'; s.async = true; s.defer = true; s.crossOrigin = 'anonymous';
  document.head.appendChild(s);
  box.addEventListener('click', function (e) {
    var b = e.target.closest('[data-meta]'); if (!b || !window.FB) return;
    if (b.dataset.meta === 'wa') {
      FB.login(function (r) { if (r.authResponse && r.authResponse.code) { waCode = r.authResponse.code; tryWa(); } },
        { config_id: box.dataset.wa, response_type: 'code', override_default_response_type: true,
          extras: { setup: {}, featureType: '', sessionInfoVersion: '3' } });
    } else {
      FB.login(function (r) {
        if (r.authResponse && r.authResponse.code) {
          var f = document.getElementById('meta-pages-form'); f.code.value = r.authResponse.code; f.submit();
        }
      }, { config_id: box.dataset.login, response_type: 'code', override_default_response_type: true });
    }
  });
})();
