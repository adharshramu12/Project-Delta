// Update notifications: the banner when a newer Delta is out, Update now (after a confirm; never by itself), What's
// new after an update, and Settings > Updates and help. Notes from GitHub are cleaned on the PC and only ever set
// as text here.
(() => {
  "use strict";
  const D = window.Delta, {$, api, h, icon, btn} = D;
  let info = null;

  function notes(title, text, url, onClose) {
    D.sheet(title, body => body.append(...[
      h("div", {class: "notes", text: text || "Small fixes and improvements."}),
      /^https:\/\/github\.com\//.test(url || "") ? h("p", {}, h("a", {href: url, target: "_blank", rel: "noopener noreferrer", text: "Full notes on GitHub"})) : null].filter(Boolean)), onClose);
  }
  function banner() {
    const box = $("updbanner"), v = info;
    if (!v || !v.show_banner) { box.hidden = true; box.replaceChildren(); return; }
    box.replaceChildren(
      h("div", {class: "upd-main"}, icon("download"), h("div", {},
        h("b", {text: `Delta ${v.latest} is out` + (v.prerelease ? " (beta)" : "")}),
        v.highlights.length ? h("ul", {class: "upd-list"}, v.highlights.slice(0, 3).map(t => h("li", {text: t})))
          : h("div", {class: "muted", text: "A newer version is ready. Your chats and settings stay as they are."}))),
      h("div", {class: "row"},
        v.can_install ? btn("Update now", () => updateNow(false), "primary sm") : h("span", {class: "small", text: "To update, run delta update in PowerShell."}),
        v.notes ? btn("What's new", () => notes(`What's new in Delta ${v.latest}`, v.notes, v.url), "sm") : null,
        btn("Later", () => act("/api/updates/later"), "sm"),
        btn("Skip this version", () => act("/api/updates/skip", {version: v.latest}), "sm")));
    box.hidden = false;
  }
  async function act(path, body) {
    const r = await api(path, {method: "POST", body: JSON.stringify(body || {})}).catch(() => null);
    if (r && "current" in r) { info = r; render(); }
  }
  async function updateNow(stop) {
    const v = info;
    if (!v) return;
    if (!stop && !await D.confirm({title: `Update to Delta ${v.latest}?`, body: "Delta closes, updates and opens again by itself. It takes a few minutes. Your chats, settings and keys are kept.", ok: "Update now"})) return;
    const r = await api("/api/updates/install", {method: "POST", body: JSON.stringify({version: v.latest, stop_tasks: !!stop})}).catch(e => ({detail: D.plain(e)}));
    if (r.busy) {
      if (await D.confirm({title: "A task is running", body: "Stop it and update now? Or press Cancel and update when it ends.", ok: "Stop and update", danger: true})) updateNow(true);
      return;
    }
    if (!r.ok) { D.toast({title: "Could not update", body: D.detail(r) || "Try again later.", kind: "danger", timeout: 12000}); return; }
    D.disconnect();
    document.body.replaceChildren(h("main", {class: "off-screen", role: "status", "aria-live": "polite"}, h("div", {},
      h("span", {class: "hero-mark"}, icon("logo")), h("h1", {text: `Updating to Delta ${r.version}`}),
      h("p", {class: "muted", text: r.message}), h("span", {class: "spinner", "aria-hidden": "true"}))));
  }

  // ---- Settings > Updates and help (applied at once, like start with Windows)
  function card() {
    const v = info;
    if (!v) return;
    $("u_check").checked = v.enabled; $("u_beta").checked = v.channel === "beta"; $("u_tg").checked = v.telegram;
    $("u_beta").disabled = $("u_tg").disabled = !v.enabled;
    $("u_now").disabled = !v.enabled;
    const when = v.checked_at ? ` Last checked ${D.when(v.checked_at)}.` : "";
    $("u_state").textContent = !v.enabled ? `You have Delta ${v.current}. Update checks are off, so Delta never asks GitHub.`
      : v.newer ? `Delta ${v.latest} is out. You have ${v.current}.${when}`
      : v.latest ? `You have the newest version, Delta ${v.current}.${when}`
      : v.error ? `You have Delta ${v.current}. Could not check: ${v.error}`
      : `You have Delta ${v.current}. Not checked yet.`;
  }
  async function save(body) {
    const r = await api("/api/updates/settings", {method: "PUT", body: JSON.stringify(body)}).catch(() => null);
    if (r && "current" in r) info = r; else D.toast({title: "Could not save", kind: "danger"});
    render();
  }
  $("u_check").onchange = () => save({check: $("u_check").checked});
  $("u_beta").onchange = () => save({channel: $("u_beta").checked ? "beta" : "stable"});
  $("u_tg").onchange = () => save({telegram: $("u_tg").checked});
  $("u_now").onclick = async () => {
    $("u_now").disabled = true; $("u_state").textContent = "Checking...";
    const r = await api("/api/updates/check", {method: "POST"}).catch(() => null);
    if (r && "current" in r) info = r;
    render();
    if (r && r.enabled && !r.newer && r.latest) D.toast({title: "You have the newest version", kind: "ok"});
  };

  function render() { banner(); card(); }
  async function refresh() {
    const r = await api("/api/updates").catch(() => null);
    if (r && "current" in r) { info = r; render(); }
    return r;
  }
  D.on("view", v => { if (v === "settings") refresh(); });
  D.updates = {
    async start() {
      const r = await refresh();
      D.every(3600000, refresh);  // the service checks GitHub; this only reads its last answer
      const w = r && r.whats_new;
      if (w) notes(`What's new in Delta ${w.version}`, w.notes || (w.highlights || []).join("\n"), w.url,
        () => api("/api/updates/whats-new/seen", {method: "POST"}).catch(() => {}));
    },
  };
})();
