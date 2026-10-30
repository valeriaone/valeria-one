/* ars01 (v0.20): the register's cart. It only adds up for the screen — the server prices every item again from its own
   record and does the real sum (same rules: integers in the smallest unit, tax half up), so what is charged never
   depends on this page. */
(function(){
  var form=document.getElementById('ps-form'); if(!form) return;
  var cur=form.getAttribute('data-cur'), loc=form.getAttribute('data-loc'), digits=+form.getAttribute('data-digits');
  var bp=+form.getAttribute('data-tax-bp'), incl=form.getAttribute('data-tax-in')==='1';
  var lines=[], $=function(id){return document.getElementById(id);};
  var nf; try{nf=new Intl.NumberFormat(loc,{style:'currency',currency:cur});}catch(e){nf=new Intl.NumberFormat('en',{style:'currency',currency:cur});}
  function fmt(m){return nf.format(m/Math.pow(10,digits));}
  function minor(s){s=String(s||'').replace(/,/g,'').trim(); if(!s) return null; var v=Number(s); if(!isFinite(v)||v<0) return null; return Math.round(v*Math.pow(10,digits));}
  function halfUp(n,d){return Math.floor((2*n+d)/(2*d));}                 // n, d >= 0: round half up, like the server
  function esc(s){var d=document.createElement('div'); d.textContent=s; return d.innerHTML;}
  function sums(){
    var sub=0; lines.forEach(function(l){sub+=l.qty*l.unit;});
    var dv=$('ps-disc').value, disc=0;
    if($('ps-dk').value==='pct'){var p=Number(String(dv).replace(',','.'))||0; disc=halfUp(sub*Math.round(p*100),10000);} else {disc=minor(dv)||0;}
    if(disc>sub) disc=sub;
    var base=sub-disc, tax, tot;
    if(incl){var net=halfUp(base*10000,10000+bp); tax=base-net; tot=base;} else {tax=halfUp(base*bp,10000); tot=base+tax;}
    return {sub:sub,disc:disc,tax:tax,tot:tot};
  }
  function draw(){
    var ul=$('ps-lines'), hid=$('ps-hidden'); ul.innerHTML=''; hid.innerHTML='';
    if(!lines.length){ul.innerHTML='<li class="muted small ps-empty">'+esc(form.getAttribute('data-empty')||'')+'</li>';}
    lines.forEach(function(l,i){
      var li=document.createElement('li');
      li.innerHTML='<span class="ps-ln"><b dir="auto">'+esc(l.name)+'</b><br><span class="small muted">'+fmt(l.unit)+'</span></span>'+
        '<span class="ps-qty"><button type="button" class="chip" data-i="'+i+'" data-d="-1" aria-label="−">−</button><b>'+l.qty+'</b>'+
        '<button type="button" class="chip" data-i="'+i+'" data-d="1" aria-label="+">+</button></span><b class="ps-amt">'+fmt(l.qty*l.unit)+'</b>';
      ul.appendChild(li);
      [['line_item',l.id||''],['line_desc',l.id?'':l.name],['line_qty',l.qty],['line_unit',l.id?'':l.raw]].forEach(function(p){
        var x=document.createElement('input'); x.type='hidden'; x.name=p[0]; x.value=p[1]; hid.appendChild(x);});
    });
    var s=sums();
    $('ps-sub').textContent=fmt(s.sub); $('ps-dsc').textContent=s.disc?'− '+fmt(s.disc):'—'; $('ps-tax').textContent=fmt(s.tax);
    $('ps-tot').textContent=fmt(s.tot); $('ps-tot2').textContent=lines.length?fmt(s.tot):'';
    $('ps-charge').disabled=!lines.length;
    var g=minor($('ps-given').value); $('ps-change').textContent=(g===null)?'—':(g>=s.tot?fmt(g-s.tot):'− '+fmt(s.tot-g));
    $('ps-change').classList.toggle('bad', g!==null&&g<s.tot);
    var cash=(form.querySelector('input[name=method]:checked')||{}).value==='cash'; $('ps-cashbox').hidden=!cash;
  }
  form.setAttribute('data-empty', ($('ps-lines').textContent||'').trim());
  document.addEventListener('click',function(e){
    var it=e.target.closest('.ps-item');
    if(it){var id=it.getAttribute('data-id'), f=null; lines.forEach(function(l){if(l.id===id) f=l;});
      if(f) f.qty+=1; else lines.push({id:id,name:it.getAttribute('data-name'),unit:+it.getAttribute('data-price'),qty:1});
      draw(); return;}
    var q=e.target.closest('[data-d]');
    if(q){var l=lines[+q.getAttribute('data-i')]; l.qty+=+q.getAttribute('data-d'); if(l.qty<1) lines.splice(+q.getAttribute('data-i'),1); draw(); return;}
    if(e.target.closest('#ps-cadd')){var raw=$('ps-camt').value, m=minor(raw); if(!m){$('ps-camt').focus(); return;}
      lines.push({id:null,name:$('ps-cdesc').value.trim()||$('ps-camt').getAttribute('placeholder'),unit:m,raw:raw.replace(/,/g,'').trim(),qty:1});
      $('ps-cdesc').value=''; $('ps-camt').value=''; draw();}
  });
  $('ps-q').addEventListener('input',function(){var v=this.value.toLowerCase().trim();
    Array.prototype.forEach.call(document.querySelectorAll('.ps-item'),function(b){b.hidden=v&&b.getAttribute('data-find').indexOf(v)<0;});});
  ['ps-disc','ps-dk','ps-given'].forEach(function(id){$(id).addEventListener('input',draw); $(id).addEventListener('change',draw);});
  Array.prototype.forEach.call(form.querySelectorAll('input[name=method]'),function(r){r.addEventListener('change',draw);});
  form.addEventListener('submit',function(){$('ps-charge').disabled=true;});            // one tap = one sale
  draw();
})();
