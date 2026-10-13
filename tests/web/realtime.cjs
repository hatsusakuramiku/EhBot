/* Run with: node tests/web/realtime.cjs <path-to-jsdom>. Uses the real HTMX
 * runtime and application scripts, with an in-memory WebSocket server. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { JSDOM } = require(process.argv[2] ? path.resolve(process.argv[2]) : "jsdom");
const asset = name => fs.readFileSync(path.join(__dirname, "../../app/web/static", name), "utf8");

function content(state = "queued", search = "") {
  const escape = text => text.replaceAll("&", "&amp;").replaceAll('"', "&quot;");
  const filter = `/candidates?search=${encodeURIComponent(search + ' $tag:"big breasts"$')}`;
  const exclude = `/candidates?search=${encodeURIComponent(search + ' $-tag:"big breasts"$')}`;
  return `<header>候选</header>
    <div class="ui-filter-layout"><form id="candidate-filters" method="get" action="/candidates">
      <input name="view" value="list" type="hidden"><input name="dir" value="asc" type="hidden">
      <select name="sort"><option value="title" selected>标题</option></select>
      <input name="language" value="stale" type="checkbox" checked>
      <input name="language" value="japanese" type="checkbox"><input name="language" value="english" type="checkbox">
      <input name="search" id="search" value="${escape(search)}"><button type="submit">应用</button>
    </form><div data-candidate-id="1"><input type="checkbox" name="candidate_ids" value="1">
      <a class="ui-tag" data-tag="big breasts" href="${escape(filter)}" data-tag-exclude="${escape(exclude)}"
        data-tag-filter-term="${escape('$tag:"big breasts"$')}" data-tag-exclude-term="${escape('$-tag:"big breasts"$')}">big breasts</a>
      <span data-state>${state}</span></div></div>
    <form method="post" action="/candidates/1/metadata"><input name="field_name" value="Title" type="hidden">
      <input id="title" name="field_value" value="server title"><button type="submit">保存</button></form>
    <form id="batch" method="post" action="/downloaded/batch"><input name="candidate_ids" value="1" type="checkbox" checked>
      <input name="candidate_ids" value="2" type="checkbox" checked>
      <button id="pack" type="submit" formaction="/downloaded/1/repack" name="action" value="repack">重新打包</button></form>
    <button id="external-pack" type="submit" form="batch" formmethod="post"
      formaction="/downloaded/1/repack" name="action" value="repack">确认重新打包</button>
    <form id="work-metadata-editor" data-metadata-editor>
      <input type="hidden" name="original__Title" value="${state}">
      <div data-field-name="Title"><input data-field="value" value="file title"></div></form>
    <form id="queue-batch" x-data="{ selected: [] }"><input name="job_ids" value="9" type="checkbox" x-model="selected">
      <span data-queue-selected x-text="selected.length"></span></form>`;
}

async function scenario(prefix) {
  const dom = new JSDOM(`<!doctype html><meta name="csrf-token" content="test-csrf">
    <meta name="realtime-path" content="${prefix}/ws/ui"><body><div data-realtime-status hidden></div>
    <main id="main">${content()}</main><div class="ui-toasts"></div></body>`, {
    url: `http://testserver${prefix}/candidates?page_size=125&page=3&language=stale`, runScripts: "outside-only", pretendToBeVisual: true,
  });
  const win = dom.window;
  win.scrollTo = () => {};
  const intervals = [];
  win.setInterval = (...args) => { intervals.push(args); return 1; };
  win.fetch = () => { throw new Error("Connected UI must use WebSocket"); };
  const sockets = [], frames = [], results = [];
  let search = "", state = "queued", sequence = 0, holdWrite = false, holdRead = false;
  const delayed = [];
  const catalogue = {
    "web.list": {method: "GET", path: "/candidates", params: [], encoding: "form"},
    "web.metadata": {method: "POST", path: "/candidates/{candidate_id}/metadata", params: ["candidate_id"], encoding: "form"},
    "web.repack": {method: "POST", path: "/downloaded/{candidate_id}/repack", params: ["candidate_id"], encoding: "form"},
  };
  class FakeSocket {
    constructor(url) { this.url = url; sockets.push(this); queueMicrotask(() => this.onopen()); }
    message(frame) { this.onmessage({data: JSON.stringify(frame)}); }
    send(raw) {
      const frame = JSON.parse(raw); frames.push(frame);
      if (frame.type === "hello") {
        assert.equal(frame.csrf_token, "test-csrf");
        this.message({type: "ready", commands: catalogue, epoch: "epoch-1", sequence}); return;
      }
      if (frame.type !== "command") return;
      this.message({type: "command.accepted", request_id: frame.request_id});
      if (frame.name === "web.list") search = new URLSearchParams(frame.payload.query).get("search") || "";
      if (frame.name === "web.repack" && holdWrite) return;
      const result = {type: "command.result", request_id: frame.request_id, format: "html", data: content(state, search), status: 200, sequence};
      if (frame.name === "web.list" && holdRead) { delayed.push(result); return; }
      results.push(result);
      queueMicrotask(() => this.message(result));
    }
    close() {}
  }
  win.WebSocket = FakeSocket;
  // jsdom requires an XPath type; browsers default an omitted type to ANY_TYPE.
  const evaluate = win.XPathEvaluator.prototype.evaluate;
  win.XPathEvaluator.prototype.evaluate = function (expr, context, resolver, type, result) {
    return evaluate.call(this, expr, context, resolver, type || 0, result || null);
  };
  const expressionEvaluate = win.XPathExpression.prototype.evaluate;
  win.XPathExpression.prototype.evaluate = function (context, type, result) {
    return expressionEvaluate.call(this, context, type || 0, result || null);
  };
  win.eval(asset("vendor/htmx-2.0.4.min.js"));
  win.eval(asset("vendor/alpine-3.14.8.min.js"));
  win.eval(asset("ui.js"));
  win.eval(asset("realtime.js"));
  const waitFor = async predicate => {
    for (let i = 0; i < 1000 && !predicate(); i++) await new Promise(resolve => setTimeout(resolve, 5));
    assert.ok(predicate(), "timed out waiting for UI update");
  };
  await waitFor(() => results.length > 0);
  await new Promise(resolve => setTimeout(resolve, 80));
  assert.equal(sockets.length, 1);
  assert.equal(sockets[0].url, `ws://testserver${prefix}/ws/ui`);
  assert.equal(intervals.length, 0, "page state must not be polled");
  const originalUrl = win.location.href;
  const title = win.document.getElementById("title");
  const metadataEditor = win.document.getElementById("work-metadata-editor");
  metadataEditor.dataset.dirty = "true";
  metadataEditor.querySelector("[data-field-name]").dataset.fetchedValue = "fetched draft";
  metadataEditor.querySelector('[data-field="value"]').value = "fetched draft";
  title.value = "unsaved title"; title.focus(); title.setSelectionRange(2, 7);
  title.dispatchEvent(new win.Event("input", {bubbles: true}));
  const checkbox = win.document.querySelector('[data-candidate-id] input'); checkbox.checked = true;
  const jobBox = win.document.querySelector('[name="job_ids"]'); jobBox.checked = true;
  jobBox.dispatchEvent(new win.Event("change", {bubbles: true}));
  const drawer = win.document.createElement("aside"); drawer.innerHTML = '<input value="drawer edit">'; win.document.body.appendChild(drawer);
  state = "running"; sequence++;
  sockets[0].message({type: "state", epoch: "epoch-1", sequence, scope: "conversion", data: {candidate_id: 1}});
  await waitFor(() => win.document.querySelector("[data-state]").textContent === "running");
  await new Promise(resolve => setTimeout(resolve, 40));
  assert.equal(win.document.getElementById("title"), title, "dirty form is preserved as the same DOM node");
  assert.equal(title.value, "unsaved title");
  assert.equal(win.document.getElementById("work-metadata-editor"), metadataEditor, "editor survives changes to hidden snapshot fields");
  assert.equal(metadataEditor.querySelector('[data-field="value"]').value, "fetched draft");
  assert.equal(metadataEditor.querySelector("[data-field-name]").dataset.fetchedValue, "fetched draft");
  assert.equal(win.document.activeElement, title);
  assert.deepEqual([title.selectionStart, title.selectionEnd], [2, 7]);
  assert.equal(win.document.querySelector('[data-candidate-id] input').checked, true);
  assert.equal(win.document.querySelector('[name="job_ids"]').checked, true);
  assert.equal(win.document.querySelector('[data-queue-selected]').textContent, "1", "Alpine batch selection survives the swap");
  assert.equal(drawer.querySelector("input").value, "drawer edit");
  const field = win.document.getElementById("search"); field.value = "needle";
  field.form.dispatchEvent(new win.SubmitEvent("submit", {bubbles: true, cancelable: true}));
  await waitFor(() => win.document.getElementById("search").value === "needle" && frames.some(f => f.name === "web.list" && new URLSearchParams(f.payload.query).get("search") === "needle"));
  await new Promise(resolve => setTimeout(resolve, 80));
  assert.equal(new URLSearchParams(frames.findLast(f => f.name === "web.list").payload.query).get("page_size"), "125");
  const unsent = win.document.getElementById("search"); unsent.value = "unsubmitted needle";
  unsent.dispatchEvent(new win.Event("input", {bubbles: true}));
  win.document.querySelector('[name="language"][value="stale"]').checked = false;
  win.document.querySelector('[name="language"][value="japanese"]').checked = true;
  win.document.querySelector('[name="language"][value="english"]').checked = true;
  win.document.querySelector("a[data-tag]").click();
  await waitFor(() => search.includes('$tag:"big breasts"$'));
  await new Promise(resolve => setTimeout(resolve, 80));
  assert.equal(search, 'unsubmitted needle $tag:"big breasts"$', "tag reads unsent search text");
  const query = new URLSearchParams(frames.findLast(f => f.name === "web.list").payload.query);
  assert.deepEqual(query.getAll("language"), ["japanese", "english"], "tag keeps multiple facets and drops unchecked ones");
  assert.equal(query.get("page"), null);
  assert.equal(query.get("page_size"), "125");
  assert.equal(query.get("view"), "list");
  assert.equal(query.get("sort"), "title");
  assert.equal(query.get("dir"), "asc");
  win.document.querySelector("a[data-tag]").click();
  await new Promise(resolve => setTimeout(resolve, 180));
  assert.equal(search.split('$tag:"big breasts"$').length, 2, "repeated tag is not duplicated");
  assert.equal(win.location.href, originalUrl, "filtering leaves URL/history fixed");
  const tag = win.document.querySelector("a[data-tag]");
  tag.dispatchEvent(new win.MouseEvent("contextmenu", {bubbles: true, cancelable: true, clientX: 900, clientY: 400}));
  assert.equal(win.document.querySelectorAll('[role="menuitem"]').length, 2);
  win.document.querySelectorAll('[role="menuitem"]')[1].click();
  await waitFor(() => search.includes('$-tag:"big breasts"$'));
  await new Promise(resolve => setTimeout(resolve, 80));
  assert.equal(win.location.href, originalUrl);
  assert.equal(win.document.querySelector('[role="menu"]'), null);
  let keyboardTag = win.document.querySelector("a[data-tag]");
  keyboardTag.dispatchEvent(new win.KeyboardEvent("keydown", {key: "ContextMenu", bubbles: true, cancelable: true}));
  assert.equal(keyboardTag.getAttribute("aria-expanded"), "true");
  win.document.activeElement.dispatchEvent(new win.KeyboardEvent("keydown", {key: "ArrowDown", bubbles: true, cancelable: true}));
  assert.equal(win.document.activeElement, win.document.querySelector('[role="menu"]').lastElementChild);
  win.document.activeElement.dispatchEvent(new win.KeyboardEvent("keydown", {key: "Escape", bubbles: true}));
  assert.equal(win.document.activeElement, keyboardTag);
  assert.equal(keyboardTag.getAttribute("aria-expanded"), "false");
  keyboardTag.dispatchEvent(new win.KeyboardEvent("keydown", {key: "F10", shiftKey: true, bubbles: true}));
  win.dispatchEvent(new win.Event("scroll"));
  assert.equal(win.document.querySelector('[role="menu"]'), null, "scroll closes menu");
  keyboardTag.dispatchEvent(new win.KeyboardEvent("keydown", {key: "ContextMenu", bubbles: true}));
  state = "complete"; sequence++;
  sockets[0].message({type: "state", epoch: "epoch-1", sequence, scope: "conversion", data: {candidate_id: 1}});
  await waitFor(() => win.document.querySelector("[data-state]").textContent === "complete");
  assert.equal(win.document.querySelector('[role="menu"]'), null, "fragment replacement closes menu");
  holdRead = true;
  win.EhBotRealtime.load("/candidates?search=obsolete&page_size=125");
  await waitFor(() => delayed.length === 1);
  win.EhBotRealtime.load("/candidates?search=latest&page_size=125");
  holdRead = false; sockets[0].message(delayed[0]);
  await new Promise(resolve => setTimeout(resolve, 30));
  assert.notEqual(win.document.getElementById("search").value, "obsolete", "old query result is discarded");
  await waitFor(() => win.document.getElementById("search").value === "latest");
  await new Promise(resolve => setTimeout(resolve, 60));
  const beforeGap = results.length;
  sequence += 3;
  sockets[0].message({type: "state", epoch: "epoch-1", sequence, scope: "conversion", data: {candidate_id: 1}});
  await waitFor(() => results.length > beforeGap);
  await new Promise(resolve => setTimeout(resolve, 60));
  const beforePing = results.length;
  sequence++;
  sockets[0].message({type: "ping", epoch: "epoch-1", sequence});
  await waitFor(() => results.length > beforePing);
  await new Promise(resolve => setTimeout(resolve, 60));
  const corrected = results.length;
  sockets[0].message({type: "ping", epoch: "epoch-1", sequence});
  await new Promise(resolve => setTimeout(resolve, 160));
  assert.equal(results.length, corrected, "reconciled heartbeat does not repeatedly request snapshots");
  const pack = win.document.getElementById("pack"); holdWrite = true;
  pack.form.dispatchEvent(new win.SubmitEvent("submit", {bubbles: true, cancelable: true, submitter: pack}));
  const write = frames.findLast(frame => frame.name === "web.repack");
  assert.deepEqual(write.payload.params, {candidate_id: "1"});
  assert.equal(write.payload.body.filter(pair => pair[0] === "candidate_ids").length, 2, "multi-value forms keep all selected IDs");
  assert.ok(write.payload.body.some(pair => pair[0] === "action" && pair[1] === "repack"));
  assert.equal(pack.disabled, true, "submit is disabled while request is outstanding");
  sockets[0].onclose({code: 4401});
  await new Promise(resolve => setTimeout(resolve, 30));
  assert.equal(pack.disabled, false);
  assert.equal(frames.filter(frame => frame.name === "web.repack").length, 1, "unconfirmed write is never replayed");
  assert.match(win.document.querySelector("[data-realtime-status]").textContent, /重新登录/);
  let fallback;
  win.fetch = (url, options) => {
    fallback = {url, options};
    return Promise.resolve({ok: true, status: 200, url: originalUrl, headers: {get: () => "text/html"},
      text: () => Promise.resolve(content("fallback saved", search))});
  };
  pack.form.dispatchEvent(new win.SubmitEvent("submit", {bubbles: true, cancelable: true, submitter: pack}));
  assert.equal(pack.disabled, true);
  assert.equal(new URL(fallback.url).pathname, `${prefix}/downloaded/1/repack`);
  assert.equal(new URL(fallback.url).searchParams.get("page_size"), "125");
  assert.deepEqual(Array.from(fallback.options.body.getAll("candidate_ids")), ["1", "2"]);
  await waitFor(() => win.document.querySelector("[data-state]").textContent === "fallback saved");
  assert.equal(win.document.getElementById("title"), title, "HTTP fallback preserves unsaved inputs too");
  assert.equal(win.location.href, originalUrl);
  const stableState = win.document.querySelector("[data-state]");
  let errorStatus = 422;
  win.fetch = (url, options) => {
    assert.equal(options.method, "POST", "external submitter formmethod overrides the form");
    assert.equal(options.body.get("action"), "repack");
    return Promise.resolve({ok: false, status: errorStatus, url: originalUrl,
    headers: {get: () => "application/json"}, json: () => Promise.resolve(errorStatus === 422
      ? {detail: [{loc: ["body", "action"], msg: "Field required"}]} : {detail: "Method Not Allowed"})});
  };
  const external = win.document.getElementById("external-pack");
  external.form.setAttribute("method", "get");
  external.click();
  await waitFor(() => win.document.querySelector(".ui-toasts").textContent.includes("Field required"));
  assert.equal(external.disabled, false);
  assert.equal(win.document.querySelector("[data-state]"), stableState, "JSON failures never replace content with an error envelope");
  errorStatus = 405;
  external.click();
  await waitFor(() => win.document.querySelector(".ui-toasts").textContent.includes("Method Not Allowed"));
  assert.equal(win.document.getElementById("title"), title, "JSON failures keep dirty form nodes");
  holdWrite = false;
  state = "reconnected state";
  sockets[0].onclose({code: 1001});
  assert.equal(win.document.querySelector("[data-realtime-status]").textContent, "实时重连中");
  assert.equal(win.document.querySelector("[data-realtime-status] button"), null);
  await waitFor(() => sockets.length === 2);
  await waitFor(() => win.document.querySelector("[data-state]").textContent === state);
  assert.equal(win.document.querySelector("[data-realtime-status]").textContent, "实时已连接");
  assert.equal(win.document.getElementById("title"), title);
  const reads = results.length;
  win.fetch = () => Promise.resolve({status: 401});
  sockets[1].onclose({code: 1006});
  await waitFor(() => win.document.querySelector("[data-realtime-status]").textContent.includes("重新登录"));
  await new Promise(resolve => setTimeout(resolve, 1300));
  assert.equal(sockets.length, 2, "rejected upgrades with expired auth do not reconnect forever");
  assert.equal(results.length, reads);
  win.close();
}

async function logScenario(prefix) {
  const row = '<li data-log-row><span class="ui-badge"></span><time></time><code class="ui-log-logger"></code>' +
    '<p class="ui-log-event"></p><p class="ui-log-message"></p><p class="ui-log-meta"></p>' +
    '<details class="ui-log-trace"><pre></pre></details></li>';
  const dom = new JSDOM(`<meta name="realtime-path" content="${prefix}/ws/ui"><main>
    <section data-log-panel data-level="INFO" data-limit="10"><form class="ui-log-filter">
      <select data-log-level><option>INFO</option><option>WARNING</option></select></form>
    <ol data-log-list></ol><template data-log-template>${row}</template><div data-log-empty></div>
    <div data-log-live><button data-log-pause></button><span data-log-status></span></div></section></main>`, {
    url: `http://testserver${prefix}/logs`, runScripts: "outside-only",
  });
  const win = dom.window, listeners = {}, requests = [];
  win.EhBotRealtime = {
    on: (name, fn) => { listeners[name] = fn; },
    fetch: url => {
      if (url.includes("limit=1&") || url.endsWith("limit=1")) {
        return Promise.resolve({ok: true, json: () => Promise.resolve({levels: []})});
      }
      return new Promise(resolve => requests.push({url, resolve}));
    },
  };
  const entry = event => ({event, level: {code: "ERROR", label: "错误", tone: "danger"},
    timestamp: "2026-10-12T00:00:00Z", logger: "test", error_message: "message", candidate_id: 42, exception: "trace"});
  const resolveSnapshot = (request, sequence, events) => request.resolve({ok: true, json: () => Promise.resolve({
    entries: events.map(entry), log_epoch: "log-1", log_sequence: sequence,
  })});
  const flush = () => new Promise(resolve => setTimeout(resolve, 10));
  win.eval(asset("logs.js"));
  listeners.ready({epoch: "log-1"});
  listeners.log({epoch: "log-1", sequence: 2, data: entry("new during snapshot")});
  resolveSnapshot(requests[0], 1, ["snapshot"]); await flush();
  assert.deepEqual(Array.from(win.document.querySelectorAll(".ui-log-event"), n => n.textContent), ["new during snapshot", "snapshot"]);
  assert.equal(win.document.querySelector(".ui-log-meta a").pathname, `${prefix}/works/42`);
  assert.equal(win.document.querySelector(".ui-log-message").textContent, "message");
  listeners.log({epoch: "log-1", sequence: 1, data: entry("snapshot replay")});
  assert.equal(win.document.querySelectorAll("[data-log-row]").length, 2, "snapshot watermark discards replay duplicates");
  listeners.refresh_request();
  listeners.log({epoch: "log-1", sequence: 3, data: entry("already in snapshot")});
  resolveSnapshot(requests[1], 3, ["already in snapshot", "snapshot"]); await flush();
  assert.equal(win.document.querySelectorAll("[data-log-row]").length, 2);
  listeners.refresh_request();
  const select = win.document.querySelector("select"); select.value = "WARNING";
  select.dispatchEvent(new win.Event("change"));
  resolveSnapshot(requests[3], 4, ["latest filter"]); await flush();
  resolveSnapshot(requests[2], 3, ["obsolete filter"]); await flush();
  assert.equal(win.document.querySelector(".ui-log-event").textContent, "latest filter", "older snapshot cannot replace newer filter");
  win.document.querySelector("[data-log-pause]").click();
  listeners.log({epoch: "log-1", sequence: 5, data: entry("paused entry")});
  assert.equal(win.document.querySelectorAll("[data-log-row]").length, 1);
  win.document.querySelector("[data-log-pause]").click();
  assert.equal(win.document.querySelector(".ui-log-event").textContent, "paused entry");
  win.close();
}

(async () => {
  await scenario("");
  await scenario("/ehbot");
  await logScenario("");
  await logScenario("/ehbot");
  process.stdout.write("realtime and log DOM scenarios passed (root and proxy prefix)\n");
})().catch(error => { process.stderr.write(error.stack + "\n"); process.exitCode = 1; });
