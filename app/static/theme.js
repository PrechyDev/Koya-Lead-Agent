// Theme: Auto (follows the device) / Light / Dark, remembered in this browser (D-95).
// Loaded in <head> without defer so the right theme is set before the page is drawn (no flash).
(function () {
  var KEY = "la-theme";
  var ORDER = ["auto", "light", "dark"];
  var LABELS = { auto: "◐ Auto", light: "☀ Light", dark: "☾ Dark" };
  var HINTS = { auto: "follows your device", light: "always light", dark: "always dark" };

  function saved() {
    try { return localStorage.getItem(KEY) || "auto"; } catch (e) { return "auto"; }
  }
  function apply(mode) {
    if (mode === "light" || mode === "dark") document.documentElement.setAttribute("data-theme", mode);
    else document.documentElement.removeAttribute("data-theme");
  }
  function label(button, mode) {
    button.textContent = LABELS[mode];
    button.setAttribute("aria-label", "Theme: " + mode + " (" + HINTS[mode] + "). Click to change.");
    button.title = "Theme: " + HINTS[mode];
  }

  apply(saved());
  document.addEventListener("DOMContentLoaded", function () {
    var button = document.getElementById("theme-toggle");
    if (!button) return;
    label(button, saved());
    button.addEventListener("click", function () {
      var next = ORDER[(ORDER.indexOf(saved()) + 1) % ORDER.length];
      try { localStorage.setItem(KEY, next); } catch (e) { /* private mode: still switch for this page */ }
      apply(next);
      label(button, next);
    });
  });
})();
