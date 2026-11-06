// Settings view: permission level, quiet hours, browser, folders, blocked apps and sites, learning, the remote PIN,
// emergency contacts, notifications, appearance, add-ons and keys. Save applies everything on the form at once;
// the PIN, contacts and sign-in have their own buttons because they act right away.
(() => {
  "use strict";
  const D = window.Delta, {$, api, h, btn} = D;
  const lines = id => $(id).value.split("\n").map(s => s.trim()).filter(Boolean);
  let dirty = false, loaded = null;
  const say = (id, ok, text) => { $(id).className = "msg " + (ok ? "ok" : "err"); $(id).textContent = text; };

  // ---- emergency contacts
  function sosRow(c) {
    const n = h("input", {placeholder: "Name, for example Amma", maxlength: "60", "aria-label": "Contact name", value: c.name || ""});
    const w = h("input", {placeholder: "Name in WhatsApp or phone number", maxlength: "60", "aria-label": "WhatsApp name or number", value: c.whatsapp || ""});
    const r = h("input", {placeholder: "Relation", maxlength: "40", "aria-label": "Relation", value: c.relation || ""});
    const row = h("div", {class: "sosrow"}, n, w, r);
    row.append(h("button", {type: "button", class: "icon-btn sm", "aria-label": "Remove contact", title: "Remove", onclick: () => row.remove()}, D.icon("x")));
    $("sos_list").append(row);
  }
  async function sosCard() {
    const r = await api("/api/emergency").catch(() => ({contacts: []}));
    $("sos_list").replaceChildren();
    (r.contacts || []).forEach(sosRow);
    if (!(r.contacts || []).length) sosRow({});
  }
  $("sos_add").onclick = () => { if ($("sos_list").children.length < 5) sosRow({}); else say("sos_msg", false, "Up to 5 contacts."); };
  $("sos_save").onclick = async () => {
    const contacts = [...document.querySelectorAll(".sosrow")].map(d => {
      const [n, w, r] = d.querySelectorAll("input"); return {name: n.value.trim(), whatsapp: w.value.trim(), relation: r.value.trim()};
    }).filter(c => c.name && c.whatsapp);
    const r = await api("/api/emergency", {method: "PUT", body: JSON.stringify({contacts})}).catch(e => ({detail: D.plain(e)}));
    say("sos_msg", r.ok, r.ok ? r.message : "Could not save: each contact needs a name and a WhatsApp name or number.");
  };

  // ---- remote PIN and Telegram
  async function guardCard() {
    const s = await api("/api/security").catch(() => null); if (!s) return;
    $("g_state").className = "pill " + (s.locked ? "danger" : s.has_pin ? "ok" : "warn");
    $("g_state").textContent = s.locked ? `Locked: ${s.reason}` : s.has_pin ? "PIN set, protecting phone control" : "No PIN: sensitive phone actions are blocked";
    $("g_save").textContent = s.has_pin ? "Change PIN" : "Set PIN";
    $("g_unlock").hidden = !s.locked; $("g_lock").hidden = s.locked;
    D.emit("security", s);
  }
  $("g_save").onclick = async () => {
    const r = await api("/api/security/pin", {method: "PUT", body: JSON.stringify({pin: $("g_pin").value.trim()})}).catch(e => ({detail: D.plain(e)}));
    say("g_msg", r.ok, r.ok ? r.message : (typeof r.detail === "string" ? r.detail : "Use 4 to 8 digits."));
    if (r.ok) $("g_pin").value = "";
    guardCard();
  };
  const lockAct = path => async () => { const r = await api(path, {method: "POST"}).catch(e => ({message: D.plain(e)})); say("g_msg", true, r.message || "Done."); guardCard(); };
  $("g_unlock").onclick = lockAct("/api/security/unlock");
  $("g_lock").onclick = lockAct("/api/security/lock");
  async function tgCard() {
    const s = await api("/api/telegram").catch(() => null); if (!s) return;
    const on = (s.users || []).length > 0;
    $("tg_state").className = "pill " + (on ? "ok" : "");
    $("tg_state").textContent = on ? `Connected${s.bot ? " to @" + s.bot : ""}` : s.has_token ? "Bot added, phone not connected" : "Not set up";
    $("tg_setup").textContent = on ? "Telegram settings" : "Set up Telegram";
  }
  $("tg_setup").onclick = () => D.openSetup("telegram");

  // ---- browser
  function renderBrowsers(b) {
    const box = $("s_browsers");
    box.replaceChildren();
    for (const o of b.options || []) {
      const i = h("input", {type: "radio", name: "s_browser", value: o.id, checked: !!o.current, disabled: !o.available});
      i.onchange = () => { $("s_engine_row").hidden = i.value !== "private"; };
      box.append(h("label", {class: "choice" + (o.available ? "" : " unavailable")}, i,
        h("span", {}, h("b", {text: o.label}), h("small", {text: o.available ? o.help + (o.uses ? " " + o.uses : "") : "Not available: " + o.reason}))));
    }
    $("s_engine").replaceChildren(...(b.engines || []).map(e => h("option", {value: e.id, disabled: !e.available, selected: !!e.current, text: e.label + (e.available ? "" : " (not installed)")})));
    $("s_engine_row").hidden = b.choice !== "private";
    $("s_signin").disabled = !b.signin_available;
    $("s_bmsg").className = "msg"; $("s_bmsg").textContent = b.running_note || (b.signin_available ? "" : "Delta's private browser is not available on this PC.");
  }
  $("s_signin").onclick = async () => {
    $("s_bmsg").className = "msg"; $("s_bmsg").textContent = "Opening Delta's private browser on this PC...";
    const r = await api("/api/browser/signin", {method: "POST", body: JSON.stringify({url: $("s_signin_url").value.trim()})}).catch(e => ({message: D.plain(e)}));
    say("s_bmsg", r.ok, r.message || "");
  };

  // ---- load and save
  function fill(s) {
    const b = s.boundaries;
    renderBrowsers(s.browser); $("s_orange").checked = s.browser.orange_cursor;
    const lvl = document.querySelector(`input[name=s_autonomy][value="${b.autonomy}"]`); if (lvl) lvl.checked = true;
    $("s_quiet").checked = b.quiet_hours.enabled; $("s_qstart").value = b.quiet_hours.start; $("s_qend").value = b.quiet_hours.end;
    $("s_allowed").value = b.allowed_paths.join("\n"); $("s_denied").value = b.denied_paths.join("\n");
    $("s_apps").value = b.denied_apps.join("\n"); $("s_domains").value = b.denied_domains.join("\n");
    $("s_onlydomains").value = b.allowed_domains.join("\n");
    $("s_savedir").value = s.save_dir; $("s_savedir").title = s.save_dir_resolved;
    $("s_protected").textContent = "Always protected, whatever you set: Windows system files, saved passwords and keys, and Delta's own data folder (so Delta can never change its own rules). New files currently go to: " + s.save_dir_resolved;
    const a = s.attachments || {};
    $("s_vision").replaceChildren(...[["auto", "Any AI I have that can see pictures"], ["off", "Off: use only the text in pictures"],
      ...(a.providers || []).map(p => [p, "Only " + p + ", for pictures"])].map(([v, t]) => h("option", {value: v, text: t, selected: v === (a.vision || "auto")})));
    $("s_vmodel").value = a.vision_model || "";
    $("s_vnote").textContent = a.can_see_pictures ? "An AI that can see pictures is ready." : "No AI here can see pictures, so Delta uses the text in them and says so.";
    $("s_mem").checked = s.memory.enabled; $("s_extract").checked = s.memory.auto_extract; $("s_skills").checked = s.memory.learn_skills;
    $("s_addons").replaceChildren(...(s.addons.available.length ? s.addons.available.map(a => h("label", {class: "check"},
      h("input", {type: "checkbox", dataset: {addon: a}, checked: s.addons.enabled.includes(a)}), " ", a))
      : [h("p", {class: "muted small", text: "No add-ons installed."})]));
    $("s_keys").replaceChildren(...Object.entries(s.keys).map(([name, k]) => h("div", {class: "krow"}, h("span", {text: k.label}),
      k.set ? h("span", {class: "row"}, h("span", {class: "pill ok", text: "Saved"}), btn("Remove", async () => {
        if (!await D.confirm({title: `Remove the ${k.label} key from this PC?`, body: "Delta stops using it right away.", ok: "Remove key", danger: true})) return;
        await api(`/api/settings/key/${encodeURIComponent(name)}`, {method: "DELETE"}).catch(() => {}); load(); D.emit("status:refresh");
      }, "sm")) : btn("Add in setup", () => D.openSetup(name === "telegram_bot_token" ? "telegram" : "models"), "sm"))));
    setDirty(false);
  }
  async function load() {
    $("settingserr").hidden = true;
    guardCard(); sosCard(); tgCard(); notifyState();
    const s = await api("/api/settings").catch(e => ({error: e}));
    if (!s || !s.boundaries) { $("settingserr").hidden = false; $("settingserr").textContent = "Settings could not be loaded. " + (s.error ? D.plain(s.error) : ""); return; }
    loaded = s; fill(s);
  }
  function setDirty(v) {
    dirty = v;
    document.querySelector(".savebar").classList.toggle("dirty", v);
    if (v) { $("s_err").className = "msg"; $("s_err").textContent = "You have unsaved changes."; }
    else if ($("s_err").textContent === "You have unsaved changes.") $("s_err").textContent = "";
  }
  const own = el => el.closest("#s_guard, #s_sos, #set_notify, #set_look") || el.id === "s_signin_url";
  $("settingsbody").addEventListener("input", e => { if (!own(e.target)) setDirty(true); });
  $("settingsbody").addEventListener("change", e => { if (!own(e.target)) setDirty(true); });
  $("s_reset").onclick = () => { if (loaded) fill(loaded); $("s_err").textContent = ""; };
  $("s_save").onclick = async () => {
    const picked = document.querySelector("input[name=s_browser]:checked");
    const browser = {orange_cursor: $("s_orange").checked};
    if (picked) { browser.choice = picked.value; if (picked.value === "private" && $("s_engine").value) browser.engine = $("s_engine").value; }
    const body = {
      browser,
      boundaries: {autonomy: (document.querySelector("input[name=s_autonomy]:checked") || {}).value,
        allowed_paths: lines("s_allowed"), denied_paths: lines("s_denied"), denied_apps: lines("s_apps"),
        denied_domains: lines("s_domains"), allowed_domains: lines("s_onlydomains"),
        quiet_hours: {enabled: $("s_quiet").checked, start: $("s_qstart").value || "23:30", end: $("s_qend").value || "07:00"}},
      memory: {enabled: $("s_mem").checked, auto_extract: $("s_extract").checked, learn_skills: $("s_skills").checked},
      save_dir: $("s_savedir").value || "auto",
      addons_enabled: [...document.querySelectorAll("[data-addon]")].filter(i => i.checked).map(i => i.dataset.addon),
      attachments: {vision: $("s_vision").value || "auto", vision_model: $("s_vmodel").value.trim()},
    };
    $("s_save").disabled = true;
    const r = await api("/api/settings", {method: "PUT", body: JSON.stringify(body)}).catch(e => ({detail: D.plain(e)}));
    $("s_save").disabled = false;
    if (!r.ok) { say("s_err", false, typeof r.detail === "string" ? r.detail : "Please check the values."); return; }
    D.pref.set("delta_gs_browser", "1");
    setDirty(false);
    $("s_err").textContent = "";
    D.toast({title: "Settings saved and applied", body: [r.browser_note, r.restart_needed ? "Restart Delta for the add-on change to take effect." : ""].filter(Boolean).join(" "), kind: "ok"});
    D.emit("settings:saved"); load();
  };

  // ---- notifications and appearance (kept in this browser)
  function notifyState() {
    $("n_sound").checked = !D.chat.muted;
    const p = "Notification" in window ? Notification.permission : "unsupported";
    $("n_state").textContent = p === "granted" ? "Desktop notifications are on." : p === "denied" ? "Desktop notifications are blocked for this page in the browser's site settings." : p === "unsupported" ? "This browser cannot show desktop notifications." : "Desktop notifications are off.";
    $("n_allow").hidden = p !== "default";
  }
  $("n_sound").onchange = () => D.chat.setMuted(!$("n_sound").checked);
  $("n_allow").onclick = () => Notification.requestPermission().then(notifyState);
  D.on("mute", () => { $("n_sound").checked = !D.chat.muted; });
  const themeRadios = () => { for (const r of document.querySelectorAll("input[name=theme]")) r.checked = r.value === window.DeltaTheme.mode; };
  for (const r of document.querySelectorAll("input[name=theme]")) r.onchange = () => window.DeltaTheme.set(r.value);
  document.addEventListener("delta-theme", themeRadios);
  themeRadios();

  // ---- section links
  for (const a of document.querySelectorAll(".settings-nav a")) a.addEventListener("click", e => {
    e.preventDefault(); const t = document.querySelector(a.getAttribute("href")); if (t) { t.scrollIntoView({block: "start"}); t.querySelector("input,select,textarea,button")?.focus({preventScroll: true}); }
  });
  $("opensetup").onclick = () => D.openSetup();
  $("shutdown2").onclick = () => D.shutdown();
  window.addEventListener("beforeunload", e => { if (dirty) e.preventDefault(); });

  D.views.settings = {
    show(o) { (dirty ? Promise.resolve(guardCard()) : load()).then(() => { if (o.section) $(o.section)?.scrollIntoView({block: "start"}); }); },
    hide() { if (dirty) D.toast({title: "Settings not saved", body: "Your changes are still on the Settings page. Save them there.", kind: "warn", action: {label: "Open Settings", fn: () => D.go("settings")}}); },
  };
  D.settings = {guardCard, tgCard};
})();
