(() => {
  "use strict";

  // The audit imports keep their source labels intact. This editor sends a
  // separate human correction and approves that sample once its warnings are resolved.
  window.createSampleFixEditor = ({el, getCurrent, request, message, setBusy, onSaved, onCancel}) => {
    const canvas = el("lanes");
    const context = canvas.getContext("2d");
    const colors = ["#e6b974", "#ff777b", "#78d7ff", "#9ce69a"];
    let editing = false;
    let loading = false;
    let saving = false;
    let generation = 0;
    let nodeSequence = 0;
    let activeItem = null;
    let photo = null;
    let lanes = [];
    let selectedSlot = 2;
    let selectedPoint = -1;
    let dragging = null;
    let drawingNewLine = false;
    let history = [];

    const laneFor = slot => lanes.find(lane => lane.slot === slot);
    const clone = value => JSON.parse(JSON.stringify(value));
    const clamp = (value, maximum) => Math.round(Math.max(0, Math.min(maximum - 1, value)) * 10) / 10;
    const currentSize = () => activeItem?.geometry || getCurrent()?.geometry || {width: canvas.width, height: canvas.height};

    function repairableWarnings() {
      const allowed = new Set(activeItem?.repairable_qc_reasons || []);
      return (activeItem?.qc_reasons || []).filter(reason => allowed.has(reason));
    }

    function completeWarnings() {
      return activeItem?.complete_annotation_qc_reasons || [];
    }

    function resolvedWarnings() {
      const complete = new Set(completeWarnings());
      return [...new Set([...(activeItem?.resolved_qc_reasons || []).filter(reason => !complete.has(reason)),
        ...(el("fix-resolve-warnings").checked ? repairableWarnings() : []),
        ...(el("fix-complete-annotation").checked ? completeWarnings() : [])])];
    }

    function remainingWarnings() {
      const resolved = new Set(resolvedWarnings());
      return (activeItem?.source_qc_reasons || activeItem?.qc_reasons || []).filter(reason => !resolved.has(reason));
    }

    function completeReady() {
      return !(completeWarnings().length || activeItem?.source === "zod_mirror" && lanes.length === 0) || (el("fix-complete-annotation").checked
        && (lanes.length > 0 || el("fix-no-visible-paint").checked));
    }

    function clearCompleteness() {
      el("fix-complete-annotation").checked = false;
      el("fix-no-visible-paint").checked = false;
    }

    function saveLabel() {
      el("save-fix").textContent = remainingWarnings().length || !completeReady() ? "Save correction" : "Save & approve";
      el("fix-resolve-warnings").disabled = saving;
      el("fix-complete-annotation").disabled = saving;
      el("fix-no-visible-paint").disabled = saving;
      const needsWholePhoto = completeWarnings().length || activeItem?.source === "zod_mirror" && lanes.length === 0;
      el("fix-complete-annotation-control").hidden = !needsWholePhoto;
      el("fix-no-visible-paint-control").hidden = !needsWholePhoto
        || !el("fix-complete-annotation").checked || lanes.length > 0;
      if (editing) document.body.style.setProperty("--sample-tools-height", `${el("fix-tools").getBoundingClientRect().height}px`);
    }

    function prepareLane(source) {
      const lane = clone(source);
      lane._nodes = lane.points.map(() => ++nodeSequence);
      if (Array.isArray(lane.segments)) {
        lane._segments = lane.segments.map(segment => (Array.isArray(segment) ? segment : segment.points)
          .map(point => lane._nodes[lane.points.findIndex(candidate => candidate[0] === point[0] && candidate[1] === point[1])])
          .filter(Number.isInteger));
      }
      return lane;
    }

    function segmentsFor(lane) {
      if (!lane._segments) return [lane.points];
      const points = new Map(lane._nodes.map((node, index) => [node, lane.points[index]]));
      return lane._segments.map(segment => segment.map(node => points.get(node)).filter(Boolean));
    }

    function payloadLane(lane) {
      const xy = point => point.slice(0, 2);
      const replacedSegmented = Array.isArray(activeItem?.geometry.lanes.find(original => original.slot === lane.slot)?.segments);
      return {slot: lane.slot, points: lane.points.map(xy),
        ...(lane._segments ? {segments: segmentsFor(lane).filter(segment => segment.length).map(segment => segment.map(xy))}
          : replacedSegmented ? {segments: [lane.points.map(xy)]} : {})};
    }

    function snapshot() {
      history.push(clone(lanes));
      if (history.length > 50) history.shift();
      el("fix-undo").disabled = !history.length;
    }

    function contentRect() {
      const rect = canvas.getBoundingClientRect();
      const size = currentSize();
      const scale = Math.min(rect.width / size.width, rect.height / size.height);
      const width = size.width * scale, height = size.height * scale;
      return {left: rect.left + (rect.width - width) / 2, top: rect.top + (rect.height - height) / 2, width, height};
    }

    function pointAt(event, captured = false) {
      const rect = contentRect();
      if (!rect.width || !rect.height) return null;
      if (!captured && (event.clientX < rect.left || event.clientX > rect.left + rect.width
          || event.clientY < rect.top || event.clientY > rect.top + rect.height)) return null;
      const size = currentSize();
      return [clamp((event.clientX - rect.left) * size.width / rect.width, size.width),
        clamp((event.clientY - rect.top) * size.height / rect.height, size.height)];
    }

    function nearestHandle(point) {
      const size = currentSize();
      const rect = contentRect();
      // Only the visible handles of the selected slot can start a drag.
      // A nearby different line must never steal a click intended for drawing.
      const lane = laneFor(selectedSlot);
      if (!lane || drawingNewLine) return null;
      let best = {distance: 6 * 6};
      lane.points.forEach((candidate, index) => {
        const dx = (candidate[0] - point[0]) * rect.width / size.width;
        const dy = (candidate[1] - point[1]) * rect.height / size.height;
        const distance = dx * dx + dy * dy;
        if (distance < best.distance) best = {slot: selectedSlot, index, distance};
      });
      return Number.isInteger(best.index) ? best : null;
    }

    function insertionIndex(points, point) {
      if (points.length < 2) return points.length;
      let best = {distance: Infinity, index: points.length};
      for (let index = 0; index < points.length - 1; index++) {
        const a = points[index], b = points[index + 1];
        const dx = b[0] - a[0], dy = b[1] - a[1];
        const denominator = dx * dx + dy * dy;
        const projection = denominator ? ((point[0] - a[0]) * dx + (point[1] - a[1]) * dy) / denominator : 0;
        const t = Math.max(0, Math.min(1, projection));
        const distance = (point[0] - a[0] - t * dx) ** 2 + (point[1] - a[1] - t * dy) ** 2;
        if (distance < best.distance) {
          best = {distance, index: index + 1};
          if (index === 0 && projection < 0) best.index = 0;
          if (index === points.length - 2 && projection > 1) best.index = points.length;
        }
      }
      return best.index;
    }

    function insertPoint(lane, point) {
      const node = ++nodeSequence;
      let index = insertionIndex(lane.points, point);
      if (lane._segments?.length) {
        // Extend only one observed section. Editing never joins across an
        // existing missing/occluded interval as a side effect.
        const sections = segmentsFor(lane);
        let selected = 0, distance = Infinity;
        sections.forEach((section, sectionIndex) => {
          for (let i = 0; i < section.length; i++) {
            const a = section[i], b = section[Math.min(i + 1, section.length - 1)];
            const dx = b[0] - a[0], dy = b[1] - a[1];
            const denominator = dx * dx + dy * dy;
            const t = denominator ? Math.max(0, Math.min(1, ((point[0] - a[0]) * dx + (point[1] - a[1]) * dy) / denominator)) : 0;
            const candidate = (point[0] - a[0] - t * dx) ** 2 + (point[1] - a[1] - t * dy) ** 2;
            if (candidate < distance) { distance = candidate; selected = sectionIndex; }
          }
        });
        const section = lane._segments[selected];
        const position = insertionIndex(sections[selected], point);
        const anchor = section[Math.min(position, section.length - 1)];
        index = anchor === undefined ? lane.points.length : lane._nodes.indexOf(anchor) + (position === section.length ? 1 : 0);
        section.splice(position, 0, node);
      } else if (lane._segments) {
        lane._segments.push([node]);
      }
      lane.points.splice(index, 0, point);
      lane._nodes.splice(index, 0, node);
      return index;
    }

    function paint() {
      if (!editing || !photo) return;
      const size = currentSize();
      canvas.width = size.width;
      canvas.height = size.height;
      context.drawImage(photo, 0, 0, size.width, size.height);
      for (const lane of lanes) {
        const active = lane.slot === selectedSlot;
        context.strokeStyle = colors[lane.slot - 1];
        context.lineWidth = Math.max(active ? 4 : 3, size.width / (active ? 260 : 360));
        context.lineCap = "round";
        context.lineJoin = "round";
        context.globalAlpha = active ? 1 : 0.7;
        for (const points of segmentsFor(lane)) {
          if (!Array.isArray(points) || points.length < 2) continue;
          context.beginPath();
          points.forEach((point, index) => index ? context.lineTo(point[0], point[1]) : context.moveTo(point[0], point[1]));
          context.stroke();
        }
        context.globalAlpha = 1;
        if (lane.points.length) {
          const bottom = lane.points.reduce((a, b) => a[1] > b[1] ? a : b);
          context.font = `bold ${Math.max(17, size.width / 55)}px system-ui`;
          context.lineWidth = 4;
          context.strokeStyle = "#08140c";
          context.strokeText(String(lane.slot), bottom[0] + 7, bottom[1] - 7);
          context.fillStyle = colors[lane.slot - 1];
          context.fillText(String(lane.slot), bottom[0] + 7, bottom[1] - 7);
        }
      }
      const selected = laneFor(selectedSlot);
      if (selected) selected.points.forEach((point, index) => {
        context.beginPath();
        context.arc(point[0], point[1], Math.max(7, size.width / 155), 0, Math.PI * 2);
        context.fillStyle = index === selectedPoint ? "#ffffff" : colors[selectedSlot - 1];
        context.fill();
        context.lineWidth = Math.max(2, size.width / 480);
        context.strokeStyle = "#102219";
        context.stroke();
      });
      const selectedCount = selected?.points.length || 0;
      el("fix-help").textContent = selectedCount === 0
        ? `Slot ${selectedSlot}: tap two visible points on the photo to make a line.`
        : `Slot ${selectedSlot}: ${selectedCount} point${selectedCount === 1 ? "" : "s"}. Tap to add; drag a point to move it.`;
      el("fix-delete-lane").disabled = saving || !selected;
      el("fix-remove-point").disabled = saving || !selected || selectedPoint < 0;
      el("fix-add-lane").disabled = saving || lanes.length >= 4;
      saveLabel();
    }

    function toggle() {
      el("fix-tools").hidden = !editing;
      el("cancel-fix").hidden = !editing;
      el("save-fix").hidden = !editing;
      for (const id of ["exclude", "fix", "approve", "skip"]) el(id).hidden = editing;
      el("sample").querySelector(".actions").classList.toggle("editing", editing);
      el("sample").classList.toggle("fix-active", editing);
      canvas.classList.toggle("fixing", editing);
      el("fix-slot").disabled = saving;
      el("fix-undo").disabled = saving || !history.length;
      el("save-fix").disabled = saving;
      el("cancel-fix").disabled = saving;
      saveLabel();
      setBusy(loading || editing || saving);
      document.body.style.setProperty("--sample-actions-height", `${el("sample").querySelector(".actions").getBoundingClientRect().height}px`);
      document.body.style.setProperty("--sample-tools-height", `${el("fix-tools").getBoundingClientRect().height}px`);
      if (editing) paint();
    }

    async function begin() {
      const item = getCurrent();
      if (editing || loading || saving || !item) return;
      const ticket = ++generation;
      loading = true;
      toggle();
      message("Opening line editor…");
      try {
        const image = new Image();
        image.src = item.image_url;
        await image.decode();
        if (ticket !== generation || getCurrent()?.image_id !== item.image_id
            || getCurrent()?.fingerprint !== item.fingerprint || getCurrent()?.revision !== item.revision) return;
        activeItem = item;
        el("fix-resolve-warnings").checked = false;
        el("fix-complete-annotation").checked = item.human_complete_annotation === true;
        el("fix-no-visible-paint").checked = item.no_visible_lane_paint === true;
        el("fix-complete-reasons").textContent = completeWarnings().map(reason => reason.replaceAll("_", " ")).join(", ");
        el("fix-warning-resolution").hidden = !repairableWarnings().length;
        el("fix-warning-reasons").textContent = repairableWarnings().map(reason => reason.replaceAll("_", " ")).join(", ");
        const hints = [];
        if (repairableWarnings().includes("native_polyline_not_single_valued")) {
          hints.push("A source trace doubled back or had two positions on the same image row. Check that the corrected lines follow visible paint without doubling back.");
        }
        if (repairableWarnings().includes("native_points_outside_image")) {
          hints.push("A source trace went outside the photo. Keep corrected points inside the photo and within visible paint.");
        }
        if (repairableWarnings().includes("potential_transverse_marking")) {
          hints.push("A source line may be a crosswise marking or road edge and was left out. Check that the remaining labels cover visible lane paint.");
        }
        el("fix-warning-hint").textContent = hints.join(" ")
          || "Check the corrected positions, lane slots and visible endpoints against the photo.";
        photo = image;
        lanes = (item.geometry.lanes || []).map(prepareLane);
        selectedSlot = lanes.find(lane => lane.slot === 2)?.slot || lanes[0]?.slot || 2;
        selectedPoint = -1;
        drawingNewLine = (laneFor(selectedSlot)?.points.length || 0) < 2;
        history = [];
        editing = true;
        loading = false;
        el("fix-slot").value = String(selectedSlot);
        toggle();
        paint();
        canvas.closest("figure").scrollIntoView({block: "start"});
        const repairable = new Set([...(item.repairable_qc_reasons || []), ...completeWarnings()]);
        const blocked = (item.qc_reasons || []).some(reason => !repairable.has(reason));
        message(blocked
          ? "Edit the visible painted lines. Saving keeps this sample pending because a source warning needs import repair."
          : completeWarnings().length
            ? "Check the whole photo and add every missing visible painted boundary in slots 1–4. Confirm the complete-photo check to save and approve this photo."
            : repairableWarnings().length
            ? "Edit the visible painted lines, then confirm the warning check. Save & approve saves the correction and approves this sample."
            : "Drag a point, or tap the converted photo to add one. Save & approve saves the corrected labels and approves this sample.");
      } catch (error) {
        message(error.message || "Could not load the image for editing.", true);
      } finally {
        if (ticket === generation) { loading = false; toggle(); }
      }
    }

    function cancel() {
      if (!editing || saving) return;
      editing = false;
      dragging = null;
      activeItem = null;
      toggle();
      Promise.resolve(onCancel()).catch(error => message(error.message || "Could not redraw the sample. Refresh to try again.", true));
      message("Correction cancelled; the converted lines are unchanged.");
    }

    async function save() {
      if (!editing || saving) return;
      const item = activeItem;
      if (!item || getCurrent()?.image_id !== item.image_id) return;
      if (completeWarnings().length && el("fix-complete-annotation").checked
          && lanes.length === 0 && !el("fix-no-visible-paint").checked) {
        message("Confirm that no lane paint is visible, or add the missing painted boundaries before saving.", true);
        return;
      }
      for (const lane of lanes) {
        if (lane.points.length < 2) {
          message(`Slot ${lane.slot} needs at least two points. Add another point or remove the line.`, true);
          return;
        }
      }
      saving = true;
      toggle();
      message(remainingWarnings().length ? "Saving corrected lines…" : "Saving and approving corrected lines…");
      let saved = false;
      try {
        const result = await request("/correction", {method: "POST", headers: {"Content-Type": "application/json"},
          body: JSON.stringify({image_id: item.image_id, revision: item.revision, fingerprint: item.fingerprint,
            lanes: lanes.map(payloadLane), resolved_qc_reasons: resolvedWarnings(), approve: true,
            complete_annotation: el("fix-complete-annotation").checked,
            no_visible_lane_paint: completeWarnings().length > 0 && el("fix-complete-annotation").checked
              && lanes.length === 0 && el("fix-no-visible-paint").checked})});
        saved = true;
        editing = false;
        dragging = null;
        toggle();
        await onSaved(result);
        activeItem = null;
      } catch (error) {
        message(saved ? "Correction saved, but the queue could not refresh. Refresh to continue."
          : error.message || "Could not save the correction.", true);
      } finally {
        saving = false;
        toggle();
      }
    }

    canvas.addEventListener("pointerdown", event => {
      if (!editing || saving) return;
      event.preventDefault();
      const point = pointAt(event);
      if (!point) return;
      const handle = nearestHandle(point);
      if (handle) {
        selectedSlot = handle.slot;
        selectedPoint = handle.index;
        el("fix-slot").value = String(selectedSlot);
        snapshot();
        dragging = {id: event.pointerId, slot: selectedSlot, index: selectedPoint};
        canvas.setPointerCapture(event.pointerId);
      } else {
        snapshot();
        let lane = laneFor(selectedSlot);
        if (!lane) { lane = {slot: selectedSlot, points: [], _nodes: []}; lanes.push(lane); }
        selectedPoint = insertPoint(lane, point);
        if (lane.points.length >= 2) drawingNewLine = false;
        clearCompleteness();
      }
      paint();
    });
    canvas.addEventListener("pointermove", event => {
      if (!editing || saving || dragging?.id !== event.pointerId) return;
      event.preventDefault();
      const lane = laneFor(dragging.slot);
      if (!lane) return;
      const point = pointAt(event, true);
      if (!point) return;
      if (lane.points[dragging.index][0] !== point[0] || lane.points[dragging.index][1] !== point[1]) clearCompleteness();
      lane.points[dragging.index].splice(0, 2, ...point);
      paint();
    });
    const stopDrag = event => {
      if (dragging?.id === event.pointerId) dragging = null;
    };
    canvas.addEventListener("pointerup", stopDrag);
    canvas.addEventListener("pointercancel", stopDrag);
    el("fix-slot").addEventListener("change", () => {
      if (!editing || saving) return;
      selectedSlot = Number(el("fix-slot").value);
      selectedPoint = -1;
      drawingNewLine = (laneFor(selectedSlot)?.points.length || 0) < 2;
      paint();
    });
    el("fix-add-lane").addEventListener("click", () => {
      if (!editing || saving) return;
      const next = [2, 3, 1, 4].find(slot => !laneFor(slot));
      if (!next) return;
      selectedSlot = next;
      selectedPoint = -1;
      drawingNewLine = true;
      el("fix-slot").value = String(next);
      paint();
      message(`Slot ${next} selected. Tap the photo twice to draw a visible painted line.`);
    });
    el("fix-delete-lane").addEventListener("click", () => {
      if (!editing || saving || !laneFor(selectedSlot)) return;
      snapshot();
      lanes = lanes.filter(lane => lane.slot !== selectedSlot);
      drawingNewLine = true;
      clearCompleteness();
      selectedPoint = -1;
      paint();
    });
    el("fix-remove-point").addEventListener("click", () => {
      const lane = laneFor(selectedSlot);
      if (!editing || saving || !lane || selectedPoint < 0) return;
      snapshot();
      const node = lane._nodes[selectedPoint];
      lane.points.splice(selectedPoint, 1);
      lane._nodes.splice(selectedPoint, 1);
      if (lane._segments) lane._segments = lane._segments.map(segment => segment.filter(value => value !== node)).filter(segment => segment.length);
      drawingNewLine = lane.points.length < 2;
      clearCompleteness();
      selectedPoint = -1;
      paint();
    });
    el("fix-undo").addEventListener("click", () => {
      if (!editing || saving || !history.length) return;
      lanes = history.pop();
      drawingNewLine = (laneFor(selectedSlot)?.points.length || 0) < 2;
      clearCompleteness();
      selectedPoint = -1;
      paint();
      el("fix-undo").disabled = !history.length;
    });
    el("fix").addEventListener("click", begin);
    el("cancel-fix").addEventListener("click", cancel);
    el("save-fix").addEventListener("click", save);
    el("fix-resolve-warnings").addEventListener("change", saveLabel);
    el("fix-complete-annotation").addEventListener("change", () => {
      if (!el("fix-complete-annotation").checked) el("fix-no-visible-paint").checked = false;
      saveLabel();
    });
    el("fix-no-visible-paint").addEventListener("change", saveLabel);
    window.addEventListener("resize", () => { if (editing) toggle(); });
    return {isEditing: () => editing || loading, reset: () => {
      if (saving) return;
      if (loading) { ++generation; loading = false; toggle(); }
      if (editing) cancel();
    }};
  };
})();
