(() => {
  'use strict';
  const $=id=>document.getElementById(id), api='/api/studio/model-errors', ns='http://www.w3.org/2000/svg';
  let collections=[], current=null, after='', busy=false, serial=0, lastSaved=null;
  const initialSelection=new URLSearchParams(location.search);
  for(const id of ['policy','kind','status']){
    const value=initialSelection.get(id);
    if(value && Array.from($(id).options).some(option=>option.value===value))$(id).value=value;
  }
  const message=text=>$('message').textContent=text;
  const verdictLabels={acceptable:'Model prediction accepted',confirmed_model_error:'Model mistake confirmed',label_question:'Label question flagged',skip:'Skipped for later'};
  async function request(path,options={}){
    const response=await fetch(api+path,{cache:'no-store',signal:AbortSignal.timeout(15000),...options});
    if(!response.ok){$('pair-link').hidden=response.status!==401;throw new Error(response.status===401?'Pair this phone with the PC first.':`Review unavailable (${response.status}). Try refresh.`);}
    return response.json();
  }
  function lock(value){busy=value;document.querySelectorAll('.review-actions button,[data-review-filter],#undo-last,#next,#collection,#policy,#kind,#status,#refresh').forEach(node=>node.disabled=value);}
  function renderSummary(summary){
    if(!summary)return;$('review-outcomes').hidden=false;$('review-totals').replaceChildren();
    for(const [key,title,filter] of [['open_alerts','Open alerts','open'],['unreviewed','Not reviewed','pending'],['accepted_predictions','Model accepted','accepted'],['confirmed_model_errors','Confirmed mistakes','confirmed_model_error'],['label_questions','Label questions','label_question'],['skipped','Skipped','skipped']]){
      const button=document.createElement('button'),count=document.createElement('strong'),label=document.createElement('span');
      count.textContent=summary[key].toLocaleString();label.textContent=title;button.type='button';button.dataset.reviewFilter=filter;button.append(count,label);
      button.onclick=()=>{if(!busy){$('status').value=filter;load(true);}};$('review-totals').append(button);
    }
  }
  function draw(){
    const svg=$('photo');svg.replaceChildren();if(!current)return;
    const image=document.createElementNS(ns,'image');image.setAttribute('href',`${api}/${encodeURIComponent($('collection').value)}/cases/${encodeURIComponent(current.image_id)}/photo`);image.setAttribute('width','1640');image.setAttribute('height','590');svg.append(image);
    const policy=current.policies[$('policy').value];if(!policy)return;
    function line(points,color,label,dashed=false){const node=document.createElementNS(ns,'polyline');node.setAttribute('points',points.map(p=>p.join(',')).join(' '));node.setAttribute('fill','none');node.setAttribute('stroke',color);node.setAttribute('stroke-width','5');if(dashed)node.setAttribute('stroke-dasharray','13 7');const title=document.createElementNS(ns,'title');title.textContent=label;node.append(title);svg.append(node);}
    if($('show-labels').checked)current.reference.forEach((points,i)=>{const missed=policy.missed_indices.includes(i);line(points,missed?'#ffdb69':'#85f5a0',`${missed?'Missed':'Matched'} saved lane ${i+1}`,missed);});
    if($('show-predictions').checked)policy.predicted.forEach((points,i)=>{const wrong=policy.false_positive_indices.includes(i);line(points,wrong?'#ff7c85':'#68e1ff',`${wrong?'False':'Matched'} prediction ${i+1}`);});
  }
  async function load(reset=false){
    if(busy)return;if(reset)after='';const version=++serial;
    const selection=new URLSearchParams({collection:$('collection').value,policy:$('policy').value,kind:$('kind').value,status:$('status').value});
    history.replaceState(null,'',`?${selection}`);
    lock(true);$('case').hidden=true;current=null;
    try{
      const id=$('collection').value, meta=collections.find(c=>c.id===id);
      if(!id){message('No error evaluations yet. Collections appear when validation finishes.');return;}
      $('provenance').textContent=`${meta.stage || id}${meta.historical?' · Historical evaluation':''}${meta.checkpoint_sha256?` · Checkpoint ${meta.checkpoint_sha256.slice(0,12)}`:''}. Validation photos only.`;
      if(typeof meta.false_positive_budget==='number')$('provenance').textContent+=` Recall target with at most ${meta.false_positive_budget} false lane predictions. Exploratory validation tuning.`;
      const metrics=meta.metrics || {}, byPolicy=metrics.decoder_comparisons?{stock:metrics,...metrics.decoder_comparisons}:metrics;
      $('evaluation-metrics').replaceChildren();
      for(const [policy,title] of [['stock','Original decoder'],['recall_budget_v4','Recall v4 - false-positive limit'],['short_span_v1','Short-span v1 · exploratory'],['short_span_geometry_v3','Geometry v3 · exploratory'],['short_span_geometry_v2','Strict geometry v2 · rejected prototype']]){
        const m=byPolicy[policy];if(!m)continue;
        const card=document.createElement('div'), heading=document.createElement('strong'), values=document.createElement('p');heading.textContent=title;
        values.textContent=`F1 ${(m.lane_f1*100).toFixed(2)}% · Precision ${(m.precision*100).toFixed(2)}% · Recall ${(m.recall*100).toFixed(2)}% · ${m.fn} missed · ${m.fp} false`;
        card.append(heading,values);$('evaluation-metrics').append(card);
      }
      if(meta.phase!=='complete'){message(`Evaluation is being prepared${meta.processed?` · ${meta.processed.toLocaleString()} images processed`:''}. This list refreshes automatically.`);return;}
      const params=new URLSearchParams({policy:$('policy').value,kind:$('kind').value,status:$('status').value,after,limit:'1'});
      let queue=await request(`/${encodeURIComponent(id)}/queue?${params}`);
      if(!queue.items.length && after){after='';params.set('after','');queue=await request(`/${encodeURIComponent(id)}/queue?${params}`);}
      renderSummary(queue.review_summary);
      if(!queue.items.length){message('No cases match these filters. Choose another decoder, mistake type or review status.');return;}
      const row=queue.items[0], item=await request(`/${encodeURIComponent(id)}/cases/${encodeURIComponent(row.image_id)}`);
      if(version!==serial)return;current={...item,review:row.review};after=item.image_id;
      const p=item.policies[$('policy').value];$('case-title').textContent=`${item.source} · ${item.image_id}`;
      $('counts').textContent=`Automated scoring: ${p.fn} missed lanes · ${p.fp} false predictions · ${p.tp} matched lanes`;
      $('notes').value=row.review?.notes || '';message(`${queue.total.toLocaleString()} matching photos${row.review?` · Your verdict: ${verdictLabels[row.review.action] || row.review.action}`:''}`);
      $('undo-current').hidden=!row.review;
      $('case').hidden=false;draw();
    }catch(error){message(error.message);}finally{lock(false);}
  }
  async function refresh(){
    if(busy)return;const old=$('collection').value || initialSelection.get('collection');
    try{
      collections=(await request('')).collections;
      const signature=JSON.stringify(collections.map(c=>[c.id,c.phase]));
      if($('collection').dataset.signature!==signature){
        $('collection').replaceChildren();collections.forEach(c=>{const option=document.createElement('option');option.value=c.id;option.textContent=`${c.id} · ${c.phase==='complete'?`${c.cases} error photos`:'Preparing'}`;$('collection').append(option);});
        $('collection').value=collections.some(c=>c.id===old)?old:(collections.find(c=>c.phase==='complete')?.id || collections[0]?.id || '');
        $('collection').dataset.signature=signature;
      }
      if(!current)await load(true);
      else renderSummary(await request(`/${encodeURIComponent($('collection').value)}/summary?policy=${encodeURIComponent($('policy').value)}`));
    }catch(error){message(error.message);}
  }
  document.querySelectorAll('.review-actions button[data-action]').forEach(button=>button.onclick=async()=>{
    if(busy||!current)return;lock(true);
    const decision={collection:$('collection').value,image_id:current.image_id,policy:$('policy').value,fingerprint:current.fingerprint};
    try{const result=await request(`/${encodeURIComponent(decision.collection)}/decision`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...decision,collection:undefined,action:button.dataset.action,notes:$('notes').value})});
      lastSaved=decision;renderSummary(result.summary);$('decision-feedback').hidden=false;$('undo-last').hidden=false;
      $('decision-result').textContent=`${decision.image_id}: ${result.message || 'Review decision saved.'}`;lock(false);await load();}
    catch(error){message(error.message);lock(false);}
  });
  async function undo(decision){
    if(busy||!decision)return;lock(true);
    try{const params=new URLSearchParams({policy:decision.policy,fingerprint:decision.fingerprint});
      const result=await request(`/${encodeURIComponent(decision.collection)}/decisions/${encodeURIComponent(decision.image_id)}?${params}`,{method:'DELETE'});
      renderSummary(result.summary);lastSaved=null;$('undo-last').hidden=true;$('decision-feedback').hidden=false;
      $('decision-result').textContent=`${decision.image_id}: decision undone. The case is back in Not reviewed.`;
      $('status').value='pending';lock(false);await load(true);
    }catch(error){message(error.message);lock(false);}
  }
  $('undo-last').onclick=()=>undo(lastSaved);
  $('undo-current').onclick=()=>current&&undo({collection:$('collection').value,image_id:current.image_id,policy:$('policy').value,fingerprint:current.fingerprint});
  for(const id of ['collection','policy','kind','status'])$(id).onchange=()=>{lastSaved=null;$('decision-feedback').hidden=true;load(true);};
  $('show-labels').onchange=draw;$('show-predictions').onchange=draw;$('next').onclick=()=>load();$('refresh').onclick=refresh;
  setInterval(()=>{if(!document.hidden)refresh();},15000);refresh();
})();
