/* Progressive enhancement for /settings/{section}.
 *
 * Everything on the settings page already works without this file: every control
 * is a real form field, 试跑 and 预览 are real submit buttons posting to real
 * endpoints, and the server validates every value it stores. What this adds is
 * that the operator finds out about a bad regex or an odd path before pressing
 * 保存 rather than after.
 *
 * Two rules, the same ones `work.js` follows:
 *
 * 1. It writes no vocabulary. There is no state label in this file. The only
 *    Chinese it produces is the browser's own regex error text, which is data
 *    from the engine, not a status word.
 * 2. It is never the gate. The DSL rendered here is a preview of what
 *    `render_rule_dsl` will produce, and the syntax check is `new RegExp` in the
 *    same browser. The authority is `validate_rule_ast` on the server, which
 *    runs whether or not this file loaded -- so a disagreement between the two
 *    can only ever cost the operator one refused save, never an unchecked one.
 */
(function () {
  "use strict";

  /* ------------------------------------------------------------ rule editor */

  var editor = document.querySelector("[data-rule-editor]");
  if (editor) {
    var preview = editor.querySelector("[data-dsl-preview]");
    var problem = editor.querySelector("[data-syntax-error]");

    /* Mirrors `render_rule_dsl`'s quoting closely enough to be recognisable.
     * JSON.stringify is what the Python side uses too (`json.dumps`), so a
     * pattern with a quote or a backslash renders the same on both sides. */
    var quote = function (value) {
      return JSON.stringify(value);
    };

    var renderRow = function (row) {
      var field = row.querySelector("[data-row-field]");
      var kind = row.querySelector("[data-row-kind]");
      var operator = row.querySelector("[data-row-operator]");
      var value = row.querySelector("[data-row-value]");
      if (!field || !field.value) return null;

      var token = "{" + field.value + "}";
      var raw = value ? value.value.trim() : "";

      if (kind && kind.value === "regex") {
        if (!raw) return null;
        try {
          new RegExp(raw);
        } catch (error) {
          /* The message is the engine's, not ours. JavaScript's regex dialect
           * is not Python's, so this catches the common mistakes (an unclosed
           * group, a dangling quantifier) and misses the dialect-specific ones
           * -- which the server then refuses. */
          return { error: String(error.message || error) };
        }
        return { text: "Regex(" + token + ", " + quote(raw) + ")" };
      }

      var op = operator ? operator.value : "";
      if (op === "EXISTS" || op === "NOT_EXISTS") {
        return { text: token + " " + op };
      }
      if (!raw) return null;
      if (op === "HAS_ANY" || op === "HAS_ALL") {
        var items = raw
          .split(",")
          .map(function (item) {
            return item.trim();
          })
          .filter(Boolean);
        if (!items.length) return null;
        return { text: token + " " + op + " " + JSON.stringify(items) };
      }
      if (op === ">" || op === ">=" || op === "<" || op === "<=") {
        if (isNaN(parseFloat(raw))) {
          return { error: raw };
        }
        return { text: token + " " + op + " " + parseFloat(raw) };
      }
      return { text: token + " " + op + " " + quote(raw) };
    };

    var render = function () {
      var rows = editor.querySelectorAll("[data-condition-row]");
      var parts = [];
      var failure = null;
      for (var i = 0; i < rows.length; i += 1) {
        var result = renderRow(rows[i]);
        if (!result) continue;
        if (result.error) {
          failure = result.error;
          continue;
        }
        parts.push(result.text);
      }

      if (problem) {
        /* `hidden` rather than removing the node: the element carries
         * `role="alert"`, and a live region that is created at the moment it
         * gets its first message is the classic way to get one that never
         * announces. */
        problem.hidden = !failure;
        problem.textContent = failure || "";
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
        ["kind", "field", "op", "value"].forEach(function (prefix) {
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
