/* Progressive enhancement for /settings/{section}.
 *
 * Everything on the settings page already works without this file: every control
 * is a real form field, 试跑 and 预览 are real submit buttons posting to real
 * endpoints, and the server validates every value it stores. What this adds is
 * that the operator sees the rule's DSL as they build it, and can grow the
 * condition list from an HTML button instead of saving each row.
 *
 * Two rules, the same ones `work.js` follows:
 *
 * 1. It writes no vocabulary. There is no state label in this file and no
 *    Chinese: field and operator choices come from the `vocabulary` the
 *    template was rendered with, and the preview is a rendering of what
 *    `render_rule_dsl` will produce.
 * 2. It is never the gate. The DSL rendered here is a preview, and every rule
 *    is validated by `validate_rule_ast` on the server when it is saved. The
 *    engine's own grammar is the only authority on what an operator may store,
 *    so a disagreement between the browser and the server can only ever cost
 *    one refused save -- the same thing a missing this file costs anyway.
 */
(function () {
  "use strict";

  /* ------------------------------------------------------------ rule editor */

  var editor = document.querySelector("[data-rule-editor]");
  if (editor) {
    var preview = editor.querySelector("[data-dsl-preview]");

    /* Mirrors `render_rule_dsl`'s quoting closely enough to be recognisable.
     * JSON.stringify is what the Python side uses too (`json.dumps`), so a
     * value with a quote or a backslash renders the same on both sides. */
    var quote = function (value) {
      return JSON.stringify(value);
    };

    /* Two-word operators are stored as one token, rendered with a space. The
     * inverse map lives in the server's `_OPERATOR_RENDER`, too. */
    var SPACED = {
      NOT_LIKE: "NOT LIKE",
      NOT_IN: "NOT IN",
      NOT_EXISTS: "NOT EXISTS",
    };

    /* The parse that asked an operator "does it apply to this field?" -- the
     * engine's vocabulary already locked the operator list to what the field's
     * role allows, so the browser only has to render the submitted token. */
    var renderRow = function (row) {
      var field = row.querySelector("[data-row-field]");
      var operator = row.querySelector("[data-row-operator]");
      var value = row.querySelector("[data-row-value]");
      if (!field || !field.value) return null;

      var token = "{" + field.value + "}";
      var raw = value ? value.value.trim() : "";
      var op = operator ? operator.value : "";
      var spaced = SPACED[op] || op;

      if (op === "EXISTS" || op === "NOT_EXISTS") {
        /* A collection field carries one tag in parens; a scalar field carries
         * nothing. Both match `render_rule_dsl`. */
        if (field.value === "TAG") {
          return raw
            ? { text: token + " " + spaced + "(" + quote(raw) + ")" }
            : null;
        }
        return { text: token + " " + spaced };
      }
      if (!raw) return null;
      if (op === "IN" || op === "NOT_IN") {
        var items = raw
          .split(",")
          .map(function (item) {
            return item.trim();
          })
          .filter(Boolean);
        if (!items.length) return null;
        return { text: token + " " + spaced + " " + JSON.stringify(items) };
      }
      if (op === ">" || op === ">=" || op === "<" || op === "<=") {
        return { text: token + " " + spaced + " " + parseFloat(raw) };
      }
      return { text: token + " " + spaced + " " + quote(raw) };
    };

    var render = function () {
      var rows = editor.querySelectorAll("[data-condition-row]");
      var parts = [];
      for (var i = 0; i < rows.length; i += 1) {
        var result = renderRow(rows[i]);
        if (!result) continue;
        parts.push(result.text);
      }

      if (!preview) return;
      if (!parts.length) {
        preview.textContent = "";
        return;
      }
      if (parts.length === 1) {
        /* One row saves as itself, not as a group of one -- same as the server
         * does, so the preview matches the DSL that gets stored. */
        preview.textContent = parts[0];
        return;
      }
      var group = editor.querySelector("[name='group_operator']");
      var joiner = " " + (group ? group.value : "AND") + " ";
      preview.textContent = "(" + parts.join(joiner) + ")";
    };

    /* ------------------------------------------------- arbitrary condition rows
     *
     * The number of conditions was never a rule-engine limit: the AST takes any
     * number of children, `_parse_rule_condition` walks whatever the form sends,
     * and the rows submit as parallel lists that repeat a name. The cap was the
     * Jinja loop that rendered exactly `max(rows + 1, 3)` of them, so an operator
     * who wanted a fourth field had to save, reopen and use the spare row.
     *
     * A row is cloned from one already on the page rather than built here, so the
     * field and operator `<option>` lists stay generated from the engine's own
     * vocabulary in `_auto_approval.html` -- this file still writes no vocabulary.
     */
    var rowsHost = editor.querySelector("[data-condition-rows]");
    var addRow = editor.querySelector("[data-condition-add]");
    var addRowHost = editor.querySelector("[data-condition-add-row]");
    var countLabel = editor.querySelector("[data-condition-count]");

    var rowList = function () {
      return rowsHost
        ? rowsHost.querySelectorAll("[data-condition-row]")
        : editor.querySelectorAll("[data-condition-row]");
    };

    /* Renumbers the legends and re-points every `for`/`id` pair after an add or a
     * remove. Without the id fixup two rows would share `field-3`, and a
     * `<label for>` pointing at a duplicate id sends the focus to whichever the
     * browser saw first -- so clicking 「字段」 on row 4 would focus row 3. */
    var resequence = function () {
      var rows = rowList();
      for (var i = 0; i < rows.length; i += 1) {
        var row = rows[i];
        var index = i + 1;
        var number = row.querySelector("[data-row-number]");
        if (number) number.textContent = "条件 " + index;
        ["field", "op", "value"].forEach(function (prefix) {
          var control = row.querySelector("[id^='" + prefix + "-']");
          var label = row.querySelector("label[for^='" + prefix + "-']");
          if (!control) return;
          var id = prefix + "-" + index;
          control.id = id;
          if (label) label.setAttribute("for", id);
        });
        var remove = row.querySelector("[data-row-remove]");
        /* Never offer to remove the last row: the editor must always show one,
         * and an empty row costs nothing because a blank field is skipped. */
        if (remove) remove.hidden = rows.length <= 1;
      }
      if (countLabel) {
        countLabel.textContent = rows.length + " 个条件";
      }
    };

    if (addRow && rowsHost) {
      if (addRowHost) addRowHost.hidden = false;

      addRow.addEventListener("click", function () {
        var rows = rowList();
        var template = rows[rows.length - 1];
        if (!template) return;
        var clone = template.cloneNode(true);
        /* A clone of a filled row would duplicate its values, which is not what
         * 添加条件 means. Reset to the empty state the server skips. */
        clone.querySelectorAll("select, input").forEach(function (control) {
          if (control.tagName === "SELECT") control.selectedIndex = 0;
          else control.value = "";
        });
        rowsHost.appendChild(clone);
        resequence();
        var field = clone.querySelector("[data-row-field]");
        if (field) field.focus();
        render();
      });

      /* Delegated, so a row added after load is covered by the same handler. */
      rowsHost.addEventListener("click", function (event) {
        var button = event.target.closest("[data-row-remove]");
        if (!button) return;
        var row = button.closest("[data-condition-row]");
        if (!row || rowList().length <= 1) return;
        row.parentNode.removeChild(row);
        resequence();
        render();
      });

      resequence();
    }

    editor.addEventListener("input", render);
    editor.addEventListener("change", render);
    render();
  }

  /* -------------------------------------------------------- path template */

  var templateInput = document.querySelector("[data-template-input]");
  if (templateInput) {
    var tokens = document.querySelectorAll("[data-template-token]");
    for (var t = 0; t < tokens.length; t += 1) {
      tokens[t].addEventListener("click", function (event) {
        var token = event.currentTarget.getAttribute("data-template-token");
        /* Insert at the cursor rather than appending: an operator adding
         * `{artist}` in front of `{title}` should not have to retype the rest.
         * `selectionStart` is null on some input types, hence the fallback. */
        var start = templateInput.selectionStart;
        var end = templateInput.selectionEnd;
        var current = templateInput.value;
        if (start === null || start === undefined) {
          templateInput.value = current + token;
        } else {
          templateInput.value =
            current.slice(0, start) + token + current.slice(end);
          var caret = start + token.length;
          templateInput.setSelectionRange(caret, caret);
        }
        templateInput.focus();
      });
    }
  }
})();
