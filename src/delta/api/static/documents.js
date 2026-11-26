// Library view: everything Delta made (documents, presentations, spreadsheets, pictures, Build projects), whatever chat
// it came from, newest first, with search and filters. Open it in its app, show it in File Explorer, go back to the
// chat that made it, rename or delete (to the Recycle Bin). Files come through the authenticated routes as blobs, so
// the access token never goes into a URL. Data: /api/library (delta.library).
(() => {
  "use strict";
  const D = window.Delta, {$, h, icon, btn} = D;
  const box = $("documents");
  const path = name => `/api/documents/${encodeURIComponent(name)}`;
  const TYPES = {pdf: "PDF", docx: "Word", pptx: "PowerPoint", xlsx: "Excel", md: "Markdown", html: "Web page", tex: "LaTeX", jpg: "Photo", png: "Picture", project: "Build project"};
  const FILTERS = [["all", "All"], ["document", "Documents"], ["presentation", "Presentations"], ["spreadsheet", "Spreadsheets"], ["picture", "Pictures"], ["project", "Projects"]];
  // pictures keep the name that goes with their credit; people see what they show instead
  const label = d => d.kind !== "picture" ? d.name : d.sticker ? "Sticker" + (d.what ? ": " + d.what : "") : "Photo" + (d.what ? ": " + d.what : "");
  let items = [], filter = "all", query = "", loaded = false;
  const thumbs = new Map();  // picture name -> object URL, kept while the page lives

  function save(blob, name) {
    const u = URL.createObjectURL(blob), a = h("a", {href: u, download: name});
    document.body.append(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(u), 30000);
  }
  function downloadButton(name, label = "Download") {
    return btn(label, async () => {
      try { save(await D.blob(path(name)), name); }
      catch (e) { D.toast({title: "Download failed", body: D.plain(e), kind: "danger"}); }
    }, "sm", "download");
  }
  const blobOf = d => D.blob(d.kind === "picture" ? `/api/pictures/${encodeURIComponent(d.name)}` : path(d.name));
  async function post(url, body) {
    const r = await D.api(url, {method: "POST", body: JSON.stringify(body)}).catch(e => ({ok: false, message: D.plain(e)}));
    if (r && r.ok === false) D.toast({title: r.message || "That did not work", kind: "warn"});
    return r || {};
  }
  function fromChat(d) {
    if (!d.chat_id) return null;
    return h("button", {type: "button", class: "linklike small", onclick: () => { D.go("chat"); D.views.chat.open && D.views.chat.open(d.chat_id); }},
      icon("chat"), h("span", {text: "From chat: " + (d.chat_title || "open the chat")}));
  }
  async function rename(d) {
    const stem = d.name.replace(/\.[^.]+$/, "");
    const dlg = D.sheet("Rename " + d.name, body => {
      const input = h("input", {value: stem, "aria-label": "New name", maxlength: 100});
      const msg = h("div", {class: "msg", role: "status"});
      body.append(h("div", {class: "row inline-form"}, input, btn("Rename", async () => {
        const r = await post("/api/library/rename", {id: d.id, name: input.value});
        if (r.ok) { dlg.close(); refresh(); } else msg.textContent = r.message || "";
      }, "primary sm")), msg);
      setTimeout(() => { input.focus(); input.select(); }, 30);
    });
  }
  async function remove(d) {
    if (!await D.confirm({title: `Delete ${d.name}?`, body: "It goes to the Recycle Bin, so you can still get it back from there.", ok: "Delete", danger: true})) return;
    const r = await post("/api/library/delete", {id: d.id});
    if (r.ok) { D.toast({title: "Moved to the Recycle Bin", kind: "ok"}); refresh(); }
  }
  function preview(d) {
    D.sheet(label(d), async body => {
      const meta = `${TYPES[d.type] || d.type.toUpperCase()} · ${d.kind === "project" ? d.what : D.bytes(d.size)} · ${new Date(d.modified).toLocaleString()}`;
      body.append(h("div", {class: "row"}, ...actions(d, true), h("span", {class: "muted small", text: meta})));
      const origin = fromChat(d); if (origin) body.append(origin);
      if (d.what && d.kind !== "project") body.append(h("p", {class: "muted small", text: (d.kind === "picture" ? "Searched for: " : "Made for: ") + d.what}));
      if (d.kind === "project") return;
      const slot = h("div", {class: "doc-preview"}, D.skeleton(4));
      body.append(slot);
      try {
        const b = await blobOf(d);
        if (d.kind === "picture") slot.replaceChildren(h("img", {class: "lib-big", src: D.sheetURL(b), alt: d.name}));
        else if (d.type === "pdf") slot.replaceChildren(h("iframe", {class: "doc-frame tall", src: D.sheetURL(b), title: "Preview of " + d.name}));
        else if (d.type === "md" || d.type === "tex") slot.replaceChildren(h("pre", {class: "code doc-text", text: (await b.text()).slice(0, 200000)}));
        else slot.replaceChildren(D.empty({ic: "doc", title: "Open it in its own app", body: "Press Open to see it in " + (TYPES[d.type] || "its app") + "."}));
      } catch (e) { slot.replaceChildren(D.failed("The preview could not be loaded. " + D.plain(e))); }
    });
  }
  function actions(d, full) {
    if (d.kind === "project") return [btn("Open in Build", () => { D.go("build"); D.views.build.open && D.views.build.open(d.id.slice(5)); }, "primary sm", "code")];
    const out = [btn("Open", () => post("/api/library/open", {id: d.id, how: "app"}), "primary sm", "external")];
    if (full) {
      out.push(btn("Show in folder", () => post("/api/library/open", {id: d.id, how: "folder"}), "sm", "folder"));
      out.push(btn("Download", async () => { try { save(await blobOf(d), d.name); } catch (e) { D.toast({title: "Download failed", body: D.plain(e), kind: "danger"}); } }, "sm", "download"));
      if (d.kind !== "picture") out.push(btn("Rename", () => rename(d), "sm", "edit"));
      out.push(btn("Delete", () => remove(d), "sm danger", "trash"));
    }
    return out;
  }
  function thumb(d) {
    const slot = h("span", {class: "file-ic" + (d.kind === "picture" ? " pic" : ""), text: d.kind === "picture" ? "" : d.kind === "project" ? "</>" : (d.type || "").toUpperCase().slice(0, 4)});
    if (d.kind === "picture") {
      const have = thumbs.get(d.name);
      const put = u => slot.replaceChildren(h("img", {src: u, alt: "", loading: "lazy"}));
      if (have) put(have); else blobOf(d).then(b => { const u = URL.createObjectURL(b); thumbs.set(d.name, u); put(u); }).catch(() => {});
    }
    return slot;
  }
  function render() {
    const q = query.trim().toLowerCase();
    const shown = items.filter(d => (filter === "all" || d.kind === filter) && (!q || `${d.name} ${d.what} ${d.chat_title}`.toLowerCase().includes(q)));
    const chips = h("div", {class: "chips", role: "tablist", "aria-label": "Show"}, FILTERS.map(([k, label]) => {
      const n = k === "all" ? items.length : items.filter(d => d.kind === k).length;
      return h("button", {type: "button", role: "tab", class: "chip" + (filter === k ? " on" : ""), "aria-selected": String(filter === k), onclick: () => { filter = k; render(); }, text: `${label} ${n}`});
    }));
    const search = h("input", {type: "search", class: "lib-search", placeholder: "Search by name, topic or chat", value: query, "aria-label": "Search the Library",
      oninput: e => { query = e.target.value; clearTimeout(search.t); search.t = setTimeout(() => { render(); const s = box.querySelector(".lib-search"); if (s) { s.focus(); s.setSelectionRange(s.value.length, s.value.length); } }, 150); }});
    const head = h("div", {class: "lib-head"}, search, chips);
    if (!shown.length) {
      box.replaceChildren(head, D.empty({ic: "doc", title: items.length ? "Nothing matches" : "Nothing here yet",
        body: items.length ? "Try other words or another filter." : 'Ask Delta for a presentation, a report or photos, for example "make a PPT about solar power". Everything it makes shows up here.',
        action: items.length ? null : {label: "Go to Chat", fn: () => D.go("chat")}}));
      return;
    }
    const list = h("div", {class: "table-list", role: "list"});
    D.paged(list, shown, d => h("div", {class: "drow", role: "listitem"},
      h("button", {type: "button", class: "drow-main", onclick: () => preview(d), "aria-label": "Details for " + d.name},
        thumb(d),
        h("span", {class: "drow-name"}, h("span", {text: label(d)}), d.chat_title ? h("small", {class: "muted", text: "From: " + d.chat_title}) : null),
        h("span", {class: "muted small", text: TYPES[d.type] || d.type}),
        h("span", {class: "muted small", text: new Date(d.modified).toLocaleString([], {month: "short", day: "numeric", hour: "2-digit", minute: "2-digit"})})),
      ...actions(d, false)), 40);
    box.replaceChildren(head, list);
  }
  async function refresh() {
    if (!loaded) box.replaceChildren(D.skeleton(4));
    const r = await D.api("/api/library").catch(e => ({error: e}));
    if (!r || !Array.isArray(r.items)) { box.replaceChildren(D.failed(r && r.error ? D.plain(r.error) : D.detail(r) || "The Library could not be loaded.", refresh)); return; }
    loaded = true; items = r.items;
    if (r.folder) $("docfolder").textContent = "Everything Delta made for you, from every chat. Files are in " + r.folder;
    render();
  }
  $("docs_refresh").onclick = refresh;
  D.on("documents:changed", () => { if (D.view === "documents") refresh(); });
  D.views.documents = {show: refresh, downloadButton};
})();
