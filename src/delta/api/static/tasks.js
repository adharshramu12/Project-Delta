// Tasks panel in the dashboard's right-hand drawer (delta.engine): recent tasks with filters, a run
// timeline for each one (time, tool, short result, model, tokens, screenshot), and Resume / Discard for
// tasks a crash or power cut interrupted. Loaded after the main script, so it uses its `api` helper and
// `token`. Kept in its own file so the main page only needs a container and one <script> tag.
(() => {
  const box = document.getElementById("tasks");
  if (!box || typeof api !== "function") return;
  const style = document.createElement("style");
  style.textContent = `
#tasks .tfilters{display:flex;gap:6px;margin-bottom:8px}
#tasks select,#tasks input{border:1px solid var(--line);border-radius:var(--r-sm);background:var(--paper);color:var(--ink);font:13px var(--font);padding:6px 8px}
#tasks input{flex:1;min-width:0}
#tasks .trow{border-bottom:1px solid var(--line);padding:8px 0;font-size:13.5px;line-height:1.45;cursor:pointer}
#tasks .trow:last-of-type{border-bottom:0}
#tasks .trow small{display:block;color:var(--muted);font-size:12px;margin-top:2px}
#tasks .trow .row{display:flex;gap:6px;margin-top:6px}
#tasks .trow .row button{padding:4px 10px;font-size:12.5px}
.tst{font-size:10.5px;font-weight:600;padding:2px 7px;border-radius:var(--r-pill);margin-right:6px;background:var(--surface-2);color:var(--muted);white-space:nowrap}
.tst.done{color:var(--ok);background:var(--ok-soft)}
.tst.failed,.tst.cancelled{color:var(--bad);background:var(--bad-soft)}
.tst.interrupted,.tst.waiting_user{color:var(--warn);background:var(--warn-soft)}
.tst.acting,.tst.planning,.tst.verifying,.tst.queued{color:var(--accent,var(--ink));background:var(--accent-soft,var(--surface-2))}
#timeline{position:fixed;inset:0;z-index:30;background:rgba(0,0,0,.35);display:flex;align-items:center;justify-content:center}
#timeline .tl{background:var(--surface);color:var(--ink);border-radius:var(--r-md);box-shadow:var(--shadow-2);width:min(920px,calc(100% - 32px));max-height:86vh;overflow:auto;padding:18px 20px}
#timeline .thead{display:flex;justify-content:space-between;gap:12px;align-items:flex-start}
#timeline h2{margin:0 0 4px;font-size:16px}
#timeline .meta{color:var(--muted);font-size:12.5px}
#timeline table{width:100%;border-collapse:collapse;margin-top:12px;font-size:12.5px}
#timeline th{text-align:left;color:var(--muted);font-weight:600;border-bottom:1px solid var(--line);padding:6px 6px}
#timeline td{border-bottom:1px solid var(--line);padding:6px 6px;vertical-align:top}
#timeline td.res{max-width:360px;word-break:break-word}
#timeline td.bad{color:var(--bad)}
#timeline img{height:54px;border-radius:6px;border:1px solid var(--line)}
#timeline .answer{margin-top:12px;white-space:pre-wrap;font-size:13.5px;background:var(--surface-2);border-radius:var(--r-sm);padding:10px 12px}`;
  document.head.appendChild(style);

  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const label = s => (s || "").replace("_", " ");
  const when = ts => ts ? new Date(ts * 1000).toLocaleString([], {month: "short", day: "numeric", hour: "2-digit", minute: "2-digit"}) : "";

  const filters = el("div", "tfilters");
  const state = el("select");
  for (const [v, t] of [["", "All"], ["interrupted", "Interrupted"], ["acting,planning,verifying,queued", "Running"], ["done", "Done"], ["failed", "Failed"], ["cancelled", "Cancelled"], ["waiting_user", "Waiting for you"]]) {
    const o = el("option", "", t); o.value = v; state.appendChild(o);
  }
  const q = el("input"); q.placeholder = "Search tasks";
  filters.append(state, q);
  const list = el("div");
  box.append(filters, list);

  async function resume(id, confirmLast, where) {
    const r = await api(`/api/tasks/${id}/resume`, {method: "POST", body: JSON.stringify(confirmLast ? {confirm_last: confirmLast} : {})}).catch(e => ({message: e.message}));
    if (r.needs_confirmation) {
      const ask = el("div", "card");
      ask.appendChild(el("b", "", r.message));
      const row = el("div", "row");
      for (const o of r.options) { const b = el("button", o.id === "done" ? "primary" : "", o.label); b.type = "button"; b.onclick = () => { ask.remove(); resume(id, o.id, where); }; row.appendChild(b); }
      const no = el("button", "", "Not now"); no.type = "button"; no.onclick = () => ask.remove(); row.appendChild(no);
      ask.appendChild(row); (where || list).prepend(ask);
      return;
    }
    if (!r.ok) alert(r.message || "Could not resume.");
    else if (where && where.dataset && where.dataset.interrupted) where.remove();
    refresh();
  }
  async function discard(id) { await api(`/api/tasks/${id}/discard`, {method: "POST"}).catch(() => {}); refresh(); }

  function actions(t, where) {
    const row = el("div", "row");
    const go = el("button", "primary", "Resume"), drop = el("button", "", "Discard");
    go.type = drop.type = "button";
    go.title = "Continue from the last step. Nothing that already changed something is done again without asking you.";
    go.onclick = e => { e.stopPropagation(); go.disabled = true; resume(t.id, null, where); };
    drop.onclick = e => { e.stopPropagation(); drop.disabled = true; discard(t.id); if (where && where !== list) where.remove(); };
    row.append(go, drop);
    return row;
  }

  async function refresh() {
    const params = new URLSearchParams({limit: "30"});
    if (state.value) params.set("state", state.value);
    if (q.value.trim()) params.set("q", q.value.trim());
    const r = await api("/api/tasks?" + params).catch(() => null);
    if (!r) return;
    list.innerHTML = "";
    if (!r.enabled) { list.appendChild(el("div", "empty", "The task engine is switched off in settings.")); return; }
    if (!r.tasks.length) { list.appendChild(el("div", "empty", "No tasks yet.")); return; }
    for (const t of r.tasks) {
      const row = el("div", "trow");
      row.append(el("span", "tst " + t.state, label(t.state)), el("span", "", (t.text || "").slice(0, 90)));
      row.appendChild(el("small", "", `${when(t.created)} · ${t.channel} · ${t.steps} step(s)` + (t.tokens_in + t.tokens_out ? ` · ${(t.tokens_in + t.tokens_out).toLocaleString()} tokens` : "")));
      if (t.state === "interrupted") row.appendChild(actions(t, list));
      row.onclick = () => openTimeline(t.id);
      list.appendChild(row);
    }
  }

  async function shot(taskId, name) {
    try {
      const blob = await fetch(`/api/runs/${taskId}/${encodeURIComponent(name)}`, {headers: {"Authorization": "Bearer " + token}}).then(x => { if (!x.ok) throw 0; return x.blob(); });
      const a = el("a"); a.href = URL.createObjectURL(blob); a.target = "_blank";
      const img = el("img"); img.src = a.href; img.alt = name; a.appendChild(img); return a;
    } catch { return el("span", "", name); }
  }

  async function openTimeline(id) {
    const d = await api(`/api/tasks/${id}/timeline`).catch(() => null);
    if (!d || !d.task) return;
    document.getElementById("timeline")?.remove();
    const wrap = el("div"); wrap.id = "timeline";
    const card = el("div", "tl");
    const head = el("div", "thead");
    const left = el("div");
    const h = el("h2"); h.append(el("span", "tst " + d.task.state, label(d.task.state)), document.createTextNode((d.task.text || "").slice(0, 160)));
    left.append(h, el("div", "meta", `${when(d.task.created)} · ${d.task.channel} · ${d.task.wall_s}s · ${(d.task.tokens_in + d.task.tokens_out).toLocaleString()} tokens · ${d.task.provider || "no AI"}` + (d.task.resumes ? ` · resumed ${d.task.resumes}x` : "")));
    const close = el("button", "", "Close"); close.type = "button"; close.onclick = () => wrap.remove();
    head.append(left, close);
    card.appendChild(head);
    if (d.task.state === "interrupted") card.appendChild(actions(d.task, card));
    const table = el("table");
    const tr = el("tr"); for (const c of ["Time", "Tool", "Result", "Model", "Tokens", "Screen"]) tr.appendChild(el("th", "", c));
    table.appendChild(tr);
    const t0 = d.task.created;
    for (const s of d.steps) {
      const row = el("tr");
      row.appendChild(el("td", "", "+" + Math.max(0, s.started - t0).toFixed(1) + "s" + (s.duration_ms != null ? ` (${(s.duration_ms / 1000).toFixed(1)}s)` : "")));
      const tool = el("td"); tool.appendChild(el("b", "", s.tool)); if (s.side_effect) tool.appendChild(el("div", "meta", "changes something"));
      row.appendChild(tool);
      const res = s.unconfirmed ? "started, never finished" : (!s.ran ? "not run: " : (s.ok ? "" : "failed: ")) + (s.summary || "");
      row.appendChild(el("td", "res" + (s.ok === false || s.unconfirmed ? " bad" : ""), res.slice(0, 240)));
      row.appendChild(el("td", "", s.model || ""));
      row.appendChild(el("td", "", s.tokens_in + s.tokens_out ? `${s.tokens_in}/${s.tokens_out}` : ""));
      const sc = el("td"); if (s.screenshot) shot(id, s.screenshot).then(a => sc.appendChild(a)); row.appendChild(sc);
      table.appendChild(row);
    }
    if (!d.steps.length) { const r = el("tr"); const c = el("td", "", "No tool steps."); c.colSpan = 6; r.appendChild(c); table.appendChild(r); }
    card.appendChild(table);
    for (const c of d.checks) card.appendChild(el("div", "meta", (c.ok ? "Check passed: " : "Check failed: ") + c.summary));
    if (d.task.answer) card.appendChild(el("div", "answer", d.task.answer));
    wrap.appendChild(card);
    wrap.onclick = e => { if (e.target === wrap) wrap.remove(); };
    document.body.appendChild(wrap);
  }

  // Interrupted tasks, shown in the chat when the dashboard opens (checkInterrupted in the main page).
  window.showInterruptedTasks = tasks => {
    const log = document.getElementById("log");
    if (!log) return;
    for (const t of tasks) {
      if (document.querySelector(`[data-interrupted="${t.id}"]`)) continue;
      const c = el("div", "checklist"); c.dataset.interrupted = t.id;
      c.appendChild(el("div", "chead", "I was interrupted while working on this:"));
      c.appendChild(el("div", "citem", "\u201c" + (t.text || "") + "\u201d"));
      c.appendChild(el("div", "citem", t.steps ? `${t.steps} step(s) had run. Resume continues from there; anything that already changed something is not done again without asking you.` : "No steps had run yet."));
      const row = actions(t, c); row.style.marginTop = "6px";
      const view = el("button", "", "Show steps"); view.type = "button"; view.onclick = () => openTimeline(t.id); row.appendChild(view);
      c.appendChild(row);
      log.appendChild(c);
    }
  };
  window.openTaskTimeline = openTimeline;

  state.onchange = refresh;
  let timer = null; q.oninput = () => { clearTimeout(timer); timer = setTimeout(refresh, 250); };
  refresh();
  setInterval(() => { if (!document.hidden) refresh(); }, 15000);
})();
