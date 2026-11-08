// Start-up and the frame around every view: sidebar, top bar status (model, browser, phone control, connection),
// warnings, first-run guidance, keyboard shortcuts and shutdown. Loaded last.
(() => {
  "use strict";
  const D = window.Delta, {$, api, h, icon} = D;
  D.hydrate();

  // ---- sidebar
  const narrow = () => innerWidth < 760;
  function collapse(v) {
    document.body.classList.toggle("sb-collapsed", v);
    $("sbcollapse").setAttribute("aria-label", v ? "Expand sidebar" : "Collapse sidebar");
    $("sbcollapse").title = (v ? "Expand sidebar" : "Collapse sidebar") + " (Ctrl+B)";
    D.pref.set("delta_sidebar", v ? "collapsed" : "open");
  }
  function toggleSidebar() {
    if (narrow()) { const open = document.body.classList.toggle("sb-open"); $("scrim").hidden = !open; if (open) document.querySelector(".nav-item")?.focus(); }
    else collapse(!document.body.classList.contains("sb-collapsed"));
  }
  $("sbcollapse").onclick = toggleSidebar;
  $("menubtn").onclick = toggleSidebar;
  $("scrim").onclick = () => { document.body.classList.remove("sb-open"); $("scrim").hidden = true; };
  collapse(D.pref.get("delta_sidebar") === "collapsed");
  for (const n of document.querySelectorAll(".nav-item[data-go]")) n.addEventListener("click", () => D.go(n.dataset.go));

  // ---- theme
  const themeLabel = () => { $("themelabel").textContent = "Theme: " + {system: "System", light: "Light", dark: "Dark"}[window.DeltaTheme.mode]; };
  $("themebtn").onclick = () => window.DeltaTheme.next();
  document.addEventListener("delta-theme", themeLabel);
  themeLabel();

  // ---- top bar status
  const chip = (id, text, kind, title) => { const c = $(id); c.querySelector(".v").textContent = text; c.dataset.kind = kind || ""; if (title) c.title = title; };
  let status = null, health = "", unauthorized = false;
  async function refreshStatus() {
    try {
      const s = await api("/api/status"), g = s.governor;
      status = s; unauthorized = false; health = g.health;
      const model = s.last_provider || s.providers[0] || "";
      chip("st_model", model ? "Model: " + model : "No AI model", model ? "" : "warn",
        model ? `Models in order: ${s.providers.join(", ")}. Click to manage.` : "Delta has no AI model yet. Click to connect one.");
      $("warnings").hidden = !(s.warnings || []).length;
      $("warnings").replaceChildren(...(s.warnings || []).map(w => h("div", {class: "banner-line"}, icon("alert"), h("span", {text: w}))));
      gsUpdate();
    } catch (e) {
      unauthorized = e.message === "unauthorized";
      status = null; health = "";
    }
    connChip();
  }
  function connChip() {
    const c = D.conn;
    let text, kind;
    if (unauthorized) { text = "No access"; kind = "danger"; }
    else if (c === "open") { text = health === "NORMAL" || !health ? "Connected" : health === "THROTTLE" ? "Busy PC" : health === "CRITICAL" ? "PC overloaded" : D.words(health.toLowerCase()); kind = health && health !== "NORMAL" ? (health === "CRITICAL" ? "danger" : "warn") : "ok"; }
    else if (c === "off") { text = "Stopped"; kind = "danger"; }
    else { text = status ? "Reconnecting" : "Offline"; kind = status ? "warn" : "danger"; }
    chip("st_conn", text, kind, unauthorized ? "This page's access key was not accepted. Open Delta again from its shortcut." : "Connection and this PC's load. Click for details.");
    if (!$("sysinfo").hidden) sysinfo();
  }
  function sysinfo() {
    const box = $("sysinfo");
    const row = (k, v) => h("div", {class: "kv"}, h("span", {class: "muted", text: k}), h("b", {text: v}));
    if (!status) { box.replaceChildren(h("p", {text: unauthorized ? "This page's access key was not accepted. Open Delta again from its Desktop or Start menu shortcut." : "Delta is not reachable. It may be starting up or shut down. The dashboard keeps trying."})); return; }
    const g = status.governor;
    box.replaceChildren(...[
      row("Live updates", D.conn === "open" ? "Connected" : "Reconnecting"),
      row("Health", g.health === "NORMAL" ? "Ready" : D.words(g.health.toLowerCase()) + (g.reasons.length ? ": " + g.reasons.join(", ") : "")),
      row("Memory used", `${g.ram_pct}%`), row("Processor", `${g.cpu_pct}%`),
      g.gpu_temp_c == null ? null : row("Graphics card", `${g.gpu_temp_c} °C, ${g.vram_pct ?? "?"}% memory`),
      row("Delta uses", `${g.process_rss_mb} MB`), row("Models", status.providers.join(", ") || "none"),
      row("Permission level", {"ask-everything": "Careful", "ask-risky": "Balanced", "trusted": "Trusted", "autopilot": "Autopilot"}[status.autonomy] || status.autonomy)].filter(Boolean));
  }
  $("st_conn").onclick = e => {
    e.stopPropagation();
    const open = $("sysinfo").hidden;
    $("sysinfo").hidden = !open; $("st_conn").setAttribute("aria-expanded", String(open));
    if (open) sysinfo();
  };
  document.addEventListener("click", e => { const si = $("sysinfo"); if (si && !si.hidden && !si.contains(e.target)) { si.hidden = true; $("st_conn").setAttribute("aria-expanded", "false"); } });  // gone on the Updating screen
  $("st_model").onclick = () => status && status.providers.length ? D.go("apps") : D.openSetup("models");
  $("st_browser").onclick = () => D.go("settings", {section: "set_browser"});
  $("st_remote").onclick = () => D.go("settings", {section: "set_remote"});
  async function refreshChips() {
    const s = await api("/api/settings").catch(() => null);
    if (s && s.browser) chip("st_browser", s.browser.label || "Browser", "", `Browser: ${s.browser.describe || s.browser.label}. Click to change.`);
    D.settings.guardCard();
  }
  D.on("security", s => chip("st_remote", s.locked ? "Remote locked" : s.has_pin ? "PIN on" : "No PIN", s.locked ? "danger" : s.has_pin ? "" : "warn",
    s.locked ? "Phone control is locked after wrong PINs. Click to unlock." : s.has_pin ? "Phone control is protected by your PIN." : "No PIN: sensitive phone actions are blocked. Click to set one."));
  D.on("conn", connChip);
  D.on("status:refresh", () => { refreshStatus(); refreshChips(); });
  D.on("settings:saved", refreshChips);

  // ---- first-run guidance: connect a model, pick a browser, try an example
  function gsUpdate() {
    const box = $("getstarted"); if (!box || !status) return;
    const steps = {gs_model: status.providers.length > 0, gs_browser: D.pref.get("delta_gs_browser") === "1", gs_try: D.pref.get("delta_tried") === "1"};
    for (const [id, done] of Object.entries(steps)) {
      const li = $(id); if (!li) continue;
      li.classList.toggle("done", done);
      const dot = li.querySelector(".gs-dot"); dot.replaceChildren(icon(done ? "check" : "circle"));
      li.querySelector("button")?.toggleAttribute("hidden", done);
    }
    box.hidden = D.pref.get("delta_gs_hidden") === "1" || Object.values(steps).every(Boolean);
  }
  function gsBind() {
    if (!$("getstarted")) return;
    $("gs_hide").onclick = () => { D.pref.set("delta_gs_hidden", "1"); gsUpdate(); };
    $("gs_model_go").onclick = () => D.openSetup("models");
    $("gs_browser_go").onclick = () => { D.pref.set("delta_gs_browser", "1"); D.go("settings", {section: "set_browser"}); };
    gsUpdate();
  }
  D.on("hero", gsBind);
  gsBind();
  D.on("setup:done", gsUpdate);

  // ---- shutdown
  D.shutdown = async () => {
    if (!await D.confirm({title: "Shut Delta down?", body: "Models are unloaded and nothing keeps running, including schedules and phone control. Open Delta from your Desktop or Start menu to start again.", ok: "Shut down", danger: true})) return;
    D.disconnect();
    await api("/api/shutdown", {method: "POST"}).catch(() => {});
    document.body.replaceChildren(h("main", {class: "off-screen"}, h("div", {}, h("span", {class: "hero-mark"}, icon("logo")), h("h1", {text: "Delta is off"}),
      h("p", {class: "muted", text: "Everything is stopped and memory is freed. Open Delta from your Desktop or Start menu to start again."}))));
    setTimeout(() => window.close(), 1500);
  };
  $("shutdown").onclick = D.shutdown;

  // ---- keyboard
  const ORDER = ["chat", "tasks", "apps", "schedules", "documents", "memory", "settings"];
  document.addEventListener("keydown", e => {
    if (e.ctrlKey && !e.shiftKey && !e.altKey && e.key.toLowerCase() === "b") { e.preventDefault(); toggleSidebar(); }
    else if (e.altKey && !e.ctrlKey && /^[1-7]$/.test(e.key)) { e.preventDefault(); D.go(ORDER[Number(e.key) - 1]); }
    else if (e.key === "Escape") {
      const si = $("sysinfo"); if (!si) return;  // the Updating screen replaced the dashboard
      if (!si.hidden) { si.hidden = true; $("st_conn").focus(); }
      else if (document.body.classList.contains("sb-open")) { document.body.classList.remove("sb-open"); $("scrim").hidden = true; }
    }
  });

  // ---- start
  if ("Notification" in window && Notification.permission === "default") Notification.requestPermission().catch?.(() => {});
  let start = "chat";
  try { start = sessionStorage.getItem("delta_view") || "chat"; } catch {}
  D.go(start);
  D.connect();
  D.chat.start();
  D.memory.start();
  refreshStatus(); refreshChips();
  D.setup.start();
  D.updates.start().catch(() => {}).finally(() => D.tour.maybe());  // What's new after an update first, then the tour
  D.every(5000, refreshStatus);
  D.every(60000, refreshChips);
  D.on("visible", refreshStatus);
})();
