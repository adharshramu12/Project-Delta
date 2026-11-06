// Tasks view (delta.engine): every task with filters and search, a timeline for each one (time, tool, short
// result, model, tokens, screenshot), and Resume / Discard for tasks a crash or power cut interrupted. Self-heal
// lines ("Button moved, found it again") and an Undo button for the task's file changes (delta.selfheal) are shown
// in the timeline. Interrupted tasks are also offered in the chat when the dashboard opens.
(() => {
  "use strict";
  const D = window.Delta, {api, h, icon, btn} = D;
  const box = document.getElementById("tasks");
  if (!box) return;

  const PAGE = 30;
  const FILTERS = [["", "All"], ["acting,planning,verifying,queued", "Running"], ["waiting_user", "Waiting for you"], ["interrupted", "Interrupted"],
    ["done", "Done"], ["failed", "Failed"], ["cancelled", "Cancelled"]];
  const LABEL = {acting: "Running", planning: "Planning", verifying: "Checking", queued: "Queued", waiting_user: "Waiting for you",
    interrupted: "Interrupted", done: "Done", failed: "Failed", cancelled: "Cancelled"};
  const KIND = {done: "ok", failed: "danger", cancelled: "", interrupted: "warn", waiting_user: "warn", acting: "accent", planning: "accent", verifying: "accent", queued: "accent"};
  const pill = s => h("span", {class: "pill " + (KIND[s] || ""), text: LABEL[s] || D.words(s)});

  let filter = "", offset = 0, loaded = false;
  const tabs = h("div", {class: "seg", role: "tablist", "aria-label": "Filter tasks"});
  for (const [v, t] of FILTERS) {
    tabs.append(h("button", {type: "button", role: "tab", class: "seg-btn", "aria-selected": String(v === filter), dataset: {v}, onclick: () => {
      filter = v; for (const b of tabs.children) b.setAttribute("aria-selected", String(b.dataset.v === v)); refresh();
    }}, t));
  }
  const q = h("input", {type: "search", placeholder: "Search tasks", "aria-label": "Search tasks"});
  const list = h("div", {class: "table-list", role: "list"});
  const more = btn("Show more", () => load(true), "more");
  box.append(h("div", {class: "toolbar"}, tabs, h("label", {class: "search"}, icon("search"), q), btn("Stop all", stopAll, "sm", "stop")), list);

  async function resume(id, confirmLast, where) {
    const r = await api(`/api/tasks/${encodeURIComponent(id)}/resume`, {method: "POST", body: JSON.stringify(confirmLast ? {confirm_last: confirmLast} : {})}).catch(e => ({message: D.plain(e)}));
    if (r.needs_confirmation) {
      const ask = h("div", {class: "card ask"}, h("b", {text: r.message}),
        h("div", {class: "row"}, (r.options || []).map(o => btn(o.label, () => { ask.remove(); resume(id, o.id, where); }, o.id === "done" ? "primary" : "")),
          btn("Not now", () => ask.remove())));
      (where || list).prepend(ask);
      return;
    }
    if (!r.ok) D.toast({title: "Could not resume", body: r.message || "", kind: "danger"});
    else {
      D.toast({title: "Resumed", body: "Delta continues from the last step. Progress shows in Chat.", kind: "ok"});
      if (where && where.dataset && where.dataset.interrupted) where.remove();
    }
    refresh(); D.emit("tasks:changed");
  }
  async function discard(id) {
    const r = await api(`/api/tasks/${encodeURIComponent(id)}/discard`, {method: "POST"}).catch(() => null);
    if (r && r.message) D.toast({title: r.ok ? "Discarded" : "Not discarded", body: r.message, kind: r.ok ? "info" : "warn"});
    refresh(); D.emit("tasks:changed");
  }
  function actions(t, where) {
    const go = btn("Resume", e => { e.stopPropagation(); go.disabled = true; resume(t.id, null, where); }, "primary sm");
    go.title = "Continue from the last step. Nothing that already changed something is done again without asking you.";
    const drop = btn("Discard", e => { e.stopPropagation(); drop.disabled = true; discard(t.id); if (where && where !== list) where.remove(); }, "sm");
    return h("div", {class: "row"}, go, drop);
  }

  // Stop: Delta lets go of the mouse and keyboard; nothing of yours is closed.
  const RUNNING = new Set(["queued", "planning", "acting", "verifying", "waiting_user"]);
  const stopped = () => { setTimeout(refresh, 600); D.emit("tasks:changed"); };
  const stopButton = id => btn("Stop", async e => {
    e.stopPropagation(); e.currentTarget.disabled = true;
    await api(`/api/tasks/${encodeURIComponent(id)}/cancel`, {method: "POST"}).catch(() => null); stopped();
  }, "sm", "stop");
  async function stopAll() {
    const n = ((await api("/api/tasks/stop-all", {method: "POST"}).catch(() => null)) || {}).stopped || 0;
    D.toast({title: n ? `Stopped ${n}` : "Nothing is running", kind: n ? "ok" : "info"}); stopped();
  }

  function row(t) {
    const tokens = (t.tokens_in || 0) + (t.tokens_out || 0);
    const meta = [D.when(t.created), t.channel, `${t.steps} ${t.steps === 1 ? "step" : "steps"}`];
    if (tokens) meta.push(`${tokens.toLocaleString()} tokens`);
    if (t.wall_s) meta.push(D.dur(t.wall_s));
    const r = h("div", {class: "trow", role: "listitem"},
      h("button", {type: "button", class: "trow-main", onclick: () => openTimeline(t.id), "aria-label": "Open steps of: " + (t.text || "task")},
        pill(t.state), h("span", {class: "trow-text", text: (t.text || "").slice(0, 160)}), h("span", {class: "trow-meta muted", text: meta.join(" · ")}), icon("chev-right")));
    if (t.state === "interrupted") r.append(actions(t, list));
    if (RUNNING.has(t.state)) r.append(h("div", {class: "row"}, stopButton(t.id)));
    return r;
  }
  async function load(append) {
    if (!append) offset = 0;
    const params = new URLSearchParams({limit: String(PAGE), offset: String(offset)});
    if (filter) params.set("state", filter);
    if (q.value.trim()) params.set("q", q.value.trim());
    if (!loaded && !append) list.replaceChildren(D.skeleton(5));
    const r = await api("/api/tasks?" + params).catch(e => ({error: e}));
    more.remove();
    if (r.error || !r.tasks) { if (!append) list.replaceChildren(D.failed(r.error ? D.plain(r.error) : D.detail(r) || "Tasks could not be loaded.", refresh)); return; }
    loaded = true;
    if (!append) list.replaceChildren();
    if (!r.enabled) { list.append(D.empty({ic: "tasks", title: "The task engine is switched off", body: "Turn on engine.enabled in Delta's settings file to keep a history of tasks."})); return; }
    if (!r.tasks.length && !append) {
      list.append(filter || q.value.trim() ? D.empty({ic: "search", title: "No tasks match", body: "Try another filter or search."})
        : D.empty({ic: "tasks", title: "No tasks yet", body: "Ask Delta to do something and it shows up here.", action: {label: "Go to Chat", fn: () => D.go("chat")}}));
      return;
    }
    const frag = document.createDocumentFragment();
    for (const t of r.tasks) frag.append(row(t));
    list.append(frag);
    offset += r.tasks.length;
    if (r.tasks.length === PAGE) list.after(more);
  }
  const refresh = () => load(false);

  async function badge() {
    const r = await api("/api/tasks?state=interrupted&limit=20").catch(() => null);
    const n = r && r.tasks ? r.tasks.length : 0;
    const b = document.getElementById("nb_tasks"); b.hidden = !n; b.textContent = String(n); b.title = n ? `${n} interrupted` : "";
  }

  async function shot(taskId, name) {
    try {
      const u = D.sheetURL(await D.blob(`/api/runs/${encodeURIComponent(taskId)}/${encodeURIComponent(name)}`));
      return h("a", {href: u, target: "_blank", title: "Open screenshot"}, h("img", {src: u, alt: "Screenshot " + name, loading: "lazy"}));
    } catch { return h("span", {class: "muted", text: name}); }
  }

  async function openTimeline(id) {
    const d = await api(`/api/tasks/${encodeURIComponent(id)}/timeline`).catch(() => null);
    if (!d || !d.task) { D.toast({title: "That task's steps could not be loaded", kind: "danger"}); return; }
    const t = d.task, t0 = t.created;
    const at = ts => "+" + Math.max(0, ts - t0).toFixed(1) + " s";
    D.sheet((t.text || "Task").slice(0, 120), body => {
      body.append(h("div", {class: "tl-meta"}, pill(t.state),
        h("span", {class: "muted", text: [D.when(t.created), t.channel, `${t.wall_s} s`, `${((t.tokens_in || 0) + (t.tokens_out || 0)).toLocaleString()} tokens`, t.provider || "no AI"].join(" · ") + (t.resumes ? ` · resumed ${t.resumes}x` : "")})));
      if (t.text && t.text.length > 120) body.append(h("p", {text: t.text}));
      if (t.state === "interrupted") body.append(actions(t, body));
      // Steps: a table, a page at a time for very long runs.
      const table = h("div", {class: "tl-steps", role: "table", "aria-label": "Steps"},
        h("div", {class: "tl-row tl-headrow", role: "row"}, ["Time", "Tool", "Result", "Model", "Tokens", "Screen"].map(c => h("span", {role: "columnheader", text: c}))));
      body.append(h("h3", {text: "Steps"}));
      if (!d.steps.length) body.append(h("p", {class: "muted", text: "No tool steps."}));
      else {
        body.append(table);
        D.paged(table, d.steps, s => {
          const flags = [s.side_effect && "changes something", s.undo && "undo available", s.recovery && "self-heal step", s.failure && s.ok === false && D.words(s.failure)].filter(Boolean);
          const res = s.unconfirmed ? "started, never finished" : (!s.ran ? "not run: " : (s.ok ? "" : "failed: ")) + (s.summary || "");
          const sc = h("span", {role: "cell", class: "tl-shot"});
          if (s.screenshot) shot(id, s.screenshot).then(a => sc.append(a));
          return h("div", {class: "tl-row", role: "row"},
            h("span", {role: "cell", class: "muted mono", text: at(s.started) + (s.duration_ms != null ? ` (${(s.duration_ms / 1000).toFixed(1)} s)` : "")}),
            h("span", {role: "cell"}, h("code", {text: s.tool}), flags.map(f => h("small", {class: "muted", text: f}))),
            h("span", {role: "cell", class: "tl-res" + (s.ok === false || s.unconfirmed ? " bad" : ""), text: res.slice(0, 240)}),
            h("span", {role: "cell", class: "muted", text: s.model || ""}),
            h("span", {role: "cell", class: "muted", text: s.tokens_in + s.tokens_out ? `${s.tokens_in}/${s.tokens_out}` : ""}), sc);
        }, 50);
      }
      const section = (title, lines, line) => { if (lines && lines.length) body.append(h("h3", {text: title}), h("ul", {class: "tl-lines"}, lines.map(line))); };
      section("Self-heal", d.recoveries, r => h("li", {class: r.ok ? "ok" : ""}, h("span", {class: "mono muted", text: at(r.ts)}), r.summary));
      section("Read-only helpers", d.helpers, x => h("li", {class: x.ok ? "ok" : ""}, h("span", {class: "mono muted", text: at(x.ts)}),
        x.summary + (x.tokens ? ` (${x.tokens} tokens, ${x.seconds || 0} s)` : ""), (x.sources || []).slice(0, 4).map(s => h("small", {class: "muted block", text: s}))));
      // Memory recalled, answer grounding and learning: one plain line each.
      for (const [key, title] of [["memory", "Memory"], ["grounding", "Answer check"], ["learning", "Learning"]])
        section(title, d[key], l => h("li", {class: l.ok === false ? "" : "ok"}, h("span", {class: "mono muted", text: at(l.ts)}), l.summary));
      if (d.undo && (d.undo.available || d.undo.restored || (d.undo.skipped || []).length)) {
        const u = h("div", {class: "card"}, h("b", {text: "File changes"}));
        if (d.undo.available) {
          const b = btn(`Undo file changes (${d.undo.files})`, async () => {
            if (!await D.confirm({title: "Put back the files this task changed?", body: "Files in the way are kept in Delta's undo folder.", ok: "Undo changes"})) return;
            b.disabled = true;
            const r = await api(`/api/tasks/${encodeURIComponent(id)}/undo`, {method: "POST"}).catch(e => ({message: D.plain(e)}));
            D.toast({title: r.ok === false ? "Undo did not finish" : "Files put back", body: r.message || "", kind: r.ok === false ? "warn" : "ok"});
            openTimeline(id);
          }, "", "refresh");
          b.title = "Put back the files this task changed. Files in the way are kept in Delta's undo folder.";
          u.append(h("div", {class: "row"}, b));
        } else if (d.undo.restored) u.append(h("p", {class: "muted", text: "File changes were undone."}));
        for (const s of d.undo.skipped || []) u.append(h("p", {class: "muted small", text: `Not undoable: ${s.path} (${s.why})`}));
        body.append(u);
      }
      if (d.checks && d.checks.length) section("Checks", d.checks, c => h("li", {class: c.ok ? "ok" : "bad"}, icon(c.ok ? "check" : "x"), (c.ok ? "Passed: " : "Failed: ") + c.summary));
      if (t.answer) body.append(h("h3", {text: "Answer"}), h("div", {class: "answer-box"}, D.linkify(t.answer)));
    });
  }

  // Interrupted tasks, shown in the chat when the dashboard opens.
  D.showInterruptedTasks = tasks => {
    for (const t of tasks) {
      if (document.querySelector(`[data-interrupted="${CSS.escape(t.id)}"]`)) continue;
      const c = h("div", {class: "card interrupted", dataset: {interrupted: t.id}},
        h("div", {class: "card-top"}, icon("alert"), h("b", {text: "I was interrupted while working on this"})),
        h("p", {text: "\u201c" + (t.text || "") + "\u201d"}),
        h("p", {class: "muted small", text: t.steps ? `${t.steps} ${t.steps === 1 ? "step" : "steps"} had run. Resume continues from there; anything that already changed something is not done again without asking you.` : "No steps had run yet."}));
      const r = actions(t, c);
      r.append(btn("Show steps", () => openTimeline(t.id), "sm"));
      c.append(r);
      D.chat.put(c);
    }
  };
  D.openTimeline = openTimeline;

  let timer = 0;
  q.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(refresh, 250); });
  D.on("tasks:changed", () => { badge(); if (D.view === "tasks") refresh(); });
  D.views.tasks = {show: refresh};
  D.every(15000, refresh, () => D.view === "tasks" && offset <= PAGE);
  D.every(60000, badge);
  badge();
})();
