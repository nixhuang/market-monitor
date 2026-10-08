(function (window) {
  'use strict';

  if (window.location && window.location.origin && window.location.origin !== 'https://nixhuang.github.io') {
    const show = () => {
      ['runState', 'runMsg', 'appliedState', 'appliedBrief'].forEach(id => {
        const node = window.document.getElementById(id);
        if (node) node.textContent = '本地预览：尚未提交 GitHub，不会触发线上运行';
      });
      ['runLight', 'ruleLight'].forEach(id => {
        const node = window.document.getElementById(id);
        if (node) node.dataset.phase = 'idle';
      });
      [['runLightTxt', '本地预览 · 行情为历史快照'], ['ruleLightTxt', '本地预览 · 尚未发布']].forEach(([id, text]) => {
        const node = window.document.getElementById(id);
        if (node) node.textContent = text;
      });
      ['btnRunNow', 'btnCheckStatus'].forEach(id => {
        const node = window.document.getElementById(id);
        if (node) node.disabled = true;
      });
      return {phase: 'preview', message: '仅本地预览'};
    };
    window.MMRunStatus = {init: () => { show(); return window.MMRunStatus; },
      refresh: async () => show(), watchConfig: async () => show(), startManual: async () => show()};
    show();
    return;
  }

  const MAX_WAIT = 10 * 60 * 1000;
  const RETRY_WAIT = 5 * 60 * 1000;
  const SHA = /^[a-f0-9]{40,64}$/i;
  const FILES = ['holdings.json', 'settings.json'];
  const terminal = new Set(['published', 'failed', 'expired']);
  let options = {}, initialized = false, timer = null, controller = null, version = 0;
  let targets = {}, receiptVersion = 0, configSince = 0, configRun = null, manual = null;
  let latestStatus = null, verifiedSnapshot = null, actionsError = '';
  let configState = {phase: 'idle', message: '尚无本页提交待验证'};
  let manualState = {phase: 'idle', message: ''};
  let publication = {phase: 'idle', message: '正在读取公开发布状态…'};
  let retryPending = false, retrySince = 0;
  // 普通打开页面时，若一次没读到公开状态，自动再试两次，避免停在“没读到”。
  const PUBLIC_RETRY_MAX = 2;
  let publicRetry = 0;
  let authNotice = '', checkFeedback = '', checkPhase = 'idle';
  let checkRequest = 0, checkDismissed = false;
  let latestRules = null, rulesError = '', ruleSince = 0;

  function getToken() {
    if (options.getToken) {
      try { return options.getToken() || ''; } catch (_) { return ''; }
    }
    try { return window.localStorage.getItem('mm_gh_token_v1') || ''; } catch (_) { return ''; }
  }
  function saveToken(t) {
    try { window.localStorage.setItem('mm_gh_token_v1', String(t || '').trim()); return true; }
    catch (_) { return false; }
  }
  function looksLikeToken(t) {
    return /^(gh[pousr]_|github_pat_)/.test(String(t || '').trim());
  }
  function apiURL(path) {
    return 'https://api.github.com/repos/' + encodeURIComponent(options.owner) + '/' +
      encodeURIComponent(options.repo) + path;
  }
  function bust(path) {
    return path + (path.includes('?') ? '&' : '?') + '_mm=' + Date.now() + '-' + version;
  }
  let apiBlockedUntil = 0;
  function authMessage(status, message = '', remaining = null, required = '', retryAt = 0, authenticated = false) {
    const isLimit = status === 429 || status === 403 && (remaining === '0' || /rate limit|secondary rate|abuse detection/i.test(message));
    if (isLimit) {
      const wait = retryAt ? '；请在 ' + new Date(retryAt).toLocaleTimeString('zh-CN') + ' 后再试' : '；请稍后再试';
      return {kind: 'rate_limit', text: status + '：GitHub API 限流，不代表令牌过期' + wait};
    }
    if (status === 401) return {kind: 'auth', text: '401：令牌无效、被撤销或过期，请在设置页更新'};
    if (status === 403 && /resource not accessible/i.test(message)) {
      return {kind: 'permission', text: '403：此令牌无权执行这项操作，不是到期失效；请确认授权仓库 nixhuang/market-monitor，立即运行需要 Actions: Read and write，编辑需要 Contents: Read and write' +
        (required ? '；接口要求 ' + required : '')};
    }
    if (status === 403) return {kind: 'forbidden', text: '403：GitHub 拒绝请求，尚不能确定是权限或访问限制；' +
      (authenticated ? '已随请求携带本机令牌，不代表令牌已过期' : '本次请求未携带令牌，请在同一浏览器的设置页填写')};
    if (status === 404) return {kind: 'not_found', text: '404：找不到 workflow/run，或令牌未获授权访问这个仓库；请核对仓库选择'};
    return {kind: 'http', text: 'HTTP ' + status};
  }
  // 令牌只发送给 GitHub API；超时覆盖请求和响应体读取。
  async function request(url, {method = 'GET', body, github = false, signal, text = false} = {}) {
    if (github && Date.now() < apiBlockedUntil) {
      const error = new Error(authMessage(429, '', '0', '', apiBlockedUntil, !!getToken()).text);
      error.status = 429; error.kind = 'rate_limit'; error.retryAt = apiBlockedUntil;
      throw error;
    }
    const local = new AbortController();
    const abort = () => local.abort();
    if (signal) {
      if (signal.aborted) local.abort();
      else signal.addEventListener('abort', abort, {once: true});
    }
    const headers = {};
    if (github) {
      headers.Accept = 'application/vnd.github+json';
      const token = getToken();
      if (token) headers.Authorization = 'Bearer ' + token;
    }
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    let timeout;
    const expired = new Promise((_, reject) => {
      timeout = window.setTimeout(() => {
        local.abort();
        reject(new Error('网络请求超时；可稍后查运行状态，不代表运行失败'));
      }, options.timeoutMs);
    });
    try {
      return await Promise.race([expired, (async () => {
        const response = await window.fetch(url, {
          method, headers, cache: 'no-store', signal: local.signal,
          ...(body !== undefined ? {body: JSON.stringify(body)} : {})
        });
        if (!response.ok) {
          let diagnostic = {kind: 'http', text: response.status === 404
            ? 'status.json 尚未发布或页面不存在（404）' : '公开页面 HTTP ' + response.status};
          let retryAt = 0;
          if (github) {
            let payload = {};
            try { payload = await response.json(); } catch (_) {}
            const header = name => response.headers && response.headers.get ? response.headers.get(name) : null;
            const remaining = header('x-ratelimit-remaining');
            const retry = Number(header('retry-after'));
            const reset = Number(header('x-ratelimit-reset'));
            const message = String(payload.message || '');
            if (response.status === 429 || response.status === 403 && (remaining === '0' || /rate limit|secondary rate|abuse detection/i.test(message))) {
              retryAt = Math.max(Date.now() + 60000, retry > 0 ? Date.now() + retry * 1000 : 0,
                remaining === '0' && reset > 0 ? reset * 1000 : 0);
              apiBlockedUntil = Math.max(apiBlockedUntil, retryAt);
            }
            diagnostic = authMessage(response.status, message, remaining,
              header('x-accepted-github-permissions') || '', retryAt, !!headers.Authorization);
          }
          const error = new Error(diagnostic.text);
          error.status = response.status;
          error.kind = diagnostic.kind;
          error.retryAt = retryAt;
          throw error;
        }
        if (response.status === 204) return null;
        return text ? await response.text() : await response.json();
      })()]);
    } finally {
      window.clearTimeout(timeout);
      if (signal) signal.removeEventListener('abort', abort);
    }
  }
  function schema(value) {
    return value && typeof value.run_id === 'string' && value.run_id.length > 0 &&
      value.config_files && FILES.every(file => SHA.test(value.config_files[file] || ''));
  }
  function configMatches(value, expected) {
    if (!schema(value) || !Object.entries(expected).every(([file, receipt]) =>
      value.config_files[file] === receipt.blobSHA)) return false;
    const newest = Object.values(expected).reduce((a, b) => a.revision > b.revision ? a : b);
    // 恢复旧内容会得到相同 blob：必须有本次提交或之后生成的运行证据。
    return value.source_sha === newest.commitSHA ||
      (configRun && value.run_id === String(configRun.id)) ||
      Date.parse(value.started_at) >= newest.savedAt;
  }
  function samePublication(status, snapshot) {
    return schema(status) && schema(snapshot) && status.run_id === snapshot.run_id &&
      (status.request_id || '') === (snapshot.request_id || '') &&
      FILES.every(file => status.config_files[file] === snapshot.config_files[file]);
  }
  function manualMatches(value, target) {
    return schema(value) && target && value.request_id === target.request_id &&
      (!target.run_id || value.run_id === target.run_id);
  }
  function activeConfig() { return Object.keys(targets).length > 0 && !terminal.has(configState.phase); }
  function activeManual() { return manual && !terminal.has(manualState.phase); }
  function cancelPoll() {
    version++;
    window.clearTimeout(timer);
    timer = null;
    if (controller) controller.abort();
    controller = null;
  }
  function state() {
    const primary = activeManual() ? manualState :
      (Object.keys(targets).length ? configState : (manual ? manualState : publication));
    return {
      phase: primary.phase, message: primary.message,
      run_id: manual && manual.run_id || configRun && String(configRun.id) || latestStatus && latestStatus.run_id || null,
      request_id: manual && manual.request_id || null,
      pending: JSON.parse(JSON.stringify(targets)),
      config: {...configState}, manual: {...manualState}, publication: {...publication},
      status: latestStatus, snapshot: verifiedSnapshot, verified: !!verifiedSnapshot,
      actionsError
    };
  }
  function summaryText(s) {
    if (!s) return '本页未找到运行摘要';
    const summary = s.summary || {}, counts = s.list_counts || {}, dates = s.actual_dates || {};
    const list = value => Array.isArray(value) ? value.join(', ') || '无' : value == null ? '未知' : String(value);
    return [
      '本页生成数据 run ' + s.run_id + ' · 事件 ' + (s.event || '未知') + ' · 源提交 ' + (s.source_sha || '未知'),
      '开始 ' + (s.started_at_bj || s.started_at || '未知') + ' · 完成 ' + (s.finished_at_bj || s.finished_at || '未知') + ' · 目标交易日 ' + (s.target_trade_date || '未知'),
      '运行计划 ' + (s.schedule || '未记录') + ' · 数据模式 ' + (s.mode || '未知'),
      '红 ' + (summary.red ?? '未知') + ' / 黄 ' + (summary.yellow ?? '未知') + ' / 绿 ' + (summary.green ?? '未知') +
        ' / 灰 ' + (summary.gray ?? '未知') + ' / 总数 ' + (summary.total ?? '未知') + ' · 宏观有效 ' + (summary.macro_ok ?? '未知'),
      (s.groups || [{key: 'positions', label: '持仓'}, {key: 'focus', label: '重点关注'}])
        .map(g => g.label + ' ' + (counts[g.key] ?? 0)).join(' · ') +
        ' · 设了加仓价 ' + (counts.triggers ?? '未知') + ' 只 · 定投提醒 ' + (counts.dca ? '已开启' : '未开启'),
      '行情实际日期 ' + (dates.min || '未知') + ' ～ ' + (dates.max || '未知') +
        ' · 过期 ' + list(summary.stale_symbols) + ' · 缺失 ' + list(summary.missing_symbols),
      '覆盖情况 ' + JSON.stringify(s.coverage ?? null),
      '生效规则 ' + JSON.stringify(s.effective_settings || {}),
      '配置 blob ' + JSON.stringify(s.config_files)
    ].join('\n');
  }
  function el(id) { return window.document.getElementById(id); }
  function pageSnapshot() {
    const embedded = el('snapshotData');
    if (!embedded) return null;
    try {
      const value = JSON.parse(embedded.textContent);
      return schema(value) ? value : null;
    } catch (_) { return null; }
  }
  function shortSummary(s) {
    const info = s.summary || {}, dates = s.actual_dates || {};
    const bad = [...(info.stale_symbols || []), ...(info.missing_symbols || [])];
    return '数据日期 ' + (dates.max || '未知') + ' · 红' + (info.red ?? 0) +
      ' 黄' + (info.yellow ?? 0) + ' 绿' + (info.green ?? 0) +
      (info.gray ? ' 灰' + info.gray : '') + ' · ' +
      (bad.length ? bad.length + ' 只未更新' : (info.total ?? 0) + ' 只全部更新') +
      ' · 规则已按当前设置生成';
  }
  function compactLegacySummary(s) {
    const detail = el('runSummary');
    const card = detail && detail.parentElement;
    if (!s || !card || card.classList.contains('sumcard')) return;
    const heading = card.querySelector('h2');
    if (!heading || !heading.textContent.includes('本次运行与生效配置摘要')) return;
    card.classList.add('sumcard');
    heading.remove();
    const line = window.document.createElement('div');
    line.className = 'sumline';
    line.style.cssText = 'padding:10px 14px;font-size:13px;line-height:1.6';
    line.textContent = shortSummary(s);
    const disclosure = window.document.createElement('details');
    disclosure.style.padding = '0 6px 8px';
    const title = window.document.createElement('summary');
    title.textContent = '运行详情（给核对用，平时不用看）';
    title.style.cssText = 'font-size:11.5px;color:#6ba3f0;cursor:pointer;padding:2px 8px';
    card.insertBefore(line, detail);
    card.insertBefore(disclosure, detail);
    disclosure.append(title, detail);
  }
  function networkMessage(error) {
    if (!error || error.name === 'TypeError' || error.message === 'Failed to fetch' || error.message === 'NetworkError when attempting to fetch resource.') {
      return '网络暂时无法核对最新发布；本页已生成的数据仍可查看，请稍后查运行状态';
    }
    return error.message || '暂时无法核对最新发布；本页数据仍可查看';
  }
  function completedTime(s) {
    const raw = s && (s.finished_at_bj || s.finished_at);
    if (!raw) return '';
    const value = String(raw);
    if (!/(?:Z|[+-]\d{2}:\d{2})$/i.test(value)) return '';
    const date = new Date(value);
    if (!Number.isFinite(date.getTime())) return '';
    return date.toLocaleString('sv-SE', {timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit',
      day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false});
  }
  function humanTime(s) {
    const raw = s && (s.finished_at_bj || s.started_at_bj || s.finished_at || s.started_at);
    if (!raw) return '';
    const match = String(raw).match(/(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})/);
    return match ? match[1] + ' ' + match[2] : String(raw);
  }
  function dataTimeText(s) {
    if (s && s.data_time_text) return s.data_time_text;
    const dates = (s && s.actual_dates) || {};
    const date = dates.max || dates.min || '';
    return date ? date + (s && s.mode === 'closed' ? ' 收盘（美东交易日）' : '') : '';
  }
  // 上次成功读到的看板状态：编辑页没有内嵌快照，网络失败时用它兜底，避免一律显示“未知”。
  const LAST_KEY = 'mm_last_pub_v1';
  function loadLast() {
    try {
      const raw = window.localStorage && window.localStorage.getItem(LAST_KEY);
      const v = raw ? JSON.parse(raw) : null;
      return (v && v.dataTime) ? v : null;
    } catch (_) { return null; }
  }
  function saveLast(s) {
    const dt = dataTimeText(s);
    if (!dt || !s) return;
    try {
      if (window.localStorage) window.localStorage.setItem(LAST_KEY,
        JSON.stringify({dataTime: dt, when: humanTime(s), run_id: s.run_id, at: Date.now()}));
    } catch (_) { /* 隐私模式不能存储时忽略 */ }
  }
  function plainApplied() {
    const when = humanTime(verifiedSnapshot || latestStatus);
    if (activeManual()) {
      const phase = manualState.phase;
      if (phase === 'dispatching' || phase === 'queued') return '运行已提交，正在更新看板，通常约 5–9 分钟…';
      if (phase === 'running') return '正在运行，正在抓最新行情…';
      if (phase === 'generated') return '运行已生成，正在发布到看板…';
      if (phase === 'failed') return '运行失败：' + manualState.message.replace(/^派发失败：/, '');
      if (phase === 'expired') return '运行跟踪超时，请点查运行状态确认，不代表失败';
    }
    if (Object.keys(targets).length) {
      const phase = configState.phase;
      if (phase === 'published') return '已生效：看板已按你保存的设置更新' + (when ? '（' + when + '）' : '');
      if (phase === 'failed') return '保存未生效：本次运行失败，请查看运行记录';
      if (phase === 'expired') return '保存已提交，但还没验证到生效；请点查运行状态确认';
      return '保存成功，看板正在更新，通常约 5–9 分钟…';
    }
    if (publication.phase === 'published' && (verifiedSnapshot || latestStatus)) {
      return '看板最新数据时间 ' + (dataTimeText(verifiedSnapshot || latestStatus) || '未知') +
        ' · 看板更新于 ' + (when || '未知');
    }
    if (publication.phase === 'waiting') {
      const last = loadLast();
      if (last) return '这次没读到看板最新状态（多为网络问题）。上次读到的数据时间 ' + last.dataTime +
        (last.when ? ' · 看板更新于 ' + last.when : '') + '；已保存的内容不受影响';
      return '这次没读到看板状态（多为网络问题）；已保存的内容不受影响，可点「查运行状态」重试';
    }
    return '还没在这台设备保存过修改';
  }
  let reloadStarted = false, reloadDeferred = false;
  function reloadCurrent(force = false) {
    if (!verifiedSnapshot || publication.phase !== 'published' || rulesError || !latestRules ||
        verifiedSnapshot.config_files['settings.json'] !== latestRules.sha) return false;
    const receipt = targets['settings.json'];
    if (receipt && configState.phase !== 'published') return false;
    if (!window.location || typeof window.location.replace !== 'function') return false;
    const url = new URL(window.location.href);
    if (!force && (reloadStarted || url.searchParams.get('mm_refreshed_run') === verifiedSnapshot.run_id)) return false;
    try {
      if (options.canReload && !options.canReload()) { reloadDeferred = true; return false; }
    } catch (_) { reloadDeferred = true; return false; }
    reloadDeferred = false; reloadStarted = true;
    url.searchParams.set('_mm', String(Date.now()));
    url.searchParams.set('mm_refreshed_run', verifiedSnapshot.run_id);
    window.location.replace(url.href);
    return true;
  }
  function ruleState() {
    const snap = el('snapshotData') ? pageSnapshot() : verifiedSnapshot;
    const appliedSHA = snap && snap.config_files['settings.json'];
    if (rulesError) return {phase: 'idle', text: '暂无法核对最新规则；请稍后查运行状态'};
    if (!latestRules) return {phase: 'idle', text: '正在核对最新规则…'};
    const receipt = targets['settings.json'];
    const expectedSHA = receipt && receipt.savedAt > latestRules.checkedAt
      ? receipt.blobSHA : latestRules.sha;
    if (appliedSHA === expectedSHA) return {phase: 'ok', text: '规则按当前设置生效'};
    if (!appliedSHA) return {phase: 'idle', text: '未读到有效看板快照，暂无法核对规则'};
    if (verifiedSnapshot && verifiedSnapshot.config_files['settings.json'] === expectedSHA) {
      return {phase: 'busy', text: reloadDeferred ? '新规则已发布；有未保存编辑，保存后将自动刷新当前页'
        : '新规则已发布，本页仍是旧规则；正在刷新当前页', reload: true};
    }
    const phase = receipt && receipt.blobSHA === expectedSHA && configState.phase === 'failed'
      ? 'failed' : latestRules.run && phaseForRun(latestRules.run).phase;
    if (phase === 'failed') return {phase: 'bad', text: '规则未生效：对应运行失败，请查看运行记录'};
    if (ruleSince && Date.now() - ruleSince >= MAX_WAIT) {
      return {phase: 'bad', text: '规则尚未生效：本页仍用旧设置，等待已超过 10 分钟；请查运行状态（不代表运行失败）'};
    }
    if (phase === 'generated') return {phase: 'busy', text: '新规则已生成，正在等待看板发布…'};
    return {phase: 'busy', text: '规则已保存，正在重跑…（通常约 5–9 分钟）'};
  }
  function lightState() {
    // 点了「立即运行」但没令牌/令牌不对：直接把原因写在灯上，否则按钮看着像坏了
    if (authNotice) return {phase: 'bad', text: authNotice};
    const current = pageSnapshot();
    const trackedPublished = manual && !manual.rejected && manualState.phase === 'published' ||
      Object.keys(targets).length && configState.phase === 'published';
    const snap = trackedPublished && verifiedSnapshot ? verifiedSnapshot : current || verifiedSnapshot || latestStatus;
    const oldPage = current && snap && !samePublication(current, snap);
    const finished = completedTime(snap);
    const kind = {schedule: '自动', workflow_dispatch: '手动', push: '保存后'}[snap && snap.event] || '';
    const success = kind + '抓取成功 · 完成于 ' + (finished || '未记录') + '（北京时间） · ';
    const suffix = oldPage ? ' · 新结果已发布，本页仍是旧数据' : '';
    const info = snap ? (snap.summary || {}) : {};
    const cnt = info.red != null
      ? '红' + (info.red ?? 0) + ' 黄' + (info.yellow ?? 0) + ' 绿' + (info.green ?? 0) +
        (info.gray ? ' 灰' + info.gray : '')
      : '';
    const busyText = phase => {
      if (phase === 'dispatching' || phase === 'queued') return '正在提交运行请求…';
      if (phase === 'running') return '正在抓取最新行情…';
      if (phase === 'generated') return '抓取完成，正在发布到看板…';
      return '正在抓取…';
    };
    if (manual && manualState.phase === 'failed' && (!manual.rejected || !checkFeedback || publication.phase !== 'published')) {
      return {phase: 'bad', text: manual.rejected ? manualState.message
        : '运行失败：' + manualState.message.replace(/^派发失败：/, '')};
    }
    if (activeManual()) return {phase: 'busy', text: busyText(manualState.phase)};
    if (Object.keys(targets).length) {
      if (configState.phase === 'failed') return {phase: 'bad', text: '运行失败：本次运行未成功'};
      if (activeConfig()) return {phase: 'busy', text: busyText(configState.phase)};
    }
    if (snap && info.red != null) {
      const bad = [...new Set([...(info.stale_symbols || []), ...(info.missing_symbols || [])])];
      if (bad.length) {
        return {phase: 'bad', text: ((info.stale_symbols || []).length ? '当日行情未取得 ' : '取数失败 ') +
          bad.length + ' 只：' + bad.slice(0, 6).join('、') + (bad.length > 6 ? ' 等' : '')};
      }
      const unsupported = (info.unsupported_symbols || []).length;
      const total = info.total || 0;
      if (!total) return {phase: 'idle', text: Object.values(snap.registered_counts || {}).some(n => n > 0)
        ? '没有开启的监测标的，未抓取报价' : '清单为空，未抓取报价'};
      if (unsupported >= total) return {phase: 'idle', text: '清单中 ' + unsupported + ' 个特殊代码暂不支持报价，未抓取报价'};
      if (unsupported) {
        return {phase: 'ok', text: success + cnt + ' · ' +
          (total - unsupported) + ' 项数据已更新 · ' + unsupported + ' 个特殊代码暂不支持报价' + suffix, reload: !!oldPage};
      }
      return {phase: 'ok', text: success + cnt + ' · ' + total + ' 项数据已更新' + suffix, reload: !!oldPage};
    }
    if (publication.phase === 'waiting') return {phase: 'busy', text: '正在读取运行状态…'};
    return {phase: 'idle', text: '点「立即运行」抓最新行情'};
  }
  function put(id, text, phase) {
    const box = el(id);
    if (!box) return;
    box.textContent = text;
    box.style.whiteSpace = 'pre-wrap';
    box.dataset.phase = phase;
    box.setAttribute('aria-live', 'polite');
  }
  function render() {
    const s = state();
    let message = s.message;
    const embedded = el('snapshotData');
    if (s.phase === 'published' && embedded && verifiedSnapshot) {
      try {
        if (!samePublication(verifiedSnapshot, JSON.parse(embedded.textContent))) {
          message += '；当前页面仍是旧快照，可点击「刷新当前看板」查看新版本';
        }
      } catch (_) { message += '；当前页快照无效，请刷新当前看板'; }
    }
    if (actionsError && (activeManual() || activeConfig())) {
      message += '\n运行记录暂时无法读取：' + actionsError;
    }
    put('runState', message, s.phase);
    put('runMsg', message, s.phase);
    if (el('checkResult')) {
      el('checkResult').hidden = !checkFeedback;
      put('checkResult', checkFeedback, checkPhase);
      if (checkFeedback) {
        const close = window.document.createElement('button');
        close.type = 'button';
        close.textContent = '关闭';
        close.setAttribute('aria-label', '关闭运行状态核对结果');
        close.style.cssText = 'float:right;margin:0 0 6px 10px;cursor:pointer';
        close.onclick = () => {
          checkDismissed = true;
          checkFeedback = '';
          checkPhase = 'idle';
          render();
        };
        el('checkResult').appendChild(close);
      }
      if (checkFeedback && publication.phase === 'published' && verifiedSnapshot &&
          pageSnapshot() && !samePublication(pageSnapshot(), verifiedSnapshot)) {
        const link = window.document.createElement('a');
        link.textContent = '刷新当前看板';
        link.href = '#';
        link.onclick = event => { event.preventDefault(); reloadCurrent(true); };
        link.style.cssText = 'display:block;margin-top:6px;color:#6ba3f0';
        el('checkResult').appendChild(link);
      }
    }
    const light = lightState();
    if (el('runLight')) el('runLight').dataset.phase = light.phase;
    put('runLightTxt', light.text, light.phase);
    if (light.reload && el('runLightTxt')) {
      const link = window.document.createElement('a');
      link.textContent = '刷新当前看板';
      link.href = '#';
      link.onclick = event => { event.preventDefault(); reloadCurrent(true); };
      link.style.cssText = 'display:inline-block;margin-left:8px;color:#6ba3f0';
      el('runLightTxt').appendChild(link);
    }
    const rule = ruleState();
    if (el('ruleLight')) el('ruleLight').dataset.phase = rule.phase;
    put('ruleLightTxt', rule.text, rule.phase);
    if (rule.reload && el('ruleLightTxt') && reloadDeferred) {
      put('ruleLightTxt', '新规则已发布；有未保存编辑，保存后将自动刷新当前页', 'busy');
    }
    const currentPage = pageSnapshot();
    compactLegacySummary(currentPage);
    // 运行详情现在只在编辑页展示（首页已移除）：优先本页内嵌快照，其次网络读到的最新状态。
    const detailSnap = currentPage || verifiedSnapshot || latestStatus;
    put('runSummary',
      (detailSnap ? summaryText(detailSnap) : '还没读到本次运行的详情；点「查运行状态」核对一次') +
      '\n' + publication.message, publication.phase);
    const appliedText = plainApplied();
    const appliedPhase = activeManual() ? manualState.phase : configState.phase;
    // 编辑页只回答“我保存的东西生效了吗”，详细核对字段留在首页；折叠时摘要行同步一句。
    put('appliedState', appliedText, appliedPhase);
    put('appliedBrief', appliedText, appliedPhase);
    if (el('dataTime')) {
      const snap = verifiedSnapshot || latestStatus || pageSnapshot();
      const dt = dataTimeText(snap);
      if (dt) put('dataTime', '数据时间 ' + dt, publication.phase);
      else {
        const last = loadLast();
        put('dataTime', last ? '数据时间 ' + last.dataTime + '（上次读到，这次没取到最新）'
          : '数据时间 未能读取（不影响已保存内容）', publication.phase);
      }
    }
    ['btnRun', 'btnRunNow'].forEach(id => { if (el(id)) el(id).disabled = !!activeManual(); });
    const box = el('appliedState') || el('runMsg') || el('runState');
    if (box && verifiedSnapshot && (!Object.keys(targets).length || configState.phase === 'published') &&
        (!manual || manual.rejected || manualState.phase === 'published')) {
      const link = window.document.createElement('a');
      link.textContent = '返回看板';
      link.href = bust('./index.html') + '&mm_run=' + encodeURIComponent(verifiedSnapshot.run_id);
      link.style.cssText = 'display:inline-block;margin:8px;color:#6ba3f0';
      box.appendChild(link);
    }
    if (typeof options.onChange === 'function') options.onChange(s);
    const receipt = targets['settings.json'];
    if (rule.reload || receipt && configState.phase === 'published') {
      reloadCurrent();
      if (reloadDeferred) {
        const note = '新规则已发布；有未保存编辑，保存后将自动刷新当前页';
        put('ruleLightTxt', note, 'busy');
        put('appliedBrief', note, 'busy');
      }
    }
    return s;
  }
  function phaseForRun(run) {
    if (!run) return {phase: 'queued', message: '等待定位对应运行；尚未确认开始'};
    const id = String(run.id);
    if (run.status === 'completed') {
      if (run.conclusion === 'success') return {phase: 'generated', message: 'run ' + id + ' 生成成功，尚未验证 Pages 发布'};
      return {phase: 'failed', message: 'run ' + id + ' 未成功：' + (run.conclusion || '结论未知')};
    }
    if (run.status === 'in_progress') return {phase: 'running', message: 'run ' + id + ' 正在运行'};
    return {phase: 'queued', message: 'run ' + id + ' 排队/等待中（' + run.status + '）'};
  }
  async function findRun(query, match, signal) {
    // run-name 在 GitHub REST 中对应 display_title，绝不以“最新一次”代替 request_id。
    for (let page = 1; page <= 3; page++) {
      const data = await request(apiURL('/actions/workflows/daily.yml/runs?branch=' +
        encodeURIComponent(options.branch) + '&per_page=100&page=' + page + query), {github: true, signal});
      const runs = data.workflow_runs || [];
      const found = runs.find(match);
      if (found) return found;
      if (runs.length < 100) break;
    }
    return null;
  }
  function schedule() {
    const ruleWaiting = ruleSince > 0 && Date.now() - ruleSince < MAX_WAIT && ruleState().phase === 'busy';
    const tracking = activeConfig() || activeManual() || retryPending || reloadDeferred;
    if (!tracking && !ruleWaiting && publicRetry >= PUBLIC_RETRY_MAX) return;
    const since = Math.min(activeConfig() ? configSince : Infinity, activeManual() ? manual.since : Infinity);
    const delay = Math.max(apiBlockedUntil - Date.now(), tracking ? (Date.now() - since < 120000 ? 8000 : 20000)
      : ruleWaiting ? 60000 : (publicRetry === 0 ? 4000 : 10000));
    timer = window.setTimeout(() => { void tick(); }, delay);
  }
  async function tick() {
    cancelPoll();
    const epoch = version;
    retryPending = retrySince > 0 && Date.now() - retrySince < RETRY_WAIT;
    controller = new AbortController();
    const signal = controller.signal;
    const expected = JSON.parse(JSON.stringify(targets));
    const manualTarget = manual ? {...manual} : null;
    let status = null, snapshot = null, publicError = '', actionError = '';
    let nextConfigRun = configRun, nextManualRun = null;
    const publicTask = (async () => {
      try {
        status = await request(bust('./status.json'), {signal});
        if (!schema(status)) throw new Error('status.json 缺少有效 run_id/配置 blob SHA，无法验证生效');
        const html = await request(bust('./index.html'), {signal, text: true});
        const parsed = new window.DOMParser().parseFromString(html, 'text/html');
        const node = parsed.querySelector('script#snapshotData');
        if (!node) throw new Error('看板缺少 snapshotData；生成成功不等于已发布');
        const candidate = JSON.parse(node.textContent);
        if (samePublication(status, candidate)) snapshot = candidate;
        else publicError = 'status.json 与看板快照不一致，仍在等待 Pages 发布';
      } catch (error) { publicError = networkMessage(error); }
    })();
    const actionTask = (async () => {
      try {
        if (manualTarget && !manualTarget.rejected) {
          nextManualRun = manualTarget.run_id
            ? await request(apiURL('/actions/runs/' + encodeURIComponent(manualTarget.run_id)), {github: true, signal})
            : await findRun('&event=workflow_dispatch', run =>
              run.event === 'workflow_dispatch' && (run.display_title || '').includes(manualTarget.request_id), signal);
        }
        if (Object.keys(expected).length) {
          const newest = Object.values(expected).reduce((a, b) => a.revision > b.revision ? a : b);
          nextConfigRun = configRun
            ? await request(apiURL('/actions/runs/' + encodeURIComponent(configRun.id)), {github: true, signal})
            : await findRun('&event=push', run => run.event === 'push' && run.head_sha === newest.commitSHA, signal);
        }
      } catch (error) { actionError = networkMessage(error); }
    })();
    let nextRules = null, nextRulesError = '';
    const rulesTask = (async () => {
      const checkedAt = Date.now();
      try {
        const settings = await request(bust(apiURL('/contents/settings.json?ref=' +
          encodeURIComponent(options.branch))), {github: true, signal});
        if (!SHA.test(settings && settings.sha || '')) throw new Error('最新规则缺少有效 blob SHA');
        nextRules = {sha: settings.sha, checkedAt, run: null};
      } catch (error) {
        try {
          const raw = await request(bust('https://raw.githubusercontent.com/' +
            encodeURIComponent(options.owner) + '/' + encodeURIComponent(options.repo) + '/' +
            encodeURIComponent(options.branch) + '/settings.json'), {signal, text: true});
          JSON.parse(raw);
          const bytes = new TextEncoder().encode(raw);
          const header = new TextEncoder().encode('blob ' + bytes.length + '\0');
          const blob = new Uint8Array(header.length + bytes.length);
          blob.set(header); blob.set(bytes, header.length);
          const hash = await window.crypto.subtle.digest('SHA-1', blob);
          nextRules = {sha: Array.from(new Uint8Array(hash), b => b.toString(16).padStart(2, '0')).join(''), checkedAt, run: null};
        } catch (_) {
          nextRulesError = networkMessage(error);
          return;
        }
      }
      const current = el('snapshotData') ? pageSnapshot() : verifiedSnapshot;
      if (current && current.config_files['settings.json'] === nextRules.sha) return;
      try {
        const commits = await request(bust(apiURL('/commits?path=settings.json&sha=' +
          encodeURIComponent(options.branch) + '&per_page=1')), {github: true, signal});
        const commit = Array.isArray(commits) && commits[0];
        if (commit && SHA.test(commit.sha || '')) {
          const atCommit = await request(apiURL('/contents/settings.json?ref=' +
            encodeURIComponent(commit.sha)), {github: true, signal});
          if (atCommit && atCommit.sha === nextRules.sha) {
            nextRules.run = await findRun('&event=push', run =>
              run.event === 'push' && run.head_sha === commit.sha, signal);
          }
        }
      } catch (_) {
        // 没有运行证据时保留等待状态，不把无关运行的失败当成规则失败。
      }
    })();
    await Promise.all([publicTask, actionTask, rulesTask]);
    if (epoch !== version) return state();
    if (nextRules) {
      if (!latestRules || nextRules.sha !== latestRules.sha) ruleSince = 0;
      latestRules = nextRules;
      const current = el('snapshotData') ? pageSnapshot() : (snapshot || verifiedSnapshot);
      if (current && current.config_files['settings.json'] !== nextRules.sha) {
        if (!ruleSince) ruleSince = Date.now();
      } else ruleSince = 0;
    }
    rulesError = nextRulesError;
    actionsError = actionError;
    // 网络失败时保留上次成功读到的状态：状态灯不该因为一次断网就从绿灯跳黄。
    if (schema(status)) latestStatus = status;
    if (snapshot) verifiedSnapshot = snapshot;
    configRun = nextConfigRun;
    publication = snapshot
      ? {phase: 'published', message: '已发布完成：看板与状态文件一致（run ' + snapshot.run_id + '）'}
      : {phase: 'waiting', message: publicError || '尚未验证发布'};
    if (snapshot) { saveLast(snapshot); publicRetry = PUBLIC_RETRY_MAX; }
    else if (publicError && publicRetry < PUBLIC_RETRY_MAX) publicRetry++;
    retryPending = !snapshot && (!!publicError || !!actionError) && retryPending;
    if (Object.keys(expected).length) {
      if (snapshot && configMatches(status, expected) && configMatches(snapshot, expected)) {
        configState = {phase: 'published', message: '本页已提交的全部配置已发布生效（blob SHA 与看板快照双验证）'};
      } else {
        configState = phaseForRun(nextConfigRun);
        if (configState.phase !== 'failed' && status && configMatches(status, expected)) {
          configState = {phase: 'generated', message: '配置生成结果已出现，尚未验证看板发布一致'};
        }
        if (Date.now() - configSince >= MAX_WAIT && configState.phase !== 'failed') {
          configState = {phase: 'expired', message: '配置跟踪已达 10 分钟，停止自动轮询；提交目标已保留，请稍后查运行状态。这不代表运行失败'};
        } else if (publicError) configState.message += '\n' + publicError;
      }
    }
    if (manualTarget && !manualTarget.rejected) {
      if (nextManualRun) manual.run_id = String(nextManualRun.id);
      else if (!manual.run_id && manualMatches(status, manual)) manual.run_id = status.run_id;
      const runPhase = phaseForRun(nextManualRun);
      if (runPhase.phase === 'failed') manualState = runPhase;
      else if (snapshot && manualMatches(status, manual) && manualMatches(snapshot, manual)) {
        manualState = {phase: 'published', message: '指定 run ' + manual.run_id + ' 已发布完成（request_id、run_id、配置 blob 与看板快照均匹配）'};
      } else {
        manualState = runPhase;
        if (manualMatches(status, manual)) manualState = {phase: 'generated', message: '指定 run 已生成，尚未验证看板发布一致'};
        if (Date.now() - manual.since >= MAX_WAIT) {
          manualState = {phase: 'expired', message: '指定运行跟踪已达 10 分钟，停止自动轮询；request_id/run_id 已保留，请稍后查运行状态。这不代表运行失败'};
        } else if (publicError) manualState.message += '\n' + publicError;
      }
    }
    if (Date.now() - configSince >= MAX_WAIT && activeConfig()) {
      configState = {phase: 'expired', message: '配置自动跟踪窗口已结束，目标仍保留；请稍后查运行状态，不代表运行失败'};
    }
    if (manual && Date.now() - manual.since >= MAX_WAIT && activeManual()) {
      manualState = {phase: 'expired', message: '指定运行自动跟踪窗口已结束，request_id/run_id 仍保留；请稍后查运行状态，不代表运行失败'};
    }
    // 已验证目标也保留；之后保存另一文件时仍校验旧文件，防止被旧运行回滚。
    render();
    schedule();
    return state();
  }
  function refresh() {
    if (!initialized) init();
    publicRetry = 0;
    if (configState.phase === 'expired') { configSince = Date.now(); configState.phase = 'waiting'; }
    if (manual && manualState.phase === 'expired') { manual.since = Date.now(); manualState.phase = 'waiting'; }
    return tick();
  }
  function watchConfig({file, blobSHA, commitSHA}) {
    if (!initialized) init();
    if (!FILES.includes(file) || !SHA.test(blobSHA || '') || !SHA.test(commitSHA || '')) {
      throw new Error('watchConfig 需要 holdings.json/settings.json 及提交回执中的有效 blobSHA、commitSHA');
    }
    cancelPoll();
    targets[file] = {blobSHA, commitSHA, savedAt: Date.now(), revision: ++receiptVersion};
    configSince = Date.now();
    retrySince = Date.now();
    configRun = null;
    configState = {phase: 'queued', message: file + ' 已提交，正在跟踪全部待生效配置'};
    verifiedSnapshot = null;
    render();
    return tick();
  }
  function uuid() {
    if (window.crypto.randomUUID) return window.crypto.randomUUID();
    const bytes = window.crypto.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 15) | 64;
    bytes[8] = (bytes[8] & 63) | 128;
    const hex = Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('');
    return hex.slice(0, 8) + '-' + hex.slice(8, 12) + '-' + hex.slice(12, 16) + '-' + hex.slice(16, 20) + '-' + hex.slice(20);
  }
  async function startManual() {
    if (!initialized) init();
    if (activeManual()) return state();
    if (!getToken()) {
      // 首页没有令牌输入框：先问一次，贴了就当场存下并继续运行，不用跳去编辑页
      const asked = window.prompt(
        '立即运行需要 GitHub 令牌（权限：Actions Read and write）。\n\n' +
        '粘贴令牌后点「确定」立即开始运行；点「取消」则只查看，不运行。\n' +
        '令牌只保存在这台设备的浏览器里，不会写进公开仓库。');
      if (asked && asked.trim()) {
        if (!looksLikeToken(asked)) {
          authNotice = '这不像 GitHub 令牌（应以 ghp_ / github_pat_ 开头），已取消运行';
          render();
          return {...state(), phase: 'auth', message: authNotice};
        }
        if (!saveToken(asked)) {
          authNotice = '这个浏览器不让存本地数据，无法保存令牌；请到编辑页贴令牌后再回来点运行';
          render();
          return {...state(), phase: 'auth', message: authNotice};
        }
      } else {
        authNotice = '没有令牌，无法立即运行：点「立即运行」贴一次令牌，或打开编辑页贴一次（同一浏览器通用）';
        render();
        return {...state(), phase: 'auth', message: authNotice};
      }
    }
    authNotice = '';
    checkFeedback = '';
    cancelPoll();
    retrySince = Date.now();
    manual = {request_id: uuid(), run_id: null, since: Date.now()};
    manualState = {phase: 'dispatching', message: '正在提交立即运行请求（' + manual.request_id + '）'};
    render();
    try {
      await request(apiURL('/actions/workflows/daily.yml/dispatches'), {
        method: 'POST', github: true, body: {ref: options.branch, inputs: {request_id: manual.request_id}}
      });
      manualState = {phase: 'queued', message: '运行请求已接受；正在按 request_id 定位指定运行，尚未完成'};
    } catch (error) {
      if (!error.status) {
        // POST 超时可能已被接受：继续查同一 UUID，不自动重复派发。
        manualState = {phase: 'queued', message: '派发响应未确认：' + error.message + '；仅跟踪此 request_id，不重复发送'};
      } else {
        manual.rejected = true;
        manualState = {phase: 'failed', message: '未启动：运行请求被拒绝。' + error.message +
          (error.status === 422 ? '；daily.yml 必须声明 workflow_dispatch.inputs.request_id' : '')};
        render();
        schedule();
        return state();
      }
    }
    render();
    return tick();
  }
  // 「查运行状态」点了要有反馈：否则状态没变化时页面文字不动，看着像按钮坏了。
  async function manualCheck() {
    const sequence = ++checkRequest;
    checkDismissed = false;
    const b = el('btnCheckStatus');
    if (b) { b.disabled = true; b.textContent = '核对中…'; }
    checkFeedback = '正在核对最新任务和已发布看板…';
    checkPhase = 'busy';
    render();
    let recentRun = null, recentError = '';
    try {
      await refresh();
      if ((!manual || manual.rejected) && !Object.keys(targets).length) {
        try {
          const data = await request(bust(apiURL('/actions/workflows/daily.yml/runs?branch=' +
            encodeURIComponent(options.branch) + '&per_page=1')), {github: true});
          recentRun = data && data.workflow_runs && data.workflow_runs[0] || null;
        } catch (error) { recentError = networkMessage(error); }
      }
      const published = publication.phase === 'published' && verifiedSnapshot;
      checkPhase = published ? 'ok' : 'busy';
      const lines = [];
      if (manual && manual.rejected) {
        lines.push('上次立即运行未启动：' + manualState.message.replace(/^未启动：运行请求被拒绝。/, ''));
        lines.push('查状态不会重新派发，请处理拒绝原因后再点立即运行。');
        checkPhase = 'bad';
      } else if (manual) {
        lines.push('你的运行：' + manualState.message);
        if (manualState.phase === 'failed') checkPhase = 'bad';
        else if (manualState.phase !== 'published') checkPhase = 'busy';
      } else if (Object.keys(targets).length) {
        lines.push('保存后的运行：' + configState.message);
        if (configState.phase === 'failed') checkPhase = 'bad';
        else if (configState.phase !== 'published') checkPhase = 'busy';
      } else if (recentRun) {
        const phase = phaseForRun(recentRun);
        const label = phase.phase === 'generated'
          ? (published && String(recentRun.id) === verifiedSnapshot.run_id ? '已完成并发布' : '生成成功，等待看板发布')
          : phase.phase === 'running' ? '运行中' : phase.phase === 'failed' ? '失败（' + (recentRun.conclusion || '未知') + '）' : '排队中';
        lines.push('最新任务：' + label + ' · run ' + recentRun.id);
        if (phase.phase === 'failed') checkPhase = 'bad';
        else if (!published || String(recentRun.id) !== verifiedSnapshot.run_id) checkPhase = 'busy';
      } else lines.push(recentError ? '最新任务状态未能核对：' + recentError : '未找到最新任务记录。');
      if (published) {
        lines.push('已核对线上 run ' + verifiedSnapshot.run_id + '，看板与状态文件一致。');
        lines.push('数据更新时间：' + (humanTime(verifiedSnapshot) || '未知') + '（北京时间）');
        const current = pageSnapshot();
        lines.push(current && !samePublication(current, verifiedSnapshot)
          ? '当前页面仍是旧数据，可刷新当前看板。' : '当前页面已是这版发布结果，暂无页面更新。');
      } else lines.push('核对尚未完成：' + publication.message);
      if (actionsError) lines.push('对应运行记录无法核对：' + actionsError);
      if (recentError || actionsError) checkPhase = 'bad';
      if (sequence === checkRequest && !checkDismissed) {
        checkFeedback = lines.join('\n');
        render();
      }
    } finally {
      if (b) { b.disabled = false; b.textContent = '查运行状态'; }
    }
  }

  function init(opts = {}) {
    if (initialized && !Object.keys(opts).length) return window.MMRunStatus;
    cancelPoll();
    options = {owner: 'nixhuang', repo: 'market-monitor', branch: 'main', timeoutMs: 15000, ...options, ...opts};
    initialized = true;
    if (el('btnRunNow')) el('btnRunNow').onclick = () => { void startManual(); };
    if (el('btnCheckStatus')) el('btnCheckStatus').onclick = () => manualCheck();
    render(); // 不等网络请求，先保留本页生成数据并折叠旧版摘要。
    void refresh();
    return window.MMRunStatus;
  }
  window.MMRunStatus = {refresh, watchConfig, startManual, init};
  // 首页这轮改版后已经没有 runState / runMsg / runSummary 了，只看这三个会导致
  // 首页根本不 init —— 按钮没绑 onclick，点了就是「没反应」。改成任一控件存在即初始化。
  function autoInit() {
    if (initialized) return;
    const ids = ['runMsg', 'runSummary', 'runLight', 'runLightTxt',
                 'ruleLight', 'ruleLightTxt', 'btnRunNow', 'btnCheckStatus', 'appliedState'];
    if (ids.some(id => el(id))) init();
  }
  if (window.document.readyState === 'loading') window.document.addEventListener('DOMContentLoaded', autoInit);
  else autoInit();
})(window);
