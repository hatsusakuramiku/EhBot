(() => {
  const builder = document.querySelector('[data-rule-builder]');
  if (!builder) return;
  const list = builder.querySelector('[data-condition-list]');
  const template = document.querySelector('[data-condition-template]');
  const render = () => {
    const children = [...list.querySelectorAll('.rule-condition')].map((row) => {
      const field = row.querySelector('[data-field]').value;
      const operator = row.querySelector('[data-operator]').value;
      const valueInput = row.querySelector('[data-value]');
      const condition = { kind: 'condition', field, operator };
      if (!['EXISTS', 'NOT_EXISTS'].includes(operator)) {
        condition.value = ['HAS_ANY', 'HAS_ALL'].includes(operator)
          ? valueInput.value.split(',').map((item) => item.trim()).filter(Boolean)
          : valueInput.value;
      }
      return condition;
    });
    const ast = { kind: 'group', operator: builder.querySelector('[data-group-operator]').value, children };
    builder.querySelector('[data-ast-json]').value = JSON.stringify(ast);
    builder.querySelector('[data-dsl-preview]').textContent = children.map((item) => `{${item.field}} ${item.operator}${item.value === undefined ? '' : ` ${JSON.stringify(item.value)}`}`).join(` ${ast.operator} `);
  };
  const add = () => { const row = template.content.firstElementChild.cloneNode(true); list.append(row); render(); };
  builder.querySelector('[data-add-condition]').addEventListener('click', add);
  builder.addEventListener('input', render);
  builder.addEventListener('change', render);
  builder.addEventListener('click', (event) => { if (event.target.matches('[data-remove-condition]')) { event.target.closest('.rule-condition').remove(); render(); } });
  add();
})();
