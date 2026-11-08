// First-run tour: a few spotlight steps over the real dashboard. Shown once per PC after setup (saved on the PC:
// /api/tour), skippable (Esc), keyboard driven (arrows; Tab stays in the card), read out by screen readers, and
// replayable from Settings > Updates and help or by typing /tour in the chat box. A step whose element is hidden on
// this screen (a narrow window) is skipped. No library, nothing fetched.
(() => {
  "use strict";
  const D = window.Delta, {$, api, h} = D;
  const STEPS = [
    {title: "Welcome to Delta", body: "Delta is an open source AI agent that does real work on your Windows PC and checks the result before it says done. Here is a quick look around."},
    {sel: "#composer", view: "chat", title: "Ask in plain words", body: "Type what you want done here. Try \"open notepad\". Anything that changes or sends something waits for your OK first."},
    {sel: ".nav-item[data-go='tasks']", title: "Watch your tasks", body: "Every task, step by step, from here, your phone and schedules. Press Stop anytime, or press Esc twice on the keyboard."},
    {sel: ".nav-item[data-go='apps']", title: "Connect apps", body: "Connect Gmail, Notion, Slack and more, and pick the AI model Delta thinks with."},
    {sel: ".nav-item[data-go='schedules']", title: "Schedules", body: "Reminders and tasks that run by themselves, like \"every day at 9 am, check my college portal\"."},
    {sel: ".nav-item[data-go='documents']", also: ".nav-item[data-go='memory']", title: "Documents and memory", body: "Files Delta made for you are in Documents. What it remembers is in Memory, and you can make it forget anything."},
    {sel: "#set_keys", view: "settings", title: "Add a free AI key", body: "Settings > Keys. A free Groq key from console.groq.com/keys takes a minute and works well. Keys stay in Windows Credential Manager."},
    {sel: "#set_logins", view: "settings", title: "Saved logins", body: "Save sign-ins for sites like your college portal. Delta types them only on that exact site, and reads simple text captchas by itself."},
    {sel: "#set_about", view: "settings", title: "Phone and start-up", body: "Control Delta from Telegram on your phone (Phone and PIN, above). Turn on start with Windows so Telegram and schedules keep working after a restart. Replay this tour from Updates and help, or type /tour."},
  ];
  let i = -1, root = null, spot, card, titleEl, bodyEl, countEl, backB, nextB, skipB, cur = [], from = "chat", lastFocus = null, raf = 0, token = 0;
  const wait = ms => new Promise(r => setTimeout(r, ms));
  const clamp = (v, lo, hi) => Math.max(lo, Math.min(v, hi));

  function onScreen(el) {
    if (!el || el.closest("[hidden]") || !el.getClientRects().length) return false;
    const r = el.getBoundingClientRect();
    return r.width > 2 && r.height > 2 && r.right > 0 && r.bottom > 0 && r.left < innerWidth && r.top < innerHeight;
  }
  async function find(sel, mine) {
    let off = 0;
    for (let t = 0; t < 30 && off < 3 && mine === token; t++) {  // a view may still be loading: up to 1.5 s
      const el = document.querySelector(sel);
      if (el && !el.closest("[hidden]") && el.getClientRects().length) {
        el.scrollIntoView({block: "nearest"});
        if (onScreen(el)) return el;
        off++;  // laid out but off screen (the folded sidebar of a narrow window): skip it soon
      }
      await wait(50);
    }
    return null;
  }

  function build() {
    spot = h("div", {class: "tour-spot", "aria-hidden": "true"});
    titleEl = h("h2", {id: "tour_title", tabindex: "-1"});
    bodyEl = h("p", {id: "tour_body"});
    countEl = h("span", {class: "tour-count muted small", "aria-live": "polite"});
    skipB = h("button", {type: "button", class: "link", onclick: () => end(true)}, "Skip tour");
    backB = D.btn("Back", () => move(-1), "sm");
    nextB = D.btn("Next", () => move(1), "primary sm");
    card = h("div", {class: "tour-card", role: "dialog", "aria-modal": "true", "aria-labelledby": "tour_title", "aria-describedby": "tour_body"},
      countEl, titleEl, bodyEl, h("div", {class: "tour-foot"}, skipB, h("span", {class: "grow"}), backB, nextB),
      h("p", {class: "muted small tour-keys", text: "Arrow keys to move, Esc to skip."}));
    root = h("div", {class: "tour", id: "tour"}, h("div", {class: "tour-block"}), spot, card);
    document.body.append(root);
  }

  let busy = false;
  async function show(n, dir) {
    const mine = ++token;
    busy = true;
    try {
      while (n >= 0 && n < STEPS.length) {
        const s = STEPS[n];
        if (s.view && D.view !== s.view) D.go(s.view);
        const el = s.sel ? await find(s.sel, mine) : null;
        if (mine !== token || !root) return;
        if (!s.sel || el) { i = n; render(s, el); return; }
        n += dir;  // not on this screen: skip it
      }
    } finally { if (mine === token) busy = false; }
    if (n >= STEPS.length) end(false);
  }
  function render(s, el) {
    cur = el ? [el, s.also ? document.querySelector(s.also) : null].filter(onScreen) : [];
    titleEl.textContent = s.title;
    bodyEl.textContent = s.body;
    countEl.textContent = `Step ${i + 1} of ${STEPS.length}`;
    backB.disabled = i === 0;
    nextB.textContent = i === STEPS.length - 1 ? "Done" : "Next";
    place();
    titleEl.focus({preventScroll: true});
  }
  function place() {
    if (!root) return;
    const m = 12, pad = 6, vw = innerWidth, vh = innerHeight;
    const live = cur.filter(e => e.isConnected);
    root.classList.toggle("centered", !live.length);
    if (!live.length) { spot.hidden = true; card.classList.add("mid"); card.style.left = card.style.top = ""; return; }
    const rs = live.map(e => e.getBoundingClientRect());
    const r = {left: clamp(Math.min(...rs.map(x => x.left)), 0, vw), top: clamp(Math.min(...rs.map(x => x.top)), 0, vh),
      right: clamp(Math.max(...rs.map(x => x.right)), 0, vw), bottom: clamp(Math.max(...rs.map(x => x.bottom)), 0, vh)};
    spot.hidden = false;
    Object.assign(spot.style, {left: r.left - pad + "px", top: r.top - pad + "px", width: r.right - r.left + 2 * pad + "px", height: r.bottom - r.top + 2 * pad + "px"});
    card.classList.remove("mid");
    const cw = card.offsetWidth, ch = card.offsetHeight;
    let left, top;
    if (r.right + pad + m + cw <= vw - m && r.right - r.left < vw / 2) { left = r.right + pad + m; top = r.top; }  // right of it (the sidebar)
    else if (r.bottom + pad + m + ch <= vh - m) { top = r.bottom + pad + m; left = r.left; }  // below
    else if (r.top - pad - m - ch >= m) { top = r.top - pad - m - ch; left = r.left; }  // above (the chat box)
    else { left = vw - cw - m; top = vh - ch - m; }  // a big section: in the corner, over it
    card.style.left = clamp(left, m, Math.max(m, vw - cw - m)) + "px";
    card.style.top = clamp(top, m, Math.max(m, vh - ch - m)) + "px";
  }
  const later = () => { cancelAnimationFrame(raf); raf = requestAnimationFrame(place); };

  function move(d) {
    if (busy) return;  // still finding the next step
    if (d > 0 && i >= STEPS.length - 1) { end(false); return; }
    if (d < 0 && i <= 0) return;
    show(i + d, d);
  }
  function onKey(e) {
    if (!root) return;
    const k = e.key;
    if (k === "Escape") { e.preventDefault(); end(true); }
    else if (k === "ArrowRight" || k === "ArrowDown") { e.preventDefault(); move(1); }
    else if (k === "ArrowLeft" || k === "ArrowUp") { e.preventDefault(); move(-1); }
    else if (k === "Tab") {
      const items = [skipB, backB, nextB].filter(b => !b.disabled);
      const at = items.indexOf(document.activeElement);
      e.preventDefault();
      items[at < 0 ? 0 : (at + (e.shiftKey ? -1 : 1) + items.length) % items.length].focus();
    } else if (k !== "Enter" && k !== " ") return;  // Enter and Space press the focused button as usual
    e.stopPropagation();  // the dashboard's own shortcuts wait until the tour ends
  }

  function start() {
    if (root || !$("scrim")) return;  // no dashboard left (the Updating screen replaced it)
    const open = ["setup", "sheet", "confirm"].map(id => $(id)).find(d => d && d.open);
    if (open) { open.addEventListener("close", () => setTimeout(start, 200), {once: true}); return; }
    from = D.view; lastFocus = document.activeElement;
    document.body.classList.remove("sb-open"); $("scrim").hidden = true;
    build();
    window.addEventListener("keydown", onKey, true);
    window.addEventListener("resize", later);
    document.addEventListener("scroll", later, true);
    show(0, 1);
  }
  function end(skipped) {
    if (!root) return;
    token++;
    root.remove(); root = null; cur = [];
    window.removeEventListener("keydown", onKey, true);
    window.removeEventListener("resize", later);
    document.removeEventListener("scroll", later, true);
    api("/api/tour", {method: "PUT", body: JSON.stringify({done: true})}).catch(() => {});
    D.go(from === "settings" ? "settings" : "chat");
    if (from !== "settings") $("text").focus(); else lastFocus?.focus?.();
    if (skipped && from !== "settings") D.toast({title: "Tour skipped", body: "Take it anytime from Settings > Updates and help, or type /tour."});
  }
  async function maybe() {
    const t = await api("/api/tour").catch(() => null);
    if (t && !t.done && t.setup_completed) start();
  }
  D.on("setup:done", () => setTimeout(maybe, 300));
  $("tour_start").onclick = start;
  D.tour = {start, maybe, get open() { return !!root; }};
})();
