/* Shell behaviour: theme, density, sidebar state, and the toast region.
 *
 * No build step and no framework — R0 vendored HTMX and Alpine at fixed
 * versions and this file is plain ES2019. Everything here is per-browser
 * preference, so it is stored in localStorage rather than on the server: the
 * operator's choice of theme is not something the API should have an opinion
 * about, and R8's settings page is where a server-side default would belong.
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
  };
})();
