/**
 * AgentCooperation Web Console - Live Run Console
 *
 * Streams closed-loop run events over SSE (with a polling fallback), renders
 * per-agent output, the step timeline, file diffs and cost/token metrics, and
 * replays persisted runs from the JSONL event store.
 */

const RunConsole = {
  state: {
    initialized: false,
    optionsLoaded: false,
    currentTaskId: null,
    source: null,
    pollTimer: null,
    durationTimer: null,
    startedAt: 0,
    lastSeq: 0,
    seenSeqs: new Set(),
    terminal: false,
    paused: false,
    replay: false,
    historicalState: null,
    mock: true,
    approvalPending: false,
    approvalTimer: null,
    evidence: {},
    usage: { prompt: 0, completion: 0, cost: 0 },
    openLines: {},
    submitting: false,
  },

  EVENT_TYPES: [
    'status', 'output', 'tool_call', 'tool_result', 'test_line',
    'diff', 'usage', 'approval_required', 'result',
  ],

  AGENT_STYLES: {
    gemini: 'run-agent-gemini',
    glm: 'run-agent-glm',
    deepseek: 'run-agent-deepseek',
    pytest: 'run-agent-pytest',
    human: 'run-agent-human',
    system: 'run-agent-system',
  },

  PHASE_LABELS: {
    develop: '研发',
    test: '测试',
    review: '独立复核',
    repair: '自修',
    'repair-test': '修复后测试',
    approval: '人工审批',
    state: '状态',
    usage: '用量',
  },

  STAGE_LABELS: {
    start: '开始',
    complete: '完成',
    failed: '失败',
    skipped: '跳过',
    approved: '已批准',
    rejected: '已拒绝',
  },

  MAX_OUTPUT_LINES: 1500,
  MAX_TIMELINE_ITEMS: 200,
  MAX_DIFF_LINES: 1200,

  init() {
    if (this.state.initialized) return;
    this.state.initialized = true;

    const requirement = document.getElementById('runRequirement');
    if (requirement) {
      requirement.addEventListener('keydown', (event) => {
        if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) {
          event.preventDefault();
          this.submit();
        }
      });
    }

    this.loadOptions();
  },

  toggleAdvanced() {
    const panel = document.getElementById('runAdvancedPanel');
    const label = document.getElementById('runAdvancedToggleText');
    if (!panel) return;
    const hidden = panel.classList.toggle('hidden');
    if (label) label.textContent = hidden ? '高级选项' : '收起高级选项';
  },

  onModeChange() {
    this.loadHistory();
  },

  async refresh() {
    if (!this.state.optionsLoaded) await this.loadOptions();
    await this.loadHistory();
  },

  async loadOptions() {
    try {
      const res = await fetch('/api/runs/options');
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      this.state.optionsLoaded = true;

      const repoSelect = document.getElementById('runRepoSelect');
      if (repoSelect) {
        repoSelect.innerHTML = '';
        (data.repos || []).forEach((repo) => {
          const option = document.createElement('option');
          option.value = repo.path;
          option.textContent = repo.path === data.default_repo
            ? `${repo.name} (默认)`
            : repo.name;
          option.title = repo.path;
          repoSelect.appendChild(option);
        });
        if (data.default_repo) repoSelect.value = data.default_repo;
      }

      const reviewerSelect = document.getElementById('runReviewerSelect');
      if (reviewerSelect && Array.isArray(data.reviewers)) {
        reviewerSelect.innerHTML = '';
        data.reviewers.forEach((key) => {
          const option = document.createElement('option');
          option.value = key;
          option.textContent = key === 'glm' ? 'GLM' : key === 'deepseek' ? 'DeepSeek' : key;
          reviewerSelect.appendChild(option);
        });
        if (data.default_reviewer) reviewerSelect.value = data.default_reviewer;
      }

      const maxRepair = document.getElementById('runMaxRepair');
      if (maxRepair) {
        if (data.max_repair_limit) maxRepair.max = String(data.max_repair_limit);
        if (data.max_repair_attempts !== undefined && !maxRepair.dataset.userEdited) {
          maxRepair.value = String(data.max_repair_attempts);
        }
        maxRepair.addEventListener('input', () => {
          maxRepair.dataset.userEdited = '1';
        }, { once: true });
      }

      const hint = document.getElementById('runDefaultsHint');
      if (hint) {
        const repoName = this.repoName(data.default_repo) || data.default_repo || '-';
        hint.textContent = `默认仓库 ${repoName} · 复核 ${data.default_reviewer || 'glm'} · 最多 ${data.max_repair_attempts ?? 3} 轮修复`;
      }

      const historyRepo = document.getElementById('runHistoryRepo');
      if (historyRepo) {
        const selected = historyRepo.value;
        historyRepo.innerHTML = '<option value="">全部仓库</option>';
        (data.repos || []).forEach((repo) => {
          const option = document.createElement('option');
          option.value = repo.path;
          option.textContent = repo.name || repo.path;
          option.title = repo.path;
          historyRepo.appendChild(option);
        });
        if (selected) historyRepo.value = selected;
      }
    } catch (err) {
      this.showError(`加载运行默认值失败: ${err.message}`);
    }
  },

  repoName(path) {
    if (!path) return '';
    return String(path).split(/[\\/]/).filter(Boolean).pop() || path;
  },

  async loadHistory() {
    const tbody = document.getElementById('runHistoryBody');
    if (!tbody) return;

    const mock = window.App ? App.state.isMock : false;
    const repoEl = document.getElementById('runHistoryRepo');
    const repo = repoEl ? repoEl.value : '';
    let url = `/api/runs?mock=${mock}&limit=50`;
    if (repo) url += `&repo=${encodeURIComponent(repo)}`;
    try {
      const res = await fetch(url);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      const runs = data.runs || [];

      if (runs.length === 0) {
        tbody.innerHTML = '<tr><td colspan="8" class="p-8 text-center text-slate-500">暂无闭环运行记录</td></tr>';
        return;
      }

      tbody.innerHTML = runs.map((run) => {
        const stateBadge = window.App ? App.getStateBadge(run.state) : run.state;
        const reviewer = run.reviewer_model
          ? String(run.reviewer_model).split('/').pop()
          : '-';
        const cost = Number(run.cost_cny || 0).toFixed(5);
        const created = window.App ? App.formatDate(run.created_at) : (run.created_at || '-');
        const title = window.App ? App.escapeHtml(run.title || '') : (run.title || '');
        return `
          <tr class="border-b border-slate-800 hover:bg-slate-800/40 transition cursor-pointer" data-task-id="${window.App ? App.escapeHtml(run.task_id) : run.task_id}" onclick="RunConsole.openReplay(this.dataset.taskId)">
            <td class="p-3 font-mono text-xs text-sky-300">${window.App ? App.escapeHtml(run.task_id) : run.task_id}</td>
            <td class="p-3 text-slate-200 max-w-xs truncate">${title}</td>
            <td class="p-3 font-mono text-xs text-slate-300">${window.App ? App.escapeHtml(this.repoName(run.repo_root)) : this.repoName(run.repo_root)}</td>
            <td class="p-3">${stateBadge}</td>
            <td class="p-3 font-mono text-xs text-slate-300">${window.App ? App.escapeHtml(reviewer) : reviewer}</td>
            <td class="p-3 font-mono text-xs text-slate-300">${run.repair_attempts || 0}</td>
            <td class="p-3 font-mono text-xs text-amber-400">¥${cost}</td>
            <td class="p-3 font-mono text-xs text-slate-400 whitespace-nowrap">${created}</td>
          </tr>
        `;
      }).join('');
    } catch (err) {
      tbody.innerHTML = `<tr><td colspan="8" class="p-8 text-center text-rose-400">加载运行历史失败: ${window.App ? App.escapeHtml(err.message) : err.message}</td></tr>`;
    }
  },

  async submit() {
    if (this.state.submitting) return;

    const requirementEl = document.getElementById('runRequirement');
    const requirement = (requirementEl ? requirementEl.value : '').trim();
    if (!requirement) {
      this.showError('请先输入研发需求');
      if (requirementEl) requirementEl.focus();
      return;
    }

    const mock = window.App ? App.state.isMock : true;
    if (!mock) {
      const ok = window.confirm('当前为真实调用 (Live) 模式，将调用真实模型并产生费用。确定启动闭环运行吗？');
      if (!ok) return;
    }

    const repoEl = document.getElementById('runRepoSelect');
    const reviewerEl = document.getElementById('runReviewerSelect');
    const maxRepairEl = document.getElementById('runMaxRepair');
    const testPathsEl = document.getElementById('runTestPaths');

    const testPaths = (testPathsEl && testPathsEl.value ? testPathsEl.value : '')
      .split(/[\n,]/)
      .map((item) => item.trim())
      .filter(Boolean);

    const payload = {
      requirement,
      mock,
      repo: repoEl && repoEl.value ? repoEl.value : undefined,
      reviewer: reviewerEl && reviewerEl.value ? reviewerEl.value : undefined,
      max_repair: maxRepairEl && maxRepairEl.value !== ''
        ? Number.parseInt(maxRepairEl.value, 10)
        : undefined,
      test_paths: testPaths.length > 0 ? testPaths : undefined,
    };

    this.hideError();
    this.setSubmitting(true);
    try {
      const res = await fetch('/api/runs', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(data.error || `HTTP ${res.status}`);
      }

      if (window.App) {
        App.showToast(`运行已启动: ${data.task_id}`, 'success');
      }
      this.resetForRun(data.task_id, { mock: data.mock, replay: false });
      this.connect(data.task_id);
      this.loadHistory();
    } catch (err) {
      this.showError(`启动运行失败: ${err.message}`);
    } finally {
      this.setSubmitting(false);
    }
  },

  setSubmitting(working) {
    this.state.submitting = working;
    const button = document.getElementById('runSubmitBtn');
    if (!button) return;
    button.disabled = working;
    const span = button.querySelector('span');
    if (span) span.textContent = working ? '启动中...' : '启动闭环运行';
  },

  showError(message) {
    const box = document.getElementById('runError');
    if (!box) return;
    box.textContent = message;
    box.classList.remove('hidden');
  },

  hideError() {
    const box = document.getElementById('runError');
    if (!box) return;
    box.textContent = '';
    box.classList.add('hidden');
  },

  clearPanes() {
    const output = document.getElementById('runOutputPane');
    const timeline = document.getElementById('runTimeline');
    const diff = document.getElementById('runDiffPane');
    const meta = document.getElementById('runDiffMeta');
    if (output) output.innerHTML = '';
    if (timeline) timeline.innerHTML = '';
    if (diff) diff.innerHTML = '';
    if (meta) meta.textContent = '等待 diff';
    this.state.openLines = {};
    this.state.evidence = {};
    this.renderEvidence();
  },

  resetForRun(taskId, { mock = true, replay = false } = {}) {
    this.closeStream();
    this.stopPolling();
    this.stopDurationTimer();
    this.state.currentTaskId = taskId;
    this.state.lastSeq = 0;
    this.state.seenSeqs = new Set();
    this.state.terminal = false;
    this.state.paused = false;
    this.state.replay = replay;
    this.state.historicalState = null;
    this.state.mock = mock !== false;
    this.state.startedAt = 0;
    this.state.usage = { prompt: 0, completion: 0, cost: 0 };
    this.state.openLines = {};

    this.clearPanes();
    this.hideApproval();
    this.setActivePanelVisible(true);
    this.setConnection('connecting');
    this.updateTaskId(taskId);
    this.updateMode(mock);
    this.updateReplayBadge(replay);
    this.updateState('INIT');
    this.setMetrics({ duration: null, cost: 0, tokens: 0, repairs: 0 });

    const timerEl = document.getElementById('runDuration');
    if (timerEl) timerEl.textContent = '0.0s';
  },

  setActivePanelVisible(visible) {
    const panel = document.getElementById('runActivePanel');
    const grid = document.getElementById('runLiveGrid');
    if (panel) panel.classList.toggle('hidden', !visible);
    if (grid) grid.classList.toggle('hidden', !visible);
  },

  updateTaskId(taskId) {
    const el = document.getElementById('runTaskId');
    if (el) el.textContent = taskId || '-';
  },

  updateMode(mock) {
    const el = document.getElementById('runModeBadge');
    if (!el) return;
    el.textContent = mock ? 'MOCK' : 'LIVE';
    el.className = mock
      ? 'px-2 py-0.5 text-2xs font-mono rounded bg-amber-500/20 text-amber-300 border border-amber-500/30'
      : 'px-2 py-0.5 text-2xs font-mono rounded bg-emerald-500/20 text-emerald-300 border border-emerald-500/30';
  },

  updateReplayBadge(replay) {
    const el = document.getElementById('runReplayBadge');
    if (el) el.classList.toggle('hidden', !replay);
  },

  updateState(state) {
    const el = document.getElementById('runStateBadge');
    if (!el || !state) return;
    el.innerHTML = window.App ? App.getStateBadge(state) : state;
  },

  setConnection(mode) {
    const dot = document.getElementById('runConnectionDot');
    if (!dot) return;
    const classes = {
      connecting: 'pulse-indicator bg-amber-500',
      live: 'pulse-indicator bg-emerald-500',
      reconnecting: 'pulse-indicator bg-amber-500',
      paused: 'pulse-indicator bg-amber-400',
      done: 'pulse-indicator bg-slate-500',
      failed: 'pulse-indicator bg-rose-500',
    };
    dot.className = classes[mode] || classes.connecting;
  },

  setMetrics({ duration, cost, tokens, repairs }) {
    const durationEl = document.getElementById('runDuration');
    const costEl = document.getElementById('runCost');
    const tokensEl = document.getElementById('runTokens');
    const repairsEl = document.getElementById('runRepairs');
    if (duration !== undefined && duration !== null && durationEl) {
      durationEl.textContent = `${Number(duration).toFixed(1)}s`;
    }
    if (cost !== undefined && cost !== null && costEl) {
      costEl.textContent = `¥${Number(cost).toFixed(5)}`;
    }
    if (tokens !== undefined && tokens !== null && tokensEl) {
      tokensEl.textContent = Number(tokens).toLocaleString();
    }
    if (repairs !== undefined && repairs !== null && repairsEl) {
      repairsEl.textContent = String(repairs);
    }
  },

  startDurationTimer() {
    if (this.state.durationTimer) return;
    if (!this.state.startedAt) this.state.startedAt = Date.now();
    this.state.durationTimer = setInterval(() => {
      if (this.state.terminal) return;
      const elapsed = (Date.now() - this.state.startedAt) / 1000;
      this.setMetrics({ duration: elapsed });
    }, 500);
  },

  stopDurationTimer() {
    if (this.state.durationTimer) {
      clearInterval(this.state.durationTimer);
      this.state.durationTimer = null;
    }
  },

  connect(taskId) {
    this.closeStream();
    this.stopPolling();
    this.state.terminal = false;

    if (typeof EventSource === 'undefined') {
      this.startPolling(taskId);
      return;
    }

    const url = `/api/runs/${encodeURIComponent(taskId)}/events/stream?idle_timeout=120&heartbeat=15`;
    const source = new EventSource(url);
    this.state.source = source;

    this.EVENT_TYPES.forEach((type) => {
      source.addEventListener(type, (message) => {
        let event;
        try {
          event = JSON.parse(message.data);
        } catch (err) {
          console.warn('Invalid run event payload', err);
          return;
        }
        this.handleEvent(event);
      });
    });

    source.onopen = () => {
      if (!this.state.terminal && !this.state.paused) this.setConnection('live');
      this.stopPolling();
    };

    source.onerror = () => {
      if (this.state.terminal) return;
      this.setConnection('reconnecting');
      // EventSource reconnects on its own (resuming via Last-Event-ID). The
      // polling watchdog covers proxies that close the stream permanently.
      this.startPolling(taskId);
    };
  },

  closeStream() {
    if (this.state.source) {
      this.state.source.close();
      this.state.source = null;
    }
  },

  startPolling(taskId) {
    if (this.state.pollTimer) return;
    this.state.pollTimer = setInterval(async () => {
      if (this.state.terminal) {
        this.stopPolling();
        return;
      }
      try {
        const res = await fetch(`/api/runs/${encodeURIComponent(taskId)}/events?after=${this.state.lastSeq}`);
        if (!res.ok) return;
        const data = await res.json();
        (data.events || []).forEach((event) => this.handleEvent(event));
        if (this.state.terminal) this.stopPolling();
      } catch (err) {
        // Keep polling; the next tick may recover.
      }
    }, 2500);
  },

  stopPolling() {
    if (this.state.pollTimer) {
      clearInterval(this.state.pollTimer);
      this.state.pollTimer = null;
    }
  },

  handleEvent(event) {
    if (!event || typeof event.seq !== 'number') return;
    if (this.state.seenSeqs.has(event.seq)) return;
    this.state.seenSeqs.add(event.seq);
    this.state.lastSeq = Math.max(this.state.lastSeq, event.seq);
    if (this.state.approvalPending) this.armApprovalWatchdog();
    if (this.state.paused) {
      this.state.paused = false;
      this.setConnection('live');
    }
    if (event.type !== 'output') this.state.openLines = {};
    if (event.type !== 'result') this.startDurationTimer();

    const payload = event.payload || {};
    switch (event.type) {
      case 'output':
        if (payload.decision === 'rejected') {
          this.updateEvidence({ approval: '已拒绝' });
        }
        this.appendOutput(payload.text, event.agent, 'output', payload.stream);
        break;
      case 'test_line':
        this.appendOutput(payload.line, event.agent, 'test', payload.stream);
        break;
      case 'tool_call':
        this.appendTool(event, true);
        break;
      case 'tool_result':
        this.appendTool(event, false);
        break;
      case 'status':
        this.handleStatus(event);
        break;
      case 'usage':
        this.handleUsage(payload);
        break;
      case 'diff':
        this.renderDiff(event);
        break;
      case 'approval_required':
        this.handleApproval(event);
        break;
      case 'result':
        this.handleResult(event);
        break;
      default:
        break;
    }
  },

  normalizeText(value) {
    if (value === null || value === undefined) return '';
    return String(value).replace(/\x1b\[[0-9;]*m/g, '');
  },

  createOutputRow(agent, kind, stream, text) {
    const row = document.createElement('div');
    row.className = 'run-line';
    if (kind === 'test') row.classList.add('run-line-test');
    if (stream === 'stderr') row.classList.add('run-line-stderr');
    if (kind === 'test' && /\b(FAILED|ERROR|FAIL|Traceback|AssertionError)\b/i.test(text)) {
      row.classList.add('run-line-error');
    }

    const badge = document.createElement('span');
    badge.className = `run-agent ${this.AGENT_STYLES[agent] || 'run-agent-system'}`;
    badge.textContent = (agent || 'system').toUpperCase();
    row.appendChild(badge);

    const body = document.createElement('span');
    body.className = 'run-text';
    body.textContent = text || ' ';
    row.appendChild(body);
    return { row, body };
  },

  appendOutput(text, agent, kind, stream) {
    const pane = document.getElementById('runOutputPane');
    if (!pane) return;
    const content = this.normalizeText(text);
    if (!content) return;

    const lines = content.split(/\r?\n/);
    if (content.endsWith('\n')) lines.pop();
    if (kind === 'output') {
      // LLM streams arrive as arbitrary chunks; keep appending to the same
      // visual line until a newline (or another agent's event) breaks it.
      const key = `${agent}|${stream}`;
      let open = this.state.openLines[key];
      lines.forEach((rawLine, index) => {
        const line = rawLine.replace(/\t/g, '    ');
        if (index === 0 && open && open.row.isConnected) {
          open.body.textContent += line;
          return;
        }
        open = this.createOutputRow(agent, kind, stream, line || ' ');
        pane.appendChild(open.row);
      });
      if (content.endsWith('\n')) {
        delete this.state.openLines[key];
      } else if (open) {
        this.state.openLines[key] = open;
      }
    } else {
      lines.forEach((rawLine) => {
        const line = rawLine.replace(/\t/g, '    ');
        const created = this.createOutputRow(agent, kind, stream, line || ' ');
        pane.appendChild(created.row);
      });
    }

    while (pane.childElementCount > this.MAX_OUTPUT_LINES) {
      pane.removeChild(pane.firstChild);
    }
    this.scrollOutputToEnd(pane);
  },

  appendTool(event, isCall) {
    const pane = document.getElementById('runOutputPane');
    if (!pane) return;
    this.state.openLines = {};
    const payload = event.payload || {};
    const name = payload.name || 'tool';
    let detail = '';
    if (payload.parameters && Object.keys(payload.parameters).length > 0) {
      try {
        detail = JSON.stringify(payload.parameters);
      } catch (err) {
        detail = String(payload.parameters);
      }
      if (detail.length > 240) detail = `${detail.slice(0, 240)}…`;
    }

    const row = document.createElement('div');
    const policyRejected = !isCall && payload.policy_rejected === true;
    row.className = `run-line run-line-tool ${isCall ? 'run-tool-call' : 'run-tool-result'}${policyRejected ? ' run-tool-policy' : ''}`;

    const badge = document.createElement('span');
    badge.className = `run-agent ${this.AGENT_STYLES[event.agent] || 'run-agent-system'}`;
    badge.textContent = (event.agent || 'system').toUpperCase();
    row.appendChild(badge);

    const body = document.createElement('span');
    body.className = 'run-text';
    const succeeded = payload.succeeded === undefined
      ? ''
      : payload.succeeded ? '✓ ' : '✗ ';
    if (isCall) {
      body.textContent = `⚙ ${name} ${detail}`;
    } else if (policyRejected) {
      const command = payload.command ? ` · ${payload.command}` : '';
      const reason = payload.reason ? ` — ${payload.reason}` : '';
      body.textContent = `${succeeded}${name} 已拒绝${command}${reason}`;
    } else {
      body.textContent = `${succeeded}${name}`;
    }
    row.appendChild(body);

    pane.appendChild(row);
    while (pane.childElementCount > this.MAX_OUTPUT_LINES) {
      pane.removeChild(pane.firstChild);
    }
    this.scrollOutputToEnd(pane);
  },

  scrollOutputToEnd(pane) {
    const autoScroll = document.getElementById('runAutoScroll');
    if (!autoScroll || !autoScroll.checked) return;
    pane.scrollTop = pane.scrollHeight;
  },

  handleStatus(event) {
    const payload = event.payload || {};
    if (payload.state) {
      this.updateState(payload.state);
      this.setMetrics({ repairs: payload.repair_attempts });
      this.appendTimeline(
        event,
        `状态 → ${payload.state}`,
        payload.summary ? this.truncate(payload.summary, 160) : '',
        'state',
      );
      return;
    }

    const phase = payload.phase || event.phase || '';
    const stage = payload.stage || '';
    const labels = [];
    if (phase) labels.push(this.PHASE_LABELS[phase] || phase);
    if (stage) labels.push(this.STAGE_LABELS[stage] || stage);
    let text = labels.join(' · ') || '状态更新';

    const details = [];
    if (payload.attempt !== undefined) details.push(`第 ${payload.attempt} 轮修复`);
    if (payload.model) details.push(payload.model);
    if (Array.isArray(payload.files_written)) details.push(`${payload.files_written.length} 个文件`);
    if (payload.files_written && !Array.isArray(payload.files_written) && typeof payload.files_written === 'object') {
      details.push(`${Object.keys(payload.files_written).length} 个文件`);
    }
    if (payload.tool_steps !== undefined) details.push(`${payload.tool_steps} 次工具`);
    if (payload.diff_lines !== undefined) details.push(`${payload.diff_lines} 行 diff`);
    if (payload.duration_s !== undefined) details.push(`${Number(payload.duration_s).toFixed(2)}s`);
    if (payload.test_passed !== undefined && payload.test_passed !== null) {
      details.push(payload.test_passed ? '测试通过' : '测试未通过');
    }
    if (payload.summary) details.push(this.truncate(payload.summary, 120));
    if (payload.error) details.push(this.truncate(payload.error, 160));
    if (payload.reason && !payload.summary) details.push(this.truncate(payload.reason, 120));

    if (details.length > 0) text += ` — ${details.join(' · ')}`;

    const evidence = {};
    if (stage === 'complete' && payload.passed !== undefined) {
      evidence.tests = payload.passed === true;
      if (payload.total !== undefined) evidence.testsTotal = Number(payload.total) || 0;
      if (payload.failed !== undefined) evidence.testsFailed = Number(payload.failed) || 0;
    }
    if (payload.test_passed !== undefined && payload.test_passed !== null) {
      evidence.tests = payload.test_passed === true;
    }
    if (payload.commit_sha) evidence.commit = payload.commit_sha;
    if (payload.diff_lines !== undefined) evidence.diffLines = Number(payload.diff_lines) || 0;
    if (phase === 'review' && stage === 'failed') {
      evidence.reviewApproved = false;
      evidence.reviewSummary = payload.error || '独立复核调用失败';
    }
    if (payload.review) this.applyVerdict(evidence, payload.review);
    if (payload.verdict) this.applyVerdict(evidence, payload.verdict);
    if (Object.keys(evidence).length > 0) this.updateEvidence(evidence);

    if (phase === 'approval' && (stage === 'approved' || stage === 'rejected')) {
      const decision = stage === 'approved' ? '已批准' : '已拒绝';
      const notes = payload.notes || payload.summary || '';
      this.updateEvidence({ approval: notes ? `${decision}：${this.truncate(notes, 160)}` : decision });
      this.hideApproval();
    }

    let tone = 'neutral';
    if (stage === 'failed') tone = 'error';
    else if (stage === 'complete' || stage === 'approved') tone = 'success';
    else if (stage === 'skipped') tone = 'warn';
    else if (stage === 'start') tone = 'active';
    else if (phase === 'state') tone = 'state';

    this.appendTimeline(event, text, '', tone);
  },

  handleUsage(payload) {
    this.state.usage.prompt += Number(payload.prompt_tokens || 0);
    this.state.usage.completion += Number(payload.completion_tokens || 0);
    this.state.usage.cost += Number(payload.cost_cny || 0);
    this.setMetrics({
      cost: this.state.usage.cost,
      tokens: this.state.usage.prompt + this.state.usage.completion,
    });
  },

  handleApproval(event) {
    this.clearApprovalTimer();
    const payload = event.payload || {};
    const banner = document.getElementById('runApprovalBanner');
    if (!banner) return;

    const parts = ['待人工审批：' + (payload.reason || '运行已暂停，等待批准后继续')];
    if (payload.severity) parts.push(`严重度: ${payload.severity}`);
    if (payload.commit_sha) parts.push(`Commit: ${String(payload.commit_sha).slice(0, 12)}`);
    if (Array.isArray(payload.tests) && payload.tests.length > 0) {
      parts.push(`涉及测试文件: ${payload.tests.join(', ')}`);
    }

    const textEl = document.getElementById('runApprovalText');
    if (textEl) textEl.textContent = parts.join('\n');
    const statusEl = document.getElementById('runApprovalStatus');
    if (statusEl) statusEl.textContent = '';
    const canDecide = !this.state.replay || this.state.historicalState === 'WAITING_APPROVAL';
    const notesEl = document.getElementById('runApprovalNotes');
    if (notesEl) {
      notesEl.value = '';
      notesEl.disabled = false;
      notesEl.classList.toggle('hidden', !canDecide);
    }
    const allowTestsEl = document.getElementById('runApprovalAllowTests');
    if (allowTestsEl) {
      allowTestsEl.checked = false;
      allowTestsEl.disabled = !canDecide;
    }
    const allowTestsRow = document.getElementById('runApprovalAllowTestsRow');
    if (allowTestsRow) allowTestsRow.classList.toggle('hidden', !canDecide);

    const actions = document.getElementById('runApprovalActions');
    if (actions) actions.classList.toggle('hidden', !canDecide);
    const replayNote = document.getElementById('runApprovalReplayNote');
    if (replayNote) replayNote.classList.toggle('hidden', canDecide);
    this.state.approvalPending = false;
    this.setApprovalPending(false);

    banner.classList.remove('hidden');
    this.updateState('WAITING_APPROVAL');

    const evidence = { approval: '等待人工审批' };
    if (payload.commit_sha) evidence.commit = payload.commit_sha;
    if (payload.severity) evidence.severity = payload.severity;
    if (payload.verdict) this.applyVerdict(evidence, payload.verdict);
    this.updateEvidence(evidence);

    this.appendTimeline(event, '等待人工审批', this.truncate(payload.reason || '', 160), 'warn');
  },

  setApprovalPending(pending) {
    const approveBtn = document.getElementById('runApproveBtn');
    const rejectBtn = document.getElementById('runRejectBtn');
    const notesEl = document.getElementById('runApprovalNotes');
    const allowTestsEl = document.getElementById('runApprovalAllowTests');
    if (approveBtn) approveBtn.disabled = pending;
    if (rejectBtn) rejectBtn.disabled = pending;
    if (notesEl) notesEl.disabled = pending;
    if (allowTestsEl) allowTestsEl.disabled = pending;
  },

  clearApprovalTimer() {
    if (this.state.approvalTimer) {
      window.clearTimeout(this.state.approvalTimer);
      this.state.approvalTimer = null;
    }
  },

  armApprovalWatchdog() {
    this.clearApprovalTimer();
    // Re-armed by each resumed event; fires only when the stream stalls.
    this.state.approvalTimer = window.setTimeout(() => {
      this.state.approvalTimer = null;
      this.state.approvalPending = false;
      this.setApprovalPending(false);
      const statusEl = document.getElementById('runApprovalStatus');
      if (statusEl) {
        statusEl.textContent = '审批已提交，但 30 秒无新事件；请刷新历史或检查任务日志后重试';
      }
    }, 30000);
  },

  hideApproval() {
    this.clearApprovalTimer();
    const banner = document.getElementById('runApprovalBanner');
    if (banner) banner.classList.add('hidden');
    const textEl = document.getElementById('runApprovalText');
    if (textEl) textEl.textContent = '';
    const statusEl = document.getElementById('runApprovalStatus');
    if (statusEl) statusEl.textContent = '';
    const notesEl = document.getElementById('runApprovalNotes');
    if (notesEl) {
      notesEl.value = '';
      notesEl.classList.remove('hidden');
    }
    const allowTestsEl = document.getElementById('runApprovalAllowTests');
    if (allowTestsEl) allowTestsEl.checked = false;
    this.state.approvalPending = false;
    this.setApprovalPending(false);
  },

  async decideApproval(approved) {
    const taskId = this.state.currentTaskId;
    if (!taskId || this.state.approvalPending) return;

    const notesEl = document.getElementById('runApprovalNotes');
    const notes = (notesEl ? notesEl.value : '').trim().slice(0, 2000);
    const allowTestsEl = document.getElementById('runApprovalAllowTests');
    const allowUntrustedTests = approved && !!(allowTestsEl && allowTestsEl.checked);
    if (!approved) {
      const ok = window.confirm('确认拒绝该运行？拒绝后任务将标记为 FAILED。');
      if (!ok) return;
    } else if (!this.state.mock) {
      const ok = window.confirm('该任务为 LIVE 模式，批准后将调用真实模型并产生费用。确定继续吗？');
      if (!ok) return;
    }

    this.state.approvalPending = true;
    this.setApprovalPending(true);
    const statusEl = document.getElementById('runApprovalStatus');
    if (statusEl) statusEl.textContent = approved ? '正在提交批准…' : '正在提交拒绝…';

    const controller = new AbortController();
    const abortTimer = window.setTimeout(() => controller.abort(), 15000);
    try {
      const res = await fetch(
        `/api/runs/${encodeURIComponent(taskId)}/${approved ? 'approve' : 'reject'}?mock=${this.state.mock}`,
        {
          method: 'POST',
          signal: controller.signal,
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            notes: notes || undefined,
            allow_untrusted_tests: allowUntrustedTests,
          }),
        },
      );
      window.clearTimeout(abortTimer);
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);

      if (statusEl) {
        statusEl.textContent = approved
          ? '已提交批准，等待恢复运行的事件…'
          : '已提交拒绝，等待终态事件…';
      }
      this.clearApprovalTimer();
      this.armApprovalWatchdog();
      if (window.App) {
        App.showToast(approved ? `已批准: ${taskId}` : `已拒绝: ${taskId}`, approved ? 'success' : 'error');
      }
      this.loadHistory();
    } catch (err) {
      window.clearTimeout(abortTimer);
      this.clearApprovalTimer();
      this.state.approvalPending = false;
      this.setApprovalPending(false);
      if (statusEl) statusEl.textContent = `审批操作失败: ${err.message}`;
      if (window.App) App.showToast(`审批操作失败: ${err.message}`, 'error');
    }
  },

  applyVerdict(target, verdict) {
    if (!verdict || typeof verdict !== 'object') return;
    if (verdict.approved !== undefined) target.reviewApproved = verdict.approved === true;
    if (verdict.severity) target.severity = verdict.severity;
    if (verdict.summary) target.reviewSummary = verdict.summary;
    if (Array.isArray(verdict.critical_issues)) target.criticalIssues = verdict.critical_issues;
  },

  updateEvidence(partial) {
    if (!partial || Object.keys(partial).length === 0) return;
    Object.assign(this.state.evidence, partial);
    this.renderEvidence();
  },

  renderEvidence() {
    const body = document.getElementById('runEvidenceBody');
    const meta = document.getElementById('runEvidenceMeta');
    if (!body) return;
    const data = this.state.evidence;
    body.innerHTML = '';

    const add = (label, value, cls = 'text-slate-200', wide = false) => {
      const wrap = document.createElement('div');
      if (wide) wrap.className = 'col-span-2';
      const dt = document.createElement('dt');
      dt.className = 'text-slate-500';
      dt.textContent = label;
      const dd = document.createElement('dd');
      dd.className = `mt-0.5 break-words ${cls}`;
      dd.textContent = value;
      wrap.appendChild(dt);
      wrap.appendChild(dd);
      body.appendChild(wrap);
    };

    let rows = 0;
    if (data.tests !== undefined) {
      const detail = data.testsTotal
        ? ` （${data.testsTotal} 个用例，${data.testsFailed || 0} 失败）`
        : '';
      add('测试结果', `${data.tests ? '通过' : '未通过'}${detail}`, data.tests ? 'text-emerald-400' : 'text-rose-400');
      rows += 1;
    }
    if (data.reviewApproved !== undefined) {
      add(
        '复核结论',
        data.reviewApproved ? '批准' : '拒绝',
        data.reviewApproved ? 'text-emerald-400' : 'text-rose-400',
      );
      rows += 1;
    }
    if (data.severity) {
      add('严重度', data.severity, data.severity === 'P0' ? 'text-rose-400' : 'text-amber-300');
      rows += 1;
    }
    if (data.commit) {
      add('Commit', String(data.commit).slice(0, 12), 'text-slate-200 font-mono');
      rows += 1;
    }
    if (data.diffFiles !== undefined) {
      add('变更文件', `${data.diffFiles} 个`, 'text-slate-200');
      rows += 1;
    }
    if (data.diffLines !== undefined) {
      add('Diff 行数', `${data.diffLines}`, 'text-slate-200');
      rows += 1;
    }
    if (data.approval) {
      add('人工审批', data.approval, 'text-amber-300', true);
      rows += 1;
    }
    if (data.reviewSummary) {
      add('复核摘要', this.truncate(data.reviewSummary, 400), 'text-slate-300', true);
      rows += 1;
    }
    if (Array.isArray(data.criticalIssues) && data.criticalIssues.length > 0) {
      add('关键问题', data.criticalIssues.map((item) => `• ${item}`).join('\n'), 'text-rose-300 whitespace-pre-wrap', true);
      rows += 1;
    }

    if (rows === 0) {
      const empty = document.createElement('div');
      empty.className = 'col-span-2 text-slate-500';
      empty.textContent = '等待测试、复核与审批证据';
      body.appendChild(empty);
      if (meta) meta.textContent = '等待证据';
      return;
    }
    if (meta) meta.textContent = `${rows} 项证据`;
  },

  handleResult(event) {
    const payload = event.payload || {};
    const success = payload.success === true;
    const state = payload.state || (success ? 'SUCCESS' : 'FAILED');
    const paused = payload.terminal === false || (!success && state === 'WAITING_APPROVAL');

    this.stopDurationTimer();
    this.updateState(state);

    const evidence = {};
    if (payload.test_passed !== undefined && payload.test_passed !== null) {
      evidence.tests = payload.test_passed === true;
    }
    if (payload.verdict) this.applyVerdict(evidence, payload.verdict);
    if (payload.final_commit_sha) evidence.commit = payload.final_commit_sha;
    if (paused) evidence.approval = '等待人工审批';
    this.updateEvidence(evidence);

    if (paused) {
      // A paused result only closes one segment: keep the stream and polling
      // alive so events from the resumed run arrive on the same task id.
      this.state.paused = true;
      this.setConnection('paused');
      this.state.startedAt = null;
      this.setMetrics({
        duration: payload.duration_s,
        cost: payload.cost_cny,
        tokens: Number(payload.prompt_tokens || 0) + Number(payload.completion_tokens || 0),
        repairs: payload.repair_attempts || 0,
      });
      this.appendOutput(
        `\n=== 运行暂停: ${state} · ${Number(payload.duration_s || 0).toFixed(1)}s · ¥${Number(payload.cost_cny || 0).toFixed(5)} ===`,
        'system',
        'output',
        'stdout',
      );
      if (payload.summary) {
        this.appendOutput(this.truncate(payload.summary, 500), 'system', 'output', 'stdout');
      }
      this.appendTimeline(
        event,
        `运行暂停 · ${state}`,
        payload.summary
          ? this.truncate(payload.summary, 180)
          : '等待审批或后续操作，可从同一任务继续',
        'warn',
      );
      return;
    }

    this.state.terminal = true;
    this.state.paused = false;
    this.hideApproval();
    this.stopPolling();
    this.closeStream();
    this.updateReplayBadge(this.state.replay);
    this.setConnection(success ? 'done' : 'failed');

    const prompt = Number(payload.prompt_tokens || 0);
    const completion = Number(payload.completion_tokens || 0);
    this.setMetrics({
      duration: payload.duration_s,
      cost: payload.cost_cny,
      tokens: prompt + completion,
      repairs: payload.repair_attempts || 0,
    });

    this.appendOutput(
      `\n=== 运行结束: ${state} · ${Number(payload.duration_s || 0).toFixed(1)}s · ¥${Number(payload.cost_cny || 0).toFixed(5)} ===`,
      'system',
      'output',
      'stdout',
    );
    if (payload.summary) {
      this.appendOutput(this.truncate(payload.summary, 500), 'system', 'output', 'stdout');
    }
    this.appendTimeline(
      event,
      `运行结束 · ${state}`,
      payload.summary ? this.truncate(payload.summary, 180) : '',
      success ? 'success' : 'error',
    );

    if (window.App) {
      App.showToast(
        success ? `运行完成: ${state}` : `运行结束: ${state}`,
        success ? 'success' : 'error',
      );
    }
    this.loadHistory();
  },

  appendTimeline(event, text, detail, tone) {
    const container = document.getElementById('runTimeline');
    if (!container) return;

    const item = document.createElement('div');
    item.className = `run-timeline-item run-tone-${tone || 'neutral'}`;

    const head = document.createElement('div');
    head.className = 'run-timeline-head';

    const time = document.createElement('span');
    time.className = 'run-timeline-time';
    time.textContent = this.formatClock(event.timestamp);
    head.appendChild(time);

    const agent = document.createElement('span');
    agent.className = `run-agent ${this.AGENT_STYLES[event.agent] || 'run-agent-system'}`;
    agent.textContent = (event.agent || 'system').toUpperCase();
    head.appendChild(agent);

    const body = document.createElement('span');
    body.className = 'run-timeline-text';
    body.textContent = text;
    head.appendChild(body);
    item.appendChild(head);

    if (detail) {
      const detailEl = document.createElement('div');
      detailEl.className = 'run-timeline-detail';
      detailEl.textContent = detail;
      item.appendChild(detailEl);
    }

    container.appendChild(item);
    while (container.childElementCount > this.MAX_TIMELINE_ITEMS) {
      container.removeChild(container.firstChild);
    }
    container.scrollTop = container.scrollHeight;
  },

  formatClock(timestamp) {
    if (!timestamp) return '--:--:--';
    const date = new Date(timestamp);
    if (Number.isNaN(date.getTime())) return '--:--:--';
    return date.toLocaleTimeString('zh-CN', { hour12: false });
  },

  renderDiff(event) {
    const payload = event.payload || {};
    const pane = document.getElementById('runDiffPane');
    const meta = document.getElementById('runDiffMeta');
    if (!pane) return;

    const patch = this.normalizeText(payload.patch || '');
    pane.innerHTML = '';
    if (!patch) {
      pane.textContent = '本次事件没有 diff 内容';
    } else {
      const allLines = patch.split(/\r?\n/);
      const shown = allLines.slice(0, this.MAX_DIFF_LINES);
      const fragment = document.createDocumentFragment();
      shown.forEach((line) => {
        const row = document.createElement('div');
        let cls = 'diff-line';
        if (line.startsWith('+++') || line.startsWith('---')) cls += ' diff-meta';
        else if (line.startsWith('+')) cls += ' diff-add';
        else if (line.startsWith('-')) cls += ' diff-del';
        else if (line.startsWith('@@')) cls += ' diff-hunk';
        else if (line.startsWith('diff --git') || line.startsWith('index ') || line.startsWith('\\')) cls += ' diff-meta';
        row.className = cls;
        row.textContent = line || ' ';
        fragment.appendChild(row);
      });
      if (allLines.length > this.MAX_DIFF_LINES) {
        const more = document.createElement('div');
        more.className = 'diff-line diff-meta';
        more.textContent = `… 省略 ${allLines.length - this.MAX_DIFF_LINES} 行`;
        fragment.appendChild(more);
      }
      pane.appendChild(fragment);
    }

    if (meta) {
      const sha = payload.commit_sha ? String(payload.commit_sha).slice(0, 8) : '-';
      const files = Array.isArray(payload.generated_files) ? payload.generated_files.length : 0;
      meta.textContent = `commit ${sha} · ${files} 个文件 · ${patch ? patch.split(/\r?\n/).length : 0} 行`;
    }

    const evidence = {};
    if (Array.isArray(payload.generated_files)) evidence.diffFiles = payload.generated_files.length;
    if (patch) evidence.diffLines = patch.split(/\r?\n/).length;
    if (payload.commit_sha) evidence.commit = payload.commit_sha;
    this.updateEvidence(evidence);

    pane.scrollTop = 0;
  },

  async openReplay(taskId) {
    if (!taskId) return;
    const mock = window.App ? App.state.isMock : false;
    try {
      const res = await fetch(`/api/runs/${encodeURIComponent(taskId)}?mock=${mock}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      const run = data.run || {};

      this.resetForRun(taskId, { mock: run.mock_mode, replay: true });
      this.state.historicalState = run.state || null;
      this.updateState(run.state || 'INIT');
      this.setMetrics({
        cost: run.cost_cny || 0,
        tokens: Number(run.prompt_tokens || 0) + Number(run.completion_tokens || 0),
        repairs: run.repair_attempts || 0,
      });

      const lastSeq = data.events ? Number(data.events.last_seq || 0) : 0;
      if (lastSeq === 0) {
        this.appendOutput('该运行没有持久化事件记录（可能由旧版本产生），仅显示任务状态。', 'system', 'output', 'stdout');
        this.setConnection('done');
        this.loadHistory();
        return;
      }
      this.connect(taskId);
    } catch (err) {
      this.showError(`回放运行失败: ${err.message}`);
    }
  },

  truncate(value, max) {
    const text = this.normalizeText(value);
    if (text.length <= max) return text;
    return `${text.slice(0, max)}…`;
  },
};

window.RunConsole = RunConsole;
