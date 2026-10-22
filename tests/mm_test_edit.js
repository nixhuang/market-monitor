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
  getElementById(id) {
    if(id==='set_target_space_pct'||id==='set_target_high_space_pct')return null;
    return els[id] || (els[id] = makeEl(id));
  },
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
  eval(code + '\n;globalThis.__T={render,data,pageState,ETF_SET,get SET(){return SET;},SET_DEFS,collectSettings,setJsonText,loadSettings,initGroups,configText,addSymbol,previewCsv,moveSymbol,load,monitorEnabled,renderSettings,settingsError,normalizeSettings};');
  globalThis.__T.initGroups(JSON.parse(fs.readFileSync(path.join(ROOT,'groups.json'),'utf8')));
  console.log('PASS 脚本加载无异常');
} catch (e) {
  console.error('FAIL 脚本加载异常:', e.message);
  process.exit(1);
}

const T = globalThis.__T;
// 独立固定历史默认值，不能用生成器自身的值充当期望值而漏掉默认参数漂移。
const expectedDefaults = {
  boll_n:20,boll_k:2,boll_near_pct:0.5,boll_near_band_pct:15,chg_red:4,chg_yellow:2,
  rsi_high:70,rsi_low:35,near_52w_low_pct:1,trigger_gap_pct:5,reduce_gap_pct:3,
  target_space_pct:30,target_high_space_pct:70,target_gap_pct:3,vol_ratio:1.5,vol_ratio_red:3,vol_ratio_z:1.8,vol_ratio_red_z:3.6,
  amp_yellow:5,amp_red:8,ma_short:50,ma_long:200,hy_green:350,hy_red:400,
  vix_green:20,vix_yellow:30,vix_red:40,use_realtime:1,dca_every:0,dca_start:'',signal_single_open:1,
};
check(Object.keys(T.SET_DEFS).length===Object.keys(expectedDefaults).length&&
  Object.entries(expectedDefaults).every(([key,value])=>T.SET_DEFS[key]===value), '共用schema仅更新目标空间30／70，保留全部非相关默认值');
T.renderSettings();
const targetMigrated=T.normalizeSettings({target_space_pct:50,trigger_gap_pct:8});
check(targetMigrated.values.target_space_pct===30&&targetMigrated.values.target_high_space_pct===70&&
  targetMigrated.values.trigger_gap_pct===8&&targetMigrated.warnings.length===0,
  '旧空间50静默退役为30／70，不产生维护警告且保留其它阈值');
check(T.normalizeSettings({target_space_pct:50.0,target_high_space_pct:70}).warnings.length===0,
  '旧50与已固定70配置兼容归一化');
for(const [key,value] of [['target_space_pct',29],['target_space_pct',70],['target_high_space_pct',100]]){
  const result=T.normalizeSettings({[key]:value});
  check(result.values[key]===T.SET_DEFS[key]&&result.warnings.length===1, key+' 拒绝非固定值 '+value);
}
const migrated=T.normalizeSettings({fund_enable:1,fund_om_drop:7,fund_unknown:true,trigger_gap_pct:8,reduce_gap_pct:2,signal_single_open:0});
check(!Object.keys(migrated.values).some(k=>k.startsWith('fund_'))&&migrated.warnings.length===0&&
  migrated.values.trigger_gap_pct===8&&migrated.values.reduce_gap_pct===2&&migrated.values.signal_single_open===0,
  '读取旧fund设置静默剔除、不产生维护警告且保留技术与交互设置');
check(!src.includes('id="effectiveRuleText"')&&!src.includes('id="ruleDetails"')&&
  !src.includes('id="sourceDetails"')&&src.includes('href="./#signalRules"'),
  '设置页移除重复的完整规则和宏观细则，仅指向首页');
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

// 长持仓 / 短持仓：设置页每行只留一个下拉，两个区和其它分组都在里面选；列表按两区显示。
const rowN = (pos.match(/<div class="row"/g) || []).length;
const selN = (pos.match(/<select class="move"/g) || []).length;
check(rowN > 0 && selN === rowN, '持仓每行只有一个下拉（不再单独一个长/短控件）');
const sel0 = (pos.match(/<select class="move"[\s\S]*?<\/select>/) || [''])[0];
check(sel0.includes('>长持仓<') && sel0.includes('>短持仓<') && sel0.includes('>重点<'),
      '一个下拉里就能选长持仓 / 短持仓 / 其它分组');
check(src.indexOf('class="hold"') === -1, '已移除独立的长/短持仓下拉');
check(src.includes('.edit-group .move{grid-column:2 / span 2;grid-row:3'),
      '手机上这个下拉独占一格，不会和其它控件叠在一起');
T.data.positions['BRK-B'].hold = 'long';
T.render();
const pos2 = els.list_positions._html;
check(pos2.indexOf('— 长持仓 —') >= 0 && pos2.indexOf('— 长持仓 —') < pos2.indexOf('— 短持仓 —'),
      '设置页持仓按「长持仓 → 短持仓」两区显示');
check(/<option value="positions:long"[^>]*>长持仓<\/option>/.test(pos2) &&
      /<option value="positions:long" selected>/.test(pos2),
      '长持仓区的标的，下拉里回显为「长持仓」');
check(T.configText().includes('"hold": "long"'), '长持仓写回 holdings.json 的 hold 字段');
T.moveSymbol('positions', 'positions:short', 'BRK-B');
check(T.data.positions['BRK-B'].hold === 'short', '下拉改选「短持仓」后写回 hold=short');
T.moveSymbol('positions', 'focus', 'BRK-B');
check(!('hold' in (T.data.focus['BRK-B'] || {})), '移出持仓时清掉 hold');
T.moveSymbol('focus', 'positions:long', 'BRK-B');
check(T.data.positions['BRK-B'].hold === 'long', '从其它分组也能直接挪进长持仓区');
check(els.list_technology._html.includes('>长持仓<'), '其它分组的行同样提供长/短持仓两个区可选');
delete T.data.positions['BRK-B'].hold;
T.render();

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

check(!src.includes('id="filterAll"') && !src.includes('group-filter') &&
  !els.groupEditor.innerHTML.includes('id="filter_'), '设置页不展示全组或分组搜索控件');
check(els.pager_technology._html.includes('第 3/3 页'), '移除搜索后分页仍可翻到最后一页');
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
T.SET.fund_enable=1;T.SET.fund_om_drop=7;T.SET.fund_future_rule=99;
const retiredSaved=JSON.parse(T.setJsonText());
check(!Object.keys(retiredSaved).some(k=>k.startsWith('fund_'))&&retiredSaved.custom_rule===42&&
  retiredSaved.amp_yellow===3&&retiredSaved.amp_red===9, '保存剔除全部旧fund设置但保留其他设置');
check(!src.includes('基本面')&&!src.includes('set_fund_')&&!Object.keys(T.SET_DEFS).some(k=>k.startsWith('fund_')),
  '设置页无基本面UI或schema参数');
delete T.SET.fund_enable;delete T.SET.fund_om_drop;delete T.SET.fund_future_rule;
check(!('hy_yellow' in saved), '保存规则不保留无效的旧黄色信用利差阈值');
check(src.includes('id="set_amp_yellow"') && src.includes('id="set_amp_red"'), '编辑页确实有两个振幅输入项');
check(!src.includes('id="set_hy_yellow"')&&src.includes('垃圾债利差 ≥ x bp 黄')&&src.includes('垃圾债利差 ≥ x bp 红'), '信用利差仅保留有效的黄红两条阈值');
check(!src.includes('同类只取最高分')&&!src.includes('站上200日均线的股票不足50%为黄'),
  '设置页不再保留宏观完整说明');
check(!src.includes('高收益债利差')&&!src.includes('跑路价签'), '编辑页只使用垃圾债利差新名称');
check(src.includes('id="set_ma_short"')&&src.includes('id="set_ma_long"')&&
  src.includes('id="set_amp_yellow"')&&src.includes('id="set_amp_red"'), '设置页保留可编辑阈值输入框');
check(!src.includes('id="sourceDetails"')&&!src.includes('五指标综合市场风险')&&
  !src.includes('id="effectiveRuleText"')&&src.includes('生效规则详见<a href="./#signalRules"'),
  '完整规则迁至首页，设置页保留参数入口');

(async () => {
  // 等初始化的模拟读取结束，避免其错误文案与本次迁移断言竞争。
  await new Promise(resolve=>setImmediate(resolve));
  const migrationRequests=[];
  const beforeMigrationStore=JSON.stringify(store);
  els.msg.textContent='迁移前';
  global.fetch=async(url,options)=>{migrationRequests.push(options.method);return {ok:true,status:200,
    json:async()=>({content:Buffer.from('{"target_space_pct":50,"amp_yellow":3,"amp_red":9}').toString('base64'),sha:'a'.repeat(40)})};};
  await T.loadSettings();
  check(migrationRequests.join(',')==='GET'&&JSON.stringify(store)===beforeMigrationStore&&els.msg.textContent==='迁移前',
    '读取旧50仅GET，无维护上传、无本地写入、无参数警告');
  check(T.normalizeSettings({target_space_pct:50}).values.target_high_space_pct===70,
    '缺高空间配置时浏览器自动使用固定70');
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
  check(Number(els.set_ma_short.value)===50&&Number(els.set_ma_long.value)===200, '均线默认50／200日');
  els.set_ma_short.value='20';els.set_ma_long.value='250';
  check(T.settingsError()==='', '两条均线可改为20／250');
  T.collectSettings();
  const periods=JSON.parse(T.setJsonText());
  check(periods.ma_short===20&&periods.ma_long===250, '均线周期真正写入settings JSON');
  els.set_ma_short.value='20.5';check(T.settingsError().includes('ma_short'), '均线周期小数被拒绝');
  els.set_ma_short.value='251';check(T.settingsError().includes('ma_short'), '均线超过250日被拒绝');
  els.set_ma_short.value='200';els.set_ma_long.value='50';
  check(T.settingsError().includes('ma_short'), '均线顺序倒置被拒绝');
  const unrelatedSettings=JSON.parse(T.setJsonText());
  for(const [short,long] of [[0,0],[0,250],[50,0],[250,0],[0,2],[2,250],[50,50]]){
    els.set_ma_short.value=String(short);els.set_ma_long.value=String(long);
    check(T.settingsError()==='', '均线组合'+short+'／'+long+'可保存');
    T.collectSettings();const saved=JSON.parse(T.setJsonText());
    check(saved.ma_short===short&&saved.ma_long===long, '零值保留在settings JSON '+short+'／'+long);
    check(Object.keys(unrelatedSettings).filter(k=>!['ma_short','ma_long'].includes(k)).every(k=>JSON.stringify(saved[k])===JSON.stringify(unrelatedSettings[k])), '均线关闭不改其他设置');
    T.renderSettings();
    check(String(els.set_ma_short.value)===String(short)&&String(els.set_ma_long.value)===String(long), '关闭值重新渲染仍为0');
  }
  for(const key of ['ma_short','ma_long']){
    for(const bad of ['', ' ', '1', '-1', '0.5', '251', 'abc', 'Infinity']){
      T.SET.ma_short=50;T.SET.ma_long=200;T.renderSettings();
      els['set_'+key].value=bad;
      check(T.settingsError().includes(key), key+' 拒绝非法值 '+JSON.stringify(bad));
    }
  }
  check(src.includes('填0关闭对应均线警示')&&src.includes('min="0" max="250"'), '输入框及说明支持零值独立关闭');
  T.SET.ma_short=50;T.SET.ma_long=200;T.renderSettings();
  els.set_boll_n.value='0';
  check(T.settingsError().includes('boll_n'), '布林周期零被阻止');
  els.set_boll_n.value='20';els.set_rsi_low.value='80';
  check(T.settingsError().includes('RSI'), 'RSI上下限倒置被阻止');
  els.set_rsi_low.value='30';els.set_chg_yellow.value='6';
  check(T.settingsError().includes('chg_yellow'), '黄色高于红色阈值被阻止');
  els.set_chg_yellow.value='2';els.set_dca_start.value='2026-02-30';
  check(T.settingsError().includes('定投起点日'), '虚构日期不能保存');
  els.set_dca_start.value='';T.renderSettings();

  // ---- 减仓价：只有持仓分组有这一栏 ----
  persisted={positions:{AAPL:{note:'苹果',trigger:80,reduce:200},MSFT:{note:'微软',reduce:300}},
    focus:{ZS:{note:'Zscaler',reduce:50}},technology:{AMD:{note:'超威',reduce:90}}};
  await T.load();
  const gh=els.groupEditor.innerHTML;
  check(gh.includes('id="newReduce_positions"')&&!gh.includes('newRDir_')&&!gh.includes('rdir'), '持仓的添加栏只有减仓价，没有方向下拉');
  check(!/newReduce_(?!positions)/.test(gh), '其他分组的添加栏没有减仓价');
  const lp=els.list_positions.innerHTML;
  check(lp.includes('data-f="reduce"')&&lp.includes('value="200"')&&lp.includes('value="300"'), '持仓每行显示已存的减仓价');
  check(!lp.includes('<select class="rdir"')&&!lp.includes('reduce_dir'), '持仓每行没有方向下拉');
  check(!els.list_focus.innerHTML.includes('data-f="reduce"')&&!els.list_technology.innerHTML.includes('data-f="reduce"'), '其他分组每行没有减仓价');
  let cfg2=JSON.parse(T.configText());
  check(cfg2.positions.AAPL.reduce===200&&!('reduce_dir' in cfg2.positions.AAPL)&&cfg2.positions.AAPL.trigger===80&&cfg2.positions.MSFT.reduce===300, '保存内容保留减仓价和加仓价');
  doc.getElementById('newSym_positions').value='NVDA';doc.getElementById('newNote_positions').value='英伟达';doc.getElementById('newTrig_positions').value='';
  doc.getElementById('newReduce_positions').value='95.5';
  T.addSymbol('positions');
  cfg2=JSON.parse(T.configText());
  check(cfg2.positions.NVDA&&cfg2.positions.NVDA.reduce===95.5&&!('reduce_dir' in cfg2.positions.NVDA)&&!('trigger' in cfg2.positions.NVDA), '添加持仓时可填减仓价（不存方向）');
  check(doc.getElementById('newReduce_positions').value==='', '添加后清空减仓价');
  for(const bad of ['-3','0','abc']){
    doc.getElementById('newSym_positions').value='AMZN';doc.getElementById('newNote_positions').value='亚马逊';doc.getElementById('newReduce_positions').value=bad;
    T.addSymbol('positions');
    check(!('AMZN' in JSON.parse(T.configText()).positions), '减仓价“'+bad+'”被拒绝，不添加');
  }
  doc.getElementById('newReduce_positions').value='';T.addSymbol('positions');
  cfg2=JSON.parse(T.configText());
  check(cfg2.positions.AMZN&&!('reduce' in cfg2.positions.AMZN)&&!('reduce_dir' in cfg2.positions.AMZN), '不填减仓价时不写 reduce');
  T.moveSymbol('positions','focus','NVDA');
  cfg2=JSON.parse(T.configText());
  check(cfg2.focus.NVDA&&!cfg2.positions.NVDA&&!('reduce' in cfg2.focus.NVDA)&&!('reduce_dir' in cfg2.focus.NVDA), '移出持仓时清掉减仓价，避免留下不监测的残值');
  check(src.includes('id="set_reduce_gap_pct"'), '规则区有减仓价 ±x% 设置');
  T.SET.reduce_gap_pct=3;T.renderSettings();
  check(Number(els.set_reduce_gap_pct.value)===3&&T.settingsError()==='', '减仓价 ±x% 默认 3% 且通过校验');
  els.set_reduce_gap_pct.value='101';check(T.settingsError().includes('reduce_gap_pct'), '减仓价 ±x% 超过 100% 被拒绝');
  els.set_reduce_gap_pct.value='3';T.renderSettings();

  // ---- 目标价：持仓和重点关注都有这一栏，其他分组没有 ----
  persisted={positions:{AAPL:{note:'苹果',target:350,target_at:'2026-09'}},focus:{ZS:{note:'Zscaler',target:250.5,target_at:'2026-10'}},
    technology:{AMD:{note:'超威',target:90,target_at:'2026-10'}}};
  await T.load();
  const gt=els.groupEditor.innerHTML;
  check(gt.includes('id="newTarget_positions"')&&gt.includes('id="newTarget_focus"'), '持仓和重点关注的添加栏都有目标价');
  check(!/newTarget_(?!positions|focus)/.test(gt), '其他分组的添加栏没有目标价');
  check(els.list_positions.innerHTML.includes('data-f="target"')&&els.list_positions.innerHTML.includes('value="350"'), '持仓每行显示已存的目标价');
  check(els.list_focus.innerHTML.includes('data-f="target"')&&els.list_focus.innerHTML.includes('value="250.5"'), '重点关注每行显示已存的目标价');
  check(!els.list_technology.innerHTML.includes('data-f="target"'), '其他分组每行没有目标价');
  cfg2=JSON.parse(T.configText());
  check(cfg2.positions.AAPL.target===350&&cfg2.positions.AAPL.target_at==='2026-09'&&cfg2.focus.ZS.target===250.5, '保存内容保留目标价和保存月份');
  doc.getElementById('newSym_focus').value='NVDA';doc.getElementById('newNote_focus').value='英伟达';
  doc.getElementById('newTarget_focus').value='1000';
  T.addSymbol('focus');
  cfg2=JSON.parse(T.configText());
  check(cfg2.focus.NVDA&&cfg2.focus.NVDA.target===1000&&/^\d{4}-(0[1-9]|1[0-2])$/.test(cfg2.focus.NVDA.target_at), '添加时填目标价：同时记下保存月份');
  check(doc.getElementById('newTarget_focus').value==='', '添加后清空目标价');
  for(const bad of ['-3','0','abc']){
    doc.getElementById('newSym_focus').value='AMZN';doc.getElementById('newNote_focus').value='亚马逊';doc.getElementById('newTarget_focus').value=bad;
    T.addSymbol('focus');
    check(!('AMZN' in JSON.parse(T.configText()).focus), '目标价“'+bad+'”被拒绝，不添加');
  }
  doc.getElementById('newTarget_focus').value='';T.addSymbol('focus');
  cfg2=JSON.parse(T.configText());
  check(cfg2.focus.AMZN&&!('target' in cfg2.focus.AMZN)&&!('target_at' in cfg2.focus.AMZN), '不填目标价时不写 target');
  T.moveSymbol('focus','positions','NVDA');
  cfg2=JSON.parse(T.configText());
  check(cfg2.positions.NVDA&&cfg2.positions.NVDA.target===1000, '持仓和重点关注之间移动，目标价保留');
  T.moveSymbol('positions','technology','NVDA');
  cfg2=JSON.parse(T.configText());
  check(cfg2.technology.NVDA&&!('target' in cfg2.technology.NVDA)&&!('target_at' in cfg2.technology.NVDA), '移到其他分组时清掉目标价和月份');
  check(src.includes('id="set_target_gap_pct"')&&!src.includes('id="set_target_space_pct"')&&
    !src.includes('id="set_target_high_space_pct"'), '仅目标价 ±x% 可编辑，固定30／70不造输入框');
  T.SET.target_gap_pct=3;T.SET.target_space_pct=30;T.SET.target_high_space_pct=70;T.renderSettings();
  check(Number(els.set_target_gap_pct.value)===3&&T.SET.target_space_pct===30&&
    T.SET.target_high_space_pct===70&&T.settingsError()==='', '目标价默认±3%，无空间输入框也能校验固定30／70');
  check(src.includes('30%≤空间&lt;70%')&&src.includes('高空间 ≥70%（含边界）')&&
    src.includes('目标价 / 现价 − 1')&&src.includes('所有分组个股空间 ≥30%')&&
    src.includes('不含 ETF、指数、期货')&&src.includes('有效手填目标价覆盖共识取数失败')&&
    src.includes('旧价仅展示不计数')&&src.includes('无数据不显示胶囊'), '静态说明明确边界、公式、范围和失败口径');
  els.set_target_gap_pct.value='101';check(T.settingsError().includes('target_gap_pct'), '目标价 ±x% 超过 100% 被拒绝');
  els.set_target_gap_pct.value='3';T.SET.target_space_pct=50;
  check(T.settingsError().includes('target_space_pct'), '未归一化旧50值不能通过固定30校验');
  T.SET.target_space_pct=30;T.SET.target_high_space_pct=69;
  check(T.settingsError().includes('target_high_space_pct'), '高空间非70不能通过固定70校验');
  T.SET.target_high_space_pct=70;T.renderSettings();
  const fixedSaved=JSON.parse(T.setJsonText());
  check(fixedSaved.target_space_pct===30&&fixedSaved.target_high_space_pct===70&&T.settingsError()==='',
    '无空间输入框时采集保存仍保留固定30／70');

  // 手动新增只要求有效代码；名称备注不在静态表中时也可留空。
  check(els.groupEditor.innerHTML.includes('placeholder="名称备注（选填）"'), '新增栏明确标注名称备注选填');
  doc.getElementById('newSym_energy').value='ZZZX';doc.getElementById('newNote_energy').value='  ';
  T.addSymbol('energy');
  cfg2=JSON.parse(T.configText());
  check(!!cfg2.energy.ZZZX&&!('note' in cfg2.energy.ZZZX), '不填名称的未知代码可添加并保存，无空备注字段');
  check(els.list_energy.innerHTML.includes('ZZZX'), '未填备注的标的仍显示股票代码');
  check(els.newSym_energy.value===''&&els.msg.textContent.includes('已添加 ZZZX'), '添加成功后清空代码并反馈');
  els.newSym_energy.value='';els.newNote_energy.value='  ';T.addSymbol('energy');
  check(Object.keys(JSON.parse(T.configText()).energy).length===Object.keys(cfg2.energy).length, '空代码仍不能添加');
  console.log();
  if (fails) { console.error(fails + ' 项失败'); process.exitCode = 1; }
  else console.log('全部通过');
})().catch(error => { console.error(error); process.exitCode = 1; });
