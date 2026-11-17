// 验证「首页点立即运行没反应」的修复：首页只有 btnRunNow / btnCheckStatus / runLightTxt
const fs = require('fs');
const path = require('path');
const ROOT = path.resolve(__dirname, '..');
let src = fs.readFileSync(path.join(ROOT, 'run-status.js'), 'utf8');

const els = {};
function makeEl(id) {
  return {
    id, textContent: '', innerHTML: '', disabled: false, value: '', style: {}, dataset: {},
    setAttribute(k, v) { this['attr_' + k] = v; },
    appendChild() {}, classList: {add() {}, remove() {}, contains() {return false;}},
    addEventListener() {}, querySelector() {return null;}, querySelectorAll() {return [];},
    insertAdjacentHTML() {}, focus() {},
  };
}
// 首页实际存在的元素（运行详情/摘要卡已删除）
['btnRunNow', 'btnCheckStatus', 'runLight', 'runLightTxt', 'ruleLight', 'ruleLightTxt', 'snapshotData'].forEach(id => els[id] = makeEl(id));

let store = {};
let prompts = [];
const win = {
  localStorage: {
    getItem: k => (k in store ? store[k] : null),
    setItem: (k, v) => { store[k] = String(v); },
    removeItem: k => { delete store[k]; },
  },
  crypto: {randomUUID: () => 'uuid-test-0001', getRandomValues: a => a},
  location: {hash: '', href: 'https://nixhuang.github.io/market-monitor/index.html'},
  prompt: (msg) => { prompts.push(msg); return prompts.returnValue; },
  fetch: () => Promise.reject(new Error('offline')),
  setTimeout: () => 0, clearTimeout: () => {},
  AbortController: class {constructor(){this.signal={addEventListener(){},aborted:false};}abort(){}},
  encodeURIComponent, decodeURIComponent, JSON, Math, Date, isNaN, parseInt, parseFloat,
  Error, Promise, Object, Array, String, Number, Boolean, RegExp, Set, Map,
  console,
  document: {
    readyState: 'complete',
    getElementById: id => els[id] || null,
    addEventListener() {}, createElement: () => makeEl('tmp'),
    querySelector: () => null, querySelectorAll: () => [],
  },
};
win.window = win;
win.self = win;

const vm = require('vm');
const ctx = vm.createContext(win);
vm.runInContext(src, ctx);

let fails = 0;
const check = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) fails++; };

// 1) 首页（无 runState/runMsg/runSummary）必须完成 init —— 这是「点了没反应」的根因
check(typeof win.MMRunStatus === 'object', '首页元素组合下 MMRunStatus 已导出（说明 init 跑过）');
check(typeof els.btnRunNow.onclick === 'function', 'btnRunNow 已绑定点击（修复前是 undefined）');
check(typeof els.btnCheckStatus.onclick === 'function', '首页 btnCheckStatus 已绑定点击');

// 2) 无令牌点运行：应弹一次 prompt。2026-11-17 定：抬头灯只报「运行未启动」，
//    整段令牌诊断归「状态」查询（mm_test_rs.js 有真实例覆盖），否则手机上一行会折行。
prompts.returnValue = null;
els.btnRunNow.onclick();
check(prompts.length === 1, '无令牌时弹了一次令牌输入框');
check(els.runLightTxt.textContent === '运行未启动',
      '取消后灯只报「运行未启动」：' + JSON.stringify(els.runLightTxt.textContent));
check(els.runLight.dataset.phase === 'bad', '灯相位为 bad（红色）');
check(/没有令牌，无法立即运行/.test(src), '整段未启动诊断仍在真源里（详情由状态查询承接）');

// 3) 粘贴了假令牌：同样只报状态，诊断不丢
prompts.returnValue = 'abcdef123';
els.btnRunNow.onclick();
check(els.runLightTxt.textContent === '运行未启动', '假令牌被拒绝，灯仍只报「运行未启动」');
check(/不像 GitHub 令牌/.test(src), '假令牌诊断仍在真源里（详情由状态查询承接）');
check(!('mm_gh_token_v1' in store), '假令牌没有写入 localStorage');

// 4) 粘贴真令牌：存入并放行（会走到派发，这里 fetch 拒绝 → 不报错即可）
prompts.returnValue = 'github_pat_testAAA';
let threw = false;
try { els.btnRunNow.onclick(); } catch (e) { threw = true; }
check(!threw, '真令牌路径不抛异常');
check(store['mm_gh_token_v1'] === 'github_pat_testAAA', '真令牌已存入 localStorage');
check(!/没有令牌|不像/.test(els.runLightTxt.textContent || '') &&
      els.runLightTxt.textContent !== '运行未启动', '真令牌后灯上不再显示令牌错误');

console.log(fails ? `\n${fails} 项失败` : '\n全部通过');
process.exit(fails ? 1 : 0);
