"use strict";
const $ = (id) => document.getElementById(id),
  canvas = $("canvas"),
  ctx = canvas.getContext("2d");
const colors = { 1: "#ffc273", 2: "#ff7979", 3: "#75c9ff", 4: "#91edb1" },
  slotNames = {
    1: "Outer left",
    2: "Your lane · left",
    3: "Your lane · right",
    4: "Outer right",
  },
  edgeColors = { left: "#f5a9e2", right: "#eddf89" },
  statusNames = {
    visible: "visible",
    absent: "no boundary here",
    unknown: "unclear / hidden",
    not_reviewed: "not reviewed",
  };
let current = null,
  photo = null,
  mask = null,
  imageReady = false,
  policyReady = false,
  busy = false,
  editing = false,
  zodNoVisiblePaint = false,
  lanes = [],
  selected = 0,
  mode = "move",
  drag = null;
let roadEdges = emptyEdges(),
  editingLayer = "lanes",
  selectedEdge = "right",
  editingCandidate = null,
  editingCandidateVersion = null,
  editingEdgeCandidate = null,
  editingEdgeCandidateVersion = null,
  hiddenModelProposals = { lanes: null, edges: null },
  assistIds = [],
  editUndo = [],
  editRedo = [],
  zoom = 1,
  skipped = [],
  seen = [],
  started = 0,
  viewer = null,
  lastDecision = null;
let toastTimer,
  pollTimer,
  pendingRequest = null,
  draftAvailable = null;
let reviewConflict = false, claimRenewing = false;
let reviewClient = (() => {
  try {
    let id = sessionStorage.getItem('bc-review-client');
    if (!id) sessionStorage.setItem('bc-review-client', id = uid());
    return id;
  } catch { return uid(); }
})();
// Duplicating a tab copies sessionStorage. Detect that copy before requesting
// work so each live tab has its own reservation and draft namespace.
const reviewDocument = uid();
const reviewClientReady = new Promise(resolve => {
  if (!globalThis.BroadcastChannel) { resolve(); return; }
  const channel = new BroadcastChannel('bc-review-tabs');
  channel.onmessage = ({data}) => {
    if (data?.type === 'probe' && data.client === reviewClient && data.document !== reviewDocument) {
      channel.postMessage({type: 'taken', document: data.document, client: reviewClient});
    } else if (data?.type === 'taken' && data.document === reviewDocument && data.client === reviewClient) {
      reviewClient = uid();
      try { sessionStorage.setItem('bc-review-client', reviewClient); } catch {}
    }
  };
  channel.postMessage({type: 'probe', client: reviewClient, document: reviewDocument});
  setTimeout(resolve, 75);
});
function uid() {
  if (globalThis.crypto?.randomUUID) return crypto.randomUUID();
  const b = new Uint8Array(16);
  crypto.getRandomValues(b);
  b[6] = (b[6] & 15) | 64;
  b[8] = (b[8] & 63) | 128;
  const s = [...b].map((v) => v.toString(16).padStart(2, "0")).join("");
  return `${s.slice(0, 8)}-${s.slice(8, 12)}-${s.slice(12, 16)}-${s.slice(16, 20)}-${s.slice(20)}`;
}
function copy(v) {
  return JSON.parse(JSON.stringify(v));
}
function emptyEdges() {
  return {
    left: { status: "not_reviewed", kind: "pavement_edge", points: [] },
    right: { status: "not_reviewed", kind: "pavement_edge", points: [] },
  };
}
function storageGet(k) {
  try {
    return localStorage.getItem(k);
  } catch {
    return null;
  }
}
function storageSet(k, v) {
  try {
    localStorage.setItem(k, v);
    return true;
  } catch {
    return false;
  }
}
function storageRemove(k) {
  try {
    localStorage.removeItem(k);
  } catch {
    /* Private browsing can disable storage. */
  }
}
function textNode(tag, text, className = "") {
  const n = document.createElement(tag);
  n.textContent = text;
  if (className) n.className = className;
  return n;
}
function toast(message, error = false, preference = message) {
  const key = "bc-toast-hidden:" + preference;
  if (error) $("save-status").textContent = message;
  clearTimeout(toastTimer);
  if (storageGet(key) === "1") {
    $("toast").hidden = true;
    return;
  }
  const dismiss = textNode(
    "button",
    "Don't show again",
    "button quiet toast-dismiss",
  );
  dismiss.onclick = () => {
    storageSet(key, "1");
    $("toast").hidden = true;
  };
  $("toast").replaceChildren(textNode("span", message), dismiss);
  $("toast").classList.toggle("error", error);
  $("toast").hidden = false;
  toastTimer = setTimeout(
    () => ($("toast").hidden = true),
    error ? 8500 : 4500,
  );
}
async function confirmAction(message) {
  const key = "bc-confirm-hidden:" + message;
  if (storageGet(key) === "1") return true;
  const dialog = $("confirm-dialog");
  if (dialog.open) return false;
  $("confirm-message").textContent = message;
  $("confirm-remember").checked = false;
  return new Promise((resolve) => {
    let settled = false;
    const finish = (accepted) => {
      if (settled) return;
      settled = true;
      if (accepted && $("confirm-remember").checked) storageSet(key, "1");
      dialog.close();
      resolve(accepted);
    };
    $("confirm-accept").onclick = () => finish(true);
    $("confirm-cancel").onclick = () => finish(false);
    dialog.oncancel = (event) => {
      event.preventDefault();
      finish(false);
    };
    dialog.showModal();
    $("confirm-cancel").focus();
  });
}
$("reset-messages").onclick = () => {
  try {
    for (let i = localStorage.length - 1; i >= 0; i--) {
      const key = localStorage.key(i);
      if (
        key?.startsWith("bc-confirm-hidden:") ||
        key?.startsWith("bc-toast-hidden:")
      )
        storageRemove(key);
    }
    toast("Hidden messages and confirmations restored on this device.");
  } catch {
    toast("This browser cannot update saved message preferences.", true);
  }
};
function setOnline(online) {
  $("connection").classList.toggle("offline", !online);
  $("connection").replaceChildren(
    document.createElement("i"),
    document.createTextNode(online ? " PC connected" : " PC offline"),
  );
}
async function api(url, options = {}) {
  await reviewClientReady;
  let response;
  try {
    response = await fetch(url, {
      credentials: "same-origin",
      ...options,
      headers: { "Content-Type": "application/json", "X-Review-Client": reviewClient, ...options.headers },
    });
  } catch {
    setOnline(false);
    throw new Error(
      "Could not reach the PC. A save is not confirmed until the PC responds. Reconnect and retry the same decision.",
    );
  }
  let body;
  try {
    body = await response.json();
  } catch {
    const e = new Error(
      "The PC did not return a complete response. Retry the same decision.",
    );
    e.status = response.status;
    throw e;
  }
  if (!response.ok) {
    if (response.status === 401 && !url.endsWith("/pair")) showPairing();
    const e = new Error(
      typeof body.detail === "string"
        ? body.detail
        : "The request could not be saved. Check your input and try again.",
    );
    e.status = response.status;
    throw e;
  }
  setOnline(true);
  return body;
}
function post(url, data = {}) {
  return api(url, { method: "POST", body: JSON.stringify(data) });
}

// Point identities let Undo last point remove the last tap even if it was
// inserted between older points. They never enter the training annotations.
function prepareFeature(f) {
  f.points = (f.points || []).map((p) => p.slice(0, 2));
  if (!f._nodes || f._nodes.length !== f.points.length) {
    f._nodes = f.points.map(() => uid());
    f._added = [...f._nodes];
  }
  f._added = (f._added || f._nodes).filter((id) => f._nodes.includes(id));
  return f;
}
function draftLanes(g) {
  if (current?.zod_audit) return (g?.lanes || []).filter(l => l.slot && l.physical !== false).map(l => {
    const sourceSections = (l.segments || [l.points]).map(section => Array.isArray(section) ? section : section.points);
    const sections = sourceSections.map(section => simplify(section));
    const feature = prepareFeature({slot: l.slot, kind: l.kind || "unknown", points: sections.flat()});
    feature.segments = sections;
    feature._sectionFingerprint = JSON.stringify(feature.points);
    feature._sourceSections = sourceSections.map(section => section.map(p => p.slice(0, 2)));
    return feature;
  });
  return (g?.lanes || [])
    .filter((l) => l.slot && l.physical !== false)
    .map((l) =>
      prepareFeature({
        slot: l.slot,
        kind: l.kind || "solid",
        points: l.polyline_points
          ? copy(l.polyline_points)
          : simplify(l.points || []),
      }),
    );
}
function draftEdges(g) {
  const out = emptyEdges();
  for (const side of ["left", "right"])
    if (g?.road_edges?.[side])
      out[side] = { ...out[side], ...copy(g.road_edges[side]) };
  for (const e of Object.values(out)) prepareFeature(e);
  return out;
}
function activeFeature() {
  return editingLayer === "lanes"
    ? lanes[selected]
    : roadEdges[selectedEdge].status === "visible"
      ? roadEdges[selectedEdge]
      : null;
}
function activeName() {
  return editingLayer === "edges"
    ? `${selectedEdge === "left" ? "Left" : "Right"} road edge`
    : lanes[selected]
      ? `${lanes[selected].slot} · ${slotNames[lanes[selected].slot]}`
      : "No painted line selected";
}
function modelProposalKey() {
  return current?.teacher
    ? `${$("model-select").value}:${current.teacher.teacher_version || ""}`
    : null;
}
function modelProposalVisible(layer) {
  const key = modelProposalKey();
  return (
    $("show-teacher").checked &&
    key !== null &&
    hiddenModelProposals[layer] !== key
  );
}
function editState() {
  return copy({
    lanes,
    roadEdges,
    editingLayer,
    selected,
    selectedEdge,
    mode,
    editingCandidate,
    editingCandidateVersion,
    editingEdgeCandidate,
    editingEdgeCandidateVersion,
    hiddenModelProposals,
    assistIds,
    zodConfirmation: current?.zod_audit ? {
      noVisiblePaint: zodNoVisiblePaint,
    } : null,
  });
}
function checkpoint() {
  editUndo.push(editState());
  if (editUndo.length > 150) editUndo.shift();
  editRedo = [];
}
function applyState(s) {
  ({
    lanes,
    roadEdges,
    editingLayer,
    selected,
    selectedEdge,
    mode,
    editingCandidate,
    editingCandidateVersion = null,
    editingEdgeCandidate = null,
    editingEdgeCandidateVersion = null,
    hiddenModelProposals = { lanes: null, edges: null },
    assistIds = [],
  } = copy(s));
  if (current?.zod_audit) {
    zodNoVisiblePaint = Boolean(s.zodConfirmation?.noVisiblePaint);
  }
  drag = null;
  for (const f of [...lanes, ...Object.values(roadEdges)]) prepareFeature(f);
}
function changed() {
  if (current?.zod_audit && lanes.length) zodNoVisiblePaint = false;
  renderEditor();
  persistDraft();
  draw();
  if (current?.zod_audit && !zodNoVisiblePaint)
    $("save-status").textContent = "Draft only. Check the labels, then save the correction.";
}
function undoEdit() {
  if (!editing || busy || pendingRequest || !editUndo.length) return;
  editRedo.push(editState());
  applyState(editUndo.pop());
  changed();
}
function redoEdit() {
  if (!editing || busy || pendingRequest || !editRedo.length) return;
  editUndo.push(editState());
  applyState(editRedo.pop());
  changed();
}
function removeNode(f, i) {
  const id = f._nodes[i];
  f.points.splice(i, 1);
  f._nodes.splice(i, 1);
  f._added = f._added.filter((n) => n !== id);
}
function undoNode() {
  const f = activeFeature();
  if (!editing || busy || pendingRequest || !f?.points.length) return;
  checkpoint();
  const i = f._nodes.indexOf(f._added.at(-1));
  removeNode(f, i < 0 ? f.points.length - 1 : i);
  changed();
}
function draftFeatures() {
  return [
    ...lanes,
    ...Object.values(roadEdges).filter((edge) => edge.status === "visible"),
  ];
}
function pointOutsideImage(point) {
  return (
    !Number.isFinite(point[0]) ||
    !Number.isFinite(point[1]) ||
    point[0] < 0 ||
    point[0] >= canvas.width ||
    point[1] < 0 ||
    point[1] >= canvas.height
  );
}
function autoFixPoints() {
  if (!editing || busy || pendingRequest || draftAvailable?.pending) return;
  const features = draftFeatures().filter((feature) =>
    feature.points.some(
      (point) => point.every(Number.isFinite) && pointOutsideImage(point),
    ),
  );
  if (!features.length) {
    toast(
      "No movable out-of-image points. Remove any invalid points manually.",
      true,
    );
    return false;
  }
  checkpoint();
  let moved = 0,
    merged = 0;
  for (const feature of features) {
    prepareFeature(feature);
    const rows = new Map(),
      kept = [];
    feature.points.forEach((point, index) => {
      const valid = point.every(Number.isFinite),
        outside = valid && pointOutsideImage(point),
        entry = {
          point: valid
            ? [
                Math.max(0, Math.min(canvas.width - 1, point[0])),
                Math.max(0, Math.min(canvas.height - 1, point[1])),
              ]
            : point.slice(),
          id: feature._nodes[index],
          outside,
        };
      if (outside) moved++;
      const row = valid ? entry.point[1].toFixed(4) : entry.id;
      if (rows.has(row)) {
        const previous = rows.get(row);
        // Prefer an existing in-image point when clamping joins image rows.
        if (kept[previous].outside && !outside) kept[previous] = entry;
        merged++;
      } else {
        rows.set(row, kept.length);
        kept.push(entry);
      }
    });
    feature.points = kept.map((entry) => entry.point);
    feature._nodes = kept.map((entry) => entry.id);
    feature._added = feature._added.filter((id) => feature._nodes.includes(id));
  }
  changed();
  const unfinished = draftFeatures().some(
    (feature) =>
      feature.points.length < 2 ||
      Math.max(...feature.points.map((point) => point[1])) -
        Math.min(...feature.points.map((point) => point[1])) <
        2 ||
      feature.points.some(pointOutsideImage),
  );
  toast(
    `${moved} points moved inside the photo${merged ? `; ${merged} overlapping points merged` : ""}. ` +
      (unfinished
        ? "A boundary still needs manual correction. You can undo this fix."
        : "Point repair is undoable until you save."),
    unfinished,
    "point-repair",
  );
  return !unfinished;
}

function draftStorageKey(imageId) {
  return 'bc-draft-v2:' + imageId + ':' + encodeURIComponent(viewer?.reviewer || '') + ':' + reviewClient;
}
function draftKey() {
  return current ? draftStorageKey(current.image_id) : null;
}
function persistDraft(force = false) {
  if (!current || (!force && !editing && !pendingRequest)) return;
  const saved = storageSet(
    draftKey(),
    JSON.stringify({
      version: 2,
      image_id: current.image_id,
      revision: current.revision,
      source_fingerprint: current.fingerprint || null,
      state: editState(),
      notes: $("notes").value,
      pending: pendingRequest,
      reviewer: viewer?.reviewer,
      saved_at: Date.now(),
    }),
  );
  $("draft-status").textContent = saved
    ? "Draft saved on this device. Tap Save correction to send it to the PC."
    : "Draft is in memory only. Keep this page open until you save to the PC.";
}
function clearDraft() {
  if (draftKey()) storageRemove(draftKey());
  draftAvailable = null;
  $("draft-banner").hidden = true;
}
function offerDraft() {
  draftAvailable = null;
  $("draft-banner").hidden = true;
  try {
    const d = JSON.parse(storageGet(draftKey()) || storageGet('bc-draft-v2:' + current.image_id) || "null");
    if (!d || d.version !== 2 || d.image_id !== current.image_id || !d.state || d.reviewer !== viewer?.reviewer)
      return;
    draftAvailable = d;
  } catch {
    return;
  }
  const same = draftAvailable.revision === current.revision && (!current.zod_audit || draftAvailable.source_fingerprint === current.fingerprint),
    wrongReviewer =
      draftAvailable.pending && draftAvailable.reviewer !== viewer?.reviewer,
    pending =
      draftAvailable.pending && draftAvailable.reviewer === viewer?.reviewer;
  $("draft-message").textContent = wrongReviewer
    ? "This pending save belongs to another reviewer on this device. Reconnect as that reviewer or check Activity before discarding it."
    : pending
      ? "A save was not confirmed on this device. Restore it, then retry the same decision."
      : same
        ? "You have an unfinished draft saved on this device."
        : "An older draft exists, but the saved review has changed. Discard this stale draft.";
  $("restore-draft").disabled = wrongReviewer || (!same && !pending);
  $("draft-banner").hidden = false;
}
async function recoverStartupDraft() {
  // A committed save can disappear from the pending queue before its response
  // reaches this device. Find that exact image before opening a new queue item.
  const candidates = [];
  try {
    const count = Math.min(localStorage.length, 4096);
    for (let i = 0; i < count; i++) {
      const key = localStorage.key(i);
      if (!key?.startsWith("bc-draft-v2:")) continue;
      try {
        const d = JSON.parse(storageGet(key) || "null");
        if (
          d?.version !== 2 ||
          d.reviewer !== viewer?.reviewer ||
          typeof d.image_id !== "string" ||
          !d.image_id.length ||
          d.image_id.length > 100 ||
          (key !== draftStorageKey(d.image_id) && key !== "bc-draft-v2:" + d.image_id) ||
          !d.state ||
          !Array.isArray(d.state.lanes) ||
          !d.state.roadEdges ||
          (d.pending &&
            (d.pending.image_id !== d.image_id ||
              d.pending.reviewer !== viewer?.reviewer))
        )
          continue;
        candidates.push(d);
      } catch {
        // An unreadable local record must not prevent access to other photos.
      }
    }
  } catch {
    return false;
  }
  candidates.sort(
    (a, b) =>
      Number(Boolean(b.pending)) - Number(Boolean(a.pending)) ||
      Number(b.saved_at || 0) - Number(a.saved_at || 0),
  );
  if (!candidates.length) return false;
  busy = true;
  controls();
  try {
    for (const d of candidates.slice(0, 10)) {
      try {
        const result = await api(
          "/api/images/" + encodeURIComponent(d.image_id),
        );
        // Stale unfinished drafts remain available in Activity; an unconfirmed
        // request can still be retried idempotently after the revision changes.
        if (!d.pending && (result.revision !== d.revision || result.zod_audit && d.source_fingerprint !== result.fingerprint)) continue;
        await displayImage(result);
        return true;
      } catch (e) {
        if (e.status === 401) throw e;
        console.info("Could not reopen a local draft:", e.message);
      }
    }
    return false;
  } finally {
    busy = false;
    controls();
  }
}
$("restore-draft").onclick = () => {
  if (!draftAvailable || busy) return;
  const d = draftAvailable;
  if (
    (d.pending && d.reviewer !== viewer?.reviewer) ||
    (!d.pending && (d.revision !== current?.revision || current?.zod_audit && d.source_fingerprint !== current.fingerprint))
  )
    return;
  editing = !d.pending || d.pending.action === "corrected";
  applyState(d.state);
  $("notes").value = d.notes || "";
  pendingRequest = d.pending || null;
  if (current?.zod_audit && !d.state.zodConfirmation && d.pending) {
    zodNoVisiblePaint = Boolean(d.pending.no_visible_lane_paint);
  }
  draftAvailable = null;
  editUndo = [];
  editRedo = [];
  $("draft-banner").hidden = true;
  changed();
  controls();
  $("save-status").textContent = pendingRequest
    ? "Previous save not confirmed. Retry the same decision."
    : "Draft restored. Check it, then save to the PC.";
};
$("discard-draft").onclick = async () => {
  if (
    draftAvailable?.pending &&
    !(await confirmAction(
      "Discard this local retry? The decision may already be saved on the PC. Check Activity before making a new decision.",
    ))
  )
    return;
  clearDraft();
  toast("Local draft discarded. Saved reviews are unchanged.");
  controls();
};

function controls() {
  const emptyZodSample = Boolean(current?.zod_audit && !current.geometry.lanes.length);
  $("approve").textContent = current?.zod_audit
    ? emptyZodSample ? "Approve no-visible-paint sample" : "Approve conversion sample"
    : "Human approve";
  $("reject").textContent = current?.zod_audit ? "Bad conversion — hold group" : "Exclude photo";
  $("phone-exclude").textContent = current?.zod_audit ? "Bad lines" : "Exclude";
  const emptyZodDraft = Boolean(current?.zod_audit && editing && !lanes.length && zodNoVisiblePaint);
  $("save-correction").textContent = emptyZodDraft ? "Save no-visible-paint decision" : "Save correction";
  $("zod-empty-status").hidden = !emptyZodDraft;
  $("zod-empty-status").textContent = emptyZodDraft ? "Empty-label draft ready. Save the no-visible-paint decision to record it and move to the next sample. Model lines are reference predictions; they are not saved with this empty draft." : "";
  $("zod-no-paint").setAttribute("aria-pressed", String(zodNoVisiblePaint));
  $("quick-approve").textContent = emptyZodSample ? "Approve no paint" : "Approve sample";
  const enabled = Boolean(
      policyReady && current && imageReady && !busy && !draftAvailable?.pending,
    ),
    blocked =
      busy ||
      Boolean(pendingRequest) ||
      Boolean(draftAvailable?.pending) ||
      Boolean(window.reviewEase?.blocked());
  for (const id of [
    "approve",
    "reject",
    "correct",
    "save-correction",
    "skip",
    "no-lanes",
  ])
    $(id).disabled = !enabled;
  $("approve").hidden = editing;
  if (window.reviewEase?.blocked()) {
    for (const id of [
      "approve",
      "reject",
      "correct",
      "save-correction",
      "skip",
      "no-lanes",
    ])
      $(id).disabled = true;
  }
  $("correct").hidden = editing;
  $("save-correction").hidden = !editing;
  $("editor").hidden = !editing;
  $("drawing-bar").hidden = !editing;
  $("stage").classList.toggle("editing", editing);
  $("stage").classList.toggle("panning", editing && mode === "pan");
  $("show-teacher").disabled = !current?.teacher_options?.length;
  $("show-mask").disabled = current?.source_paint_available === false;
  $("undo").hidden = !lastDecision;
  $("undo").disabled = blocked;
  $("quick-undo").hidden = !lastDecision;
  $("quick-undo").disabled = blocked;
  document.body.classList.toggle('has-review-undo', Boolean(lastDecision));
  for (const id of [
    "split-filter",
    "status-filter",
    "phone-queue",
    "cancel-edit",
    "use-model",
    "use-model-edges",
    "refresh-models",
    "notes",
    "model-select",
    "edit-lanes",
    "edit-edges",
    "edge-left",
    "edge-right",
    "edge-status",
    "edge-kind",
    "trace-edge",
    "zod-no-paint",
  ])
    $(id).disabled = blocked;
  $("review-label-checks").disabled = blocked || editing || Number($("review-label-checks").dataset.pending || 0) === 0;
  $("activity").disabled = busy || Boolean(pendingRequest);
  $("discard-draft").disabled = busy || Boolean(pendingRequest);
  $("restore-draft").disabled =
    busy ||
    Boolean(pendingRequest) ||
    Boolean(
      draftAvailable?.pending && draftAvailable.reviewer !== viewer?.reviewer,
    ) ||
    Boolean(
      draftAvailable &&
        (draftAvailable.revision !== current?.revision || current?.zod_audit && draftAvailable.source_fingerprint !== current.fingerprint) &&
        !draftAvailable.pending,
    );
  const f = activeFeature();
  for (const id of ["add-point", "remove-point", "delete-lane"])
    $(id).disabled = blocked || !f;
  $("add-lane").disabled = blocked || lanes.length >= 4;
  $("lane-slot").disabled = blocked || !lanes[selected];
  $("lane-kind").disabled = blocked || !lanes[selected];
  $("edge-kind").disabled =
    blocked || roadEdges[selectedEdge].status !== "visible";
  const model = current?.teacher;
  $("use-model").disabled =
    blocked ||
    !(model?.lanes || []).some((l) => l.slot && l.points?.length >= 2);
  $("use-model-edges").disabled =
    blocked ||
    !Object.values(model?.road_edges || {}).some(
      (e) => e.status === "visible" && e.points?.length >= 2,
    );
  $("undo-node").disabled = blocked || !f?.points.length;
  $("auto-fix-points").disabled =
    blocked ||
    !editing ||
    !draftFeatures().some((feature) => feature.points.some(pointOutsideImage));
  $("undo-edit").disabled = blocked || !editUndo.length;
  $("redo-edit").disabled = blocked || !editRedo.length;
  $("finish-lane").disabled = blocked || !f || f.points.length < 2;
  $("pan-mode").disabled = blocked;
  for (const b of $("lane-list").querySelectorAll("button"))
    b.disabled = blocked;
  if (pendingRequest) {
    for (const id of [
      "approve",
      "reject",
      "save-correction",
      "correct",
      "skip",
      "no-lanes",
    ])
      $(id).disabled = true;
    const retry = {
      approved: "approve",
      rejected: "reject",
      corrected: "save-correction",
    }[pendingRequest.action];
    if (retry) $(retry).disabled = busy;
  }
  $("sample-quick-actions").hidden =
    !["audit_sample", "label_check", "zod_audit", "zod_repair"].includes($("status-filter").value) ||
    !current || editing;
  $("quick-approve").disabled = $("approve").disabled;
  $("quick-fix").disabled = $("correct").disabled;
  $("batch-review").disabled = Boolean(current?.zod_audit) || busy;
  if (current?.zod_audit && editing && !pendingRequest &&
      !lanes.length && !zodNoVisiblePaint) {
    $("save-correction").disabled = true;
  }
  if (reviewConflict) {
    for (const id of ['approve', 'reject', 'correct', 'save-correction', 'skip', 'no-lanes', 'quick-approve', 'quick-fix', 'use-model', 'use-model-edges']) $(id).disabled = true;
  }
  window.reviewEase?.applyFocus();
}

function showReviewConflict(message) {
  reviewConflict = true;
  persistDraft(true);
  $('review-conflict').hidden = false;
  $('review-conflict-message').textContent = message;
  controls();
}
async function renewReviewClaim() {
  if (!current || busy || pendingRequest || !viewer || claimRenewing || reviewConflict || document.hidden) return;
  const item = current;
  claimRenewing = true;
  try {
    await post('/api/images/' + encodeURIComponent(item.image_id) + '/claim', {revision: item.revision, fingerprint: item.fingerprint || null});
  } catch (error) {
    if (current === item && error.status === 409) showReviewConflict(error.message);
  } finally { claimRenewing = false; }
}
setInterval(renewReviewClaim, 60000);
document.addEventListener('visibilitychange', () => { if (!document.hidden) void renewReviewClaim(); });
$('conflict-latest').onclick = async () => {
  if (busy || !current) return;
  persistDraft();
  busy = true;
  controls();
  try { await displayImage(await api('/api/images/' + encodeURIComponent(current.image_id))); }
  catch (error) { showReviewConflict(error.message); }
  finally { busy = false; controls(); }
};
$('conflict-next').onclick = async () => {
  if (busy || !current) return;
  persistDraft();
  seen.push(current.image_id);
  editing = false;
  await loadNext();
};
$('conflict-download').onclick = () => {
  if (!current) return;
  persistDraft();
  const url = URL.createObjectURL(new Blob([storageGet(draftKey()) || JSON.stringify(current)], {type: 'application/json'}));
  const link = document.createElement('a');
  link.href = url; link.download = current.image_id + '-my-draft.json'; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
};
$('download-draft').onclick = () => $('conflict-download').click();
window.addEventListener('pagehide', () => {
  persistDraft();
  // Keep the lease through reloads: a delayed unload release could otherwise
  // erase the refreshed page's reservation. Abandoned leases expire normally.
});
let statsRefreshPromise = null;
let statsRefreshPending = false;
function refreshStats() {
  statsRefreshPending = true;
  if (!statsRefreshPromise) {
    statsRefreshPromise = (async () => {
      while (statsRefreshPending) {
        statsRefreshPending = false;
        await updateStats();
      }
    })().finally(() => { statsRefreshPromise = null; });
  }
  return statsRefreshPromise;
}
async function updateStats() {
  const s = await api("/api/stats");
  $("zod-audit-summary").hidden = !s.zod_audits;
  if (s.zod_audits) {
    const z = s.zod_audits;
    $("zod-audit-counts").textContent = `${z.pending.toLocaleString()} samples to check · ${(z.label_cleared || 0).toLocaleString()} labels cleared (${(z.individually_approved || 0).toLocaleString()} individually approved) · ${(z.explicit_repair_photos || 0).toLocaleString()} photos need repair · ${(z.awaiting_source_clearance || 0).toLocaleString()} await source audits · ${z.model_proposals_ready || 0} / ${z.model_proposals_total || 0} model comparisons ready`;
    if (z.prepared_zod_images) $("zod-audit-counts").textContent += ` · Latest snapshot: ${z.prepared_zod_images.toLocaleString()} ZOD images / ${z.prepared_train_images.toLocaleString()} total training images`;
  }
  const auditSamples = (s.active_source_audit_samples ?? s.source_audit_samples ?? 0) + (s.zod_audits?.pending || 0);
  const repairSamples = s.conversion_diagnostic_samples || 0;
  const labelChecks = s.label_checks_pending || 0;
  const totalLabelChecks = s.label_checks_total_pending ?? labelChecks;
  for (const id of ["status-filter", "phone-queue"])
    $(id).querySelector('option[value="label_check"]').textContent = `UFLD label checks (${labelChecks})`;
  $("review-label-checks").dataset.pending = String(labelChecks);
  $("label-check-summary").hidden = totalLabelChecks === 0;
  $("label-check-counts").textContent = `${labelChecks} ready to check${totalLabelChecks > labelChecks ? ` · ${totalLabelChecks - labelChecks} saved in Review later` : ""}. These are targeted checks from the missed-lane inspection; your existing labels are kept.`;
  $("review-label-checks").disabled = labelChecks === 0 || editing || busy || Boolean(pendingRequest) || Boolean(draftAvailable?.pending);
  for (const id of ["status-filter", "phone-queue"]) {
    $(id).querySelector('option[value="repair_sample"]').textContent = `Repair examples (${repairSamples})`;
  }
  $("review-repair-examples").disabled = repairSamples === 0;
  $("repair-example-hint").hidden = repairSamples === 0;
  $("repair-example-hint").textContent = `${repairSamples} representative repair examples help diagnose the remaining conversions. Reviewing one changes only that photo; related photos stay held. Complete Audit samples first.`;
  $("status-filter").querySelector('option[value="later"]').textContent = `Review later (${s.review_later || 0})`;
  $("phone-queue").querySelector('option[value="later"]').textContent = `Review later (${s.review_later || 0})`;
  const zodQueue = ["zod_audit", "zod_repair", "zod_later"].includes($("status-filter").value);
  $("phone-progress").textContent = zodQueue && s.zod_audits
    ? `${s.zod_audits.pending.toLocaleString()} audits · ${(s.zod_audits.prepared_zod_images || s.zod_audits.label_cleared || 0).toLocaleString()} ${s.zod_audits.prepared_zod_images ? 'exported' : 'cleared'}`
    : `${auditSamples} audits \u00b7 ${s.review_later || 0} later`;
  const flaggedConversions = s.flagged_source_conversions || 0;
  const otherManual = s.other_manual_pending || 0;
  $("pending").textContent = auditSamples.toLocaleString();
  $("review-backlog").textContent = `${s.review_later || 0} photos saved in Review later. ` + `${flaggedConversions.toLocaleString()} flagged conversions need conversion fixes and remain outside training. Manual correction is optional.${otherManual ? ` ${otherManual.toLocaleString()} other images need individual review.` : ""}`;
  $("accepted").textContent = s.accepted.toLocaleString();
  $("rejected").textContent = s.rejected.toLocaleString();
  $("corrected").textContent = s.corrected.toLocaleString();
  $("progress-value").textContent =
    `${s.reviewed.toLocaleString()} human-reviewed`;
  $("progress-fill").style.width =
    `${s.reviewed + s.pending ? (s.reviewed / (s.reviewed + s.pending)) * 100 : 0}%`;
  $("source-audit-summary").hidden = !s.source_audit_groups?.length;
  const revisedAuditPending = s.source_audit_groups?.some(
    (g) => (g.batch_id?.startsWith("comma-source-repair-audit") || g.batch_id?.startsWith("a2d2-source-repair-audit")) && g.state !== "passed",
  );
  $("review-source-sample").disabled = auditSamples === 0;
  $("source-audit-heading").textContent = s.deferred_source_audit_samples && auditSamples === 0 ? "Source audits ? samples saved for later" : auditSamples === 0
    ? "Source audits · up to date"
    : revisedAuditPending
      ? "Revised source conversions · fresh sampled review"
      : "Source annotations · sampled review";
  $("source-audit-hint").textContent = s.deferred_source_audit_samples && auditSamples === 0 ? `${s.deferred_source_audit_samples} skipped audit samples are in Review later. Their groups remain held until those samples are reviewed.` : auditSamples === 0
    ? "All available audit samples are reviewed. A flag means a conversion check is uncertain, not that every drawn line is wrong. Flagged conversions and failed audit groups stay out of training until their issues are resolved."
    : revisedAuditPending
    ? "Start with these fresh audit samples. A flag means an uncertain check, not necessarily a bad line. Other conversions stay held until their group's samples pass. Your previous reviews are preserved."
    : "Review the selected audit samples first. A flag means an uncertain conversion check; you do not need to redraw the flagged backlog to finish this audit. If a sample needs correction, its group stays held for a conversion fix.";
  $("source-audit-counts").textContent = `${auditSamples} audit samples left · ${flaggedConversions.toLocaleString()} flagged conversions excluded · ${Math.max(0, (s.awaiting_source_audit || 0) - (s.held_source_repairs || 0))} waiting for sample checks · ${s.held_source_repairs || 0} held after failed audits · ${s.source_validated || 0} source-validated without individual review`;
  const prepared = s.preparation.state === "ready",
    ex = s.export,
    currentExport =
      ex.revision === s.revision &&
      ["ready", "partial", "awaiting_review"].includes(ex.state);
  const steps = [
    [
      "Images & source masks",
      prepared
        ? `${s.registered.toLocaleString()} images verified`
        : s.preparation.stage || "Preparing",
      prepared ? "done" : "active",
    ],
    [
      "Painted-line extraction",
      `${s.curated.toLocaleString()} label files; some contain no lines`,
      prepared ? "done" : "",
    ],
    [
      "Quality checks & splits",
      prepared ? "Checks complete · recording sessions separated" : "Preparing",
      prepared ? "done" : "",
    ],
    [
      "Human decisions",
      `${s.reviewed} human-reviewed · ${auditSamples} audit samples left${otherManual ? ` · ${otherManual} individual reviews` : ""}`,
      auditSamples || otherManual ? "active" : "done",
    ],
    ...(flaggedConversions ? [["Conversion repairs", `${flaggedConversions.toLocaleString()} flagged conversions excluded from training; manual correction is optional`, ""]] : []),
    [
      "Painted-line export",
      currentExport
        ? `${ex.exported} accepted images`
        : ex.state === "building"
          ? "Updating labels…"
          : "Waiting for accepted labels",
      currentExport && s.pending === 0 && !s.awaiting_source_audit ? "done" : "",
    ],
    [
      "Road-edge export",
      ex.road_edges
        ? `${ex.road_edges.exported} reviewed pairs · ${ex.road_edges.excluded_not_reviewed} accepted photos need edge review`
        : "Separate export · review both sides to include a photo",
      "",
    ],
    [
      "UFLDv2 baseline",
      s.ufld_training
        ? s.ufld_training.phase === "status_unavailable"
          ? "Training status unavailable · saved reviews are ready"
          : s.ufld_training.phase === "error"
          ? "Training stopped · check PC logs"
          : `Provisional · ${s.ufld_training.phase.replaceAll("_", " ")} · epoch ${s.ufld_training.epoch || 0}/${s.ufld_training.epochs} · step ${s.ufld_training.step || 0}/${s.ufld_training.total_steps || "…"}${s.ufld_training.best_val_f1 != null ? ` · best validation F1 ${(100 * s.ufld_training.best_val_f1).toFixed(1)}%` : ""}`
        : "Not started",
      s.ufld_training?.phase === "trained_awaiting_review" ? "done" : s.ufld_training && !["error", "status_unavailable"].includes(s.ufld_training.phase) ? "active" : "",
    ],
    ...(s.ufld_queue?.state === "waiting"
      ? [["Next UFLDv2 benchmark", `Queued after this run · ${s.ufld_queue.train_images} training images · ${s.ufld_queue.val_images} validation images · ${s.ufld_queue.epochs} epochs`, ""]]
      : s.ufld_queue?.state === "error"
        ? [["Next UFLDv2 benchmark", `Queue needs attention · ${s.ufld_queue.reason || "Check PC logs"}`, ""]]
        : []),
  ];
  $("milestones").replaceChildren(
    ...steps.map(([title, subtitle, status], i) => {
      const item = textNode("li", "", status),
        label = textNode("div", title);
      label.append(textNode("small", subtitle));
      item.append(
        textNode("span", status === "done" ? "✓" : String(i + 1), "step"),
        label,
      );
      return item;
    }),
  );
  $("export-state").textContent =
    ex.state === "error"
      ? "Export needs attention"
      : ex.state === "building"
        ? "Updating export…"
        : currentExport
          ? `${ex.exported} painted-line images · ${s.pending || s.awaiting_source_audit ? "partial validation" : "eligible labels ready"}`
          : "Export update pending";
  $("export-state").title = ex.error || (ex.errors || []).join("; ");
  if (s.models) {
    $("gpu-status").textContent = s.models.cuda
      ? `${s.models.gpu} · ${s.models.vram_gib} GB · CUDA ready`
      : "GPU software setup pending";
    const names = {
      sam3: "SAM 3",
      sam2_large: "SAM 2.1 Hiera Large",
      dinov3: "DINOv3 ViT-7B",
    };
    $("model-status").replaceChildren(
      ...Object.entries(s.models.models || {}).map(([n, m]) =>
        textNode(
          "p",
          `${names[n] || n}: ${m.state === "deferred_no_trained_decoder" ? "decoder not trained" : m.state === "decoder_training" ? `decoder training · step ${m.step || 0} / ${m.total_steps || "…"}` : m.state === "decoder_waiting_for_download" ? "downloading official 7B weights" : m.state === "decoder_fit_check" ? "checking real 7B GPU fit" : m.state === "waiting_for_access" ? "waiting for Hugging Face access" : m.state === "running" ? `${m.done} / ${m.total} images` : (m.state || "pending").replaceAll("_", " ")}`,
        ),
      ),
    );
  }
  if (s.paint_proposals) {
    const p = s.paint_proposals;
    $("model-status").append(
      textNode(
        "p",
        `Road-marking model · CPU: ${p.done || 0} / ${p.total || "…"} · ${p.state}`,
      ),
    );
  }
  if (
    current &&
    !editing &&
    !busy &&
    !pendingRequest &&
    ((!current.teacher_options?.some((o) => o.name === "sam3") &&
      ["running", "complete"].includes(s.models?.models?.sam3?.state)) ||
      (!current.teacher_options?.some((o) => o.name === "comma_segnet_fresh") &&
        ["running", "complete"].includes(s.paint_proposals?.state)) ||
      (!current.teacher_options?.some((o) => o.name === "dinov3_seg") &&
        ["running", "complete"].includes(s.dinov3_proposals?.state)))
  ) {
    await refreshModelOptions();
  }
  return s;
}
function updateSummary() {
  if (!current) {
    $("label-summary").textContent = "No image loaded";
    return;
  }
  const p = editing
      ? lanes
      : (current.geometry.lanes || []).filter((l) => l.slot),
    e = editing ? roadEdges : current.geometry.road_edges || emptyEdges();
  $("label-summary").textContent =
    `${p.length} painted ${p.length === 1 ? "line" : "lines"} · Left road edge: ${statusNames[e.left?.status || "not_reviewed"]} · Right: ${statusNames[e.right?.status || "not_reviewed"]}`;
}
function draw() {
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  const referenceCanvas = $("reference-canvas"),
    referenceContext = referenceCanvas.getContext("2d");
  referenceCanvas.width = canvas.width;
  referenceCanvas.height = canvas.height;
  if (!photo || !imageReady) return;
  ctx.drawImage(photo, 0, 0, canvas.width, canvas.height);
  referenceContext.drawImage(photo, 0, 0, canvas.width, canvas.height);
  if ($("show-mask").checked && mask)
    referenceContext.drawImage(mask, 0, 0, canvas.width, canvas.height);
  if (current.crop) {
    const c = current.crop;
    for (const ctx of [canvas.getContext("2d"), referenceContext]) {
    ctx.fillStyle = "rgba(0,0,0,.23)";
    ctx.fillRect(0, 0, canvas.width, c.y0);
    ctx.fillRect(0, c.y1, canvas.width, canvas.height - c.y1);
    ctx.save();
    ctx.strokeStyle = "rgba(255,255,255,.32)";
    ctx.setLineDash([7, 7]);
    ctx.strokeRect(c.x0, c.y0, c.x1 - c.x0, c.y1 - c.y0);
    ctx.restore();
    }
  }
  const scale =
    canvas.width / Math.max(canvas.getBoundingClientRect().width, 1);
  function line(f, color, label, active = false, dashed = false, target = ctx) {
    const ctx = target;
    const pts = f.points || [];
    if (!pts.length) return;
    ctx.save();
    ctx.strokeStyle = color;
    ctx.lineWidth = (active ? 3 : 2.3) * scale;
    ctx.lineJoin = "round";
    ctx.lineCap = "round";
    if (dashed) ctx.setLineDash([8 * scale, 5 * scale]);
    ctx.beginPath();
    const sections = Array.isArray(f.segments) && (!f._sectionFingerprint || f._sectionFingerprint === JSON.stringify(pts)) ? f.segments : [pts];
    sections.forEach(section => {
      const points = Array.isArray(section) ? section : section.points;
      (points || []).forEach((p, i) => i ? ctx.lineTo(p[0], p[1]) : ctx.moveTo(p[0], p[1]));
    });
    ctx.stroke();
    ctx.setLineDash([]);
    const bottom = pts.reduce((a, b) => (a[1] > b[1] ? a : b)),
      tx = Math.min(bottom[0] + 7 * scale, canvas.width - 36 * scale);
    ctx.font = `600 ${11 * scale}px system-ui`;
    ctx.lineWidth = 3 * scale;
    ctx.strokeStyle = "#101710";
    ctx.strokeText(label, tx, bottom[1] - 7 * scale);
    ctx.fillStyle = color;
    ctx.fillText(label, tx, bottom[1] - 7 * scale);
    if (active)
      for (const p of pts) {
        ctx.beginPath();
        ctx.fillStyle = "#fbfffb";
        ctx.strokeStyle = color;
        ctx.lineWidth = 1.8 * scale;
        ctx.arc(p[0], p[1], 5.5 * scale, 0, Math.PI * 2);
        ctx.fill();
        ctx.stroke();
      }
    ctx.restore();
  }
  if (modelProposalVisible("lanes"))
    (current.teacher.lanes || [])
      .filter((l) => l.slot)
      .forEach((l) => line(l, "#d1a4f7", "Model", false, !current.teacher.boundary_completion, referenceContext));
  if (current?.zod_audit && $("zod-observed").checked) {
    for (const lane of current.source_geometry?.lanes || []) {
      const sections = lane.observed_paint_segments || lane.segments || [lane.points];
      line({...lane, segments: sections, points: sections.flatMap(section => Array.isArray(section) ? section : section.points)},
        "#e6b974", "Observed paint", false, false, referenceContext);
    }
  }
  if (
    modelProposalVisible("edges") &&
    current.teacher?.road_edges &&
    !window.reviewEase?.paintOnly()
  ) {
    for (const side of ["left", "right"]) {
      const edge = current.teacher.road_edges[side];
      if (edge?.status === "visible")
        line(
          edge,
          "#c995ff",
          side === "left" ? "Model edge L" : "Model edge R",
          false,
          true,
          referenceContext,
        );
    }
  }
  if ($("show-labels").checked || editing)
    (editing ? lanes : current.geometry.lanes || [])
      .filter((l) => l.slot)
      .forEach((l, i) =>
        line(
          l,
          colors[l.slot],
          String(l.slot),
          editing && editingLayer === "lanes" && i === selected,
        ),
      );
  if ($("show-edges").checked || editing) {
    const e = editing ? roadEdges : current.geometry.road_edges || emptyEdges();
    for (const side of ["left", "right"])
      if (e[side]?.status === "visible")
        line(
          e[side],
          edgeColors[side],
          side === "left" ? "Edge L" : "Edge R",
          editing && editingLayer === "edges" && selectedEdge === side,
          true,
        );
  }
  updateSummary();
  window.reviewEase?.drawSeeds(ctx, scale);
}
async function loadNext(reset = false) {
  if (busy) return;
  if (pendingRequest || draftAvailable?.pending) {
    toast(
      "Restore and retry the unconfirmed decision, or discard its local retry, before changing photos.",
      true,
    );
    return;
  }
  if (
    editing &&
    !(await confirmAction(
      "Leave this unfinished draft on this device and move to another photo?",
    ))
  )
    return;
  busy = true;
  controls();
  if (reset) {
    skipped = [];
    seen = [];
  }
  $("stage-message").textContent = "Loading the next image…";
  $("stage-message").hidden = false;
  try {
    const params = new URLSearchParams({
      split: $("split-filter").value,
      status: $("status-filter").value,
      exclude: [...new Set([...skipped, ...seen])].join(","),
    });
    await displayImage(await api("/api/next?" + params));
  } catch (e) {
    toast(e.message, true);
    $("stage-message").textContent = e.message;
  } finally {
    busy = false;
    controls();
  }
}
function labelCheckTopics(check) {
  const names = {
    visible_span: "Visible paint endpoints",
    paint_versus_road_edge: "Paint versus road edge",
    double_line_slots: "Double-line boundary slots",
    empty_label_with_possible_paint: "Possible paint missing from empty labels",
    possible_outer_paint_omission: "Possible missing outer painted boundary",
    opposite_carriageway: "Opposite carriageway",
  };
  return (Array.isArray(check?.topics) ? check.topics : [])
    .map((topic) => names[topic] || String(topic).replaceAll("_", " ").replaceAll("-", " "))
    .join(" · ");
}
function renderLabelCheck(check) {
  $("label-check-note").hidden = !check;
  $("label-check-heading").textContent = check?.state === "completed" ? "UFLD label check · completed" : "UFLD label check · please inspect";
  $("label-check-topics").textContent = labelCheckTopics(check);
  $("label-check-observation").textContent = check?.observation || "";
}
async function displayImage(result) {
  if (result) {
    await post('/api/images/' + encodeURIComponent(result.image_id) + '/claim', {
      revision: result.revision, fingerprint: result.fingerprint || null, activate: true,
    });
  }
  const previous = current;
  if (previous && previous.image_id !== result?.image_id) {
    void post('/api/images/' + encodeURIComponent(previous.image_id) + '/claim', {revision: previous.revision, release: true}).catch(() => {});
  }
  reviewConflict = false;
  $('review-conflict').hidden = true;
  current = result;
  $("zod-audit-note").hidden = !result?.zod_audit;
  zodNoVisiblePaint = false;
  $("zod-observed-control").hidden = !result?.geometry?.boundary_completion;
  if (result?.zod_audit) {
    const completion = result.geometry.boundary_completion;
    const deduplication = result.geometry.boundary_deduplication;
    $("zod-sample-info").textContent = `This sample represents ${result.group_images.toLocaleString()} photos. ${result.group_state === "needs_repair" ? "The group is held for further repair. " : ""}${completion ? `Continuous lane-path target: ${completion.interpolated_gaps} gaps connected. ${completion.requires_repair ? "Boundary association or slot order needs repair. " : ""}Original painted sections remain available for comparison. ` : ""}Inspect the boundary path and lane slots; use a model proposal only after checking it. ${result.qc_reasons.map(value => value.replaceAll("_", " ")).join("; ")}`;
    if (deduplication) $('zod-sample-info').textContent += ` Paired paint: ${deduplication.merged_pairs.length} duplicate outer/main assignments consolidated. ${deduplication.suspect_pairs.length ? 'Close boundaries still need inspection; separate narrow lanes have been preserved.' : 'Each paired marking occupies one lane slot.'}`;
  }
  renderLabelCheck(result?.label_check);
  $("queue-heading").textContent = $("status-filter").value === "label_check" ? "UFLD label checks" : "Review queue";
  $("restore-later").hidden = !result?.review_later;
  if (result?.zod_audit) $("restore-later").hidden = true;
  $("skip").textContent = result?.review_later ? "Keep for later \u2192" : "Skip for now \u2192";
  window.reviewEase?.resetPhoto();
  imageReady = false;
  editing = false;
  lanes = [];
  roadEdges = emptyEdges();
  mask = null;
  photo = null;
  pendingRequest = null;
  mode = "move";
  drag = null;
  editingCandidate = null;
  editingCandidateVersion = null;
  editingEdgeCandidate = null;
  editingEdgeCandidateVersion = null;
  hiddenModelProposals = { lanes: null, edges: null };
  assistIds = [];
  editingLayer = "lanes";
  selectedEdge = "right";
  editUndo = [];
  editRedo = [];
  draftAvailable = null;
  $("notes").value = "";
  $("draft-banner").hidden = true;
  $("draft-status").textContent = "";
  $("stage").scrollTop = 0;
  $("stage").scrollLeft = 0;
  setZoom(1);
  controls();
  updateSummary();
  if (!result) {
    $("stage-message").textContent =
      $("status-filter").value === "label_check"
        ? "No UFLD label checks are waiting in this split. Skipped checks are in Review later; your saved labels are preserved."
        : $("status-filter").value === "audit_sample"
        ? "No audit samples are waiting. New samples appear after the next import batch finishes conversion and quality checks. Flagged conversions remain excluded; you do not need to redraw that backlog."
        : ["zod_audit", "zod_repair", "zod_later"].includes($("status-filter").value)
        ? "No ZOD sample is available in this queue. Repair-held and skipped samples remain separate; check the clearance counts before training."
        : "This queue is clear. Change a filter to revisit images or return to skipped ones.";
    $("stage-message").hidden = false;
    $("image-name").textContent = "No more images in this queue";
    $("provenance").textContent = "Your saved decisions stay on the PC.";
    draw();
    return;
  }
  const id = result.image_id;
  canvas.width = result.width;
  canvas.height = result.height;
  $("image-name").textContent = id;
  const splitLabel = { train: "TRAIN", val: "VALIDATION", test: "TEST" }[result.split] || "UNASSIGNED";
  $("split-badge").textContent = splitLabel;
  const count = (result.geometry.lanes || []).filter((l) => l.slot).length;
  const revisedConversion = !!result.source_conversion?.repair_policy;
  $("provenance").textContent =
    $("status-filter").value === "repair_sample"
      ? "Repair example: reviewing this photo does not approve related photos."
      : result.origin === "human"
      ? `Human ${result.review_status} · saved labels`
      : result.source_conversion?.state === "sample"
        ? revisedConversion
          ? "Revised source conversion · fresh audit sample; inspect the painted lines"
          : "Source audit sample · inspect the converted painted lines"
        : result.source_conversion?.state === "validated"
          ? "Source-validated conversion · sample-audited, not individually human reviewed"
          : result.source_conversion?.state === "awaiting_audit"
            ? revisedConversion
              ? "Revised source conversion · waiting for fresh sample checks; not in training yet"
              : "Waiting for sample audit · not in training yet"
            : revisedConversion && result.source_conversion?.state === "flagged"
              ? "Revised source conversion · flagged for individual inspection"
              : `Automatic paint proposal · ${count ? `${count} lines to inspect` : "no drawable lines; check the photo"}`;
  const tags = [];
  for (const [key, e] of Object.entries(result.tags || {}))
    if (e.value === true || key === "lighting")
      tags.push(
        key === "lighting"
          ? String(e.value).replaceAll("_", " ")
          : key.replaceAll("_", " "),
      );
  $("scene-tags").replaceChildren(
    ...tags.slice(0, 6).map((t) => textNode("span", t)),
  );
  const details = [
    ["Split", result.split === "val" ? "Validation" : result.split || "Unassigned"],
    ["Source", result.source],
    ["Dimensions", `${result.width} × ${result.height}`],
    [
      "Status",
      ["approved", "corrected", "rejected"].includes(result.review_status)
        ? `Human ${result.review_status}`
        : result.review_later ? "Review later" : result.source_conversion?.state?.replaceAll("_", " ") || "To review",
    ],
  ];
  $("image-details").replaceChildren(
    ...details.flatMap(([k, v]) => [textNode("dt", k), textNode("dd", v)]),
  );
  $("qc-issues").replaceChildren(
    ...(result.qc?.issues || [])
      .filter((i) => i.code !== "no_lanes")
      .slice(0, 6)
      .map((i) => textNode("span", i.code.replaceAll("_", " "))),
  );
  populateModels();
  const img = new Image();
  await new Promise((resolve, reject) => {
    img.onload = resolve;
    img.onerror = () =>
      reject(
        new Error("Could not load the photo. No decision has been saved."),
      );
    img.src = result.image_url;
  });
  if (current?.image_id !== id) return;
  photo = img;
  imageReady = true;
  started = performance.now();
  $("stage-message").hidden = true;
  draw();
  offerDraft();
  controls();
  if ($("show-mask").checked && current.source_paint_available !== false) loadMask();
}
async function loadMask() {
  if (!current || mask) return;
  const id = current.image_id,
    img = new Image();
  img.onload = () => {
    if (current?.image_id === id) {
      mask = img;
      draw();
    }
  };
  img.onerror = () =>
    toast("Source paint is unavailable for this image.", true);
  img.src = current.mask_url;
}
function submittedEdges() {
  const out = {};
  for (const side of ["left", "right"]) {
    const e = roadEdges[side];
    out[side] = {
      status: e.status,
      kind: e.kind,
      points: e.status === "visible" ? e.points.map((p) => p.slice(0, 2)) : [],
    };
  }
  return out;
}
async function save(action) {
  if (
    busy || reviewConflict ||
    !policyReady ||
    !current ||
    !imageReady ||
    window.reviewEase?.blocked()
  )
    return;
  if (draftAvailable?.pending) {
    toast(
      "Restore or discard the unconfirmed save before making a decision.",
      true,
    );
    return;
  }
  if (pendingRequest && pendingRequest.action !== action) {
    toast("Retry the pending decision first.", true);
    return;
  }
  if (!pendingRequest) {
    if (
      action === "rejected" &&
      !(await confirmAction(
        current.zod_audit ? "Mark this conversion as bad and hold its whole group for repair?" : "Exclude this entire photo from both training datasets? To fix missing or wrong labels, choose Edit labels instead.",
      ))
    )
      return;
    if (action === "corrected") {
      if (
        draftFeatures().some((feature) =>
          feature.points.some(pointOutsideImage),
        )
      ) {
        if (!autoFixPoints()) return;
      }
      if (
        lanes.some((l) => l.points.length < 2) ||
        Object.values(roadEdges).some(
          (e) => e.status === "visible" && e.points.length < 2,
        )
      ) {
        toast(
          "Each visible boundary needs at least two points. Finish it, delete it, or change its visibility.",
          true,
        );
        return;
      }
      if (
        !lanes.length && !(current.zod_audit && zodNoVisiblePaint) &&
        !(await confirmAction(
          "Save with zero painted lane lines? Any road-edge labels will be kept separately.",
        ))
      )
        return;
    }
  }
  const body = pendingRequest || {
    image_id: current.image_id,
    action,
    revision: current.revision,
    event_id: uid(),
    reviewer: viewer.reviewer,
    notes: $("notes").value,
    seconds: Math.max(0, (performance.now() - started) / 1000),
    lanes:
      action === "corrected"
        ? lanes.map((l) => current.zod_audit && l._sectionFingerprint === JSON.stringify(l.points) ? {
            slot: l.slot, kind: l.kind, points: l._sourceSections.flat(), segments: l._sourceSections,
          } : ({
            slot: l.slot,
            kind: l.kind,
            points: l.points.map((p) => p.slice(0, 2)),
          }))
        : null,
    candidate: action === "corrected" ? editingCandidate : null,
    candidate_version: action === "corrected" ? editingCandidateVersion : null,
    assist_ids: action === "corrected" && assistIds.length ? assistIds : null,
    fingerprint: current.zod_audit?.fingerprint || null,
    // The explicit Approve/Save action is the whole-photo confirmation.
    complete_annotation: Boolean(current.zod_audit),
    no_visible_lane_paint: Boolean(current.zod_audit && (action === "approved"
      ? !current.geometry.lanes.length : zodNoVisiblePaint)),
    road_edge_candidate: action === "corrected" ? editingEdgeCandidate : null,
    road_edge_candidate_version:
      action === "corrected" ? editingEdgeCandidateVersion : null,
    road_edges: action === "corrected" ? submittedEdges() : null,
  };
  pendingRequest = copy(body);
  busy = true;
  controls();
  persistDraft();
  $("save-status").textContent = "Saving to PC…";
  try {
    const r = await post("/api/reviews", body);
    const wasZod = Boolean(current.zod_audit);
    pendingRequest = null;
    clearDraft();
    lastDecision = {
      event_id: r.event_id,
      reviewer: viewer.reviewer,
      image_id: current.image_id,
      source: wasZod ? 'zod_mirror' : null,
    };
    storageSet("bc-last-decision", JSON.stringify(lastDecision));
    $("save-status").textContent = wasZod ? "ZOD sample saved on PC. The collection remains held outside training." : "Saved on PC. Exports update automatically.";
    seen.push(current.image_id);
    editing = false;
    toast(
      wasZod ? `ZOD sample saved. ${r.group_state === "needs_repair" ? "Its group is held for repair." : "The group still needs its remaining sample checks."}` : action === "rejected"
        ? "Photo excluded. You can undo this decision."
        : "Labels saved to human approved.",
    );
    busy = false;
    await loadNext();
    if (
      current && current.image_id !== r.image_id &&
      ["audit_sample", "label_check"].includes($("status-filter").value)
    )
      $("stage").scrollIntoView({ block: "start", behavior: "auto" });
    void refreshStats().catch(() => {});
  } catch (e) {
    const refused = [400, 401, 403, 404, 409, 422].includes(e.status);
    if (refused) pendingRequest = null;
    $("save-status").textContent = refused
      ? "Not saved. Resolve the issue, then try again."
      : "Save not confirmed. Retry the same decision before editing.";
    persistDraft();
    if (e.status === 409) showReviewConflict(e.message);
    toast(e.message, true);
  } finally {
    busy = false;
    controls();
  }
}
function simplify(pts, tolerance = 1.5) {
  if (pts.length < 3) return pts.map((p) => p.slice(0, 2));
  const a = pts[0],
    b = pts.at(-1);
  let best = 0,
    index = 0;
  for (let i = 1; i < pts.length - 1; i++) {
    const p = pts[i],
      dx = b[0] - a[0],
      dy = b[1] - a[1],
      t = Math.max(
        0,
        Math.min(
          1,
          ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / (dx * dx + dy * dy || 1),
        ),
      ),
      d = Math.hypot(p[0] - a[0] - t * dx, p[1] - a[1] - t * dy);
    if (d > best) {
      best = d;
      index = i;
    }
  }
  if (best <= tolerance) return [a.slice(0, 2), b.slice(0, 2)];
  return [
    ...simplify(pts.slice(0, index + 1), tolerance).slice(0, -1),
    ...simplify(pts.slice(index), tolerance),
  ];
}
async function beginEdit(candidate = null) {
  if (
    !current ||
    busy ||
    pendingRequest ||
    !policyReady ||
    window.reviewEase?.blocked()
  )
    return;
  if (draftAvailable?.pending) {
    toast(
      "Restore or discard the unconfirmed save before starting a new draft.",
      true,
    );
    return;
  }
  if (!editing && draftAvailable) {
    if (
      !(await confirmAction(
        "Replace the unfinished draft on this device with a new draft?",
      ))
    )
      return;
    clearDraft();
  }
  if (editing) {
    checkpoint();
    if (candidate) lanes = draftLanes(current.teacher);
    editingCandidate = candidate;
    editingCandidateVersion = candidate
      ? current.teacher?.teacher_version
      : null;
    if (candidate) hiddenModelProposals.lanes = modelProposalKey();
    editingLayer = "lanes";
    selected = 0;
    mode = "move";
    changed();
    return;
  }
  editing = true;
  mode = "move";
  selected = 0;
  editingLayer = "lanes";
  editingCandidate = candidate;
  editingCandidateVersion = candidate ? current.teacher?.teacher_version : null;
  editingEdgeCandidate = null;
  editingEdgeCandidateVersion = null;
  lanes = draftLanes(candidate ? current.teacher : current.geometry);
  roadEdges = draftEdges(current.geometry);
  editUndo = [];
  editRedo = [];
  if (candidate) hiddenModelProposals.lanes = modelProposalKey();
  $("show-labels").checked = true;
  changed();
  $("save-status").textContent =
    "Draft only. Tap Save correction when you are done.";
}
function renderEditor() {
  window.reviewEase?.applyFocus();
  if (!editing) {
    controls();
    return;
  }
  for (const layer of ["lanes", "edges"]) {
    $("edit-" + layer).classList.toggle("selected", editingLayer === layer);
    $("edit-" + layer).setAttribute(
      "aria-selected",
      String(editingLayer === layer),
    );
  }
  $("lane-panel").hidden = editingLayer !== "lanes";
  $("edge-panel").hidden = editingLayer !== "edges";
  $("lane-list").replaceChildren(
    ...lanes.map((l, i) => {
      const b = textNode(
        "button",
        `${l.slot} · ${slotNames[l.slot]}`,
        "lane-chip" + (i === selected ? " selected" : ""),
      );
      b.append(textNode("small", `${l.kind} · ${l.points.length} points`));
      b.onclick = () => {
        if (busy || pendingRequest) return;
        selected = i;
        mode = "move";
        renderEditor();
        draw();
      };
      return b;
    }),
  );
  if (!lanes.length)
    $("lane-list").append(
      textNode(
        "p",
        "No painted lines. Add visible paint, or leave this empty for an unmarked road.",
        "hint",
      ),
    );
  if (lanes[selected]) {
    $("lane-slot").value = String(lanes[selected].slot);
    $("lane-kind").value = lanes[selected].kind;
  }
  for (const side of ["left", "right"]) {
    $("edge-" + side).classList.toggle("selected", selectedEdge === side);
    $("edge-" + side).setAttribute(
      "aria-pressed",
      String(selectedEdge === side),
    );
    $("edge-" + side).replaceChildren(
      document.createTextNode(
        `${side === "left" ? "Left" : "Right"} road edge`,
      ),
      textNode("small", statusNames[roadEdges[side].status]),
    );
  }
  $("edge-status").value = roadEdges[selectedEdge].status;
  $("edge-kind").value = roadEdges[selectedEdge].kind;
  const reviewed = Object.values(roadEdges).filter(
    (e) =>
      e.status !== "not_reviewed" &&
      (e.status !== "visible" || e.points.length >= 2),
  ).length;
  $("edge-review-progress").textContent =
    `${reviewed} of 2 sides reviewed. Review both sides to include this photo in the separate road-edge export. Unknown sides are excluded from training targets.`;
  const f = activeFeature();
  $("drawing-target").textContent = activeName();
  const instruction =
    mode === "pan"
      ? "Pan mode: scroll the photo. Tap Pan photo again to edit."
      : mode === "new" || mode === "add"
        ? `Tap to place points · ${f?.points.length || 0} points. Finish when the visible boundary is covered.`
        : mode === "remove"
          ? "Tap a handle to remove that point."
          : "Drag a handle to adjust. Use Add points to extend the selected boundary.";
  $("drawing-instruction").textContent = instruction;
  $("edit-mode").textContent = instruction;
  $("finish-lane").hidden = !["new", "add"].includes(mode);
  $("add-point").classList.toggle("active", mode === "add");
  $("remove-point").classList.toggle("active", mode === "remove");
  $("pan-mode").setAttribute("aria-pressed", String(mode === "pan"));
  $("delete-lane").textContent =
    editingLayer === "edges" ? "Remove edge trace" : "Delete painted line";
  controls();
  updateSummary();
}
function defaultEdgeForTracing() {
  const edge = roadEdges[selectedEdge],
    firstVisit = !edge._visited;
  // A draft default saves a tap. It never changes stored/legacy labels, and an
  // explicit visibility choice remains intact when the reviewer returns.
  edge._visited = true;
  if (firstVisit && edge.status === "not_reviewed") {
    checkpoint();
    edge.status = "visible";
  }
  if (edge.status === "visible" && !edge.points.length) mode = "new";
}
function selectLayer(layer) {
  if (!editing || busy || pendingRequest) return;
  editingLayer = layer;
  mode = "move";
  renderEditor();
  persistDraft();
  draw();
}
function focusCanvas() {
  if (window.innerWidth < 850)
    $("drawing-bar").scrollIntoView({ block: "start", behavior: "smooth" });
}
function canvasPoint(e) {
  const r = canvas.getBoundingClientRect();
  return [
    Math.max(
      0,
      Math.min(
        canvas.width - 1,
        ((e.clientX - r.left) * canvas.width) / r.width,
      ),
    ),
    Math.max(
      0,
      Math.min(
        canvas.height - 1,
        ((e.clientY - r.top) * canvas.height) / r.height,
      ),
    ),
  ];
}
function nearestPoint(p) {
  const scale = canvas.width / canvas.getBoundingClientRect().width;
  let found = null,
    best = 22 * scale;
  const candidates =
    editingLayer === "lanes"
      ? lanes.map((f, index) => ({ f, index }))
      : ["left", "right"]
          .filter((s) => roadEdges[s].status === "visible")
          .map((side) => ({ f: roadEdges[side], side }));
  for (const c of candidates)
    c.f.points.forEach((q, pi) => {
      const d = Math.hypot(q[0] - p[0], q[1] - p[1]);
      if (d < best) {
        best = d;
        found = { ...c, pi };
      }
    });
  return found;
}
canvas.addEventListener("pointerdown", (e) => {
  if (!editing || busy || pendingRequest || mode === "pan") return;
  e.preventDefault();
  const p = canvasPoint(e),
    f = activeFeature();
  if (mode === "new" || mode === "add") {
    if (!f) return;
    if (f.points.length >= 500) {
      toast("This boundary already has 500 points.", true);
      return;
    }
    if (f.points.some((q) => Math.abs(q[1] - p[1]) < 1)) {
      toast(
        "Place this point slightly higher or lower than the existing point.",
      );
      return;
    }
    checkpoint();
    const id = uid(),
      pairs = f.points.map((point, i) => ({ point, id: f._nodes[i] }));
    pairs.push({ point: p, id });
    pairs.sort((a, b) => b.point[1] - a.point[1]);
    f.points = pairs.map((x) => x.point);
    f._nodes = pairs.map((x) => x.id);
    f._added.push(id);
    changed();
    return;
  }
  const near = nearestPoint(p);
  if (!near) return;
  if (editingLayer === "lanes") selected = near.index;
  else selectedEdge = near.side;
  checkpoint();
  if (mode === "remove") {
    removeNode(near.f, near.pi);
    changed();
    return;
  }
  drag = { feature: near.f, pi: near.pi };
  canvas.setPointerCapture(e.pointerId);
  renderEditor();
  draw();
});
canvas.addEventListener("pointermove", (e) => {
  if (!drag || !editing || pendingRequest) return;
  e.preventDefault();
  const p = canvasPoint(e),
    f = drag.feature;
  if (drag.pi > 0) p[1] = Math.min(p[1], f.points[drag.pi - 1][1] - 1);
  if (drag.pi < f.points.length - 1)
    p[1] = Math.max(p[1], f.points[drag.pi + 1][1] + 1);
  p[1] = Math.max(0, Math.min(canvas.height - 1, p[1]));
  f.points[drag.pi] = p;
  draw();
});
for (const type of ["pointerup", "pointercancel", "lostpointercapture"])
  canvas.addEventListener(type, () => {
    if (drag) {
      drag = null;
      changed();
    }
  });
function setZoom(v) {
  zoom = Math.max(1, Math.min(3, v));
  $("canvas-wrap").style.width = `${zoom * 100}%`;
  $("reference-wrap").style.width = `${zoom * 100}%`;
  $("zoom-level").textContent = `${Math.round(zoom * 100)}%`;
  requestAnimationFrame(draw);
}
$("zoom-in").onclick = () => setZoom(zoom + 0.5);
$("zoom-out").onclick = () => setZoom(zoom - 0.5);
function showReference(visible) {
  $("reference-pane").hidden = !visible;
  $("image-comparison").classList.toggle("reference-hidden", !visible);
  $("toggle-reference").textContent = visible ? "Hide reference" : "Show reference";
  $("toggle-reference").setAttribute("aria-expanded", String(visible));
  storageSet("bc-reference-visible", visible ? "1" : "0");
  requestAnimationFrame(draw);
}
$("toggle-reference").onclick = () => showReference($("reference-pane").hidden);
showReference(storageGet("bc-reference-visible") !== "0");
let synchronizingViews = false;
for (const [source, target] of [["stage", "reference-stage"], ["reference-stage", "stage"]])
  $(source).addEventListener("scroll", () => {
    if (synchronizingViews) return;
    synchronizingViews = true;
    $(target).scrollTop = $(source).scrollTop;
    $(target).scrollLeft = $(source).scrollLeft;
    requestAnimationFrame(() => { synchronizingViews = false; });
  });
window.addEventListener("resize", () => requestAnimationFrame(draw));
$("correct").onclick = () => beginEdit();
$("edit-lanes").onclick = () => selectLayer("lanes");
$("edit-edges").onclick = () => selectLayer("edges");
$("cancel-edit").onclick = async () => {
  if (
    busy ||
    pendingRequest ||
    !(await confirmAction(
      "Discard this unsaved draft? Saved labels on the PC will stay unchanged.",
    ))
  )
    return;
  clearDraft();
  editing = false;
  hiddenModelProposals = { lanes: null, edges: null };
  if (current?.zod_audit) {
    zodNoVisiblePaint = false;
  }
  editUndo = [];
  editRedo = [];
  controls();
  draw();
  $("save-status").textContent = "Draft discarded. Saved labels are unchanged.";
};
$("add-lane").onclick = () => {
  if (busy || pendingRequest) return;
  const used = new Set(lanes.map((l) => l.slot)),
    slot = [2, 3, 1, 4].find((n) => !used.has(n));
  if (!slot) return;
  checkpoint();
  lanes.push(prepareFeature({ slot, kind: "solid", points: [] }));
  selected = lanes.length - 1;
  editingLayer = "lanes";
  mode = "new";
  changed();
  focusCanvas();
};
function finishBoundary() {
  if (busy || pendingRequest) return;
  if (activeFeature()?.points.length < 2) {
    toast(
      "Place at least two points, or remove the unfinished boundary.",
      true,
    );
    return;
  }
  mode = "move";
  changed();
}
$("finish-lane").onclick = finishBoundary;
$("delete-lane").onclick = () => {
  if (busy || pendingRequest || !activeFeature()) return;
  checkpoint();
  if (editingLayer === "lanes") {
    lanes.splice(selected, 1);
    selected = Math.max(0, selected - 1);
  } else
    roadEdges[selectedEdge] = prepareFeature({
      status: "not_reviewed",
      kind: roadEdges[selectedEdge].kind,
      points: [],
    });
  mode = "move";
  changed();
};
$("no-lanes").onclick = async () => {
  if (!current || busy || pendingRequest) return;
  if (current.zod_audit) {
    await prepareZodEmptyDraft();
    return;
  }
  if (!editing) await beginEdit();
  if (!editing) return;
  checkpoint();
  lanes = [];
  selected = 0;
  editingLayer = "lanes";
  mode = "move";
  changed();
  toast(
    "Painted lines cleared in this draft. Road edges are kept. Save when ready.",
  );
};
$("add-point").onclick = () => {
  mode = mode === "add" ? "move" : "add";
  renderEditor();
  persistDraft();
  focusCanvas();
};
$("remove-point").onclick = () => {
  mode = mode === "remove" ? "move" : "remove";
  renderEditor();
  persistDraft();
};
$("undo-node").onclick = undoNode;
$("auto-fix-points").onclick = autoFixPoints;
$("undo-edit").onclick = undoEdit;
$("redo-edit").onclick = redoEdit;
$("pan-mode").onclick = () => {
  mode = mode === "pan" ? "move" : "pan";
  renderEditor();
  persistDraft();
};
$("lane-slot").onchange = () => {
  const slot = Number($("lane-slot").value);
  if (lanes.some((l, i) => i !== selected && l.slot === slot)) {
    toast(
      "That position already has a line. Change or delete the other line first.",
      true,
    );
    $("lane-slot").value = String(lanes[selected].slot);
    return;
  }
  checkpoint();
  lanes[selected].slot = slot;
  changed();
};
$("lane-kind").onchange = () => {
  if (!lanes[selected]) return;
  checkpoint();
  lanes[selected].kind = $("lane-kind").value;
  changed();
};
for (const side of ["left", "right"])
  $("edge-" + side).onclick = () => {
    if (!editing || busy || pendingRequest) return;
    selectedEdge = side;
    mode = "move";
    defaultEdgeForTracing();
    renderEditor();
    persistDraft();
    draw();
  };
$("edge-status").onchange = async () => {
  const f = roadEdges[selectedEdge],
    status = $("edge-status").value;
  if (
    f.points.length &&
    status !== "visible" &&
    !(await confirmAction(
      "Remove this edge trace and change its visibility? You can undo this edit.",
    ))
  ) {
    $("edge-status").value = f.status;
    return;
  }
  checkpoint();
  f._visited = true;
  f.status = status;
  if (status !== "visible") {
    f.points = [];
    f._nodes = [];
    f._added = [];
  }
  mode = status === "visible" && !f.points.length ? "new" : "move";
  changed();
  if (mode === "new") focusCanvas();
};
$("edge-kind").onchange = () => {
  checkpoint();
  roadEdges[selectedEdge].kind = $("edge-kind").value;
  changed();
};
$("trace-edge").onclick = () => {
  if (busy || pendingRequest) return;
  checkpoint();
  roadEdges[selectedEdge].status = "visible";
  mode = roadEdges[selectedEdge].points.length ? "add" : "new";
  changed();
  focusCanvas();
};
$("approve").onclick = () => save("approved");
$("quick-approve").onclick = () => $("approve").click();
$("quick-fix").onclick = async () => {
  await beginEdit();
  if (editing) $("editor").scrollIntoView({ block: "start" });
};
$("reject").onclick = () => save("rejected");
$("save-correction").onclick = () => save("corrected");
$("skip").onclick = async () => {
  if (busy || pendingRequest || !current) return;
  if (editing && !(await confirmAction("Keep this unfinished draft on this device and save the photo in Review later?"))) return;
  const wasEditing = editing;
  if (editing) persistDraft();
  busy = true; controls();
  try {
    const saved = await post(`/api/images/${encodeURIComponent(current.image_id)}/defer`, {revision: current.revision, deferred: true, fingerprint: current.zod_audit?.fingerprint || null});
    if (current.zod_audit) {
      lastDecision = {event_id: saved.event_id, reviewer: viewer.reviewer, image_id: current.image_id, source: 'zod_mirror'};
      storageSet('bc-last-decision', JSON.stringify(lastDecision));
    }
    skipped.push(current.image_id); editing = false;
    busy = false; await loadNext();
    void refreshStats().catch(() => {});
    toast("Saved in Review later");
  } catch (error) { editing = wasEditing; toast(error.message, true); }
  finally { busy = false; controls(); }
};
$("restore-later").onclick = async () => {
  if (busy || pendingRequest || !current) return;
  if (editing && !(await confirmAction("Keep this draft on this device and return the photo to its review queue?"))) return;
  if (editing) persistDraft();
  busy = true; controls();
  try {
    await post(`/api/images/${encodeURIComponent(current.image_id)}/defer`, {revision: current.revision, deferred: false});
    editing = false; skipped.push(current.image_id);
    busy = false; await loadNext();
    void refreshStats().catch(() => {});
    toast("Returned to the review queue");
  } catch (error) { toast(error.message, true); }
  finally { busy = false; controls(); }
};
$("split-filter").onchange = () => loadNext(true);
function focusLabelChecks() {
  $("split-filter").value = "all";
  $("review-target").value = "lanes";
  $("review-target").dispatchEvent(new Event("change"));
  $("phone-queue").value = "label_check";
}
$("status-filter").onchange = () => {
  if ($("status-filter").value === "label_check") focusLabelChecks();
  loadNext(true);
};
$("review-label-checks").onclick = () => {
  $("status-filter").value = "label_check";
  $("status-filter").dispatchEvent(new Event("change"));
};
$("review-source-sample").onclick = () => { $("status-filter").value = "audit_sample"; $("split-filter").value = "all"; loadNext(true); };
$("review-zod-audits").onclick = () => { $("status-filter").value = "zod_audit"; $("phone-queue").value = "zod_audit"; $("split-filter").value = "all"; loadNext(true); };
$("zod-observed").onchange = draw;
async function prepareZodEmptyDraft() {
  if (!current?.zod_audit || busy || pendingRequest || draftAvailable?.pending) return;
  const id = current.image_id;
  if (!editing) await beginEdit();
  if (!editing || current?.image_id !== id) { controls(); return; }
  checkpoint();
  lanes = [];
  selected = 0;
  editingLayer = "lanes";
  editingCandidate = null;
  editingCandidateVersion = null;
  hiddenModelProposals.lanes = null;
  zodNoVisiblePaint = true;
  changed();
  $("save-status").textContent = "Empty-label draft ready. Save the no-visible-paint decision when ready.";
}
$("zod-no-paint").onclick = prepareZodEmptyDraft;
$("review-source-flagged").onclick = () => { $("status-filter").value = "flagged"; $("split-filter").value = "all"; loadNext(true); };
$("review-repair-examples").onclick = () => { $("status-filter").value = "repair_sample"; $("split-filter").value = "all"; loadNext(true); };
$("notes").oninput = persistDraft;
function populateModels() {
  const options = current?.teacher_options || [],
    preferred = storageGet("bc-model-preference") || "sam3";
  $("model-select").replaceChildren(
    ...options.map((o) => {
      const n = textNode("option", o.label);
      n.value = o.name;
      return n;
    }),
  );
  if (options.some((o) => o.name === preferred))
    $("model-select").value = preferred;
  const chosen = options.find((o) => o.name === $("model-select").value);
  if (current) current.teacher = chosen?.geometry || null;
  $("show-teacher").disabled = !options.length;
  $("model-options").hidden = !$("show-teacher").checked || !options.length;
  if (chosen) {
    const model = chosen.geometry;
    const sourceGuided =
      model.source_guided === true || chosen.name === "sam2_large";
    const paintCount = (model.lanes || []).filter(
      (l) => l.slot && l.points?.length >= 2,
    ).length;
    const edgeCount = Object.values(model.road_edges || {}).filter(
      (e) => e.status === "visible" && e.points?.length >= 2,
    ).length;
    const emptyReason =
      chosen.summary?.empty_reason ||
      model.model_meta?.empty_reason ||
      (sourceGuided && model.model_meta?.components === 0
        ? "No source paint to refine; independent lane inference was not run."
        : "");
    $("model-note").textContent =
      `${sourceGuided ? "Source-guided refinement" : "Independent image inference"}: ${paintCount} painted lines · ${edgeCount} road-edge candidates. ` +
      (emptyReason ||
        (!paintCount && !edgeCount
          ? "No drawable candidates passed the checks. This is not confirmation that the photo has no boundaries."
          : "Road edges approximate a model road-mask contour; inspect them before saving."));
    if (model.boundary_completion) $("model-note").textContent += ` Continuous boundary proposal: ${model.boundary_completion.interpolated_gaps} gaps connected. ${model.boundary_completion.requires_repair ? "Check boundary association and lane slots. " : ""}Interpolated paths are not painted pixels.`;
    if (model.model_meta?.evaluation_caveat)
      $("model-note").textContent +=
        " " + model.model_meta.evaluation_caveat + ".";
  } else
    $("model-note").textContent =
      "No model candidates are available for this photo yet.";
  controls();
}
async function refreshModelOptions(announce = false) {
  if (!current || busy || pendingRequest || draftAvailable?.pending) return;
  const id = current.image_id;
  try {
    const fresh = await api("/api/images/" + encodeURIComponent(id));
    if (current?.image_id !== id) return;
    current.teacher_options = fresh.teacher_options;
    populateModels();
    draw();
    if (announce) toast("Model candidates refreshed. Your draft is unchanged.");
  } catch (e) {
    if (announce) toast(e.message, true);
  }
}
$("show-labels").onchange = draw;
$("show-edges").onchange = draw;
$("show-teacher").onchange = async () => {
  if ($("show-teacher").checked) {
    hiddenModelProposals = { lanes: null, edges: null };
    persistDraft();
  }
  if (current && $("show-teacher").checked && !editing) {
    const id = current.image_id;
    try {
      const fresh = await api("/api/images/" + encodeURIComponent(id));
      if (current?.image_id === id)
        current.teacher_options = fresh.teacher_options;
    } catch (e) {
      toast(e.message, true);
    }
  }
  populateModels();
  draw();
};
$("model-select").onchange = () => {
  storageSet("bc-model-preference", $("model-select").value);
  populateModels();
  draw();
};
$("use-model").onclick = async () => {
  if (!current?.teacher || busy || pendingRequest) return;
  if (
    editing &&
    !(await confirmAction(
      "Replace only the painted lines in this draft with the selected model? Road edges will be kept.",
    ))
  )
    return;
  await beginEdit($("model-select").value);
};
$("refresh-models").onclick = () => refreshModelOptions(true);
$("use-model-edges").onclick = async () => {
  if (
    !current?.teacher?.road_edges ||
    busy ||
    pendingRequest ||
    draftAvailable?.pending
  )
    return;
  if (
    !(await confirmAction(
      "Copy this model's left and right road-edge candidates into your draft? Existing road-edge traces will be replaced. Painted lines will be kept.",
    ))
  )
    return;
  const candidate = current.teacher,
    name = $("model-select").value;
  if (!editing) await beginEdit();
  if (!editing) return;
  checkpoint();
  roadEdges = draftEdges(candidate);
  for (const e of Object.values(roadEdges)) e._visited = true;
  editingEdgeCandidate = name;
  editingEdgeCandidateVersion = candidate.teacher_version;
  hiddenModelProposals.edges = modelProposalKey();
  editingLayer = "edges";
  selectedEdge = roadEdges.right.status === "visible" ? "right" : "left";
  mode = "move";
  $("show-edges").checked = true;
  changed();
  toast(
    "Road-edge candidates copied into your draft. Inspect and correct them before saving.",
  );
};
$("show-mask").onchange = () => {
  if ($("show-mask").checked) loadMask();
  draw();
};
$("undo").onclick = async () => {
  if (busy || pendingRequest || !lastDecision) return;
  if (
    editing &&
    !(await confirmAction(
      "Leave this draft on this device and undo your last saved decision?",
    ))
  )
    return;
  persistDraft();
  busy = true;
  controls();
  try {
    const r = await post("/api/undo", lastDecision);
    seen = seen.filter((id) => id !== r.image_id);
    skipped = skipped.filter((id) => id !== r.image_id);
    lastDecision = null;
    storageRemove("bc-last-decision");
    await refreshStats();
    await displayImage(
      await api("/api/images/" + encodeURIComponent(r.image_id)),
    );
    toast("Last saved decision undone. Its earlier labels were restored.");
  } catch (e) {
    toast(e.message, true);
  } finally {
    busy = false;
    controls();
  }
};
$("quick-undo").onclick = () => $("undo").click();
$("export").onclick = async () => {
  try {
    await post("/api/export");
    toast("Separate painted-line and road-edge exports queued.");
  } catch (e) {
    toast(e.message, true);
  }
};
$("backup").onclick = async () => {
  try {
    const r = await post("/api/backup");
    toast("Review backup saved on the PC.");
    $("backup").title = r.folder;
  } catch (e) {
    toast(e.message, true);
  }
};
function showPairing() {
  viewer = null;
  $("workspace").hidden = true;
  $("pair-screen").hidden = false;
  $("pair-phone").hidden = true;
  clearInterval(pollTimer);
}
async function openWorkspace(s) {
  viewer = s;
  $("workspace").hidden = false;
  $("pair-screen").hidden = true;
  $("pair-phone").hidden = !s.local;
  $("logout").hidden = s.local;
  $("reviewer-label").textContent = `Reviewing as ${s.reviewer}`;
  try {
    const p = await api("/api/labeling-policy");
    policyReady = Boolean(
      (p.version === "brakecheck-boundaries-v2" ||
        p.policy?.version === "brakecheck-boundaries-v2") &&
        p.model_candidate_api >= 2,
    );
    if (!policyReady) throw new Error("Updating");
  } catch {
    policyReady = false;
    $("stage-message").textContent =
      "The reviewer is being updated. Refresh this page in a moment; saved reviews are safe.";
    $("stage-message").hidden = false;
    controls();
    return;
  }
  try {
    lastDecision = JSON.parse(storageGet("bc-last-decision") || "null");
  } catch {
    lastDecision = null;
  }
  if (lastDecision?.reviewer !== s.reviewer) lastDecision = null;
  const requestedQueue = new URLSearchParams(location.search).get("queue");
  if (["zod_audit", "zod_repair", "zod_later"].includes(requestedQueue)) {
    $("status-filter").value = requestedQueue;
    $("phone-queue").value = requestedQueue;
    $("split-filter").value = "all";
  }
  await refreshStats();
  if (!(await recoverStartupDraft())) {
    const requestedImage = new URLSearchParams(location.search).get("image");
    if (/^zod-[0-9]{6}$/.test(requestedImage || "")) await displayImage(await api("/api/images/" + requestedImage));
    else await loadNext();
  }
  await window.reviewEase?.restore();
  clearInterval(pollTimer);
  pollTimer = setInterval(
    () => refreshStats().catch((e) => console.info(e.message)),
    6000,
  );
}
let remoteAccessKey = "";
async function showLeaderboard() {
  $("leaderboard-dialog").showModal();
  $("leaderboard-status").textContent = "Loading reviewers...";
  try {
    const data = await api("/api/leaderboard");
    $("leaderboard-rows").replaceChildren(...data.reviewers.map((reviewer, index) => {
      const row = document.createElement("tr"), name = document.createElement("td");
      name.append(textNode("strong", `${index + 1}. ${reviewer.reviewer}`),
        textNode("small", `${reviewer.approved} approved / ${reviewer.corrected} corrected / ${reviewer.rejected} excluded`));
      row.append(name, textNode("td", reviewer.reviewed.toLocaleString()), textNode("td", reviewer.review_later.toLocaleString()));
      return row;
    }));
    $("leaderboard-status").textContent = data.reviewers.length ? "" : "No reviews yet. Your first saved decision will appear here.";
  } catch (error) { $("leaderboard-status").textContent = error.message; }
}
$("leaderboard").onclick = showLeaderboard;
$("phone-leaderboard").onclick = showLeaderboard;
$("leaderboard-close").onclick = () => $("leaderboard-dialog").close();
$("copy-remote-url").onclick = async () => {
  try { await navigator.clipboard.writeText($("remote-url").value); toast("Remote link copied"); }
  catch { $("remote-url").select(); toast("Select and copy the remote link"); }
};
$("pair-form").onsubmit = async (e) => {
  e.preventDefault();
  $("pair-error").textContent = "";
  const b = $("pair-form").querySelector("button");
  b.disabled = true;
  try {
    const name = $("reviewer-name").value.trim(),
      r = remoteAccessKey ? await post("/api/remote-pair", {access_key: remoteAccessKey, reviewer: name})
        : await post("/api/pair", {code: $("pair-code").value.trim(), reviewer: name});
    if (remoteAccessKey) history.replaceState(null, "", location.pathname + location.search);
    storageSet("bc-reviewer", name);
    await openWorkspace({ ...r, local: false });
  } catch (e) {
    $("pair-error").textContent = e.message;
  } finally {
    b.disabled = false;
  }
};
async function showHostPairing() {
  const i = await api("/api/host");
  $("host-pin").textContent = i.code;
  $("remote-share").hidden = !i.remote_url;
  $("remote-url").value = i.remote_url || "";
  if (i.remote_url) $("remote-qr").src = "/api/host/remote-qr?t=" + Date.now();
  $("host-urls").replaceChildren(
    ...i.urls.map((url) => {
      const a = textNode("a", url);
      a.href = url;
      return a;
    }),
  );
  $("pair-qr").src = "/api/host/qr?t=" + Date.now();
  $("pair-dialog").showModal();
}
$("pair-phone").onclick = () =>
  showHostPairing().catch((e) => toast(e.message, true));
$("revoke").onclick = async () => {
  if (
    !(await confirmAction(
      "Disconnect every paired phone and generate a new code?",
    ))
  )
    return;
  try {
    await post("/api/host/revoke");
    $("pair-dialog").close();
    await showHostPairing();
  } catch (e) {
    toast(e.message, true);
  }
};
$("logout").onclick = async () => {
  if (pendingRequest) {
    toast("Retry your pending save before disconnecting.", true);
    return;
  }
  try {
    await post("/api/logout");
    showPairing();
  } catch (e) {
    toast(e.message, true);
  }
};
for (const b of document.querySelectorAll(".close-dialog"))
  b.onclick = () => b.closest("dialog").close();
$("activity").onclick = async () => {
  if (!viewer || pendingRequest || busy) return;
  try {
    const rows = await api("/api/history");
    $("history-list").replaceChildren(
      ...rows.map((row) => {
        const div = textNode("div", "", "history-row"),
          label = textNode("div", "");
        label.append(
          textNode("strong", row.image_id),
          textNode(
            "small",
            `${row.action} · ${row.reviewer} · ${new Date(row.created_at * 1000).toLocaleString()}`,
          ),
        );
        const b = textNode("button", "View", "button");
        b.onclick = async () => {
          if (pendingRequest || busy) return;
          if (draftAvailable?.pending) {
            toast(
              "Restore or discard the unconfirmed save before opening another photo.",
              true,
            );
            return;
          }
          if (
            editing &&
            !(await confirmAction(
              "Leave this unfinished draft on this device and open the saved image?",
            ))
          )
            return;
          busy = true;
          controls();
          try {
            await displayImage(
              await api("/api/images/" + encodeURIComponent(row.image_id)),
            );
            $("history-dialog").close();
          } catch (e) {
            toast(e.message, true);
          } finally {
            busy = false;
            controls();
          }
        };
        div.append(label, b);
        return div;
      }),
    );
    if (!rows.length)
      $("history-list").append(
        textNode(
          "p",
          "No human decisions yet. Your first review will appear here.",
          "empty-state",
        ),
      );
    $("history-dialog").showModal();
  } catch (e) {
    toast(e.message, true);
  }
};
document.addEventListener("keydown", (e) => {
  if (
    e.repeat ||
    ["INPUT", "TEXTAREA", "SELECT"].includes(e.target.tagName) ||
    document.querySelector("dialog[open]") ||
    !viewer
  )
    return;
  if (editing && (e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "z") {
    e.preventDefault();
    e.shiftKey ? redoEdit() : undoEdit();
    return;
  }
  if (editing && (e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "y") {
    e.preventDefault();
    redoEdit();
    return;
  }
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  if (editing && e.key === "Backspace") {
    e.preventDefault();
    undoNode();
    return;
  }
  if (editing && e.key === "Escape") {
    e.preventDefault();
    if (["new", "add"].includes(mode)) finishBoundary();
    else {
      mode = "move";
      renderEditor();
    }
    return;
  }
  if (!editing && e.key.toLowerCase() === "y") save("approved");
  if (!editing && e.key.toLowerCase() === "n") save("rejected");
  if (!editing && e.key.toLowerCase() === "c") beginEdit();
});
window.addEventListener("beforeunload", (e) => {
  if (editing || busy || pendingRequest) {
    persistDraft();
    e.preventDefault();
    e.returnValue = "";
  }
});
window.addEventListener("resize", draw);
window.addEventListener("offline", () => setOnline(false));
window.addEventListener("online", () => {
  if (viewer) refreshStats().catch(() => {});
});
(async () => {
  const fragment = new URLSearchParams(location.hash.slice(1));
  remoteAccessKey = fragment.get("access") || "";
  if (remoteAccessKey) {
    $("pair-code").required = false;
    $("pair-code").closest("label").hidden = true;
    $("pair-screen").querySelector(".pair-card > p:not(.eyebrow)").textContent = "Enter your reviewer name to connect securely. Your completed photos appear on the leaderboard.";
    $("pair-form").querySelector("button").textContent = "Start reviewing";
  }
  const code = fragment.get("pair");
  if (code) {
    $("pair-code").value = code;
    history.replaceState(null, "", location.pathname);
  }
  $("reviewer-name").value = storageGet("bc-reviewer") || "";
  try {
    const s = await api("/api/session");
    if (s.paired) await openWorkspace(s);
    else showPairing();
  } catch (e) {
    showPairing();
    $("pair-error").textContent = e.message;
  }
})();
