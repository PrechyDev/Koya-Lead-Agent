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

  // Word counter + Start button enablement for the objective. The "at least 5 words" message itself is shown
  // under the box by the field checks below (data-min-words), like every other field message.
  function countWords(text) {
    // Same rule as the server (app/lib/objective.py): a word is a space-separated token containing a letter.
    return (text || "").trim().split(/\s+/).filter(function (t) { return /\p{L}/u.test(t); }).length;
  }
  function updateCounter(textarea) {
    var counter = document.getElementById(textarea.id + "-count");
    if (counter) counter.textContent = textarea.value.trim().length + " / 1000";
    var submit = document.getElementById("start-run");
    if (!submit || submit.hasAttribute("data-locked")) return;
    submit.disabled = countWords(textarea.value) < parseInt(textarea.getAttribute("data-min-words") || "5", 10);
  }
  document.addEventListener("input", function (event) {
    if (event.target.matches("textarea[data-counter]")) updateCounter(event.target);
  });
  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("textarea[data-counter]").forEach(updateCounter);
  });
  // Safety net: after any HTMX request, put Start back to what the objective allows (HTMX re-enables buttons
  // it disabled during a request, whatever their state was before).
  document.addEventListener("htmx:afterRequest", function () {
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

  // Field checks (design.md §2), like Google Forms: a problem is shown in red directly UNDER the field it's
  // about, with a red border, while the person types (after a short pause, so it doesn't flash on every key)
  // and at once when they leave the field. It disappears as soon as the value is fixed. An EMPTY field shows
  // nothing (people can see it's empty); the submit button just stays disabled until the form is complete.
  // Buttons can add their own condition with data-requires="<field id>" (e.g. Reject needs a note). Buttons
  // marked data-locked (server decided) or data-custom-enable (own script) are left alone.
  var FIELDS = "input:not([type=hidden]), textarea, select";

  function fieldError(el) {
    var value = el.value.trim();
    if (!value) return "";
    var minWords = parseInt(el.getAttribute("data-min-words") || "0", 10);
    if (minWords && countWords(value) < minWords) return "Describe the companies in at least " + minWords + " words.";
    if (el.type === "email" && !el.checkValidity()) return "Enter a valid email address, like name@company.com.";
    if (el.minLength > 0 && el.value.length < el.minLength) return "Use at least " + el.minLength + " characters.";
    if (!el.checkValidity()) return "Please check this field.";
    var other = el.getAttribute("data-match") && document.getElementById(el.getAttribute("data-match"));
    if (other && other.value && el.value !== other.value) return "The passwords don't match.";
    return "";
  }

  function showFieldError(el, message) {
    var id = (el.id || el.name) + "-error";
    var slot = document.getElementById(id);
    if (!slot && !message) return;
    if (!slot) {
      slot = document.createElement("p");
      slot.id = id;
      slot.className = "field-error";
      slot.setAttribute("aria-live", "polite");
      el.insertAdjacentElement("afterend", slot);
      el.setAttribute("aria-describedby", ((el.getAttribute("aria-describedby") || "") + " " + id).trim());
    }
    slot.textContent = message;
    el.classList.toggle("invalid", Boolean(message));
    el.setAttribute("aria-invalid", message ? "true" : "false");
  }

  function checkNow(el) {
    showFieldError(el, fieldError(el));
    // Changing a password re-checks its confirmation (and the reverse), so "don't match" updates both ways.
    var form = el.closest("form");
    if (!form) return;
    form.querySelectorAll('[data-match="' + el.id + '"]').forEach(function (c) { if (c.value) showFieldError(c, fieldError(c)); });
  }

  var timers = {};
  function checkSoon(el) {
    var key = el.id || el.name;
    clearTimeout(timers[key]);
    // A confirmation box showing "don't match" is re-checked at once when this field changes.
    var form = el.closest("form");
    if (form) form.querySelectorAll('[data-match="' + el.id + '"].invalid').forEach(function (c) { showFieldError(c, fieldError(c)); });
    // Already showing a message? Update it at once (so it clears the moment the value is fixed).
    if (el.classList.contains("invalid")) return checkNow(el);
    timers[key] = setTimeout(function () { checkNow(el); }, 600);
  }

  function formBlocked(form) {
    return Array.prototype.some.call(form.querySelectorAll(FIELDS), function (el) {
      if (el.disabled) return false;
      if (el.required && !el.value.trim()) return true;
      var minWords = parseInt(el.getAttribute("data-min-words") || "0", 10);
      if (minWords && countWords(el.value) < minWords) return true;
      return Boolean(fieldError(el));
    });
  }

  function refreshForm(form) {
    var buttons = Array.prototype.filter.call(form.querySelectorAll('button[type="submit"]'), function (b) {
      return !b.hasAttribute("data-locked") && !b.hasAttribute("data-custom-enable");
    });
    var guarded = form.querySelector("[required], [minlength], [data-match], [data-min-words], [type=email]");
    var withOwnRule = buttons.filter(function (b) { return b.hasAttribute("data-requires"); });
    if (!guarded && !withOwnRule.length) return;
    var blocked = guarded ? formBlocked(form) : false;
    buttons.forEach(function (b) {
      var need = b.getAttribute("data-requires") && document.getElementById(b.getAttribute("data-requires"));
      b.disabled = blocked || Boolean(need && !need.value.trim());
    });
  }

  function refreshAll(root) {
    (root || document).querySelectorAll("form").forEach(refreshForm);
  }
  document.addEventListener("input", function (event) {
    var el = event.target;
    if (!el.matches || !el.matches(FIELDS)) return;
    checkSoon(el);
    var form = el.closest("form");
    if (form) refreshForm(form);
  });
  document.addEventListener("change", function (event) {
    var form = event.target.closest && event.target.closest("form");
    if (form) refreshForm(form);
  });
  document.addEventListener("focusout", function (event) {
    var el = event.target;
    if (el.matches && el.matches(FIELDS)) { clearTimeout(timers[el.id || el.name]); checkNow(el); }
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
