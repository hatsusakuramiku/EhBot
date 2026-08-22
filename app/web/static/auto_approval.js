(() => {
  const builder = document.querySelector('[data-rule-builder]');
  if (!builder) return;
  const field = builder.querySelector('[data-rule-field]');
  const pattern = builder.querySelector('[data-rule-pattern]');
  const preview = builder.querySelector('[data-dsl-preview]');
  const render = () => {
    const value = pattern.value.trim();
    preview.textContent = value
      ? `Regex({${field.value}}, ${JSON.stringify(value)})`
      : '';
  };
  builder.addEventListener('input', render);
  builder.addEventListener('change', render);
  render();
})();