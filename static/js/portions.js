// Explicit portions become editable diary text. Saved defaults never log food.
(() => {
  const root = document.querySelector('[data-portion-builder]');
  const prompt = document.getElementById('id_prompt');
  if (!root || !prompt || typeof fetch !== 'function') return;
  const gettext = window.gettext || ((text) => text);
  const form = root.querySelector('form');
  const name = form.elements.name;
  const amount = form.elements.quantity;
  const unit = form.elements.unit;
  const note = form.elements.note;
  const kind = document.getElementById('portion-kind');
  const remember = root.querySelector('[data-portion-remember]');
  const defaultsNote = root.querySelector('[data-portion-default]');
  const status = root.querySelector('[data-portion-status]');
  const forget = root.querySelector('[data-portion-forget]');
  const usual = root.querySelector('[data-portion-usual]');
  const undo = root.querySelector('[data-portion-undo]');
  let foods = JSON.parse(document.getElementById('saved-portions').textContent);
  let selected = null;
  let forgotten = null;
  let busy = false;
  const key = (value) => value.normalize('NFKC').trim().replace(/\s+/gu, ' ').toLowerCase();
  const unitsPlural = {piece: 'pieces', slice: 'slices', cup: 'cups', bowl: 'bowls', plate: 'plates', serving: 'servings', handful: 'handfuls'};
  const english = document.documentElement.lang.split('-')[0] === 'en';
  const quantity = () => Math.round(Number(amount.value) * 100) / 100;

  function announce(message, error = false) {
    status.textContent = message;
    status.classList.toggle('portion-error', error);
  }

  function setSelected(food) {
    selected = food;
    remember.checked = !food;
    forget.hidden = !food;
    usual.hidden = !food;
    defaultsNote.textContent = food
      ? gettext('Using your usual portion. Changes are just for today unless you remember them.')
      : gettext('Saved just for you. Reuse it next time without typing.');
    if (food) {
      amount.value = food.quantity;
      unit.value = food.unit;
      note.value = food.note;
      root.querySelector('details').open = Boolean(food.note);
    }
  }

  function choose(food) {
    name.value = food.name;
    setSelected(food);
    announce('');
    amount.focus({preventScroll: true});
    amount.select();
  }

  function renderFoods() {
    const suggestions = document.getElementById('portion-suggestions');
    const chips = root.querySelector('[data-portion-chips]');
    suggestions.replaceChildren();
    chips.replaceChildren();
    foods.forEach((food, index) => {
      const option = document.createElement('option');
      option.value = food.name;
      option.label = `${food.quantity} ${food.unit_label} · ${food.note}`;
      suggestions.append(option);
      if (index >= 6) return;
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'portion-chip';
      const title = document.createElement('bdi');
      title.textContent = food.name;
      const detail = document.createElement('span');
      detail.textContent = `${food.quantity} ${food.unit_label}`;
      button.append(title, detail);
      button.addEventListener('click', () => choose(food));
      chips.append(button);
    });
    chips.hidden = !foods.length;
  }

  async function post(url, data) {
    const response = await fetch(url, {method: 'POST', body: data, credentials: 'same-origin', headers: {'Accept': 'application/json'}});
    let result;
    try { result = await response.json(); } catch { throw new Error(gettext('Could not save this portion. Please try again.')); }
    if (!response.ok) {
      const errors = result.errors ? Object.values(result.errors).flat().map((error) => error.message).join(' ') : '';
      throw new Error(result.error || errors || gettext('Could not save this portion. Please try again.'));
    }
    return result.portion;
  }

  function actionData(action) {
    const data = new FormData();
    data.set('csrfmiddlewaretoken', form.elements.csrfmiddlewaretoken.value);
    data.set('action', action);
    return data;
  }

  async function operation(work) {
    if (busy) return;
    busy = true;
    const controls = [...form.querySelectorAll('input, select, button')];
    controls.forEach((control) => { control.disabled = true; });
    root.setAttribute('aria-busy', 'true');
    try { await work(); }
    catch (error) { announce(error.message || gettext('Could not save this portion. Please try again.'), true); }
    finally {
      busy = false;
      controls.forEach((control) => { control.disabled = false; });
      root.removeAttribute('aria-busy');
    }
  }

  name.addEventListener('input', () => {
    const match = foods.find((food) => key(food.name) === key(name.value)) || null;
    if (match?.id !== selected?.id || (!match && selected)) setSelected(match);
  });
  usual.addEventListener('click', () => { if (selected) setSelected(selected); });
  root.querySelectorAll('[data-portion-step], [data-portion-scale]').forEach((button) => {
    button.addEventListener('click', () => {
      const current = quantity() || 1;
      const next = button.dataset.portionScale ? current * Number(button.dataset.portionScale) : current + Number(button.dataset.portionStep);
      amount.value = String(Math.max(0.01, Math.min(100000, Math.round(next * 100) / 100)));
    });
  });

  form.addEventListener('submit', (event) => {
    event.preventDefault();
    if (busy || !form.reportValidity()) return;
    // Capture the values before disabling controls; disabled inputs are omitted by FormData.
    const data = new FormData(form);
    const shouldSave = remember.checked;
    const foodName = name.value.trim().replace(/\s+/gu, ' ');
    const details = note.value.trim().replace(/\s+/gu, ' ');
    const unitText = english
      ? (quantity() === 1 ? unit.value : (unitsPlural[unit.value] || unit.value))
      : unit.selectedOptions[0].textContent;
    const foodText = `${quantity()} ${unitText} ${foodName}${details ? ` (${details})` : ''}`;
    // Diary text follows the interface. Saved unit codes and generated English
    // nutrition fields remain canonical in the database.
    const line = kind.value ? `${kind.selectedOptions[0].textContent}: ${foodText}` : foodText;
    const nextPrompt = () => {
      const emptyHeader = kind.value && prompt.value.trim() === `${kind.selectedOptions[0].textContent}:`;
      if (!prompt.value.trim() || emptyHeader) return line;
      return `${prompt.value}${prompt.value.endsWith('\n') ? '' : '\n'}${line}`;
    };
    if (nextPrompt().length > prompt.maxLength) {
      announce(gettext('Your description is full. Shorten it before adding another portion.'), true);
      return;
    }
    operation(async () => {
      let food = selected;
      if (shouldSave) food = await post(form.action, data);
      else if (selected) food = await post(selected.url, actionData('use'));
      if (food) {
        foods = [food, ...foods.filter((entry) => entry.id !== food.id)];
        renderFoods();
      }
      // The description may have been edited while the save request was in flight.
      if (nextPrompt().length > prompt.maxLength) throw new Error(gettext('Your description is full. Shorten it before adding another portion.'));
      prompt.value = nextPrompt();
      prompt.dispatchEvent(new Event('input', {bubbles: true}));
      name.value = '';
      amount.value = '1';
      unit.value = 'serving';
      note.value = '';
      setSelected(null);
      root.querySelector('details').open = false;
      undo.hidden = true;
      announce(gettext('Added to your description. Add another food, or estimate your meals.'));
    });
  });

  forget.addEventListener('click', () => {
    if (!selected) return;
    const food = selected;
    operation(async () => {
      await post(food.url, actionData('archive'));
      forgotten = food;
      foods = foods.filter((entry) => entry.id !== food.id);
      renderFoods();
      setSelected(null);
      undo.hidden = false;
      announce(gettext('Removed from saved foods. Your diary has not changed.'));
    });
  });
  undo.addEventListener('click', () => {
    if (!forgotten) return;
    operation(async () => {
      const food = await post(forgotten.url, actionData('restore'));
      foods = [food, ...foods.filter((entry) => entry.id !== food.id)];
      renderFoods();
      name.value = food.name;
      setSelected(food);
      forgotten = null;
      undo.hidden = true;
      announce(gettext('Saved food restored.'));
    });
  });
  renderFoods();
  root.hidden = false;
})();
