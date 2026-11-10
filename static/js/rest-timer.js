// A saved deadline keeps rest accurate even when background callbacks are delayed.
((global) => {
  class RestTimer {
    constructor({now = Date.now} = {}) {
      this.now = now;
      this.deadline = null;
      this.pausedRemaining = 0;
    }

    update(state) {
      const server = Date.parse(state.server_time);
      const end = Date.parse(state.rest_ends_at);
      this.deadline = state.active && Number.isFinite(end) && Number.isFinite(server)
        ? this.now() + end - server : null;
      this.pausedRemaining = state.active ? (state.rest_remaining || 0) : 0;
    }

    snapshot() {
      return {
        remaining: this.deadline === null ? this.pausedRemaining : Math.max(0, Math.ceil((this.deadline - this.now()) / 1000)),
        paused: this.deadline === null && this.pausedRemaining > 0,
        complete: this.deadline !== null && this.deadline <= this.now(),
      };
    }
  }
  if (typeof module !== 'undefined' && module.exports) module.exports = RestTimer;
  else global.CotingaRestTimer = RestTimer;
})(globalThis);
