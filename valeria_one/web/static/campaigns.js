/* v0.18 campaigns + sequences: the email's blocks (add, move up, remove) and the SMS counter. Without JS the form still
   works: the blocks shown are saved as they are, and the count is shown after saving. */
(function(){
  var GSM="@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà",
      EXT="^{}\\[~]|€\f";
  function segments(t){
    var gsm=true,n=0,i,c;
    for(i=0;i<t.length;i++){c=t.charAt(i); if(GSM.indexOf(c)<0&&EXT.indexOf(c)<0){gsm=false;break;}}
    if(gsm){for(i=0;i<t.length;i++){n+=EXT.indexOf(t.charAt(i))>=0?2:1;} return {chars:n,segments:n?(n<=160?1:Math.ceil(n/153)):0,encoding:'GSM-7'};}
    n=t.length; return {chars:n,segments:n?(n<=70?1:Math.ceil(n/67)):0,encoding:'Unicode'};
  }
  function count(ta){
    var out=document.querySelector(ta.getAttribute('data-sms-count')); if(!out) return;
    var s=segments(ta.value), tpl=out.getAttribute('data-tpl')||'{chars} · {segments}';
    out.textContent=tpl.replace('{chars}',s.chars).replace('{segments}',s.segments).replace('{encoding}',s.encoding);
  }
  document.addEventListener('input',function(e){ if(e.target.matches('[data-sms-count]')) count(e.target); });
  Array.prototype.forEach.call(document.querySelectorAll('[data-sms-count]'),count);
  function kind(blk){                                     /* only the boxes a block needs */
    var t=blk.querySelector('[name=blk_t]').value, url=blk.querySelector('[name=blk_url]');
    url.hidden=!(t==='image'||t==='button');
  }
  Array.prototype.forEach.call(document.querySelectorAll('[data-blk]'),kind);
  document.addEventListener('change',function(e){ var b=e.target.closest('[data-blk]'); if(b&&e.target.name==='blk_t') kind(b); });
  document.addEventListener('click',function(e){
    var add=e.target.closest('[data-blk-add]'), up=e.target.closest('[data-blk-up]'), rm=e.target.closest('[data-blk-rm]');
    if(add){
      var tpl=document.getElementById('cg-blk-tpl'), box=document.querySelector('[data-blocks]');
      if(tpl&&box){box.appendChild(tpl.content.cloneNode(true)); var all=box.querySelectorAll('[data-blk]'); kind(all[all.length-1]); all[all.length-1].querySelector('textarea').focus();}
    }
    if(up){var b=up.closest('[data-blk]'); if(b&&b.previousElementSibling) b.parentNode.insertBefore(b,b.previousElementSibling);}
    if(rm){var r=rm.closest('[data-blk]'); if(r&&r.parentNode.querySelectorAll('[data-blk]').length>1) r.remove();}
    var sadd=e.target.closest('[data-step-add]');               /* sequences: one more message */
    if(sadd){var st=document.getElementById('sq-step-tpl'), list=document.querySelector('[data-steps]');
      if(st&&list){list.appendChild(st.content.cloneNode(true)); chan(list.lastElementChild); renumber();}}
    var srm=e.target.closest('[data-step-rm]');
    if(srm){var s=srm.closest('[data-step]'); if(s&&s.parentNode.querySelectorAll('[data-step]').length>1){s.remove(); renumber();}}
  });
  function renumber(){Array.prototype.forEach.call(document.querySelectorAll('[data-step] [data-n]'),function(el,i){el.textContent=String(i+1);});}
  function chan(step){                                    /* sequences: the subject only for email */
    var c=step.querySelector('[name=st_channel]'), s=step.querySelector('[data-only-email]'), t=step.querySelector('[data-only-wa]');
    if(c&&s) s.hidden=c.value!=='email'; if(c&&t) t.hidden=c.value!=='whatsapp';
  }
  Array.prototype.forEach.call(document.querySelectorAll('[data-step]'),chan); renumber();
  document.addEventListener('change',function(e){ var s=e.target.closest('[data-step]'); if(s&&e.target.name==='st_channel') chan(s); });
})();
