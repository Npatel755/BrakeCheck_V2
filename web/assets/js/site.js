/* BrakeSense website: the camera band (a real drive, or the illustration) and the waitlist forms. No dependencies. */
(() => {
  "use strict";

  /* ---------- Waitlist ----------
     <meta name="waitlist-endpoint"> decides where signups go:
     ""          not connected yet: say so, never pretend it worked
     "netlify"   Netlify Forms (the form markup already carries data-netlify)
     a URL       any form service that accepts a POST (Formspree, Basin, a Supabase function) */
  const endpoint = (document.querySelector('meta[name="waitlist-endpoint"]')?.content || "").trim();

  for (const form of document.querySelectorAll("form.waitlist")) {
    const input = form.querySelector('input[type="email"]');
    const button = form.querySelector('button[type="submit"]');
    const message = form.querySelector(".waitlist__msg");
    const say = (text, tone) => { message.textContent = text; message.dataset.tone = tone; };

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const email = input.value.trim();
      if (!email || !input.checkValidity()) {
        input.setAttribute("aria-invalid", "true");
        say("Enter an email address, like name@example.com.", "error");
        input.focus();
        return;
      }
      input.removeAttribute("aria-invalid");
      if (form.elements.company?.value) return; // the hidden field only bots fill in
      if (!endpoint) {
        say("Signups aren’t connected yet. This copy of the site is a preview.", "error");
        return;
      }
      const label = button.textContent;
      button.disabled = true;
      button.textContent = "Joining…";
      try {
        const data = new FormData(form);
        const response = endpoint === "netlify"
          ? await fetch("/", { method: "POST", headers: { "Content-Type": "application/x-www-form-urlencoded" }, body: new URLSearchParams(data).toString() })
          : await fetch(endpoint, { method: "POST", headers: { Accept: "application/json" }, body: data });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        form.classList.add("is-done");
        say(`You’re on the list. We’ll email ${email} once, when BrakeSense is on the App Store.`, "ok");
      } catch {
        say("That didn’t go through. Check your connection and try again.", "error");
        button.disabled = false;
        button.textContent = label;
      }
    });
  }

  /* ---------- Camera band ---------- */
  const band = document.querySelector(".drive");
  const hudRoot = band?.querySelector("[data-hud]");
  if (!band || !hudRoot) return;
  const params = new URLSearchParams(location.search);
  if (params.get("hero") === "illustration" || params.get("hero") === "road") {
    document.documentElement.dataset.hero = params.get("hero");
  }
  const mode = document.documentElement.dataset.hero === "illustration" ? "illustration" : "road";

  /* The readouts, named and laid out as in ContentView's camera page (RiskPill, speedReadout, carAheadNumbers). */
  const LABELS = ["No reading", "Low risk", "Moderate risk", "High risk", "BRAKE NOW"];
  const view = {
    label: hudRoot.querySelector("[data-label]"),
    v: Object.fromEntries([...hudRoot.querySelectorAll("[data-v]")].map((el) => [el.dataset.v, el])),
    n: Object.fromEntries([...hudRoot.querySelectorAll("[data-n]")].map((el) => [el.dataset.n, el])),
    shown: {},
  };
  const put = (key, value, apply) => {
    if (view.shown[key] === value) return;
    view.shown[key] = value;
    apply(value);
  };
  const show = (s) => {
    put("level", s.level, (level) => {
      hudRoot.dataset.level = String(level);
      band.classList.toggle("is-brake", level === 4);
      view.label.textContent = LABELS[level];
    });
    put("speed", s.speed ?? "--", (v) => {
      view.v.speed.textContent = String(v);
      view.v.speed.classList.toggle("is-empty", v === "--");
    });
    for (const key of ["gap", "their", "closing", "ttc"]) {
      put(key, s[key] ?? "--", (v) => { view.v[key].textContent = String(v); });
      put(`${key}-note`, s[key] === null || s[key] === undefined ? s.notes?.[key] || "" : "", (v) => { view.n[key].textContent = v; });
    }
    // The app reddens the closing time below 2 s.
    put("ttc-alert", typeof s.ttc === "string" && Number(s.ttc) < 2, (on) => view.v.ttc.classList.toggle("is-alert", on));
  };

  /* Readings from the real drive (October 11, 2026, video starts 169.0 s into the recording), every 0.2 s, as the
     early build measured and showed them: [speed mph, gap ft, their speed mph, gap closing mph, closing time s,
     notes for their speed / gap closing / closing time when a value is missing]. */
  const NOTE = { N: "Needs GPS", T: "Needs target", D: "Direction uncertain", S: "Sampling", C: "Camera delayed" };
  const ROWS = [
    [25, null, null, null, null, "NTN"], [25, null, null, null, null, "NTN"], [25, null, null, null, null, "NTN"],
    [25, null, null, null, null, "NTN"], [21, null, null, null, null, "NTN"], [21, 94, null, null, null, "DSS"],
    [21, 91, null, null, null, "DSS"], [21, 91, null, null, null, "DSS"], [21, 91, null, null, null, "DSS"],
    [20, 93, null, null, null, "SSS"], [20, null, null, null, null, "CCC"], [20, null, null, null, null, "CCC"],
    [20, null, null, null, null, "CCC"], [20, 73, null, null, null, "DSS"], [17, 71, null, null, null, "DSS"],
    [17, 70, null, null, null, "DSS"], [17, 64, null, null, null, "SSS"], [17, 64, null, null, null, "SSS"],
    [17, 64, null, null, null, "SSS"], [15, 63, null, null, null, "SSS"], [15, null, null, null, null, "CCC"],
    [15, null, null, null, null, "CCC"], [15, null, null, null, null, "CCC"], [15, 44, null, null, null, "DSS"],
    [14, 42, null, null, null, "DSS"], [14, 41, null, null, null, "DSS"], [14, 36, null, null, null, "DSS"],
    [14, 35, 4, 9, "2.5", ""], [14, 35, 4, 9, "2.5", ""], [14, 33, 4, 10, "2.4", ""],
    [14, null, null, null, null, "CCC"], [14, 30, 5, 9, "2.4", ""], [14, 30, 5, 9, "2.4", ""],
    [14, null, null, null, null, "CCC"], [null, 29, null, 5, "3.9", "N"], [null, null, null, null, null, "CCC"],
    [null, 28, null, null, null, "DSS"], [null, 28, null, null, null, "DSS"], [null, 28, null, null, null, "DSS"],
    [null, 28, null, 0, null, "D"], [null, 28, null, 0, null, "D"], [null, 28, null, 0, null, "D"],
    [null, 28, null, 0, null, "D"], [null, 28, null, 0, null, "D"], [null, 28, null, 0, null, "D"],
    [null, 28, null, 0, null, "D"], [null, 28, null, 0, null, "D"], [null, 28, null, 0, null, "D"],
  ];
  /* The meter as the current build shows those readings (LeadDangerContinuity: risk rises at once, a lower band
     needs 0.35 s of new lower readings, and brief misses hold the last band): No reading, High risk, Low risk. */
  const METER = [[0, 0], [5.2, 3], [8.2, 1]];
  const ROAD_STILL = 6.3; // the poster frame: High risk, 30 ft, 2.4 s

  const roadState = (t) => {
    const r = ROWS[Math.max(0, Math.min(ROWS.length - 1, Math.floor(t / 0.2 + 1e-6)))];
    const code = r[5];
    return {
      level: METER.reduce((level, [at, value]) => (t >= at ? value : level), 0),
      speed: r[0], gap: r[1], their: r[2], closing: r[3], ttc: r[4],
      notes: { their: NOTE[code[0]] || "", closing: NOTE[code[1]] || "", ttc: NOTE[code[2]] || "" },
    };
  };

  /* ---------- Illustration ----------
     A camera view drawn with a pinhole projection: a road point at distance z feet appears f * CAMERA_HEIGHT / z
     pixels below the horizon. The car ahead, its target box and the meter follow one scripted ten-second drive. */
  const CAMERA_HEIGHT = 4;
  const CAR_WIDTH = 6.2;
  const LOOP = 10.4;
  const DRIVE = [[0, 38, 116], [2, 38, 110], [3, 37, 82], [3.9, 37, 58], [4.7, 36, 39], [5.4, 33, 34],
                 [6.2, 26, 35], [7, 21, 40], [7.8, 22, 46], [9.6, 33, 100], [LOOP, 38, 116]];
  const LEVELS = [[0, 1], [3, 2], [3.9, 3], [4.7, 4], [6.2, 3], [6.9, 2], [7.6, 1]];
  const CLOSING = [[0, null], [2.3, null], [2.4, 6.4], [3, 3.6], [3.9, 2.2], [4.7, 1.4], [5.3, 1.2],
                   [6.2, 2.8], [6.9, 5.2], [7.3, null]];
  const BRAKE_LIGHTS = [2, 7.8];
  const ILLUSTRATION_STILL = 4.95;

  const sample = (t) => {
    for (let i = 1; i < DRIVE.length; i += 1) {
      if (t <= DRIVE[i][0]) {
        const [t0, s0, g0] = DRIVE[i - 1];
        const [t1, s1, g1] = DRIVE[i];
        let u = (t - t0) / (t1 - t0);
        u = u * u * (3 - 2 * u);
        return { speed: s0 + (s1 - s0) * u, gap: g0 + (g1 - g0) * u };
      }
    }
    return { speed: DRIVE[0][1], gap: DRIVE[0][2] };
  };
  const closingAt = (t) => {
    const i = CLOSING.findIndex(([at]) => at > t);
    if (i < 1) return null;
    const [t0, v0] = CLOSING[i - 1];
    const [t1, v1] = CLOSING[i];
    if (v0 === null) return null;
    if (v1 === null) return v0;
    return v0 + ((v1 - v0) * (t - t0)) / (t1 - t0);
  };
  const illustrationState = (t) => {
    const { speed, gap } = sample(t);
    const ttc = closingAt(t);
    // closing speed from the gap and the closing time, so the four numbers agree (ft/s to mph)
    const closingMph = ttc === null ? null : (gap / ttc) * 0.6818;
    return {
      level: LEVELS.reduce((level, [at, value]) => (t >= at ? value : level), 1),
      speed: Math.round(speed), gap: Math.round(gap),
      their: closingMph === null ? null : Math.max(0, Math.round(speed - closingMph)),
      closing: closingMph === null ? null : Math.round(closingMph),
      ttc: ttc === null ? null : ttc.toFixed(1),
      notes: { their: "Sampling", closing: "Sampling", ttc: "Sampling" },
    };
  };

  class Illustration {
    constructor() {
      this.canvas = band.querySelector(".drive__canvas");
      this.ctx = this.canvas.getContext("2d");
      this.car = band.querySelector(".drive__car");
      this.box = band.querySelector(".drive__box");
      this.stage = band.querySelector(".drive__stage");
      const template = document.getElementById("car-template");
      if (template) this.car.append(template.content.cloneNode(true));
      this.phase = 0;
      this.resize();
      band.classList.add("is-ready");
    }

    resize() {
      const w = this.stage.clientWidth;
      const h = this.stage.clientHeight;
      if (!w || !h) return;
      const ratio = Math.min(window.devicePixelRatio || 1, 2);
      this.canvas.width = Math.round(w * ratio);
      this.canvas.height = Math.round(h * ratio);
      this.ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
      this.w = w;
      this.h = h;
      this.horizon = h * 0.47;
      this.f = ((h - this.horizon) * 19) / CAMERA_HEIGHT; // ground 19 ft ahead sits on the bottom edge
      this.pt = w / 640;
    }

    project(x, z) {
      return [this.w / 2 + (this.f * x) / z, this.horizon + (this.f * CAMERA_HEIGHT) / z];
    }

    quad(x, z1, z2, width) {
      const [ax, ay] = this.project(x - width / 2, z1);
      const [bx] = this.project(x + width / 2, z1);
      const [cx, cy] = this.project(x + width / 2, z2);
      const [dx] = this.project(x - width / 2, z2);
      const { ctx } = this;
      ctx.beginPath();
      ctx.moveTo(ax, ay);
      ctx.lineTo(bx, ay);
      ctx.lineTo(cx, cy);
      ctx.lineTo(dx, cy);
      ctx.closePath();
      ctx.fill();
    }

    drawScene(t) {
      const { ctx, w, h, horizon, pt } = this;
      const sky = ctx.createLinearGradient(0, 0, 0, horizon);
      sky.addColorStop(0, "#07132a");
      sky.addColorStop(0.7, "#162a4b");
      sky.addColorStop(1, "#283e62");
      ctx.fillStyle = sky;
      ctx.fillRect(0, 0, w, horizon + 1);
      ctx.fillStyle = "#0b1528";
      ctx.beginPath();
      ctx.moveTo(0, horizon + 1);
      for (let x = 0; x <= w + 12; x += 12) {
        const hill = 7 + 5 * Math.sin(x * 0.011 / pt) + 3 * Math.sin(x * 0.037 / pt + 1.3) + 1.4 * Math.sin(x * 0.09 / pt);
        ctx.lineTo(x, horizon - hill * pt);
      }
      ctx.lineTo(w, horizon + 1);
      ctx.closePath();
      ctx.fill();
      const ground = ctx.createLinearGradient(0, horizon, 0, h);
      ground.addColorStop(0, "#111b2e");
      ground.addColorStop(1, "#060b15");
      ctx.fillStyle = ground;
      ctx.fillRect(0, horizon, w, h - horizon);
      const road = ctx.createLinearGradient(0, horizon, 0, h);
      road.addColorStop(0, "#1c2943");
      road.addColorStop(0.35, "#121c31");
      road.addColorStop(1, "#0a111e");
      ctx.fillStyle = road;
      this.quad(0, 2.5, 3000, 42);
      ctx.fillStyle = "rgba(230, 184, 74, 0.55)";
      this.quad(-18, 2.5, 3000, 0.5);
      ctx.fillStyle = "rgba(220, 232, 248, 0.6)";
      this.quad(18, 2.5, 3000, 0.5);
      for (let z = 3 - (this.phase % 40); z < 900; z += 40) {
        const z1 = Math.max(z, 2.5);
        const z2 = z + 10;
        if (z2 <= 2.5) continue;
        ctx.fillStyle = `rgba(220, 232, 248, ${Math.max(0.12, 0.85 - z / 700).toFixed(3)})`;
        this.quad(-6, z1, z2, 0.5);
        this.quad(6, z1, z2, 0.5);
      }
      for (const [x, base, drift] of [[-12, 150, 0], [12, 245, 2.1]]) {
        const z = base + 24 * Math.sin(t * 0.45 + drift);
        const [cx, cy] = this.project(x, z);
        const carW = (this.f * 6) / z;
        ctx.fillStyle = "#0a1220";
        ctx.fillRect(cx - carW / 2, cy - carW * 0.62, carW, carW * 0.62);
        ctx.save();
        ctx.fillStyle = "#ff5d6c";
        ctx.shadowColor = "rgba(255, 77, 94, 0.9)";
        ctx.shadowBlur = 5 * pt;
        const light = Math.max(1, carW * 0.16);
        ctx.fillRect(cx - carW * 0.46, cy - carW * 0.4, light, light * 0.6);
        ctx.fillRect(cx + carW * 0.46 - light, cy - carW * 0.4, light, light * 0.6);
        ctx.restore();
      }
      const haze = ctx.createLinearGradient(0, horizon - 6 * pt, 0, horizon + 34 * pt);
      haze.addColorStop(0, "rgba(40, 62, 98, 0)");
      haze.addColorStop(0.35, "rgba(40, 62, 98, 0.55)");
      haze.addColorStop(1, "rgba(40, 62, 98, 0)");
      ctx.fillStyle = haze;
      ctx.fillRect(0, horizon - 6 * pt, w, 40 * pt);
    }

    frame(t, dt) {
      if (!this.w) this.resize();
      if (!this.w) return;
      const { speed, gap } = sample(t);
      this.phase += speed * 1.4667 * dt;
      this.drawScene(t);
      const [x, groundY] = this.project(0, gap);
      const width = (this.f * CAR_WIDTH) / gap;
      const height = width * 0.75;
      const left = x - width / 2;
      const top = groundY - height * (146 / 150);
      this.car.style.width = `${width.toFixed(1)}px`;
      this.car.style.transform = `translate3d(${left.toFixed(1)}px, ${top.toFixed(1)}px, 0)`;
      const pad = 4 * this.pt;
      this.box.style.width = `${(width * 0.97 + pad * 2).toFixed(1)}px`;
      this.box.style.height = `${(height * 0.92 + pad * 2).toFixed(1)}px`;
      this.box.style.transform = `translate3d(${(left + width * 0.015 - pad).toFixed(1)}px, ${(top + height * 0.02 - pad).toFixed(1)}px, 0)`;
      band.classList.toggle("is-braking-ahead", t >= BRAKE_LIGHTS[0] && t < BRAKE_LIGHTS[1]);
    }
  }

  /* ---------- Playback ----------
     One moving thing on the page. It stops when off screen or when the visitor pauses it, and shows a still frame
     when they prefer reduced motion. */
  const toggle = band.querySelector("[data-drive-toggle]");
  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)");
  let wanted = !reduce.matches;
  let onScreen = true;
  let raf = 0;

  if (mode === "road") {
    const video = band.querySelector("[data-drive-video]");
    const render = () => show(roadState(video.currentTime || 0));
    const tick = () => {
      render();
      raf = requestAnimationFrame(tick);
    };
    const sync = () => {
      const run = wanted && onScreen && !document.hidden;
      if (run && video.paused) {
        video.play().catch(() => { wanted = false; label(); }); // autoplay refused (Low Power Mode): offer Play
      } else if (!run && !video.paused) {
        video.pause();
      }
      if (run && !raf) raf = requestAnimationFrame(tick);
      if (!run && raf) { cancelAnimationFrame(raf); raf = 0; render(); }
      label();
    };
    const label = () => { if (toggle) toggle.textContent = wanted ? "Pause" : "Play"; };
    const still = () => {
      if (!wanted && video.readyState >= 1 && video.currentTime === 0) video.currentTime = ROAD_STILL;
      render();
    };
    video.addEventListener("loadedmetadata", still);
    video.addEventListener("seeked", render);
    if (!wanted) show(roadState(ROAD_STILL)); else show(roadState(0));
    if (toggle) {
      toggle.hidden = false;
      toggle.addEventListener("click", () => { wanted = !wanted; sync(); });
    }
    document.addEventListener("visibilitychange", sync);
    reduce.addEventListener?.("change", () => { wanted = !reduce.matches; sync(); });
    if ("IntersectionObserver" in window) {
      new IntersectionObserver((entries) => { onScreen = entries.some((e) => e.isIntersecting); sync(); }).observe(band);
    }
    sync();
  } else {
    const scene = new Illustration();
    let clock = wanted ? 1.6 : ILLUSTRATION_STILL;
    let last = 0;
    const render = (dt) => { scene.frame(clock, dt); show(illustrationState(clock)); };
    const tick = (now) => {
      const dt = Math.min((now - last) / 1000, 0.05);
      last = now;
      clock = (clock + dt) % LOOP;
      render(dt);
      raf = requestAnimationFrame(tick);
    };
    const sync = () => {
      const run = wanted && onScreen && !document.hidden;
      if (run && !raf) { last = performance.now(); raf = requestAnimationFrame(tick); }
      else if (!run && raf) { cancelAnimationFrame(raf); raf = 0; }
      if (toggle) toggle.textContent = wanted ? "Pause" : "Play";
    };
    if (toggle) {
      toggle.hidden = false;
      toggle.addEventListener("click", () => { wanted = !wanted; sync(); });
    }
    render(0);
    sync();
    document.addEventListener("visibilitychange", sync);
    reduce.addEventListener?.("change", () => { wanted = !reduce.matches; sync(); });
    if ("IntersectionObserver" in window) {
      new IntersectionObserver((entries) => { onScreen = entries.some((e) => e.isIntersecting); sync(); }).observe(band);
    }
    if ("ResizeObserver" in window) {
      new ResizeObserver(() => { scene.resize(); if (!raf) render(0); }).observe(band.querySelector(".drive__stage"));
    }
  }
})();
