/* BrakeSense website: the camera band (a real drive with the app's readouts) and the waitlist forms. No dependencies. */
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

  /* ---------- Camera band ----------
     The video is a real drive with the old build's overlays removed; the readouts are drawn here in the current
     camera-screen design (ContentView's RiskPill, speedReadout and carAheadNumbers). */
  const band = document.querySelector(".drive");
  const hudRoot = band?.querySelector("[data-hud]");
  const video = band?.querySelector("[data-drive-video]");
  if (!band || !hudRoot || !video) return;

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
  const STILL = 6.3; // the poster frame: High risk, 30 ft, 2.4 s

  const stateAt = (t) => {
    const r = ROWS[Math.max(0, Math.min(ROWS.length - 1, Math.floor(t / 0.2 + 1e-6)))];
    const code = r[5];
    return {
      level: METER.reduce((level, [at, value]) => (t >= at ? value : level), 0),
      speed: r[0], gap: r[1], their: r[2], closing: r[3], ttc: r[4],
      notes: { their: NOTE[code[0]] || "", closing: NOTE[code[1]] || "", ttc: NOTE[code[2]] || "" },
    };
  };

  /* ---------- Playback ----------
     One moving thing on the page. It stops when off screen or when the visitor pauses it, shows the poster frame
     when they prefer reduced motion, and offers Play when the browser refuses autoplay (iOS Low Power Mode). */
  const toggle = band.querySelector("[data-drive-toggle]");
  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)");
  let wanted = !reduce.matches;
  let onScreen = true;
  let raf = 0;

  const render = () => show(stateAt(video.currentTime || 0));
  const label = () => { if (toggle) toggle.textContent = wanted ? "Pause" : "Play"; };
  const tick = () => {
    render();
    raf = requestAnimationFrame(tick);
  };
  const sync = () => {
    const run = wanted && onScreen && !document.hidden;
    if (run && video.paused) {
      video.play().catch(() => { wanted = false; label(); });
    } else if (!run && !video.paused) {
      video.pause();
    }
    if (run && !raf) raf = requestAnimationFrame(tick);
    if (!run && raf) { cancelAnimationFrame(raf); raf = 0; render(); }
    label();
  };

  video.addEventListener("loadedmetadata", () => {
    if (!wanted && video.currentTime === 0) video.currentTime = STILL;
    render();
  });
  video.addEventListener("seeked", render);
  show(stateAt(wanted ? 0 : STILL));
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
})();
