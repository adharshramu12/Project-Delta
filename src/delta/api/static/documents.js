// Documents view: files Delta made (make_document), newest first, with a preview and a download. Files come through
// the authenticated /api/documents route as blobs, so the access token never goes into a URL.
(() => {
  "use strict";
  const D = window.Delta, {$, h, icon, btn} = D;
  const box = $("documents");
  const path = name => `/api/documents/${encodeURIComponent(name)}`;
  const TYPES = {pdf: "PDF", docx: "Word", pptx: "PowerPoint", xlsx: "Excel", md: "Markdown", html: "Web page", tex: "LaTeX"};
  let loaded = false;

  function downloadButton(name, label = "Download") {
    return btn(label, async () => {
      try {
        const u = URL.createObjectURL(await D.blob(path(name)));
        const a = h("a", {href: u, download: name});
        document.body.append(a); a.click(); a.remove();
        setTimeout(() => URL.revokeObjectURL(u), 30000);
      } catch (e) { D.toast({title: "Download failed", body: D.plain(e), kind: "danger"}); }
    }, "sm", "download");
  }
  function preview(d) {
    D.sheet(d.name, async body => {
      body.append(h("div", {class: "row"}, downloadButton(d.name), h("span", {class: "muted small", text: `${TYPES[d.type] || d.type.toUpperCase()} · ${D.bytes(d.size)} · ${new Date(d.modified).toLocaleString()}`})));
      const slot = h("div", {class: "doc-preview"}, D.skeleton(4));
      body.append(slot);
      try {
        const b = await D.blob(path(d.name));
        if (d.type === "pdf") slot.replaceChildren(h("iframe", {class: "doc-frame tall", src: D.sheetURL(b), title: "Preview of " + d.name}));
        else if (d.type === "md" || d.type === "tex") slot.replaceChildren(h("pre", {class: "code doc-text", text: (await b.text()).slice(0, 200000)}));
        else slot.replaceChildren(D.empty({ic: "doc", title: "No preview for this file type", body: "Download it to open it in its own app."}));
      } catch (e) { slot.replaceChildren(D.failed("The preview could not be loaded. " + D.plain(e))); }
    });
  }
  async function refresh() {
    if (!loaded) box.replaceChildren(D.skeleton(4));
    const r = await D.api("/api/documents").catch(e => ({error: e}));
    if (!r || !Array.isArray(r.documents)) { box.replaceChildren(D.failed(r && r.error ? D.plain(r.error) : D.detail(r) || "Documents could not be loaded.", refresh)); return; }
    loaded = true;
    if (r.folder) $("docfolder").textContent = "Reports, notes and files Delta made for you. Saved in " + r.folder;
    if (!r.documents.length) {
      box.replaceChildren(D.empty({ic: "doc", title: "No documents yet", body: 'Ask Delta for a report, for example "summarise the PDFs in my Downloads folder into a one page report".',
        action: {label: "Go to Chat", fn: () => D.go("chat")}}));
      return;
    }
    const list = h("div", {class: "table-list", role: "list"});
    D.paged(list, r.documents, d => h("div", {class: "drow", role: "listitem"},
      h("button", {type: "button", class: "drow-main", onclick: () => preview(d), "aria-label": "Preview " + d.name},
        h("span", {class: "file-ic", text: (d.type || "").toUpperCase().slice(0, 4)}),
        h("span", {class: "drow-name", text: d.name}),
        h("span", {class: "muted small", text: `${TYPES[d.type] || d.type} · ${D.bytes(d.size)}`}),
        h("span", {class: "muted small", text: new Date(d.modified).toLocaleString([], {month: "short", day: "numeric", hour: "2-digit", minute: "2-digit"})})),
      downloadButton(d.name)), 40);
    box.replaceChildren(list);
  }
  $("docs_refresh").onclick = refresh;
  D.on("documents:changed", () => { if (D.view === "documents") refresh(); });
  D.views.documents = {show: refresh, downloadButton};
})();
