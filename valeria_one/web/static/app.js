(function(){
  var KEY='vo-theme', root=document.documentElement;
  try{var s=localStorage.getItem(KEY); if(s) root.setAttribute('data-theme',s);}catch(e){}
  document.addEventListener('click',function(e){
    var m=e.target.closest('[data-mode]');
    if(m){var th=root.getAttribute('data-theme'),def=root.getAttribute('data-def');   // data-def: what "no choice yet" looks like (the public pages: dark)
      var dark=th==='dark'||(!th&&(def?def==='dark':matchMedia('(prefers-color-scheme: dark)').matches));
      var next=dark?'light':'dark'; root.setAttribute('data-theme',next); try{localStorage.setItem(KEY,next);}catch(e){} }
    var t=e.target.closest('[data-menu]'); if(t){document.querySelector('.side').classList.toggle('open');}
    if(e.target.classList&&e.target.classList.contains('side')){e.target.classList.remove('open');}
  });
  document.addEventListener('submit',function(e){
    var q=e.target.getAttribute('data-confirm'); if(q&&!confirm(q)) e.preventDefault();
  });
  document.addEventListener('change',function(e){
    if(e.target.matches('select[data-autosubmit]')) e.target.form.submit();
    if(e.target.matches('select[data-price-for]')){          // the launch offer shows the chosen country's price
      var o=document.getElementById(e.target.getAttribute('data-price-for')), opt=e.target.selectedOptions[0];
      if(o) o.textContent=o.getAttribute('data-tpl').replace('{price}', (opt&&opt.getAttribute('data-price'))||o.getAttribute('data-usd'));
    }
    if(e.target.matches('select[data-channel-picker]')){
      var v=e.target.value; document.querySelectorAll('[data-for-channel]').forEach(function(x){x.hidden=x.getAttribute('data-for-channel')!==v; x.querySelectorAll('input').forEach(function(i){i.disabled=x.hidden;});});
    }
  });
  document.addEventListener('click',function(e){            /* copy a link */
    var b=e.target.closest('[data-copy]'); if(!b) return;
    var el=document.querySelector(b.getAttribute('data-copy')); if(!el||!navigator.clipboard) return;
    navigator.clipboard.writeText(el.textContent.trim()).then(function(){ b.classList.add('ok'); });
  });
  document.addEventListener('click',function(e){            /* email presets: the provider's SMTP server, port 587 */
    var b=e.target.closest('[data-smtp]'); if(!b) return;
    var box=b.closest('[data-for-channel]'); var h=box.querySelector('[name=cfg_smtp_host]'), p=box.querySelector('[name=cfg_smtp_port]');
    if(h) h.value=b.getAttribute('data-smtp'); if(p) p.value='587';
    var u=box.querySelector('[name=cfg_username]'); if(u) u.focus();
  });
  var picker=document.querySelector('select[data-channel-picker]'); if(picker) picker.dispatchEvent(new Event('change',{bubbles:true}));
  var msgs=document.querySelector('.msgs'); if(msgs) msgs.scrollTop=msgs.scrollHeight;
})();
