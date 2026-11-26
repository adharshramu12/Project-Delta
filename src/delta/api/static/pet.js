// Tri, Delta's pet: a small triangle (Delta's own Δ) in the corner that shows what Delta is doing. It thinks while
// Delta plans, works while it acts, jumps when a task is done, tips over when one fails, hides while you type,
// sleeps when nothing happens, greets you, and laughs when you click it. It has arms and legs (it waves, dances and
// flexes), wears an outfit (double-click to change it), now and then rides its bike across the screen, and its friend
// Tia visits with hearts. It only reacts to real events or plays when Delta is idle, so it is never noise. Drawn with SVG shapes and CSS classes (no images, no library, no inline styles). Off with Settings >
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
    silly: ["*sneeze*", "Did you know triangles are the strongest shape?", "I can lift 3 pixels. Easily.", "Is it snack time?"],
    ride: ["Vroom vroom!", "Beep beep, coming through!", "Wheee!"],
    dance: ["Dance break!", "Look at my moves!"],
    flex: ["Strong triangle!", "Leg day every day."],
    wave: ["Hi there!", "Hello!"],
    outfit: ["New look!", "How do I look?", "Fashion!"],
    tia: ["Look who's here: Tia!", "Tia came to say hi!", "My friend Tia is visiting!"],
  };
  const pick = list => list[Math.floor(Math.random() * list.length)];

  // ---- the drawing: body, brows, eyes, two mouths (shown by mood), cheeks, shadow
  const svg = el("svg", {viewBox: "0 0 120 124", class: "tri-svg", "aria-hidden": "true", focusable: "false"});
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
  // arms and legs, behind the body so they come out of its sides and bottom
  const limb = (cls, d, x, y, r) => { const g = el("g", {class: cls}); g.append(el("path", {d, class: "tri-limb"}), el("circle", {cx: x, cy: y, r, class: "tri-hand"})); return g; };
  body.prepend(limb("tri-arm l", "M30 76 Q14 78 4 90", 3, 91, 6), limb("tri-arm r", "M90 76 Q106 78 116 90", 117, 91, 6),
    limb("tri-leg l", "M46 99 L44 112", 42, 114, 0), limb("tri-leg r", "M74 99 L76 112", 78, 114, 0));
  for (const leg of body.querySelectorAll(".tri-leg")) {
    const x = leg.classList.contains("l") ? 41 : 79;
    leg.lastChild.replaceWith(el("ellipse", {cx: x, cy: 114, rx: 7, ry: 3.5, class: "tri-hand"}));
  }
  // outfits: one group each, shown by data-outfit
  const outfit = el("g", {class: "tri-outfit"});
  const part = (name, ...shapes) => { const g = el("g", {class: "fit " + name}); shapes.forEach(([t, a]) => g.append(el(t, a))); outfit.append(g); };
  part("cap", ["path", {d: "M42 34 Q60 14 78 34 Z", class: "fit-a"}], ["path", {d: "M74 33 L92 36 L76 38 Z", class: "fit-a"}]);
  part("party", ["path", {d: "M50 26 L60 -4 L70 26 Z", class: "fit-b"}], ["circle", {cx: 60, cy: -5, r: 4, class: "fit-c"}]);
  part("crown", ["path", {d: "M46 30 L46 16 L53 24 L60 12 L67 24 L74 16 L74 30 Z", class: "fit-c"}]);
  part("shades", ["rect", {x: 39, y: 60, width: 18, height: 12, rx: 4, class: "fit-d"}], ["rect", {x: 63, y: 60, width: 18, height: 12, rx: 4, class: "fit-d"}], ["path", {d: "M57 65 L63 65", class: "fit-line"}]);
  part("scarf", ["path", {d: "M22 92 Q60 102 98 92 L98 99 Q60 108 22 99 Z", class: "fit-b"}], ["path", {d: "M84 97 L90 112 L82 112 Z", class: "fit-b"}]);
  part("bow", ["path", {d: "M60 30 L48 22 L48 38 Z M60 30 L72 22 L72 38 Z", class: "fit-e"}], ["circle", {cx: 60, cy: 30, r: 3.5, class: "fit-e"}]);
  body.append(outfit);
  // the bike: hidden until a ride
  const bike = el("g", {class: "tri-bike"});
  bike.append(el("circle", {cx: 26, cy: 112, r: 10, class: "bike-wheel"}), el("circle", {cx: 94, cy: 112, r: 10, class: "bike-wheel"}),
    el("path", {d: "M26 112 L48 98 L80 98 L94 112 M48 98 L60 112 L80 98 M86 88 L80 98 M82 86 L92 86", class: "bike-frame"}));
  svg.append(el("ellipse", {cx: 60, cy: 120, rx: 40, ry: 4, class: "tri-shadow"}), bike, body);
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
  const TRICKS = ["spin", "peek", "wiggle", "hop", "wave", "dance", "flex"], SPOTS = ["spot-left", "spot-top", "spot-mid"];
  const OUTFITS = ["", "cap", "party", "crown", "shades", "scarf"];
  function dress(name, remember = true) {
    pet.dataset.outfit = name || "";
    if (remember) D.pref.set("delta_pet_outfit", name || null);
  }
  dress(D.pref.get("delta_pet_outfit") || "", false);
  function trick(name) {  // a trick with its line, when it has one
    set(name, name === "dance" ? 2400 : 1600);
    if (LINES[name] && Math.random() < 0.6) say(pick(LINES[name]), 2000);
  }
  function ride() {  // across the bottom of the screen and back, on the bike
    if (!free() || pet.classList.contains("riding")) return;
    home(); pet.classList.add("riding"); set("riding", 6200); say(pick(LINES.ride), 2200);
    setTimeout(() => pet.classList.remove("riding"), 6200);
  }
  let tia = null;
  function tiaVisits() {  // Tri's friend Tia comes by for a few seconds, with hearts
    if (!free() || tia || !pet.isConnected) return;
    tia = svg.cloneNode(true);
    tia.querySelector(".tri-bike")?.remove();
    tia.setAttribute("class", "tri-svg");
    const box = h("span", {class: "tia", "aria-hidden": "true"}, tia, h("span", {class: "tia-hearts", text: "\u2764 \u2764 \u2764"}));
    box.querySelector(".tri-outfit").dataset.fit = "bow";
    pet.append(box); set("hop", 1200); say(pick(LINES.tia), 3000);
    setTimeout(() => { box.classList.add("leaving"); }, 6000);
    setTimeout(() => { box.remove(); tia = null; }, 6800);
  }
  let runs = 0, playTimer = 0;
  const home = () => { pet.classList.remove(...SPOTS); runs = 0; };
  const free = () => !busy && !still.matches && !pet.classList.contains("ducked") && !document.hidden;
  function playLater() {
    clearTimeout(playTimer);
    playTimer = setTimeout(() => {
      if (free() && mood === "idle") {
        const roll = Math.random();
        if (roll < 0.12) ride();
        else if (roll < 0.22) tiaVisits();
        else if (roll < 0.3) { dress(pick(OUTFITS.filter(o => o !== pet.dataset.outfit))); set("hop", 900); say(pick(LINES.outfit), 2000); }
        else trick(pick(TRICKS));
      }
      playLater();
    }, 45000 + Math.random() * 45000);
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
    const roll = Math.random();
    if (roll < 0.12) { ride(); return; }
    if (roll < 0.2) { tiaVisits(); return; }
    trick(pick(TRICKS));
  };
  pet.ondblclick = e => {  // a new outfit
    e.preventDefault();
    const i = OUTFITS.indexOf(pet.dataset.outfit || "");
    dress(OUTFITS[(i + 1) % OUTFITS.length]); set("spin", 800); say(pick(LINES.outfit), 1800);
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

  D.pet = {set, say, ride, tiaVisits, dress, trick};  // voice mode: Tri listens and talks along

  // ---- on and off (Settings > Look and feel)
  function show(on) {
    if (on && !pet.isConnected) { document.body.append(pet); restartIdle(); playLater(); setTimeout(greet, 1500); }
    if (!on && pet.isConnected) pet.remove();
    if (on && pet.isConnected && new Date().getHours() < 5) dress("cap", false);  // very late: a cap, for the night
  }
  const sw = $("s_pet");
  if (sw) {
    sw.checked = D.pref.get("delta_pet") !== "off";
    sw.onchange = () => { D.pref.set("delta_pet", sw.checked ? null : "off"); show(sw.checked); };
  }
  show(D.pref.get("delta_pet") !== "off");
})();
