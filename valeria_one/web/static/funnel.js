/* ars02 (v0.21): a funnel page's countdown — days, hours, minutes, seconds to the time the owner set. Without JS the
   boxes simply show dashes; nothing else on the page needs JS. */
(function(){
  var els=document.querySelectorAll('[data-until]');
  if(!els.length) return;
  function pad(n){return (n<10?'0':'')+n;}
  function tick(){
    Array.prototype.forEach.call(els,function(el){
      var left=Math.max(0,Math.floor((Date.parse(el.getAttribute('data-until'))-Date.now())/1000));
      if(isNaN(left)) return;
      el.querySelector('[data-d]').textContent=Math.floor(left/86400);
      el.querySelector('[data-h]').textContent=pad(Math.floor(left%86400/3600));
      el.querySelector('[data-m]').textContent=pad(Math.floor(left%3600/60));
      el.querySelector('[data-s]').textContent=pad(left%60);
    });
  }
  tick(); setInterval(tick,1000);
})();
