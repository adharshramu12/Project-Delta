// Build: Delta Build, a coding agent for the user's own projects (delta.studio). Three columns: projects and
// files, the open file or a change's diff, and the chat with the agent. Live steps arrive as studio.* events on the
// dashboard's one connection. Everything shown from a project is set as text (never as HTML).
(() => {
  "use strict";
  const D = window.Delta, {api, h, icon, btn} = D;
  const box = document.getElementById("studio");
  if (!box) return;
  const call = (path, opts) => api(path, opts).catch(e => ({detail: D.plain(e)}));
  const enc = encodeURIComponent;
  let current = null, project = null, running = false, steps = null;
  let showing = ["memory"];  // what the middle column shows, so a refresh shows it again with the new content

  // ---- layout
  const side = h("aside", {class: "st-side", "aria-label": "Projects and files"});
  const viewer = h("section", {class: "st-view", "aria-label": "File or change"});
  const log = h("div", {class: "st-log", role: "log", "aria-live": "polite"});
  const input = h("textarea", {class: "st-input", rows: 3, placeholder: "Describe what to build or change…", "aria-label": "Request"});
  const send = h("button", {type: "submit", class: "btn primary"}, icon("send"), "Send");
  const stopBtn = h("button", {type: "button", class: "btn", hidden: true}, icon("stop"), "Stop");
  const form = h("form", {class: "st-ask"}, input, h("div", {class: "row"}, send, stopBtn,
    h("span", {class: "muted small", text: "Delta works only inside this project, asks before running any command, and every file change can be undone."})));
  const chat = h("section", {class: "st-chat", "aria-label": "Chat with Delta Build"}, log, form);
  box.append(h("div", {class: "st-grid"}, side, viewer, chat));

  // ---- projects
  async function home() {
    current = null; project = null; showing = ["memory"];
    const o = await call("/api/studio");
    if (o.detail) { side.replaceChildren(D.failed(D.detail(o), home)); return; }
    const name = h("input", {class: "input", placeholder: "New project name", "aria-label": "New project name", maxlength: 60});
    const path = h("input", {class: "input", placeholder: "C:\\path\\to\\your\\project", "aria-label": "Existing project folder"});
    const add = async (body) => {
      const r = await call("/api/studio/projects", {method: "POST", body: JSON.stringify(body)});
      if (r.detail) { D.toast({title: "Not opened", body: D.detail(r), kind: "danger"}); return; }
      open(r.id);
    };
    side.replaceChildren(
      h("h3", {class: "list-title", text: "Projects"}),
      ...(o.projects.length ? o.projects.map(p => h("button", {type: "button", class: "st-proj" + (p.exists ? "" : " gone"), onclick: () => open(p.id)},
        h("b", {text: p.name}), h("span", {class: "muted small", text: p.exists ? p.path : "Folder not found: " + p.path}),
        p.running ? h("span", {class: "pill accent", text: "Working"}) : null)) : [h("p", {class: "muted small", text: "No projects yet."})]),
      h("h3", {class: "list-title", text: "New project"}),
      h("form", {class: "st-form", onsubmit: e => { e.preventDefault(); if (name.value.trim()) add({name: name.value}); }},
        name, h("small", {class: "muted", text: `Made in ${o.home}`}), h("button", {type: "submit", class: "btn"}, icon("plus"), "Create")),
      h("h3", {class: "list-title", text: "Open an existing project"}),
      h("form", {class: "st-form", onsubmit: e => { e.preventDefault(); if (path.value.trim()) add({path: path.value}); }},
        path, h("small", {class: "muted", text: "Paste the folder's path (in File Explorer: right-click the folder, Copy as path)."}),
        h("button", {type: "submit", class: "btn"}, icon("external"), "Open")));
    viewer.replaceChildren(D.empty({ic: "spark", title: "Build with Delta",
      body: "Create a project or open one you have. Then tell Delta what to build: it reads the code, makes the changes and keeps notes about the project for next time."}));
    log.replaceChildren(); setRunning(false); form.hidden = true;
  }

  async function open(pid, keepView) {
    const p = await call(`/api/studio/projects/${enc(pid)}`);
    if (p.detail) { D.toast({title: "Not opened", body: D.detail(p), kind: "danger"}); home(); return; }
    current = pid; project = p; form.hidden = false;
    setRunning(p.running);
    renderSide();
    if (!keepView) showMemory(); else refreshView();
    renderHistory();
  }

  function renderSide() {
    const p = project;
    const files = p.tree.filter(f => !f.endsWith("/"));
    side.replaceChildren(
      h("div", {class: "row st-head"}, btn("All projects", home, "sm"), h("b", {class: "st-name", text: p.name})),
      h("p", {class: "muted small st-path", text: p.path}),
      h("div", {class: "row"},
        btn("Preview", () => (project.preview ? showPreview(project.preview) : previewSetup()), "sm primary", "play"),
        btn("Project memory", showMemory, "sm"),
        btn("Undo last request", async () => {
          if (!await D.confirm({title: "Undo the last request?", body: "The files it changed go back to how they were before it.", ok: "Undo"})) return;
          const r = await call(`/api/studio/projects/${enc(current)}/undo`, {method: "POST"});
          if (r.detail) D.toast({title: "Nothing undone", body: D.detail(r), kind: "warn"});
          else { D.toast({title: "Undone", body: `${r.restored.length} file(s) put back.`, kind: "ok"}); open(current); }
        }, "sm"),
        btn("Remove", async () => {
          if (!await D.confirm({title: `Remove "${p.name}" from Build?`, body: "Its files stay on your PC; only Delta's chat and undo history for it are deleted.", ok: "Remove", danger: true})) return;
          const r = await call(`/api/studio/projects/${enc(current)}`, {method: "DELETE"});
          if (r.detail) D.toast({title: "Not removed", body: D.detail(r), kind: "danger"}); else home();
        }, "sm danger-outline")),
      h("div", null, !(p.allowed && p.allowed.length) ? null : h("details", {class: "st-allowed"}, h("summary", {class: "small", text: `Always-allowed commands (${p.allowed.length})`}),
        ...p.allowed.map(cmd => h("div", {class: "row"}, h("code", {class: "small", text: cmd}), btn("Ask again", async () => {
          await call(`/api/studio/projects/${enc(current)}/disallow`, {method: "POST", body: JSON.stringify({command: cmd})});
          open(current, true);
        }, "sm"))))),
      h("h3", {class: "list-title", text: `Files (${files.length}${p.tree.length >= 400 ? "+" : ""})`}),
      h("div", {class: "st-tree", role: "list"}, ...(files.length ? files.map(f => h("button", {type: "button", role: "listitem", class: "st-file",
        title: f, onclick: () => showFile(f)}, h("span", {class: "st-dir", text: f.includes("/") ? f.slice(0, f.lastIndexOf("/") + 1) : ""}),
        h("span", {text: f.slice(f.lastIndexOf("/") + 1)}))) : [h("p", {class: "muted small", text: "No files yet."})])));
  }

  // ---- viewer
  function code(text) { return h("pre", {class: "st-code"}, h("code", {text})); }
  function refreshView() {
    const [kind, arg] = showing;
    if (kind === "file") showFile(arg); else if (kind === "diff") showDiff(arg);
    else if (kind === "preview" && project.preview) showPreview(project.preview); else showMemory();
  }
  // ---- live preview: the project's page in a sandboxed frame (the dashboard allows frames from this PC only)
  function previewSetup(error) {
    showing = ["setup"];
    const cmd = h("input", {class: "input", placeholder: "npm run dev", "aria-label": "Dev server command"});
    const start = async command => {
      viewer.replaceChildren(h("p", {class: "muted", text: command ? `Starting "${command}"… (up to 90 seconds)` : "Starting…"}));
      const r = await call(`/api/studio/projects/${enc(current)}/preview`, {method: "POST", body: JSON.stringify({command})});
      if (r.detail) { previewSetup(D.detail(r)); return; }
      project.preview = r; showPreview(r);
    };
    viewer.replaceChildren(h("div", {class: "st-vhead"}, h("b", {text: "Live preview"})),
      error ? h("pre", {class: "st-code st-err", text: error}) : null,
      h("p", {class: "small", text: "A plain HTML, CSS and JavaScript project: Delta shows the folder as it is."}),
      btn("Show the project", () => start(""), "primary", "play"),
      h("p", {class: "small st-gap", text: "An app with a dev server (React, Vite, Next.js, Flask…): enter the command that starts it. It runs in the project folder until you stop it."}),
      h("form", {class: "row", onsubmit: e => { e.preventDefault(); if (cmd.value.trim()) start(cmd.value.trim()); }},
        cmd, h("button", {type: "submit", class: "btn"}, "Start")));
  }
  function showPreview(pv) {
    showing = ["preview"];
    const frame = h("iframe", {class: "st-frame", src: pv.url, title: "Live preview of the project",
      sandbox: "allow-scripts allow-forms allow-same-origin allow-modals allow-popups", referrerpolicy: "no-referrer"});
    const logs = h("pre", {class: "st-code st-logs", hidden: true});
    viewer.replaceChildren(
      h("div", {class: "row st-pbar"}, h("span", {class: "pill ok", text: pv.kind === "static" ? "Serving the folder" : "Dev server"}),
        h("code", {class: "small st-url", text: pv.url}),
        btn("Reload", () => { frame.src = pv.url + (pv.url.includes("?") ? "&" : "?") + "r=" + Date.now(); }, "sm", "refresh"),
        h("a", {class: "btn sm", href: pv.url, target: "_blank", rel: "noopener noreferrer"}, icon("external"), "Open in browser"),
        pv.kind === "server" ? btn("Output", async () => {
          const r = await call(`/api/studio/projects/${enc(current)}/preview`);
          logs.textContent = r.logs || "(no output)"; logs.hidden = !logs.hidden;
        }, "sm") : null,
        btn("Stop preview", async () => {
          await call(`/api/studio/projects/${enc(current)}/preview`, {method: "DELETE"});
          project.preview = null; showMemory();
        }, "sm danger-outline")),
      logs, frame);
  }
  function showMemory() {
    showing = ["memory"];
    viewer.replaceChildren(h("div", {class: "st-vhead"}, h("b", {text: "Project memory"}), h("span", {class: "muted small", text: ".delta/PROJECT.md"})),
      project.memory ? code(project.memory) : D.empty({ic: "memory", title: "No project memory yet",
        body: "After the first request Delta writes down what the project is, how to run it, decisions and next steps, and reads it again every time."}));
  }
  async function showFile(rel) {
    showing = ["file", rel];
    const f = await call(`/api/studio/projects/${enc(current)}/file?path=${enc(rel)}`);
    if (f.detail) { viewer.replaceChildren(D.failed(D.detail(f), () => showFile(rel))); return; }
    viewer.replaceChildren(h("div", {class: "st-vhead"}, h("b", {text: f.path}), h("span", {class: "muted small", text: D.bytes(f.size)})),
      f.binary ? h("p", {class: "muted", text: "A binary file: not shown."}) : code(f.text + (f.cut ? "\n… (shortened)" : "")));
  }
  async function showDiff(turn) {
    showing = ["diff", turn];
    const d = await call(`/api/studio/projects/${enc(current)}/diff?turn=${enc(turn)}`);
    if (d.detail) { D.toast({title: "Not shown", body: D.detail(d), kind: "warn"}); return; }
    viewer.replaceChildren(h("div", {class: "st-vhead"}, h("b", {text: "What this request changed"})),
      ...d.map(f => h("div", {class: "st-diff"}, h("div", {class: "row"}, h("b", {text: f.path}), h("span", {class: "pill", text: f.kind})),
        h("pre", {class: "st-code"}, ...f.diff.split("\n").map(line => h("span", {class: line.startsWith("+") && !line.startsWith("+++") ? "add"
          : line.startsWith("-") && !line.startsWith("---") ? "del" : line.startsWith("@@") ? "hunk" : "", text: line + "\n"}))))));
  }

  // ---- chat
  function bubble(m) {
    const changed = Object.entries(m.changed || {});
    return h("div", {class: "st-msg " + m.role},
      h("div", {class: "st-text", text: m.text}),
      changed.length ? h("div", {class: "st-changed"}, h("span", {class: "muted small", text: `${changed.length} file(s): ` + changed.map(([f, k]) => `${f} (${k})`).join(", ")}),
        m.turn ? btn("See changes", () => showDiff(m.turn), "sm") : null) : null,
      m.status && m.status !== "done" ? h("span", {class: "pill warn", text: D.words(m.status)}) : null);
  }
  function renderHistory() {
    log.replaceChildren(...(project.history.length ? project.history.map(bubble)
      : [h("p", {class: "muted small st-hint", text: "Try: \"Make a simple to-do web app in plain HTML, CSS and JavaScript\" or \"Explain this project and suggest the next three improvements\"."})]));
    if (running) log.append(steps = h("div", {class: "st-steps"}, h("span", {class: "muted small", text: "Working…"}), ...(project.asks || []).map(askCard)));
    log.scrollTop = log.scrollHeight;
  }
  function setRunning(on) {
    running = on; send.disabled = on; stopBtn.hidden = !on; input.disabled = on;
  }
  form.onsubmit = async e => {
    e.preventDefault();
    const text = input.value.trim();
    if (!text || !current) return;
    setRunning(true);
    const r = await call(`/api/studio/projects/${enc(current)}/run`, {method: "POST", body: JSON.stringify({text})});
    if (r.detail) { setRunning(false); D.toast({title: "Not started", body: D.detail(r), kind: "danger"}); return; }
    input.value = "";
    project.history.push({role: "user", text});
    renderHistory();
  };
  input.addEventListener("keydown", e => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); form.requestSubmit(); } });
  stopBtn.onclick = () => call(`/api/studio/projects/${enc(current)}/stop`, {method: "POST"});

  // A command waiting for the user's yes. "Always" is offered only for commands that do not send anything off the PC.
  function askCard(a) {
    const answer = async (allow, always) => {
      card.querySelectorAll("button").forEach(b => { b.disabled = true; });
      const r = await call(`/api/studio/projects/${enc(current)}/answer`, {method: "POST", body: JSON.stringify({id: a.id, allow, always})});
      if (r.detail) D.toast({title: "Not answered", body: D.detail(r), kind: "warn"});
    };
    const card = h("div", {class: "st-ask-card" + (a.sends ? " sends" : ""), dataset: {ask: a.id}, role: "group", "aria-label": "Command waiting for your OK"},
      h("b", {text: a.sends ? "Run this? It sends something off your PC" : "Run this command in the project?"}),
      h("pre", {class: "st-code", text: a.command}),
      a.why ? h("span", {class: "muted small", text: a.why}) : null,
      h("div", {class: "row"}, btn("Run it", () => answer(true, false), "sm primary"),
        a.sends ? null : btn("Always allow this command here", () => answer(true, true), "sm"),
        btn("Don't run", () => answer(false, false), "sm danger-outline")));
    return card;
  }

  const LABEL = {list_files: "Looked at the files", read_file: "Read", search: "Searched for", write_file: "Wrote", edit_file: "Edited",
    delete_file: "Deleted", update_memory: "Updated the project memory", run_command: "Wants to run",
    start_preview: "Started the live preview", check_preview: "Checked the preview", preview_logs: "Read the preview's output",
    stop_preview: "Stopped the preview"};
  D.on("ws", m => {
    if (m.type !== "event" || !String(m.event).startsWith("studio.") || !m.data || m.data.project !== current) return;
    const d = m.data;
    if (m.event === "studio.done") { setRunning(false); open(current, true); return; }
    if (m.event === "studio.preview") {
      project.preview = d.alive ? d : null;
      if (d.alive) showPreview(d); else if (showing[0] === "preview") showMemory();
      return;
    }
    if (!steps) { steps = h("div", {class: "st-steps"}); log.append(steps); }
    if (m.event === "studio.tool") {
      const a = d.args || {};
      steps.append(h("div", {class: "st-step"}, icon("circle"), h("span", {text: `${LABEL[d.tool] || d.tool} ${a.path || a.query || a.folder || ""}`.trim()})));
    } else if (m.event === "studio.tool_done" && !d.ok) {
      steps.append(h("div", {class: "st-step bad"}, icon("alert"), h("span", {class: "small", text: d.summary})));
    } else if (m.event === "studio.approve") {
      steps.append(askCard(d));
    } else if (m.event === "studio.approved") {
      const card = log.querySelector(`[data-ask="${CSS.escape(d.id)}"]`);
      if (card) card.replaceChildren(h("span", {class: "muted small", text: d.allow ? "Allowed." : "Not allowed: Delta carries on without it."}));
    } else if (m.event === "studio.command") {
      steps.append(h("div", {class: "st-step"}, icon("play"), h("code", {text: d.command})));
    } else if (m.event === "studio.command_done") {
      const ok = d.exit === 0 && !d.timed_out;
      const out = h("details", {class: "st-out"}, h("summary", {class: ok ? "" : "bad",
        text: d.timed_out ? `Timed out after ${d.seconds}s and was stopped` : `${ok ? "Passed" : "Failed"} (exit ${d.exit}, ${d.seconds}s)`}),
        h("pre", {class: "st-code", text: d.tail || "(no output)"}));
      if (!ok) out.open = true;
      steps.append(out);
    } else if (m.event === "studio.note") {
      steps.append(h("div", {class: "st-note", text: d.text}));
    }
    log.scrollTop = log.scrollHeight;
  });

  D.views.build = {show: () => (current ? open(current, true) : home())};
})();
