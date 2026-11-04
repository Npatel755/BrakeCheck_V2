(() => {
  "use strict";
  const api = "/api/studio/simulation";
  const el = id => document.getElementById(id);
  const colors = ["#e6b974", "#ff777b", "#78d7ff", "#9ce69a"];
  let current = null;
  let busy = false;
  let serial = 0;

  async function request(path, options = {}) {
    const response = await fetch(api + path, {...options, credentials: "same-origin"});
    const value = await response.json();
    if (!response.ok) {
      if (response.status === 401) el("pair-link").hidden = false;
      throw new Error(typeof value.detail === "string" ? value.detail : "The request could not be completed. Refresh and try again.");
    }
    el("pair-link").hidden = true;
    return value;
  }
  function message(text, error = false) {
    el("message").textContent = text;
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
    el("refresh").disabled = value;
    el("split").disabled = value;
    el("queue").disabled = value;
  }
  function format(value) { return Number.isFinite(value) ? value.toLocaleString() : "—"; }

  async function status() {
    const result = await request("");
    const counts = result.counts;
    const metadata = result.metadata || {};
    el("imported").textContent = format(counts.imported || metadata.imported || metadata.converted || metadata.counts?.converted || 0);
    el("remaining").textContent = format(counts.pending);
    el("eligible").textContent = format(counts.eligible_train_candidates);
    const phase = String(metadata.state || "not_started").replaceAll("_", " ");
    el("import-state").textContent = result.audits_ready
      ? `${format(counts.groups)} conversion groups · ${format(counts.approved)} samples approved · ${format(counts.groups_blocked)} groups blocked`
      : `Import: ${phase}. Audit samples appear once conversion is complete.`;
    el("progress-note").textContent = result.policy;
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
    for (const lane of item.geometry.lanes) {
      if (!lane.points?.length) continue;
      context.strokeStyle = colors[lane.slot - 1] || "#ffffff";
      context.lineWidth = 4;
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
      context.font = "bold 23px system-ui";
      context.lineWidth = 5;
      context.strokeStyle = "#08140c";
      context.strokeText(String(lane.slot), x, y);
      context.fillStyle = colors[lane.slot - 1] || "#ffffff";
      context.fillText(String(lane.slot), x, y);
    }
  }

  async function showSample(item, ticket, focusPhoto = false) {
    await draw(item, ticket);
    if (ticket !== serial) return;
    el("photo").src = item.image_url;
    el("image-id").textContent = item.image_id;
    el("sample-meta").textContent = `${item.split === "train" ? "Train" : "Synthetic validation"} · ${item.geometry.lanes.length} converted lines · group of ${format(item.group_images)} photos · ${item.session_id}`;
    el("warnings").hidden = !item.qc_reasons.length;
    el("warnings").textContent = item.qc_reasons.length ? warningGuidance(item) : "";
    el("policy-risks").textContent = "Simulator geometry is not a visibility guarantee. Inspect actual paint, line slots and anything hidden by vehicles.";
    el("saved-decision").hidden = !item.decision;
    el("saved-decision").textContent = item.decision ? `${item.decision.action} · ${item.decision.reviewer}` : "";
    current = item;
    el("sample").hidden = false;
    if (focusPhoto === true) el("sample").querySelector(".photo-pair").scrollIntoView({block: "start"});
    message(item.previous_decision_stale ? "These labels changed after the previous decision. Check the current conversion again." : "Inspect both views, then approve, exclude or skip this sample.");
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
      const overview = await status();
      const query = new URLSearchParams({split: el("split").value, status: el("queue").value});
      if (typeof after === "string" && after) query.set("after", after);
      const queue = await request("/queue?" + query);
      if (ticket !== serial) return;
      const item = queue.items[0];
      if (!item) {
        el("empty").hidden = false;
        if (after) {
          el("empty-title").textContent = "Reached the end of this queue";
          el("empty-message").textContent = "Your queue selection is unchanged. Refresh to start again with any samples still waiting here.";
          message("Reached the end of the selected queue.");
          return;
        }
        el("empty-title").textContent = queue.audits_ready ? "No samples in this queue" : "Conversion is preparing the samples";
        el("empty-message").textContent = queue.audits_ready
          ? (el("queue").value === "pending" ? "This queue is clear. Skipped samples remain in Skipped for later; their groups stay held." : "Choose a different queue or split to inspect other samples.")
          : "The import continues in the background. You can leave this page open; status refreshes automatically.";
        message(queue.audits_ready ? "All available samples in this queue have a saved decision." : "Waiting for the imported photos and their converted labels.");
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
    if (busy || !current) return;
    const item = current;
    disabled(true);
    message("Saving your sample check…");
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
  for (const id of ["split", "queue"]) el(id).addEventListener("change", load);
  el("refresh").addEventListener("click", load);
  setInterval(() => {
    if (document.hidden || busy) return;
    if (!current) load();
    else status().catch(error => message(error.message, true));
  }, 20000);
  load();
})();
