/**
 * CodexRecovery - isolated module for the "设置 Codex 预计恢复时间" form.
 *
 * All timestamps are interpreted and displayed as Beijing time
 * (Asia/Shanghai, UTC+08:00) regardless of the browser's local timezone.
 *
 * Concurrency model
 * -----------------
 * - ``modeGeneration`` is bumped on every mode switch. Any response whose
 *   generation differs from the current one is discarded, so a response from a
 *   mode the operator has already left can never leak into the new mode.
 * - ``readSeq`` / ``writeSeq`` are independent monotonic counters. A read can
 *   never invalidate an in-flight write and vice versa, so a background refresh
 *   cannot leave a save permanently busy (race D) and a stale read cannot
 *   resurrect an old value after a successful save.
 * - ``loadedMode`` records which mode the currently displayed data belongs to.
 *   The form is only submittable when ``loadedMode === currentMode()`` (race C).
 * - ``writeBarrier`` records the ``readSeq`` observed when a write started. Any
 *   read that was issued before the write is discarded when it resolves.
 */

const CodexRecovery = {
  BEIJING_OFFSET_MINUTES: 8 * 60,

  state: {
    savedIso: null,
    loadedMode: null,
    activeMode: null,
    // Monotonic generation counter bumped on every mode switch. Any response
    // whose generation differs from the current one is discarded.
    modeGeneration: 0,
    // Independent request identifiers for reads and writes so a background
    // refresh can never invalidate an in-flight save (and vice versa).
    readSeq: 0,
    writeSeq: 0,
    // Reads issued at or before this sequence number predate the most recent
    // write and must be discarded when they resolve (write barrier).
    writeBarrier: 0,
    saving: false,
    loading: false,
    dirty: false,
    failed: false,
  },

  toBeijingIso(date) {
    if (!(date instanceof Date) || Number.isNaN(date.getTime())) {
      throw new Error('toBeijingIso requires a valid Date');
    }
    const shifted = new Date(date.getTime() + this.BEIJING_OFFSET_MINUTES * 60 * 1000);
    const pad = (n, width = 2) => String(n).padStart(width, '0');
    const y = shifted.getUTCFullYear();
    const mo = pad(shifted.getUTCMonth() + 1);
    const d = pad(shifted.getUTCDate());
    const h = pad(shifted.getUTCHours());
    const mi = pad(shifted.getUTCMinutes());
    const s = pad(shifted.getUTCSeconds());
    return `${y}-${mo}-${d}T${h}:${mi}:${s}+08:00`;
  },

  localInputToBeijingIso(value) {
    if (typeof value !== 'string' || value.trim() === '') {
      throw new Error('请输入预计恢复时间');
    }
    const match = value.trim().match(
      /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?$/
    );
    if (!match) {
      throw new Error('日期时间格式无效，请使用日期时间选择器');
    }
    const [, y, mo, d, h, mi, s] = match;
    const year = Number(y);
    const month = Number(mo);
    const day = Number(d);
    const hour = Number(h);
    const minute = Number(mi);
    const second = s ? Number(s) : 0;
    if (month < 1 || month > 12 || day < 1 || day > 31 || hour > 23 || minute > 59 || second > 59) {
      throw new Error('日期时间超出有效范围');
    }
    const probe = new Date(Date.UTC(year, month - 1, day, hour, minute, second));
    if (
      probe.getUTCFullYear() !== year ||
      probe.getUTCMonth() !== month - 1 ||
      probe.getUTCDate() !== day ||
      probe.getUTCHours() !== hour ||
      probe.getUTCMinutes() !== minute ||
      probe.getUTCSeconds() !== second
    ) {
      throw new Error('日期时间不存在，请检查年月日');
    }
    const pad = (n) => String(n).padStart(2, '0');
    return `${y}-${mo}-${d}T${pad(hour)}:${pad(minute)}:${pad(second)}+08:00`;
  },

  isoToLocalInput(iso) {
    if (!iso) return '';
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return '';
    const shifted = new Date(date.getTime() + this.BEIJING_OFFSET_MINUTES * 60 * 1000);
    const pad = (n) => String(n).padStart(2, '0');
    return (
      `${shifted.getUTCFullYear()}-${pad(shifted.getUTCMonth() + 1)}-${pad(shifted.getUTCDate())}` +
      `T${pad(shifted.getUTCHours())}:${pad(shifted.getUTCMinutes())}`
    );
  },

  formatBeijingDisplay(iso) {
    if (!iso) return '未设置';
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return '未设置';
    const shifted = new Date(date.getTime() + this.BEIJING_OFFSET_MINUTES * 60 * 1000);
    const pad = (n) => String(n).padStart(2, '0');
    return (
      `${shifted.getUTCFullYear()}-${pad(shifted.getUTCMonth() + 1)}-${pad(shifted.getUTCDate())} ` +
      `${pad(shifted.getUTCHours())}:${pad(shifted.getUTCMinutes())} (北京时间 UTC+08:00)`
    );
  },

  isResponseCurrent(responseMode, responseSeq, currentMode, currentSeq) {
    return responseMode === currentMode && responseSeq === currentSeq;
  },

  shouldApplyToInput(isDirty, isFocused) {
    return !isDirty && !isFocused;
  },

  /**
   * The mode the UI currently belongs to. Prefer the live App state so a
   * response is judged against the *current* selection, not the mode that was
   * active when the request was issued.
   */
  currentMode() {
    if (typeof window !== 'undefined' && window.App && window.App.state) {
      return !!window.App.state.isMock;
    }
    if (this.state.activeMode !== undefined && this.state.activeMode !== null) {
      return !!this.state.activeMode;
    }
    return !!this.state.loadedMode;
  },

  /**
   * The form is submittable only when the data currently displayed belongs to
   * the mode the operator is looking at, and no load is in flight or failed.
   */
  isReady() {
    return (
      this.state.loadedMode !== null &&
      this.state.loadedMode === this.currentMode() &&
      !this.state.loading &&
      !this.state.failed
    );
  },

  els: {},

  init() {
    this.els = {
      form: document.getElementById('codexRecoveryForm'),
      input: document.getElementById('codexRecoveryInput'),
      saveBtn: document.getElementById('codexRecoverySaveBtn'),
      clearBtn: document.getElementById('codexRecoveryClearBtn'),
      current: document.getElementById('codexRecoveryCurrent'),
      feedback: document.getElementById('codexRecoveryFeedback'),
      hint: document.getElementById('codexRecoveryHint'),
    };
    if (!this.els.form) return;

    this.els.input.addEventListener('input', () => {
      this.state.dirty = true;
    });
    this.els.form.addEventListener('submit', (event) => {
      event.preventDefault();
      this.save();
    });
    this.els.clearBtn.addEventListener('click', (event) => {
      event.preventDefault();
      this.clear();
    });

    // The template ships the controls enabled; lock them until the first load
    // for the current mode completes (race C).
    this.state.loadedMode = null;
    this.state.activeMode = null;
    this.state.failed = false;
    this.applyDisabledState();
  },

  setFeedback(message, type) {
    const el = this.els.feedback;
    if (!el) return;
    el.textContent = message || '';
    el.classList.remove('hidden');
    if (type === 'success') {
      el.className = 'mt-2 text-xs rounded-lg px-3 py-2 border border-emerald-500/40 bg-emerald-500/10 text-emerald-300';
      el.setAttribute('role', 'status');
      el.setAttribute('aria-live', 'polite');
    } else if (type === 'error') {
      el.className = 'mt-2 text-xs rounded-lg px-3 py-2 border border-rose-500/40 bg-rose-500/10 text-rose-300';
      el.setAttribute('role', 'alert');
      el.setAttribute('aria-live', 'assertive');
    } else {
      el.className = 'mt-2 text-xs rounded-lg px-3 py-2 border border-slate-700 bg-slate-900 text-slate-300';
      el.setAttribute('role', 'status');
      el.setAttribute('aria-live', 'polite');
    }
  },

  clearFeedback() {
    const el = this.els.feedback;
    if (!el) return;
    el.textContent = '';
    el.classList.add('hidden');
  },

  /**
   * Recompute the disabled state of every control from the current readiness.
   * A save in flight always disables the form; otherwise the ready gate decides.
   */
  applyDisabledState() {
    const busy = this.state.saving;
    const ready = this.isReady();
    const disabled = busy || !ready;
    if (this.els.saveBtn) this.els.saveBtn.disabled = disabled;
    if (this.els.clearBtn) this.els.clearBtn.disabled = disabled;
    if (this.els.input) this.els.input.disabled = disabled;
    if (this.els.form) this.els.form.setAttribute('aria-busy', busy ? 'true' : 'false');
  },

  setBusy(busy) {
    this.state.saving = busy;
    this.applyDisabledState();
  },

  renderCurrent(iso) {
    this.state.savedIso = iso || null;
    if (this.els.current) {
      this.els.current.textContent = this.formatBeijingDisplay(iso);
    }
  },

  /**
   * Refresh the form from the server for the given mode.
   *
   * Background refreshes never touch the busy state or the feedback area, and
   * never overwrite a field the user is editing. A failed background refresh
   * keeps the draft and surfaces a recoverable error (race A).
   */
  async refresh(mock, isBackground = false) {
    if (!this.els.form) return;
    const mode = !!mock;
    this.state.activeMode = mode;
    const generation = this.state.modeGeneration;
    const seq = ++this.state.readSeq;
    if (!isBackground) {
      this.state.loading = true;
      this.state.failed = false;
      this.applyDisabledState();
    }
    try {
      const res = await fetch(`/api/codex/quota?mock=${mode}`);
      if (!res.ok) {
        throw new Error(`加载失败 (HTTP ${res.status})`);
      }
      const data = await res.json();
      if (generation !== this.state.modeGeneration) return;
      if (seq !== this.state.readSeq) return;
      if (seq <= this.state.writeBarrier) return;
      if (mode !== this.currentMode()) return;
      const quota = data.quota || {};
      this.state.loadedMode = mode;
      this.state.failed = false;
      this.renderCurrent(quota.estimated_recovery_at);
      const input = this.els.input;
      if (input && this.shouldApplyToInput(this.state.dirty, document.activeElement === input)) {
        input.value = this.isoToLocalInput(quota.estimated_recovery_at);
      }
      if (!isBackground) {
        this.clearFeedback();
      }
    } catch (err) {
      if (generation !== this.state.modeGeneration) return;
      if (seq !== this.state.readSeq) return;
      if (seq <= this.state.writeBarrier) return;
      if (mode !== this.currentMode()) return;
      if (isBackground) {
        // Race A: a failed background refresh must not wipe the operator's
        // draft. Keep the input and the last known saved value, and surface a
        // recoverable error so the failure is not silent.
        this.setFeedback(`自动刷新失败，草稿已保留：${err.message}`, 'error');
        return;
      }
      // A failed foreground load must not leave a stale, submittable form
      // behind (race C).
      this.state.loadedMode = null;
      this.state.failed = true;
      this.state.savedIso = null;
      if (this.els.current) this.els.current.textContent = '未设置';
      if (this.els.input) this.els.input.value = '';
      this.setFeedback(`加载预计恢复时间失败：${err.message}`, 'error');
    } finally {
      if (!isBackground) {
        // Only the newest foreground read may release the loading gate; a
        // stale read from a previous mode must not unlock the new mode (race B).
        if (generation === this.state.modeGeneration && seq === this.state.readSeq) {
          this.state.loading = false;
          this.applyDisabledState();
        }
      }
    }
  },

  async save() {
    if (this.state.saving) return;
    if (!this.isReady()) {
      this.setFeedback('表单尚未加载完成，请稍候再试', 'error');
      return;
    }
    let iso;
    try {
      iso = this.localInputToBeijingIso(this.els.input ? this.els.input.value : '');
    } catch (err) {
      this.setFeedback(err.message, 'error');
      return;
    }
    await this.submit(iso, '预计恢复时间已保存');
  },

  async clear() {
    if (this.state.saving) return;
    if (!this.isReady()) {
      // Never clear before the current mode has loaded (race C).
      this.setFeedback('表单尚未加载完成，无法清除', 'error');
      return;
    }
    await this.submit(null, '预计恢复时间已清除');
  },

  async submit(payload, successMessage) {
    const mode = this.currentMode();
    const generation = this.state.modeGeneration;
    const seq = ++this.state.writeSeq;
    // Write barrier: any read issued before this point is stale once the write
    // succeeds and must be discarded when it resolves (race D).
    this.state.writeBarrier = this.state.readSeq;
    this.setBusy(true);
    this.clearFeedback();
    try {
      const res = await fetch('/api/codex/recovery', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          mock: mode,
          estimated_recovery_at: payload,
        }),
      });
      let data = null;
      try {
        data = await res.json();
      } catch (parseErr) {
        data = null;
      }
      if (generation !== this.state.modeGeneration) return;
      if (seq !== this.state.writeSeq) return;
      if (mode !== this.currentMode()) return;
      if (!res.ok) {
        const message = data && data.error ? data.error : `保存失败 (HTTP ${res.status})`;
        this.setFeedback(message, 'error');
        return;
      }
      this.state.writeBarrier = Math.max(this.state.writeBarrier, this.state.readSeq);
      this.state.dirty = false;
      this.state.loadedMode = mode;
      this.state.failed = false;
      this.renderCurrent(data.estimated_recovery_at);
      if (this.els.input) {
        this.els.input.value = this.isoToLocalInput(data.estimated_recovery_at);
      }
      this.setFeedback(successMessage, 'success');
    } catch (err) {
      if (generation === this.state.modeGeneration && seq === this.state.writeSeq) {
        this.setFeedback(`请求异常：${err.message}`, 'error');
      }
    } finally {
      // Always release the busy state for this write, even if a background
      // refresh bumped readSeq in the meantime.
      if (seq === this.state.writeSeq) {
        this.setBusy(false);
      }
    }
  },

  /**
   * Reset transient state when the operator flips the mock/live switch so a
   * response from the previous mode cannot leak into the new one. The form is
   * disabled until the new mode finishes loading.
   */
  onModeChange(targetMode) {
    this.state.modeGeneration += 1;
    this.state.readSeq += 1;
    this.state.writeSeq += 1;
    this.state.writeBarrier = this.state.readSeq;
    this.state.dirty = false;
    this.state.savedIso = null;
    this.state.loadedMode = null;
    this.state.activeMode = targetMode !== undefined ? !!targetMode : null;
    this.state.failed = false;
    this.state.loading = true;
    this.clearFeedback();
    if (this.els.current) this.els.current.textContent = '加载中...';
    if (this.els.input) this.els.input.value = '';
    this.applyDisabledState();
  },
};

if (typeof window !== 'undefined') {
  window.CodexRecovery = CodexRecovery;
}
if (typeof module !== 'undefined' && module.exports) {
  module.exports = CodexRecovery;
}
