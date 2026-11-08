// Delta dashboard core: access token, API calls, safe DOM building, icons, views, toasts, dialogs, the live
// connection and polling. Plain browser JavaScript, no framework and no build step. Every view file adds itself
// to the shared `Delta` object. Text from the user, the AI, web pages or apps is only ever set with textContent.
(() => {
  "use strict";
  // ---- access token: arrives in the URL fragment (never sent to the server); kept for reloads when storage allows
  let token = new URLSearchParams(location.hash.slice(1)).get("token") || "";
  try { if (token) sessionStorage.setItem("delta_token", token); else token = sessionStorage.getItem("delta_token") || ""; } catch {}
  if (location.hash) history.replaceState(null, "", "/");
  const $ = id => document.getElementById(id);
  const api = (path, opts = {}) => fetch(path, {...opts, headers: {"Authorization": "Bearer " + token, "Content-Type": "application/json"}})
    .then(r => { if (r.status === 401) throw new Error("unauthorized"); return r.json(); });
  const blob = path => fetch(path, {headers: {"Authorization": "Bearer " + token}})
    .then(r => { if (!r.ok) throw new Error(String(r.status)); return r.blob(); });

  // ---- events between the view files
  const subs = {};
  const on = (name, fn) => { (subs[name] = subs[name] || new Set()).add(fn); return () => subs[name].delete(fn); };
  const emit = (name, data) => { for (const fn of [...(subs[name] || [])]) { try { fn(data); } catch (e) { console.error(e); } } };

  // ---- icons: plain line drawings, built as SVG elements (no icon font, nothing fetched)
  const P = d => ["path", {d}];
  const ICONS = {
    logo: [["rect", {x: 2, y: 2, width: 20, height: 20, rx: 6, class: "logo-bg"}], ["path", {d: "M12 7.2 16.6 16H7.4z", class: "logo-fg"}]],
    sidebar: [["rect", {x: 3, y: 4, width: 18, height: 16, rx: 2.5}], P("M9 4v16")],
    panel: [["rect", {x: 3, y: 4, width: 18, height: 16, rx: 2.5}], P("M15 4v16")],
    chat: [P("M20.5 12a8.5 8.5 0 0 1-12.4 7.6L3.5 20.5l1-4.4A8.5 8.5 0 1 1 20.5 12z")],
    tasks: [P("M3.5 6.5 5 8l2.5-2.5M3.5 12.5 5 14l2.5-2.5M3.5 18.5 5 20l2.5-2.5M11 7h10M11 13h10M11 19h10")],
    apps: [["rect", {x: 3.5, y: 3.5, width: 7, height: 7, rx: 1.8}], ["rect", {x: 13.5, y: 3.5, width: 7, height: 7, rx: 1.8}], ["rect", {x: 3.5, y: 13.5, width: 7, height: 7, rx: 1.8}], ["rect", {x: 13.5, y: 13.5, width: 7, height: 7, rx: 1.8}]],
    clock: [["circle", {cx: 12, cy: 12, r: 8.5}], P("M12 7.5V12l3 2")],
    doc: [P("M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"), P("M14 3v5h5M9 13h6M9 17h4")],
    memory: [P("M12 3.5 3.5 8 12 12.5 20.5 8z"), P("M3.5 12 12 16.5 20.5 12M3.5 16 12 20.5 20.5 16")],
    settings: [P("M4 7h8M16 7h4M4 17h3M11 17h9"), ["circle", {cx: 14, cy: 7, r: 2}], ["circle", {cx: 9, cy: 17, r: 2}]],
    plus: [P("M12 5v14M5 12h14")],
    search: [["circle", {cx: 11, cy: 11, r: 6.5}], P("m20 20-4.2-4.2")],
    theme: [["circle", {cx: 12, cy: 12, r: 8.5}], ["path", {d: "M12 3.5a8.5 8.5 0 0 0 0 17z", class: "fill"}]],
    power: [P("M12 3v9"), P("M17.7 6.8a8 8 0 1 1-11.4 0")],
    menu: [P("M4 7h16M4 12h16M4 17h16")],
    cpu: [["rect", {x: 6, y: 6, width: 12, height: 12, rx: 2}], P("M9.5 9.5h5v5h-5zM9 3v3M15 3v3M9 18v3M15 18v3M3 9h3M3 15h3M18 9h3M18 15h3")],
    globe: [["circle", {cx: 12, cy: 12, r: 8.5}], P("M3.5 12h17M12 3.5c2.3 2.4 3.5 5.2 3.5 8.5s-1.2 6.1-3.5 8.5c-2.3-2.4-3.5-5.2-3.5-8.5S9.7 5.9 12 3.5z")],
    phone: [["rect", {x: 7, y: 3, width: 10, height: 18, rx: 2}], P("M11 17.5h2")],
    bell: [P("M6 9a6 6 0 1 1 12 0c0 6 2.5 8 2.5 8h-17S6 15 6 9"), P("M10.3 20.5a2 2 0 0 0 3.4 0")],
    x: [P("M6 6l12 12M18 6 6 18")],
    "arrow-up": [P("M12 19V5M6 11l6-6 6 6")],
    edit: [P("M12 20h8.5M16.5 3.5a2.1 2.1 0 1 1 3 3L7.5 18.5l-4 1 1-4z")],
    play: [["circle", {cx: 12, cy: 12, r: 8.5}], P("M10 8.8v6.4l5-3.2z")],
    send: [P("M21 3 10.5 13.5M21 3l-6.5 18-4-7.5-7.5-4z")],
    video: [["rect", {x: 3, y: 6.5, width: 12.5, height: 11, rx: 2}], P("m15.5 10.5 5-3v9l-5-3")],
    refresh: [P("M20 11a8 8 0 0 0-14.3-4.9L4 8M4 4v4h4M4 13a8 8 0 0 0 14.3 4.9L20 16M20 20v-4h-4")],
    check: [P("M5 12.5 10 17.5 19 7")],
    "check-circle": [["circle", {cx: 12, cy: 12, r: 8.5}], P("m8.5 12.2 2.4 2.4 4.6-4.8")],
    circle: [["circle", {cx: 12, cy: 12, r: 7.5}]],
    alert: [P("M12 4 2.8 19.5h18.4z"), P("M12 10v4M12 17h.01")],
    info: [["circle", {cx: 12, cy: 12, r: 8.5}], P("M12 11v5M12 8h.01")],
    stop: [["rect", {x: 6.5, y: 6.5, width: 11, height: 11, rx: 1.5}]],
    "chev-right": [P("m9.5 6 6 6-6 6")],
    "chev-down": [P("m6 9.5 6 6 6-6")],
    pin: [P("M12 16.5V21M8.5 3.5h7l-1 6 3.5 3.5h-12L9.5 9.5z")],
    trash: [P("M4 6.5h16M9 6.5V4h6v2.5M18.5 6.5l-.9 13.5H6.4L5.5 6.5M10 10.5v6M14 10.5v6")],
    download: [P("M12 4v11M7 10.5l5 5 5-5M4.5 19.5h15")],
    external: [P("M14 4h6v6M20 4l-9 9M18 14v5a1.5 1.5 0 0 1-1.5 1.5h-11A1.5 1.5 0 0 1 4 19V7.5A1.5 1.5 0 0 1 5.5 6H10")],
    sound: [P("M11 5 6.5 9H3v6h3.5L11 19z"), P("M15.5 9a4.5 4.5 0 0 1 0 6M18.5 6a8.5 8.5 0 0 1 0 12")],
    mute: [P("M11 5 6.5 9H3v6h3.5L11 19z"), P("m16 9.5 5 5M21 9.5l-5 5")],
    spark: [P("M12 3v4M12 17v4M3 12h4M17 12h4")],
    user: [["circle", {cx: 12, cy: 8.5, r: 4}], P("M4.5 20.5a7.5 7.5 0 0 1 15 0")],
    clip: [P("M20 11.5 12.2 19.3a5 5 0 0 1-7.1-7.1l8.1-8.1a3.3 3.3 0 0 1 4.7 4.7l-8.1 8.1a1.7 1.7 0 0 1-2.4-2.4l7.4-7.4")],
    image: [["rect", {x: 3.5, y: 4.5, width: 17, height: 15, rx: 2}], ["circle", {cx: 9, cy: 10, r: 1.8}], P("m4 18 5.5-5.5 4 4 2.5-2.5 4.5 4.5")],
  };
  const SVGNS = "http://www.w3.org/2000/svg";
  function icon(name, cls) {
    const svg = document.createElementNS(SVGNS, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("focusable", "false");
    svg.setAttribute("class", "ic" + (cls ? " " + cls : ""));
    for (const [tag, attrs] of ICONS[name] || ICONS.circle) {
      const e = document.createElementNS(SVGNS, tag);
      for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, String(v));
      svg.appendChild(e);
    }
    return svg;
  }
  function hydrate(root = document) {
    for (const el of root.querySelectorAll("[data-icon]")) {
      if (el.querySelector(":scope > svg.ic")) continue;
      el.prepend(icon(el.dataset.icon));
    }
  }

  // ---- safe element builder: h("div", {class: "x", onclick: fn}, "text", child)
  function h(tag, props, ...kids) {
    const el = document.createElement(tag);
    if (props) {
      for (const [k, v] of Object.entries(props)) {
        if (v == null || v === false) continue;
        if (k === "class") el.className = v;
        else if (k === "text") el.textContent = v;
        else if (k === "dataset") Object.assign(el.dataset, v);
        else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
        else if (k in el && typeof v !== "string") el[k] = v;
        else el.setAttribute(k, v === true ? "" : String(v));
      }
    }
    append(el, kids);
    return el;
  }
  function append(el, kids) {
    for (const k of kids) {
      if (k == null || k === false) continue;
      if (Array.isArray(k)) append(el, k);
      else el.append(k instanceof Node ? k : document.createTextNode(String(k)));
    }
    return el;
  }
  const btn = (label, fn, cls = "", ic = "") => h("button", {type: "button", class: "btn " + cls, onclick: fn}, ic ? icon(ic) : null, label);

  // Links in AI answers: http(s) only, opened outside the dashboard, never as HTML.
  function linkify(text) {
    const out = document.createDocumentFragment();
    const re = /\bhttps?:\/\/[^\s<>"'()]+[^\s<>"'().,;:!?]/g;
    let last = 0, m;
    while ((m = re.exec(text))) {
      if (m.index > last) out.append(text.slice(last, m.index));
      out.append(h("a", {href: m[0], target: "_blank", rel: "noopener noreferrer", text: m[0]}));
      last = m.index + m[0].length;
    }
    if (last < text.length) out.append(text.slice(last));
    return out;
  }

  // ---- formatting
  const k = n => n >= 1e6 ? (n / 1e6).toFixed(1) + "M" : n >= 1000 ? (n / 1000).toFixed(1) + "k" : String(n || 0);
  const dur = s => s < 60 ? s.toFixed(1) + " s" : Math.floor(s / 60) + " min " + Math.round(s % 60) + " s";
  const when = ts => ts ? new Date(ts * 1000).toLocaleString([], {month: "short", day: "numeric", hour: "2-digit", minute: "2-digit"}) : "";
  const bytes = n => n < 1024 ? n + " B" : n < 1048576 ? (n / 1024).toFixed(0) + " KB" : (n / 1048576).toFixed(1) + " MB";
  const words = s => (s || "").replace(/_/g, " ");
  function plain(e) {
    const m = (e && e.message) || String(e || "");
    if (m === "unauthorized") return "Delta did not accept this page's access key. Open Delta again from its shortcut.";
    if (/fetch|network/i.test(m)) return "Delta is not reachable. It may be starting up or shut down.";
    return m || "Something went wrong.";
  }
  const detail = r => (r && (typeof r.detail === "string" ? r.detail : r.message)) || "";

  // ---- states: empty, loading, error
  function empty({ic = "info", title, body, action} = {}) {
    return h("div", {class: "empty"}, h("span", {class: "empty-ic"}, icon(ic)), title ? h("b", {text: title}) : null,
      body ? h("p", {text: body}) : null, action ? btn(action.label, action.fn, "primary") : null);
  }
  function skeleton(rows = 3) {
    return h("div", {class: "skeleton-list", "aria-busy": "true", "aria-label": "Loading"},
      Array.from({length: rows}, () => h("div", {class: "skeleton-row"}, h("span", {class: "sk sk-a"}), h("span", {class: "sk sk-b"}))));
  }
  function failed(message, retry) {
    return h("div", {class: "error-state", role: "alert"}, icon("alert"), h("span", {text: message}), retry ? btn("Try again", retry, "sm") : null);
  }
  // A long list, shown a page at a time so a slow laptop never builds thousands of rows at once.
  function paged(box, items, render, size = 40) {
    let shown = 0;
    const more = h("button", {type: "button", class: "btn more"});
    const page = () => {
      more.remove();
      const frag = document.createDocumentFragment();
      for (const it of items.slice(shown, shown + size)) frag.append(render(it));
      shown += size; box.append(frag);
      if (shown < items.length) { more.textContent = `Show ${Math.min(size, items.length - shown)} more of ${items.length - shown}`; box.append(more); }
    };
    more.onclick = page;
    page();
  }

  // ---- toasts
  const KIND_IC = {info: "info", ok: "check-circle", warn: "alert", danger: "alert", approval: "bell"};
  function toast({title, body = "", kind = "info", action = null, timeout = 6000}) {
    const box = $("toasts");
    while (box.children.length >= 4) box.firstChild.remove();
    let timer = 0;
    const close = () => { clearTimeout(timer); t.classList.add("leaving"); setTimeout(() => t.remove(), 160); };
    const t = h("div", {class: "toast t-" + kind, role: kind === "danger" ? "alert" : "status"},
      h("span", {class: "toast-ic"}, icon(KIND_IC[kind] || "info")),
      h("div", {class: "toast-text"}, h("b", {text: title}), body ? h("span", {text: body}) : null),
      action ? h("button", {type: "button", class: "btn sm primary", onclick: () => { close(); action.fn(); }}, action.label) : null,
      h("button", {type: "button", class: "icon-btn sm", "aria-label": "Dismiss", onclick: close}, icon("x")));
    box.append(t);
    if (timeout) {
      const arm = () => { timer = setTimeout(close, timeout); };
      t.addEventListener("mouseenter", () => clearTimeout(timer));
      t.addEventListener("mouseleave", arm);
      arm();
    }
    return close;
  }

  // ---- dialogs: confirm and the side sheet
  function confirmBox({title, body = "", ok = "OK", danger = false}) {
    return new Promise(resolve => {
      const d = $("confirm");
      $("cf_title").textContent = title; $("cf_body").textContent = body;
      $("cf_yes").textContent = ok; $("cf_yes").className = "btn " + (danger ? "danger" : "primary");
      d.returnValue = "";
      d.addEventListener("close", () => resolve(d.returnValue === "yes"), {once: true});
      d.showModal();
      (danger ? $("cf_no") : $("cf_yes")).focus();
    });
  }
  let sheetURLs = [], sheetOnClose = null;
  function sheet(title, build, onClose) {
    const d = $("sheet");
    closeSheetCleanup();
    $("sheet_title").textContent = title;
    $("sheet_body").replaceChildren();
    sheetOnClose = onClose || null;
    build($("sheet_body"));
    if (!d.open) d.showModal();
    return d;
  }
  function closeSheetCleanup() {
    for (const u of sheetURLs) URL.revokeObjectURL(u);
    sheetURLs = [];
    if (sheetOnClose) { const f = sheetOnClose; sheetOnClose = null; f(); }
  }
  const sheetURL = b => { const u = URL.createObjectURL(b); sheetURLs.push(u); return u; };

  // ---- views
  const views = {};
  const TITLES = {chat: "Chat", tasks: "Tasks", apps: "Apps", schedules: "Schedules", documents: "Documents", memory: "Memory", settings: "Settings"};
  let view = "";
  function go(name, opts) {
    if (!TITLES[name]) name = "chat";
    const prev = view;
    view = name;
    for (const s of document.querySelectorAll(".views > .view")) s.hidden = s.dataset.view !== name;
    for (const n of document.querySelectorAll(".nav-item[data-go]")) {
      if (n.dataset.go === name) n.setAttribute("aria-current", "page"); else n.removeAttribute("aria-current");
    }
    document.body.dataset.view = name;
    if (name !== "chat") setTitle(TITLES[name]);
    try { sessionStorage.setItem("delta_view", name); } catch {}
    if (prev && prev !== name && views[prev] && views[prev].hide) views[prev].hide();
    if (views[name] && views[name].show) views[name].show(opts || {});
    document.body.classList.remove("sb-open"); $("scrim").hidden = true;
    emit("view", name);
  }
  function setTitle(t) { $("pagetitle").textContent = t; }

  // ---- polling that pauses while the window is hidden
  function every(ms, fn, when = () => true) {
    let timer = 0, live = true;
    const tick = async () => {
      if (!live || closing) return;  // page went off-screen (update or shutdown): stop background work
      if (!document.hidden && when()) { try { await fn(); } catch { /* next tick tries again */ } }
      if (live) timer = setTimeout(tick, ms);
    };
    timer = setTimeout(tick, ms);
    return () => { live = false; clearTimeout(timer); };
  }
  document.addEventListener("visibilitychange", () => { if (!document.hidden) emit("visible"); });

  // ---- the live connection: one WebSocket, reconnects with backoff
  let ws = null, tries = 0, retryTimer = 0, closing = false, conn = "connecting";
  function setConn(s) { conn = s; emit("conn", s); }
  function connect() {
    if (closing) return;
    clearTimeout(retryTimer);
    setConn(tries ? "reconnecting" : "connecting");
    try { ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/ws?token=${encodeURIComponent(token)}`); }
    catch { retry(); return; }
    ws.onopen = () => { const again = tries > 0; tries = 0; setConn("open"); if (again) emit("reconnected"); };
    ws.onmessage = ev => { let m; try { m = JSON.parse(ev.data); } catch { return; } emit("ws", m); };
    ws.onclose = () => { ws = null; if (!closing) retry(); };
  }
  function retry() {
    setConn("reconnecting");
    const delay = Math.min(30000, 1000 * 2 ** Math.min(tries, 5)) * (0.8 + Math.random() * 0.4);
    tries++;
    retryTimer = setTimeout(connect, delay);
  }
  on("visible", () => { if (!ws && !closing) { tries = Math.max(tries, 1); connect(); } });
  function disconnect() { closing = true; clearTimeout(retryTimer); if (ws) ws.close(); setConn("off"); }

  // ---- preferences kept in this browser
  const pref = {
    get(key, dflt = "") { try { const v = localStorage.getItem(key); return v == null ? dflt : v; } catch { return dflt; } },
    set(key, v) { try { if (v == null) localStorage.removeItem(key); else localStorage.setItem(key, v); } catch {} },
  };

  window.Delta = {token, $, api, blob, on, emit, icon, hydrate, h, btn, linkify, k, dur, when, bytes, words, plain, detail,
    empty, skeleton, failed, paged, toast, confirm: confirmBox, sheet, sheetURL, views, go, setTitle, every, connect, disconnect,
    pref, get view() { return view; }, get conn() { return conn; }};

  $("sheet").addEventListener("close", closeSheetCleanup);
  $("sheet").addEventListener("click", e => { if (e.target === $("sheet")) $("sheet").close(); });
  $("sheet_close").addEventListener("click", () => $("sheet").close());
})();
