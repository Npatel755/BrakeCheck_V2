(() => {
  'use strict';
  const $ = id => document.getElementById(id), root = '/api/studio/experiments';
  let selected = new URLSearchParams(location.search).get('run'), busy = false, lastSuccess = null, runSignature = '';
  const number = value => typeof value === 'number' && Number.isFinite(value) ? value.toLocaleString(undefined, {maximumFractionDigits: 3}) : '—';
  const score = value => typeof value === 'number' && value >= 0 ? `${(value * 100).toFixed(2)}%` : '—';
  const labels = {waiting_for_reference:'Queued ? waiting for reference run', waiting_for_gpu:'Waiting for GPU', caching_validation_candidates:'Evaluating recall candidates on CPU', candidate_cache_complete:'Validation cache ready', calibrating_decoder:'Selecting recall decoder', preparing:'Checking dataset', loading_model:'Loading model', initial_validation:'Initial validation', training:'Training', validation:'Validating epoch', trained_awaiting_review:'Finished · awaiting review', interrupted:'Stopped · worker exited', error:'Error', complete:'Complete', stopped:'Stopped', stopped_by_user:'Stopped by you', paused:'Paused'};
  const label = phase => labels[phase] || String(phase || 'Unknown').replaceAll('_', ' ');
  const runName = run => ['ufld_run','teacher'].includes(run.run_id) ? run.name.replaceAll('\\','/').split('/').slice(-2).join(' / ') : run.run_id;
  const collections = {converted_plus_proposals:'Converted + proposals', converted_plus_simulated:'Converted + simulation', converted_plus_proposals_plus_simulated:'Converted + proposals + simulation'};
  const el = (tag, text, cls) => { const node = document.createElement(tag); node.textContent = text; if (cls) node.className = cls; return node; };
  async function get(url) {
    const response = await fetch(url, {cache:'no-store', signal:AbortSignal.timeout(12000)});
    if (!response.ok) { const error = new Error(response.status === 401 ? 'Pair this phone with the PC to view experiments.' : `Status unavailable (${response.status}). Retrying automatically.`); error.status = response.status; throw error; }
    return response.json();
  }
  function chart(id, rows, accessor, title, percent = false) {
    const container = $(id);
    const points = rows.filter(r => typeof r.epoch === 'number' && Number.isFinite(accessor(r)));
    // Keep hovered/focused points in place during polls with unchanged history.
    const signature = JSON.stringify(points.map(p => [p.epoch, accessor(p)]));
    if (container.dataset.signature === signature) return;
    container.dataset.signature = signature;
    container.replaceChildren();
    if (!points.length) { container.append(el('p','Waiting for the first completed epoch.','hint')); return; }
    const ns = 'http://www.w3.org/2000/svg', svg = document.createElementNS(ns,'svg');
    svg.setAttribute('viewBox','0 0 360 180'); svg.setAttribute('role','group'); svg.setAttribute('aria-label',title);
    const values = points.map(accessor), low = percent ? 0 : Math.min(...values) * .9, high = percent ? 1 : Math.max(...values) * 1.1 || 1;
    const first = points[0].epoch, last = Math.max(first+1, points.at(-1).epoch);
    const xy = points.map(p => [42+(p.epoch-first)/(last-first)*300, 144-(accessor(p)-low)/(high-low)*120]);
    const line = document.createElementNS(ns,'polyline'); line.setAttribute('points',xy.map(p=>p.join(',')).join(' ')); line.setAttribute('fill','none'); line.setAttribute('stroke','#b0edcd'); line.setAttribute('stroke-width','2.5'); svg.append(line);
    const tooltip = document.createElementNS(ns,'g');
    tooltip.setAttribute('class','chart-tooltip'); tooltip.setAttribute('visibility','hidden');
    tooltip.setAttribute('role','tooltip'); tooltip.id = `${id}-tooltip`;
    const background = document.createElementNS(ns,'rect'), tooltipText = document.createElementNS(ns,'text');
    background.setAttribute('width','210'); background.setAttribute('height','30'); background.setAttribute('rx','6');
    tooltip.append(background, tooltipText);
    xy.forEach(([cx,cy], index) => {
      const dot=document.createElementNS(ns,'circle'); dot.setAttribute('cx',cx);dot.setAttribute('cy',cy);dot.setAttribute('r','4');dot.setAttribute('fill','#b0edcd');svg.append(dot);
      const hit=document.createElementNS(ns,'circle');
      hit.setAttribute('cx',cx);hit.setAttribute('cy',cy);hit.setAttribute('r','11');hit.setAttribute('fill','transparent');
      hit.setAttribute('class','chart-point');hit.setAttribute('tabindex','0');hit.setAttribute('role','img');
      const text=`Epoch ${points[index].epoch} · ${percent?'Lane F1':'Training loss'} ${percent?score(accessor(points[index])):number(accessor(points[index]))}`;
      hit.setAttribute('aria-label',text);hit.setAttribute('aria-describedby',tooltip.id);
      const show=()=>{
        const x=Math.max(5,Math.min(145,cx-105)), y=Math.max(5,cy-39);
        background.setAttribute('x',x);background.setAttribute('y',y);
        tooltipText.setAttribute('x',x+10);tooltipText.setAttribute('y',y+19);tooltipText.textContent=text;
        tooltip.setAttribute('visibility','visible');
      };
      const hide=()=>tooltip.setAttribute('visibility','hidden');
      hit.addEventListener('pointerenter',show);hit.addEventListener('pointerleave',hide);
      hit.addEventListener('focus',show);hit.addEventListener('blur',hide);hit.addEventListener('click',show);
      hit.addEventListener('keydown',event=>{if(event.key==='Escape')hide();});
      svg.append(hit);
    });
    [[5,25,percent?score(high):number(high)],[5,147,percent?score(low):number(low)],[42,169,`Epoch ${first}`],[272,169,`Epoch ${points.at(-1).epoch}`]].forEach(([x,y,value])=>{const t=document.createElementNS(ns,'text');t.setAttribute('x',x);t.setAttribute('y',y);t.textContent=value;svg.append(t);});
    svg.append(tooltip);container.append(svg);
  }
  function detail(run) {
    $('detail').hidden=false; $('run-title').textContent=runName(run); $('kind').textContent=run.kind;
    $('phase').textContent=label(run.phase); $('phase').className='status'+(['error','interrupted','failed'].includes(run.phase)?' error':'');
    $('worker').textContent=`${run.worker_alive === true ? 'Worker running' : run.worker_alive === false ? 'Worker is not running' : 'Worker status unavailable'} · Last report ${new Date(run.updated_at*1000).toLocaleString()}`;
    const error=run.error_reason || run.error || run.stop_reason || (run.phase==='interrupted' ? `The saved report says “${label(run.reported_phase)}”, but its worker has exited.` : '');
    $('error').hidden=!error; $('error').textContent=error;
    const hasSteps=run.total_steps>0 && typeof run.step==='number', pct=hasSteps?Math.min(100,Math.max(0,run.step/run.total_steps*100)):null;
    $('progress-label').textContent=hasSteps?`${number(run.step)} / ${number(run.total_steps)} optimizer steps`:'Waiting for optimizer progress';
    $('percent').textContent=pct===null?'':`${pct.toFixed(1)}%`; if(pct===null)$('bar').removeAttribute('value');else $('bar').value=pct;
    $('stage').textContent=`Epoch ${number(run.epoch)} / ${number(run.epochs)}${run.phase==='initial_validation'?' · Evaluating the starting checkpoint before training.':''}`;
    $('metrics').replaceChildren();
    [['Training photos',number(run.train_images)],['Validation photos',number(run.val_images)],['Training loss',number(run.train_loss ?? run.loss)],['Best lane F1',score(run.best_val_f1)],['Best short-span F1 · exploratory',score(run.best_short_span_val_f1)],['Peak GPU memory',typeof run.peak_vram_gib==='number'?`${number(run.peak_vram_gib)} GiB`:'—']].forEach(([name,value])=>{const box=el('div','','metric');box.append(el('strong',value),el('span',name));$('metrics').append(box);});
    const v=run.validation || {}, comparisons=run.decoder_comparisons || {};
    [['Latest lane F1',score(v.lane_f1)],['Latest precision',score(v.precision)],['Latest recall',score(v.recall)],['Missed lanes ? latest validation',number(v.fn)],['False predictions ? latest validation',number(v.fp)],['Latest geometry decoder F1 ? exploratory',score(comparisons.short_span_geometry_v3?.lane_f1 ?? comparisons.short_span_geometry_v2?.lane_f1)]].forEach(([name,value])=>{const box=el('div','','metric');box.append(el('strong',value),el('span',name));$('metrics').append(box);});
    if(run.sampling_mode==='balanced')$('stage').textContent+=' ? Equal source sampling';
    if(typeof run.false_positive_budget==='number'){
      if(typeof run.baseline_recall==='number' && typeof run.selected_recall==='number')$('stage').textContent=`Recall experiment: ${(run.baseline_recall*100).toFixed(2)}% baseline to ${(run.selected_recall*100).toFixed(2)}% selected, within ${run.false_positive_budget} false lane predictions. Exploratory validation result.`;
      else $('stage').textContent+=` · Recall decoder limit: ${run.false_positive_budget} false lane predictions`;
    }
    if(comparisons.recall_budget_v4){
      const r=comparisons.recall_budget_v4;
      [['Recall decoder: recall',score(r.recall)],['Recall decoder: F1',score(r.lane_f1)],['Recall decoder: false predictions',`${number(r.fp)} / ${number(run.false_positive_budget)}`]].forEach(([name,value])=>{const box=el('div','','metric');box.append(el('strong',value),el('span',name));$('metrics').append(box);});
    }
    chart('loss-chart',run.history||[],r=>r.train_loss,'Training loss by epoch');
    chart('f1-chart',run.history||[],r=>r.validation?.lane_f1,'Validation lane F1 by epoch',true);
    $('log').textContent=run.log || 'No log output yet. Progress is reported above.';
  }
  async function refresh() {
    if(busy)return; busy=true; $('refresh').disabled=true;
    try {
      const data=await get(root); $('pair-link').hidden=true;
      if(!data.runs.some(r=>r.id===selected))selected=data.runs[0]?.id;
      const signature=JSON.stringify([selected,data.runs.map(run=>[run.id,run.phase,run.active])]);
      if(signature!==runSignature){
        const scroll=$('runs').scrollTop, focused=document.activeElement?.dataset?.run;
        $('runs').replaceChildren();runSignature=signature;
        data.runs.forEach(run=>{const button=el('button','','run-card');button.type='button';button.dataset.run=run.id;button.setAttribute('aria-pressed',String(run.id===selected));button.append(el('strong',runName(run)),el('small',run.kind),el('span',`${run.active?'● ':''}${label(run.phase)}`,'status'));button.onclick=()=>{selected=run.id;history.replaceState(null,'',`?run=${encodeURIComponent(selected)}`);refresh();};$('runs').append(button);if(focused===run.id)button.focus({preventScroll:true});});
        $('runs').scrollTop=scroll;
      }
      if(!data.runs.length){$('runs').replaceChildren(el('p','No training runs have reported status yet.','hint'));$('detail').hidden=true;}
      const queue=data.queues[0]; $('queue').hidden=!queue;
      if(queue){$('queue').replaceChildren(el('h2','Experiment queue'),el('p',`${label(queue.state)} · ${queue.worker_alive===false?'Queue worker exited':queue.worker_alive===true?'Queue worker running':'Worker status unavailable'}`));const jobs=el('div','','queue-jobs');queue.jobs.forEach(job=>jobs.append(el('span',`${collections[job.collection_id] || job.id} · ${label(job.state)}`)));$('queue').append(jobs);}
      $('warnings').hidden=!data.warnings.length;$('warnings').textContent=data.warnings.join(' · ');
      if(selected)detail(await get(`${root}/${encodeURIComponent(selected)}`));
      lastSuccess=new Date();$('connection').textContent=`● Live · Updated ${lastSuccess.toLocaleTimeString()} · Refreshes every 5 seconds`;
    } catch(error){$('connection').textContent=`${error.message}${lastSuccess?` Last successful update: ${lastSuccess.toLocaleTimeString()}. Displayed data may be out of date.`:''}`;$('pair-link').hidden=error.status!==401;}
    finally{busy=false;$('refresh').disabled=false;}
  }
  $('refresh').onclick=refresh;
  document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh();});
  setInterval(()=>{if(!document.hidden)refresh();},5000);refresh();
})();
