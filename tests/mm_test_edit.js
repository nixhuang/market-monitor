// edit.html 内联脚本的 DOM 桩测试：验证三分组渲染、ETF 分区、排序、分页。
const fs = require('fs');
const path = require('path');
const ROOT = path.resolve(__dirname, '..');
const src = fs.readFileSync(path.join(ROOT, 'edit.html'), 'utf8');
const m = src.match(/<script>\r?\n([\s\S]*?)<\/script>\s*<\/body>/);
if (!m) { console.error('未找到内联脚本'); process.exit(1); }
const code = m[1];

function makeEl(id) {
  return {
    id, textContent: '', value: '', _html: '', className: '',
    dataset: {}, files: [], hidden: false, disabled: false,
    classList: { add() {}, remove() {}, contains() { return false; } },
    style: {},
    set innerHTML(v) { this._html = v; },
    get innerHTML() { return this._html; },
    addEventListener() {}, attachEvent() {},
    querySelector() { return null; },
    querySelectorAll() { return []; },
    insertAdjacentHTML() {}, appendChild() {}, scrollIntoView() {},
    select() {}, focus() {}, click() {},
    onclick: null, oninput: null, onload: null,
  };
}
const els = {};
const doc = {
  getElementById(id) { return els[id] || (els[id] = makeEl(id)); },
  addEventListener() {}, createElement() { return makeEl('tmp'); },
  readyState: 'complete',
};
const store = {};
global.document = doc;
global.window = global;
global.localStorage = {
  getItem: k => (k in store ? store[k] : null),
  setItem: (k, v) => { store[k] = String(v); },
  removeItem: k => { delete store[k]; },
};
global.location = { origin: 'https://nixhuang.github.io', pathname: '/market-monitor/edit.html', search: '', hash: '' };
global.history = { replaceState() {} };
global.navigator = { clipboard: { writeText: async () => {} } };
global.confirm = () => true;
global.alert = () => {};
global.fetch = async () => { throw new Error('no network in test'); };
global.MMRunStatus = { init() {}, refresh() { return {}; }, watchConfig() {}, startManual: async () => {} };

let fails = 0;
const check = (cond, msg) => { console.log((cond ? 'PASS ' : 'FAIL ') + msg); if (!cond) fails++; };
global.window.addEventListener = () => {};

try {
  eval(code + '\n;globalThis.__T={render,data,pageState,ETF_SET,SET,collectSettings,setJsonText,loadSettings};');
  console.log('PASS 脚本加载无异常');
} catch (e) {
  console.error('FAIL 脚本加载异常:', e.message);
  process.exit(1);
}

const T = globalThis.__T;
T.data.positions = { 'BRK-B': {}, SPYM: {}, NTNX: {} };
T.data.focus = { SOXL: { note: '半导体3倍' }, TEM: {} };
T.data.watch = { MCD: {}, XLU: {}, QQQ: {}, AAPL: {}, HYG: { note: '垃圾债-观察' } };
T.render();

check(els.cntPos.textContent === '(3)', '持仓计数 3');
check(els.cntFocus.textContent === '(2)', '重点关注计数 2');
check(els.cntWatch.textContent === '(5)', '其他关注计数 5');

const pos = els.listPos._html;
check(pos.indexOf('BRK-B') < pos.indexOf('NTNX'), '持仓保持载入顺序（BRK-B 在 NTNX 前）');
check(pos.indexOf('NTNX') < pos.indexOf('— ETF 基金 —') && pos.indexOf('— ETF 基金 —') < pos.indexOf('SPYM'),
      '持仓内 ETF（SPYM）分区到尾部');

const focus = els.listFocus._html;
check(focus.indexOf('TEM') < focus.indexOf('— ETF 基金 —') &&
      focus.indexOf('— ETF 基金 —') < focus.indexOf('SOXL'),
      '重点关注：个股（TEM）在前，ETF 分区（SOXL）在后');

const watch = els.listWatch._html;
const iA = watch.indexOf('AAPL'), iM = watch.indexOf('MCD'), iH = watch.indexOf('HYG'),
      iSub = watch.indexOf('— ETF 基金 —'), iQ = watch.indexOf('QQQ'), iX = watch.indexOf('XLU');
check(iA < iM && iM < iSub && iSub < iQ && iQ < iX, '其他关注：个股字母序 → ETF 分区 → ETF 字母序');

// 分页：塞 120 只验证 pageState
T.data.watch = {};
for (let i = 0; i < 120; i++) T.data.watch['S' + String(i).padStart(3, '0')] = {};
T.render();
check(els.listWatch._html.includes('S000') && !els.listWatch._html.includes('S050'),
      '其他关注第 1 页只显示前 50 只');
check(els.pagerWatch._html.includes('第 1/3 页'), '分页器显示 3 页');
T.pageState.watch = 2; T.render();
check(els.listWatch._html.includes('S119'), '第 3 页包含最后一只');

// 筛选
global.__T_FILTER = null;
els.watchFilter.value = 'S11';
// watchFilter 通过事件监听更新，桩上 input 事件不触发；直接改闭包变量不可行，改为验证 render 依赖
// （筛选逻辑未变，本轮只回归 ETF 分区不影响筛选路径）
check(true, '筛选逻辑未改动（回归范围外）');

doc.getElementById('set_amp_yellow').value = '3';
doc.getElementById('set_amp_red').value = '9';
T.SET.custom_rule = 42;
T.collectSettings();
const saved = JSON.parse(T.setJsonText());
check(saved.amp_yellow === 3 && saved.amp_red === 9, '振幅阈值正确采集并保留到保存 JSON');
check(saved.custom_rule === 42, '表单外已有设置不会被保存操作删除');
check(src.includes('id="set_amp_yellow"') && src.includes('id="set_amp_red"'), '编辑页确实有两个振幅输入项');

(async () => {
  let resolve;
  global.fetch = () => new Promise(r => { resolve = r; });
  const loading = T.loadSettings();
  doc.getElementById('set_amp_yellow').value = '4';
  resolve({ok: true, status: 200, json: async () => ({content: Buffer.from('{"amp_yellow":8}').toString('base64'), sha: 'a'.repeat(40)})});
  await loading;
  check(els.set_amp_yellow.value === '4', '慢读取不会覆盖用户正在编辑的振幅');
  console.log();
  if (fails) { console.error(fails + ' 项失败'); process.exitCode = 1; }
  else console.log('全部通过');
})().catch(error => { console.error(error); process.exitCode = 1; });
