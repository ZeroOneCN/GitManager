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
    modal: { dismissible: true, onClose: null, returnFocus: null },
    confirm: { resolve: null, dismissible: true, returnFocus: null },
    audit: {
      page: 1,
      pageSize: 10,
      total: 0,
      totalPages: 0,
      from: 0,
      to: 0,
      items: [],
      repoId: '',
      action: '',
      onlyFailed: false,
    },
    browse: null,
    scan: null,
    roots: null,
    ssh: null,
    sshDraft: { editingId: null, testOutput: null },
  };

  // 全局禁用原生对话框：所有确认/提示一律走自研 Modal。
  window.__nativeDialogCalls = { confirm: 0, alert: 0, prompt: 0 };
  window.confirm = function () {
    window.__nativeDialogCalls.confirm += 1;
    console.warn('window.confirm 已被禁用，请使用自研 Modal。');
    return false;
  };
  window.alert = function () {
    window.__nativeDialogCalls.alert += 1;
    console.warn('window.alert 已被禁用，请使用自研 Modal。');
  };
  window.prompt = function () {
    window.__nativeDialogCalls.prompt += 1;
    console.warn('window.prompt 已被禁用，请使用自研 Modal。');
    return null;
  };

  const el = {
    viewDashboard: document.getElementById('view-dashboard'),
    viewAudit: document.getElementById('view-audit'),
    tabs: Array.from(document.querySelectorAll('.tab')),
    summary: document.getElementById('summary-strip'),
    summaryNote: document.getElementById('summary-note'),
    grid: document.getElementById('repo-grid'),
    empty: document.getElementById('empty-state'),
    emptyHint: document.getElementById('empty-hint'),
    btnRefresh: document.getElementById('btn-refresh'),
    btnAdd: document.getElementById('btn-add'),
    btnAddEmpty: document.getElementById('btn-add-empty'),
    btnRoot: document.getElementById('btn-root'),
    btnSsh: document.getElementById('btn-ssh'),
    btnScan: document.getElementById('btn-scan'),
    btnCredentials: document.getElementById('btn-credentials'),
    identityStrip: document.getElementById('identity-strip'),
    toastStack: document.getElementById('toast-stack'),
    backdrop: document.getElementById('modal-backdrop'),
    modal: document.getElementById('modal'),
    modalTitle: document.getElementById('modal-title'),
    modalBody: document.getElementById('modal-body'),
    modalFoot: document.getElementById('modal-foot'),
    modalClose: document.getElementById('modal-close'),
    confirmBackdrop: document.getElementById('confirm-backdrop'),
    confirmModal: document.getElementById('confirm-modal'),
    confirmTitle: document.getElementById('confirm-title'),
    confirmBody: document.getElementById('confirm-body'),
    confirmFoot: document.getElementById('confirm-foot'),
    confirmClose: document.getElementById('confirm-close'),
    auditRepo: document.getElementById('audit-repo'),
    auditAction: document.getElementById('audit-action'),
    auditFailed: document.getElementById('audit-failed'),
    auditRefresh: document.getElementById('audit-refresh'),
    auditBody: document.getElementById('audit-body'),
    auditEmpty: document.getElementById('audit-empty'),
    auditRange: document.getElementById('audit-range'),
    auditFirst: document.getElementById('audit-first'),
    auditPrev: document.getElementById('audit-prev'),
    auditNext: document.getElementById('audit-next'),
    auditLast: document.getElementById('audit-last'),
    auditPage: document.getElementById('audit-page'),
    auditGoto: document.getElementById('audit-goto'),
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
    ssh_test: 'SSH 连接测试',
    scan: '扫描仓库',
    ls_files: '读取索引',
    check_ignore: '忽略规则探测',
    ignore_append: '追加 .gitignore',
    mute: '本地静音',
    mute_remove: '取消静音',
    untrack: '取消跟踪',
    remote_set_url: '修改远端地址',
    credential_delete: '清除 HTTPS 凭据',
    credential_login: 'HTTPS 凭据登录',
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

  /**
   * 复制文本到剪贴板（无剪贴板权限时降级为隐藏 textarea + execCommand）。
   * 参数：value 待复制文本；label 提示用名称，默认“内容”。
   * 返回值：Promise<boolean>，是否复制成功。
   */
  async function copyText(value, label = '内容') {
    const text = String(value ?? '');
    if (!text) return false;
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(text);
        toast(`已复制${label}。`, 'success', 2200);
        return true;
      }
    } catch (_) {
      // 忽略权限错误，继续走降级方案
    }
    try {
      const area = document.createElement('textarea');
      area.value = text;
      area.setAttribute('readonly', 'readonly');
      area.style.position = 'fixed';
      area.style.opacity = '0';
      document.body.appendChild(area);
      area.select();
      const ok = document.execCommand('copy');
      area.remove();
      if (ok) {
        toast(`已复制${label}。`, 'success', 2200);
        return true;
      }
    } catch (_) {
      // 继续走失败提示
    }
    toast(`复制${label}失败，请手动选择文本。`, 'error');
    return false;
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
      const pending = dirty.pending !== undefined ? dirty.pending : dirty.total;
      if (pending > 0) return { key: 'dirty', label: `工作区脏 ${pending}` };
      // 全部变更被本地静音：绝不能显示为“已同步/干净”
      return { key: 'muted', label: `含静音 ${dirty.muted || 0}` };
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
      renderIdentityStrip();
    } catch (err) {
      toast(`读取配置失败：${err.message}`, 'error');
    }
  }

  function renderIdentityStrip() {
    if (!el.identityStrip) return;
    const settings = state.settings || {};
    const identity = settings.default_ssh_identity;
    const roots = settings.allowed_roots || [];
    const serviceUrl = settings.service_url || `http://${settings.host || '127.0.0.1'}:${settings.port || 8765}`;
    el.identityStrip.hidden = false;
    el.identityStrip.innerHTML = `<span class="identity-dot"></span>
      <span class="identity-label">访问地址</span>
      <code class="full-text service-url" id="service-url">${escapeHtml(serviceUrl)}</code>
      <button class="btn-link" data-copy="${escapeHtml(serviceUrl)}" data-copy-label="访问地址">复制</button>
      <span class="identity-sep">·</span>
      <span class="identity-label">推送身份</span>
      <span class="identity-name">${escapeHtml(identity ? identity.name : '系统 SSH 默认')}</span>
      ${identity ? '<span class="badge source-default">全局默认</span>' : ''}
      <span class="identity-sep">·</span>
      <span class="identity-label">白名单根目录</span>
      <span class="identity-name">${escapeHtml(roots.length ? roots.join('、') : '未配置')}</span>
      <button class="btn-link" id="identity-manage">管理</button>`;
    const manage = document.getElementById('identity-manage');
    if (manage) manage.addEventListener('click', openSshModal);
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
    const pending = statuses.reduce((sum, st) => {
      const dirty = st.dirty || {};
      return sum + (dirty.pending !== undefined ? dirty.pending : (dirty.total || 0));
    }, 0);
    const muted = statuses.reduce((sum, st) => sum + ((st.dirty && st.dirty.muted) || 0), 0);
    const trackedIgnored = statuses.reduce((sum, st) => sum + ((st.dirty && st.dirty.tracked_ignored) || 0), 0);

    if (!state.repos.length) {
      el.summary.hidden = true;
      if (el.summaryNote) el.summaryNote.hidden = true;
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
      metric(pending, '待处理变更数', pending > 0 ? 'is-warn' : ''),
      metric(muted, '已静音数', muted > 0 ? 'is-muted' : ''),
      metric(trackedIgnored, '已跟踪但被 ignore', trackedIgnored > 0 ? 'is-alert' : ''),
    ].join('');
    if (el.summaryNote) {
      el.summaryNote.hidden = muted === 0;
      el.summaryNote.innerHTML = muted
        ? `<span class="dot"></span>有 ${muted} 项变更被本地静音，仓库并未真正干净；展开卡片中的「已静音的变更」可恢复。`
        : '';
    }
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

  /**
   * 返回远端协议的中文标签与样式类。
   * 参数：protocol 协议标识（https/ssh/local/unknown）。
   * 返回值：{label, cls}。
   */
  function protocolMeta(protocol) {
    if (protocol === 'https') return { label: 'HTTPS', cls: 'is-https' };
    if (protocol === 'ssh') return { label: 'SSH', cls: 'is-ssh' };
    if (protocol === 'local') return { label: '本地', cls: 'is-local' };
    return { label: '未知', cls: 'is-muted' };
  }

  /**
   * 渲染仓库远端地址（完整展示，不省略，附带协议徽标与复制按钮）。
   * 参数：status 仓库状态对象（可能为 null）。
   * 返回值：HTML 字符串；无远端时为空串。
   */
  function remoteHtml(status) {
    if (!status || !Array.isArray(status.remotes) || !status.remotes.length) return '';
    return status.remotes.map((remote) => {
      const url = remote.push_url || remote.fetch_url || '';
      if (!url) return '';
      const meta = protocolMeta(remote.protocol);
      return `<div class="remote-line">
        <span class="label">${escapeHtml(remote.name)}</span>
        <span class="protocol-badge ${meta.cls}">${meta.label}</span>
        <code class="full-text">${escapeHtml(url)}</code>
        <button class="btn-link" data-copy="${escapeHtml(url)}" data-copy-label="远端地址">复制</button>
      </div>`;
    }).join('');
  }

  /**
   * 渲染远端的协议/身份提示（仅在有意义时显示，避免误导）。
   * 参数：status 仓库状态对象。
   * 返回值：HTML 字符串。
   */
  function remoteHintHtml(status) {
    const info = status && status.remote_info;
    if (!info) return '';
    const parts = [];
    if (info.https_with_ssh_identity) {
      parts.push(`<div class="inline-hint is-warn">
        <span>SSH 身份对该仓库不生效（当前为 HTTPS 远端）。</span>
        ${info.ssh_convertible ? '<button class="btn-link" data-act="remote">改用 SSH 地址</button>' : ''}
      </div>`);
    }
    if (info.credential_warning) {
      parts.push(`<div class="inline-hint is-alert">
        <span>${escapeHtml(info.credential_warning.message)}</span>
        <button class="btn-link" data-act="remote">处理</button>
      </div>`);
    }
    return parts.join('');
  }

  function cardHtml(repo, status) {
    const health = healthOf(status);
    const protocol = status && status.remote_info ? status.remote_info.protocol : 'unknown';
    const protocolInfo = protocolMeta(protocol);
    const header = `<div class="card-head">
      <div class="repo-name" title="${escapeHtml(repo.name)}">${escapeHtml(repo.name)}</div>
      <div class="repo-pills">${pillBranch(status)}
        ${status && protocol !== 'unknown' ? `<span class="pill pill-protocol ${protocolInfo.cls}">${protocolInfo.label}</span>` : ''}
        <span class="pill is-${health.key}"><span class="dot"></span>${escapeHtml(health.label)}</span>
      </div>
    </div>
    <div class="repo-path" title="${escapeHtml(repo.path)}">${escapeHtml(repo.path)}</div>
    ${remoteHtml(status)}
    ${status ? remoteHintHtml(status) : ''}`;

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
    const ignoreTotal = status.ignore && status.ignore.counts && status.ignore.counts.total
      ? status.ignore.counts.total
      : dirty.total;

    const stats = `<div class="stat-row">
      ${statCell(ahead === null ? '—' : ahead, '待推送', ahead === null ? 'is-muted' : '', !status.upstream ? '未设置上游' : '')}
      ${statCell(behind === null ? '—' : behind, '待拉取', behind === null ? 'is-muted' : '')}
      ${statCell(dirty.pending !== undefined ? dirty.pending : dirty.total, '待处理', (dirty.pending !== undefined ? dirty.pending : dirty.total) ? 'is-warn' : '')}
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
        <div class="foot-meta"><span class="label">${escapeHtml(fetchText)}</span>${status.upstream ? ` · 上游 ${escapeHtml(status.upstream)}` : ''}
          <span class="foot-ssh" title="当前生效的推送身份">· SSH ${escapeHtml(status.ssh_identity ? status.ssh_identity.name : '系统默认')}</span></div>
        <div class="card-actions">
          <button class="btn btn-sub btn-sm" data-act="commit" ${dirty.total ? '' : 'disabled'} title="${dirty.total ? '勾选文件并提交' : '工作区干净，无需提交'}">提交</button>
          <button class="btn btn-ghost btn-sm" data-act="ignore" title="查看忽略归因并处理待处理文件">忽略规则（${ignoreTotal}）</button>
          <button class="btn btn-ghost btn-sm" data-act="remote" title="查看远端协议与 HTTPS/SSH 切换">远端</button>
          <button class="btn btn-ghost btn-sm" data-act="ssh" title="为该仓库绑定 SSH 推送身份">身份</button>
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
      dirty.deleted ? `<span class="chip is-deleted"><span class="dot"></span>已删除 ${dirty.deleted}</span>` : '',
      dirty.untracked ? `<span class="chip is-untracked"><span class="dot"></span>未跟踪 ${dirty.untracked}</span>` : '',
      dirty.conflicts ? `<span class="chip is-conflict"><span class="dot"></span>冲突 ${dirty.conflicts}</span>` : '',
      dirty.muted ? `<span class="chip is-muted"><span class="dot"></span>已静音 ${dirty.muted}</span>` : '',
      dirty.tracked_ignored ? `<span class="chip is-alert"><span class="dot"></span>已跟踪但被 ignore ${dirty.tracked_ignored}</span>` : '',
    ].join('');

    const groups = [
      ['已暂存', status.files.staged, ''],
      ['已修改（未暂存）', status.files.modified, ''],
      ['已删除（未暂存）', status.files.deleted, 'code-deleted'],
      ['未跟踪', status.files.untracked, ''],
      ['冲突', status.files.conflicts, 'code-conflict'],
    ].map(([title, items, cls]) => {
      const visible = (items || []).filter((item) => !item.muted);
      if (!visible.length) return '';
      return `<div class="file-group">
        <div class="file-group-title">${escapeHtml(title)}</div>
        ${visible.map((item) => `<div class="file-row">
          <span class="file-code ${cls}">${escapeHtml(item.code.replace(/\s+/g, '␣'))}</span>
          <span class="file-path" title="${escapeHtml(item.path)}">${escapeHtml(item.path)}</span>
          ${item.diagnosis === 'tracked_ignored' ? '<span class="file-flag is-alert">已跟踪·忽略无效</span>' : ''}
        </div>`).join('')}
      </div>`;
    }).join('');

    const allFiles = ['staged', 'modified', 'deleted', 'untracked', 'conflicts']
      .flatMap((group) => (status.files[group] || []));
    const mutedFiles = allFiles.filter((item) => item.muted);
    const mutedBlock = mutedFiles.length ? `<details class="muted-details">
      <summary>已静音的变更（${mutedFiles.length}）</summary>
      <div class="file-scroll">
        ${mutedFiles.map((item) => `<div class="file-row">
          <span class="file-code">${escapeHtml((item.code || '').replace(/\s+/g, '␣'))}</span>
          <span class="file-path" title="${escapeHtml(item.path)}">${escapeHtml(item.path)}</span>
          <span class="file-flag">静音</span>
          <button class="btn-link" data-unmute="${item.mute_id}" data-repo="${status.repo.id}">取消静音</button>
        </div>`).join('')}
      </div>
      <div class="hint">静音仅影响看板判定，仓库文件与 git 状态未改变。</div>
    </details>` : '';

    return `<div class="dirty-block">
      <div class="dirty-chips">${chips}</div>
      <details class="dirty-details">
        <summary>查看文件明细（${dirty.total}）</summary>
        <div class="file-scroll">${groups || '<div class="commit-empty">未静音的改动为空</div>'}</div>
      </details>
      ${mutedBlock}
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
    if (action === 'ignore') return openIgnoreModal(id);
    if (action === 'remote') return openRemoteModal(id);
    if (action === 'ssh') return openRepoSshModal(id);
    if (action === 'remove') return removeRepo(id);
    return undefined;
  }

  /**
   * 取消静音（仅本地规则，不改仓库文件）。
   * 参数：repoId 仓库主键；muteId 静音规则主键。
   * 返回值：Promise<void>。
   */
  async function unmutePath(repoId, muteId) {
    if (!Number.isFinite(repoId) || !Number.isFinite(muteId)) return;
    try {
      const result = await api(`/api/repos/${repoId}/mutes/${muteId}`, { method: 'DELETE' });
      if (result.status) {
        state.statuses.set(repoId, result.status);
        renderSummary();
        renderRepos();
      }
      toast(result.message || '已取消静音。', 'success');
    } catch (err) {
      toast(err.message, 'error', 6000);
    }
  }

  async function removeRepo(id) {
    const repo = repoById(id);
    if (!repo) return;
    const ok = await confirmDialog({
      title: '移除仓库',
      message: `仅从列表中移除「${repo.name}」的配置记录，不会删除磁盘目录。\n确定继续？`,
      confirmLabel: '移除',
      danger: true,
    });
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

  /**
   * 将焦点放到弹窗内的第一个可用交互控件（主按钮优先，其次输入框）。
   * 参数：preferredSelector 可选的首选选择器。
   * 返回值：无。
   */
  function focusModalStart(preferredSelector = null) {
    const target = (preferredSelector && el.modal.querySelector(preferredSelector))
      || el.modal.querySelector('#modal-foot .btn-primary:not([disabled])')
      || el.modal.querySelector('#modal-body input:not([type="hidden"]):not([disabled]), #modal-body textarea:not([disabled]), #modal-body select:not([disabled])')
      || el.modal.querySelector('#modal-body button:not([disabled])')
      || el.modal.querySelector('#modal-foot button:not([disabled])')
      || el.modalClose;
    if (target) target.focus();
  }

  /**
   * 打开自研 Modal。
   * 参数：
   *   title 标题；body/foot HTML 内容；
   *   wide 是否使用宽版（最大仍不超过 480px）；
   *   dismissible 是否允许遮罩点击关闭（危险确认应传 false）；
   *   onClose 关闭回调（确认框用它返回 Promise 结果）；
   *   autofocus 打开时优先聚焦的选择器。
   * 返回值：无。
   */
  function openModal({ title, body, foot, wide = false, dismissible = true, onClose = null, autofocus = null }) {
    state.modal.returnFocus = document.activeElement;
    state.modal.dismissible = dismissible;
    state.modal.onClose = onClose;
    el.modalTitle.textContent = title;
    el.modalBody.innerHTML = body || '';
    el.modalFoot.innerHTML = foot || '';
    el.modal.classList.toggle('modal-wide', Boolean(wide));
    el.backdrop.hidden = false;
    document.body.classList.add('modal-open');
    window.setTimeout(() => focusModalStart(autofocus), 0);
  }

  function setModalBody(html) { el.modalBody.innerHTML = html; }
  function setModalFoot(html) { el.modalFoot.innerHTML = html; }

  /**
   * 关闭 Modal 并恢复焦点到触发元素。
   * 参数：无。
   * 返回值：无。
   */
  function closeModal() {
    if (el.backdrop.hidden) return;
    if (!el.confirmBackdrop.hidden) closeConfirm(false);
    if (state.scan && state.scan.pollTimer) {
      window.clearTimeout(state.scan.pollTimer);
    }
    const onClose = state.modal.onClose;
    const returnFocus = state.modal.returnFocus;
    state.modal.onClose = null;
    el.backdrop.hidden = true;
    el.modalBody.innerHTML = '';
    el.modalFoot.innerHTML = '';
    document.body.classList.remove('modal-open');
    state.browse = null;
    state.scan = null;
    state.roots = null;
    state.ssh = null;
    state.sshDraft = { editingId: null, testOutput: null };
    if (typeof onClose === 'function') onClose();
    if (returnFocus && returnFocus.isConnected && typeof returnFocus.focus === 'function') {
      returnFocus.focus();
    }
  }

  /**
   * 关闭确认层并返回结果（不影响其下方的业务弹窗）。
   * 参数：value 是否确认。
   * 返回值：无。
   */
  function closeConfirm(value) {
    if (el.confirmBackdrop.hidden) return;
    const resolve = state.confirm.resolve;
    const returnFocus = state.confirm.returnFocus;
    state.confirm.resolve = null;
    state.confirm.returnFocus = null;
    el.confirmBackdrop.hidden = true;
    el.confirmBody.innerHTML = '';
    el.confirmFoot.innerHTML = '';
    if (!el.backdrop.hidden) document.body.classList.add('modal-open');
    else document.body.classList.remove('modal-open');
    if (typeof resolve === 'function') resolve(Boolean(value));
    if (returnFocus && returnFocus.isConnected && typeof returnFocus.focus === 'function') {
      returnFocus.focus();
    }
  }

  /**
   * 自研确认弹窗，替代 window.confirm；层级高于业务弹窗，不销毁业务弹窗内容。
   * 参数：
   *   title 标题；message 正文（纯文本）；confirmLabel 确认按钮文案；
   *   danger 是否危险操作（确认按钮语义红，禁止点击遮罩关闭）。
   * 返回值：Promise<boolean>，确认返回 true，取消/ESC/关闭返回 false。
   */
  function confirmDialog({ title, message, confirmLabel = '确认', danger = false }) {
    return new Promise((resolve) => {
      if (!el.confirmBackdrop.hidden) closeConfirm(false);
      state.confirm.resolve = resolve;
      state.confirm.dismissible = !danger;
      state.confirm.returnFocus = document.activeElement;
      el.confirmTitle.textContent = title;
      el.confirmBody.innerHTML = `<p class="confirm-text">${escapeHtml(message).replace(/\n/g, '<br>')}</p>`;
      el.confirmFoot.innerHTML = `<button class="btn btn-ghost" id="confirm-cancel">取消</button>
        <button class="btn ${danger ? 'btn-danger-solid' : 'btn-primary'}" id="confirm-ok" data-autofocus>${escapeHtml(confirmLabel)}</button>`;
      el.confirmBackdrop.hidden = false;
      document.body.classList.add('modal-open');
      const okBtn = document.getElementById('confirm-ok');
      const cancelBtn = document.getElementById('confirm-cancel');
      if (okBtn) {
        okBtn.addEventListener('click', () => closeConfirm(true));
      }
      if (cancelBtn) cancelBtn.addEventListener('click', () => closeConfirm(false));
      if (okBtn) window.setTimeout(() => okBtn.focus(), 0);
    });
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
    const mutedCount = (status && status.dirty && status.dirty.muted) || 0;
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
        <div class="kv"><span>工作区</span><strong>${dirty ? `${dirty} 个未提交改动${mutedCount ? `（含 ${mutedCount} 项静音）` : ''}` : '干净'}</strong></div>
        <label class="check" style="margin-top:14px">
          <input type="checkbox" id="pull-stash" ${dirty && defaultStash ? 'checked' : ''}>
          <span>pull 前自动 stash（成功后自动 pop；失败则保留，不会静默丢弃）</span>
        </label>
        <div class="hint">本次操作固定执行 <code>git pull --ff-only</code>，不会产生合并提交。若未设置 upstream、本地与远端分叉或工作区有改动，将中止并返回原因。</div>
        <div id="pull-diagnosis"></div>
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
        renderDiagnosis('pull-diagnosis', id, err);
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
    let remoteDetails = [];
    try {
      const data = await api(`/api/repos/${id}/remotes`);
      remotes = data.items || [];
      remoteDetails = data.remotes || [];
    } catch (err) {
      toast(`读取远端列表失败：${err.message}`, 'error');
    }

    const remoteListHtml = remoteDetails.length
      ? `<div class="remote-list">${remoteDetails.map((remote) => {
          const meta = protocolMeta(remote.protocol);
          return `<div class="remote-line">
            <span class="label">${escapeHtml(remote.name)}</span>
            <span class="protocol-badge ${meta.cls}">${meta.label}</span>
            <code class="full-text">${escapeHtml(remote.push_url || remote.fetch_url)}</code>
            <button class="btn-link" data-copy="${escapeHtml(remote.push_url || remote.fetch_url)}" data-copy-label="远端地址">复制</button>
          </div>`;
        }).join('')}</div>`
      : '';

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
        ${remoteListHtml}
        ${forceSection}
        <div id="push-diagnosis"></div>
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
        renderDiagnosis('push-diagnosis', id, err);
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
      forceRun.addEventListener('click', async () => {
        if (!(forceEnable.checked && forceAck.checked)) return;
        const ok = await confirmDialog({
          title: '二次确认：强制推送',
          message: '确定执行强制推送（--force-with-lease）？\n该操作可能改写远端历史，并会记录审计日志。',
          confirmLabel: '强制推送',
          danger: true,
        });
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
      ['deleted', '已删除（未暂存）'],
      ['untracked', '未跟踪'],
      ['conflicts', '冲突'],
    ];

    // 同一文件可能同时出现在 staged 与 modified，按路径去重并合并分组
    const merged = new Map();
    for (const file of changes.files || []) {
      const entry = merged.get(file.path) || { path: file.path, code: file.code, groups: new Set(), muted: false, diagnosis: null };
      entry.groups.add(file.group);
      if (file.group === 'conflicts') entry.code = file.code;
      if (file.muted) entry.muted = true;
      if (file.diagnosis) entry.diagnosis = file.diagnosis;
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
          ${entry.muted ? '<span class="file-flag">静音</span>' : ''}
          ${entry.diagnosis === 'tracked_ignored' ? '<span class="file-flag is-alert">已跟踪·忽略无效</span>' : ''}
        </label>`).join('')}
      </div>`;
    }).join('');

    const conflictBanner = conflicts.length
      ? `<div class="error-box">存在 ${conflicts.length} 个未解决的冲突文件，需先手动解决后才能提交。</div>`
      : '';

    openModal({
      title: `提交 · ${repo.name}`,
      wide: true,
      autofocus: '#commit-message',
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
          <div class="selection-hint" id="commit-hint" ${merged.size ? '' : 'hidden'}>请至少勾选一项变更</div>
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
    const hint = document.getElementById('commit-hint');
    const submitBtn = document.getElementById('commit-submit');
    const selectedCount = () => checks.filter((box) => box.checked).length;

    // 根据勾选状态同步提交按钮与提示（空勾选时禁用并提示）
    const syncCommitState = () => {
      const count = selectedCount();
      submitBtn.disabled = Boolean(conflicts.length) || count === 0;
      allBox.checked = count > 0 && count === checks.length;
      if (hint) hint.hidden = count > 0 || !checks.length;
    };

    allBox.addEventListener('change', () => {
      checks.forEach((box) => { box.checked = allBox.checked; });
      if (!checks.length) allBox.checked = false;
      syncCommitState();
    });
    checks.forEach((box) => box.addEventListener('change', syncCommitState));
    syncCommitState();

    const messageInput = document.getElementById('commit-message');
    const countLabel = document.getElementById('commit-count');
    messageInput.addEventListener('input', () => {
      countLabel.textContent = `${messageInput.value.length} 字`;
    });
    messageInput.focus();

    submitBtn.addEventListener('click', async () => {
      clearModalError();
      const files = selectedCount() ? checks.filter((box) => box.checked).map((box) => box.value) : [];
      const message = messageInput.value.trim();
      if (!files.length) { showModalError('请至少勾选一项变更。'); return; }
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
        // git 非零退出时原样透出命令、退出码与输出
        let text = err.message;
        if (err.payload && err.payload.output && !text.includes(err.payload.output)) {
          text += `\n${err.payload.output}`;
        }
        showModalError(text);
        refreshOne(id, true);
      } finally {
        busyButton(submitBtn, false);
        syncCommitState();
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
      focusModalStart();
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

    if (paths.length > 1) {
      const ok = await confirmDialog({
        title: '批量添加仓库',
        message: `将把 ${paths.length} 个仓库加入管理列表（仅注册路径，不会修改仓库内容）。`,
        confirmLabel: '批量添加',
      });
      if (!ok) return;
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
  // 白名单根目录管理
  // ------------------------------------------------------------------

  async function openRootsModal() {
    state.roots = { items: [] };
    openModal({
      title: '允许的根目录',
      wide: true,
      body: '<div class="card-loading">正在读取根目录…</div>',
      foot: '<button class="btn btn-ghost" id="roots-close">关闭</button>',
    });
    document.getElementById('roots-close').addEventListener('click', closeModal);
    await loadRoots();
  }

  async function loadRoots() {
    try {
      const data = await api('/api/allowed-roots');
      state.roots = { items: data.items || [] };
      renderRoots();
    } catch (err) {
      showModalError(`读取根目录失败：${err.message}`);
    }
  }

  function renderRoots() {
    const roots = state.roots ? state.roots.items : [];
    const rows = roots.length
      ? roots.map((root) => `<div class="root-row">
          <div class="root-row-main">
            <div class="root-row-path">${escapeHtml(root.path)}
              <button class="btn-link" data-copy="${escapeHtml(root.path)}" data-copy-label="根目录路径">复制</button>
            </div>
            <div class="root-row-meta">${escapeHtml(String(root.managed_repos))} 个受管仓库</div>
          </div>
          <button class="btn btn-danger btn-sm" data-root-remove="${root.id}">移除</button>
        </div>`).join('')
      : '<div class="dir-empty">尚未配置白名单根目录。</div>';

    setModalBody(`
      <div class="hint">所有扫描、添加与 git 操作都要求仓库位于以下根目录内；移除根目录不会删除受管仓库，但会让其超出白名单而不可操作。</div>
      <div class="root-list">${rows}</div>
      <div class="inline-form">
        <input class="input" id="root-path" placeholder="输入绝对路径，例如 E:\\Code\\Projects">
        <button class="btn btn-primary" id="root-add">添加根目录</button>
      </div>
      <div class="error-box" id="modal-error" hidden></div>`);
    setModalFoot('<button class="btn btn-ghost" id="roots-close">关闭</button>');
    document.getElementById('roots-close').addEventListener('click', closeModal);
    document.getElementById('root-add').addEventListener('click', addRootPath);
    document.getElementById('root-path').addEventListener('keydown', (event) => {
      if (event.key === 'Enter') addRootPath();
    });
    document.querySelectorAll('[data-root-remove]').forEach((button) => {
      button.addEventListener('click', () => removeRootPath(Number(button.dataset.rootRemove)));
    });
  }

  async function addRootPath() {
    const input = document.getElementById('root-path');
    const path = input ? input.value.trim() : '';
    if (!path) {
      showModalError('请输入绝对路径。');
      return;
    }
    const button = document.getElementById('root-add');
    busyButton(button, true);
    clearModalError();
    try {
      const result = await api('/api/allowed-roots', { method: 'POST', body: { path } });
      await loadSettings();
      await loadRoots();
      toast(result.message || '根目录已添加。', 'success');
    } catch (err) {
      showModalError(`添加失败：${err.message}`);
      busyButton(button, false);
    }
  }

  async function removeRootPath(id) {
    const item = (state.roots ? state.roots.items : []).find((root) => root.id === id);
    if (!item) return;
    const baseMessage = item.managed_repos
      ? `该根目录下仍有 ${item.managed_repos} 个受管仓库。移除后这些仓库仍保留在列表，但会超出白名单而不可操作。\n继续移除？`
      : `确定从白名单移除「${item.path}」？不会删除磁盘目录。`;
    const ok = await confirmDialog({
      title: '移除根目录',
      message: baseMessage,
      confirmLabel: '移除',
      danger: true,
    });
    if (!ok) return;
    try {
      const result = await api(`/api/allowed-roots/${id}`, { method: 'DELETE' });
      await afterRootChange(result.message);
    } catch (err) {
      if (err.status === 409 && err.payload && err.payload.code === 'root_in_use') {
        const forced = await confirmDialog({
          title: '强制移除根目录',
          message: `${err.message}\n确认仍要移除？`,
          confirmLabel: '强制移除',
          danger: true,
        });
        if (!forced) return;
        try {
          const forcedResult = await api(`/api/allowed-roots/${id}?force=true`, { method: 'DELETE' });
          await afterRootChange(forcedResult.message);
        } catch (secondErr) {
          showModalError(`移除失败：${secondErr.message}`);
        }
        return;
      }
      showModalError(`移除失败：${err.message}`);
    }
  }

  async function afterRootChange(message) {
    await loadSettings();
    await loadRoots();
    await loadRepos();
    await refreshAll(true);
    toast(message || '根目录已更新。', 'success');
  }

  // ------------------------------------------------------------------
  // 仓库扫描
  // ------------------------------------------------------------------

  async function openScanModal() {
    state.scan = {
      roots: [],
      selectedRoots: new Set(),
      depth: (state.settings && state.settings.scan_max_depth) || 6,
      limit: (state.settings && state.settings.scan_result_limit) || 200,
      job: null,
      selected: new Set(),
      pollTimer: null,
    };
    openModal({
      title: '扫描本地仓库',
      wide: true,
      body: '<div class="card-loading">正在读取白名单根目录…</div>',
      foot: '<button class="btn btn-ghost" id="scan-close">关闭</button>',
    });
    document.getElementById('scan-close').addEventListener('click', closeModal);
    try {
      const data = await api('/api/allowed-roots');
      state.scan.roots = data.items || [];
      state.scan.selectedRoots = new Set(state.scan.roots.map((root) => root.path));
      renderScan();
    } catch (err) {
      showModalError(`读取根目录失败：${err.message}`);
    }
  }

  function renderScan() {
    const scan = state.scan;
    if (!scan) return;
    const job = scan.job;
    const running = job && job.status === 'running';
    const rootsHtml = scan.roots.length
      ? scan.roots.map((root) => `<label class="check inline scan-root">
          <input type="checkbox" data-scan-root="${escapeHtml(root.path)}" ${scan.selectedRoots.has(root.path) ? 'checked' : ''} ${running ? 'disabled' : ''}>
          <span title="${escapeHtml(root.path)}">${escapeHtml(root.name || root.path)}</span>
        </label>`).join('')
      : '<div class="warn-box">没有可用的白名单根目录，请先在「根目录」中添加。</div>';

    const config = `<div class="scan-config">
      <div class="scan-roots">${rootsHtml}</div>
      <div class="scan-options">
        <label class="field-inline"><span>最大深度</span>
          <input class="input input-sm" id="scan-depth" type="number" min="1" max="12" value="${escapeHtml(scan.depth)}" ${running ? 'disabled' : ''}></label>
        <label class="field-inline"><span>结果上限</span>
          <input class="input input-sm" id="scan-limit" type="number" min="1" max="500" value="${escapeHtml(scan.limit)}" ${running ? 'disabled' : ''}></label>
        <button class="btn btn-primary" id="scan-start" ${running || !scan.roots.length ? 'disabled' : ''}>${job ? '重新扫描' : '开始扫描'}</button>
        ${running ? '<button class="btn btn-ghost" id="scan-cancel">停止</button>' : ''}
      </div>
      <div class="hint">只读扫描：不会执行任何 git 命令；自动跳过 node_modules、.venv、dist、build 等噪音目录与符号链接。</div>
    </div>`;

    let progress = '';
    if (job) {
      const statusText = {
        running: '扫描中', done: '已完成', cancelled: '已停止', error: '出错',
      }[job.status] || job.status;
      progress = `<div class="scan-progress ${job.status === 'running' ? 'is-running' : ''}">
        <div class="scan-progress-main">
          <span class="scan-status">${escapeHtml(statusText)}</span>
          <span>已扫描 <strong>${job.scanned_dirs}</strong> 个目录</span>
          <span>发现 <strong>${job.found}</strong> 个仓库</span>
          <span>耗时 ${escapeHtml(formatDuration(job.duration_ms))}</span>
          ${job.truncated ? '<span class="badge warn">已达结果上限</span>' : ''}
        </div>
        ${job.error_samples && job.error_samples.length ? `<details class="scan-errors"><summary>读取失败的目录（${job.error_samples.length}）</summary><pre>${escapeHtml(job.error_samples.join('\n'))}</pre></details>` : ''}
      </div>`;
    }

    const items = (job && job.items) || [];
    const addable = items.filter((item) => !item.managed);
    const rows = items.length
      ? items.map((item) => {
        const checked = scan.selected.has(item.path);
        return `<tr class="${item.managed ? 'is-managed' : ''}">
          <td class="cell-check"><input type="checkbox" data-scan-pick="${escapeHtml(item.path)}" ${item.managed ? 'disabled' : ''} ${checked ? 'checked' : ''}></td>
          <td><div class="scan-name">${escapeHtml(item.name)}</div><div class="scan-path" title="${escapeHtml(item.path)}">${escapeHtml(item.path)}</div></td>
          <td><span class="pill pill-branch">${escapeHtml(item.branch || (item.detached ? 'detached' : '无提交'))}</span></td>
          <td>${item.managed ? '<span class="badge ok">已在管理</span>' : '<span class="badge neutral">未添加</span>'}</td>
        </tr>`;
      }).join('')
      : (job && job.status !== 'running' ? '<tr><td colspan="4"><div class="dir-empty">没有发现 Git 仓库。</div></td></tr>' : '');

    const table = job ? `<div class="scan-result-head">
        <span>扫描结果（${items.length}${job.truncated ? '+ / 已截断' : ''}）</span>
        <div class="scan-result-actions">
          <button class="btn-link" id="scan-select-all" ${addable.length ? '' : 'disabled'}>全选可添加</button>
          <button class="btn-link" id="scan-invert" ${addable.length ? '' : 'disabled'}>反选</button>
        </div>
      </div>
      <div class="scan-table-wrap"><table class="scan-table">
        <thead><tr><th class="cell-check"></th><th>仓库</th><th>分支</th><th>状态</th></tr></thead>
        <tbody>${rows}</tbody>
      </table></div>` : '';

    setModalBody(`${config}${progress}${table}<div class="error-box" id="modal-error" hidden></div>`);
    const addCount = scan.selected.size;
    setModalFoot(`<button class="btn btn-ghost" id="scan-close">关闭</button>
      <button class="btn btn-primary" id="scan-add" ${addCount ? '' : 'disabled'}>批量添加选中（${addCount}）</button>`);
    bindScanEvents();
  }

  function bindScanEvents() {
    const scan = state.scan;
    if (!scan) return;
    document.getElementById('scan-close').addEventListener('click', closeModal);
    document.querySelectorAll('[data-scan-root]').forEach((box) => {
      box.addEventListener('change', () => {
        if (box.checked) scan.selectedRoots.add(box.dataset.scanRoot);
        else scan.selectedRoots.delete(box.dataset.scanRoot);
      });
    });
    const depthInput = document.getElementById('scan-depth');
    if (depthInput) depthInput.addEventListener('input', () => { scan.depth = depthInput.value; });
    const limitInput = document.getElementById('scan-limit');
    if (limitInput) limitInput.addEventListener('input', () => { scan.limit = limitInput.value; });
    const startBtn = document.getElementById('scan-start');
    if (startBtn) startBtn.addEventListener('click', startScan);
    const cancelBtn = document.getElementById('scan-cancel');
    if (cancelBtn) cancelBtn.addEventListener('click', cancelScan);
    document.querySelectorAll('[data-scan-pick]').forEach((box) => {
      box.addEventListener('change', () => {
        if (box.checked) scan.selected.add(box.dataset.scanPick);
        else scan.selected.delete(box.dataset.scanPick);
        updateScanFoot();
      });
    });
    const selectAll = document.getElementById('scan-select-all');
    if (selectAll) {
      selectAll.addEventListener('click', () => {
        (scan.job.items || []).filter((item) => !item.managed).forEach((item) => scan.selected.add(item.path));
        renderScan();
      });
    }
    const invert = document.getElementById('scan-invert');
    if (invert) {
      invert.addEventListener('click', () => {
        const next = new Set();
        (scan.job.items || []).filter((item) => !item.managed).forEach((item) => {
          if (!scan.selected.has(item.path)) next.add(item.path);
        });
        scan.selected = next;
        renderScan();
      });
    }
    const addBtn = document.getElementById('scan-add');
    if (addBtn) addBtn.addEventListener('click', addScannedRepos);
  }

  function updateScanFoot() {
    const scan = state.scan;
    if (!scan) return;
    const button = document.getElementById('scan-add');
    if (button) {
      button.textContent = `批量添加选中（${scan.selected.size}）`;
      button.disabled = scan.selected.size === 0;
    }
  }

  async function startScan() {
    const scan = state.scan;
    if (!scan) return;
    const roots = Array.from(scan.selectedRoots);
    if (!roots.length) {
      showModalError('请至少选择一个扫描根目录。');
      return;
    }
    const depth = Number(scan.depth) || 6;
    const limit = Number(scan.limit) || 200;
    const button = document.getElementById('scan-start');
    busyButton(button, true);
    clearModalError();
    try {
      const job = await api('/api/scan/start', {
        method: 'POST',
        body: { roots, max_depth: depth, limit },
      });
      scan.job = job;
      scan.selected.clear();
      renderScan();
      pollScan();
    } catch (err) {
      showModalError(`启动扫描失败：${err.message}`);
      busyButton(button, false);
    }
  }

  function pollScan() {
    const scan = state.scan;
    if (!scan || !scan.job) return;
    const jobId = scan.job.job_id;
    if (scan.pollTimer) window.clearTimeout(scan.pollTimer);
    const tick = async () => {
      if (!state.scan || !state.scan.job || state.scan.job.job_id !== jobId) return;
      try {
        const job = await api(`/api/scan/${jobId}`);
        if (!state.scan || state.scan.job.job_id !== jobId) return;
        state.scan.job = job;
        renderScan();
        if (job.status === 'running') {
          state.scan.pollTimer = window.setTimeout(tick, 500);
        }
      } catch (err) {
        toast(`读取扫描进度失败：${err.message}`, 'error');
      }
    };
    state.scan.pollTimer = window.setTimeout(tick, 500);
  }

  async function cancelScan() {
    const scan = state.scan;
    if (!scan || !scan.job) return;
    try {
      scan.job = await api(`/api/scan/${scan.job.job_id}/cancel`, { method: 'POST' });
      renderScan();
      toast('已请求停止扫描。', 'info');
    } catch (err) {
      toast(`停止扫描失败：${err.message}`, 'error');
    }
  }

  async function addScannedRepos() {
    const scan = state.scan;
    if (!scan || !scan.selected.size) return;
    const paths = Array.from(scan.selected);
    const button = document.getElementById('scan-add');
    const ok = await confirmDialog({
      title: '批量添加仓库',
      message: `将把扫描选中的 ${paths.length} 个仓库加入管理列表（仅注册路径，不会修改仓库内容）。`,
      confirmLabel: '批量添加',
    });
    if (!ok) return;
    busyButton(button, true);
    try {
      const result = await api('/api/repos', { method: 'POST', body: { paths } });
      const added = result.added || 0;
      const failed = (result.results || []).filter((item) => !item.ok).length;
      const successKeys = new Set(
        (result.results || []).filter((item) => item.ok).map((item) => normalizePath(item.path))
      );
      (scan.job.items || []).forEach((item) => {
        if (successKeys.has(normalizePath(item.path))) item.managed = true;
      });
      scan.selected.clear();
      await loadRepos();
      refreshAll(true);
      renderScan();
      toast(`已添加 ${added} 个仓库${failed ? `，${failed} 条失败` : ''}。`, failed ? 'warn' : 'success');
    } catch (err) {
      showModalError(`批量添加失败：${err.message}`);
      busyButton(button, false);
    }
  }

  // ------------------------------------------------------------------
  // SSH 身份
  // ------------------------------------------------------------------

  async function openSshModal() {
    state.ssh = { scan: null, identities: [], default: null };
    state.sshDraft = { editingId: null, testOutput: null };
    openModal({
      title: 'SSH 推送身份',
      wide: true,
      body: '<div class="card-loading">正在扫描本机 SSH 配置…</div>',
      foot: '<button class="btn btn-ghost" id="ssh-close">关闭</button>',
    });
    document.getElementById('ssh-close').addEventListener('click', closeModal);
    await loadSshData();
  }

  async function loadSshData() {
    try {
      const [scan, identities] = await Promise.all([
        api('/api/ssh/scan'),
        api('/api/ssh/identities'),
      ]);
      state.ssh = { scan, identities: identities.items || [], default: identities.default || null };
      renderSsh();
    } catch (err) {
      showModalError(`读取 SSH 配置失败：${err.message}`);
    }
  }

  function renderSsh() {
    const ssh = state.ssh;
    if (!ssh) return;
    const scan = ssh.scan || { keys: [], hosts: [], ssh_dir: '' };
    const identities = ssh.identities;
    const draft = state.sshDraft;

    const identityRows = identities.length
      ? identities.map((item) => {
        const flags = [
          item.is_default ? '<span class="badge ok">全局默认</span>' : '',
          item.enabled ? '' : '<span class="badge neutral">已禁用</span>',
          item.bound_repos ? `<span class="badge neutral">绑定 ${item.bound_repos} 个仓库</span>` : '',
          item.key_exists ? '' : '<span class="badge fail">密钥缺失</span>',
        ].join(' ');
        return `<div class="ssh-row ${item.enabled ? '' : 'is-disabled'}" data-ssh-row="${item.id}">
          <div class="ssh-row-main">
            <div class="ssh-row-title"><span class="ssh-name">${escapeHtml(item.name)}</span> ${flags}</div>
            <div class="ssh-row-meta">
              <code class="full-text">${escapeHtml(item.key_path)}</code>
              <button class="btn-link" data-copy="${escapeHtml(item.key_path)}" data-copy-label="私钥路径">复制</button>
              ${item.host_alias ? ` · Host <code class="full-text">${escapeHtml(item.host_alias)}</code>
                <button class="btn-link" data-copy="${escapeHtml(item.host_alias)}" data-copy-label="Host 别名">复制</button>` : ' · 未配置 Host'}
            </div>
            <div class="ssh-row-meta">${item.fingerprint ? `指纹 ${escapeHtml(item.fingerprint)}` : escapeHtml((item.warnings || []).join('；') || '无指纹信息')}</div>
          </div>
          <div class="ssh-row-actions">
            <button class="btn btn-ghost btn-sm" data-ssh-test="${item.id}">测试连接</button>
            ${item.is_default ? '' : `<button class="btn btn-ghost btn-sm" data-ssh-default="${item.id}" ${item.enabled ? '' : 'disabled'}>设为默认</button>`}
            <button class="btn btn-ghost btn-sm" data-ssh-toggle="${item.id}" data-enabled="${item.enabled ? '1' : '0'}">${item.enabled ? '禁用' : '启用'}</button>
            <button class="btn btn-ghost btn-sm" data-ssh-edit="${item.id}">编辑</button>
            <button class="btn btn-danger btn-sm" data-ssh-delete="${item.id}">删除</button>
          </div>
        </div>`;
      }).join('')
      : '<div class="dir-empty">还没有配置身份。可从下方扫描结果一键添加，或手动填写。</div>';

    const keyRows = (scan.keys || []).length
      ? scan.keys.map((key) => `<div class="ssh-key-row">
          <div class="ssh-row-main">
            <div class="ssh-row-title"><span class="ssh-name">${escapeHtml(key.name)}</span>
              ${key.configured ? '<span class="badge ok">已配置</span>' : ''}
              ${key.encrypted ? '<span class="badge warn">已加密</span>' : ''}
              ${key.readable ? '' : '<span class="badge fail">不可读</span>'}</div>
            <div class="ssh-row-meta">
              <code class="full-text">${escapeHtml(key.key_path)}</code>
              <button class="btn-link" data-copy="${escapeHtml(key.key_path)}" data-copy-label="私钥路径">复制</button>
            </div>
            <div class="ssh-row-meta">${key.fingerprint ? `指纹 ${escapeHtml(key.fingerprint)}${key.bits ? ` · ${escapeHtml(key.bits)} 位` : ''}` : escapeHtml(key.warnings.join('；') || '无指纹信息')}
              ${key.suggested_alias ? ` · 建议 Host ${escapeHtml(key.suggested_alias)}` : ''}
              ${key.comment ? ` · ${escapeHtml(key.comment)}` : ''}</div>
          </div>
          <div class="ssh-row-actions">
            <button class="btn btn-ghost btn-sm" data-ssh-adopt="${escapeHtml(key.key_path)}" data-alias="${escapeHtml(key.suggested_alias || '')}" data-name="${escapeHtml(key.name)}" ${key.configured ? 'disabled' : ''}>添加为身份</button>
          </div>
        </div>`).join('')
      : `<div class="dir-empty">在 ${escapeHtml(scan.ssh_dir || '~/.ssh')} 中未发现私钥文件。</div>`;

    const hosts = (scan.hosts || []).length
      ? `<div class="ssh-hosts">config 中可用 Host：${scan.hosts.map((host) => `<span class="chip is-stitched">${escapeHtml(host)}</span>`).join('')}</div>`
      : '';

    const editing = draft.editingId;
    const current = editing ? identities.find((item) => item.id === editing) : null;
    const form = `<div class="ssh-form">
      <div class="ssh-form-title">${editing ? `编辑身份：${escapeHtml(current ? current.name : '')}` : '手动新增身份'}</div>
      <div class="ssh-form-grid">
        <label class="field"><span>显示名</span><input class="input" id="ssh-name" value="${escapeHtml(current ? current.name : '')}" placeholder="例如 工作账号"></label>
        <label class="field"><span>私钥路径</span><input class="input" id="ssh-key-path" value="${escapeHtml(current ? current.key_path : '')}" placeholder="C:\\Users\\me\\.ssh\\id_ed25519"></label>
        <label class="field"><span>Host 别名（可选）</span><input class="input" id="ssh-host" value="${escapeHtml(current ? current.host_alias : '')}" placeholder="例如 github.com"></label>
      </div>
      <div class="ssh-form-actions">
        <button class="btn btn-primary btn-sm" id="ssh-save">${editing ? '保存修改' : '保存身份'}</button>
        ${editing ? '<button class="btn btn-ghost btn-sm" id="ssh-cancel-edit">取消编辑</button>' : ''}
      </div>
      <div class="hint">只保存路径与别名，不读取、不上传私钥正文；删除身份也不会删除磁盘密钥文件。</div>
    </div>`;

    const testBox = draft.testOutput
      ? `<div class="ssh-test ${draft.testOutput.status === 'success' ? 'is-ok' : 'is-fail'}">
          <div class="ssh-test-head">连接测试：${escapeHtml(draft.testOutput.identity_name)} → ${escapeHtml(draft.testOutput.host)}
            <span class="badge ${draft.testOutput.status === 'success' ? 'ok' : 'fail'}">${escapeHtml(draft.testOutput.status)}</span>
            ${draft.testOutput.permission_issue ? '<span class="badge warn">权限异常</span>' : ''}
          </div>
          <pre>${escapeHtml(draft.testOutput.output || '（无输出）')}</pre>
          ${(draft.testOutput.warnings || []).length ? `<div class="hint">${escapeHtml(draft.testOutput.warnings.join('；'))}</div>` : ''}
        </div>`
      : '';

    setModalBody(`
      <div class="ssh-meta">
        <span>SSH 目录：${escapeHtml(scan.ssh_dir || '—')}</span>
        <span>全局默认：${escapeHtml(ssh.default ? ssh.default.name : '未设置（使用系统 SSH 默认）')}</span>
      </div>
      ${hosts}
      <div class="ssh-section"><div class="ssh-section-head"><h3>已配置身份（${identities.length}）</h3></div>${identityRows}</div>
      ${testBox}
      <div class="ssh-section"><div class="ssh-section-head"><h3>扫描到的私钥（${(scan.keys || []).length}）</h3><button class="btn btn-ghost btn-sm" id="ssh-rescan">重新扫描</button></div>${keyRows}</div>
      ${form}
      <div class="error-box" id="modal-error" hidden></div>`);
    setModalFoot('<button class="btn btn-ghost" id="ssh-close">关闭</button>');
    bindSshEvents();
  }

  function bindSshEvents() {
    document.getElementById('ssh-close').addEventListener('click', closeModal);
    const rescan = document.getElementById('ssh-rescan');
    if (rescan) rescan.addEventListener('click', async () => {
      busyButton(rescan, true);
      await loadSshData();
    });
    document.getElementById('ssh-save').addEventListener('click', saveSshIdentity);
    const cancelEdit = document.getElementById('ssh-cancel-edit');
    if (cancelEdit) cancelEdit.addEventListener('click', () => {
      state.sshDraft.editingId = null;
      renderSsh();
    });
    document.querySelectorAll('[data-ssh-edit]').forEach((button) => {
      button.addEventListener('click', () => {
        state.sshDraft.editingId = Number(button.dataset.sshEdit);
        renderSsh();
      });
    });
    document.querySelectorAll('[data-ssh-delete]').forEach((button) => {
      button.addEventListener('click', () => deleteSshIdentity(Number(button.dataset.sshDelete)));
    });
    document.querySelectorAll('[data-ssh-default]').forEach((button) => {
      button.addEventListener('click', () => setDefaultSsh(Number(button.dataset.sshDefault)));
    });
    document.querySelectorAll('[data-ssh-toggle]').forEach((button) => {
      button.addEventListener('click', () => toggleSsh(
        Number(button.dataset.sshToggle), button.dataset.enabled === '1'
      ));
    });
    document.querySelectorAll('[data-ssh-test]').forEach((button) => {
      button.addEventListener('click', () => testSsh(Number(button.dataset.sshTest), button));
    });
    document.querySelectorAll('[data-ssh-adopt]').forEach((button) => {
      button.addEventListener('click', () => adoptSshKey(button.dataset.sshAdopt, button.dataset.alias, button.dataset.name, button));
    });
  }

  async function saveSshIdentity() {
    const nameInput = document.getElementById('ssh-name');
    const keyInput = document.getElementById('ssh-key-path');
    const hostInput = document.getElementById('ssh-host');
    const payload = {
      name: nameInput.value.trim(),
      key_path: keyInput.value.trim(),
      host_alias: hostInput.value.trim(),
    };
    if (!payload.name || !payload.key_path) {
      showModalError('显示名与私钥路径必填。');
      return;
    }
    const button = document.getElementById('ssh-save');
    busyButton(button, true);
    clearModalError();
    try {
      if (state.sshDraft.editingId) {
        await api(`/api/ssh/identities/${state.sshDraft.editingId}`, { method: 'PATCH', body: payload });
      } else {
        await api('/api/ssh/identities', { method: 'POST', body: payload });
      }
      state.sshDraft.editingId = null;
      state.sshDraft.testOutput = null;
      await loadSettings();
      await loadSshData();
      toast('SSH 身份已保存。', 'success');
    } catch (err) {
      showModalError(`保存失败：${err.message}`);
      busyButton(button, false);
    }
  }

  async function deleteSshIdentity(id) {
    const item = state.ssh.identities.find((candidate) => candidate.id === id);
    if (!item) return;
    const ok = await confirmDialog({
      title: '删除 SSH 身份',
      message: `删除身份「${item.name}」的配置记录？\n不会删除磁盘私钥文件，相关仓库绑定会被解除。`,
      confirmLabel: '删除',
      danger: true,
    });
    if (!ok) return;
    try {
      const result = await api(`/api/ssh/identities/${id}`, { method: 'DELETE' });
      await loadSettings();
      await loadSshData();
      toast(result.message || '身份已删除。', 'success');
    } catch (err) {
      showModalError(`删除失败：${err.message}`);
    }
  }

  async function setDefaultSsh(id) {
    try {
      await api(`/api/ssh/identities/${id}/default`, { method: 'POST' });
      await loadSettings();
      await loadSshData();
      toast('已设为全局默认身份。', 'success');
    } catch (err) {
      showModalError(`设置默认失败：${err.message}`);
    }
  }

  async function toggleSsh(id, enabled) {
    try {
      await api(`/api/ssh/identities/${id}`, { method: 'PATCH', body: { enabled: !enabled } });
      await loadSettings();
      await loadSshData();
      toast(`身份已${enabled ? '禁用' : '启用'}。`, 'success');
    } catch (err) {
      showModalError(`更新失败：${err.message}`);
    }
  }

  async function testSsh(id, button) {
    busyButton(button, true);
    try {
      const result = await api(`/api/ssh/identities/${id}/test`, { method: 'POST', body: {} });
      state.sshDraft.testOutput = result;
      renderSsh();
    } catch (err) {
      showModalError(`测试失败：${err.message}`);
      busyButton(button, false);
    }
  }

  async function adoptSshKey(keyPath, alias, name, button) {
    busyButton(button, true);
    try {
      await api('/api/ssh/identities', {
        method: 'POST',
        body: { name: name || basename(keyPath), key_path: keyPath, host_alias: alias || '' },
      });
      await loadSettings();
      await loadSshData();
      toast('已添加为 SSH 身份。', 'success');
    } catch (err) {
      showModalError(`添加失败：${err.message}`);
      busyButton(button, false);
    }
  }

  async function openRepoSshModal(id) {
    const repo = repoById(id);
    if (!repo) return;
    openModal({
      title: `仓库 SSH 身份 · ${repo.name}`,
      body: '<div class="card-loading">正在读取身份配置…</div>',
      foot: '<button class="btn btn-ghost" id="repo-ssh-close">关闭</button>',
    });
    document.getElementById('repo-ssh-close').addEventListener('click', closeModal);
    try {
      const data = await api('/api/ssh/identities');
      const status = statusById(id);
      const current = status && status.ssh_identity;
      const options = (data.items || []).filter((item) => item.enabled).map((item) => {
        const selected = current && current.source === 'repo' && current.id === item.id;
        return `<option value="${item.id}" ${selected ? 'selected' : ''}>${escapeHtml(item.name)}${item.is_default ? '（全局默认）' : ''}</option>`;
      }).join('');
      setModalBody(`<div class="hint">仓库级绑定优先于全局默认；切换只影响后续的 fetch / pull / push，并会记录到审计日志。</div>
        <div class="kv-row"><span class="label">当前生效</span>
          <strong>${escapeHtml(current ? current.name : '系统 SSH 默认')}</strong>
          ${current ? `<span class="badge neutral">${current.source === 'repo' ? '仓库绑定' : '全局默认'}</span>` : ''}</div>
        <label class="field" style="margin-top:14px"><span>绑定身份</span>
          <select class="input" id="repo-ssh-select">
            <option value="">不绑定（使用全局默认）</option>
            ${options}
          </select></label>
        ${(data.items || []).length ? '' : '<div class="warn-box">还没有可用身份，请先在「SSH 身份」中添加。</div>'}
        <div class="error-box" id="modal-error" hidden></div>`);
      setModalFoot(`<button class="btn btn-ghost" id="repo-ssh-close">关闭</button>
        <button class="btn btn-primary" id="repo-ssh-save">保存绑定</button>`);
      document.getElementById('repo-ssh-close').addEventListener('click', closeModal);
      document.getElementById('repo-ssh-save').addEventListener('click', async () => {
        const select = document.getElementById('repo-ssh-select');
        const value = select.value ? Number(select.value) : null;
        const saveBtn = document.getElementById('repo-ssh-save');
        busyButton(saveBtn, true);
        try {
          await api(`/api/repos/${id}/ssh-identity`, { method: 'PUT', body: { identity_id: value } });
          await loadSettings();
          await refreshOne(id, true);
          toast('仓库 SSH 身份绑定已更新。', 'success');
          closeModal();
        } catch (err) {
          showModalError(`保存失败：${err.message}`);
          busyButton(saveBtn, false);
        }
      });
    } catch (err) {
      showModalError(`读取身份失败：${err.message}`);
    }
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
    params.set('page', String(state.audit.page));
    params.set('page_size', String(state.audit.pageSize));
    if (state.audit.repoId) params.set('repo_id', state.audit.repoId);
    if (state.audit.action) params.set('action', state.audit.action);
    if (state.audit.onlyFailed) params.set('only_failed', 'true');

    el.auditRefresh.classList.add('is-busy');
    try {
      const data = await api(`/api/audit?${params.toString()}`);
      state.audit.total = data.total || 0;
      state.audit.totalPages = data.total_pages || 0;
      state.audit.page = data.page || 1;
      state.audit.from = data.from || 0;
      state.audit.to = data.to || 0;
      state.audit.items = data.items || [];
      renderAudit();
    } catch (err) {
      toast(`读取审计失败：${err.message}`, 'error');
    } finally {
      el.auditRefresh.classList.remove('is-busy');
    }
  }

  /**
   * 同步审计分页控件状态（首末页置灰、页码输入范围、页码显示）。
   * 参数：无。
   * 返回值：无。
   */
  function syncAuditPager() {
    const totalPages = state.audit.totalPages;
    const page = state.audit.page;
    const noPages = totalPages <= 0;
    el.auditFirst.disabled = noPages || page <= 1;
    el.auditPrev.disabled = noPages || page <= 1;
    el.auditNext.disabled = noPages || page >= totalPages;
    el.auditLast.disabled = noPages || page >= totalPages;
    el.auditGoto.disabled = noPages || totalPages <= 1;
    el.auditPage.disabled = noPages;
    el.auditPage.max = String(Math.max(1, totalPages));
    el.auditPage.value = String(noPages ? 1 : page);
  }

  function renderAudit() {
    const items = state.audit.items;
    if (!items.length) {
      el.auditBody.innerHTML = '';
      el.auditEmpty.hidden = false;
      el.auditRange.textContent = `共 ${state.audit.total} 条 / 第 0-0 条`;
      syncAuditPager();
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
        <td class="cell-identity">${escapeHtml(item.identity_name || '—')}</td>
        <td class="cell-command" title="${escapeHtml(item.command)}">${escapeHtml(item.command)}</td>
        <td><span class="badge exit">${escapeHtml(item.exit_code)}</span></td>
        <td class="cell-duration">${escapeHtml(formatDuration(item.duration_ms))}</td>
        <td><span class="badge ${item.ok ? 'ok' : 'fail'}">${item.ok ? '成功' : '失败'}</span></td>
        <td class="cell-summary" title="${escapeHtml(summary)}">${escapeHtml(summary.split('\n')[0] || '—')}</td>
      </tr>
      <tr class="audit-detail-row" hidden><td colspan="9">
        <div class="audit-detail">
          <div class="label">完整命令</div>
          <pre>${escapeHtml(item.command)}</pre>
          <div class="label">SSH 身份</div>
          <pre>${escapeHtml(item.identity_name || '（未使用配置身份）')}</pre>
          <div class="label">输出摘要</div>
          <pre>${escapeHtml(summary)}</pre>
        </div>
      </td></tr>`;
    }).join('');

    el.auditRange.textContent = `共 ${state.audit.total} 条 / 第 ${state.audit.from}-${state.audit.to} 条`
      + ` · 第 ${state.audit.page}/${Math.max(1, state.audit.totalPages)} 页`;
    syncAuditPager();
  }

  /**
   * 跳转到指定审计页（自动收敛到合法范围）。
   * 参数：page 目标页码。
   * 返回值：无。
   */
  function gotoAuditPage(page) {
    const totalPages = state.audit.totalPages;
    const target = totalPages <= 0 ? 1 : Math.max(1, Math.min(Number(page) || 1, totalPages));
    state.audit.page = target;
    loadAudit();
  }

  // ------------------------------------------------------------------
  // 事件绑定与初始化
  // ------------------------------------------------------------------

  // ------------------------------------------------------------------
  // 忽略规则面板 / 静音 / 取消跟踪
  // ------------------------------------------------------------------

  const DIAGNOSIS_TEXT = {
    untracked_ignorable: '未被任何规则匹配，可加入 .gitignore',
    rule_matched: '命中忽略规则，但仍出现在看板',
    tracked_ignored: '已被跟踪，忽略规则不生效',
    dirty_tracked: '已跟踪的改动，正常提交即可',
  };

  /**
   * 渲染忽略面板中的单个文件行（含规则来源原文与勾选框）。
   * 参数：item 归因文件对象。
   * 返回值：HTML 字符串。
   */
  function ignoreRowHtml(item) {
    const checked = item.diagnosis === 'untracked_ignorable' || item.diagnosis === 'tracked_ignored';
    const source = item.ignore_source
      ? `<code class="rule-source full-text" title="规则来源">${escapeHtml(item.ignore_source)}</code>
         <button class="btn-link" data-copy="${escapeHtml(item.ignore_source)}" data-copy-label="规则来源">复制</button>`
      : '';
    return `<div class="ignore-row">
      <input type="checkbox" class="ignore-check" data-path="${escapeHtml(item.path)}"
        data-diagnosis="${escapeHtml(item.diagnosis)}" data-tracked="${item.tracked ? '1' : '0'}" ${checked ? 'checked' : ''}>
      <div class="ignore-main">
        <div class="ignore-path full-text">${escapeHtml(item.path)}</div>
        <div class="ignore-meta">
          <span class="diag-badge is-${escapeHtml(item.diagnosis)}">${escapeHtml(DIAGNOSIS_TEXT[item.diagnosis] || item.diagnosis)}</span>
          ${source}
        </div>
      </div>
    </div>`;
  }

  /**
   * 打开忽略规则面板，按成因分组列出待处理文件与可执行动作。
   * 参数：id 仓库主键。
   * 返回值：Promise<void>。
   */
  async function openIgnoreModal(id) {
    const repo = repoById(id);
    if (!repo) return;
    let report;
    try {
      report = await api(`/api/repos/${id}/ignore`);
    } catch (err) {
      toast(`读取忽略归因失败：${err.message}`, 'error', 6000);
      return;
    }
    const files = report.files || [];
    const counts = report.counts || {};
    const mutedRows = files.filter((item) => item.muted);
    const sections = [
      {
        diagnosis: 'untracked_ignorable',
        title: '可忽略（未被任何规则匹配）',
        hint: '可写入 .gitignore，或仅本地静音。',
        items: files.filter((item) => item.diagnosis === 'untracked_ignorable'),
      },
      {
        diagnosis: 'rule_matched',
        title: '命中规则但仍出现在看板',
        hint: '规则来源原样展示，便于排查反向规则或嵌套规则。',
        items: files.filter((item) => item.diagnosis === 'rule_matched'),
      },
      {
        diagnosis: 'tracked_ignored',
        title: '已跟踪但命中 ignore 规则',
        hint: '忽略规则对已跟踪文件不生效；可取消跟踪（保留磁盘文件），或仅本地静音。',
        items: files.filter((item) => item.diagnosis === 'tracked_ignored'),
      },
      {
        diagnosis: 'dirty_tracked',
        title: '已跟踪的改动',
        hint: '正常提交即可；也可仅本地静音（不会改变仓库状态）。',
        items: files.filter((item) => item.diagnosis === 'dirty_tracked'),
      },
    ];
    const sectionHtml = sections.map((section) => {
      if (!section.items.length) return '';
      return `<div class="ignore-section">
        <div class="ignore-section-title">${escapeHtml(section.title)}<span class="count">${section.items.length}</span></div>
        <div class="hint">${escapeHtml(section.hint)}</div>
        ${section.items.map((item) => ignoreRowHtml(item)).join('')}
      </div>`;
    }).join('');
    const mutesHtml = (report.mutes || []).length ? `<div class="ignore-section">
      <div class="ignore-section-title">当前静音规则<span class="count">${report.mutes.length}</span></div>
      ${report.mutes.map((mute) => `<div class="ignore-row">
        <div class="ignore-main">
          <div class="ignore-path full-text">${escapeHtml(mute.pattern)}</div>
          <div class="ignore-meta"><span class="diag-badge is-muted">静音</span>
            ${mute.note ? `<span class="muted-note">${escapeHtml(mute.note)}</span>` : ''}
            <button class="btn-link" data-unmute="${mute.id}" data-repo="${id}">取消静音</button>
          </div>
        </div>
      </div>`).join('')}
    </div>` : '';
    const mutedHtml = mutedRows.length ? `<div class="ignore-section">
      <div class="ignore-section-title">已静音的变更<span class="count">${mutedRows.length}</span></div>
      ${mutedRows.map((item) => `<div class="ignore-row">
        <div class="ignore-main">
          <div class="ignore-path full-text">${escapeHtml(item.path)}</div>
          <div class="ignore-meta"><span class="diag-badge is-muted">静音</span>
            <button class="btn-link" data-unmute="${item.mute_id}" data-repo="${id}">取消静音</button>
          </div>
        </div>
      </div>`).join('')}
    </div>` : '';

    openModal({
      title: `忽略规则 · ${repo.name}`,
      wide: true,
      body: `
        <div class="repo-reference">
          <div><div class="name">${escapeHtml(repo.name)}</div><div class="path">${escapeHtml(repo.path)}</div></div>
        </div>
        <div class="ignore-summary">
          <span class="chip is-warn"><span class="dot"></span>待处理 ${counts.pending || 0}</span>
          <span class="chip is-muted"><span class="dot"></span>已静音 ${counts.muted || 0}</span>
          <span class="chip is-alert"><span class="dot"></span>已跟踪但被 ignore ${counts.tracked_ignored || 0}</span>
          <span class="chip"><span class="dot"></span>规则命中 ${counts.rule_matched || 0}</span>
        </div>
        <div class="hint">.gitignore：${escapeHtml(report.gitignore.path)}（${report.gitignore.exists ? '已存在' : '不存在，将在确认后创建'}）</div>
        ${sectionHtml || '<div class="commit-empty">当前没有待处理文件。</div>'}
        ${mutesHtml}
        ${mutedHtml}
        <div class="error-box" id="modal-error" hidden></div>
      `,
      foot: `<button class="btn btn-ghost" id="ignore-close">关闭</button>
        <button class="btn btn-ghost" id="ignore-mute" ${files.some((item) => !item.muted) ? '' : 'disabled'}>仅本地静音（选中）</button>
        <button class="btn btn-ghost" id="ignore-untrack" ${(counts.tracked_ignored || 0) ? '' : 'disabled'}>取消跟踪（选中）</button>
        <button class="btn btn-primary" id="ignore-append" ${files.some((item) => item.diagnosis === 'untracked_ignorable' && !item.muted) ? '' : 'disabled'}>写入 .gitignore（选中）</button>`,
    });

    document.getElementById('ignore-close').addEventListener('click', closeModal);
    el.modalBody.querySelectorAll('[data-unmute]').forEach((button) => {
      button.addEventListener('click', async () => {
        await unmutePath(id, Number(button.dataset.unmute));
        openIgnoreModal(id);
      });
    });
    document.getElementById('ignore-append').addEventListener('click', () => {
      const selected = collectIgnoreSelection();
      const paths = selected.filter((item) => item.diagnosis === 'untracked_ignorable').map((item) => item.path);
      if (!paths.length) {
        toast('请先勾选“可忽略（未被任何规则匹配）”的文件。', 'warn', 5000);
        return;
      }
      showIgnorePreview(id, paths);
    });
    document.getElementById('ignore-mute').addEventListener('click', async () => {
      const selected = collectIgnoreSelection();
      if (!selected.length) {
        toast('请先勾选需要静音的文件。', 'warn', 5000);
        return;
      }
      try {
        const result = await api(`/api/repos/${id}/mutes`, {
          method: 'POST',
          body: { patterns: selected.map((item) => item.path) },
        });
        await afterRepoAction(id, result, result.message || '已静音（仅本地生效）。');
      } catch (err) {
        showModalError(err.message);
      }
    });
    const untrackBtn = document.getElementById('ignore-untrack');
    untrackBtn.addEventListener('click', async () => {
      const selected = collectIgnoreSelection();
      const paths = selected.filter((item) => item.diagnosis === 'tracked_ignored').map((item) => item.path);
      if (!paths.length) {
        toast('请先在“已跟踪但命中 ignore 规则”中勾选文件。', 'warn', 5000);
        return;
      }
      const ok = await confirmDialog({
        title: '取消跟踪（危险操作）',
        message: `将从版本库移除以下 ${paths.length} 个路径，但保留磁盘文件：\n${paths.join('\n')}\n\n执行后还需要额外提交一次删除变更（不会自动提交）。`,
        confirmLabel: '取消跟踪',
        danger: true,
      });
      if (!ok) return;
      try {
        const result = await api(`/api/repos/${id}/untrack`, {
          method: 'POST',
          body: { paths, confirm: true },
        });
        await afterRepoAction(id, result, result.message || '已取消跟踪。');
      } catch (err) {
        showModalError(err.message);
      }
    });
  }

  /**
   * 读取忽略面板中已勾选的项（路径、归因、是否已跟踪）。
   * 参数：无。
   * 返回值：[{path, diagnosis, tracked}]。
   */
  function collectIgnoreSelection() {
    return Array.from(el.modalBody.querySelectorAll('.ignore-check:checked')).map((box) => ({
      path: box.dataset.path,
      diagnosis: box.dataset.diagnosis,
      tracked: box.dataset.tracked === '1',
    }));
  }

  /**
   * 在忽略面板内展示 .gitignore 追加预览，确认后才写入。
   * 参数：id 仓库主键；paths 待忽略路径。
   * 返回值：Promise<void>。
   */
  async function showIgnorePreview(id, paths) {
    let preview;
    try {
      preview = await api(`/api/repos/${id}/ignore/preview`, {
        method: 'POST',
        body: { paths },
      });
    } catch (err) {
      showModalError(`生成预览失败：${err.message}`);
      return;
    }
    const rules = preview.rules || [];
    const skipped = preview.skipped || [];
    if (!rules.length) {
      setModalBody(`<div class="ignore-section">
        <div class="ignore-section-title">无需追加</div>
        <div class="hint">${escapeHtml(preview.target)} 中已存在等价规则。下面是被跳过的项：</div>
        ${skipped.map((item) => `<div class="ignore-row"><div class="ignore-main">
          <div class="ignore-path full-text">${escapeHtml(item.path)}</div>
          <div class="ignore-meta"><code class="rule-source">${escapeHtml(item.rule)}</code>${escapeHtml(item.reason)}</div>
        </div></div>`).join('')}
      </div>`);
      setModalFoot('<button class="btn btn-ghost" id="ignore-back">返回</button>');
      document.getElementById('ignore-back').addEventListener('click', () => openIgnoreModal(id));
      return;
    }
    setModalBody(`
      <div class="kv"><span>目标文件</span><strong class="full-text">${escapeHtml(preview.target)}</span></div>
      <div class="kv"><span>文件状态</span><strong>${preview.exists ? '已存在，将在末尾追加' : '不存在，将新建'}</strong></div>
      <div class="kv"><span>换行风格</span><strong>${preview.newline === 'crlf' ? 'CRLF' : 'LF'}</strong></div>
      <div class="section-label" style="margin-top:12px">将要追加的规则</div>
      <pre class="preview-block full-text">${escapeHtml(preview.append)}</pre>
      ${skipped.length ? `<div class="section-label" style="margin-top:12px">已存在、将跳过</div>
        <pre class="preview-block full-text">${escapeHtml(skipped.map((item) => `${item.rule}  ← ${item.path}`).join('\n'))}</pre>` : ''}
      <details class="dirty-details" style="margin-top:12px">
        <summary>查看追加后的完整预览</summary>
        <pre class="preview-block full-text">${escapeHtml(preview.preview)}</pre>
      </details>
      <div class="hint">只会追加到仓库根 .gitignore，不覆盖原有内容；已存在的等价规则不会重复追加。</div>
      <div class="error-box" id="modal-error" hidden></div>
    `);
    setModalFoot(`<button class="btn btn-ghost" id="ignore-back">返回</button>
      <button class="btn btn-primary" id="ignore-confirm">追加并刷新</button>`);
    document.getElementById('ignore-back').addEventListener('click', () => openIgnoreModal(id));
    const confirmBtn = document.getElementById('ignore-confirm');
    confirmBtn.addEventListener('click', async () => {
      busyButton(confirmBtn, true);
      try {
        const result = await api(`/api/repos/${id}/ignore`, {
          method: 'POST',
          body: { paths },
        });
        let message = result.message || '已追加规则。';
        if (!preview.exists) message += ' 新建的 .gitignore 会以未跟踪状态出现，可一并提交。';
        await afterRepoAction(id, result, message, 6000);
      } catch (err) {
        showModalError(err.message);
        busyButton(confirmBtn, false);
      }
    });
  }

  /**
   * 动作成功后的统一收尾：更新状态、关闭弹窗、刷新并提示。
   * 参数：id 仓库主键；result 接口结果；message 提示；duration 显示时长。
   * 返回值：Promise<void>。
   */
  async function afterRepoAction(id, result, message, duration = 4000) {
    if (result.status) state.statuses.set(id, result.status);
    renderSummary();
    renderRepos();
    closeModal();
    toast(message, 'success', duration);
    await refreshOne(id, true);
  }

  // ------------------------------------------------------------------
  // 远端协议与 HTTPS 凭据
  // ------------------------------------------------------------------

  /**
   * 打开远端面板：展示协议、完整地址、SSH 身份提示与切换入口。
   * 参数：id 仓库主键。
   * 返回值：Promise<void>。
   */
  async function openRemoteModal(id) {
    const repo = repoById(id);
    if (!repo) return;
    let data;
    try {
      data = await api(`/api/repos/${id}/remotes`);
    } catch (err) {
      toast(`读取远端失败：${err.message}`, 'error', 6000);
      return;
    }
    const info = (statusById(id) || {}).remote_info || {};
    const rows = (data.remotes || []).map((remote) => {
      const meta = protocolMeta(remote.protocol);
      return `<div class="remote-line">
        <span class="label">${escapeHtml(remote.name)}</span>
        <span class="protocol-badge ${meta.cls}">${meta.label}</span>
        <code class="full-text">${escapeHtml(remote.push_url || remote.fetch_url)}</code>
        <button class="btn-link" data-copy="${escapeHtml(remote.push_url || remote.fetch_url)}" data-copy-label="远端地址">复制</button>
      </div>`;
    }).join('') || '<div class="commit-empty">该仓库未配置远端。</div>';
    const hints = [];
    if (info.https_with_ssh_identity) {
      hints.push('<div class="inline-hint is-warn"><span>SSH 身份对该仓库不生效（当前为 HTTPS 远端）。</span></div>');
    }
    if (info.credential_warning) {
      hints.push(`<div class="inline-hint is-alert"><span>${escapeHtml(info.credential_warning.message)}</span></div>`);
    }
    openModal({
      title: `远端 · ${repo.name}`,
      wide: true,
      body: `
        <div class="repo-reference">
          <div><div class="name">${escapeHtml(repo.name)}</div><div class="path">${escapeHtml(repo.path)}</div></div>
        </div>
        <div class="section-label">远端列表</div>
        ${rows}
        ${hints.join('')}
        <div class="hint">协议徽标来自 push URL；SSH 地址形如 git@github.com:owner/repo.git。</div>
        <div class="error-box" id="modal-error" hidden></div>
      `,
      foot: `<button class="btn btn-ghost" id="remote-credentials">管理 HTTPS 凭据</button>
        ${info.ssh_convertible ? '<button class="btn btn-primary" id="remote-convert">改用 SSH 地址</button>' : ''}
        <button class="btn btn-ghost" id="remote-close">关闭</button>`,
    });
    document.getElementById('remote-close').addEventListener('click', closeModal);
    document.getElementById('remote-credentials').addEventListener('click', openCredentialsModal);
    const convertBtn = document.getElementById('remote-convert');
    if (convertBtn) {
      convertBtn.addEventListener('click', () => showRemoteSshPreview(id, info.name || ''));
    }
  }

  /**
   * 展示 HTTPS→SSH 地址转换预览并允许选择 only-push / only-fetch。
   * 参数：id 仓库主键；remote 远端名称。
   * 返回值：Promise<void>。
   */
  async function showRemoteSshPreview(id, remote) {
    let preview;
    try {
      preview = await api(`/api/repos/${id}/remote/ssh/preview`, {
        method: 'POST',
        body: { remote },
      });
    } catch (err) {
      showModalError(`生成转换预览失败：${err.message}`);
      return;
    }
    if (!preview.fetch_convertible && !preview.push_convertible) {
      showModalError(preview.reason || '该远端地址不支持转换为 SSH。');
      return;
    }
    const bothDisabled = !(preview.fetch_convertible && preview.push_convertible);
    setModalBody(`
      <div class="kv"><span>远端</span><strong>${escapeHtml(preview.remote)}</strong></div>
      <div class="kv"><span>当前 fetch</span><strong class="full-text">${escapeHtml(preview.fetch_url)}</strong></div>
      <div class="kv"><span>当前 push</span><strong class="full-text">${escapeHtml(preview.push_url)}</strong></div>
      <div class="section-label" style="margin-top:14px">目标 SSH 地址</div>
      <div class="kv"><span>fetch</span><strong class="full-text">${escapeHtml(preview.ssh_fetch_url || '（不支持转换）')}</strong></div>
      <div class="kv"><span>push</span><strong class="full-text">${escapeHtml(preview.ssh_push_url || '（不支持转换）')}</strong></div>
      <label class="check" style="margin-top:12px"><input type="radio" name="ssh-mode" value="both" ${bothDisabled ? 'disabled' : 'checked'}>
        <span>同时修改 fetch 与 push（推荐）</span></label>
      <label class="check"><input type="radio" name="ssh-mode" value="push" ${bothDisabled ? 'checked' : ''} ${preview.push_convertible ? '' : 'disabled'}>
        <span>只修改 push</span></label>
      <label class="check"><input type="radio" name="ssh-mode" value="fetch" ${preview.fetch_convertible ? '' : 'disabled'}>
        <span>只修改 fetch</span></label>
      <div class="hint">仅支持 github.com / gitee.com / gitlab.com；其他域名不猜测。修改后会记录审计（remote_set_url）。</div>
      <div class="error-box" id="modal-error" hidden></div>
    `);
    setModalFoot(`<button class="btn btn-ghost" id="remote-back">返回</button>
      <button class="btn btn-primary" id="remote-confirm">确认切换</button>`);
    document.getElementById('remote-back').addEventListener('click', () => openRemoteModal(id));
    document.getElementById('remote-confirm').addEventListener('click', async () => {
      const modeInput = el.modalBody.querySelector('input[name="ssh-mode"]:checked');
      const mode = modeInput ? modeInput.value : 'both';
      const ok = await confirmDialog({
        title: '修改远端地址',
        message: `将把 ${preview.remote} 的地址切换为 SSH（模式：${mode === 'both' ? 'fetch + push' : mode === 'push' ? '仅 push' : '仅 fetch'}）。\n该修改会写入仓库配置并记录审计。`,
        confirmLabel: '切换',
      });
      if (!ok) return;
      try {
        const result = await api(`/api/repos/${id}/remote/ssh`, {
          method: 'POST',
          body: { remote: preview.remote, mode, confirm: true },
        });
        await afterRepoAction(id, result, result.message || '远端已切换为 SSH。');
      } catch (err) {
        showModalError(err.message);
      }
    });
  }

  /**
   * 打开 HTTPS 凭据面板：列出 cmdkey 中的 git 条目，支持清除与交互式登录。
   * 参数：无。
   * 返回值：Promise<void>。
   */
  async function openCredentialsModal() {
    let data;
    try {
      data = await api('/api/credentials');
    } catch (err) {
      toast(`读取凭据失败：${err.message}`, 'error', 6000);
      return;
    }
    const items = data.items || [];
    const rows = items.length ? items.map((item) => `<div class="ignore-row">
      <div class="ignore-main">
        <div class="ignore-path full-text">${escapeHtml(item.host)}</div>
        <div class="ignore-meta"><code class="rule-source full-text">${escapeHtml(item.target)}</code>
          <button class="btn-link" data-copy="${escapeHtml(item.target)}" data-copy-label="凭据目标">复制</button>
          <button class="btn-link is-danger" data-credential-delete="${escapeHtml(item.target)}">清除该 host 凭据</button>
        </div>
      </div>
    </div>`).join('') : '<div class="commit-empty">未发现 git HTTPS 凭据。</div>';
    openModal({
      title: 'HTTPS 凭据',
      wide: true,
      body: `
        <div class="hint">来自 Windows 凭据管理器（cmdkey），仅展示 host 与目标，不展示密码。</div>
        <div class="ignore-section">${rows}</div>
        <div class="hint">若推送报 403/401，可先清除对应 host 凭据，再重新登录；或把远端切换为 SSH 地址。</div>
        ${data.message ? `<div class="hint">${escapeHtml(data.message)}</div>` : ''}
        <details class="dirty-details">
          <summary>查看 cmdkey 原始输出</summary>
          <pre class="preview-block full-text">${escapeHtml(data.raw || '')}</pre>
        </details>
        <div class="error-box" id="modal-error" hidden></div>
      `,
      foot: `<button class="btn btn-ghost" id="credential-close">关闭</button>
        <button class="btn btn-primary" id="credential-login">使用此账号登录（HTTPS）</button>`,
    });
    document.getElementById('credential-close').addEventListener('click', closeModal);
    el.modalBody.querySelectorAll('[data-credential-delete]').forEach((button) => {
      button.addEventListener('click', async () => {
        const target = button.dataset.credentialDelete;
        const ok = await confirmDialog({
          title: '清除 HTTPS 凭据',
          message: `将清除凭据目标：\n${target}\n\n下次推送会要求重新登录。该操作会记录审计。`,
          confirmLabel: '清除',
          danger: true,
        });
        if (!ok) return;
        try {
          const result = await api('/api/credentials/delete', {
            method: 'POST',
            body: { target, confirm: true },
          });
          toast(result.message || '已清除凭据。', 'success', 5000);
          openCredentialsModal();
        } catch (err) {
          showModalError(err.message);
        }
      });
    });
    document.getElementById('credential-login').addEventListener('click', async () => {
      const ok = await confirmDialog({
        title: '交互式凭据登录',
        message: '将在独立控制台窗口启动 Git Credential Manager 登录。\n登录过程可能需要你在窗口中输入账号或授权，本页面不会收集密码。',
        confirmLabel: '打开登录窗口',
      });
      if (!ok) return;
      try {
        const result = await api('/api/credentials/login', {
          method: 'POST',
          body: { confirm: true },
        });
        const message = result.install_url
          ? `${result.message}\n安装地址：${result.install_url}`
          : result.message;
        if (result.ok) toast(message, 'success', 7000);
        else toast(message, 'warn', 9000);
        if (result.launched) closeModal();
      } catch (err) {
        showModalError(err.message);
      }
    });
  }

  /**
   * 渲染远端错误诊断卡片（标题、原因、可执行入口与原始输出）。
   * 参数：containerId 容器元素 id；repoId 仓库主键；err 请求异常。
   * 返回值：无。
   */
  function renderDiagnosis(containerId, repoId, err) {
    const box = document.getElementById(containerId);
    const payload = err && err.payload ? err.payload : {};
    const diagnosis = payload.diagnosis;
    if (!box || !diagnosis) return;
    const actions = (diagnosis.suggestions || []).map((item) =>
      `<button class="btn btn-ghost btn-sm" data-diag-key="${escapeHtml(item.key)}" title="${escapeHtml(item.hint || '')}">${escapeHtml(item.label)}</button>`
    ).join('');
    box.innerHTML = `<div class="diagnosis-card">
      <div class="diagnosis-title">${escapeHtml(diagnosis.title)}</div>
      <div class="diagnosis-detail">${escapeHtml(diagnosis.detail)}</div>
      <div class="diagnosis-actions">${actions}</div>
      <details class="raw-output">
        <summary>查看原始输出（可复制）</summary>
        <div class="kv"><span>命令</span><strong class="full-text">${escapeHtml(payload.command || '')}</strong></div>
        <div class="kv"><span>退出码</span><strong>${escapeHtml(String(payload.exit_code == null ? '' : payload.exit_code))}</strong></div>
        <pre class="preview-block full-text">${escapeHtml(payload.output || '')}</pre>
        <button class="btn-link" data-copy="${escapeHtml(payload.output || '')}" data-copy-label="原始输出">复制原始输出</button>
      </details>
    </div>`;
    box.querySelectorAll('[data-diag-key]').forEach((button) => {
      button.addEventListener('click', () => runDiagnosisAction(button.dataset.diagKey, repoId));
    });
  }

  /**
   * 执行诊断卡片上的动作入口。
   * 参数：key 动作标识；repoId 仓库主键。
   * 返回值：无。
   */
  function runDiagnosisAction(key, repoId) {
    if (key === 'use_ssh') {
      closeModal();
      openRemoteModal(repoId);
      return;
    }
    if (key === 'https_credentials') {
      closeModal();
      openCredentialsModal();
      return;
    }
    if (key === 'ssh_test' || key === 'manage_ssh') {
      closeModal();
      openRepoSshModal(repoId);
      return;
    }
    if (key === 'pull') {
      closeModal();
      openPullModal(repoId);
      return;
    }
    if (key === 'retry') {
      const retryBtn = document.getElementById('push-confirm') || document.getElementById('pull-confirm');
      if (retryBtn) retryBtn.click();
      return;
    }
    if (key === 'view_audit') {
      closeModal();
      switchTab('audit');
      loadAudit();
    }
  }

  function bindEvents() {
    el.tabs.forEach((tab) => tab.addEventListener('click', () => switchTab(tab.dataset.tab)));

    el.btnRefresh.addEventListener('click', () => refreshAll(false));
    el.btnAdd.addEventListener('click', openAddRepoModal);
    el.btnAddEmpty.addEventListener('click', openAddRepoModal);
    el.btnRoot.addEventListener('click', openRootsModal);
    el.btnSsh.addEventListener('click', openSshModal);
    el.btnScan.addEventListener('click', openScanModal);
    if (el.btnCredentials) el.btnCredentials.addEventListener('click', openCredentialsModal);

    el.grid.addEventListener('click', (event) => {
      const unmuteBtn = event.target.closest('[data-unmute]');
      if (unmuteBtn) {
        const repoId = Number(unmuteBtn.dataset.repo);
        unmutePath(repoId, Number(unmuteBtn.dataset.unmute));
        return;
      }
      const button = event.target.closest('[data-act]');
      if (!button) return;
      const card = event.target.closest('.repo-card');
      if (!card) return;
      handleCardAction(button.dataset.act, Number(card.dataset.id));
    });

    // 统一的“复制”按钮（data-copy 携带完整文本）
    document.addEventListener('click', (event) => {
      const copyBtn = event.target.closest('[data-copy]');
      if (!copyBtn) return;
      event.preventDefault();
      copyText(copyBtn.dataset.copy, copyBtn.dataset.copyLabel || '内容');
    });

    el.modalClose.addEventListener('click', closeModal);
    el.backdrop.addEventListener('click', (event) => {
      // 危险确认不允许点击遮罩关闭，必须显式点“取消”
      if (event.target === el.backdrop && state.modal.dismissible) closeModal();
    });
    el.confirmClose.addEventListener('click', () => closeConfirm(false));
    el.confirmBackdrop.addEventListener('click', (event) => {
      if (event.target === el.confirmBackdrop && state.confirm.dismissible) {
        closeConfirm(false);
      }
    });
    document.addEventListener('keydown', (event) => {
      const confirmOpen = !el.confirmBackdrop.hidden;
      const modalOpen = !el.backdrop.hidden;
      if (!confirmOpen && !modalOpen) return;
      if (event.key === 'Escape') {
        event.preventDefault();
        if (confirmOpen) closeConfirm(false);
        else closeModal();
        return;
      }
      // 焦点陷阱：确认层优先，其次业务弹窗
      if (event.key !== 'Tab') return;
      const scope = confirmOpen ? el.confirmModal : el.modal;
      const focusables = Array.from(scope.querySelectorAll(
        'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]),'
        + ' textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
      )).filter((node) => node.offsetParent !== null);
      if (!focusables.length) return;
      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    });

    el.auditRefresh.addEventListener('click', () => {
      state.audit.page = 1;
      loadAudit();
    });
    el.auditRepo.addEventListener('change', () => {
      state.audit.repoId = el.auditRepo.value;
      state.audit.page = 1;
      loadAudit();
    });
    el.auditAction.addEventListener('change', () => {
      state.audit.action = el.auditAction.value;
      state.audit.page = 1;
      loadAudit();
    });
    el.auditFailed.addEventListener('change', () => {
      state.audit.onlyFailed = el.auditFailed.checked;
      state.audit.page = 1;
      loadAudit();
    });
    el.auditFirst.addEventListener('click', () => gotoAuditPage(1));
    el.auditPrev.addEventListener('click', () => gotoAuditPage(state.audit.page - 1));
    el.auditNext.addEventListener('click', () => gotoAuditPage(state.audit.page + 1));
    el.auditLast.addEventListener('click', () => gotoAuditPage(state.audit.totalPages));
    el.auditGoto.addEventListener('click', () => gotoAuditPage(el.auditPage.value));
    el.auditPage.addEventListener('keydown', (event) => {
      if (event.key !== 'Enter') return;
      event.preventDefault();
      gotoAuditPage(el.auditPage.value);
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
