/* Live refresh for /works/{id}.
 *
 * The page works without this file: every action is a form, the timeline is
 * server-rendered, and a reload shows the current state. What this adds is that
 * an operator watching a download does not have to reload to see it finish.
 * The same three rules `activity.js` follows:
 *
 * 1. It patches values, never structure. A timeline node's markup is written
 *    once, by `ui.timeline_node`. When a poll finds a node the page has never
 *    rendered -- a new job, a new audit entry -- it says so and offers a reload
 *    instead of building one here.
 * 2. It writes no vocabulary. Every label and tone in a patch comes from the
 *    payload `app/api/status.py` resolved. There is no state name in this file.
 * 3. It stops when nothing is moving. `live: false` from the server ends the
 *    polling, and a hidden tab drops to the idle cadence.
 *
 * Metadata editing is deliberately not here. Each field is already a working
 * one-field form, and the JSON drawer on `/candidates` is the fast path; a third
 * implementation of the same PATCH would be one more thing to keep in step.
 */
(function () {
  "use strict";

  var root = document.querySelector("[data-work-root]");
  if (!root) return;

  var workId = root.getAttribute("data-work-id");
  if (!workId) return;

  /* Fallbacks only. The real cadence comes from `/api/v1/meta`, so it stays a
   * server decision an operator can change in one place. */
  var interval = 2000;
  var idleInterval = 15000;

  var timer = null;
  var stream = null;
  var stopped = root.getAttribute("data-live") !== "true";
  var inFlight = false;
  var renderedNodes = parseInt(root.getAttribute("data-timeline"), 10) || 0;

  /* ------------------------------------------------------------- patching */

  function badgeInto(host, view) {
    /* One badge per host element, replaced in place. Mirrors the `badge` macro
     * attribute for attribute -- `data-code` rather than `title`, so the raw
     * enum stays out of the tooltip and out of the accessibility tree. */
    if (!host) return;
    if (!view) {
      host.textContent = "";
      return;
    }
    var badge = host.firstElementChild;
    if (!badge || badge.className !== "badge") {
      host.textContent = "";
      badge = document.createElement("span");
      badge.className = "badge";
      host.appendChild(badge);
    }
    badge.setAttribute("data-tone", view.tone);
    badge.setAttribute("data-live", view.live ? "true" : "false");
    badge.setAttribute("data-code", view.code);
    badge.textContent = view.label;
  }

  function slot(host, name) {
    return host ? host.querySelector('[data-field="' + name + '"]') : null;
  }

  function apply(snapshot) {
    badgeInto(slot(document, "stage"), snapshot.stage);
    badgeInto(slot(document, "status"), snapshot.status);

    var missing = false;
    snapshot.timeline.forEach(function (node) {
      if (node.kind !== "JOB") return;
      var host = document.querySelector(
        '[data-timeline-key="job:' + node.job.job_id + '"]'
      );
      if (!host) {
        missing = true;
        return;
      }
      badgeInto(slot(host, "state"), node.state);
      var reason = slot(host, "reason");
      if (reason) reason.textContent = node.reason || "";
    });

    /* A node count that grew means an audit entry the page has never rendered,
     * which is structure -- the same class of change as a job it has never
     * seen. Both are announced rather than built. */
    if (snapshot.timeline.length !== renderedNodes) missing = true;
    if (missing) announceChange();

    /* The action bar depends on what the work can still do, and that is a
     * server decision this file will not re-derive. So when the work stops
     * moving, the page says the actions may have changed rather than trying to
     * rewrite them. */
    if (!snapshot.live) {
      announceChange();
      stop();
    }
  }

  function announceChange() {
    var host = document.querySelector("[data-work-changed]");
    if (!host || host.getAttribute("data-shown") === "true") return;
    host.setAttribute("data-shown", "true");
    host.hidden = false;
    host.className = "ui-callout";
    host.setAttribute("data-tone", "active");
    host.setAttribute("role", "status");
    var body = document.createElement("div");
    body.className = "ui-callout-body";
    var title = document.createElement("span");
    title.className = "ui-callout-title";
    title.textContent = "这本书有新的进展";
    body.appendChild(title);
    var actions = document.createElement("div");
    actions.className = "ui-callout-actions";
    var reload = document.createElement("button");
    reload.className = "ui-btn";
    reload.setAttribute("data-size", "sm");
    reload.setAttribute("type", "button");
    reload.textContent = "刷新查看";
    reload.addEventListener("click", function () {
      window.location.reload();
    });
    actions.appendChild(reload);
    host.appendChild(body);
    host.appendChild(actions);
  }

  /* -------------------------------------------------------------- polling */

  function poll() {
    if (inFlight || stopped) return;
    inFlight = true;
    fetch("/api/v1/works/" + encodeURIComponent(workId), {
      headers: { Accept: "application/json" },
    })
      .then(function (response) {
        if (!response.ok) throw new Error("work " + response.status);
        return response.json();
      })
      .then(apply)
      .catch(function () {
        /* A failed poll is not worth a toast: the next one is seconds away and
         * the operator's own reload is the escalation. */
      })
      .then(function () {
        inFlight = false;
      });
  }

  function arm() {
    clear();
    if (stopped) return;
    var wait = document.visibilityState === "visible" ? interval : idleInterval;
    timer = window.setInterval(poll, wait);
  }

  function clear() {
    if (timer !== null) {
      window.clearInterval(timer);
      timer = null;
    }
  }

  function stop() {
    stopped = true;
    clear();
    if (stream) {
      stream.close();
      stream = null;
    }
  }

  /* --------------------------------------------------------------- events */

  function subscribe() {
    if (!window.EventSource) return;
    stream = new EventSource("/api/v1/events");
    /* The event carries only an id, so this fetches the snapshot rather than
     * trusting the payload: the page shows one consistent read instead of a
     * patchwork. Events for other works are ignored. */
    ["candidate", "download", "conversion"].forEach(function (name) {
      stream.addEventListener(name, function () {
        stopped = false;
        poll();
        if (timer === null) arm();
      });
    });
    stream.onerror = function () {
      /* The browser reconnects using the server's `retry`; polling is the
       * fallback either way. */
    };
  }

  /* ---------------------------------------------------------------- start */

  fetch("/api/v1/meta", { headers: { Accept: "application/json" } })
    .then(function (response) {
      return response.ok ? response.json() : null;
    })
    .catch(function () {
      return null;
    })
    .then(function (meta) {
      if (meta && meta.polling) {
        interval = meta.polling.interval_ms || interval;
        idleInterval = meta.polling.idle_interval_ms || idleInterval;
      }
      arm();
      subscribe();
    });

  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible") poll();
    arm();
  });
})();
