// Native forms remain usable without JavaScript. Workout saves are serialized.
(() => {
  const root = document.querySelector('[data-gym]');
  if (!root || typeof fetch !== 'function') return;
  const gettext = window.gettext || ((value) => value);
  root.classList.add('has-gym-js');
  const clock = (value) => {
    const seconds = Math.max(0, Math.floor(value));
    const parts = [Math.floor(seconds / 60) % 60, seconds % 60].map((x) => String(x).padStart(2, '0'));
    if (seconds >= 3600) parts.unshift(String(Math.floor(seconds / 3600)).padStart(2, '0'));
    return parts.join(':');
  };
  root.querySelectorAll('[data-auto-submit]').forEach((select) => select.addEventListener('change', () => select.form.requestSubmit()));

  const search = root.querySelector('[data-exercise-search]');
  let pickerQueue = Promise.resolve();
  const added = new Set();
  function bindPicker() {
    root.querySelectorAll('[data-picker-add]').forEach((form) => {
      const button = form.querySelector('button');
      const id = form.elements.exercise.value;
      if (added.has(id)) { button.disabled = true; button.textContent = gettext('Added'); }
      form.addEventListener('submit', (event) => {
        event.preventDefault();
        if (button.disabled) return;
        button.disabled = true;
        pickerQueue = pickerQueue.then(async () => {
          try {
            const response = await fetch(form.getAttribute('action'), {method:'POST', body:new FormData(form), credentials:'same-origin', headers:{Accept:'application/json'}});
            const result = await response.json();
            if (!response.ok) throw new Error(result.error || gettext('Could not add this exercise. Please try again.'));
            if (result.state) root.querySelectorAll('input[name=version]').forEach((input) => { input.value = result.state.version; });
            added.add(id);
            button.textContent = gettext('Added');
            const status = root.querySelector('[data-search-status]');
            if (status) status.textContent = gettext('Exercise added. Keep choosing, or return to your plan.');
          } catch (error) {
            button.disabled = false;
            const status = root.querySelector('[data-search-status]');
            if (status) status.textContent = error.message || gettext('Could not add this exercise. Please try again.');
          }
        });
      });
    });
  }
  bindPicker();
  if (search) {
    let debounce;
    let controller;
    async function searchExercises() {
      controller?.abort();
      const activeController = new AbortController();
      controller = activeController;
      const params = new URLSearchParams(new FormData(search));
      const url = new URL(location.href);
      url.search = params.toString();
      const fragmentUrl = new URL(url);
      fragmentUrl.searchParams.set('fragment', '1');
      const status = search.querySelector('[data-search-status]');
      status.textContent = gettext('Searching…');
      try {
        await pickerQueue;
        const response = await fetch(fragmentUrl, {credentials:'same-origin', signal:activeController.signal});
        if (!response.ok) throw new Error();
        if (controller !== activeController) return;
        const documentFragment = new DOMParser().parseFromString(await response.text(), 'text/html');
        const results = documentFragment.getElementById('gym-exercise-results');
        if (!results) throw new Error();
        root.querySelector('#gym-exercise-results').replaceWith(results);
        history.replaceState(null, '', url);
        status.textContent = '';
        bindPicker();
      } catch (error) {
        if (error.name !== 'AbortError') status.textContent = gettext('Search is unavailable. Press Search to try again.');
      }
    }
    search.addEventListener('input', (event) => { if (event.target.name === 'q') { clearTimeout(debounce); debounce = setTimeout(searchExercises, 250); } });
    search.addEventListener('change', (event) => { if (event.target.name !== 'q') searchExercises(); });
    search.addEventListener('submit', (event) => { event.preventDefault(); clearTimeout(debounce); searchExercises(); });
  }

  const routinePicker = root.querySelector('[data-routine-picker]');
  if (routinePicker) {
    const query = routinePicker.querySelector('[data-routine-query]');
    const onlySelected = routinePicker.querySelector('[data-selected-only]');
    const choices = [...routinePicker.querySelectorAll('[data-choice]')];
    const chosenOrder = [...routinePicker.querySelectorAll('input[name=exercises]:checked')].map((input) => input.value);
    const normalize = (value) => value.toLocaleLowerCase().replace(/ي/g, 'ی').replace(/ك/g, 'ک');
    function renderChoices() {
      const words = normalize(query.value).trim().split(/\s+/).filter(Boolean);
      let visible = 0;
      choices.forEach((choice) => {
        const input = choice.querySelector('input');
        const matches = words.every((word) => normalize(choice.dataset.search).includes(word));
        choice.hidden = !matches || (onlySelected.checked && !input.checked);
        choice.classList.toggle('is-selected', input.checked);
        if (!choice.hidden) visible++;
      });
      const selected = choices.map((choice) => choice.querySelector('input')).filter((input) => input.checked);
      routinePicker.querySelector('[data-selection-count]').textContent = `${selected.length} / 40 · ${gettext('exercises selected')}`;
      routinePicker.querySelector('[data-choice-empty]').hidden = visible > 0;
      const chips = routinePicker.querySelector('[data-selected-chips]');
      chips.replaceChildren();
      chosenOrder.forEach((id) => {
        const input = selected.find((item) => item.value === id);
        if (!input) return;
        const button = document.createElement('button');
        button.type = 'button'; button.className = 'gym-selected-chip';
        button.textContent = `${input.dataset.name} ×`;
        button.setAttribute('aria-label', `${gettext('Remove')}: ${input.dataset.name}`);
        button.addEventListener('click', () => { input.checked = false; chosenOrder.splice(chosenOrder.indexOf(id), 1); renderChoices(); });
        chips.append(button);
      });
    }
    routinePicker.addEventListener('change', (event) => {
      const input = event.target;
      if (input.name === 'exercises') {
        if (input.checked && chosenOrder.length >= 40) { input.checked = false; return; }
        if (input.checked) chosenOrder.push(input.value);
        else chosenOrder.splice(chosenOrder.indexOf(input.value), 1);
      }
      renderChoices();
    });
    query.addEventListener('input', renderChoices);
    query.addEventListener('keydown', (event) => { if (event.key === 'Enter') event.preventDefault(); });
    routinePicker.closest('form').addEventListener('formdata', (event) => {
      event.formData.delete('exercises');
      chosenOrder.forEach((id) => event.formData.append('exercises', id));
    });
    renderChoices();
  }

  root.querySelectorAll('[data-equipment-preset]').forEach((button) => button.addEventListener('click', () => {
    button.closest('[data-equipment-picker]').querySelectorAll('input[type=checkbox]').forEach((input) => { input.checked = button.dataset.equipmentPreset === 'all'; });
  }));

  root.addEventListener('invalid', (event) => {
    let ancestor = event.target.closest('details');
    while (ancestor) { ancestor.open = true; ancestor = ancestor.parentElement.closest('details'); }
  }, true);

  const workout = root.querySelector('[data-workout]');
  if (!workout) return;
  let state = JSON.parse(document.getElementById('gym-workout-state').textContent);
  const status = workout.querySelector('[data-save-status]');
  const retry = workout.querySelector('[data-save-retry]');
  const dialog = workout.querySelector('[data-bodyweight-dialog]');
  let clockAnchor = Date.now();
  let manualRestOpen = false;
  const queue = [];
  const invalid = new Map();
  let sending = false;
  let conflicted = false;
  let navigationAllowed = false;
  let reloadAfterSave = false;
  let redirectAfterSave = '';
  let inFlight = null;
  const finishDialog = workout.querySelector('[data-finish-dialog]');
  const finishForm = workout.querySelector('[data-finish-form]');
  let finishApproved = false;
  let unreviewedDrafts = {};
  try { unreviewedDrafts = JSON.parse(localStorage.getItem(`cotinga:gym:draft:${location.pathname}`) || '{}'); } catch { /* Storage is optional. */ }
  const storageKey = `cotinga:gym:draft:${location.pathname}`;
  function say(message, error = false) {
    status.textContent = message;
    status.classList.toggle('is-error', error);
  }
  function persistDrafts() {
    const jobs = [...invalid.values(), ...(inFlight ? [inFlight] : []), ...queue];
    const drafts = {...unreviewedDrafts};
    workout.querySelectorAll('.is-recovered[data-set-form]').forEach((form) => {
      const data = new FormData(form);
      const checkbox = form.querySelector('[name$="-completed"]');
      if (checkbox) data.set(checkbox.name, checkbox.checked ? 'on' : '');
      jobs.push({form, data});
    });
    jobs.forEach((job) => {
      if (!job.form.dataset.setForm) return;
      drafts[job.form.dataset.setForm] = Object.fromEntries([...job.data.entries()].filter(([key]) => key.startsWith('set-')));
    });
    try {
      if (Object.keys(drafts).length) localStorage.setItem(storageKey, JSON.stringify(drafts));
      else localStorage.removeItem(storageKey);
    } catch { /* The visible unsaved state and navigation guard still protect edits. */ }
  }
  function applyState(next) {
    state = next;
    clockAnchor = Date.now();
    restTimer.update(state);
    workout.querySelectorAll('input[name=version]').forEach((input) => { input.value = state.version; });
    workout.querySelector('[data-completed-count]').textContent = state.totals.completed;
    workout.querySelector('[data-set-count]').textContent = state.totals.total;
    workout.querySelector('[data-volume]').textContent = Number(state.totals.volume).toLocaleString(document.documentElement.lang);
    const unfinished = workout.querySelector('[data-unfinished-label]');
    if (unfinished) unfinished.hidden = !state.totals.remaining;
    const pause = workout.querySelector('[data-workout-pause]');
    const resume = workout.querySelector('[data-workout-resume]');
    if (pause) pause.hidden = state.paused;
    if (resume) resume.hidden = !state.paused;
    renderClocks();
  }
  async function drain() {
    if (sending || conflicted || !queue.length) return;
    sending = true;
    retry.hidden = true;
    while (queue.length) {
      const job = queue.shift();
      inFlight = job;
      job.data.set('version', state.version);
      persistDrafts();
      say(gettext('Saving…'));
      try {
        const response = await fetch(job.form.getAttribute('action'), {method:'POST', body:job.data, credentials:'same-origin', headers:{Accept:'application/json'}});
        let result;
        try { result = await response.json(); } catch { throw new Error(gettext('Connection interrupted. Your edits are still here. Retry saving.')); }
        if (!response.ok) {
          const message = result.error || gettext('Could not save this change. Please try again.');
          let error = job.form.querySelector('[data-set-error]');
          if (!error) {
            error = document.createElement('p'); error.className = 'gym-action-error';
            error.dataset.setError = ''; error.setAttribute('role', 'alert'); job.form.append(error);
          }
          error.textContent = message;
          job.form.classList.add('has-save-error');
          if (result.fields) Object.entries(result.fields).forEach(([field]) => {
            const input = job.form.querySelector(`[name="set-${job.form.dataset.setForm}-${field}"]`);
            if (input) { input.setAttribute('aria-invalid', 'true'); input.closest('details')?.setAttribute('open', ''); }
          });
          if (job.form === finishForm && finishDialog?.open) {
            finishDialog.querySelector('[data-finish-error]').textContent = message;
          }
          if (response.status === 409) {
            conflicted = true;
            retry.textContent = gettext('Reload latest workout');
            retry.hidden = false;
            queue.unshift(job);
          } else {
            invalid.set(job.form, job);
            const error = job.form.querySelector('[data-set-error]');
            if (error) error.textContent = message;
            const check = job.form.querySelector('[name$="-completed"]');
            if (check) check.checked = job.form.closest('[data-set-wrapper]').classList.contains('is-complete');
            if (result.needs_weight && typeof dialog.showModal === 'function' && !dialog.open) dialog.showModal();
          }
          say(message, true);
          if (conflicted) break;
          continue;
        }
        invalid.delete(job.form);
        if (job.form.dataset.setForm) delete unreviewedDrafts[job.form.dataset.setForm];
        job.form.classList.remove('is-recovered', 'has-save-error');
        job.form.querySelectorAll('[aria-invalid]').forEach((input) => input.removeAttribute('aria-invalid'));
        job.form.classList.toggle('is-pending', queue.some((pending) => pending.form === job.form));
        const error = job.form.querySelector('[data-set-error]');
        if (error) error.textContent = '';
        if (job.form.dataset.setForm) {
          const name = `set-${job.form.dataset.setForm}-completed`;
          job.form.closest('[data-set-wrapper]').classList.toggle('is-complete', job.data.get(name) === 'on');
        }
        if (job.data.get('action') === 'timer' && job.data.get('command') === 'cancel') manualRestOpen = false;
        applyState(result.state);
        if (result.records) {
          const summary = workout.querySelector('[data-workout-records]');
          if (summary) summary.innerHTML = result.records.summary;
          Object.entries(result.records.sets).forEach(([id, html]) => {
            const target = workout.querySelector(`[data-set-records="${id}"]`);
            if (target) target.innerHTML = html;
          });
        }
        reloadAfterSave ||= result.reload;
        redirectAfterSave ||= result.redirect;
      } catch (error) {
        const rowError = job.form.querySelector('[data-set-error]');
        if (rowError) rowError.textContent = error.message || gettext('Connection interrupted. Your edits are still here. Retry saving.');
        if (!job.form.dataset.setForm || !queue.some((pending) => pending.form === job.form)) queue.unshift(job);
        say(error.message || gettext('Connection interrupted. Your edits are still here. Retry saving.'), true);
        retry.textContent = gettext('Retry saving');
        retry.hidden = false;
        break;
      } finally { inFlight = null; persistDrafts(); }
    }
    sending = false;
    if (!queue.length) {
      if (invalid.size) {
        const firstError = [...invalid.values()][0];
        const message = firstError.form.querySelector('[data-set-error]')?.textContent;
        say(message || gettext('Some changes need attention. Check the highlighted fields.'), true);
      }
      else say(gettext('All changes saved'));
      if (redirectAfterSave || reloadAfterSave) {
        persistDrafts();
        navigationAllowed = true;
        if (redirectAfterSave) location.assign(redirectAfterSave);
        else location.reload();
      }
    }
  }
  function enqueue(form, submitter) {
    if (form.dataset.setForm) {
      form.classList.add('is-pending');
      form.querySelector('[data-set-error]').textContent = gettext('Saving…');
    }
    const data = new FormData(form);
    if (submitter?.name) data.set(submitter.name, submitter.value);
    const check = form.querySelector('[name$="-completed"]');
    if (check) data.set(check.name, check.checked ? 'on' : '');
    const pending = form.dataset.setForm && queue.find((job) => job.form === form);
    if (pending) pending.data = data;
    else queue.push({form, data});
    persistDrafts();
    drain();
  }
  workout.querySelectorAll('[data-gym-save]').forEach((form) => {
    form.addEventListener('submit', (event) => {
      if (event.defaultPrevented) return;
      if (form === finishForm && finishDialog && !finishApproved) {
        event.preventDefault(); openFinish(); return;
      }
      event.preventDefault();
      if (form.reportValidity()) enqueue(form, event.submitter);
    });
    if (form.dataset.setForm) form.addEventListener('change', () => { if (form.reportValidity()) enqueue(form); });
  });
  function openFinish() {
    if (!finishDialog) return;
    const pending = sending || queue.length;
    const summary = finishDialog.querySelector('[data-finish-summary]');
    summary.textContent = pending ? gettext('Your latest sets will save before the workout finishes.') :
      `${state.totals.completed} ${gettext('sets complete')}. ${state.totals.remaining ? gettext('Unfinished sets will be skipped. Your completed sets are kept.') : gettext('Your workout is ready to save.')}`;
    finishDialog.querySelector('[data-finish-error]').textContent = '';
    finishDialog.querySelector('[data-confirm-finish]').hidden = !pending && !state.totals.completed;
    if (!pending && !state.totals.completed) summary.textContent = gettext('Mark at least one set complete using its check button, then finish your workout.');
    if (!finishDialog.open) finishDialog.showModal();
  }
  workout.querySelectorAll('[data-open-finish]').forEach((button) => button.addEventListener('click', openFinish));
  finishDialog?.querySelectorAll('[data-finish-close]').forEach((button) => button.addEventListener('click', () => finishDialog.close()));
  finishDialog?.querySelector('[data-confirm-finish]').addEventListener('click', () => {
    if (workout.querySelector('.is-recovered')) {
      finishDialog.querySelector('[data-finish-error]').textContent = gettext('Save or review your recovered set edits before finishing.'); return;
    }
    if (invalid.size) {
      const failed = [...invalid.values()].find((job) => job.form.dataset.setForm);
      if (failed) {
        finishDialog.close(); failed.form.scrollIntoView({block:'center', behavior:'smooth'});
        failed.form.querySelector('[aria-invalid], input:not([type=hidden])')?.focus(); return;
      }
    }
    finishForm.elements.confirm_unfinished.checked = true;
    finishApproved = true;
    finishForm.requestSubmit(finishForm.querySelector('button[value=finish]'));
    finishApproved = false;
  });
  retry.addEventListener('click' , () => { if (conflicted) { persistDrafts(); navigationAllowed = true; location.reload(); } else drain(); });
  addEventListener('online', () => { if (!conflicted) drain(); });
  addEventListener('beforeunload', (event) => {
    if (!navigationAllowed && (sending || queue.length || invalid.size || workout.querySelector('.is-recovered'))) { event.preventDefault(); event.returnValue = ''; }
  });
  workout.querySelectorAll('[data-weight-open]').forEach((button) => button.addEventListener('click', () => dialog.showModal()));
  dialog.querySelector('[data-dialog-close]').addEventListener('click', () => dialog.close());
  if (dialog.dataset.open && typeof dialog.showModal === 'function') dialog.showModal();

  // Interrupted row edits are restored only after the user chooses to review them.
  try {
    const drafts = unreviewedDrafts;
    if (Object.keys(drafts).length) {
      const restore = document.createElement('button');
      restore.type = 'button'; restore.className = 'button subtle';
      restore.textContent = gettext('Review unsaved set edits');
      status.after(restore);
      restore.addEventListener('click', () => {
        Object.entries(drafts).forEach(([id, values]) => {
          const form = [...workout.querySelectorAll('[data-set-form]')].find((candidate) => candidate.dataset.setForm === id);
          if (!form) return;
          Object.entries(values).forEach(([name, value]) => {
            const input = form.elements.namedItem(name);
            if (!input) return;
            if (input.type === 'checkbox') input.checked = value === 'on';
            else input.value = value;
          });
          form.classList.add('is-recovered');
        });
        unreviewedDrafts = {};
        persistDrafts();
        restore.remove();
        say(gettext('Recovered edits are ready to review. Save each set you want to keep.'));
      });
    }
  } catch { /* A blocked storage API does not prevent server-backed logging. */ }

  workout.querySelector('[data-open-rest]')?.addEventListener('click', () => {
    manualRestOpen = true;
    renderClocks();
    workout.querySelector('[data-rest-panel]')?.scrollIntoView({block:'nearest'});
  });
  const originalTitle = document.title;
  const restTimer = new window.CotingaRestTimer();
  function renderClocks() {
    const elapsed = state.elapsed + (state.active && !state.paused ? Math.max(0, Date.now() - clockAnchor) / 1000 : 0);
    workout.querySelector('[data-session-clock]').textContent = clock(elapsed);
    const timer = workout.querySelector('[data-rest-clock]');
    if (!timer) return;
    const {remaining, paused, complete} = restTimer.snapshot();
    if (remaining || complete) manualRestOpen = true;
    const restPanel = workout.querySelector('[data-rest-panel]');
    if (restPanel) { restPanel.hidden = !manualRestOpen; restPanel.classList.toggle('is-ready', complete); }
    timer.textContent = clock(remaining);
    workout.querySelector('[data-timer-pause]').hidden = paused || !remaining;
    workout.querySelector('[data-timer-resume]').hidden = !paused;
    const message = complete ? gettext('Rest complete. Ready for your next set.') : '';
    const announcement = workout.querySelector('[data-timer-announcement]');
    if (announcement.textContent !== message) announcement.textContent = message;
    document.title = complete ? `${gettext('Rest complete')} · Cotinga` : remaining && !paused ? `${clock(remaining)} · ${gettext('Rest timer')} · Cotinga` : originalTitle;
  }
  // Reconcile immediately after tab switches and back/forward-cache restores.
  document.addEventListener('visibilitychange', renderClocks);
  window.addEventListener('focus', renderClocks);
  window.addEventListener('pageshow', renderClocks);
  window.addEventListener('pagehide', () => { document.title = originalTitle; });
  restTimer.update(state);
  if ('wakeLock' in navigator) {
    const toggle = workout.querySelector('[data-keep-awake]');
    let lock;
    if (toggle) {
      workout.querySelector('[data-wake-label]').hidden = false;
      const acquire = async () => {
        if (!toggle.checked || document.visibilityState !== 'visible') return;
        try { lock = await navigator.wakeLock.request('screen'); } catch { toggle.checked = false; }
      };
      toggle.addEventListener('change', () => { if (toggle.checked) acquire(); else lock?.release(); });
      document.addEventListener('visibilitychange', acquire);
    }
  }
  renderClocks();
  setInterval(renderClocks, 1000);
})();
