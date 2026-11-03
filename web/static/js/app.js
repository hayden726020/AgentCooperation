/**
 * AgentCooperation Web Console - Frontend Application Logic
 */

const App = {
  state: {
    currentTab: 'dashboard',
    isMock: false,
    autoRefresh: true,
    refreshIntervalId: null,
    activeTaskId: null,
    tokenChart: null,
    costChart: null,
    filters: {
      taskState: '',
      taskModel: '',
    },
  },

  init(initialMock = false) {
    this.state.isMock = initialMock;
    const mockSwitch = document.getElementById('mockModeToggle');
    if (mockSwitch) {
      mockSwitch.checked = initialMock;
      mockSwitch.addEventListener('change', (e) => {
        this.state.isMock = e.target.checked;
        this.updateModeBadge();
        if (window.CodexRecovery) {
          window.CodexRecovery.onModeChange();
        }
        if (window.RunConsole) {
          window.RunConsole.onModeChange();
        }
        this.refreshCurrentTab();
      });
    }

    const autoRefreshToggle = document.getElementById('autoRefreshToggle');
    if (autoRefreshToggle) {
      autoRefreshToggle.addEventListener('change', (e) => {
        this.state.autoRefresh = e.target.checked;
        this.setupAutoRefresh();
      });
    }

    if (window.CodexRecovery) {
      window.CodexRecovery.init();
    }
    if (window.RunConsole) {
      window.RunConsole.init();
    }

    this.setupTabs();
    this.updateModeBadge();
    this.refreshCurrentTab();
    this.setupAutoRefresh();

    if (window.lucide) {
      window.lucide.createIcons();
    }
  },

  updateModeBadge() {
    const badge = document.getElementById('envModeBadge');
    if (badge) {
      if (this.state.isMock) {
        badge.textContent = '🧪 模拟账库 (Mock)';
        badge.className = 'px-2.5 py-1 text-xs font-semibold rounded-full bg-amber-500/20 text-amber-300 border border-amber-500/40';
      } else {
        badge.textContent = '🟢 真实调用 (Live)';
        badge.className = 'px-2.5 py-1 text-xs font-semibold rounded-full bg-emerald-500/20 text-emerald-300 border border-emerald-500/40';
      }
    }
  },

  setupTabs() {
    const tabButtons = document.querySelectorAll('.tab-btn');
    tabButtons.forEach((btn) => {
      btn.addEventListener('click', () => {
        const tab = btn.dataset.tab;
        this.switchTab(tab);
      });
    });
  },

  switchTab(tabName) {
    this.state.currentTab = tabName;
    document.querySelectorAll('.tab-btn').forEach((btn) => {
      if (btn.dataset.tab === tabName) {
        btn.classList.add('bg-slate-800', 'text-sky-400', 'border-sky-500');
        btn.classList.remove('text-slate-400', 'border-transparent');
      } else {
        btn.classList.remove('bg-slate-800', 'text-sky-400', 'border-sky-500');
        btn.classList.add('text-slate-400', 'border-transparent');
      }
    });

    document.querySelectorAll('.tab-content').forEach((content) => {
      content.classList.add('hidden');
    });

    const activeContent = document.getElementById(`tab-${tabName}`);
    if (activeContent) {
      activeContent.classList.remove('hidden');
    }

    this.refreshCurrentTab();
  },

  setupAutoRefresh() {
    if (this.state.refreshIntervalId) {
      clearInterval(this.state.refreshIntervalId);
      this.state.refreshIntervalId = null;
    }
    if (this.state.autoRefresh) {
      this.state.refreshIntervalId = setInterval(() => {
        this.refreshCurrentTab(true);
      }, 5000);
    }
  },

  async refreshCurrentTab(isBackground = false) {
    const mockParam = `?mock=${this.state.isMock}`;
    try {
      await this.loadGlobalStatus(mockParam);

      if (this.state.currentTab === 'dashboard') {
        await this.loadDashboard(mockParam);
      } else if (this.state.currentTab === 'tasks') {
        await this.loadTasks(mockParam);
      } else if (this.state.currentTab === 'multica') {
        await this.loadMulticaBoard();
      } else if (this.state.currentTab === 'run') {
        if (window.RunConsole) {
          await window.RunConsole.refresh();
        }
      } else if (this.state.currentTab === 'codex') {
        await this.loadCodexCenter(mockParam, isBackground);
      } else if (this.state.currentTab === 'budget') {
        await this.loadBudgetDetails(mockParam);
      }

      if (this.state.activeTaskId) {
        await this.loadTaskDetail(this.state.activeTaskId, true);
      }

      if (window.lucide) {
        window.lucide.createIcons();
      }
    } catch (err) {
      if (!isBackground) {
        console.error('Refresh failed:', err);
      }
    }
  },

  async loadGlobalStatus(mockParam) {
    const res = await fetch(`/api/status${mockParam}`);
    if (!res.ok) return;
    const data = await res.json();

    const spendEl = document.getElementById('headerSpend');
    if (spendEl) {
      spendEl.textContent = `¥${data.spending.total_cost_cny.toFixed(4)} / ¥${data.spending.monthly_limit_cny}`;
    }

    const quotaEl = document.getElementById('headerQuotaBadge');
    if (quotaEl) {
      const qState = data.codex_quota.state;
      quotaEl.textContent = `Codex: ${qState}`;
      if (qState === 'READY') {
        quotaEl.className = 'px-2 py-0.5 text-xs rounded bg-emerald-500/20 text-emerald-300 border border-emerald-500/30';
      } else if (qState === 'EXHAUSTED') {
        quotaEl.className = 'px-2 py-0.5 text-xs rounded bg-rose-500/20 text-rose-300 border border-rose-500/30';
      } else {
        quotaEl.className = 'px-2 py-0.5 text-xs rounded bg-amber-500/20 text-amber-300 border border-amber-500/30';
      }
    }
  },

  async loadDashboard(mockParam) {
    const res = await fetch(`/api/status${mockParam}`);
    if (!res.ok) return;
    const data = await res.json();

    document.getElementById('dashCost').textContent = `¥${data.spending.total_cost_cny.toFixed(4)}`;
    document.getElementById('dashCostLimit').textContent = `限额: ¥${data.spending.monthly_limit_cny.toFixed(2)}`;

    const costProgress = document.getElementById('dashCostProgress');
    if (costProgress) {
      costProgress.style.width = `${Math.min(100, data.spending.percent_used)}%`;
      if (data.spending.total_cost_cny >= data.spending.warning_cny) {
        costProgress.className = 'h-2 rounded-full bg-rose-500';
      } else {
        costProgress.className = 'h-2 rounded-full bg-sky-500';
      }
    }
    document.getElementById('dashCostPercent').textContent = `${data.spending.percent_used}% 已用`;

    document.getElementById('dashTaskTotal').textContent = data.tasks_count.total;
    document.getElementById('dashTaskSuccess').textContent = `${data.tasks_count.success} 成功`;
    document.getElementById('dashTaskFailed').textContent = `${data.tasks_count.failed} 失败`;
    document.getElementById('dashTaskActive').textContent = `${data.tasks_count.active} 运行中`;

    document.getElementById('dashTokensTotal').textContent = data.spending.total_tokens.toLocaleString();
    document.getElementById('dashTokensPrompt').textContent = `输入: ${data.spending.prompt_tokens.toLocaleString()}`;
    document.getElementById('dashTokensCompletion').textContent = `输出: ${data.spending.completion_tokens.toLocaleString()}`;

    document.getElementById('dashCodexState').textContent = data.codex_quota.state;
    document.getElementById('dashAuditsPending').textContent = `${data.audits.pending_count} 条待补审`;

    const tasksRes = await fetch(`/api/tasks${mockParam}&limit=5`);
    if (tasksRes.ok) {
      const taskData = await tasksRes.json();
      const recentList = document.getElementById('dashRecentTasksList');
      if (recentList) {
        if (!taskData.tasks || taskData.tasks.length === 0) {
          recentList.innerHTML = `<div class="p-6 text-center text-slate-500">暂无任务记录，点击上方“新建任务”开始</div>`;
        } else {
          recentList.innerHTML = taskData.tasks.map(t => this.renderTaskCard(t)).join('');
        }
      }
    }

    this.renderCharts(data.spending);
  },

  renderCharts(spending) {
    const ctx = document.getElementById('tokenChart');
    if (!ctx) return;

    if (this.state.tokenChart) {
      this.state.tokenChart.destroy();
    }

    this.state.tokenChart = new Chart(ctx, {
      type: 'doughnut',
      data: {
        labels: ['输入 Token (Prompt)', '输出 Token (Completion)'],
        datasets: [{
          data: [spending.prompt_tokens, spending.completion_tokens],
          backgroundColor: ['#38bdf8', '#818cf8'],
          borderColor: '#0f172a',
          borderWidth: 2,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: {
            position: 'bottom',
            labels: { color: '#94a3b8', boxWidth: 12, padding: 15 },
          },
        },
      },
    });
  },

  renderTaskCard(t) {
    const stateBadge = this.getStateBadge(t.state);
    const modelBadge = t.primary_model === 'glm'
      ? `<span class="px-2 py-0.5 text-xs font-mono rounded bg-cyan-900/40 text-cyan-300 border border-cyan-700/50">GLM Flash</span>`
      : `<span class="px-2 py-0.5 text-xs font-mono rounded bg-blue-900/40 text-blue-300 border border-blue-700/50">DeepSeek Flash</span>`;

    return `
      <div class="p-4 rounded-xl glass-card glass-card-hover flex items-center justify-between cursor-pointer" onclick="App.openTaskDetail('${t.task_id}')">
        <div class="space-y-1">
          <div class="flex items-center space-x-2">
            <span class="font-mono text-xs text-slate-400">${t.task_id}</span>
            ${modelBadge}
            ${stateBadge}
          </div>
          <div class="font-medium text-slate-100 text-sm line-clamp-1">${this.escapeHtml(t.title)}</div>
        </div>
        <div class="text-right">
          <div class="text-xs text-sky-400 font-mono">¥${(t.total_cost_cny || 0).toFixed(4)}</div>
          <div class="text-xs text-slate-500">${this.formatDate(t.created_at)}</div>
        </div>
      </div>
    `;
  },

  async loadTasks(mockParam) {
    let url = `/api/tasks${mockParam}`;
    if (this.state.filters.taskState) url += `&state=${this.state.filters.taskState}`;
    if (this.state.filters.taskModel) url += `&model=${this.state.filters.taskModel}`;

    const res = await fetch(url);
    if (!res.ok) return;
    const data = await res.json();

    const tbody = document.getElementById('tasksTableBody');
    if (!tbody) return;

    if (!data.tasks || data.tasks.length === 0) {
      tbody.innerHTML = `<tr><td colspan="7" class="p-8 text-center text-slate-500">未找到匹配的任务记录</td></tr>`;
      return;
    }

    tbody.innerHTML = data.tasks.map(t => `
      <tr class="border-b border-slate-800 hover:bg-slate-800/40 transition">
        <td class="p-3 font-mono text-xs text-sky-300">
          <button class="hover:underline text-left" onclick="App.openTaskDetail('${t.task_id}')">${t.task_id}</button>
        </td>
        <td class="p-3 font-medium text-slate-200 max-w-xs truncate">${this.escapeHtml(t.title)}</td>
        <td class="p-3 font-mono text-xs">
          ${t.primary_model === 'glm' ? '<span class="text-cyan-400">GLM Flash</span>' : '<span class="text-blue-400">DeepSeek Flash</span>'}
        </td>
        <td class="p-3">${this.getStateBadge(t.state)}</td>
        <td class="p-3 text-xs text-slate-300 font-mono">${t.repair_attempts || 0}</td>
        <td class="p-3 text-xs font-mono text-amber-400">¥${(t.total_cost_cny || 0).toFixed(4)}</td>
        <td class="p-3 text-xs text-slate-400 whitespace-nowrap">${this.formatDate(t.created_at)}</td>
      </tr>
    `).join('');
  },

  async openTaskDetail(taskId) {
    this.state.activeTaskId = taskId;
    const modal = document.getElementById('taskDetailModal');
    if (modal) modal.classList.remove('hidden');
    await this.loadTaskDetail(taskId);
  },

  closeTaskDetail() {
    this.state.activeTaskId = null;
    const modal = document.getElementById('taskDetailModal');
    if (modal) modal.classList.add('hidden');
  },

  async loadTaskDetail(taskId, isSilent = false) {
    const mockParam = `?mock=${this.state.isMock}`;
    const res = await fetch(`/api/tasks/${taskId}${mockParam}`);
    if (!res.ok) return;
    const data = await res.json();
    const t = data.task;
    const logs = data.logs || [];

    document.getElementById('modalTaskId').textContent = t.task_id;
    document.getElementById('modalTaskTitle').textContent = t.title;
    document.getElementById('modalTaskState').innerHTML = this.getStateBadge(t.state);
    document.getElementById('modalTaskModel').textContent = t.primary_model.toUpperCase();
    document.getElementById('modalTaskCost').textContent = `¥${(t.total_cost_cny || 0).toFixed(5)}`;
    document.getElementById('modalTaskReq').textContent = t.requirement;
    document.getElementById('modalTaskBranch').textContent = t.branch_name || '未创建分支';
    document.getElementById('modalTaskWorktree').textContent = t.worktree_path || '无独立工作树';
    document.getElementById('modalTaskSummary').textContent = t.result_summary || '进行中或无总结';

    this.renderFsmProgression(t.state, logs);

    const timeline = document.getElementById('modalTaskLogs');
    if (timeline) {
      if (logs.length === 0) {
        timeline.innerHTML = `<div class="p-4 text-center text-slate-500">尚无执行日志</div>`;
      } else {
        timeline.innerHTML = logs.map(log => this.renderLogItem(log)).join('');
      }
    }
  },

  renderFsmProgression(currentState, logs) {
    const fsmContainer = document.getElementById('modalFsmFlow');
    if (!fsmContainer) return;

    const pipelineSteps = [
      { id: 'INIT', label: '1. 初始化' },
      { id: 'PLANNING', label: '2. 规划' },
      { id: 'DEVELOPING', label: '3. 构建代码' },
      { id: 'TESTING', label: '4. 测试运行' },
      { id: 'REVIEWING', label: '5. 独立复核' },
      { id: 'SUCCESS', label: '6. 交付成功' },
    ];

    const hasVisited = (state) => {
      if (currentState === state) return true;
      if (state === 'INIT') return true;
      return logs.some(l => {
        const step = (l.step_name || '').toLowerCase();
        if (state === 'PLANNING' && step.includes('planning')) return true;
        if (state === 'DEVELOPING' && step.includes('developing')) return true;
        if (state === 'TESTING' && step.includes('testing')) return true;
        if (state === 'REVIEWING' && step.includes('gemini')) return true;
        return false;
      });
    };

    let html = '<div class="flex items-center flex-wrap gap-2 py-2">';
    pipelineSteps.forEach((step, idx) => {
      const isActive = currentState === step.id;
      const isPassed = !isActive && hasVisited(step.id);

      let nodeClass = 'fsm-node px-3 py-1.5 rounded-lg text-xs font-mono border ';
      if (isActive) {
        nodeClass += 'active text-sky-300 font-semibold';
      } else if (isPassed) {
        nodeClass += 'passed border-emerald-500/30 text-emerald-300';
      } else {
        nodeClass += 'border-slate-700 text-slate-500 bg-slate-900/50';
      }

      html += `<div class="${nodeClass}">${step.label}</div>`;
      if (idx < pipelineSteps.length - 1) {
        html += `<span class="fsm-arrow text-slate-600 text-xs">➔</span>`;
      }
    });

    if (['REPAIRING', 'ESCALATING', 'CODEX_BACKLOGGED', 'WAITING_APPROVAL', 'FAILED'].includes(currentState)) {
      html += `
        <span class="fsm-arrow text-amber-500 text-xs">➔</span>
        <div class="fsm-node active px-3 py-1.5 rounded-lg text-xs font-mono border border-rose-500/50 bg-rose-500/10 text-rose-300 font-semibold">
          分支节点: ${currentState}
        </div>
      `;
    }

    html += '</div>';
    fsmContainer.innerHTML = html;
  },

  renderLogItem(log) {
    const roleColors = {
      PRIMARY: 'bg-cyan-500/10 text-cyan-300 border-cyan-500/30',
      REVIEWER: 'bg-purple-500/10 text-purple-300 border-purple-500/30',
      EXPERT: 'bg-amber-500/10 text-amber-300 border-amber-500/30',
      TEST_RUNNER: 'bg-emerald-500/10 text-emerald-300 border-emerald-500/30',
      SYSTEM: 'bg-slate-500/10 text-slate-300 border-slate-500/30',
    };
    const roleBadge = `<span class="px-2 py-0.5 text-2xs font-mono uppercase rounded border ${roleColors[log.agent_role] || 'bg-slate-800 text-slate-400'}">${log.agent_role}</span>`;

    const statusBadge = log.status === 'SUCCESS'
      ? `<span class="text-emerald-400 text-xs font-medium">✓ 成功</span>`
      : `<span class="text-rose-400 text-xs font-medium">✗ ${log.status}</span>`;

    return `
      <div class="p-3.5 rounded-xl border border-slate-800 bg-slate-900/60 space-y-2">
        <div class="flex items-center justify-between">
          <div class="flex items-center space-x-2">
            ${roleBadge}
            <span class="font-medium text-sm text-slate-200">${this.escapeHtml(log.step_name)}</span>
            <span class="text-xs text-slate-400 font-mono">(${log.agent_model})</span>
          </div>
          <div class="flex items-center space-x-3">
            <span class="text-xs font-mono text-slate-400">Tokens: ${(log.prompt_tokens + log.completion_tokens).toLocaleString()}</span>
            <span class="text-xs font-mono text-amber-400">¥${(log.cost_cny || 0).toFixed(5)}</span>
            ${statusBadge}
          </div>
        </div>

        ${log.output_snippet ? `
          <details class="text-xs">
            <summary class="cursor-pointer text-slate-400 hover:text-slate-200 select-none">查看输出摘要</summary>
            <pre class="code-box mt-1 text-slate-300">${this.escapeHtml(log.output_snippet)}</pre>
          </details>
        ` : ''}

        ${log.error_snippet ? `
          <div class="p-2 rounded bg-rose-950/40 border border-rose-800/50 text-rose-300 text-xs font-mono">
            <strong>错误报告:</strong> ${this.escapeHtml(log.error_snippet)}
          </div>
        ` : ''}
      </div>
    `;
  },

  async loadMulticaBoard() {
    const res = await fetch('/api/multica/board');
    if (!res.ok) return;
    const data = await res.json();

    const columns = ['todo', 'in_progress', 'in_review', 'done', 'failed'];
    columns.forEach(col => {
      const container = document.getElementById(`kanban-col-${col}`);
      if (!container) return;
      const issues = data.columns[col] || [];
      if (issues.length === 0) {
        container.innerHTML = `<div class="p-4 text-center text-slate-600 text-xs border border-dashed border-slate-800 rounded-lg">空泳道</div>`;
      } else {
        container.innerHTML = issues.map(issue => `
          <div class="p-3.5 rounded-xl glass-card border border-slate-800 hover:border-sky-500/40 space-y-2 cursor-pointer" onclick="App.openMulticaDetail('${issue.id}')">
            <div class="flex items-center justify-between">
              <span class="font-mono text-xs text-sky-400 font-semibold">${issue.id}</span>
              <span class="px-2 py-0.5 text-2xs rounded bg-slate-800 text-slate-300 uppercase">${issue.priority}</span>
            </div>
            <div class="font-medium text-sm text-slate-100">${this.escapeHtml(issue.title)}</div>
            <div class="text-xs text-slate-400 line-clamp-2">${this.escapeHtml(issue.description)}</div>
            <div class="pt-2 border-t border-slate-800 flex items-center justify-between text-2xs text-slate-500">
              <span>👤 ${issue.assignee || '未认领'}</span>
              ${col === 'todo' ? `
                <button onclick="event.stopPropagation(); App.runMulticaIssue('${issue.id}')" class="px-2 py-1 rounded bg-sky-600 hover:bg-sky-500 text-white font-medium">执行</button>
              ` : ''}
            </div>
          </div>
        `).join('');
      }
    });
  },

  async runMulticaIssue(issueId) {
    if (!confirm(`确定通过 AgentCooperation 认领并执行 Issue ${issueId} 吗？`)) return;
    try {
      const res = await fetch('/api/multica/run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          issue_id: issueId,
          mock: this.state.isMock,
        }),
      });
      const data = await res.json();
      if (res.ok) {
        this.showToast(`Issue ${issueId} 已投入后台执行！Job ID: ${data.job_id}`, 'success');
        this.refreshCurrentTab();
      } else {
        this.showToast(`执行失败: ${data.error}`, 'error');
      }
    } catch (e) {
      this.showToast(`请求异常: ${e.message}`, 'error');
    }
  },

  async loadCodexCenter(mockParam, isBackground = false) {
    const res = await fetch(`/api/codex/quota${mockParam}`);
    if (!res.ok) {
      if (window.CodexRecovery) {
        window.CodexRecovery.setFeedback(`加载 Codex 额度失败 (HTTP ${res.status})`, 'error');
      }
      return;
    }
    const data = await res.json();
    const quota = data.quota;
    const audits = data.audits || [];

    document.getElementById('codexCardState').textContent = quota.state;
    document.getElementById('codexCardLastCheck').textContent = quota.last_checked_at ? this.formatDate(quota.last_checked_at) : '未记录';
    document.getElementById('codexCardRecovery').textContent = quota.estimated_recovery_at
      ? (window.CodexRecovery
          ? window.CodexRecovery.formatBeijingDisplay(quota.estimated_recovery_at)
          : this.formatDate(quota.estimated_recovery_at))
      : '未设置';
    document.getElementById('codexCardReason').textContent = quota.exhausted_reason || '正常运行无异常';

    if (window.CodexRecovery) {
      window.CodexRecovery.refresh(this.state.isMock, isBackground);
    }

    const tbody = document.getElementById('auditsTableBody');
    if (tbody) {
      if (audits.length === 0) {
        tbody.innerHTML = `<tr><td colspan="6" class="p-8 text-center text-slate-500">暂无待补审或历史补审记录</td></tr>`;
      } else {
        tbody.innerHTML = audits.map(a => `
          <tr class="border-b border-slate-800 hover:bg-slate-800/40">
            <td class="p-3 font-mono text-xs text-sky-400">${a.audit_id}</td>
            <td class="p-3 font-mono text-xs text-slate-300">${a.task_id}</td>
            <td class="p-3"><span class="px-2 py-0.5 text-xs rounded font-bold ${a.severity === 'P0' ? 'bg-rose-500/20 text-rose-300 border border-rose-500/40' : 'bg-amber-500/20 text-amber-300 border border-amber-500/40'}">${a.severity}</span></td>
            <td class="p-3 text-xs text-slate-300 max-w-xs truncate">${this.escapeHtml(a.issue_description)}</td>
            <td class="p-3"><span class="px-2 py-0.5 text-xs rounded ${a.status === 'RESOLVED' ? 'badge-success' : 'badge-warning'}">${a.status}</span></td>
            <td class="p-3 text-xs text-slate-400">${this.formatDate(a.created_at)}</td>
          </tr>
        `).join('');
      }
    }
  },

  async loadBudgetDetails(mockParam) {
    const res = await fetch(`/api/budget${mockParam}`);
    if (!res.ok) return;
    const data = await res.json();
    const limits = data.limits;
    const reservations = data.reservations || [];

    document.getElementById('policyMonthly').textContent = `¥${limits.monthly_cny.toFixed(2)}`;
    document.getElementById('policyWarning').textContent = `¥${limits.warning_cny.toFixed(2)}`;
    document.getElementById('policyPause').textContent = `¥${limits.pause_new_cny.toFixed(2)}`;
    document.getElementById('policyTaskLimit').textContent = `¥${limits.task_limit_cny.toFixed(2)}`;

    const tbody = document.getElementById('reservationsTableBody');
    if (tbody) {
      if (reservations.length === 0) {
        tbody.innerHTML = `<tr><td colspan="6" class="p-8 text-center text-slate-500">暂无预留记账记录</td></tr>`;
      } else {
        tbody.innerHTML = reservations.map(r => `
          <tr class="border-b border-slate-800 hover:bg-slate-800/40 font-mono text-xs">
            <td class="p-3 text-sky-400">${r.reservation_id}</td>
            <td class="p-3 text-slate-300">${r.task_id}</td>
            <td class="p-3 text-amber-400">¥${r.estimated_cost_cny.toFixed(5)}</td>
            <td class="p-3 text-emerald-400">${r.actual_cost_cny !== null ? `¥${r.actual_cost_cny.toFixed(5)}` : '-'}</td>
            <td class="p-3"><span class="px-2 py-0.5 rounded ${r.status === 'SETTLED' ? 'badge-success' : 'badge-warning'}">${r.status}</span></td>
            <td class="p-3 text-slate-400">${this.formatDate(r.created_at)}</td>
          </tr>
        `).join('');
      }
    }
  },

  openNewTaskModal() {
    const modal = document.getElementById('newTaskModal');
    if (modal) modal.classList.remove('hidden');
  },

  closeNewTaskModal() {
    const modal = document.getElementById('newTaskModal');
    if (modal) modal.classList.add('hidden');
  },

  async submitNewTask(event) {
    event.preventDefault();
    const title = document.getElementById('newTaskTitle').value.trim();
    const req = document.getElementById('newTaskReq').value.trim();
    const model = document.querySelector('input[name="taskModel"]:checked').value;
    const mock = document.getElementById('newTaskMock').checked;
    const testCmd = document.getElementById('newTaskTestCmd').value.trim();

    if (!req) {
      alert('请输入任务需求描述');
      return;
    }

    try {
      const res = await fetch('/api/tasks', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          title: title || req.slice(0, 30),
          requirement: req,
          model: model,
          mock: mock,
          test_command: testCmd || null,
        }),
      });
      const data = await res.json();
      if (res.ok) {
        this.closeNewTaskModal();
        this.showToast(`任务已成功排队！后台 Job ID: ${data.job_id}`, 'success');
        this.switchTab('tasks');
      } else {
        alert(`创建失败: ${data.error}`);
      }
    } catch (e) {
      alert(`请求异常: ${e.message}`);
    }
  },

  showToast(message, type = 'info') {
    const container = document.getElementById('toastContainer');
    if (!container) return;

    const toast = document.createElement('div');
    const colorClass = type === 'success'
      ? 'bg-emerald-900/90 border-emerald-500 text-emerald-100'
      : type === 'error'
      ? 'bg-rose-900/90 border-rose-500 text-rose-100'
      : 'bg-slate-900/90 border-sky-500 text-sky-100';

    toast.className = `p-3 rounded-lg border shadow-xl text-xs font-medium flex items-center space-x-2 transition-all transform translate-y-2 opacity-0 duration-300 ${colorClass}`;
    toast.innerHTML = `<span>${message}</span>`;
    container.appendChild(toast);

    setTimeout(() => {
      toast.classList.remove('translate-y-2', 'opacity-0');
    }, 10);

    setTimeout(() => {
      toast.classList.add('opacity-0');
      setTimeout(() => toast.remove(), 300);
    }, 4000);
  },

  getStateBadge(state) {
    const map = {
      SUCCESS: 'badge-success',
      FAILED: 'badge-failed',
      PLANNING: 'badge-running',
      DEVELOPING: 'badge-running',
      TESTING: 'badge-running',
      REVIEWING: 'badge-purple',
      REPAIRING: 'badge-warning',
      ESCALATING: 'badge-warning',
      CODEX_BACKLOGGED: 'badge-warning',
      WAITING_APPROVAL: 'badge-warning',
      BUDGET_STOPPED: 'badge-failed',
      INIT: 'bg-slate-800 text-slate-400',
    };
    const cls = map[state] || 'bg-slate-800 text-slate-400';
    return `<span class="px-2 py-0.5 text-xs rounded font-mono font-medium ${cls}">${this.escapeHtml(state ?? '')}</span>`;
  },

  formatDate(isoStr) {
    if (!isoStr) return '-';
    try {
      const d = new Date(isoStr);
      return `${d.getMonth() + 1}/${d.getDate()} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}:${String(d.getSeconds()).padStart(2, '0')}`;
    } catch {
      return isoStr;
    }
  },

  escapeHtml(str) {
    if (!str) return '';
    return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  },
};

window.App = App;
