// The wall clock owns the countdown; Web Audio schedules the alarm independently
// of background-tab JavaScript callbacks. No silent audio or keep-alive loops.
((global) => {
  class RestTimer {
    constructor({now = Date.now, createAudio, onSoundChange = () => {}} = {}) {
      this.now = now;
      const Audio = global.AudioContext || global.webkitAudioContext;
      this.supported = Boolean(createAudio || Audio);
      this.createAudio = createAudio || (() => new Audio());
      this.onSoundChange = onSoundChange;
      this.enabled = false;
      this.pageActive = true;
      this.audio = null;
      this.scheduled = null;
      this.preview = [];
      this.key = null;
      this.deadline = null;
      this.pausedRemaining = 0;
      this.armed = false;
      this.sounded = false;
    }

    get soundReady() { return this.enabled && this.audio?.state === 'running'; }

    update(state) {
      const key = state.active ? state.rest_ends_at : null;
      const server = Date.parse(state.server_time);
      const end = Date.parse(key);
      const deadline = key && Number.isFinite(end) && Number.isFinite(server) ? this.now() + end - server : null;
      if (key !== this.key) {
        this.cancelAlarm();
        this.key = key;
        this.sounded = false;
        // Loading an already-expired timer must not replay an old alarm.
        this.armed = deadline !== null && deadline > this.now();
      }
      this.deadline = deadline;
      this.pausedRemaining = state.active ? (state.rest_remaining || 0) : 0;
      this.syncAlarm();
    }

    snapshot() {
      this.syncAlarm();
      return {
        remaining: this.deadline === null ? this.pausedRemaining : Math.max(0, Math.ceil((this.deadline - this.now()) / 1000)),
        paused: this.deadline === null && this.pausedRemaining > 0,
        complete: this.deadline !== null && this.deadline <= this.now(),
      };
    }

    stopNodes(nodes) {
      nodes.forEach((node) => { try { node.stop(); } catch { /* Already ended. */ } });
    }

    cancelAlarm() {
      if (this.scheduled) {
        if (this.audio.currentTime >= this.scheduled.at) this.sounded = true;
        this.stopNodes(this.scheduled.nodes);
      }
      this.scheduled = null;
    }

    tones(at) {
      const audio = this.audio;
      return [660, 880].map((frequency, index) => {
        const start = at + index * .24;
        const oscillator = audio.createOscillator();
        const gain = audio.createGain();
        oscillator.type = 'sine'; oscillator.frequency.value = frequency;
        gain.gain.setValueAtTime(0, start);
        gain.gain.linearRampToValueAtTime(.16, start + .02);
        gain.gain.exponentialRampToValueAtTime(.001, start + .2);
        oscillator.connect(gain); gain.connect(audio.destination);
        oscillator.onended = () => { oscillator.disconnect(); gain.disconnect(); };
        oscillator.start(start); oscillator.stop(start + .21);
        return oscillator;
      });
    }

    syncAlarm() {
      if (this.scheduled && this.audio.currentTime >= this.scheduled.at) this.sounded = true;
      if (!this.enabled || !this.pageActive || this.deadline === null || !this.armed || !this.soundReady) {
        this.cancelAlarm();
        return;
      }
      if (this.sounded || this.scheduled) return;
      // A suspended device may resume late. Announce a recent rest, never beep
      // for a stale workout minutes or hours after the user has moved on.
      const seconds = (this.deadline - this.now()) / 1000;
      if (seconds < -60) return;
      const at = this.audio.currentTime + Math.max(.02, seconds);
      try { this.scheduled = {at, nodes: this.tones(at)}; }
      catch { this.cancelAlarm(); }
    }

    setEnabled(enabled) {
      this.enabled = enabled && this.supported;
      if (!this.enabled) {
        this.cancelAlarm();
        this.stopNodes(this.preview);
        this.preview = [];
      }
      this.syncAlarm();
      this.onSoundChange();
    }

    async activateSound() {
      if (!this.enabled || !this.supported) return false;
      try {
        if (!this.audio || this.audio.state === 'closed') {
          this.audio = this.createAudio();
          this.audio.addEventListener('statechange', () => { this.syncAlarm(); this.onSoundChange(); });
        }
        if (this.audio.state !== 'running') await this.audio.resume();
        this.syncAlarm();
      } catch { /* Keep the timer usable; another user gesture can retry audio. */ }
      this.onSoundChange();
      return this.soundReady;
    }

    async testSound() {
      if (await this.activateSound() && this.pageActive) {
        // An imminent or just-fired alarm already previews the same chime.
        if (this.scheduled && this.scheduled.at - this.audio.currentTime < .5) return;
        this.stopNodes(this.preview);
        try { this.preview = this.tones(this.audio.currentTime + .02); }
        catch { /* Audio can become unavailable independently of the timer. */ }
      }
    }

    setPageActive(active) {
      this.pageActive = active;
      if (!active) { this.stopNodes(this.preview); this.preview = []; }
      this.syncAlarm();
    }
  }
  if (typeof module !== 'undefined' && module.exports) module.exports = RestTimer;
  else global.CotingaRestTimer = RestTimer;
})(globalThis);
