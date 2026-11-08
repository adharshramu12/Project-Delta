// Apps view. AI models: the main AI, AI apps you already have (Claude Code, Codex, LM Studio) and Google Calendar.
// Connected apps: Notion, GitHub, Slack, Gmail and other apps that publish an MCP server (delta.connectors): see
// each app's tools with their risk, switch tools on or off, and add a custom server after confirming exactly what
// will run. Everything an app's server says (tool names, descriptions) is shown with textContent, never as HTML.
(() => {
  "use strict";
  const D = window.Delta, {$, api, h, icon, btn} = D;

  // ---- AI models
  async function renderMain() {
    const p = await api("/api/apps/preferred").catch(() => null); if (!p) return;
    const sel = $("s_main");
    sel.replaceChildren(h("option", {value: "auto", text: "Automatic (local first, then your plans, then free cloud)"}),
      ...p.options.map(o => h("option", {value: o.name, text: o.label + (o.ready ? "" : " (not ready)")})));
    sel.value = p.preferred;
    $("s_fallback").checked = !!p.fallback;
  }
  $("s_fallback").onchange = async () => {
    const on = $("s_fallback").checked;
    const r = await api("/api/apps/preferred", {method: "PUT", body: JSON.stringify({name: $("s_main").value, fallback: on})}).catch(e => ({detail: D.plain(e)}));
    if (r && r.detail) { $("s_fallback").checked = !on; D.toast({title: "Not changed", body: D.detail(r), kind: "danger"}); }
    else D.toast({title: on ? "Fallback on" : "Fallback off", body: on ? "Your other AIs answer when the main AI fails." : "Only your main AI answers.", kind: "ok"});
  };
  $("s_main").onchange = async () => {
    const r = await api("/api/apps/preferred", {method: "PUT", body: JSON.stringify({name: $("s_main").value})}).catch(e => ({detail: D.plain(e)}));
    if (r && r.detail) D.toast({title: "Main AI not changed", body: D.detail(r), kind: "danger"});
    else D.toast({title: "Main AI changed", body: $("s_main").selectedOptions[0].textContent, kind: "ok"});
    D.emit("status:refresh");
  };
  const JOBS = [["plan", "Planning", "once per task"], ["chat", "Doing steps", "every step: most tokens"], ["verify", "Checking", "once at the end"]];
  const PRESETS = {claude_code: {
    "Save tokens": {models: {plan: "sonnet", chat: "haiku", verify: "haiku"}, efforts: {plan: "low", chat: "low", verify: "low"}},
    "Balanced": {models: {plan: "sonnet", chat: "sonnet", verify: "haiku"}, efforts: {plan: "low", chat: "low", verify: "low"}},
    "Best quality": {models: {plan: "opus", chat: "sonnet", verify: "haiku"}, efforts: {plan: "high", chat: "medium", verify: "low"}},
  }};
  function modelPicker(a, msg, rerender) {
    const grid = h("div", {class: "jobs"});
    const pick = {};
    for (const [job, label, hint] of JOBS) {
      const m = h("select", {"aria-label": label + " model"}), e = h("select", {"aria-label": label + " effort"});
      const ids = a.options.map(o => o.id);
      if (!a.options.length || !ids.includes(a.models[job])) m.append(h("option", {value: a.models[job] || "", text: a.models[job] || "app default"}));
      for (const o of a.options) m.append(h("option", {value: o.id, text: o.label}));
      m.value = a.models[job] || "";
      const fill = () => {
        const levels = (a.options.find(o => o.id === m.value) || {efforts: ["low", "medium", "high"]}).efforts;
        const keep = e.value || a.efforts[job];
        e.replaceChildren(...["", ...levels].map(v => h("option", {value: v, text: v ? "effort: " + v : "effort: default"})));
        e.value = levels.includes(keep) ? keep : "";
      };
      m.onchange = fill; e.value = a.efforts[job] || ""; fill();
      pick[job] = [m, e];
      grid.append(h("span", {class: "job"}, h("b", {text: label}), h("small", {class: "muted", text: hint})), m, e);
    }
    const save = async body => {
      const r = await api(`/api/apps/${encodeURIComponent(a.name)}/models`, {method: "PUT", body: JSON.stringify(body)}).catch(e => ({detail: D.plain(e)}));
      msg.textContent = r.ok ? "Saved. Used from the next message." : (D.detail(r) || "Could not save."); if (r.ok) rerender();
    };
    const row = h("div", {class: "row"});
    for (const [name, body] of Object.entries(PRESETS[a.name] || {})) row.append(btn(name, () => save(body), "sm"));
    row.append(btn("Save models", () => save({models: Object.fromEntries(JOBS.map(([j]) => [j, pick[j][0].value])),
      efforts: Object.fromEntries(JOBS.map(([j]) => [j, pick[j][1].value]))}), "primary sm"));
    return h("details", {class: "models"}, h("summary", {text: "Models for each job"}), grid, row);
  }
  const state = (ok, text) => h("span", {class: "pill " + (ok === true ? "ok" : ok === false ? "warn" : ""), text});
  // Google: official Calendar API (exact, instant, no tokens) instead of driving its web page.
  async function googleCard(box) {
    const g = await api("/api/google").catch(() => null); if (!g) return;
    const msg = h("div", {class: "msg", role: "status"}), row = h("div", {class: "row"});
    const act = async (path, opts) => {
      msg.textContent = "Working. Finish the sign-in in the browser tab if one opens.";
      const r = await api(path, opts).catch(e => ({message: D.plain(e)}));
      msg.textContent = r.message || (r.ok ? "Saved." : (r.detail ? "Please check the client ID." : "")); if (r.ok) renderApps(box);
    };
    let st;
    if (g.connected) { st = state(true, "Connected"); row.append(btn("Disconnect", () => act("/api/google/disconnect", {method: "POST"}), "sm")); }
    else if (g.client) { st = state(false, "Not connected"); row.append(btn("Connect Google", () => act("/api/google/connect", {method: "POST"}), "primary sm")); }
    else {
      st = state(false, "Needs a Google client ID");
      const id = h("input", {placeholder: "xxxx.apps.googleusercontent.com", "aria-label": "Google client ID"});
      const secret = h("input", {type: "password", placeholder: "Client secret", "aria-label": "Google client secret"});
      row.append(id, secret, btn("Save", () => act("/api/google/client", {method: "PUT", body: JSON.stringify({client_id: id.value.trim(), client_secret: secret.value.trim()})}), "primary sm"));
      row.classList.add("inline-form");
    }
    box.append(h("div", {class: "card"}, h("div", {class: "card-top"}, h("b", {text: "Google Calendar"}), st),
      h("small", {class: "muted", text: g.connected ? "Meetings use the official Calendar API: exact, instant, no tokens." : "Connect once so meetings use the official Calendar API instead of the web page."}), row, msg));
  }
  async function renderApps(box = $("s_appslist")) {
    if (box.id === "s_appslist") renderMain();
    if (!box.children.length) box.append(D.skeleton(2));
    const list = await api("/api/apps").catch(() => []);
    const frag = document.createDocumentFragment();
    for (const a of list) {
      const msg = h("div", {class: "msg", role: "status"}), row = h("div", {class: "row"});
      const button = (label, fn, primary) => {
        const b = btn(label, async () => {
          b.disabled = true; msg.textContent = "Working...";
          try { const r = await fn(); msg.textContent = r.message || ""; if (r.ok && r.refresh) renderApps(box); }
          catch (e) { msg.textContent = D.plain(e); }
          b.disabled = false;
        }, primary ? "primary sm" : "sm");
        row.append(b);
      };
      const post = path => api(path, {method: "POST"});
      let st, help = "", picker = null;
      if (a.kind === "cli") {
        st = !a.installed ? state(null, "Not installed") : a.signed_in ? state(true, "Signed in") : state(false, "Installed, not signed in");
        if (a.installed) {
          if (!a.signed_in) button("Sign in", () => post(`/api/apps/${encodeURIComponent(a.name)}/login`), true);
          button("Check", async () => ({ok: true, refresh: true, message: ""}));
          if (a.signed_in) button("Send test message", () => post(`/api/apps/${encodeURIComponent(a.name)}/test`));
          if (a.signed_in) picker = modelPicker(a, msg, () => renderApps(box));
          help = a.signed_in ? "Delta can use your plan. Each reply says when it answered." : "";
        } else help = a.name === "claude_code" ? "Install Claude Code, then come back here." : "Install the Codex app or CLI, then come back here.";
      } else {
        st = a.running ? state(true, "Server running") : state(a.installed ? false : null, a.installed ? "Installed, server off" : "Not installed");
        help = a.running ? `Running at ${a.url}` : "";
        if (a.installed && !a.running) button("Start server", async () => ({...await post(`/api/apps/${encodeURIComponent(a.name)}/start`), refresh: true}), true);
        if (a.running) button("Send test message", () => post(`/api/apps/${encodeURIComponent(a.name)}/test`));
        if (a.running) button("Stop server", async () => ({...await post(`/api/apps/${encodeURIComponent(a.name)}/stop`), refresh: true}));
        button("Check", async () => ({ok: true, refresh: true, message: ""}));
      }
      frag.append(h("div", {class: "card"}, h("div", {class: "card-top"}, h("b", {text: a.label}), st), help ? h("small", {class: "muted", text: help}) : null, picker, row, msg));
    }
    box.replaceChildren(frag);
    if (!list.length && box.id !== "w_apps") box.append(h("p", {class: "muted small", text: "No AI apps found on this PC. A free Groq key works well."}));
    await googleCard(box);
  }

  // ---- connected apps (MCP)
  const mbox = $("mcpapps");
  const call = (path, body) => api(path, body === undefined ? {} : {method: "POST", body: JSON.stringify(body)}).catch(e => ({status: "error", message: D.plain(e)}));
  const TAGS = {"official": "Official", "official-preview": "Official preview", "reference": "MCP reference", "community": "Community", "custom": "Custom"};
  const banner = h("div", {class: "banner warn", role: "status", hidden: true});
  const grid = h("div", {class: "cards grid"});
  const customBox = h("div", {class: "card"});
  // Advanced: MCP is how apps talk to Delta; it turns on by itself. Only custom servers live here.
  const legacy = h("div", {class: "stack"});
  mbox.append(banner, grid, h("details", {class: "card"}, h("summary", {text: "Advanced: custom MCP servers"}), legacy, h("h3", {class: "list-title", text: "Add another MCP server"}), customBox));
  const open = {};  // app id -> "connect" | "tools" (which panel is open on its card)
  let loaded = false, lastSig = "";

  async function refresh() {
    if (!loaded) grid.replaceChildren(D.skeleton(3));
    const data = await call("/api/connectors");
    if (!data || !Array.isArray(data.apps)) { grid.replaceChildren(D.failed((data && (D.detail(data) || data.message)) || "Apps are not available.", refresh)); return; }
    loaded = true;
    banner.hidden = data.secrets_ok;
    banner.textContent = data.secrets_ok ? "" : "Windows DPAPI is not working on this PC, so apps that need a sign-in cannot be connected (Delta never stores tokens unencrypted). Repair Delta by running the installer again.";
    if (document.activeElement && mbox.contains(document.activeElement) && /INPUT|SELECT|TEXTAREA/.test(document.activeElement.tagName)) return;  // never redraw under the user's typing
    const sig = JSON.stringify(data.apps);
    if (sig === lastSig || Object.values(open).includes("tools")) return;  // unchanged, or the user is reading a tool list
    lastSig = sig;
    grid.replaceChildren(...[...data.apps].sort((a, b) => Number(b.connected) - Number(a.connected)).map(card));
    servers();
  }
  async function servers() {
    const m = await call("/api/mcp-servers");
    if (!m || !Array.isArray(m.servers) || !m.servers.length) { legacy.replaceChildren(); return; }
    const sw = h("input", {type: "checkbox", checked: m.enabled});
    sw.onchange = async () => { await call("/api/mcp-servers", {enabled: sw.checked}); D.toast({title: "Saved", body: "Restart Delta for this to take effect.", kind: "ok"}); };
    legacy.replaceChildren(h("label", {class: "check"}, sw, " Run the servers from my settings file"), ...m.servers.map(x => h("p", {class: "small muted", text: `${x.name}: ${x.runs}`})));
  }
  function card(a) {
    const st = a.state === "connected" ? state(true, "Connected") : a.state === "needs reconnect" ? state(false, "Needs reconnect") : state(null, "Not connected");
    const row = h("div", {class: "row"});
    const b = (label, fn, cls = "sm") => { const x = btn(label, fn, cls); row.append(x); return x; };
    const msg = h("div", {class: "msg", role: "status"});
    const panel = h("div", {class: "app-panel"});
    if (!a.connected || a.state === "needs reconnect") b(a.state === "needs reconnect" ? "Reconnect" : "Connect", () => { open[a.id] = open[a.id] === "connect" ? "" : "connect"; draw(); }, "primary sm");
    if (a.connected) {
      b("Tools", () => { open[a.id] = open[a.id] === "tools" ? "" : "tools"; draw(); });
      b("Check", async () => { msg.textContent = "Checking..."; const r = await call(`/api/connectors/${encodeURIComponent(a.id)}/health`, {}); msg.className = "msg" + (r.ok ? "" : " err"); msg.textContent = r.message || ""; });
      b("Disconnect", async () => {
        if (!await D.confirm({title: `Disconnect ${a.name}?`, body: "Its sign-in is revoked at the app (when it allows that) and deleted from this PC.", ok: "Disconnect", danger: true})) return;
        const r = await call(`/api/connectors/${encodeURIComponent(a.id)}/disconnect`, {}); msg.textContent = r.message || ""; refresh();
      });
    }
    if (a.docs) row.append(h("a", {class: "link sm", href: a.docs, target: "_blank", rel: "noopener noreferrer"}, "Docs", icon("external")));
    const tags = [TAGS[a.status] || a.status, a.transport === "stdio" ? "runs on this PC" : "", a.connected ? `${a.tools} tools` : ""].filter(Boolean).join(" · ");
    const el = h("div", {class: "card app" + (a.connected ? " connected" : "")},
      h("div", {class: "card-top"}, h("span", {class: "app-ic", "aria-hidden": "true", text: (a.name || "?").slice(0, 1).toUpperCase()}), h("b", {text: a.name}), st),
      h("p", {class: "small", text: a.description}), h("small", {class: "muted", text: tags}),
      a.status === "community" ? h("small", {class: "muted", text: "Not published by the app's vendor."}) : null,
      a.last_error && a.state !== "connected" ? h("small", {class: "err", text: a.last_error}) : null, row, msg, panel);
    function draw() { el.classList.toggle("wide", open[a.id] === "tools"); panel.replaceChildren(); if (open[a.id] === "connect") panel.append(connectForm(a, msg)); if (open[a.id] === "tools") toolsPanel(a, panel); }
    draw();
    return el;
  }
  function input(label, type, name) {
    const i = h("input", {type, name, autocomplete: "off"});
    return [h("label", {class: "field"}, label, i), i];
  }
  function connectForm(a, msg) {
    const f = h("form", {class: "stack"});
    const fields = {};
    for (const it of a.inputs || []) { const [w, i] = input(it.label || it.name, "text", "in_" + it.name); fields["in_" + it.name] = i; f.append(w); }
    for (const s of a.secrets || []) { const [w, i] = input((s.label || s.env) + " (hidden)", "password", "sec_" + s.env); fields["sec_" + s.env] = i; f.append(w); }
    if (a.auth === "token") {
      if (a.auth_help) f.append(h("a", {class: "link sm", href: a.auth_help, target: "_blank", rel: "noopener noreferrer"}, "Create a token", icon("external")));
      const [w, i] = input(a.auth_label || "Token", "password", "token"); fields.token = i; f.append(w);
    }
    if (a.auth === "oauth" && a.client === "user") {
      if (a.auth_label) f.append(h("p", {class: "small", text: a.auth_label}));
      f.append(h("pre", {class: "code", text: "Redirect URL for your OAuth app:\nhttp://127.0.0.1:" + (a.redirect_port || 8765) + "/callback"}));
      const [w1, i1] = input("Client id", "text", "client_id"); fields.client_id = i1; f.append(w1);
      if (a.client_secret) { const [w2, i2] = input("Client secret (hidden)", "password", "client_secret"); fields.client_secret = i2; f.append(w2); }
    }
    if (a.auth === "oauth" && a.client !== "user") f.append(h("p", {class: "small muted", text: `A ${a.name} sign-in page opens in your browser. Delta keeps the sign-in encrypted on this PC.`}));
    const go = h("button", {type: "submit", class: "btn primary sm", text: a.auth === "oauth" ? "Sign in" : "Connect"});
    f.append(h("div", {class: "row"}, go));
    f.onsubmit = async e => {
      e.preventDefault(); go.disabled = true; msg.className = "msg"; msg.textContent = "Connecting...";
      const body = {inputs: {}, secrets: {}};
      for (const [key, i] of Object.entries(fields)) {
        if (key.startsWith("in_")) body.inputs[key.slice(3)] = i.value.trim();
        else if (key.startsWith("sec_")) body.secrets[key.slice(4)] = i.value.trim();
        else body[key] = i.value.trim();
      }
      const r = await call(`/api/connectors/${encodeURIComponent(a.id)}/connect`, body);
      for (const i of Object.values(fields)) if (i.type === "password") i.value = "";
      go.disabled = false;
      msg.className = "msg" + (r.status === "error" ? " err" : "");
      msg.textContent = r.message || D.detail(r) || "";
      if (r.status === "sign_in" && r.url) msg.append(h("br"), h("a", {href: r.url, target: "_blank", rel: "noopener noreferrer", text: "Did not see it? Open the sign-in page"}));
      if (r.status === "connected") { open[a.id] = ""; refresh(); }
    };
    return f;
  }
  async function toolsPanel(a, panel) {
    panel.replaceChildren(D.skeleton(2));
    const data = await call(`/api/connectors/${encodeURIComponent(a.id)}/tools`);
    panel.replaceChildren();
    if (!data || !Array.isArray(data.tools)) { panel.append(h("div", {class: "msg err", text: (data && (D.detail(data) || data.message)) || "Could not load the tools."})); return; }
    const cb = h("input", {type: "checkbox", checked: !!data.trust_read_only_hints});
    cb.onchange = async () => { await call(`/api/connectors/${encodeURIComponent(a.id)}/trust`, {trust: cb.checked}); toolsPanel(a, panel); };
    panel.append(h("label", {class: "check small"}, cb, ` Trust ${a.name}'s own "read-only" labels (tools it marks read-only then run without asking)`));
    if (data.truncated) panel.append(h("p", {class: "small muted", text: "This app lists more tools than Delta shows; only the first 300 are available."}));
    const list = h("div", {class: "tools"});
    D.paged(list, data.tools, t => {
      const on = h("input", {type: "checkbox", checked: t.enabled, "aria-label": "Use " + t.name});
      on.onchange = () => call(`/api/connectors/${encodeURIComponent(a.id)}/tools/${encodeURIComponent(t.name)}`, {enabled: on.checked});
      const sel = h("select", {"aria-label": "When to ask for " + t.name});
      for (const [v, label] of [["", "default (" + t.risk + ")"], ["read", "read: never asks"], ["external", "asks first"], ["destructive", "destructive: always asks"]])
        sel.append(h("option", {value: v, text: label, selected: (t.user_risk || "") === v}));
      sel.onchange = async () => { await call(`/api/connectors/${encodeURIComponent(a.id)}/tools/${encodeURIComponent(t.name)}`, {risk: sel.value}); toolsPanel(a, panel); };
      return h("div", {class: "tool"}, on, h("div", {}, h("code", {text: t.name}), h("small", {class: "muted block", text: t.description})), sel);
    }, 60);
    panel.append(list);
  }

  // Custom server: preview exactly what will run or be reached, then confirm.
  const [w1, idIn] = input("Short id (lowercase, for example mytools)", "text", "id");
  const [w2, urlIn] = input("Server URL (https://...), or leave empty", "text", "url");
  const [w3, cmdIn] = input("Or a command that starts a local server (for example npx -y some-mcp-server@1.2.3)", "text", "command");
  const oauthCb = h("input", {type: "checkbox"});
  const preview = h("div", {class: "msg", role: "status"});
  const cf = h("form", {class: "stack"}, w1, w2, w3, h("label", {class: "check small"}, oauthCb, " This server signs in with OAuth"),
    h("div", {class: "row"}, h("button", {type: "submit", class: "btn sm", text: "Preview"})), preview);
  customBox.append(h("p", {class: "small muted", text: "Only add servers you trust: a local server is a program that runs on your PC, and a remote one receives what your tasks send it."}), cf);
  cf.onsubmit = async e => {
    e.preventDefault();
    const body = {id: idIn.value.trim(), name: idIn.value.trim(), url: urlIn.value.trim(), command: cmdIn.value.trim(), auth: oauthCb.checked ? "oauth" : "none", confirm: false};
    const r = await call("/api/connectors/custom", body);
    preview.replaceChildren();
    if (r.status !== "confirm") { preview.className = "msg" + (r.status === "error" || r.detail ? " err" : ""); preview.textContent = r.message || (typeof r.detail === "string" ? r.detail : JSON.stringify(r.detail || r)); refresh(); return; }
    preview.className = "msg";
    const yes = btn("Yes, add and start it", async () => {
      yes.disabled = true;
      const r2 = await call("/api/connectors/custom", {...body, confirm: true});
      preview.textContent = r2.message || "";
      if (r2.status === "sign_in" && r2.url) preview.append(" ", h("a", {href: r2.url, target: "_blank", rel: "noopener noreferrer", text: "Open the sign-in page"}));
      refresh();
    }, "primary sm");
    preview.append(h("p", {class: "small", text: "This is exactly what Delta will run or reach:"}), h("pre", {class: "code", text: r.message}), h("div", {class: "row"}, yes, btn("Cancel", () => preview.replaceChildren(), "sm")));
  };

  $("apps_keys").onclick = () => D.openSetup && D.openSetup("models");
  D.renderAIApps = renderApps;
  let stopPoll = null;
  D.views.apps = {
    show() { renderApps(); refresh(); if (!stopPoll) stopPoll = D.every(3000, refresh, () => D.view === "apps"); },
    hide() { if (stopPoll) { stopPoll(); stopPoll = null; } },
  };
})();
