// Optional enhancements. Navigation and forms also work without JavaScript.
document.querySelectorAll('[data-confirm]').forEach((form) => {
  form.addEventListener('submit', (event) => {
    if (!window.confirm(form.dataset.confirm)) event.preventDefault();
  });
});

document.querySelectorAll('[data-password-toggle]').forEach((button) => {
  button.addEventListener('click', () => {
    const input = document.getElementById(button.dataset.passwordToggle);
    const show = input.type === 'password';
    input.type = show ? 'text' : 'password';
    button.textContent = show ? 'Hide' : 'Show';
    button.setAttribute('aria-pressed', String(show));
  });
});

document.querySelectorAll('[data-model-picker]').forEach((picker) => {
  const query = picker.querySelector('[data-model-query]');
  const free = picker.querySelector('[data-model-free]');
  const select = picker.querySelector('[data-model-select]');
  const status = picker.querySelector('[data-model-status]');
  let timer;
  let pending;
  const search = async () => {
    if (pending) pending.abort();
    pending = new AbortController();
    const url = new URL(picker.dataset.modelUrl, window.location.origin);
    url.searchParams.set('q', query.value);
    if (free.checked) url.searchParams.set('free', '1');
    status.textContent = 'Searching OpenRouter models…';
    try {
      const response = await fetch(url, { signal: pending.signal, headers: { Accept: 'application/json' } });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || 'Model search is unavailable. Please try again.');
      const current = select.value;
      const currentLabel = select.selectedOptions[0]?.textContent;
      const options = [new Option('Choose a model', '')];
      if (current && !result.models.some((model) => model.id === current)) {
        options.push(new Option(currentLabel || current, current));
      }
      result.models.forEach((model) => options.push(new Option(`${model.name} · ${model.pricing}`, model.id)));
      select.replaceChildren(...options);
      select.value = current;
      status.textContent = result.total ? `${result.total} matching models${result.total > result.models.length ? '; narrow your search to see more' : ''}. Prices are USD per million tokens.` : 'No matching models. Try a different search.';
    } catch (error) {
      if (error.name !== 'AbortError') status.textContent = error.message;
    }
  };
  query.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(search, 250); });
  free.addEventListener('change', search);
});

document.querySelectorAll('[data-busy-form]').forEach((form) => {
  const button = form.querySelector('[data-busy-label]');
  const originalNodes = [...button.childNodes].map((node) => node.cloneNode(true));
  form.addEventListener('submit', () => {
    button.disabled = true;
    button.textContent = button.dataset.busyLabel;
    const status = form.querySelector('[data-busy-status]');
    if (status) status.textContent = 'This can take a little while. Your meals will be ready to review when the model finishes.';
  });
  window.addEventListener('pageshow', () => {
    button.disabled = false;
    button.replaceChildren(...originalNodes.map((node) => node.cloneNode(true)));
    const status = form.querySelector('[data-busy-status]');
    if (status) status.textContent = '';
  });
});
