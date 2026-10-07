(function (window) {
  'use strict';

  const MAX_WAIT = 10 * 60 * 1000;
  const SHA = /^[a-f0-9]{40,64}$/i;
  const FILES = ['holdings.json', 'settings.json'];
  const terminal = new Set(['published', 'failed', 'expired']);
  let options = {}, initialized = false, timer = null, controller = null, version = 0;
  let targets = {}, receiptVersion = 0, configSince = 0, configRun = null, manual = null;
  let latestStatus = null, verifiedSnapshot = null, latestRun = null, actionsError = '';
  let configState = {phase: 'idle', message: '尚无本页提交待验证'};
  let manualState = {phase: 'idle', message: ''};
  let publication = {phase: 'idle', message: '正在读取公开发布状态…'};

  function getToken() {
    if (options.getToken) {
      try { return options.getToken() || ''; } catch (_) { return ''; }
    }
    try { return window.localStorage.getItem('mm_gh_token_v1') || ''; } catch (_) { return ''; }
  }
  function apiURL(path) {
    return 'https://api.github.com/repos/' + encodeURIComponent(options.owner) + '/' +
      encodeURIComponent(options.repo) + path;
  }
  function bust(path) {
    return path + (path.includes('?') ? '&' : '?') + '_mm=' + Date.now() + '-' + version;
  }
  function authMessage(status) {
    if (status === 401) return '401：令牌无效或过期，请重新填写';
    if (status === 403) return '403：权限不足或 GitHub 限流；读取运行需 Actions Read，手动重跑需 Actions Read and write，编辑保存需 Contents Read and write';
    if (status === 404) return '404：找不到 workflow/run，或令牌无权访问；请检查仓库及 daily.yml';
    return 'HTTP ' + status;
  }
  // 令牌只发送给 GitHub API；超时覆盖请求和响应体读取。
  async function request(url, {method = 'GET', body, github = false, signal, text = false} = {}) {
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
        reject(new Error('网络请求超时；可稍后刷新状态，不代表运行失败'));
      }, options.timeoutMs);
    });
    try {
      return await Promise.race([expired, (async () => {
        const response = await window.fetch(url, {
          method, headers, cache: 'no-store', signal: local.signal,
          ...(body !== undefined ? {body: JSON.stringify(body)} : {})
        });
        if (!response.ok) {
          const error = new Error(github ? authMessage(response.status) :
            (response.status === 404 ? 'status.json 尚未发布或页面不存在（404）' : '公开页面 HTTP ' + response.status));
          error.status = response.status;
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
    if (!s) return '暂无可验证的发布摘要';
    const summary = s.summary || {}, counts = s.list_counts || {}, dates = s.actual_dates || {};
    const list = value => Array.isArray(value) ? value.join(', ') || '无' : value == null ? '未知' : String(value);
    return [
      '已发布 run ' + s.run_id + ' · 事件 ' + (s.event || '未知') + ' · 源提交 ' + (s.source_sha || '未知'),
      '开始 ' + (s.started_at_bj || s.started_at || '未知') + ' · 完成 ' + (s.finished_at_bj || s.finished_at || '未知') + ' · 目标交易日 ' + (s.target_trade_date || '未知'),
      '运行计划 ' + (s.schedule || '未记录') + ' · 数据模式 ' + (s.mode || '未知'),
      '红 ' + (summary.red ?? '未知') + ' / 黄 ' + (summary.yellow ?? '未知') + ' / 绿 ' + (summary.green ?? '未知') +
        ' / 灰 ' + (summary.gray ?? '未知') + ' / 总数 ' + (summary.total ?? '未知') + ' · 宏观有效 ' + (summary.macro_ok ?? '未知'),
      '持仓 ' + (counts.positions ?? '未知') + ' · 关注 ' + (counts.watch ?? '未知') +
        ' · trigger ' + (counts.triggers ?? '未知') + ' · 定投 ' + (counts.dca ?? '未知'),
      '行情实际日期 ' + (dates.min || '未知') + ' ～ ' + (dates.max || '未知') +
        ' · 过期 ' + list(summary.stale_symbols) + ' · 缺失 ' + list(summary.missing_symbols),
      '覆盖情况 ' + JSON.stringify(s.coverage ?? null),
      '生效规则 ' + JSON.stringify(s.effective_settings || {}),
      '配置 blob ' + JSON.stringify(s.config_files)
    ].join('\n');
  }
  function el(id) { return window.document.getElementById(id); }
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
          message += '；当前页面仍是旧快照，点击「刷新看板」查看新版本';
        }
      } catch (_) { message += '；当前页快照无效，请点击「刷新看板」'; }
    }
    if (actionsError) message += '\nActions：' + actionsError + '；公开发布状态仍可验证';
    put('runState', message, s.phase);
    put('runMsg', message, s.phase);
    put('runSummary', summaryText(verifiedSnapshot) + '\n' + publication.message, publication.phase);
    if (el('appliedState')) {
      const receipts = Object.entries(targets).map(([file, r]) =>
        file + ' · 提交 ' + r.commitSHA + ' · blob ' + r.blobSHA).join('\n');
      put('appliedState', configState.message + (receipts ? '\n' + receipts : '') +
        '\n\n' + summaryText(verifiedSnapshot), configState.phase);
    }
    ['btnRun', 'btnRunNow'].forEach(id => { if (el(id)) el(id).disabled = !!activeManual(); });
    const box = el('appliedState') || el('runMsg') || el('runState');
    if (box && verifiedSnapshot && (!Object.keys(targets).length || configState.phase === 'published') &&
        (!manual || manualState.phase === 'published')) {
      const link = window.document.createElement('a');
      link.textContent = '刷新看板';
      link.href = bust('./index.html') + '&mm_run=' + encodeURIComponent(verifiedSnapshot.run_id);
      link.style.cssText = 'display:inline-block;margin:8px;color:#6ba3f0';
      if (el('appliedState')) { link.target = '_blank'; link.rel = 'noopener noreferrer'; }
      box.appendChild(link);
    }
    if (typeof options.onChange === 'function') options.onChange(s);
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
    if (!activeConfig() && !activeManual()) return;
    const since = Math.min(activeConfig() ? configSince : Infinity, activeManual() ? manual.since : Infinity);
    timer = window.setTimeout(() => { void tick(); }, Date.now() - since < 120000 ? 8000 : 20000);
  }
  async function tick() {
    cancelPoll();
    const epoch = version;
    controller = new AbortController();
    const signal = controller.signal;
    const expected = JSON.parse(JSON.stringify(targets));
    const manualTarget = manual ? {...manual} : null;
    let status = null, snapshot = null, publicError = '', actionError = '';
    let nextConfigRun = configRun, nextManualRun = null, nextLatestRun = latestRun;
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
      } catch (error) { publicError = error.message; }
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
        } else if (!manualTarget) {
          const data = await request(apiURL('/actions/workflows/daily.yml/runs?branch=' +
            encodeURIComponent(options.branch) + '&per_page=1'), {github: true, signal});
          nextLatestRun = (data.workflow_runs || [])[0] || null;
        }
      } catch (error) { actionError = error.message; }
    })();
    await Promise.all([publicTask, actionTask]);
    if (epoch !== version) return state();
    actionsError = actionError;
    latestStatus = schema(status) ? status : null;
    verifiedSnapshot = snapshot;
    latestRun = nextLatestRun;
    configRun = nextConfigRun;
    publication = snapshot
      ? {phase: 'published', message: '已发布完成：status.json 与 index.html 的 run_id、配置 blob 均一致'}
      : {phase: 'waiting', message: publicError || '尚未验证发布'};
    if (Object.keys(expected).length) {
      if (snapshot && configMatches(status, expected) && configMatches(snapshot, expected)) {
        configState = {phase: 'published', message: '本页已提交的全部配置已发布生效（blob SHA 与看板快照双验证）'};
      } else {
        configState = phaseForRun(nextConfigRun);
        if (configState.phase !== 'failed' && status && configMatches(status, expected)) {
          configState = {phase: 'generated', message: '配置生成结果已出现，尚未验证看板发布一致'};
        }
        if (Date.now() - configSince >= MAX_WAIT && configState.phase !== 'failed') {
          configState = {phase: 'expired', message: '配置跟踪已达 10 分钟，停止自动轮询；提交目标已保留，请稍后刷新状态。这不代表运行失败'};
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
          manualState = {phase: 'expired', message: '指定运行跟踪已达 10 分钟，停止自动轮询；request_id/run_id 已保留，请稍后刷新状态。这不代表运行失败'};
        } else if (publicError) manualState.message += '\n' + publicError;
      }
    } else if (!manualTarget && !Object.keys(expected).length && latestRun) {
      if (!snapshot || snapshot.run_id !== String(latestRun.id) || latestRun.conclusion !== 'success') {
        const recent = phaseForRun(latestRun);
        publication = {...recent, message: recent.message + '\n' + publication.message};
      }
    }
    if (Date.now() - configSince >= MAX_WAIT && activeConfig()) {
      configState = {phase: 'expired', message: '配置自动跟踪窗口已结束，目标仍保留；请稍后刷新状态，不代表运行失败'};
    }
    if (manual && Date.now() - manual.since >= MAX_WAIT && activeManual()) {
      manualState = {phase: 'expired', message: '指定运行自动跟踪窗口已结束，request_id/run_id 仍保留；请稍后刷新状态，不代表运行失败'};
    }
    // 已验证目标也保留；之后保存另一文件时仍校验旧文件，防止被旧运行回滚。
    render();
    schedule();
    return state();
  }
  function refresh() {
    if (!initialized) init();
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
      manualState = {phase: 'auth', message: '立即重跑需要令牌及 Actions Read and write；无令牌仍可查看公开清单和发布状态'};
      put('runState', manualState.message, 'auth');
      put('runMsg', manualState.message, 'auth');
      return {...state(), phase: 'auth', message: manualState.message};
    }
    cancelPoll();
    manual = {request_id: uuid(), run_id: null, since: Date.now()};
    manualState = {phase: 'dispatching', message: '正在提交立即重跑请求（' + manual.request_id + '）'};
    render();
    try {
      await request(apiURL('/actions/workflows/daily.yml/dispatches'), {
        method: 'POST', github: true, body: {ref: options.branch, inputs: {request_id: manual.request_id}}
      });
      manualState = {phase: 'queued', message: '重跑请求已接受；正在按 request_id 定位指定运行，尚未完成'};
    } catch (error) {
      if (!error.status) {
        // POST 超时可能已被接受：继续查同一 UUID，不自动重复派发。
        manualState = {phase: 'queued', message: '派发响应未确认：' + error.message + '；仅跟踪此 request_id，不重复发送'};
      } else {
        manual.rejected = true;
        manualState = {phase: 'failed', message: '派发失败：' + error.message +
          (error.status === 422 ? '；daily.yml 必须声明 workflow_dispatch.inputs.request_id' : '')};
        render();
        schedule();
        return state();
      }
    }
    render();
    return tick();
  }
  function init(opts = {}) {
    if (initialized && !Object.keys(opts).length) return window.MMRunStatus;
    cancelPoll();
    options = {owner: 'nixhuang', repo: 'market-monitor', branch: 'main', timeoutMs: 15000, ...options, ...opts};
    initialized = true;
    ['btnRun', 'btnRunNow'].forEach(id => {
      if (el(id)) el(id).onclick = () => { void startManual(); };
    });
    if (el('btnCheckStatus')) el('btnCheckStatus').onclick = () => { void refresh(); };
    void refresh();
    return window.MMRunStatus;
  }
  window.MMRunStatus = {refresh, watchConfig, startManual, init};
  function autoInit() {
    if (!initialized && (el('runState') || el('runMsg') || el('runSummary'))) init();
  }
  if (window.document.readyState === 'loading') window.document.addEventListener('DOMContentLoaded', autoInit);
  else autoInit();
})(window);
