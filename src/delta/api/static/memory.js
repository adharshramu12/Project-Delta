// Memory view: what Delta remembers (with where each memory came from and Forget), what it learned from your
// corrections and choices (Keep / Undo), the skills it learned, and lessons still being tested (Use it now / Reject).
(() => {
  "use strict";
  const D = window.Delta, {$, api, h, icon, btn} = D;
  let tab = "mem";
  const stale = {mem: true, learned: true, skills: true, cands: true};
  const empty = (box, ic, title, body) => box.replaceChildren(D.empty({ic, title, body}));
  const count = (id, n) => { $(id).textContent = n ? String(n) : ""; };

  // ---- memory
  let items = [];
  async function refreshMemory() {
    const box = $("memory");
    if (stale.mem && !items.length) box.replaceChildren(D.skeleton(4));
    const r = await api("/api/memory").catch(e => ({error: e}));
    if (!Array.isArray(r)) { box.replaceChildren(D.failed(r.error ? D.plain(r.error) : "Memory could not be loaded.", refreshMemory)); return; }
    items = r; stale.mem = false; count("memcount", items.length);
    drawMemory();
  }
  function drawMemory() {
    const box = $("memory"), q = $("memsearch").value.trim().toLowerCase(), trust = $("memtrust").value;
    const shown = items.filter(m => (!trust || m.trust === trust) && (!q || (m.content || "").toLowerCase().includes(q)));
    $("forget_outside").hidden = !items.some(m => m.trust === "untrusted");
    if (!items.length) { empty(box, "memory", "Delta has not remembered anything yet", 'Tell it something to keep, like "I prefer short answers", or add it above.'); return; }
    if (!shown.length) { empty(box, "search", "Nothing matches", "Try another word or source."); return; }
    box.replaceChildren();
    D.paged(box, shown, m => {
      const outside = m.trust === "untrusted";
      // Where each memory came from: notes from web pages, files or messages are kept as data only.
      return h("div", {class: "mrow", role: "listitem"},
        h("div", {class: "mrow-main"},
          h("p", {text: m.content}),
          h("div", {class: "mrow-meta"}, m.kind === "episode" ? h("span", {class: "pill", text: "Task"}) : null,
            outside ? h("span", {class: "pill warn", text: "From outside: data only"}) : null,
            h("small", {class: "muted", text: m.provenance || ""}))),
        h("button", {type: "button", class: "btn sm", title: "Forget this", onclick: async e => {
          e.currentTarget.disabled = true;
          await api(`/api/memory/${encodeURIComponent(m.id)}`, {method: "DELETE"}).catch(() => {});
          D.toast({title: "Forgotten", body: m.content.slice(0, 120), kind: "info"}); refreshMemory();
        }}, "Forget"));
    }, 50);
  }
  $("addmem").onsubmit = async e => {
    e.preventDefault(); const t = $("memtext").value.trim(); if (!t) return; $("memtext").value = "";
    const r = await api("/api/memory", {method: "POST", body: JSON.stringify({content: t})}).catch(err => ({error: err}));
    if (r.error || !r.id) D.toast({title: "Not saved", body: r.error ? D.plain(r.error) : "Nothing was left to save after removing secrets.", kind: "warn"});
    else D.toast({title: "Delta will remember that", kind: "ok"});
    refreshMemory();
  };
  let ft = 0;
  $("memsearch").addEventListener("input", () => { clearTimeout(ft); ft = setTimeout(drawMemory, 150); });
  $("memtrust").addEventListener("change", drawMemory);
  $("forget_outside").onclick = async () => {
    if (!await D.confirm({title: "Forget everything from outside content?", body: "Notes Delta took from web pages, files and messages are deleted. What you told it stays.", ok: "Forget them", danger: true})) return;
    const r = await api("/api/memory/forget", {method: "POST", body: JSON.stringify({trust: "untrusted"})}).catch(() => ({}));
    D.toast({title: `Forgot ${r.forgotten || 0} ${r.forgotten === 1 ? "note" : "notes"}`, kind: "info"}); refreshMemory();
  };

  // ---- learned from corrections and choices
  const KIND = {preference: "Your instruction", avoid: "Won't do again", trust: "Suggestion", lesson: "Lesson"};
  function actionRow(buttons) { return h("div", {class: "row"}, buttons); }
  async function refreshLearned() {
    const r = await api("/api/learned").catch(() => null);
    if (!Array.isArray(r)) { if (tab === "learned") $("learned").replaceChildren(D.failed("Could not load what Delta learned.", refreshLearned)); return; }
    const list = r.filter(i => i.status !== "undone").slice(0, 200);
    const waiting = list.filter(i => i.status === "pending").length;
    count("learnedcount", list.length);
    const b = $("nb_memory"); b.hidden = !waiting; b.textContent = String(waiting); b.title = waiting ? `${waiting} waiting for your OK` : "";
    stale.learned = false;
    if (!list.length) { empty($("learned"), "spark", "Nothing learned yet", 'Corrections like "keep it short" show up here.'); return; }
    $("learned").replaceChildren(...list.map(i => {
      const act = (label, action, cls) => btn(label, () => api(`/api/learned/${encodeURIComponent(i.id)}`, {method: "POST", body: JSON.stringify({action})}).then(refreshLearned).catch(() => {}), cls);
      return h("div", {class: "mrow" + (i.status === "pending" ? " pending" : ""), role: "listitem"},
        h("div", {class: "mrow-main"}, h("div", {class: "mrow-meta"}, h("span", {class: "pill " + (i.status === "pending" ? "warn" : ""), text: KIND[i.kind] || i.kind}),
          i.status === "pending" ? h("small", {class: "muted", text: "Needs your OK"}) : null), h("p", {text: i.content})),
        actionRow([i.status === "pending" ? act("Keep", "keep", "primary sm") : null, act(i.status === "pending" ? "No thanks" : "Undo", "undo", "sm")]));
    }));
  }

  // ---- skills
  async function refreshSkills() {
    const r = await api("/api/skills").catch(() => null);
    if (!Array.isArray(r)) { if (tab === "skills") $("skills").replaceChildren(D.failed("Could not load skills.", refreshSkills)); return; }
    const live = r.filter(s => s.status !== "retired");
    count("skillcount", live.length);
    stale.skills = false;
    if (!r.length) { empty($("skills"), "spark", "No skills yet", "Delta learns a skill each time it verifiably finishes a multi-step task."); return; }
    const label = {candidate: "Learning", active: "Proven", retired: "Retired"};
    $("skills").replaceChildren(...r.map(s => h("div", {class: "mrow", role: "listitem"},
      h("div", {class: "mrow-main"}, h("div", {class: "mrow-meta"}, h("b", {text: s.name}), h("span", {class: "pill " + (s.status === "active" ? "ok" : ""), text: label[s.status] || s.status})),
        h("p", {class: "small", text: s.when_to_use}), h("small", {class: "muted", text: `Worked ${s.successes} ${s.successes === 1 ? "time" : "times"}, failed ${s.failures}`})),
      btn("Forget skill", async () => {
        if (!await D.confirm({title: `Forget the skill "${s.name}"?`, body: "Delta learns it again if it finishes the same kind of task later.", ok: "Forget", danger: true})) return;
        await api(`/api/skills/${encodeURIComponent(s.id)}`, {method: "DELETE"}).catch(() => {}); refreshSkills();
      }, "sm"))));
  }

  // ---- lessons and repaired steps being tested
  async function refreshCandidates() {
    const r = await api("/api/learn").catch(() => null);
    if (!Array.isArray(r)) { if (tab === "cands") $("candidates").replaceChildren(D.failed("Could not load lessons.", refreshCandidates)); return; }
    const list = r.filter(i => i.status === "candidate" || i.status === "active").slice(0, 100);
    count("candcount", list.filter(i => i.status === "candidate").length);
    stale.cands = false;
    if (!list.length) { empty($("candidates"), "spark", "Nothing being tested", "Lessons from recoveries and repaired skill steps wait here until they have worked a few times."); return; }
    $("candidates").replaceChildren(...list.map(i => {
      const act = (label, action, cls) => btn(label, () => api(`/api/learn/${encodeURIComponent(i.ref)}`, {method: "POST", body: JSON.stringify({action})}).then(refreshCandidates).catch(() => {}), cls);
      const outside = i.trust === "untrusted";
      return h("div", {class: "mrow", role: "listitem"},
        h("div", {class: "mrow-main"}, h("div", {class: "mrow-meta"}, h("span", {class: "pill", text: i.kind === "recipe" ? "Repaired step" : "Lesson"}),
          h("span", {class: "pill " + (i.status === "candidate" ? "warn" : "ok"), text: i.status === "candidate" ? "Waiting" : "In use"}),
          h("small", {class: "muted", text: `${i.ref} · worked ${i.successes}, failed ${i.failures}` + (outside ? " · from outside content: never used" : "")})),
          h("p", {text: i.summary})),
        actionRow([i.status === "candidate" && !outside ? act("Use it now", "promote", "primary sm") : null, act("Reject", "reject", "sm")]));
    }));
  }

  // ---- tabs
  const LOAD = {mem: refreshMemory, learned: refreshLearned, skills: refreshSkills, cands: refreshCandidates};
  function setTab(name) {
    tab = name;
    for (const t of document.querySelectorAll(".tabs .tab")) t.setAttribute("aria-selected", String(t.dataset.tab === name));
    for (const p of document.querySelectorAll(".tabpanel")) p.hidden = p.dataset.panel !== name;
    LOAD[name]();
  }
  const tabs = [...document.querySelectorAll(".tabs .tab")];
  for (const t of tabs) {
    t.addEventListener("click", () => setTab(t.dataset.tab));
    t.addEventListener("keydown", e => {
      const i = tabs.indexOf(t), j = e.key === "ArrowRight" ? (i + 1) % tabs.length : e.key === "ArrowLeft" ? (i + tabs.length - 1) % tabs.length : -1;
      if (j >= 0) { e.preventDefault(); tabs[j].focus(); setTab(tabs[j].dataset.tab); }
    });
  }
  const changed = (key, fn) => () => { stale[key] = true; if (D.view === "memory" && tab === key) fn(); };
  D.on("memory:changed", changed("mem", refreshMemory));
  D.on("skills:changed", changed("skills", refreshSkills));
  D.on("learned:changed", () => { refreshLearned(); refreshCandidates(); });
  D.views.memory = {show(o) { const t = o.tab || tab; for (const key of Object.keys(LOAD)) if (key !== t) LOAD[key](); setTab(t); }};
  D.memory = {start() { refreshLearned(); }};
})();
