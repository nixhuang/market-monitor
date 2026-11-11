// edit.html 离线 DOM 桩测试：验证折叠、实际控件、保存及清单渲染。
const fs = require('fs');
const path = require('path');
const vm = require('node:vm');
const ROOT = path.resolve(__dirname, '..');
const src = fs.readFileSync(path.join(ROOT, 'edit.html'), 'utf8');
const scripts=[...src.matchAll(/<script\b((?:"[^"]*"|'[^']*'|[^'">])*)>([\s\S]*?)<\/script\s*>/gi)]
  .map(([,attrs,body])=>({src:attrs.match(/(?:^|\s)src\s*=\s*(?:"([^"]*)"|'([^']*)')/i)?.slice(1).find(value=>value!==undefined),body}));
const inlineScripts=scripts.filter(script=>script.src===undefined&&script.body.trim());
if(inlineScripts.length!==1)throw new Error('设置页应有且仅有一个非空内联脚本，实际：'+inlineScripts.length);
const code=inlineScripts[0].body;
function localScriptPath(filename){
  const matches=scripts.filter(script=>script.src?.split(/[?#]/,1)[0]==='./'+filename);
  if(matches.length!==1)throw new Error('设置页应唯一引用本地脚本 '+filename+'，实际：'+matches.length);
  return path.join(ROOT,filename);
}
const runStatusScriptPath=localScriptPath('run-status.js');

const decodeAttribute=value=>value.replace(/&quot;/g,'"').replace(/&lt;/g,'<').replace(/&gt;/g,'>').replace(/&amp;/g,'&');
function makeEl(id) {
  return {
    id, tagName: '', textContent: '', value: '', _html: '', className: '', open: false,
    dataset: {}, files: [], hidden: false, disabled: false,
    classList: { add() {}, remove() {}, contains() { return false; } },
    style: {},
    set innerHTML(v) {
      this._html = v;
      for (const match of v.matchAll(/<([a-z][\w-]*)\b((?:"[^"]*"|'[^']*'|[^'">])*)>/gi)) {
        const id = match[2].match(/\bid="([^"]+)"/);
        if (!id) continue;
        const decodedId=decodeAttribute(id[1]);
        const child = els[decodedId] = makeEl(decodedId);
        child.tagName = match[1].toUpperCase();
        if (child.tagName === 'DETAILS') child.open = /(?:^|\s)open(?:\s|=|$)/.test(match[2]);
      }
      this._rows=[];
      // 生产行新增stock-prices嵌套div；按div深度取完整行，不能在子容器结束处截断删/移控件。
      for(const match of v.matchAll(/<div\b(?=[^>]*\bclass="[^"]*\bstock-row\b[^"]*")(?=[^>]*\bdata-k="([^"]*)")(?=[^>]*\bdata-s="([^"]*)")[^>]*>/g)){
        let depth=1,end=-1;
        const tags=/<\/?div\b[^>]*>/g;tags.lastIndex=match.index+match[0].length;
        for(let tag;(tag=tags.exec(v));){depth+=tag[0].startsWith('</')?-1:1;if(depth===0){end=tag.index;break;}}
        if(end<0)throw new Error('股票行div结构未闭合');
        const body=v.slice(match.index+match[0].length,end),row=makeEl('row');row._html=body;
        row.dataset={k:decodeAttribute(match[1]),s:decodeAttribute(match[2])};
        row._inputs=[...body.matchAll(/<input\b((?:"[^"]*"|'[^']*'|[^'">])*)>/gi)].map(([,attrs])=>{
          const input=makeEl('input');input.tagName='INPUT';
          const field=attrs.match(/(?:^|\s)data-f="([^"]+)"/),value=attrs.match(/(?:^|\s)value="([^"]*)"/);
          if(!field||!value)throw new Error('清单输入控件缺少 data-f 或 value 属性');
          input.dataset.f=field[1];
          input.value=decodeAttribute(value[1]);return input;
        });
        if(!/<button class="del"/.test(body)||!/<select class="move"/.test(body))throw new Error('股票行缺少真实删除/移动控件');
        const prices=body.match(/<div class="stock-prices" style="--price-fields:(\d+)">([\s\S]*?)<\/div>/);
        if(!prices)throw new Error('股票行缺少第二排价格容器');
        row._priceCount=Number(prices[1]);
        row._priceFields=[...prices[2].matchAll(/data-f="([^"]+)"/g)].map(m=>m[1]);
        row._del=makeEl('delete');row._move=makeEl('move');this._rows.push(row);
      }
    },
    get innerHTML() { return this._html; },
    addEventListener() {}, attachEvent() {}, setAttribute(k,v) { this[k]=v; },
    querySelector(selector) {
      if(selector==='.del')return this._del||null;
      if(selector==='.move')return this._move||null;
      const field=selector.match(/^\[data-f="([^"]+)"\]$/);
      return field?(this._inputs||[]).find(input=>input.dataset.f===field[1])||null:null;
    },
    querySelectorAll(selector) { return selector==='.row'?(this._rows||[]):selector==='input'?(this._inputs||[]):[]; },
    insertAdjacentHTML() {}, appendChild() {}, scrollIntoView() { this.scrolled=true; },
    select() {}, focus() { doc.activeElement=this; }, click() {},
    onclick: null, oninput: null, onload: null,
  };
}
const els = {};
for (const match of src.slice(0, src.indexOf('<script')).matchAll(/<([a-z][\w-]*)\b((?:"[^"]*"|'[^']*'|[^'">])*)>/gi)) {
  const id = match[2].match(/\bid="([^"]+)"/);
  if (!id) continue;
  const el = els[id[1]] = makeEl(id[1]);
  el.tagName = match[1].toUpperCase();
  if (el.tagName === 'DETAILS') el.open = /(?:^|\s)open(?:\s|=|$)/.test(match[2]);
}
const doc = {
  getElementById(id) { return els[id] || null; },
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
Object.defineProperty(global,'navigator',{value:{clipboard:{writeText:async()=>{}}},configurable:true});
global.confirm = () => true;
global.alert = () => {};
global.fetch = async () => { throw new Error('no network in test'); };
let runOptions;
global.MMRunStatus = { init(options) { runOptions=options; }, refresh() { return {}; }, watchConfig() {}, startManual: async () => {} };

let fails = 0;
const check = (cond, msg) => { console.log((cond ? 'PASS ' : 'FAIL ') + msg); if (!cond) fails++; };
global.window.addEventListener = () => {};

try {
  eval(code + '\n;globalThis.__T={render,get data(){return data;},pageState,ETF_SET,get SET(){return SET;},SET_DEFS,collectSettings,setJsonText,loadSettings,initGroups,configText,addSymbol,findSymbol,markDefaults,isTicker,normTicker,moveSymbol,load,monitorEnabled,renderSettings,settingsError,normalizeSettings,trackReceipt,saveRules,loadRun,bootstrap,setToken:t=>{TOKEN=t;}};');
  globalThis.__T.initGroups(JSON.parse(fs.readFileSync(path.join(ROOT,'groups.json'),'utf8')));
  if(!process.argv.includes('--layout-fixture'))console.log('PASS 脚本加载无异常');
} catch (e) {
  console.error('FAIL 脚本加载异常:', e.message);
  process.exit(1);
}

const T = globalThis.__T;
async function runDraftRecovery(){
  await new Promise(resolve=>setImmediate(resolve));
  const clone=value=>JSON.parse(JSON.stringify(value));
  const same=(a,b)=>JSON.stringify(a)===JSON.stringify(b);
  let holdings={positions:{AAPL:{note:'原备注',trigger:80,reduce:200,target:250,target_at:'2026-10',cost:90}},
    focus:{},technology:Object.fromEntries(Array.from({length:60},(_,i)=>['S'+String(i).padStart(3,'0'),{trigger:10}]))};
  let settings={...T.SET_DEFS,custom_rule:{kept:true}};
  let sha='a'.repeat(40),putStatus=200,getStatus=200;
  const requests=[],copies=[],prompts=[];
  const response=(payload,status=200)=>({ok:status===200,status,json:async()=>payload});
  const mockFetch=async(url,options)=>{
    requests.push({url,method:options.method,body:options.body&&JSON.parse(options.body)});
    if(options.method==='GET')return response({content:Buffer.from(JSON.stringify(url.includes('settings.json')?settings:holdings)).toString('base64'),sha},getStatus);
    return response({content:{sha:'b'.repeat(40)},commit:{sha:'c'.repeat(40)}},putStatus);
  };
  global.fetch=mockFetch;
  global.confirm=text=>{prompts.push(text);return true;};
  global.navigator.clipboard.writeText=async text=>copies.push(text);
  T.setToken('github_pat_offline_fixture');
  await T.load();await new Promise(resolve=>setImmediate(resolve));
  const input=(kind,symbol,field)=>els['list_'+kind].querySelectorAll('.row').find(row=>row.dataset.s===symbol)?.querySelector('[data-f="'+field+'"]');
  const edit=(kind,symbol,field,value)=>{const el=input(kind,symbol,field);el.value=value;el.oninput();return el;};
  const saveList=()=>els.btnSave.onclick({target:els.btnSave});
  const saveSettings=()=>els.btnSaveRules.onclick({target:els.btnSaveRules});
  const editSetting=(key,value)=>{els['set_'+key].value=value;els['set_'+key].oninput();};
  const bodyValue=()=>JSON.parse(Buffer.from(requests.at(-1).body.content,'base64').toString('utf8'));

  for(const field of ['trigger','reduce','target']){
    for(const raw of ['  bad" <&  ','Infinity','1e309',...(field==='trigger'?[]:['0','-1'])]){
      const before=T.configText(),count=requests.length;
      edit('positions','AAPL',field,raw);T.render();
      check(input('positions','AAPL',field).value===raw&&input('positions','AAPL',field)['aria-invalid']==='true',field+' 无效原文跨渲染保留');
      els.fold_positions.open=false;
      await saveList();
      check(requests.length===count&&T.configText()===before&&els.fold_positions.open&&doc.activeElement===input('positions','AAPL',field)&&doc.activeElement.scrolled,
        field+' 无效草稿阻断提交并定位分组、标的与字段');
      edit('positions','AAPL',field,'100');
    }
    edit('positions','AAPL',field,'');
    check(!(field in T.data.positions.AAPL)&&(field!=='target'||!('target_at' in T.data.positions.AAPL)),field+' 留空可清除错误及旧值');
    edit('positions','AAPL',field,'100');
  }
  T.pageState.technology=1;T.render();edit('technology','S055','trigger','abc');
  T.pageState.technology=0;T.render();els.fold_technology.open=false;
  const beforeHidden=requests.length;await saveList();
  check(requests.length===beforeHidden&&T.pageState.technology===1&&els.fold_technology.open&&
    doc.activeElement===input('technology','S055','trigger')&&doc.activeElement.value==='abc','非当前分页错误也能阻断并准确翻页定位');
  T.moveSymbol('technology','positions','S055');
  check(!!T.data.technology.S055&&!T.data.positions.S055&&input('technology','S055','trigger').value==='abc','移动不会丢弃无效输入');
  edit('technology','S055','trigger','');
  edit('positions','AAPL','reduce','bad');
  const beforeManual=copies.length;T.setToken('');await saveList();await els.btnCopy.onclick();
  check(copies.length===beforeManual,'手动导出与复制入口同样阻断无效草稿');
  T.setToken('github_pat_offline_fixture');edit('positions','AAPL','reduce','100');
  await saveList();check(requests.at(-1).method==='PUT'&&bodyValue().positions.AAPL.reduce===100,'修正全部草稿后才允许提交新值');
  let resolveWrite;
  global.fetch=()=>new Promise(resolve=>{resolveWrite=resolve;});
  const inFlight=saveList();edit('positions','AAPL','reduce','bad');
  resolveWrite(response({content:{sha:'d'.repeat(40)},commit:{sha:'e'.repeat(40)}}));await inFlight;
  check(els.dirty.textContent.includes('未保存')&&input('positions','AAPL','reduce').value==='bad','提交期间新增的无效输入不能被旧快照回执标为已保存');
  global.fetch=mockFetch;
  edit('positions','AAPL','reduce','');

  // 清单409：取消、读取失败、确认合并、显式重试和二次冲突。
  holdings=clone(JSON.parse(T.configText()));await T.load();await new Promise(resolve=>setImmediate(resolve));
  edit('positions','AAPL','note','本地备注');putStatus=409;await saveList();
  check(els.msg.textContent.includes('409')&&els.msg.textContent.includes('保留草稿后读取最新版'),'清单冲突提供真实的保留草稿恢复入口');
  const beforeCancel=requests.length,beforeDraft=T.configText();global.confirm=()=>false;await saveList();
  check(requests.length===beforeCancel&&T.configText()===beforeDraft,'取消恢复不请求、不改草稿、不重试旧SHA');
  global.confirm=text=>{prompts.push(text);return true;};getStatus=503;await saveList();
  check(requests.at(-1).method==='GET'&&T.configText()===beforeDraft,'远端读取失败仍保留清单草稿');
  getStatus=200;holdings.positions.AAPL.note='远端备注';holdings.positions.AAPL.trigger=123;holdings.remote_extra={keep:true};sha='f'.repeat(40);
  let confirms=0;global.confirm=()=>++confirms===1;
  await els.btnReload.onclick();check(T.configText()===beforeDraft,'取消同字段合并保留原草稿和冲突状态');
  global.confirm=text=>{prompts.push(text);return true;};const beforeMerge=requests.length;
  await els.btnReload.onclick();
  check(requests.length===beforeMerge+1&&requests.at(-1).method==='GET'&&T.data.positions.AAPL.note==='本地备注'&&
    T.data.positions.AAPL.trigger===123&&JSON.parse(T.configText()).remote_extra.keep,'清单合并只GET，保留本地修改和远端非冲突更新');
  check(prompts.some(text=>text.includes('本地备注')&&text.includes('远端备注')),'清单同字段冲突明确展示双方值再确认');
  putStatus=200;await saveList();
  check(requests.at(-1).method==='PUT'&&requests.at(-1).body.sha==='f'.repeat(40)&&bodyValue().positions.AAPL.trigger===123,'明确再次保存使用新版SHA而不是旧SHA或强推');

  // 规则409：dirty不阻断显式恢复，未修改字段与未知字段保留远端值。
  editSetting('vol_ratio','1.8');putStatus=409;await saveSettings();
  editSetting('dca_start','2026-10-08');editSetting('dca_every','7');
  const settingDraft=els.set_vol_ratio.value,ruleCount=requests.length;
  await T.loadSettings();check(requests.length===ruleCount,'普通后台重读不能覆盖冲突草稿');
  settings={...settings,vol_ratio:2.2,amp_red:12,custom_rule:{remote:true}};sha='e'.repeat(40);
  confirms=0;global.confirm=()=>++confirms===1;await saveSettings();
  check(els.set_vol_ratio.value===settingDraft&&els.set_amp_red.value==='8','规则取消同字段合并不换SHA或表单');
  global.confirm=text=>{prompts.push(text);return true;};
  getStatus=503;await saveSettings();check(els.set_vol_ratio.value==='1.8'&&els.rulesDirty.textContent.includes('未保存'),'规则读取失败保留草稿和dirty');
  getStatus=200;
  let resolveRead;global.fetch=()=>new Promise(resolve=>{resolveRead=resolve;});
  const loading=saveSettings();editSetting('vol_ratio','not-a-number');
  resolveRead(response({content:Buffer.from(JSON.stringify(settings)).toString('base64'),sha}));await loading;
  check(els.set_vol_ratio.value==='not-a-number'&&els.msg.textContent.includes('停止合并'),'恢复期间继续输入时慢GET不能覆盖草稿');
  global.fetch=mockFetch;await saveSettings();
  check(els.set_vol_ratio.value==='not-a-number'&&els.set_amp_red.value==='12'&&els.set_dca_every.value==='7'&&T.SET.custom_rule.remote,'显式恢复保留无效原始输入、定投草稿和远端隐藏字段');
  const beforeInvalid=requests.length;await saveSettings();
  check(requests.length===beforeInvalid&&els.msg.textContent.includes('vol_ratio'),'恢复后的无效规则仍不能提交');
  editSetting('vol_ratio','1.9');putStatus=200;await saveSettings();
  check(requests.at(-1).method==='PUT'&&requests.at(-1).body.sha==='e'.repeat(40)&&bodyValue().amp_red===12&&
    bodyValue().vol_ratio===1.9&&bodyValue().custom_rule.remote&&bodyValue().dca_every===7,'规则经再次保存携带最新SHA和显式合并结果');
  editSetting('vol_ratio','2.0');putStatus=409;await saveSettings();
  const beforeSecond=requests.length;settings.vol_ratio=2.3;sha='9'.repeat(40);await saveSettings();
  check(requests.length===beforeSecond+1&&requests.at(-1).method==='GET','合并后再次409仍先读取，不自动PUT覆盖他人');
  check(Object.keys(store).length===0,'恢复过程不写本地存储或令牌备份');
  T.setToken('');
}
function settingsLayoutFixture(){
  // 每个正式分组放两只合成证券；调用真实render而非重新手写生产行，绝不读取/保存真实仓位。
  const groups=JSON.parse(fs.readFileSync(path.join(ROOT,'groups.json'),'utf8'));
  const expected=[];
  groups.forEach((g,i)=>{
    T.data[g.key]={};
    for(const suffix of ['A','B']){
      const sym='TEST'+i+suffix;
      T.data[g.key][sym]={note:'合成备注 '+suffix+' <&"',trigger:12.3456,reduce:23.4567,target:34.5678,cost:45.6789};
      if(g.key==='positions')T.data[g.key][sym].hold=suffix==='A'?'long':'short';
      expected.push({group:g.key,symbol:sym,fields:g.key==='positions'?['trigger','reduce','target']:g.key==='focus'?['trigger','target']:['trigger']});
    }
    T.pageState[g.key]=0;
  });
  T.render();
  const body=groups.map(g=>'<details class="card edit-group" open data-test-group="'+g.key+'"><summary>合成 '+g.key+'</summary><div id="list_'+g.key+'">'+els['list_'+g.key]._html+'</div></details>').join('');
  const styles=[...src.matchAll(/<style\b[^>]*>[\s\S]*?<\/style>/gi)].map(m=>m[0]).join('');
  return {html:'<!doctype html><html><head><meta charset="utf-8">'+styles+'</head><body>'+body+'</body></html>',expected};
}
if(process.argv.includes('--layout-fixture')){
  process.stdout.write(JSON.stringify(settingsLayoutFixture()));
}else if(process.argv.includes('--draft-recovery')){
  runDraftRecovery().then(()=>{if(fails)process.exitCode=1;else console.log('定向用例全部通过');})
    .catch(error=>{console.error(error);process.exitCode=1;});
}else{
// 独立固定历史默认值，不能用生成器自身的值充当期望值而漏掉默认参数漂移。
const expectedDefaults = {
  boll_n:20,boll_k:2,boll_near_pct:0.5,boll_near_band_pct:15,chg_red:4,chg_yellow:2,
  rsi_high:70,rsi_low:35,near_52w_low_pct:1,trigger_gap_pct:5,reduce_gap_pct:3,
  target_space_pct:40,target_high_space_pct:90,target_gap_pct:3,vol_ratio:1.5,vol_ratio_red:3,vol_ratio_z:2,vol_ratio_red_z:3.6,
  amp_yellow:5,amp_red:8,ma_short:50,ma_long:200,hy_green:350,hy_red:400,
  vix_green:20,vix_yellow:30,vix_red:40,use_realtime:1,dca_every:0,dca_start:'',signal_single_open:1,group_single_open:0,
};
check(Object.keys(T.SET_DEFS).length===Object.keys(expectedDefaults).length&&
  Object.entries(expectedDefaults).every(([key,value])=>T.SET_DEFS[key]===value), '共用schema仅更新目标空间40／90，保留全部非相关默认值');
T.renderSettings();
const targetMigrated=T.normalizeSettings({target_space_pct:50,trigger_gap_pct:8});
check(targetMigrated.values.target_space_pct===40&&targetMigrated.values.target_high_space_pct===90&&
  targetMigrated.values.trigger_gap_pct===8&&targetMigrated.warnings.length===0,
  '旧空间50静默退役为40／90，不产生维护警告且保留其它阈值');
check(T.normalizeSettings({target_space_pct:50.0,target_high_space_pct:90}).warnings.length===0,
  '旧50与已固定90配置兼容归一化');
for(const [key,value] of [['target_space_pct',29],['target_space_pct',70],['target_high_space_pct',100]]){
  const result=T.normalizeSettings({[key]:value});
  check(result.values[key]===T.SET_DEFS[key]&&result.warnings.length===1, key+' 拒绝非固定值 '+value);
}
const migrated=T.normalizeSettings({fund_enable:1,fund_om_drop:7,fund_unknown:true,trigger_gap_pct:8,reduce_gap_pct:2,signal_single_open:0});
check(!Object.keys(migrated.values).some(k=>k.startsWith('fund_'))&&migrated.warnings.length===0&&
  migrated.values.trigger_gap_pct===8&&migrated.values.reduce_gap_pct===2&&migrated.values.signal_single_open===0,
  '读取旧fund设置静默剔除、不产生维护警告且保留技术与交互设置');
const removedIds=['effectiveRuleText','ruleDetails','sourceDetails','targetSpaceNote',
  'appliedCard','appliedBrief','appliedState','dataTime','detailCard','runSummary'];
check(removedIds.every(id=>doc.getElementById(id)===null&&!src.includes(id)),
  '规则说明与底部生效／运行详情节点及内联引用真实删除');
check(!/布林判定说明|RSI判定说明|量比判定说明|monitorNote_|单独布林|RSI 三条全过界/.test(src),
  '参数区和清单组无残留金融规则说明及动态更新引用');
// 三块说明搬进设置页：允许模板挂载用的 id，仍禁止任何站内规则锚点、书签跳转与 ?rules= 入口。
// 规则样式生成块里的 #rules-lamp-table 等是 CSS 选择器，先剔除以免误判成锚点。
const srcNoRuleCss=src.replace(/\/\* MM_RULECSS_BEGIN[\s\S]*?\/\* MM_RULECSS_END \*\//,'');
check(!/[?&]rules=|#rule[-\w]*|#signalRules|#glossaryRules|#pageNotes|location\.(?:replace|assign)\s*\(/i.test(srcNoRuleCss)
  && src.includes("['rulesTpl','signalRules'],['glossaryTpl','glossaryRules']")
  && src.includes("doc.getElementById('notesTpl')"),
  '设置页无规则链接、规则锚点或旧书签自动跳转（三块只读首页模板挂载）');
check(src.includes('href="./index.html"')&&src.includes('id="authCard"')&&src.includes('id="manualCard"'),
  '删除规则跳转不删除普通返回看板链接和授权／手动操作入口');
// 「← 回看板」原来只是 .sub 里 12px 的裸链接，手机上太小点不到；单独放大成按钮大小的块，
// 但必须保持同一个 href 与文案，不能变成新入口或改跳转。
check(src.includes('<a class="back-board" href="./index.html">← 回看板</a>')
  && /\.sub a\.back-board\{display:inline-block;margin-top:0;padding:9px 16px;font-size:15px;/.test(src),
  '返回看板链接已放大为按钮大小（字号 15px + 内边距），href 与文案不变');
// 2026-10-30 设置页版面统一：折叠标题一律 15px、写死 46px 同高；字号分档统一；间距收窄。
// 统一总块必须写在 MM_RULECSS_END 之后（后声明压过生成块里 13px 的规则卡标题）。
{
  const iEnd=src.indexOf('MM_RULECSS_END');
  const uni=src.indexOf('设置页版面统一');
  check(iEnd>=0&&uni>iEnd, '设置页统一总块位于规则生成块之后（能覆盖 13px 规则卡标题）');
  check(/details\.fold>summary,\.edit-group>summary\{box-sizing:border-box;height:46px;\s*padding-top:0;padding-bottom:0;display:flex;align-items:center;flex-wrap:nowrap\}/.test(src)
    && /#fedRules>summary,#fedGlossary>summary,details\.card:has\(>\.rules-body\)>summary\{\s*box-sizing:border-box;height:46px;/.test(src),
    '三处折叠标题（编辑分组／定投／规则卡）统一写死 46px、flex 垂直居中');
  check(/details\.fold>summary\{font-size:15px/.test(src)
    && /\.edit-group>summary\{color:var\(--text\);font-size:15px/.test(src),
    '折叠标题字号统一 15px（手机桌面同档）');
  check(/\.setsec::after\{content:"";flex:1;height:1px/.test(src)&&/\.setsec\{display:flex;align-items:center/.test(src), '参数分类标题右侧有分隔线');
  check(/\.setrow label\{flex:1;font-size:13px/.test(src)
    && /\.setsec\{font-size:14px;font-weight:600;color:var\(--head-blue/.test(src) && /\.subsec\{font-size:12px/.test(src)
    && /\.note\{color:var\(--dim\);font-size:12\.5px/.test(src)
    && /\nh2\{font-size:16px;color:#fff;font-weight:600/.test(src),
    '设置项文字加大：label 13px、分节小字 12px、说明 12.5px、小节标题 14px');
  check(/\.card\{background:var\(--card\);border:1px solid var\(--line\);border-radius:12px;\s*padding:12px;margin-bottom:10px\}/.test(src)
    && /\.setgrid\{display:grid;grid-template-columns:1fr;gap:5px\}/.test(src)
    && /\.row\{display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-bottom:5px\}/.test(src)
    && !/details\.fold>summary\{font-size:15px;line-height:20px\}/.test(src),
    '间距收窄：卡片外距 14→10、网格间隙 7→5、行距 7→5；手机 15px 覆盖行已删');
  // 2026-10-30 二次调整：同卡并列的两行开关、回看板下方的空隙、三块说明卡标题。
  check(/details\.set summary\{font-size:15px;color:#6ba3f0;font-weight:600;cursor:pointer;/.test(src)
    && /details\.set>summary\{box-sizing:border-box;height:46px;display:flex;/.test(src),
    '参数开关「布林 / 均线 / …」15px 蓝色 600 粗细，与「显示与交互」字号字重同高全部一致');
  check(/<div class="set-block" id="displayCard">\s*<div class="set-title">显示与交互<\/div>/.test(src)&&/<div class="card" id="dcaCard">\s*<h2>定投提醒<\/h2>/.test(src)
    &&!/<details[^>]*id="(?:displayCard|dcaCard)"/.test(src), '「显示与交互」「定投提醒」不再折叠，直接显示');
  check(src.includes('.dca-grid,.dca-actions{display:grid;grid-template-columns:1fr 1fr;gap:8px}')&&
    /<div class="dca-grid">[\s\S]*set_dca_start[\s\S]*set_dca_every[\s\S]*<\/div>\s*<div class="dca-actions">\s*<button id="btnClearDca">[^<]*<\/button>\s*<button class="primary" id="btnSaveDca">/.test(src),
    '定投两排：第一排起点日与间隔，第二排保存与关闭');
  check(/button\.danger\{color:#c9433d;font-weight:500\}/.test(src)&&/button\.danger\{color:#c9433d;font-weight:500\}[^\n]*\n/.test(src)&&src.includes('.token-tools button.danger{padding:6px 12px;font-size:13px')&&/<div class="row token-tools">\s*<button id="btnWipe" class="danger">清除本机令牌<\/button>\s*<\/div>\s*<div class="group-tools" id="rulesTools">/.test(src)&&src.indexOf('id="dcaCard"')<src.indexOf('id="btnWipe"'), '「清除本机令牌」放回定投提醒下面，灰色按钮底色、暗红字#c9433d、13px小按钮');
  check(/\.sub\{color:var\(--dim\);font-size:12\.5px;margin-bottom:4px;/.test(src)
    && /#msg\{font-size:13px;margin:0 2px 8px;/.test(src)&&src.includes('#msg:empty{display:none}'),
    '回看板按钮到「规则参数」卡之间的空隙收窄（46 → 28px）');
  // 「综合判断与宏观细则」：注入时拆掉模板自带的 section 卡壳 + 补 fold 卡类，三块说明卡完全同款
  check(/var sec=frag\.querySelector\('section\.card\.page-notes'\);/.test(src)
    && /mr\.className='card fold';/.test(src)
    && /#marketRiskRules>summary\{color:var\(--head-blue,#bfd5ff\)\}/.test(src),
    '「综合判断与宏观细则」拆壳补 fold 类与「预警规则」同款（46px/15px/#bfd5ff/600）');
}
store.mm_gh_token_v1='github_pat_test_dispatch';
check(runOptions.getToken()==='github_pat_test_dispatch', '编辑页仍可核验保存生效所需的授权状态');
check(!src.includes('id="runCard"')&&!src.includes('id="btnRun"')&&!src.includes('id="btnRuns"')&&
  !src.includes('id="btnCheckStatus"')&&!src.includes('id="runList"'), '编辑页不再重复首页运行控件与20条运行记录');
check(['btnSave','btnSaveRules','btnResetRules','btnSaveDca','btnClearDca','btnReload'].every(id=>
  doc.getElementById(id)&&typeof els[id].onclick==='function'), '清单／规则／定投保存及恢复默认、重读按钮仍绑定真实功能');
const groupKeys=JSON.parse(fs.readFileSync(path.join(ROOT,'groups.json'),'utf8')).map(g=>g.key);
const foldIds=['setToggle',...groupKeys.map(k=>'fold_'+k)];
check(foldIds.every(id=>els[id]&&els[id].tagName==='DETAILS'&&els[id].open===false),
  '所有设置、定投和清单分组初始关闭（含持仓）');
check(!/<details\b[^>]*\sopen(?:\s|=|>)/.test(src), '静态折叠项不含默认open属性');
els.setToggle.open=true;els.fold_focus.open=true;
T.render();T.renderSettings();
check(els.setToggle.open&&els.fold_focus.open&&!els.fold_positions.open,
  '普通清单和参数重渲染保留手动展开／关闭状态');
groupKeys.forEach(k=>{els['fold_'+k].open=false;});T.render();
check(groupKeys.every(k=>els['fold_'+k].open===false), '分组全部收起后重渲染不会自动打开持仓或其它组');
check(!src.includes('btnExpandGroups')&&!src.includes('btnCollapseGroups'), '设置页已移除「全部展开」「全部收起」');
check(/<div class="row group-tools">[\s\S]*?<button id="btnReload">[^<]*<\/button><button class="primary" id="btnSave">[^<]*<\/button>\s*<\/div>/.test(src), '「重新读取已保存清单」「保存清单与分组」移到分类清单标题栏');
check(runOptions.canReload()===false, '后台状态刷新不重载设置页以免重置手动折叠状态');
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
check(pos.indexOf('NTNX') < pos.indexOf('SPYM') && !pos.includes('ETF 基金'),
      '持仓内 ETF（SPYM）排在个股之后，且不再有单独的 ETF 分区标题');
check(/<span class="sym etf">SPYM<\/span>/.test(pos) && /<span class="sym">NTNX<\/span>/.test(pos) && src.includes('.sym.etf{color:var(--link-blue,#6ba3f0)}'),
      'ETF 代码用蓝色（sym etf），股票代码保持白字');

// 长持仓 / 短持仓：设置页每行只留一个下拉，两个区和其它分组都在里面选；列表按两区显示。
const rowN = (pos.match(/<div class="row stock-row"/g) || []).length;
const selN = (pos.match(/<select class="move"/g) || []).length;
check(rowN > 0 && selN === rowN, '持仓每行只有一个下拉（不再单独一个长/短控件）');
const sel0 = (pos.match(/<select class="move"[\s\S]*?<\/select>/) || [''])[0];
check(sel0.includes('>长持仓<') && sel0.includes('>短持仓<') && sel0.includes('>重点<'),
      '一个下拉里就能选长持仓 / 短持仓 / 其它分组');
check(src.indexOf('class="hold"') === -1, '已移除独立的长/短持仓下拉');
check(src.includes('.edit-group .stock-head,.edit-group .stock-row{display:grid;grid-template-columns:54px 100px repeat(var(--price-fields),minmax(0,1fr)) 56px 26px;')
  &&src.includes('.edit-group .stock-row~.stock-row{border-top:1px solid var(--stock-divider)}')
  &&src.includes('.stock-prices{display:contents}')&&!src.includes('grid-row:2;display:grid;'),
  '每股单排：列标题与每行共用同一组列轨道，价格容器display:contents不增排，相邻股票有分隔线');
check(src.includes('.edit-group .stock-row>.sym{width:auto;min-width:0;padding:0;border:0;border-radius:0;background:none;'),
  '代码是不可改的纯文字，不再画占宽的胶囊框');
for(const kind of groupKeys){
  const expectedPrices=kind==='positions'?['trigger','reduce','target']:kind==='focus'?['trigger','target']:['trigger'];
  const labels={trigger:'加仓价',reduce:'减仓价',target:'目标价',cost:'成本价'};
  const html=els['list_'+kind]._html,rows=els['list_'+kind].querySelectorAll('.row');
  if(!rows.length)continue;
  const head=html.match(/^<div class="stock-head"[^>]*--price-fields:(\d+)[^>]*>([\s\S]*?)<\/div>/);
  const headLabels=head?[...head[2].matchAll(/<span>([^<]*)<\/span>/g)].map(m=>m[1]):[];
  check(!!head&&Number(head[1])===expectedPrices.length&&JSON.stringify(headLabels)===JSON.stringify(['代码','备注',...expectedPrices.map(f=>labels[f]),'分组','']),
    kind+' 每组列表上方有列标题，价位列与该组实际价位字段一一对应');
  check(rows.every(r=>JSON.stringify(r._priceFields)===JSON.stringify(expectedPrices)&&r._priceCount===expectedPrices.length
    &&r._inputs.some(i=>i.dataset.f==='note')&&!!r._del&&!!r._move),kind+' 真实render每股一排且备注/价格/删除/移动控件完整');
  check(!html.includes('placeholder='),kind+' 行内输入框不再放占位文字，由列标题说明');
}
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
check(els.fold_focus.open===false&&els.fold_positions.open===false, '移动标的不会强制展开源组或目标组');
T.moveSymbol('focus', 'positions:long', 'BRK-B');
check(T.data.positions['BRK-B'].hold === 'long', '从其它分组也能直接挪进长持仓区');
check(els.list_technology._html.includes('>长持仓<'), '其它分组的行同样提供长/短持仓两个区可选');
delete T.data.positions['BRK-B'].hold;
T.render();

const focus = els.list_focus._html;
check(focus.indexOf('TEM') < focus.indexOf('SOXL') && !focus.includes('ETF 基金'),
      '重点关注：个股（TEM）在前，ETF（SOXL）在后，无分区标题');

const watch = els.list_technology._html;
const iA = watch.indexOf('AAPL'), iM = watch.indexOf('MCD'), iH = watch.indexOf('HYG'),
      iQ = watch.indexOf('QQQ'), iX = watch.indexOf('XLU');
check(iA < iM && iM < iQ && iQ < iX && !watch.includes('ETF 基金'), '其他关注：个股字母序 → ETF 字母序，无分区标题');

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
// 代码规范化与校验（原 CSV/EBK 导入模块里留下的唯一部分，供新增入口使用）
check(T.normTicker('brk.b')==='BRK-B'&&T.normTicker('BRK.B-US')==='BRK-B'&&T.normTicker('$aapl')==='AAPL'&&T.normTicker('31#.SPX')==='.SPX', '代码规范化：大小写、点转横线、-US后缀、31#前缀');
check(T.isTicker('MSFT')&&T.isTicker('BRK-B')&&T.isTicker('2USDCNY')&&T.isTicker('.SPX'), '普通股票、带横线代码及汇率/指数代码有效');
check(!T.isTicker('SGOV')&&!T.isTicker('00700.HK')&&!T.isTicker('600519')&&!T.isTicker('')&&!T.isTicker('total')&&!T.isTicker('1ABC'), 'SGOV、港股、A股、空值、表头噪声和数字开头代码无效');
els.fold_healthcare.open=false;
doc.getElementById('newSym').value='ABC';doc.getElementById('newNote').value='Example Company';doc.getElementById('newMove').value='healthcare';
T.addSymbol();
check(T.data.healthcare.ABC&&T.data.healthcare.ABC.note==='Example Company', '统一入口添加只写入目标分类与备注');
check(!('watch' in JSON.parse(T.configText())), '保存不包含旧其他关注');
check(src.includes('location.origin!==\'https://nixhuang.github.io\''), '非正式来源隔离远端写入');
check(!src.includes('importCard')&&!src.includes('list-import')&&!src.includes('btnLink')&&!src.includes('import-audit')&&
  !/previewCsv|readCsv|parseCsv/.test(src), 'CSV/EBK 导入与手机令牌链接功能已从设置页移除');

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
check(!src.includes('id="set_hy_yellow"')&&src.includes('垃圾债利差黄线（bp）')&&src.includes('垃圾债利差红线（bp）')&&
  src.includes('id="set_hy_green"')&&src.includes('id="set_hy_red"'), '信用利差仅保留有效黄红阈值控件和简短标签');
check(!src.includes('同类只取最高分')&&!src.includes('站上200日均线的股票不足50%为黄'),
  '设置页不再保留宏观完整说明');
check(!src.includes('高收益债利差')&&!src.includes('跑路价签'), '编辑页只使用垃圾债利差新名称');
check(src.includes('id="set_ma_short"')&&src.includes('id="set_ma_long"')&&
  src.includes('id="set_amp_yellow"')&&src.includes('id="set_amp_red"'), '设置页保留可编辑阈值输入框');
check(!src.includes('id="sourceDetails"')&&!src.includes('五指标综合市场风险')&&
  !src.includes('id="effectiveRuleText"')&&Object.keys(expectedDefaults).filter(k=>
    !['target_space_pct','target_high_space_pct'].includes(k)).every(k=>doc.getElementById('set_'+k)),
  '完整规则不重复展示，全部真实可编辑参数控件保留');

// 分组与行数据同样走双引号属性和文字上下文转义。
const unsafeKey='offline" data-injected="yes',unsafeText='<img src=x onerror="bad()"> & 中文';
const baseGroups=JSON.parse(fs.readFileSync(path.join(ROOT,'groups.json'),'utf8'));
T.initGroups([...baseGroups,{key:unsafeKey,label:unsafeText}]);
T.data[unsafeKey]={'A&B<CODE>':{note:unsafeText,trigger:'" onfocus="bad()'}};T.render();
check(!els.groupEditor.innerHTML.includes(' data-injected="yes')&&
  els.groupEditor.innerHTML.includes('offline&quot; data-injected=&quot;yes')&&
  els.groupEditor.innerHTML.includes('&lt;img src=x onerror=&quot;bad()&quot;> &amp; 中文'),
  '分组键和标签转义后不能逃出属性或注入HTML且中文与&只转义一次');
check(els['list_'+unsafeKey].innerHTML.includes('data-k="offline&quot; data-injected=&quot;yes"')&&
  els['list_'+unsafeKey].innerHTML.includes('A&amp;B&lt;CODE>')&&
  !els['list_'+unsafeKey].innerHTML.includes('<img')&&!els['list_'+unsafeKey].innerHTML.includes(' onfocus="bad()'),
  '行分组、代码、备注、价格属性均不能注入HTML或事件属性');
const unsafeRow=els['list_'+unsafeKey].querySelectorAll('.row')[0];
check(unsafeRow.querySelector('[data-f="note"]').value===unsafeText&&
  unsafeRow.querySelector('[data-f="trigger"]').value==='" onfocus="bad()',
  'DOM桩保留属性内的大于号、引号和实体，不截断控件或遗漏后续字段');
delete T.data[unsafeKey];T.initGroups(baseGroups);

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
  check(T.normalizeSettings({target_space_pct:50}).values.target_high_space_pct===90,
    '缺高空间配置时浏览器自动使用固定90');
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
  els.fold_focus.open=true;els.fold_materials.open=false;
  await T.load();
  check(els.fold_focus.open&&!els.fold_materials.open&&els.setToggle.open,
    '重新读取清单及异步参数读取保留用户的手动折叠状态');
  check(T.monitorEnabled('positions')&&T.monitorEnabled('focus')&&!T.monitorEnabled('materials'), '旧配置关闭值不影响持仓重点关注始终监测');
  const normalized=JSON.parse(T.configText());
  check(normalized.group_monitoring.positions===true&&normalized.group_monitoring.focus===true, '保存内容将核心两组固定开启');
  check(!els.groupEditor.innerHTML.includes('id="monitor_positions"')&&!els.groupEditor.innerHTML.includes('id="monitor_focus"'), '两组不再生成监测开关');
  check(!/monitorNote_/.test(els.groupEditor.innerHTML)&&els.monitor_materials['aria-checked']==='false'&&
    normalized.materials.XLB.note==='板块'&&normalized.materials.DD.note==='化工', '删除组规则说明，关闭状态及XLB和普通成员数据仍保留');
  els.monitor_materials.onclick({preventDefault(){},stopPropagation(){}});
  const after=JSON.parse(T.configText());
  check(after.group_monitoring.materials&&after.group_monitoring.future_group, '点击开关序列化并保留未知开关键');
  check(!els.fold_materials.open&&els.fold_focus.open, '监测开关重渲染保留各组打开与关闭状态');
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
  check(src.includes('短均线（交易日，0关闭）')&&src.includes('长均线（交易日，0关闭）')&&
    src.includes('min="0" max="250"')&&!src.includes('填0关闭对应均线警示'), '均线用简短关闭标签与输入约束替代规则说明');
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
  check(!gh.includes('newReduce_')&&!gh.includes('class="add row"')&&!/id="newSym_/.test(gh), '分组里不再有逐组新增表单');
  check(!!els.newSym&&!!els.newNote&&!!els.newMove&&!!els.btnAddSym&&src.includes('<h2>新增股票</h2>'), '页面只有一个统一新增入口');
  const lp=els.list_positions.innerHTML;
  check(lp.includes('data-f="reduce"')&&lp.includes('value="200"')&&lp.includes('value="300"'), '持仓每行显示已存的减仓价');
  check(!lp.includes('<select class="rdir"')&&!lp.includes('reduce_dir'), '持仓每行没有方向下拉');
  check(!els.list_focus.innerHTML.includes('data-f="reduce"')&&!els.list_technology.innerHTML.includes('data-f="reduce"'), '其他分组每行没有减仓价');
  let cfg2=JSON.parse(T.configText());
  check(cfg2.positions.AAPL.reduce===200&&!('reduce_dir' in cfg2.positions.AAPL)&&cfg2.positions.AAPL.trigger===80&&cfg2.positions.MSFT.reduce===300, '保存内容保留减仓价和加仓价');
  doc.getElementById('newSym').value='NVDA';doc.getElementById('newNote').value='英伟达';doc.getElementById('newMove').value='positions:long';
  T.addSymbol();
  cfg2=JSON.parse(T.configText());
  check(cfg2.positions.NVDA&&cfg2.positions.NVDA.hold==='long'&&cfg2.positions.NVDA.note==='英伟达'&&!('reduce' in cfg2.positions.NVDA)&&!('trigger' in cfg2.positions.NVDA),
    '统一入口选「长持仓」添加：写入hold=long，不预置价位');
  check(doc.getElementById('newSym').value===''&&doc.getElementById('newNote').value==='', '添加后清空代码与备注');
  doc.getElementById('newSym').value='AMZN';doc.getElementById('newNote').value='亚马逊';doc.getElementById('newMove').value='';
  T.addSymbol();
  check(!('AMZN' in JSON.parse(T.configText()).positions), '未选分组时拒绝添加');
  doc.getElementById('newMove').value='positions:short';T.addSymbol();
  cfg2=JSON.parse(T.configText());
  check(cfg2.positions.AMZN&&cfg2.positions.AMZN.hold==='short'&&!('reduce' in cfg2.positions.AMZN)&&!('reduce_dir' in cfg2.positions.AMZN), '选「短持仓」添加且不写 reduce');
  T.data.positions.NVDA.reduce=95.5;
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
  check(!gt.includes('newTarget_'), '分组里不再有逐组新增表单（目标价在添加后的行里填）');
  check(els.list_positions.innerHTML.includes('data-f="target"')&&els.list_positions.innerHTML.includes('value="350"'), '持仓每行显示已存的目标价');
  check(els.list_focus.innerHTML.includes('data-f="target"')&&els.list_focus.innerHTML.includes('value="250.5"'), '重点关注每行显示已存的目标价');
  check(!els.list_technology.innerHTML.includes('data-f="target"'), '其他分组每行没有目标价');
  cfg2=JSON.parse(T.configText());
  check(cfg2.positions.AAPL.target===350&&cfg2.positions.AAPL.target_at==='2026-09'&&cfg2.focus.ZS.target===250.5, '保存内容保留目标价和保存月份');
  doc.getElementById('newSym').value='NVDA';doc.getElementById('newNote').value='英伟达';doc.getElementById('newMove').value='focus';
  T.addSymbol();
  cfg2=JSON.parse(T.configText());
  check(cfg2.focus.NVDA&&!('target' in cfg2.focus.NVDA)&&!('target_at' in cfg2.focus.NVDA), '统一入口添加到重点：不预置目标价');
  check(els.fold_focus.open===true&&els.msg.textContent.includes('已添加 NVDA'), '添加后自动展开目标分组并提示在行内填价位');
  T.data.focus.NVDA.target=1000;T.data.focus.NVDA.target_at='2026-11';
  cfg2=JSON.parse(T.configText());
  T.moveSymbol('focus','positions','NVDA');
  cfg2=JSON.parse(T.configText());
  check(cfg2.positions.NVDA&&cfg2.positions.NVDA.target===1000, '持仓和重点关注之间移动，目标价保留');
  T.moveSymbol('positions','technology','NVDA');
  cfg2=JSON.parse(T.configText());
  check(cfg2.technology.NVDA&&!('target' in cfg2.technology.NVDA)&&!('target_at' in cfg2.technology.NVDA), '移到其他分组时清掉目标价和月份');
  check(src.includes('id="set_target_gap_pct"')&&!src.includes('id="set_target_space_pct"')&&
    !src.includes('id="set_target_high_space_pct"'), '仅目标价 ±x% 可编辑，固定40／90不造输入框');
  T.SET.target_gap_pct=3;T.SET.target_space_pct=40;T.SET.target_high_space_pct=90;T.renderSettings();
  check(Number(els.set_target_gap_pct.value)===3&&T.SET.target_space_pct===40&&
    T.SET.target_high_space_pct===90&&T.settingsError()==='', '目标价默认±3%，无空间输入框也能校验固定40／90');
  check(!src.includes('目标空间固定分档')&&!src.includes('目标价 / 现价 − 1')&&
    !src.includes('旧价仅展示不计数')&&src.includes('目标价距离（±%）'), '删除目标空间金融说明，仅保留必要目标价标签');
  els.set_target_gap_pct.value='101';check(T.settingsError().includes('target_gap_pct'), '目标价 ±x% 超过 100% 被拒绝');
  els.set_target_gap_pct.value='3';T.SET.target_space_pct=50;
  check(T.settingsError().includes('target_space_pct'), '未归一化旧50值不能通过固定40校验');
  T.SET.target_space_pct=40;T.SET.target_high_space_pct=89;
  check(T.settingsError().includes('target_high_space_pct'), '高空间非90不能通过固定90校验');
  T.SET.target_high_space_pct=90;T.renderSettings();
  const fixedSaved=JSON.parse(T.setJsonText());
  check(fixedSaved.target_space_pct===40&&fixedSaved.target_high_space_pct===90&&T.settingsError()==='',
    '无空间输入框时采集保存仍保留固定40／90');

  // ---- 同一只股票全页只能一份：载入合并、新增拦截、移动整份转移、导入不复制；成本价不在设置页 ----
  persisted={positions:{AAPL:{note:'苹果',cost:325.03,hold:'long'}},focus:{AAPL:{note:'苹果',trigger:325,target:400,target_at:'2026-10'},ZS:{note:'云安全'}},
    technology:{AAPL:{note:''},ZS:{note:'云安全',trigger:230},AMD:{note:'超威'}}};
  await T.load();
  let uq=JSON.parse(T.configText());
  check(Object.keys(uq.positions).join()==='AAPL'&&!uq.focus.AAPL&&!uq.technology.AAPL&&!uq.technology.ZS&&uq.focus.ZS&&uq.technology.AMD,
    '载入时同一代码只保留最高优先级（持仓>重点>其他）那一份，重复项被移除');
  check(uq.positions.AAPL.trigger===325&&uq.positions.AAPL.target===400&&uq.positions.AAPL.target_at==='2026-10'&&uq.positions.AAPL.cost===325.03&&uq.positions.AAPL.hold==='long',
    '被移除重复项里胜出者缺的加仓价/目标价并入，已有成本与长/短不被覆盖');
  check(uq.focus.ZS.trigger===230, '重点与其他分组重复：缺的加仓价并入保留的重点那份');
  check(els.dirty.textContent.includes('未保存'), '载入时合并过重复项，提示有未保存改动');
  check(!els.list_positions.innerHTML.includes('data-f="cost"')&&!els.list_positions.innerHTML.includes('成本价'), '设置页持仓不再有成本价输入或列');
  check(JSON.parse(T.configText()).positions.AAPL.cost===325.03, '不显示成本价，但保存时已有成本原样保留');
  doc.getElementById('newSym').value='AAPL';doc.getElementById('newMove').value='technology';T.addSymbol();
  check(!JSON.parse(T.configText()).technology.AAPL&&els.msg.textContent.includes('已在「持仓」'), '新增代码已存在于别的分组：拒绝复制，提示用分组下拉转移');
  T.moveSymbol('positions','focus','AAPL');
  uq=JSON.parse(T.configText());
  check(!uq.positions.AAPL&&uq.focus.AAPL&&uq.focus.AAPL.trigger===325&&uq.focus.AAPL.target===400&&uq.focus.AAPL.note==='苹果',
    '从持仓转到重点：整份记录带着已有信息转移，不是复制');
  T.moveSymbol('focus','positions:short','AAPL');
  uq=JSON.parse(T.configText());
  check(uq.positions.AAPL&&uq.positions.AAPL.hold==='short'&&uq.positions.AAPL.trigger===325&&!uq.focus.AAPL,
    '从重点转回持仓（短持仓）：加仓价等随之带回，重点里不留副本');

  // 默认值灰字：每个设置框左侧标出默认数值，开关项标开/关
  {
    const log=[];const fake=(id,tag)=>{els[id]={tagName:tag,closest:()=>({}),parentNode:{insertBefore:(n,e)=>log.push([id,n.textContent,n.className])}};};
    fake('set_boll_n','INPUT');fake('set_chg_red','INPUT');fake('set_use_realtime','SELECT');fake('set_group_single_open','SELECT');
    T.markDefaults();
    const got=Object.fromEntries(log.map(([id,text,cls])=>[id,[text,cls]]));
    check(got.set_boll_n&&got.set_boll_n[0]==='20'&&got.set_chg_red&&got.set_chg_red[0]==='4'&&got.set_boll_n[1]==='def', '数值框左侧标出默认数值（只标数值，不写「默认」）');
    check(got.set_use_realtime[0]==='开'&&got.set_group_single_open[0]==='关', '开关项标默认的开/关');
    check(/\.setrow \.def\{[^}]*font-size:13px[^}]*color:#6f7785/.test(src), '默认值是灰字、不小于13px');
  }
  // 搜索定位：精确优先、前缀其次、找不到返回 null；代码全页唯一所以只会命中一个分组；返回所在页
  const holderKey=sym=>Object.keys(T.data).find(g=>T.data[g]&&T.data[g][sym]);
  const fa=T.findSymbol(' aapl ');
  check(fa&&fa.exact&&fa.kind===holderKey('AAPL')&&fa.symbol==='AAPL'&&fa.page===0, '搜索代码（不分大小写、去空格）精确命中所在分组');
  const fp=T.findSymbol('AAP');
  check(fp&&!fp.exact&&fp.symbol==='AAPL', '前缀也能定位（标注按前缀匹配）');
  check(T.findSymbol('NOSUCHXX')===null&&T.findSymbol('  ')===null, '找不到或空输入返回空，不报错');
  check(/<div class="row card-head">\s*<h2>规则参数<\/h2>\s*<button id="btnResetRules">[^<]*<\/button>\s*<button class="primary" id="btnSaveRules">/.test(src), '「恢复默认」「保存规则」在「规则参数」标题栏（恢复在左、保存在右）');
  check(/<h2>分类清单<\/h2>\s*<span class="dirty-text" id="dirty"[^>]*><\/span>\s*<button id="btnReload">/.test(src)&&!src.includes('savebar'), '「有未保存的改动」提示紧跟「分类清单」标题，字号15px');
  check(/<div class="row search-row">\s*<span id="groupTotal"><\/span>\s*<input id="symSearch"/.test(src)&&src.includes('.group-tools{flex-wrap:nowrap}'), '分组/记录数与搜索定位同一排，标题栏固定一行');
  {
    // 统一按钮顺序：同一组里取消/恢复/关闭类在左，保存/确认类（primary）在右
    const order=(...ids)=>ids.map(id=>src.indexOf('id="'+id+'"'));
    const asc=a=>a.every((v,i)=>i===0||v>a[i-1]);
    check(asc(order('btnBack','btnCopy'))&&asc(order('btnReload','btnSave'))&&asc(order('btnResetRules','btnSaveRules'))&&asc(order('btnClearDca','btnSaveDca')),
      '设置页各组按钮统一：恢复/取消/关闭在左，保存/确认在右');
  }
  check(src.includes('id="symSearch"')&&src.includes('id="btnLocate"')&&src.includes('.stock-row.hit{'), '分类清单上方有搜索框、定位按钮和命中高亮样式');

  // 手动新增只要求有效代码；名称备注不在静态表中时也可留空。
  check(src.includes('id="newNote" placeholder="名称备注（选填）"'), '新增栏明确标注名称备注选填');
  doc.getElementById('newSym').value='ZZZX';doc.getElementById('newNote').value='  ';doc.getElementById('newMove').value='energy';
  T.addSymbol();
  cfg2=JSON.parse(T.configText());
  check(!!cfg2.energy.ZZZX&&!('note' in cfg2.energy.ZZZX), '不填名称的未知代码可添加并保存，无空备注字段');
  check(els.list_energy.innerHTML.includes('ZZZX'), '未填备注的标的仍显示股票代码');
  check(els.newSym.value===''&&els.msg.textContent.includes('已添加 ZZZX'), '添加成功后清空代码并反馈');
  els.newSym.value='';els.newNote.value='  ';T.addSymbol();
  check(Object.keys(JSON.parse(T.configText()).energy).length===Object.keys(cfg2.energy).length, '空代码仍不能添加');
  // 真实按钮保存：只用内存令牌与模拟PUT，覆盖回执缺失、提交校验和恢复默认。
  T.renderSettings();
  const writes=[];
  global.fetch=async(url,options)=>{
    writes.push({url,options});
    return {ok:true,status:200,json:async()=>({content:{sha:'b'.repeat(40)},commit:{sha:'c'.repeat(40)}})};
  };
  T.setToken('github_pat_memory_only');
  await els.btnSave.onclick({target:els.btnSave});
  check(writes.length===1&&writes[0].options.method==='PUT'&&writes[0].url.includes('/contents/holdings.json')&&
    !els.btnSave.disabled, '底部详情删除后保存清单按钮仍提交并恢复可用');
  const beforeInvalid=writes.length;
  els.set_ma_short.value='1';await T.saveRules(els.btnSaveRules);
  check(writes.length===beforeInvalid&&els.msg.textContent.includes('ma_short'), '保存规则仍阻止非法参数写入');
  T.renderSettings();await els.btnSaveRules.onclick({target:els.btnSaveRules});
  const submitted=JSON.parse(Buffer.from(JSON.parse(writes.at(-1).options.body).content,'base64').toString('utf8'));
  check(writes.at(-1).options.method==='PUT'&&writes.at(-1).url.includes('/contents/settings.json')&&
    submitted.vol_ratio_z===2&&submitted.vol_ratio_red_z===3.6&&
    submitted.vix_green===20&&submitted.vix_yellow===30&&submitted.vix_red===40&&!els.btnSaveRules.disabled,
    '保存规则真实序列化并保留黄2／红3.6σ与VIX20／30／40');
  check(T.trackReceipt('settings.json',{content:{sha:'bad'}})===false&&els.msg.className==='err',
    '移除生效UI不移除有效blob／commit回执校验');
  T.SET.dca_start='2026-10-01';T.SET.dca_every=5;T.SET.vol_ratio_z=1;T.renderSettings();
  els.btnResetRules.onclick();
  check(T.SET.vol_ratio_z===2&&T.SET.vol_ratio_red_z===3.6&&T.SET.dca_every===5&&T.SET.dca_start==='2026-10-01'&&
    els.setToggle.open, '恢复默认保留定投和手动折叠状态，并恢复黄2／红3.6σ');

  // 延迟回执：快照、编辑基线和共享写锁必须彼此独立，所有请求均为内存桩。
  Object.assign(T.SET,expectedDefaults);T.renderSettings();
  await T.saveRules(els.btnSaveRules);
  const pendingWrites=[];
  global.fetch=(url,options)=>new Promise(resolve=>pendingWrites.push({url,options,resolve}));
  const finishWrite=(job,blob='d',commit='e',status=200)=>job.resolve({ok:status===200,status,
    json:async()=>({content:{sha:blob.repeat(40)},...(commit?{commit:{sha:commit.repeat(40)}}:{})})});
  const decoded=job=>JSON.parse(Buffer.from(JSON.parse(job.options.body).content,'base64').toString('utf8'));
  const editSetting=(key,value)=>{els['set_'+key].value=value;els['set_'+key].oninput();};
  const firstSave=T.saveRules(els.btnSaveRules);
  const firstWrite=pendingWrites[0];
  editSetting('vol_ratio','1.8');
  check(['btnSaveRules','btnSaveDca','btnClearDca'].every(id=>els[id].disabled)&&
    !els.set_vol_ratio.disabled&&!els.btnResetRules.disabled,'写入期间共享保存按钮锁定，但输入与恢复默认仍可用');
  await T.saveRules(els.btnSaveRules);await els.btnSaveDca.onclick({target:els.btnSaveDca});
  await els.btnClearDca.onclick({target:els.btnClearDca});
  check(pendingWrites.length===1&&decoded(firstWrite).vol_ratio===1.5,
    '连续保存及跨定投入口不重复写入，首个请求保持1.5快照');
  finishWrite(firstWrite);await firstSave;
  check(els.set_vol_ratio.value==='1.8'&&els.rulesDirty.textContent.includes('未保存')&&
    els.msg.textContent.includes('尚未保存'),'1.5提交回执不覆盖1.8后续输入且不会把后改值误报已保存');
  editSetting('vol_ratio','1.5');
  check(els.rulesDirty.textContent==='', '退回实际已提交快照可准确清除dirty，而非只靠单调编辑计数');
  editSetting('vol_ratio','1.8');
  const secondSave=T.saveRules(els.btnSaveRules),secondWrite=pendingWrites[1];
  check(JSON.parse(secondWrite.options.body).sha==='d'.repeat(40)&&decoded(secondWrite).vol_ratio===1.8,
    '下一次保存携带首个有效blob SHA并提交后改1.8');
  finishWrite(secondWrite,'f','a');await secondSave;
  check(els.rulesDirty.textContent.includes('已提交')&&!els.msg.textContent.includes('尚未保存')&&
    ['btnSaveRules','btnSaveDca','btnClearDca'].every(id=>!els[id].disabled),'无新编辑时准确提示本次提交且三个入口恢复可用');
  editSetting('vol_ratio','1.9');
  const failedSave=T.saveRules(els.btnSaveRules),failedWrite=pendingWrites[2];
  editSetting('vol_ratio','2.0');finishWrite(failedWrite,'b','c',409);await failedSave;
  check(els.msg.textContent.includes('409')&&els.set_vol_ratio.value==='2.0'&&els.rulesDirty.textContent.includes('未保存'),
    '冲突失败保留提交期间输入及dirty');
  editSetting('vol_ratio','1.8');check(els.rulesDirty.textContent==='', '失败不推进已提交基线');
  editSetting('vol_ratio','2.0');
  const recovery=T.saveRules(els.btnSaveRules),recoveryRead=pendingWrites[3];
  check(recoveryRead.options.method==='GET','409后再次保存先保留草稿并读取最新版，不重复提交旧SHA');
  recoveryRead.resolve({ok:true,status:200,json:async()=>({content:Buffer.from(JSON.stringify({...T.SET,vol_ratio:1.8})).toString('base64'),sha:'f'.repeat(40)})});
  await recovery;pendingWrites.pop();
  check(els.set_vol_ratio.value==='2.0'&&els.rulesDirty.textContent.includes('未保存'),'恢复只合并规则草稿，仍需再次明确保存');
  const missingSave=T.saveRules(els.btnSaveRules),missingWrite=pendingWrites[3];
  check(JSON.parse(missingWrite.options.body).sha==='f'.repeat(40),'失败不推进blob SHA');
  finishWrite(missingWrite,'b','');await missingSave;
  check(els.msg.className==='err'&&els.rulesDirty.textContent.includes('未保存'), '缺commit回执不把规则误标已确认提交');
  const resetSave=T.saveRules(els.btnSaveRules),resetWrite=pendingWrites[4];
  check(JSON.parse(resetWrite.options.body).sha==='b'.repeat(40), '已接受但缺commit时仍保存有效新blob SHA供后续冲突校验');
  editSetting('dca_start','2026-10-08');editSetting('dca_every','7');
  T.SET.custom_rule=42;els.btnResetRules.onclick();
  check(els.set_dca_start.value==='2026-10-08'&&els.set_dca_every.value==='7'&&T.SET.custom_rule===42&&
    Number(els.set_vol_ratio.value)===1.5, '提交期间恢复默认保留当前未提交定投与表单外参数');
  await T.loadSettings();
  check(pendingWrites.length===5, '写入期间重新读取不覆盖当前规则或抢改SHA');
  finishWrite(resetWrite,'c','d');await resetSave;
  check(Number(els.set_vol_ratio.value)===1.5&&els.set_dca_every.value==='7'&&els.rulesDirty.textContent.includes('未保存'),
    '恢复默认后旧请求成功仍只确认旧快照，新默认值及定投保持dirty');
  await T.loadSettings();check(pendingWrites.length===5,'已存在未保存规则时清单重读不顺带覆盖规则');
  const afterResetSave=els.btnSaveDca.onclick({target:els.btnSaveDca}),afterResetWrite=pendingWrites[5];
  check(decoded(afterResetWrite).vol_ratio===1.5&&decoded(afterResetWrite).dca_every===7&&
    decoded(afterResetWrite).dca_start==='2026-10-08', '定投入口提交恢复默认后的完整最新快照');
  finishWrite(afterResetWrite,'e','f');await afterResetSave;
  const clearSave=els.btnClearDca.onclick({target:els.btnClearDca}),clearWrite=pendingWrites[6];
  check(decoded(clearWrite).dca_every===0&&decoded(clearWrite).dca_start==='','关闭定投仍提交关闭值');
  finishWrite(clearWrite,'a','b');await clearSave;
  editSetting('vol_ratio','1.9');
  global.fetch=async()=>{throw new Error('模拟断网');};
  await T.saveRules(els.btnSaveRules);
  check(els.msg.className==='err'&&els.rulesDirty.textContent.includes('未保存')&&!els.btnSaveRules.disabled,
    '网络失败保留dirty并释放写锁');
  T.setToken('');await T.saveRules(els.btnSaveRules);
  check(els.rulesDirty.textContent.includes('未保存')&&JSON.parse(els.rulesOut.value).vol_ratio===1.9,
    '手动复制模式只生成JSON，不冒认已提交或清空dirty');
  T.setToken('github_pat_memory_only');
  global.fetch=async()=>({ok:true,status:200,json:async()=>({content:{sha:'c'.repeat(40)},commit:{sha:'d'.repeat(40)}})});
  await T.saveRules(els.btnSaveRules);
  let resolveSettings;
  global.fetch=()=>new Promise(resolve=>{resolveSettings=resolve;});
  const lateLoad=T.loadSettings();els.btnResetRules.onclick();
  resolveSettings({ok:true,status:200,json:async()=>({content:Buffer.from('{"vol_ratio":2.3}').toString('base64'),sha:'e'.repeat(40)})});
  await lateLoad;
  check(Number(els.set_vol_ratio.value)===1.5&&els.rulesDirty.textContent.includes('未保存'),
    '恢复默认推进读取版本，慢GET不能覆盖默认值');
  global.fetch=async()=>({ok:true,status:200,json:async()=>({content:{sha:'e'.repeat(40)},commit:{sha:'f'.repeat(40)}})});
  await T.saveRules(els.btnSaveRules);T.setToken('');

  // 运行共享状态脚本而非桩：所有已删除节点查询必须返回null。
  const requests=[],reloads=[];
  let snapshot={run_id:'offline-run',source_sha:'c'.repeat(40),started_at:new Date().toISOString(),
    config_files:{'holdings.json':'a'.repeat(40),'settings.json':'b'.repeat(40)},summary:{total:1},actual_dates:{max:'2026-10-22'}};
  let configConclusion='success';
  const runtime={document:doc,location:{origin:location.origin,href:location.origin+location.pathname,replace:u=>reloads.push(u)},
    localStorage:{getItem:()=>null,setItem(){},removeItem(){}},
    setTimeout:()=>1,clearTimeout(){},AbortController,URL,TextEncoder,
    crypto:{randomUUID:()=> 'offline-request'},
    DOMParser:class {parseFromString(){return {querySelector:()=>({textContent:JSON.stringify(snapshot)})};}},
    fetch:async(url,options)=>{
      requests.push({url,method:options.method});
      let response;
      if(url.includes('dispatches')){snapshot={...snapshot,request_id:'offline-request'};return {ok:true,status:204};}
      if(url.includes('/contents/settings.json'))response={sha:'b'.repeat(40)};
      else if(url.includes('/actions/'))response={workflow_runs:[
        {id:'offline-run',event:'push',head_sha:'c'.repeat(40),status:'completed',conclusion:configConclusion},
        {id:'offline-run',event:'workflow_dispatch',display_title:'offline-request',status:'completed',conclusion:'success'}]};
      else if(url.includes('status.json'))response=snapshot;
      else if(url.includes('index.html'))return {ok:true,status:200,text:async()=>'<mock-snapshot>'};
      else if(url.includes('/commits'))response=[];
      else throw new Error('未mock的请求 '+url);
      return {ok:true,status:200,json:async()=>response};
    }};
  runtime.window=runtime;
  vm.runInNewContext(fs.readFileSync(runStatusScriptPath,'utf8'),runtime);
  runtime.MMRunStatus.init({...runOptions,getToken:()=> 'github_pat_memory_only'});
  const beforeRefresh=Object.fromEntries(foldIds.map(id=>[id,els[id].open]));
  const published=await runtime.MMRunStatus.refresh();
  check(published.verified&&published.publication.phase==='published'&&removedIds.every(id=>doc.getElementById(id)===null),
    '真实运行脚本在生效与运行详情DOM缺失时仍完成刷新和发布校验');
  check(foldIds.every(id=>els[id].open===beforeRefresh[id])&&reloads.length===0,
    '真实状态刷新不新增节点、不展开或重载手动折叠项');
  const receipt=await runtime.MMRunStatus.watchConfig({file:'settings.json',blobSHA:'b'.repeat(40),commitSHA:'c'.repeat(40)});
  check(receipt.config.phase==='published'&&receipt.pending['settings.json'].blobSHA==='b'.repeat(40),
    '无生效卡片时真实提交跟踪仍双验证配置与看板快照');
  snapshot={...snapshot,source_sha:'d'.repeat(40),started_at:'2026-01-01T00:00:00Z',
    config_files:{...snapshot.config_files,'settings.json':'d'.repeat(40)}};
  configConclusion='failure';
  const failed=await runtime.MMRunStatus.watchConfig({file:'settings.json',blobSHA:'b'.repeat(40),commitSHA:'c'.repeat(40)});
  check(failed.config.phase==='failed', '无详情DOM时运行失败状态仍正常处理、不误报生效');
  const manual=await runtime.MMRunStatus.startManual();
  check(requests.filter(r=>r.method==='POST').length===1&&manual.manual.phase==='published',
    '共享实际运行功能仍按request_id派发和校验（请求全部模拟）');
  // 旧规则书签应正常初始化设置页，不跳到首页，也不写入真实存储。
  const bootstrapRequests=[],redirects=[];
  const beforeBootstrapStore=JSON.stringify(store);
  const savedLocation={search:location.search,hash:location.hash,replace:location.replace};
  const previousFetch=global.fetch;
  global.location.search='?rules=1';global.location.hash='#signalRules';
  global.location.replace=url=>redirects.push(url);
  global.fetch=async(url,options)=>{
    bootstrapRequests.push({url,method:options?.method||'GET'});
    if(url==='./groups.json')return {ok:true,json:async()=>JSON.parse(fs.readFileSync(path.join(ROOT,'groups.json'),'utf8'))};
    if(url==='./symbol-names.json')return {ok:true,json:async()=>({})};
    const payload=url.includes('/contents/holdings.json')?{positions:{AAPL:{note:'苹果'}},focus:{}}:
      url.includes('/contents/settings.json')?expectedDefaults:null;
    if(!payload)throw new Error('初始化未mock的请求 '+url);
    return {ok:true,status:200,json:async()=>({content:Buffer.from(JSON.stringify(payload)).toString('base64'),sha:'a'.repeat(40)})};
  };
  T.setToken('github_pat_bootstrap_memory_only');
  await T.bootstrap();await new Promise(resolve=>setImmediate(resolve));
  check(redirects.length===0&&bootstrapRequests.some(r=>r.url==='./groups.json')&&
    bootstrapRequests.some(r=>r.url.includes('/contents/holdings.json'))&&
    bootstrapRequests.some(r=>r.url.includes('/contents/settings.json')),
    '携带?rules=1和规则锚点仍正常加载设置页，不执行旧规则跳转');
  check(groupKeys.every(k=>els['fold_'+k].open===false)&&els.setToggle.open,
    '完整初始化后清单默认关闭，参数普通刷新仍保留手动展开状态');
  check(bootstrapRequests.every(r=>r.method==='GET')&&JSON.stringify(store)===beforeBootstrapStore&&
    removedIds.every(id=>doc.getElementById(id)===null), '旧书签初始化只模拟GET、不写存储、不重建已删除说明和详情DOM');
  T.setToken('');global.fetch=previousFetch;
  Object.assign(global.location,savedLocation);
  await runDraftRecovery();
  console.log();
  if (fails) { console.error(fails + ' 项失败'); process.exitCode = 1; }
  else console.log('全部通过');
})().catch(error => { console.error(error); process.exitCode = 1; });
}
