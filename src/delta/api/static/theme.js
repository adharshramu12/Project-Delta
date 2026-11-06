// Theme: "system" (follows Windows), "light" or "dark". Loaded in <head> so the page never flashes the wrong theme.
(function () {
  "use strict";
  var dark = window.matchMedia("(prefers-color-scheme: dark)");
  var mode = "system";
  try { mode = localStorage.getItem("delta-theme") || "system"; } catch (e) { /* storage off */ }
  if (["system", "light", "dark"].indexOf(mode) < 0) mode = "system";
  function apply(m) {
    if (m) mode = m;
    try { localStorage.setItem("delta-theme", mode); } catch (e) { /* storage off */ }
    var root = document.documentElement;
    root.dataset.theme = mode === "system" ? (dark.matches ? "dark" : "light") : mode;
    root.dataset.mode = mode;
    document.dispatchEvent(new CustomEvent("delta-theme", {detail: mode}));
  }
  window.DeltaTheme = {
    get mode() { return mode; },
    set: apply,
    next: function () { apply({system: "light", light: "dark", dark: "system"}[mode]); }
  };
  dark.addEventListener("change", function () { if (mode === "system") apply(); });
  apply();
})();
