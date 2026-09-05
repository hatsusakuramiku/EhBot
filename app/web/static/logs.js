/* Live tail for /logs.
 *
 * The page is already a working log viewer without this file: the first page of
 * records is server-rendered and the level `<select>` submits a GET. What this
 * adds is the live part -- an `EventSource` on `/api/v1/logs/stream` that
 * prepends records as they are logged -- plus 暂停 and a level change that does
 * not tear the stream down.
 *
 * Rules it follows, the same three the other page scripts do:
 *
 * 1. **It writes no vocabulary.** A level's Chinese label and tone come from the
 *    `StatusView` the server resolved; this file reads them off the payload and
 *    never maps a code to a word. That is why a live row's badge cannot disagree
 *    with a server-rendered one.
 * 2. **It builds a row from the server's own markup.** The first rendered row is
 *    cloned as a template, so the structure lives in `logs.html` alone. When the
 *    page loaded empty there is nothing to clone, so it falls back to fetching
 *    the snapshot -- which is the same markup, rendered by the same macro, one
 *    request later -- rather than assembling a row from scratch here.
 * 3. **It reveals its own controls.** 暂停 and the status line ship `hidden`; a
 *    browser without `EventSource` never sees a pause button for a stream that
 *    was never opened.
 *
 * Why the level filter runs in the browser: the stream is one broadcast queue,
 * so a server-side filter would need the connection rebuilt on every change --
 * and the records it was opened to catch are exactly the ones lost in that gap.
 */
(function () {
  "use strict";

  var panel = document.querySelector("[data-log-panel]");
  if (!panel) return;

  var list = panel.querySelector("[data-log-list]");
  var emptyState = panel.querySelector("[data-log-empty]");
  var liveTools = panel.querySelector("[data-log-live]");
  var pauseButton = panel.querySelector("[data-log-pause]");
  var statusLine = panel.querySelector("[data-log-status]");
  var select = panel.querySelector("[data-log-level]");
  var form = panel.querySelector(".ui-log-filter");

  var streamPath = panel.getAttribute("data-stream") || "/api/v1/logs/stream";
  var level = panel.getAttribute("data-level") || "INFO";
  var limit = parseInt(panel.getAttribute("data-limit"), 10) || 100;

  /* Severity order, mirrored from `app/logs/reader.py`. Duplicated rather than
   * fetched because it is an ordering and not vocabulary: no Chinese, no tone,
   * and a level the table does not know always passes -- the same answer
   * `passes_min_level` gives, for the same reason. A line nobody can classify is
   * what an operator is hunting during an incident. */
  var SEVERITY = { DEBUG: 10, INFO: 20, WARNING: 30, ERROR: 40, CRITICAL: 50 };

  var paused = false;
  /* Records that arrived while paused, newest last. Bounded by `limit`: a pause
   * left on overnight must cost a fixed amount of memory, and the operator who
   * resumes wants the newest lines, not an hour of history. */
  var pending = [];
  var stream = null;
  var rowTemplate = null;
  var seen = {};

  function passes(code) {
    var floor = SEVERITY[level];
    var severity = SEVERITY[String(code || "").toUpperCase()];
    if (!floor || !severity) return true;
    return severity >= floor;
  }

  /* ------------------------------------------------------------- row markup */

  function captureTemplate() {
    if (rowTemplate) return rowTemplate;
    var first = list && list.querySelector("[data-log-row]");
    if (!first) return null;
    rowTemplate = first.cloneNode(true);
    return rowTemplate;
  }

  function setText(node, text) {
    if (node) node.textContent = text;
  }

  /* Fills a clone of the server's row. Every branch removes the element it has
   * no value for rather than leaving it empty: an empty `<code>` renders as a
   * stray box, and an empty `<time>` would be localised into "Invalid Date". */
  function buildRow(entry) {
    var template = captureTemplate();
    if (!template) return null;
    var row = template.cloneNode(true);

    var badge = row.querySelector(".ui-badge");
    if (badge) {
      setText(badge, entry.level.label);
      badge.setAttribute("data-tone", entry.level.tone);
    }

    var time = row.querySelector("time");
    if (time) {
      if (entry.timestamp) {
        time.setAttribute("datetime", entry.timestamp);
        time.textContent = entry.timestamp;
        /* `ui.js` skips anything already localised, and this is a fresh node. */
        delete time.dataset.localized;
        time.removeAttribute("title");
      } else {
        time.parentNode.removeChild(time);
      }
    }

    var logger = row.querySelector(".ui-log-logger");
    if (logger) {
      if (entry.logger) setText(logger, entry.logger);
      else logger.parentNode.removeChild(logger);
    }

    setText(row.querySelector(".ui-log-event"), entry.raw || entry.event);

    var message = row.querySelector(".ui-log-message");
    if (message) {
      if (entry.error_message) setText(message, entry.error_message);
      else message.parentNode.removeChild(message);
    }

    var meta = row.querySelector(".ui-log-meta");
    if (meta) {
      if (entry.error_code || entry.job_id || entry.candidate_id || entry.request_id) {
        meta.textContent = "";
        if (entry.error_code) meta.appendChild(codeNode(entry.error_code));
        if (entry.job_id) meta.appendChild(spanNode("任务 #" + entry.job_id));
        if (entry.candidate_id) {
          var link = document.createElement("a");
          link.href = "/works/" + entry.candidate_id;
          link.textContent = "作品 #" + entry.candidate_id;
          meta.appendChild(link);
        }
        if (entry.request_id) meta.appendChild(codeNode(entry.request_id));
      } else {
        meta.parentNode.removeChild(meta);
      }
    }

    var trace = row.querySelector(".ui-log-trace");
    if (trace) {
      if (entry.exception) {
        var pre = trace.querySelector("pre");
        setText(pre, entry.exception);
        /* Closed on arrival: a stack that expanded itself would push every
         * other line off the screen on the one page where position matters. */
        trace.removeAttribute("open");
      } else {
        trace.parentNode.removeChild(trace);
      }
    }
    return row;
  }

  function codeNode(text) {
    var node = document.createElement("code");
    node.textContent = text;
    return node;
  }

  function spanNode(text) {
    var node = document.createElement("span");
    node.textContent = text;
    return node;
  }

  /* ---------------------------------------------------------------- inserting */

  function prepend(entry) {
    var row = buildRow(entry);
    if (!row) {
      /* Nothing to clone yet, so the structure has to come from the server.
       * One fetch, and every later record uses the row it produced. */
      refresh();
      return;
    }
    list.insertBefore(row, list.firstChild);
    trim();
    syncEmpty();
    if (window.EhBotUI && window.EhBotUI.formatTimes) {
      window.EhBotUI.formatTimes();
    }
  }

  function trim() {
    /* The same ceiling the server applies, enforced here too: a page left open
     * on a busy deployment would otherwise grow without bound. */
    var rows = list.querySelectorAll("[data-log-row]");
    for (var i = rows.length - 1; i >= limit; i -= 1) {
      rows[i].parentNode.removeChild(rows[i]);
    }
  }

  function syncEmpty() {
    if (!emptyState) return;
    emptyState.hidden = list.querySelector("[data-log-row]") !== null;
  }

  function status(text) {
    if (statusLine) statusLine.textContent = text;
  }

  /* ------------------------------------------------------------------ refresh */

  function refresh() {
    var url = "/api/v1/logs?level=" + encodeURIComponent(level) +
      "&limit=" + encodeURIComponent(limit);
    return fetch(url, { headers: { Accept: "application/json" } })
      .then(function (response) {
        return response.ok ? response.json() : null;
      })
      .catch(function () {
        return null;
      })
      .then(function (payload) {
        if (!payload || !payload.entries) return;
        list.textContent = "";
        rowTemplate = null;
        /* Rendered from the payload, which needs a template -- and the template
         * came from a server-rendered row that has just been removed. So the
         * first entry is built from the markup captured before the clear, which
         * is why `captureTemplate` runs before `list.textContent` is emptied. */
        payload.entries.forEach(function (entry) {
          var row = buildRow(entry);
          if (row) list.appendChild(row);
        });
        syncEmpty();
        if (window.EhBotUI && window.EhBotUI.formatTimes) {
          window.EhBotUI.formatTimes();
        }
      });
  }

  /* ------------------------------------------------------------------- stream */

  function handle(event) {
    var payload;
    try {
      payload = JSON.parse(event.data);
    } catch (error) {
      /* A frame this page cannot read is still a log line, so it is shown as
       * one rather than dropped -- the same choice the file reader makes. */
      payload = { level: "LOG_OTHER", raw: String(event.data || ""), event: "" };
    }
    /* The stream replays its buffer on connect, and a reconnect replays it
     * again, so the id is what stops a record appearing twice. */
    if (event.lastEventId) {
      if (seen[event.lastEventId]) return;
      seen[event.lastEventId] = true;
    }
    var entry = normalise(payload);
    if (!passes(entry.level.code)) return;
    if (paused) {
      pending.push(entry);
      if (pending.length > limit) pending.shift();
      status("已暂停，" + pending.length + " 条待显示");
      return;
    }
    prepend(entry);
  }

  /* The stream carries the raw formatted record -- the same JSON the file holds
   * -- while `/api/v1/logs` serves entries whose level is already a resolved
   * `StatusView`. This is the one place the two shapes meet, so it is the one
   * place that has to reconcile them. A level with no view yet is given its own
   * code as the label: unmistakably raw, and never a Chinese word this file
   * chose. */
  function normalise(payload) {
    if (payload.level && typeof payload.level === "object") return payload;
    var code = String(payload.level || "LOG_OTHER").toUpperCase();
    var view = levelViews[code] || { code: code, label: code, tone: "muted" };
    return {
      level: view,
      timestamp: payload.timestamp || "",
      logger: payload.logger || "",
      event: payload.event || "",
      request_id: payload.request_id || null,
      job_id: payload.job_id || null,
      candidate_id: payload.candidate_id || null,
      error_code: payload.error_code || null,
      error_message: payload.error_message || null,
      exception: payload.exception || null,
      raw: payload.raw || null,
    };
  }

  /* Seeded from the snapshot endpoint so the labels are the server's. Until it
   * answers, `normalise` falls back to the raw code, which is honest. */
  var levelViews = {};

  function loadLevelViews() {
    return fetch("/api/v1/logs?limit=1", { headers: { Accept: "application/json" } })
      .then(function (response) {
        return response.ok ? response.json() : null;
      })
      .catch(function () {
        return null;
      })
      .then(function (payload) {
        if (!payload || !payload.entries) return;
        payload.entries.forEach(function (entry) {
          levelViews[entry.level.code] = entry.level;
        });
      });
  }

  function subscribe() {
    if (!window.EventSource) return;
    stream = new EventSource(streamPath);
    stream.addEventListener("log", handle);
    stream.onopen = function () {
      status(paused ? "已暂停" : "实时");
    };
    stream.onerror = function () {
      /* The browser reconnects on its own using the server's `retry`, and the
       * replay covers what was missed, so there is nothing to do but say so. */
      status("连接中断，正在重连");
    };
    if (liveTools) liveTools.hidden = false;
    status("实时");
  }

  /* -------------------------------------------------------------- controls */

  if (pauseButton) {
    pauseButton.addEventListener("click", function () {
      paused = !paused;
      pauseButton.setAttribute("aria-pressed", paused ? "true" : "false");
      pauseButton.textContent = paused ? "继续" : "暂停";
      if (paused) {
        status("已暂停");
        return;
      }
      /* Oldest first, because each is prepended: pushing them in arrival order
       * is what leaves the newest on top. */
      pending.forEach(prepend);
      pending = [];
      status("实时");
    });
  }

  if (select && form) {
    /* Applied without a navigation, so the stream survives a level change. The
     * URL is updated so a reload and a shared link show the same view. */
    select.addEventListener("change", function () {
      level = select.value;
      panel.setAttribute("data-level", level);
      if (window.history && window.history.replaceState) {
        window.history.replaceState(
          {}, "", "/logs?level=" + encodeURIComponent(level) +
          "&limit=" + encodeURIComponent(limit)
        );
      }
      pending = [];
      refresh();
    });
    /* The no-JavaScript submit path is now the slower duplicate of the line
     * above, so it is suppressed rather than left to reload the document. */
    form.addEventListener("submit", function (event) {
      event.preventDefault();
    });
  }

  loadLevelViews().then(subscribe);
})();
