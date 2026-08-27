/* Live progress on the activity page.
 *
 * Replaces `<meta http-equiv="refresh">`, which reloaded the whole document
 * every few seconds and took scroll position, any open menu and any checkbox
 * selection with it. Three rules shape what this file is allowed to do:
 *
 * 1. It patches values, never structure. A row's markup is written once, by the
 *    `job_row` macro in `activity.html`. Building rows here would be a second
 *    copy of that macro, and the two would drift — the exact duplication this
 *    refactor exists to remove. When a poll finds a job the page has never
 *    rendered, it says so and offers a reload instead.
 * 2. It writes no vocabulary. Every label and tone in a patch comes from the
 *    payload, which `app/api/status.py` resolved. There is no state name in this
 *    file.
 * 3. It stops when nothing is moving. `live: false` from the server ends the
 *    polling entirely, and a hidden tab pauses it, so an idle browser left open
 *    overnight costs nothing.
 */
(function () {
  "use strict";

  var root = document.querySelector("[data-activity-root]");
  if (!root) return;

  /* Fallbacks only. The real values come from `/api/v1/meta`, so the cadence is
   * a server decision an operator can change in one place. */
  var interval = 2000;
  var idleInterval = 15000;

  var timer = null;
  var stream = null;
  /* Set once the server says nothing is advancing on its own. */
  var stopped = root.getAttribute("data-live") !== "true";
  var inFlight = false;

  /* ------------------------------------------------------------ patching */

  function badgeInto(host, view) {
    /* One badge per host element, replaced in place. Mirrors the `badge` macro
     * in `components/ui.html` attribute for attribute -- including `data-code`
     * rather than `title`, so the raw enum stays out of the tooltip and out of
     * the accessibility tree. */
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

  function field(row, name) {
    return row.querySelector('[data-field="' + name + '"]');
  }

  function text(row, name, value) {
    var node = field(row, name);
    if (node) node.textContent = value === null || value === undefined ? "" : value;
  }

  function patchRow(row, job) {
    badgeInto(field(row, "state"), job.state);
    badgeInto(field(row, "attention"), job.attention);
    badgeInto(field(row, "note"), job.note);
    text(row, "detail", job.torrent.detail);
    text(row, "priority", job.priority);
    text(row, "attempt", job.attempt_count);
    text(row, "error", job.error_message);
    text(
      row,
      "stalled",
      job.torrent.stalled_minutes === null
        ? ""
        : "已停滞 " + job.torrent.stalled_minutes + " 分钟"
    );
    var progress = field(row, "progress");
    if (progress) progress.value = job.progress_percent;
  }

  /* -------------------------------------------------------------- polling */

  function sectionsFor(snapshot) {
    return root.getAttribute("data-tab") === "packing"
      ? snapshot.packing
      : snapshot.downloads;
  }

  function apply(snapshot) {
    /* Group counts and the banner are patched from the same payload as the
     * rows, so a heading can never claim a number the rows contradict. */
    var seen = {};
    var moved = false;
    sectionsFor(snapshot).forEach(function (section) {
      var host = document.querySelector(
        '[data-group="' + section.group.code + '"]'
      );
      if (host) {
        var count = host.querySelector('[data-field="group-count"]');
        if (count) count.textContent = section.count;
      }
      section.jobs.forEach(function (job) {
        seen[job.job_id] = true;
        var row = document.querySelector('[data-job-id="' + job.job_id + '"]');
        if (!row) {
          moved = true;
          return;
        }
        /* A job that changed section has to move between two tables, which is
         * structure — so it counts as a membership change, not a patch. */
        if (host && !host.contains(row)) moved = true;
        patchRow(row, job);
      });
    });

    /* A row the page still shows but the snapshot no longer lists has left the
     * queue (finished, cancelled). Also a membership change. */
    var rows = document.querySelectorAll("[data-queue-sections] [data-job-id]");
    for (var i = 0; i < rows.length; i += 1) {
      if (!seen[rows[i].getAttribute("data-job-id")]) moved = true;
    }

    renderAttention(snapshot.attention);
    if (moved) announceChange();
    if (!snapshot.live) stop();
  }

  function renderAttention(attention) {
    var host = document.querySelector("[data-attention-banner]");
    if (!host) return;
    if (!attention.total) {
      host.textContent = "";
      return;
    }
    var callout = host.firstElementChild;
    if (!callout) {
      /* Built here only when the page loaded with nothing needing attention.
       * The structure matches `ui.callout`, and it holds no vocabulary of its
       * own — the title counts, the badges come from the payload. */
      callout = document.createElement("div");
      callout.className = "ui-callout";
      callout.setAttribute("data-tone", "danger");
      callout.setAttribute("role", "alert");
      var body = document.createElement("div");
      body.className = "ui-callout-body";
      var title = document.createElement("span");
      title.className = "ui-callout-title";
      var items = document.createElement("span");
      items.className = "ui-callout-items";
      body.appendChild(title);
      body.appendChild(items);
      callout.appendChild(body);
      host.appendChild(callout);
    }
    callout.querySelector(".ui-callout-title").textContent =
      "有 " + attention.total + " 项任务需要处理";
    var items = callout.querySelector(".ui-callout-items");
    items.textContent = "";
    attention.reasons.forEach(function (entry) {
      var slot = document.createElement("span");
      items.appendChild(slot);
      badgeInto(slot, entry.reason);
      var count = document.createElement("span");
      count.className = "ui-hint";
      count.style.margin = "0";
      count.textContent = entry.count;
      items.appendChild(count);
    });
  }

  function announceChange() {
    /* Deliberately not an automatic reload: the operator may be halfway through
     * a selection, and pulling the page out from under them to show a row that
     * just finished is worse than a line they can ignore. */
    var host = document.querySelector("[data-queue-changed]");
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
    title.textContent = "队列有新的变动";
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

  function poll() {
    if (inFlight || stopped) return;
    inFlight = true;
    fetch("/api/v1/queue", { headers: { Accept: "application/json" } })
      .then(function (response) {
        if (!response.ok) throw new Error("queue " + response.status);
        return response.json();
      })
      .then(apply)
      .catch(function () {
        /* A failed poll is not worth a toast: the next one is two seconds away,
         * and the operator's own reload is the escalation. */
      })
      .then(function () {
        inFlight = false;
      });
  }

  /* -------------------------------------------------------- scheduling */

  function arm() {
    clear();
    if (stopped) return;
    /* Hidden tabs poll on the idle cadence rather than not at all, so a tab
     * brought back to the front is at most one idle interval stale — and the
     * visible cadence resumes immediately on `visibilitychange`. */
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

  /* ------------------------------------------------------------- events */

  function subscribe() {
    if (!window.EventSource) return;
    stream = new EventSource("/api/v1/events");
    /* A worker finishing publishes immediately, so the page reflects a
     * completed job in well under the polling interval. The event carries only
     * an id: this fetches the snapshot rather than trusting the payload, so the
     * page shows one consistent read instead of a patchwork. */
    ["download", "conversion"].forEach(function (name) {
      stream.addEventListener(name, function () {
        /* The event means something moved, which can also mean it moved back
         * into a live state — so undo the stop before polling. */
        stopped = false;
        poll();
        if (timer === null) arm();
      });
    });
    stream.onerror = function () {
      /* The browser reconnects on its own using the server's `retry`. Polling
       * is the fallback either way, so there is nothing to do here. */
    };
  }

  /* -------------------------------------------------------------- start */

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
