"use strict";
(() => {
  const $=id=>document.getElementById(id), canvas=$('lead-canvas'),ctx=canvas.getContext('2d');
  const base='/api/studio/lead-cars';
  let client;
  try{client=sessionStorage.getItem('bc-lead-client');if(!client){client=crypto.randomUUID?.()||Date.now()+'-'+Math.random().toString(16).slice(2);sessionStorage.setItem('bc-lead-client',client);}}catch{client=crypto.randomUUID?.()||Date.now()+'-'+Math.random().toString(16).slice(2);}
  const documentID=crypto.randomUUID?.()||Math.random().toString(16).slice(2);
  const clientReady=new Promise(resolve=>{
    if(!globalThis.BroadcastChannel){resolve();return;}
    const channel=new BroadcastChannel('bc-lead-tabs');
    channel.onmessage=({data})=>{
      if(data?.type==='probe'&&data.client===client&&data.document!==documentID)channel.postMessage({type:'taken',client,document:data.document});
      else if(data?.type==='taken'&&data.document===documentID&&data.client===client){client=crypto.randomUUID?.()||Date.now()+'-'+Math.random().toString(16).slice(2);try{sessionStorage.setItem('bc-lead-client',client);}catch{}}
    };
    channel.postMessage({type:'probe',client,document:documentID});setTimeout(resolve,75);
  });
  let item=null, photo=null, box=null, busy=false, imageReady=false, dirty=false, conflict=false;
  let drag=null, history=[], pending=null, viewer=null, last=null, seen=[], panning=false;
  const copy=v=>v==null?v:JSON.parse(JSON.stringify(v));
  const uid=()=>crypto.randomUUID?.() || Date.now()+'-'+Math.random().toString(16).slice(2);
  const key=()=>item&&`bc-lead-draft:${viewer?.reviewer}:${item.image_id}`;
  const say=(text,error=false)=>{$('status').textContent=text;$('status').classList.toggle('error',error);};
  async function api(path,body){
    await clientReady;
    const r=await fetch(path,{method:body===undefined?'GET':'POST',headers:{'Content-Type':'application/json','X-Review-Client':'lead-'+client},...(body===undefined?{}:{body:JSON.stringify(body)})});
    let data;try{data=await r.json();}catch{throw new Error('PC returned an unreadable response. Your draft is kept.');}
    if(!r.ok){const e=new Error(data.detail||'Request failed');e.status=r.status;throw e;}
    return data;
  }
  function persist(){
    if(!item)return;
    try{localStorage.setItem(key(),JSON.stringify({revision:item.revision,fingerprint:item.fingerprint,box,notes:$('notes').value,pending,dirty}));}catch{}
  }
  function controls(){
    const blocked=busy||!item||!imageReady||conflict;
    $('save').disabled=blocked||(!box&&!pending)||!!(pending&&pending.action!=='box');
    $('save').textContent=pending?.action==='box'?'Retry save →':'Save lead box →';
    $('no-lead').disabled=blocked||!!(pending&&pending.action!=='no_lead');
    $('no-lead').textContent=pending?.action==='no_lead'?'Retry no-lead save':'No lead vehicle';
    $('skip').disabled=blocked||!!(pending&&pending.action!=='skip');
    $('skip').textContent=pending?.action==='skip'?'Retry skip':'Unclear / skip';
    $('clear').disabled=blocked||!box||!!pending;
    $('undo-draft').disabled=blocked||!history.length||!!pending;
    $('queue').disabled=busy||!!pending;
    $('notes').disabled=blocked||!!pending;
    $('reload').hidden=!conflict;
    $('undo-saved').hidden=!last;
    $('undo-saved').disabled=busy||dirty||!!pending;
    $('pan').setAttribute('aria-pressed',String(panning));
  }
  function paint(){
    ctx.clearRect(0,0,canvas.width,canvas.height);
    if(photo)ctx.drawImage(photo,0,0,canvas.width,canvas.height);
    const s=canvas.width/Math.max(1,canvas.getBoundingClientRect().width);
    ctx.lineWidth=2*s;
    if($('show-candidates').checked&&item){
      ctx.strokeStyle='#f2c680';ctx.setLineDash([7*s,5*s]);
      for(const c of item.detections||[]){const b=c.box;ctx.strokeRect(b[0],b[1],b[2]-b[0],b[3]-b[1]);}
    }
    ctx.setLineDash([]);
    if(box){
      ctx.fillStyle='#50f7ca20';ctx.fillRect(box[0],box[1],box[2]-box[0],box[3]-box[1]);
      ctx.strokeStyle='#7cffe0';ctx.lineWidth=3*s;ctx.strokeRect(box[0],box[1],box[2]-box[0],box[3]-box[1]);
      ctx.fillStyle='#7cffe0';
      for(const [x,y] of [[box[0],box[1]],[box[2],box[1]],[box[2],box[3]],[box[0],box[3]]])ctx.fillRect(x-5*s,y-5*s,10*s,10*s);
    }
  }
  function point(e){const r=canvas.getBoundingClientRect();return [Math.max(0,Math.min(canvas.width,(e.clientX-r.left)*canvas.width/r.width)),Math.max(0,Math.min(canvas.height,(e.clientY-r.top)*canvas.height/r.height))];}
  function changed(){dirty=true;persist();paint();controls();say('Draft only. Save the lead box when ready.');}
  canvas.addEventListener('pointerdown',e=>{
    if(!item||busy||pending||conflict||!imageReady||panning||e.button!==0)return;
    const p=point(e),tol=15*canvas.width/canvas.getBoundingClientRect().width;
    history.push(copy(box));if(history.length>50)history.shift();
    let corner=-1;
    if(box)corner=[[box[0],box[1]],[box[2],box[1]],[box[2],box[3]],[box[0],box[3]]].findIndex(c=>Math.hypot(p[0]-c[0],p[1]-c[1])<=tol);
    if(corner>=0)drag={mode:'resize',corner,initial:copy(box),start:p};
    else if(box&&p[0]>box[0]&&p[0]<box[2]&&p[1]>box[1]&&p[1]<box[3])drag={mode:'move',initial:copy(box),start:p};
    else {box=[...p,...p];drag={mode:'draw',start:p};}
    canvas.setPointerCapture(e.pointerId);e.preventDefault();paint();
  });
  canvas.addEventListener('pointermove',e=>{
    if(!drag)return;
    const p=point(e);
    if(drag.mode==='move'){
      const b=drag.initial,dx=Math.max(-b[0],Math.min(canvas.width-b[2],p[0]-drag.start[0])),dy=Math.max(-b[1],Math.min(canvas.height-b[3],p[1]-drag.start[1]));
      box=[b[0]+dx,b[1]+dy,b[2]+dx,b[3]+dy];
    }else{
      const b=drag.initial;
      const other=drag.mode==='draw'?drag.start: [[b[2],b[3]],[b[0],b[3]],[b[0],b[1]],[b[2],b[1]]][drag.corner];
      box=[Math.min(other[0],p[0]),Math.min(other[1],p[1]),Math.max(other[0],p[0]),Math.max(other[1],p[1])];
    }
    paint();e.preventDefault();
  });
  function finish(){if(!drag)return;drag=null;if(!box||box[2]-box[0]<3||box[3]-box[1]<3)box=history.pop()??null;changed();}
  canvas.addEventListener('pointerup',finish);canvas.addEventListener('pointercancel',finish);
  $('clear').onclick=()=>{history.push(copy(box));box=null;changed();};
  $('undo-draft').onclick=()=>{if(history.length){box=history.pop();changed();}};
  $('show-candidates').onchange=paint;
  $('notes').oninput=()=>{dirty=true;persist();controls();};
  $('zoom').onchange=()=>{canvas.style.width=Number($('zoom').value)*100+'%';paint();};
  $('pan').onclick=()=>{panning=!panning;canvas.style.touchAction=panning?'pan-x pan-y':'none';canvas.style.cursor=panning?'grab':'crosshair';controls();};
  async function counts(){const s=await api(base);const c=s.counts,done=c.box+c.no_lead;$('count').textContent=`${done} / ${c.total} labeled`;$('counts-detail').textContent=`${c.pending} to label · ${c.skip} skipped`;$('progress').max=Math.max(1,c.total);$('progress').value=done;
    const t=s.training,node=$('training-status');node.hidden=!t;
    if(t){node.textContent=t.status==='complete'?'Lead model training complete. This is an experimental checkpoint; the phone app has not changed.':t.status==='failed'?'Lead model training stopped with an error. Your saved labels are preserved.':`Lead model training: epoch ${t.epoch||0}${t.epochs?' / '+t.epochs:''}. Your saved labels are preserved.`;}}
  setInterval(()=>{if(viewer?.paired&&!busy)counts().catch(()=>{});},15000);
  async function display(next){
    imageReady=false;photo=null;item=next;box=null;pending=null;dirty=false;conflict=false;history=[];drag=null;
    $('notes').value='';$('draft-notice').hidden=true;$('empty').hidden=!!item;canvas.hidden=!item;
    $('stage').scrollTop=0;$('stage').scrollLeft=0;$('zoom').value='1';canvas.style.width='100%';
    if(!item){$('empty').textContent='No available images in this queue. Try Labeled or Unclear / skipped.';say('Your saved labels stay on the PC.');controls();return;}
    try{sessionStorage.setItem('bc-lead-current',item.image_id);}catch{}
    $('image-info').textContent=`${item.image_id} · ${item.source} · ${item.curation_bucket.replaceAll('_',' ')}`;
    canvas.width=item.width;canvas.height=item.height;box=copy(item.box);$('notes').value=item.notes||'';
    let draft;
    try{draft=JSON.parse(localStorage.getItem(key())||'null');}catch{}
    if(draft){
      if(draft.revision===item.revision&&draft.fingerprint===item.fingerprint){box=draft.box;pending=draft.pending;dirty=!!draft.dirty;$('notes').value=draft.notes||'';$('draft-notice').hidden=false;}
      else {conflict=true;say('A saved local draft belongs to an older label. Reload latest to use the current label; the draft is kept.',true);}
    }
    const img=new Image();
    await new Promise((resolve,reject)=>{img.onload=resolve;img.onerror=()=>reject(new Error('Photo could not load; no label was saved.'));img.src=item.image_url;});
    photo=img;imageReady=true;
    if(!conflict)say(pending?'Previous save is unconfirmed. Retry the same action.':item.decision?`Saved ${item.decision==='box'?'lead box':item.decision==='no_lead'?'no lead vehicle':'as unclear'} · ${item.reviewer}`:'Drag across the lead vehicle, then save.');
    paint();controls();
  }
  async function next(){const q=$('queue').value;await display(await api(base+'/next?queue='+encodeURIComponent(q)+'&exclude='+seen.join(',')));}
  async function save(action){
    if(busy||!item||conflict||!imageReady||pending&&pending.action!==action)return;
    if(action==='box'&&!box&&!pending)return;
    const payload=pending||{image_id:item.image_id,revision:item.revision,fingerprint:item.fingerprint,event_id:uid(),action,box:action==='box'?box.map(v=>Math.round(v*1000)/1000):null,notes:$('notes').value};
    pending=copy(payload);persist();busy=true;controls();say('Saving to PC…');
    try{
      const result=await api(base+'/labels',payload);
      if(!result.saved)throw new Error('PC did not confirm this label. Retry it.');
      last={event_id:result.event_id,image_id:result.image_id};
      try{localStorage.removeItem(key());localStorage.setItem('bc-lead-last:'+viewer.reviewer,JSON.stringify(last));}catch{}
      pending=null;dirty=false;seen.push(item.image_id);await counts();await next();
    }catch(e){if(e.status===409)conflict=true;say(e.message,true);}
    finally{busy=false;controls();}
  }
  $('save').onclick=()=>save('box');$('no-lead').onclick=()=>save('no_lead');$('skip').onclick=()=>save('skip');
  $('queue').onchange=async()=>{if(dirty)persist();seen=[];busy=true;controls();try{await next();}catch(e){say(e.message,true);}finally{busy=false;controls();}};
  $('reload').onclick=async()=>{
    if(!item)return;
    // Retain the stale draft separately so resolving a conflict never destroys it.
    try{const old=localStorage.getItem(key());if(old)localStorage.setItem(key()+':conflict:'+Date.now(),old);localStorage.removeItem(key());}catch{}
    busy=true;controls();try{await display(await api(base+'/images/'+item.image_id));}catch(e){say(e.message,true);}finally{busy=false;controls();}
  };
  $('undo-saved').onclick=async()=>{
    if(!last||busy||dirty||pending)return;
    busy=true;controls();
    try{const result=await api(base+'/undo',{event_id:last.event_id});last=null;try{localStorage.removeItem('bc-lead-last:'+viewer.reviewer);}catch{}seen=seen.filter(id=>id!==result.image_id);await counts();await display(await api(base+'/images/'+result.image_id));}
    catch(e){say(e.message,true);}finally{busy=false;controls();}
  };
  $('export').onclick=async()=>{if(busy)return;busy=true;controls();try{const r=await api(base+'/export',{});$('export-status').textContent=`Exported ${r.images} saved labels (${r.boxes} boxes, ${r.no_lead} no-lead photos). ${r.path}`;}catch(e){say(e.message,true);}finally{busy=false;controls();}};
  document.addEventListener('keydown',e=>{if(['INPUT','SELECT','TEXTAREA'].includes(document.activeElement.tagName))return;if(e.key==='Enter'&&!$('save').disabled){e.preventDefault();save('box');}});
  window.addEventListener('beforeunload',()=>{if(dirty||pending)persist();});
  window.addEventListener('resize',paint);
  setInterval(async()=>{
    if(!item||busy||conflict)return;
    try{await api(base+'/images/'+item.image_id+'/claim',{revision:item.revision,fingerprint:item.fingerprint});}
    catch(e){if(e.status===409){conflict=true;say(e.message,true);controls();}}
  },120000);
  (async()=>{busy=true;controls();try{viewer=await api('/api/session');if(!viewer.paired){$('pairing').hidden=false;return;}$('workspace').hidden=false;try{last=JSON.parse(localStorage.getItem('bc-lead-last:'+viewer.reviewer)||'null');}catch{}await counts();let saved;try{saved=sessionStorage.getItem('bc-lead-current');}catch{}if(saved){try{await display(await api(base+'/images/'+encodeURIComponent(saved)));}catch(e){if(e.status===404||e.status===409)await next();else throw e;}}else await next();}catch(e){if(e.status===401)$('pairing').hidden=false;say(e.message,true);}finally{busy=false;controls();}})();
})();
