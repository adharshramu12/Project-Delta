// Chat: the chat list in the sidebar, the conversation, live task progress, approvals (inline, in the progress
// panel, the bell, a toast, a chime and a desktop notification), questions from Delta, proof screenshots, documents
// and the usage of each answer. Messages are stored by Delta per chat; memory, skills and approvals are shared by
// every chat. Work in a chat you are not looking at keeps running and marks that chat (working or new answer).
(() => {
  "use strict";
  const D = window.Delta, {$, api, h, icon, btn} = D;

  let current = D.pref.get("delta_chat");  // open chat id ("" = a new chat, created on the first message)
  const taskChat = {};  // task id -> chat id
  const unread = new Set(), busy = new Set();
  const shown = taskId => !taskChat[taskId] || taskChat[taskId] === current;
  let chatURLs = [];  // screenshots and previews of the open chat, freed when it closes
  const keepURL = b => { const u = URL.createObjectURL(b); chatURLs.push(u); return u; };
  const log = $("log");
  const HERO = $("hero").cloneNode(true);
  const NO_USAGE = () => ({tasks: 0, tokens: 0, calls: 0, secs: 0, estimate: false, fileTokens: 0});
  let usage = NO_USAGE();

  const scrollEnd = el => { const near = log.scrollHeight - log.scrollTop - log.clientHeight < 160; if (near || el) log.scrollTop = log.scrollHeight; };
  function put(el) { $("hero")?.remove(); log.append(el); scrollEnd(el); return el; }
  // A short line in the conversation (Delta remembered something, a skill changed, a chat reply was sent).
  function note(text, kind = "", ic = "info") { return put(h("div", {class: "note-line " + kind}, icon(ic), h("span", {text}))); }

  // ---- messages
  function userMsg(text, files) {
    const list = (files || []).filter(f => f && f.id);
    const bubble = h("div", {class: "bubble", text});
    if (!list.length) return put(h("div", {class: "msg me"}, bubble));
    const row = h("div", {class: "msg-files", role: "list", "aria-label": "Attached files"}, list.map(f => fileChip(f)));
    return put(h("div", {class: "msg me"}, h("div", {class: "me-col"}, row, bubble)));
  }
  // ---- attachment chips (names come from files: text only)
  const KINDS = {image: "Picture", pdf: "PDF", docx: "Word", pptx: "PowerPoint", text: "Text"};
  function fileChip(f, extra) {
    const chip = h("span", {class: "achip", role: "listitem", title: f.name}, icon(f.kind === "image" ? "image" : "doc"),
      h("span", {class: "an"}, h("b", {text: f.name || "file"}), h("small", {text: extra || [KINDS[f.kind] || "File", f.summary].filter(Boolean).join(", ")})));
    if (f.kind === "image" && f.id) D.blob(`/api/attachments/${encodeURIComponent(f.id)}/preview`).then(b => chip.firstChild.replaceWith(h("img", {src: keepURL(b), alt: ""}))).catch(() => {});
    return chip;
  }
  function usageLine(r) {
    const t = r.tokens || {}, u = r.usage || {};
    // Tokens per task: reported by the AI, an estimate (labelled), or "not reported" (some CLI AIs).
    const src = u.source, tok = (u.input || 0) + (u.output || 0);
    const amount = src === "not reported" ? "tokens not reported"
      : src ? `${D.k(tok)} tokens${src === "estimate" ? " (estimate)" : src === "mixed" ? " (part estimate)" : ""}`
      : `${D.k((t.input || 0) + (t.output || 0))} tokens`;
    const calls = u.calls || t.calls;
    const parts = [];
    const a = u.attachments;
    if (a && a.attachments) parts.push(`${a.attachments} file(s): ~${D.k(a.attachment_tokens_est || 0)} tokens` + (a.attachment_images ? `, ${a.attachment_images} picture(s) seen` : ""));
    if (r.provider) parts.push("via " + r.provider);
    if (r.fallback) parts.push(r.fallback);
    if (r.attempts) parts.push(r.attempts + (r.attempts === 1 ? " attempt" : " attempts"));
    if (calls) parts.push(`${calls} AI ${calls === 1 ? "call" : "calls"}`, amount + (t.cached ? ` (+${D.k(t.cached)} cached)` : ""));
    else if (r.status === "done") parts.push("instant, 0 tokens");
    if (r.wall_s) parts.push(D.dur(r.wall_s));
    return {parts, tokens: src === "not reported" ? 0 : src ? tok : (t.input || 0) + (t.output || 0), calls: calls || 0, estimate: src === "estimate" || src === "mixed"};
  }
  const STATUS = {done: ["Done", "ok"], needs_user: ["Needs your answer", "warn"], failed: ["Did not finish", "danger"],
    cancelled: ["Stopped", ""], blocked: ["Blocked", "danger"], denied: ["Not allowed", "danger"]};
  function renderAnswer(r, live) {
    const u = usageLine(r);
    const [label, kind] = STATUS[r.status] || [D.words(r.status || "Answer"), ""];
    const ok = r.status === "done" || r.status === "needs_user";
    put(h("div", {class: "msg delta" + (ok ? "" : " failed")},
      h("span", {class: "avatar"}, icon("logo")),
      h("div", {class: "answer"},
        h("div", {class: "answer-text"}, D.linkify(String(r.answer || ""))),
        // a sign-in's captcha to read (small JPEG)
        live && r.image && /^[A-Za-z0-9+/=]+$/.test(r.image) ? h("img", {class: "captcha-pic", alt: "Captcha: reply with these characters", src: "data:image/jpeg;base64," + r.image}) : null,
        h("div", {class: "answer-meta"}, h("span", {class: "pill " + kind, text: label}), u.parts.length ? h("span", {text: u.parts.join(" · ")}) : null))));
    usage.tasks++; usage.tokens += u.tokens; usage.calls += u.calls; usage.secs += r.wall_s || 0; usage.estimate = usage.estimate || u.estimate;
    usage.fileTokens += ((r.usage || {}).attachments || {}).attachment_tokens_est || 0;
    renderUsage();
    if (r.checks && r.checks.length) showProof(r);
    if (live && r.status === "needs_user" && r.asked_about) showOptions(r);
  }
  function renderUsage() {
    const box = $("usage");
    if (!usage.tasks) { box.replaceChildren(h("p", {class: "muted small", text: "Tokens and time for each answer show here."})); return; }
    const cell = (v, l) => h("div", {class: "stat"}, h("b", {text: v}), h("small", {text: l}));
    box.replaceChildren(cell(String(usage.tasks), usage.tasks === 1 ? "answer" : "answers"),
      cell(D.k(usage.tokens) + (usage.estimate ? "*" : ""), "tokens"), cell(String(usage.calls), "AI calls"), cell(usage.secs ? D.dur(usage.secs) : "0 s", "working"));
    if (usage.fileTokens) box.append(h("p", {class: "muted small span", text: `Attached files: about ${D.k(usage.fileTokens)} tokens.`}));
    if (usage.estimate) box.append(h("p", {class: "muted small span", text: "* includes estimates where the AI did not report its count."}));
  }

  // ---- task runs: a compact step trail in the chat, the plan checklist in the progress panel
  const runs = {};
  const MAX_STEPS = 200;
  function runFor(taskId) {
    if (runs[taskId]) return runs[taskId];
    const list = h("ol", {class: "run-steps"});
    const label = h("span", {class: "run-label", text: "Working"});
    const toggle = h("button", {type: "button", class: "run-toggle", "aria-expanded": "true"}, icon("chev-down"), label);
    const stop = btn("Stop", () => stopTask(taskId), "sm", "stop");
    const block = h("div", {class: "run running", dataset: {task: taskId}}, h("div", {class: "run-head"}, h("span", {class: "spinner", "aria-hidden": "true"}), toggle, stop), list);
    toggle.onclick = () => { const open = block.classList.toggle("collapsed"); toggle.setAttribute("aria-expanded", String(!open)); };
    const r = runs[taskId] = {block, list, label, stop, steps: 0, hidden: 0, started: Date.now(), plan: null, goal: "", status: "running"};
    put(block);
    return r;
  }
  function stepLine(taskId, cls, tool, text) {
    const r = runFor(taskId);
    r.steps++;
    if (r.list.children.length >= MAX_STEPS) { r.hidden++; r.label.textContent = `Working: ${r.steps} steps (${r.hidden} older not shown)`; return; }
    r.list.append(h("li", {class: cls}, tool ? h("code", {text: tool}) : null, text ? h("span", {text}) : null));
    updateRunLabel(taskId);
    scrollEnd();
  }
  function updateRunLabel(taskId) {
    const r = runs[taskId]; if (!r) return;
    const done = r.plan ? r.plan.filter(i => i.done).length : 0;
    const base = r.status === "running" ? "Working" : r.status === "done" ? "Finished" : "Ended";
    r.label.textContent = `${base}: ${r.steps} ${r.steps === 1 ? "step" : "steps"}` + (r.plan ? `, ${done} of ${r.plan.length} planned done` : "");
    if (taskId === active) renderProgress();
  }
  let active = "", clock = 0;
  function showChecklist(taskId, plan) {
    const steps = (plan.steps || []).filter(Boolean); if (!steps.length) return;
    const r = runFor(taskId);
    r.goal = plan.replay ? `Using learned skill: ${plan.replay}` : (plan.goal || "Plan");
    r.plan = steps.map(s => ({text: String(s), low: String(s).toLowerCase(), done: false}));
    active = taskId; renderProgress(); updateRunLabel(taskId);
  }
  // Each tool call ticks the first open step that names it. Plans in plain words ("open Notepad") name no tool:
  // then each action (not a read or a check) moves the list on by one step; the last step waits for the final check.
  function tickStep(taskId, tool) {
    const r = runs[taskId], items = r && r.plan; if (!items) return;
    let i = items.findIndex(it => !it.done && it.low.includes(tool.toLowerCase()));
    const looking = /^(read_|list_|look_|browser_read|media_status|path_info|recall|screenshot)/.test(tool);
    if (i === -1 && !looking) { i = items.findIndex(it => !it.done); if (i === items.length - 1) i = -1; }
    if (i === -1) return;
    for (let j = 0; j <= i; j++) items[j].done = true;
    updateRunLabel(taskId);
  }
  function tickAll(taskId) { const r = runs[taskId]; if (r && r.plan) { r.plan.forEach(it => { it.done = true; }); updateRunLabel(taskId); } }
  function renderProgress() {
    const box = $("progress"), r = runs[active];
    clearInterval(clock); clock = 0;
    if (!r) { box.replaceChildren(h("p", {class: "muted small", text: "Nothing running. Progress shows here while Delta works."})); return; }
    const secs = () => Math.round((Date.now() - r.started) / 1000);
    const time = h("span", {class: "muted small", text: r.status === "running" ? `${secs()} s` : ""});
    const [label, kind] = r.status === "running" ? ["Working", "accent"] : (STATUS[r.status] || [D.words(r.status), ""]);
    const head = h("div", {class: "prog-head"}, h("span", {class: "pill " + kind, text: label}), time,
      r.status === "running" ? btn("Stop", () => stopTask(active), "sm", "stop") : null);
    const goal = r.goal ? h("p", {class: "prog-goal", text: r.goal}) : null;
    const items = r.plan ? h("ol", {class: "checklist"}, r.plan.map(it => h("li", {class: it.done ? "done" : ""}, icon(it.done ? "check-circle" : "circle"), h("span", {text: it.text})))) : null;
    const count = h("p", {class: "muted small", text: `${r.steps} ${r.steps === 1 ? "step" : "steps"} so far` + (r.plan ? "" : ". This task has no written plan.")});
    box.replaceChildren(...[head, goal, items, count].filter(Boolean));
    if (r.status === "running") clock = setInterval(() => { if (!document.hidden) time.textContent = `${secs()} s`; }, 1000);
  }
  async function stopTask(taskId) {
    const r = runs[taskId];
    if (r) { r.stop.disabled = true; r.stop.lastChild.textContent = "Stopping"; }
    await api(`/api/tasks/${taskId}/cancel`, {method: "POST"}).catch(() => {});
  }
  function taskEnded(taskId, status) {
    const r = runs[taskId];
    if (r) { r.status = status || "done"; r.block.classList.remove("running"); r.stop.remove(); r.block.classList.add("collapsed");
      r.block.querySelector(".run-toggle").setAttribute("aria-expanded", "false"); updateRunLabel(taskId); }
    for (const c of document.querySelectorAll(`.approval[data-task="${CSS.escape(taskId)}"]`)) dropApproval(c.dataset.approval);
    if (taskId === active) renderProgress();
  }

  // ---- approvals
  const pending = new Map();
  let muted = D.pref.get("delta_mute") === "1";
  function approvalCard(a, where) {
    const answer = async (ok, forTask, edit) => {
      for (const b of card.querySelectorAll("button")) b.disabled = true;
      const r = await api(`/api/approvals/${encodeURIComponent(a.id)}`, {method: "POST", body: JSON.stringify({approved: ok, for_task: !!forTask, edit})}).catch(e => ({error: D.plain(e)}));
      if (r && r.error) D.toast({title: "Your answer did not reach Delta", body: r.error, kind: "danger"});
      dropApproval(a.id, edit != null ? "Edit requested" : ok ? (forTask ? "Allowed for this task" : "Approved") : "Denied");
    };
    // An email draft: Approve / Edit / Cancel. Edit says what to change; Delta redrafts and asks again.
    const ed = a.can_edit ? h("textarea", {rows: "2", hidden: true, placeholder: "What should change?", "aria-label": "What should change in the email"}) : null;
    const edit = () => { if (ed.hidden) { ed.hidden = false; ed.focus(); } else answer(false, false, ed.value.trim()); };
    // "Allow for this task" shows only when Delta offers it: never for permanent deletes, terminals, data in a link.
    const card = h("div", {class: "approval", role: "group", "aria-label": "Delta needs your OK", dataset: {approval: a.id, task: a.task_id || "", where}},
      h("div", {class: "approval-top"}, icon("bell"), h("span", {text: "Needs your OK"}), a.risk ? h("span", {class: "pill warn", text: D.words(a.risk)}) : null),
      h("p", {class: "approval-what" + (a.can_edit ? " draft" : ""), text: a.summary}), ed,
      a.reason ? h("p", {class: "approval-why"}, h("span", {class: "muted", text: "Why Delta asks: "}), a.reason) : null,
      h("div", {class: "row"},
        h("button", {type: "button", class: "btn primary", title: "Do this one action now", onclick: () => answer(true)}, "Approve"),
        a.for_task ? h("button", {type: "button", class: "btn", title: "Also allow this same kind of action until this task ends. Not saved.", onclick: () => answer(true, true)}, "Allow for this task") : null,
        a.can_edit ? h("button", {type: "button", class: "btn", title: "Say what to change; nothing is sent", onclick: edit}, "Edit") : null,
        h("button", {type: "button", class: "btn danger-outline", title: "Don't do it. Delta looks for another way or stops.", onclick: () => answer(false)}, a.can_edit ? "Cancel" : "Deny")),
      a.for_task && where === "log" ? h("p", {class: "muted small", text: "Allow for this task also allows this kind of action until the task ends. It is not saved."}) : null);
    return card;
  }
  function renderApproval(a) {
    if (pending.has(a.id)) return;
    pending.set(a.id, a);
    $("approvals").querySelector(".placeholder")?.remove();
    $("approvals").append(approvalCard(a, "panel"));
    if (shown(a.task_id)) put(approvalCard(a, "log"));
    alertApproval(a);
  }
  function dropApproval(id, outcome) {
    if (!pending.has(id)) return;
    const a = pending.get(id);
    pending.delete(id);
    for (const el of document.querySelectorAll(`.approval[data-approval="${CSS.escape(id)}"]`)) {
      if (outcome && el.dataset.where === "log") el.replaceWith(h("div", {class: "note-line " + (outcome === "Denied" ? "bad" : "ok")}, icon(outcome === "Denied" ? "x" : "check"), h("span", {text: `${outcome}: ${a.summary}`})));
      else el.remove();
    }
    updateBell();
  }
  function updateBell() {
    const n = pending.size;
    $("bell").classList.toggle("has", n > 0);
    $("bellcount").hidden = !n; $("bellcount").textContent = n > 9 ? "9+" : String(n);
    $("bell").setAttribute("aria-label", n ? `${n} ${n > 1 ? "approvals" : "approval"} waiting` : "Approvals");
    $("bell").title = $("bell").getAttribute("aria-label");
    $("nb_chat").hidden = !n; $("nb_chat").textContent = String(n);
    if (!n && !$("approvals").children.length) $("approvals").append(h("p", {class: "muted small placeholder", text: "Nothing waiting. Actions that change or send something wait here for your OK."}));
    titleBar();
  }
  let audio;
  function chime() {
    if (muted) return;
    try {
      audio = audio || new (window.AudioContext || window.webkitAudioContext)();
      const t = audio.currentTime;
      [[659.25, 0], [880, .14]].forEach(([f, d]) => {   // E5 then A5: a soft two-note bell
        const o = audio.createOscillator(), g = audio.createGain();
        o.type = "sine"; o.frequency.value = f; o.connect(g); g.connect(audio.destination);
        g.gain.setValueAtTime(0.0001, t + d); g.gain.exponentialRampToValueAtTime(0.18, t + d + 0.02);
        g.gain.exponentialRampToValueAtTime(0.0001, t + d + 0.55); o.start(t + d); o.stop(t + d + 0.6);
      });
    } catch {}
  }
  function alertApproval(a) {
    updateBell();
    const bell = $("bell"); bell.classList.remove("ring"); void bell.offsetWidth; bell.classList.add("ring");
    chime();
    D.toast({title: "Delta needs your OK", body: a.summary, kind: "approval", timeout: 9000, action: {label: "Review", fn: openApprovals}});
    if ((document.hidden || !document.hasFocus()) && "Notification" in window && Notification.permission === "granted") {
      const n = new Notification("Delta needs your OK", {body: a.summary, tag: "delta-approval-" + a.id, requireInteraction: true});
      n.onclick = () => { window.focus(); openApprovals(); n.close(); };
    }
  }
  function openApprovals() {
    if (D.view !== "chat") D.go("chat");
    setPanel(true, true);
    const first = log.querySelector(".approval") || $("approvals").querySelector(".approval");
    if (first) { first.scrollIntoView({block: "center"}); first.querySelector("button")?.focus(); }
  }
  function setMuted(m) {
    muted = m; D.pref.set("delta_mute", m ? "1" : "0");
    $("mute").replaceChildren(icon(m ? "mute" : "sound"));
    $("mute").setAttribute("aria-pressed", String(!m)); $("mute").title = m ? "Approval sound off" : "Approval sound on";
    D.emit("mute", m);
  }

  // ---- the progress panel: open by default on wide windows, a slide-over on narrow ones
  function setPanel(open, temporary) {
    document.body.classList.toggle("panel-closed", !open);
    $("panelbtn").setAttribute("aria-pressed", String(open));
    if (!temporary) D.pref.set("delta_drawer", open ? "open" : "closed");
  }

  // ---- interrupted tasks: offered, never resumed silently (a half-done task may already have sent something)
  async function checkInterrupted() {
    const t = await api("/api/tasks?state=interrupted&limit=5").catch(() => null);
    if (t && t.enabled && t.tasks.length && D.showInterruptedTasks) { D.showInterruptedTasks(t.tasks); return; }
    const u = await api("/api/runs/unfinished").catch(() => null); const run = u && u.run; if (!run) return;
    const done = (run.steps || []).filter(s => s.ok).map(s => s.tool);
    const close = () => api(`/api/runs/${encodeURIComponent(run.task_id)}/dismiss`, {method: "POST"}).catch(() => {}).finally(() => box.remove());
    const box = h("div", {class: "card interrupted"},
      h("div", {class: "card-top"}, icon("alert"), h("b", {text: "I was interrupted while working on this"})),
      h("p", {text: "\u201c" + run.request + "\u201d"}),
      h("p", {class: "muted small", text: done.length ? `${done.length} ${done.length === 1 ? "step was" : "steps were"} already done: ${done.join(", ")}` : "No steps had finished yet."}),
      h("div", {class: "row"}, btn("Resume", async () => {
        await close();
        $("text").value = done.length ? `${run.request} (resume: these steps already ran before you stopped: ${done.join(", ")}; check the current state before doing anything again)` : run.request;
        $("send").requestSubmit();
      }, "primary"), btn("Dismiss", close)));
    put(box);
  }

  // ---- questions from Delta: tap an option (or type your own) to continue the same request
  function showForm(r) {
    const picks = r.questions.map(() => new Set());
    const box = h("div", {class: "qform card"});
    r.questions.forEach((q, qi) => {
      const opts = h("div", {class: "qopts", role: q.multi ? "group" : "radiogroup", "aria-label": q.q});
      for (const o of q.options) {
        const b = h("button", {type: "button", class: "opt", "aria-pressed": "false"}, icon("circle"), h("span", {text: o}));
        b.onclick = () => {
          if (q.multi) { picks[qi].has(o) ? picks[qi].delete(o) : picks[qi].add(o); }
          else picks[qi] = new Set([o]);
          for (const x of opts.children) { const on = picks[qi].has(x.lastChild.textContent); x.setAttribute("aria-pressed", String(on)); x.firstChild.replaceWith(icon(on ? "check-circle" : "circle")); }
        };
        opts.append(b);
      }
      box.append(h("div", {class: "qblock"}, h("b", {text: q.q + (q.multi ? " (pick all that apply)" : "")}), opts));
    });
    box.append(h("div", {class: "row"}, btn("Continue", () => {
      const lines = r.questions.map((q, qi) => `${q.q} -> ${[...picks[qi]].join(", ") || "(no preference)"}`);
      $("text").value = `${r.asked_about}\nMy answers:\n${lines.join("\n")}`; $("send").requestSubmit(); box.remove();
    }, "primary")));
    put(box);
  }
  function showOptions(r) {
    if (r.questions && r.questions.length) return showForm(r);
    const row = h("div", {class: "qopts quick", role: "group", "aria-label": "Answer Delta"});
    const answer = choice => { $("text").value = `${r.asked_about} (${choice})`; $("send").requestSubmit(); row.remove(); };
    for (const o of r.options || []) row.append(h("button", {type: "button", class: "opt", onclick: () => answer(o)}, o));
    row.append(h("button", {type: "button", class: "opt ghost", onclick: () => { $("text").value = r.asked_about + " ("; $("text").focus(); row.remove(); }}, "Something else"));
    put(row);
  }

  // ---- proof: checkpoints judged against the final screenshot, plus the screenshots
  async function showProof(r) {
    const box = h("div", {class: "proof"}, h("p", {class: "proof-head", text: r.seen_screen ? "Checked against a screenshot of the final screen" : "Checked against the tool log (no screenshot available)"}));
    for (const c of r.checks) box.append(h("div", {class: "pcheck " + (c.met ? "ok" : "bad")}, icon(c.met ? "check" : "x"),
      h("span", {}, c.checkpoint, c.seen ? h("small", {class: "muted", text: ": " + c.seen}) : null)));
    put(box);
    const list = await api(`/api/runs/${encodeURIComponent(r.task_id)}`).catch(() => ({images: []}));
    if (!list.images || !list.images.length) return;
    const strip = h("div", {class: "pimgs"});
    for (const name of list.images.slice(0, 12)) {
      try {
        const u = keepURL(await D.blob(`/api/runs/${encodeURIComponent(r.task_id)}/${encodeURIComponent(name)}`));
        strip.append(h("a", {href: u, target: "_blank", title: name}, h("img", {src: u, alt: "Screenshot " + name, loading: "lazy"})));
      } catch {}
    }
    box.append(strip);
  }

  // ---- a document Delta made: preview the PDF and offer each file. Files come through the authenticated
  // /api/documents route as blobs, so the token never goes into a URL.
  async function showDocument(d) {
    const box = h("div", {class: "card doc-card"}, h("div", {class: "card-top"}, icon("doc"), h("b", {text: d.title || "Document"})));
    put(box);
    if (d.preview) {
      try { box.append(h("iframe", {class: "doc-frame", src: keepURL(await D.blob(`/api/documents/${encodeURIComponent(d.preview)}`)), title: "Preview of " + d.preview})); }
      catch { /* no preview: the buttons below still work */ }
    }
    box.append(h("div", {class: "row"}, (d.files || []).map(name => D.views.documents.downloadButton(name, "Download " + name.split(".").pop().toUpperCase()))));
  }

  // ---- live events
  D.on("ws", m => {
    if (m.type === "approval") renderApproval(m.approval);
    else if (m.type === "result") {
      const r = m.result;
      taskEnded(r.task_id, r.status);
      const chat = r.chat_id || taskChat[r.task_id];
      if (chat) busy.delete(chat);
      if (!chat || chat === current) renderAnswer(r, true);
      else unread.add(chat);
      D.emit("memory:changed"); D.emit("tasks:changed"); refreshChats();
    } else if (m.type === "event") {
      const d = m.data || {}, ev = m.event;
      if (ev === "governor.health") D.emit("status:refresh");
      if (ev === "skill.changed") D.emit("skills:changed");
      if (ev === "memory.learned") D.emit("memory:changed");
      if (ev === "learning.added") D.emit("learned:changed");
      if (ev === "document.created") D.emit("documents:changed");
      if (ev === "chat.handoff") {
        D.toast({title: `${d.person} needs you`, body: d.reason, kind: "warn", timeout: 0});
        if (window.Notification && Notification.permission === "granted") new Notification(`Delta: ${d.person} needs you`, {body: d.reason});
      }
      if (d.task_id && !shown(d.task_id)) return;  // belongs to a chat that is not open
      if (ev === "task.plan" && d.plan && d.plan.needs_tools) showChecklist(d.task_id, d.plan);
      if (ev === "task.step") { tickStep(d.task_id, d.tool); if (!(d.args && d.args.auto)) stepLine(d.task_id, "", d.tool, JSON.stringify(d.args || {}).slice(0, 140)); }
      if (ev === "task.verify") { if (d.passed) tickAll(d.task_id); stepLine(d.task_id, d.passed ? "ok" : "bad", "", (d.passed ? "Verified: " : "Not verified: ") + d.reason); }
      if (ev === "task.recovery") stepLine(d.task_id, d.ok ? "ok" : "", "", "Self-heal: " + d.line);
      if (ev === "task.attachments") stepLine(d.task_id, "", "", `Read ${d.files} file(s) on this PC: ~${D.k(d.tokens)} tokens` + (d.full_tokens > d.tokens ? ` instead of ${D.k(d.full_tokens)}` : ""));
      if (ev === "task.grounding" && ((d.unverified || []).length || (d.contradicted || []).length))
        stepLine(d.task_id, d.contradicted.length ? "bad" : "", "", "Answer check: " + (d.contradicted.length ? "not true: " + d.contradicted.join(", ") : "not confirmed: " + d.unverified.join(", ")));
      if (ev === "chat.replied") note(`Replied to ${d.person} on ${d.app}: \u201c${d.text}\u201d`, "ok", "send");
      if (ev === "chat.skipped") note(`You skipped a reply to ${d.person}: \u201c${d.text}\u201d`, "", "send");
      if (ev === "chat.handoff") note(`Stopped chatting with ${d.person} (${d.app}): ${d.reason}. Please reply yourself.`, "bad", "alert");
      if (ev === "chat.ended") note(`Finished chatting with ${d.person} on ${d.app}: ${d.reason}`, "", "send");
      if (ev === "skill.changed") {
        const w = {learned: "learned a new skill", promoted: "skill is now proven", retired: "retired a failing skill", reinforced: "skill worked again", weakened: "skill did not work this time"};
        note(`Delta ${w[d.change] || d.change}: ${d.skill && d.skill.name}`, "ok", "spark");
      }
      if (ev === "memory.learned") note("Remembered: " + d.fact, "", "memory");
      if (ev === "document.created") showDocument(d);
      if (ev === "learning.added") {
        const i = d.item || {};
        note(i.status === "pending" ? `Suggestion waiting for your OK in Memory, Learned: ${i.content}` : `Got it, I'll remember: ${i.content}`, "ok", "spark");
      }
    }
  });
  // After a reconnect, anything missed while offline is fetched again.
  D.on("reconnected", () => { api("/api/approvals").then(list => list.forEach(renderApproval)).catch(() => {}); refreshChats(); });

  // ---- chats
  function greet() {
    const g = $("greet"); if (!g) return;
    const hr = new Date().getHours();
    g.textContent = (hr < 12 ? "Good morning" : hr < 17 ? "Good afternoon" : "Good evening") + ". What should we get done?";
  }
  function showHero() {
    log.replaceChildren(HERO.cloneNode(true));
    greet(); bindExamples(); D.emit("hero");
  }
  function freeChat() {
    for (const u of chatURLs) URL.revokeObjectURL(u);
    chatURLs = [];
    for (const id of Object.keys(runs)) delete runs[id];
    active = ""; renderProgress();
    usage = NO_USAGE(); renderUsage();
  }
  function titleBar(t) {
    if (t !== undefined) titleBar.t = t;
    const n = pending.size;
    document.title = n ? `(${n}) Approval needed · Delta` : titleBar.t ? `${titleBar.t} · Delta` : "Delta";
    if (D.view === "chat") D.setTitle(titleBar.t || "New chat");
  }
  function setCurrent(id, title) {
    current = id; unread.delete(id);
    D.pref.set("delta_chat", id || null);
    titleBar(id ? (title || "") : "");
    for (const el of document.querySelectorAll(".citm")) el.classList.toggle("on", el.dataset.id === id);
  }
  function newChat() {
    freeChat(); setCurrent(""); showHero(); D.go("chat"); $("text").focus(); refreshChats();
    if (files.length) clearFiles();
  }
  async function openChat(id) {
    if (D.view !== "chat") D.go("chat");
    log.replaceChildren(D.skeleton(3));
    const data = await api(`/api/chats/${encodeURIComponent(id)}`).catch(() => null);
    if (!data || !data.chat) { newChat(); return; }
    freeChat();
    setCurrent(id, data.chat.title);
    log.replaceChildren();
    if (!data.messages.length) showHero();
    for (const m of data.messages) {
      if (m.role === "user") userMsg(m.text, (m.meta || {}).attachments);
      else renderAnswer({...m.meta, answer: m.text}, false);
    }
    const last = data.messages[data.messages.length - 1];
    if (last && last.role === "delta" && last.meta.status === "needs_user" && last.meta.asked_about) showOptions({...last.meta, answer: last.text});
    const cap = data.captcha;  // a sign-in still waiting for the captcha reply (expires after 10 minutes)
    if (cap && /^[A-Za-z0-9+/=]+$/.test(cap.image)) put(h("div", {class: "msg delta"}, h("span", {class: "avatar"}, icon("logo")), h("div", {class: "answer"}, h("div", {class: "answer-text", text: `Captcha for ${cap.login}: reply with just these characters, or 'cancel'.`}), h("img", {class: "captcha-pic", alt: "Captcha: reply with these characters", src: "data:image/jpeg;base64," + cap.image}))));
    for (const tid of data.running || []) { taskChat[tid] = id; runFor(tid); stepLine(tid, "", "", "Still working on this..."); active = tid; renderProgress(); }
    for (const a of pending.values()) if (a.task_id && taskChat[a.task_id] === id) put(approvalCard(a, "log"));
    log.scrollTop = log.scrollHeight;
    refreshChats();
  }
  const DAY = 86400e3;
  function groupOf(c) {
    if (c.pinned) return "Pinned";
    const start = new Date(); start.setHours(0, 0, 0, 0);
    const t = c.updated * 1000;
    return t >= start ? "Today" : t >= start - DAY ? "Yesterday" : t >= start - 7 * DAY ? "Previous 7 days" : t >= start - 30 * DAY ? "Previous 30 days" : "Older";
  }
  let chatTimer = 0, chatsLoaded = false;
  function refreshChats() { clearTimeout(chatTimer); chatTimer = setTimeout(loadChats, 120); }
  async function loadChats() {
    const q = $("chatsearch").value.trim();
    const box = $("chatlist");
    if (!chatsLoaded) box.replaceChildren(D.skeleton(4));
    const list = await api("/api/chats" + (q ? "?q=" + encodeURIComponent(q) : "")).catch(() => null);
    if (!list) { if (!chatsLoaded) box.replaceChildren(h("p", {class: "none", text: "Chats could not be loaded."})); return; }
    chatsLoaded = true;
    const frag = document.createDocumentFragment();
    if (!list.length) frag.append(h("p", {class: "none", text: q ? "No chats match." : "Your chats will show up here."}));
    let group = "";
    for (const c of list.slice(0, 200)) {
      if (c.running) busy.add(c.id); else busy.delete(c.id);
      const g = groupOf(c);
      if (g !== group) { group = g; frag.append(h("div", {class: "grp", text: g})); }
      frag.append(chatItem(c));
      if (c.id === current) titleBar(c.title);
    }
    box.replaceChildren(frag);
  }
  function chatItem(c) {
    const state = busy.has(c.id) ? "running" : unread.has(c.id) ? "unread" : "";
    const title = h("span", {class: "t", text: c.title});
    const main = h("button", {type: "button", class: "citm-main", title: c.preview || c.title, onclick: () => openChat(c.id)},
      h("span", {class: "st", "aria-label": state === "running" ? "Working" : state === "unread" ? "New answer" : null}), title);
    const act = (ic, label, fn, cls = "") => h("button", {type: "button", class: "icon-btn xs " + cls, "aria-label": label, title: label, onclick: e => { e.stopPropagation(); fn(); }}, icon(ic));
    const el = h("div", {class: "citm " + state + (c.id === current ? " on" : ""), role: "listitem", dataset: {id: c.id}}, main,
      h("span", {class: "acts"},
        act("pin", c.pinned ? "Unpin" : "Pin", () => api(`/api/chats/${encodeURIComponent(c.id)}`, {method: "PATCH", body: JSON.stringify({pinned: !c.pinned})}).then(refreshChats)),
        act("edit", "Rename", () => {
          const inp = h("input", {class: "rename", value: c.title, "aria-label": "Chat name", maxlength: "120"});
          main.replaceWith(inp); inp.focus(); inp.select();
          let done = false;
          const save = () => { if (done) return; done = true;
            if (inp.value.trim() === c.title) { refreshChats(); return; }  // unchanged: keep the stored name
            api(`/api/chats/${encodeURIComponent(c.id)}`, {method: "PATCH", body: JSON.stringify({title: inp.value})})
              .then(ch => { if (c.id === current) setCurrent(c.id, ch.title); refreshChats(); }).catch(refreshChats); };
          inp.onkeydown = ev => { if (ev.key === "Enter") inp.blur(); if (ev.key === "Escape") { done = true; refreshChats(); } };
          inp.onblur = save;
        }),
        act("trash", "Delete", async () => {
          if (!await D.confirm({title: `Delete "${c.title}"?`, body: (c.running ? "Its running task is stopped too. " : "") + "This can't be undone.", ok: "Delete chat", danger: true})) return;
          await api(`/api/chats/${encodeURIComponent(c.id)}`, {method: "DELETE"}).catch(() => {});
          if (c.id === current) newChat(); else refreshChats();
        }, "danger")));
    return el;
  }

  // ---- sending
  const failed = text => put(h("div", {class: "msg delta failed"}, h("span", {class: "avatar"}, icon("logo")), h("div", {class: "answer"}, h("div", {class: "answer-text", text}))));
  $("send").onsubmit = async e => {
    e.preventDefault(); const text = $("text").value.trim();
    if (files.some(f => f.state === "loading")) { D.toast({title: "Still reading your files", kind: "warn"}); return; }
    const ready = files.filter(f => f.state === "ready");
    if (!text && !ready.length) return;
    if (D.view !== "chat") D.go("chat");
    $("text").value = ""; grow();
    const sent = userMsg(text, ready.map(f => f.att));
    const ids = ready.map(f => f.att.id);
    clearFiles();
    const sentFrom = current;
    const started = await api("/api/tasks", {method: "POST", body: JSON.stringify({text, chat_id: current, attachments: ids})})
      .catch(err => { failed("Could not reach Delta. " + D.plain(err)); return null; });
    if (started && started.text && !text) { const b = sent.querySelector(".bubble"); if (b) b.textContent = started.text; }
    if (!started || !started.task_id) {
      if (started && started.detail) failed(D.detail(started));
      return;
    }
    taskChat[started.task_id] = started.chat_id; busy.add(started.chat_id);
    if (sentFrom === current && started.chat_id !== current) setCurrent(started.chat_id);  // a new chat was just created
    if (started.chat_id === current) { runFor(started.task_id); active = started.task_id; renderProgress(); }
    D.pref.set("delta_tried", "1");
    refreshChats();
  };
  function grow() { const t = $("text"); t.style.height = "auto"; t.style.height = Math.min(t.scrollHeight, 200) + "px"; }
  $("text").addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); $("send").requestSubmit(); } });
  $("text").addEventListener("input", grow);
  function bindExamples() {
    for (const c of log.querySelectorAll(".example")) c.onclick = () => {
      const q = c.dataset.q; $("text").value = q; $("text").focus(); grow();
      if (!q.endsWith(" ")) $("send").requestSubmit();
    };
    D.hydrate(log);
  }

  // ---- attach (button, drop, paste): uploaded and read at once, so the chip says what was found
  let files = [];
  function renderFiles() { $("attachlist").hidden = !files.length; $("attachlist").replaceChildren(...files.map(f => f.el)); }
  function clearFiles() { files = []; renderFiles(); }
  function chipFor(f, file) {
    const chip = f.att ? fileChip(f.att) : fileChip({name: file.name, kind: file.type.startsWith("image/") ? "image" : ""}, f.error || "Reading on this PC");
    chip.classList.toggle("loading", !f.att && !f.error); chip.classList.toggle("bad", !!f.error);
    chip.append(h("button", {type: "button", class: "icon-btn xs", "aria-label": "Remove " + file.name, onclick: () => {
      files = files.filter(x => x !== f); renderFiles();
      if (f.att) api(`/api/attachments/${encodeURIComponent(f.att.id)}`, {method: "DELETE"}).catch(() => {});
    }}, icon("x")));
    return f.el = chip;
  }
  async function upload(f, file) {
    try {
      const r = await fetch("/api/attachments?name=" + encodeURIComponent(file.name || "picture.png"), {method: "PUT", body: file, headers: {"Authorization": "Bearer " + D.token, "Content-Type": "application/octet-stream"}});
      const data = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(D.detail(data) || "Delta could not take that file.");
      f.att = data.attachment; f.state = "ready";
      if (f.att.kind === "image" && !data.can_see_pictures) f.att.summary += ", text only";
    } catch (err) { f.state = "error"; f.error = err.message || "Delta could not take that file."; }
    const old = f.el; old.replaceWith(chipFor(f, file));
  }
  function addFiles(list) {
    // Every file is sent and read at once: a picture read with OCR never holds up the PDF next to it.
    const started = [];
    for (const file of [...list]) {
      if (files.length >= 5) { D.toast({title: "Up to 5 files per message", kind: "warn"}); break; }
      const f = {state: "loading"}; chipFor(f, file); files.push(f); started.push([f, file]);
    }
    renderFiles();
    return Promise.all(started.map(([f, file]) => upload(f, file)));
  }
  $("attachbtn").onclick = () => $("attachinput").click();
  $("attachinput").onchange = () => { addFiles($("attachinput").files); $("attachinput").value = ""; };
  $("text").addEventListener("paste", e => { const l = e.clipboardData && e.clipboardData.files; if (l && l.length) { e.preventDefault(); addFiles(l); } });
  const zone = document.querySelector(".chat-main"), drop = on => $("composer").classList.toggle("dropping", on);
  const dragged = e => e.dataTransfer && [...e.dataTransfer.types].includes("Files");
  zone.addEventListener("dragover", e => { if (dragged(e)) { e.preventDefault(); drop(true); } });
  zone.addEventListener("dragleave", e => { if (!zone.contains(e.relatedTarget)) drop(false); });
  zone.addEventListener("drop", e => { if (!dragged(e)) return; e.preventDefault(); drop(false); addFiles(e.dataTransfer.files); });

  // ---- wiring
  $("bell").onclick = openApprovals;
  $("mute").onclick = () => setMuted(!muted);
  $("panelbtn").onclick = () => { if (D.view !== "chat") D.go("chat"); setPanel(document.body.classList.contains("panel-closed")); };
  $("newchat").onclick = newChat;
  $("chatsearch").addEventListener("input", refreshChats);
  document.addEventListener("keydown", e => {
    if (e.ctrlKey && e.shiftKey && e.key.toLowerCase() === "o") { e.preventDefault(); newChat(); }
  });
  D.views.chat = {show() { titleBar(); }};

  D.chat = {
    start() {
      setMuted(muted);
      const saved = D.pref.get("delta_drawer");
      setPanel(saved ? saved === "open" : innerWidth >= 1100, !saved);
      renderProgress(); renderUsage(); updateBell(); greet(); bindExamples();
      checkInterrupted();
      if (current) openChat(current); else refreshChats();
      api("/api/approvals").then(a => a.forEach(renderApproval)).catch(() => {});
    },
    newChat, openChat, setMuted, get muted() { return muted; }, put, note, refreshChats, showHero,
    get chatId() { return current; },
  };
})();
