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

  const workout = root.querySelector('[data-workout]');
  if (!workout) return;
  let state = JSON.parse(document.getElementById('gym-workout-state').textContent);
  const status = workout.querySelector('[data-save-status]');
  const retry = workout.querySelector('[data-save-retry]');
  const dialog = workout.querySelector('[data-bodyweight-dialog]');
  let clockAnchor = Date.now();
  const queue = [];
  const invalid = new Map();
  let sending = false;
  let conflicted = false;
  let navigationAllowed = false;
  let reloadAfterSave = false;
  let redirectAfterSave = '';
  let inFlight = null;
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
        job.form.classList.remove('is-recovered');
        job.form.classList.toggle('is-pending', queue.some((pending) => pending.form === job.form));
        const error = job.form.querySelector('[data-set-error]');
        if (error) error.textContent = '';
        if (job.form.dataset.setForm) {
          const name = `set-${job.form.dataset.setForm}-completed`;
          job.form.closest('[data-set-wrapper]').classList.toggle('is-complete', job.data.get(name) === 'on');
        }
        applyState(result.state);
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
      if (invalid.size) say(gettext('Some changes need attention. Check the message beside your set.'), true);
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
      event.preventDefault();
      if (form.reportValidity()) enqueue(form, event.submitter);
    });
    if (form.dataset.setForm) form.addEventListener('change', () => { if (form.reportValidity()) enqueue(form); });
  });
  retry.addEventListener('click', () => { if (conflicted) { persistDrafts(); navigationAllowed = true; location.reload(); } else drain(); });
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

  let hadRunningRest = Boolean(state.rest_ends_at && Date.parse(state.rest_ends_at) > Date.parse(state.server_time));
  const soundToggle = workout.querySelector('[data-timer-sound]');
  let audio;
  soundToggle?.addEventListener('change', () => {
    if (soundToggle.checked && window.AudioContext) { audio ||= new AudioContext(); audio.resume().catch(() => {}); }
  });
  function chime() {
    if (!soundToggle?.checked || !audio) return;
    [0, .22].forEach((delay) => {
      const oscillator = audio.createOscillator();
      const gain = audio.createGain();
      oscillator.type = 'sine'; oscillator.frequency.value = 660;
      gain.gain.setValueAtTime(0, audio.currentTime + delay);
      gain.gain.linearRampToValueAtTime(.12, audio.currentTime + delay + .02);
      gain.gain.exponentialRampToValueAtTime(.001, audio.currentTime + delay + .17);
      oscillator.connect(gain); gain.connect(audio.destination);
      oscillator.start(audio.currentTime + delay); oscillator.stop(audio.currentTime + delay + .18);
    });
  }
  function renderClocks() {
    const elapsed = state.elapsed + (state.active && !state.paused ? Math.max(0, Date.now() - clockAnchor) / 1000 : 0);
    workout.querySelector('[data-session-clock]').textContent = clock(elapsed);
    const timer = workout.querySelector('[data-rest-clock]');
    if (!timer) return;
    const remaining = state.rest_ends_at ? Math.max(0, Math.ceil((Date.parse(state.rest_ends_at) - Date.parse(state.server_time) - (Date.now() - clockAnchor)) / 1000)) : (state.rest_remaining || 0);
    timer.textContent = clock(remaining);
    const paused = !state.rest_ends_at && Boolean(state.rest_remaining);
    workout.querySelector('[data-timer-pause]').hidden = paused || !remaining;
    workout.querySelector('[data-timer-resume]').hidden = !paused;
    if (state.rest_ends_at && remaining) hadRunningRest = true;
    if (hadRunningRest && state.rest_ends_at && !remaining) {
      hadRunningRest = false;
      workout.querySelector('[data-timer-announcement]').textContent = gettext('Rest complete. Ready for your next set.');
      chime();
    }
    if (!state.rest_ends_at) hadRunningRest = false;
  }
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
