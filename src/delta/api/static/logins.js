// Settings > Logins and watchers. A password is sent once to this PC's Delta and never comes back.
(() => {
  "use strict";
  const D = window.Delta, {$, api, h, btn} = D;
  const say = (id, ok, t) => { $(id).className = "msg " + (ok ? "ok" : "err"); $(id).textContent = t; };
  const err = e => ({detail: D.plain(e)});
  const det = r => typeof r.detail === "string" ? r.detail : "Please check the values.";
  const send = (url, method, body) => api(url, {method, ...(body ? {body: JSON.stringify(body)} : {})});
  // A button that calls the API, shows the answer as a toast and reloads the lists
  const act = (label, url, title, body, ok) => btn(label, async () => {
    const t = await send(url, "POST", body && body()).catch(err);
    D.toast(ok ? ok(t) : {title, body: t.message || det(t), kind: t.ok ? "ok" : "warn"}); load(); });
  const rm = (url, ask) => btn("Remove", async () => { if (confirm(ask)) { await send(url, "DELETE"); load(); } });
  const card = (name, pill, warn, note, ...actions) => h("div", {class: "card"},
    h("div", {class: "card-top"}, h("b", {text: name}), h("span", {class: "pill" + (warn ? " warn" : ""), text: pill})),
    h("small", {class: "muted", text: note}), h("div", {class: "inline-form"}, ...actions));
  async function load() {
    const r = await api("/api/logins").catch(() => null);
    if (!r) return;
    $("lg_list").replaceChildren(...r.logins.map(l => {
      const sel = h("select", {"aria-label": "Permission for " + l.name}, ...["ask", "always"].map(v =>
        h("option", {value: v, selected: l.permission === v, text: v === "ask" ? "Ask every time" : "Always allow"})));
      sel.onchange = () => send(`/api/logins/${l.id}/permission`, "PUT", {permission: sel.value});
      return card(l.name, l.origin, l.insecure,
        (l.username || "no user name") + (l.last_used ? ", last used " + l.last_used + " (" + l.last_outcome + ")" : ""),
        sel, act("Test", `/api/logins/${l.id}/test`, l.name), rm(`/api/logins/${l.id}`, "Remove " + l.name + "?"));
    }));
    if (!r.vault) say("lg_msg", false, "Windows DPAPI is not available, so logins cannot be saved. Repair Delta with the installer.");
    $("wt_login").replaceChildren(...r.logins.map(l => h("option", {value: l.id, text: l.name})));
    const w = await api("/api/watchers").catch(() => null);
    if (!w) return;
    $("wt_list").replaceChildren(...w.watchers.map(x => card(x.name, x.last_status || "not checked yet", false,
      `${x.what}; every ${Math.round(x.interval_minutes / 60)} h; ${x.last_message || ""}`,
      act("Check now", `/api/watchers/${x.id}/run`, x.name), rm(`/api/watchers/${x.id}`, "Stop watching " + x.name + "?"))),
      ...w.offers.map(o => { const when = h("input", {type: "datetime-local", "aria-label": "Remind me at"}), url = `/api/watchers/offers/${o.id}`;
        return h("div", {class: "card"}, h("small", {text: o.section + ": " + o.line}), h("div", {class: "inline-form"}, when,
          act("Add reminder", url, "", () => ({when: when.value}),
            t => ({title: t.ok ? "Reminder added" : "Not added", body: t.ok ? "" : det(t), kind: t.ok ? "ok" : "warn"})),
          btn("Dismiss", async () => { await send(url, "POST", {dismiss: true}); load(); }))); }));
  }
  $("lg_add").onclick = async () => {
    const v = id => $(id).value.trim();
    const body = {name: v("lg_name"), site: v("lg_site"), username: v("lg_user"), password: $("lg_pass").value,
      login_url: v("lg_url"), permission: $("lg_perm").value, allow_http: $("lg_http").checked};
    const r = await send("/api/logins", "POST", body).catch(err);
    $("lg_pass").value = "";
    say("lg_msg", r.ok, r.ok ? r.message : det(r));
    if (r.ok) { for (const id of ["lg_name", "lg_site", "lg_user", "lg_url"]) $(id).value = ""; load(); }
  };
  $("wt_add").onclick = async () => {
    const body = {login: $("wt_login").value, what: $("wt_what").value.split(",").map(s => s.trim()).filter(Boolean),
      every_hours: Number($("wt_hours").value) || 6, notify: $("wt_digest").checked ? "digest" : "instant"};
    const r = await send("/api/watchers", "POST", body).catch(err);
    say("wt_msg", r.ok, r.ok ? "Watching. The first check runs in a minute." : det(r));
    if (r.ok) load();
  };
  const prev = D.views.settings.show;
  D.views.settings.show = o => { load(); return prev(o); };
})();
