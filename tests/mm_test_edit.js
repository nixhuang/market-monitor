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
    addEventListener() {}, attachEvent() {}, setAttribute(k,v) { this[k]=v; },
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
let runOptions;
global.MMRunStatus = { init(options) { runOptions=options; }, refresh() { return {}; }, watchConfig() {}, startManual: async () => {} };
global.MMListImport = require(path.join(ROOT,'list-import.js'));

let fails = 0;
const check = (cond, msg) => { console.log((cond ? 'PASS ' : 'FAIL ') + msg); if (!cond) fails++; };
global.window.addEventListener = () => {};

try {
  eval(code + '\n;globalThis.__T={render,data,pageState,ETF_SET,SET,collectSettings,setJsonText,loadSettings,initGroups,configText,addSymbol,previewCsv,moveSymbol,load,monitorEnabled,renderSettings,settingsError};');
  globalThis.__T.initGroups(JSON.parse(fs.readFileSync(path.join(ROOT,'groups.json'),'utf8')));
  console.log('PASS 脚本加载无异常');
} catch (e) {
  console.error('FAIL 脚本加载异常:', e.message);
  process.exit(1);
}

const T = globalThis.__T;
store.mm_gh_token_v1='github_pat_test_dispatch';
check(runOptions.getToken()==='github_pat_test_dispatch', '编辑页仍可核验保存生效所需的授权状态');
check(!src.includes('id="runCard"')&&!src.includes('id="btnRun"')&&!src.includes('id="btnRuns"')&&
  !src.includes('id="btnCheckStatus"')&&!src.includes('id="runList"'), '编辑页不再重复首页运行控件与20条运行记录');
check(src.includes('id="appliedCard"')&&src.includes('id="appliedState"')&&src.includes('id="detailCard"'),
  '编辑页仍保留保存生效核对与折叠运行详情');
delete store.mm_gh_token_v1;
T.data.positions = { 'BRK-B': {}, SPYM: {}, NTNX: {} };
T.data.focus = { SOXL: { note: '半导体3倍' }, TEM: {} };
T.data.technology = { MCD: {}, XLU: {}, QQQ: {}, AAPL: {}, HYG: { note: '垃圾债-观察' } };
T.render();

check(els.cnt_positions.textContent === '(3)', '持仓计数 3');
check(els.cnt_focus.textContent === '(2)', '重点关注计数 2');
check(els.cnt_technology.textContent === '(5)', 'IT 分类计数 5');

const pos = els.list_positions._html;
check(pos.indexOf('BRK-B') < pos.indexOf('NTNX'), '持仓保持载入顺序（BRK-B 在 NTNX 前）');
check(pos.indexOf('NTNX') < pos.indexOf('— ETF 基金 —') && pos.indexOf('— ETF 基金 —') < pos.indexOf('SPYM'),
      '持仓内 ETF（SPYM）分区到尾部');

const focus = els.list_focus._html;
check(focus.indexOf('TEM') < focus.indexOf('— ETF 基金 —') &&
      focus.indexOf('— ETF 基金 —') < focus.indexOf('SOXL'),
      '重点关注：个股（TEM）在前，ETF 分区（SOXL）在后');

const watch = els.list_technology._html;
const iA = watch.indexOf('AAPL'), iM = watch.indexOf('MCD'), iH = watch.indexOf('HYG'),
      iSub = watch.indexOf('— ETF 基金 —'), iQ = watch.indexOf('QQQ'), iX = watch.indexOf('XLU');
check(iA < iM && iM < iSub && iSub < iQ && iQ < iX, '其他关注：个股字母序 → ETF 分区 → ETF 字母序');

// 分页：塞 120 只验证 pageState
T.data.technology = {};
for (let i = 0; i < 120; i++) T.data.technology['S' + String(i).padStart(3, '0')] = {};
T.render();
check(els.list_technology._html.includes('S000') && !els.list_technology._html.includes('S050'),
      '其他关注第 1 页只显示前 50 只');
check(els.pager_technology._html.includes('第 1/3 页'), '分页器显示 3 页');
T.pageState.technology = 2; T.render();
check(els.list_technology._html.includes('S119'), '第 3 页包含最后一只');

els.filter_technology.oninput({target:{value:'S11'}});
check(els.list_technology._html.includes('S119')&&!els.list_technology._html.includes('S000'), '分类代码筛选生效');
T.previewCsv('symbol,name\nXLV,医疗\nABC,Example Company\n','healthcare');
check(!Object.keys(T.data.healthcare).length, 'CSV 预览前不修改目标组');
els.btnApply.onclick();
check(T.data.healthcare.XLV.note==='医疗'&&T.data.healthcare.ABC.note==='Example Company', '确认导入只写入目标分类与名称');
check(!('watch' in JSON.parse(T.configText())), '保存不包含旧其他关注');
T.previewCsv('symbol,name\nBRK.B-US,Berkshire\n','positions');
T.data.positions.EXTRA={note:'新添加公司'};
els.btnApply.onclick();
check(!!T.data.positions.EXTRA, '清单变化后旧导入预览不能覆盖新标的');
check(els.msg.textContent.includes('重新预览'), '清单变化提示重新确认');
els.csvKind.value='focus';els.csvKind.onchange();
check(src.includes('location.origin!==\'https://nixhuang.github.io\''), '非正式来源隔离远端写入');
T.data.index_funds={};
T.previewCsv('symbol\nAAPL\n2USDCNY\nMSFT\n','index_funds','mixed.csv');
els.btnApply.onclick();
check(Object.keys(T.data.index_funds).join(',')==='AAPL,2USDCNY,MSFT', 'CSV 含汇率代码仍保留全部普通股票');
T.previewCsv('31#BRK.B\n31#.SPX\n31#SGOV\n74#00700\n1600519\n','index_funds','sample.ebk');
els.btnApply.onclick();
check(!!T.data.index_funds['BRK-B']&&!!T.data.index_funds['.SPX']&&!T.data.index_funds.SGOV, 'EBK 股类和指数保留，SGOV及非美股排除');
const beforeAmbiguous=T.configText();
T.previewCsv('Apple,AAPL\nMicrosoft,MSFT\n','positions','no-header.csv');
els.btnApply.onclick();
check(T.configText()===beforeAmbiguous, '歧义 CSV 不修改或覆盖持仓');
check(els.csvInfo.textContent.includes('多个列同样像股票代码'), '歧义 CSV 要求补充代码表头');
T.previewCsv('name,symbol\nApple,AAPL\nMicrosoft,MSFT\n','financials','with-header.csv');
els.btnApply.onclick();
check(!!T.data.financials.AAPL&&!!T.data.financials.MSFT&&!T.data.financials.APPLE, '补充表头后正确导入股票而非公司名');
T.previewCsv('AAPL\nMSFT\n','energy','single-column.csv');
els.btnApply.onclick();
check(!!T.data.energy.AAPL&&!!T.data.energy.MSFT, '无表头单列代码仍可导入');

doc.getElementById('set_amp_yellow').value = '3';
doc.getElementById('set_amp_red').value = '9';
T.SET.custom_rule = 42;
T.collectSettings();
const saved = JSON.parse(T.setJsonText());
check(saved.amp_yellow === 3 && saved.amp_red === 9, '振幅阈值正确采集并保留到保存 JSON');
check(saved.custom_rule === 42, '表单外已有设置不会被保存操作删除');
check(!('hy_yellow' in saved), '保存规则不保留无效的旧黄色信用利差阈值');
check(src.includes('id="set_amp_yellow"') && src.includes('id="set_amp_red"'), '编辑页确实有两个振幅输入项');
check(src.includes('RSI 周期固定为 6 / 12 / 24') && src.includes('同侧两条达到或越过阈值为黄，三条为红'), '编辑页说明三线 RSI 分级规则');
check(!src.includes('RSI ≥ x → 超买红') && !src.includes('RSI ≤ x → 超卖红'), '旧单条 RSI 红灯说明已删除');
check(src.includes('布林上、下轨逼近、触碰、穿越均为黄')&&src.includes('同时出现 RSI 至少两条同侧达到或越过阈值时为红'), '设置页说明布林独立黄与RSI双信号红');
check(!src.includes('id="set_hy_yellow"')&&src.includes('垃圾债利差 ≥ x bp 黄')&&src.includes('垃圾债利差 ≥ x bp 红'), '信用利差仅保留有效的黄红两条阈值');
check(src.includes('一周扩大至少 50 bp 也为红')&&src.includes('站上200日均线的股票不足50%为黄')&&src.includes('金融压力为周度'), '设置页明确说明信用急升和新增市场风险规则');
check(!src.includes('高收益债利差')&&!src.includes('跑路价签'), '编辑页只使用垃圾债利差新名称');
check(src.includes('距52周低点')&&src.includes('上穿或跌破50／200日均线')&&src.includes('基本面单独提示'), '设置页覆盖其他固定个股与基本面规则');
check(src.includes('50／200日均线穿越（固定判定）')&&src.includes('昨日收盘价低于本轮均线')&&src.includes('日线不足对应周期时不计算')&&src.includes('当前不启用250日'), '均线规则单独列明且不擅自改为250日');
check(src.includes('五指标综合市场风险')&&src.includes('同类指标不重复算跨类确认')&&src.includes('一级为市场风险参考和持仓'), '完整说明综合风险及最高优先级去重');

(async () => {
  let resolve;
  global.fetch = () => new Promise(r => { resolve = r; });
  const loading = T.loadSettings();
  doc.getElementById('set_amp_yellow').value = '4';
  resolve({ok: true, status: 200, json: async () => ({content: Buffer.from('{"amp_yellow":8}').toString('base64'), sha: 'a'.repeat(40)})});
  await loading;
  check(els.set_amp_yellow.value === '4', '慢读取不会覆盖用户正在编辑的振幅');
  const cfg=JSON.parse(T.configText());cfg.positions.AAPL={note:'测试',trigger:80};
  cfg.materials={XLB:{note:'板块'},DD:{note:'化工'}};cfg.custom_field='preserved';
  cfg.group_monitoring={positions:false,focus:true,materials:false,future_group:true};
  let persisted=cfg;
  global.fetch=async()=>({ok:true,status:200,json:async()=>({content:Buffer.from(JSON.stringify(persisted)).toString('base64'),sha:'a'.repeat(40)})});
  await T.load();
  check(T.monitorEnabled('positions')&&T.monitorEnabled('focus')&&!T.monitorEnabled('materials'), '旧配置关闭值不影响持仓重点关注始终监测');
  const normalized=JSON.parse(T.configText());
  check(normalized.group_monitoring.positions===true&&normalized.group_monitoring.focus===true, '保存内容将核心两组固定开启');
  check(!els.groupEditor.innerHTML.includes('id="monitor_positions"')&&!els.groupEditor.innerHTML.includes('id="monitor_focus"'), '两组不再生成监测开关');
  check(els.monitorNote_materials.textContent.includes('XLB')&&els.monitorNote_materials.textContent.includes('仍正常抓取和报警'), '关闭化材组说明XLB例外');
  els.monitor_materials.onclick({preventDefault(){},stopPropagation(){}});
  const after=JSON.parse(T.configText());
  check(after.group_monitoring.materials&&after.group_monitoring.future_group, '点击开关序列化并保留未知开关键');
  check(JSON.stringify(after.materials)===JSON.stringify(cfg.materials)&&after.positions.AAPL.trigger===80&&after.custom_field==='preserved', '开关不改成员备注加仓价及未知字段');
  persisted=after;await T.load();
  check(T.monitorEnabled('materials')&&els.monitor_materials['aria-checked']==='true', '保存内容重载后监测状态一致');
  T.renderSettings();
  check(T.settingsError()==='', '默认规则均通过范围与顺序校验');
  els.set_boll_n.value='0';
  check(T.settingsError().includes('boll_n'), '布林周期零被阻止');
  els.set_boll_n.value='20';els.set_rsi_low.value='80';
  check(T.settingsError().includes('RSI'), 'RSI上下限倒置被阻止');
  els.set_rsi_low.value='30';els.set_chg_yellow.value='6';
  check(T.settingsError().includes('chg_yellow'), '黄色高于红色阈值被阻止');
  els.set_chg_yellow.value='2';els.set_dca_start.value='2026-02-30';
  check(T.settingsError().includes('定投起点日'), '虚构日期不能保存');
  els.set_dca_start.value='';T.renderSettings();
  console.log();
  if (fails) { console.error(fails + ' 项失败'); process.exitCode = 1; }
  else console.log('全部通过');
})().catch(error => { console.error(error); process.exitCode = 1; });
