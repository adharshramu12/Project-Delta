// Tri, Delta's pet: a small triangle (Delta's own Δ) in the corner that shows what Delta is doing. It thinks while
// Delta plans, works while it acts, jumps when a task is done, tips over when one fails, hides while you type,
// sleeps when nothing happens, greets you, and laughs when you click it. It only reacts to real events, so it is
// never noise. Drawn with SVG shapes and CSS classes (no images, no library, no inline styles). Off with Settings >
// Look and feel > "Show Tri" (kept in this browser); with Windows' reduced motion it stays still.
(() => {
  "use strict";
  const D = window.Delta, {$, h} = D;
  const NS = "http://www.w3.org/2000/svg";
  const el = (tag, attrs = {}) => {
    const e = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, String(v));
    return e;
  };
  const LINES = {
    click: ["Hi! I'm Tri.", "Hehe, that tickles!", "Ready when you are!"],
    run: ["Catch me!", "Too slow!", "Over here!"],
    caught: ["Okay, okay, you got me!", "You win. Again?"],
    done: ["Done!", "Nice, that worked.", "All finished!", "Ta-da!"],
    oops: ["Hmm, that one didn't work.", "Oops. Check what went wrong above."],
    ask: ["I need your answer to carry on."],
    back: ["Welcome back!", "Oh, you're back!"],
    silly: ["*sneeze*", "Did you know triangles are the strongest shape?"],
  };
  const pick = list => list[Math.floor(Math.random() * list.length)];

  // ---- the drawing: body, brows, eyes, two mouths (shown by mood), cheeks, shadow
  const svg = el("svg", {viewBox: "0 0 120 116", class: "tri-svg", "aria-hidden": "true", focusable: "false"});
  const eyes = el("g", {class: "tri-eyes"});
  eyes.append(el("ellipse", {cx: 49, cy: 66, rx: 8, ry: 10, class: "tri-eye"}), el("ellipse", {cx: 71, cy: 66, rx: 8, ry: 10, class: "tri-eye"}),
    el("circle", {cx: 51, cy: 69, r: 4.5, class: "tri-pupil"}), el("circle", {cx: 73, cy: 69, r: 4.5, class: "tri-pupil"}));
  const body = el("g", {class: "tri-body"});
  body.append(el("path", {d: "M60 10 L112 100 L8 100 Z", class: "tri-shape"}),
    el("path", {d: "M40 52 L54 55", class: "tri-brow"}), el("path", {d: "M80 52 L66 55", class: "tri-brow"}), eyes,
    el("path", {d: "M51 84 Q60 92 69 84", class: "tri-mouth smile"}), el("ellipse", {cx: 60, cy: 87, rx: 4, ry: 5, class: "tri-mouth o"}),
    el("ellipse", {cx: 38, cy: 82, rx: 5, ry: 3, class: "tri-cheek"}), el("ellipse", {cx: 82, cy: 82, rx: 5, ry: 3, class: "tri-cheek"}),
    el("text", {x: 92, y: 30, class: "tri-z"}));
  body.lastChild.textContent = "z";
  svg.append(el("ellipse", {cx: 60, cy: 108, rx: 40, ry: 4, class: "tri-shadow"}), body);
  const bubble = h("span", {class: "tri-bubble", role: "status", "aria-live": "polite"});
  const pet = h("button", {type: "button", class: "tri", "aria-label": "Tri, Delta's pet. Click to say hi."}, bubble, svg);
  pet.dataset.mood = "idle";

  let mood = "idle", busy = false, idleTimer = 0, sayTimer = 0, moodTimer = 0, hiddenAt = 0;
  function set(next, ms = 0) {
    clearTimeout(moodTimer);
    mood = next; pet.dataset.mood = next;
    if (ms) moodTimer = setTimeout(() => set(busy ? "working" : "idle"), ms);
    restartIdle();
  }
  function say(text, ms = 3500) {
    clearTimeout(sayTimer);
    bubble.textContent = text; pet.classList.add("talking");
    sayTimer = setTimeout(() => pet.classList.remove("talking"), ms);
  }
  function restartIdle() {
    clearTimeout(idleTimer);
    idleTimer = setTimeout(() => { if (!busy && mood === "idle") set("sleep"); }, 90000);
  }

  // ---- play. A click mostly gets a trick; sometimes Tri runs to another corner ("catch me") and lets itself be
  // caught on the third try. Left alone, it does a small trick now and then. All of it is CSS classes, and none of
  // it happens while Delta works, while you type, or with Windows' reduced motion.
  const still = window.matchMedia("(prefers-reduced-motion: reduce)");
  const TRICKS = ["spin", "peek", "wiggle", "hop"], SPOTS = ["spot-left", "spot-top", "spot-mid"];
  let runs = 0, playTimer = 0;
  const home = () => { pet.classList.remove(...SPOTS); runs = 0; };
  const free = () => !busy && !still.matches && !pet.classList.contains("ducked") && !document.hidden;
  function playLater() {
    clearTimeout(playTimer);
    playTimer = setTimeout(() => { if (free() && mood === "idle") set(pick(TRICKS), 1600); playLater(); }, 45000 + Math.random() * 45000);
  }
  pet.onclick = () => {
    if (!free()) { set("laugh", 1400); return; }
    if (runs >= 2) { home(); set("laugh", 1400); say(pick(LINES.caught)); return; }
    if (runs || Math.random() < 0.3) {
      const spot = pick(SPOTS.filter(s => !pet.classList.contains(s)));
      pet.classList.remove(...SPOTS); pet.classList.add(spot); runs += 1;
      set("hop", 900); say(pick(LINES.run), 1800); return;
    }
    if (Math.random() < 0.25) { set("laugh", 1400); say(pick(Math.random() < 0.3 ? LINES.silly : LINES.click)); return; }
    set(pick(TRICKS), 1600);
  };
  // Eyes follow the pointer (one of nine directions, as a class).
  let lookAt = 0;
  document.addEventListener("pointermove", e => {
    if (e.timeStamp - lookAt < 120 || !pet.isConnected) return;
    lookAt = e.timeStamp;
    const r = pet.getBoundingClientRect(), dx = e.clientX - (r.left + r.width / 2), dy = e.clientY - (r.top + r.height / 2);
    pet.dataset.look = (Math.abs(dy) < 60 ? "" : dy < 0 ? "u" : "d") + (Math.abs(dx) < 60 ? "" : dx < 0 ? "l" : "r");
  }, {passive: true});
  // Never in the way: while Tri covers a button, link or box of the page, it fades and clicks go through it.
  const CONTROL = "button,a,input,select,textarea,summary,label,[role=button],[role=tab],[role=switch]";
  setInterval(() => {
    if (!pet.isConnected || document.hidden) return;
    const r = pet.getBoundingClientRect();
    const covered = [[.5, .5], [.2, .3], [.8, .3], [.2, .85], [.8, .85]].some(([fx, fy]) => {
      const under = document.elementsFromPoint(r.left + r.width * fx, r.top + r.height * fy).find(n => !pet.contains(n));
      return !!(under && under.closest(CONTROL));
    });
    pet.classList.toggle("aside", covered);
  }, 400);

  // ---- real events only
  D.on("ws", m => {
    if (m.type === "result") {
      busy = false;
      const st = m.result && m.result.status;
      home();
      if (st === "done") { set("done", 2600); say(pick(LINES.done)); }
      else if (st === "needs_user") { set("curious", 4000); say(pick(LINES.ask)); }
      else if (st === "cancelled") set("idle");
      else { set("oops", 3500); say(pick(LINES.oops)); }
      return;
    }
    if (m.type !== "event") return;
    if (m.event === "task.started" || m.event === "task.queued") { busy = true; set("thinking"); }
    else if (m.event === "task.plan") set("thinking");
    else if (m.event === "task.step") { busy = true; if (mood !== "working") set("working"); }
  });
  // Typing: Tri ducks down so it never distracts, and peeks back when you are done.
  const box = $("text");
  if (box) {
    box.addEventListener("focus", () => pet.classList.add("ducked"));
    box.addEventListener("blur", () => pet.classList.remove("ducked"));
  }
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) { hiddenAt = Date.now(); return; }
    if (hiddenAt && Date.now() - hiddenAt > 10 * 60000) { set("done", 1800); say(pick(LINES.back)); }
    else if (mood === "sleep") set("idle");
  });

  // ---- greeting once per day, with the owner's first name when setup has it
  async function greet() {
    const today = new Date().toDateString();
    if (D.pref.get("delta_pet_greeted") === today) return;
    D.pref.set("delta_pet_greeted", today);
    const s = await D.api("/api/setup").catch(() => ({}));
    const name = String((s && s.owner_name) || "").trim().split(/\s+/)[0];
    const hr = new Date().getHours();
    const part = hr < 12 ? "Good morning" : hr < 17 ? "Good afternoon" : "Good evening";
    set("done", 1800); say(`${part}${name ? ", " + name : ""}! I'm Tri. Let's get things done.`, 5000);
  }

  D.pet = {set, say};  // voice mode: Tri listens and talks along

  // ---- on and off (Settings > Look and feel)
  function show(on) {
    if (on && !pet.isConnected) { document.body.append(pet); restartIdle(); playLater(); setTimeout(greet, 1500); }
    if (!on && pet.isConnected) pet.remove();
  }
  const sw = $("s_pet");
  if (sw) {
    sw.checked = D.pref.get("delta_pet") !== "off";
    sw.onchange = () => { D.pref.set("delta_pet", sw.checked ? null : "off"); show(sw.checked); };
  }
  show(D.pref.get("delta_pet") !== "off");
})();
