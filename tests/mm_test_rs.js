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
    ['runLight', 'runLightTxt', 'ruleLight', 'ruleLightTxt', 'runSummary', 'runMsg', 'checkResult', 'btnCheckStatus']).map(id => [id, element()]));
  if (page) { els.snapshotData = element(); els.snapshotData.textContent = JSON.stringify(page); }
  const data = {status: snapshot(), published: snapshot(), sha: B, failRules: false, failPublic: false,
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
    assert.equal(f.data.reloads.length, 1);
    assert.equal(new URL(f.data.reloads[0]).pathname, '/market-monitor/index.html');
    assert.equal(new URL(f.data.reloads[0]).searchParams.get('mm_refreshed_run'), '2');
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
    assert.equal(f.data.reloads.length, 1);
    assert.equal(new URL(f.data.reloads[0]).pathname, '/market-monitor/index.html');
    assert.equal(new URL(f.data.reloads[0]).searchParams.get('mm_refreshed_run'), '2');
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
    assert.match(f.els.checkResult.textContent,/已核对线上 run 1/);
    assert.match(f.els.checkResult.textContent,/上次立即运行未启动/);
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
    await f.els.btnCheckStatus.onclick();assert.equal(f.els.btnCheckStatus.textContent,'查运行状态');
    assert.match(f.els.checkResult.textContent,/核对尚未完成/);
    assert.equal(f.els.checkResult.hidden,false);
  });
  await check('核对显示最新任务运行中，而不是把旧版已发布误称新任务完成', async () => {
    const f=fixture();await f.els.btnCheckStatus.onclick();
    assert.match(f.els.checkResult.textContent,/最新任务：运行中 · run 2/);
    assert.match(f.els.checkResult.textContent,/已核对线上 run 1/);
    assert.equal(f.els.checkResult.dataset.phase,'busy');
    assert.equal(f.els.btnCheckStatus.textContent,'查运行状态');
  });
  await check('完成且与线上同版时显示完成；旧页有明确刷新入口', async () => {
    const f=fixture();f.data.run={id:1,status:'completed',conclusion:'success'};
    await f.els.btnCheckStatus.onclick();assert.match(f.els.checkResult.textContent,/最新任务：已完成并发布/);
    assert.equal(f.els.checkResult.dataset.phase,'ok');
    f.data.status=snapshot(B,'3');f.data.published=snapshot(B,'3');
    await f.els.btnCheckStatus.onclick();assert.match(f.els.checkResult.textContent,/当前页面仍是旧数据/);
    assert.equal(f.els.checkResult.children.at(-1).textContent,'刷新当前看板');
    f.els.checkResult.children.at(-1).onclick({preventDefault(){}});
    assert.equal(new URL(f.data.reloads.at(-1)).searchParams.get('mm_refreshed_run'),'3');
  });
  await check('最新任务失败和排队各有清楚结果，不影响已发布数据', async () => {
    for(const [status,conclusion,label] of [['queued',null,'排队中'],['completed','failure','失败']]){
      const f=fixture();f.data.run={id:2,status,conclusion};await f.els.btnCheckStatus.onclick();
      assert.match(f.els.checkResult.textContent,new RegExp('最新任务：'+label));
      assert.match(f.els.checkResult.textContent,/已核对线上 run 1/);
    }
  });
  await check('完成时间显示北京时间时分秒，开始时间不能冒充完成时间', async () => {
    const page=snapshot();page.finished_at='2026-10-08T01:05:06Z';
    const f=fixture(page);await f.win.MMRunStatus.refresh();
    assert.match(f.els.runLightTxt.textContent,/完成于 2026-10-08 09:05:06（北京时间）/);
    delete page.finished_at;f.els.snapshotData.textContent=JSON.stringify(page);
    await f.win.MMRunStatus.refresh();assert.match(f.els.runLightTxt.textContent,/完成于 未记录/);
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
    assert.match(f.els.runLightTxt.textContent,/完成于 2026-10-08 10:11:12/);
    assert.match(f.els.runLightTxt.textContent,/红1/);
    assert.match(f.els.runLightTxt.textContent,/本页仍是旧数据/);
    assert.equal(f.els.runLightTxt.children.at(-1).textContent,'刷新当前看板');
    assert.equal(JSON.parse(f.els.snapshotData.textContent).run_id,'1');
  });
  await check('规则发布后同页只刷新一次，缓存旧页不循环刷新', async () => {
    const f=fixture();f.data.sha=C;f.data.status=snapshot(C,'2');f.data.published=snapshot(C,'2');
    await f.win.MMRunStatus.refresh();await f.win.MMRunStatus.refresh();
    assert.equal(f.data.reloads.length,1);
    const cached=fixture();cached.win.location.href='https://example.test/market-monitor/index.html?mm_refreshed_run=2';
    cached.data.sha=C;cached.data.status=snapshot(C,'2');cached.data.published=snapshot(C,'2');
    await cached.win.MMRunStatus.refresh();assert.equal(cached.data.reloads.length,0);
  });
  await check('未保存编辑阻止刷新，保存完成后才自动刷新', async () => {
    const f=fixture();let dirty=true;
    f.win.MMRunStatus.init({canReload:()=>!dirty});
    f.data.sha=C;f.data.status=snapshot(C,'2');f.data.published=snapshot(C,'2');
    await f.win.MMRunStatus.refresh();assert.equal(f.data.reloads.length,0);
    assert.match(f.els.ruleLightTxt.textContent,/有未保存编辑/);
    dirty=false;await f.win.MMRunStatus.refresh();assert.equal(f.data.reloads.length,1);
  });
  await check('产物未发布或已被更新的规则不能触发刷新', async () => {
    const f=fixture();f.data.sha=C;f.data.status=snapshot(C,'2');
    await f.win.MMRunStatus.refresh();assert.equal(f.data.reloads.length,0);
    f.data.sha=A;f.data.published=snapshot(C,'2');
    await f.win.MMRunStatus.refresh();assert.equal(f.data.reloads.length,0);
  });
  await check('设置页的保存回执验证发布后仍停留本页刷新', async () => {
    const f=fixture(null,true);f.win.location.href='https://example.test/market-monitor/edit.html';
    f.data.sha=C;
    await f.win.MMRunStatus.watchConfig({file:'settings.json',blobSHA:C,commitSHA:D});
    assert.equal(f.data.reloads.length,0);
    f.data.status=snapshot(C,'2');f.data.published=snapshot(C,'2');
    await f.win.MMRunStatus.refresh();assert.equal(f.data.reloads.length,1);
    assert.equal(new URL(f.data.reloads[0]).pathname,'/market-monitor/edit.html');
  });
  await check('自动、手动、保存后完成标签仅依据实际事件', async () => {
    for(const [event,label] of [['schedule','自动'],['workflow_dispatch','手动'],['push','保存后']]){
      const page=snapshot();page.event=event;
      const f=fixture(page);await f.win.MMRunStatus.refresh();
      assert.match(f.els.runLightTxt.textContent,new RegExp('^'+label+'抓取成功'));
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
    assert.match(f.els.checkResult.textContent,/已核对线上/);
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
  console.log('全部通过');
})().catch(error => { console.error(error); process.exitCode = 1; });
