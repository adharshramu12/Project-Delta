// Schedules view: what runs by itself, its next run and last result, pause / resume / run now / delete, and a
// create box that shows the parsed schedule in plain words before anything is saved.
(() => {
  "use strict";
  const D = window.Delta, {api, h, icon, btn} = D;
  const box = document.getElementById("schedules");
  if (!box) return;
  const call = (path, opts) => api(path, opts).catch(e => ({detail: D.plain(e)}));
  const KIND = {done: "ok", failed: "danger", missed: "danger", needs_user: "warn", held: "warn"};

  const text = h("textarea", {id: "schedtext", rows: 2, placeholder: "Every day at 9 am check my attendance on the college website", "aria-label": "What should run, and when"});
  const form = h("form", {id: "addsched", class: "card create"},
    h("label", {class: "field", for: "schedtext"}, "New schedule", h("small", {text: 'Say what and when in plain words, for example "remind me at 6 pm to call Ravi" or "every Monday 10 am summarise my Downloads folder".'})),
    text, h("div", {class: "row"}, h("button", {type: "submit", class: "btn primary"}, "Preview")));
  const preview = h("div", {id: "schedpreview", class: "card preview", role: "status", hidden: true});
  const list = h("div", {class: "table-list", role: "list"});
  box.append(form, preview, h("h3", {class: "list-title", text: "Scheduled"}), list);
  let loaded = false;

  async function refresh() {
    if (!loaded) list.replaceChildren(D.skeleton(3));
    const items = await api("/api/schedules").catch(e => ({error: e}));
    if (!Array.isArray(items)) { list.replaceChildren(D.failed(items.error ? D.plain(items.error) : D.detail(items) || "Schedules could not be loaded.", refresh)); return; }
    loaded = true;
    if (!items.length) {
      list.replaceChildren(D.empty({ic: "clock", title: "Nothing scheduled yet", body: "Describe a reminder or a repeating task above. You see the plain-words schedule before it is saved."}));
      return;
    }
    list.replaceChildren(...items.map(row));
  }
  function row(s) {
    const status = s.running ? h("span", {class: "pill accent", text: "Running"})
      : s.last_status ? h("span", {class: "pill " + (KIND[s.last_status] || ""), text: D.words(s.last_status)}) : null;
    const msg = h("p", {class: "muted small", role: "status"});
    const act = (label, fn, cls = "sm") => {
      const b = btn(label, async () => {
        b.disabled = true;
        const r = await fn();
        if (r && r.detail) D.toast({title: "Not done", body: D.detail(r), kind: "danger"});
        else if (r && r.message) msg.textContent = r.message;
        refresh();
      }, cls);
      return b;
    };
    return h("div", {class: "srow" + (s.enabled ? "" : " off"), role: "listitem"},
      h("span", {class: "srow-ic"}, icon(s.kind === "reminder" ? "bell" : "clock")),
      h("div", {class: "srow-main"},
        h("div", {class: "srow-title"}, h("b", {text: s.name}), s.kind === "reminder" ? h("span", {class: "pill", text: "Reminder"}) : null,
          s.enabled ? null : h("span", {class: "pill", text: "Paused"}), status),
        h("p", {class: "muted small", text: `${s.when} · next: ${s.enabled ? s.next_run_text : "paused"}`}),
        s.last_message ? h("p", {class: "small", text: `Last run (${s.last_run_text}): ${s.last_message.slice(0, 200)}`}) : null, msg),
      h("div", {class: "row srow-acts"},
        act("Run now", () => call(`/api/schedules/${encodeURIComponent(s.id)}/run`, {method: "POST"})),
        s.enabled ? act("Pause", () => call(`/api/schedules/${encodeURIComponent(s.id)}/pause`, {method: "POST"}))
          : act("Resume", () => call(`/api/schedules/${encodeURIComponent(s.id)}/resume`, {method: "POST"})),
        act("Delete", async () => await D.confirm({title: `Delete "${s.name}"?`, body: "It will not run again.", ok: "Delete", danger: true})
          ? call(`/api/schedules/${encodeURIComponent(s.id)}`, {method: "DELETE"}) : null, "sm danger-outline")));
  }

  form.onsubmit = async e => {
    e.preventDefault();
    const t = text.value.trim();
    if (!t) { text.focus(); return; }
    const p = await call("/api/schedules/preview", {method: "POST", body: JSON.stringify({text: t})});
    preview.hidden = false;
    if (p.detail) { preview.className = "card preview error"; preview.replaceChildren(icon("alert"), h("span", {text: D.detail(p)})); return; }
    preview.className = "card preview";
    const save = btn("Save schedule", async () => {
      save.disabled = true;
      const s = await call("/api/schedules", {method: "POST", body: JSON.stringify({text: t, confirm: true})});
      if (s.detail) { preview.className = "card preview error"; preview.replaceChildren(icon("alert"), h("span", {text: D.detail(s)})); return; }
      preview.hidden = true; text.value = ""; D.toast({title: "Schedule saved", body: p.plain, kind: "ok"}); refresh();
    }, "primary");
    preview.replaceChildren(h("p", {class: "small muted", text: "Delta understood:"}), h("p", {class: "preview-plain", text: `${p.plain} (${p.tz_label})`}),
      h("div", {class: "row"}, save, btn("Cancel", () => { preview.hidden = true; })));
    save.focus();
  };
  text.addEventListener("keydown", e => { if (e.key === "Enter" && (e.ctrlKey || !e.shiftKey)) { e.preventDefault(); form.requestSubmit(); } });

  // Results arrive as "schedule.result" events on the dashboard's one live connection.
  D.on("ws", m => {
    if (m.type === "event" && m.event === "schedule.result") {
      if (D.view === "schedules") refresh();
      D.toast({title: "Delta: " + m.data.name, body: (m.data.text || "").slice(0, 200), kind: "info"});
      if (window.Notification && Notification.permission === "granted") new Notification("Delta: " + m.data.name, {body: (m.data.text || "").slice(0, 200)});
    }
  });
  D.views.schedules = {show: refresh};
  D.every(30000, refresh, () => D.view === "schedules");
})();
