// Small behaviours HTMX doesn't cover. No framework; everything degrades gracefully.
(function () {
  // Copy buttons: "Copy" -> "Copied ✓" for 2s, with a select-text fallback.
  document.addEventListener("click", function (event) {
    var btn = event.target.closest("[data-copy]");
    if (!btn) return;
    var source = document.getElementById(btn.getAttribute("data-copy"));
    if (!source) return;
    var text = source.innerText;
    var done = function () {
      var original = btn.getAttribute("data-label") || btn.textContent;
      btn.setAttribute("data-label", original);
      btn.textContent = "Copied ✓";
      setTimeout(function () { btn.textContent = original; }, 2000);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done, function () { selectText(source); });
    } else {
      selectText(source);
    }
  });

  function selectText(node) {
    var range = document.createRange();
    range.selectNodeContents(node);
    var sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
  }

  // Example chips fill the objective textarea.
  document.addEventListener("click", function (event) {
    var chip = event.target.closest("[data-fill]");
    if (!chip) return;
    var target = document.getElementById(chip.getAttribute("data-target"));
    if (!target) return;
    target.value = chip.getAttribute("data-fill");
    target.dispatchEvent(new Event("input", { bubbles: true }));
    target.dispatchEvent(new Event("keyup", { bubbles: true }));
    target.focus();
    updateCounter(target);
  });

  // Character counter + Start button enablement for the objective.
  function updateCounter(textarea) {
    var counter = document.getElementById(textarea.id + "-count");
    if (counter) counter.textContent = textarea.value.trim().length + " / 1000";
    var submit = document.getElementById("start-run");
    var reason = document.getElementById("start-run-reason");
    if (!submit || submit.hasAttribute("data-locked")) return;
    var len = textarea.value.trim().length;
    var ok = len >= 5 && len <= 1000;
    submit.disabled = !ok;
    if (reason) reason.textContent = ok ? "" : (len < 5 ? "Enter at least 5 characters to start." : "Keep it under 1,000 characters.");
  }
  document.addEventListener("input", function (event) {
    if (event.target.matches("textarea[data-counter]")) updateCounter(event.target);
  });
  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("textarea[data-counter]").forEach(updateCounter);
  });

  // Drawer: close with Esc / backdrop / ✕, and return focus to the row that opened it.
  var lastRow = null;
  document.addEventListener("click", function (event) {
    var row = event.target.closest("tr.clickable");
    if (row) lastRow = row;
    if (event.target.closest("[data-close-drawer]") || event.target.classList.contains("drawer-backdrop")) closeDrawer();
  });
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") closeDrawer();
    var row = event.target.closest && event.target.closest("tr.clickable");
    if (row && (event.key === "Enter" || event.key === " ")) { event.preventDefault(); row.click(); }
  });
  function closeDrawer() {
    var slot = document.getElementById("drawer");
    if (slot && slot.innerHTML.trim()) {
      slot.innerHTML = "";
      if (lastRow) lastRow.focus();
    }
  }

  // Tabs: aria-selected follows the clicked tab.
  document.addEventListener("click", function (event) {
    var tab = event.target.closest(".tab");
    if (!tab) return;
    tab.parentElement.querySelectorAll(".tab").forEach(function (t) { t.setAttribute("aria-selected", "false"); });
    tab.setAttribute("aria-selected", "true");
  });

  // Dismissible banners.
  document.addEventListener("click", function (event) {
    var close = event.target.closest(".banner .close");
    if (close) close.closest(".banner").remove();
  });

  // "Last updated" stamp for the live panel.
  document.body && document.body.addEventListener("htmx:afterSwap", function (event) {
    var stamp = document.getElementById("live-stamp");
    if (stamp && event.detail.target && event.detail.target.id === "run-live") stamp.textContent = "Updated just now";
  });
})();
