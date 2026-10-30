/* v0.18: the app on the home screen. Registers the service worker, offers "Install the app" (Android/desktop: the
   browser's own prompt; iPhone: how to add it from Safari's Share menu), and "Notify me on this device" (Web Push). */
(function(){
  if(!('serviceWorker' in navigator)) return;
  navigator.serviceWorker.register('/sw.js',{scope:'/'}).catch(function(){});
  var bar=document.getElementById('pw-bar'), deferred=null, KEY='vo-install-hide';
  function hidden(){try{return localStorage.getItem(KEY)==='1';}catch(e){return false;}}
  var standalone=window.matchMedia('(display-mode: standalone)').matches||navigator.standalone===true;
  function show(which){if(!bar||hidden()||standalone) return; bar.hidden=false; Array.prototype.forEach.call(bar.querySelectorAll('[data-pw]'),function(x){x.hidden=x.getAttribute('data-pw')!==which;});}
  window.addEventListener('beforeinstallprompt',function(e){e.preventDefault(); deferred=e; show('install');});
  var ios=/iphone|ipad|ipod/i.test(navigator.userAgent)&&!window.MSStream;
  if(ios&&!standalone) show('ios');
  document.addEventListener('click',function(e){
    if(e.target.closest('[data-pw-install]')&&deferred){deferred.prompt(); deferred.userChoice.finally(function(){deferred=null; if(bar) bar.hidden=true;});}
    if(e.target.closest('[data-pw-hide]')){try{localStorage.setItem(KEY,'1');}catch(x){} if(bar) bar.hidden=true;}
    var p=e.target.closest('[data-push]'); if(p) subscribe(p);
  });
  function b64(s){s=s.replace(/-/g,'+').replace(/_/g,'/'); while(s.length%4) s+='='; var r=atob(s), a=new Uint8Array(r.length); for(var i=0;i<r.length;i++) a[i]=r.charCodeAt(i); return a;}
  function say(el,t){var o=el.parentNode.querySelector('[data-push-st]'); if(o) o.textContent=t;}
  function subscribe(btn){
    if(!('PushManager' in window)||!('Notification' in window)){say(btn,btn.getAttribute('data-no')); return;}
    var csrf=btn.getAttribute('data-csrf');
    Notification.requestPermission().then(function(perm){
      if(perm!=='granted'){say(btn,btn.getAttribute('data-no')); return null;}
      return fetch('/app/push/key',{credentials:'same-origin'}).then(function(r){return r.json();}).then(function(j){
        return navigator.serviceWorker.ready.then(function(reg){return reg.pushManager.subscribe({userVisibleOnly:true,applicationServerKey:b64(j.key)});});
      }).then(function(sub){
        return fetch('/app/push/subscribe',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-CSRF':csrf},body:JSON.stringify(sub)});
      }).then(function(r){say(btn,r.ok?btn.getAttribute('data-ok'):btn.getAttribute('data-no')); if(r.ok) btn.classList.add('on');});
    }).catch(function(){say(btn,btn.getAttribute('data-no'));});
  }
})();
