(function(){
  var KEY='vo-theme', root=document.documentElement;
  try{var s=localStorage.getItem(KEY); if(s) root.setAttribute('data-theme',s);}catch(e){}
  document.addEventListener('click',function(e){
    var m=e.target.closest('[data-mode]');
    if(m){var dark=root.getAttribute('data-theme')==='dark'||(!root.getAttribute('data-theme')&&matchMedia('(prefers-color-scheme: dark)').matches);
      var next=dark?'light':'dark'; root.setAttribute('data-theme',next); try{localStorage.setItem(KEY,next);}catch(e){} }
    var t=e.target.closest('[data-menu]'); if(t){document.querySelector('.side').classList.toggle('open');}
    if(e.target.classList&&e.target.classList.contains('side')){e.target.classList.remove('open');}
  });
  document.addEventListener('submit',function(e){
    var q=e.target.getAttribute('data-confirm'); if(q&&!confirm(q)) e.preventDefault();
  });
  document.addEventListener('change',function(e){
    if(e.target.matches('select[data-autosubmit]')) e.target.form.submit();
    if(e.target.matches('select[data-channel-picker]')){
      var v=e.target.value; document.querySelectorAll('[data-for-channel]').forEach(function(x){x.hidden=x.getAttribute('data-for-channel')!==v; x.querySelectorAll('input').forEach(function(i){i.disabled=x.hidden;});});
    }
  });
  var picker=document.querySelector('select[data-channel-picker]'); if(picker) picker.dispatchEvent(new Event('change',{bubbles:true}));
  var msgs=document.querySelector('.msgs'); if(msgs) msgs.scrollTop=msgs.scrollHeight;
})();
