// Optional enhancements. Navigation and forms also work without JavaScript.
document.querySelectorAll('[data-profile-dialog]').forEach((dialog) => {
  if (typeof dialog.showModal !== 'function') return;
  const open = () => {
    dialog.showModal();
    [...dialog.querySelectorAll('[required]')].find((field) => !field.value)?.focus();
  };
  if (dialog.dataset.autoOpen === 'true') {
    dialog.close();
    open();
  }
  document.querySelectorAll('[data-open-profile]').forEach((link) => {
    link.addEventListener('click', (event) => {
      event.preventDefault();
      open();
    });
  });
  dialog.addEventListener('cancel', (event) => {
    if (dialog.dataset.autoOpen === 'true') {
      event.preventDefault();
      window.location.assign(dialog.dataset.cancelUrl);
    }
  });
});
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
    button.textContent = show ? gettext('Hide') : gettext('Show');
    button.setAttribute('aria-pressed', String(show));
  });
});

document.querySelectorAll('[data-model-picker]').forEach((picker) => {
  const input = picker.querySelector('[data-model-input]');
  const select = picker.querySelector('[data-model-select]');
  const list = picker.querySelector('[data-model-options]');
  const free = picker.querySelector('[data-model-free]');
  const status = picker.querySelector('[data-model-status]');
  const caption = picker.querySelector('[data-model-chosen]');
  const models = [...select.options].filter((option) => option.value).map((option) => ({
    id: option.value, name: option.dataset.name || option.value,
    pricing: option.dataset.price || '', free: option.dataset.free === 'true',
  }));
  let shown = [];
  let active = -1;
  let query = '';
  const selected = () => models.find((model) => model.id === select.value);
  const close = () => {
    list.hidden = true;
    input.setAttribute('aria-expanded', 'false');
    input.removeAttribute('aria-activedescendant');
    input.value = selected()?.name || '';
  };
  const highlight = (index) => {
    if (!shown.length) return;
    active = (index + shown.length) % shown.length;
    [...list.children].forEach((option, i) => option.classList.toggle('highlighted', i === active));
    input.setAttribute('aria-activedescendant', `model-option-${active}`);
    list.children[active].scrollIntoView({ block: 'nearest' });
  };
  const choose = (model) => {
    select.value = model.id;
    caption.textContent = model.pricing;
    status.textContent = gettext('Model selected. Save your settings to use it.');
    input.focus();
    close();
  };
  const render = (value = '') => {
    query = value;
    const term = value.trim().toLowerCase().replace(/[۰-۹]/g, (digit) => String('۰۱۲۳۴۵۶۷۸۹'.indexOf(digit))).replace(/گوگل/g, 'google').replace(/جمینای|جمنای/g, 'gemini').replace(/آرگون/g, 'argon');
    const matching = models.filter((model) => (!free.checked || model.free) && `${model.name} ${model.id}`.toLowerCase().includes(term));
    matching.sort((a, b) => Number(b.id === select.value) - Number(a.id === select.value));
    shown = matching.slice(0, 60);
    list.replaceChildren(...shown.map((model, index) => {
      const option = document.createElement('button');
      option.type = 'button'; option.role = 'option'; option.tabIndex = -1;
      option.id = `model-option-${index}`;
      option.setAttribute('aria-selected', String(model.id === select.value));
      const name = document.createElement('strong'); name.textContent = model.name; name.dir = 'auto';
      const price = document.createElement('span'); price.textContent = model.pricing; price.dir = 'auto';
      option.append(name, price);
      option.addEventListener('click', () => choose(model));
      return option;
    }));
    if (!shown.length) {
      const empty = document.createElement('p');
      empty.className = 'model-empty'; empty.textContent = gettext('No matching models. Try a different search.');
      list.append(empty);
    }
    list.hidden = false;
    input.setAttribute('aria-expanded', 'true');
    active = -1;
    input.removeAttribute('aria-activedescendant');
    status.textContent = matching.length ? interpolate(gettext('%s matching models. Use arrow keys and Enter to select.'), [matching.length]) : gettext('No matching models. Try a different search.');
  };
  picker.querySelector('[data-model-combobox]').hidden = false;
  picker.querySelector('[data-model-label]').htmlFor = input.id;
  select.hidden = true;
  input.value = selected()?.name || '';
  input.addEventListener('focus', () => { input.select(); render(); });
  input.addEventListener('click', () => { if (list.hidden) { input.select(); render(); } });
  input.addEventListener('input', () => render(input.value));
  input.addEventListener('keydown', (event) => {
    if (event.isComposing) return;
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      if (list.hidden) render();
      highlight(active < 0 ? (event.key === 'ArrowDown' ? 0 : shown.length - 1) : active + (event.key === 'ArrowDown' ? 1 : -1));
    } else if (event.key === 'Enter' && !list.hidden) {
      event.preventDefault();
      if (shown.length) choose(shown[Math.max(active, 0)]);
    } else if (event.key === 'Escape') {
      event.preventDefault(); close();
    }
  });
  picker.querySelector('[data-model-open]').addEventListener('click', () => {
    if (list.hidden) { input.focus(); input.select(); render(); } else close();
  });
  free.addEventListener('change', () => render(query));
  picker.addEventListener('focusout', () => setTimeout(() => { if (!picker.contains(document.activeElement)) close(); }, 0));
  document.addEventListener('click', (event) => { if (!picker.contains(event.target)) close(); });
});

document.querySelectorAll('[data-busy-form]').forEach((form) => {
  const button = form.querySelector('[data-busy-label]');
  const originalNodes = [...button.childNodes].map((node) => node.cloneNode(true));
  form.addEventListener('submit', () => {
    button.disabled = true;
    button.textContent = button.dataset.busyLabel;
    const status = form.querySelector('[data-busy-status]');
    if (status) status.textContent = gettext('This can take a little while. Your suggestion will be ready to review when the model finishes.');
  });
  window.addEventListener('pageshow', () => {
    button.disabled = false;
    button.replaceChildren(...originalNodes.map((node) => node.cloneNode(true)));
    const status = form.querySelector('[data-busy-status]');
    if (status) status.textContent = '';
  });
});

document.querySelectorAll('[data-outlook-plan]').forEach((form) => {
  const goal = form.querySelector('[data-outlook-goal]');
  const updateGoal = () => {
    const maintain = form.querySelector('input[name="mode"]:checked')?.value === 'maintain';
    goal.hidden = maintain;
    goal.querySelector('input').required = !maintain;
    goal.querySelector('input').disabled = maintain;
  };
  form.querySelectorAll('input[name="mode"]').forEach((input) => input.addEventListener('change', updateGoal));
  updateGoal();
});

document.querySelectorAll('[data-outlook-horizon]').forEach((select) => {
  select.addEventListener('change', () => select.form.requestSubmit());
});

document.querySelectorAll('[data-outlook-chart]').forEach((chart) => {
  const source = document.getElementById('outlook-week-data');
  if (!source) return;
  const rows = JSON.parse(source.textContent);
  const input = chart.querySelector('[data-outlook-week]');
  const cursor = chart.querySelector('[data-outlook-cursor]');
  const weekLabel = chart.querySelector('[data-outlook-week-label]');
  const dateLabel = chart.querySelector('[data-outlook-week-date]');
  const numbers = new Intl.NumberFormat(document.documentElement.lang, {maximumFractionDigits:1, minimumFractionDigits:1});
  const dates = new Intl.DateTimeFormat(document.documentElement.lang, {day:'numeric', month:'short', year:'numeric', calendar:'gregory'});
  const update = () => {
    const week = Number(input.value);
    const row = rows[week];
    cursor.setAttribute('x1', row.x);
    cursor.setAttribute('x2', row.x);
    weekLabel.textContent = `${gettext('Week')} ${new Intl.NumberFormat(document.documentElement.lang).format(week)}`;
    dateLabel.textContent = dates.format(new Date(`${row.date}T12:00:00`));
    const maintenance = chart.querySelector('[data-outlook-week-maintenance]');
    if (maintenance) maintenance.textContent = row.maintenance_values[1] === null ? '—' : new Intl.NumberFormat(document.documentElement.lang).format(row.maintenance_values[1]);
    chart.querySelectorAll('[data-outlook-week-value]').forEach((value) => {
      const weight = row.values[Number(value.dataset.outlookWeekValue)];
      value.textContent = weight === null ? '—' : numbers.format(weight);
    });
    input.setAttribute('aria-valuetext', `${weekLabel.textContent}, ${dateLabel.textContent}`);
  };
  chart.querySelector('[data-outlook-scrubber]').hidden = false;
  input.value = Math.min(12, rows.length-1);
  input.addEventListener('input', update);
  update();
});
