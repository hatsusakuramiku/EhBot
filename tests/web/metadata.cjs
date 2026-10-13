/* Run: node tests/web/metadata.cjs <path-to-jsdom>. Real metadata editor script. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { JSDOM } = require(path.resolve(process.argv[2]));
const dom = new JSDOM(`<meta name="csrf-token" content="test"><main><form id="editor" data-metadata-editor data-candidate="1">
  <p data-metadata-status></p>
  <div data-field-name="Title" data-lock-field="Title"><input data-field="value" data-original="File title" value="File title"></div>
  <div data-field-name="Summary" data-lock-field="Summary"><input data-field="value" data-original="File summary" value="File summary"></div>
  <div data-field-name="Series" data-manual="1"><input data-field="value" data-original="Manual series" value="Manual series"></div>
  <div data-field-name="Writer"><input data-field="value" data-original="Writer" value="Writer"><input type="checkbox" data-field="lock" data-original="1" checked></div>
  <button type="submit">保存</button></form><form id="fetch" action="/candidates/1/exhentai-metadata"><button type="submit">拉取</button></form></main>`,
  {url: "https://ui.example/works/1", runScripts: "outside-only"});
const win = dom.window, requests = [];
let refreshes = 0;
win.EhBotRealtime = {refresh: () => refreshes++, fetch: (url, options) => new Promise(resolve => requests.push({url, options, resolve}))};
win.eval(fs.readFileSync(path.join(__dirname, "../../app/web/static/metadata.js"), "utf8"));
const editor = win.document.querySelector("[data-metadata-editor]");
const value = name => editor.querySelector(`[data-field-name="${name}"] [data-field="value"]`);
const fields = {Title: "Fetched title", Summary: "Fetched summary", Series: "Fetched series", Writer: "Fetched writer"};
const flush = () => new Promise(resolve => setTimeout(resolve, 10));
const snapshot = {packaged: true, fetched_fields: fields, fields: []};
const submit = form => form.dispatchEvent(new win.SubmitEvent("submit", {bubbles: true, cancelable: true}));
const respond = (request, body) => request.resolve({ok: true, json: () => Promise.resolve(body)});
(async () => {
  value("Summary").value = "Unsaved typed summary";
  submit(win.document.getElementById("fetch"));
  assert.equal(requests[0].options.method, "POST");
  respond(requests[0], {metadata_editor: snapshot}); await flush();
  assert.equal(value("Title").value, "Fetched title");
  assert.equal(value("Summary").value, "Unsaved typed summary");
  assert.equal(value("Series").value, "Manual series");
  assert.equal(value("Writer").value, "Writer");
  assert.equal(editor.dataset.dirty, "true");
  assert.match(editor.querySelector("[data-metadata-status]").textContent, /归档尚未修改/);
  assert.equal(requests.length, 1, "fetch never implicitly submits a save");
  submit(editor);
  const payload = JSON.parse(requests[1].options.body);
  assert.deepEqual(payload, {fields: {Summary: "Unsaved typed summary"}, locks: {}, fetched_fields: {Title: "Fetched title"}});
  respond(requests[1], {sync_skipped: "WORK_PACK_RUNNING", sync_message: "正在打包，本次未同步归档"}); await flush();
  assert.equal(value("Title").value, "Fetched title");
  assert.equal(editor.dataset.dirty, "true");
  assert.equal(refreshes, 0, "skipped writes preserve input for retry");
  submit(editor);
  respond(requests[2], {sync_message: "归档文件已同步", metadata_editor: {fields: [
    {field_name: "Title", field_value: "Fetched title", is_manual: false},
    {field_name: "Summary", field_value: "Unsaved typed summary", is_manual: true},
  ]}}); await flush();
  assert.equal(value("Title").dataset.original, "Fetched title");
  assert.equal(editor.dataset.dirty, undefined);
  assert.equal(refreshes, 1);
  const payloadAfter = win.EhBotMetadata.changes(editor);
  assert.equal(Object.keys(payloadAfter.fields).length, 0);
  assert.equal(Object.keys(payloadAfter.fetched_fields).length, 0);
  win.EhBotMetadata.applyFetched(editor, {packaged: false, fetched_fields: {Title: "Next pack title"}});
  assert.equal(win.EhBotMetadata.hasChanges(win.EhBotMetadata.changes(editor)), 0, "unpacked scrape stays automatic");
  win.close();
  process.stdout.write("metadata DOM scenarios passed\n");
})().catch(error => {win.close(); process.stderr.write(error.stack + "\n"); process.exitCode = 1;});
