// Apps page: connect Notion, GitHub, Slack, Gmail and other apps that publish an MCP server (delta.connectors),
// see each app's tools with their risk, switch tools on or off, and add a custom server after confirming exactly
// what will run. Loaded after the main script, so it uses its `api` helper. Everything an app's server says
// (tool names, descriptions) is shown with textContent, never as HTML.
(() => {
  if (typeof api !== "function") return;
  const style = document.createElement("style");
  style.textContent = `
#mcpapps .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:10px;margin-top:12px}
#mcpapps .app{margin:0}
#mcpapps .app p{margin:0;font-size:13px;color:var(--muted);line-height:1.45}
#mcpapps .st{font-size:11px;font-weight:600;padding:2px 8px;border-radius:var(--r-pill);background:var(--surface-2);color:var(--muted)}
#mcpapps .st.connected{color:var(--ok);background:var(--ok-soft)}
#mcpapps .st.needs{color:var(--warn);background:var(--warn-soft)}
#mcpapps form{display:flex;flex-direction:column;gap:6px}
#mcpapps input[type=text],#mcpapps input[type=password]{padding:7px 10px;border:1px solid var(--line-strong);border-radius:8px;background:var(--paper);color:var(--ink);font:13px var(--font)}
#mcpapps .msg{font-size:12.5px;white-space:pre-wrap}
#mcpapps .msg.err{color:var(--bad)}
#mcpapps .tools{display:flex;flex-direction:column;gap:6px;max-height:340px;overflow:auto;margin-top:6px}
#mcpapps .tool{display:grid;grid-template-columns:auto 1fr auto;gap:8px;align-items:start;font-size:12.5px;border-top:1px solid var(--line);padding-top:6px}
#mcpapps .tool small{display:block;color:var(--muted)}
#mcpapps select{padding:3px 6px;font-size:12px;border-radius:6px;border:1px solid var(--line-strong);background:var(--paper);color:var(--ink)}
#mcpapps pre{white-space:pre-wrap;font:12px/1.4 var(--mono);background:var(--surface-2);padding:8px;border-radius:8px;margin:0}`;
  document.head.appendChild(style);

  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const call = (path, body) => api(path, body === undefined ? {} : {method: "POST", body: JSON.stringify(body)}).catch(e => ({status: "error", message: e.message}));
  const TAGS = {"official": "official", "official-preview": "official preview", "reference": "MCP reference", "community": "community", "custom": "custom"};

  const overlay = el("div", "overlay"); overlay.id = "mcpapps"; overlay.hidden = true;
  const wiz = el("div", "wiz");
  const head = el("div", "top"); head.style.cssText = "display:flex;justify-content:space-between;align-items:center";
  head.append(el("h2", "", "Apps"));
  const close = el("button", "", "Close"); close.type = "button"; close.onclick = () => { overlay.hidden = true; clearInterval(timer); };
  head.append(close);
  const lead = el("p", "lead", "Connect your apps once, then just ask: \"find my notes about the launch in Notion\", \"open a GitHub issue for this bug\". Sign-ins are encrypted on this PC with Windows DPAPI. Anything that changes or sends something asks you first.");
  const banner = el("div", "msg");
  const grid = el("div", "grid");
  const customBox = el("div", "app");
  wiz.append(head, lead, banner, grid, el("h3", "", "Add another MCP server"), customBox);
  overlay.append(wiz);
  document.body.append(overlay);

  const btn = document.createElement("button");
  btn.id = "openmcp"; btn.className = "iconbtn"; btn.title = "Apps: Notion, GitHub, Slack, Gmail and more"; btn.setAttribute("aria-label", "Apps");
  btn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M9 2v6M15 2v6M6 8h12v4a6 6 0 0 1-12 0z"/><path d="M12 18v4"/></svg>';
  const anchor = document.getElementById("openapps");
  if (anchor && anchor.parentNode) anchor.parentNode.insertBefore(btn, anchor); else document.body.append(btn);
  let timer = 0;
  btn.onclick = () => { overlay.hidden = false; refresh(); clearInterval(timer); timer = setInterval(refresh, 3000); };

  const open = {};  // app id -> "connect" | "tools" (which panel is open on its card)

  async function refresh() {
    if (overlay.hidden) { clearInterval(timer); return; }
    const data = await call("/api/connectors");
    if (!data || !Array.isArray(data.apps)) { banner.className = "msg err"; banner.textContent = (data && (data.detail || data.message)) || "Apps are not available."; return; }
    banner.className = "msg"; banner.textContent = data.secrets_ok ? "" : "Windows DPAPI is not working on this PC, so apps that need a sign-in cannot be connected (Delta never stores tokens unencrypted). Repair Delta by running the installer again.";
    const focused = document.activeElement && overlay.contains(document.activeElement) && document.activeElement.tagName === "INPUT";
    if (focused) return;  // do not redraw under the user's typing
    grid.innerHTML = "";
    for (const a of data.apps) grid.append(card(a));
  }

  function card(a) {
    const box = el("div", "app");
    const top = el("div", "top");
    const name = el("b", "", a.name);
    const state = el("span", "st " + (a.state === "connected" ? "connected" : a.state === "needs reconnect" ? "needs" : ""), a.state);
    top.append(name, state);
    box.append(top, el("p", "", a.description));
    box.append(el("small", "tag", (TAGS[a.status] || a.status) + (a.transport === "stdio" ? " · runs on this PC" : "") + (a.connected ? ` · ${a.tools} tools` : "")));
    if (a.status === "community") box.append(el("p", "", "Not published by the app's vendor."));
    if (a.last_error && a.state !== "connected") box.append(el("p", "", a.last_error));
    const row = el("div", "row");
    const b = (label, fn, primary) => { const x = el("button", primary ? "primary" : "", label); x.type = "button"; x.onclick = fn; row.append(x); return x; };
    const msg = el("div", "msg");
    const panel = el("div", "");
    if (!a.connected || a.state === "needs reconnect") b(a.state === "needs reconnect" ? "Reconnect" : "Connect", () => { open[a.id] = "connect"; draw(); }, true);
    if (a.connected) {
      b("Tools", () => { open[a.id] = open[a.id] === "tools" ? "" : "tools"; draw(); });
      b("Check", async () => { msg.textContent = "Checking..."; const r = await call(`/api/connectors/${a.id}/health`, {}); msg.className = "msg" + (r.ok ? "" : " err"); msg.textContent = r.message; });
      b("Disconnect", async () => { if (!confirm(`Disconnect ${a.name}? Its sign-in is revoked at the app (when it allows that) and deleted from this PC.`)) return; const r = await call(`/api/connectors/${a.id}/disconnect`, {}); msg.textContent = r.message; refresh(); });
    }
    if (a.docs) { const d = el("a", "", "Docs"); d.href = a.docs; d.target = "_blank"; d.rel = "noopener"; d.style.fontSize = "12.5px"; row.append(d); }
    box.append(row, msg, panel);
    function draw() { panel.innerHTML = ""; if (open[a.id] === "connect") panel.append(connectForm(a, msg)); if (open[a.id] === "tools") toolsPanel(a, panel); }
    draw();
    return box;
  }

  function input(label, type, name) {
    const wrap = el("label", "", label); wrap.style.cssText = "display:flex;flex-direction:column;gap:3px;font-size:12.5px";
    const i = document.createElement("input"); i.type = type; i.name = name; i.autocomplete = "off"; wrap.append(i); return [wrap, i];
  }

  function connectForm(a, msg) {
    const f = document.createElement("form");
    const fields = {};
    for (const it of a.inputs || []) { const [w, i] = input(it.label || it.name, "text", "in_" + it.name); fields["in_" + it.name] = i; f.append(w); }
    for (const s of a.secrets || []) { const [w, i] = input((s.label || s.env) + " (hidden)", "password", "sec_" + s.env); fields["sec_" + s.env] = i; f.append(w); }
    if (a.auth === "token") {
      if (a.auth_help) { const l = el("a", "", "Create a token"); l.href = a.auth_help; l.target = "_blank"; l.rel = "noopener"; f.append(l); }
      const [w, i] = input(a.auth_label || "Token", "password", "token"); fields.token = i; f.append(w);
    }
    if (a.auth === "oauth" && a.client === "user") {
      if (a.auth_label) f.append(el("p", "", a.auth_label));
      f.append(el("pre", "", "Redirect URL for your OAuth app:\nhttp://127.0.0.1:" + (a.redirect_port || 8765) + "/callback"));
      const [w1, i1] = input("Client id", "text", "client_id"); fields.client_id = i1; f.append(w1);
      if (a.client_secret) { const [w2, i2] = input("Client secret (hidden)", "password", "client_secret"); fields.client_secret = i2; f.append(w2); }
    }
    if (a.auth === "oauth" && a.client !== "user") f.append(el("p", "", `A ${a.name} sign-in page opens in your browser. Delta keeps the sign-in encrypted on this PC.`));
    const go = el("button", "primary", a.auth === "oauth" ? "Sign in" : "Connect"); go.type = "submit"; f.append(go);
    f.onsubmit = async e => {
      e.preventDefault(); go.disabled = true; msg.className = "msg"; msg.textContent = "Connecting...";
      const body = {inputs: {}, secrets: {}};
      for (const [k, i] of Object.entries(fields)) {
        if (k.startsWith("in_")) body.inputs[k.slice(3)] = i.value.trim();
        else if (k.startsWith("sec_")) body.secrets[k.slice(4)] = i.value.trim();
        else body[k] = i.value.trim();
      }
      const r = await call(`/api/connectors/${a.id}/connect`, body);
      for (const i of Object.values(fields)) if (i.type === "password") i.value = "";
      go.disabled = false;
      msg.className = "msg" + (r.status === "error" ? " err" : "");
      msg.textContent = r.message || r.detail || "";
      if (r.status === "sign_in" && r.url) {
        const l = el("a", "", "Did not see it? Open the sign-in page"); l.href = r.url; l.target = "_blank"; l.rel = "noopener";
        msg.append(document.createElement("br"), l);
      }
      if (r.status === "connected") { open[a.id] = ""; refresh(); }
    };
    return f;
  }

  async function toolsPanel(a, panel) {
    const data = await call(`/api/connectors/${a.id}/tools`);
    if (!data || !Array.isArray(data.tools)) { panel.append(el("div", "msg err", (data && (data.detail || data.message)) || "Could not load the tools.")); return; }
    const trust = document.createElement("label"); trust.style.fontSize = "12.5px";
    const cb = document.createElement("input"); cb.type = "checkbox"; cb.checked = !!data.trust_read_only_hints;
    cb.onchange = async () => { await call(`/api/connectors/${a.id}/trust`, {trust: cb.checked}); panel.innerHTML = ""; toolsPanel(a, panel); };
    trust.append(cb, document.createTextNode(` Trust ${a.name}'s own "read-only" labels (tools it marks read-only then run without asking)`));
    panel.append(trust);
    if (data.truncated) panel.append(el("p", "", "This app lists more tools than Delta shows; only the first 300 are available."));
    const list = el("div", "tools");
    for (const t of data.tools) {
      const row = el("div", "tool");
      const on = document.createElement("input"); on.type = "checkbox"; on.checked = t.enabled; on.title = "Switch this tool on or off";
      on.onchange = () => call(`/api/connectors/${a.id}/tools/${encodeURIComponent(t.name)}`, {enabled: on.checked});
      const info = el("div", ""); info.append(el("b", "", t.name), el("small", "", t.description));
      const sel = document.createElement("select");
      for (const [v, label] of [["", "default (" + t.risk + ")"], ["read", "read: never asks"], ["external", "asks first"], ["destructive", "destructive: always asks"]]) {
        const o = document.createElement("option"); o.value = v; o.textContent = label; if ((t.user_risk || "") === v) o.selected = true; sel.append(o);
      }
      sel.onchange = async () => { await call(`/api/connectors/${a.id}/tools/${encodeURIComponent(t.name)}`, {risk: sel.value}); panel.innerHTML = ""; toolsPanel(a, panel); };
      row.append(on, info, sel);
      list.append(row);
    }
    panel.append(list);
  }

  // Custom server: preview exactly what will run or be reached, then confirm.
  const cf = document.createElement("form");
  const [w1, idIn] = input("Short id (lowercase, e.g. mytools)", "text", "id");
  const [w2, urlIn] = input("Server URL (https://...) or leave empty", "text", "url");
  const [w3, cmdIn] = input("Or a command that starts a local server (e.g. npx -y some-mcp-server@1.2.3)", "text", "command");
  const oauthLabel = document.createElement("label"); oauthLabel.style.fontSize = "12.5px";
  const oauthCb = document.createElement("input"); oauthCb.type = "checkbox";
  oauthLabel.append(oauthCb, document.createTextNode(" This server signs in with OAuth"));
  const preview = el("div", "msg");
  const add = el("button", "", "Preview"); add.type = "submit";
  cf.append(w1, w2, w3, oauthLabel, add, preview);
  customBox.append(el("p", "", "Only add servers you trust: a local server is a program that runs on your PC, and a remote one receives what your tasks send it."), cf);
  cf.onsubmit = async e => {
    e.preventDefault();
    const body = {id: idIn.value.trim(), name: idIn.value.trim(), url: urlIn.value.trim(), command: cmdIn.value.trim(), auth: oauthCb.checked ? "oauth" : "none", confirm: false};
    const r = await call("/api/connectors/custom", body);
    preview.innerHTML = "";
    if (r.status !== "confirm") { preview.className = "msg" + (r.status === "error" || r.detail ? " err" : ""); preview.textContent = r.message || JSON.stringify(r.detail || r); refresh(); return; }
    preview.className = "msg";
    preview.append(el("pre", "", r.message));
    const yes = el("button", "primary", "Yes, add and start it"); yes.type = "button";
    yes.onclick = async () => { yes.disabled = true; const r2 = await call("/api/connectors/custom", {...body, confirm: true}); preview.textContent = r2.message || ""; if (r2.status === "sign_in" && r2.url) { const l = el("a", "", " Open the sign-in page"); l.href = r2.url; l.target = "_blank"; l.rel = "noopener"; preview.append(l); } refresh(); };
    preview.append(yes);
  };
})();
