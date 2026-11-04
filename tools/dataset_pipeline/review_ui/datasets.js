"use strict";
(() => {
  const $ = id => document.getElementById(id), root = "/api/studio/datasets";
  const names = {a2d2:"Audi A2D2",comma10k:"comma10k",carla:"CARLA simulation"};
  const colors = {1:"#ffc273",2:"#ff7979",3:"#75c9ff",4:"#91edb1"};
  let catalog = null, selected = "converted_only", refreshing = false, sampleOffset = 0, sampleBase = null;
  const number = value => typeof value === "number" && Number.isFinite(value) ? value.toLocaleString() : "Not ready";
  const percent = value => typeof value === "number" && Number.isFinite(value) ? (value*100).toFixed(1) + "%" : null;
  const isSimulation = variant => variant.id.includes("simulated");
  const isProposals = variant => variant.id.includes("proposals");
  const modelCount = variant => isSimulation(variant) ? variant.model_extra_train_images : variant.extra_train_images;
  const el = (tag,text,className) => { const node=document.createElement(tag); if(text!==undefined)node.textContent=text; if(className)node.className=className; return node; };
  const link = (label,href) => { const node=el("a",label,"action"); node.href=href.startsWith(root+"/base/samples")?"#base-preview-title":href; if(href.startsWith("https:")){node.target="_blank";node.rel="noopener";} return node; };

  async function api(path="") {
    const response=await fetch(root+path,{credentials:"same-origin"});
    let data;try{data=await response.json();}catch{data={};}
    if(!response.ok){
      $("pair-link").hidden=response.status!==401;
      const error=new Error(data.detail || `Unable to load datasets (${response.status})`);error.status=response.status;throw error;
    }
    $("pair-link").hidden=true;return data;
  }
  function variantStatus(variant) {
    const state=String(variant.state || "");
    if(state.includes("error") || state.includes("unavailable"))return "error";
    if(isSimulation(variant))return "planned";
    return variant.training_ready ? "ready" : "preparing";
  }
  function setList(node,items) { node.replaceChildren(...items.map(item=>el("li",typeof item==="string"?item:item.label || item.description || item.title || ""))); }
  function draw(canvas,image,geometry) {
    canvas.width=image.naturalWidth;canvas.height=image.naturalHeight;
    const ctx=canvas.getContext("2d");ctx.drawImage(image,0,0);
    const sx=canvas.width/(geometry?.width || canvas.width),sy=canvas.height/(geometry?.height || canvas.height);
    for(const lane of geometry?.lanes || []){
      if(lane.physical===false || !lane.slot)continue;
      const points=(lane.points || []).filter(p=>Array.isArray(p)&&Number.isFinite(p[0])&&Number.isFinite(p[1])&&p[0]>=0&&p[1]>=0);
      if(points.length<2)continue;
      ctx.strokeStyle=colors[lane.slot] || "#b0edcd";ctx.lineWidth=Math.max(2,canvas.width/330);ctx.lineJoin="round";ctx.lineCap="round";
      ctx.beginPath();points.forEach((p,index)=>index?ctx.lineTo(p[0]*sx,p[1]*sy):ctx.moveTo(p[0]*sx,p[1]*sy));ctx.stroke();
    }
  }
  async function samples(reset=false) {
    if(!catalog?.base?.train_images)return;
    if(reset)sampleOffset=0;
    $("more-samples").disabled=true;$("sample-message").textContent="Loading cleared photo examples…";
    try{
      const result=await api(`/base/samples?offset=${sampleOffset}&limit=3`);
      const holder=document.createDocumentFragment();
      const loads=[];
      for(const item of result.items || []){
        const figure=el("figure"),anchor=el("a");anchor.href=item.image_url;anchor.target="_blank";anchor.rel="noopener";
        anchor.setAttribute("aria-label",`Open cleared ${names[item.source] || item.source || "training"} image`);
        const canvas=el("canvas"),caption=el("figcaption"),image=new Image();image.decoding="async";
        const count=Number(item.lanes ?? item.geometry?.lanes?.length ?? 0);
        caption.append(el("strong",names[item.source] || item.source || "Cleared photo"),el("span",`${count} painted ${count===1?"line":"lines"} · ${item.split || "train"}`));
        canvas.setAttribute("aria-label",`Cleared training image with ${count} painted lane lines`);
        anchor.append(canvas);figure.append(anchor,caption);holder.append(figure);
        loads.push(new Promise(resolve=>{
          image.onload=()=>{draw(canvas,image,item.geometry);resolve(true);};
          image.onerror=()=>{caption.append(el("span"," · Image unavailable"));resolve(false);};image.src=item.image_url;
        }));
      }
      $("base-samples").replaceChildren(holder);
      await Promise.allSettled(loads);
      sampleOffset=result.next_offset ?? 0;
      $("sample-message").textContent=result.items?.length ? "" : "No cleared photo examples available yet.";
    }catch(error){$("sample-message").textContent=error.message;}
    finally{$("more-samples").disabled=false;}
  }
  function renderCards() {
    const holder=document.createDocumentFragment();
    catalog.variants.forEach((variant,index)=>{
      const button=el("button",undefined,"collection");button.type="button";button.id="tab-"+variant.id;
      button.setAttribute("role","tab");button.setAttribute("aria-selected",String(variant.id===selected));
      button.setAttribute("aria-controls","collection-detail");button.tabIndex=variant.id===selected?0:-1;
      button.append(el("span",`COLLECTION ${String(index+1).padStart(2,"0")}`,"number"),el("h3",variant.title),
        el("p",variant.description),el("span",variant.state_label || "Preparing","status "+variantStatus(variant)));
      const composition=el("div",undefined,"mini-composition");composition.append(el("span",`${number(variant.base_train_images)} cleared`));
      if(isProposals(variant))composition.append(el("span",` + ${number(modelCount(variant) ?? 0)} approved model additions`,"extra"));
      if(isSimulation(variant))composition.append(el("span",` + ${number(variant.simulated_extra_train_images ?? 0)} approved simulated additions`,"simulation"));
      button.append(composition);
      button.onclick=()=>choose(variant.id);
      button.onkeydown=event=>{
        let next=index;
        if(event.key==="ArrowRight" || event.key==="ArrowDown")next=(index+1)%catalog.variants.length;
        else if(event.key==="ArrowLeft" || event.key==="ArrowUp")next=(index+catalog.variants.length-1)%catalog.variants.length;
        else if(event.key==="Home")next=0;else if(event.key==="End")next=catalog.variants.length-1;else return;
        event.preventDefault();choose(catalog.variants[next].id);$("tab-"+selected).focus();
      };
      holder.append(button);
    });
    $("collections").replaceChildren(holder);
  }
  function renderOvernight() {
    const queue=catalog.overnight_queue;
    $("overnight-panel").hidden=!queue;if(!queue)return;
    const states={waiting_import:"Waiting for simulation import",waiting_proposals:"Preparing model proposals",waiting_space:"Waiting for free storage",building:"Building provisional snapshot",running:"Running",complete:"Complete",error:"Needs attention"};
    $("overnight-status").textContent=states[queue.state] || "Queued";
    $("overnight-status").className="status "+(queue.state==="error"?"error":queue.state==="complete"?"ready":"preparing");
    const holder=document.createDocumentFragment();
    for(const job of queue.jobs || []){
      const card=el("article",undefined,"overnight-job"),variant=catalog.variants.find(v=>v.id===job.collection_id),index=job.id?.split("_").pop();
      card.append(el("p",`EXPERIMENT ${index || ""}`,"eyebrow"),el("h3",variant?.title || "Provisional experiment"),el("span",states[job.state] || "Queued","status "+(job.state==="complete"?"ready":job.state==="error"?"error":"preparing")));
      const progress=job.progress || {},parts=[];
      if(typeof progress.epoch==="number")parts.push(`Epoch ${number(progress.epoch)}${typeof progress.epochs==="number"?" / "+number(progress.epochs):""}`);
      if(typeof progress.step==="number")parts.push(`Step ${number(progress.step)}${typeof progress.steps==="number"?" / "+number(progress.steps):""}`);
      if(typeof job.train_images==="number")parts.push(`${number(job.train_images)} provisional training photos`);
      if(parts.length)card.append(el("p",parts.join(" · "),"job-progress"));
      const f1=percent(job.metrics?.best_val_f1);
      if(f1)card.append(el("p",`Best real-validation F1: ${f1}`,"job-metric"));
      const action=link("View collection",`/datasets?collection=${encodeURIComponent(job.collection_id)}`);
      action.onclick=event=>{event.preventDefault();choose(job.collection_id);$("collection-detail").scrollIntoView({behavior:"smooth",block:"start"});};card.append(action);holder.append(card);
    }
    $("overnight-jobs").replaceChildren(holder);
    $("overnight-message").textContent=queue.message || "All runs include the same cleared base and use the unchanged real-image validation set.";
  }
  function progressText() {
    const experiment=catalog.active_experiment;if(!experiment)return "";
    const teacherPhase=["prepared","preparing_teacher","training_teacher"].includes(experiment.phase);
    const p=(experiment.phase==="training_ufld"?experiment.training_progress:teacherPhase?experiment.teacher_progress:null) || {};
    const phases={prepared:"Experiment prepared",preparing_teacher:"Preparing the lane proposal model",training_teacher:"Training the lane proposal model",
      copying:"Preparing separate photo copies",predicting:"Generating model proposals",proposals_complete:"Model proposals are ready",
      building_training_snapshot:"Preparing the combined training collection",training_ufld:"Training UFLDv2 with the combined collection",
      complete_awaiting_comparison_review:"Training complete; results ready to compare",error:"Preparation needs attention"};
    let text=phases[experiment.phase] || "Model experiment in progress";
    if(experiment.phase==="preparing_teacher" && typeof p.processed==="number" && typeof p.total==="number")text+=` · checking ${p.split || "source"} labels: ${number(p.processed)} / ${number(p.total)}`;
    else if(typeof p.epoch==="number")text+=` · epoch ${p.epoch}`;
    if(experiment.phase==="predicting" && typeof experiment.proposals_done==="number")text+=` · ${number(experiment.proposals_done)} proposals processed`;
    return text;
  }
  function renderDetail() {
    const variant=catalog.variants.find(v=>v.id===selected);if(!variant)return;
    $("collection-detail").setAttribute("aria-labelledby","tab-"+variant.id);
    $("detail-kicker").textContent=isSimulation(variant)&&isProposals(variant)?"CLEARED BASE + MODEL PROPOSALS + SIMULATION":isSimulation(variant)?"CLEARED BASE + SIMULATION":isProposals(variant)?"CLEARED BASE + MODEL PROPOSALS":"THE REFERENCE COLLECTION";
    $("detail-title").textContent=variant.title;$("detail-description").textContent=variant.description;
    $("detail-status").textContent=variant.state_label || "Preparing";$("detail-status").className="status "+variantStatus(variant);
    const basePiece=el("div",undefined,"piece");basePiece.append(el("strong",number(variant.base_train_images)),el("span","cleared converted training photos"));
    $("composition").replaceChildren(basePiece);
    for(const [count,label,className] of [
      ...(isProposals(variant)?[[modelCount(variant),"approved model-proposal additions","extra"]]:[]),
      ...(isSimulation(variant)?[[variant.simulated_extra_train_images ?? 0,"approved simulated additions","simulation"]]:[])
    ]){
      const extra=el("div",undefined,"piece "+className);
      extra.append(el("strong",count===null || count===undefined?"Preparing":number(count)),el("span",label));
      $("composition").append(el("span","+","plus"),extra);
    }
    const counts=[];
    if(typeof variant.total_train_images==="number")counts.push(`${number(variant.total_train_images)} training photos ${variant.training_ready?"ready":"currently included"}`);
    if(typeof variant.candidate_train_images==="number")counts.push(`${number(variant.candidate_train_images)} candidate additions · approval pending`);
    if(isSimulation(variant))counts.push(variant.simulation_import?`${number(variant.imported_simulated_train_images ?? 0)} synthetic TRAIN photos imported · awaiting verified training selection`:`${number(variant.preview_images ?? 0)} inspection samples · full simulation set not imported`);
    if(variant.id==="converted_only" && percent(catalog.base?.metrics?.best_val_f1))counts.push(`Reference validation F1: ${percent(catalog.base.metrics.best_val_f1)}`);
    $("detail-counts").replaceChildren(...counts.map(text=>el("span",text)));
    const trial=variant.provisional_experiment,provisional=[];
    if(typeof variant.provisional_available_extra_train_images==="number")provisional.push(`${number(variant.provisional_available_extra_train_images)} quality-checked model additions available for provisional experiments`);
    if(trial){
      if(typeof trial.train_images==="number")provisional.push(`${number(trial.train_images)} photos in this private experiment`);
      const f1=percent(trial.metrics?.best_val_f1);if(f1)provisional.push(`Provisional best validation F1: ${f1}`);
    }
    if(provisional.length)provisional.push("These additions have not been added to the approved collection.");
    $("provisional-counts").textContent=provisional.join(" · ");$("provisional-counts").hidden=!provisional.length;
    const shared=["All cleared converted training photos remain included.","Your saved corrections remain part of the cleared base.","Validation and test photos are kept out of training."];
    const contents=variant.contents || (isSimulation(variant)?[shared[0],"Adds CARLA simulation images in a separate collection when imported.","Real TuSimple photos are excluded while their data permissions remain unresolved."]:isProposals(variant)?[shared[0],"Adds quality-checked model proposals on copies of flagged photos.","Original flagged conversions and human decisions stay unchanged."] : shared);
    setList($("contents"),contents);setList($("steps"),variant.steps || []);
    $("progress").textContent=isProposals(variant)?progressText():"";$("progress").hidden=!$("progress").textContent;
    const importing=isSimulation(variant)?variant.simulation_import:null;
    const stages={starting:"Starting simulation import",downloading:"Downloading simulated photos",converting:"Converting simulator lane labels",awaiting_audit:"Simulation import ready for sample audits",ready:"Simulation import complete; training selection pending",error:"Simulation import needs attention"};
    $("simulation-progress").textContent=importing?`${stages[importing.state] || "Simulation import"} · ${number(importing.downloaded)} / ${number(importing.total_expected)} downloaded · ${number(importing.converted)} converted${typeof importing.audit_samples==="number"?` · ${number(importing.audit_samples)} audit samples`:""}`:"";
    $("simulation-progress").hidden=!importing;
    $("links").replaceChildren(...(variant.links || []).filter(item=>typeof item.href==="string" && (item.href.startsWith("/") && !item.href.startsWith("//") || item.href.startsWith("#") || item.href==="https://carlanebenchmark.github.io/")).map(item=>link(item.label,item.href)));
    $("extra-preview").replaceChildren();$("extra-preview").hidden=!isSimulation(variant);
    if(isSimulation(variant)){
      const grid=el("div",undefined,"sample-grid");
      (variant.previews || []).filter(item=>typeof item.image_url==="string" && item.image_url.startsWith("/static/dataset_samples/carlane-tulane/")).forEach((item,index)=>{
        const figure=el("figure"),anchor=el("a"),image=el("img");
        const path=item.image_url;anchor.href=path;anchor.target="_blank";anchor.rel="noopener";
        image.src=path;image.alt=`TuLane CARLA simulated driving example ${index+1}`;image.loading="lazy";image.width=1280;image.height=720;
        anchor.append(image);figure.append(anchor,el("figcaption",`Simulation sample ${index+1} · inspection only`));grid.append(figure);
      });
      $("extra-preview").append(el("h3","Inspect the simulated addition"),el("p","These are publisher-selected preview images. Simulator labels need sample audits for visible lane paint, occlusions and road edges before a training addition is selected."),grid);
    }
  }
  function choose(id) {
    selected=id;
    for(const variant of catalog.variants){const node=$("tab-"+variant.id);node.setAttribute("aria-selected",String(id===variant.id));node.tabIndex=id===variant.id?0:-1;}
    renderDetail();
    try{history.replaceState(null,"",`/datasets?collection=${encodeURIComponent(id)}`);}catch{}
  }
  async function refresh() {
    if(refreshing)return;refreshing=true;$("refresh").disabled=true;
    try{
      const data=await api();
      if(!data.base || !Array.isArray(data.variants))throw new Error("Dataset status is being prepared. Try refresh shortly.");
      const previousFocus=document.activeElement?.id;
      catalog=data;
      if(!data.variants.some(v=>v.id===selected))selected=data.variants[0]?.id;
      $("base-train").textContent=number(data.base.train_images);$("base-val").textContent=number(data.base.val_images);$("base-test").textContent=number(data.base.reserved_test_images);
      $("source-breakdown").replaceChildren(...(data.base.sources || []).map(source=>el("span",`${names[source.source] || source.source} · ${number(source.train_images)} training photos`)));
      renderCards();renderDetail();renderOvernight();
      if(previousFocus?.startsWith("tab-"))$(previousFocus)?.focus({preventScroll:true});
      const warnings=data.warnings || [];$("warnings").textContent=warnings.join(" ");$("warnings").hidden=!warnings.length;
      $("updated").textContent="Status checked "+new Date().toLocaleTimeString()+" · Every collection references the same cleared base.";
      $("content").hidden=false;$("message").textContent="";
      const fingerprint=data.base.manifest_sha256 || data.base.run_id;
      if(sampleBase!==fingerprint){sampleBase=fingerprint;await samples(true);}
    }catch(error){$("message").textContent=error.message;}
    finally{refreshing=false;$("refresh").disabled=false;}
  }
  const initial=new URLSearchParams(location.search).get("collection");if(initial)selected=initial;
  $("refresh").onclick=refresh;$("more-samples").onclick=()=>samples();
  setInterval(()=>{if(!document.hidden)refresh();},15000);refresh();
})();
