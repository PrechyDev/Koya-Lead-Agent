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

  // Required inputs (design.md §4): a submit button stays disabled until every required field in its form is
  // valid, with the reason shown beside it. Buttons can add their own condition with data-requires="<field id>"
  // (e.g. Reject needs a note). Buttons marked data-locked (server decided) or data-custom-enable (own script)
  // are left alone.
  function fieldName(el) {
    var label = el.id && document.querySelector('label[for="' + el.id + '"]');
    return label ? label.textContent.replace(/\(.*?\)/g, "").trim() : (el.name || "a field");
  }

  function formProblem(form) {
    var fields = Array.prototype.filter.call(form.querySelectorAll("input, textarea, select"), function (el) {
      return !el.disabled && el.type !== "hidden";
    });
    var missing = fields.filter(function (el) { return el.required && !el.value.trim(); });
    if (missing.length) return "Fill in: " + missing.map(fieldName).join(", ") + ".";
    var invalid = fields.filter(function (el) { return !el.checkValidity(); });
    if (invalid.length) {
      var el = invalid[0];
      if (el.minLength > 0 && el.value.length < el.minLength) return fieldName(el) + " needs at least " + el.minLength + " characters.";
      if (el.type === "email") return "Enter a valid email address.";
      return "Check " + fieldName(el) + ".";
    }
    var mismatch = fields.filter(function (el) {
      var other = el.getAttribute("data-match") && document.getElementById(el.getAttribute("data-match"));
      return other && el.value !== other.value;
    });
    if (mismatch.length) return "The passwords don't match.";
    return "";
  }

  function reasonSlot(form) {
    var slot = form.querySelector("[data-form-reason]");
    if (!slot) {
      slot = document.createElement("p");
      slot.className = "disabled-reason";
      slot.setAttribute("data-form-reason", "");
      slot.setAttribute("aria-live", "polite");
      // Right after the buttons: inside their row if they sit in one, else at the end of the form.
      var buttons = form.querySelectorAll('button[type="submit"]');
      var last = buttons[buttons.length - 1];
      if (last && last.parentElement === form) last.insertAdjacentElement("afterend", slot);
      else (last ? last.parentElement : form).appendChild(slot);
    }
    return slot;
  }

  function refreshForm(form) {
    var buttons = Array.prototype.filter.call(form.querySelectorAll('button[type="submit"]'), function (b) {
      return !b.hasAttribute("data-locked") && !b.hasAttribute("data-custom-enable");
    });
    var guarded = form.querySelector("[required], [minlength], [data-match]");
    var withOwnRule = buttons.filter(function (b) { return b.hasAttribute("data-requires"); });
    if (!guarded && !withOwnRule.length) return;
    var problem = guarded ? formProblem(form) : "";
    var reasons = problem ? [problem] : [];
    buttons.forEach(function (b) {
      var need = b.getAttribute("data-requires") && document.getElementById(b.getAttribute("data-requires"));
      var ownProblem = need && !need.value.trim() ? (b.getAttribute("data-requires-reason") || ("Fill in " + fieldName(need) + ".")) : "";
      if (ownProblem && reasons.indexOf(ownProblem) < 0) reasons.push(ownProblem);
      b.disabled = Boolean(problem || ownProblem);
    });
    reasonSlot(form).textContent = reasons.join(" ");
  }

  function refreshAll(root) {
    (root || document).querySelectorAll("form").forEach(refreshForm);
  }
  ["input", "change"].forEach(function (type) {
    document.addEventListener(type, function (event) {
      var form = event.target.closest && event.target.closest("form");
      if (form) refreshForm(form);
    });
  });
  // Browser autofill may not fire input events until the user interacts with the page.
  ["pointerdown", "keydown", "focusin"].forEach(function (type) {
    document.addEventListener(type, function () { refreshAll(); }, true);
  });
  document.addEventListener("DOMContentLoaded", function () {
    refreshAll();
    setTimeout(refreshAll, 600);
  });
  document.addEventListener("htmx:load", function (event) { refreshAll(event.target); });

  // Loading state only once the browser has accepted the form (the submit event fires after validation).
  // HTMX forms get it from the htmx-request class; plain forms (login, accept invite) get it here.
  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (form.hasAttribute("hx-post")) return;
    var button = event.submitter || form.querySelector('button[type="submit"]');
    if (!button) return;
    button.classList.add("is-loading");
    setTimeout(function () { button.disabled = true; }, 0);  // after the browser has read the button's value
  });
  // Coming back with the browser's Back button must not leave a spinner on.
  window.addEventListener("pageshow", function () {
    document.querySelectorAll(".btn.is-loading").forEach(function (b) { b.classList.remove("is-loading"); b.disabled = false; });
    refreshAll();
  });

  // Lost connection (Render asleep, Wi-Fi drop): say so in the System Message bar instead of freezing silently.
  // Live polling keeps retrying on its own schedule; the banner clears on the next successful request.
  function connectionBanner(show) {
    var bar = document.getElementById("system-message");
    var existing = document.getElementById("offline-banner");
    if (!show) { if (existing) existing.remove(); return; }
    if (existing || !bar) return;
    bar.insertAdjacentHTML("afterbegin",
      '<div id="offline-banner" class="banner error" role="alert"><span class="icon" aria-hidden="true">✕</span>' +
      '<div class="msg">Can\'t reach the server. Retrying automatically… ' +
      '<button type="button" class="btn small" onclick="location.reload()">Retry now</button></div></div>');
  }
  document.addEventListener("htmx:sendError", function () { connectionBanner(true); });
  document.addEventListener("htmx:afterRequest", function (event) {
    if (event.detail && event.detail.successful) connectionBanner(false);
  });

  // "Last updated" stamp for the live panel.
  document.body && document.body.addEventListener("htmx:afterSwap", function (event) {
    var stamp = document.getElementById("live-stamp");
    if (stamp && event.detail.target && event.detail.target.id === "run-live") stamp.textContent = "Updated just now";
  });
})();
