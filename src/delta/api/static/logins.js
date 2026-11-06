// Settings > Logins and watchers. A password is sent once to this PC's Delta and never comes back.
(() => {
  "use strict";
  const D = window.Delta, {$, api, h, btn} = D;
  const say = (id, ok, t) => { $(id).className = "msg " + (ok ? "ok" : "err"); $(id).textContent = t; };
  const err = e => ({detail: D.plain(e)});
  const det = r => typeof r.detail === "string" ? r.detail : "Please check the values.";
  async function load() {
    const r = await api("/api/logins").catch(() => null);
    if (!r) return;
    $("lg_list").replaceChildren(...r.logins.map(l => {
      const sel = h("select", {"aria-label": "Permission for " + l.name}, ...["ask", "always"].map(v =>
        h("option", {value: v, selected: l.permission === v, text: v === "ask" ? "Ask every time" : "Always allow"})));
      sel.onchange = () => api(`/api/logins/${l.id}/permission`, {method: "PUT", body: JSON.stringify({permission: sel.value})});
      return h("div", {class: "card"}, h("div", {class: "card-top"}, h("b", {text: l.name}), h("span", {class: "pill" + (l.insecure ? " warn" : ""), text: l.origin})),
        h("small", {class: "muted", text: (l.username || "no user name") + (l.last_used ? ", last used " + l.last_used + " (" + l.last_outcome + ")" : "")}),
        h("div", {class: "inline-form"}, sel, btn("Test", async () => { const t = await api(`/api/logins/${l.id}/test`, {method: "POST"}).catch(err); D.toast({title: l.name, body: t.message || det(t), kind: t.ok ? "ok" : "warn"}); load(); }),
          btn("Remove", async () => { if (confirm("Remove " + l.name + "?")) { await api(`/api/logins/${l.id}`, {method: "DELETE"}); load(); } })));
    }));
    if (!r.vault) say("lg_msg", false, "Windows DPAPI is not available, so logins cannot be saved. Repair Delta with the installer.");
    $("wt_login").replaceChildren(...r.logins.map(l => h("option", {value: l.id, text: l.name})));
    const w = await api("/api/watchers").catch(() => null);
    if (!w) return;
    $("wt_list").replaceChildren(...w.watchers.map(x => h("div", {class: "card"},
      h("div", {class: "card-top"}, h("b", {text: x.name}), h("span", {class: "pill", text: x.last_status || "not checked yet"})),
      h("small", {class: "muted", text: `${x.what}; every ${Math.round(x.interval_minutes / 60)} h; ${x.last_message || ""}`}),
      h("div", {class: "inline-form"}, btn("Check now", async () => { const t = await api(`/api/watchers/${x.id}/run`, {method: "POST"}).catch(err); D.toast({title: x.name, body: t.message || det(t), kind: t.ok ? "ok" : "warn"}); load(); }),
        btn("Remove", async () => { if (confirm("Stop watching " + x.name + "?")) { await api(`/api/watchers/${x.id}`, {method: "DELETE"}); load(); } })))),
      ...w.offers.map(o => { const when = h("input", {type: "datetime-local", "aria-label": "Remind me at"});
        return h("div", {class: "card"}, h("small", {text: o.section + ": " + o.line}), h("div", {class: "inline-form"}, when,
          btn("Add reminder", async () => { const t = await api(`/api/watchers/offers/${o.id}`, {method: "POST", body: JSON.stringify({when: when.value})}).catch(err); D.toast({title: t.ok ? "Reminder added" : "Not added", body: t.ok ? "" : det(t), kind: t.ok ? "ok" : "warn"}); load(); }),
          btn("Dismiss", async () => { await api(`/api/watchers/offers/${o.id}`, {method: "POST", body: JSON.stringify({dismiss: true})}); load(); }))); }));
  }
  $("lg_add").onclick = async () => {
    const body = {name: $("lg_name").value.trim(), site: $("lg_site").value.trim(), username: $("lg_user").value.trim(), password: $("lg_pass").value,
      login_url: $("lg_url").value.trim(), permission: $("lg_perm").value, allow_http: $("lg_http").checked};
    const r = await api("/api/logins", {method: "POST", body: JSON.stringify(body)}).catch(err);
    $("lg_pass").value = "";
    say("lg_msg", r.ok, r.ok ? r.message : det(r));
    if (r.ok) { for (const id of ["lg_name", "lg_site", "lg_user", "lg_url"]) $(id).value = ""; load(); }
  };
  $("wt_add").onclick = async () => {
    const body = {login: $("wt_login").value, what: $("wt_what").value.split(",").map(s => s.trim()).filter(Boolean),
      every_hours: Number($("wt_hours").value) || 6, notify: $("wt_digest").checked ? "digest" : "instant"};
    const r = await api("/api/watchers", {method: "POST", body: JSON.stringify(body)}).catch(err);
    say("wt_msg", r.ok, r.ok ? "Watching. The first check runs in a minute." : det(r));
    if (r.ok) load();
  };
  const prev = D.views.settings.show;
  D.views.settings.show = o => { load(); return prev(o); };
})();
