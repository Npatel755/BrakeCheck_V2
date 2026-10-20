/* BrakeSense website: the hero alert illustration and the waitlist forms. No dependencies. */
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

  /* ---------- Hero illustration ----------
     A camera view drawn with a pinhole projection: a road point at distance z feet appears
     f * CAMERA_HEIGHT / z pixels below the horizon. The car ahead, its target box and the
     risk meter follow one scripted ten-second drive. Levels and labels match the app
     (BrakeTheme.name(for:) and RiskPill). */
  const huds = [...document.querySelectorAll("[data-hud]")];
  const template = document.getElementById("car-template");
  if (!huds.length || !template) return;

  const CAMERA_HEIGHT = 4;      // ft above the road
  const CAR_WIDTH = 6.2;        // ft, matching the 200-unit-wide car drawing
  const LOOP = 10.4;            // s
  // [time s, your speed mph, gap to the car ahead ft]
  const DRIVE = [[0, 38, 116], [2, 38, 110], [3, 37, 82], [3.9, 37, 58], [4.7, 36, 39], [5.4, 33, 34],
                 [6.2, 26, 35], [7, 21, 40], [7.8, 22, 46], [9.6, 33, 100], [LOOP, 38, 116]];
  const LEVELS = [[0, 1], [3, 2], [3.9, 3], [4.7, 4], [6.2, 3], [6.9, 2], [7.6, 1]];
  const BRAKE_LIGHTS = [2, 7.8];
  const VOICE = [4.7, 6.1];
  const LABELS = ["No reading", "Low risk", "Moderate risk", "High risk", "BRAKE NOW"];
  const STILL = 4.95;           // the frame shown when motion is reduced

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
  const levelAt = (t) => LEVELS.reduce((level, [at, value]) => (t >= at ? value : level), 1);
  const within = (t, [from, to]) => t >= from && t < to;

  class Hud {
    constructor(root) {
      this.root = root;
      this.canvas = root.querySelector("canvas");
      this.ctx = this.canvas.getContext("2d");
      this.car = root.querySelector(".hud__car");
      this.car.append(template.content.cloneNode(true));
      this.box = root.querySelector(".hud__box");
      this.label = root.querySelector("[data-label]");
      this.speed = root.querySelector("[data-speed]");
      this.gap = root.querySelector("[data-gap]");
      this.ttc = root.querySelector("[data-ttc]");
      this.phase = 0;
      this.shown = {};
      this.resize();
      root.classList.add("is-ready");
    }

    get visible() { return this.root.offsetWidth > 0; }

    resize() {
      const w = this.root.clientWidth;
      const h = this.root.clientHeight;
      if (!w || !h) return;
      const ratio = Math.min(window.devicePixelRatio || 1, 2);
      this.canvas.width = Math.round(w * ratio);
      this.canvas.height = Math.round(h * ratio);
      this.ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
      this.w = w;
      this.h = h;
      const portrait = h > w;
      this.horizon = h * (portrait ? 0.53 : 0.47);
      // Ground at nearZ feet sits on the bottom edge of the screen.
      const nearZ = portrait ? 14 : 19;
      this.f = ((h - this.horizon) * nearZ) / CAMERA_HEIGHT;
      this.pt = w / (portrait ? 330 : 640);
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

      // Low hills on the horizon, the same shape every frame.
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

      // Paint: a yellow left edge, white dashed lane lines (10 ft dash, 30 ft gap), a white right edge.
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

      // Two cars further up the neighbouring lanes, drifting a little so the road feels alive.
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

      // Haze where the road meets the sky.
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
      const before = sample(Math.max(0, t - 0.15)).gap;
      const closing = (before - gap) / 0.15; // ft/s
      this.phase += speed * 1.4667 * dt;
      this.drawScene(t);

      // The car ahead sits in the middle of your lane, `gap` feet away.
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

      const level = levelAt(t);
      this.set("level", level, () => {
        this.root.dataset.level = String(level);
        this.root.classList.toggle("is-brake", level === 4);
        this.label.textContent = LABELS[level];
      });
      this.set("lights", within(t, BRAKE_LIGHTS), (on) => this.root.classList.toggle("is-braking-ahead", on));
      this.set("speed", Math.round(speed), (v) => { this.speed.textContent = String(v); });
      this.set("gap", Math.round(gap), (v) => { this.gap.textContent = String(v); });
      const ttc = closing > 2 && gap / closing < 9.95 ? (gap / closing).toFixed(1) : "–";
      this.set("ttc", ttc, (v) => { this.ttc.textContent = v; });
    }

    set(key, value, apply) {
      if (this.shown[key] === value) return;
      this.shown[key] = value;
      apply(value);
    }
  }

  const screens = huds.map((root) => new Hud(root));
  const voice = document.querySelector("[data-voice]");
  const toggle = document.querySelector("[data-hud-toggle]");
  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)");
  let wanted = !reduce.matches; // the visitor's choice; off by default when motion is reduced
  let onScreen = true;
  let raf = 0;
  let clock = STILL;            // seconds into the loop
  let last = 0;

  const render = (dt) => {
    for (const hud of screens) if (hud.visible) hud.frame(clock, dt);
    voice?.classList.toggle("is-on", within(clock, VOICE));
  };
  const tick = (now) => {
    const dt = Math.min((now - last) / 1000, 0.05);
    last = now;
    clock = (clock + dt) % LOOP;
    render(dt);
    raf = requestAnimationFrame(tick);
  };
  const sync = () => {
    const run = wanted && onScreen && !document.hidden;
    if (run && !raf) {
      last = performance.now();
      raf = requestAnimationFrame(tick);
    } else if (!run && raf) {
      cancelAnimationFrame(raf);
      raf = 0;
    }
    if (toggle) toggle.textContent = wanted ? "Pause animation" : "Play animation";
  };

  if (toggle) {
    toggle.hidden = false;
    toggle.addEventListener("click", () => { wanted = !wanted; sync(); });
  }
  if (wanted) clock = 1.6; // start just before the car ahead brakes
  render(0);
  sync();

  document.addEventListener("visibilitychange", sync);
  reduce.addEventListener?.("change", () => { wanted = !reduce.matches; sync(); });
  if ("IntersectionObserver" in window) {
    new IntersectionObserver((entries) => {
      onScreen = entries.some((entry) => entry.isIntersecting);
      sync();
    }).observe(document.querySelector(".hero-figure"));
  }
  if ("ResizeObserver" in window) {
    const observer = new ResizeObserver(() => {
      for (const hud of screens) hud.resize();
      if (!raf) render(0);
    });
    for (const root of huds) observer.observe(root);
  }
})();
