/* Valeria One · lead27: one lead's page — cards into the message, the owner's own voice (recorded here), Valeria's voice */
(function(){
  var d=document, body=d.getElementById('ld-body');
  d.addEventListener('click',function(e){                     /* a card: its line goes where the cursor is */
    var b=e.target.closest('[data-insert]'); if(!b||!body) return;
    var v=b.getAttribute('data-insert'), s=body.selectionStart||body.value.length;
    body.value=body.value.slice(0,s)+(s&&body.value[s-1]!=='\n'?'\n':'')+v+body.value.slice(s); body.focus();
  });
  d.addEventListener('submit',function(e){                    /* "open in my app" carries what is in the box now */
    var f=e.target; if(!f.hasAttribute||!f.hasAttribute('data-copy-body')||!body) return;
    var h=f.querySelector('[name=body]'); if(h) h.value=body.value;
  });
  var box=d.querySelector('[data-voice]'); if(!box) return;
  var state=box.querySelector('[data-voice-state]'), play=box.querySelector('[data-voice-play]'), vid=d.getElementById('ld-voice-id');
  var rec=null, chunks=[], btn=box.querySelector('[data-rec-btn]'), speak=box.querySelector('[data-speak-btn]');
  var share=box.querySelector('[data-share-btn]'), shareNote=d.querySelector('[data-share-note]');
  function say(t){ state.textContent=t||''; }
  function ready(){ if(share){ share.hidden=false; } if(shareNote){ shareNote.hidden=false; } }
  function mark(how){                                         /* on record: sent by the owner, from their own WhatsApp */
    var fd=new FormData(); fd.append('csrf',box.getAttribute('data-csrf')); fd.append('how',how); fd.append('do',share.getAttribute('data-do'));
    var x=new XMLHttpRequest(); x.open('POST',share.getAttribute('data-shared')); x.send(fd);
  }
  if(share) share.addEventListener('click',function(){        /* the voice note from THEIR number: the phone's share sheet */
    if(!play.src) return;
    fetch(play.src).then(function(r){ return r.blob(); }).then(function(b){
      var ext=(b.type.indexOf('mp4')>=0||b.type.indexOf('m4a')>=0)?'m4a':(b.type.indexOf('wav')>=0?'wav':(b.type.indexOf('mpeg')>=0?'mp3':'ogg'));
      var file=new File([b],'voice.'+ext,{type:b.type||'audio/ogg'});
      if(navigator.canShare&&navigator.share&&navigator.canShare({files:[file]})){
        navigator.share({files:[file],text:body?body.value:''}).then(function(){ mark('share'); }).catch(function(){});
      } else {                                                  /* a computer: the file downloads, the chat opens */
        var a=d.createElement('a'); a.href=URL.createObjectURL(b); a.download=file.name; d.body.appendChild(a); a.click(); a.remove();
        window.open(share.getAttribute('data-wa')+(body&&body.value?('?text='+encodeURIComponent(body.value)):''),'_blank','noopener');
        mark('download');
      }
    }).catch(function(){ say(box.getAttribute('data-err')); });
  });
  function got(j){
    if(j.error){ say(box.getAttribute('data-err')+' '+j.error); return; }
    vid.value=j.id; play.src=j.url; play.hidden=false; ready();
    if(j.transcript&&body&&!body.value.trim()) body.value=j.transcript;
    else if(j.transcript&&body) body.value=body.value+'\n'+j.transcript;
    say(j.why==='no_key'?box.getAttribute('data-nokey'):(j.text?box.getAttribute('data-spoke'):box.getAttribute('data-ok')));
  }
  function post(url, fd){
    fd.append('csrf',box.getAttribute('data-csrf'));
    var x=new XMLHttpRequest(); x.open('POST',url);
    x.onload=function(){ var j={}; try{ j=JSON.parse(x.responseText||'{}'); }catch(err){ j={error:String(x.status)}; } got(j); };
    x.onerror=function(){ say(box.getAttribute('data-err')); };
    x.send(fd);
  }
  function pick(){
    var c=['audio/mp4','audio/ogg;codecs=opus','audio/webm;codecs=opus','audio/webm'];
    for(var i=0;i<c.length;i++){ if(window.MediaRecorder&&MediaRecorder.isTypeSupported&&MediaRecorder.isTypeSupported(c[i])) return c[i]; }
    return '';
  }
  if(btn) btn.addEventListener('click',function(){
    if(rec&&rec.state==='recording'){ rec.stop(); return; }
    if(!navigator.mediaDevices||!window.MediaRecorder){ say(box.getAttribute('data-err')); return; }
    navigator.mediaDevices.getUserMedia({audio:true}).then(function(stream){
      var type=pick(); chunks=[]; rec=type?new MediaRecorder(stream,{mimeType:type}):new MediaRecorder(stream);
      rec.ondataavailable=function(ev){ if(ev.data&&ev.data.size) chunks.push(ev.data); };
      rec.onstop=function(){
        stream.getTracks().forEach(function(t){ t.stop(); });
        btn.textContent='🎙 '+box.getAttribute('data-rec'); say(box.getAttribute('data-wait'));
        var mime=(rec.mimeType||type||'audio/webm').split(';')[0], blob=new Blob(chunks,{type:mime}), fd=new FormData();
        fd.append('audio',blob,'voice.'+(mime.indexOf('mp4')>=0?'m4a':(mime.indexOf('ogg')>=0?'ogg':'webm')));
        post(box.getAttribute('data-url'),fd);
      };
      rec.start(); btn.textContent='■ '+box.getAttribute('data-stop'); say('…');
    }).catch(function(){ say(box.getAttribute('data-err')); });
  });
  if(speak) speak.addEventListener('click',function(){
    var fd=new FormData(); fd.append('text',body?body.value:''); say(box.getAttribute('data-wait'));
    var url=box.getAttribute('data-speak'), x=new XMLHttpRequest(); fd.append('csrf',box.getAttribute('data-csrf')); x.open('POST',url);
    x.onload=function(){ var j={}; try{ j=JSON.parse(x.responseText||'{}'); }catch(err){ j={error:String(x.status)}; }
      if(j.error){ say(box.getAttribute('data-err')+' '+j.error); return; }
      vid.value=j.id; play.src=j.url; play.hidden=false; ready(); say(box.getAttribute('data-spoke')); };
    x.onerror=function(){ say(box.getAttribute('data-err')); }; x.send(fd);
  });
})();
