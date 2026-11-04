"use strict";
(() => {
  let picking = null,
    batchItems = [],
    batchBusy = false,
    pending = [],
    lastBatch = [];
  const batchDialog = $("batch-dialog");
  const key = (kind) => `bc-batch-${kind}:${viewer?.reviewer || ""}`;
  const persist = () => {
    storageSet(key("pending"), JSON.stringify(pending));
    storageSet(key("undo"), JSON.stringify(lastBatch));
  };
  function applyFocus() {
    const focused = $("review-target").value === "lanes";
    $("edit-edges").hidden = focused;
    $("use-model-edges").hidden = focused;
    $("show-edges").closest("label").hidden = focused;
    if (focused) {
      $("show-edges").checked = false;
      editingLayer = "lanes";
    }
    $("batch-review").disabled =
      Boolean(current?.zod_audit) || !policyReady ||
      editing ||
      busy ||
      Boolean(pendingRequest) ||
      Boolean(draftAvailable?.pending);
    $("assist-trace").disabled =
      Boolean(current?.zod_audit) || !editing || busy || Boolean(pendingRequest) || !activeFeature();
  }
  $("review-target").value = storageGet("bc-review-target") || "lanes";
  $("review-target").onchange = () => {
    storageSet("bc-review-target", $("review-target").value);
    if ($("review-target").value === "both") $("show-edges").checked = true;
    cancelTrace();
    applyFocus();
    renderEditor();
    draw();
  };
  function targetKey() {
    return `${current?.image_id}:${editingLayer}:${editingLayer === "edges" ? selectedEdge : lanes[selected]?.slot}`;
  }
  function cancelTrace() {
    picking = null;
    $("assist-cancel").hidden = true;
    draw();
  }
  $("assist-cancel").onclick = cancelTrace;
  $("assist-trace").onclick = () => {
    if (!editing || busy || pendingRequest || !activeFeature()) return;
    picking = { target: targetKey(), points: [] };
    $("assist-cancel").hidden = false;
    mode = "move";
    toast(
      "Tap the near end, then the far end of the same marking or edge. The model must support the trace.",
    );
    focusCanvas();
    draw();
  };
  canvas.addEventListener(
    "pointerdown",
    async (event) => {
      if (!picking) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      if (busy || pendingRequest) return;
      if (!editing || targetKey() !== picking.target) {
        cancelTrace();
        toast("The selected boundary changed. Start the assisted trace again.");
        return;
      }
      picking.points.push(canvasPoint(event));
      draw();
      if (picking.points.length < 2) return;
      const id = current.image_id,
        layer = editingLayer,
        feature = activeFeature();
      const points = copy(picking.points);
      picking = null;
      $("assist-cancel").hidden = true;
      busy = true;
      controls();
      try {
        const result = await post(
          `/api/images/${encodeURIComponent(id)}/trace`,
          {
            revision: current.revision,
            points,
            layer,
            side: selectedEdge,
          },
        );
        if (current?.image_id !== id) return;
        checkpoint();
        feature.points = result.points;
        delete feature._nodes;
        delete feature._added;
        prepareFeature(feature);
        assistIds.push(result.assist_id);
        if (layer === "edges") feature.status = "visible";
        mode = "move";
        changed();
        toast(
          "Model trace added to your draft. Inspect it; Undo edit restores the previous trace.",
        );
      } catch (error) {
        toast(error.message, true);
      } finally {
        busy = false;
        controls();
        applyFocus();
        draw();
      }
    },
    true,
  );
  for (const id of [
    "edit-lanes",
    "edit-edges",
    "lane-list",
    "edge-left",
    "edge-right",
    "undo-edit",
    "redo-edit",
    "cancel-edit",
  ])
    $(id).addEventListener("click", cancelTrace);

  function batchControls() {
    for (const id of ["batch-load", "batch-model", "batch-select"])
      $(id).disabled = batchBusy || pending.length > 0;
    $("batch-approve").disabled =
      batchBusy ||
      (!pending.length && !batchItems.some((item) => item.checkbox.checked));
    $("batch-approve").textContent = pending.length
      ? `Retry ${pending.length} pending saves`
      : "Approve selected";
    $("batch-undo").disabled =
      batchBusy || pending.length > 0 || !lastBatch.length;
    $("batch-close").disabled = batchBusy;
    for (const item of batchItems)
      item.checkbox.disabled = batchBusy || pending.length > 0 || !item.ready;
    controls();
  }
  function paint(target, image, geometry) {
    target.width = image.naturalWidth;
    target.height = image.naturalHeight;
    const context = target.getContext("2d"),
      scale = target.width / 500;
    context.drawImage(image, 0, 0);
    for (const lane of geometry.lanes || []) {
      if (!lane.slot || !lane.points?.length) continue;
      const points = lane.polyline_points || lane.points;
      context.beginPath();
      points.forEach((point, i) =>
        i ? context.lineTo(...point) : context.moveTo(...point),
      );
      context.strokeStyle = colors[lane.slot];
      context.lineWidth = 2 * scale;
      context.stroke();
    }
  }
  function clippedLanes(geometry, width, height) {
    return (geometry.lanes || [])
      .filter((lane) => lane.slot && lane.physical !== false)
      .map((lane) => {
        const rows = new Set();
        const points = (lane.polyline_points || lane.points || [])
          .map(([x, y]) => [
            Math.max(0, Math.min(width - 1, x)),
            Math.max(0, Math.min(height - 1, y)),
          ])
          .filter((point) => {
            if (!point.every(Number.isFinite))
              throw new Error(
                "Invalid model point; review this photo individually.",
              );
            const row = point[1].toFixed(4);
            if (rows.has(row)) return false;
            rows.add(row);
            return true;
          });
        if (
          points.length < 2 ||
          Math.max(...points.map((p) => p[1])) -
            Math.min(...points.map((p) => p[1])) <
            2
        )
          throw new Error(
            "An unfinished boundary needs individual correction.",
          );
        return { slot: lane.slot, kind: lane.kind || "solid", points };
      });
  }
  async function loadBatch() {
    if (batchBusy || pending.length) return;
    batchBusy = true;
    batchControls();
    $("batch-status").textContent = "Loading photos…";
    try {
      const rows = await api(
        `/api/batch?limit=8&split=${$("split-filter").value}&status=${["audit_sample", "flagged", "later", "label_check"].includes($("status-filter").value) ? $("status-filter").value : "pending"}&exclude=${encodeURIComponent(current?.image_id || "")}`,
      );
      batchItems = [];
      $("batch-grid").replaceChildren();
      for (const detail of rows) {
        const name = $("batch-model").value;
        const option =
          name === "current"
            ? null
            : detail.teacher_options.find(
                (candidate) => candidate.name === name,
              );
        const item = { detail, name, ready: false, option };
        const card = textNode("article", "", "batch-card"),
          thumbnail = document.createElement("canvas");
        item.checkbox = document.createElement("input");
        item.checkbox.type = "checkbox";
        item.checkbox.disabled = true;
        item.checkbox.onchange = batchControls;
        const label = textNode("label", "", "batch-selection");
        label.append(
          item.checkbox,
          document.createTextNode("I inspected these painted lines"),
        );
        const inspect = textNode("button", "Inspect larger", "button quiet");
        inspect.disabled = true;
        const note = textNode("p", detail.image_id, "hint");
        card.append(thumbnail, note);
        if (detail.label_check) {
          card.append(
            textNode("strong", "UFLD label check", "label-check-topics"),
            textNode("p", labelCheckTopics(detail.label_check), "hint"),
            textNode("p", detail.label_check.observation || "", "hint"),
          );
        }
        card.append(label, inspect);
        $("batch-grid").append(card);
        batchItems.push(item);
        const image = new Image();
        image.onload = () => {
          try {
            if (name !== "current" && !option)
              throw new Error("Selected model is not ready for this photo.");
            item.geometry = option
              ? {
                  lanes: clippedLanes(
                    option.geometry,
                    detail.width,
                    detail.height,
                  ),
                }
              : detail.geometry;
            paint(thumbnail, image, item.geometry);
            note.textContent = `${detail.image_id} · ${(item.geometry.lanes || []).filter((lane) => lane.slot).length} painted lines · ${option?.label || (detail.origin === "human" ? "saved human labels" : "source proposal")}`;
            item.ready = true;
            inspect.disabled = false;
            inspect.onclick = () => {
              paint($("batch-preview-canvas"), image, item.geometry);
              $("batch-preview").showModal();
            };
          } catch (error) {
            thumbnail.width = image.naturalWidth;
            thumbnail.height = image.naturalHeight;
            thumbnail.getContext("2d").drawImage(image, 0, 0);
            note.textContent = `${detail.image_id}: ${error.message}`;
          }
          batchControls();
        };
        image.onerror = () => {
          note.textContent =
            "Photo failed to load; it cannot be approved here.";
        };
        image.src = detail.image_url;
      }
      $("batch-status").textContent = rows.length
        ? "Select only photos whose displayed painted-line labels are correct. No photos are selected automatically."
        : $("status-filter").value === "label_check"
          ? "No other UFLD label checks are waiting in this split. Your current photo remains in the main reviewer; skipped checks are in Review later."
          : $("status-filter").value === "audit_sample"
          ? "No audit samples are waiting in this split. New samples appear after import batches finish conversion and quality checks."
          : "No unassigned photos in this split. Your current photo remains in the main reviewer.";
    } catch (error) {
      $("batch-status").textContent = error.message;
    } finally {
      batchBusy = false;
      batchControls();
    }
  }
  async function saveBatch() {
    if (batchBusy) return;
    if (!pending.length) {
      const selectedItems = batchItems.filter(
        (item) => item.ready && item.checkbox.checked,
      );
      if (!selectedItems.length) return;
      if (
        !(await confirmAction(
          `Approve the painted lines in these ${selectedItems.length} photos? Continue only after inspecting every selected photo. Road edges stay unchanged.`,
        ))
      )
        return;
      lastBatch = [];
      pending = selectedItems.map((item) => ({
        image_id: item.detail.image_id,
        action: item.option ? "corrected" : "approved",
        revision: item.detail.revision,
        event_id: uid(),
        reviewer: viewer.reviewer,
        notes: "Human inspected and approved in painted-line batch review.",
        lanes: item.option ? item.geometry.lanes : null,
        road_edges: item.option ? item.detail.geometry.road_edges : null,
        candidate: item.option ? item.name : null,
        candidate_version: item.option
          ? item.option.geometry.teacher_version
          : null,
      }));
      persist();
    }
    batchBusy = true;
    batchControls();
    try {
      while (pending.length) {
        const body = pending[0];
        $("batch-status").textContent =
          `Saving ${body.image_id} · ${pending.length} remaining…`;
        try {
          const result = await post("/api/reviews", body);
          lastBatch.push({
            event_id: result.event_id,
            image_id: body.image_id,
            reviewer: viewer.reviewer,
          });
          const completed = batchItems.find(
            (item) => item.detail.image_id === body.image_id,
          );
          if (completed) {
            completed.ready = false;
            completed.checkbox.checked = false;
          }
          pending.shift();
          persist();
        } catch (error) {
          if ([400, 404, 409, 422].includes(error.status)) {
            const refused = batchItems.find(
              (item) => item.detail.image_id === body.image_id,
            );
            if (refused) {
              refused.ready = false;
              refused.checkbox.checked = false;
            }
            pending.shift();
            persist();
            throw new Error(
              `${body.image_id}: ${error.message} Other pending saves are retained for retry.`,
            );
          }
          throw error;
        }
      }
      $("batch-status").textContent =
        `${lastBatch.length} photos saved. Use Load photos for the next batch, or Undo last batch.`;
      batchItems.forEach((item) => {
        item.checkbox.checked = false;
        item.ready = false;
      });
      await refreshStats();
    } catch (error) {
      $("batch-status").textContent =
        `${error.message} ${pending.length ? "Use Retry pending saves; saved requests will not be duplicated." : "Load photos again to inspect current labels."}`;
    } finally {
      batchBusy = false;
      batchControls();
    }
  }
  $("batch-review").onclick = async () => {
    if (editing || busy || pendingRequest || draftAvailable?.pending) return;
    batchDialog.showModal();
    if (!pending.length) await loadBatch();
    else batchControls();
  };
  $("batch-load").onclick = loadBatch;
  $("batch-model").onchange = loadBatch;
  $("batch-select").onclick = () => {
    for (const item of batchItems) item.checkbox.checked = item.ready;
    batchControls();
  };
  $("batch-approve").onclick = saveBatch;
  $("batch-close").onclick = () => {
    if (!batchBusy) batchDialog.close();
  };
  batchDialog.oncancel = (event) => {
    if (batchBusy) event.preventDefault();
  };
  $("batch-preview-close").onclick = () => $("batch-preview").close();
  $("batch-undo").onclick = async () => {
    if (batchBusy || pending.length || !lastBatch.length) return;
    if (
      !(await confirmAction(
        "Undo the saved decisions from the last batch? Any image changed since that batch will be kept as it is.",
      ))
    )
      return;
    batchBusy = true;
    batchControls();
    try {
      while (lastBatch.length) {
        const decision = lastBatch.at(-1);
        await post("/api/undo", decision);
        lastBatch.pop();
        persist();
      }
      $("batch-status").textContent =
        "Last batch undone. Load photos to review again.";
      await refreshStats();
    } catch (error) {
      $("batch-status").textContent = error.message;
    } finally {
      batchBusy = false;
      batchControls();
    }
  };
  window.reviewEase = {
    paintOnly: () => $("review-target").value === "lanes",
    resetPhoto() {
      picking = null;
      $("assist-cancel").hidden = true;
    },
    blocked: () => batchBusy || pending.length > 0,
    applyFocus,
    drawSeeds(context, scale) {
      applyFocus();
      if (!picking || targetKey() !== picking.target) return;
      for (const point of picking.points) {
        context.beginPath();
        context.arc(...point, 7 * scale, 0, 2 * Math.PI);
        context.fillStyle = "#e6b4ff";
        context.fill();
      }
    },
    async restore() {
      try {
        pending = JSON.parse(storageGet(key("pending")) || "[]");
        lastBatch = JSON.parse(storageGet(key("undo")) || "[]");
        if (
          !Array.isArray(pending) ||
          pending.length > 12 ||
          pending.some(
            (body) =>
              body.reviewer !== viewer.reviewer ||
              !["approved", "corrected"].includes(body.action),
          )
        )
          pending = [];
        if (!Array.isArray(lastBatch) || lastBatch.length > 12) lastBatch = [];
      } catch {
        pending = [];
        lastBatch = [];
      }
      applyFocus();
      if (pending.length) {
        batchDialog.showModal();
        $("batch-status").textContent =
          "A batch save was not confirmed. Retry pending saves to recover without duplicate decisions.";
      }
      batchControls();
    },
  };
  applyFocus();
})();
