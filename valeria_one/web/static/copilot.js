/* Valeria inside the office and the studio: chat, look things up, propose actions the person confirms. */
(function(){
  var root = document.getElementById('cp'); if(!root) return;
  var $ = function(s){ return root.querySelector(s); }, M = $('.cp-m'), KEY = 'vo-cp';
  var turns = []; try { turns = JSON.parse(sessionStorage.getItem(KEY) || '[]'); } catch(e) {}
  function save(){ try { sessionStorage.setItem(KEY, JSON.stringify(turns.slice(-24))); } catch(e) {} }
  function text(t){                                              // plain text with https:// and /app/… links made clickable
    var d = document.createElement('div'), re = /(https:\/\/[^\s)]+|\/app\/[^\s)]+)/g, last = 0, m;
    while((m = re.exec(t))){ d.appendChild(document.createTextNode(t.slice(last, m.index)));
      var a = document.createElement('a'); a.href = m[1]; if(m[1][0] !== '/'){ a.target = '_blank'; a.rel = 'noopener'; } a.textContent = m[1]; d.appendChild(a); last = re.lastIndex; }
    d.appendChild(document.createTextNode(t.slice(last))); return d; }
  function say(r, t, acts){
    var d = document.createElement('div'); d.className = r === 'user' ? 'q' : 'a';
    if(r === 'user') d.textContent = t; else d.appendChild(text(t));
    (acts || []).forEach(function(a){
      var b = document.createElement('button'); b.type = 'button'; b.className = 'cp-act'; b.textContent = root.dataset.do + ': ' + a.label;
      b.onclick = function(){ b.disabled = true;
        fetch('/app/assistant/do', {method:'POST', credentials:'same-origin', headers:{'Content-Type':'application/json','X-CSRF':root.dataset.csrf}, body:JSON.stringify({token:a.token})})
          .then(function(r){ return r.json(); }).then(function(j){ b.textContent = j.done || ('⚠ ' + (j.error || '')); b.classList.add(j.done ? 'ok' : 'bad');
            if(j.done){ turns.push({r:'assistant', t:j.done}); save(); } })
          .catch(function(){ b.disabled = false; }); };
      d.appendChild(b); });
    M.appendChild(d); M.scrollTop = M.scrollHeight; }
  function start(){ M.innerHTML = ''; if(!turns.length) say('assistant', root.dataset.hello); turns.forEach(function(x){ say(x.r, x.t); }); }
  $('.cp-b').onclick = function(){ root.classList.toggle('on'); if(root.classList.contains('on')){ start(); setTimeout(function(){ $('textarea').focus(); }, 50); } };
  $('.cp-x').onclick = function(){ root.classList.remove('on'); };
  var ta = $('textarea');
  ta.addEventListener('keydown', function(e){ if(e.key === 'Enter' && !e.shiftKey){ e.preventDefault(); $('.cp-f').requestSubmit(); } });
  $('.cp-f').onsubmit = function(e){
    e.preventDefault(); var q = ta.value.trim(); if(!q) return; ta.value = '';
    say('user', q); turns.push({r:'user', t:q}); save();
    var w = document.createElement('div'); w.className = 'a'; w.textContent = '…'; M.appendChild(w); M.scrollTop = M.scrollHeight;
    fetch('/app/assistant', {method:'POST', credentials:'same-origin', headers:{'Content-Type':'application/json','X-CSRF':root.dataset.csrf},
                             body:JSON.stringify({turns:turns, page:root.dataset.page + ' ' + location.pathname})})
      .then(function(r){ return r.json(); }).then(function(j){
        w.remove();
        if(j.reason === 'no_ai'){ say('assistant', root.dataset.nokey); return; }
        var t = j.say || j.error || root.dataset.busy; say('assistant', t, j.actions); turns.push({r:'assistant', t:t}); save(); })
      .catch(function(){ w.remove(); say('assistant', root.dataset.busy); });
  };
})();
