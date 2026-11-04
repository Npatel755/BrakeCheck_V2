"use strict";
(() => {
  const $ = (id) => document.getElementById(id), apiRoot = "/api/experiments/flagged";
  const colors = {1:"#ffc273",2:"#ff7979",3:"#75c9ff",4:"#91edb1"};
  let current = null, photo = null, busy = false, runId = null, requestGeneration = 0, retryDecision = null, after = "";
  const number = (value) => Number(value || 0).toLocaleString();
  async function api(path, options = {}) {
    const response = await fetch(apiRoot + path, {credentials:"same-origin",...options});
    let body; try { body = await response.json(); } catch { body = {}; }
    if (!response.ok) {
      if (response.status === 401) $("pair-link").hidden = false;
      const error = new Error(body.detail || `Request failed (${response.status})`); error.status = response.status; throw error;
    }
    $("pair-link").hidden = true; return body;
  }
  function lock(value) {
    busy = value;
    for (const id of ["exclude","approve","skip"]) $(id).disabled = value || !current;
    $("approve").disabled = value || !current || !current.approval_allowed;
    $("approve").title = current?.approval_block_reason || "Accept this proposal for the isolated experiment";
    for (const id of ["run","split","filter","refresh"]) $(id).disabled = value;
  }
  function message(text) { $("message").textContent = text; }
  function draw(canvas, geometry) {
    const width = photo.naturalWidth, height = photo.naturalHeight;
    canvas.width = width; canvas.height = height;
    const context = canvas.getContext("2d"); context.drawImage(photo,0,0,width,height);
    const sx = width / (Number(geometry.width) || width), sy = height / (Number(geometry.height) || height);
    for (const lane of Array.isArray(geometry.lanes) ? geometry.lanes : []) {
      if (!lane || !lane.slot || lane.physical === false) continue;
      const points = (Array.isArray(lane.points) ? lane.points : []).filter(p => Array.isArray(p) && Number.isFinite(p[0]) && Number.isFinite(p[1]));
      if (points.length < 2) continue;
      context.strokeStyle = colors[lane.slot] || "#f4f5b4";
      context.lineWidth = Math.max(2,width/300); context.lineJoin = "round"; context.lineCap = "round";
      context.beginPath(); context.moveTo(points[0][0]*sx,points[0][1]*sy);
      for (const point of points.slice(1)) context.lineTo(point[0]*sx,point[1]*sy);
      context.stroke();
    }
  }
  function showStatus(status) {
    const meta = status.metadata || {}, counts = status.counts || {};
    const phase = meta.phase || meta.status?.phase || meta.teacher?.phase || meta.state || "Experiment prepared";
    const names = {prepared:"Experiment prepared",preparing_teacher:"Preparing the proposal teacher",training_teacher:"Training the proposal teacher",
      copying:"Copying flagged images",predicting:"Generating fresh proposals",proposals_complete:"Fresh proposals ready",
      building_training_snapshot:"Freezing the training dataset",training_ufld:"Training the UFLDv2 experiment",
      complete_awaiting_comparison_review:"Training complete · comparison review",error:"Experiment needs attention"};
    let phaseText = names[phase] || (typeof phase === "string" ? phase.replaceAll("_"," ") : "Experiment prepared");
    const progress = status.progress || {}, teacher = progress.teacher_training || meta.teacher_progress || {};
    const training = progress.ufld_training || progress.ufld_run || {};
    const active = phase === "training_ufld" ? training : phase === "training_teacher" ? teacher : null;
    if (active?.epoch !== undefined) phaseText += ` · epoch ${active.epoch}${active.epochs ? "/" + active.epochs : ""}`;
    $("phase").textContent = phaseText;
    $("counts").replaceChildren();
    const base = meta.base_train_images ?? meta.base_counts?.train ?? meta.base?.train_images;
    const extra = meta.extra_train_images ?? meta.training?.extra_images;
    const values = [["copied images",counts.copies],["fresh proposals",counts.proposals_ready],
      ["optional checks left",counts.optional_checks_remaining],["approved here",counts.approved],
      ["excluded",counts.excluded],["skipped",counts.skipped]];
    if (base !== undefined) values.unshift(["base training images",base]);
    if (extra !== undefined) values.unshift(["extra selected",extra]);
    if (meta.copies_done !== undefined) values.push(["copies prepared",meta.copies_done]);
    if (meta.proposals_done !== undefined) values.push(["proposals processed",meta.proposals_done]);
    const repair = progress.proposal_repair;
    if (repair) {
      values.push(["hood-clipped proposals repaired",repair.completed || 0]);
      if (repair.total !== undefined) values.push(["proposals in repair batch",repair.total]);
    }
    for (const [label,value] of values) { const badge = document.createElement("span"); badge.textContent = `${number(value)} ${label}`; $("counts").append(badge); }
    $("policy").textContent = status.policy;
    $("snapshot-warning").hidden = !status.snapshot_frozen;
  }
  async function status() { if (runId) showStatus(await api(`/${encodeURIComponent(runId)}/status`)); }
  async function loadNext() {
    if (!runId || busy) return;
    const generation = ++requestGeneration; lock(true); current = null; photo = null; retryDecision = null;
    $("review").hidden = true; $("empty").hidden = true; message("Loading experiment copy…");
    try {
      const params = new URLSearchParams({split:$("split").value,status:$("filter").value,limit:"1"});
      if (after) params.set("after",after);
      const result = await api(`/${encodeURIComponent(runId)}/queue?${params}`);
      if (generation !== requestGeneration) return;
      if (!result.items.length) {
        $("empty-title").textContent = $("filter").value === "pending" ? "No optional checks ready in this split" : "No copies in this category";
        $("empty-hint").textContent = "You can change the filters. Fresh proposals will appear while preparation continues.";
        $("empty").hidden = false; message(""); return;
      }
      const item = result.items[0], image = new Image(); image.decoding = "async";
      await new Promise((resolve,reject) => { image.onload = resolve; image.onerror = () => reject(new Error("The copied image could not be loaded")); image.src = item.image_url; });
      if (generation !== requestGeneration) return;
      current = item; photo = image;
      $("copy-title").textContent = item.original_image_id || item.copy_id;
      $("copy-meta").textContent = `${item.source || "source"} · ${item.split || "unassigned"} · ${item.decision?.action || "not checked here"}`;
      draw($("original"),item.original_geometry || {}); draw($("proposal"),item.proposal.geometry || {});
      const reasons = Array.isArray(item.proposal.quality?.reasons) ? item.proposal.quality.reasons.filter(reason => typeof reason === "string") : [];
      for (const reason of item.training_geometry?.reasons || []) if (!reasons.includes(reason)) reasons.push(reason);
      const reasonNames = {low_paint_confidence:"low paint confidence",short_observed_paint_span:"short visible paint span",
        flip_prediction_disagreement:"model disagreement after image flip",ambiguous_lane_geometry:"uncertain lane shape",unsupported_curve:"uncertain curve",
        probable_same_boundary_multiple_slots:"fragments may belong to one boundary but use different lane slots",
        invalid_painted_geometry:"invalid painted-line geometry",no_observed_painted_lines:"no observed painted lines"};
      const reasonText = reasons.map(reason => reasonNames[reason] || reason.replaceAll("_", " ")).join(", ");
      $("quality").textContent = item.quality_passes ? "Automatic proposal quality checks: pass" : `Model quality hold${reasonText ? ": " + reasonText : ""}`;
      $("approval-help").textContent = item.approval_allowed
        ? item.approval_overrides_quality
          ? "You can approve this proposal if its painted lines are correct. Your decision accepts the advisory quality hold only in this isolated experiment."
          : "Optional check: approve if these painted lines are correct. Automatic experiment training does not wait for individual approvals."
        : `${item.approval_block_reason || "This prediction cannot be approved."} You can exclude or skip this copy. This does not block the automatic experiment.`;
      const provenance = item.proposal.teacher_provenance || {};
      $("teacher").textContent = provenance.name || provenance.model || provenance.teacher || "Fresh teacher proposal";
      $("holdout").hidden = item.split === "train";
      $("review").hidden = false; message("");
    } catch (error) { message(error.message); }
    finally { if (generation === requestGeneration) lock(false); }
  }
  function uid() {
    if (globalThis.crypto?.randomUUID) return crypto.randomUUID();
    return `experiment-${Date.now()}-${Math.random().toString(36).slice(2)}`;
  }
  async function decide(action) {
    if (!current || busy) return;
    if (action === "approve" && !current.approval_allowed) { message(current.approval_block_reason || "This prediction cannot be approved."); return; }
    lock(true); message("Saving experiment decision…");
    if (!retryDecision || retryDecision.copy_id !== current.copy_id || retryDecision.action !== action) {
      retryDecision = {copy_id:current.copy_id,action,event_id:uid(),revision:current.revision,proposal_sha256:current.proposal_sha256};
    }
    try {
      await api(`/${encodeURIComponent(runId)}/decision`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(retryDecision)});
      after = current.copy_id; retryDecision = null; lock(false); await loadNext(); await status();
    } catch (error) {
      lock(false); message(error.message);
      if (error.status === 409) { retryDecision = null; await loadNext(); message("The copy changed. Review the current proposal before saving again."); }
    }
  }
  async function initialize() {
    lock(true); message("");
    try {
      const result = await api(""); $("run").replaceChildren();
      for (const item of result.experiments) { const option = document.createElement("option"); option.value = item.run_id; option.textContent = item.run_id; $("run").append(option); }
      if (!result.experiments.length) { $("phase").textContent = "No experiment prepared yet"; $("empty").hidden = false; return; }
      runId = result.experiments[0].run_id; await status();
    } catch (error) { message(error.message); }
    finally { lock(false); }
    await loadNext();
  }
  $("run").onchange = async () => { after = ""; runId = $("run").value; await status(); await loadNext(); };
  $("split").onchange = () => { after = ""; loadNext(); }; $("filter").onchange = () => { after = ""; loadNext(); };
  $("refresh").onclick = async () => { after = ""; if (!runId) return initialize(); await status(); await loadNext(); };
  for (const action of ["approve","exclude","skip"]) $(action).onclick = () => decide(action);
  setInterval(async () => { if (!busy && runId) { try { await status(); if (!current) await loadNext(); } catch (error) { message(error.message); } } },10000);
  initialize();
})();
