/* Bulk selection and the rename drawer. Realtime updates live in realtime.js. */
(function () {
  "use strict";

  /* Re-read on every in-place update, because a batch action replaces the whole
   * list: the previous `form` is detached, the tab may have changed, and `live`
   * flips the moment a pack is queued. Everything below is therefore looked up
   * when it is used or rebound by `setUp`, and nothing is captured once at load.
   */
  var form = null;

  function reveal(selector) {
    document.querySelectorAll(selector).forEach(function (node) {
      node.hidden = false;
    });
  }

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

  /* Bound to the new markup after each swap. Listeners go on the elements the
   * swap brought in, so there is nothing to remove: the old ones went away with
   * the nodes they were attached to. */
  function bindSelection() {
    if (form) {
      if (!form.dataset.selectionBound) {
        form.dataset.selectionBound = "true";
        form.addEventListener("change", syncSelected);
      }
      syncSelected();
    }

    document.querySelectorAll("[data-select]").forEach(function (button) {
      if (button.dataset.bound) return;
      button.dataset.bound = "true";
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
  }

  /* ---------------------------------------------------------- rename drawer */

  /* The drawer itself is teleported out of `<main>` by Alpine, so a swap does
   * not touch it and these stay valid for the life of the document. Only the row
   * buttons that open it are replaced, which is why `bindRenameTriggers` is the
   * part that re-runs. */
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
    if (!drawer.hidden && renameForm.dataset.dirty === "true") {
      filenameField.focus();
      return;
    }
    lastTrigger = button;
    var id = button.getAttribute("data-rename-open");
    var path = button.getAttribute("data-rename-name") || "";
    var relative = button.getAttribute("data-rename-relative") || "";
    var title = button.getAttribute("data-rename-title") || "";

    renameForm.setAttribute("action", "/downloaded/" + id + "/rename");
    renameForm.elements.work_id.value = button.getAttribute("data-rename-work-id");
    renameForm.elements.expected_revision.value = button.getAttribute("data-rename-revision");
    renameForm.elements.idempotency_key.value = crypto.randomUUID();
    renameForm.elements.repack.checked = false;
    delete renameForm.dataset.dirty;
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

  function bindRenameTriggers() {
    document.querySelectorAll("[data-rename-open]").forEach(function (button) {
      if (button.dataset.bound) return;
      button.dataset.bound = "true";
      button.addEventListener("click", function () {
        openDrawer(button);
      });
    });
  }

  if (drawer) {
    drawer.querySelectorAll("[data-rename-close]").forEach(function (node) {
      node.addEventListener("click", closeDrawer);
    });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && !drawer.hidden) closeDrawer();
    });
  }

  function setUp() {
    form = document.querySelector("[data-downloaded-form]");
    if (!form) return;
    reveal("[data-select-tools]");
    reveal("[data-rename-open]");
    bindSelection();
    bindRenameTriggers();
  }
  document.addEventListener("ehbot:operation", function (event) {
    if (event.detail.form === renameForm) closeDrawer();
  });
  window.EhBotUI.onContentReady(setUp);
})();
