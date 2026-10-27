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
    setAttribute(name, value) { this[name] = value; }, appendChild(child) { this.children.push(child); },
    classList: {contains() { return false; }}};
}
function fixture(page = snapshot(), onlyRule = false) {
  const els = Object.fromEntries((onlyRule ? ['ruleLight', 'ruleLightTxt'] :
    ['runLight', 'runLightTxt', 'ruleLight', 'ruleLightTxt', 'runSummary', 'runMsg', 'checkResult', 'btnCheckStatus', 'btnReturnBoard']).map(id => [id, element()]));
  // 首页这颗按钮渲染出来就是「状态」；点完必须还原成页面原本文案，不能变成别的名字。
  if (els.btnCheckStatus) els.btnCheckStatus.textContent = '状态';
  if (page) { els.snapshotData = element(); els.snapshotData.textContent = JSON.stringify(page); }
  const data = {status: snapshot(), published: snapshot(), sha: B, holdingsSHA: A, failHoldings: false,
    failRules: false, failPublic: false,
    run: {id: 2, event: 'push', head_sha: D, status: 'in_progress', conclusion: null}, delay: null, commitSHA: null, raw: null, reloads: []};
  const urls = [], timers = [], events = {};
  let now = Date.parse('2026-10-08T02:00:00Z');
  class Clock extends Date { static now() { return now; } }
  const win = {document: {readyState: 'complete', getElementById: id => els[id] || null,
      createElement: element, addEventListener(name, fn) { events[name] = fn; }},
    addEventListener(name, fn) { events[name] = fn; },
    localStorage: {getItem() { return null; }, setItem() {}},
    setTimeout(fn, ms) { timers.push({fn, ms}); return timers.length; }, clearTimeout() {},
    location: {href: 'https://example.test/market-monitor/index.html', replace(url) { data.reloads.push(url); }},
    URL, Date: Clock, AbortController, TextEncoder, crypto: webcrypto, DOMParser: class { parseFromString(html) {
      return {querySelector() { return {textContent: html}; }};
    }},
    fetch: async (url, options) => {
      urls.push(url);
      const response = v => ({ok: true, status: 200, json: async () => clone(v), text: async () => JSON.stringify(v)});
      if (url.includes('/dispatches')) {
        data.posts=(data.posts||0)+1;
        data.requestId=JSON.parse(options.body).inputs.request_id;
        assert.equal(options.headers.Authorization,'Bearer github_pat_test');
        if(data.dispatchSuccess)return {ok:true,status:204};
        const failure=data.dispatchError||{status:403,message:'Resource not accessible by personal access token'};
        return {ok:false,status:failure.status,json:async()=>({message:failure.message}),
          headers:{get:name=>(failure.headers||{})[name]??null}};
      }
      if (url.includes('/contents/holdings.json')) {
        if (data.failHoldings) throw new TypeError('Failed to fetch');
        return response({sha: url.includes('ref=' + D) ? data.commitHoldingsSHA || data.holdingsSHA : data.holdingsSHA});
      }
      if (url.includes('/contents/settings.json')) {
        if (data.delay) return data.delay;
        if (data.failRules) throw new TypeError('Failed to fetch');
        return response({sha: url.includes('ref=' + D) ? data.commitSHA || data.sha : data.sha});
      }
      if (url.includes('raw.githubusercontent.com')) {
        const raw = url.includes('/holdings.json') ? data.holdingsRaw : data.raw;
        if (raw == null) throw new TypeError('Failed to fetch');
        return {ok: true, status: 200, text: async () => raw};
      }
      if (url.includes('/commits?')) {
        if (data.failRunLookup) throw new TypeError('Failed to fetch');
        return response(data.commits || [{sha: D}]);
      }
      if (url.includes('/actions/workflows/')) return response({workflow_runs: data.run ? [data.run] : []});
      if (/\/actions\/runs\/\d+\/jobs/.test(url)) return response({jobs: data.jobs || []});
      if (url.includes('/actions/runs/')) return response(data.run);
      if (data.failPublic) throw new TypeError('Failed to fetch');
      if (url.includes('status.json')) return response(data.status);
      if (url.includes('index.html')) return response(data.published);
      throw new Error('Unexpected URL ' + url);
    }};
  win.window = win;
  vm.runInNewContext(code, win);
  return {win, els, data, urls, timers, events, advance(ms) { now += ms; }};
}
async function check(name, fn) { await fn(); console.log('PASS ' + name); }
(async () => {
  await check('相同 settings blob 规则绿灯；运行灯不再声明规则生效', async () => {
    const f = fixture(); await f.win.MMRunStatus.refresh();
    assert.equal(f.els.ruleLight.dataset.phase, 'ok');
    assert.match(f.els.ruleLightTxt.textContent, /^已生效$/);
    // 2026-10-20：运行灯不再重复念灯级计数，也不报「N 项数据已更新」，只报完成时间
    assert.match(f.els.runLightTxt.textContent, /^成功 ·(?:未记录|\d{4}-\d{2}-\d{2} \d{2}:\d{2})（北京时间）/);
    assert.doesNotMatch(f.els.runLightTxt.textContent, /红44 黄20 绿65/);
    assert.doesNotMatch(f.els.runLightTxt.textContent, /项数据已更新/);
    assert.doesNotMatch(f.els.runLightTxt.textContent, /规则/);
  });
  await check('返回入口初始化即存在；核对失败仍可避缓存重载本地路径且不伪造规则确认', async () => {
    const f = fixture();
    const link = f.els.btnReturnBoard;
    assert.equal(typeof link.onclick, 'function');
    f.data.failPublic = true; f.data.failRules = true;
    f.win.location.href = 'file:///C:/demo/demo-local.html?keep=1&_mm=old&mm_run=stale#rules';
    await f.win.MMRunStatus.refresh();
    assert.strictEqual(f.els.btnReturnBoard, link);
    assert.notEqual(f.els.ruleLight.dataset.phase, 'ok');
    const requests = f.urls.length;
    f.advance(1000);
    link.onclick();
    const url = new URL(link.href);
    assert.equal(url.protocol, 'file:');
    assert.equal(url.pathname, '/C:/demo/demo-local.html');
    assert.equal(url.hash, '');
    assert.equal(url.searchParams.get('keep'), '1');
    assert.notEqual(url.searchParams.get('_mm'), 'old');
    assert.equal(url.searchParams.getAll('_mm').length, 1);
    assert(!url.searchParams.has('mm_run'));
    assert.equal(f.urls.length, requests, '点击仅准备导航，不派发运行或核验请求');
  });
  await check('返回入口核验通过携带对应run且不重复创建；编辑页旧返回行为保留', async () => {
    const f = fixture(); await f.win.MMRunStatus.refresh();
    const link = f.els.btnReturnBoard;
    assert.equal(new URL(link.href).searchParams.get('mm_run'), '1');
    assert(!f.els.runMsg.children.some(child => child.textContent === '返回看板'));
    await f.win.MMRunStatus.refresh();
    assert.strictEqual(f.els.btnReturnBoard, link);
    assert(!f.els.runMsg.children.some(child => child.textContent === '返回看板'));
    const editor = fixture(); delete editor.els.btnReturnBoard;
    await editor.win.MMRunStatus.refresh();
    const legacy = editor.els.runMsg.children.find(child => child.textContent === '返回看板');
    assert(legacy && legacy.href.startsWith('./index.html?'));
  });
  await check('首页无页底运行详情仍核验规则与绑定返回；编辑页保留发布详情', async () => {
    const f = fixture(); delete f.els.runMsg;
    await f.win.MMRunStatus.refresh();
    assert.equal(f.els.ruleLight.dataset.phase, 'ok');
    assert.match(f.els.ruleLightTxt.textContent, /^已生效$/);
    assert.equal(typeof f.els.btnReturnBoard.onclick, 'function');
    assert.equal(new URL(f.els.btnReturnBoard.href).searchParams.get('mm_run'), '1');
    const editor = fixture(); delete editor.els.btnReturnBoard;
    await editor.win.MMRunStatus.refresh();
    assert.match(editor.els.runMsg.textContent, /当前发布：run 1，已核验/);
  });
  await check('最新设置已保存、Pages 设置尚旧也能检测黄灯', async () => {
    const f = fixture(); f.data.sha = C; await f.win.MMRunStatus.refresh();
    assert.equal(f.els.ruleLight.dataset.phase, 'busy');
    assert.equal(f.els.ruleLightTxt.textContent, '正在更新');
    assert.equal(f.els.runLight.dataset.phase, 'ok');
    assert(f.urls.some(url => url.includes('/contents/settings.json')));
    assert(f.timers.some(t => t.ms === 8000));
  });
  await check('新发布不能把旧页面规则变绿；刷新新页后才绿', async () => {
    const f = fixture(); f.data.sha = C; f.data.status = snapshot(C, '2'); f.data.published = snapshot(C, '2');
    f.data.published.summary.red = 1; await f.win.MMRunStatus.refresh();
    assert.equal(f.els.ruleLight.dataset.phase, 'busy');
    assert.equal(f.els.ruleLightTxt.textContent, '需要刷新');
    assert.equal(f.data.reloads.length, 0, '状态更新不能自动刷新当前页');
    // 计数已从运行灯移除（2026-10-20）：只核对仍是「本页成功」这条，且不再念计数
    assert.match(f.els.runLightTxt.textContent, /^成功 ·(?:未记录|\d{4}-\d{2}-\d{2} \d{2}:\d{2})（北京时间）/);
    assert.doesNotMatch(f.els.runLightTxt.textContent, /红\d/);
    f.els.snapshotData.textContent = JSON.stringify(f.data.published);
    await f.win.MMRunStatus.refresh(); assert.equal(f.els.ruleLight.dataset.phase, 'ok');
  });
  await check('行情缺失运行红灯，但同版规则仍绿灯', async () => {
    const page = snapshot(); page.summary.missing_symbols = ['NTNX', 'MCD'];
    const f = fixture(page); await f.win.MMRunStatus.refresh();
    assert.equal(f.els.runLight.dataset.phase, 'bad');
    assert.match(f.els.runLightTxt.textContent, /取数失败 2 只/);
    assert.equal(f.els.ruleLight.dataset.phase, 'ok');
  });
  await check('市场风险缺项保留提示，完整行情不再显示更新计数', async () => {
    const page = snapshot(); page.event = 'push'; page.market_risk = {missing: ['VIX'], valid_count: 4};
    const f = fixture(page); await f.win.MMRunStatus.refresh();
    assert.equal(f.els.runLight.dataset.phase, 'partial');
    assert.match(f.els.runLightTxt.textContent, /市场风险数据缺失：VIX（有效指标 4\/5）/);
    assert.doesNotMatch(f.els.runLightTxt.textContent, /已更新|或过期|抓取成功/);
    page.market_risk = {missing: [], valid_count: 5};
    const ok = fixture(page); await ok.win.MMRunStatus.refresh();
    assert.equal(ok.els.runLight.dataset.phase, 'ok');
  });
  await check('今日提示按指定文案显示；不支持报价项仍显示且不重复完整计数', async () => {
    const page = snapshot(); page.event = 'schedule';
    page.finished_at_bj = '2026-10-22T08:18:36+08:00';
    page.summary.total = 99;
    page.market_risk = {missing: ['垃圾债利差'], valid_count: 4};
    const f = fixture(page); await f.win.MMRunStatus.refresh();
    assert.equal(f.els.runLightTxt.textContent,
      '自动运行已完成 · 市场风险数据缺失：垃圾债利差（有效指标 4/5） · 完成于 2026-10-22 08:18（北京时间）');
    page.summary.unsupported_symbols = ['SPECIAL'];
    for (const risk of [page.market_risk, {missing: [], valid_count: 5}]) {
      page.market_risk = risk;
      const partial = fixture(page); await partial.win.MMRunStatus.refresh();
      assert.match(partial.els.runLightTxt.textContent, /1 个特殊代码暂不支持报价/);
      assert.doesNotMatch(partial.els.runLightTxt.textContent, /已更新/);
    }
  });
  await check('对应规则运行失败红灯，别误用行情绿灯', async () => {
    const f = fixture(); f.data.sha = C; f.data.run.status = 'completed'; f.data.run.conclusion = 'failure';
    await f.win.MMRunStatus.refresh(); assert.equal(f.els.ruleLight.dataset.phase, 'bad');
    assert.equal(f.els.ruleLightTxt.textContent, '更新失败');
    assert.equal(f.els.runLight.dataset.phase, 'ok');
  });
  await check('规则已生成、产物不一致继续黄灯', async () => {
    const f = fixture(); f.data.sha = C; f.data.status = snapshot(C, '2');
    f.data.run.status = 'completed'; f.data.run.conclusion = 'success';
    await f.win.MMRunStatus.refresh(); assert.equal(f.els.ruleLight.dataset.phase, 'busy');
    assert.equal(f.els.ruleLightTxt.textContent, '等待发布');
  });
  await check('无法读最新规则不误绿；运行灯保留当前页数据', async () => {
    const f = fixture(); await f.win.MMRunStatus.refresh();
    f.data.failRules = true; f.data.failPublic = true;
    await f.win.MMRunStatus.refresh(); assert.equal(f.els.ruleLight.dataset.phase, 'idle');
    assert.equal(f.els.ruleLightTxt.textContent, '暂无法核验');
    assert.equal(f.els.runLight.dataset.phase, 'ok');
  });
  await check('无关运行失败不影响已经应用的规则', async () => {
    const f = fixture(); f.data.run.status = 'completed'; f.data.run.conclusion = 'failure';
    await f.win.MMRunStatus.refresh(); assert.equal(f.els.ruleLight.dataset.phase, 'ok');
  });
  await check('只改清单分组也必须等待更新，不能借同版规则显示绿灯', async () => {
    const f = fixture(); await f.win.MMRunStatus.refresh();
    f.data.holdingsSHA = C;
    await f.win.MMRunStatus.watchConfig({file: 'holdings.json', blobSHA: C, commitSHA: D});
    assert.equal(f.els.ruleLight.dataset.phase, 'busy');
    assert.equal(f.els.ruleLightTxt.textContent, '正在更新');
    assert.equal(f.data.reloads.length, 0);
  });
  await check('清单分组更新全阶段自动核验，刷新当前页后才绿灯', async () => {
    const f=fixture();await f.win.MMRunStatus.refresh();f.data.holdingsSHA=C;
    f.data.run.status='queued';await f.win.MMRunStatus.refresh();
    assert.equal(f.els.ruleLightTxt.textContent,'等待更新');
    assert.equal(f.els.ruleLight.dataset.phase,'busy');
    f.data.run.status='in_progress';await f.win.MMRunStatus.refresh();
    assert.equal(f.els.ruleLightTxt.textContent,'正在更新');
    const fresh=snapshot(B,'2');fresh.config_files['holdings.json']=C;
    f.data.status=fresh;f.data.run.status='completed';f.data.run.conclusion='success';
    await f.win.MMRunStatus.refresh();assert.equal(f.els.ruleLightTxt.textContent,'等待发布');
    f.data.published=fresh;await f.win.MMRunStatus.refresh();
    assert.equal(f.els.ruleLightTxt.textContent,'需要刷新');
    assert.equal(f.els.ruleLight.dataset.phase,'busy');assert.equal(f.data.reloads.length,0);
    assert.equal(JSON.parse(f.els.snapshotData.textContent).config_files['holdings.json'],A);
    f.els.snapshotData.textContent=JSON.stringify(fresh);await f.win.MMRunStatus.refresh();
    assert.equal(f.els.ruleLightTxt.textContent,'已生效');assert.equal(f.els.ruleLight.dataset.phase,'ok');
    assert(f.urls.some(url=>url.includes('/commits?path=holdings.json')));
  });
  await check('无法定位运行先等待、对应失败红灯、取消仍等待', async () => {
    for(const [run,text,phase] of [[null,'等待更新','busy'],
      [{id:2,event:'push',head_sha:D,status:'completed',conclusion:'failure'},'更新失败','bad'],
      [{id:2,event:'push',head_sha:D,status:'completed',conclusion:'cancelled'},'等待更新','busy']]) {
      const f=fixture();f.data.holdingsSHA=C;f.data.run=run;await f.win.MMRunStatus.refresh();
      assert.equal(f.els.ruleLightTxt.textContent,text);assert.equal(f.els.ruleLight.dataset.phase,phase);
    }
  });
  await check('清单及规则同时变化必须验证同一提交包含全部配置', async () => {
    const f=fixture();f.data.holdingsSHA=C;f.data.sha=A;f.data.commitHoldingsSHA=A;
    f.data.run.status='completed';f.data.run.conclusion='failure';
    await f.win.MMRunStatus.refresh();assert.equal(f.els.ruleLightTxt.textContent,'等待更新');
    assert.equal(f.els.ruleLight.dataset.phase,'busy');
    f.data.commitHoldingsSHA=C;await f.win.MMRunStatus.refresh();
    assert.equal(f.els.ruleLightTxt.textContent,'更新失败');
  });
  await check('清单无法读取或对应任务查询报错必须灰灯，不用旧确认冒充实时', async () => {
    const f=fixture();await f.win.MMRunStatus.refresh();f.data.failHoldings=true;
    await f.win.MMRunStatus.refresh();assert.equal(f.els.ruleLightTxt.textContent,'暂无法核验');
    assert.equal(f.els.ruleLight.dataset.phase,'idle');
    const g=fixture();g.data.holdingsSHA=C;g.data.failRunLookup=true;
    await g.win.MMRunStatus.refresh();assert.equal(g.els.ruleLightTxt.textContent,'暂无法核验');
    assert.equal(g.els.ruleLight.dataset.phase,'idle');
  });
  await check('保存清单回执发布后的手动刷新只在点击时执行', async () => {
    const f=fixture();await f.win.MMRunStatus.refresh();f.data.holdingsSHA=C;
    await f.win.MMRunStatus.watchConfig({file:'holdings.json',blobSHA:C,commitSHA:D});
    const fresh=snapshot(B,'2');fresh.config_files['holdings.json']=C;
    fresh.source_sha=D;f.data.status=fresh;f.data.published=fresh;
    const result=await f.win.MMRunStatus.refresh();assert.equal(result.config.phase,'published');
    assert.equal(f.els.ruleLightTxt.textContent,'需要刷新');assert.equal(f.data.reloads.length,0);
    const link=f.els.runLightTxt.children.at(-1);assert.equal(link.textContent,'刷新当前看板');
    f.win.location.href='https://example.test/market-monitor/index.html?focus=pos&edit=1&keep=1&token=synthetic#s-test';
    link.onclick({preventDefault(){}});assert.equal(f.data.reloads.length,1);
    const refreshed=new URL(f.data.reloads[0]);
    assert.equal(refreshed.hash,'');assert.equal(refreshed.searchParams.has('focus'),false);
    assert.equal(refreshed.searchParams.has('edit'),false);assert.equal(refreshed.searchParams.get('keep'),'1');
    assert.equal(refreshed.searchParams.get('token'),'synthetic');
    assert.equal(new URL(f.data.reloads[0]).searchParams.get('mm_refreshed_run'),'2');
    assert.equal(new URL(f.data.reloads[0]).searchParams.get('_mm_reset_view'),'1');
  });
  await check('新保存回执在远端读数尚未更新时也立即取消绿灯', async () => {
    const f=fixture();await f.win.MMRunStatus.refresh();f.advance(1);
    const pending=f.win.MMRunStatus.watchConfig({file:'holdings.json',blobSHA:C,commitSHA:D});
    assert.equal(f.els.ruleLightTxt.textContent,'等待更新');assert.notEqual(f.els.ruleLight.dataset.phase,'ok');
    await pending;
  });
  await check('清单原文也按Git blob核验，不把原文未读到误认为已生效', async () => {
    const raw='{"分组":"持仓"}\r\n';const bytes=Buffer.from(raw);
    const sha=createHash('sha1').update(Buffer.from('blob '+bytes.length+'\0')).update(bytes).digest('hex');
    const page=snapshot();page.config_files['holdings.json']=sha;
    const f=fixture(page);f.data.status=page;f.data.published=page;
    f.data.failHoldings=true;f.data.holdingsRaw=raw;
    await f.win.MMRunStatus.refresh();assert.equal(f.els.ruleLightTxt.textContent,'已生效');
    assert.equal(f.els.ruleLight.dataset.phase,'ok');
  });
  await check('保存恢复相同blob仍核对本次回执生成证据，旧结果不误绿', async () => {
    const f=fixture();await f.win.MMRunStatus.refresh();f.advance(1);
    const commit='e'.repeat(40);f.data.run={id:2,event:'push',head_sha:commit,status:'in_progress'};
    await f.win.MMRunStatus.watchConfig({file:'holdings.json',blobSHA:A,commitSHA:commit});
    assert.notEqual(f.els.ruleLight.dataset.phase,'ok');assert.equal(f.data.reloads.length,0);
  });
  await check('配置轮询前两分钟8秒、之后20秒，不自动刷新或派发任务', async () => {
    const f=fixture();f.data.holdingsSHA=C;await f.win.MMRunStatus.refresh();
    assert.equal(f.timers.at(-1).ms,8000);f.advance(121000);
    await f.win.MMRunStatus.refresh();assert.equal(f.timers.at(-1).ms,20000);
    assert.equal(f.data.posts||0,0);assert.equal(f.data.reloads.length,0);
  });
  await check('已生效首页低频核对，保持打开也能发现另一页修改清单', async () => {
    const f=fixture();await f.win.MMRunStatus.refresh();assert.equal(f.timers.at(-1).ms,60000);
    f.data.holdingsSHA=C;await f.timers.at(-1).fn();await new Promise(resolve=>setImmediate(resolve));
    assert.equal(f.els.ruleLightTxt.textContent,'正在更新');assert.equal(f.timers.at(-1).ms,8000);
  });
  await check('后台暂停状态查询，回到前台复用核验且不刷新页面', async () => {
    const f=fixture();await f.win.MMRunStatus.refresh();const requests=f.urls.length;
    f.win.document.hidden=true;f.events.visibilitychange();assert.equal(f.urls.length,requests);
    const timers=f.timers.length;await f.win.MMRunStatus.refresh();
    assert.equal(f.timers.length-timers,f.urls.length-requests,'隐藏页面只有请求超时计时器，不排下一轮查询');
    f.data.holdingsSHA=C;f.win.document.hidden=false;f.events.visibilitychange();
    await new Promise(resolve=>setImmediate(resolve));
    assert.equal(f.els.ruleLightTxt.textContent,'正在更新');assert.equal(f.data.reloads.length,0);
  });
  await check('规则等待超过十分钟明确旧规则但不捏造运行失败', async () => {
    const f = fixture(); f.data.sha = C; await f.win.MMRunStatus.refresh(); f.advance(11 * 60000);
    await f.win.MMRunStatus.refresh(); assert.equal(f.els.ruleLight.dataset.phase, 'idle');
    assert.equal(f.els.ruleLightTxt.textContent, '暂无法核验');
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
    await f.win.MMRunStatus.refresh(); assert.equal(f.els.ruleLightTxt.textContent, '需要刷新');
    assert.equal(f.data.reloads.length, 0);
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
  await check('403权限不足是未启动；核对公开状态不重发且恢复数据灯', async () => {
    const f=fixture();f.win.MMRunStatus.init({getToken:()=> 'github_pat_test'});
    const rejected=await f.win.MMRunStatus.startManual();
    assert.equal(rejected.manual.phase,'failed');
    assert.match(f.els.runLightTxt.textContent,/未启动.*此令牌无权/);
    assert.doesNotMatch(f.els.runLightTxt.textContent,/运行失败/);
    await f.els.btnCheckStatus.onclick();
    assert.equal(f.data.posts,1);
    assert.equal(f.els.runLight.dataset.phase,'ok');
    assert.match(f.els.checkResult.textContent,/当前发布：run 1/);
    assert.match(f.els.checkResult.textContent,/最新任务：未启动（手动）/);
    assert.equal(f.els.checkResult.hidden,false);
  });
  await check('限流响应尊重冷却时间，查状态仍读公开页面且不反复请求API', async () => {
    const f=fixture();f.data.dispatchError={status:403,message:'API rate limit exceeded',headers:{'x-ratelimit-remaining':'0','retry-after':'120'}};
    f.win.MMRunStatus.init({getToken:()=> 'github_pat_test'});
    await f.win.MMRunStatus.startManual();
    assert.match(f.els.runLightTxt.textContent,/限流，不代表令牌过期/);
    await f.els.btnCheckStatus.onclick();
    const before=f.urls.length;
    await f.win.MMRunStatus.startManual();
    assert.equal(f.data.posts,1);
    assert.equal(f.urls.length,before);
    f.advance(121000);await f.win.MMRunStatus.startManual();assert.equal(f.data.posts,2);
  });
  await check('401与未知403不能被错误归为限流或确认过期', async () => {
    for(const [status,message,expected] of [[401,'Bad credentials',/无效、被撤销或过期/],
      [403,'Forbidden',/尚不能确定/],[429,'Too many requests',/限流/]]) {
      const f=fixture();f.data.dispatchError={status,message};f.win.MMRunStatus.init({getToken:()=> 'github_pat_test'});
      await f.win.MMRunStatus.startManual();assert.match(f.els.runLightTxt.textContent,expected);
    }
  });
  await check('公开状态读取失败必须显示核对未完成，不是假成功', async () => {
    const f=fixture();await f.win.MMRunStatus.refresh();f.data.failPublic=true;
    await f.els.btnCheckStatus.onclick();assert.equal(f.els.btnCheckStatus.textContent,'状态');
    assert.match(f.els.checkResult.textContent,/当前发布：暂无法核验/);
    assert.equal(f.els.checkResult.hidden,false);
  });
  await check('核对显示最新任务运行中，而不是把旧版已发布误称新任务完成', async () => {
    const f=fixture();await f.els.btnCheckStatus.onclick();
    assert.match(f.els.checkResult.textContent,/最新任务：运行中 · run 2/);
    assert.match(f.els.checkResult.textContent,/当前发布：run 1/);
    assert.equal(f.els.checkResult.dataset.phase,'busy');
    assert.equal(f.els.btnCheckStatus.textContent,'状态');
  });
  await check('完成且与线上同版时显示完成；旧页有明确刷新入口', async () => {
    const f=fixture();f.data.run={id:1,status:'completed',conclusion:'success'};
    await f.els.btnCheckStatus.onclick();assert.match(f.els.checkResult.textContent,/最新任务：已完成并发布/);
    assert.equal(f.els.checkResult.dataset.phase,'ok');
    f.data.status=snapshot(B,'3');f.data.published=snapshot(B,'3');
    await f.els.btnCheckStatus.onclick();assert.match(f.els.checkResult.textContent,/已有新版，请刷新当前看板/);
    assert.equal(f.els.checkResult.children.at(-1).textContent,'刷新当前看板');
    f.els.checkResult.children.at(-1).onclick({preventDefault(){}});
    assert.equal(new URL(f.data.reloads.at(-1)).searchParams.get('mm_refreshed_run'),'3');
  });
  await check('成功但当日日线未就绪：写明未发布与重试，并带北京时间', async () => {
    const f=fixture();
    f.data.run={id:2,status:'completed',conclusion:'success',run_started_at:'2026-10-21T00:46:21Z',created_at:'2026-10-21T00:46:21Z'};
    f.data.jobs=[{steps:[{name:'抓数据并生成页面',conclusion:'success'},{name:'当日日线未就绪：约 30 分钟后自动再试',conclusion:'success'},
      {name:'校验最新输入并提交更新',conclusion:'skipped'}]}];
    await f.els.btnCheckStatus.onclick();
    assert.match(f.els.checkResult.textContent,/最新任务：本轮未发布 · run 2（10\/21 08:46 北京时间）/);
    assert.match(f.els.checkResult.textContent,/当日日线未就绪，保留上一版。约 30 分钟后自动再试/);
    assert.equal(f.els.checkResult.dataset.phase,'busy');
    f.data.run.run_started_at='2026-10-21T03:15:21Z'; // 美东23:15仍在重试窗口
    await f.els.btnCheckStatus.onclick();
    assert.match(f.els.checkResult.textContent,/约 30 分钟后自动再试/);
    f.data.run.run_started_at='2026-10-21T04:15:21Z'; // 美东零点后不再承诺重试
    await f.els.btnCheckStatus.onclick();
    assert.doesNotMatch(f.els.checkResult.textContent,/约 30 分钟后自动再试/);
    f.data.run.run_started_at='2026-10-21T00:46:21Z';
    f.data.jobs=[{steps:[{name:'抓数据并生成页面',conclusion:'success'},{name:'校验最新输入并提交更新',conclusion:'success'}]}];
    await f.els.btnCheckStatus.onclick();
    assert.match(f.els.checkResult.textContent,/最新任务：等待发布 · run 2（10\/21 08:46 北京时间）/);
  });
  await check('最新任务失败和排队各有清楚结果，不影响已发布数据', async () => {
    for(const [status,conclusion,label] of [['queued',null,'排队中'],['completed','failure','失败']]){
      const f=fixture();f.data.run={id:2,status,conclusion};await f.els.btnCheckStatus.onclick();
      assert.match(f.els.checkResult.textContent,new RegExp('最新任务：'+label));
      assert.match(f.els.checkResult.textContent,/当前发布：run 1/);
    }
  });
  await check('状态详情统一四段且去重，运行排队发布失败取消均保留真实语义', async () => {
    for (const [status, conclusion, label, action, phase] of [
      ['queued', null, '排队中', '新任务尚未发布，暂不需要刷新。', 'busy'],
      ['in_progress', null, '运行中', '新任务尚未发布，暂不需要刷新。', 'busy'],
      ['completed', 'success', '等待发布', '生成完成，等待发布；暂不需要刷新。', 'busy'],
      ['completed', 'failure', '失败（failure）', '本轮未发布，继续使用上一版；请查看运行记录。', 'bad'],
      ['completed', 'cancelled', '已取消', '不是抓取失败；继续使用上一版。', 'busy']
    ]) {
      const page=snapshot();page.finished_at='2026-10-08T01:05:06Z';
      const f=fixture(page);f.data.status=clone(page);f.data.published=clone(page);
      f.data.run={id:2,status,conclusion,run_started_at:'2026-10-08T02:00:00Z'};
      await f.els.btnCheckStatus.onclick();
      assert.deepEqual(f.els.checkResult.textContent.split('\n'),[
        '最新任务：'+label+' · run 2（10/08 10:00 北京时间）',
        '当前发布：run 1，当前页面与线上版本一致。',
        '数据更新时间：2026-10-08 09:05（北京时间）',action]);
      assert.equal(f.els.checkResult.dataset.phase,phase);
      assert.doesNotMatch(f.els.checkResult.textContent,/看板与状态文件一致|暂无页面更新|blob SHA|request_id/);
      assert.equal(f.data.reloads.length,0);assert.equal(f.data.posts||0,0);
    }
  });
  await check('同版已发布只需三行，编辑页不伪称当前看板已刷新', async () => {
    for(const page of [snapshot(),null]) {
      const f=fixture(page);f.data.run={id:1,status:'completed',conclusion:'success'};
      await f.els.btnCheckStatus.onclick();
      const text=f.els.checkResult.textContent;
      assert.equal(text.split('\n').length,3);
      assert.match(text,/最新任务：已完成并发布/);
      assert.match(text,page?/当前页面与线上版本一致/:/当前发布：run 1，已核验/);
      assert.doesNotMatch(text,/暂无页面更新|暂不需要刷新|看板与状态文件一致/);
      if(!page)assert.doesNotMatch(text,/当前页面与线上版本一致/);
    }
  });
  await check('已有新版时刷新提示优先，其他任务运行排队生成不遮蔽', async () => {
    for(const [status,conclusion] of [['queued',null],['in_progress',null],['completed','success']]) {
      const f=fixture();f.data.status=snapshot(B,'3');f.data.published=snapshot(B,'3');
      f.data.run={id:4,status,conclusion};await f.els.btnCheckStatus.onclick();
      assert.match(f.els.checkResult.textContent,/当前发布：run 3。/);
      assert.match(f.els.checkResult.textContent,/已有新版，请刷新当前看板/);
      assert.doesNotMatch(f.els.checkResult.textContent,/暂不需要刷新|暂无页面更新|与线上版本一致|发布已有新版/);
      assert.equal(f.els.checkResult.children.at(-1).textContent,'刷新当前看板');
      assert.equal(f.data.reloads.length,0);
    }
  });
  await check('发布不同步或网络失败不宣称无需刷新，缺任务记录也不猜状态', async () => {
    for(const mode of ['network','mismatch','missing','invalid']) {
      const f=fixture();
      if(mode==='network')f.data.failPublic=true;
      if(mode==='mismatch')f.data.status=snapshot(B,'2');
      if(mode==='missing')f.data.run=null;
      if(mode==='invalid')f.data.published={run_id:'bad'};
      await f.els.btnCheckStatus.onclick();
      const text=f.els.checkResult.textContent;
      if(mode==='missing')assert.match(text,/最新任务：未找到记录/);
      else {
        assert.match(text,/当前发布：暂无法核验/);
        assert.doesNotMatch(text,/当前页面与线上版本一致|暂不需要刷新|已有新版，请刷新/);
      }
      assert.equal(f.data.reloads.length,0);
    }
  });
  await check('手动与保存回执各阶段沿用统一简短详情，不混入其他最新任务', async () => {
    for(const origin of ['手动','保存后']) {
      for(const [status,conclusion,label] of [
        ['queued',null,'排队中'],['in_progress',null,'运行中'],
        ['completed','success','等待发布'],['completed','failure','失败'],
        ['completed','cancelled','已取消'],['expired',null,'跟踪超时'],['published','success','已完成并发布']
      ]) {
        const f=fixture();
        if(origin==='手动') {
          f.data.dispatchSuccess=true;f.win.MMRunStatus.init({getToken:()=> 'github_pat_test'});
          await f.win.MMRunStatus.startManual();
          f.data.run={id:2,event:'workflow_dispatch',display_title:f.data.requestId,status,conclusion};
        } else {
          f.data.sha=C;await f.win.MMRunStatus.watchConfig({file:'settings.json',blobSHA:C,commitSHA:D});
          f.data.run={id:2,event:'push',head_sha:D,status,conclusion};
        }
        if(status==='expired') {f.data.run.status='in_progress';f.advance(601000);}
        if(status==='published') {
          f.data.run.status='completed';const fresh=snapshot(origin==='手动'?B:C,'2');
          fresh.request_id=origin==='手动'?f.data.requestId:'';
          f.data.status=fresh;f.data.published=fresh;
        }
        await f.els.btnCheckStatus.onclick();const text=f.els.checkResult.textContent;
        assert(text.startsWith('最新任务：'+label+'（'+origin+'） · run 2'),text);
        assert.doesNotMatch(text,/你的运行：|保存后的运行：|blob SHA|request_id、|暂无页面更新/);
        if(status==='expired')assert.match(text,/尚不能确认运行结果/);
        if(conclusion==='cancelled')assert.notEqual(f.els.checkResult.dataset.phase,'bad');
        assert.equal(f.data.posts||0,origin==='手动'?1:0);assert.equal(f.data.reloads.length,0);
      }
    }
  });
  await check('手动请求未定位时不填造run号，拒绝原因及仅查询边界保持', async () => {
    const f=fixture();f.data.run=null;f.data.dispatchSuccess=true;
    f.win.MMRunStatus.init({getToken:()=> 'github_pat_test'});await f.win.MMRunStatus.startManual();
    await f.els.btnCheckStatus.onclick();
    assert.match(f.els.checkResult.textContent,/最新任务：等待定位（手动）/);
    assert.doesNotMatch(f.els.checkResult.textContent.split('\n')[0],/run/);
    assert.equal(f.data.posts,1);
    const g=fixture();g.win.MMRunStatus.init({getToken:()=> 'github_pat_test'});
    await g.win.MMRunStatus.startManual();await g.els.btnCheckStatus.onclick();
    assert.match(g.els.checkResult.textContent,/原因：403：此令牌无权/);
    assert.match(g.els.checkResult.textContent,/查状态不会重新派发/);
    assert.equal(g.data.posts,1);
  });
  await check('完成时间显示北京时间到分钟，开始时间不能冒充完成时间', async () => {
    const page=snapshot();page.finished_at='2026-10-08T01:05:06Z';
    const f=fixture(page);await f.win.MMRunStatus.refresh();
    assert.match(f.els.runLightTxt.textContent,/成功 ·2026-10-08 09:05（北京时间）/);
    delete page.finished_at;f.els.snapshotData.textContent=JSON.stringify(page);
    await f.win.MMRunStatus.refresh();assert.match(f.els.runLightTxt.textContent,/成功 ·未记录（北京时间）/);
    assert.doesNotMatch(f.els.runLightTxt.textContent,/09:05:06/);
  });
  await check('本次手动运行发布后显示新完成时间，但不伪装旧表格已经刷新', async () => {
    const page=snapshot();page.finished_at='2026-10-08T01:05:06Z';
    const f=fixture(page);f.data.dispatchSuccess=true;
    f.win.MMRunStatus.init({getToken:()=> 'github_pat_test'});
    await f.win.MMRunStatus.startManual();
    const fresh=snapshot(B,'2');fresh.request_id=f.data.requestId;
    fresh.finished_at_bj='2026-10-08T10:11:12+08:00';fresh.summary.red=1;
    f.data.status=fresh;f.data.published=fresh;
    f.data.run={id:2,event:'workflow_dispatch',display_title:f.data.requestId,status:'completed',conclusion:'success'};
    const result=await f.win.MMRunStatus.refresh();
    assert.equal(result.manual.phase,'published');
    assert.match(f.els.runLightTxt.textContent,/成功 ·2026-10-08 10:11（北京时间）/);
    assert.doesNotMatch(f.els.runLightTxt.textContent,/红\d/);
    assert.match(f.els.runLightTxt.textContent,/本页仍是旧数据/);
    assert.equal(f.els.runLightTxt.children.at(-1).textContent,'刷新当前看板');
    assert.equal(JSON.parse(f.els.snapshotData.textContent).run_id,'1');
  });
  await check('配置发布后不自动刷新，缓存旧页同样提示需要刷新', async () => {
    const f=fixture();f.data.sha=C;f.data.status=snapshot(C,'2');f.data.published=snapshot(C,'2');
    await f.win.MMRunStatus.refresh();await f.win.MMRunStatus.refresh();
    assert.equal(f.data.reloads.length,0);
    assert.equal(f.els.ruleLightTxt.textContent,'需要刷新');
    const cached=fixture();cached.win.location.href='https://example.test/market-monitor/index.html?mm_refreshed_run=2';
    cached.data.sha=C;cached.data.status=snapshot(C,'2');cached.data.published=snapshot(C,'2');
    await cached.win.MMRunStatus.refresh();assert.equal(cached.data.reloads.length,0);
  });
  await check('未保存编辑及保存完成均不会被轮询自动刷新', async () => {
    const f=fixture();let dirty=true;
    f.win.MMRunStatus.init({canReload:()=>!dirty});
    f.data.sha=C;f.data.status=snapshot(C,'2');f.data.published=snapshot(C,'2');
    await f.win.MMRunStatus.refresh();assert.equal(f.data.reloads.length,0);
    assert.equal(f.els.ruleLightTxt.textContent,'需要刷新');
    dirty=false;await f.win.MMRunStatus.refresh();assert.equal(f.data.reloads.length,0);
    assert.equal(f.els.ruleLightTxt.textContent,'需要刷新');
  });
  await check('产物未发布或已被更新的规则不能触发刷新', async () => {
    const f=fixture();f.data.sha=C;f.data.status=snapshot(C,'2');
    await f.win.MMRunStatus.refresh();assert.equal(f.data.reloads.length,0);
    f.data.sha=A;f.data.published=snapshot(C,'2');
    await f.win.MMRunStatus.refresh();assert.equal(f.data.reloads.length,0);
  });
  await check('设置页保存回执验证发布后留在本页，不自动刷新编辑器', async () => {
    const f=fixture(null,true);f.win.location.href='https://example.test/market-monitor/edit.html';
    f.data.sha=C;
    await f.win.MMRunStatus.watchConfig({file:'settings.json',blobSHA:C,commitSHA:D});
    assert.equal(f.data.reloads.length,0);
    f.data.status=snapshot(C,'2');f.data.published=snapshot(C,'2');
    const result=await f.win.MMRunStatus.refresh();assert.equal(f.data.reloads.length,0);
    assert.equal(result.config.phase,'published');
  });
  await check('自动、手动、保存后完成标签仅依据实际事件', async () => {
    // 成功灯已精简为「成功 ·时间」（2026-10-27），事件标签只在「…运行已完成」这条里出现，
    // 所以用市场风险缺项那条分支来核对 kind 仍然只认真实事件。
    for(const [event,label] of [['schedule','自动'],['workflow_dispatch','手动'],['push','保存后']]){
      const page=snapshot();page.event=event;page.market_risk={missing:['VIX'],valid_count:4};
      const f=fixture(page);await f.win.MMRunStatus.refresh();
      assert.equal(f.els.runLight.dataset.phase,'partial');
      assert.match(f.els.runLightTxt.textContent,new RegExp('^'+label+'运行已完成'));
    }
  });
  await check('核对结果可关闭，轮询不会重现；下次点击仍可重新查看', async () => {
    const f=fixture();
    await f.els.btnCheckStatus.onclick();
    assert.equal(f.els.checkResult.hidden,false);
    const close=f.els.checkResult.children.find(c=>c.textContent==='关闭');
    assert.equal(close.type,'button');
    assert.equal(close['aria-label'],'关闭运行状态核对结果');
    close.onclick();
    assert.equal(f.els.checkResult.hidden,true);
    await f.win.MMRunStatus.refresh();
    assert.equal(f.els.checkResult.hidden,true);
    await f.els.btnCheckStatus.onclick();
    assert.equal(f.els.checkResult.hidden,false);
    assert.match(f.els.checkResult.textContent,/当前发布：/);
  });
  await check('查询尚未结束时关闭也不会在结果返回后重新出现', async () => {
    const f=fixture();
    let resolve;
    f.data.delay=new Promise(r=>{resolve=r;});
    const pending=f.els.btnCheckStatus.onclick();
    const close=f.els.checkResult.children.find(c=>c.textContent==='关闭');
    assert(close);
    close.onclick();
    resolve({ok:true,status:200,json:async()=>({sha:B})});
    await pending;
    assert.equal(f.els.checkResult.hidden,true);
  });
  await check('旧的市场参考日期不把已经更新的当日日线判为红灯', async () => {
    const page=snapshot();page.summary={red:0,yellow:0,green:2,total:2,gray:0,
      stale_symbols:[],missing_symbols:[],unsupported_symbols:[],
      reference_dates:{'BD#US10Y':'2026-10-06'},today_prices:1,prior_prices:0,missing_prices:0};
    const f=fixture(page);await f.win.MMRunStatus.refresh();
    assert.equal(f.els.runLight.dataset.phase,'ok');
    assert.match(f.els.runLightTxt.textContent,/^成功 ·(?:未记录|\d{4}-\d{2}-\d{2} \d{2}:\d{2})（北京时间）/);
    assert.doesNotMatch(f.els.runLightTxt.textContent,/项数据已更新|当日行情未取得|项参考值|只日线/);
  });
  await check('取消的运行不叫失败，且说明看板仍是上次成功数据', async () => {
    const f=fixture();f.data.run={id:9,event:'workflow_dispatch',head_sha:D,status:'completed',conclusion:'cancelled'};
    await f.els.btnCheckStatus.onclick();
    const text=f.els.checkResult.textContent;
    assert.match(text,/已取消/);
    assert.match(text,/不是抓取失败/);
    assert.doesNotMatch(text,/失败（cancelled）/);
    assert.notEqual(f.els.checkResult.dataset.phase,'bad');
    const g=fixture();g.data.run={id:10,event:'workflow_dispatch',head_sha:D,status:'completed',conclusion:'failure'};
    await g.els.btnCheckStatus.onclick();
    assert.match(g.els.checkResult.textContent,/失败（failure）/);
  });
  await check('43项总数包含42项日线和1项正常收益率，不再显示参考值提示', async () => {
    const page=snapshot();page.summary={red:3,yellow:14,green:26,total:43,gray:0,
      stale_symbols:[],missing_symbols:[],unsupported_symbols:[],
      reference_dates:{'BD#US10Y':'2026-10-07'},today_prices:42,prior_prices:0,missing_prices:0};
    const f=fixture(page);await f.win.MMRunStatus.refresh();
    assert.match(f.els.runLightTxt.textContent,/^成功 ·(?:未记录|\d{4}-\d{2}-\d{2} \d{2}:\d{2})（北京时间）/);
    assert.doesNotMatch(f.els.runLightTxt.textContent,/红3 黄14 绿26|项数据已更新/);
    assert.doesNotMatch(f.els.runLightTxt.textContent,/项参考值|42 只/);
  });
  console.log('全部通过');
})().catch(error => { console.error(error); process.exitCode = 1; });
