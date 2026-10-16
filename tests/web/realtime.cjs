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
    <div id="api-key-reveal" data-api-key-reveal data-public-id="${state === 'key-replaced' ? 'replacement-key' : 'fixture-key'}">metadata only</div>
    <form data-credential-command="add" data-settings-action="credential-add" method="post" action="/archive-settings/passwords">
      <input type="hidden" name="expected_revision" value="${escape(state)}">
      <input type="hidden" name="idempotency_key" value="vault-${escape(state)}">
      <input name="name" value="fixture"><input name="password" type="password"></form>
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
    <form id="work-metadata-editor" data-metadata-editor data-work-id="work-1" data-revision="${escape(state)}">
      <input type="hidden" name="work_id" value="work-1">
      <input type="hidden" name="expected_revision" value="${escape(state)}">
      <input type="hidden" name="idempotency_key" value="metadata-${escape(state)}">
      <input type="hidden" name="original__Title" value="${state}">
      <div data-field-name="Title"><input data-field="value" value="file title"></div></form>
    <form id="queue-batch" x-data="{ selected: [] }"><input name="job_ids" value="9" type="checkbox" x-model="selected">
      <span data-queue-selected x-text="selected.length"></span></form>
    <form data-manual-form method="post" action="/manual-add">
      <input type="hidden" name="idempotency_key" value="manual-${escape(state)}">
      <textarea name="input"></textarea></form>
    <form data-settings-action="system" method="post" action="/settings/system">
      <input type="hidden" name="expected_revision" value="${escape(state)}">
      <input type="hidden" name="idempotency_key" value="settings-${escape(state)}">
      <input name="timezone" value="UTC"></form>
    <section data-ai-operation="operation-original"><p data-ai-operation-state>${escape(state)}</p></section>
    <form data-work-source="preview" method="post" action="/works/1/preview">
      <input type="hidden" name="work_id" value="original-work">
      <input type="hidden" name="expected_revision" value="${escape(state)}">
      <input type="hidden" name="idempotency_key" value="preview-${escape(state)}">
      <input name="preview_url" value="https://telegra.ph/stored"></form>
    <form data-settings-action="toolchain-install" method="post" action="/archive-settings/toolchain/install">
      <input type="hidden" name="expected_revision" value="${escape(state)}">
      <input type="hidden" name="idempotency_key" value="install-${escape(state)}"></form>
    <form data-settings-action="ai-master" method="post" action="/settings/ai/master">
      <input type="hidden" name="expected_revision" value="${escape(state)}">
      <input type="hidden" name="idempotency_key" value="master-${escape(state)}">
      <input type="checkbox" name="ai_enabled" checked></form>
    <form data-settings-action="path-ai" method="post" action="/archive-settings/paths/ai">
      <input type="hidden" name="expected_revision" value="${escape(state)}">
      <input type="hidden" name="idempotency_key" value="path-${escape(state)}">
      <textarea name="ai_prompt">stored prompt</textarea><input name="ai_batch_size" value="20">
      <input type="checkbox" name="ai_stream"></form>
    <form data-settings-action="path-ai-prompt" method="post" action="/archive-settings/paths/ai/prompt-default">
      <input type="hidden" name="expected_revision" value="${escape(state)}">
      <input type="hidden" name="idempotency_key" value="reset-${escape(state)}"></form>
    <form data-settings-action="path-model-source" method="post" action="/archive-settings/paths/ai/models">
      <input type="hidden" name="expected_revision" value="${escape(state)}">
      <input type="hidden" name="idempotency_key" value="source-${escape(state)}">
      <input type="radio" name="ai_model_source" value="default" checked>
      <input type="radio" name="ai_model_source" value="custom"></form>
    <form data-settings-action="rearchive" method="post" action="/archive-settings/paths/rearchive">
      <input type="hidden" name="expected_revision" value="${escape(state)}">
      <input type="hidden" name="idempotency_key" value="rearchive-${escape(state)}">
      <input type="checkbox" name="force"><input type="checkbox" name="dry_run"></form>
    <form data-settings-action="path-cache-clear" method="post" action="/archive-settings/paths/ai/cache/clear">
      <input type="hidden" name="expected_revision" value="${escape(state)}">
      <input type="hidden" name="idempotency_key" value="clear-${escape(state)}"></form>
    <form data-settings-action="torrent" method="post" action="/archive-settings/torrent">
      <input type="hidden" name="expected_revision" value="${escape(state)}">
      <input type="hidden" name="idempotency_key" value="torrent-${escape(state)}">
      <input name="password" type="password"></form>
    <form data-ai-catalogue-command="keys-add" data-settings-action="ai-keys-add-original" method="post" action="/settings/ai/providers/1/keys">
      <input type="hidden" name="provider_uuid" value="original-provider">
      <input type="hidden" name="expected_revision" value="${escape(state)}">
      <input type="hidden" name="idempotency_key" value="keys-${escape(state)}">
      <textarea name="api_keys"></textarea></form>
    <form data-ai-chain-command data-settings-action="ai-chain-default-append" method="post" action="/settings/ai/chain/append">
      <input type="hidden" name="expected_revision" value="${escape(state)}">
      <input type="hidden" name="idempotency_key" value="chain-${escape(state)}">
      <select name="model_identity"><option value="1:original-model">one</option><option value="2:second-model">two</option></select></form>
    <form data-rule-command data-settings-action="rule-paths-editor" method="post" action="/archive-settings/paths/rules">
      <input type="hidden" name="expected_revision" value="${escape(state)}">
      <input type="hidden" name="idempotency_key" value="rule-${escape(state)}">
      <input name="name" value="Saved rule"><input name="condition_value" value="Saved condition"></form>
    ${state === "source-deleted" ? "" : `<form data-source-form="row-7" method="post" action="/sources">
      <input type="hidden" name="expected_revision" value="${escape(state)}">
      <input type="hidden" name="idempotency_key" value="key-${escape(state)}">
      <input id="source-draft-name" name="display_name" value="Saved source"></form>`}`;
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
  let search = "", state = "queued", sequence = 0, holdWrite = false, holdRead = false, renameConflict = true;
  const delayed = [];
  const catalogue = {
    "web.list": {method: "GET", path: "/candidates", params: [], encoding: "form"},
    "web.metadata": {method: "POST", path: "/candidates/{candidate_id}/metadata", params: ["candidate_id"], encoding: "form"},
    "web.repack": {method: "POST", path: "/downloaded/{candidate_id}/repack", params: ["candidate_id"], encoding: "form"},
    "web.rename": {method: "POST", path: "/downloaded/{candidate_id}/rename", params: ["candidate_id"], encoding: "form"},
    "web.source": {method: "POST", path: "/sources", params: [], encoding: "form"},
    "web.settings": {method: "POST", path: "/settings/system", params: [], encoding: "form"},
    "web.torrent": {method: "POST", path: "/archive-settings/torrent", params: [], encoding: "form"},
    "web.credential": {method: "POST", path: "/archive-settings/passwords", params: [], encoding: "form"},
    "web.ai-keys": {method: "POST", path: "/settings/ai/providers/{provider_id}/keys", params: ["provider_id"], encoding: "form"},
    "web.ai-chain": {method: "POST", path: "/settings/ai/chain/append", params: [], encoding: "form"},
    "web.rule": {method: "POST", path: "/archive-settings/paths/rules", params: [], encoding: "form"},
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
      if (frame.name === "web.rename" && renameConflict) {
        Object.assign(result, {type: "command.error", status: 409, error: {message: "作品已变化"}});
      }
      if (frame.name === "web.source") {
        const draft = Object.fromEntries(frame.payload.body);
        Object.assign(result, {type: "command.error", status: 409, error: {message: "来源已变化"},
          data: content("source-deleted", search) + `<section data-source-draft>保留草稿</section>
          <form data-source-form="row-7" data-dirty="true" method="post" action="/sources">
            <input type="hidden" name="expected_revision" value="${draft.expected_revision}">
            <input type="hidden" name="idempotency_key" value="${draft.idempotency_key}">
            <input id="source-draft-name" name="display_name" value="${draft.display_name}">
            <button type="submit" name="confirmed_revision" value="20">核对后提交</button></form>`});
      }
      if (frame.name === "web.settings") {
        const draft = Object.fromEntries(frame.payload.body);
        Object.assign(result, {type: "command.error", status: 409, error: {message: "设置已变化"},
          data: content(state, search).replace(/<form data-settings-action="system"[\s\S]*?<\/form>/,
            `<form data-settings-action="system" data-dirty="true" method="post" action="/settings/system">
            <input type="hidden" name="expected_revision" value="${draft.expected_revision}">
            <input type="hidden" name="idempotency_key" value="${draft.idempotency_key}">
            <input name="timezone" value="${draft.timezone}">
            <button type="submit" name="confirmed_command" value="confirmation">Confirm settings</button></form>`) });
      }
      if (frame.name === "web.torrent") {
        const draft = Object.fromEntries(frame.payload.body);
        Object.assign(result, {type: "command.error", status: 409, error: {message: "设置已变化"},
          data: content(state, search).replace(/<form data-settings-action="torrent"[\s\S]*?<\/form>/,
            `<form data-settings-action="torrent" data-dirty="true" method="post" action="/archive-settings/torrent">
            <input type="hidden" name="expected_revision" value="${draft.expected_revision}">
            <input type="hidden" name="idempotency_key" value="${draft.idempotency_key}">
            <input name="password" type="password"><button type="submit" name="confirmed_command" value="confirmation">Confirm torrent</button></form>`)});
      }
      if (frame.name === "web.credential") {
        const draft = Object.fromEntries(frame.payload.body);
        Object.assign(result, {type: "command.error", status: 409, error: {message: "密码库已变化"},
          data: content(state, search) + `<form data-credential-command="add" data-credential-draft
            data-settings-action="credential-draft-add" data-dirty="true" method="post" action="/archive-settings/passwords">
            <input type="hidden" name="expected_revision" value="${draft.expected_revision}">
            <input type="hidden" name="idempotency_key" value="${draft.idempotency_key}">
            <input name="name" value="${draft.name}"><input name="password" type="password">
            <button type="submit" name="confirmed_command" value="vault-confirmation">Confirm vault</button></form>`});
      }
      if (frame.name === "web.rule") {
        const draft = Object.fromEntries(frame.payload.body);
        Object.assign(result, {type: "command.error", status: 409, error: {message: "规则已变化"},
          data: content(state, search).replace(/<form data-rule-command[\s\S]*?<\/form>/,
            `<form data-rule-command data-settings-action="rule-paths-editor" data-dirty="true" method="post" action="/archive-settings/paths/rules">
            <input type="hidden" name="expected_revision" value="${draft.expected_revision}">
            <input type="hidden" name="idempotency_key" value="${draft.idempotency_key}">
            <input name="name" value="${draft.name}"><input name="condition_value" value="${draft.condition_value}">
            <button type="submit" name="confirmed_command" value="rule-confirmation">Confirm rule</button></form>`) });
      }
      if (frame.name === "web.ai-keys") {
        const draft = Object.fromEntries(frame.payload.body);
        Object.assign(result, {type: "command.error", status: 409, error: {message: "Key 列表已变化"},
          data: content(state, search) + `<form data-ai-catalogue-command="keys-add" data-ai-catalogue-draft
            data-settings-action="ai-catalogue-draft-keys-original" data-dirty="true" method="post" action="/settings/ai/providers/1/keys">
            <input type="hidden" name="provider_uuid" value="${draft.provider_uuid}">
            <input type="hidden" name="expected_revision" value="${draft.expected_revision}">
            <input type="hidden" name="idempotency_key" value="${draft.idempotency_key}">
            <textarea name="api_keys"></textarea><button type="submit" name="confirmed_command" value="key-confirmation">Confirm keys</button></form>`});
      }
      if (frame.name === "web.ai-chain") {
        const draft = Object.fromEntries(frame.payload.body);
        const [modelId, modelUuid] = draft.model_identity.split(":");
        Object.assign(result, {type: "command.error", status: 409, error: {message: "模型链已变化"},
          data: content(state, search) + `<form data-ai-chain-command data-settings-action="ai-chain-draft-original" data-dirty="true" method="post" action="/settings/ai/chain/append">
            <input type="hidden" name="model_id" value="${modelId}"><input type="hidden" name="model_uuid" value="${modelUuid}">
            <input type="hidden" name="expected_revision" value="${draft.expected_revision}">
            <input type="hidden" name="idempotency_key" value="${draft.idempotency_key}">
            <button type="submit" name="confirmed_command" value="chain-confirmation">Confirm chain</button></form>`});
      }
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
  const revealed = win.document.querySelector('[data-api-key-reveal]');
  revealed.dataset.oneTimeSecret = '';
  revealed.textContent = 'browser-only-api-key';
  state = 'key-stable'; sequence++;
  sockets[0].message({type: 'state', epoch: 'epoch-1', sequence, scope: 'refresh'});
  await waitFor(() => win.document.querySelector('[data-state]').textContent === state);
  assert.equal(win.document.querySelector('[data-api-key-reveal]'), revealed, 'background refresh keeps the one response available to copy');
  assert.equal(revealed.textContent, 'browser-only-api-key');
  state = 'key-replaced'; sequence++;
  sockets[0].message({type: 'state', epoch: 'epoch-1', sequence, scope: 'refresh'});
  await waitFor(() => win.document.querySelector('[data-state]').textContent === state);
  assert(!win.document.querySelector('[data-one-time-secret]'), 'replacement removes stale plaintext');
  const navigationReveal = win.document.querySelector('[data-api-key-reveal]');
  navigationReveal.dataset.oneTimeSecret = '';
  navigationReveal.textContent = 'navigation-only-api-key';
  state = 'queued';
  win.EhBotRealtime.load(originalUrl);
  await waitFor(() => win.document.querySelector('[data-state]').textContent === 'queued');
  assert(!win.document.body.textContent.includes('navigation-only-api-key'), 'explicit navigation clears one-time plaintext');
  const title = win.document.getElementById("title");
  const metadataEditor = win.document.getElementById("work-metadata-editor");
  const metadataRevision = metadataEditor.dataset.revision;
  const manualForm = win.document.querySelector('[data-manual-form]');
  const manualKey = manualForm.elements.idempotency_key.value;
  const settingsForm = win.document.querySelector('[data-settings-action="system"]');
  const settingsRevision = settingsForm.elements.expected_revision.value;
  const settingsKey = settingsForm.elements.idempotency_key.value;
  settingsForm.elements.timezone.value = "Asia/Tokyo";
  settingsForm.elements.timezone.dispatchEvent(new win.Event("input", {bubbles: true}));
  manualForm.elements.input.value = "unsent gallery link";
  manualForm.elements.input.dispatchEvent(new win.Event("input", {bubbles: true}));
  const metadataKey = metadataEditor.querySelector('[name="idempotency_key"]').value;
  metadataEditor._metadataCommand = {identity: "draft", key: metadataKey};
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
  assert.equal(win.document.querySelector('[data-manual-form]'), manualForm, "manual link draft retains the same form");
  assert.equal(manualForm.elements.input.value, "unsent gallery link");
  assert.equal(manualForm.elements.idempotency_key.value, manualKey);
  assert.equal(win.document.querySelector('[data-settings-action="system"]'), settingsForm);
  assert.equal(settingsForm.elements.timezone.value, "Asia/Tokyo");
  assert.equal(settingsForm.elements.expected_revision.value, settingsRevision);
  assert.equal(settingsForm.elements.idempotency_key.value, settingsKey);
  assert.equal(win.document.getElementById("work-metadata-editor"), metadataEditor, "editor survives changes to hidden snapshot fields");
  assert.equal(metadataEditor.querySelector('[data-field="value"]').value, "fetched draft");
  assert.equal(metadataEditor.querySelector("[data-field-name]").dataset.fetchedValue, "fetched draft");
  assert.equal(metadataEditor.dataset.revision, metadataRevision);
  assert.equal(metadataEditor.querySelector('[name="expected_revision"]').value, metadataRevision);
  assert.equal(metadataEditor.querySelector('[name="idempotency_key"]').value, metadataKey);
  assert.equal(metadataEditor._metadataCommand.key, metadataKey);
  assert.equal(win.document.activeElement, title);
  assert.deepEqual([title.selectionStart, title.selectionEnd], [2, 7]);
  assert.equal(win.document.querySelector('[data-candidate-id] input').checked, true);
  assert.equal(win.document.querySelector('[name="job_ids"]').checked, true);
  assert.equal(win.document.querySelector('[data-queue-selected]').textContent, "1", "Alpine batch selection survives the swap");
  assert.equal(drawer.querySelector("input").value, "drawer edit");
  settingsForm.dispatchEvent(new win.Event("submit", {bubbles: true, cancelable: true}));
  await waitFor(() => win.document.querySelector('[data-settings-action="system"] [name="confirmed_command"]'));
  const rejectedSettings = win.document.querySelector('[data-settings-action="system"]');
  const installDraft = win.document.querySelector('[data-settings-action="toolchain-install"]');
  installDraft.dataset.dirty = "true";
  const installRevision = installDraft.elements.expected_revision.value;
  const installKey = installDraft.elements.idempotency_key.value;
  const masterDraft = win.document.querySelector('[data-settings-action="ai-master"]');
  masterDraft.elements.ai_enabled.checked = false;
  masterDraft.elements.ai_enabled.dispatchEvent(new win.Event("change", {bubbles: true}));
  const masterRevision = masterDraft.elements.expected_revision.value;
  const masterKey = masterDraft.elements.idempotency_key.value;
  const pathDraft = win.document.querySelector('[data-settings-action="path-ai"]');
  pathDraft.elements.ai_prompt.value = "unsaved path prompt";
  pathDraft.elements.ai_batch_size.value = "31";
  pathDraft.elements.ai_stream.checked = true;
  pathDraft.elements.ai_prompt.dispatchEvent(new win.Event("input", {bubbles: true}));
  const pathRevision = pathDraft.elements.expected_revision.value;
  const pathKey = pathDraft.elements.idempotency_key.value;
  const resetDraft = win.document.querySelector('[data-settings-action="path-ai-prompt"]');
  resetDraft.dataset.dirty = "true";
  const resetRevision = resetDraft.elements.expected_revision.value;
  const resetKey = resetDraft.elements.idempotency_key.value;
  const modelSourceDraft = win.document.querySelector('[data-settings-action="path-model-source"]');
  modelSourceDraft.elements.ai_model_source.value = "custom";
  modelSourceDraft.querySelector('[value="custom"]').dispatchEvent(new win.Event("change", {bubbles: true}));
  const modelSourceRevision = modelSourceDraft.elements.expected_revision.value;
  const modelSourceKey = modelSourceDraft.elements.idempotency_key.value;
  const rearchiveDraft = win.document.querySelector('[data-settings-action="rearchive"]');
  const workSourceDraft = win.document.querySelector('[data-work-source="preview"]');
  workSourceDraft.elements.preview_url.value = 'https://telegra.ph/unsaved';
  workSourceDraft.elements.preview_url.dispatchEvent(new win.Event('input', {bubbles: true}));
  const workSourceRevision = workSourceDraft.elements.expected_revision.value;
  const workSourceKey = workSourceDraft.elements.idempotency_key.value;
  rearchiveDraft.elements.force.checked = true;
  rearchiveDraft.elements.dry_run.checked = true;
  rearchiveDraft.elements.force.dispatchEvent(new win.Event("change", {bubbles: true}));
  const rearchiveKey = rearchiveDraft.elements.idempotency_key.value;
  const rearchiveRevision = rearchiveDraft.elements.expected_revision.value;
  const cacheDraft = win.document.querySelector('[data-settings-action="path-cache-clear"]');
  cacheDraft.dataset.dirty = "true";
  const cacheKey = cacheDraft.elements.idempotency_key.value;
  assert.notEqual(rejectedSettings, settingsForm, "server conflict controls replace the submitted form");
  assert.equal(rejectedSettings.elements.timezone.value, "Asia/Tokyo");
  assert.equal(rejectedSettings.elements.expected_revision.value, settingsRevision);
  assert.equal(rejectedSettings.elements.idempotency_key.value, settingsKey);
  const torrentForm = win.document.querySelector('[data-settings-action="torrent"]');
  torrentForm.elements.password.value = "browser-only-fixture";
  torrentForm.elements.password.dispatchEvent(new win.Event("input", {bubbles: true}));
  const torrentRevision = torrentForm.elements.expected_revision.value;
  const torrentKey = torrentForm.elements.idempotency_key.value;
  torrentForm.dispatchEvent(new win.Event("submit", {bubbles: true, cancelable: true}));
  await waitFor(() => win.document.querySelector('[data-settings-action="torrent"] [name="confirmed_command"]'));
  await waitFor(() => win.document.querySelector('[data-settings-action="torrent"] [name="password"]').value === "browser-only-fixture");
  const rejectedTorrent = win.document.querySelector('[data-settings-action="torrent"]');
  assert.equal(rejectedTorrent.elements.expected_revision.value, torrentRevision);
  assert.equal(rejectedTorrent.elements.idempotency_key.value, torrentKey);
  assert(!rejectedTorrent.outerHTML.includes("browser-only-fixture"), "password stays a DOM property, not serialized HTML");
  const vaultForm = win.document.querySelector('[data-settings-action="credential-add"]');
  vaultForm.elements.password.value = 'browser-only-vault-password';
  vaultForm.elements.password.dispatchEvent(new win.Event('input', {bubbles: true}));
  const vaultRevision = vaultForm.elements.expected_revision.value;
  const vaultCommand = vaultForm.elements.idempotency_key.value;
  vaultForm.dispatchEvent(new win.Event('submit', {bubbles: true, cancelable: true}));
  await waitFor(() => win.document.querySelector('[data-credential-draft] [name="confirmed_command"]'));
  await waitFor(() => win.document.querySelector('[data-credential-draft] [name="password"]').value === 'browser-only-vault-password');
  const vaultDraft = win.document.querySelector('[data-credential-draft]');
  assert.equal(vaultDraft.elements.expected_revision.value, vaultRevision);
  assert.equal(vaultDraft.elements.idempotency_key.value, vaultCommand);
  assert(!vaultDraft.outerHTML.includes('browser-only-vault-password'), 'vault plaintext remains only a DOM property');
  const keyForm = win.document.querySelector('[data-ai-catalogue-command="keys-add"]');
  keyForm.elements.api_keys.value = "browser-only-key-fixture";
  keyForm.elements.api_keys.dispatchEvent(new win.Event("input", {bubbles: true}));
  const keyRevision = keyForm.elements.expected_revision.value;
  const keyCommand = keyForm.elements.idempotency_key.value;
  keyForm.dispatchEvent(new win.Event("submit", {bubbles: true, cancelable: true}));
  await waitFor(() => win.document.querySelector('[data-ai-catalogue-draft] [name="confirmed_command"]'));
  await waitFor(() => win.document.querySelector('[data-ai-catalogue-draft] [name="api_keys"]').value === "browser-only-key-fixture");
  const keyDraft = win.document.querySelector('[data-ai-catalogue-draft]');
  assert.equal(keyDraft.elements.provider_uuid.value, "original-provider");
  assert.equal(keyDraft.elements.expected_revision.value, keyRevision);
  assert.equal(keyDraft.elements.idempotency_key.value, keyCommand);
  assert(!keyDraft.outerHTML.includes("browser-only-key-fixture"), "Key plaintext is only in a DOM property");
  const chainForm = win.document.querySelector('[data-settings-action="ai-chain-default-append"]');
  chainForm.elements.model_identity.value = "2:second-model";
  chainForm.elements.model_identity.dispatchEvent(new win.Event("change", {bubbles: true}));
  const chainRevision = chainForm.elements.expected_revision.value;
  const chainCommand = chainForm.elements.idempotency_key.value;
  chainForm.dispatchEvent(new win.Event("submit", {bubbles: true, cancelable: true}));
  await waitFor(() => win.document.querySelector('[data-settings-action="ai-chain-draft-original"] [name="confirmed_command"]'));
  const chainDraft = win.document.querySelector('[data-settings-action="ai-chain-draft-original"]');
  assert.equal(chainDraft.elements.model_id.value, "2");
  assert.equal(chainDraft.elements.model_uuid.value, "second-model");
  assert.equal(chainDraft.elements.expected_revision.value, chainRevision);
  assert.equal(chainDraft.elements.idempotency_key.value, chainCommand);
    const ruleForm = win.document.querySelector('[data-settings-action="rule-paths-editor"]');
    const ruleRevision = ruleForm.elements.expected_revision.value;
    const ruleKey = ruleForm.elements.idempotency_key.value;
    ruleForm.elements.namedItem("name").value = "My rule draft";
    ruleForm.elements.condition_value.value = "My condition draft";
    ruleForm.elements.condition_value.dispatchEvent(new win.Event("input", {bubbles: true}));
    ruleForm.dispatchEvent(new win.Event("submit", {bubbles: true, cancelable: true}));
    await waitFor(() => win.document.querySelector('[data-settings-action="rule-paths-editor"] [name="confirmed_command"]'));
    const rejectedRule = win.document.querySelector('[data-settings-action="rule-paths-editor"]');
    assert.notEqual(rejectedRule, ruleForm);
    const retryRule = win.document.createElement("form");
    retryRule.setAttribute("data-rule-command", ""); retryRule.setAttribute("data-dirty", "true");
    retryRule.setAttribute("data-settings-action", "rule-paths-delete-original");
    retryRule.innerHTML = '<input name="rule_uuid" value="original-rule">' +
      '<input name="expected_revision" value="5"><input name="idempotency_key" value="original-delete">';
    win.document.getElementById("main").appendChild(retryRule);
    const profileDraft = win.document.createElement("form");
    profileDraft.setAttribute("data-profile-command", ""); profileDraft.setAttribute("data-dirty", "true");
    profileDraft.setAttribute("data-settings-action", "profile-original");
    profileDraft.innerHTML = '<input name="profile_uuid" value="original-profile">' +
      '<input name="expected_revision" value="4"><input name="idempotency_key" value="profile-edit">' +
      '<input name="executable_path" value="my-tool-draft">';
    win.document.getElementById("main").appendChild(profileDraft);
    const providerDraft = win.document.createElement("form");
    providerDraft.setAttribute("data-ai-provider-command", ""); providerDraft.dataset.dirty = "true";
    providerDraft.setAttribute("data-settings-action", "ai-provider-save-original");
    providerDraft.innerHTML = '<input name="provider_uuid" value="original-provider">' +
      '<input name="expected_revision" value="3"><input name="idempotency_key" value="provider-edit">' +
      '<input name="name" value="provider draft">';
    win.document.getElementById("main").appendChild(providerDraft);
    const modelDraft = win.document.createElement("form");
    modelDraft.dataset.aiCatalogueCommand = "model-params"; modelDraft.dataset.dirty = "true";
    modelDraft.dataset.settingsAction = "ai-model-params-original";
    modelDraft.innerHTML = '<input name="model_uuid" value="original-model">' +
      '<input name="expected_revision" value="8"><input name="idempotency_key" value="model-edit">' +
      '<textarea name="params"></textarea>';
    modelDraft.elements.params.value = '{"max_tokens":32}';
    win.document.getElementById("main").appendChild(modelDraft);
    const networkDraft = win.document.createElement("form");
    networkDraft.setAttribute("data-ai-network-command", "");
    networkDraft.setAttribute("data-ai-network-draft", "");
    networkDraft.dataset.dirty = "true";
    networkDraft.dataset.settingsAction = "ai-network-draft-model-original";
    networkDraft.innerHTML = '<input name="model_uuid" value="original-model">' +
      '<input name="provider_uuid" value="original-provider">' +
      '<input name="expected_revision" value="8"><input name="provider_revision" value="3">' +
      '<input name="keys_revision" value="4"><input name="idempotency_key" value="original-probe">' +
      '<button name="confirmed_command" value="explicit-current-conditions">confirm</button>';
    win.document.getElementById("main").appendChild(networkDraft);
    const beforeRulesRefresh = results.length;
    sequence++;
    sockets[0].message({type: "state", epoch: "epoch-1", sequence, scope: "settings", data: {}});
    await waitFor(() => results.length > beforeRulesRefresh);
    await new Promise(resolve => setTimeout(resolve, 60));
    assert.equal(win.document.querySelector('[data-settings-action="rule-paths-editor"]'), rejectedRule);
    assert.equal(rejectedRule.elements.namedItem("name").value, "My rule draft");
    assert.equal(rejectedRule.elements.condition_value.value, "My condition draft");
    assert.equal(rejectedRule.elements.expected_revision.value, ruleRevision);
    assert.equal(rejectedRule.elements.idempotency_key.value, ruleKey);
    assert.equal(win.document.querySelector('[data-settings-action="rule-paths-delete-original"]'), retryRule,
      "a rejected command for a removed rule survives a snapshot without that rule");
    assert.equal(retryRule.elements.rule_uuid.value, "original-rule");
    assert.equal(retryRule.elements.expected_revision.value, "5");
    assert.equal(retryRule.elements.idempotency_key.value, "original-delete");
    assert.equal(win.document.querySelector('[data-settings-action="profile-original"]'), profileDraft);
    assert.equal(profileDraft.elements.profile_uuid.value, "original-profile");
    assert.equal(profileDraft.elements.expected_revision.value, "4");
    assert.equal(profileDraft.elements.executable_path.value, "my-tool-draft");
    assert.equal(win.document.querySelector('[data-settings-action="ai-provider-save-original"]'), providerDraft);
    assert.equal(providerDraft.elements.provider_uuid.value, "original-provider");
    assert.equal(providerDraft.elements.expected_revision.value, "3");
    assert.equal(providerDraft.elements.idempotency_key.value, "provider-edit");
    assert.equal(providerDraft.elements.name.value, "provider draft");
    assert.equal(win.document.querySelector('[data-ai-catalogue-draft]'), keyDraft);
    assert.equal(win.document.querySelector('[data-credential-draft]'), vaultDraft);
    assert.equal(vaultDraft.elements.password.value, 'browser-only-vault-password');
    assert.equal(vaultDraft.elements.expected_revision.value, vaultRevision);
    assert.equal(vaultDraft.elements.idempotency_key.value, vaultCommand);
    assert(vaultDraft.querySelector('[name="confirmed_command"]'), 'broadcast keeps explicit vault confirmation');
    assert.equal(keyDraft.elements.api_keys.value, "browser-only-key-fixture");
    assert.equal(keyDraft.elements.expected_revision.value, keyRevision);
    assert.equal(keyDraft.elements.idempotency_key.value, keyCommand);
    assert(keyDraft.querySelector('[name="confirmed_command"]'), "later broadcasts keep explicit confirmation");
    assert.equal(win.document.querySelector('[data-settings-action="ai-model-params-original"]'), modelDraft);
    assert.equal(modelDraft.elements.model_uuid.value, "original-model");
    assert.equal(modelDraft.elements.expected_revision.value, "8");
    assert.equal(modelDraft.elements.idempotency_key.value, "model-edit");
    assert.equal(modelDraft.elements.params.value, '{"max_tokens":32}');
    assert.equal(win.document.querySelector('[data-ai-network-draft]'), networkDraft,
      "a rejected network operation survives a snapshot that no longer contains its model");
    assert.equal(networkDraft.elements.model_uuid.value, "original-model");
    assert.equal(networkDraft.elements.provider_uuid.value, "original-provider");
    assert.equal(networkDraft.elements.expected_revision.value, "8");
    assert.equal(networkDraft.elements.provider_revision.value, "3");
    assert.equal(networkDraft.elements.keys_revision.value, "4");
    assert.equal(networkDraft.elements.idempotency_key.value, "original-probe");
    assert.equal(networkDraft.elements.confirmed_command.value, "explicit-current-conditions");
    assert.equal(win.document.querySelector('[data-ai-operation-state]').textContent, state,
      "operation results still refresh while the original conflict conditions remain protected");
    assert.equal(win.document.querySelector('[data-settings-action="ai-chain-draft-original"]'), chainDraft);
    assert.equal(chainDraft.elements.model_uuid.value, "second-model");
    assert.equal(chainDraft.elements.expected_revision.value, chainRevision);
    assert.equal(chainDraft.elements.idempotency_key.value, chainCommand);
    assert(chainDraft.querySelector('[name="confirmed_command"]'), "deleted model chain draft keeps explicit confirmation controls");
  const beforeSettingsRefresh = results.length;
  sequence++;
  sockets[0].message({type: "state", epoch: "epoch-1", sequence, scope: "settings", data: {}});
  await waitFor(() => results.length > beforeSettingsRefresh);
  await new Promise(resolve => setTimeout(resolve, 60));
  assert.equal(win.document.querySelector('[data-settings-action="system"]'), rejectedSettings);
  assert.equal(rejectedSettings.elements.expected_revision.value, settingsRevision);
  assert.equal(rejectedSettings.elements.idempotency_key.value, settingsKey);
  assert.equal(win.document.querySelector('[data-settings-action="toolchain-install"]'), installDraft);
  assert.equal(installDraft.elements.expected_revision.value, installRevision);
  assert.equal(installDraft.elements.idempotency_key.value, installKey);
  assert.equal(win.document.querySelector('[data-settings-action="ai-master"]'), masterDraft);
  assert.equal(masterDraft.elements.ai_enabled.checked, false);
  assert.equal(masterDraft.elements.expected_revision.value, masterRevision);
  assert.equal(masterDraft.elements.idempotency_key.value, masterKey);
  assert.equal(win.document.querySelector('[data-settings-action="path-ai"]'), pathDraft);
  assert.equal(pathDraft.elements.ai_prompt.value, "unsaved path prompt");
  assert.equal(pathDraft.elements.ai_batch_size.value, "31");
  assert.equal(pathDraft.elements.ai_stream.checked, true);
  assert.equal(pathDraft.elements.expected_revision.value, pathRevision);
  assert.equal(pathDraft.elements.idempotency_key.value, pathKey);
  assert.equal(win.document.querySelector('[data-settings-action="path-ai-prompt"]'), resetDraft);
  assert.equal(resetDraft.elements.expected_revision.value, resetRevision);
  assert.equal(resetDraft.elements.idempotency_key.value, resetKey);
  assert.equal(win.document.querySelector('[data-settings-action="path-model-source"]'), modelSourceDraft);
  assert.equal(modelSourceDraft.elements.ai_model_source.value, "custom");
  assert.equal(modelSourceDraft.elements.expected_revision.value, modelSourceRevision);
  assert.equal(modelSourceDraft.elements.idempotency_key.value, modelSourceKey);
  assert.equal(win.document.querySelector('[data-settings-action="rearchive"]'), rearchiveDraft);
  assert.equal(win.document.querySelector('[data-work-source="preview"]'), workSourceDraft);
  assert.equal(workSourceDraft.elements.preview_url.value, 'https://telegra.ph/unsaved');
  assert.equal(workSourceDraft.elements.expected_revision.value, workSourceRevision);
  assert.equal(workSourceDraft.elements.idempotency_key.value, workSourceKey);
  assert.equal(rearchiveDraft.elements.force.checked, true);
  assert.equal(rearchiveDraft.elements.dry_run.checked, true);
  assert.equal(rearchiveDraft.elements.expected_revision.value, rearchiveRevision);
  assert.equal(rearchiveDraft.elements.idempotency_key.value, rearchiveKey);
  assert.equal(win.document.querySelector('[data-settings-action="path-cache-clear"]'), cacheDraft);
  assert.equal(cacheDraft.elements.idempotency_key.value, cacheKey);
  const selectedRow = win.document.querySelector('[data-candidate-id="1"]');
  const heldSelection = win.document.createElement("form");
  heldSelection.setAttribute("data-candidate-form", "");
  heldSelection.innerHTML = '<input type="hidden" name="expected_revision_1" value="observed-7">' +
    '<input type="hidden" name="idempotency_key" value="observed-batch-key">';
  selectedRow.before(heldSelection); heldSelection.appendChild(selectedRow);
  const beforeSelectedUpdate = results.length;
  state = "selection-updated"; sequence++;
  sockets[0].message({type: "state", epoch: "epoch-1", sequence, scope: "candidate", data: {candidate_id: 1}});
  await waitFor(() => results.length > beforeSelectedUpdate);
  await new Promise(resolve => setTimeout(resolve, 60));
  assert.equal(win.document.querySelector('[data-candidate-id="1"]'), selectedRow, "selected rows keep the observed snapshot");
  assert.equal(win.document.querySelector('[data-state]').textContent, "running");
  assert.equal(heldSelection.querySelector('[name="expected_revision_1"]').value, "observed-7");
  assert.equal(heldSelection.querySelector('[name="idempotency_key"]').value, "observed-batch-key");
  assert.match(win.document.querySelector('[data-realtime-status]').textContent, /原版本/);
  heldSelection.removeAttribute("data-candidate-form");
  heldSelection.setAttribute("data-downloaded-form", "");
  const beforeDownloadedUpdate = results.length;
  state = "downloaded-selection-updated"; sequence++;
  sockets[0].message({type: "state", epoch: "epoch-1", sequence, scope: "download", data: {candidate_id: 1}});
  await waitFor(() => results.length > beforeDownloadedUpdate);
  await new Promise(resolve => setTimeout(resolve, 60));
  assert.equal(win.document.querySelector('[data-candidate-id="1"]'), selectedRow, "downloaded selections retain the observed row");
  assert.equal(heldSelection.querySelector('[name="expected_revision_1"]').value, "observed-7");
  assert.equal(heldSelection.querySelector('[name="idempotency_key"]').value, "observed-batch-key");
  const renameTrigger = win.document.createElement("button");
  renameTrigger.type = "button";
  Object.assign(renameTrigger.dataset, {renameOpen: "1", renameName: "Old.cbz", renameRelative: "Old.cbz",
    renameWorkId: "observed-work", renameRevision: "7"});
  heldSelection.appendChild(renameTrigger);
  const renameDrawer = win.document.createElement("div");
  renameDrawer.setAttribute("data-rename-drawer", ""); renameDrawer.hidden = true;
  renameDrawer.innerHTML = '<form method="post" data-rename-form>' +
    '<input name="work_id" type="hidden"><input name="expected_revision" type="hidden">' +
    '<input name="idempotency_key" type="hidden"><input id="rename-filename" name="filename">' +
    '<input id="rename-directory" name="directory"><input name="repack" type="checkbox">' +
    '<button type="submit">save</button></form>';
  win.document.body.appendChild(renameDrawer);
  win.eval(asset("downloaded.js")); renameTrigger.click();
  const renameForm = renameDrawer.querySelector("form");
  const renameKey = renameForm.elements.idempotency_key.value;
  renameForm.elements.filename.value = "Unsaved name";
  renameForm.elements.directory.value = "Unsaved/Directory";
  renameForm.elements.filename.dispatchEvent(new win.Event("input", {bubbles: true}));
  renameTrigger.dataset.renameRevision = "99"; renameTrigger.click();
  assert.equal(renameForm.elements.expected_revision.value, "7", "reopening a dirty drawer cannot adopt a newer version");
  const beforeRename = results.length;
  renameForm.dispatchEvent(new win.SubmitEvent("submit", {bubbles: true, cancelable: true}));
  await waitFor(() => results.length > beforeRename && !renameForm.hasAttribute("aria-busy"));
  assert.equal(renameDrawer.hidden, false, "409 keeps the rename drawer open");
  assert.equal(renameForm.elements.filename.value, "Unsaved name");
  assert.equal(renameForm.elements.directory.value, "Unsaved/Directory");
  assert.equal(renameForm.elements.expected_revision.value, "7");
  assert.equal(renameForm.elements.idempotency_key.value, renameKey);
  assert.equal(renameForm.dataset.dirty, "true");
  renameConflict = false;
  renameForm.dispatchEvent(new win.SubmitEvent("submit", {bubbles: true, cancelable: true}));
  await waitFor(() => renameDrawer.hidden);
  state = "selection-released";
  win.EhBotRealtime.load("/candidates?search=release_selection&page_size=125");
  await waitFor(() => win.document.querySelector('[data-state]').textContent === "selection-released");
  assert.equal(win.document.querySelector('[name="candidate_ids"]').checked, false, "explicit filtering releases the old selection");
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
  assert.match(win.document.querySelector("[data-realtime-status]").textContent, /设置有更新.*原版本/);
  assert.equal(win.document.getElementById("title"), title);
  const sourceName = win.document.getElementById("source-draft-name");
  const sourceForm = sourceName.form;
  const sourceVersion = sourceForm.elements.expected_revision.value;
  const sourceKey = sourceForm.elements.idempotency_key.value;
  sourceName.value = "My source draft";
  sourceName.dispatchEvent(new win.Event("input", {bubbles: true}));
  const beforeSourceRefresh = results.length;
  state = "source-deleted";
  sockets[1].message({type: "state", epoch: "epoch-1", sequence: ++sequence, scope: "conversion", data: {candidate_id: 1}});
  await waitFor(() => results.length > beforeSourceRefresh);
  await new Promise(resolve => setTimeout(resolve, 60));
  assert.equal(win.document.getElementById("source-draft-name"), sourceName, "background deletion preserves source draft node");
  assert.equal(sourceName.value, "My source draft");
  assert.match(win.document.querySelector("[data-realtime-status]").textContent, /未保存草稿已保留/);
  win.document.getElementById("external-pack").click();
  await waitFor(() => win.document.querySelector("[data-state]").textContent === "source-deleted");
  assert.equal(win.document.getElementById("source-draft-name"), sourceName, "unrelated submission keeps remotely deleted source draft");
  assert.equal(sourceForm.elements.expected_revision.value, sourceVersion, "refresh cannot silently adopt a newer version");
  assert.equal(sourceForm.elements.idempotency_key.value, sourceKey, "unconfirmed draft keeps its original command identity");
  sourceForm.dispatchEvent(new win.SubmitEvent("submit", {bubbles: true, cancelable: true}));
  await waitFor(() => win.document.querySelector('[name="confirmed_revision"]'));
  const conflictForm = win.document.getElementById("source-draft-name").form;
  assert.notEqual(conflictForm, sourceForm, "failed source submission displays server conflict controls");
  assert.equal(conflictForm.elements.display_name.value, "My source draft");
  assert.equal(conflictForm.elements.expected_revision.value, sourceVersion);
  assert.equal(conflictForm.elements.idempotency_key.value, sourceKey);
  assert.equal(conflictForm.dataset.dirty, "true", "returned conflict draft remains protected from refresh");
  delete conflictForm.dataset.dirty;
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
