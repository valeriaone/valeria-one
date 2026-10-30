/* v0.18 workflow builder: a vertical list of steps with "+ add step" between them; an "if" holds a Yes list and a No list,
   side by side on a wide screen and stacked on a phone. No dragging: steps move with ↑ ↓. The steps live as JSON in the
   form's hidden "steps" box, which the server checks again before saving. No library. */
(function(){
  var box=document.getElementById('wf-data'), input=document.getElementById('wf-steps'), root=document.getElementById('wf-root');
  if(!box||!input||!root) return;
  var D=JSON.parse(box.textContent), W=D.words||{}, steps=[], open=null, menu=null;
  try{steps=JSON.parse(input.value||'[]');}catch(e){steps=[];}
  function w(k){return W['wf_e_'+k]||k;}
  function f(s,o){return String(s).replace(/\{(\w+)\}/g,function(m,k){return (o[k]===undefined||o[k]===null||o[k]==='')?'…':o[k];});}
  function el(tag,cls,txt){var e=document.createElement(tag); if(cls) e.className=cls; if(txt!==undefined) e.textContent=txt; return e;}
  function key(p){return p.join('.');}
  function find(a,k,v){for(var i=0;i<a.length;i++){if(String(a[i][k])===String(v)) return a[i];} return null;}
  var ICON={message:'✉',tag:'#',untag:'#',field:'✎',assign:'☺',move:'➜',deal:'+',task:'☑',sequence:'⇉',notify:'★',wait:'⧗',wait_until:'☷','if':'?'};
  var ACT=['message','tag','untag','field','assign','move','deal','task','sequence','notify'], FLOW=['wait','wait_until','if'];
  function can(t){
    if(t==='field') return D.fields.length>0;
    if(t==='wait_until') return D.fields.some(function(x){return x.kind==='date';});
    if(t==='sequence') return D.sequences.length>0;
    if(t==='assign') return D.team.length>0;
    return true;
  }
  function fresh(t){
    var dates=D.fields.filter(function(x){return x.kind==='date';});
    return ({message:{t:t,text:'',kind:'service',ask:true},tag:{t:t,tag:''},untag:{t:t,tag:''},
      field:{t:t,key:(D.fields[0]||{}).key,value:''},assign:{t:t,user_id:(D.team[0]||{}).id},
      move:{t:t,stage:(D.stages[0]||{}).key},deal:{t:t,stage:(D.stages[0]||{}).key},task:{t:t,title:'',hours:1},
      sequence:{t:t,sequence_id:(D.sequences[0]||{}).id},notify:{t:t,text:''},wait:{t:t,amount:1,unit:'days'},
      wait_until:{t:t,key:(dates[0]||{}).key,offset:0,yearly:false,at:'09:00'},
      'if':{t:t,cond:{kind:'replied',hours:0},yes:[],no:[]}})[t];
  }
  function cond(c){
    c=c||{}; var fl=find(D.fields,'key',c.key);
    return f(w('c_'+c.kind),{value:c.value,field:fl?fl.label:'',hours:c.hours});
  }
  function say(s){
    var o={}, t=s.t;
    if(t==='message'){o.text=(s.text||'').slice(0,70);}
    if(t==='tag'||t==='untag'){o.tag=s.tag;}
    if(t==='field'){var fl=find(D.fields,'key',s.key); o.field=fl?fl.label:''; o.value=s.value;}
    if(t==='assign'){var m=find(D.team,'id',s.user_id); o.who=m?m.name:'';}
    if(t==='move'||t==='deal'){var st=find(D.stages,'key',s.stage); o.stage=st?st.label:'';}
    if(t==='task'){o.title=s.title; o.hours=s.hours;}
    if(t==='sequence'){var q=find(D.sequences,'id',s.sequence_id); o.name=q?q.name:'';}
    if(t==='notify'){o.text=(s.text||'').slice(0,70);}
    if(t==='wait'){o.n=s.amount; o.unit=w('unit_'+s.unit);}
    if(t==='wait_until'){var d=find(D.fields,'key',s.key); o.field=d?d.label:''; o.offset=s.offset?((s.offset>0?'+':'')+s.offset+' '+w('unit_days')):''; o.yearly=s.yearly?w('yearly'):'';}
    if(t==='if'){o.cond=cond(s.cond);}
    var line=f(w('s_'+t),o);
    if(t==='message'){line+=' · '+(s.kind==='marketing'?w('kind_marketing'):w('kind_service'))+(s.ask!==false?' · '+w('asks_first'):'');}
    return line;
  }
  function field(label,input){var l=el('label','wf-l'); l.appendChild(el('span','',label)); l.appendChild(input); return l;}
  function inp(s,k,type,attrs){
    var i=el(type==='textarea'?'textarea':'input'); if(type!=='textarea') i.type=type||'text';
    i.value=s[k]===undefined||s[k]===null?'':s[k]; i.setAttribute('dir','auto');
    for(var a in (attrs||{})) i.setAttribute(a,attrs[a]);
    i.addEventListener('input',function(){s[k]=(type==='number')?(i.value===''?'':Number(i.value)):i.value; sync(false);});
    return i;
  }
  function sel(s,k,opts,after){
    var x=el('select');
    opts.forEach(function(o){var op=el('option','',o[1]); op.value=o[0]; if(String(s[k])===String(o[0])) op.selected=true; x.appendChild(op);});
    if(s[k]===undefined&&opts.length) s[k]=opts[0][0];
    x.addEventListener('change',function(){s[k]=x.value; sync(false); if(after) after();});
    return x;
  }
  function chk(s,k,label){
    var l=el('label','chk'), c=el('input'); c.type='checkbox'; c.checked=!!s[k];
    c.addEventListener('change',function(){s[k]=c.checked; sync(false);}); l.appendChild(c); l.appendChild(document.createTextNode(' '+label)); return l;
  }
  function editor(s){
    var e=el('div','wf-edit'), t=s.t;
    var stages=D.stages.map(function(x){return [x.key,x.label];}), flds=D.fields.map(function(x){return [x.key,x.label];});
    if(t==='message'){
      e.appendChild(field(w('text'),inp(s,'text','textarea',{maxlength:1000,rows:3})));
      e.appendChild(el('p','small muted',w('names_hint')));
      e.appendChild(field(w('kind'),sel(s,'kind',[['service',w('kind_service')],['marketing',w('kind_marketing')]])));
      if(s.ask===undefined) s.ask=true; e.appendChild(chk(s,'ask',w('ask')));
    }
    if(t==='tag'||t==='untag') e.appendChild(field(w('tag'),inp(s,'tag','text',{maxlength:60})));
    if(t==='field'){e.appendChild(field(w('field'),sel(s,'key',flds))); e.appendChild(field(w('value'),inp(s,'value','text',{maxlength:200})));}
    if(t==='assign') e.appendChild(field(w('person'),sel(s,'user_id',D.team.map(function(x){return [x.id,x.name];}))));
    if(t==='move'||t==='deal') e.appendChild(field(w('stage'),sel(s,'stage',stages)));
    if(t==='task'){e.appendChild(field(w('title'),inp(s,'title','text',{maxlength:200}))); e.appendChild(field(w('hours'),inp(s,'hours','number',{min:0,max:720})));}
    if(t==='sequence') e.appendChild(field(w('sequence'),sel(s,'sequence_id',D.sequences.map(function(x){return [x.id,x.name];}))));
    if(t==='notify') e.appendChild(field(w('text'),inp(s,'text','textarea',{maxlength:500,rows:2})));
    if(t==='wait'){var r=el('div','row'); r.appendChild(inp(s,'amount','number',{min:1,max:525600,'aria-label':w('amount')}));
      r.appendChild(sel(s,'unit',D.units.map(function(u){return [u,w('unit_'+u)];}))); e.appendChild(field(w('amount'),r));}
    if(t==='wait_until'){
      e.appendChild(field(w('field'),sel(s,'key',D.fields.filter(function(x){return x.kind==='date';}).map(function(x){return [x.key,x.label];}))));
      e.appendChild(field(w('offset'),inp(s,'offset','number',{min:-365,max:365})));
      e.appendChild(field(w('at'),inp(s,'at','time',{})));
      e.appendChild(chk(s,'yearly',w('yearly')));
    }
    if(t==='if'){
      var c=s.cond=s.cond||{kind:'replied',hours:0}, extra=el('div');
      var redo=function(){extra.innerHTML='';
        if(c.kind==='tag'||c.kind==='source') extra.appendChild(field(w('value'),inp(c,'value','text',{maxlength:60})));
        if(c.kind==='field'){if(!c.key&&D.fields[0]) c.key=D.fields[0].key; extra.appendChild(field(w('field'),sel(c,'key',flds))); extra.appendChild(field(w('value'),inp(c,'value','text',{maxlength:60})));}
        if(c.kind==='channel') extra.appendChild(field(w('channel'),sel(c,'value',['whatsapp','sms','email','messenger','instagram','telegram','viber'].map(function(x){return [x,x];}))));
        if(c.kind==='replied') extra.appendChild(field(w('replied_hours'),inp(c,'hours','number',{min:0,max:2160})));
      };
      e.appendChild(field(w('check'),sel(c,'kind',D.conds.filter(function(k){return k!=='field'||D.fields.length;}).map(function(k){return [k,w('k_'+k)];}),function(){redo(); sync(true);})));
      e.appendChild(extra); redo();
    }
    var done=el('button','btn primary fit',w('done')); done.type='button';
    done.addEventListener('click',function(){open=null; sync(true);}); e.appendChild(done);
    return e;
  }
  function addBtn(arr,at){
    var li=el('li','wf-add'), b=el('button','wf-plus','+ '+w('add')); b.type='button';
    b.addEventListener('click',function(){
      if(menu){menu.remove(); if(menu.parentNode===null&&menu._for===li){menu=null; return;}}
      menu=el('div','wf-menu'); menu._for=li;
      [ACT,FLOW].forEach(function(group){
        var g=el('div','wf-menu-g');
        group.forEach(function(t){
          var c=el('button','chip',(ICON[t]||'')+' '+w('t_'+t)); c.type='button'; if(!can(t)){c.disabled=true; c.title=w('needs_'+t);}
          c.addEventListener('click',function(){arr.splice(at,0,fresh(t)); menu=null; open=null; sync(true); focusLast(arr,at);});
          g.appendChild(c);
        });
        menu.appendChild(g);
      });
      li.appendChild(menu);
    });
    li.appendChild(b); return li;
  }
  var pendingOpen=null;
  function focusLast(arr,at){pendingOpen=arr[at];}
  function list(arr,path){
    var ol=el('ol','wf-list');
    ol.appendChild(addBtn(arr,0));
    arr.forEach(function(s,i){ol.appendChild(step(s,path.concat([i]),arr,i)); ol.appendChild(addBtn(arr,i+1));});
    return ol;
  }
  function tool(label,fn,cls){var b=el('button','chip '+(cls||''),label); b.type='button'; b.setAttribute('aria-label',label); b.title=label; b.addEventListener('click',fn); return b;}
  function step(s,p,arr,i){
    var li=el('li','wf-step wf-'+s.t), card=el('div','wf-card'), head=el('div','wf-head');
    if(pendingOpen===s){open=key(p); pendingOpen=null;}
    head.appendChild(el('span','wf-ico',ICON[s.t]||'•'));
    var txt=el('button','wf-say',say(s)); txt.type='button'; txt.setAttribute('dir','auto');
    txt.addEventListener('click',function(){open=(open===key(p))?null:key(p); sync(true);});
    head.appendChild(txt);
    var tools=el('span','wf-tools');
    if(i>0) tools.appendChild(tool('↑',function(){arr.splice(i-1,0,arr.splice(i,1)[0]); open=null; sync(true);}));
    if(i<arr.length-1) tools.appendChild(tool('↓',function(){arr.splice(i+1,0,arr.splice(i,1)[0]); open=null; sync(true);}));
    tools.appendChild(tool('✕',function(){if(confirm(w('delete_q'))){arr.splice(i,1); open=null; sync(true);}},'danger'));
    head.appendChild(tools); card.appendChild(head);
    if(open===key(p)) card.appendChild(editor(s));
    li.appendChild(card);
    if(s.t==='if'){
      var br=el('div','wf-branches');
      ['yes','no'].forEach(function(b){var col=el('div','wf-branch wf-'+b); col.appendChild(el('div','wf-blabel',w(b)));
        s[b]=s[b]||[]; if(!s[b].length) col.appendChild(el('p','small muted',w('empty_branch'))); col.appendChild(list(s[b],p.concat([b]))); br.appendChild(col);});
      li.appendChild(br);
    }
    return li;
  }
  function sync(redraw){
    input.value=JSON.stringify(steps);
    if(redraw){var y=window.scrollY; root.innerHTML=''; if(!steps.length) root.appendChild(el('p','muted wf-empty',w('empty'))); root.appendChild(list(steps,[])); window.scrollTo(0,y);}
  }
  document.getElementById('wf-form').addEventListener('submit',function(){input.value=JSON.stringify(steps);});
  sync(true);
})();
