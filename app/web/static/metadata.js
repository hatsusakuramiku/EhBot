/* One edit model for the detail page and candidate drawer. Fetched values are
 * drafts; only actual typing becomes a manual override. */
(function () {
  "use strict";
  function rows(host) { return Array.from(host.querySelectorAll("[data-field-name]")); }
  function changes(host) {
    var payload = { fields: {}, locks: {}, fetched_fields: {} };
    rows(host).forEach(function (row) {
      var name = row.dataset.fieldName, value = row.querySelector('[data-field="value"]');
      var lock = row.querySelector('[data-field="lock"]');
      if (value && !value.disabled) {
        if (row.dataset.fetchedValue !== undefined && value.value === row.dataset.fetchedValue) {
          payload.fetched_fields[name] = value.value;
        } else if (value.value !== value.dataset.original) payload.fields[name] = value.value;
      }
      if (lock && !lock.disabled && (lock.checked ? "1" : "0") !== lock.dataset.original) {
        payload.locks[row.dataset.lockField || name] = lock.checked;
      }
    });
    var name = host.querySelector("[data-new-field]"), value = host.querySelector("[data-new-value]");
    if (name && name.value.trim()) payload.fields[name.value.trim()] = value.value;
    payload.work_id = host.dataset.workId;
    payload.expected_revision = Number(host.dataset.revision);
    var identity = JSON.stringify(payload);
    if (!host._metadataCommand || host._metadataCommand.identity !== identity) {
      host._metadataCommand = { identity: identity, key: window.crypto && window.crypto.randomUUID
        ? window.crypto.randomUUID() : Date.now().toString(36) + Math.random().toString(36).slice(2) };
    }
    payload.idempotency_key = host._metadataCommand.key;
    return payload;
  }
  function hasChanges(payload) {
    return Object.keys(payload.fields).length || Object.keys(payload.locks).length || Object.keys(payload.fetched_fields).length;
  }
  function fetchPayload(host) {
    var payload = { work_id: host.dataset.workId, expected_revision: Number(host.dataset.revision) };
    var identity = JSON.stringify(payload);
    if (!host._metadataFetchCommand || host._metadataFetchCommand.identity !== identity) {
      host._metadataFetchCommand = { identity: identity, key: window.crypto && window.crypto.randomUUID
        ? window.crypto.randomUUID() : Date.now().toString(36) + Math.random().toString(36).slice(2) };
    }
    payload.idempotency_key = host._metadataFetchCommand.key;
    return payload;
  }
  function setVersion(host, editor) {
    host.dataset.workId = editor.work_id;
    host.dataset.revision = String(editor.revision);
    ["work_id", "expected_revision"].forEach(function (name) {
      var input = host.querySelector('[name="' + name + '"]');
      if (input) input.value = name === "work_id" ? editor.work_id : String(editor.revision);
    });
    delete host._metadataCommand;
    delete host._metadataFetchCommand;
  }
  function applyFetched(host, editor) {
    rows(host).forEach(function (row) {
      var name = row.dataset.fieldName, value = row.querySelector('[data-field="value"]');
      var lock = row.querySelector('[data-field="lock"]');
      var fields = editor.fetched_fields || {};
      // Typed inputs and local lock changes are protected even before saving.
      if (!value || value.disabled || row.dataset.manual === "1" || (lock && lock.checked)
          || !Object.prototype.hasOwnProperty.call(fields, name)) return;
      var baseline = row.dataset.fetchedValue === undefined ? value.dataset.original : row.dataset.fetchedValue;
      if (value.value !== baseline) return;
      value.value = fields[name];
      if (editor.packaged) row.dataset.fetchedValue = value.value;
      else {
        value.dataset.original = value.value; delete row.dataset.fetchedValue;
        var original = host.querySelector('[name="original__' + name + '"]');
        if (original) original.value = value.value;
      }
    });
    host.dataset.dirty = "true";
  }
  function json(url, method, payload) {
    var csrf = document.querySelector('meta[name="csrf-token"]');
    return window.EhBotRealtime.fetch(url, { method: method,
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf ? csrf.content : "" },
      body: payload ? JSON.stringify(payload) : undefined,
    }).then(function (response) {
      return response.json().then(function (data) {
        if (!response.ok) throw new Error(data.error ? data.error.message : "请求失败（HTTP " + response.status + "）");
        return data;
      });
    });
  }
  function reset(host, editor) {
    var fields = {};
    editor.fields.forEach(function (field) { fields[field.field_name] = field; });
    rows(host).forEach(function (row) {
      var field = fields[row.dataset.fieldName], value = row.querySelector('[data-field="value"]');
      var lock = row.querySelector('[data-field="lock"]');
      if (field && value) { value.value = field.field_value; value.dataset.original = value.value; }
      if (field && lock) { lock.checked = !!field.is_locked; lock.dataset.original = lock.checked ? "1" : "0"; }
      var original = host.querySelector('[name="original__' + row.dataset.fieldName + '"]');
      var lockOriginal = host.querySelector('[name="lock_original__' + (row.dataset.lockField || row.dataset.fieldName) + '"]');
      if (field && original) original.value = field.field_value;
      if (field && lockOriginal) lockOriginal.value = field.is_locked ? "1" : "0";
      if (field) row.dataset.manual = field.is_manual ? "1" : "0";
      delete row.dataset.fetchedValue;
    });
    delete host.dataset.dirty;
    setVersion(host, editor);
  }
  document.addEventListener("submit", function (event) {
    var form = event.target, editor = document.querySelector("[data-metadata-editor]");
    if (!editor) return;
    var fetch = form.action.endsWith("/exhentai-metadata");
    if (form !== editor && !fetch) return;
    event.preventDefault(); event.stopImmediatePropagation();
    var status = editor.querySelector("[data-metadata-status]");
    var payload = fetch ? fetchPayload(editor) : changes(editor);
    if (!fetch && !hasChanges(payload)) { status.textContent = "没有需要保存的修改"; return; }
    var controls = Array.from(editor.elements).concat(Array.from(form.elements)).filter(function (node) { return !node.disabled; });
    controls.forEach(function (node) { node.disabled = true; });
    editor.dataset.dirty = "true";
    status.textContent = fetch ? "正在拉取…" : "正在保存…";
    json("/api/v1/works/" + editor.dataset.candidate + (fetch ? "/fetch-metadata" : "/metadata"), fetch ? "POST" : "PATCH", payload)
      .then(function (body) {
        // Restore disabled flags before examining editable values.
        controls.forEach(function (node) { node.disabled = false; });
        if (fetch) {
          if (body.status === "RUNNING") { status.textContent = "仍在拉取，可稍后再次查询；草稿已保留"; return; }
          if (!body.metadata_editor || body.snapshot_changed || body.work_id !== payload.work_id
              || body.input_revision !== payload.expected_revision
              || Number(editor.dataset.revision) !== payload.expected_revision) {
            status.textContent = "作品已变化，请核对最新状态；草稿与原版本已保留"; return;
          }
          if (body.metadata_editor.error) { status.textContent = body.metadata_editor.error; return; }
          applyFetched(editor, body.metadata_editor);
          setVersion(editor, body.metadata_editor);
          status.textContent = body.metadata_editor.packaged ? "拉取完成，归档尚未修改；请点击保存元数据" : "拉取完成，下次打包使用这些值";
        } else if (!body.metadata_editor || body.snapshot_changed) {
          status.textContent = "修改已受理，但作品随后有变化；请核对最新状态，草稿已保留";
        } else if (body.sync_skipped && !["WORK_NOT_PACKED", "NO_ARCHIVE_CHANGES"].includes(body.sync_skipped)) {
          status.textContent = body.sync_message;
        } else {
          reset(editor, body.metadata_editor);
          status.textContent = body.sync_message;
          window.EhBotRealtime.refresh();
        }
      }).catch(function (error) { status.textContent = error.message; })
      .finally(function () { controls.forEach(function (node) { node.disabled = false; }); });
  }, true);
  window.EhBotMetadata = { changes: changes, hasChanges: hasChanges, applyFetched: applyFetched, json: json,
                          fetchPayload: fetchPayload };
})();
