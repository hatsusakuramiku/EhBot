/* Keyboard control, bulk selection and the metadata drawer for /candidates.
 *
 * Everything here is an addition to a page that already works. The list filters,
 * pages, opens a candidate and approves one or many through plain forms and
 * links; this file makes a hundred-candidate review fast, and takes nothing away
 * when it fails to load. Three rules, the same ones `activity.js` follows:
 *
 * 1. It writes no vocabulary. Metadata provenance is rendered from the `source`
 *    payload `app/api/status.py` resolved, and field labels come from the
 *    payload too. There is no Chinese state name in this file.
 * 2. It clones structure, never invents it. A drawer row comes from the
 *    `<template data-metadata-row>` the page authored; building one here would
 *    be a second copy of that markup, and the two would drift.
 * 3. It reveals its own controls. Anything that cannot work without JavaScript
 *    ships `hidden` and is unhidden below, so a dead button never reaches the
 *    screen.
 */
(function () {
  "use strict";

  var root = document.querySelector("[data-candidates-root]");
  if (!root) return;

  var csrf = root.getAttribute("data-csrf") || "";
  var form = document.querySelector("[data-candidate-form]");

  /* --------------------------------------------------------- reveal controls */

  function reveal(selector) {
    document.querySelectorAll(selector).forEach(function (node) {
      node.hidden = false;
    });
  }

  reveal("[data-select-tools]");
  reveal("[data-metadata-open]");

  /* -------------------------------------------------------------- selection */

  function boxes() {
    if (!form) return [];
    return Array.prototype.slice.call(
      form.querySelectorAll("input[name=candidate_ids]")
    );
  }

  function items() {
    return Array.prototype.slice.call(
      document.querySelectorAll("[data-candidate-id]")
    );
  }

  function boxIn(item) {
    return item ? item.querySelector("input[name=candidate_ids]") : null;
  }

  function syncSelected() {
    /* `data-selected` is what `.ui-card` and `.ui-table tbody tr` already style
     * for a chosen row, so the attribute the macro writes on a server render is
     * the same one kept current here. */
    items().forEach(function (item) {
      var box = boxIn(item);
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
     * second count in this file could disagree with the one on screen. */
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

  /* --------------------------------------------------------------- keyboard */

  function isTyping(target) {
    if (!target) return false;
    if (target.isContentEditable) return true;
    var tag = target.tagName;
    return tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA";
  }

  function currentItem() {
    var active = document.activeElement;
    return active ? active.closest("[data-candidate-id]") : null;
  }

  function focusItem(item) {
    if (!item) return;
    /* The checkbox first: it is the control the next keystroke acts on, and
     * focusing it means `space` keeps working as itself. A tab with nothing to
     * select falls back to the title link so the cursor is still visible. */
    var target = boxIn(item) || item.querySelector("a");
    if (target) target.focus();
    item.scrollIntoView({ block: "nearest" });
  }

  function move(delta) {
    var all = items();
    if (!all.length) return;
    var index = all.indexOf(currentItem());
    if (index === -1) index = delta > 0 ? -1 : 0;
    var next = Math.min(Math.max(index + delta, 0), all.length - 1);
    focusItem(all[next]);
  }

  document.addEventListener("keydown", function (event) {
    if (event.metaKey || event.ctrlKey || event.altKey) return;
    if (drawerOpen()) return;
    /* A search box has first claim on every letter. `x` and `a` inside the
     * filter field must type, not review. */
    if (isTyping(event.target) && event.target.type !== "checkbox") return;

    var item;
    if (event.key === "j") {
      move(1);
    } else if (event.key === "k") {
      move(-1);
    } else if (event.key === "x") {
      item = currentItem();
      var box = boxIn(item);
      if (!box) return;
      box.checked = !box.checked;
      announceSelection();
    } else if (event.key === "a") {
      item = currentItem();
      var quick = item ? item.querySelector("[data-quick-approve]") : null;
      if (!quick) return;
      /* Submits through the button's own `formaction`, which is the
       * single-candidate approve route -- the same one a click uses. */
      quick.click();
    } else {
      return;
    }
    event.preventDefault();
  });

  /* -------------------------------------------------------------- filtering */

  document.querySelectorAll("[data-autosubmit]").forEach(function (control) {
    control.addEventListener("change", function () {
      var owner = control.form;
      if (owner) owner.requestSubmit ? owner.requestSubmit() : owner.submit();
    });
  });

  /* ------------------------------------------------------- metadata drawer */

  var drawer = document.querySelector("[data-metadata-drawer]");
  var rowTemplate = document.querySelector("template[data-metadata-row]");
  var rowHost = drawer ? drawer.querySelector("[data-metadata-rows]") : null;
  var statusLine = drawer ? drawer.querySelector("[data-metadata-status]") : null;
  var titleNode = drawer ? drawer.querySelector(".ui-drawer-title") : null;
  var detailLink = drawer ? drawer.querySelector("[data-metadata-detail]") : null;
  var saveButton = drawer ? drawer.querySelector("[data-metadata-save]") : null;
  /* The element that had focus when the drawer opened, so closing returns the
   * operator to the row they were on rather than to the top of the document. */
  var opener = null;
  var openId = null;

  function drawerOpen() {
    return !!drawer && !drawer.hidden;
  }

  function say(message) {
    if (statusLine) statusLine.textContent = message || "";
  }

  function badgeInto(host, view) {
    /* Mirrors the `badge` macro attribute for attribute, `data-code` included:
     * the raw code stays out of `title` and so out of the accessibility tree. */
    if (!host) return;
    host.textContent = "";
    if (!view) return;
    var badge = document.createElement("span");
    badge.className = "badge";
    badge.setAttribute("data-tone", view.tone);
    badge.setAttribute("data-live", view.live ? "true" : "false");
    badge.setAttribute("data-code", view.code);
    badge.textContent = view.label;
    host.appendChild(badge);
  }

  function renderRows(entries) {
    if (!rowHost || !rowTemplate) return;
    rowHost.textContent = "";
    entries.forEach(function (entry) {
      var row = rowTemplate.content.firstElementChild.cloneNode(true);
      row.setAttribute("data-field-name", entry.field_name);
      row.querySelector('[data-field="label"]').textContent = entry.field_label;
      badgeInto(row.querySelector('[data-field="source"]'), entry.source);
      var value = row.querySelector('[data-field="value"]');
      value.value = entry.field_value === null ? "" : entry.field_value;
      value.setAttribute("data-original", value.value);
      var lock = row.querySelector('[data-field="lock"]');
      lock.checked = !!entry.is_locked;
      lock.setAttribute("data-original", lock.checked ? "1" : "0");
      rowHost.appendChild(row);
    });
  }

  function changes() {
    /* Only what the operator actually touched. Sending every field would
     * re-stamp each one as a manual override and lose the provenance the drawer
     * is there to show. */
    var fields = {};
    var locks = {};
    if (!rowHost) return { fields: fields, locks: locks };
    rowHost.querySelectorAll("[data-field-name]").forEach(function (row) {
      var name = row.getAttribute("data-field-name");
      var value = row.querySelector('[data-field="value"]');
      var lock = row.querySelector('[data-field="lock"]');
      if (value.value !== value.getAttribute("data-original")) {
        fields[name] = value.value;
      }
      if ((lock.checked ? "1" : "0") !== lock.getAttribute("data-original")) {
        locks[name] = lock.checked;
      }
    });
    return { fields: fields, locks: locks };
  }

  function openDrawer(candidateId, trigger, note) {
    if (!drawer) return;
    opener = trigger || null;
    openId = candidateId;
    drawer.hidden = false;
    if (rowHost) rowHost.textContent = "";
    if (detailLink) detailLink.setAttribute("href", "/candidates/" + candidateId);
    if (titleNode) titleNode.textContent = "元数据 · 候选 #" + candidateId;
    say("正在加载…");
    fetch("/api/v1/works/" + candidateId, {
      headers: { Accept: "application/json" },
    })
      .then(function (response) {
        return response.json().then(function (payload) {
          if (!response.ok) throw payload;
          return payload;
        });
      })
      .then(function (payload) {
        renderRows(payload.metadata || []);
        /* `note` is the caller's word about what just happened -- a save, for
         * instance. Set after the load so it is not overwritten by it. */
        say(note || "");
        var first = rowHost && rowHost.querySelector('[data-field="value"]');
        if (first) first.focus();
      })
      .catch(function (payload) {
        say((payload && payload.error && payload.error.message) || "加载失败");
      });
  }

  function closeDrawer() {
    if (!drawer || drawer.hidden) return;
    drawer.hidden = true;
    openId = null;
    if (opener) opener.focus();
    opener = null;
  }

  document.querySelectorAll("[data-metadata-open]").forEach(function (button) {
    button.addEventListener("click", function () {
      openDrawer(button.getAttribute("data-metadata-open"), button);
    });
  });

  document.querySelectorAll("[data-metadata-close]").forEach(function (node) {
    node.addEventListener("click", closeDrawer);
  });

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && drawerOpen()) closeDrawer();
  });

  if (saveButton) {
    saveButton.addEventListener("click", function () {
      if (openId === null) return;
      var payload = changes();
      if (
        !Object.keys(payload.fields).length &&
        !Object.keys(payload.locks).length
      ) {
        say("没有需要保存的修改");
        return;
      }
      saveButton.disabled = true;
      say("正在保存…");
      fetch("/api/v1/works/" + openId + "/metadata", {
        method: "PATCH",
        headers: {
          Accept: "application/json",
          "Content-Type": "application/json",
          "X-CSRF-Token": csrf,
        },
        body: JSON.stringify(payload),
      })
        .then(function (response) {
          return response.json().then(function (body) {
            if (!response.ok) throw body;
            return body;
          });
        })
        .then(function () {
          /* Re-read rather than patching the inputs from the response: editing
           * a field can move the candidate out of 待补充 and can change what
           * every other field's provenance is, and the server is the only thing
           * that knows. */
          openDrawer(openId, opener, "已保存，刷新列表后生效");
        })
        .catch(function (body) {
          say((body && body.error && body.error.message) || "保存失败");
        })
        .then(function () {
          saveButton.disabled = false;
        });
    });
  }
})();
