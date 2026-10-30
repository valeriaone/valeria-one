(function(){
  var KEY='vo-theme', root=document.documentElement;
  try{var s=localStorage.getItem(KEY); if(s) root.setAttribute('data-theme',s);}catch(e){}
  function isDark(){var th=root.getAttribute('data-theme'),def=root.getAttribute('data-def');   // data-def: what "no choice yet" looks like (the public pages: dark)
    return th==='dark'||(!th&&(def?def==='dark':matchMedia('(prefers-color-scheme: dark)').matches));}
  function pressed(){var d=isDark(); Array.prototype.forEach.call(document.querySelectorAll('.vsw[data-mode]'),function(b){b.setAttribute('aria-pressed',d?'true':'false');});}
  pressed();                                                  // mon01: one button; aria-pressed = dark mode is on
  document.addEventListener('click',function(e){
    var m=e.target.closest('[data-mode]');
    if(m){var next=isDark()?'light':'dark'; root.setAttribute('data-theme',next); try{localStorage.setItem(KEY,next);}catch(e){} pressed();}
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
  /* mon01: boxes that only matter for one choice (data-when="trigger=deal.stage" or "a0_do=tag,task") */
  function when(){
    Array.prototype.forEach.call(document.querySelectorAll('[data-when]'),function(el){
      var p=el.getAttribute('data-when').split('='), f=el.closest('form'), s=f&&f.querySelector('[name="'+p[0]+'"]');
      if(!s) return; var ok=p[1].split(',').indexOf(s.value)>=0; el.hidden=!ok;
      Array.prototype.forEach.call(el.querySelectorAll('input,select,textarea'),function(i){i.disabled=!ok;});
    });
  }
  document.addEventListener('change',function(e){ if(e.target.tagName==='SELECT') when(); });
  when();
  /* mon01: the owner's own fields save by themselves when they change (the Save button does the same without JS) */
  document.addEventListener('change',function(e){
    var el=e.target, f=el.form; if(!f||!f.hasAttribute('data-inline')||(el.name||'').indexOf('cf__')!==0) return;
    var row=el.closest('.cf-row'), st=row&&row.querySelector('.cf-st'), body=new FormData();
    body.append('csrf',f.querySelector('[name=csrf]').value); body.append(el.name,el.value);
    if(st){st.textContent='…'; st.className='cf-st small muted';}
    fetch(f.action,{method:'POST',body:body,credentials:'same-origin',headers:{'X-Requested-With':'fetch'}})
      .then(function(r){return r.json();})
      .then(function(j){
        if(st){st.textContent=j.ok?'✓ '+(j.saved||''):(j.error||'!'); st.className='cf-st small '+(j.ok?'ok':'bad');}
        if(j.ok&&j.values){var k=el.name.slice(4); el.value=(k in j.values)?j.values[k]:'';}
      })
      .catch(function(){ if(st){st.textContent='!'; st.className='cf-st small bad';} });
  });
  document.addEventListener('click',function(e){            /* ghl01: a saved reply, one tap into the reply box */
    var b=e.target.closest('[data-insert]'); if(!b) return;
    var ta=b.form&&b.form.querySelector('textarea[name=body]'); if(!ta) return;
    var v=b.getAttribute('data-insert'), s=ta.selectionStart||0, en=ta.selectionEnd||0;
    ta.value=ta.value?ta.value.slice(0,s)+v+ta.value.slice(en):v; ta.focus();
  });
  var picker=document.querySelector('select[data-channel-picker]'); if(picker) picker.dispatchEvent(new Event('change',{bubbles:true}));
  var msgs=document.querySelector('.msgs'); if(msgs) msgs.scrollTop=msgs.scrollHeight;
})();
