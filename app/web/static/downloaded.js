/* Bulk selection, the rename drawer and live pack progress for /downloaded.
 *
 * Everything here is an addition to a page that already works: the list filters,
 * pages, selects with plain checkboxes and packs, removes and re-downloads
 * through real forms. This file makes a fifty-book library fast to work through
 * and takes nothing away when it fails to load. The same three rules
 * `candidates.js` and `activity.js` follow:
 *
 * 1. It writes no vocabulary. The pack badge is replaced with markup the server
 *    resolved from `app/api/status.py`; there is no Chinese state name below.
 * 2. It patches fields, it never builds a row. A poll that finds a work the page
 *    has never rendered shows a change notice instead, because row markup built
 *    here would be a second copy of the `work_row` macro and the two would
 *    drift.
 * 3. It reveals its own controls. The select tools and the rename buttons ship
 *    `hidden` and are unhidden below, so a dead button never reaches the screen.
 */
(function () {
  "use strict";

  var root = document.querySelector("[data-downloaded-root]");
  if (!root) return;

  var form = document.querySelector("[data-downloaded-form]");
  var tab = root.getAttribute("data-tab") || "all";
  var live = root.getAttribute("data-live") === "true";

  /* --------------------------------------------------------- reveal controls */

  function reveal(selector) {
    document.querySelectorAll(selector).forEach(function (node) {
      node.hidden = false;
    });
  }

  reveal("[data-select-tools]");
  reveal("[data-rename-open]");

  /* -------------------------------------------------------------- selection */

  function boxes() {
    if (!form) return [];
    return Array.prototype.slice.call(
      form.querySelectorAll("input[name=candidate_ids]")
    );
  }

  function syncSelected() {
    /* `data-selected` is what `.ui-card` and a table row already style, so the
     * attribute the macro writes on a server render is the one kept current
     * here rather than a second class of this file's own. */
    Array.prototype.slice
      .call(document.querySelectorAll("[data-work-id]"))
      .forEach(function (item) {
        var box = item.querySelector("input[name=candidate_ids]");
        var on = !!(box && box.checked);
        var card = item.classList.contains("ui-card")
          ? item
          : item.querySelector(".ui-card") || item;
        if (on) card.setAttribute("data-selected", "true");
        else card.removeAttribute("data-selected");
      });
  }

  function announceSelection() {
    /* One bubbling `change` so Alpine recounts the toolbar from the checkboxes.
     * The count is never computed here: the page owns that expression, and a
     * second count could disagree with the one on screen. */
    if (!form) return;
    syncSelected();
    form.dispatchEvent(new Event("change", { bubbles: true }));
  }

  if (form) {
    form.addEventListener("change", syncSelected);
    syncSelected();
  }

  document.querySelectorAll("[data-select]").forEach(function (button) {
    button.addEventListener("click", function () {
      var mode = button.getAttribute("data-select");
      boxes().forEach(function (box) {
        if (mode === "all") box.checked = true;
        else if (mode === "none") box.checked = false;
        else if (mode === "invert") box.checked = !box.checked;
      });
      announceSelection();
    });
  });

  /* ---------------------------------------------------------- rename drawer */

  var drawer = document.querySelector("[data-rename-drawer]");
  var renameForm = drawer ? drawer.querySelector("[data-rename-form]") : null;
  var currentLine = drawer ? drawer.querySelector("[data-rename-current]") : null;
  var filenameField = drawer ? drawer.querySelector("#rename-filename") : null;
  var directoryField = drawer ? drawer.querySelector("#rename-directory") : null;
  var lastTrigger = null;

  function closeDrawer() {
    if (!drawer) return;
    drawer.hidden = true;
    /* Focus goes back where it came from: a keyboard user who opened this from a
     * row must not be returned to the top of the document. */
    if (lastTrigger) lastTrigger.focus();
  }

  function openDrawer(button) {
    if (!drawer || !renameForm) return;
    lastTrigger = button;
    var id = button.getAttribute("data-rename-open");
    var path = button.getAttribute("data-rename-name") || "";
    var relative = button.getAttribute("data-rename-relative") || "";
    var title = button.getAttribute("data-rename-title") || "";

    renameForm.setAttribute("action", "/downloaded/" + id + "/rename");
    /* Pre-filled from the current name so the operator edits rather than
     * retypes, and so submitting untouched is the documented no-op instead of a
     * silent move to a template-derived path. */
    var base = path.split(/[\\/]/).pop() || "";
    if (base.toLowerCase().slice(-4) === ".cbz") base = base.slice(0, -4);
    if (filenameField) filenameField.value = base || title;
    if (directoryField) {
      var parts = relative.split("/");
      parts.pop();
      directoryField.value = parts.join("/");
    }
    if (currentLine) currentLine.textContent = relative || path;

    drawer.hidden = false;
    if (filenameField) filenameField.focus();
  }

  document.querySelectorAll("[data-rename-open]").forEach(function (button) {
    button.addEventListener("click", function () {
      openDrawer(button);
    });
  });

  if (drawer) {
    drawer.querySelectorAll("[data-rename-close]").forEach(function (node) {
      node.addEventListener("click", closeDrawer);
    });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && !drawer.hidden) closeDrawer();
    });
  }

  /* ------------------------------------------------------------ live packing */

  /* Only started when the server says something is moving, which is what keeps a
   * library of finished books from waking the process every few seconds. */
  if (!live) return;

  var notice = document.querySelector("[data-queue-changed]");
  var timer = null;

  /* Fallbacks only. The real values come from `/api/v1/meta`, so the cadence is
   * an operator setting rather than a constant compiled into this file. */
  var interval = 5000;
  var idleInterval = 20000;

  function patch(item) {
    var host = document.querySelector('[data-work-id="' + item.candidate_id + '"]');
    if (!host) return false;
    var badge = host.querySelector('[data-field="pack"]');
    if (badge) {
      /* The whole badge is replaced from the server-resolved payload rather than
       * having its text rewritten, so the label and the tone can never come from
       * two different states. */
      badge.innerHTML =
        '<span class="ui-badge" data-tone="' +
        item.pack.tone +
        '" data-code="' +
        item.pack.code +
        '">' +
        item.pack.label +
        "</span>";
    }
    var error = host.querySelector('[data-field="pack-error"]');
    if (error) error.textContent = item.pack_error_message || "";
    return true;
  }

  function poll() {
    var url = "/api/v1/downloaded?tab=" + encodeURIComponent(tab);
    fetch(url, { headers: { Accept: "application/json" } })
      .then(function (response) {
        /* 401 means the session went away. Reloading lands on the login page
         * rather than leaving a stale grid updating itself. */
        if (response.status === 401) {
          window.location.reload();
          return null;
        }
        return response.ok ? response.json() : null;
      })
      .then(function (payload) {
        if (!payload) return;
        var unknown = false;
        payload.works.forEach(function (item) {
          if (!patch(item)) unknown = true;
        });
        if (unknown && notice) {
          notice.hidden = false;
          notice.textContent = "列表有更新，刷新以查看";
        }
        if (!payload.live && timer) {
          window.clearInterval(timer);
          timer = null;
        }
      })
      .catch(function () {
        /* Swallowed on purpose: a failed poll is a missing update, not an error
         * worth putting on screen. The next tick tries again. */
      });
  }

  function schedule() {
    if (timer) window.clearInterval(timer);
    /* A hidden tab polls at the idle cadence, which the server derives from the
     * active one -- so no setting can make a background tab poll faster than a
     * foreground one. */
    var wait = document.visibilityState === "visible" ? interval : idleInterval;
    timer = window.setInterval(poll, wait);
  }

  fetch("/api/v1/meta", { headers: { Accept: "application/json" } })
    .then(function (response) {
      return response.ok ? response.json() : null;
    })
    .then(function (meta) {
      if (meta && meta.polling) {
        interval = meta.polling.interval_ms || interval;
        idleInterval = meta.polling.idle_interval_ms || idleInterval;
      }
    })
    .catch(function () {
      /* Keep the fallbacks: a page that cannot read the cadence still polls. */
    })
    .then(function () {
      schedule();
    });

  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible") poll();
    schedule();
  });
})();
