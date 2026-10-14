/* Run: node tests/web/accounts.cjs <path-to-jsdom>. No external services. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { JSDOM } = require(process.argv[2] ? path.resolve(process.argv[2]) : "jsdom");
const asset = name => fs.readFileSync(path.join(__dirname, "../../app/web/static", name), "utf8");

function markup(csrf = "first-csrf") {
  const account = id => `<form data-account-edit data-revision="1" data-original-label="${id}" method="post" action="/api/v1/accounts/commands">
    <input type="hidden" name="account_id" value="${id}"><input name="label" value="${id}">
    <input type="password" data-account-field="bot_token" data-mask="********" value="********">
    <input type="checkbox" data-account-clear="bot_token"><input data-login-code><input data-login-password>
    <button type="submit">save</button>${["reveal", "hide", "test", "login-start", "login-complete"].map(action => `<button type="button" data-account-action="${action}">${action}</button>`).join("")}
    <p data-account-result></p></form>`;
  return `<div data-account-manager data-csrf="${csrf}" data-command-url="/api/v1/accounts/commands"
    data-account-url="/api/v1/accounts/ACCOUNT_ID" data-reveal-url="/api/v1/accounts/ACCOUNT_ID/reveal"
    data-operation-url="/api/v1/accounts/operations/OPERATION_ID"><p data-account-notice></p>
    <form data-account-create method="post" action="/api/v1/accounts/commands"><select name="kind"><option>TG_BOT</option></select>
    <input name="label"><button type="submit">create</button></form>${account("a")}${account("b")}</div>`;
}

async function scenario() {
  const dom = new JSDOM(`<meta name="realtime-path" content="/ws/ui"><main id="main">${markup()}</main>`, {
    url: "http://testserver/settings/connections", runScripts: "outside-only", pretendToBeVisual: true,
  });
  const win = dom.window, callbacks = [], posts = [];
  win.EhBotUI = {onContentReady(callback) { callbacks.push(callback); callback(); }, toast() {}};
  win.WebSocket = class { send() {} close() {} };
  const evaluate = win.XPathEvaluator.prototype.evaluate;
  win.XPathEvaluator.prototype.evaluate = function (expr, context, resolver, type, result) {
    return evaluate.call(this, expr, context, resolver, type || 0, result || null);
  };
  const expressionEvaluate = win.XPathExpression.prototype.evaluate;
  win.XPathExpression.prototype.evaluate = function (context, type, result) {
    return expressionEvaluate.call(this, context, type || 0, result || null);
  };
  win.eval(asset("vendor/htmx-2.0.4.min.js"));
  let handler = async () => { throw new Error("response lost"); };
  win.fetch = async (url, options) => {
    if (options.method !== "POST") return handler({url, options});
    assert.equal(options.headers["Content-Type"], "application/json", "capture handler must not send the form again");
    const entry = {url, options, payload: JSON.parse(options.body)};
    posts.push(entry);
    return handler(entry);
  };
  win.eval(asset("realtime.js"));
  win.eval(asset("accounts.js"));
  const settle = () => new Promise(resolve => setTimeout(resolve, 10));
  const submit = form => form.dispatchEvent(new win.SubmitEvent("submit", {bubbles: true, cancelable: true}));
  const edit = win.document.querySelector('[name="account_id"][value="a"]').form;
  const other = win.document.querySelector('[name="account_id"][value="b"]').form;
  const field = edit.querySelector("[data-account-field]");
  field.value = "unsaved-token";
  field.dispatchEvent(new win.Event("input", {bubbles: true}));
  const create = win.document.querySelector("[data-account-create]");
  create.elements.label.value = "New bot";
  submit(create); await settle();
  submit(create); await settle();
  assert.equal(posts.length, 2, "one JSON request per submit");
  assert.deepEqual(posts[0].payload, posts[1].payload, "lost create response uses identical command/key");
  assert.equal(create.dataset.dirty, "true", "uncertain command survives realtime swaps");

  // Exercise HTMX's actual preserved-node swap, as the realtime module does.
  const previousRoot = win.document.querySelector("[data-account-manager]");
  const fragment = win.document.createElement("div");
  fragment.innerHTML = markup("replacement-csrf");
  for (const [old, replacement, id] of [
    [create, fragment.querySelector("[data-account-create]"), "preserved-create"],
    [edit, fragment.querySelector('[name="account_id"][value="a"]').form, "preserved-edit"],
  ]) {
    old.id = replacement.id = id;
    old.setAttribute("hx-preserve", "true");
    replacement.setAttribute("hx-preserve", "true");
  }
  win.htmx.swap(win.document.getElementById("main"), fragment.innerHTML, {swapStyle: "innerHTML", settleDelay: 0});
  await settle();
  assert.equal(win.document.getElementById("preserved-create"), create);
  assert.equal(win.document.getElementById("preserved-edit"), edit);
  callbacks.forEach(callback => callback());
  callbacks.forEach(callback => callback());
  assert.equal(previousRoot.isConnected, false);
  submit(edit); await settle();
  const firstEdit = posts.at(-1);
  assert.equal(firstEdit.options.headers["X-CSRF-Token"], "replacement-csrf");
  assert.equal(firstEdit.payload.fields.bot_token, "unsaved-token");
  const count = posts.length;
  submit(edit); await settle();
  assert.equal(posts.length, count + 1, "preserved form is bound once");
  assert.deepEqual(posts.at(-1).payload, firstEdit.payload, "lost edit response keeps its key across a swap");
  assert.equal(field.value, "unsaved-token");

  handler = async () => ({ok: true, json: async () => ({fields: {bot_token: "saved-token"}})});
  edit.querySelector('[data-account-action="reveal"]').click(); await settle();
  assert.equal(field.value, "unsaved-token", "reveal never overwrites an edited field");
  assert.equal(field.type, "text");
  edit.querySelector('[data-account-action="hide"]').click(); await settle();
  assert.equal(field.value, "unsaved-token");
  assert.equal(field.type, "password");

  handler = async () => ({ok: true, json: async () => ({status: "COMPLETED", result: {challenge_id: "challenge-a"}})});
  edit.querySelector('[data-account-action="login-start"]').click(); await settle();
  assert.equal(edit.dataset.challenge, "challenge-a");
  assert.equal(edit.dataset.dirty, "true", "login challenge stays with its form");
  edit.querySelector("[data-login-code]").value = "12345";
  handler = async () => { throw new Error("response lost"); };
  edit.querySelector('[data-account-action="login-complete"]').click(); await settle();
  assert.equal(posts.at(-1).payload.challenge_id, "challenge-a");
  assert.equal(posts.at(-1).payload.code, "12345");
  delete edit.dataset.challenge;
  handler = async entry => ({ok: true, json: async () => entry.options.method === "POST"
    ? {status: "PENDING", operation_id: "stop-a"}
    : {status: "COMPLETED", result: {revision: 4, label: "a", fields: {bot_token: "********"}}}});
  submit(edit); await settle();
  assert.equal(edit.dataset.revision, "4", "completed stop uses its frozen result revision");
  assert.equal(field.dataset.dirty, undefined, "accepted field is no longer an unsaved draft");
  assert.equal(field.value, "********");
  assert.equal(edit.dataset.operation, undefined);
  assert.equal(create.elements.label.value, "New bot", "another form's draft survives completion");
  handler = async () => { throw new Error("response lost"); };
  submit(edit); await settle();
  assert.equal(posts.at(-1).payload.action, "rename");
  assert.equal(posts.at(-1).payload.expected_revision, 4);
  assert.equal(other.isConnected, false);
  dom.window.close();
}

scenario().then(() => console.log("account DOM retry, capture, preserved-form and masking scenarios passed"))
  .catch(error => { console.error(error); process.exitCode = 1; });
