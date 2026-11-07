// First-run setup: an AI model (a free key or an AI app already on this PC), how careful Delta should be, Telegram on
// the phone (optional) and a name. Opens by itself until setup is complete; Settings can open it again.
(() => {
  "use strict";
  const D = window.Delta, {$, api, h} = D;
  const STEPS = ["welcome", "models", "trust", "telegram", "name", "done"];
  const NAMES = ["Welcome", "AI model", "Permissions", "Phone", "Name", "Done"];
  let step = 0;
  const dlg = $("setup");

  function showStep(i) {
    step = Math.max(0, Math.min(STEPS.length - 1, i));
    for (const s of dlg.querySelectorAll("section[data-step]")) s.hidden = s.dataset.step !== STEPS[step];
    if (STEPS[step] === "telegram") tgSteps();
    $("dots").replaceChildren(...STEPS.map((_, j) => h("li", {class: j < step ? "done" : j === step ? "on" : "", "aria-current": j === step ? "step" : null, title: NAMES[j]},
      h("span", {class: "sr-only", text: `Step ${j + 1}: ${NAMES[j]}`}))));
    $("wback").hidden = step === 0 || step === STEPS.length - 1;
    $("wnext").textContent = step === STEPS.length - 2 ? "Finish" : step === STEPS.length - 1 ? "Start using Delta" : step === STEPS.indexOf("telegram") ? "Next (or skip)" : "Next";
    $("werr").textContent = "";
    dlg.querySelector(".wiz-body").scrollTop = 0;
    dlg.querySelector(`section[data-step="${STEPS[step]}"] h2`)?.focus?.();
  }
  function renderSetup(s) {
    const hw = s.hardware;
    $("hw").textContent = `This PC: ${hw.ram_gb} GB memory (${hw.ram_free_gb} GB free)` + (hw.gpu ? `, ${hw.gpu} with ${hw.vram_gb} GB` : ", no NVIDIA graphics card found") + `.\n\n${hw.recommendation}`;
    $("localbox").textContent = hw.ollama_running ? "A local model server (Ollama or LM Studio) is running on this PC: private, free and offline."
      : hw.can_run_local ? "Optional: install Ollama (ollama.com) later to also run a model privately on this PC."
      : "Local models are not recommended on this PC. A cloud key is all you need.";
    for (const row of dlg.querySelectorAll(".keyrow")) {
      const k = s.keys[row.dataset.key], msg = row.querySelector(".msg");
      if (k && k.set && !msg.textContent) { msg.className = "msg ok"; msg.textContent = "Already saved."; }
    }
    const a = dlg.querySelector(`input[name=autonomy][value="${s.autonomy}"]`); if (a) a.checked = true;
    $("ownername").value = $("ownername").value || s.owner_name || "";
    $("tgid").value = $("tgid").value || (s.telegram.user_ids || []).join(", ");
    $("tgon").checked = $("tgon").checked || s.telegram.enabled;
    autoRow();
  }
  // With Telegram on, offer to start Delta at sign-in (off unless ticked): the bot only answers while Delta runs.
  let autoKnown = false, autoSupported = true;
  async function autoRow() {
    $("tgautorow").hidden = !$("tgon").checked || !autoSupported;
    if (autoKnown || !autoSupported) return;
    const a = await api("/api/autostart").catch(() => null);
    if (!a || !a.supported) { autoSupported = false; $("tgautorow").hidden = true; return; }
    autoKnown = true; $("tgauto").checked = !!a.enabled;
  }
  $("tgon").addEventListener("change", autoRow);
  async function openSetup(at) {
    if (D.renderAIApps) D.renderAIApps($("w_apps"));
    if (!dlg.open) dlg.showModal();
    showStep(Math.max(0, STEPS.indexOf(at || "welcome")));
    const s = await api("/api/setup").catch(() => null);
    if (s) renderSetup(s); else $("hw").textContent = "Delta is not reachable right now.";
  }
  for (const row of dlg.querySelectorAll(".keyrow")) {
    const b = row.querySelector("button"), input = row.querySelector("input"), msg = row.querySelector(".msg");
    b.onclick = async () => {
      if (!input.value.trim()) { input.focus(); return; }
      b.disabled = true; msg.className = "msg"; msg.textContent = "Checking with the service...";
      const r = await api("/api/setup/key", {method: "POST", body: JSON.stringify({name: row.dataset.key, value: input.value})}).catch(() => ({ok: false, message: "Delta is not reachable."}));
      b.disabled = false; msg.className = "msg " + (r.ok ? "ok" : "err"); msg.textContent = r.message || "";
      if (r.ok) { input.value = ""; if (row.dataset.key === "telegram_bot_token") { $("tgon").checked = true; autoRow(); tgSteps(); } D.emit("status:refresh"); }
    };
    input.addEventListener("keydown", e => { if (e.key === "Enter") { e.preventDefault(); b.click(); } });
    // A pasted Telegram token is saved at once: one less button for people new to this.
    if (row.dataset.key === "telegram_bot_token") input.addEventListener("input", () => { if (/^\d{6,}:[\w-]{30,}$/.test(input.value.trim())) b.click(); });
  }
  // Tick off the Telegram steps already done, so people see where they are.
  async function tgSteps() {
    const s = await api("/api/telegram").catch(() => null);
    if (!s) return;
    for (const [id, done] of [["tgs1", s.has_token], ["tgs2", s.has_token], ["tgs3", (s.users || []).length > 0]]) $(id).classList.toggle("done", !!done);
    if ((s.users || []).length) { $("tgcode").className = "msg ok"; $("tgcode").textContent = `Connected${s.bot ? " to @" + s.bot : ""}. Message your bot from your phone; /help lists what it can do.`; }
  }
  // Telegram pairing: Delta shows a one-time code; sending it to the bot adds that phone. No user ids to look up.
  let pairing = 0;
  $("tgpair").onclick = async () => {
    const out = $("tgcode"), b = $("tgpair"), mine = ++pairing;
    b.disabled = true; out.className = "msg"; out.textContent = "Connecting to Telegram...";
    const before = ((await api("/api/telegram").catch(() => ({}))).users || []).length;
    const r = await api("/api/telegram/pair", {method: "POST"}).catch(e => ({detail: D.plain(e)}));
    b.disabled = false;
    if (!r.code) { out.className = "msg err"; out.textContent = D.detail(r) || "Could not connect."; return; }
    out.replaceChildren(h("div", {class: "pair-code mono", text: r.code}),
      h("div", {}, `On your phone, open @${r.bot} in Telegram and send this code (valid 10 minutes). `,
        r.link ? h("a", {href: r.link, target: "_blank", rel: "noopener noreferrer", text: "Or open it with the code filled in"}) : null));
    for (let i = 0; i < 300 && mine === pairing; i++) {  // up to 10 minutes
      await new Promise(res => setTimeout(res, 2000));
      if (!dlg.open) return;
      const s = await api("/api/telegram").catch(() => null);
      if (s && (s.users || []).length > before) {
        out.className = "msg ok"; out.textContent = `Connected. Message @${s.bot} from your phone; approvals arrive there too.`;
        $("tgid").value = s.users.join(", "); $("tgon").checked = true; autoRow(); tgSteps(); D.settings.tgCard(); return;
      }
    }
    if (mine === pairing) { out.className = "msg err"; out.textContent = "The code expired. Press Connect my phone again."; }
  };
  $("wback").onclick = () => showStep(step - 1);
  $("wnext").onclick = async () => {
    const name = STEPS[step];
    if (name === "models") {
      const s = await api("/api/setup").catch(() => null);
      if (!s) { $("werr").textContent = "Delta is not reachable right now."; return; }
      renderSetup(s);
      if (!s.models_ready.length) { $("werr").textContent = "Add at least one key (or start Ollama) so Delta can think."; return; }
    }
    if (name === "name") {
      const ids = $("tgid").value.split(/[ ,]+/).filter(Boolean);
      if ($("tgon").checked && !ids.length) { showStep(STEPS.indexOf("telegram")); $("werr").textContent = "Add your Telegram user id, or turn Telegram off."; return; }
      const body = {owner_name: $("ownername").value, autonomy: dlg.querySelector("input[name=autonomy]:checked").value, telegram_enabled: $("tgon").checked, telegram_user_ids: ids};
      if ($("tgon").checked && autoKnown) body.start_with_windows = $("tgauto").checked;
      $("wnext").disabled = true;
      const r = await api("/api/setup/complete", {method: "POST", body: JSON.stringify(body)}).catch(e => ({error: D.plain(e)}));
      $("wnext").disabled = false;
      if (!r.ok) { $("werr").textContent = "Could not save: " + (D.detail(r) || r.error || "unknown error"); return; }
      const level = {"ask-everything": "Careful", "ask-risky": "Balanced", "trusted": "Trusted", "autopilot": "Autopilot"}[body.autonomy];
      $("summary").textContent = `AI models ready: ${r.models_ready.join(", ") || "none"}\nPermission level: ${level}\nTelegram: ${body.telegram_enabled ? "on" : "off"}` +
        (r.start_with_windows === true ? "\nStarts when you sign in to Windows: yes" : r.start_with_windows === false ? "\nStarts when you sign in to Windows: no" : "") +
        (r.restart_needed ? "\n\nRestart Delta (Shut down, then open it again) to connect Telegram." : "");
    }
    if (name === "done") { dlg.close(); D.emit("status:refresh"); D.emit("setup:done"); return; }
    showStep(step + 1);
  };
  $("wclose").onclick = () => dlg.close();
  dlg.addEventListener("close", () => { pairing++; D.emit("status:refresh"); });
  D.openSetup = openSetup;
  D.setup = {start() { api("/api/setup").then(s => { if (!s.completed) openSetup(); }).catch(() => {}); }};
})();
