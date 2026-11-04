(() => {
  "use strict";
  const el = id => document.getElementById(id);
  const colors = ["#e6b974", "#ff777b", "#78d7ff", "#9ce69a"];
  let current = null;
  let busy = false;
  let serial = 0;
  let originalDisplayed = false;

  function api() { if (el("source").value === "zod_mirror") return "/api/studio/zod";
    return el("source").value === "college_held_repairs"
    ? "/api/studio/college-repairs" : "/api/studio/real-sources/" + el("source").value; }
  async function request(path, options = {}) {
    const response = await fetch(api() + path, {...options, credentials: "same-origin"});
    const value = await response.json();
    if (!response.ok) {
      if (response.status === 401) el("pair-link").hidden = false;
      throw new Error(typeof value.detail === "string" ? value.detail : "Request failed. Refresh and try again.");
    }
    el("pair-link").hidden = true;
    return value;
  }
  function message(value, error = false) {
    el("message").textContent = value;
    el("message").classList.toggle("error", error);
  }
  function warningGuidance(item, saved = false) {
    const warnings = item.qc_reasons || [];
    const names = warnings.map(reason => reason.replaceAll("_", " ")).join(", ");
    const repairable = new Set(item.repairable_qc_reasons || []);
    const complete = new Set(item.complete_annotation_qc_reasons || []);
    const blocked = warnings.filter(reason => !repairable.has(reason) && !complete.has(reason));
    const prefix = saved ? "Correction saved. Remaining warning: " + names + ". " : "Conversion warnings: " + names + ". ";
    if (blocked.length) return prefix + "These source warnings need import repair: " + blocked.map(reason => reason.replaceAll("_", " ")).join(", ") + ". You can save line edits, then exclude or skip this sample.";
    if (warnings.some(reason => complete.has(reason))) return prefix + "This photo needs a complete manual label check. In Fix lines, add every missing visible painted boundary, correct the lane slots, then check the whole-photo confirmation and Save & approve.";
    return prefix + "Open Fix lines, check the warning confirmation after inspecting the corrected labels, then Save & approve.";
  }
  function disabled(value) {
    busy = value;
    for (const id of ["approve", "exclude", "skip", "fix"]) el(id).disabled = value || !current;
    if (current?.qc_reasons.length) el("approve").disabled = true;
    if (originalDisplayed) { el("approve").disabled = true; el("fix").disabled = true; }
    el("compare-original").disabled = value || !current;
    for (const id of ["source", "split", "queue", "refresh"]) el(id).disabled = value;
  }
  function count(value) { return Number.isFinite(value) ? value.toLocaleString() : "—"; }

  async function status() {
    const result = await request("");
    const counts = result.counts || {};
    const metadata = result.metadata || {};
    const progress = result.progress || {};
    el("imported").textContent = count(counts.imported || progress.completed_images || progress.processed || progress.converted || progress.downloaded || metadata.converted || metadata.imported || 0);
    const repairs = result.source === "college_held_repairs";
    const zod = result.source === "zod_mirror";
    el("remaining").textContent = count(zod ? counts.audits_remaining : result.audits_ready ? (repairs && el("queue").value === "needs_repair" ? counts.pending_problem_samples : counts.pending) : (progress.audits_remaining ?? metadata.audits_remaining ?? 0));
    el("repair-problems-option").hidden = !(repairs || zod);
    el("eligible").textContent = count(zod ? counts.prepared_zod_images || 0 : repairs ? counts.audit_passed_photos || 0 : counts.eligible_train_candidates || 0);
    el("eligible-label").textContent = zod ? "ZOD in latest prepared snapshot" : repairs ? "photos passing group audits" : "eligible train candidates";
    el("import-state").textContent = zod
      ? `ZOD: ${count(counts.groups)} conversion groups · ${count(counts.samples)} audit samples · ${count(counts.groups_blocked)} groups need repair · ${count(counts.imported || 0)} photos ready`
      : result.audits_ready
      ? `${count(counts.groups)} groups · ${count(counts.approved)} approved samples · ${count(counts.groups_blocked)} blocked groups`
      : `Import: ${String(progress.state || metadata.state || "not started").replaceAll("_", " ")}. Audit samples appear when conversion finishes.`;
    el("progress-note").textContent = result.policy || "Imported groups need sample checks before they can be used in training.";
    const heldNote = el("held-clearance-note");
    heldNote.hidden = result.source !== "college_station_lane_marking" || !counts.hold;
    heldNote.textContent = heldNote.hidden ? "" : `${count(counts.group_cleared_held || 0)} held photos cleared by existing group audits; ${count(counts.individually_cleared_held || 0)} individually; ${count(counts.held_uncleared ?? counts.hold)} still held. Group clearance applies only to the same warnings and unchanged lines. Edited sample lines and whole-photo fixes for missing labels apply only to those photos.`;
    el("repair-batch-note").hidden = !(repairs || zod);
    el("repair-batch-note").textContent = zod ? `${count(counts.groups_audited)} groups passed sample checks; ${count(counts.photos_in_repair_groups)} photos are in groups held for repair. Choose Needs repair to inspect failed or edited groups. Fixing one sample does not fix the rest of its group.` : repairs ? `${count(counts.ready_candidate_photos ?? counts.imported)} photos have candidates ready to audit; ${count(counts.problem_photos || 0)} need more repair or a usable visible span. ${count(counts.groups_needing_repair || 0)} groups need another repair. Choose Needs repair to inspect those problem groups. ${count(counts.not_staged)} original held photos are not in this batch. Your earlier 358 approvals are preserved. Passing these audits records candidate quality; training uses separately integrated cleared snapshots.` : "";
    return result;
  }

  async function draw(item, ticket) {
    const image = new Image();
    image.src = item.image_url;
    await image.decode();
    if (ticket !== serial) return;
    const canvas = el("lanes");
    const context = canvas.getContext("2d");
    canvas.width = item.geometry.width;
    canvas.height = item.geometry.height;
    context.drawImage(image, 0, 0, canvas.width, canvas.height);
    if (item.source === "zod_mirror" && el("zod-polygons").checked) {
      for (const polygon of item.source_polygons || []) {
        if (!polygon.points?.length) continue;
        context.beginPath();
        polygon.points.forEach((point, index) => index ? context.lineTo(point[0], point[1]) : context.moveTo(point[0], point[1]));
        context.closePath();
        context.fillStyle = polygon.class === "lm_dashed" ? "#ffe16422" : "#b0b0b011";
        context.strokeStyle = polygon.class === "lm_dashed" ? "#ffe164aa" : "#b0b0b077";
        context.lineWidth = 1.5;
        context.fill(); context.stroke();
      }
    }
    for (const lane of item.geometry.lanes || []) {
      if (!lane.points?.length) continue;
      context.strokeStyle = colors[lane.slot - 1] || "#ffffff";
      context.lineWidth = Math.max(2, canvas.width / 320);
      context.lineJoin = "round";
      context.lineCap = "round";
      const segments = Array.isArray(lane.segments) ? lane.segments : [lane.points];
      for (const segment of segments) {
        const points = Array.isArray(segment) ? segment : segment.points;
        if (!Array.isArray(points) || points.length < 2) continue;
        context.beginPath();
        points.forEach((point, index) => index ? context.lineTo(point[0], point[1]) : context.moveTo(point[0], point[1]));
        context.stroke();
      }
      const point = lane.points.reduce((bottom, next) => next[1] > bottom[1] ? next : bottom);
      const x = Math.max(7, Math.min(canvas.width - 30, point[0] + 8));
      const y = Math.max(25, Math.min(canvas.height - 8, point[1] - 6));
      context.font = `bold ${Math.max(16, canvas.width / 55)}px system-ui`;
      context.lineWidth = 5;
      context.strokeStyle = "#08140c";
      context.strokeText(String(lane.slot), x, y);
      context.fillStyle = colors[lane.slot - 1] || "#ffffff";
      context.fillText(String(lane.slot), x, y);
    }
  }

  async function showSample(item, ticket, focusPhoto = false) {
    originalDisplayed = false;
    await draw(item, ticket);
    if (ticket !== serial) return;
    el("photo").src = item.image_url;
    el("image-id").textContent = item.image_id;
    el("sample-meta").textContent = `${item.split.toUpperCase()} · ${item.geometry.lanes.length} converted lines · group of ${count(item.group_images)} · ${item.session_id} · ${item.group_key}`;
    const repairs = item.source === "college_held_repairs";
    const zod = item.source === "zod_mirror";
    el("exclude").textContent = zod ? "Bad conversion — hold group" : "Exclude";
    el("approve").textContent = zod ? "Conversion looks correct" : "Approve";
    el("zod-source-controls").hidden = item.source !== "zod_mirror";
    el("compare-original").hidden = !repairs;
    el("compare-original").textContent = "Show original lines";
    el("candidate-view-label").textContent = item.boundary_completion ? "Saved continuous middle boundary" : repairs ? "Repair candidate" : "Converted lane candidates";
    el("repair-info").hidden = !(repairs || zod);
    el("repair-info").textContent = zod ? `${item.group_samples || 3} sample(s) represent ${count(item.group_images)} photos with the same conversion signature. ${item.group_state === "needs_repair" ? "This group is held for converter repair. A corrected photo cannot clear the rest of the group." : "If any boundary is missed, misplaced, duplicated or in the wrong slot, choose Bad conversion to hold this group."}` : repairs ? `${item.candidate_origin === "model_proposal" ? "Model proposal" : "Source traces for recheck"}. ${item.repair_description} Original warnings: ${(item.original_qc_reasons || []).map(reason => reason.replaceAll("_", " ")).join(", ")}. ${item.audit_mode === "needs_repair" ? "This group still needs repair. Approval is blocked; inspect, exclude or skip it." : "Approve only if all visible painted boundaries and slots are correct."}` : "";
    if (zod && item.conversion_findings?.length) el("repair-info").textContent += " Known issue: " + item.conversion_findings.map(value => value.finding).join("; ");
    if (item.boundary_completion) el("repair-info").textContent += ` Middle line saved continuously across ${item.boundary_completion.interpolated_gaps} dash gaps. Original paint sections and your approval are preserved.`;
    el("warnings").hidden = !item.qc_reasons.length;
    el("warnings").textContent = item.qc_reasons.length ? warningGuidance(item) : "";
    el("policy-risks").textContent = item.annotation_policy_risks.length
      ? "Inspect carefully: " + item.annotation_policy_risks.join(", ").replaceAll("_", " ") + "."
      : "Inspect paint visibility, occlusion and lane ordering.";
    el("saved-decision").hidden = !item.decision;
    el("saved-decision").textContent = item.decision ? `${item.decision.action} · ${item.decision.reviewer}` : "";
    current = item;
    el("sample").hidden = false;
    if (focusPhoto === true) el("sample").querySelector(".photo-pair").scrollIntoView({block: "start"});
    message(item.previous_decision_stale ? "These labels changed since the previous decision. Check again." : "Inspect the original and converted views, then decide.");
  }

  async function load(focusPhoto = false, after = "") {
    if (busy) return;
    const ticket = ++serial;
    current = null;
    disabled(true);
    el("sample").hidden = true;
    el("empty").hidden = true;
    message("Loading sample…");
    try {
      await status();
      const query = new URLSearchParams({split: el("split").value, status: el("queue").value});
      if (typeof after === "string" && after) query.set("after", after);
      const result = await request("/queue?" + query);
      if (ticket !== serial) return;
      const item = result.items[0];
      if (!item) {
        el("empty").hidden = false;
        if (after) {
          el("empty-title").textContent = "Reached the end of this queue";
          el("empty-message").textContent = "Your queue selection is unchanged. Refresh to start again with any samples still waiting here.";
          message("Reached the end of the selected queue.");
          return;
        }
        el("empty-title").textContent = result.audits_ready ? "No samples in this queue" : "Conversion is preparing audit samples";
        el("empty-message").textContent = result.audits_ready
          ? "Choose another queue or split to inspect samples. Skipped groups remain held."
          : "The import can continue in the background; refresh here once it completes.";
        message(result.audits_ready ? "No sample waiting in this queue." : "Waiting for converted photos and audits.");
        return;
      }
      await showSample(item, ticket, focusPhoto);
    } catch (error) {
      message(error.message, true);
    } finally {
      disabled(false);
      if (current?.qc_reasons.length) el("approve").disabled = true;
    }
  }

  async function decide(action) {
    if (busy || !current || originalDisplayed && action === "approve") return;
    const item = current;
    disabled(true);
    message("Saving sample check…");
    try {
      const bytes = new Uint8Array(16);
      crypto.getRandomValues(bytes);
      const eventId = Array.from(bytes, b => b.toString(16).padStart(2, "0")).join("");
      await request("/decision", {method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({image_id: item.image_id, action, event_id: eventId, revision: item.revision, fingerprint: item.fingerprint})});
      current = null;
      disabled(false);
      const queue = el("queue").value;
      await load(true, queue === "all" || queue === action ? item.image_id : "");
    } catch (error) {
      message(error.message, true);
      disabled(false);
      if (current?.qc_reasons.length) el("approve").disabled = true;
    }
  }
  window.createSampleFixEditor({
    el, getCurrent: () => current, request, message, setBusy: disabled,
    onCancel: async () => {
      if (!current) return;
      disabled(true);
      try { await draw(current, ++serial); }
      finally { disabled(false); }
    },
    onSaved: async saved => {
      if (saved.decision?.action === "approve" && !saved.qc_reasons.length) {
        const queue = el("queue").value;
        current = null;
        disabled(false);
        await load(true, ["approve", "all"].includes(queue) ? saved.image_id : "");
        if (current) message("Correction saved and approved. Review the next sample.");
        return;
      }
      const imageId = current.image_id;
      const ticket = ++serial;
      current = null;
      disabled(true);
      try {
        const item = await request("/samples/" + encodeURIComponent(imageId));
        await showSample(item, ticket);
        await status();
        message(item.qc_reasons.length
          ? warningGuidance(item, true)
          : "Correction saved. This sample still needs approval.");
      } finally {
        disabled(false);
      }
    }
  });
  for (const action of ["approve", "exclude", "skip"]) el(action).addEventListener("click", () => decide(action));
  el("compare-original").addEventListener("click", async () => {
    if (busy || !current?.original_geometry) return;
    originalDisplayed = !originalDisplayed;
    disabled(true);
    try {
      await draw(originalDisplayed ? {...current, geometry: current.original_geometry} : current, ++serial);
      el("compare-original").textContent = originalDisplayed ? "Show repair candidate" : "Show original lines";
      el("candidate-view-label").textContent = originalDisplayed ? "Original source lines" : current.boundary_completion ? "Saved continuous middle boundary" : "Repair candidate";
      message(originalDisplayed ? "Viewing the original source lines. Switch back to the repair candidate before approving." : "Inspect the repair candidate, then approve, exclude or skip its group sample.");
    } catch (error) { message(error.message, true); }
    finally { disabled(false); }
  });
  for (const id of ["source", "split", "queue"]) el(id).addEventListener("change", () => {
    if (!["college_held_repairs", "zod_mirror"].includes(el("source").value) && el("queue").value === "needs_repair") el("queue").value = "pending";
    load();
  });
  el("refresh").addEventListener("click", load);
  const selected = new URLSearchParams(location.search).get("source");
  if (["college_station_lane_marking", "pandaset_plus", "college_held_repairs", "zod_mirror"].includes(selected)) el("source").value = selected;
  el("zod-polygons").addEventListener("change", () => {
    if (current && !busy) draw(current, ++serial).catch(error => message(error.message, true));
  });
  setInterval(() => {
    if (!document.hidden && !busy) status().then(result => {
      if (result.source === "zod_mirror" && result.audits_ready && !current) load();
    }).catch(error => message(error.message, true));
  }, 20000);
  load();
})();
