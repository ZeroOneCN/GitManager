/* GitManager 前端交互脚本：无框架、无构建，直接操作 DOM。 */
(() => {
  'use strict';

  // ------------------------------------------------------------------
  // 全局状态与元素引用
  // ------------------------------------------------------------------

  const state = {
    settings: null,
    repos: [],
    statuses: new Map(),
    loadingAll: false,
    audit: {
      offset: 0,
      limit: 50,
      total: 0,
      items: [],
      repoId: '',
      action: '',
      onlyFailed: false,
    },
    browse: null,
  };

  const el = {
    viewDashboard: document.getElementById('view-dashboard'),
    viewAudit: document.getElementById('view-audit'),
    tabs: Array.from(document.querySelectorAll('.tab')),
    summary: document.getElementById('summary-strip'),
    grid: document.getElementById('repo-grid'),
    empty: document.getElementById('empty-state'),
    emptyHint: document.getElementById('empty-hint'),
    btnRefresh: document.getElementById('btn-refresh'),
    btnAdd: document.getElementById('btn-add'),
    btnAddEmpty: document.getElementById('btn-add-empty'),
    toastStack: document.getElementById('toast-stack'),
    backdrop: document.getElementById('modal-backdrop'),
    modal: document.getElementById('modal'),
    modalTitle: document.getElementById('modal-title'),
    modalBody: document.getElementById('modal-body'),
    modalFoot: document.getElementById('modal-foot'),
    modalClose: document.getElementById('modal-close'),
    auditRepo: document.getElementById('audit-repo'),
    auditAction: document.getElementById('audit-action'),
    auditFailed: document.getElementById('audit-failed'),
    auditRefresh: document.getElementById('audit-refresh'),
    auditBody: document.getElementById('audit-body'),
    auditEmpty: document.getElementById('audit-empty'),
    auditRange: document.getElementById('audit-range'),
    auditPrev: document.getElementById('audit-prev'),
    auditNext: document.getElementById('audit-next'),
  };

  const ACTION_LABELS = {
    status: '读取状态',
    branch: '读取分支',
    head: '读取 HEAD',
    upstream: '读取上游',
    ahead_behind: '计算差距',
    last_commit: '读取最近提交',
    git_dir: '定位 git 目录',
    remote_list: '读取远端列表',
    detect_repo: '探测仓库',
    detect_bare: '探测裸仓库',
    fetch: '获取远端',
    pull: '拉取',
    stash_push: '自动 stash',
    stash_pop: '恢复 stash',
    push: '推送',
    push_set_upstream: '设置上游并推送',
    push_force: '强制推送',
    push_force_blocked: '强制推送被拒绝',
    stage: '暂存文件',
    commit: '提交',
  };

  // ------------------------------------------------------------------
  // 基础工具
  // ------------------------------------------------------------------

  function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>"']/g, (ch) => ({
      '&': '&amp;',
      '<': '&lt;',
      '>': '&gt;',
      '"': '&quot;',
      "'": '&#39;',
    }[ch]));
  }

  async function api(path, options = {}) {
    const opts = { method: options.method || 'GET', headers: {} };
    if (options.body !== undefined) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(options.body);
    }
    const res = await fetch(path, opts);
    const text = await res.text();
    let data = null;
    if (text) {
      try { data = JSON.parse(text); } catch (_) { data = { message: text }; }
    }
    if (!res.ok) {
      const error = new Error((data && data.message) || `请求失败（HTTP ${res.status}）`);
      error.status = res.status;
      error.payload = data || {};
      throw error;
    }
    return data || {};
  }

  function toast(message, type = 'info', timeout = 4500) {
    const node = document.createElement('div');
    node.className = `toast is-${type}`;
    node.innerHTML = `<div class="toast-text">${escapeHtml(message)}</div>
      <button class="toast-close" aria-label="关闭提示">×</button>`;
    node.querySelector('.toast-close').addEventListener('click', () => node.remove());
    el.toastStack.appendChild(node);
    if (timeout > 0) {
      window.setTimeout(() => node.remove(), timeout);
    }
  }

  function formatDateTime(iso) {
    if (!iso) return '—';
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return iso;
    const pad = (n) => String(n).padStart(2, '0');
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} `
      + `${pad(date.getHours())}:${pad(date.getMinutes())}`;
  }

  function formatDuration(ms) {
    const value = Number(ms) || 0;
    return value >= 1000 ? `${(value / 1000).toFixed(2)} s` : `${value} ms`;
  }

  function repoById(id) {
    return state.repos.find((repo) => repo.id === id) || null;
  }

  function statusById(id) {
    return state.statuses.get(id) || null;
  }

  function basename(path) {
    const parts = String(path || '').split(/[\\/]/).filter(Boolean);
    return parts.length ? parts[parts.length - 1] : String(path || '');
  }

  function healthOf(status) {
    if (!status) return { key: 'unknown', label: '读取中' };
    if (status.error) return { key: 'error', label: '不可用' };
    const dirty = status.dirty || {};
    const upstream = status.upstream_status;
    if ((dirty.conflicts || 0) > 0) {
      return { key: 'conflict', label: `冲突 ${dirty.conflicts}` };
    }
    if (upstream && upstream.ahead > 0 && upstream.behind > 0) {
      return { key: 'diverged', label: `已分叉 +${upstream.ahead}/-${upstream.behind}` };
    }
    if (upstream && upstream.behind > 0) {
      return { key: 'behind', label: `待拉取 ${upstream.behind}` };
    }
    if (upstream && upstream.ahead > 0) {
      return { key: 'ahead', label: `待推送 ${upstream.ahead}` };
    }
    if ((dirty.total || 0) > 0) {
      return { key: 'dirty', label: `工作区脏 ${dirty.total}` };
    }
    if (!status.upstream) {
      return { key: 'noupstream', label: '未设置上游' };
    }
    if (status.detached) {
      return { key: 'detached', label: 'Detached HEAD' };
    }
    if (!upstream) {
      return { key: 'unknown', label: '上游状态未知' };
    }
    return { key: 'synced', label: '已同步' };
  }

  // ------------------------------------------------------------------
  // 数据加载
  // ------------------------------------------------------------------

  async function loadSettings() {
    try {
      state.settings = await api('/api/settings');
    } catch (err) {
      toast(`读取配置失败：${err.message}`, 'error');
    }
  }

  async function loadRepos() {
    try {
      const data = await api('/api/repos');
      state.repos = data.items || [];
      for (const id of Array.from(state.statuses.keys())) {
        if (!state.repos.some((repo) => repo.id === id)) state.statuses.delete(id);
      }
      renderSummary();
      renderRepos();
      syncAuditRepoOptions();
    } catch (err) {
      toast(`读取仓库列表失败：${err.message}`, 'error');
    }
  }

  async function refreshAll(silent = false) {
    if (state.loadingAll) return;
    state.loadingAll = true;
    el.btnRefresh.classList.add('is-busy');
    el.btnRefresh.disabled = true;
    try {
      const data = await api('/api/status/all');
      const next = new Map();
      for (const status of data.items || []) next.set(status.repo.id, status);
      state.statuses = next;
      renderSummary();
      renderRepos();
      if (!silent) toast('已刷新全部仓库状态。', 'info', 2200);
    } catch (err) {
      toast(`刷新失败：${err.message}`, 'error');
    } finally {
      state.loadingAll = false;
      el.btnRefresh.classList.remove('is-busy');
      el.btnRefresh.disabled = false;
    }
  }

  async function refreshOne(id, silent = false) {
    try {
      const status = await api(`/api/repos/${id}/status`);
      state.statuses.set(id, status);
      renderSummary();
      renderRepos();
      return status;
    } catch (err) {
      if (!silent) toast(`刷新失败：${err.message}`, 'error');
      return null;
    }
  }

  async function fetchOne(id) {
    try {
      const result = await api(`/api/repos/${id}/fetch`, { method: 'POST' });
      if (result.status) state.statuses.set(id, result.status);
      renderSummary();
      renderRepos();
      toast(result.message || '已获取远端信息。', 'success');
    } catch (err) {
      toast(`获取远端失败：${err.message}`, 'error');
    }
  }

  // ------------------------------------------------------------------
  // 看板渲染
  // ------------------------------------------------------------------

  function renderSummary() {
    const statuses = state.repos.map((repo) => statusById(repo.id)).filter(Boolean);
    const behind = statuses.filter((st) => st.upstream_status && st.upstream_status.behind > 0).length;
    const ahead = statuses.filter((st) => st.upstream_status && st.upstream_status.ahead > 0).length;
    const conflicts = statuses.reduce((sum, st) => sum + ((st.dirty && st.dirty.conflicts) || 0), 0);
    const dirtyRepos = statuses.filter((st) => (st.dirty && st.dirty.total) > 0).length;

    if (!state.repos.length) {
      el.summary.hidden = true;
      el.empty.hidden = false;
      el.grid.hidden = true;
      if (state.settings && state.settings.allowed_roots) {
        el.emptyHint.textContent = `点击右上角「添加仓库」，在白名单目录中一次多选本地 Git 仓库。`
          + `当前白名单：${state.settings.allowed_roots.join('、')}`;
      }
      return;
    }

    el.summary.hidden = false;
    el.empty.hidden = true;
    el.grid.hidden = false;
    el.summary.innerHTML = [
      metric(state.repos.length, '仓库总数', ''),
      metric(behind, '待拉取仓库', behind > 0 ? 'is-info' : ''),
      metric(ahead, '待推送仓库', ahead > 0 ? 'is-warn' : ''),
      metric(conflicts, '冲突文件', conflicts > 0 ? 'is-alert' : ''),
      metric(dirtyRepos, '脏工作区仓库', dirtyRepos > 0 ? 'is-warn' : ''),
    ].join('');
  }

  function metric(value, label, cls) {
    return `<div class="summary-item ${cls}">
      <div class="summary-value">${value}<span class="unit"></span></div>
      <div class="summary-label">${label}</div>
    </div>`;
  }

  function renderRepos() {
    if (!state.repos.length) {
      el.grid.innerHTML = '';
      renderSummary();
      return;
    }
    el.grid.innerHTML = state.repos
      .map((repo) => cardHtml(repo, statusById(repo.id)))
      .join('');
    renderSummary();
  }

  function pillBranch(status) {
    if (!status) return '<span class="pill pill-branch">…</span>';
    if (status.branch) {
      return `<span class="pill pill-branch" title="当前分支">${escapeHtml(status.branch)}</span>`;
    }
    if (status.detached) {
      return `<span class="pill pill-branch" title="当前处于 detached HEAD">detached</span>`;
    }
    return '<span class="pill pill-branch">无提交</span>';
  }

  function cardHtml(repo, status) {
    const health = healthOf(status);
    const header = `<div class="card-head">
      <div class="repo-name" title="${escapeHtml(repo.name)}">${escapeHtml(repo.name)}</div>
      <div class="repo-pills">${pillBranch(status)}
        <span class="pill is-${health.key}"><span class="dot"></span>${escapeHtml(health.label)}</span>
      </div>
    </div>
    <div class="repo-path" title="${escapeHtml(repo.path)}">${escapeHtml(repo.path)}</div>`;

    if (!status) {
      return `<article class="repo-card" data-id="${repo.id}" data-health="unknown">
        ${header}
        <div class="card-loading">正在读取仓库状态…</div>
        <div class="card-foot">
          <div class="foot-meta"><span class="label">状态</span> 等待刷新</div>
          <div class="card-actions">
            <button class="btn btn-ghost btn-sm" data-act="refresh">刷新</button>
            <button class="btn btn-danger btn-sm" data-act="remove">移除</button>
          </div>
        </div>
      </article>`;
    }

    if (status.error) {
      return `<article class="repo-card" data-id="${repo.id}" data-health="error">
        ${header}
        <div class="error-banner"><strong>${escapeHtml(status.error.code || 'ERROR')}</strong>${escapeHtml(status.error.message || '仓库状态不可用')}</div>
        <div class="card-foot">
          <div class="foot-meta"><span class="label">检查时间</span> ${escapeHtml(formatDateTime(status.checked_at))}</div>
          <div class="card-actions">
            <button class="btn btn-ghost btn-sm" data-act="refresh">刷新</button>
            <button class="btn btn-danger btn-sm" data-act="remove">移除</button>
          </div>
        </div>
      </article>`;
    }

    const dirty = status.dirty || { total: 0, staged: 0, modified: 0, untracked: 0, conflicts: 0 };
    const upstream = status.upstream_status;
    const ahead = upstream ? upstream.ahead : null;
    const behind = upstream ? upstream.behind : null;

    const stats = `<div class="stat-row">
      ${statCell(ahead === null ? '—' : ahead, '待推送', ahead === null ? 'is-muted' : '', !status.upstream ? '未设置上游' : '')}
      ${statCell(behind === null ? '—' : behind, '待拉取', behind === null ? 'is-muted' : '')}
      ${statCell(dirty.total, '工作区改动', dirty.total ? 'is-warn' : '')}
      ${statCell(dirty.conflicts, '冲突文件', dirty.conflicts ? 'is-alert' : '')}
    </div>`;

    const dirtyBlock = dirty.total > 0 ? dirtyDetailHtml(status) : '';
    const commitBlock = commitHtml(status.last_commit);
    const fetchText = status.last_fetch_at
      ? `最后 fetch ${formatDateTime(status.last_fetch_at)}`
      : '从未 fetch';

    return `<article class="repo-card" data-id="${repo.id}" data-health="${health.key}">
      ${header}
      ${stats}
      ${dirtyBlock}
      ${commitBlock}
      <div class="card-foot">
        <div class="foot-meta"><span class="label">${escapeHtml(fetchText)}</span>${status.upstream ? ` · 上游 ${escapeHtml(status.upstream)}` : ''}</div>
        <div class="card-actions">
          <button class="btn btn-sub btn-sm" data-act="commit" ${dirty.total ? '' : 'disabled'} title="${dirty.total ? '勾选文件并提交' : '工作区干净，无需提交'}">提交</button>
          <button class="btn btn-ghost btn-sm" data-act="pull" title="git pull --ff-only">拉取</button>
          <button class="btn btn-ghost btn-sm" data-act="push" title="推送当前分支到上游">推送</button>
          <button class="btn btn-ghost btn-sm" data-act="fetch" title="git fetch --prune">获取远端</button>
          <button class="btn btn-ghost btn-sm" data-act="refresh" title="重新读取本地状态">刷新</button>
          <button class="btn btn-danger btn-sm" data-act="remove" title="仅从列表移除，不删除磁盘目录">移除</button>
        </div>
      </div>
    </article>`;
  }

  function statCell(value, label, cls, title = '') {
    return `<div class="stat" ${title ? `title="${escapeHtml(title)}"` : ''}>
      <div class="stat-value ${cls}">${escapeHtml(value)}</div>
      <div class="stat-label">${escapeHtml(label)}</div>
    </div>`;
  }

  function dirtyDetailHtml(status) {
    const dirty = status.dirty;
    const chips = [
      dirty.staged ? `<span class="chip is-staged"><span class="dot"></span>已暂存 ${dirty.staged}</span>` : '',
      dirty.modified ? `<span class="chip is-modified"><span class="dot"></span>已修改 ${dirty.modified}</span>` : '',
      dirty.untracked ? `<span class="chip is-untracked"><span class="dot"></span>未跟踪 ${dirty.untracked}</span>` : '',
      dirty.conflicts ? `<span class="chip is-conflict"><span class="dot"></span>冲突 ${dirty.conflicts}</span>` : '',
    ].join('');

    const groups = [
      ['已暂存', status.files.staged, ''],
      ['已修改（未暂存）', status.files.modified, ''],
      ['未跟踪', status.files.untracked, ''],
      ['冲突', status.files.conflicts, 'code-conflict'],
    ].filter(([, items]) => items && items.length)
      .map(([title, items, cls]) => `<div class="file-group">
        <div class="file-group-title">${escapeHtml(title)}</div>
        ${items.map((item) => `<div class="file-row">
          <span class="file-code ${cls}">${escapeHtml(item.code.replace(/\s+/g, '␣'))}</span>
          <span class="file-path" title="${escapeHtml(item.path)}">${escapeHtml(item.path)}</span>
        </div>`).join('')}
      </div>`).join('');

    return `<div class="dirty-block">
      <div class="dirty-chips">${chips}</div>
      <details class="dirty-details">
        <summary>查看文件明细（${dirty.total}）</summary>
        <div class="file-scroll">${groups || '<div class="commit-empty">暂无明细</div>'}</div>
      </details>
    </div>`;
  }

  function commitHtml(commit) {
    if (!commit) {
      return `<div class="commit-block">
        <div class="section-label">最近提交</div>
        <div class="commit-empty">暂无提交记录</div>
      </div>`;
    }
    return `<div class="commit-block">
      <div class="section-label">最近提交</div>
      <div class="commit-subject" title="${escapeHtml(commit.subject)}">${escapeHtml(commit.subject)}</div>
      <div class="commit-meta">
        <span>${escapeHtml(commit.author)}</span>
        <span>·</span>
        <span>${escapeHtml(formatDateTime(commit.date))}</span>
        <span class="sha" title="${escapeHtml(commit.sha)}">${escapeHtml(commit.short)}</span>
      </div>
    </div>`;
  }

  // ------------------------------------------------------------------
  // 卡片操作
  // ------------------------------------------------------------------

  async function handleCardAction(action, id) {
    if (action === 'refresh') return refreshOne(id);
    if (action === 'fetch') return fetchOne(id);
    if (action === 'pull') return openPullModal(id);
    if (action === 'push') return openPushModal(id);
    if (action === 'commit') return openCommitModal(id);
    if (action === 'remove') return removeRepo(id);
    return undefined;
  }

  async function removeRepo(id) {
    const repo = repoById(id);
    if (!repo) return;
    const ok = window.confirm(
      `仅从列表中移除「${repo.name}」的配置记录，不会删除磁盘目录。\n\n确定继续？`
    );
    if (!ok) return;
    try {
      const result = await api(`/api/repos/${id}`, { method: 'DELETE' });
      state.statuses.delete(id);
      await loadRepos();
      renderSummary();
      renderRepos();
      toast(result.message || '已移除仓库配置。', 'success');
    } catch (err) {
      toast(`移除失败：${err.message}`, 'error');
    }
  }

  // ------------------------------------------------------------------
  // 弹窗基础
  // ------------------------------------------------------------------

  function openModal({ title, body, foot, wide = false }) {
    el.modalTitle.textContent = title;
    el.modalBody.innerHTML = body || '';
    el.modalFoot.innerHTML = foot || '';
    el.modal.classList.toggle('modal-wide', Boolean(wide));
    el.backdrop.hidden = false;
    document.body.style.overflow = 'hidden';
  }

  function setModalBody(html) { el.modalBody.innerHTML = html; }
  function setModalFoot(html) { el.modalFoot.innerHTML = html; }

  function closeModal() {
    el.backdrop.hidden = true;
    el.modalBody.innerHTML = '';
    el.modalFoot.innerHTML = '';
    document.body.style.overflow = '';
    state.browse = null;
  }

  function showModalError(message) {
    const box = document.getElementById('modal-error');
    if (!box) { toast(message, 'error'); return; }
    box.hidden = false;
    box.textContent = message;
  }

  function clearModalError() {
    const box = document.getElementById('modal-error');
    if (box) { box.hidden = true; box.textContent = ''; }
  }

  function busyButton(button, busy) {
    if (!button) return;
    button.disabled = busy;
    button.classList.toggle('is-busy', busy);
  }

  // ------------------------------------------------------------------
  // 拉取
  // ------------------------------------------------------------------

  function openPullModal(id) {
    const repo = repoById(id);
    const status = statusById(id);
    if (!repo) return;
    const dirty = (status && status.dirty && status.dirty.total) || 0;
    const defaultStash = Boolean(state.settings && state.settings.pull_auto_stash_default);
    const upstreamText = status && status.upstream ? status.upstream : '未设置';

    openModal({
      title: `拉取 · ${repo.name}`,
      body: `
        <div class="repo-reference">
          <div><div class="name">${escapeHtml(repo.name)}</div><div class="path">${escapeHtml(repo.path)}</div></div>
        </div>
        <div class="kv"><span>当前分支</span><strong>${escapeHtml(status && status.branch ? status.branch : '未知')}</strong></div>
        <div class="kv"><span>上游分支</span><strong>${escapeHtml(upstreamText)}</strong></div>
        <div class="kv"><span>工作区</span><strong>${dirty ? `${dirty} 个未提交改动` : '干净'}</strong></div>
        <label class="check" style="margin-top:14px">
          <input type="checkbox" id="pull-stash" ${dirty && defaultStash ? 'checked' : ''}>
          <span>pull 前自动 stash（成功后自动 pop；失败则保留，不会静默丢弃）</span>
        </label>
        <div class="hint">本次操作固定执行 <code>git pull --ff-only</code>，不会产生合并提交。若未设置 upstream、本地与远端分叉或工作区有改动，将中止并返回原因。</div>
        <div class="error-box" id="modal-error" hidden></div>
      `,
      foot: `<button class="btn btn-ghost" id="pull-cancel">取消</button>
        <button class="btn btn-primary" id="pull-confirm">执行拉取</button>`,
    });

    document.getElementById('pull-cancel').addEventListener('click', closeModal);
    const confirmBtn = document.getElementById('pull-confirm');
    confirmBtn.addEventListener('click', async () => {
      clearModalError();
      busyButton(confirmBtn, true);
      const stashBox = document.getElementById('pull-stash');
      try {
        const result = await api(`/api/repos/${id}/pull`, {
          method: 'POST',
          body: { auto_stash: Boolean(stashBox && stashBox.checked) },
        });
        if (result.status) state.statuses.set(id, result.status);
        renderSummary();
        renderRepos();
        closeModal();
        let message = result.message || '拉取成功。';
        if (result.stash && result.stash.used) {
          message += result.stash.popped
            ? '\n自动 stash 已恢复（git stash pop）。'
            : '\n自动 stash 已保留，请手动处理。';
        }
        toast(message, 'success', 6000);
      } catch (err) {
        let message = err.message;
        if (err.payload && err.payload.output) message += `\n${err.payload.output}`;
        showModalError(message);
        if (err.payload && err.payload.dirty && stashBox) {
          stashBox.checked = false;
        }
        refreshOne(id, true);
      } finally {
        busyButton(confirmBtn, false);
      }
    });
  }

  // ------------------------------------------------------------------
  // 推送
  // ------------------------------------------------------------------

  async function openPushModal(id) {
    const repo = repoById(id);
    const status = statusById(id);
    if (!repo) return;

    let remotes = [];
    try {
      const data = await api(`/api/repos/${id}/remotes`);
      remotes = data.items || [];
    } catch (err) {
      toast(`读取远端列表失败：${err.message}`, 'error');
    }

    const upstream = status && status.upstream ? status.upstream : null;
    const branch = status && status.branch ? status.branch : null;
    const hasUpstream = Boolean(upstream);

    const upstreamSection = hasUpstream
      ? `<div class="kv"><span>推送目标</span><strong>${escapeHtml(branch || '当前分支')} → ${escapeHtml(upstream)}</strong></div>
         <div class="hint">仅推送当前分支到已配置的上游分支，默认不允许改写远端历史。</div>`
      : `<div class="kv"><span>当前分支</span><strong>${escapeHtml(branch || '未知')}</strong></div>
         <div class="kv"><span>上游分支</span><strong>未设置</strong></div>
         <div class="form-section">
           <label class="field"><span>选择远端（remote）</span>
             <select id="push-remote">
               ${remotes.length ? remotes.map((name) => `<option value="${escapeHtml(name)}">${escapeHtml(name)}</option>`).join('')
                 : '<option value="">（仓库没有配置 remote）</option>'}
             </select>
           </label>
         </div>
         <div class="warn-box">该分支尚未设置 upstream。确认后将执行 <code>git push --set-upstream &lt;remote&gt; ${escapeHtml(branch || '<branch>')}</code>。</div>`;

    const forceSection = hasUpstream
      ? `<details class="force-section">
          <summary class="title" style="cursor:pointer;list-style:none">高级：强制推送（风险操作）</summary>
          <label class="check"><input type="checkbox" id="force-enable">
            <span>我了解强制推送可能改写远端历史，需要二次确认</span></label>
          <label class="check"><input type="checkbox" id="force-ack">
            <span>我确认已与团队沟通，并接受 <code>--force-with-lease</code> 的后果</span></label>
          <div class="force-actions">
            <button class="btn btn-danger" id="force-run" disabled>强制推送</button>
          </div>
          <div class="hint">强制推送会写入审计日志（标记 forced），且只使用 <code>--force-with-lease</code>。</div>
        </details>`
      : '';

    openModal({
      title: `推送 · ${repo.name}`,
      body: `
        <div class="repo-reference">
          <div><div class="name">${escapeHtml(repo.name)}</div><div class="path">${escapeHtml(repo.path)}</div></div>
        </div>
        ${upstreamSection}
        ${forceSection}
        <div class="error-box" id="modal-error" hidden></div>
      `,
      foot: `<button class="btn btn-ghost" id="push-cancel">取消</button>
        <button class="btn btn-primary" id="push-confirm">${hasUpstream ? '推送当前分支' : '设置上游并推送'}</button>`,
    });

    document.getElementById('push-cancel').addEventListener('click', closeModal);

    const confirmBtn = document.getElementById('push-confirm');
    const runPush = async (force) => {
      clearModalError();
      const payload = { force, confirm_force: force };
      if (!hasUpstream) {
        const remoteSelect = document.getElementById('push-remote');
        const remote = remoteSelect ? remoteSelect.value : '';
        if (!remote) { showModalError('仓库没有可用的 remote，无法设置 upstream。'); return; }
        payload.set_upstream = true;
        payload.remote = remote;
      }
      busyButton(confirmBtn, true);
      try {
        const result = await api(`/api/repos/${id}/push`, { method: 'POST', body: payload });
        if (result.status) state.statuses.set(id, result.status);
        renderSummary();
        renderRepos();
        closeModal();
        toast(result.message || '推送成功。', 'success', 5000);
      } catch (err) {
        let message = err.message;
        if (err.payload && err.payload.output) message += `\n${err.payload.output}`;
        showModalError(message);
        refreshOne(id, true);
      } finally {
        busyButton(confirmBtn, false);
      }
    };

    confirmBtn.addEventListener('click', () => runPush(false));

    const forceEnable = document.getElementById('force-enable');
    const forceAck = document.getElementById('force-ack');
    const forceRun = document.getElementById('force-run');
    if (forceEnable && forceAck && forceRun) {
      const syncForce = () => {
        forceRun.disabled = !(forceEnable.checked && forceAck.checked);
      };
      forceEnable.addEventListener('change', syncForce);
      forceAck.addEventListener('change', syncForce);
      forceRun.addEventListener('click', () => {
        if (!(forceEnable.checked && forceAck.checked)) return;
        const ok = window.confirm(
          '二次确认：确定执行强制推送（--force-with-lease）？\n该操作可能改写远端历史，并会记录审计日志。'
        );
        if (!ok) return;
        runPush(true);
      });
    }
  }

  // ------------------------------------------------------------------
  // 提交
  // ------------------------------------------------------------------

  async function openCommitModal(id) {
    const repo = repoById(id);
    if (!repo) return;

    let changes;
    try {
      changes = await api(`/api/repos/${id}/changes`);
    } catch (err) {
      toast(`读取变更失败：${err.message}`, 'error');
      return;
    }

    const conflicts = changes.conflicts || [];
    const groupOrder = [
      ['staged', '已暂存'],
      ['modified', '已修改（未暂存）'],
      ['untracked', '未跟踪'],
      ['conflicts', '冲突'],
    ];

    // 同一文件可能同时出现在 staged 与 modified，按路径去重并合并分组
    const merged = new Map();
    for (const file of changes.files || []) {
      const entry = merged.get(file.path) || { path: file.path, code: file.code, groups: new Set() };
      entry.groups.add(file.group);
      if (file.group === 'conflicts') entry.code = file.code;
      merged.set(file.path, entry);
    }

    const groupsHtml = groupOrder.map(([group, title]) => {
      const items = Array.from(merged.values()).filter((entry) => entry.groups.has(group));
      if (!items.length) return '';
      return `<div class="commit-group">
        <div class="commit-group-title"><span class="title">${escapeHtml(title)} · ${items.length}</span></div>
        ${items.map((entry) => `<label class="commit-file">
          <input type="checkbox" class="commit-check" value="${escapeHtml(entry.path)}" ${group === 'conflicts' ? 'disabled' : ''}>
          <span class="code ${group === 'conflicts' ? 'is-conflict' : ''}">${escapeHtml(entry.code.replace(/\s+/g, '␣'))}</span>
          <span class="path" title="${escapeHtml(entry.path)}">${escapeHtml(entry.path)}</span>
        </label>`).join('')}
      </div>`;
    }).join('');

    const conflictBanner = conflicts.length
      ? `<div class="error-box">存在 ${conflicts.length} 个未解决的冲突文件，需先手动解决后才能提交。</div>`
      : '';

    openModal({
      title: `提交 · ${repo.name}`,
      wide: true,
      body: `
        <div class="repo-reference">
          <div><div class="name">${escapeHtml(repo.name)}</div><div class="path">${escapeHtml(repo.path)}</div></div>
        </div>
        ${conflictBanner}
        <div class="form-section">
          <div class="commit-toolbar">
            <div class="section-label">选择要提交的文件（${merged.size}）</div>
            <label class="check"><input type="checkbox" id="commit-all"><span>全选</span></label>
          </div>
          <div class="commit-groups">${groupsHtml || '<div class="dir-empty">工作区没有可提交的变更。</div>'}</div>
        </div>
        <div class="form-section">
          <label class="field"><span>提交信息</span>
            <textarea class="textarea" id="commit-message" placeholder="例如：fix: 修复仓库状态解析在重命名文件时丢失路径" maxlength="5000"></textarea>
          </label>
          <div class="message-meta"><span>仅提交勾选的文件</span><span id="commit-count">0 字</span></div>
        </div>
        <div class="error-box" id="modal-error" hidden></div>
      `,
      foot: `<button class="btn btn-ghost" id="commit-cancel">取消</button>
        <button class="btn btn-primary" id="commit-submit" ${conflicts.length || !merged.size ? 'disabled' : ''}>提交选中文件</button>`,
    });

    document.getElementById('commit-cancel').addEventListener('click', closeModal);

    const allBox = document.getElementById('commit-all');
    const checks = Array.from(document.querySelectorAll('.commit-check')).filter((box) => !box.disabled);
    const selectedCount = () => checks.filter((box) => box.checked).length;

    allBox.addEventListener('change', () => {
      checks.forEach((box) => { box.checked = allBox.checked; });
      if (!checks.length) allBox.checked = false;
    });
    checks.forEach((box) => box.addEventListener('change', () => {
      allBox.checked = selectedCount() === checks.length;
    }));

    const messageInput = document.getElementById('commit-message');
    const countLabel = document.getElementById('commit-count');
    messageInput.addEventListener('input', () => {
      countLabel.textContent = `${messageInput.value.length} 字`;
    });
    messageInput.focus();

    const submitBtn = document.getElementById('commit-submit');
    submitBtn.addEventListener('click', async () => {
      clearModalError();
      const files = selectedCount() ? checks.filter((box) => box.checked).map((box) => box.value) : [];
      const message = messageInput.value.trim();
      if (!files.length) { showModalError('请至少勾选一个文件。'); return; }
      if (!message) { showModalError('提交信息不能为空。'); return; }
      busyButton(submitBtn, true);
      try {
        const result = await api(`/api/repos/${id}/commit`, {
          method: 'POST',
          body: { files, message },
        });
        closeModal();
        toast(`提交成功：${result.commit.short} ${result.commit.subject}`, 'success', 6000);
        await refreshOne(id, true);
      } catch (err) {
        let text = err.message;
        if (err.payload && err.payload.output) text += `\n${err.payload.output}`;
        showModalError(text);
        refreshOne(id, true);
      } finally {
        busyButton(submitBtn, false);
      }
    });
  }

  // ------------------------------------------------------------------
  // 添加仓库（目录浏览 + 多选）
  // ------------------------------------------------------------------

  async function openAddRepoModal() {
    state.browse = {
      mode: 'browse',
      roots: [],
      path: null,
      parent: null,
      entries: [],
      isRepo: false,
      selected: new Map(),
      manual: '',
    };
    openModal({
      title: '添加仓库',
      wide: true,
      body: '<div class="card-loading">正在读取白名单目录…</div>',
      foot: '<button class="btn btn-ghost" id="add-cancel">取消</button>',
    });
    document.getElementById('add-cancel').addEventListener('click', closeModal);

    try {
      const data = await api('/api/fs/list');
      state.browse.roots = data.roots || [];
      if (state.browse.roots.length) {
        await browseTo(state.browse.roots[0].path);
      } else {
        renderAddRepo();
      }
    } catch (err) {
      showModalError(`读取白名单目录失败：${err.message}`);
    }
  }

  async function browseTo(path) {
    const browse = state.browse;
    if (!browse || !path) return;
    try {
      const data = await api(`/api/fs/list?path=${encodeURIComponent(path)}`);
      browse.roots = data.roots || browse.roots;
      browse.path = data.path;
      browse.parent = data.parent;
      browse.entries = data.entries || [];
      browse.isRepo = Boolean(data.is_repo);
      renderAddRepo();
    } catch (err) {
      if (browse.mode === 'browse') {
        showModalError(`打开目录失败：${err.message}`);
      }
    }
  }

  function addRepoBodyHtml() {
    const browse = state.browse;
    if (!browse) return '';
    const modeTabs = `<div class="mode-tabs">
      <button class="mode-tab ${browse.mode === 'browse' ? 'is-active' : ''}" data-mode="browse">浏览目录</button>
      <button class="mode-tab ${browse.mode === 'manual' ? 'is-active' : ''}" data-mode="manual">手动输入路径</button>
    </div>`;

    if (browse.mode === 'manual') {
      return `${modeTabs}
        <div class="hint">每行一个本地仓库绝对路径（必须是 Git 仓库，且位于白名单根目录内）。</div>
        <textarea class="textarea" id="manual-paths" style="min-height:180px;margin-top:10px"
          placeholder="E:\\Code\\MyRepo&#10;E:\\Code\\AnotherRepo">${escapeHtml(browse.manual)}</textarea>
        <div class="error-box" id="modal-error" hidden></div>`;
    }

    const roots = browse.roots.length
      ? `<div class="root-chips">${browse.roots.map((root) => `<button class="root-chip ${browse.path && normalizePath(browse.path) === normalizePath(root.path) ? 'is-active' : ''}" data-root="${escapeHtml(root.path)}" title="${escapeHtml(root.path)}">${escapeHtml(root.name || root.path)}</button>`).join('')}</div>`
      : '<div class="warn-box">配置中没有可用的白名单根目录，请在 .env 中设置 GITMANAGER_ALLOWED_ROOTS。</div>';

    const entries = browse.entries.length
      ? browse.entries.map((entry) => {
        const selected = browse.selected.has(entry.path);
        return `<div class="dir-row ${selected ? 'is-selected' : ''}" data-row="${escapeHtml(entry.path)}">
          <input type="checkbox" data-pick="${escapeHtml(entry.path)}" data-name="${escapeHtml(entry.name)}" ${selected ? 'checked' : ''} aria-label="选择 ${escapeHtml(entry.name)}">
          <button type="button" class="dir-name" data-enter="${escapeHtml(entry.path)}" title="打开 ${escapeHtml(entry.path)}">${escapeHtml(entry.name)}</button>
          ${entry.is_repo ? '<span class="dir-badge">Git 仓库</span>' : ''}
        </div>`;
      }).join('')
      : '<div class="dir-empty">当前目录没有子目录。</div>';

    const repoHint = browse.isRepo
      ? `<div class="info-box">当前目录本身是 Git 仓库，可直接选择它。
          <button class="btn-link" data-pick-current="${escapeHtml(browse.path)}">选择当前目录</button></div>`
      : '';

    const selectedList = Array.from(browse.selected.keys()).map((path) => `<span class="selected-chip" title="${escapeHtml(path)}">
        <span>${escapeHtml(path)}</span>
        <button type="button" data-unpick="${escapeHtml(path)}" aria-label="取消选择">×</button>
      </span>`).join('');

    return `${modeTabs}
      ${roots}
      <div class="browser-toolbar">
        <button class="btn btn-ghost btn-sm" id="browse-parent" ${browse.parent ? '' : 'disabled'}>上一级</button>
        <span class="browser-path" title="${escapeHtml(browse.path || '')}">${escapeHtml(browse.path || '根目录')}</span>
        <button class="btn btn-ghost btn-sm" id="browse-refresh">重新加载</button>
      </div>
      ${repoHint}
      <div class="dir-list">${entries}</div>
      <div class="selected-bar">
        <div class="selected-head"><span>已选目录（<span id="selected-count">${browse.selected.size}</span>）</span>
          <button class="btn-link" type="button" id="selected-clear" ${browse.selected.size ? '' : 'disabled'}>清空</button></div>
        <div class="selected-chips" id="selected-chips">${selectedList || '<span class="muted">尚未选择目录，可勾选多个后一次添加。</span>'}</div>
      </div>
      <div class="hint">单击目录名进入下一级；勾选目录可跨层级多选。添加时会自动定位到仓库根目录。</div>
      <div class="error-box" id="modal-error" hidden></div>`;
  }

  function normalizePath(path) {
    return String(path || '').replace(/[\\/]+/g, '\\').replace(/\\$/, '').toLowerCase();
  }

  function addRepoFootHtml() {
    const browse = state.browse;
    if (!browse) return '';
    const count = browse.selected.size;
    const label = browse.mode === 'manual' ? '添加路径' : `添加选中（${count}）`;
    const disabled = browse.mode === 'manual' ? false : !count;
    return `<button class="btn btn-ghost" id="add-cancel">取消</button>
      <button class="btn btn-primary" id="add-submit" ${disabled ? 'disabled' : ''}>${escapeHtml(label)}</button>`;
  }

  function renderAddRepo() {
    setModalBody(addRepoBodyHtml());
    setModalFoot(addRepoFootHtml());
    bindAddRepoEvents();
  }

  function bindAddRepoEvents() {
    const browse = state.browse;
    if (!browse) return;

    document.querySelectorAll('[data-mode]').forEach((button) => {
      button.addEventListener('click', () => {
        browse.mode = button.dataset.mode;
        clearModalError();
        renderAddRepo();
      });
    });

    const cancelBtn = document.getElementById('add-cancel');
    if (cancelBtn) cancelBtn.addEventListener('click', closeModal);

    document.querySelectorAll('[data-root]').forEach((button) => {
      button.addEventListener('click', () => browseTo(button.dataset.root));
    });

    const parentBtn = document.getElementById('browse-parent');
    if (parentBtn) parentBtn.addEventListener('click', () => browseTo(browse.parent));

    const refreshBtn = document.getElementById('browse-refresh');
    if (refreshBtn) refreshBtn.addEventListener('click', () => browseTo(browse.path || undefined));

    document.querySelectorAll('[data-enter]').forEach((button) => {
      button.addEventListener('click', () => browseTo(button.dataset.enter));
    });

    document.querySelectorAll('[data-pick]').forEach((box) => {
      box.addEventListener('change', () => {
        const path = box.dataset.pick;
        if (box.checked) browse.selected.set(path, box.dataset.name || basename(path));
        else browse.selected.delete(path);
        const row = box.closest('.dir-row');
        if (row) row.classList.toggle('is-selected', box.checked);
        updateSelectionUi();
      });
    });

    const pickCurrent = document.querySelector('[data-pick-current]');
    if (pickCurrent) {
      pickCurrent.addEventListener('click', () => {
        const path = pickCurrent.dataset.pickCurrent;
        browse.selected.set(path, basename(path));
        renderAddRepo();
      });
    }

    const clearBtn = document.getElementById('selected-clear');
    if (clearBtn) {
      clearBtn.addEventListener('click', () => {
        browse.selected.clear();
        renderAddRepo();
      });
    }

    document.querySelectorAll('[data-unpick]').forEach((button) => {
      button.addEventListener('click', () => {
        browse.selected.delete(button.dataset.unpick);
        renderAddRepo();
      });
    });

    const manualInput = document.getElementById('manual-paths');
    if (manualInput) {
      manualInput.addEventListener('input', () => { browse.manual = manualInput.value; });
    }

    const submitBtn = document.getElementById('add-submit');
    if (submitBtn) submitBtn.addEventListener('click', submitAddRepos);
  }

  function updateSelectionUi() {
    const browse = state.browse;
    if (!browse) return;
    const count = document.getElementById('selected-count');
    if (count) count.textContent = String(browse.selected.size);
    const chips = document.getElementById('selected-chips');
    if (chips) {
      const list = Array.from(browse.selected.keys()).map((path) => `<span class="selected-chip" title="${escapeHtml(path)}">
        <span>${escapeHtml(path)}</span>
        <button type="button" data-unpick="${escapeHtml(path)}" aria-label="取消选择">×</button>
      </span>`).join('');
      chips.innerHTML = list || '<span class="muted">尚未选择目录，可勾选多个后一次添加。</span>';
      chips.querySelectorAll('[data-unpick]').forEach((button) => {
        button.addEventListener('click', () => {
          browse.selected.delete(button.dataset.unpick);
          renderAddRepo();
        });
      });
    }
    const submit = document.getElementById('add-submit');
    if (submit && browse.mode === 'browse') {
      submit.textContent = `添加选中（${browse.selected.size}）`;
      submit.disabled = browse.selected.size === 0;
    }
  }

  async function submitAddRepos() {
    const browse = state.browse;
    if (!browse) return;
    let paths = [];
    if (browse.mode === 'manual') {
      paths = (browse.manual || '').split('\n').map((line) => line.trim()).filter(Boolean);
    } else {
      paths = Array.from(browse.selected.keys());
    }
    if (!paths.length) {
      showModalError('请先选择或填写至少一个仓库路径。');
      return;
    }

    const submitBtn = document.getElementById('add-submit');
    busyButton(submitBtn, true);
    clearModalError();
    try {
      const result = await api('/api/repos', { method: 'POST', body: { paths } });
      renderAddResults(result);
      const added = result.added || 0;
      const failed = (result.results || []).filter((item) => !item.ok).length;
      if (added > 0) {
        toast(`已添加 ${added} 个仓库${failed ? `，${failed} 条失败` : ''}。`, failed ? 'warn' : 'success');
        await loadRepos();
        refreshAll(true);
      } else {
        toast(failed ? '没有新增仓库，请查看失败原因。' : '没有新的仓库。', failed ? 'warn' : 'info');
      }
    } catch (err) {
      showModalError(`添加失败：${err.message}`);
      busyButton(submitBtn, false);
    }
  }

  function renderAddResults(result) {
    const items = result.results || [];
    const list = items.map((item) => `<div class="result-item ${item.ok ? 'ok' : 'fail'}">
      <span class="mark">${item.ok ? '成功' : '失败'}</span>
      <div>
        <div class="path">${escapeHtml(item.path)}</div>
        <div class="msg">${escapeHtml(item.message || item.error || '')}</div>
      </div>
    </div>`).join('');

    setModalBody(`<div class="result-list">${list || '<div class="dir-empty">没有结果。</div>'}</div>`);
    setModalFoot(`<button class="btn btn-ghost" id="add-again">继续添加</button>
      <button class="btn btn-primary" id="add-done">完成</button>`);
    document.getElementById('add-done').addEventListener('click', closeModal);
    document.getElementById('add-again').addEventListener('click', () => {
      state.browse.mode = 'browse';
      renderAddRepo();
    });
    document.getElementById('add-again').focus();
  }

  // ------------------------------------------------------------------
  // 审计视图
  // ------------------------------------------------------------------

  async function switchTab(name) {
    state.activeTab = name;
    const isAudit = name === 'audit';
    el.viewDashboard.hidden = isAudit;
    el.viewAudit.hidden = !isAudit;
    el.tabs.forEach((tab) => {
      const active = tab.dataset.tab === name;
      tab.classList.toggle('is-active', active);
      tab.setAttribute('aria-selected', active ? 'true' : 'false');
    });
    if (isAudit) {
      await loadAuditActions();
      syncAuditRepoOptions();
      await loadAudit();
    }
  }

  function syncAuditRepoOptions() {
    if (!el.auditRepo) return;
    const current = state.audit.repoId;
    const options = ['<option value="">全部仓库</option>'].concat(
      state.repos.map((repo) => `<option value="${repo.id}" ${String(repo.id) === String(current) ? 'selected' : ''}>${escapeHtml(repo.name)}</option>`)
    );
    el.auditRepo.innerHTML = options.join('');
  }

  async function loadAuditActions() {
    try {
      const data = await api('/api/audit/actions');
      const current = state.audit.action;
      el.auditAction.innerHTML = ['<option value="">全部动作</option>'].concat(
        (data.items || []).map((action) => `<option value="${escapeHtml(action)}" ${action === current ? 'selected' : ''}>${escapeHtml(ACTION_LABELS[action] || action)}</option>`)
      ).join('');
    } catch (err) {
      toast(`读取动作列表失败：${err.message}`, 'error');
    }
  }

  async function loadAudit() {
    const params = new URLSearchParams();
    params.set('limit', String(state.audit.limit));
    params.set('offset', String(state.audit.offset));
    if (state.audit.repoId) params.set('repo_id', state.audit.repoId);
    if (state.audit.action) params.set('action', state.audit.action);
    if (state.audit.onlyFailed) params.set('only_failed', 'true');

    el.auditRefresh.classList.add('is-busy');
    try {
      const data = await api(`/api/audit?${params.toString()}`);
      state.audit.total = data.total || 0;
      state.audit.items = data.items || [];
      renderAudit();
    } catch (err) {
      toast(`读取审计失败：${err.message}`, 'error');
    } finally {
      el.auditRefresh.classList.remove('is-busy');
    }
  }

  function renderAudit() {
    const items = state.audit.items;
    if (!items.length) {
      el.auditBody.innerHTML = '';
      el.auditEmpty.hidden = false;
      el.auditRange.textContent = '共 0 条记录';
      el.auditPrev.disabled = true;
      el.auditNext.disabled = true;
      return;
    }
    el.auditEmpty.hidden = true;

    el.auditBody.innerHTML = items.map((item) => {
      const repoName = (() => {
        const repo = state.repos.find((candidate) => candidate.id === item.repo_id);
        return repo ? repo.name : basename(item.repo_path) || '—';
      })();
      const actionLabel = ACTION_LABELS[item.action] || item.action;
      const summary = item.output_summary || '（无输出）';
      return `<tr class="audit-row ${item.ok ? '' : 'is-failed'}" data-aid="${item.id}">
        <td class="cell-time">${escapeHtml(formatDateTime(item.ts))}</td>
        <td class="cell-repo" title="${escapeHtml(item.repo_path || '')}">${escapeHtml(repoName)}</td>
        <td class="cell-action">${escapeHtml(actionLabel)}${item.forced ? '<span class="badge forced">强制</span>' : ''}</td>
        <td class="cell-command" title="${escapeHtml(item.command)}">${escapeHtml(item.command)}</td>
        <td><span class="badge exit">${escapeHtml(item.exit_code)}</span></td>
        <td class="cell-duration">${escapeHtml(formatDuration(item.duration_ms))}</td>
        <td><span class="badge ${item.ok ? 'ok' : 'fail'}">${item.ok ? '成功' : '失败'}</span></td>
        <td class="cell-summary" title="${escapeHtml(summary)}">${escapeHtml(summary.split('\n')[0] || '—')}</td>
      </tr>
      <tr class="audit-detail-row" hidden><td colspan="8">
        <div class="audit-detail">
          <div class="label">完整命令</div>
          <pre>${escapeHtml(item.command)}</pre>
          <div class="label">输出摘要</div>
          <pre>${escapeHtml(summary)}</pre>
        </div>
      </td></tr>`;
    }).join('');

    const from = state.audit.offset + 1;
    const to = Math.min(state.audit.offset + items.length, state.audit.total);
    el.auditRange.textContent = `第 ${from}-${to} 条 / 共 ${state.audit.total} 条`;
    el.auditPrev.disabled = state.audit.offset <= 0;
    el.auditNext.disabled = state.audit.offset + items.length >= state.audit.total;
  }

  // ------------------------------------------------------------------
  // 事件绑定与初始化
  // ------------------------------------------------------------------

  function bindEvents() {
    el.tabs.forEach((tab) => tab.addEventListener('click', () => switchTab(tab.dataset.tab)));

    el.btnRefresh.addEventListener('click', () => refreshAll(false));
    el.btnAdd.addEventListener('click', openAddRepoModal);
    el.btnAddEmpty.addEventListener('click', openAddRepoModal);

    el.grid.addEventListener('click', (event) => {
      const button = event.target.closest('[data-act]');
      if (!button) return;
      const card = event.target.closest('.repo-card');
      if (!card) return;
      handleCardAction(button.dataset.act, Number(card.dataset.id));
    });

    el.modalClose.addEventListener('click', closeModal);
    el.backdrop.addEventListener('click', (event) => {
      if (event.target === el.backdrop) closeModal();
    });
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && !el.backdrop.hidden) closeModal();
    });

    el.auditRefresh.addEventListener('click', () => {
      state.audit.offset = 0;
      loadAudit();
    });
    el.auditRepo.addEventListener('change', () => {
      state.audit.repoId = el.auditRepo.value;
      state.audit.offset = 0;
      loadAudit();
    });
    el.auditAction.addEventListener('change', () => {
      state.audit.action = el.auditAction.value;
      state.audit.offset = 0;
      loadAudit();
    });
    el.auditFailed.addEventListener('change', () => {
      state.audit.onlyFailed = el.auditFailed.checked;
      state.audit.offset = 0;
      loadAudit();
    });
    el.auditPrev.addEventListener('click', () => {
      if (state.audit.offset > 0) {
        state.audit.offset = Math.max(0, state.audit.offset - state.audit.limit);
        loadAudit();
      }
    });
    el.auditNext.addEventListener('click', () => {
      if (state.audit.offset + state.audit.items.length < state.audit.total) {
        state.audit.offset += state.audit.limit;
        loadAudit();
      }
    });
    el.auditBody.addEventListener('click', (event) => {
      const row = event.target.closest('.audit-row');
      if (!row) return;
      const detail = row.nextElementSibling;
      if (detail && detail.classList.contains('audit-detail-row')) {
        detail.hidden = !detail.hidden;
      }
    });
  }

  async function init() {
    bindEvents();
    await loadSettings();
    await loadRepos();
    await refreshAll(true);
  }

  init();
})();
