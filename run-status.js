(function (window) {
  'use strict';

  function bindBoardReturn(snapshot = null, revision = 0) {
    const link = window.document.getElementById('btnReturnBoard');
    if (!link) return false;
    // 导航不依赖发布核验；正式页和既有只读预览共用当前路径重载。
    const updateHref = () => {
      const url = new window.URL(window.location.href);
      url.hash = '';
      url.searchParams.set('_mm', Date.now() + '-' + revision);
      url.searchParams.delete('mm_run');
      if (snapshot) url.searchParams.set('mm_run', snapshot.run_id);
      link.href = url.href;
    };
    updateHref();
    link.onclick = updateHref;
    return true;
  }

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
      bindBoardReturn();
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
  let options = {}, initialized = false, destroyed = false, timer = null, controller = null, version = 0;
  let checkController = null, dispatchController = null;
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
  // 「核对」按钮的原始文案由页面决定（首页就是「状态」），点完必须还原成它，不能写死成别的。
  let checkLabel = null;
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
      // 2026-11-01：这里原来不带 timeZone，会按**用户本地时区**显示，跟全页「只认北京时间」不一致。
      const wait = retryAt ? '；请在 ' + new Date(retryAt).toLocaleTimeString('zh-CN',
        {timeZone: 'Asia/Shanghai', hour: '2-digit', minute: '2-digit', hourCycle: 'h23'}) + ' 后再试' : '；请稍后再试';
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
  // revalidate：静态文件（看板 index.html、raw 配置原文）用「必须向服务器校验但允许 304」，
  // 而不是 no-store 的每次全量重传。语义不变——拿到的仍是线上当前内容，只是未变更时不传 body。
  async function request(url, opts = {}) {
    const {method = 'GET', body, github = false, signal, text = false, revalidate = false} = opts;
    const cancelled = () => Object.assign(new Error('请求已取消'), {name: 'AbortError'});
    if (destroyed || signal && signal.aborted) throw cancelled();
    if (github && Date.now() < apiBlockedUntil) {
      const error = new Error(authMessage(429, '', '0', '', apiBlockedUntil, !!getToken()).text);
      error.status = 429; error.kind = 'rate_limit'; error.retryAt = apiBlockedUntil;
      throw error;
    }
    const local = new AbortController();
    let rejectAbort;
    const aborted = new Promise((_, reject) => { rejectAbort = reject; });
    const abort = () => { local.abort(); rejectAbort(cancelled()); };
    if (signal) signal.addEventListener('abort', abort, {once: true});
    const ensureActive = () => {
      if (destroyed || local.signal.aborted) throw cancelled();
    };
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
      return await Promise.race([expired, aborted, (async () => {
        const response = await window.fetch(url, {
          method, headers, cache: revalidate ? 'no-cache' : 'no-store', signal: local.signal,
          ...(body !== undefined ? {body: JSON.stringify(body)} : {})
        });
        ensureActive();
        // 304 只在浏览器缓存里已有该 URL 副本时才会被 fetch 折成可用响应；冷缓存（例如首次进入
        // 且页面未入缓存）会原样抛回 304，这时退回一次全量读取，保证拿到的仍是线上真实内容。
        if (revalidate && response.status === 304) {
          return await request(url, {...opts, signal: local.signal, revalidate: false});
        }
        if (!response.ok) {
          let diagnostic = {kind: 'http', text: response.status === 404
            ? 'status.json 尚未发布或页面不存在（404）' : '公开页面 HTTP ' + response.status};
          let retryAt = 0;
          if (github) {
            let payload = {};
            try { payload = await response.json(); } catch (_) {}
            ensureActive();
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
        const value = text ? await response.text() : await response.json();
        ensureActive();
        return value;
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
  function cancelCheck() {
    if (checkController) checkController.abort();
    checkController = null;
  }
  function destroy() {
    destroyed = true;
    cancelCheck();
    cancelPoll();
    checkRequest++;
    if (dispatchController) dispatchController.abort();
    dispatchController = null;
    ['btnRunNow', 'btnCheckStatus'].forEach(id => {
      const button = el(id);
      if (button) {
        button.onclick = null;
        button.disabled = false;
        if (id === 'btnCheckStatus' && checkLabel !== null) button.textContent = checkLabel;
      }
    });
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
    const summary = s.summary || {}, dates = s.actual_dates || {};
    const list = value => Array.isArray(value) ? value.join(', ') || '无' : value == null ? '未知' : String(value);
    return [
      '本页生成数据 run ' + s.run_id + ' · 事件 ' + (s.event || '未知') + ' · 源提交 ' + (s.source_sha || '未知'),
      '开始 ' + (s.started_at_bj || s.started_at || '未知') + ' · 完成 ' + (s.finished_at_bj || s.finished_at || '未知') + ' · 目标交易日 ' + (s.target_trade_date || '未知'),
      '运行计划 ' + (s.schedule || '未记录') + ' · 数据模式 ' + (s.mode || '未知'),
      '红 ' + (summary.red ?? '未知') + ' / 黄 ' + (summary.yellow ?? '未知') + ' / 绿 ' + (summary.green ?? '未知') +
        ' / 灰 ' + (summary.gray ?? '未知') + ' / 总数 ' + (summary.total ?? '未知') + ' · 宏观有效 ' + (summary.macro_ok ?? '未知'),
      '行情实际日期 ' + (dates.min || '未知') + ' ～ ' + (dates.max || '未知') +
        ' · 过期 ' + list(summary.stale_symbols) + ' · 缺失 ' + list(summary.missing_symbols),
      '覆盖情况 ' + JSON.stringify(s.coverage ?? null),
      '生效规则 ' + JSON.stringify(s.effective_settings || {}),
      '配置 blob ' + JSON.stringify(s.config_files)
    ].join('\n');
  }
  function el(id) { return window.document.getElementById(id); }
  /* 2026-11-01：一次 render() 里 pageSnapshot() 会被调 4~6 次，每次都把 141KB 的
     <script id="snapshotData"> 整段重新 JSON.parse 一遍（约 560KB 解析 / render）。
     缓存「textContent 原文 + 解析结果」：原文没变就直接复用，只有内容真换了才重解析。 */
  let snapRaw = null, snapCache = null;
  function pageSnapshot() {
    const embedded = el('snapshotData');
    if (!embedded) return null;
    const raw = embedded.textContent;
    if (raw === snapRaw) return snapCache;
    snapRaw = raw; snapCache = null;
    try {
      const value = JSON.parse(raw);
      snapCache = schema(value) ? value : null;
    } catch (_) { snapCache = null; }
    return snapCache;
  }
  /* 每轮 tick 都要从 index.html 里取出内联快照。整篇 617KB 交给 DOMParser 会真的建出一整棵
     DOM（含 141KB 的 JSON 文本节点与 124KB 的脚本文本节点），主线程 60~200ms。
     先用字符串索引把 <script id="snapshotData"> 的内容切出来再 JSON.parse；切不到
     （页面被改写、结构变了）才回退 DOMParser，保证取不到就报错的语义不丢。 */
  function embeddedSnapshot(html) {
    const text = String(html || '');
    const open = text.indexOf('<script id="snapshotData"');
    if (open >= 0) {
      const start = text.indexOf('>', open) + 1;
      // 写成 <\/script>：斜杠转义对 JS 等价，但这段被内联进 HTML 的 <script> 时
      // 不会提前终止标签（内联版 run-status.js 就是这样挂进页面的）。
      const end = text.indexOf('<\/script>', start);
      if (start > 0 && end > start) return text.slice(start, end);
    }
    try {
      const node = new window.DOMParser().parseFromString(text, 'text/html').querySelector('script#snapshotData');
      return node ? node.textContent : '';
    } catch (_) { return ''; }
  }
  function shortSummary(s) {
    const info = s.summary || {}, dates = s.actual_dates || {};
    const bad = [...(info.stale_symbols || []), ...(info.missing_symbols || [])];
    // 红/黄/绿计数统计卡里已经有一份，这里不再重复念（2026-10-20）；只报数据日期 + 有没有没更新到的
    return '数据日期 ' + (dates.max || '未知') + ' · ' +
      (bad.length ? bad.length + ' 只未更新' : (info.total ?? 0) + ' 只全部更新') +
      (info.gray ? ' · 缺失 ' + info.gray : '') +
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
    return fmtBJ(date);
  }
  /* 北京时间格式化：两个函数原本各抄一份 12 个 option 的 toLocaleString，改一处漏一处。
     统一走这一个（sv-SE 本身就是 24 小时制，再用 hourCycle:'h23' 显式钉死，
     避免 hour12:false 在部分 ICU 下把零点渲染成「24」）。 */
  const BJ_FMT = {timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit',
    day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23'};
  function fmtBJ(date) { return date.toLocaleString('sv-SE', BJ_FMT); }
  function humanTime(s) {
    const raw = s && (s.finished_at_bj || s.finished_at || s.started_at_bj || s.started_at);
    if (!raw) return '';
    const value = String(raw), date = new Date(value);
    if (/(?:Z|[+-]\d{2}:\d{2})$/i.test(value) && Number.isFinite(date.getTime())) {
      return fmtBJ(date);
    }
    const match = value.match(/(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})/);
    return match ? match[1] + ' ' + match[2] : value;
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
  // 新任务失败/取消时，看板仍显示上一次成功发布的数据——要明确告诉用户这一点和数据时间
  function keptData() {
    const when = humanTime(verifiedSnapshot || latestStatus);
    return '；看板仍显示上次成功发布的数据' + (when ? '（' + when + '）' : '');
  }
  function failureText(state) {
    if (state.cancelled) return state.message + '，不是抓取失败' + keptData();
    return '运行失败：' + state.message.replace(/^派发失败：/, '') + keptData();
  }
  function plainApplied() {
    const when = humanTime(verifiedSnapshot || latestStatus);
    if (activeManual()) {
      const phase = manualState.phase;
      if (phase === 'dispatching' || phase === 'queued') return '运行已提交，正在更新看板，通常约 5–9 分钟…';
      if (phase === 'running') return '正在运行，正在抓最新行情…';
      if (phase === 'generated') return '运行已生成，正在发布到看板…';
      if (phase === 'failed') return failureText(manualState);
      if (phase === 'expired') return '运行跟踪超时，请点查运行状态确认，不代表失败';
    }
    if (Object.keys(targets).length) {
      const phase = configState.phase;
      if (phase === 'published') return '已生效：看板已按你保存的设置更新' + (when ? '（' + when + '）' : '');
      if (phase === 'failed') return configState.cancelled
        ? '保存已提交，但对应运行被取消（通常被更新的等待任务替换），请稍后查运行状态' + keptData()
        : '保存未生效：本次运行失败，请查看运行记录' + keptData();
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
  let reloadStarted = false;
  function expectedConfig() {
    if (!latestRules) return null;
    return Object.fromEntries(FILES.map(file => {
      const receipt = targets[file];
      const remote = latestRules.files[file];
      const awaitingReceipt = receipt && configState.phase !== 'published' &&
        remote !== receipt.blobSHA && remote === (pageSnapshot() || verifiedSnapshot)?.config_files[file];
      return [file, receipt && configState.phase !== 'published' &&
        (receipt.savedAt >= latestRules.checkedAt || awaitingReceipt) ? receipt.blobSHA : remote];
    }));
  }
  function sameConfig(value, expected) {
    return schema(value) && expected && FILES.every(file => value.config_files[file] === expected[file]);
  }
  function reloadBlockedReason() {
    if (destroyed) return '状态组件已关闭，请重新打开页面';
    if (reloadStarted) return '正在刷新，请稍候';
    if (!verifiedSnapshot || publication.phase !== 'published') return '本次发布核验未完成，请稍后查运行状态';
    if (rulesError || !expectedConfig()) return '最新规则尚未核验，请稍后查运行状态';
    if (!sameConfig(verifiedSnapshot, expectedConfig())) return '最新规则已变化，正在等待对应版本发布';
    if (Object.keys(targets).length && configState.phase !== 'published') return '本次保存尚未验证发布';
    if (!window.location || typeof window.location.replace !== 'function') return '当前环境不支持刷新';
    try {
      if (options.canReload && !options.canReload()) return '当前编辑状态不允许刷新，请先保存或退出编辑';
    } catch (_) { return '当前编辑状态无法核对，请先保存或退出编辑'; }
    return '';
  }
  function reloadCurrent(force = false) {
    if (!force || reloadBlockedReason()) return false; // 配置发布后仅提示手动刷新，不自动替换当前页。
    const url = new URL(window.location.href);
    reloadStarted = true;
    url.hash = '';
    url.searchParams.delete('focus');
    url.searchParams.delete('edit');
    url.searchParams.set('_mm_reset_view', '1');
    url.searchParams.set('_mm', String(Date.now()));
    url.searchParams.set('mm_refreshed_run', verifiedSnapshot.run_id);
    window.location.replace(url.href);
    return true;
  }
  function refreshLink(box, style) {
    const link = window.document.createElement('a');
    link.textContent = '刷新当前看板';
    link.href = '#';
    link.onclick = event => {
      event.preventDefault();
      const reason = reloadBlockedReason();
      if (!reason && reloadCurrent(true)) return;
      link.textContent = '暂不能刷新：' + (reason || '请稍后重试');
      link.setAttribute('aria-live', 'polite');
    };
    link.style.cssText = style;
    box.appendChild(link);
  }
  function ruleState() {
    const snap = el('snapshotData') ? pageSnapshot() : verifiedSnapshot;
    const expected = expectedConfig();
    const tracked = Object.keys(targets).length > 0 && expected &&
      Object.entries(targets).every(([file, receipt]) => expected[file] === receipt.blobSHA);
    const receiptPending = tracked && (!snap || !configMatches(snap, targets));
    if (rulesError) return {phase: 'idle', text: '暂无法核验'};
    if (!expected) return {phase: 'busy', text: '正在核验'};
    // 清单、分组和规则均一致，且保存回执的本次生成证据已确认，才可显示绿灯。
    if (sameConfig(snap, expected) && !receiptPending) return {phase: 'ok', text: '已生效'};
    if (sameConfig(verifiedSnapshot, expected) && (!tracked || configState.phase === 'published')) {
      return el('snapshotData') ? {phase: 'busy', text: '需要刷新'} : {phase: 'ok', text: '已生效'};
    }
    const associated = tracked && (configRun && configRun.head_sha === latestRules.commitSHA ||
      Object.values(targets).some(receipt => receipt.savedAt >= latestRules.checkedAt));
    const runState = associated ? configState : latestRules.run ? phaseForRun(latestRules.run) : null;
    if (runState && runState.phase === 'failed') return runState.cancelled
      ? {phase: 'busy', text: '等待更新'} : {phase: 'bad', text: '更新失败'};
    if (sameConfig(latestStatus, expected) && (!tracked || configMatches(latestStatus, targets))) {
      return {phase: 'busy', text: '等待发布'};
    }
    if (runState && runState.phase === 'generated') return {phase: 'busy', text: '等待发布'};
    if (latestRules.runError || (actionsError && tracked) || (!schema(snap) && el('snapshotData')) ||
        (ruleSince && Date.now() - ruleSince >= MAX_WAIT)) return {phase: 'idle', text: '暂无法核验'};
    if (runState && runState.phase === 'running') return {phase: 'busy', text: '正在更新'};
    return {phase: 'busy', text: '等待更新'};
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
    // 一切正常只报「成功 ·YYYY-MM-DD HH:MM（北京时间）」：红/黄/绿计数统计卡里已有一份，
    // 「N 项数据已更新」是废话（2026-10-20），「保存后抓取成功 · 完成于 …」在手机上会撑成两行（2026-10-27）。
    // 只有真有缺失（灰）或不支持报价的特殊代码时才补一句，跟 monitor.py 的静态文案同一口径。
    const success = '成功 ·' + (finished || '未记录') + '（北京）';
    const suffix = oldPage ? ' · 新结果已发布，本页仍是旧数据' : '';
    const info = snap ? (snap.summary || {}) : {};
    const busyText = phase => {
      if (phase === 'dispatching' || phase === 'queued') return '正在提交运行请求…';
      if (phase === 'running') return '正在抓取最新行情…';
      if (phase === 'generated') return '抓取完成，正在发布到看板…';
      return '正在抓取…';
    };
    if (manual && manualState.phase === 'failed' && (!manual.rejected || !checkFeedback || publication.phase !== 'published')) {
      return {phase: manualState.cancelled ? 'busy' : 'bad',
        text: manual.rejected ? manualState.message : failureText(manualState)};
    }
    if (activeManual()) return {phase: 'busy', text: busyText(manualState.phase)};
    if (Object.keys(targets).length) {
      if (configState.phase === 'failed') {
        return configState.cancelled
          ? {phase: 'busy', text: configState.message + '，不是抓取失败' + keptData()}
          : {phase: 'bad', text: '运行失败：本次运行未成功' + keptData()};
      }
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
      const risk = snap.market_risk || {};
      const missingRisk = Array.isArray(risk.missing) ? risk.missing : [];
      const deficits = [];
      if (info.gray) deficits.push('缺失 ' + info.gray);
      if (unsupported) deficits.push(unsupported + ' 个特殊代码暂不支持报价');
      if (missingRisk.length) {
        const valid = risk.valid_count ?? (5 - missingRisk.length);
        const parts = [kind + '运行已完成',
          '市场风险数据缺失：' + missingRisk.join('、') + '（有效指标 ' + valid + '/5）',
          ...deficits, '完成于 ' + (finished || '未记录') + '（北京时间）'];
        return {phase: 'partial', text: parts.join(' · ') + suffix, reload: !!oldPage};
      }
      const extra = deficits.length ? ' · ' + deficits.join(' · ') : '';
      return {phase: 'ok', text: success + extra + suffix, reload: !!oldPage};
    }
    if (publication.phase === 'waiting') return {phase: 'busy', text: '正在读取运行状态…'};
    return {phase: 'idle', text: '点「立即运行」抓最新行情'};
  }
  function put(id, text, phase) {
    const box = el(id);
    if (!box) return;
    box.textContent = text;
    if (id === 'runLightTxt') {
      const match = String(text).match(/^(.*?)(\d{4}-\d{2}-\d{2} \d{2}:\d{2})([\s\S]*)$/);
      if (match) {
        const timestamp = document.createElement('span');
        timestamp.className = 'run-timestamp';
        timestamp.textContent = match[2];
        box.replaceChildren(document.createTextNode(match[1]), timestamp, document.createTextNode(match[3]));
      }
    }
    box.style.whiteSpace = 'pre-wrap';
    box.dataset.phase = phase;
    box.setAttribute('aria-live', 'polite');
  }
  function render() {
    const s = state();
    if (destroyed) return s;
    let message = s.message;
    const embedded = el('snapshotData');
    if (s.phase === 'published' && embedded && verifiedSnapshot) {
      try {
        if (!samePublication(verifiedSnapshot, JSON.parse(embedded.textContent))) {
          message += ' 已有新版，请刷新当前看板。';
        }
      } catch (_) { message += '；当前页快照无效，请刷新当前看板'; }
    }
    if (actionsError && (activeManual() || activeConfig())) {
      message += '\n运行记录：暂无法核对；' + actionsError;
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
        refreshLink(el('checkResult'), 'display:block;margin-top:6px;color:#6ba3f0');
      }
    }
    const light = lightState();
    if (el('runLight')) el('runLight').dataset.phase = light.phase;
    put('runLightTxt', light.text, light.phase);
    if (light.reload && el('runLightTxt')) {
      refreshLink(el('runLightTxt'), 'display:inline-block;margin-left:8px;color:#6ba3f0');
    }
    const rule = ruleState();
    if (el('ruleLight')) el('ruleLight').dataset.phase = rule.phase;
    put('ruleLightTxt', rule.text, rule.phase);
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
    // btnRun 在现网页面里已不存在（只有设置页的 btnRunNow），留着只会每次 render 白查一次 null。
    const runBtn = el('btnRunNow');
    if (runBtn) runBtn.disabled = !!activeManual();
    const box = el('appliedState') || el('runMsg') || el('runState');
    const canReturn = verifiedSnapshot && (!Object.keys(targets).length || configState.phase === 'published') &&
      (!manual || manual.rejected || manualState.phase === 'published');
    if (!bindBoardReturn(canReturn ? verifiedSnapshot : null, version) && box && canReturn) {
      const link = window.document.createElement('a');
      link.textContent = '返回看板';
      link.href = bust('./index.html') + '&mm_run=' + encodeURIComponent(verifiedSnapshot.run_id);
      link.style.cssText = 'display:inline-block;margin:8px;color:#6ba3f0';
      box.appendChild(link);
    }
    return s;
  }
  function phaseForRun(run) {
    if (!run) return {phase: 'queued', message: '等待定位对应运行'};
    const id = String(run.id);
    if (run.status === 'completed') {
      if (run.conclusion === 'success') return {phase: 'generated', message: 'run ' + id + ' 生成完成，等待发布'};
      if (run.conclusion === 'cancelled') {
        // 取消可能来自队列替换或手动取消，不能归为抓取失败。
        return {phase: 'failed', cancelled: true, message: 'run ' + id + ' 已取消'};
      }
      return {phase: 'failed', message: 'run ' + id + ' 未成功：' + (run.conclusion || '结论未知')};
    }
    if (run.status === 'in_progress') return {phase: 'running', message: 'run ' + id + ' 运行中'};
    return {phase: 'queued', message: 'run ' + id + ' 排队中'};
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
    window.clearTimeout(timer);
    timer = null;
    // 显式核对还会继续读最新 run/jobs；由它的 finally 恢复唯一一轮后台轮询。
    if (destroyed || checkController) return;
    const ruleWaiting = ruleSince > 0 && Date.now() - ruleSince < MAX_WAIT && ruleState().text !== '需要刷新';
    const tracking = activeConfig() || activeManual() || retryPending || ruleWaiting;
    const boardWatching = !!(el('ruleLight') && el('snapshotData'));
    if (window.document.hidden || (!tracking && !boardWatching && publicRetry >= PUBLIC_RETRY_MAX)) return;
    const since = Math.min(activeConfig() ? configSince : Infinity, activeManual() ? manual.since : Infinity,
      ruleWaiting ? ruleSince : Infinity);
    const delay = Math.max(apiBlockedUntil - Date.now(), tracking ? (Date.now() - since < 120000 ? 8000 : 20000)
      : publicRetry >= PUBLIC_RETRY_MAX ? 60000 : (publicRetry === 0 ? 4000 : 10000));
    const epoch = version;
    timer = window.setTimeout(() => {
      if (!destroyed && !checkController && epoch === version) void tick();
    }, delay);
  }
  async function tick() {
    if (destroyed || checkController) return state();
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
        /* 2026-11-01：status.json 改条件校验。原来带 _mm 时间戳 + no-store，60s 一轮
           就是 232KB 全量重传（约 14MB/小时）；Pages 带 ETag，未变更时折成 304、零 body，
           内容真变了照样拿到完整新文件，语义不变。 */
        status = await request('./status.json', {signal, revalidate: true});
        if (!schema(status)) throw new Error('status.json 缺少有效 run_id/配置 blob SHA，无法验证生效');
        // index.html 走条件校验（revalidate）：Pages 带 ETag，本页没变时返回 304、零 body，
        // 弱网下从「重传 140KB」降为一次小往返；内容真变了仍会拿到完整新页面，语义不变。
        const html = await request('./index.html', {signal, text: true, revalidate: true});
        const raw = embeddedSnapshot(html);
        if (!raw) throw new Error('看板缺少 snapshotData；生成成功不等于已发布');
        const candidate = JSON.parse(raw);
        if (samePublication(status, candidate)) snapshot = candidate;
        else publicError = 'status.json 与看板快照不一致，仍在等待 Pages 发布';
      } catch (error) { publicError = networkMessage(error); }
    })();
    const actionTask = (async () => {
      try {
        if (manualTarget && !manualTarget.rejected && manualState.phase !== 'published') {
          nextManualRun = manualTarget.run_id
            ? await request(apiURL('/actions/runs/' + encodeURIComponent(manualTarget.run_id)), {github: true, signal})
            : await findRun('&event=workflow_dispatch', run =>
              run.event === 'workflow_dispatch' && (run.display_title || '').includes(manualTarget.request_id), signal);
        }
        if (Object.keys(expected).length && configState.phase !== 'published') {
          const newest = Object.values(expected).reduce((a, b) => a.revision > b.revision ? a : b);
          nextConfigRun = configRun
            ? await request(apiURL('/actions/runs/' + encodeURIComponent(configRun.id)), {github: true, signal})
            : await findRun('&event=push', run => run.event === 'push' && run.head_sha === newest.commitSHA, signal);
        }
      } catch (error) { actionError = networkMessage(error); }
    })();
    let nextRules = null, nextRulesError = '';
    const applyRules = () => {
      if (epoch !== version || destroyed) return;
      if (nextRules) {
        if (!latestRules || FILES.some(file => nextRules.files[file] !== latestRules.files[file])) ruleSince = 0;
        latestRules = {...nextRules};
        const current = el('snapshotData') ? pageSnapshot() : (snapshot || verifiedSnapshot);
        const receiptPending = Object.keys(expected).length > 0 &&
          Object.entries(expected).every(([file, receipt]) => nextRules.files[file] === receipt.blobSHA) &&
          (!current || !configMatches(current, expected));
        if (!sameConfig(current, nextRules.files) || receiptPending) {
          if (!ruleSince) ruleSince = Date.now();
        } else ruleSince = 0;
      }
      rulesError = nextRulesError;
      render();
    };
    const rulesTask = (async () => {
      const checkedAt = Date.now();
      const readSHA = async (file, ref = options.branch) => {
        try {
          const value = await request(bust(apiURL('/contents/' + file + '?ref=' +
            encodeURIComponent(ref))), {github: true, signal});
          if (!SHA.test(value && value.sha || '')) throw new Error('配置缺少有效 blob SHA');
          return value.sha;
        } catch (error) {
          if (signal.aborted || destroyed) throw error;
          // 公开原文按 Git blob 哈希核验，两类配置采用同一口径；令牌不发送给 raw 主机。
          const raw = await request(bust('https://raw.githubusercontent.com/' +
            encodeURIComponent(options.owner) + '/' + encodeURIComponent(options.repo) + '/' +
            encodeURIComponent(ref) + '/' + file), {signal, text: true});
          JSON.parse(raw);
          const bytes = new TextEncoder().encode(raw);
          const header = new TextEncoder().encode('blob ' + bytes.length + '\0');
          const blob = new Uint8Array(header.length + bytes.length);
          blob.set(header); blob.set(bytes, header.length);
          const hash = await window.crypto.subtle.digest('SHA-1', blob);
          return Array.from(new Uint8Array(hash), b => b.toString(16).padStart(2, '0')).join('');
        }
      };
      try {
        const hashes = await Promise.all(FILES.map(file => readSHA(file)));
        nextRules = {files: Object.fromEntries(FILES.map((file, i) => [file, hashes[i]])),
          checkedAt, run: null, commitSHA: null, runError: ''};
      } catch (error) { nextRulesError = networkMessage(error); applyRules(); return; }
      if (epoch !== version || destroyed) return;
      // 两份规则 SHA 一齐完成就渲染；发布和运行查询不能阻塞规则灯。
      applyRules();
      const current = el('snapshotData') ? pageSnapshot() : verifiedSnapshot;
      const pending = Object.entries(expected).some(([file, receipt]) =>
        nextRules.files[file] === receipt.blobSHA && (!current || !configMatches(current, expected)));
      if (!pending && (sameConfig(current, nextRules.files) || sameConfig(verifiedSnapshot, nextRules.files))) return;
      try {
        const changed = FILES.filter(file => !current || current.config_files[file] !== nextRules.files[file] || expected[file]);
        const commits = await Promise.all(changed.map(async file => {
          const list = await request(bust(apiURL('/commits?path=' + file + '&sha=' +
            encodeURIComponent(options.branch) + '&per_page=1')), {github: true, signal});
          return Array.isArray(list) ? list[0] : null;
        }));
        const candidates = [...new Map(commits.filter(c => c && SHA.test(c.sha || '')).map(c => [c.sha, c])).values()];
        candidates.sort((a, b) => Date.parse(b.commit?.committer?.date || '') - Date.parse(a.commit?.committer?.date || ''));
        for (const commit of candidates) {
          const hashes = await Promise.all(FILES.map(file => readSHA(file, commit.sha)));
          if (!FILES.every((file, i) => hashes[i] === nextRules.files[file])) continue;
          // 只查确实同时带这份清单和规则的提交对应的运行，不能借用无关任务结论。
          nextRules.commitSHA = commit.sha;
          nextRules.run = nextConfigRun && nextConfigRun.head_sha === commit.sha ? nextConfigRun :
            await findRun('&event=push', run => run.event === 'push' && run.head_sha === commit.sha, signal);
          if (nextRules.run) break;
        }
      } catch (error) { nextRules.runError = networkMessage(error); }
      applyRules();
    })();
    await Promise.all([publicTask, actionTask, rulesTask]);
    if (epoch !== version || destroyed) return state();
    actionsError = actionError;
    // 网络失败时保留上次成功读到的状态：状态灯不该因为一次断网就从绿灯跳黄。
    if (schema(status)) latestStatus = status;
    if (snapshot) verifiedSnapshot = snapshot;
    configRun = nextConfigRun;
    publication = snapshot
      ? {phase: 'published', message: '当前发布：run ' + snapshot.run_id + '，已核验。'}
      : {phase: 'waiting', message: publicError || '发布结果尚未核验'};
    if (snapshot) { saveLast(snapshot); publicRetry = PUBLIC_RETRY_MAX; }
    else if (publicError && publicRetry < PUBLIC_RETRY_MAX) publicRetry++;
    retryPending = !snapshot && (!!publicError || !!actionError) && retryPending;
    if (Object.keys(expected).length) {
      if (snapshot && configMatches(status, expected) && configMatches(snapshot, expected)) {
        configState = {phase: 'published', message: '本页保存的清单与规则均已发布生效。'};
      } else if (configState.phase !== 'published') {
        // 发布证据属于这组保存回执；查询失败不能撤销，新保存由 watchConfig 重置。
        configState = phaseForRun(nextConfigRun);
        if (configState.phase !== 'failed' && status && configMatches(status, expected)) {
          configState = {phase: 'generated', message: '配置生成完成，等待发布'};
        }
        if (Date.now() - configSince >= MAX_WAIT && configState.phase !== 'failed') {
          configState = {phase: 'expired', message: '配置跟踪超时（10 分钟）；保存记录仍保留，请稍后查运行状态，不代表运行失败'};
        } else if (publicError) configState.message += '\n' + publicError;
      }
    }
    if (manualTarget && !manualTarget.rejected && manualState.phase !== 'published' &&
        (!dispatchController || nextManualRun || manualMatches(status, manualTarget))) {
      if (nextManualRun) manual.run_id = String(nextManualRun.id);
      else if (!manual.run_id && manualMatches(status, manual)) manual.run_id = status.run_id;
      const runPhase = phaseForRun(nextManualRun);
      if (runPhase.phase === 'failed') manualState = runPhase;
      else if (snapshot && manualMatches(status, manual) && manualMatches(snapshot, manual)) {
        manualState = {phase: 'published', message: '本次 run ' + manual.run_id + ' 已发布，核验通过。'};
      } else {
        manualState = runPhase;
        if (manualMatches(status, manual)) manualState = {phase: 'generated', message: '本次运行生成完成，等待发布'};
        if (Date.now() - manual.since >= MAX_WAIT) {
          manualState = {phase: 'expired', message: '运行跟踪超时（10 分钟）；请求记录仍保留，请稍后查运行状态，不代表运行失败'};
        } else if (publicError) manualState.message += '\n' + publicError;
      }
    }
    if (Date.now() - configSince >= MAX_WAIT && activeConfig()) {
      configState = {phase: 'expired', message: '配置跟踪超时；保存记录仍保留，请稍后查运行状态，不代表运行失败'};
    }
    if (manual && Date.now() - manual.since >= MAX_WAIT && activeManual()) {
      manualState = {phase: 'expired', message: '运行跟踪超时；请求记录仍保留，请稍后查运行状态，不代表运行失败'};
    }
    // 已验证目标也保留；之后保存另一文件时仍校验旧文件，防止被旧运行回滚。
    render();
    schedule();
    return state();
  }
  function refresh() {
    if (destroyed) return Promise.resolve(state());
    if (!initialized) init({}, false);
    cancelCheck();
    publicRetry = 0;
    if (configState.phase === 'expired') { configSince = Date.now(); configState.phase = 'waiting'; }
    if (manual && manualState.phase === 'expired') { manual.since = Date.now(); manualState.phase = 'waiting'; }
    return tick();
  }
  function watchConfig({file, blobSHA, commitSHA}) {
    if (destroyed) return Promise.resolve(state());
    if (!initialized) init({}, false);
    if (!FILES.includes(file) || !SHA.test(blobSHA || '') || !SHA.test(commitSHA || '')) {
      throw new Error('watchConfig 需要 holdings.json/settings.json 及提交回执中的有效 blobSHA、commitSHA');
    }
    cancelCheck();
    cancelPoll();
    targets[file] = {blobSHA, commitSHA, savedAt: Date.now(), revision: ++receiptVersion};
    configSince = Date.now();
    retrySince = Date.now();
    configRun = null;
    configState = {phase: 'queued', message: '清单或规则已保存，等待更新'};
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
    if (destroyed) return state();
    if (!initialized) init({}, false);
    if (dispatchController || activeManual()) return state();
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
    cancelCheck();
    cancelPoll();
    retrySince = Date.now();
    manual = {request_id: uuid(), run_id: null, since: Date.now()};
    manualState = {phase: 'dispatching', message: '正在提交运行请求…'};
    const target = manual;
    const dispatch = new AbortController();
    dispatchController = dispatch;
    const current = () => !destroyed && !dispatch.signal.aborted && manual === target;
    render();
    try {
      await request(apiURL('/actions/workflows/daily.yml/dispatches'), {
        method: 'POST', github: true, signal: dispatch.signal,
        body: {ref: options.branch, inputs: {request_id: target.request_id}}
      });
      if (!current() || manualState.phase !== 'dispatching') return state();
      manualState = {phase: 'queued', message: '请求已接受，等待定位运行'};
    } catch (error) {
      if (!current() || manualState.phase !== 'dispatching') return state();
      if (!error.status) {
        // POST 超时可能已被接受：继续查同一 UUID，不自动重复派发。
        manualState = {phase: 'queued', message: '请求是否接受尚未确认：' + error.message + '；继续跟踪，不重复发送'};
      } else {
        manual.rejected = true;
        manualState = {phase: 'failed', message: '未启动：运行请求被拒绝。' + error.message +
          (error.status === 422 ? '；daily.yml 必须声明 workflow_dispatch.inputs.request_id' : '')};
        render();
        schedule();
        return state();
      }
    } finally {
      if (dispatchController === dispatch) dispatchController = null;
    }
    render();
    return tick();
  }
  function runTime(iso) {
    const date = new Date(String(iso || ''));
    if (!iso || !Number.isFinite(date.getTime())) return '';
    const p = Object.fromEntries(new Intl.DateTimeFormat('en-US', {timeZone: 'Asia/Shanghai', month: '2-digit',
      day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23'}).formatToParts(date).map(x => [x.type, x.value]));
    return p.month + '/' + p.day + ' ' + p.hour + ':' + p.minute;
  }
  // 成功结束但没发布：生成步骤成功、提交步骤被跳过＝当日日线不齐主动不发布。读不到就当未知，不猜。
  async function publishSkip(runId, startedAt, signal) {
    try {
      const data = await request(bust(apiURL('/actions/runs/' + encodeURIComponent(runId) + '/jobs')), {github: true, signal});
      const steps = (data && Array.isArray(data.jobs) ? data.jobs : []).flatMap(j => Array.isArray(j.steps) ? j.steps : []);
      const step = name => steps.find(s => s && s.name === name);
      const gen = step('抓数据并生成页面'), commit = step('校验最新输入并提交更新');
      if (!gen || gen.conclusion !== 'success' || !commit || commit.conclusion !== 'skipped') return null;
      const retry = step('可恢复失败或缺数：30 分钟后同日有界重试') || step('当日日线未就绪：约 30 分钟后自动再试');
      // 重试步骤只在美东 20–23 点派发；到零点由保险任务的窗口检查终止。
      // hourCycle:'h23' 而不是 hour12:false —— 后者在部分 ICU 下把零点渲染成「24」，
      // h >= 20 && h < 24 会误判成「不在重试窗口」。
      const h = Number(new Date(String(startedAt || '')).toLocaleString('en-US', {timeZone: 'America/New_York', hour: '2-digit', hourCycle: 'h23'}));
      return {retry: !!(retry && retry.conclusion === 'success' && h >= 20 && h < 24)};
    } catch (error) { return null; }
  }
  // 「查运行状态」点了要有反馈：否则状态没变化时页面文字不动，看着像按钮坏了。
  async function manualCheck() {
    if (destroyed) return;
    const sequence = ++checkRequest;
    checkDismissed = false;
    const b = el('btnCheckStatus');
    if (b && checkLabel === null) checkLabel = b.textContent;
    if (b) { b.disabled = true; b.textContent = '核对中…'; }
    checkFeedback = '正在核对最新任务和已发布看板…';
    checkPhase = 'busy';
    render();
    let recentRun = null, recentError = '';
    const refreshed = refresh();
    const epoch = version;
    const check = new AbortController();
    checkController = check;
    const current = () => !destroyed && !check.signal.aborted && epoch === version && sequence === checkRequest;
    try {
      await refreshed;
      if (!current()) return;
      if ((!manual || manual.rejected) && !Object.keys(targets).length) {
        try {
          const data = await request(bust(apiURL('/actions/workflows/daily.yml/runs?branch=' +
            encodeURIComponent(options.branch) + '&per_page=1')), {github: true, signal: check.signal});
          recentRun = data && data.workflow_runs && data.workflow_runs[0] || null;
        } catch (error) { recentError = networkMessage(error); }
      }
      if (!current()) return;
      const published = publication.phase === 'published' && verifiedSnapshot;
      let nextCheckPhase = published ? 'ok' : 'busy';
      const lines = [];
      let task = null, action = '', reason = '';
      if (manual && manual.rejected) {
        lines.push('最新任务：未启动（手动）');
        reason = manualState.message.replace(/^未启动：运行请求被拒绝。/, '');
        action = '处理拒绝原因后再点立即运行；查状态不会重新派发。';
        nextCheckPhase = 'bad';
      } else if (manual) {
        task = {state: manualState, id: manual.run_id, origin: '手动'};
      } else if (Object.keys(targets).length) {
        task = {state: configState, id: configRun && configRun.id, origin: '保存后'};
      } else if (recentRun) {
        const phase = phaseForRun(recentRun);
        const samePublished = published && String(recentRun.id) === verifiedSnapshot.run_id;
        // 生成成功不等于发布；仅在提交步骤确实跳过时说明本轮未发布。
        const skip = phase.phase === 'generated' && !samePublished
          ? await publishSkip(recentRun.id, recentRun.run_started_at || recentRun.created_at, check.signal) : null;
        if (!current()) return;
        task = {state: samePublished && phase.phase === 'generated' ? {phase: 'published'} : phase,
          id: recentRun.id, at: runTime(recentRun.run_started_at || recentRun.created_at),
          conclusion: recentRun.conclusion, skip};
      } else {
        lines.push(recentError ? '最新任务：暂无法核对' : '最新任务：未找到记录');
        reason = recentError;
      }
      if (task) {
        const phase = task.state.phase;
        const labels = {dispatching: '提交中', queued: task.id ? '排队中' : '等待定位',
          running: '运行中', generated: task.skip ? '本轮未发布' : '等待发布',
          published: '已完成并发布', expired: '跟踪超时', waiting: '等待核验'};
        const label = phase === 'failed' ? (task.state.cancelled ? '已取消'
          : '失败' + (task.conclusion ? '（' + task.conclusion + '）' : '')) : labels[phase] || '等待核验';
        lines.push('最新任务：' + label + (task.origin ? '（' + task.origin + '）' : '') +
          (task.id ? ' · run ' + task.id : '') + (task.at ? '（' + task.at + ' 北京时间）' : ''));
        if (phase === 'failed') {
          nextCheckPhase = task.state.cancelled ? 'busy' : 'bad';
          action = task.state.cancelled ? '不是抓取失败；继续使用上一版。'
            : '本轮未发布，继续使用上一版；请查看运行记录。';
          if (!task.state.cancelled && !task.conclusion) {
            reason = task.state.message.replace(/^run \S+ 未成功：/, '');
          }
        } else if (phase !== 'published') {
          nextCheckPhase = 'busy';
          action = task.skip ? '当日日线未就绪，保留上一版。' +
            (task.skip.retry ? '约 30 分钟后自动再试。' : '')
            : phase === 'expired' ? '尚不能确认运行结果，请稍后查运行状态。'
            : phase === 'generated' ? '生成完成，等待发布；暂不需要刷新。'
            : '新任务尚未发布，暂不需要刷新。';
        }
      }
      if (published) {
        const current = pageSnapshot();
        const oldPage = current && !samePublication(current, verifiedSnapshot);
        lines.push('当前发布：run ' + verifiedSnapshot.run_id +
          (oldPage ? '。' : current ? '，当前页面与线上版本一致。' : '，已核验。'));
        lines.push('数据更新时间：' + (humanTime(verifiedSnapshot) || '未知') + '（北京时间）');
        if (oldPage) {
          // 已有可用新版时刷新提示优先，不能因另一个任务未结束而误称无需刷新。
          action = action.replace(/[，；]?暂不需要刷新。/, '');
          if (action && !action.endsWith('。')) action += '。';
          action = (task && task.state.phase === 'published' ? '' : action) + '已有新版，请刷新当前看板。';
        }
      } else {
        lines.push('当前发布：暂无法核验；' + publication.message);
        // 未核验发布时不宣称当前页最新，也不建议盲目刷新。
        action = action.replace(/[，；]?暂不需要刷新。/, '');
        if (action && !action.endsWith('。')) action += '。';
      }
      if (reason) lines.push('原因：' + reason);
      if (actionsError) lines.push('运行记录：暂无法核对；' + actionsError);
      if (action) lines.push(action);
      if (recentError || actionsError) nextCheckPhase = 'bad';
      if (current() && !checkDismissed) {
        checkPhase = nextCheckPhase;
        checkFeedback = lines.join('\n');
        render();
      }
    } finally {
      if (checkController === check) {
        checkController = null;
        if (current()) schedule();
      }
      if (!destroyed && sequence === checkRequest) {
        if (b) {
          b.disabled = false;
          if (checkLabel !== null) b.textContent = checkLabel;
        }
        if (!current() && !checkDismissed && checkFeedback) {
          checkFeedback = '核对已中止；请再次查运行状态';
          checkPhase = 'idle';
          render();
        }
      }
    }
  }

  function init(opts = {}, start = true) {
    if (destroyed || initialized && !Object.keys(opts).length) return window.MMRunStatus;
    cancelCheck();
    cancelPoll();
    options = {owner: 'nixhuang', repo: 'market-monitor', branch: 'main', timeoutMs: 15000, ...options, ...opts};
    initialized = true;
    if (el('btnRunNow')) el('btnRunNow').onclick = () => { void startManual(); };
    if (el('btnCheckStatus')) el('btnCheckStatus').onclick = () => manualCheck();
    render(); // 不等网络请求，先保留本页生成数据并折叠旧版摘要。
    if (start) void refresh();
    return window.MMRunStatus;
  }
  window.MMRunStatus = {refresh, watchConfig, startManual, init, destroy};
  window.document.addEventListener('visibilitychange', () => {
    if (!initialized || destroyed || checkController) return;
    if (window.document.hidden) cancelPoll();
    else void refresh();
  });
  // 首页这轮改版后已经没有 runState / runMsg / runSummary 了，只看这三个会导致
  // 首页根本不 init —— 按钮没绑 onclick，点了就是「没反应」。改成任一控件存在即初始化。
  function autoInit() {
    if (initialized || destroyed) return;
    const ids = ['runMsg', 'runSummary', 'runLight', 'runLightTxt',
                 'ruleLight', 'ruleLightTxt', 'btnRunNow', 'btnCheckStatus', 'appliedState'];
    if (ids.some(id => el(id))) init();
  }
  if (window.document.readyState === 'loading') window.document.addEventListener('DOMContentLoaded', autoInit);
  else autoInit();
})(window);
