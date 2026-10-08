'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {webcrypto, createHash} = require('node:crypto');
const code = fs.readFileSync(path.join(__dirname, '..', 'run-status.js'), 'utf8');
const A = 'a'.repeat(40), B = 'b'.repeat(40), C = 'c'.repeat(40), D = 'd'.repeat(40);
const clone = v => JSON.parse(JSON.stringify(v));
function snapshot(sha = B, run = '1') {
  return {run_id: run, request_id: '', source_sha: D, started_at: '2026-10-08T01:00:00Z',
    config_files: {'holdings.json': A, 'settings.json': sha},
    summary: {red: 44, yellow: 20, green: 65, total: 129, stale_symbols: [], missing_symbols: []}};
}
function element() {
  return {textContent: '', style: {}, dataset: {}, children: [], disabled: false,
    setAttribute() {}, appendChild(child) { this.children.push(child); },
    classList: {contains() { return false; }}};
}
function fixture(page = snapshot(), onlyRule = false) {
  const els = Object.fromEntries((onlyRule ? ['ruleLight', 'ruleLightTxt'] :
    ['runLight', 'runLightTxt', 'ruleLight', 'ruleLightTxt', 'runSummary', 'btnCheckStatus']).map(id => [id, element()]));
  if (page) { els.snapshotData = element(); els.snapshotData.textContent = JSON.stringify(page); }
  const data = {status: snapshot(), published: snapshot(), sha: B, failRules: false, failPublic: false,
    run: {id: 2, event: 'push', head_sha: D, status: 'in_progress', conclusion: null}, delay: null, commitSHA: null, raw: null};
  const urls = [], timers = [], events = {};
  let now = Date.parse('2026-10-08T02:00:00Z');
  class Clock extends Date { static now() { return now; } }
  const win = {document: {readyState: 'complete', getElementById: id => els[id] || null,
      createElement: element, addEventListener(name, fn) { events[name] = fn; }},
    addEventListener(name, fn) { events[name] = fn; },
    localStorage: {getItem() { return null; }, setItem() {}},
    setTimeout(fn, ms) { timers.push({fn, ms}); return timers.length; }, clearTimeout() {},
    Date: Clock, AbortController, TextEncoder, crypto: webcrypto, DOMParser: class { parseFromString(html) {
      return {querySelector() { return {textContent: html}; }};
    }},
    fetch: async url => {
      urls.push(url);
      const response = v => ({ok: true, status: 200, json: async () => clone(v), text: async () => JSON.stringify(v)});
      if (url.includes('/contents/settings.json')) {
        if (data.delay) return data.delay;
        if (data.failRules) throw new TypeError('Failed to fetch');
        return response({sha: url.includes('ref=' + D) ? data.commitSHA || data.sha : data.sha});
      }
      if (url.includes('raw.githubusercontent.com')) {
        if (data.raw === null) throw new TypeError('Failed to fetch');
        return {ok: true, status: 200, text: async () => data.raw};
      }
      if (url.includes('/commits?')) return response([{sha: D}]);
      if (url.includes('/actions/workflows/')) return response({workflow_runs: data.run ? [data.run] : []});
      if (url.includes('/actions/runs/')) return response(data.run);
      if (data.failPublic) throw new TypeError('Failed to fetch');
      if (url.includes('status.json')) return response(data.status);
      if (url.includes('index.html')) return response(data.published);
      throw new Error('Unexpected URL ' + url);
    }};
  win.window = win;
  vm.runInNewContext(code, win);
  return {win, els, data, urls, timers, advance(ms) { now += ms; }};
}
async function check(name, fn) { await fn(); console.log('PASS ' + name); }
(async () => {
  await check('相同 settings blob 规则绿灯；运行灯不再声明规则生效', async () => {
    const f = fixture(); await f.win.MMRunStatus.refresh();
    assert.equal(f.els.ruleLight.dataset.phase, 'ok');
    assert.match(f.els.ruleLightTxt.textContent, /规则按当前设置生效/);
    assert.match(f.els.runLightTxt.textContent, /红44 黄20 绿65/);
    assert.doesNotMatch(f.els.runLightTxt.textContent, /规则/);
  });
  await check('最新设置已保存、Pages 设置尚旧也能检测黄灯', async () => {
    const f = fixture(); f.data.sha = C; await f.win.MMRunStatus.refresh();
    assert.equal(f.els.ruleLight.dataset.phase, 'busy');
    assert.match(f.els.ruleLightTxt.textContent, /规则已保存，正在重跑/);
    assert.equal(f.els.runLight.dataset.phase, 'ok');
    assert(f.urls.some(url => url.includes('/contents/settings.json')));
    assert(f.timers.some(t => t.ms === 60000));
  });
  await check('新发布不能把旧页面规则变绿；刷新新页后才绿', async () => {
    const f = fixture(); f.data.sha = C; f.data.status = snapshot(C, '2'); f.data.published = snapshot(C, '2');
    f.data.published.summary.red = 1; await f.win.MMRunStatus.refresh();
    assert.equal(f.els.ruleLight.dataset.phase, 'busy');
    assert.match(f.els.ruleLightTxt.textContent, /本页仍是旧规则/);
    assert.equal(f.els.ruleLightTxt.children.at(-1).textContent, '打开最新看板');
    assert.match(f.els.runLightTxt.textContent, /红44/);
    f.els.snapshotData.textContent = JSON.stringify(f.data.published);
    await f.win.MMRunStatus.refresh(); assert.equal(f.els.ruleLight.dataset.phase, 'ok');
  });
  await check('行情缺失运行红灯，但同版规则仍绿灯', async () => {
    const page = snapshot(); page.summary.missing_symbols = ['NTNX', 'MCD'];
    const f = fixture(page); await f.win.MMRunStatus.refresh();
    assert.equal(f.els.runLight.dataset.phase, 'bad');
    assert.match(f.els.runLightTxt.textContent, /抓取失败 2 只/);
    assert.equal(f.els.ruleLight.dataset.phase, 'ok');
  });
  await check('对应规则运行失败红灯，别误用行情绿灯', async () => {
    const f = fixture(); f.data.sha = C; f.data.run.status = 'completed'; f.data.run.conclusion = 'failure';
    await f.win.MMRunStatus.refresh(); assert.equal(f.els.ruleLight.dataset.phase, 'bad');
    assert.match(f.els.ruleLightTxt.textContent, /对应运行失败/);
    assert.equal(f.els.runLight.dataset.phase, 'ok');
  });
  await check('规则已生成、产物不一致继续黄灯', async () => {
    const f = fixture(); f.data.sha = C; f.data.status = snapshot(C, '2');
    f.data.run.status = 'completed'; f.data.run.conclusion = 'success';
    await f.win.MMRunStatus.refresh(); assert.equal(f.els.ruleLight.dataset.phase, 'busy');
    assert.match(f.els.ruleLightTxt.textContent, /等待看板发布/);
  });
  await check('无法读最新规则不误绿；运行灯保留当前页数据', async () => {
    const f = fixture(); await f.win.MMRunStatus.refresh();
    f.data.failRules = true; f.data.failPublic = true;
    await f.win.MMRunStatus.refresh(); assert.equal(f.els.ruleLight.dataset.phase, 'idle');
    assert.match(f.els.ruleLightTxt.textContent, /暂无法核对/);
    assert.equal(f.els.runLight.dataset.phase, 'ok');
  });
  await check('无关运行失败不影响已经应用的规则', async () => {
    const f = fixture(); f.data.run.status = 'completed'; f.data.run.conclusion = 'failure';
    await f.win.MMRunStatus.refresh(); assert.equal(f.els.ruleLight.dataset.phase, 'ok');
  });
  await check('保存清单不把同版规则灯变黄', async () => {
    const f = fixture(); await f.win.MMRunStatus.refresh();
    await f.win.MMRunStatus.watchConfig({file: 'holdings.json', blobSHA: C, commitSHA: D});
    assert.equal(f.els.ruleLight.dataset.phase, 'ok');
  });
  await check('规则等待超过十分钟明确旧规则但不捏造运行失败', async () => {
    const f = fixture(); f.data.sha = C; await f.win.MMRunStatus.refresh(); f.advance(11 * 60000);
    await f.win.MMRunStatus.refresh(); assert.equal(f.els.ruleLight.dataset.phase, 'bad');
    assert.match(f.els.ruleLightTxt.textContent, /不代表运行失败/);
  });
  await check('只有规则灯控件也自动初始化', async () => {
    const f = fixture(null, true);
    assert(f.urls.some(url => url.includes('/contents/settings.json')));
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(f.els.ruleLight.dataset.phase, 'ok');
  });
  await check('旧的慢请求不能覆盖新核验', async () => {
    const f = fixture(); await f.win.MMRunStatus.refresh();
    let resolve; f.data.delay = new Promise(r => { resolve = r; });
    const old = f.win.MMRunStatus.refresh(); f.data.delay = null; f.data.sha = C;
    await f.win.MMRunStatus.refresh();
    resolve({ok: true, status: 200, json: async () => ({sha: B})}); await old;
    assert.equal(f.els.ruleLight.dataset.phase, 'busy');
  });
  await check('新规则已发布后短暂断网仍提示旧页并保留入口', async () => {
    const f = fixture(); f.data.sha = C; f.data.status = snapshot(C, '2'); f.data.published = snapshot(C, '2');
    await f.win.MMRunStatus.refresh(); f.data.failPublic = true;
    await f.win.MMRunStatus.refresh(); assert.match(f.els.ruleLightTxt.textContent, /本页仍是旧规则/);
    assert.equal(f.els.ruleLightTxt.children.at(-1).textContent, '打开最新看板');
    assert.equal(f.els.ruleLight.dataset.phase, 'busy');
  });
  await check('同轮提交版本变化不能套用另一版本的失败运行', async () => {
    const f = fixture(); f.data.sha = C; f.data.commitSHA = A;
    f.data.run.status = 'completed'; f.data.run.conclusion = 'failure';
    await f.win.MMRunStatus.refresh(); assert.equal(f.els.ruleLight.dataset.phase, 'busy');
    assert.doesNotMatch(f.els.ruleLightTxt.textContent, /运行失败/);
  });
  await check('API 限流可用原始设置核验；UTF-8 与换行采用 Git blob 哈希', async () => {
    for (const raw of ['{"说明":"规则","amp_yellow":3}\n', '{"说明":"规则","amp_yellow":3}\r\n']) {
      const bytes = Buffer.from(raw);
      const sha = createHash('sha1').update(Buffer.from('blob ' + bytes.length + '\0')).update(bytes).digest('hex');
      const f = fixture(snapshot(sha)); f.data.raw = raw; f.data.failRules = true;
      await f.win.MMRunStatus.refresh(); assert.equal(f.els.ruleLight.dataset.phase, 'ok');
    }
  });
  await check('空清单及全部不支持报价均为闲置，不误报成功', async () => {
    for (const [total, unsupported, expected] of [[0, [], /清单为空，未抓取报价/],
      [1, ['.SPX'], /1 个特殊代码暂不支持报价，未抓取报价/]]) {
      const page=snapshot();
      page.summary={red:0,yellow:0,green:0,gray:total,total,stale_symbols:[],missing_symbols:[],unsupported_symbols:unsupported};
      const f=fixture(page);await f.win.MMRunStatus.refresh();
      assert.equal(f.els.runLight.dataset.phase,'idle');
      assert.match(f.els.runLightTxt.textContent,expected);
      assert.doesNotMatch(f.els.runLightTxt.textContent,/抓取成功/);
    }
  });
  console.log('全部通过');
})().catch(error => { console.error(error); process.exitCode = 1; });
