/* Account forms use the same JSON commands as native clients. */
window.EhBotUI.onContentReady(function () {
  "use strict";
  var root = document.querySelector("[data-account-manager]");
  if (!root || root.dataset.bound) return;
  root.dataset.bound = "true";
  var manager = function () { return document.querySelector("[data-account-manager]"); };
  var message = function (text) { manager().querySelector("[data-account-notice]").textContent = text; };
  var key = function () { return Array.from(crypto.getRandomValues(new Uint32Array(4))).map(function (v) { return v.toString(16); }).join("-"); };
  var post = async function (url, data) {
    var response = await fetch(url, {method: "POST", credentials: "same-origin", cache: "no-store",
      headers: {"Content-Type": "application/json", "X-CSRF-Token": manager().dataset.csrf}, body: JSON.stringify(data)});
    var result = await response.json();
    if (!response.ok) {
      var error = new Error(result.error ? result.error.message : "请求失败");
      error.completed = !!(result.error && result.error.details && result.error.details.command_id);
      throw error;
    }
    return result;
  };
  var forms = function () { return Array.from(manager().querySelectorAll("[data-account-edit],[data-account-create]")); };
  var dirty = function (form) {
    if (form.matches("[data-account-create]")) return form.elements.label.value !== "";
    return form.dataset.challenge || form.dataset.operation || form.querySelector("[data-account-field][data-dirty]") || form.querySelector("[data-account-clear]:checked") || form.elements.label.value !== form.dataset.originalLabel;
  };
  var refresh = function (form) {
    if (forms().some(function (other) { return dirty(other); })) {
      message("操作已完成。其他账号仍有未保存修改，保存后再刷新。");
    } else { location.reload(); }
  };
  var applied = function (form, account, payload) {
    if (!account || account.deleted) return;
    form.dataset.revision = String(account.revision);
    form.dataset.originalLabel = account.label;
    form.querySelectorAll("[data-account-field]").forEach(function (input) {
      var name = input.dataset.accountField;
      input.dataset.mask = account.fields[name] || "";
      if (payload && payload.fields && payload.fields[name] === input.value) delete input.dataset.dirty;
      if (!input.dataset.dirty) { input.type = "password"; input.value = input.dataset.mask; }
    });
    if (payload && payload.clear_fields) form.querySelectorAll("[data-account-clear]").forEach(function (input) {
      if (payload.clear_fields.includes(input.dataset.accountClear)) input.checked = false;
    });
    if (!dirty(form) && !form.dataset.challenge) delete form.dataset.dirty;
  };
  var poll = async function (form, operation) {
    var output = form.querySelector("[data-account-result]");
    if (!form.isConnected) return;
    try {
      var response = await fetch(manager().dataset.operationUrl.replace("OPERATION_ID", encodeURIComponent(operation)), {cache:"no-store", credentials:"same-origin"});
      if (!response.ok) throw new Error("停止状态读取失败，请刷新核对");
      var result = await response.json();
      if (result.status === "COMPLETED") {
        delete form.dataset.operation;
        var payload = form.dataset.acceptedPayload ? JSON.parse(form.dataset.acceptedPayload) : null;
        applied(form, result.result, payload);
        delete form.dataset.acceptedPayload;
        if (result.result && result.result.deleted) form.closest("[data-account-card]").remove();
        refresh(form); return;
      }
      output.textContent = result.error_code ? "旧请求或远端停止尚未确认，后台继续重试。" : "已受理，等待旧请求与远端资源确认停止。";
      setTimeout(function () { poll(form, operation); }, 2000);
    } catch (error) { output.textContent = error.message; }
  };
  var createForm = root.querySelector("[data-account-create]");
  if (!createForm.dataset.accountBound) {
  createForm.dataset.accountBound = "true";
  createForm.addEventListener("submit", async function (event) {
    event.preventDefault();
    var form = event.target;
    if (form.dataset.busy) return;
    form.dataset.busy = "true";
    form.dataset.dirty = "true";
    try {
      var payload = {action:"create", kind:form.elements.kind.value, label:form.elements.label.value, fields:{}};
      var serialized = JSON.stringify(payload);
      if (form.dataset.pendingPayload !== serialized) { form.dataset.pendingPayload = serialized; form.dataset.pendingKey = key(); }
      payload.idempotency_key = form.dataset.pendingKey;
      var result = await post(manager().dataset.commandUrl, payload);
      if (result.status !== "COMPLETED") { message("操作结果未确认，请刷新核对，避免重复添加。"); return; }
      delete form.dataset.pendingKey; delete form.dataset.pendingPayload; delete form.dataset.dirty;
      form.reset();
      refresh(form);
    } catch (error) { message(error.message); }
    finally { delete form.dataset.busy; }
  });
  }
  forms().forEach(function (form) {
    if (!form.matches("[data-account-edit]") || form.dataset.accountBound) return;
    form.dataset.accountBound = "true";
    var output = form.querySelector("[data-account-result]");
    var accountId = form.elements.account_id.value;
    var pendingKey = null;
    var pendingPayload = null;
    var busy = false;
    form.querySelectorAll("[data-account-field]").forEach(function (input) {
      input.addEventListener("input", function () { input.dataset.dirty = "true"; });
    });
    var act = async function (action) {
      if (busy) return;
      busy = true;
      try {
        if (action === "hide") {
          form.querySelectorAll("[data-account-field]").forEach(function (input) { input.type = "password"; if (!input.dataset.dirty) input.value = input.dataset.mask; });
          return;
        }
        if (action === "reveal") {
          var revealed = await post(manager().dataset.revealUrl.replace("ACCOUNT_ID", encodeURIComponent(accountId)), {expected_revision:Number(form.dataset.revision)});
          form.querySelectorAll("[data-account-field]").forEach(function (input) {
            if (!input.dataset.dirty) input.value = revealed.fields[input.dataset.accountField] || "";
            input.type = "text";
          });
          return;
        }
        if (action === "delete" && !confirm("删除此账号配置？旧执行确认停止前仍保留配置。")) return;
        var payload = {action:action, account_id:accountId, expected_revision:Number(form.dataset.revision)};
        if (action === "save") {
          var fields = {}, clear = [];
          form.querySelectorAll("[data-account-clear]:checked").forEach(function (input) { clear.push(input.dataset.accountClear); });
          form.querySelectorAll("[data-account-field][data-dirty]").forEach(function (input) { if (!clear.includes(input.dataset.accountField)) fields[input.dataset.accountField] = input.value; });
          payload.label = form.elements.label.value;
          if (Object.keys(fields).length || clear.length) { payload.action = "edit"; payload.fields = fields; payload.clear_fields = clear; }
          else { payload.action = "rename"; }
        } else if (action.startsWith("login-") && action !== "login-start") {
          if (!form.dataset.challenge) throw new Error("请先获取本账号的验证码；刷新后需重新开始登录。");
          payload.challenge_id = form.dataset.challenge;
          if (action === "login-complete") { payload.code = form.querySelector("[data-login-code]").value; payload.password = form.querySelector("[data-login-password]").value; }
        }
        // A lost response can be retried with the same payload/key. Changing
        // the draft starts a new command, which still carries the old revision.
        var serialized = JSON.stringify(payload);
        if (pendingPayload !== serialized) { pendingKey = key(); pendingPayload = serialized; }
        payload.idempotency_key = pendingKey;
        var result = await post(manager().dataset.commandUrl, payload);
        if (result.operation_id) {
          form.dataset.operation = result.operation_id; form.dataset.acceptedPayload = serialized;
          form.dataset.dirty = "true";
          pendingKey = null; pendingPayload = null;
          poll(form, result.operation_id); return;
        }
        if (result.status !== "COMPLETED") { output.textContent = "操作结果未确认，请先刷新核对账号状态。"; return; }
        pendingKey = null; pendingPayload = null;
        if (action === "login-start") {
          form.dataset.challenge = result.result.challenge_id;
          form.dataset.dirty = "true";
          output.textContent = "验证码已发送。需要两步验证时可再填写密码。";
        } else if (action === "login-cancel") {
          delete form.dataset.challenge; output.textContent = "登录已取消，保存的配置保留。";
        } else if (action === "test") {
          applied(form, result.result);
          output.textContent = "验证成功，可启用此账号。";
        } else {
          if (action === "login-complete") { delete form.dataset.challenge; form.querySelector("[data-login-code]").value = ""; form.querySelector("[data-login-password]").value = ""; }
          applied(form, result.result, payload);
          refresh(form);
        }
      } catch (error) {
        if (error.completed) { pendingKey = null; pendingPayload = null; }
        output.textContent = error.message + "；未保存的输入已保留。";
        // Retrying an explicit provider rejection (e.g. 2FA required) with a
        // changed payload uses a new key; an unchanged transport retry replays.
      } finally { busy = false; }
    };
    form.addEventListener("submit", function (event) { event.preventDefault(); act("save"); });
    form.querySelectorAll("[data-account-action]").forEach(function (button) {
      button.addEventListener("click", function () { act(button.dataset.accountAction); });
    });
    if (form.dataset.operation) poll(form, form.dataset.operation);
  });
});
