// Schedules panel in the dashboard's right-hand drawer: list, next run, last result, pause / resume /
// run now / delete, and a create box that shows the parsed schedule in plain words before saving.
// Loaded after the main script, so it uses its `api` helper and `token`. Kept in its own file so the
// main page only needs a container and one <script> tag.
(() => {
  const box = document.getElementById("schedules");
  if (!box || typeof api !== "function") return;
  const style = document.createElement("style");
  style.textContent = `
#schedules .sch{border-bottom:1px solid var(--line);padding:9px 0;font-size:13.5px;line-height:1.45}
#schedules .sch:last-of-type{border-bottom:0}
#schedules .sch.off{opacity:.6}
#schedules .sch small{display:block;color:var(--muted);font-size:12px;margin-top:2px}
#schedules .sch .row{display:flex;gap:6px;margin-top:6px;flex-wrap:wrap}
#schedules .sch .row button{padding:4px 10px;font-size:12.5px}
#schedules .st{font-size:10.5px;font-weight:600;padding:2px 7px;border-radius:var(--r-pill);margin-left:6px;background:var(--surface-2);color:var(--muted)}
#schedules .st.done{color:var(--ok);background:var(--ok-soft)}
#schedules .st.failed,#schedules .st.missed{color:var(--bad);background:var(--bad-soft)}
#schedules .st.needs_user,#schedules .st.held{color:var(--warn);background:var(--warn-soft)}
#addsched{display:flex;gap:6px;margin-top:10px}
#schedtext{flex:1;resize:none;height:38px;border:1px solid var(--line);border-radius:var(--r-sm);background:var(--paper);color:var(--ink);font:13.5px/1.4 var(--font);padding:9px 11px}
#schedpreview{font-size:13px;margin-top:8px;padding:9px 11px;border-radius:var(--r-sm);background:var(--accent-soft)}
#schedpreview.err{background:var(--bad-soft);color:var(--bad)}
#schedpreview .row{display:flex;gap:6px;margin-top:8px}`;
  document.head.appendChild(style);

  const form = document.createElement("form");
  form.id = "addsched";
  form.innerHTML = '<textarea id="schedtext" placeholder="every day at 9 am check my attendance on the ERP"></textarea><button>Preview</button>';
  const preview = document.createElement("div");
  preview.id = "schedpreview"; preview.hidden = true;
  const list = document.createElement("div");
  box.append(list, form, preview);

  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const call = (path, opts) => api(path, opts).catch(e => ({detail: e.message}));

  async function refresh() {
    const items = await api("/api/schedules").catch(() => null);
    if (!Array.isArray(items)) return;
    list.innerHTML = "";
    if (!items.length) {
      list.appendChild(el("div", "empty", 'Nothing scheduled. Try "remind me at 6 pm to call Ravi" or "every Monday 10 am summarise my Downloads folder".'));
      return;
    }
    for (const s of items) {
      const row = el("div", "sch" + (s.enabled ? "" : " off"));
      const title = el("b", "", s.name + (s.kind === "reminder" ? " (reminder)" : ""));
      row.appendChild(title);
      if (s.running) row.appendChild(el("span", "st held", "running"));
      else if (s.last_status) row.appendChild(el("span", "st " + s.last_status, s.last_status.replace("_", " ")));
      row.appendChild(el("small", "", `${s.when} · next: ${s.next_run_text}`));
      if (s.last_message) row.appendChild(el("small", "", `last (${s.last_run_text}): ${s.last_message.slice(0, 160)}`));
      const actions = el("div", "row");
      const btn = (label, fn, primary) => { const b = el("button", primary ? "primary" : "", label); b.type = "button";
        b.onclick = async () => { b.disabled = true; const r = await fn(); if (r && r.detail) alert(r.detail); else if (r && r.message) row.appendChild(el("small", "", r.message)); refresh(); };
        actions.appendChild(b); };
      btn("Run now", () => call(`/api/schedules/${s.id}/run`, {method: "POST"}));
      if (s.enabled) btn("Pause", () => call(`/api/schedules/${s.id}/pause`, {method: "POST"}));
      else btn("Resume", () => call(`/api/schedules/${s.id}/resume`, {method: "POST"}));
      btn("Delete", () => confirm(`Delete "${s.name}"?`) ? call(`/api/schedules/${s.id}`, {method: "DELETE"}) : null);
      row.appendChild(actions);
      list.appendChild(row);
    }
  }

  form.onsubmit = async e => {
    e.preventDefault();
    const text = document.getElementById("schedtext").value.trim();
    if (!text) return;
    const p = await call("/api/schedules/preview", {method: "POST", body: JSON.stringify({text})});
    preview.hidden = false; preview.innerHTML = "";
    if (p.detail) { preview.className = "err"; preview.textContent = p.detail; return; }
    preview.className = "";
    preview.appendChild(el("div", "", p.plain + ` (${p.tz_label})`));
    const r = el("div", "row"), save = el("button", "primary", "Save"), cancel = el("button", "", "Cancel");
    save.type = cancel.type = "button";
    save.onclick = async () => { save.disabled = true;
      const s = await call("/api/schedules", {method: "POST", body: JSON.stringify({text, confirm: true})});
      if (s.detail) { preview.className = "err"; preview.textContent = s.detail; return; }
      preview.hidden = true; document.getElementById("schedtext").value = ""; refresh(); };
    cancel.onclick = () => { preview.hidden = true; };
    r.append(save, cancel); preview.appendChild(r);
  };

  // Results arrive as "schedule.result" events; a light second socket keeps this file independent.
  function listen() {
    try {
      const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/ws?token=${encodeURIComponent(token)}`);
      ws.onmessage = ev => { const m = JSON.parse(ev.data);
        if (m.type === "event" && m.event === "schedule.result") {
          refresh();
          if (window.Notification && Notification.permission === "granted") new Notification("Delta: " + m.data.name, {body: (m.data.text || "").slice(0, 200)});
        } };
      ws.onclose = ev => { if (ev.code !== 4401) setTimeout(listen, 5000); };
    } catch {}
  }
  refresh(); listen();
  setInterval(() => { if (!document.hidden) refresh(); }, 30000);
})();
