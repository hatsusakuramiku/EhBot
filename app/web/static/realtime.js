/* One WebSocket per document. Commands reuse the server's route catalogue;
 * state events invalidate server-rendered fragments without page polling. */
(function () {
  "use strict";
  var meta = document.querySelector('meta[name="realtime-path"]');
  if (!meta) return;
  var prefix = meta.content.replace(/\/ws\/ui$/, "");
  var socket = null, connected = false, retry = 0, reconnectTimer = null;
  var catalogue = {}, pending = {}, nextId = 0, listeners = {};
  var epoch = null, sequence = 0, current = location.pathname + location.search;
  var revision = 0, refreshing = false, refreshAgain = false, refreshTimer = null;
  var writing = 0, menu = null, menuTag = null;
  var logSequence = 0, logEpoch = null;

  function emit(name, frame) {
    (listeners[name] || []).forEach(function (fn) { fn(frame); });
  }
  function on(name, fn) {
    (listeners[name] || (listeners[name] = [])).push(fn);
  }
  function local(url) {
    var parsed = new URL(url, location.href);
    if (parsed.origin !== location.origin) throw new Error("外部地址不能作为实时命令");
    if (prefix && parsed.pathname !== prefix && parsed.pathname.indexOf(prefix + "/") !== 0) {
      parsed.pathname = prefix + parsed.pathname;
    }
    return parsed;
  }
  function match(method, url) {
    var path = local(url).pathname.slice(prefix.length);
    var names = Object.keys(catalogue);
    for (var i = 0; i < names.length; i++) {
      var entry = catalogue[names[i]];
      if (entry.method !== method) continue;
      var keys = [], pattern = entry.path.split("/").map(function (part) {
        if (part.charAt(0) === "{") { keys.push(part.slice(1, -1)); return "([^/]+)"; }
        return part.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
      }).join("/");
      var found = new RegExp("^" + pattern + "$").exec(path);
      if (found) {
        var params = {};
        keys.forEach(function (key, index) { params[key] = decodeURIComponent(found[index + 1]); });
        return { name: names[i], params: params, entry: entry };
      }
    }
    return null;
  }
  function command(name, payload) {
    return new Promise(function (resolve, reject) {
      if (!connected) { reject(new Error("实时连接未就绪")); return; }
      var id = String(++nextId);
      var timeout = window.setTimeout(function () {
        delete pending[id];
        reject(new Error("操作结果尚未确认，请核对当前状态后再操作"));
      }, 120000);
      pending[id] = { resolve: resolve, reject: reject, timeout: timeout };
      socket.send(JSON.stringify({ type: "command", request_id: id, name: name, payload: payload }));
    });
  }
  function request(method, url, body) {
    var parsed = local(url), item = match(method, url);
    if (!connected && method !== "GET") {
      var keys = Array.from(parsed.searchParams.keys());
      local(current).searchParams.forEach(function (value, key) {
        if (keys.indexOf(key) === -1) parsed.searchParams.append(key, value);
      });
      return window.fetch(parsed.href, { method: method, body: new URLSearchParams(body),
        headers: { "HX-Request": "true", "Content-Type": "application/x-www-form-urlencoded" },
      }).then(function (response) {
        var result = local(response.url);
        if (/\/(login|change-password)$/.test(result.pathname)) throw new Error("认证已失效，请重新登录");
        var error = result.searchParams.get("error");
        var json = (response.headers.get("Content-Type") || "").indexOf("application/json") === 0;
        return (json ? response.json() : response.text()).then(function (data) {
          return { type: response.ok && !error ? "command.result" : "command.error",
            format: json ? "json" : "html", data: data, status: response.status, location: result.pathname + result.search,
            error: { message: error || errorMessage(data, response.status) } };
        });
      });
    }
    if (!item) return Promise.reject(new Error("未注册的实时操作"));
    return command(item.name, {
      params: item.params, query: Array.from(parsed.searchParams.entries()),
      body: body === undefined ? (item.entry.encoding === "form" ? [] : null) : body,
      context: current,
    });
  }
  function errorMessage(data, status) {
    if (data && data.error) return data.error.message;
    if (data && typeof data.detail === "string") return data.detail;
    if (data && Array.isArray(data.detail)) return data.detail.map(function (item) {
      return item.loc.join(".") + ": " + item.msg;
    }).join("; ");
    if (typeof data === "string") {
      var notice = new DOMParser().parseFromString(data, "text/html").querySelector('.ui-callout[data-tone="danger"]');
      if (notice) return notice.textContent.trim();
    }
    return "操作失败（HTTP " + status + "）";
  }
  /* Existing JSON drawers use the same command channel; HTTP is a fallback
   * only before submission. A sent write is never replayed after disconnect. */
  function apiFetch(url, options) {
    options = options || {};
    var method = (options.method || "GET").toUpperCase();
    if (!connected) return window.fetch(local(url).href, options);
    var body = options.body ? JSON.parse(options.body) : undefined;
    return request(method, url, body).then(function (frame) {
      return { ok: frame.type === "command.result", status: frame.status || 400,
        json: function () { return Promise.resolve(frame.data || { error: frame.error }); } };
    });
  }
  function connectionStatus(text) {
    var host = document.querySelector("[data-realtime-status]");
    if (!host) return;
    host.textContent = text || "";
    host.hidden = false;
    emit("connection", { connected: connected });
  }
  function connect() {
    window.clearTimeout(reconnectTimer);
    socket = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + meta.content);
    socket.onopen = function () {
      var csrf = document.querySelector('meta[name="csrf-token"]');
      socket.send(JSON.stringify({ type: "hello", protocol: "ehbot-realtime.v1", client: "web",
        csrf_token: csrf ? csrf.content : "", logs: !!document.querySelector("[data-log-panel]") }));
    };
    socket.onmessage = function (event) {
      var frame;
      try { frame = JSON.parse(event.data); } catch (_) { return; }
      if (frame.type === "ready") {
        catalogue = frame.commands;
        epoch = frame.epoch; sequence = frame.sequence;
        connected = true; retry = 0;
        connectionStatus("实时已连接");
        emit("ready", frame);
        if (!document.querySelector("[data-log-panel]")) scheduleRefresh();
      } else if (frame.type === "ping") {
        socket.send(JSON.stringify({ type: "pong" }));
        if (frame.epoch !== epoch || frame.sequence > sequence) scheduleRefresh();
      } else if (frame.type === "state") {
        if (frame.epoch !== epoch || frame.sequence > sequence + 1) scheduleRefresh();
        epoch = frame.epoch;
        sequence = Math.max(sequence, frame.sequence);
        emit("state", frame);
        if (relevant(frame)) scheduleRefresh();
      } else if (frame.type === "log") {
        if (logEpoch === frame.epoch && logSequence && frame.sequence > logSequence + 1) emit("log_gap", frame);
        logEpoch = frame.epoch; logSequence = frame.sequence;
        emit("log", frame);
      } else if (frame.type === "command.result" || frame.type === "command.error") {
        var job = pending[frame.request_id];
        if (job) {
          delete pending[frame.request_id];
          window.clearTimeout(job.timeout);
          job.resolve(frame);
        }
      } else if (frame.type === "error") {
        connectionStatus(frame.error.message);
      }
    };
    socket.onclose = function (event) {
      connected = false;
      Object.keys(pending).forEach(function (id) {
        var job = pending[id]; window.clearTimeout(job.timeout);
        job.reject(new Error("连接中断，操作结果可能尚未确认；请先核对状态"));
        delete pending[id];
      });
      connectionStatus(event.code === 4401 || event.code === 4403 ? "需要重新登录" : "实时重连中");
      if (event.code !== 4401 && event.code !== 4403) {
        function retryConnection() {
          var delay = Math.min(30000, 1000 * Math.pow(2, retry++)) * (0.8 + Math.random() * 0.4);
          reconnectTimer = window.setTimeout(connect, delay);
        }
        // Rejected HTTP upgrades appear as 1006 in browsers. Distinguish an
        // expired session from a temporary proxy failure before retrying.
        if (event.code === 1006) {
          window.fetch(prefix + "/api/v1/realtime").then(function (response) {
            if (response.status === 401 || response.status === 403) connectionStatus("需要重新登录");
            else retryConnection();
          }).catch(retryConnection);
        } else retryConnection();
      }
    };
  }
  function relevant(frame) {
    var path = local(current).pathname.slice(prefix.length);
    if (path === "/logs") return false;
    if (path.indexOf("/works/") === 0 && frame.data && frame.data.candidate_id) {
      return path === "/works/" + frame.data.candidate_id;
    }
    if (path.indexOf("/settings") === 0) return frame.scope === "connection" || frame.scope === "refresh";
    return true;
  }
  function scheduleRefresh() {
    refreshAgain = true;
    if (refreshing || writing || refreshTimer) return;
    refreshTimer = window.setTimeout(function () { refreshTimer = null; refresh(); }, 100);
  }
  function refresh() {
    if (document.querySelector("[data-log-panel]")) { refreshAgain = false; emit("refresh_request", {}); return; }
    if (refreshing || writing) { refreshAgain = true; return; }
    refreshing = true; refreshAgain = false;
    var version = revision;
    var promise = connected ? request("GET", current) : window.fetch(local(current).href, {
      headers: { "HX-Request": "true" },
    }).then(function (response) {
      if (response.redirected && /\/(login|change-password)$/.test(new URL(response.url).pathname)) {
        throw new Error("认证已失效，请重新登录");
      }
      return response.text().then(function (html) { return { format: "html", data: html, status: response.status }; });
    });
    promise.then(function (frame) {
      if (version === revision && frame.format === "html" && frame.status < 400) swap(frame.data);
      if (frame.sequence !== undefined) {
        if (frame.sequence < sequence) refreshAgain = true;
        else sequence = frame.sequence;
      }
    }).catch(function (error) {
      connectionStatus(error.message || "更新失败");
    }).finally(function () {
      refreshing = false;
      if (refreshAgain && connected) scheduleRefresh();
    });
  }

  /* Re-render with the same Jinja template. Untouched keyed rows are retained;
   * dirty forms survive through hx-preserve, and selections/focus/scroll are
   * restored after HTMX/Alpine finish adopting changed markup. */
  function swap(html, submitted, writingResponse, preserveSecretDraft) {
    // A rejected settings response never echoes a password. Keep only the
    // browser's current input, alongside the server's new conflict controls.
    var torrentPassword = preserveSecretDraft && submitted && submitted.matches('[data-settings-action="torrent"]')
      ? submitted.elements.password.value : null;
    var aiKeys = preserveSecretDraft && submitted && submitted.matches('[data-ai-catalogue-command="keys-add"]')
      ? submitted.elements.api_keys.value : null;
    var vaultPassword = preserveSecretDraft && submitted && submitted.matches('[data-credential-command="add"]')
      ? submitted.elements.password.value : null;
    closeMenu();
    var main = document.getElementById("main");
    if (!main || !window.htmx) return;
    if (!writingResponse && main.querySelector("[data-source-form][data-dirty]")) {
      connectionStatus("来源配置有更新，未保存草稿已保留；提交时将校验原版本");
      return;
    }
    if (!writingResponse && main.querySelector("[data-settings-action][data-dirty]")) {
      connectionStatus("设置有更新，未保存草稿已保留；提交时将校验原版本");
    }
    if (!writingResponse && main.querySelector('[data-candidate-form] input[name="candidate_ids"]:checked, [data-downloaded-form] input[name="candidate_ids"]:checked') &&
        main.querySelector('[data-candidate-form] input[name^="expected_revision_"], [data-downloaded-form] input[name^="expected_revision_"]')) {
      connectionStatus("列表有更新，已保留当前选择与原版本；提交时将校验冲突");
      return;
    }
    var parsed = new DOMParser().parseFromString(html, "text/html");
    var fragment = parsed.getElementById("main") || parsed.body;
    var revealedKey = !writingResponse && main.querySelector('[data-api-key-reveal][data-one-time-secret]');
    var freshReveal = fragment.querySelector('[data-api-key-reveal]');
    if (revealedKey && freshReveal && revealedKey.dataset.publicId === freshReveal.dataset.publicId) {
      freshReveal.id = revealedKey.id;
      freshReveal.setAttribute("hx-preserve", "true");
    }
    var checked = Array.from(main.querySelectorAll('input[name="candidate_ids"]:checked,input[name="job_ids"]:checked')).map(function (node) {
      return [node.name, node.value];
    });
    var focus = document.activeElement, focusId = focus && focus.id;
    var focusKey = focus && focus.name ? [focus.name, focus.value] : null;
    var x = window.scrollX, y = window.scrollY;
    var selection = focus && typeof focus.selectionStart === "number" ? [focus.selectionStart, focus.selectionEnd] : null;
    var forms = Array.from(main.querySelectorAll("form")), freshForms = Array.from(fragment.querySelectorAll("form"));
    var drawer = main.querySelector("[data-metadata-drawer]");
    var freshDrawer = fragment.querySelector("[data-metadata-drawer]");
    if (drawer && freshDrawer && !drawer.hidden) {
      drawer.id = freshDrawer.id = "metadata-drawer";
      freshDrawer.setAttribute("hx-preserve", "true");
    }
    function formKey(form) {
      if (form.hasAttribute("data-work-source")) return "work-source:" + form.getAttribute("data-work-source");
      if (form.hasAttribute("data-settings-action")) return "settings:" + form.getAttribute("data-settings-action");
      if (form.hasAttribute("data-manual-form")) return "manual-add";
      if (form.hasAttribute("data-source-form")) return "source:" + form.getAttribute("data-source-form");
      if (form.matches("[data-metadata-editor]") && form.id) return form.id;
      return [form.getAttribute("action"), form.getAttribute("method"), Array.from(form.querySelectorAll('input[type="hidden"]'))
        .filter(function (node) { return node.name !== "csrf_token" && node.name !== "return_to"; })
        .map(function (node) { return node.name + "=" + node.value; }).join("&")].join("|");
    }
    var freshByKey = {};
    freshForms.forEach(function (form) { var key = formKey(form); (freshByKey[key] || (freshByKey[key] = [])).push(form); });
    forms.forEach(function (form, index) {
      var fresh = (freshByKey[formKey(form)] || []).shift();
      if (form === submitted) { delete form.dataset.dirty; return; }
        if (!fresh && form.dataset.dirty && form.matches("[data-work-source],[data-source-form],[data-rule-command],[data-profile-command],[data-ai-provider-command],[data-ai-catalogue-command],[data-ai-chain-command],[data-ai-network-command],[data-credential-command]")) {
        fresh = form.cloneNode(true);
        fragment.appendChild(fresh);
      }
      if (!form.dataset.dirty || !fresh) return;
      if (!form.id) form.id = "realtime-form-" + index;
      fresh.id = form.id;
      fresh.setAttribute("hx-preserve", "true");
      form.setAttribute("hx-preserve", "true");
    });
    ["data-work-id", "data-job-id", "data-candidate-id"].forEach(function (attribute) {
      main.querySelectorAll("[" + attribute + "]").forEach(function (node) {
        var value = node.getAttribute(attribute);
        var fresh = fragment.querySelector("[" + attribute + '="' + value + '"]');
        if (!fresh || node.hasAttribute("data-work-root") || node.querySelector("[data-dirty]")) return;
        // Selection and JS-revealed controls are transient; compare rendered
        // markup only before deciding to keep an unchanged row.
        var copy = node.cloneNode(true);
        copy.querySelectorAll("[data-selected]").forEach(function (el) { el.removeAttribute("data-selected"); });
        copy.removeAttribute("data-selected");
        copy.querySelectorAll("[data-select-tools],[data-rename-open],[data-metadata-open]").forEach(function (el) { el.hidden = true; });
        copy.querySelectorAll("[data-bound],[data-selection-bound]").forEach(function (el) { el.removeAttribute("data-bound"); el.removeAttribute("data-selection-bound"); });
        if (copy.id.indexOf("realtime-row-") === 0) copy.removeAttribute("id");
        if (copy.outerHTML !== fresh.outerHTML) return;
        if (!node.id) node.id = "realtime-row-" + attribute + "-" + value;
        fresh.id = node.id; fresh.setAttribute("hx-preserve", "true");
      });
    });
    var opened = Array.from(main.querySelectorAll("details")).map(function (node) { return node.open; });
    function restore() {
      if (vaultPassword !== null) {
        var vaultDraft = main.querySelector('[data-credential-draft][data-credential-command="add"]');
        if (vaultDraft) vaultDraft.elements.password.value = vaultPassword;
      }
      if (torrentPassword !== null) {
        var torrentForm = main.querySelector('[data-settings-action="torrent"]');
        if (torrentForm) torrentForm.elements.password.value = torrentPassword;
      }
      if (aiKeys !== null) {
        var aiDraft = main.querySelector('[data-ai-catalogue-draft][data-ai-catalogue-command="keys-add"]');
        if (aiDraft) aiDraft.elements.api_keys.value = aiKeys;
      }
      main.querySelectorAll('input[type="checkbox"]').forEach(function (node) {
        if (checked.some(function (pair) { return pair[0] === node.name && pair[1] === node.value; })) {
          node.checked = true;
          if (node.name === "job_ids") node.dispatchEvent(new Event("change", { bubbles: true }));
        }
      });
      main.querySelectorAll("details").forEach(function (node, index) { if (opened[index]) node.open = true; });
      var active = focus && focus.isConnected ? focus : (focusId && document.getElementById(focusId));
      if (!active && focusKey) active = Array.from(main.querySelectorAll("input,select,textarea,button")).find(function (node) {
        return node.name === focusKey[0] && node.value === focusKey[1];
      });
      if (active && active.focus) {
        active.focus({ preventScroll: true });
        if (selection && active.setSelectionRange) active.setSelectionRange(selection[0], selection[1]);
      }
      main.querySelectorAll("[data-candidate-form],[data-downloaded-form],#queue-batch").forEach(function (form) {
        form.dispatchEvent(new Event("change", { bubbles: true }));
      });
      window.scrollTo(x, y);
    }
    window.htmx.swap(main, fragment.innerHTML, { swapStyle: "innerHTML", settleDelay: 20 }, { afterSettleCallback: restore });
  }
  function load(url) {
    document.querySelectorAll('[data-one-time-secret]').forEach(function (node) { node.remove(); });
    document.querySelectorAll("#candidate-filters,#downloaded-filters").forEach(function (form) { delete form.dataset.dirty; });
    document.querySelectorAll('[data-candidate-form] input[name="candidate_ids"]:checked, [data-downloaded-form] input[name="candidate_ids"]:checked').forEach(function (node) {
      node.checked = false;
      node.dispatchEvent(new Event("change", { bubbles: true }));
    });
    current = local(url).pathname + local(url).search;
    revision += 1;
    scheduleRefresh();
    if (!connected) refresh();
  }
  function filterUrl(form, data, url) {
    var target = local(url), page = local(current);
    if (target.pathname === page.pathname) target.search = page.search;
    Array.from(form.elements).forEach(function (node) {
      if (node.name) target.searchParams.delete(node.name);
    });
    data.forEach(function (pair) { target.searchParams.append(pair[0], pair[1]); });
    ["page", "error", "notice"].forEach(function (key) { target.searchParams.delete(key); });
    return target;
  }
  function filterTag(tag, exclude) {
    var form = document.querySelector("#candidate-filters,#downloaded-filters");
    if (!form) return;
    var target = filterUrl(form, Array.from(new FormData(form).entries()), form.action);
    var field = form.elements.namedItem("search");
    var term = tag.getAttribute(exclude ? "data-tag-exclude-term" : "data-tag-filter-term");
    var search = field.value;
    if (search.indexOf(term) === -1) search = (search + " " + term).trim();
    field.value = search;
    target.searchParams.set("search", search);
    load(target.href);
  }

  document.addEventListener("input", function (event) {
    if (event.target.form && event.target.type !== "checkbox" && event.target.type !== "hidden") event.target.form.dataset.dirty = "true";
  });
  document.addEventListener("change", function (event) {
    var node = event.target;
    if (node.form && node.name !== "candidate_ids" && node.name !== "job_ids") node.form.dataset.dirty = "true";
  });
  document.addEventListener("submit", function (event) {
    if (event.defaultPrevented) return;
    var form = event.target, submitter = event.submitter;
      if (form.matches("[data-account-create],[data-account-edit]")) return;
    if (form.matches("[data-metadata-editor]") || form.action.endsWith("/exhentai-metadata")) return;
    var method = (submitter && submitter.getAttribute("formmethod") || form.getAttribute("method") || "GET").toUpperCase();
    var url = submitter && submitter.getAttribute("formaction") || form.getAttribute("action") || current;
    if (/\/(login|logout|change-password)$/.test(local(url).pathname) || form.classList.contains("ui-log-filter")) return;
    var data = Array.from(new FormData(form).entries());
    if (submitter && submitter.name && !submitter.disabled) data.push([submitter.name, submitter.value]);
    if (data.some(function (pair) { return typeof pair[1] !== "string"; })) return;
    event.preventDefault(); event.stopImmediatePropagation();
    if (method === "GET") {
      delete form.dataset.dirty;
      var target = local(url); target.search = new URLSearchParams(data).toString();
      if (form.matches("#candidate-filters,#downloaded-filters")) target = filterUrl(form, data, url);
      load(target.href); return;
    }
    writing += 1;
    var version = revision;
    var buttons = Array.from(form.elements).filter(function (node) { return node.type === "submit" && !node.disabled; });
    buttons.forEach(function (node) { node.disabled = true; });
    form.setAttribute("aria-busy", "true");
    request(method, url, data).then(function (frame) {
      if (version !== revision) return;
      if (frame.type === "command.result" && frame.location && local(frame.location).pathname !== local(current).pathname) {
        current = local(frame.location).pathname + local(frame.location).search;
        revision += 1;
      }
      if (frame.format === "html" && frame.status < 500) {
        refreshAgain = frame.sequence < sequence;
        if (frame.type === "command.result") delete form.dataset.dirty;
        swap(frame.data, frame.type === "command.result" || form.matches("[data-work-source],[data-source-form],[data-settings-action],[data-manual-form]") ? form : null, true,
          frame.type === "command.error");
      }
      if (frame.type === "command.error") {
        form.dataset.dirty = "true";
        window.EhBotUI.toast(frame.format ? errorMessage(frame.data, frame.status) : frame.error.message, {tone: "danger"});
      }
      if (frame.type === "command.result") {
        document.dispatchEvent(new CustomEvent("ehbot:operation", { detail: { form: form, frame: frame } }));
      }
    }).catch(function (error) { window.EhBotUI.toast(error.message, {tone: "danger"}); }).finally(function () {
      buttons.forEach(function (node) { node.disabled = false; });
      form.removeAttribute("aria-busy"); writing -= 1;
      if (refreshAgain) scheduleRefresh();
    });
  }, true);

  function closeMenu() {
    if (menu) menu.remove();
    if (menuTag) menuTag.setAttribute("aria-expanded", "false");
    menu = null;
  }
  document.addEventListener("click", function (event) {
    if (menu && menu.contains(event.target)) return;
    closeMenu();
    var tag = event.target.closest("a[data-tag]");
    var link = event.target.closest("a[href]");
    if (!link || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || event.button !== 0) return;
    var filters = document.querySelector("#candidate-filters,#downloaded-filters");
    if (tag && filters) { event.preventDefault(); filterTag(tag, false); return; }
    if (tag || (filters && (link.closest(".ui-search-chips,.ui-pagination,.ui-filters,.ui-view-switch,.ui-filter-layout")))) {
      var target = local(link.href), page = local(current);
      if (target.pathname !== page.pathname) return;
      event.preventDefault(); load(target.href);
    }
  });
  function openMenu(tag, x, y) {
    closeMenu(); menuTag = tag;
    tag.setAttribute("aria-expanded", "true");
    menu = document.createElement("div");
    menu.className = "ui-tag-menu"; menu.setAttribute("role", "menu");
    [["🔍 过滤", false], ["🚫 排除", true]].forEach(function (item) {
      var button = document.createElement("button");
      button.className = "ui-btn"; button.type = "button"; button.setAttribute("role", "menuitem");
      button.textContent = item[0];
      button.addEventListener("click", function () { filterTag(tag, item[1]); closeMenu(); tag.focus({ preventScroll: true }); });
      menu.appendChild(button);
    });
    document.body.appendChild(menu);
    menu.style.left = Math.max(0, Math.min(x, window.innerWidth - menu.offsetWidth)) + "px";
    menu.style.top = Math.max(0, Math.min(y, window.innerHeight - menu.offsetHeight)) + "px";
    menu.firstElementChild.focus();
  }
  document.addEventListener("contextmenu", function (event) {
    var tag = event.target.closest("a[data-tag]");
    if (!tag) return;
    event.preventDefault(); openMenu(tag, event.clientX, event.clientY);
  });
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && menu) { closeMenu(); if (menuTag) menuTag.focus(); }
    else if (menu && (event.key === "ArrowDown" || event.key === "ArrowUp")) {
      event.preventDefault();
      (document.activeElement === menu.firstElementChild ? menu.lastElementChild : menu.firstElementChild).focus();
    } else if (event.key === "ContextMenu" || (event.shiftKey && event.key === "F10")) {
      var tag = event.target.closest("a[data-tag]");
      if (tag) { event.preventDefault(); var rect = tag.getBoundingClientRect(); openMenu(tag, rect.left, rect.bottom); }
    }
  });
  window.addEventListener("scroll", closeMenu, true);
  document.addEventListener("visibilitychange", function () { if (!document.hidden && connected) scheduleRefresh(); });
  window.addEventListener("pagehide", function () { window.clearTimeout(reconnectTimer); if (socket) { socket.onclose = null; socket.close(); } });
  window.EhBotRealtime = { on: on, command: command, fetch: apiFetch, refresh: refresh, load: load };
  connectionStatus("正在连接实时服务");
  if (window.WebSocket) connect();
  else connectionStatus("当前浏览器不支持实时连接，可手动更新内容");
})();
