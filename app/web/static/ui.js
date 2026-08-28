/* Shell behaviour: theme, density, sidebar state, timestamps, and the toast
 * region.
 *
 * No build step and no framework — R0 vendored HTMX and Alpine at fixed
 * versions and this file is plain ES2019. Theme, density and the sidebar are
 * per-browser preference, so they are stored in localStorage rather than on the
 * server: the operator's choice of theme is not something the API should have
 * an opinion about.
 *
 * The 时区 is the one setting here that is NOT a browser preference — it is
 * stored server-side on the 系统 tab, published as a meta tag by the shell, and
 * read from there rather than from localStorage, so every browser looking at
 * this deployment reads the same clock.
 *
 * The theme is applied by an inline snippet in <head> (see `base.html`), not by
 * this file. A deferred script runs after first paint, which would show the
 * light theme for one frame on every navigation.
 */
(function () {
  "use strict";

  var KEYS = { theme: "ehbot.theme", density: "ehbot.density", nav: "ehbot.nav" };
  var root = document.documentElement;

  /* localStorage throws in a private window with site data blocked, and the
   * shell must still render. Every access goes through these two. */
  function read(key, fallback) {
    try {
      var value = window.localStorage.getItem(key);
      return value === null ? fallback : value;
    } catch (error) {
      return fallback;
    }
  }

  function write(key, value) {
    try {
      window.localStorage.setItem(key, value);
    } catch (error) {
      /* Preference is lost on reload; nothing else breaks. */
    }
  }

  /* ----------------------------------------------------------- theme */

  function applyTheme(theme) {
    if (theme === "auto") {
      /* Removing the attribute is what lets `prefers-color-scheme` decide.
       * Setting `data-theme="auto"` would not match either theme selector. */
      root.removeAttribute("data-theme");
    } else {
      root.setAttribute("data-theme", theme);
    }
    write(KEYS.theme, theme);
    reflect("[data-theme-option]", "themeOption", theme);
  }

  function currentTheme() {
    return read(KEYS.theme, "auto");
  }

  /* --------------------------------------------------------- density */

  function applyDensity(density) {
    root.setAttribute("data-density", density);
    write(KEYS.density, density);
    reflect("[data-density-option]", "densityOption", density);
  }

  function currentDensity() {
    return read(KEYS.density, "comfortable");
  }

  /* ------------------------------------------------------- nav state */

  function applyNav(collapsed) {
    var shell = document.querySelector(".ui-shell");
    if (shell) {
      shell.setAttribute("data-collapsed", collapsed ? "true" : "false");
    }
    var toggle = document.querySelector(".ui-collapse-toggle");
    if (toggle) {
      toggle.setAttribute("aria-expanded", collapsed ? "false" : "true");
    }
    write(KEYS.nav, collapsed ? "collapsed" : "expanded");
  }

  /* `aria-pressed` is the state, so the segmented controls are announced
   * correctly rather than looking pressed only in CSS. */
  function reflect(selector, dataKey, value) {
    var buttons = document.querySelectorAll(selector);
    for (var i = 0; i < buttons.length; i += 1) {
      buttons[i].setAttribute(
        "aria-pressed",
        buttons[i].dataset[dataKey] === value ? "true" : "false"
      );
    }
  }

  /* -------------------------------------------------- timestamps */

  /* Every server-rendered `<time>` carries the stored UTC value in both its
   * `datetime` attribute and its text, so a browser with no JavaScript still
   * shows a complete timestamp. This pass rewrites the TEXT into the 时区 the
   * operator chose on the 系统 tab, and leaves `datetime` alone: that attribute
   * is the machine-readable value, and a local string there would be a lie.
   *
   * Progressive enhancement, not a requirement -- if the meta tag is missing,
   * the zone name is unusable, or a value will not parse, the page keeps the raw
   * UTC text rather than showing nothing or "Invalid Date". */
  function displayTimezone() {
    var meta = document.querySelector('meta[name="display-timezone"]');
    return (meta && meta.getAttribute("content")) || "UTC";
  }

  /* `false` means "this browser cannot format at all"; `null` means "not built
   * yet". Built once, because the zone cannot change without a page load. */
  var timeFormatter = null;

  function formatter() {
    if (timeFormatter !== null) {
      return timeFormatter;
    }
    var options = {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
      timeZone: displayTimezone(),
    };
    try {
      timeFormatter = new Intl.DateTimeFormat("zh-CN", options);
    } catch (error) {
      /* A zone the server accepted by shape but this engine does not know. UTC
       * is wrong by an offset; unformatted is wrong by a whole timestamp. */
      options.timeZone = "UTC";
      try {
        timeFormatter = new Intl.DateTimeFormat("zh-CN", options);
      } catch (fallbackError) {
        timeFormatter = false;
      }
    }
    return timeFormatter;
  }

  /* SQLite writes `CURRENT_TIMESTAMP` as "2026-08-26 07:18:45" — a space
   * separator and no zone marker. Safari rejects that form outright, and the
   * engines that accept it read it as LOCAL time, which silently shifts every
   * timestamp by the viewer's offset. Both are fixed by making the UTC that the
   * database means explicit before parsing. A value that already carries an
   * offset (a Telegram message date) is left as it is. */
  function parseTimestamp(raw) {
    var text = String(raw || "").trim();
    if (!text) {
      return null;
    }
    var normalized = text.replace(" ", "T");
    if (!/(Z|[+-]\d{2}:?\d{2})$/.test(normalized)) {
      normalized += "Z";
    }
    var parsed = new Date(normalized);
    return isNaN(parsed.getTime()) ? null : parsed;
  }

  function formatTimes() {
    var format = formatter();
    if (!format) {
      return;
    }
    var elements = document.querySelectorAll("time[datetime]");
    for (var i = 0; i < elements.length; i += 1) {
      var element = elements[i];
      if (element.dataset.localized === "true") {
        continue;
      }
      var parsed = parseTimestamp(element.getAttribute("datetime"));
      if (parsed === null) {
        continue;
      }
      element.textContent = format.format(parsed);
      /* The original stays reachable on hover, so an operator comparing a page
       * against a log line does not have to convert back by hand. */
      if (!element.title) {
        element.title = element.getAttribute("datetime") + " UTC";
      }
      element.dataset.localized = "true";
    }
  }

  /* ----------------------------------------------------------- toasts */

  /* Announcements go through one live region created at load. Creating it on
   * demand and inserting text in the same tick is the classic way to get an
   * announcement that never fires. */
  function toastRegion() {
    var region = document.querySelector(".ui-toasts");
    if (!region) {
      region = document.createElement("div");
      region.className = "ui-toasts";
      region.setAttribute("aria-live", "polite");
      region.setAttribute("aria-atomic", "false");
      document.body.appendChild(region);
    }
    return region;
  }

  function toast(message, options) {
    var settings = options || {};
    var element = document.createElement("div");
    element.className = "ui-toast";
    element.setAttribute("data-tone", settings.tone || "neutral");
    element.setAttribute("role", settings.tone === "danger" ? "alert" : "status");

    var text = document.createElement("div");
    text.className = "ui-toast-text";
    var title = document.createElement("strong");
    title.textContent = message;
    text.appendChild(title);
    if (settings.detail) {
      var detail = document.createElement("span");
      detail.textContent = settings.detail;
      text.appendChild(detail);
    }

    var close = document.createElement("button");
    close.className = "ui-btn";
    close.setAttribute("data-variant", "ghost");
    close.setAttribute("data-size", "icon");
    close.setAttribute("type", "button");
    close.setAttribute("aria-label", "关闭提示");
    close.textContent = "✕";
    close.addEventListener("click", function () {
      element.remove();
    });

    element.appendChild(text);
    element.appendChild(close);
    toastRegion().appendChild(element);

    /* An error stays until dismissed: it usually names something the operator
     * has to act on, and a 5-second window is not enough to read a path. */
    if (settings.tone !== "danger") {
      window.setTimeout(function () {
        element.remove();
      }, settings.duration || 5000);
    }
    return element;
  }

  /* ------------------------------------------------------------ covers */

  /* A cover card renders with `data-pending="true"`, which shimmers a skeleton
   * under the thumbnail while the proxy fetch is in flight. Clearing it here
   * rather than in CSS is the only option: `loading="lazy"` means the request
   * may not even start until the card is scrolled into view, and CSS cannot
   * observe an image's load state.
   *
   * `error` settles it too — a failed proxy fetch shows the alt box, and
   * shimmering forever would promise an image that is never coming. */
  function settleCover(image) {
    var cover = image.closest(".ui-card-cover");
    if (cover) {
      cover.removeAttribute("data-pending");
    }
  }

  function settleLoadedCovers() {
    var images = document.querySelectorAll('.ui-card-cover[data-pending="true"] img');
    for (var i = 0; i < images.length; i += 1) {
      /* Cached covers can finish before this file runs, and a `load` listener
       * added afterwards never fires for them. */
      if (images[i].complete) {
        settleCover(images[i]);
      }
    }
  }

  function bindCovers() {
    /* `load` and `error` do not bubble, so the listener has to capture. One
     * pair on the document covers every card, including the ones HTMX swaps in
     * later. */
    function handler(event) {
      var target = event.target;
      if (target && target.tagName === "IMG") {
        settleCover(target);
      }
    }
    document.addEventListener("load", handler, true);
    document.addEventListener("error", handler, true);
    settleLoadedCovers();
  }

  /* ------------------------------------------------------------ wiring */

  function bind() {
    document.addEventListener("click", function (event) {
      var themeButton = event.target.closest("[data-theme-option]");
      if (themeButton) {
        applyTheme(themeButton.dataset.themeOption);
        return;
      }
      var densityButton = event.target.closest("[data-density-option]");
      if (densityButton) {
        applyDensity(densityButton.dataset.densityOption);
        return;
      }
      var collapse = event.target.closest(".ui-collapse-toggle");
      if (collapse) {
        var shell = document.querySelector(".ui-shell");
        applyNav(shell ? shell.getAttribute("data-collapsed") !== "true" : true);
      }
    });

    /* Reflect the stored values onto controls that only exist after the DOM is
     * ready — the inline head snippet has already set the attributes. */
    reflect("[data-theme-option]", "themeOption", currentTheme());
    reflect("[data-density-option]", "densityOption", currentDensity());
    applyNav(read(KEYS.nav, "expanded") === "collapsed");
    formatTimes();
    bindCovers();

    /* HTMX replaces markup long after this file ran, so timestamps that arrive
     * with a swap need the same pass. Re-scanning the whole document is cheaper
     * than it looks: `data-localized` makes every element already handled a
     * single attribute read. */
    document.body.addEventListener("htmx:load", formatTimes);
    /* A swapped-in card whose cover was already in the browser cache is
     * `complete` on arrival, so it needs the same catch-up pass. */
    document.body.addEventListener("htmx:load", settleLoadedCovers);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bind);
  } else {
    bind();
  }

  window.EhBotUI = {
    toast: toast,
    applyTheme: applyTheme,
    applyDensity: applyDensity,
    currentTheme: currentTheme,
    currentDensity: currentDensity,
    formatTimes: formatTimes,
  };
})();
