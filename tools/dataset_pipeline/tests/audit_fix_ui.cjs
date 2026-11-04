"use strict";

// Run with: node --test tools/dataset_pipeline/tests/audit_fix_ui.cjs
// Exercise the shipped page scripts without a server, browser, or dataset writes.
const assert = require("node:assert/strict");
const {test} = require("node:test");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const ui = path.resolve(__dirname, "../review_ui");
const copy = value => JSON.parse(JSON.stringify(value));
const flush = async () => {
  for (let turn = 0; turn < 8; turn++) await new Promise(resolve => setImmediate(resolve));
};
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return {promise, resolve, reject};
}

class Classes {
  constructor() { this.values = new Set(); }
  toggle(name, force) {
    const value = force === undefined ? !this.values.has(name) : force;
    if (value) this.values.add(name); else this.values.delete(name);
    return value;
  }
  contains(name) { return this.values.has(name); }
}

class Element {
  constructor(id, attributes = "") {
    this.id = id;
    this.hidden = /\bhidden\b/.test(attributes);
    this.disabled = /\bdisabled\b/.test(attributes);
    this.textContent = "";
    this.value = "";
    this.checked = /\bchecked\b/.test(attributes);
    this.classList = new Classes();
    this.listeners = new Map();
    this.children = new Map();
    this.scrolls = [];
  }
  addEventListener(type, listener) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(listener);
  }
  async dispatch(type, properties = {}, force = false) {
    if (type === "click" && this.disabled && !force) return;
    const event = {type, target: this, preventDefault() {}, ...properties};
    const listeners = [...(this.listeners.get(type) || [])];
    if (typeof this[`on${type}`] === "function") listeners.push(this[`on${type}`]);
    await Promise.all(listeners.map(listener => listener(event)));
    await flush();
  }
  click() { return this.dispatch("click"); }
  querySelector(selector) {
    if (!this.children.has(selector)) this.children.set(selector, new Element(selector));
    return this.children.get(selector);
  }
  closest(selector) { return this.querySelector(`closest:${selector}`); }
  getBoundingClientRect() { return {left: 0, top: 0, width: 500, height: 60}; }
  scrollIntoView(options) { this.scrolls.push(options); }
}

class Canvas extends Element {
  constructor() {
    super("lanes");
    this.width = 300;
    this.height = 150;
    this.rect = {left: 10, top: 20, width: 500, height: 375};
    this.operations = [];
    this.captures = [];
    this.context = {};
    for (const operation of ["drawImage", "beginPath", "moveTo", "lineTo", "stroke", "arc", "fill", "strokeText", "fillText", "clearRect"])
      this.context[operation] = (...args) => this.operations.push({operation, args});
  }
  getContext(kind) { assert.equal(kind, "2d"); return this.context; }
  getBoundingClientRect() { return this.rect; }
  setPointerCapture(id) { this.captures.push(id); }
}

function sample() {
  return {
    image_id: "fixture/road 01.jpg", image_url: "/fixture/road.jpg", split: "train",
    revision: 7, fingerprint: "converted-v7", session_id: "fixture-session",
    group_key: "fixture-group", group_images: 12, qc_reasons: [], source_qc_reasons: [],
    resolved_qc_reasons: [], repairable_qc_reasons: [], complete_annotation_qc_reasons: [],
    human_complete_annotation: false, no_visible_lane_paint: false, annotation_policy_risks: [],
    geometry: {width: 1000, height: 750, lanes: [
      {slot: 2, points: [[200, 700], [400, 300]]},
      {slot: 3, points: [[800, 700], [600, 300]]},
    ]}, decision: null,
  };
}

function warnedSample() {
  const item = sample();
  item.source_qc_reasons = ["native_polyline_not_single_valued"];
  item.repairable_qc_reasons = item.source_qc_reasons.slice();
  item.qc_reasons = item.source_qc_reasons.slice();
  return item;
}

function incompleteSample() {
  const item = sample();
  item.source_qc_reasons = ["native_coverage_incomplete"];
  item.qc_reasons = item.source_qc_reasons.slice();
  item.complete_annotation_qc_reasons = item.source_qc_reasons.slice();
  return item;
}

function nextSample() {
  return {...sample(), image_id: "fixture/road 02.jpg", image_url: "/fixture/road-02.jpg", fingerprint: "next-sample-v7"};
}

function repairSample() {
  const item = sample();
  return {...item, image_id: "college-0000000000000064", source: "college_held_repairs",
    image_url: "/api/studio/college-repairs/samples/college-0000000000000064/photo",
    fingerprint: "b".repeat(64), candidate_origin: "model_proposal", candidate_only: true,
    training_eligible: false, repair_description: "Check every painted divider and its visible endpoints.",
    original_qc_reasons: ["native_coverage_incomplete"],
    original_geometry: {width: 1000, height: 750, lanes: [{slot: 2, points: [[111, 701], [411, 301]]}]}};
}

function repairOverview() {
  return {source: "college_held_repairs", audits_ready: true, candidate_only: true, training_eligible: false,
    counts: {imported: 3801, pending: 123, eligible_train_candidates: 0, audit_passed_photos: 300,
      groups: 41, approved: 8, groups_blocked: 1, groups_needing_repair: 2, not_staged: 32},
    policy: "Representative candidate audits only. Training stays paused."};
}

async function boot(page, initialItem = sample(), options = {}) {
  const html = fs.readFileSync(path.join(ui, page + ".html"), "utf8");
  const elements = new Map();
  for (const match of html.matchAll(/<([a-z][a-z0-9]*)\b([^>]*\bid="([^"]+)"[^>]*)>/g)) {
    const element = match[3] === "lanes" ? new Canvas() : new Element(match[3], match[2]);
    if (match[1] === "select") {
      const remaining = html.slice(match.index + match[0].length);
      element.value = /<option\s+value="([^"]+)"/.exec(remaining)[1];
    }
    elements.set(element.id, element);
  }
  const get = id => {
    assert.ok(elements.has(id), `The actual ${page}.html must contain #${id}`);
    return elements.get(id);
  };
  get("queue").value = options.queue || "pending";
  const state = {
    item: copy(initialItem), requests: [], failures: [], nextDecode: null, correctionGate: null,
    nextItems: copy(options.nextItems || []), savedCorrections: [],
    overview: options.overview || {audits_ready: true, counts: {imported: 12, pending: 1, eligible_train_candidates: 0, groups: 1, approved: 0, groups_blocked: 0}, policy: "Fixture policy"},
  };
  const prefix = page === "simulation" ? "/api/studio/simulation"
    : options.source === "college_held_repairs" ? "/api/studio/college-repairs" : "/api/studio/real-sources/pandaset_plus";
  const fetch = async (url, options = {}) => {
    const route = url.slice(prefix.length);
    assert.ok(url.startsWith(prefix), `Unexpected API origin or source: ${url}`);
    const entry = {url, route, method: options.method || "GET", body: options.body ? JSON.parse(options.body) : undefined};
    state.requests.push(entry);
    const failureIndex = state.failures.findIndex(failure => failure.route === route);
    if (failureIndex >= 0) {
      const failure = state.failures.splice(failureIndex, 1)[0];
      return {ok: false, status: failure.status || 409, json: async () => ({detail: failure.message})};
    }
    let result;
    if (!route) result = state.overview;
    else if (route.startsWith("/queue?")) result = {audits_ready: true, items: state.item ? [state.item, ...state.nextItems] : []};
    else if (route.startsWith("/samples/")) {
      assert.equal(decodeURIComponent(route.slice("/samples/".length)), state.item.image_id);
      result = state.item;
    } else if (route === "/correction") {
      if (state.correctionGate) await state.correctionGate.promise;
      assert.equal(entry.body.image_id, state.item.image_id);
      assert.equal(entry.body.fingerprint, state.item.fingerprint);
      const resolved = copy(entry.body.resolved_qc_reasons || []);
      const remaining = state.item.source_qc_reasons.filter(reason => !resolved.includes(reason));
      const decision = entry.body.approve && !remaining.length ? {action: "approve", reviewer: "fixture-human"} : null;
      state.item = {...state.item, revision: state.item.revision + 1, fingerprint: "human-correction-v" + (state.item.revision + 1), decision,
        human_complete_annotation: entry.body.complete_annotation === true,
        no_visible_lane_paint: entry.body.no_visible_lane_paint === true,
        resolved_qc_reasons: resolved, qc_reasons: remaining, geometry: {...state.item.geometry, lanes: copy(entry.body.lanes)}};
      state.savedCorrections.push(copy(state.item));
      result = state.item;
      if (decision) state.item = state.nextItems.shift() || null;
    } else if (route === "/decision") {
      assert.equal(entry.body.image_id, state.item.image_id);
      assert.equal(entry.body.fingerprint, state.item.fingerprint, "A decision must reference the refreshed labels");
      state.item = state.nextItems.shift() || null;
      result = {ok: true};
    } else throw new Error(`Unexpected mocked endpoint: ${route}`);
    return {ok: true, status: 200, json: async () => copy(result)};
  };
  class Image {
    decode() {
      const pending = state.nextDecode;
      state.nextDecode = null;
      return pending ? pending.promise : Promise.resolve();
    }
  }
  const sandbox = {
    console, document: {getElementById: get, hidden: false,
      body: {style: {setProperty() {}}}}, Image, fetch,
    URLSearchParams, location: {search: options.source ? "?source=" + options.source : ""}, Uint8Array,
    crypto: {getRandomValues: bytes => { bytes.fill(42); return bytes; }},
    setInterval: () => 1, addEventListener() {},
  };
  sandbox.window = sandbox;
  const context = vm.createContext(sandbox);
  for (const name of ["audit_fix.js", page + ".js"])
    vm.runInContext(fs.readFileSync(path.join(ui, name), "utf8"), context, {filename: name});
  await flush();
  assert.equal(get("sample").hidden, false, "Initial queue sample is visible");
  assert.equal(get("fix").disabled, false, "Fix initializes on an actual sample");
  assert.equal(get("lanes").width, 1000);
  assert.equal(get("lanes").height, 750);
  const posts = route => state.requests.filter(entry => entry.method === "POST" && (!route || entry.route === route));
  const filters = page === "simulation" ? ["split", "queue", "refresh"] : ["source", "split", "queue", "refresh"];
  const checkFilters = disabled => {
    for (const id of filters) assert.equal(get(id).disabled, disabled, `${id} disabled=${disabled}`);
  };
  const point = async (type, x, y, pointerId = 11) => {
    const canvas = get("lanes");
    const scale = Math.min(canvas.rect.width / 1000, canvas.rect.height / 750);
    await canvas.dispatch(type, {
      clientX: canvas.rect.left + (canvas.rect.width - 1000 * scale) / 2 + x * scale,
      clientY: canvas.rect.top + (canvas.rect.height - 750 * scale) / 2 + y * scale, pointerId});
  };
  return {get, state, posts, checkFilters, point};
}

for (const page of ["simulation", "real_source_audits"]) {
  test(`${page}: replacing a segmented lane saves the new line as one explicit section`, async () => {
    const item = sample();
    const first = [[200, 700], [250, 600]], second = [[350, 400], [400, 300]];
    item.geometry.lanes[0] = {slot: 2, points: [...first, ...second], segments: [first, second]};
    const h = await boot(page, item);
    await h.get("fix").dispatch("click");
    await h.get("fix-delete-lane").dispatch("click");
    await h.get("fix-add-lane").dispatch("click");
    assert.equal(h.get("fix-slot").value, "2");
    await h.point("pointerdown", 210, 710);
    await h.point("pointerdown", 410, 290);
    await h.get("save-fix").dispatch("click");
    const lane = h.posts("/correction")[0].body.lanes.find(value => value.slot === 2);
    assert.deepEqual(lane.points, [[210, 710], [410, 290]]);
    assert.deepEqual(lane.segments, [lane.points], "An intentional redraw has its own explicit continuous section");
    assert.deepEqual(h.posts("/correction")[0].body.lanes.find(value => value.slot === 3), item.geometry.lanes[1]);
  });

  test(`${page}: drawing a new line next to another slot keeps exact clicked points`, async () => {
    const h = await boot(page);
    await h.get("fix").dispatch("click");
    await h.get("fix-add-lane").dispatch("click");
    await h.point("pointerdown", 201, 700);
    await h.point("pointerdown", 203, 692);
    assert.equal(h.get("fix-slot").value, "1");
    assert.deepEqual(h.get("lanes").captures, [], "Drawing never grabs either nearby existing handle");
    await h.get("save-fix").dispatch("click");
    const lanes = h.posts("/correction")[0].body.lanes;
    assert.deepEqual(lanes.find(lane => lane.slot === 1).points, [[201, 700], [203, 692]]);
    assert.deepEqual(lanes.filter(lane => lane.slot !== 1), sample().geometry.lanes);
  });

  test(`${page}: redrawing a removed line keeps two nearby endpoints without Add line`, async () => {
    const h = await boot(page);
    await h.get("fix").dispatch("click");
    await h.get("fix-delete-lane").dispatch("click");
    await h.point("pointerdown", 201, 700);
    await h.point("pointerdown", 203, 692);
    assert.deepEqual(h.get("lanes").captures, []);
    await h.get("save-fix").dispatch("click");
    assert.deepEqual(h.posts("/correction")[0].body.lanes.find(lane => lane.slot === 2).points,
      [[201, 700], [203, 692]]);
  });

  test(`${page}: adding near an unselected lane cannot change the selected slot`, async () => {
    const h = await boot(page);
    await h.get("fix").dispatch("click");
    await h.point("pointerdown", 799, 699);
    assert.equal(h.get("fix-slot").value, "2");
    assert.deepEqual(h.get("lanes").captures, []);
    await h.get("save-fix").dispatch("click");
    const lanes = h.posts("/correction")[0].body.lanes;
    assert.ok(lanes.find(lane => lane.slot === 2).points.some(point => point[0] === 799 && point[1] === 699));
    assert.deepEqual(lanes.find(lane => lane.slot === 3), sample().geometry.lanes[1]);
  });

  test(`${page}: clicks outside a selected handle add a point while direct handle drags still move it`, async () => {
    const h = await boot(page);
    await h.get("fix").dispatch("click");
    await h.point("pointerdown", 224, 700); // 12 CSS px away, inside the old 18 px snap zone.
    assert.deepEqual(h.get("lanes").captures, []);
    await h.point("pointerdown", 200, 700);
    await h.point("pointermove", 190, 680);
    await h.point("pointerup", 190, 680);
    assert.deepEqual(h.get("lanes").captures, [11]);
    await h.get("save-fix").dispatch("click");
    const points = h.posts("/correction")[0].body.lanes.find(lane => lane.slot === 2).points;
    assert.ok(points.some(point => point[0] === 224 && point[1] === 700));
    assert.ok(points.some(point => point[0] === 190 && point[1] === 680));
  });

  test(`${page}: Fix loads safely, freezes filters, and cancel keeps the original sample`, async () => {
    const h = await boot(page);
    const initialGets = h.state.requests.length;
    const imageGate = deferred();
    h.state.nextDecode = imageGate;
    const opening = h.get("fix").dispatch("click");
    await flush();
    h.checkFilters(true);
    // Even an artificial event on a disabled decision must not submit while loading/editing.
    await h.get("approve").dispatch("click", {}, true);
    assert.equal(h.posts().length, 0);
    imageGate.resolve();
    await opening;
    assert.equal(h.get("fix-tools").hidden, false);
    assert.equal(h.get("save-fix").hidden, false);
    h.checkFilters(true);
    await h.point("pointerdown", 500, 500);
    await h.get("cancel-fix").dispatch("click");
    assert.equal(h.posts().length, 0, "Editing and cancellation do not write decisions or labels");
    assert.equal(h.state.requests.length, initialGets, "Cancel redraws locally without changing the queue");
    assert.equal(h.get("image-id").textContent, "fixture/road 01.jpg");
    assert.equal(h.get("fix-tools").hidden, true);
    h.checkFilters(false);
    // A fresh draft starts from the converted geometry rather than cancelled edits.
    await h.get("fix").dispatch("click");
    await h.get("save-fix").dispatch("click");
    assert.deepEqual(h.posts("/correction")[0].body.lanes, sample().geometry.lanes);
  });

  test(`${page}: non-widescreen coordinates and add/move/remove/undo produce the intended correction`, async () => {
    const h = await boot(page);
    await h.get("fix").dispatch("click");
    await h.get("fix-add-lane").dispatch("click");
    assert.equal(h.get("fix-slot").value, "1");
    await h.point("pointerdown", 100, 600);
    await h.point("pointerdown", 250, 150);
    await h.point("pointerdown", 100, 600);
    await h.point("pointermove", 150, 550);
    await h.point("pointerup", 150, 550);
    assert.deepEqual(h.get("lanes").captures, [11]);
    await h.get("fix-remove-point").dispatch("click");
    await h.get("save-fix").dispatch("click");
    assert.equal(h.posts().length, 0, "An incomplete single-point line must not be saved");
    assert.match(h.get("message").textContent, /at least two points/i);
    await h.get("fix-undo").dispatch("click");
    await h.get("fix-delete-lane").dispatch("click");
    await h.get("fix-undo").dispatch("click");
    await h.get("save-fix").dispatch("click");
    const payload = h.posts("/correction")[0].body;
    assert.deepEqual(payload.lanes.find(lane => lane.slot === 1).points, [[150, 550], [250, 150]]);
    assert.deepEqual(payload.lanes.filter(lane => lane.slot !== 1), sample().geometry.lanes);
    assert.equal(payload.revision, 7);
    assert.equal(payload.fingerprint, "converted-v7");
    assert.equal(h.posts("/decision").length, 0);
  });

  for (const selectedQueue of ["skip", "approve", "all"]) test(`${page}: Save & approve advances within the ${selectedQueue} queue`, async () => {
    const h = await boot(page, sample(), {queue: selectedQueue, nextItems: [nextSample()]});
    const queueLoads = h.state.requests.filter(entry => entry.route.startsWith("/queue?")).length;
    await h.get("fix").dispatch("click");
    assert.equal(h.get("fix-warning-resolution").hidden, true);
    assert.match(h.get("save-fix").textContent, /save.*approve/i);
    await h.point("pointerdown", 200, 700);
    await h.point("pointermove", 220, 650);
    await h.point("pointerup", 220, 650);
    const saveGate = deferred();
    h.state.correctionGate = saveGate;
    const saving = h.get("save-fix").dispatch("click");
    await flush();
    assert.equal(h.get("save-fix").disabled, true);
    assert.equal(h.get("cancel-fix").disabled, true);
    h.checkFilters(true);
    await h.get("approve").dispatch("click", {}, true);
    assert.equal(h.posts("/decision").length, 0, "A second decision cannot race an in-flight correction");
    saveGate.resolve();
    await saving;
    assert.equal(h.posts("/correction")[0].body.approve, true, "The correction requests approval atomically");
    assert.equal(h.get("image-id").textContent, "fixture/road 02.jpg");
    assert.equal(h.get("queue").value, selectedQueue, "Save & approve preserves the selected queue");
    assert.equal(h.get("fix-tools").hidden, true);
    assert.equal(h.state.requests.filter(entry => entry.route.startsWith("/queue?")).length, queueLoads + 1);
    const query = new URLSearchParams(h.state.requests.filter(entry => entry.route.startsWith("/queue?")).at(-1).route.split("?")[1]);
    assert.equal(query.get("status"), selectedQueue);
    assert.equal(query.get("after"), ["approve", "all"].includes(selectedQueue) ? "fixture/road 01.jpg" : null);
    assert.equal(h.state.requests.filter(entry => entry.route.startsWith("/samples/")).length, 0);
    assert.equal(h.posts("/decision").length, 0, "Save & approve uses the single correction transaction");
    assert.equal(h.state.savedCorrections[0].decision.action, "approve");
    assert.equal(h.state.savedCorrections[0].fingerprint, "human-correction-v8");
    h.checkFilters(false);
  });

  for (const queue of ["skip", "all"]) for (const action of ["approve", "exclude", "skip"]) {
    test(`${page}: ${action} advances without changing the ${queue} queue`, async () => {
      const h = await boot(page, sample(), {queue, nextItems: [nextSample()]});
      await h.get(action).dispatch("click");
      assert.equal(h.get("queue").value, queue);
      assert.equal(h.get("image-id").textContent, "fixture/road 02.jpg");
      const decision = h.posts("/decision")[0].body;
      assert.equal(decision.action, action);
      assert.equal(decision.fingerprint, "converted-v7");
      assert.match(decision.event_id, /^[a-f0-9]{32}$/);
      const query = new URLSearchParams(h.state.requests.filter(entry => entry.route.startsWith("/queue?")).at(-1).route.split("?")[1]);
      assert.equal(query.get("status"), queue);
      assert.equal(query.get("after"), queue === "all" || queue === action ? "fixture/road 01.jpg" : null,
        "A sample that still matches the queue needs a cursor to reach the next sample");
    });
  }

  test(`${page}: reaching the end of Skipped keeps the queue and offers a fresh pass`, async () => {
    const h = await boot(page, sample(), {queue: "skip"});
    await h.get("skip").dispatch("click");
    assert.equal(h.get("queue").value, "skip");
    assert.equal(h.get("empty").hidden, false);
    assert.match(h.get("empty-title").textContent, /end.*queue/i);
    assert.match(h.get("empty-message").textContent, /refresh/i);
    h.state.item = nextSample();
    await h.get("refresh").dispatch("click");
    assert.equal(h.get("image-id").textContent, "fixture/road 02.jpg");
    const query = new URLSearchParams(h.state.requests.filter(entry => entry.route.startsWith("/queue?")).at(-1).route.split("?")[1]);
    assert.equal(query.get("status"), "skip");
    assert.equal(query.has("after"), false, "Refresh starts again without the previous cursor");
  });

  test(`${page}: warnings stay pending until their resolution is explicitly checked`, async () => {
    const h = await boot(page, warnedSample(), {queue: "skip", nextItems: [nextSample()]});
    const queueLoads = h.state.requests.filter(entry => entry.route.startsWith("/queue?")).length;
    assert.equal(h.get("approve").disabled, true);
    await h.get("fix").dispatch("click");
    assert.equal(h.get("fix-warning-resolution").hidden, false);
    assert.equal(h.get("fix-resolve-warnings").checked, false);
    assert.match(h.get("fix-warning-reasons").textContent, /native polyline not single valued/i);
    assert.equal(h.get("save-fix").textContent, "Save correction");
    await h.get("save-fix").dispatch("click");
    assert.equal(h.posts("/correction")[0].body.approve, true);
    assert.deepEqual(h.posts("/correction")[0].body.resolved_qc_reasons, []);
    assert.equal(h.state.savedCorrections[0].decision, null);
    assert.equal(h.get("image-id").textContent, "fixture/road 01.jpg");
    assert.equal(h.get("approve").disabled, true);
    assert.match(h.get("message").textContent, /native polyline not single valued/i);
    assert.match(h.get("message").textContent, /fix lines|confirm|check/i);
    assert.equal(h.state.requests.filter(entry => entry.route.startsWith("/queue?")).length, queueLoads);
    assert.equal(h.get("queue").value, "skip");
    await h.get("fix").dispatch("click");
    assert.equal(h.get("fix-resolve-warnings").checked, false);
    h.get("fix-resolve-warnings").checked = true;
    await h.get("fix-resolve-warnings").dispatch("change");
    assert.match(h.get("save-fix").textContent, /save.*approve/i);
    await h.get("save-fix").dispatch("click");
    assert.deepEqual(h.posts("/correction")[1].body.resolved_qc_reasons, ["native_polyline_not_single_valued"]);
    assert.equal(h.posts("/correction")[1].body.fingerprint, "human-correction-v8");
    assert.equal(h.state.savedCorrections[1].decision.action, "approve");
    assert.equal(h.get("image-id").textContent, "fixture/road 02.jpg");
    assert.equal(h.get("queue").value, "skip");
    assert.equal(h.posts("/decision").length, 0);
  });

  test(`${page}: resolving repairable warnings keeps other warnings blocked`, async () => {
    const item = warnedSample();
    item.source_qc_reasons.push("source_review_required");
    item.qc_reasons = item.source_qc_reasons.slice();
    const h = await boot(page, item);
    await h.get("fix").dispatch("click");
    h.get("fix-resolve-warnings").checked = true;
    await h.get("fix-resolve-warnings").dispatch("change");
    assert.equal(h.get("save-fix").textContent, "Save correction");
    await h.get("save-fix").dispatch("click");
    assert.deepEqual(h.posts("/correction")[0].body.resolved_qc_reasons, ["native_polyline_not_single_valued"]);
    assert.deepEqual(h.state.savedCorrections[0].qc_reasons, ["source_review_required"]);
    assert.equal(h.get("approve").disabled, true);
    assert.equal(h.get("image-id").textContent, item.image_id);
    assert.match(h.get("message").textContent, /source review required/i);
    assert.match(h.get("message").textContent, /exclude|skip/i,
      "A remaining warning that cannot be resolved by editing needs a different review action");
    await h.get("fix").dispatch("click");
    assert.equal(h.get("fix-warning-resolution").hidden, true,
      "Previously resolved repairable warnings cannot acknowledge a different remaining warning");
  });

  test(`${page}: a later correction preserves prior warning acknowledgements`, async () => {
    const item = warnedSample();
    item.source_qc_reasons.push("potential_transverse_marking");
    item.repairable_qc_reasons = item.source_qc_reasons.slice();
    item.resolved_qc_reasons = ["native_polyline_not_single_valued"];
    item.qc_reasons = ["potential_transverse_marking"];
    const h = await boot(page, item);
    await h.get("fix").dispatch("click");
    await h.get("save-fix").dispatch("click");
    assert.deepEqual(h.posts("/correction")[0].body.resolved_qc_reasons, ["native_polyline_not_single_valued"]);
    assert.equal(h.state.savedCorrections[0].decision, null);
    await h.get("fix").dispatch("click");
    h.get("fix-resolve-warnings").checked = true;
    await h.get("fix-resolve-warnings").dispatch("change");
    await h.get("save-fix").dispatch("click");
    assert.deepEqual(h.posts("/correction")[1].body.resolved_qc_reasons.slice().sort(),
      ["native_polyline_not_single_valued", "potential_transverse_marking"]);
    assert.equal(h.state.savedCorrections[1].decision.action, "approve");
  });

  test(`${page}: confirming a remaining single-valued warning approves saved geometry without a correction loop`, async () => {
    const item = warnedSample();
    const previous = ["ego_slot_long_extrapolation", "potential_transverse_marking"];
    item.source_qc_reasons = [...previous, "native_polyline_not_single_valued"];
    item.repairable_qc_reasons = item.source_qc_reasons.slice();
    item.resolved_qc_reasons = previous.slice();
    const h = await boot(page, item, {queue: "skip", nextItems: [nextSample()]});
    await h.get("fix").dispatch("click");
    assert.equal(h.get("fix-warning-resolution").hidden, false);
    assert.match(h.get("fix-warning-reasons").textContent, /native polyline not single valued/i);
    await h.get("save-fix").dispatch("click");
    assert.deepEqual(h.posts("/correction")[0].body.resolved_qc_reasons.slice().sort(), previous);
    assert.equal(h.state.savedCorrections[0].decision, null);
    assert.equal(h.get("image-id").textContent, item.image_id);
    assert.match(h.get("message").textContent, /native polyline not single valued/i);
    await h.get("fix").dispatch("click");
    h.get("fix-resolve-warnings").checked = true;
    await h.get("fix-resolve-warnings").dispatch("change");
    assert.match(h.get("save-fix").textContent, /save.*approve/i);
    await h.get("save-fix").dispatch("click");
    const [unconfirmed, confirmed] = h.posts("/correction").map(entry => entry.body);
    assert.deepEqual(confirmed.lanes, unconfirmed.lanes,
      "Explicitly confirming the already saved geometry does not require a meaningless point edit");
    assert.deepEqual(confirmed.resolved_qc_reasons.slice().sort(), item.source_qc_reasons.slice().sort());
    assert.equal(h.state.savedCorrections[1].decision.action, "approve");
    assert.deepEqual(h.state.savedCorrections[1].qc_reasons, []);
    assert.equal(h.get("image-id").textContent, "fixture/road 02.jpg");
    assert.equal(h.get("queue").value, "skip");
    assert.equal(h.posts("/decision").length, 0);
  });

  test(`${page}: incomplete coverage needs explicit full-photo annotation and applies to one sample`, async () => {
    const other = {...incompleteSample(), image_id: "fixture/road 02.jpg", image_url: "/fixture/road-02.jpg", fingerprint: "other-incomplete-v7"};
    const h = await boot(page, incompleteSample(), {queue: "skip", nextItems: [other]});
    await h.get("fix").dispatch("click");
    assert.equal(h.get("fix-warning-resolution").hidden, true,
      "Coverage is separate from acknowledging an ordinary repairable warning");
    assert.equal(h.get("fix-complete-annotation-control").hidden, false);
    assert.equal(h.get("fix-complete-annotation").checked, false);
    assert.match(h.get("fix-complete-reasons").textContent, /native coverage incomplete/i);
    assert.equal(h.get("save-fix").textContent, "Save correction");
    await h.get("save-fix").dispatch("click");
    const pending = h.posts("/correction")[0].body;
    assert.equal(pending.complete_annotation, false);
    assert.equal(pending.no_visible_lane_paint, false);
    assert.deepEqual(pending.resolved_qc_reasons, []);
    assert.equal(h.state.savedCorrections[0].decision, null);
    assert.equal(h.get("image-id").textContent, "fixture/road 01.jpg");
    assert.equal(h.get("approve").disabled, true);
    assert.match(h.get("message").textContent, /native coverage incomplete/i);
    assert.match(h.get("message").textContent, /all visible|complete manual label|every missing visible|whole.photo/i);
    await h.get("fix").dispatch("click");
    h.get("fix-complete-annotation").checked = true;
    await h.get("fix-complete-annotation").dispatch("change");
    assert.match(h.get("save-fix").textContent, /save.*approve/i);
    await h.get("save-fix").dispatch("click");
    const completed = h.posts("/correction")[1].body;
    assert.equal(completed.complete_annotation, true);
    assert.equal(completed.no_visible_lane_paint, false);
    assert.deepEqual(completed.resolved_qc_reasons, ["native_coverage_incomplete"]);
    assert.equal(h.state.savedCorrections[1].human_complete_annotation, true);
    assert.equal(h.state.savedCorrections[1].decision.action, "approve");
    assert.equal(h.get("image-id").textContent, other.image_id);
    assert.equal(h.get("queue").value, "skip");
    assert.equal(h.get("approve").disabled, true, "The next photo still requires its own complete annotation");
    await h.get("fix").dispatch("click");
    assert.equal(h.get("fix-complete-annotation").checked, false);
    assert.equal(h.posts("/correction").length, 2);
  });

  test(`${page}: an empty complete annotation requires an explicit no-visible-paint confirmation`, async () => {
    const h = await boot(page, incompleteSample());
    await h.get("fix").dispatch("click");
    await h.get("fix-delete-lane").dispatch("click");
    h.get("fix-slot").value = "3";
    await h.get("fix-slot").dispatch("change");
    await h.get("fix-delete-lane").dispatch("click");
    await h.get("save-fix").dispatch("click");
    assert.equal(h.posts("/correction").length, 1, "An unconfirmed empty draft can be saved pending");
    assert.equal(h.state.savedCorrections[0].decision, null);
    assert.equal(h.get("approve").disabled, true);
    await h.get("fix").dispatch("click");
    h.get("fix-complete-annotation").checked = true;
    await h.get("fix-complete-annotation").dispatch("change");
    assert.equal(h.get("fix-no-visible-paint-control").hidden, false);
    assert.equal(h.get("fix-no-visible-paint").checked, false);
    await h.get("save-fix").dispatch("click");
    assert.equal(h.posts("/correction").length, 1, "Completeness alone cannot claim that no lane paint is visible");
    assert.match(h.get("message").textContent, /no.*visible.*paint|confirm/i);
    h.get("fix-no-visible-paint").checked = true;
    await h.get("fix-no-visible-paint").dispatch("change");
    await h.get("save-fix").dispatch("click");
    const payload = h.posts("/correction")[1].body;
    assert.deepEqual(payload.lanes, []);
    assert.equal(payload.complete_annotation, true);
    assert.equal(payload.no_visible_lane_paint, true);
    assert.equal(h.state.savedCorrections[1].no_visible_lane_paint, true);
    assert.equal(h.state.savedCorrections[1].decision.action, "approve");
    assert.equal(h.get("empty").hidden, false);
  });

  test(`${page}: a full-photo check also requires separate ordinary warning acknowledgement`, async () => {
    const item = incompleteSample();
    item.source_qc_reasons.push("native_polyline_not_single_valued");
    item.qc_reasons = item.source_qc_reasons.slice();
    item.repairable_qc_reasons = ["native_polyline_not_single_valued"];
    const h = await boot(page, item);
    await h.get("fix").dispatch("click");
    h.get("fix-complete-annotation").checked = true;
    await h.get("fix-complete-annotation").dispatch("change");
    assert.equal(h.get("fix-resolve-warnings").checked, false);
    assert.equal(h.get("save-fix").textContent, "Save correction");
    h.get("fix-resolve-warnings").checked = true;
    await h.get("fix-resolve-warnings").dispatch("change");
    assert.match(h.get("save-fix").textContent, /save.*approve/i);
    await h.get("save-fix").dispatch("click");
    const payload = h.posts("/correction")[0].body;
    assert.equal(payload.complete_annotation, true);
    assert.deepEqual(payload.resolved_qc_reasons.slice().sort(), item.source_qc_reasons.slice().sort());
    assert.equal(h.state.savedCorrections[0].decision.action, "approve");
  });

  test(`${page}: editing or undoing a complete annotation requires a fresh photo check`, async () => {
    const item = incompleteSample();
    item.resolved_qc_reasons = item.source_qc_reasons.slice();
    item.qc_reasons = [];
    item.human_complete_annotation = true;
    const h = await boot(page, item);
    await h.get("fix").dispatch("click");
    assert.equal(h.get("fix-complete-annotation").checked, true);
    await h.point("pointerdown", 200, 700);
    await h.point("pointermove", 220, 650);
    await h.point("pointerup", 220, 650);
    assert.equal(h.get("fix-complete-annotation").checked, false);
    await h.get("fix-undo").dispatch("click");
    assert.equal(h.get("fix-complete-annotation").checked, false);
    assert.equal(h.get("save-fix").textContent, "Save correction");
    await h.get("save-fix").dispatch("click");
    const payload = h.posts("/correction")[0].body;
    assert.deepEqual(payload.lanes, item.geometry.lanes, "Undo restored the same geometry");
    assert.equal(payload.complete_annotation, false);
    assert.deepEqual(payload.resolved_qc_reasons, [], "Completeness acknowledgements cannot persist across an edit");
    assert.equal(h.state.savedCorrections[0].decision, null);
    assert.equal(h.get("approve").disabled, true);
  });

  test(`${page}: letterbox margins are ignored and dragging clamps to image bounds`, async () => {
    const h = await boot(page);
    // A 4:3 source is contained inside a widescreen canvas box on a small screen.
    h.get("lanes").rect.height = 281.25;
    await h.get("fix").dispatch("click");
    await h.get("fix-add-lane").dispatch("click");
    await h.get("lanes").dispatch("pointerdown", {clientX: 15, clientY: 120, pointerId: 11});
    await h.point("pointerdown", 100, 600);
    await h.point("pointerdown", 250, 150);
    await h.point("pointerdown", 100, 600);
    await h.point("pointermove", -100, 900);
    await h.point("pointerup", -100, 900);
    await h.get("save-fix").dispatch("click");
    assert.deepEqual(h.posts("/correction")[0].body.lanes.find(lane => lane.slot === 1).points,
      [[0, 749], [250, 150]], "Coordinates use the contained photo, ignoring bars and clamping captured drags");
  });

  test(`${page}: moving and adding points preserves separated visible sections`, async () => {
    const item = sample();
    const first = [[200, 700], [250, 600]], second = [[350, 400], [400, 300]];
    item.geometry.lanes[0] = {slot: 2, points: [...first, ...second], segments: [first, second]};
    const h = await boot(page, item);
    await h.get("fix").dispatch("click");
    await h.point("pointerdown", 250, 600);
    await h.point("pointermove", 260, 610);
    await h.point("pointerup", 260, 610);
    await h.point("pointerdown", 290, 540);
    await h.get("save-fix").dispatch("click");
    const lane = h.posts("/correction")[0].body.lanes.find(value => value.slot === 2);
    assert.deepEqual(lane.points, [[200, 700], [260, 610], [290, 540], [350, 400], [400, 300]]);
    assert.deepEqual(lane.segments, [[[200, 700], [260, 610], [290, 540]], second],
      "Editing one section cannot connect the gap to the next visible section");
  });

  test(`${page}: a rejected correction retains the draft and permits retry`, async () => {
    const h = await boot(page);
    await h.get("fix").dispatch("click");
    await h.point("pointerdown", 200, 700);
    await h.point("pointermove", 250, 650);
    await h.point("pointerup", 250, 650);
    h.state.failures.push({route: "/correction", status: 409, message: "Correction conflict; retry the fixture"});
    await h.get("save-fix").dispatch("click");
    assert.match(h.get("message").textContent, /Correction conflict/);
    assert.equal(h.get("message").classList.contains("error"), true);
    assert.equal(h.get("fix-tools").hidden, false);
    assert.equal(h.get("save-fix").disabled, false);
    h.checkFilters(true);
    await h.get("approve").dispatch("click", {}, true);
    assert.equal(h.posts("/decision").length, 0);
    await h.get("save-fix").dispatch("click");
    assert.equal(h.posts("/correction").length, 2);
    assert.deepEqual(h.posts("/correction")[1].body.lanes.find(lane => lane.slot === 2).points[0], [250, 650]);
    h.checkFilters(false);
  });

  test(`${page}: failed image loading releases controls and Fix can be retried`, async () => {
    const h = await boot(page);
    const imageGate = deferred();
    h.state.nextDecode = imageGate;
    const opening = h.get("fix").dispatch("click");
    await flush();
    h.checkFilters(true);
    imageGate.reject(new Error("Fixture image could not decode"));
    await opening;
    assert.match(h.get("message").textContent, /could not decode/);
    assert.equal(h.get("fix-tools").hidden, true);
    assert.equal(h.get("fix").disabled, false);
    h.checkFilters(false);
    await h.get("fix").dispatch("click");
    assert.equal(h.get("fix-tools").hidden, false);
    await h.get("cancel-fix").dispatch("click");
    assert.equal(h.posts().length, 0);
  });

  test(`${page}: a pending correction refresh failure recovers without approving unresolved warnings`, async () => {
    const h = await boot(page, warnedSample());
    await h.get("fix").dispatch("click");
    h.state.failures.push({route: "/samples/" + encodeURIComponent(h.state.item.image_id), status: 503, message: "Fixture refresh unavailable"});
    await h.get("save-fix").dispatch("click");
    assert.match(h.get("message").textContent, /could not refresh|refresh unavailable/);
    assert.equal(h.posts("/correction").length, 1);
    assert.equal(h.posts("/decision").length, 0);
    assert.equal(h.get("approve").disabled, true);
    assert.equal(h.get("refresh").disabled, false);
    await h.get("refresh").dispatch("click");
    assert.equal(h.get("sample").hidden, false);
    assert.equal(h.get("approve").disabled, true);
    await h.get("fix").dispatch("click");
    h.get("fix-resolve-warnings").checked = true;
    await h.get("fix-resolve-warnings").dispatch("change");
    await h.get("save-fix").dispatch("click");
    assert.equal(h.posts("/correction")[1].body.fingerprint, "human-correction-v8");
    assert.equal(h.state.savedCorrections[1].decision.action, "approve");
    assert.equal(h.get("empty").hidden, false);
  });
}

test("College repair audits: selected dataset uses the repair API and distinguishes audit passes from training", async () => {
  const h = await boot("real_source_audits", repairSample(), {
    source: "college_held_repairs", overview: repairOverview(), queue: "all"});
  assert.equal(h.get("source").value, "college_held_repairs");
  assert.ok(h.state.requests.every(request => request.url.startsWith("/api/studio/college-repairs")));
  assert.equal(h.get("imported").textContent, (3801).toLocaleString());
  assert.equal(h.get("remaining").textContent, "123");
  assert.equal(h.get("eligible").textContent, "300",
    "The repair count shows passed candidate audits even though training eligibility remains zero");
  assert.equal(h.get("eligible-label").textContent, "photos passing group audits");
  assert.equal(h.get("repair-batch-note").hidden, false);
  assert.match(h.get("repair-batch-note").textContent, /earlier 358 approvals are preserved/i);
  assert.match(h.get("repair-batch-note").textContent, /2 groups need another repair/i);
  assert.match(h.get("repair-batch-note").textContent, /32 original held photos/i);
  assert.match(h.get("repair-batch-note").textContent, /training uses separately integrated cleared snapshots/i);
  assert.equal(h.get("held-clearance-note").hidden, true);
  assert.equal(h.get("repair-info").hidden, false);
  assert.match(h.get("repair-info").textContent, /model proposal/i);
  assert.match(h.get("repair-info").textContent, /native coverage incomplete/i);
  assert.equal(h.get("compare-original").hidden, false);
  assert.equal(h.get("candidate-view-label").textContent, "Repair candidate");
  assert.equal(h.get("queue").value, "all");
  assert.equal(h.posts().length, 0);
});

test("College repair audits: original-line comparison blocks approval and editing without changing the queue", async () => {
  const item = repairSample();
  const h = await boot("real_source_audits", item, {
    source: "college_held_repairs", overview: repairOverview(), queue: "skip"});
  const requestsBeforeComparison = h.state.requests.length;
  const operationsBeforeComparison = h.get("lanes").operations.length;
  await h.get("compare-original").dispatch("click");
  assert.equal(h.get("candidate-view-label").textContent, "Original source lines");
  assert.equal(h.get("compare-original").textContent, "Show repair candidate");
  assert.equal(h.get("approve").disabled, true);
  assert.equal(h.get("fix").disabled, true);
  assert.equal(h.get("exclude").disabled, false);
  assert.equal(h.get("skip").disabled, false);
  assert.match(h.get("message").textContent, /original source lines.*switch back/i);
  const originalDraw = h.get("lanes").operations.slice(operationsBeforeComparison);
  assert.ok(originalDraw.some(operation => operation.operation === "moveTo" && operation.args[0] === 111),
    "The comparison actually draws the original geometry");
  assert.equal(originalDraw.filter(operation => operation.operation === "moveTo").length, 1);
  await h.get("approve").dispatch("click", {}, true);
  await h.get("fix").dispatch("click");
  assert.equal(h.posts().length, 0, "A forced approve event cannot submit while the original labels are shown");
  assert.equal(h.get("fix-tools").hidden, true);
  assert.equal(h.state.requests.length, requestsBeforeComparison,
    "Comparing labels does not reload the queue or change the selected source");
  assert.equal(h.get("source").value, "college_held_repairs");
  assert.equal(h.get("queue").value, "skip");
  h.checkFilters(false);
});

test("College repair audits: returning to the candidate approves its exact fingerprint and preserves filters", async () => {
  const item = repairSample();
  const next = {...repairSample(), image_id: "college-0000000000000065", fingerprint: "c".repeat(64)};
  const h = await boot("real_source_audits", item, {
    source: "college_held_repairs", overview: repairOverview(), queue: "all", nextItems: [next]});
  const initialQueueLoads = h.state.requests.filter(request => request.route.startsWith("/queue?")).length;
  const imageGate = deferred();
  h.state.nextDecode = imageGate;
  const comparing = h.get("compare-original").dispatch("click");
  await flush();
  h.checkFilters(true);
  assert.equal(h.get("approve").disabled, true);
  assert.equal(h.get("fix").disabled, true);
  await h.get("approve").dispatch("click", {}, true);
  assert.equal(h.posts().length, 0, "An approval cannot race the comparison image loading");
  imageGate.resolve();
  await comparing;
  const operationsBeforeReturn = h.get("lanes").operations.length;
  await h.get("compare-original").dispatch("click");
  assert.equal(h.get("candidate-view-label").textContent, "Repair candidate");
  assert.equal(h.get("compare-original").textContent, "Show original lines");
  assert.equal(h.get("approve").disabled, false);
  assert.equal(h.get("fix").disabled, false);
  h.checkFilters(false);
  const candidateDraw = h.get("lanes").operations.slice(operationsBeforeReturn);
  assert.deepEqual(candidateDraw.filter(operation => operation.operation === "moveTo").map(operation => operation.args),
    item.geometry.lanes.map(lane => lane.points[0]), "Returning restores both candidate boundaries");
  assert.equal(h.posts().length, 0);
  assert.equal(h.state.requests.filter(request => request.route.startsWith("/queue?")).length, initialQueueLoads);
  await h.get("approve").dispatch("click");
  assert.equal(h.posts("/decision").length, 1);
  const saved = h.posts("/decision")[0];
  assert.equal(saved.url, "/api/studio/college-repairs/decision");
  assert.equal(saved.body.image_id, item.image_id);
  assert.equal(saved.body.fingerprint, item.fingerprint);
  assert.equal(saved.body.revision, item.revision);
  assert.equal(saved.body.action, "approve");
  assert.equal(h.get("source").value, "college_held_repairs");
  assert.equal(h.get("queue").value, "all");
  assert.equal(h.get("image-id").textContent, next.image_id);
  const lastQueue = h.state.requests.filter(request => request.route.startsWith("/queue?")).at(-1);
  const query = new URLSearchParams(lastQueue.route.split("?")[1]);
  assert.equal(query.get("status"), "all");
  assert.equal(query.get("after"), item.image_id);
  assert.ok(h.state.requests.every(request => request.url.startsWith("/api/studio/college-repairs")));
});

test("College repair audits: Needs repair shows diagnostic counts and sample edits keep approval blocked", async () => {
  const item = {...repairSample(), audit_mode: "needs_repair",
    qc_reasons: ["repair_review_required"], source_qc_reasons: ["repair_review_required"]};
  const next = {...copy(item), image_id: "college-0000000000000065", fingerprint: "c".repeat(64)};
  const overview = repairOverview();
  Object.assign(overview.counts, {pending: 123, pending_problem_samples: 9,
    ready_candidate_photos: 3701, problem_photos: 100});
  const h = await boot("real_source_audits", item, {
    source: "college_held_repairs", overview, queue: "needs_repair", nextItems: [next]});
  assert.equal(h.get("remaining").textContent, "9", "Needs repair uses its own pending diagnostic sample count");
  assert.equal(h.get("repair-problems-option").hidden, false);
  assert.match(h.get("repair-batch-note").textContent, /100 need more repair/i);
  assert.match(h.get("repair-info").textContent, /group still needs repair.*approval is blocked/i);
  assert.equal(h.get("approve").disabled, true);
  assert.equal(h.get("fix").disabled, false);
  assert.equal(h.get("exclude").disabled, false);
  assert.equal(h.get("skip").disabled, false);
  await h.get("approve").dispatch("click");
  assert.equal(h.posts().length, 0);
  await h.get("fix").dispatch("click");
  assert.equal(h.get("fix-complete-annotation-control").hidden, true,
    "An original coverage warning does not become a whole-photo claim for a repair proposal");
  assert.equal(h.get("save-fix").textContent, "Save correction");
  await h.point("pointerdown", 200, 700);
  await h.point("pointermove", 220, 650);
  await h.point("pointerup", 220, 650);
  await h.get("save-fix").dispatch("click");
  assert.equal(h.posts("/correction").length, 1);
  assert.equal(h.posts("/decision").length, 0);
  assert.equal(h.state.savedCorrections[0].decision, null);
  assert.equal(h.state.savedCorrections[0].audit_mode, "needs_repair");
  assert.deepEqual(h.state.savedCorrections[0].qc_reasons, ["repair_review_required"]);
  assert.equal(h.get("approve").disabled, true);
  assert.equal(h.get("queue").value, "needs_repair");
  assert.equal(h.get("source").value, "college_held_repairs");
  await h.get("skip").dispatch("click");
  const skipped = h.posts("/decision")[0].body;
  assert.equal(skipped.action, "skip");
  assert.equal(skipped.fingerprint, "human-correction-v8");
  assert.equal(h.get("image-id").textContent, next.image_id);
  assert.equal(h.get("approve").disabled, true);
  assert.equal(h.get("queue").value, "needs_repair");
  assert.equal(h.get("source").value, "college_held_repairs");
  const lastQueue = h.state.requests.filter(request => request.route.startsWith("/queue?")).at(-1);
  const query = new URLSearchParams(lastQueue.route.split("?")[1]);
  assert.equal(query.get("status"), "needs_repair");
  assert.equal(query.has("after"), false, "The diagnostic pending queue removes skipped samples itself");
});

test("phone: the sticky Fix action forwards to the editor and follows its availability", async () => {
  const html = fs.readFileSync(path.join(ui, "index.html"), "utf8");
  const elements = new Map([...html.matchAll(/<([a-z][a-z0-9]*)\b([^>]*\bid="([^"]+)"[^>]*)>/g)]
    .map(match => [match[3], new Element(match[3], match[2])]));
  const get = id => {
    assert.ok(elements.has(id), `The actual index.html must contain #${id}`);
    return elements.get(id);
  };
  let opened = 0, drawn = 0;
  get("quick-fix").addEventListener("click", () => { opened++; });
  get("model-select").selectedOptions = [{textContent: "Fixture model"}];
  get("save-correction").hidden = true;
  let observer;
  const observed = [];
  const body = new Element("body");
  const context = vm.createContext({
    document: {getElementById: get, body, querySelectorAll: () => [new Element("left"), new Element("right")]},
    location: {search: "?phone=1"}, URLSearchParams,
    matchMedia: () => ({matches: true, addEventListener() {}}),
    MutationObserver: class {
      constructor(callback) { observer = callback; }
      observe(element) { observed.push(element.id); }
    },
    showReference() {}, setZoom() {}, draw() { drawn++; },
  });
  vm.runInContext(fs.readFileSync(path.join(ui, "phone.js"), "utf8"), context, {filename: "phone.js"});
  assert.equal(body.classList.contains("phone-review"), true);
  assert.equal(drawn, 1);
  assert.ok(observed.includes("correct"), "Phone Fix observes editor availability");
  await get("phone-fix").click();
  assert.equal(opened, 1, "Phone Fix opens the established quick-fix editor action");
  get("correct").disabled = true;
  observer();
  assert.equal(get("phone-fix").disabled, true);
  await get("phone-fix").click();
  assert.equal(opened, 1);
  get("correct").disabled = false;
  observer();
  await get("phone-fix").click();
  assert.equal(opened, 2);
  get("correct").hidden = true;
  observer();
  assert.equal(get("phone-fix").disabled, true, "Phone Fix is unavailable while the editor action is hidden");
  await get("phone-fix").click();
  assert.equal(opened, 2);
  get("correct").hidden = false;
  observer();
  assert.equal(get("phone-fix").disabled, false);
});
