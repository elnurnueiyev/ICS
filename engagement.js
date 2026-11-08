// Engagement & AFK hook — vanilla-JS equivalent of a React/Vue useEngagement() hook.
//
// Tracks mousemove / scroll / keydown / blur-focus, pauses the *client* timer
// after idleThresholdMs without a valid event, and heartbeats trusted counters
// to the backend. The backend recomputes active time authoritatively, so
// faking these counters only ever *reduces* credited time (see server.py).
//
// Contract after start():
//   state(): { activeS, wallS, idleS, paused, lastEventAgoMs, mouseMoves, scrolls, keys, focusEvents }
// The client timer is a UX guide only; server values in heartbeat responses win.

export class EngagementTracker {
  constructor({ onTick, onPauseChange } = {}) {
    this.onTick = onTick || (() => {});
    this.onPauseChange = onPauseChange || (() => {});
    this.reset();
  }

  reset() {
    this.startWall = Date.now();
    this.activeAccumMs = 0;          // accumulated while unpaused
    this.lastTickWall = Date.now();
    this.lastEventAt = Date.now();
    this.mouseMoves = 0;
    this.scrolls = 0;
    this.keys = 0;
    this.focusEvents = 0;
    this.paused = false;
    this.idleThresholdMs = 30000;
    this.hidden = false;
    this._pausedBeforeHidden = false;
  }

  start({ heartbeatEveryMs = 5000, idleThresholdMs = 30000, heartbeat } = {}) {
    if (this._timer) this.stop();
    this.reset();
    this.idleThresholdMs = idleThresholdMs;
    this._heartbeatEveryMs = heartbeatEveryMs;
    this._heartbeat = heartbeat || (async () => ({ server_active_s: this.activeS(), server_wall_s: this.wallS() }));
    this._hbd = { activeS: 0, wallS: 0, timer: null };

    this._onMouse = () => { this.mouseMoves++; this._bump(); };
    this._onScroll = () => { this.scrolls++; this._bump(); };
    this._onKey = () => { this.keys++; this._bump(); };
    this._onVisible = () => {
      this.focusEvents++;
      if (document.hidden) {
        this._pausedBeforeHidden = this.paused;
        this.hidden = true;
        this._accumulate();          // freeze accumulation at hide time
      } else {
        this.hidden = false;
        this.lastTickWall = Date.now();
        this._bump();
      }
      this._updatePaused();
    };
    this._onBlur = () => { this.focusEvents++; this.hidden = true; this._accumulate(); this._updatePaused(); };
    this._onFocus = () => { this.focusEvents++; this.hidden = false; this.lastTickWall = Date.now(); this._bump(); this._updatePaused(); };

    window.addEventListener("mousemove", this._onMouse, { passive: true });
    window.addEventListener("scroll", this._onScroll, { passive: true });
    window.addEventListener("keydown", this._onKey);
    document.addEventListener("visibilitychange", this._onVisible);
    window.addEventListener("blur", this._onBlur);
    window.addEventListener("focus", this._onFocus);

    this._timer = setInterval(() => this._tick(), 1000);
    this._hbTimer = setInterval(() => this._sendHeartbeat(), heartbeatEveryMs);
    this.serverCounters = null;
    this.lastError = null;
  }

  stop() {
    clearInterval(this._timer);
    clearInterval(this._hbTimer);
    window.removeEventListener("mousemove", this._onMouse);
    window.removeEventListener("scroll", this._onScroll);
    window.removeEventListener("keydown", this._onKey);
    document.removeEventListener("visibilitychange", this._onVisible);
    window.removeEventListener("blur", this._onBlur);
    window.removeEventListener("focus", this._onFocus);
    this._timer = this._hbTimer = null;
  }

  _bump() {
    // Genuine interaction wakes the client timer.
    if (!this.hidden) {
      this.lastEventAt = Date.now();
      this._accumulate();
      this._updatePaused();
    }
  }

  _accumulate() {
    const now = Date.now();
    if (!this.hidden && !this.paused) {
      this.activeAccumMs += now - this.lastTickWall;
    }
    this.lastTickWall = now;
  }

  _updatePaused() {
    const idleMs = Date.now() - this.lastEventAt;
    const shouldPause = this.hidden || idleMs > this.idleThresholdMs;
    if (shouldPause !== this.paused) {
      this._accumulate();
      this.paused = shouldPause;
      this.onPauseChange(this.paused);
    }
  }

  _tick() {
    const idleMs = Date.now() - this.lastEventAt;
    if (!this.hidden && !this.paused && idleMs > this.idleThresholdMs) this._updatePaused();
    this._accumulate();
    this.onTick(this.state());
  }

  async _sendHeartbeat() {
    if (!this._heartbeat) return;
    const snap = this.state();
    try {
      const res = await this._heartbeat({
        active_seconds: snap.activeS,
        wall_seconds: snap.wallS,
        mouse_moves: this.mouseMoves,
        scrolls: this.scrolls,
        keys: this.keys,
        focus_events: this.focusEvents,
      });
      this.serverCounters = res;
      this.lastError = null;
      this.onTick(this.state());  // re-render with trusted counters
    } catch (e) {
      this.lastError = e;
    }
  }

  activeS() {
    this._accumulate();
    return Math.floor(this.activeAccumMs / 1000);
  }

  wallS() {
    return Math.floor((Date.now() - this.startWall) / 1000);
  }

  state() {
    const now = Date.now();
    return {
      activeS: this.activeS(),
      wallS: this.wallS(),
      idleS: Math.floor((now - this.lastEventAt) / 1000),
      paused: this.paused,
      hidden: this.hidden,
      serverActiveS: this.serverCounters ? this.serverCounters.server_active_s : null,
      serverWallS: this.serverCounters ? this.serverCounters.server_wall_s : null,
      remainingS: this.serverCounters ? this.serverCounters.remaining_s : null,
      timedOut: this.serverCounters ? !!this.serverCounters.timed_out : false,
      verdict: this.serverCounters ? this.serverCounters.verdict : null,
      mouseMoves: this.mouseMoves, scrolls: this.scrolls, keys: this.keys, focusEvents: this.focusEvents,
    };
  }
}
