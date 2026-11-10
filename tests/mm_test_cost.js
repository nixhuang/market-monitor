'use strict';
// 完整执行真实面板脚本；仅注入可观察接口，不复制被测函数，不访问网络或真实浏览器存储。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { performance } = require('node:perf_hooks');
const SOURCE = fs.readFileSync(path.join(__dirname, '..', 'cost_panel.js'), 'utf8');
// Ignore formatting-only changes; keep descendant spaces and CSS token values.
const CSS = fs.readFileSync(path.join(__dirname, '..', 'cost_panel.css'), 'utf8').replace(/\/\*[\s\S]*?\*\//g,'').replace(/\s*([{}:;,])\s*/g,'$1');
const ASSETS = fs.readFileSync(path.join(__dirname, '..', 'page_assets.py'), 'utf8');
const COST_PANEL_HTML = ASSETS.match(/COST_PANEL_HTML = r'''([\s\S]*?)'''/)[1];
const LOCAL = 'mm_position_costs_local_v1';

// 取出 CSS 里「不在任何 @media 块内」的选择器（两档共用的基础规则），用来守跨端一致性
function topLevel(css) {
  const out = []; let depth = 0, buf = '';
  for (const ch of css) {
    if (ch === '{') { if (depth === 0) out.push(buf); buf = ''; depth++; continue; }
    if (ch === '}') { depth = Math.max(0, depth - 1); buf = ''; continue; }
    if (depth === 0) buf += ch;
  }
  return out.join('\n');
}
const CLOUD = 'mm_position_costs_cloud_v1';
const TOKEN = 'mm_gh_token_v1';
const PUSH_AT = 'mm_position_last_push_v2';
const PENDING = 'mm_position_pending_v1';
const SESSION_LOCAL = 'mm_position_costs_preview_v2';
const NOW = Date.parse('2026-10-20T12:00:00Z');
const SEED = {items:{AAA:{cost:100,qty:5},BBB:{cost:90,qty:2}},cash:30,fixed:20,fixed_cost:15};
const copy = v => JSON.parse(JSON.stringify(v));
const decode = s => s.replace(/&quot;/g,'"').replace(/&#39;/g,"'").replace(/&lt;/g,'<').replace(/&gt;/g,'>').replace(/&amp;/g,'&');

class Element {
  constructor(tag, doc) {
    this.tagName=tag.toUpperCase(); this.doc=doc; this.children=[]; this.attrs={};
    this.style={}; this.events={}; this.hidden=false; this.value=''; this.validationMessage='';
    this.selectionStart=0; this.selectionEnd=0; this.selectionDirection='none';
    this.classList={
      remove:(...tokens)=>{this.className=this.className.split(/\s+/).filter(x=>x&&!tokens.includes(x)).join(' ');},
      add:(...tokens)=>{this.className=[...new Set([...this.className.split(/\s+/).filter(Boolean),...tokens])].join(' ');},
      contains:token=>this.className.split(/\s+/).includes(token),
      toggle:(token,force)=>{const on=force===undefined?!this.classList.contains(token):!!force;if(on)this.classList.add(token);else this.classList.remove(token);return on;}
    };
  }
  get id(){return this.attrs.id||'';} set id(v){this.attrs.id=v;}
  get className(){return this.attrs.class||'';} set className(v){this.attrs.class=v;}
  get open(){return Object.hasOwn(this.attrs,'open');} set open(v){if(v)this.attrs.open='';else delete this.attrs.open;}
  showModal(){this.open=true;}
  close(){this.open=false;}
  get firstChild(){return this.children[0]||null;}
  get parentElement(){return this.parentNode||null;}
  get nodeType(){return this.tagName==='#TEXT'?3:1;}
  get childNodes(){return this.children;}
  get dataset(){return new Proxy(Object.fromEntries(Object.entries(this.attrs).filter(([k])=>k.startsWith('data-'))
    .map(([k,v])=>[k.slice(5).replace(/-([a-z])/g,(_,c)=>c.toUpperCase()),v])),{
      set:(data,key,value)=>{data[key]=String(value);this.setAttribute('data-'+key.replace(/[A-Z]/g,c=>'-'+c.toLowerCase()),value);return true;}
    });}
  append(...nodes){for(const node of nodes)this.appendChild(typeof node==='string'?this.doc.createTextNode(node):node);}
  get nextSibling(){if(!this.parentNode)return null;return this.parentNode.children[this.parentNode.children.indexOf(this)+1]||null;}
  get nextElementSibling(){let n=this.nextSibling;while(n&&n.tagName==='#TEXT')n=n.nextSibling;return n;}
  get previousElementSibling(){if(!this.parentNode)return null;return this.parentNode.children[this.parentNode.children.indexOf(this)-1]||null;}
  appendChild(n){if(n.parentNode)n.parentNode.removeChild(n);this.children.push(n);n.parentNode=this;return n;}
  insertBefore(n,b){
    if(b&&(b.parentNode!==this||!this.children.includes(b))){
      const error=new Error('参照节点必须是当前父节点的直接子节点');error.name='NotFoundError';throw error;
    }
    if(n===b)return n;
    if(!b)return this.appendChild(n);
    if(n.parentNode)n.parentNode.removeChild(n);
    this.children.splice(this.children.indexOf(b),0,n);n.parentNode=this;return n;
  }
  removeChild(n){this.children.splice(this.children.indexOf(n),1);n.parentNode=null;}
  replaceChild(n,o){this.insertBefore(n,o);this.removeChild(o);}
  remove(){if(this.parentNode)this.parentNode.removeChild(this);}
  cloneNode(deep=false){
    const n=new Element(this.tagName,this.doc);n.attrs={...this.attrs};n.text=this.text;n.hidden=this.hidden;
    if(deep)for(const child of this.children)n.appendChild(child.cloneNode(true));
    return n;
  }
  setAttribute(k,v){this.attrs[k]=String(v);if(k==='value')this.value=decode(String(v));}
  getAttribute(k){return Object.hasOwn(this.attrs,k)?this.attrs[k]:null;}
  hasAttribute(k){return Object.hasOwn(this.attrs,k);}
  removeAttribute(k){delete this.attrs[k];}
  get textContent(){return this.tagName==='#TEXT'?this.text:this.children.map(c=>c.textContent).join('');}
  set textContent(v){if(this.tagName==='#TEXT'){this.text=String(v);return;}for(const n of this.children)n.parentNode=null;this.children=[];if(String(v)){let n=new Element('#text',this.doc);n.text=String(v);this.appendChild(n);}}
  get innerHTML(){return this._html||'';}
  set innerHTML(html){
    this._html=html;for(const n of this.children)n.parentNode=null;this.children=[];this.doc.renders++;
    this.htmlWrites=(this.htmlWrites||0)+1;
    const stack=[this];
    for(const t of String(html).match(/<[^>]+>|[^<]+/g)||[]){
      if(t.startsWith('</')){if(stack.length>1)stack.pop();continue;}
      if(t.startsWith('<')){
        const name=t.match(/^<([\w-]+)/);if(!name)continue;
        const n=new Element(name[1],this.doc);
        const attrs=t.slice(name[0].length,-1);
        for(const m of attrs.matchAll(/([\w-]+)(?:="([^"]*)")?/g))n.setAttribute(m[1],decode(m[2]||''));
        stack.at(-1).appendChild(n);
        if(!['input','br','img','hr','col'].includes(name[1]))stack.push(n);
      }else{const n=new Element('#text',this.doc);n.text=decode(t);stack.at(-1).appendChild(n);}
    }
  }
  matches(selector){
    return selector.split(',').some(s=>{
      s=s.trim();let invalid=false;
      if(s.endsWith(':invalid')){invalid=true;s=s.slice(0,-8);}
      if(invalid&&!this.validationMessage)return false;
      if(s.endsWith(':checked')){if(!this.checked)return false;s=s.slice(0,-8);}
      const attrs=[...s.matchAll(/\[([\w-]+)(\^?=)?(?:["']([^"']*)["'])?\]/g)];
      s=s.replace(/\[[^\]]+\]/g,'');
      for(const a of attrs){let v=this.getAttribute(a[1]);if(v===null)return false;if(a[2]==='='&&v!==a[3])return false;if(a[2]==='^='&&!v.startsWith(a[3]))return false;}
      const tag=s.match(/^[\w-]+/);if(tag&&this.tagName!==tag[0].toUpperCase())return false;
      for(const id of s.matchAll(/#([\w-]+)/g))if(this.id!==id[1])return false;
      for(const cls of s.matchAll(/\.([\w-]+)/g))if(!this.className.split(/\s+/).includes(cls[1]))return false;
      return this.tagName!=='#TEXT';
    });
  }
  querySelectorAll(selector){
    let out=[];
    const all=[];const walk=n=>{for(const c of n.children){all.push(c);walk(c);}};walk(this);
    for(const group of selector.split(',')){
      const tokens=group.trim().replace(/>/g,' > ').split(/\s+/);
      for(const el of all){
        let i=tokens.length-1, n=el;
        if(!n.matches(tokens[i--]))continue;
        while(i>=0){
          if(tokens[i]==='>'){n=n.parentNode;i--;if(!n||!n.matches(tokens[i--])){n=null;break;}}
          else{n=n.parentNode;while(n&&!n.matches(tokens[i]))n=n.parentNode;if(!n)break;i--;}
        }
        if(n&&i<0&&!out.includes(el))out.push(el);
      }
    }
    return out;
  }
  querySelector(s){return this.querySelectorAll(s)[0]||null;}
  closest(s){let n=this;while(n){if(n.matches(s))return n;n=n.parentNode;}return null;}
  contains(n){while(n){if(n===this)return true;n=n.parentNode;}return false;}
  addEventListener(k,fn){(this.events[k]||=[]).push(fn);}
  dispatchEvent(e){if(!e.target)e.target=this;for(const fn of this.events[e.type]||[])fn.call(this,e);if(e.bubbles&&!e.propagationStopped&&this.parentNode)this.parentNode.dispatchEvent(e);return !e.defaultPrevented;}
  fire(k,props={}){return this.dispatchEvent(Object.assign(new MockCustomEvent(k),props));}
  click(){this.fire('click');}
  focus(){this.doc.activeElement=this;}
  setSelectionRange(a,b,d){this.selectionStart=a;this.selectionEnd=b;this.selectionDirection=d;}
  setCustomValidity(s){this.validationMessage=s;}
}
class MockCustomEvent {
  constructor(type,opts={}){this.type=type;this.detail=opts.detail??null;this.cancelable=!!opts.cancelable;this.defaultPrevented=false;this.bubbles=opts.bubbles??['click','input','keydown'].includes(type);this.propagationStopped=false;}
  preventDefault(){if(this.cancelable)this.defaultPrevented=true;}
  stopPropagation(){this.propagationStopped=true;}
}
const fixtureAnchor=sym=>'s-'+sym.replace(/[^0-9A-Za-z]+/g,'-').replace(/^-+|-+$/g,'').toLowerCase();
function makeDocument(opts={}){
  const doc={renders:0,activeElement:null,events:{}};
  doc.body=new Element('body',doc);doc.documentElement=doc.body;
  doc.createElement=t=>new Element(t,doc);
  doc.createTextNode=text=>{const node=new Element('#text',doc);node.text=String(text);return node;};
  doc.querySelectorAll=s=>doc.body.querySelectorAll(s);doc.querySelector=s=>doc.body.querySelector(s);
  doc.getElementById=id=>doc.querySelector('#'+id);
  doc.addEventListener=(k,f)=>(doc.events[k]||=[]).push(f);
  doc.dispatchEvent=Element.prototype.dispatchEvent;
  doc.fire=(k,e={})=>doc.dispatchEvent(Object.assign(new MockCustomEvent(k),e));
  /* 2026-11-02 起「编辑」(#costEditBtn) 由 monitor 服务端输出在顶栏 .runbar 里（「刷新」右边），
     不再是仓位卡的一部分 —— 夹具照真实页面结构补出这一排，否则 JS 按 document 找不到按钮，
     编辑态那条链路在测试里会退化成「无按钮」分支（真实页面恒有，见 mm_test_amp 的顺序断言）。 */
  doc.body.innerHTML='<div class="runbar"><button class="primary" id="btnRunNow">运行</button>'
    +'<button id="btnCheckStatus">状态</button><button id="btnHardRefresh">刷新</button>'
    +'<button type="button" class="cost-edit-btn" id="costEditBtn" aria-pressed="false" disabled>编辑</button></div>'
    +'<div class="dual-row"><details id="dayPnlCard"><summary><b class="dp-amt"></b><b class="dp-pct"></b></summary></details><div class="dual-body"><ul class="cost-contrib" data-owner="day"></ul></div></div>'
    +COST_PANEL_HTML+'<details id="group_positions"><summary><span class="grp-name"></span><span class="group-stats"></span></summary><div class="cost-foot asset-row cost-manage"></div><div class="cashbond-row"><span class="cb-total-amt"></span><span class="cb-total-pnl"></span><span class="cb-cash"><span class="cb-amt"></span></span><span class="cb-bond"><span class="cb-amt"></span></span></div><table></table></details><div id="signalCounts"><div class="signal-extra-row pnl-extra"><details><summary><span class="pnl-chip">盈亏到价</span><span class="pnl-count">0</span></summary><div class="signal-member-block" hidden><h5>浮盈</h5><ul class="signal-member-list" data-g="gain"></ul></div><div class="signal-member-block" hidden><h5>浮亏</h5><ul class="signal-member-list" data-g="loss"></ul></div><div class="signal-member-block" hidden><h5>减仓价</h5><ul class="signal-member-list" data-g="cut"></ul></div><div class="signal-member-block" hidden><h5>加仓价</h5><ul class="signal-member-list" data-g="add"></ul></div></details></div></div>';
  const table=doc.querySelector('table');table.className='stk';
  for(const sym of opts.symbols||['AAA','BBB']){
    const tr=new Element('tr',doc);tr.id=fixtureAnchor(sym);tr.className='green';
    if(!opts.legacySymbols)tr.setAttribute('data-symbol',sym);
    tr.setAttribute('data-price','100');
    if(opts.quoteDate!==null)tr.setAttribute('data-quote-date',opts.quoteDate||'2026-10-20');
    tr.innerHTML='<td class="sym">'+sym+'<span class="note" data-full="测试名称">测试名称</span></td><td class="px"><span class="px-price">100.00</span><span class="px-chg">+1.00%</span></td><td class="tgs"></td><td class="lamp-earn"></td><td class="cost-line"></td><td class="sig"><span class="sig-col"><span class="lvtag add signal-item" data-lv="95" data-near="0"><span class="fsz">加仓价 95 · 距 5.0%</span></span></span><span class="sig-col"><span class="lvtag cut signal-item" data-lv="104" data-near="0"><span class="fsz">减仓价 104 · 距 4.0%</span></span></span></td>';
    table.appendChild(tr);
    const rail=new Element('tr',doc);rail.className='range52-row';rail.innerHTML='<td><span class="range52-rail" aria-label="50.00低，本轮价 100.00 位于区间 50%，本轮高 150.00"><span class="range52-track"></span></span></td>';table.appendChild(rail);
  }
  for(const [sym,group] of Object.entries(opts.groups||{}))moveRow(doc,sym,group);
  if(opts.withSub){const sub=doc.createElement('div');sub.className='sub';doc.body.insertBefore(sub,doc.body.firstChild);}
  if(opts.prepareDocument)opts.prepareDocument(doc);
  return doc;
}
function moveRow(doc,sym,group){
  let tr=doc.getElementById(fixtureAnchor(sym));assert.ok(tr);
  let rail=tr.nextElementSibling;
  if(group===null){tr.remove();if(rail)rail.remove();return;}
  let root=doc.getElementById('group_'+group);
  if(!root){root=doc.createElement('details');root.id='group_'+group;root.innerHTML='<summary></summary><table></table>';doc.body.appendChild(root);}
  let table=root.querySelector('table');table.appendChild(tr);if(rail)table.appendChild(rail);
}
function shared(initial={}){
  const values=new Map(Object.entries(initial).map(([k,v])=>[k,typeof v==='string'?v:JSON.stringify(v)]));
  return {values,failGet:new Set(),failSet:new Set(),writes:[],holder:false,
    locks:{request(name,opts,cb){return Promise.resolve().then(()=>{
      if(this.shared.holder)return cb(null);
      this.shared.holder=true;return Promise.resolve(cb({name})).finally(()=>{this.shared.holder=false;});
    });}}};
}
async function settle(){for(let i=0;i<25;i++)await Promise.resolve();}
async function boot(opts={}){
  const store=opts.store||shared({[LOCAL]:opts.local||SEED,...(opts.token===false?{}:{[TOKEN]:'test-token'})});
  store.locks.shared=store;
  const doc=makeDocument(opts), events={}, dispatched=[], timers=new Map(), requests=[], readers=[], alerts=[], blobs=[];
  const session=opts.session||shared();
  const sessionStorage={getItem(k){if(session.failGet.has(k))throw Error('会话读取失败');return session.values.get(k)??null;},
    setItem(k,v){if(session.failSet.has(k))throw Error('会话配额不足');session.values.set(k,String(v));session.writes.push({k,v:String(v)});}};
  let now=opts.now??NOW, timerId=0;
  const FakeDate=class extends Date{constructor(...args){super(...(args.length?args:[now]));}static now(){return now;}};
  const localStorage={getItem(k){if(store.failGet.has(k))throw Error('禁止读取');return store.values.get(k)??null;},
    setItem(k,v){if(store.failSet.has(k))throw Error('配额不足');store.values.set(k,String(v));store.writes.push({k,v:String(v)});},
    removeItem(k){if(store.failSet.has(k))throw Error('禁止删除');store.values.delete(k);}};
  const setTimeout=(fn,delay=0)=>{let id=++timerId;timers.set(id,{fn,at:now+delay});return id;};
  const ctx={console,document:doc,localStorage,sessionStorage,navigator:opts.noLocks?{}:{locks:store.locks},location:{search:'',protocol:opts.protocol||'https:'},Date:FakeDate,
    TextEncoder,TextDecoder,Uint8Array,Blob,AbortController,URL:{createObjectURL:blob=>{blobs.push(blob);return 'blob:mock';},revokeObjectURL:()=>{}},
    btoa:s=>Buffer.from(s,'binary').toString('base64'),atob:s=>Buffer.from(s,'base64').toString('binary'),
    setTimeout,clearTimeout:id=>timers.delete(id),requestAnimationFrame:fn=>setTimeout(fn,16),cancelAnimationFrame:id=>timers.delete(id),
    FileReader:class{constructor(){readers.push(this);}readAsText(){}},
    fetch:(url,init={})=>new Promise((resolve,reject)=>{requests.push({url,init,resolve,reject,at:now});assert.ok(!opts.forbidRequests,'隔离成本统计不允许发起请求');}),
    CustomEvent:MockCustomEvent,
    addEventListener:(k,f)=>(events[k]||=[]).push(f),
    dispatchEvent:e=>{dispatched.push(e);if(!e.target)e.target=ctx;for(const f of events[e.type]||[])f.call(ctx,e);return !e.defaultPrevented;},
    alert:s=>alerts.push(s),prompt:()=>null,confirm:s=>{alerts.push(s);return opts.confirm===true;},
    __MM_RULES__:opts.rules};
  ctx.window=ctx;
  const hooks=`globalThis.__costTest={money,plain,qtyInt,trim,trim4,normalize,canonical,mergeCloud,requestSave,doPush,startPull,enterEdit,exitEdit,importData,exportData,cashFirst,nearPrice,pnlTone,paint,openClear,markClear,executeClear,openAdd,addPosition,removeAdded,clearDestination,clearConfig,
    clearState:()=>({record:clearRecord&&clone(clearRecord),busy:clearBusy,draft:clone(clearDraft),additions:clone(addDraft),added:clone(addedPositions)}),
    state:()=>({DATA:clone(DATA),CLOUD:CLOUD&&clone(CLOUD),revision,dirty,cloudVerified,cloudFirstPending,positionsAuthorized,positionSave:positionSave&&clone(positionSave),positionGeneration,draftPending,uncertain:uncertain&&clone(uncertain),deadline:REQUEST_DEADLINE,activeRequests:activeRequests.length,editing,inFlight:inFlight&&clone(inFlight),queued,writer,SYNC,NOTICE,savePaused,storageErrors:clone(storageErrors)}),
    rows:ROWS};`;
  const code=SOURCE.replace(/^<script>\s*/,'').replace(/\s*<\/script>\s*$/,'').replace('__MM_REAL__',JSON.stringify(opts.seed||SEED))
    .replace(/\}\)\(\);\s*$/,hooks+'})();');
  vm.createContext(ctx);vm.runInContext(code,ctx,{filename:'cost_panel.js'});await settle();
  const t=ctx.__costTest;
  async function tick(ms=0){const end=now+ms;let n=0;while(true){await settle();let candidate=[...timers].filter(([,v])=>v.at<=end).sort((a,b)=>a[1].at-b[1].at)[0];if(!candidate)break;if(++n>500)throw Error('定时器循环');now=candidate[1].at;timers.delete(candidate[0]);candidate[1].fn();}now=end;await settle();}
  function respond(r,data,status=200){r.resolve({ok:status>=200&&status<300,status,json:async()=>data});}
  function edit(sym='AAA',field='qty',value='1'){
    const panel=doc.getElementById('costManage');const el=panel.querySelector('input[data-sym="'+sym+'"][data-f="'+field+'"]');
    assert.ok(el,'必须存在真实渲染输入框 '+sym+'/'+field);el.value=value;el.focus();el.setSelectionRange(value.length,value.length,'none');panel.fire('input',{target:el});return el;
  }
  const api={...t,clearState:()=>copy(t.clearState()),doc,ctx,store,session,requests,readers,alerts,blobs,tick,respond,edit,events,dispatched,
    move:(sym,group)=>{moveRow(doc,sym,group);t.paint();},
    sessionLocal:()=>JSON.parse(session.values.get(SESSION_LOCAL)),
    fireWindow:(k,e={})=>{for(const f of events[k]||[])f(e);},
    enter:async()=>{t.enterEdit();await settle();},
    save:async()=>{doc.getElementById('costSave').click();await tick(0);},
    ack:(key)=>{let el=new Element('button',doc);el.setAttribute('data-ack',key);doc.fire('click',{target:el});},
    local:()=>JSON.parse(store.values.get(LOCAL)),
    puts:()=>requests.filter(r=>r.init.method==='PUT'),
    payload:r=>JSON.parse(Buffer.from(JSON.parse(r.init.body).content,'base64').toString()),
    dispose:async()=>{api.fireWindow('pagehide');await settle();}};
  return api;
}
async function finishPush(b,status=200){
  const get=b.requests.filter(r=>r.init.method!=='PUT').at(-1);
  b.respond(get,{sha:'fixture-sha',content:Buffer.from(JSON.stringify(SEED)).toString('base64')});await settle();
  const put=b.puts().at(-1);assert.ok(put);b.respond(put,status===200?{}:{message:'模拟失败'},status);await settle();return put;
}
const tests=[];function test(name,fn){tests.push({name,fn});}
function assertReadOnlySmall(b){
  const panel=b.doc.getElementById('costManage');
  assert.equal(b.doc.getElementById('costSyncState').textContent,'本页只读');
  assert.equal(panel.classList.contains('cost-edit-open'),false);
  assert.equal(b.doc.getElementById('costActs').hidden,true);
  assert.equal(b.doc.getElementById('costNotice').hidden,true);
  assert.equal(b.doc.getElementById('costEditBtn').getAttribute('aria-pressed'),'false');
  assert.equal(b.state().editing,false);assert.equal(b.state().writer,false);
  assert.equal(b.state().positionsAuthorized,false);assert.equal(b.puts().length,0);
}
test('锁只读仅顶部小状态无框，原接管/取消路径读取最新仓位且无PUT',async()=>{
  assert.match(CSS,/\.cost-state \.warn,\.cost-state \.ok,\.cost-state \.dim\{background:none;border:0;padding:0;margin:0;border-radius:0/);
  const owner=await boot({withSub:true});owner.respond(owner.requests[0],meta(SEED));await settle();
  const b=await boot({store:owner.store,withSub:true});assertReadOnlySmall(b);
  for(let i=0;i<4;i++){await b.enter();b.fireWindow('focus');b.doc.fire('visibilitychange');}
  assertReadOnlySmall(b);assert.equal(b.requests.length,0);
  await owner.dispose();assert.equal(b.state().writer,false);assert.equal(b.requests.length,0,'不新增后台waiter或自动接管');
  await b.enter();assert.equal(b.state().writer,true);assert.equal(b.state().editing,true);assert.equal(b.requests.length,1);
  b.doc.getElementById('costCancel').click();await settle();assert.equal(b.state().editing,false);
  const remote={...copy(SEED),cash:333,items:{AAA:{qty:20,cost:12}}};b.respond(b.requests.at(-1),meta(remote));await settle();
  assert.equal(b.state().DATA.cash,333);assert.equal(b.state().DATA.items.AAA.qty,20);assert.equal(b.state().DATA.items.BBB,undefined);
  assert.equal(b.state().positionSave,null);assert.equal(b.state().positionsAuthorized,false);assert.equal(b.state().queued,false);
  await b.tick(180000);assert.equal(b.puts().length,0);
  assert.equal(b.doc.getElementById('costSyncState').textContent,'仓位已同步');
  await b.enter();b.edit('AAA','qty','88');b.doc.getElementById('costCancel').click();
  assert.equal(b.state().DATA.items.AAA.qty,20);assert.equal(b.doc.getElementById('costManage').classList.contains('cost-edit-open'),false);
  await b.enter();assert.equal(b.state().editing,true);assert.equal(b.state().positionsAuthorized,false);await b.dispose();
});
test('锁request拒绝也仅顶部本页只读无框，数据读取错误仍可见且无请求授权',async()=>{
  for(const message of ['SecurityError','AbortError','无法保持跨标签页写锁']){
    const store=shared({[LOCAL]:SEED});store.locks.request=()=>Promise.reject(Error(message));
    const b=await boot({store,withSub:true});assertReadOnlySmall(b);
    assert.equal(b.state().NOTICE,'本页只读');assert.equal(b.requests.length,0);
    await b.enter();assertReadOnlySmall(b);assert.equal(b.requests.length,0);
    store.failGet.add(LOCAL);b.fireWindow('storage',{key:LOCAL});
    assert.match(b.doc.getElementById('costNotice').textContent,/本地数据读取失败/);
    assert.equal(b.doc.getElementById('costNotice').hidden,false);assert.equal(b.state().editing,false);
    store.failGet.clear();b.fireWindow('storage',{key:LOCAL});assertReadOnlySmall(b);
    await b.dispose();assert.equal(b.state().writer,false);assert.equal(b.puts().length,0);
  }
});
test('只读真正存储和GET校验错误仍可见，不能当普通锁提示丢弃',async()=>{
  const owner=await boot(),b=await boot({store:owner.store,withSub:true});assertReadOnlySmall(b);
  owner.store.failGet.add(LOCAL);b.fireWindow('storage',{key:LOCAL});
  assert.match(b.doc.getElementById('costNotice').textContent,/本地数据读取失败/);
  assert.equal(b.doc.getElementById('costNotice').hidden,false);assert.equal(b.doc.getElementById('costActs').hidden,false);
  assert.equal(b.state().editing,false);owner.store.failGet.clear();b.fireWindow('storage',{key:LOCAL});assertReadOnlySmall(b);
  await owner.dispose();await b.enter();b.doc.getElementById('costCancel').click();await settle();
  b.respond(b.requests.at(-1),meta({items:{AAA:{qty:-1}}}));await settle();
  assert.match(b.doc.getElementById('costNotice').textContent,/云端数据校验失败/);
  assert.equal(b.doc.getElementById('costNotice').hidden,false);assert.equal(b.puts().length,0);await b.dispose();
});
const meta = (data,sha='remote-sha') => ({sha,content:Buffer.from(JSON.stringify(data)).toString('base64')});
const staleAck = () => ({...copy(SEED),acks:{'AAA|loss10':'2026-10-19T00:00:00Z'},
  ack_contexts:{'AAA|loss10':{at:'2026-10-19T00:00:00Z',quote_date:'2026-10-18',rule_version:'["cost-ack-v1","loss10",10,null]'}}});
const freshRemote = () => {let d=staleAck();d.cash=9876;d.items.AAA.qty=50;return d;};
async function beginPut(b){await b.enter();b.edit();await b.save();b.respond(b.requests.at(-1),meta(SEED,'base-sha'));await settle();return b.puts().at(-1);}
function pendingBody(b,request,status=200){
  let resolve;const body=new Promise(r=>{resolve=r;});request.resolve({ok:status>=200&&status<300,status,json:()=>body});return resolve;
}
function mockRemote(data,sha='base-sha'){
  let revision=0;
  return {data:copy(data),sha,
    apply(b,request){
      const body=JSON.parse(request.init.body);
      if((body.sha||null)!==this.sha){b.respond(request,{message:'SHA conflict'},409);return false;}
      this.data=b.payload(request);this.sha='committed-'+(++revision);b.respond(request,{});return true;
    }};
}

for(const [name,field] of [['数量','qty'],['成本','cost']])test(name+'零值刷新不补种子',async()=>{
  const d=copy(SEED);d.items.AAA[field]=0;const b=await boot({local:d,token:false});assert.equal(b.state().DATA.items.AAA[field],0);await b.dispose();
  const c=await boot({store:b.store,seed:{...SEED,items:{AAA:{cost:999,qty:999}}}});assert.equal(c.state().DATA.items.AAA[field],0);
});
for(const f of ['cash','fixed','fixed_cost'])test(f+'零值刷新不回填',async()=>{let b=await boot({local:{...SEED,[f]:0}});assert.equal(b.state().DATA[f],0);});
test('旧备份没有real不被种子覆盖',async()=>{let d=copy(SEED);d.items.AAA.qty=7;let b=await boot({local:d});assert.equal(b.state().DATA.items.AAA.qty,7);assert.equal(b.local().real,true);});
test('旧版本只补缺字段并记录版本',async()=>{let d=copy(SEED);delete d.items.AAA.qty;delete d.cash;let b=await boot({local:d});assert.equal(b.local().items.AAA.qty,5);assert.equal(b.local().cash,30);assert.equal(b.local().schema_version,2);});
test('新版缺项不复活种子标的',async()=>{let b=await boot({local:{schema_version:2,items:{AAA:{cost:0,qty:0}},cash:0,fixed:0,fixed_cost:0}});assert.equal(b.state().DATA.items.BBB,undefined);});
test('同页未保存dirty草稿整份保留，云端不补零或缺项',async()=>{let b=await boot();await b.enter();b.edit('AAA','qty','0');b.edit('USD','cash','0');b.exitEdit(false);b.mergeCloud({...SEED,items:{AAA:{cost:555,qty:9},CCC:{cost:1,qty:1}}});assert.equal(b.state().DATA.items.AAA.qty,0);assert.equal(b.state().DATA.cash,0);assert.equal(b.state().DATA.items.CCC,undefined);});
test('clean云端仍按整份替换而不是合并持仓',async()=>{let b=await boot();b.mergeCloud({items:{AAA:{cost:1,qty:0}},cash:0});assert.equal(b.state().DATA.items.BBB,undefined);assert.equal(b.state().DATA.cash,0);});

// 刷新云端优先：以下请求与存储均来自 boot 的真实脚本 mock，不访问真实数据。
const cloudNumbers = d => copy({items:d.items,cash:d.cash,cash_extra:d.cash_extra,fixed:d.fixed,fixed_cost:d.fixed_cost});
for(const residue of ['dirty','uncertain','legacy','clean-pending'])for(const shape of ['双现金删除零值','全零空仓'])test('刷新'+residue+'云端全量覆盖'+shape+'且不补传',async()=>{
  const local={...copy(SEED),cash:300,cash_extra:100,_local:{revision:41,dirty:residue!=='clean-pending'}};
  if(residue==='uncertain')local._local.uncertain={payload:{...copy(SEED),cash:777},revision:40,baseSha:'old-sha'};
  if(residue==='legacy')delete local._local;
  const store=shared({[LOCAL]:local,[CLOUD]:SEED,[PENDING]:1,[TOKEN]:'test-token',[PUSH_AT]:NOW-1000});
  const b=await boot({store}),remote={schema_version:2,items:shape==='全零空仓'?{}:{AAA:{cost:0,qty:0},NEW:{cost:12,qty:3}},
    cash:shape==='全零空仓'?0:70,cash_extra:shape==='全零空仓'?0:20,fixed:0,fixed_cost:0};
  assert.equal(b.state().SYNC,'pulling');assert.equal(b.state().positionsAuthorized,false);
  assert.match(b.requests[0].url,/ref=data/);b.respond(b.requests[0],meta(remote,'fresh-sha'));await settle();
  assert.deepEqual(cloudNumbers(b.state().DATA),cloudNumbers(b.normalize(remote)));
  assert.deepEqual(cloudNumbers(b.local()),cloudNumbers(b.normalize(remote)));
  assert.equal(b.local().items.BBB,undefined);assert.equal(b.state().dirty,false);assert.equal(b.state().uncertain,null);
  assert.equal(b.local()._local.uncertain,null);
  assert.equal(store.values.get(PENDING),'0');assert.equal(store.values.get(PUSH_AT),String(NOW-1000));
  assert.equal(b.state().queued,false);assert.equal(b.state().SYNC,'synced');await b.tick(180000);
  assert.equal(b.requests.length,1);assert.equal(b.puts().length,0);
});
test('刷新无token仅GET读取data分支并替换双现金',async()=>{
  const b=await boot({token:false,local:{...copy(SEED),_local:{dirty:true,revision:3}}});
  assert.equal(b.requests[0].init.headers,undefined);assert.match(b.requests[0].url,/\/data\/position-costs\.json\?t=/);
  const remote={...copy(SEED),cash:10,cash_extra:10};b.respond(b.requests[0],remote);await settle();await b.tick(180000);
  assert.equal(b.state().DATA.cash,10);assert.equal(b.cashFirst(b.state().DATA),0);assert.equal(b.state().DATA.cash_extra,10);
  assert.equal(b.state().dirty,false);assert.equal(b.state().SYNC,'synced');assert.equal(b.requests.length,1);assert.equal(b.puts().length,0);
});
test('刷新已一致的dirty和旧事务仍清理所有残留标记',async()=>{
  const local={...copy(SEED),_local:{dirty:true,revision:7,uncertain:{payload:copy(SEED),revision:7,baseSha:'old'}}};
  const store=shared({[LOCAL]:local,[TOKEN]:'test-token',[PENDING]:1}),b=await boot({store});
  b.respond(b.requests[0],meta(SEED));await settle();await b.tick(180000);
  assert.equal(b.local()._local.dirty,false);assert.equal(b.local()._local.uncertain,null);
  assert.equal(store.values.get(PENDING),'0');
  assert.equal(b.state().SYNC,'synced');assert.equal(b.puts().length,0);assert.equal(b.requests.length,1);
});
for(const token of [true,false])test('刷新读取失败旧dirty未知事务不PUT且标题优先云端未取到 token='+token,async()=>{
  const local={...copy(SEED),_local:{dirty:true,revision:5,uncertain:{payload:copy(SEED),revision:5,baseSha:'old'}}};
  const b=await boot({local,token});
  b.respond(b.requests[0],{},500);await settle();if(token){b.respond(b.requests.at(-1),{},404);await settle();}
  assert.equal(b.state().SYNC,'nocloud');assert.match(b.doc.getElementById('costState').textContent,/云端未取到/);
  assert.doesNotMatch(b.doc.getElementById('costState').textContent,/已同步/);
  await b.tick(600000);assert.equal(b.puts().length,0);assert.equal(b.state().inFlight,null);
  assert.equal(b.state().cloudVerified,false);assert.equal(b.state().SYNC,'nocloud');assert.equal(b.local().cash,30);
});
test('API读取失败后raw成功仍按云端全量覆盖旧dirty',async()=>{
  const b=await boot({local:{...copy(SEED),_local:{dirty:true,revision:9}}});b.respond(b.requests[0],{},403);await settle();
  b.respond(b.requests.at(-1),{items:{},cash:0});await settle();await b.tick(90000);
  assert.deepEqual(copy(b.local().items),{});assert.equal(b.state().dirty,false);assert.equal(b.state().SYNC,'synced');
  assert.equal(b.requests.length,2);assert.equal(b.puts().length,0);
});
test('刷新前未保存草稿保留本机，重启读取后被云端覆盖且绝不自动上传',async()=>{
  const b=await boot();b.respond(b.requests[0],meta(SEED));await settle();await b.enter();
  b.edit('AAA','qty','99');b.edit('USD','cash_extra','40');const panel=b.doc.getElementById('costManage');
  panel.open=false;panel.fire('toggle');assert.equal(b.local().items.AAA.qty,99);assert.equal(b.local().cash_extra,40);
  await b.dispose();const c=await boot({store:b.store});c.respond(c.requests[0],meta(SEED));await settle();await c.tick(180000);
  assert.equal(c.local().items.AAA.qty,5);assert.equal(c.local().cash_extra,0);assert.equal(c.local().cash,30);
  assert.equal(c.state().SYNC,'synced');assert.equal(c.puts().length,0);assert.equal(c.requests.length,1);
});
test('明确保存上传成功后重启读取远端即同步，不额外提交',async()=>{
  const b=await boot();b.respond(b.requests[0],meta(SEED));await settle();await b.enter();b.edit('AAA','qty','11');await b.save();
  const put=await finishPush(b),remote=b.payload(put);assert.equal(b.state().dirty,false);await b.dispose();
  const c=await boot({store:b.store,now:NOW+1000});c.respond(c.requests[0],meta(remote,'saved'));await settle();await c.tick(180000);
  assert.equal(c.local().items.AAA.qty,11);assert.equal(c.state().SYNC,'synced');assert.equal(c.puts().length,0);assert.equal(c.requests.length,1);
});


test('新页填token只重开读取，不补传旧dirty或未知事务',async()=>{
  const b=await boot({token:false,local:{...copy(SEED),_local:{dirty:true,revision:7}}}),old=b.requests[0];
  b.ctx.prompt=()=> 'mock-token';b.doc.getElementById('costManage').fire('click',{target:b.doc.getElementById('costState')});await settle();
  const get=b.requests.at(-1);assert.notEqual(get,old);b.respond(get,meta({...SEED,cash:900}));await settle();
  b.respond(old,SEED);await settle();await b.tick(180000);assert.equal(b.state().DATA.cash,900);
  assert.equal(b.puts().length,0);assert.equal(b.requests.length,2);assert.equal(b.state().dirty,false);
});
test('同页未保存草稿不因填token和迟到初始GET上传或回退',async()=>{
  const b=await boot({token:false});await b.enter();b.edit('AAA','qty','88');
  const panel=b.doc.getElementById('costManage');panel.open=false;panel.fire('toggle');
  b.ctx.prompt=()=> 'mock-token';panel.fire('click',{target:b.doc.getElementById('costState')});await settle();
  b.respond(b.requests.at(-1),meta({...SEED,cash:999}));await settle();b.respond(b.requests[0],SEED);await settle();await b.tick(180000);
  assert.equal(b.state().DATA.items.AAA.qty,88);assert.equal(b.local().items.AAA.qty,88);assert.equal(b.puts().length,0);
  assert.equal(b.state().dirty,true);assert.equal(b.state().positionsAuthorized,false);
});

test('初始GET迟到不得覆盖只进入编辑未输入的新会话',async()=>{
  const b=await boot();await b.enter();b.respond(b.requests[0],meta({...SEED,cash:999}));await settle();
  assert.equal(b.state().editing,true);assert.equal(b.state().DATA.cash,30);assert.equal(b.doc.querySelector('input[data-f="cash"]').value,'30');
});



test('云端校验失败不覆盖本地、不PUT也不假同步',async()=>{
  const b=await boot({local:{...copy(SEED),_local:{dirty:true,revision:12}}});
  b.respond(b.requests[0],meta({items:{AAA:{cost:1,qty:-1}},cash:999}));await settle();await b.tick(180000);
  assert.equal(b.state().DATA.cash,30);assert.equal(b.local().items.AAA.qty,5);assert.equal(b.state().SYNC,'nocloud');
  assert.match(b.doc.getElementById('costState').textContent,/云端未取到/);assert.equal(b.puts().length,0);
});
for(const key of [LOCAL,CLOUD,PENDING])test('云端覆盖时'+key+'落盘失败不假同步也不自动PUT',async()=>{
  const b=await boot({local:{...copy(SEED),_local:{dirty:true,revision:3}}});b.store.failSet.add(key);
  b.respond(b.requests[0],meta({...SEED,cash:555}));await settle();await b.tick(180000);
  assert.equal(b.state().DATA.cash,555);assert.equal(b.state().dirty,true);assert.notEqual(b.state().SYNC,'synced');
  assert.match(b.doc.getElementById('costState').textContent,/未持久化/);assert.equal(b.puts().length,0);
});
test('旧dirty读取失败后自动重试成功只采用新云端，不补传本地',async()=>{
  const b=await boot({token:false,local:{...copy(SEED),_local:{dirty:true,revision:3}}});
  b.respond(b.requests[0],{},500);await settle();assert.equal(b.state().SYNC,'nocloud');await b.tick(5000);
  b.respond(b.requests[1],{items:{},cash:0});await settle();await b.tick(180000);
  assert.equal(b.state().dirty,false);assert.equal(b.state().SYNC,'synced');assert.deepEqual(copy(b.local().items),{});
  assert.equal(b.requests.length,2);assert.equal(b.puts().length,0);
});
test('本页上传失败重启后读取较新云端覆盖失败快照，无自动重试旧PUT',async()=>{
  const b=await boot();b.respond(b.requests[0],meta(SEED));await settle();await b.enter();b.edit('AAA','qty','9');await b.save();
  await finishPush(b,500);assert.equal(b.state().SYNC,'error');assert.ok(b.local()._local.uncertain);await b.dispose();
  const c=await boot({store:b.store}),remote={...copy(SEED),cash:300,items:{AAA:{cost:100,qty:12}}};
  assert.equal(c.state().SYNC,'pulling');c.respond(c.requests[0],meta(remote));await settle();await c.tick(180000);
  assert.equal(c.state().DATA.items.AAA.qty,12);assert.equal(c.state().SYNC,'synced');assert.equal(c.state().uncertain,null);
  assert.equal(c.requests.length,1);assert.equal(c.puts().length,0);
});




test('事务生命周期 只读storage后点击编辑接管必须新GET并保护新草稿',async()=>{
  const b=await boot();b.respond(b.requests[0],meta(SEED));await settle();const c=await boot({store:b.store});
  c.fireWindow('storage',{key:LOCAL});assert.equal(c.state().writer,false);assert.equal(c.requests.length,0);await b.dispose();await c.enter();
  assert.equal(c.state().writer,true);assert.equal(c.requests.length,1,'接管点击编辑后必须开始新的云端GET');
  assert.equal(c.state().SYNC,'pulling');assert.doesNotMatch(c.doc.getElementById('costState').textContent,/已同步/);
  c.edit('AAA','qty','88');const remote={...copy(SEED),cash:999,items:{AAA:{cost:100,qty:20}}};c.respond(c.requests[0],meta(remote));await settle();
  assert.equal(c.state().DATA.items.AAA.qty,88);assert.equal(c.state().editing,true);assert.equal(c.puts().length,0);
});


for(const action of ['保存','导入'])test('事务生命周期 旧仓位回执不能消费后来的新'+action+'代次',async()=>{
  const b=await boot();b.respond(b.requests[0],meta(SEED));await settle();await b.enter();b.edit('AAA','qty','7');await b.save();
  b.respond(b.requests.at(-1),meta(SEED));await settle();const first=b.puts()[0],oldGeneration=b.state().positionSave.generation;
  if(action==='保存'){await b.enter();b.edit('AAA','qty','9');await b.save();}else b.importData({...copy(SEED),cash:456,items:{AAA:{cost:100,qty:9}}});
  const newGeneration=b.state().positionSave.generation;assert.ok(newGeneration>oldGeneration);b.respond(first,{});await settle();
  assert.equal(b.state().positionsAuthorized,true);assert.equal(b.state().positionSave.generation,newGeneration);await b.tick(30000);
  b.respond(b.requests.at(-1),meta(b.payload(first),'first-done'));await settle();const second=b.puts()[1];assert.ok(second);
  assert.equal(b.payload(second).items.AAA.qty,9);if(action==='导入')assert.equal(b.payload(second).cash,456);
  b.respond(second,{});await settle();assert.equal(b.state().positionsAuthorized,false);assert.equal(b.state().positionSave,null);assert.equal(b.state().dirty,false);
});


test('事务生命周期 未成功仓位事务保留代次用于同页重试但不带新草稿',async()=>{
  const b=await boot();b.respond(b.requests[0],meta(SEED));await settle();await b.enter();b.edit('AAA','qty','7');await b.save();
  const generation=b.state().positionSave.generation;await finishPush(b,500);assert.equal(b.state().positionSave.generation,generation);
  await b.enter();b.edit('AAA','qty','88');b.doc.getElementById('costCancel').click();await b.tick(30000);
  b.respond(b.requests.at(-1),meta(SEED,'fixture-sha'));await settle();const retry=b.puts()[1];assert.ok(retry);assert.equal(b.payload(retry).items.AAA.qty,7);
  b.respond(retry,{});await settle();assert.equal(b.state().positionSave,null);assert.equal(b.local().items.AAA.qty,7);assert.equal(b.state().dirty,false);
});


test('事务生命周期 只读缓存没有核验资格，接管失败读取不得默认已同步',async()=>{
  const b=await boot();b.respond(b.requests[0],meta(SEED));await settle();const c=await boot({store:b.store});c.fireWindow('storage',{key:CLOUD});
  assert.equal(c.state().cloudVerified,false);assert.notEqual(c.state().SYNC,'synced');await b.dispose();await c.enter();
  const initial=c.requests[0];c.doc.getElementById('costCancel').click();await settle();const retry=c.requests.at(-1);assert.notEqual(retry,initial);
  c.respond(initial,meta({...SEED,cash:111}));await settle();assert.equal(c.state().cloudVerified,false);
  c.respond(retry,{},500);await settle();c.respond(c.requests.at(-1),{},404);await settle();
  assert.equal(c.state().SYNC,'nocloud');assert.doesNotMatch(c.doc.getElementById('costState').textContent,/已同步/);assert.equal(c.puts().length,0);
});
test('事务生命周期 接管编辑未输入时只有真实GET且内容一致才显示已同步',async()=>{
  const b=await boot();b.respond(b.requests[0],meta(SEED));await settle();const c=await boot({store:b.store});c.fireWindow('storage',{key:LOCAL});
  await b.dispose();await c.enter();assert.equal(c.state().cloudVerified,false);assert.doesNotMatch(c.doc.getElementById('costState').textContent,/已同步/);
  c.respond(c.requests[0],meta(SEED));await settle();assert.equal(c.state().cloudVerified,true);assert.equal(c.state().SYNC,'synced');
  assert.match(c.doc.getElementById('costState').textContent,/已同步/);assert.equal(c.state().editing,true);assert.equal(c.puts().length,0);
});
test('事务生命周期 bfCache缓存损坏也先撤销旧仓位授权，读取成功仍云端覆盖',async()=>{
  const b=await boot();b.respond(b.requests[0],meta(SEED));await settle();await b.enter();b.edit('AAA','qty','7');await b.save();
  assert.ok(b.state().positionSave);await b.dispose();b.store.values.set(LOCAL,'{invalid');b.fireWindow('pageshow',{persisted:true});await settle();
  assert.equal(b.state().positionsAuthorized,false);assert.equal(b.state().positionSave,null);assert.equal(b.state().cloudFirstPending,true);
  const remote={...copy(SEED),cash:999,items:{AAA:{cost:100,qty:20}}};b.respond(b.requests.at(-1),meta(remote));await settle();await b.tick(180000);
  assert.equal(b.local().items.AAA.qty,20);assert.equal(b.local().cash,999);assert.equal(b.puts().length,0);assert.equal(b.state().dirty,false);
});

const invalids=[
  ['数组items',{items:[]}],['null items',{items:null}],['字符串金额',{items:{},cash:'1'}],['负数量',{items:{AAA:{cost:1,qty:-1}}}],
  ['负成本',{items:{AAA:{cost:-1,qty:1}}}],['非有限数',{items:{},fixed:Infinity}],['null数值',{items:{},cash:null}],
  ['非法版本',{items:{},schema_version:99}],['字符串版本',{items:{},schema_version:'2'}],
  ['属性注入日期',{items:{},updated_at:'2026-10-20" onmouseover="x'}],['原型标的',JSON.parse('{"items":{"__proto__":{"qty":1,"cost":1}}}')],
];
for(const [name,data] of invalids)test('拒绝导入'+name+'且保留原值',async()=>{let b=await boot();const before=b.canonical(b.state().DATA);assert.throws(()=>b.importData(data));assert.equal(b.canonical(b.state().DATA),before);assert.throws(()=>b.mergeCloud(data));});
test('导入立即进统一队列，编辑会话退出',async()=>{let b=await boot();await b.enter();b.importData({...SEED,cash:99});await b.tick(0);assert.equal(b.state().editing,false);assert.ok(b.state().inFlight);assert.ok(b.state().NOTICE.includes('上传队列'));let p=await finishPush(b);assert.equal(b.payload(p).cash,99);assert.equal(b.state().dirty,false);});
test('文件读取迟到不覆盖之后输入',async()=>{let b=await boot();await b.enter();let input=b.doc.getElementById('costFile');input.fire('change',{target:{files:[{}],value:'x'}});b.edit('AAA','qty','9');let fr=b.readers[0];fr.result=JSON.stringify(SEED);fr.onload();assert.equal(b.state().DATA.items.AAA.qty,9);assert.match(b.state().NOTICE,/已变化/);});
test('文件读取失败有明确提示',async()=>{let b=await boot();b.doc.getElementById('costFile').fire('change',{target:{files:[{}],value:'x'}});b.readers[0].onerror();assert.match(b.state().NOTICE,/无法读取/);});
test('GET等待期间修改，PUT仍为最初快照',async()=>{let b=await boot();await b.enter();b.edit();await b.save();await b.enter();b.edit('AAA','qty','2');let p=await finishPush(b);assert.equal(b.payload(p).items.AAA.qty,1);assert.equal(b.state().CLOUD.items.AAA.qty,1);assert.equal(b.state().DATA.items.AAA.qty,2);assert.equal(b.state().dirty,true);assert.equal(b.state().editing,true);});
test('PUT等待期间修改不清dirty且不关闭新编辑',async()=>{let b=await boot();await b.enter();b.edit();await b.save();b.respond(b.requests.at(-1),{sha:'s'});await settle();await b.enter();b.edit('AAA','qty','2');b.respond(b.puts()[0],{});await settle();assert.equal(b.state().CLOUD.items.AAA.qty,1);assert.equal(b.state().dirty,true);assert.equal(b.state().editing,true);});
test('手动自动重试并发入口只有一个上传',async()=>{let b=await boot();await b.enter();b.edit();await b.save();b.requestSave(1200,false);b.requestSave(0,true);b.doPush();await b.tick(5000);assert.equal(b.requests.length,2);await finishPush(b);assert.equal(b.puts().length,1);});
test('资产比例图例删除合计文字且深浅绿为两个完整紧贴色块',async()=>{
  const b=await bootLoss({longs:['AAA'],prices:{AAA:100,BBB:100}}),foot=b.doc.getElementById('costFoot');
  assert.doesNotMatch(foot.textContent,/所有持仓/);
  const pair=foot.querySelector('.seg-all');assert.ok(pair);assert.equal(pair.tagName,'SPAN');
  assert.deepEqual(pair.children.map(n=>n.className),['s-long','s-short']);
  assert.equal(pair.getAttribute('aria-hidden'),'true');
  assert.match(pair.parentNode.getAttribute('aria-label'),/^所有持仓 [0-9.]+%$/);
  assert.equal(pair.parentNode.querySelector('b').textContent,((5*100+2*100)/(5*100+2*100+30+20)*100).toFixed(1)+'%');
  assert.match(CSS,/\.cost-legend \.seg-all\{display:inline-flex;gap:0;flex:none;?\}/);
  assert.doesNotMatch(CSS,/i\.seg-all\{|seg-all[^}]*linear-gradient/);
  assert.match(CSS,/\.cost-legend i\{width:9px;height:9px/);
});

test('所有手动保存服从15秒节流并上传最新revision',async()=>{let b=await boot();await b.enter();b.edit();await b.save();let p=await finishPush(b);await b.enter();b.edit('AAA','qty','3');await b.save();await b.tick(14999);assert.equal(b.puts().length,1);await b.tick(1);let q=await finishPush(b);assert.equal(q.at-p.at,15000);assert.equal(b.payload(q).items.AAA.qty,3);});
test('刷新不重置15秒持久化节流',async()=>{let b=await boot();await b.enter();b.edit();await b.save();await finishPush(b);await b.dispose();let c=await boot({store:b.store,now:NOW+1000});await c.enter();c.edit('AAA','qty','8');await c.save();assert.equal(c.requests.length,1);await c.tick(13999);assert.equal(c.requests.length,1);await c.tick(1);assert.equal(c.requests.length,2);});
test('失败重试保留15秒退避并服从节流，读取最新待保存版本',async()=>{let b=await boot();await b.enter();b.edit();await b.save();await finishPush(b,500);assert.equal(b.state().dirty,true);const n=b.requests.length;await b.tick(14999);assert.equal(b.requests.length,n);await b.tick(1);await finishPush(b);assert.equal(b.puts().length,2);assert.equal(b.state().dirty,false);});
test('GET失败不得当成不存在并发送PUT',async()=>{let b=await boot();await b.enter();b.edit();await b.save();b.respond(b.requests.at(-1),{message:'forbidden'},403);await settle();assert.equal(b.puts().length,0);assert.equal(b.state().SYNC,'error');});
test('canonical忽略对象键顺序和updated_at',async()=>{let b=await boot();let a=b.normalize(SEED),d=copy(a);d.items={BBB:{qty:2,cost:90},AAA:{qty:5,cost:100}};d.updated_at='2020-01-01';assert.equal(b.canonical(a),b.canonical(d));b.mergeCloud(d);await b.enter();b.edit('AAA','qty','5');await b.save();assert.equal(b.puts().length,0);assert.equal(b.state().dirty,false);});
test('旧pull在编辑后迟到不覆盖',async()=>{let b=await boot();let pull=b.requests[0];await b.enter();b.edit('AAA','qty','9');b.respond(pull,{content:Buffer.from(JSON.stringify(SEED)).toString('base64')});await settle();assert.equal(b.state().DATA.items.AAA.qty,9);});
test('旧pull在上传成功后迟到不覆盖',async()=>{let b=await boot();let pull=b.requests[0];await b.enter();b.edit('AAA','qty','9');await b.save();await finishPush(b);b.respond(pull,{content:Buffer.from(JSON.stringify(SEED)).toString('base64')});await settle();assert.equal(b.state().DATA.items.AAA.qty,9);assert.equal(b.state().dirty,false);});
test('拉取失败不上传内置快照',async()=>{let b=await boot();b.respond(b.requests[0],{},500);await settle();b.respond(b.requests.at(-1),{},404);await settle();await b.tick(1200);assert.equal(b.puts().length,0);assert.equal(b.state().inFlight,null);});
test('取消编辑恢复并立即落盘，不上传取消的草稿',async()=>{let b=await boot();await b.enter();b.edit('AAA','qty','8');let revision=b.state().revision;b.doc.getElementById('costCancel').click();await b.tick(60000);assert.equal(b.local().items.AAA.qty,5);assert.ok(b.state().revision>revision);assert.equal(b.puts().length,0);assert.equal(b.state().dirty,false);});
test('旧上传回执不会污染取消后的状态',async()=>{let b=await boot();await b.enter();b.edit('AAA','qty','1');await b.save();await b.enter();b.edit('AAA','qty','2');b.doc.getElementById('costCancel').click();await finishPush(b);assert.equal(b.state().DATA.items.AAA.qty,1);assert.equal(b.state().CLOUD.items.AAA.qty,1);await b.tick(0);assert.equal(b.state().dirty,false);});
test('折叠保留草稿并暂停待上传任务',async()=>{let b=await boot();await b.enter();b.edit('AAA','qty','8');let p=b.doc.getElementById('costManage');p.open=false;p.fire('toggle');await b.tick(60000);assert.equal(b.local().items.AAA.qty,8);assert.equal(b.state().dirty,true);assert.equal(b.state().editing,false);assert.equal(b.puts().length,0);});
test('上传中再次编辑并折叠，旧回执不自动上传草稿',async()=>{let b=await boot();await b.enter();b.edit();await b.save();await b.enter();b.edit('AAA','qty','8');let p=b.doc.getElementById('costManage');p.open=false;p.fire('toggle');await finishPush(b);await b.tick(60000);assert.equal(b.puts().length,1);assert.equal(b.state().dirty,true);});



test('退役基本面不影响ETF标签和财报日期元素',async()=>{
  let b=await boot({prepareDocument(doc){
    const row=doc.getElementById('s-aaa'),sd=row.querySelector('td.sym');
    const tag=doc.createElement('span');tag.className='tag';tag.textContent='ETF';sd.appendChild(tag);
    const date=doc.createElement('span');date.className='earn urgent';date.textContent='10/21 美东盘后 · 北京10/22 04:00';row.children[1].appendChild(date);
  }});
  assert.equal(b.rows[0].isEtf,true);
  assert.equal(b.doc.querySelector('#s-aaa .tag').textContent,'ETF');await b.enter();
  assert.ok(b.doc.querySelector('#costMembers .ep-grid [data-cost-row][data-sym="AAA"]'));
  assert.equal(b.doc.querySelector('#s-aaa .earn.urgent').textContent,'10/21 美东盘后 · 北京10/22 04:00');
  b.paint();assert.ok(b.doc.querySelector('#s-aaa .earn.urgent'));
});






test('零固定收益通过单一固收成本框同步资产与成本',async()=>{let b=await boot({local:{...SEED,fixed:0,fixed_cost:0}});await b.enter();b.edit('BOND','fixed_cost','12.3456');assert.equal(b.local().fixed,12.3456);assert.equal(b.local().fixed_cost,12.3456);});
for(const [name,fixed,fixedCost] of [['固收盈利',200,15],['固收亏损',20,200],['固收微亏',16916.34,16916.97],['无固收',0,0]]){
  test('表头仅股票ETF盈亏且保留总资产：'+name,async()=>{
    const b=await boot({local:{...copy(SEED),fixed,fixed_cost:fixedCost},token:false});
    const sum=b.doc.querySelector('.cost-sum');
    // AAA: 100*5成本=500无盈亏；BBB: 100*2-90*2=20，成本合计680。
    assert.equal(sum.querySelector('b').textContent,'$'+Math.round(700+30+fixed).toLocaleString('en-US'));
    assert.equal(sum.querySelectorAll('b')[1].textContent,'+$20.0（+2.9%）');
    assert.match(sum.getAttribute('title'),/盈亏仅股票\/ETF/);
    assert.match(sum.getAttribute('aria-label'),/股票\/ETF持仓盈亏/);
    const bond=b.doc.querySelector('.cashbond-row .cb-bond');
    assert.ok(bond);assert.equal(bond.querySelector('.cb-amt').textContent,'$'+Math.round(fixed).toLocaleString('en-US'));
    assert.equal(bond.querySelector('.pct'),null);
    assert.equal(b.state().DATA.fixed,fixed);assert.equal(b.state().DATA.fixed_cost,fixedCost);
    b.paint();assert.equal(sum.querySelectorAll('b')[1].textContent,'+$20.0（+2.9%）');
    assert.equal(b.puts().length,0);
  });
}
test('仅现金固收无股票ETF时表头盈亏为零',async()=>{
  const b=await boot({groups:{AAA:'focus',BBB:'focus'},token:false});
  assert.equal(b.doc.querySelector('.cost-sum').textContent,'$50·+$0.0（+0.0%）');
  assert.equal(b.doc.querySelector('.cashbond-row .cb-bond .pct'),null);
});
test('固收盈利不能抵消股票亏损且编辑成本仍保留',async()=>{
  const d={...copy(SEED),fixed:2000,fixed_cost:10};d.items.AAA.cost=110;
  const b=await boot({local:d,token:false});
  const sum=b.doc.querySelector('.cost-sum');
  assert.equal(sum.querySelectorAll('b')[1].textContent,'-$30.0（-4.1%）');
  assert.equal(sum.querySelectorAll('b')[1].className,'down');
  await b.enter();b.edit('BOND','fixed_cost','123.4567');await b.tick(16);
  assert.equal(b.local().fixed_cost,123.4567);assert.equal(b.local().fixed,123.4567);
  assert.equal(sum.querySelectorAll('b')[1].textContent,'-$30.0（-4.1%）');
  assert.equal(b.doc.querySelector('.cashbond-row .cb-bond .pct'),null);
  assert.equal(b.puts().length,0);
});
test('本地写失败显式提示且不清dirty不上传',async()=>{let b=await boot();b.store.failSet.add(LOCAL);await b.enter();b.edit();await b.save();assert.equal(b.state().dirty,true);assert.match(b.doc.getElementById('costState').textContent,/未持久化/);assert.equal(b.puts().length,0);assert.match(b.doc.getElementById('costNotice').textContent,/尚未持久化/);assert.equal(b.doc.getElementById('costNotice').hidden,false);assert.equal(b.doc.querySelector('[data-cost-export]'),null);});
test('存储恢复后可显式重试保存',async()=>{let b=await boot();b.store.failSet.add(LOCAL);await b.enter();b.edit();await b.save();b.store.failSet.delete(LOCAL);b.requestSave(0,true);await b.tick(0);await finishPush(b);assert.equal(b.state().dirty,false);assert.equal(b.local().items.AAA.qty,1);});
test('节流时间持久化失败禁止上传',async()=>{let b=await boot();b.store.failSet.add(PUSH_AT);await b.enter();b.edit();await b.save();assert.equal(b.puts().length,0);assert.equal(b.state().dirty,true);});
test('上传成功但本地cloud缓存失败不谎报已同步',async()=>{let b=await boot();await b.enter();b.edit();await b.save();b.store.failSet.add(CLOUD);await finishPush(b);assert.equal(b.state().dirty,true);assert.match(b.doc.getElementById('costState').textContent,/未持久化/);});
test('存储损坏不会用种子静默覆盖',async()=>{let s=shared({[LOCAL]:'{invalid'});let b=await boot({store:s});assert.equal(s.values.get(LOCAL),'{invalid');assert.match(b.doc.getElementById('costState').textContent,/未持久化/);});
test('多tab只允许一个写者，旧标签页不能覆盖',async()=>{let b=await boot();let c=await boot({store:b.store});assert.equal(c.state().writer,false);await b.enter();b.edit('AAA','qty','8');await c.enter();assert.equal(c.state().editing,false);assert.throws(()=>c.importData(SEED));assert.equal(c.local().items.AAA.qty,8);c.fireWindow('storage',{key:LOCAL});assert.equal(c.state().DATA.items.AAA.qty,8);await b.dispose();await c.enter();assert.equal(c.state().writer,true);assert.equal(c.state().DATA.items.AAA.qty,8);});
test('GET等待时pagehide取消旧请求并安全让第二tab接管',async()=>{
  let b=await boot();await b.enter();b.edit();await b.save();let old=b.requests.at(-1), c=await boot({store:b.store});
  await b.dispose();assert.equal(old.init.signal.aborted,true);await c.enter();assert.equal(c.state().writer,true);
  b.respond(old,{sha:'s'});await settle();assert.equal(b.puts().length,0);assert.equal(b.state().inFlight,null);
  assert.equal(c.state().DATA.items.AAA.qty,1);
});
test('仓位绿色外框与顶部编辑操作区契约',async()=>{
  const b=await boot({noLocks:true});
  const panel=b.doc.getElementById('costManage'), acts=b.doc.getElementById('costActs');
  assert.equal(panel.querySelector('.cost-body').firstChild,acts);
  assert.equal(acts.nextElementSibling.id,'costMembers');
  assert.deepEqual(acts.querySelectorAll('button').map(n=>n.hasAttribute('data-cost-add')?'costAdd':n.id),['costAdd','costSave','costCancel']);
  assert.equal(acts.querySelector('[data-cost-add]').hidden,true);
  assert.equal(panel.querySelector('#costExport'),null);assert.equal(panel.querySelector('#costImport'),null);
  assert.equal(acts.hidden,true);
  assert.doesNotMatch(panel.textContent,/成本价与数量可填到小数点后四位|现金两框相加，空框按0算/);
  // 终稿操作区仅编辑时显示，四列编辑器位于静态持仓卡之前。
  assert.match(CSS,/#costManage\.cost-manage\{[^}]*display:none;[^}]*border:1\.5px solid var\(--signal-border\);[^}]*box-shadow:none;/);
  assert.match(CSS,/#costManage\.cost-manage\.cost-edit-open\{display:block;?\}/);
  assert.match(CSS,/#costManage \.ep-grid\{[^}]*grid-template-columns:minmax\(0,1\.5fr\) repeat\(3,minmax\(0,1fr\)\)/);
  assert.match(CSS,/#costManage \.ep-cash\{[^}]*grid-template-columns:repeat\(2,minmax\(0,1fr\)\)/);
  await b.enter();assert.equal(acts.hidden,false);b.exitEdit(true);assert.equal(acts.hidden,true);
});
test('云端校验失败仅在顶部操作行提示，无备份或重试上传按钮',async()=>{
  const b=await boot({token:false}),panel=b.doc.getElementById('costManage');
  const acts=b.doc.getElementById('costActs'),notice=b.doc.getElementById('costNotice');
  b.respond(b.requests.find(r=>r.init.method!=='PUT'),{...copy(SEED),updated_at:'2026-10-27T05:18:05.8613258+00:00'});
  await settle();
  assert.equal(b.state().SYNC,'nocloud');
  assert.equal(acts.parentNode,panel.querySelector('.cost-body'));
  assert.equal(acts.nextElementSibling.id,'costMembers');
  assert.equal(notice.parentNode,acts);assert.equal(notice.getAttribute('role'),'status');
  assert.equal(acts.hidden,false);assert.equal(notice.hidden,false);
  assert.equal(notice.textContent,'云端数据校验失败：updated_at 必须是有效日期');
  assert.equal(b.doc.getElementById('costSave').hidden,true);
  assert.equal(b.doc.getElementById('costCancel').hidden,true);
  assert.equal(panel.querySelector('[data-cost-retry]'),null);
  assert.equal(panel.querySelector('[data-cost-export]'),null);
  assert.doesNotMatch(b.doc.getElementById('costFoot').textContent,/校验失败|导出备份|重试保存/);
  const before=b.canonical(b.state().DATA);await b.tick(30000);
  assert.equal(b.puts().length,0);assert.equal(b.canonical(b.state().DATA),before);
  await b.enter();assert.equal(acts.hidden,false);assert.equal(notice.hidden,false);
  assert.equal(b.doc.getElementById('costSave').hidden,false);
  assert.equal(b.doc.getElementById('costCancel').hidden,false);
  b.exitEdit(true);assert.equal(b.doc.getElementById('costSave').hidden,true);
  assert.equal(notice.hidden,false);assert.equal(b.puts().length,0);
  assert.equal(notice.textContent,'云端数据校验失败：updated_at 必须是有效日期');
  // 草稿错误只覆盖当前展示，取消后仍应看到尚未解决的云端校验错误。
  await b.enter();b.edit('AAA','cost','bad');await b.save();assert.match(notice.textContent,/无效输入/);
  b.exitEdit(true);assert.equal(notice.hidden,false);
  assert.equal(notice.textContent,'云端数据校验失败：updated_at 必须是有效日期');assert.equal(b.puts().length,0);
  assert.match(CSS,/\.cost-acts \.cost-notice\{[^}]*min-width:0[^}]*overflow-wrap:anywhere/);
  await b.dispose();
});
test('保存上传失败顶部只留说明，重新编辑保留正常保存取消',async()=>{
  const b=await boot();await b.enter();b.edit('AAA','qty','7');await b.save();
  await finishPush(b,403);
  const acts=b.doc.getElementById('costActs'),notice=b.doc.getElementById('costNotice');
  assert.equal(b.state().SYNC,'error');assert.equal(acts.hidden,false);assert.equal(notice.hidden,false);
  assert.ok(notice.textContent);assert.equal(notice.parentNode,acts);
  assert.equal(b.doc.getElementById('costSave').hidden,true);
  assert.equal(b.doc.querySelector('[data-cost-retry]'),null);
  assert.equal(b.doc.querySelector('[data-cost-export]'),null);
  await b.enter();assert.equal(b.doc.getElementById('costSave').hidden,false);
  assert.equal(b.doc.getElementById('costCancel').hidden,false);
  await b.dispose();
});
test('本地保存异常也在操作行纯文字提示，隔离导出入口保持独立',async()=>{
  const store=shared({[LOCAL]:SEED,[TOKEN]:'test-token'});store.failSet.add(LOCAL);
  const b=await boot({store}),notice=b.doc.getElementById('costNotice');
  assert.equal(notice.hidden,false);assert.match(notice.textContent,/尚未持久化/);
  assert.equal(b.doc.getElementById('costActs').hidden,false);
  assert.equal(b.doc.querySelector('[data-cost-export]'),null);
  assert.equal(b.doc.querySelector('[data-cost-retry]'),null);
  assert.equal(b.puts().length,0);await b.dispose();
  const isolated=await boot({noLocks:true});
  assert.ok(isolated.doc.getElementById('costFoot').querySelector('[data-cost-export]'));
  assert.equal(isolated.doc.getElementById('costNotice').hidden,true);
  await isolated.dispose();
});

test('不支持Web Locks安全降级为隔离会话编辑',async()=>{
  let b=await boot({noLocks:true,protocol:'file:'});let original=b.store.values.get(LOCAL);
  await b.enter();assert.equal(b.state().editing,true);b.edit('AAA','qty','9');await b.save();
  assert.equal(b.sessionLocal().items.AAA.qty,9);assert.equal(b.store.values.get(LOCAL),original);assert.equal(b.requests.length,0);
  assert.match(b.doc.getElementById('costState').textContent,/隔离预览/);assert.match(b.doc.getElementById('costFoot').textContent,/关闭标签页前请导出/);
});
test('跨tab接管先GET且仓位保存依旧遵守持久化节流',async()=>{
  let b=await boot();await b.enter();b.edit();await b.save();await finishPush(b);let c=await boot({store:b.store,now:NOW+1000});
  await b.dispose();await c.enter();assert.equal(c.requests.length,1);assert.equal(c.state().SYNC,'pulling');
  c.edit('AAA','qty','7');await c.save();assert.equal(c.requests.length,1);await c.tick(29000);assert.equal(c.requests.length,2);
  assert.equal(c.puts().length,0);
});
test('输入每次立即落盘但同帧只重绘一次并保留选区',async()=>{
  let b=await boot();await b.enter();let before=b.doc.renders,writes=b.store.writes.filter(x=>x.k===LOCAL).length;
  let start=performance.now();for(let i=1;i<=20;i++)b.edit('AAA','qty',String(i));let inputMs=performance.now()-start;
  const el=b.doc.activeElement;el.value='20.0000';el.setSelectionRange(1,5,'backward');
  assert.equal(b.doc.renders,before);assert.equal(b.store.writes.filter(x=>x.k===LOCAL).length-writes,20);assert.equal(b.local().items.AAA.qty,20);
  await b.tick(16);let renders=b.doc.renders-before;assert.ok(renders>0);assert.equal(b.doc.activeElement.value,'20.0000');assert.equal(b.doc.activeElement.selectionStart,1);assert.equal(b.doc.activeElement.selectionEnd,5);assert.equal(b.doc.activeElement.selectionDirection,'backward');
  console.log('  输入微基准：20次即时持久化 '+inputMs.toFixed(2)+'ms；合帧渲染写入 '+renders+' 处（mock环境，非浏览器性能结论）');
});
test('拒绝非法输入而不把前缀数字当有效值',async()=>{let b=await boot();await b.enter();let e=b.edit('AAA','qty','2oops');assert.ok(e.validationMessage);assert.equal(b.local().items.AAA.qty,5);});


test('慢GET后两次实际PUT也必须间隔15秒',async()=>{
  let b=await boot();await b.enter();b.edit();await b.save();await b.tick(45000);let p=await finishPush(b);
  await b.enter();b.edit('AAA','qty','2');await b.save();await b.tick(14999);assert.equal(b.puts().length,1);
  await b.tick(1);let q=await finishPush(b);assert.equal(q.at-p.at,15000);
});
test('旧cloud缓存未核验时不能直接清dirty',async()=>{
  let s=shared({[LOCAL]:{...SEED,_local:{dirty:true,revision:3}},[CLOUD]:SEED,[TOKEN]:'test-token'});
  let b=await boot({store:s});b.requestSave(0,true);await b.tick(0);
  assert.equal(b.state().dirty,true);assert.equal(b.state().inFlight,null);assert.equal(b.requests.length,1);
  let remote={...SEED,cash:123};b.respond(b.requests[0],meta(remote));await settle();
  assert.equal(b.state().DATA.cash,123);assert.equal(b.state().dirty,false);assert.equal(b.puts().length,0);
});
test('GET核验canonical相同则不产生重复PUT',async()=>{
  let b=await boot();await b.enter();b.edit('AAA','qty','7');await b.save();let d=b.state().DATA;d.items={BBB:{qty:2,cost:90},AAA:{qty:7,cost:100}};
  b.respond(b.requests.at(-1),{sha:'s',content:Buffer.from(JSON.stringify(d)).toString('base64')});await settle();assert.equal(b.puts().length,0);assert.equal(b.state().dirty,false);
});
test('有效云端读取可替换损坏的旧本地缓存但不触发上传',async()=>{
  let s=shared({[LOCAL]:'{invalid',[TOKEN]:'test-token'});let b=await boot({store:s});
  assert.equal(s.values.get(LOCAL),'{invalid');b.respond(b.requests[0],meta(SEED));await settle();
  assert.equal(b.canonical(b.local()),b.canonical(b.normalize(SEED)));assert.equal(b.state().dirty,false);
  assert.equal(b.puts().length,0);assert.equal(b.state().SYNC,'synced');
});
test('bfcache恢复不会用旧取消快照覆盖接管后的数据',async()=>{
  let b=await boot();await b.enter();b.edit('AAA','qty','7');await b.dispose();let c=await boot({store:b.store});await c.enter();c.edit('AAA','qty','9');await c.dispose();
  b.fireWindow('pageshow',{persisted:true});await settle();assert.equal(b.state().editing,false);await b.enter();b.edit('AAA','qty','10');b.doc.getElementById('costCancel').click();assert.equal(b.local().items.AAA.qty,9);
});
test('持久化数据与dirty和revision为同一原子记录',async()=>{
  let b=await boot();await b.enter();b.edit('AAA','qty','0.0001');let d=b.local();assert.equal(d.items.AAA.qty,0.0001);assert.equal(d._local.dirty,true);assert.equal(d._local.revision,b.state().revision);
});
test('404仅表示不存在并允许创建data文件',async()=>{
  let b=await boot();await b.enter();b.edit();await b.save();b.respond(b.requests.at(-1),{},404);await settle();assert.equal(b.puts().length,1);assert.equal(JSON.parse(b.puts()[0].init.body).sha,undefined);
});

test('无效输入经过重绘仍阻止保存',async()=>{
  let b=await boot();await b.enter();b.edit('AAA','qty','-1');b.paint();await b.save();assert.equal(b.state().editing,true);assert.equal(b.puts().length,0);assert.equal(b.local().items.AAA.qty,5);
});


test('移出全部持仓仍显示现金固定收益且持仓数量归零',async()=>{
  let b=await boot({groups:{AAA:'focus',BBB:'focus'}});assert.equal(b.doc.querySelector('.cost-sum>b').textContent,'$50');
  assert.equal(b.doc.querySelectorAll('.ep-grid [data-cost-row]').length,0);
  assert.equal(b.doc.querySelectorAll('.cost-contrib li').length,0);
  assert.equal(b.doc.querySelector('.cashbond-row .cb-cash .cb-amt').textContent,'$30');
  assert.equal(b.doc.querySelector('.cashbond-row .cb-bond .cb-amt').textContent,'$20');
  await b.enter();assert.ok(b.doc.querySelector('input[data-f="cash"]'));
  assert.equal(b.doc.querySelector('input[data-sym="AAA"]'),null);
});
test('移组后云端含旧数量也不能拉回持仓',async()=>{
  let b=await boot({groups:{AAA:'focus'}});b.mergeCloud({...SEED,items:{AAA:{cost:999,qty:999},BBB:{cost:90,qty:2}}});b.paint();
  assert.equal(b.doc.querySelector('.cost-sum>b').textContent,'$250');assert.equal(b.doc.querySelector('#costMembers li[data-sym="AAA"]'),null);
  assert.ok(b.doc.getElementById('s-aaa').closest('#group_focus'));assert.equal(b.state().DATA.items.AAA.qty,999);
});
test('移组后导入旧仓位也不会重新纳入估值',async()=>{
  let b=await boot({groups:{AAA:'focus'},token:false});b.importData({...SEED,items:{AAA:{cost:999,qty:999},BBB:{cost:90,qty:2}}});
  assert.equal(b.doc.querySelector('.cost-sum>b').textContent,'$250');assert.equal(b.doc.querySelector('#costMembers li[data-sym="AAA"]'),null);
});
test('移回持仓沿用保留旧值但不自动抹成本或增加现金',async()=>{
  let d=copy(SEED);d.items.AAA={cost:123,qty:7};let b=await boot({local:d,groups:{AAA:'focus'}});b.move('AAA','positions');
  assert.equal(b.doc.querySelector('.cost-sum>b').textContent,'$950');assert.equal(b.rows.find(r=>r.sym==='AAA').cost,123);
  assert.equal(b.state().DATA.items.AAA.qty,7);assert.equal(b.state().DATA.cash,30);await b.enter();assert.equal(b.doc.querySelector('input[data-sym="AAA"][data-f="cost"]').value,'123');
  assert.doesNotMatch(b.doc.getElementById('costFoot').textContent,/移回后沿用旧值/);
});



test('已清仓硬编码GEHC不再删掉其他分组及对应链接',async()=>{
  let b=await boot({symbols:['GEHC','BBB'],groups:{GEHC:'focus'},local:{...SEED,items:{GEHC:{cost:100,qty:500},BBB:{cost:90,qty:2}}},
    prepareDocument:d=>{let ul=d.createElement('ul');ul.innerHTML='<li><a href="#s-gehc">GEHC</a></li>';d.body.appendChild(ul);}});
  assert.ok(b.doc.getElementById('s-gehc'));assert.ok(b.doc.querySelector('a[href="#s-gehc"]'));assert.equal(b.doc.querySelector('.cost-sum>b').textContent,'$250');
});
test('移组标的不参与20日组合收益',async()=>{
  let seed={...SEED,rs20:{items:{AAA:90,BBB:10},bench:0,days:20}};let b=await boot({seed,groups:{AAA:'focus'}});
  assert.match(b.doc.querySelector('#group_positions .rs-spy').textContent,/\+10\.0%/);
  b.move('BBB','focus');assert.equal(b.doc.querySelector('#group_positions .rs-spy').textContent,'');
});

test('编辑时移组拒绝旧输入框提交已移走的标的',async()=>{
  let b=await boot();await b.enter();let input=b.doc.querySelector('input[data-sym="AAA"][data-f="qty"]');b.move('AAA','focus');input.value='999';
  b.doc.getElementById('costManage').fire('input',{target:input});assert.equal(b.state().DATA.items.AAA.qty,5);assert.equal(b.doc.querySelector('input[data-sym="AAA"]'),null);
});
test('file隔离会话刷新恢复且不触碰真实共享存储',async()=>{
  let b=await boot({noLocks:true,protocol:'file:'});await b.enter();b.edit('AAA','qty','0');await b.save();await b.dispose();
  let c=await boot({noLocks:true,protocol:'file:',store:b.store,session:b.session});assert.equal(c.state().DATA.items.AAA.qty,0);
  assert.equal(b.store.writes.length,0);assert.equal(c.requests.length,0);assert.equal(c.local().items.AAA.qty,5);
});
test('无锁多tab完全隔离不会互相覆写或覆盖正式页',async()=>{
  let real=await boot(), b=await boot({store:real.store,noLocks:true}), c=await boot({store:real.store,noLocks:true});
  await real.enter();real.edit('AAA','qty','6');await b.enter();b.edit('AAA','qty','7');await c.enter();c.edit('AAA','qty','8');
  b.fireWindow('storage',{key:LOCAL});c.fireWindow('storage',{key:LOCAL});
  assert.equal(real.local().items.AAA.qty,6);assert.equal(b.sessionLocal().items.AAA.qty,7);assert.equal(c.sessionLocal().items.AAA.qty,8);
  assert.equal(b.requests.length+c.requests.length,0);assert.equal(b.state().DATA.items.AAA.qty,7);
});

test('隔离会话存储失败显式提示并保留导出能力',async()=>{
  let b=await boot({noLocks:true});b.session.failSet.add(SESSION_LOCAL);await b.enter();b.edit('AAA','qty','8');await b.save();
  assert.equal(b.state().dirty,true);assert.match(b.doc.getElementById('costState').textContent,/未持久化/);assert.ok(b.doc.querySelector('[data-cost-export]'));
  assert.equal(b.state().DATA.items.AAA.qty,8);assert.equal(b.local().items.AAA.qty,5);assert.equal(b.requests.length,0);
});
test('隔离存储恢复后显式重试成功但仍不上传',async()=>{
  let b=await boot({noLocks:true});b.session.failSet.add(SESSION_LOCAL);await b.enter();b.edit('AAA','qty','8');await b.save();
  b.session.failSet.delete(SESSION_LOCAL);b.requestSave(0,true);assert.equal(b.sessionLocal().items.AAA.qty,8);assert.equal(b.requests.length,0);assert.deepEqual(copy(b.state().storageErrors),{});
});
test('移组后的刷新仍沿用当前分组而非本地旧数量',async()=>{
  let b=await boot();await b.dispose();let c=await boot({store:b.store,groups:{AAA:'focus'}});
  assert.equal(c.local().items.AAA.qty,5);assert.equal(c.doc.querySelector('.cost-sum>b').textContent,'$250');assert.equal(c.doc.querySelector('#costMembers li[data-sym="AAA"]'),null);
});
test('移出页面后DATA残留记录不重新创建行情行',async()=>{
  let b=await boot({groups:{AAA:null}});assert.equal(b.doc.getElementById('s-aaa'),null);assert.equal(b.state().DATA.items.AAA.qty,5);
  assert.equal(b.doc.querySelector('.cost-sum>b').textContent,'$250');
});
test('长持仓子组计入positions，移出后长持仓小计归零',async()=>{
  let b=await boot({prepareDocument:d=>{let r=d.getElementById('s-aaa'),rail=r.nextElementSibling;let sub=d.createElement('details');sub.id='group_positions_long';sub.innerHTML='<table></table>';d.getElementById('group_positions').appendChild(sub);sub.querySelector('table').appendChild(r);sub.querySelector('table').appendChild(rail);}});
  await b.enter();assert.ok(b.doc.querySelector('.ep-grid [data-cost-row][data-sym="AAA"]'));
  assert.equal(b.doc.querySelector('.cost-sum>b').textContent,'$750');
  b.move('AAA','focus');assert.equal(b.doc.querySelector('.cost-sum>b').textContent,'$250');
  assert.equal(b.doc.querySelector('.ep-grid [data-cost-row][data-sym="AAA"]'),null);
});
test('file即使支持Web Locks也仅启用隔离会话',async()=>{
  let b=await boot({protocol:'file:'});await b.enter();b.edit('AAA','qty','9');await b.save();
  assert.equal(b.sessionLocal().items.AAA.qty,9);assert.equal(b.local().items.AAA.qty,5);assert.equal(b.requests.length,0);assert.equal(b.store.holder,false);
});
test('隔离模式点击同步状态不允许写共享token',async()=>{
  let b=await boot({noLocks:true});b.ctx.prompt=()=> 'new-token';let original=b.store.values.get(TOKEN);
  b.doc.getElementById('costManage').fire('click',{target:b.doc.getElementById('costState')});
  assert.equal(b.store.values.get(TOKEN),original);assert.match(b.alerts.at(-1),/隔离预览/);
});
test('新开隔离标签页不会擅自继承另一tab的草稿',async()=>{
  let b=await boot({noLocks:true});await b.enter();b.edit('AAA','qty','9');let c=await boot({noLocks:true,store:b.store});
  assert.equal(c.state().DATA.items.AAA.qty,5);assert.equal(b.sessionLocal().items.AAA.qty,9);
});



test('旧ACK维护记录在刷新后不启动任何维护GET或PUT',async()=>{
  const remote=freshRemote();remote.items.AAA.qty=88;remote.cash=12345;
  const b=await boot({local:staleAck()});b.respond(b.requests[0],meta(remote,'new-sha'));await settle();await b.tick(600000);
  assert.equal(b.requests.length,1);assert.equal(b.puts().length,0);assert.equal(b.state().DATA.cash,12345);assert.equal(b.state().DATA.items.AAA.qty,88);assert.equal(b.state().dirty,false);
});



test('ACK维护与实际用户输入并存时仍保护用户dirty数值',async()=>{
  let b=await boot({local:staleAck()});await b.enter();b.edit('AAA','qty','7');
  b.respond(b.requests[0],meta(freshRemote()));await settle();assert.equal(b.state().DATA.items.AAA.qty,7);
  assert.equal(b.state().dirty,true);
});
test('首次pull的响应体pending同样有界并忽略迟到body',async()=>{
  let b=await boot({local:staleAck()}), old=b.requests[0];let resolve=pendingBody(b,old);await settle();
  await b.tick(b.state().deadline);assert.equal(old.init.signal.aborted,true);
  resolve(meta(freshRemote()));await settle();assert.equal(b.state().DATA.items.AAA.qty,5);
  assert.equal(b.state().cloudVerified,false);assert.equal(b.puts().length,0);
});
test('保存GET永久pending会超时清inFlight保留dirty且可重试',async()=>{
  let b=await boot();b.respond(b.requests[0],meta(SEED));await settle();await b.enter();b.edit();await b.save();let old=b.requests.at(-1);
  await b.tick(b.state().deadline);assert.equal(old.init.signal.aborted,true);assert.equal(b.state().inFlight,null);
  assert.equal(b.state().dirty,true);assert.equal(b.state().SYNC,'error');assert.equal(b.state().uncertain,null);
  b.respond(old,meta(SEED));await settle();assert.equal(b.puts().length,0);
  b.requestSave(0,true);await b.tick(0);await finishPush(b);assert.equal(b.state().dirty,false);
});
test('保存GET响应体pending超时后不再发送迟到PUT',async()=>{
  let b=await boot();b.respond(b.requests[0],meta(SEED));await settle();await b.enter();b.edit();await b.save();let get=b.requests.at(-1),resolve=pendingBody(b,get);await settle();
  await b.tick(b.state().deadline);assert.equal(b.state().inFlight,null);assert.equal(get.init.signal.aborted,true);
  resolve(meta(SEED));await settle();assert.equal(b.puts().length,0);assert.equal(b.state().dirty,true);
});
test('PUT永久pending超时持久化未知事务，迟到回执不能清dirty',async()=>{
  let b=await boot();b.respond(b.requests[0],meta(SEED));await settle();let put=await beginPut(b);
  await b.tick(b.state().deadline);assert.equal(put.init.signal.aborted,true);assert.equal(b.state().inFlight,null);
  assert.equal(b.state().dirty,true);assert.equal(b.local()._local.uncertain.payload.items.AAA.qty,1);assert.equal(b.local()._local.uncertain.baseSha,'base-sha');
  b.respond(put,{});await settle();assert.equal(b.state().dirty,true);assert.equal(b.state().cloudVerified,false);
});
test('PUT响应体pending超时后先核验已提交内容且不重复PUT',async()=>{
  let b=await boot();b.respond(b.requests[0],meta(SEED));await settle();let put=await beginPut(b),resolve=pendingBody(b,put);await settle();
  await b.tick(b.state().deadline);assert.equal(b.state().inFlight,null);assert.equal(put.init.signal.aborted,true);
  b.requestSave(0,true);await b.tick(0);let recovery=b.requests.at(-1);assert.notEqual(recovery,put);
  b.respond(recovery,meta(b.payload(put),'committed-sha'));await settle();
  assert.equal(b.puts().length,1);assert.equal(b.state().dirty,false);assert.equal(b.state().uncertain,null);
  resolve({});await settle();assert.equal(b.state().dirty,false);assert.equal(b.state().CLOUD.items.AAA.qty,1);
});
test('未知PUT恢复GET内容缺失时禁止重传或清dirty',async()=>{
  let b=await boot();b.respond(b.requests[0],meta(SEED));await settle();await beginPut(b);await b.tick(b.state().deadline);
  b.requestSave(0,true);await b.tick(0);b.respond(b.requests.at(-1),{sha:'base-sha'});await settle();
  assert.equal(b.puts().length,1);assert.equal(b.state().dirty,true);assert.ok(b.state().uncertain);assert.equal(b.state().inFlight,null);
});
test('未知PUT同SHA仅重发旧payload，新编辑随后单独上传，迟到旧请求CAS失败',async()=>{
  let b=await boot(), server=mockRemote(SEED);b.respond(b.requests[0],meta(SEED));await settle();let old=await beginPut(b);await b.tick(b.state().deadline);
  await b.enter();b.edit('AAA','qty','2');await b.save();b.respond(b.requests.at(-1),meta(server.data,server.sha));await settle();let retry=b.puts()[1];
  assert.equal(b.payload(retry).items.AAA.qty,1);assert.equal(JSON.parse(retry.init.body).sha,'base-sha');server.apply(b,retry);await settle();
  assert.equal(b.state().dirty,true);assert.equal(b.state().DATA.items.AAA.qty,2);await b.tick(30000);
  b.respond(b.requests.at(-1),meta(server.data,server.sha));await settle();let latest=b.puts()[2];assert.equal(b.payload(latest).items.AAA.qty,2);
  server.apply(b,latest);await settle();assert.equal(b.state().dirty,false);
  assert.equal(server.apply(b,old),false);await settle();assert.equal(server.data.items.AAA.qty,2);assert.equal(b.state().CLOUD.items.AAA.qty,2);
});
test('未知PUT远端SHA已变化先结束旧事务再上传新revision',async()=>{
  let b=await boot();b.respond(b.requests[0],meta(SEED));await settle();let old=await beginPut(b);await b.tick(b.state().deadline);
  await b.enter();b.edit('AAA','qty','3');await b.save();b.respond(b.requests.at(-1),meta({...SEED,cash:100},'external-sha'));await settle();
  assert.equal(b.puts().length,1);assert.equal(b.state().uncertain,null);assert.equal(b.state().dirty,true);
  await b.tick(30000);b.respond(b.requests.at(-1),meta({...SEED,cash:100},'external-sha'));await settle();assert.equal(b.payload(b.puts()[1]).items.AAA.qty,3);
  b.respond(b.puts()[1],{});await settle();b.respond(old,{});await settle();assert.equal(b.state().CLOUD.items.AAA.qty,3);assert.equal(b.state().dirty,false);
});
test('PUT未确认事务跨刷新不恢复，首次读取确认远端后直接同步',async()=>{
  let b=await boot();b.respond(b.requests[0],meta(SEED));await settle();let put=await beginPut(b);
  assert.ok(b.local()._local.uncertain);await b.dispose();assert.equal(put.init.signal.aborted,true);
  let c=await boot({store:b.store,now:NOW+61000});assert.equal(c.state().uncertain,null);
  c.respond(c.requests[0],meta(b.payload(put),'done-sha'));await settle();await c.tick(90000);
  assert.equal(c.puts().length,0);assert.equal(c.requests.length,1);assert.equal(c.state().dirty,false);
  assert.equal(c.local()._local.uncertain,null);assert.equal(c.state().SYNC,'synced');
});
test('PUT在途pagehide释放锁，新页接管后先读取而不恢复旧事务',async()=>{
  let b=await boot();b.respond(b.requests[0],meta(SEED));await settle();let put=await beginPut(b),c=await boot({store:b.store,now:NOW+61000});
  await b.dispose();assert.equal(b.state().activeRequests,0);assert.equal(b.state().inFlight,null);
  c.fireWindow('pageshow',{persisted:true});await settle();assert.equal(c.state().writer,true);
  assert.equal(c.state().uncertain,null);c.respond(c.requests[0],meta({...SEED,cash:555},'external-sha'));await settle();
  b.respond(put,{});await settle();await c.tick(90000);
  assert.equal(c.puts().length,0);assert.equal(c.state().DATA.cash,555);assert.equal(c.state().dirty,false);
});
test('未知PUT恢复GET再次超时仍保留事务且不再阻塞inFlight',async()=>{
  let b=await boot();b.respond(b.requests[0],meta(SEED));await settle();await beginPut(b);await b.tick(b.state().deadline);
  b.requestSave(0,true);await b.tick(0);let get=b.requests.at(-1);await b.tick(b.state().deadline);
  assert.equal(get.init.signal.aborted,true);assert.equal(b.state().inFlight,null);assert.ok(b.state().uncertain);assert.equal(b.puts().length,1);
});
test('未知PUT事务落盘失败时不发送请求',async()=>{
  let b=await boot();b.respond(b.requests[0],meta(SEED));await settle();await b.enter();b.edit();await b.save();
  b.store.failSet.add(LOCAL);b.respond(b.requests.at(-1),meta(SEED));await settle();assert.equal(b.puts().length,0);assert.equal(b.state().dirty,true);assert.equal(b.state().inFlight,null);
});
test('初始pull及保存请求超时均有界且自动重试次数有限',async()=>{
  let b=await boot();b.respond(b.requests[0],meta(SEED));await settle();await b.enter();b.edit();await b.save();await b.tick(600000);
  assert.equal(b.requests.length,4);assert.equal(b.state().inFlight,null);assert.equal(b.state().activeRequests,0);assert.equal(b.state().dirty,true);
});
test('旧ACK残留不启动维护PUT且后续clean外部更新不回退仓位',async()=>{
  const b=await boot({local:staleAck()}),remote=freshRemote();b.respond(b.requests[0],meta(remote,'base-sha'));await settle();await b.tick(600000);
  assert.equal(b.puts().length,0);assert.equal(b.state().uncertain,null);
  const newer=freshRemote();newer.cash=22222;newer.items.AAA.qty=77;b.mergeCloud(newer);b.paint();
  assert.equal(b.state().DATA.cash,22222);assert.equal(b.state().DATA.items.AAA.qty,77);
  await b.tick(600000);assert.equal(b.puts().length,0);assert.equal(b.state().dirty,false);
});
test('PUT超时的自动重试先GET核验，不跳过恢复事务',async()=>{
  let b=await boot();b.respond(b.requests[0],meta(SEED));await settle();let put=await beginPut(b);await b.tick(b.state().deadline);
  await b.tick(15000);let recovery=b.requests.at(-1);assert.notEqual(recovery.init.method,'PUT');assert.equal(b.puts().length,1);
  b.respond(recovery,meta(b.payload(put),'done-sha'));await settle();assert.equal(b.puts().length,1);assert.equal(b.state().dirty,false);
});
test('未知事务核验GET响应体超时仍不发第二个PUT',async()=>{
  let b=await boot();b.respond(b.requests[0],meta(SEED));await settle();await beginPut(b);await b.tick(b.state().deadline);
  b.requestSave(0,true);await b.tick(0);let req=b.requests.at(-1),resolve=pendingBody(b,req);await settle();await b.tick(b.state().deadline);
  assert.equal(b.state().inFlight,null);assert.equal(b.puts().length,1);assert.ok(b.state().uncertain);
  resolve(meta(SEED,'base-sha'));await settle();assert.equal(b.puts().length,1);
});
test('旧保存GET晚于新上传成功返回也不能发送旧PUT',async()=>{
  let b=await boot();b.respond(b.requests[0],meta(SEED));await settle();await b.enter();b.edit();await b.save();let old=b.requests.at(-1);
  await b.tick(b.state().deadline);await b.enter();b.edit('AAA','qty','8');await b.save();await finishPush(b);
  assert.equal(b.state().CLOUD.items.AAA.qty,8);b.respond(old,meta(SEED));await settle();assert.equal(b.puts().length,1);assert.equal(b.state().dirty,false);
});
test('AbortController缺失时仍按截止时间完成本地失败且忽略迟到网络',async()=>{
  let b=await boot();b.respond(b.requests[0],meta(SEED));await settle();b.ctx.AbortController=undefined;
  await b.enter();b.edit();await b.save();let get=b.requests.at(-1);await b.tick(b.state().deadline);
  assert.equal(b.state().inFlight,null);assert.equal(b.state().dirty,true);b.respond(get,meta(SEED));await settle();assert.equal(b.puts().length,0);
});
test('终稿四列编辑器右对齐数值，贡献列表金额百分比仍居中',()=>{
  assert.match(CSS,/#costManage \.ep-val\{[^}]*text-align:right/);
  assert.match(CSS,/#costManage \.ep-grid input,#costManage \.ep-cash input\{[^}]*text-align:right/);
  assert.match(CSS,/\.cost-contrib \.cv,\.cost-contrib \.ca\{[^}]*text-align:center/);
  assert.match(CSS,/\.cost-contrib \.cn-name\{[^}]*text-align:left/);
});
/* 旧六列成本表已被四列真实编辑器替代，下面为退役样式断言，不执行。 */
/*
test('所有数值CSS居中，列宽字号灯级与名称靠左保留',()=>{
  assert.equal(CSS.includes('text-align:right'),false);
  for(const selector of ['.cost-colhead span','.cost-block-title>span','.cost-cell','.cost-cell input','.cost-mini .c-cost','.cost-mini .t-mv','.cost-mini .t-pnl','.cost-mini .t-wt','.cost-mini input','.cost-contrib .cv,.cost-contrib .ca']){
    const escaped=selector.replace(/[.*+?^${}()|[\]\\]/g,'\\$&');assert.match(CSS,new RegExp(escaped+'\\{[^}]*text-align:center'));
  }
  assert.match(CSS,/\.cost-name\{[^}]*text-align:left/);assert.match(CSS,/\.cost-contrib \.cn-name\{[^}]*text-align:left/);
  // 2026-10-30 三轮：桌面档五个数值列各加 min 下限（56/52/76/126/48），
  // 601～768 这类窄桌面下纯 fr 会把盈亏格压到 101px，14px 的「+4.4% +$1,021.8」墨宽 109px 顶出去。
  // 2026-11-01：列宽提为 --cost-cols 变量（列头／框标题／列表行／现金固收四处共用一份，
  // 窄屏只改变量值）。桌面档六列仍各自带 min 下限，601～768 这类窄桌面不会把盈亏格压爆；
  // 名称列 1.55fr → 1.05fr（代码与中文名上下两行，用不着那么宽），省下的全给数值列。
  assert.match(CSS,/--cost-cols:minmax\(96px,1\.05fr\) minmax\(60px,1fr\) minmax\(56px,\.85fr\) minmax\(84px,1\.5fr\) minmax\(132px,1\.5fr\) minmax\(54px,\.9fr\)/);
  assert.match(CSS,/grid-template-columns:var\(--cost-cols\)/);
  assert.match(CSS,/grid-template-columns:var\(--cost-mini-cols\)/);
  // 同四轮：现金／固定收益行的市值列与主表同步加宽、盈亏占位列同步收窄（净和为零，仓位列起点不动）。
  assert.match(CSS,/--cost-mini-cols:minmax\(130px,1\.5fr\) minmax\(60px,\.95fr\) minmax\(52px,\.75fr\) minmax\(84px,1\.45fr\) minmax\(110px,1\.3fr\) minmax\(56px,\.9fr\)/);
  assert.match(CSS,/gap:0 var\(--cost-gap,8px\)/);
  assert.match(CSS,/\.cost-cell\.up\{color:var\(--up\)\}/);
  // 行首灯条（2026-10-28）：和统计面板个股前同款的 3×12px 竖条，不再是圆点 marker
  assert.match(CSS,/#costMembers li::before\{content:"";position:absolute;left:-9px;top:6px;\s*width:3px;height:12px;border-radius:1px;background:var\(--name-lamp,var\(--dim\)\)\}/);
  assert.match(CSS,/#costMembers li\.lv-red\{--name-lamp:var\(--red\)\}/);
  assert.match(CSS,/#costMembers li\.lv-yellow\{--name-lamp:var\(--yellow\)\}/);
  assert.match(CSS,/#costMembers li\.lv-green\{--name-lamp:var\(--green\)\}/);
  assert.match(CSS,/#costMembers li\.lv-gray\{--name-lamp:var\(--text\)\}/);
  assert.doesNotMatch(CSS,/#costMembers li\.lv-[a-z]+::marker/);
});
*/
test('数据时间和只读/同步状态跟随规则字号，定投在手机和桌面各加2px',()=>{
  assert.match(ASSETS,/--status-font-size:13\.5px/);
  assert.match(ASSETS,/:root\{--status-font-size:14\.5px\}/);
  assert.match(ASSETS,/\.sub\{[^}]*font-size:var\(--status-font-size\)/);
  assert.match(ASSETS,/\.runlight\{[^}]*font-size:var\(--status-font-size\)/);
  assert.match(CSS,/\.sub \.cost-state\{margin-left:auto;flex:none;font-size:inherit;?\}/);
  assert.match(CSS,/\.sub \.cost-state \.warn,\.sub \.cost-state \.ok,\.sub \.cost-state \.dim\{font-size:inherit;?\}/);
  assert.match(ASSETS,/\.runbar \.dca-badge\{font-size:16px;margin-left:auto\}/);
  assert.match(ASSETS,/\.runbar \.dca-badge\{margin-left:auto;font-size:18px;line-height:24px\}/);
});
test('盈亏贡献行首灯条按个股灯级上色，备注名不留ETF后缀且不中英混排',()=>{
  // 终稿当日贡献由dual-body承载；保留灯条，数值第二/三列，样式尺寸以440/1440真实harness验收。
  for(const [lv,v] of [['red','--red'],['yellow','--yellow'],['green','--green'],['gray','--text']])
    assert.ok(CSS.includes('.cost-contrib li.lv-'+lv+'{--name-lamp:var('+v+')'));
  assert.match(CSS,/\.cost-contrib li::before\{[^}]*width:3px;height:12px/);
  assert.match(CSS,/\.cost-contrib \.ca\{order:2/);assert.match(CSS,/\.cost-contrib \.cv\{order:3/);
  assert.match(CSS,/\.cost-contrib \.cn-name\{[^}]*text-align:left/);
  assert.ok(topLevel(CSS).includes('.sub .cost-state'));
  /* 旧六列成本卡及其旧桌面/手机微调已由终稿四列编辑器替代；历史断言退役。
  assert.match(CSS,/\.cost-contrib li::before\{content:"";position:absolute;left:-9px;top:50%;\s*width:3px;height:12px;border-radius:1px;background:var\(--name-lamp,var\(--dim\)\);/);
  for(const [lv,v] of [['red','--red'],['yellow','--yellow'],['green','--green'],['gray','--text']])
    assert.match(CSS,new RegExp('\\.cost-contrib li\\.lv-'+lv+'\\{--name-lamp:var\\('+v+'\\)\\}'));
  // 渲染用这只标的自己的灯级，不再全部灰灯
  assert.match(SOURCE,/lv:r\.lv\|\|'green'/);
  assert.match(SOURCE,/class="lv-'\+esc\(x\.lv\|\|'green'\)/);
  // 列宽：名称列不再独占剩余空间，数值列按多位数留足；顺序 名称 → 盈亏比 → 金额 → 图表
  // 2026-11-01：名称列 1.15fr → .8fr、gap 8 → 6，盈亏比列整体左移，省下的全给右侧贡献条
  assert.match(CSS,/grid-template-columns:minmax\(0,\.74fr\) 66px 62px minmax\(0,1fr\)/);
  // 手机档（2026-10-29）：名单文字 10.5 → 11.5px，右侧比例图相应缩短
  // （名称 : 图 = 1.15:1 → 1.3:0.85），数值列各让出 2px
  assert.match(CSS,/grid-template-columns:minmax\(0,1\.16fr\) 66px 66px minmax\(0,0\.66fr\);gap:0 6px;\s*font-size:13px;line-height:15\.75px\}/);
  // 中文备注名（.cncn）跟仓位面板 .cost-name .cn 同号 12px；代码单独 .cnsym 加粗。
  assert.match(CSS,/\.cost-contrib \.cncn\{font-size:12px\}/);
  assert.match(CSS,/\.cost-contrib li>\.cn-name,\.cost-contrib li>\.cv,\.cost-contrib li>\.ca\{height:15\.75px\}/);
  // 手机档只剩 ≤600 一档（2026-11-01 删掉 400/365/340/320 细分，只按 440 定稿）：
  // 比例图 0.66fr，名称列在 440 上约 182px，最长一行「LMT 洛克希德马丁」墨宽 113px 不会被截
  // 2026-11-01 二轮（盈亏比左移、与金额拉开距离）：盈亏比列 46 → 56px，
  // 名称列手机 1.3fr → 1.23fr / 桌面 .8fr → .77fr（收窄 10px 把整列左带），
  // 再用 .cv 的 padding-right:12px 把居中文字往左推 6px（合计左移约 10.6px）。
  // 实测：金额列左缘 692.4（1440）/ 263.8（440）一像素不动，两列墨迹间距 26 → 37 / 23.5 → 34.5。
  // （三轮「继续左移」：56 → 66px，名称列 1.23 → 1.16fr / .77 → .74fr，内边距 12 → 24px）
  assert.match(CSS,/minmax\(0,1\.16fr\) 66px 66px minmax\(0,0\.66fr\)/);
  assert.match(CSS,/minmax\(0,\.74fr\) 66px 62px minmax\(0,1fr\)/);
  assert.match(CSS,/\.cost-contrib li>\.cv\{padding-right:24px\}/);
  // 持仓市值列加宽（2026-11-01 晚）：手机 60 → 68px（这 8px 从名称列 1fr 出，不从数量列扣），
  // 桌面 fr 由 1.2 → 1.3（数量列 .95 → .85 让位）；现金／固定收益那套 mini-cols 同步。
  // 四轮再加一档：手机市值 68 → 82px（盈亏 92 → 78px 让位，固定列总和不变、名称列不受影响），
  // 桌面市值 1.3 → 1.5fr、盈亏 1.7 → 1.5fr；mini 市值 1.25 → 1.45fr、盈亏占位 1.5 → 1.3fr。
  assert.match(CSS,/--cost-cols:minmax\(0,1fr\) 52px 44px 82px 78px 50px/);
  assert.match(CSS,/--cost-mini-cols:minmax\(0,1fr\) 56px 44px 82px 52px 46px/);
  assert.match(CSS,/minmax\(84px,1\.5fr\)/);assert.match(CSS,/minmax\(84px,1\.45fr\)/);
  assert.ok(!/max-width:3[0-9][0-9]px|max-width:400px|max-width:365px/.test(CSS),
    '手机档不应再保留比 440 更窄的细分断点');
  // 持仓代码 13px（跟统计面板名单同号），手机中文备注名 12px。
  assert.match(CSS,/\.cost-name \.sy\{font-size:13px;font-weight:600/);
  assert.match(CSS,/\.cost-name \.sy\{font-size:13px\} \.cost-name \.cn\{font-size:12px\}/);
  assert.ok(!/\.cost-name \.sy\{font-size:12\.5px/.test(CSS),'仓位代码不应再是 12.5px');
  const iName=SOURCE.indexOf('\'<span class="cn-name">'), iCv=SOURCE.indexOf('\'<span class="cv '),
        iCa=SOURCE.indexOf('\'<span class="ca '), iBar=SOURCE.indexOf('\'<span class="cbar">');
  assert.ok(iName>=0&&iName<iCv&&iCv<iCa&&iCa<iBar,'渲染顺序应为 名称→盈亏比→金额→图表');
  // 每排之间一条淡分隔线
  assert.match(CSS,/\.cost-contrib li\+li\{border-top:1px solid rgba\(255,255,255,\.07\);padding-top:4px;margin-top:2px\}/);
  // 手机档（2026-10-29）：五个数值列再放宽 4～8px（数字 10.5 → 11.5px），名称列相应被挤压
  // 2026-10-30 二轮：标题行盈亏「+$1,021.8」13px 墨宽 63px 顶到格子边，盈亏列四档各再 +6px
  // 2026-10-30 三轮：仓位列再放宽（13px 的「37.8%」墨宽 35px，44/38/34px 格的内容区只剩
  // 34/34/30px 会顶出去），成本 / 数量 / 市值三列各让 2～4px 补回来，合计基本不变。
  // 2026-11-01：手机档只剩 ≤600 一档（400 / 365 / 340 / 320 细分全删，只按 440 定稿），
  // 列宽只覆盖 --cost-cols（不再各抄一条 grid-template-columns），名称列让位、盈亏列给到 92px。
  assert.match(CSS,/--cost-cols:minmax\(0,1fr\) 52px 44px 82px 78px 50px/);
  assert.match(CSS,/--cost-mini-cols:minmax\(0,1fr\) 56px 44px 82px 52px 46px/);
  // 手机档整个文件只有一条断点，且必须是 ≤600（不能残留更窄的细分档）
  assert.strictEqual((CSS.match(/@media[^{]*max-width:\d+px/g)||[]).length,
    (CSS.match(/@media[^{]*max-width:600px/g)||[]).length, '手机档只允许 ≤600 一个断点');
  // 标题行极端兜底：金额长到顶按钮时截省略号，而不是压到「编辑」上
  assert.match(CSS,/\.cost-manage>summary \.cost-sum\{min-width:0;overflow:hidden;text-overflow:ellipsis\}/);
  // 2026-11-02：「编辑」搬到顶栏后，标题行只剩「总资产·盈亏」→ 顶到最右，不留一截空白；
  // 顶栏那条 .runbar button 必须排除 .cost-edit-btn，否则会盖掉这颗的小圆角蓝边与字号
  assert.match(CSS,/\.cost-manage>summary \.group-stats\{justify-content:flex-end\}/);
  assert.match(ASSETS,/\.runbar button:not\(\.cost-edit-btn\)\{padding:6px 12px/);
  // 手机档那条同理：不排除会把同排三个按钮从 18px 压回 15px（特指度 0,2,1 > 0,1,1）。
  // 2026-11-02 定档 66×43：字号 16.5 → 18px、上下内距 7 → 8px（左右仍 14px）。
  assert.match(ASSETS,/\.runbar button:not\(\.cost-edit-btn\)\{padding:8px 14px;font-size:18px\}/);
  // 2026-11-02：「编辑」改成**固定高度 + 垂直居中**（手机 40 / 桌面 34），不再用 stretch
  // 撑满整行——同排三个按钮加到 43px 后，编辑要矮 3px 且居中，不是跟着一起变高。
  assert.match(CSS,/\.runbar \.cost-edit-btn\{align-self:center;box-sizing:border-box;height:34px\}/);
  assert.match(CSS,/\.cost-edit-btn\{padding:8px 13px;font-size:16px;line-height:1\.08\}/);
  assert.match(CSS,/\.runbar \.cost-edit-btn\{height:40px\}/);
  // 手机档按钮间距 10 → 12px（上限按「定投：999」三位数实测，富余 15px 不会换行）
  assert.match(ASSETS,/\.runbar\{padding-left:16px;padding-right:16px;gap:12px\}/);
  // 搬走后不再留「卡片内的编辑按钮」死规则
  assert.doesNotMatch(CSS,/\.cost-manage>summary \.cost-edit-btn/);
  assert.match(CSS,/\.cost-edit-btn\[hidden\]\{display:none\}/);
  // 「已同步 / 未同步」2026-11-02 起手机与桌面同一套：都搬到「数据时间 …」那排的最右侧，
  // 标题行只剩「总资产·盈亏 + 编辑」。JS 里不许再按 max-width 分档，CSS 规则必须两档共用（不在 @media 内）。
  const syncFn = SOURCE.slice(SOURCE.indexOf('function syncMirrorEl'), SOURCE.indexOf('function showNotice'));
  assert.ok(syncFn.length > 0 && !/max-width:600px/.test(syncFn), '同步状态胶囊不应再按手机档分支');
  assert.match(CSS,/\.sub \.cost-state\{margin-left:auto;flex:none;font-size:13px\}/);
  assert.ok(topLevel(CSS).includes('.sub .cost-state'), '.sub .cost-state 必须两档共用（不在手机 @media 内）');
  // 五个数值列的数字：11.5 → 13px（2026-11-01），跟「当日盈亏贡献」列表的数字同号；
  // 行高写死线上实测的 17.25px（＝线上 11.5px×1.5）——只加大字号、不加行距，整行仍是 42.5px
  assert.match(CSS,/\.cost-cell\{font-size:13px;line-height:17\.25px\}/);
  // 手机仓位指定辅助文字统一 12px；不改变桌面基础字号。
  for(const rule of [
    '.cost-colhead{font-size:12px;padding-left:12px}',
    '.cost-cell input{font-size:12px;padding:2px 3px}',
    '.cost-legend{font-size:12px;gap:3px 10px}',
    '.cost-mini .t-name .cnt{font-size:12px}',
    '.cash-inputs label>span,.cash-total{font-size:12px}',
    '.cost-foot{font-size:12px}'
  ]) assert.ok(CSS.includes(rule),rule);
  // 手机档「总持仓/长持仓/短持仓」：行高 15.5px + 上下 3/4px 内边距 = 38px，与线上一致
  // （11-01 早些时候压成 28px 那版被用户判定「变矮了」已还原，这里盯住别再改回去）
  assert.match(CSS,/\.cost-block-title\{font-size:13px;padding:3px 0 4px 12px;min-height:var\(--cost-head-h\);line-height:15\.5px\}/);
  assert.match(CSS,/\.cost-block-title \.pct,\.cost-block-title \.amt\{display:block;line-height:15\.5px\}/);
  // 现金／固定收益：字号加大到 13px（手机）/14px（桌面），行高仍锁线上实测值（17.25 / 18.75px）
  assert.match(CSS,/\.cost-mini\{font-size:13px;line-height:17\.25px;padding-left:12px\}/);
  assert.match(CSS,/font-size:14px;line-height:18\.75px/);
  assert.match(CSS,/\.cost-mini \.t-name \.cnt\{font-size:11\.5px;line-height:17\.25px/);
  */
  // 备注名：ETF 已有胶囊标签就不再重复写 ETF；含中文的只保留中文部分
  const read=f=>JSON.parse(fs.readFileSync(path.join(__dirname,'..',f),'utf8'));
  const names=read('symbol-names.json'), holdings=read('holdings.json');
  const latin=/[A-Za-z]/, cn=/[一-鿿]/;
  const dirty=v=>typeof v==='string'&&v&&((latin.test(v)&&cn.test(v))||v.indexOf('ETF')>=0);
  for(const [k,v] of Object.entries(names)) assert.equal(dirty(v),false,'symbol-names '+k+'='+v);
  let seen=0;
  for(const [g,items] of Object.entries(holdings)){
    if(g==='_说明'||g==='group_monitoring'||typeof items!=='object'||!items) continue;
    for(const [k,entry] of Object.entries(items)){
      if(!entry||typeof entry!=='object'||typeof entry.note!=='string') continue;
      seen++; assert.equal(dirty(entry.note),false,'holdings '+g+'/'+k+'='+entry.note);
      if(names[k]!==undefined) assert.equal(entry.note,names[k],'双源不一致 '+k);
    }
  }
  assert.ok(seen>50,'备注名条目过少：'+seen);
  assert.equal(names.SCHD,'嘉信红利');
  assert.equal(holdings.positions.SCHD.note,'嘉信红利');
});

// 目标状态由后端页面提供，面板只叠加自己的行情灯，不重新推断30/70或复活旧异常红。
function targetDocument(doc,state,opts={}){
  const row=doc.getElementById('s-aaa');
  const suffix={low:'',space:' pur pur-space',high:' pur pur-high',hit:' pur pur-hit',bad:''}[state];
  row.className=(opts.level||'green')+suffix;
  const tag=doc.createElement('span');tag.className='lvtag tgt '+state;
  tag.innerHTML=state==='bad'?'<span>250</span> <span>获取失败</span>':'<span>+'+(state==='high'?150:state==='space'?30:1)+'%</span> <span>250</span>';
  row.querySelector('td.sym').appendChild(tag);
  row.setAttribute('data-target-anomaly','true');
  if(opts.noTriggers){row.querySelector('.lvtag.add').remove();row.querySelector('.lvtag.cut').remove();}
  const counts=doc.getElementById('signalCounts');
  counts.innerHTML='<table></table><div class="purple-extra"><button class="signal-filter-toggle" data-kind="target_purple" aria-pressed="false">叠加条件</button><details class="text-only target-purple"><summary>空间1只　达到1只</summary><ul class="signal-member-list"><li>□<a class="signal-member-jump" href="#s-aaa">AAA</a></li></ul><ul class="signal-member-list"><li>■<a class="signal-member-jump" href="#s-aaa">AAA</a><span>今日跌破共识价 170</span></li></ul></details></div>';
  counts.setAttribute('data-filter',JSON.stringify({target_purple:{ok:true,members:[{id:'AAA',s:'AAA',d:['高空间','今日跌破共识价 170']}]}}));
}

test('目标high达到覆盖由页面决定而面板不从>100推断红灯',async()=>{
  for(const state of ['high','hit']){
    const b=await boot({token:false,prepareDocument:doc=>targetDocument(doc,state,{noTriggers:true})});
    const row=b.doc.getElementById('s-aaa');b.paint();
    assert.ok(row.className.split(/\s+/).includes('green'));
    assert.ok(!row.className.split(/\s+/).includes('red'));
    assert.equal(row.querySelector('.lvtag.tgt').className,'lvtag tgt '+state);
    assert.ok(row.className.split(/\s+/).includes('pur-'+state));
    assert.doesNotMatch(row.textContent,/疑似数据异常/);await b.dispose();
  }
});



test('目标union与行情及财报交集执行真实页面函数并身份唯一',()=>{
  const assets=fs.readFileSync(path.join(__dirname,'..','page_assets.py'),'utf8');
  const start=assets.indexOf('function intersectSignals('),end=assets.indexOf('function renderSignalFilter()',start);
  assert.ok(start>=0&&end>start,'真实页面必须提供交集函数');
  const context={};vm.createContext(context);vm.runInContext(assets.slice(start,end),context);
  const entry=(id,detail)=>({id,s:id,d:[detail]});
  const data={target_purple:{ok:true,label:'空间/达到',members:[entry('BOTH','高空间 / 今日跌破共识价'),entry('SPACE','空间'),entry('HIT','今日上穿共识价')]},
    chg:{ok:true,label:'涨跌幅',members:[entry('BOTH','涨跌幅 -20%'),entry('OTHER','涨跌幅 +5%')]},
    earnings_week:{ok:true,label:'财报',members:[entry('BOTH','财报日期'),entry('HIT','财报日期')]}};
  const before=copy(data),result=context.intersectSignals(data,['target_purple','chg','earnings_week']);
  assert.deepEqual(copy(result.members.map(m=>m.id)),['BOTH']);
  assert.equal(result.members.length,1);assert.match(result.members[0].details[0].t,/高空间.*跌破/);
  assert.deepEqual(data,before);
  const pending=context.intersectSignals({...data,target_purple:{...data.target_purple,ok:false}},['target_purple','chg']);
  assert.deepEqual(copy(pending.pending),['空间/达到']);
  assert.deepEqual(copy(context.intersectSignals(data,['target_purple','earnings_week']).members.map(m=>m.id)),['BOTH','HIT']);
});
test('页面内强制刷新清旧定位但保留凭据与无关参数，缓存清理失败仍可刷新',async()=>{
  const source=fs.readFileSync(path.join(__dirname,'..','monitor_render.py'),'utf8');
  const block=source.split('HARD_REFRESH_JS = (')[1].split('\n)')[0];
  const code=[...block.matchAll(/^\s*("(?:\\.|[^"\\])*")\s*$/gm)].map(m=>JSON.parse(m[1])).join('');
  assert.ok(code.includes('btnHardRefresh'));
  for(const fail of [false,true]){
    const button={},reloads=[];let removed=0,unregistered=0;
    const location={href:'https://example.test/index.html?focus=pos&edit=1&keep=1&token=synthetic#s-aaa',replace:url=>reloads.push(url)};
    const context={URL,Date,location,document:{getElementById:()=>button},
      window:{caches:{keys:async()=>{if(fail)throw Error('cache unavailable');return ['old'];},delete:async()=>{removed++;}}},
      navigator:{serviceWorker:{getRegistrations:async()=>[{unregister:async()=>{unregistered++;}}]}}};
    context.caches=context.window.caches;vm.runInNewContext(code,context);await button.onclick();
    const url=new URL(reloads[0]);assert.equal(url.hash,'');assert.equal(url.searchParams.has('focus'),false);
    assert.equal(url.searchParams.has('edit'),false);assert.equal(url.searchParams.get('_mm_reset_view'),'1');
    assert.equal(url.searchParams.get('keep'),'1');assert.equal(url.searchParams.get('token'),'synthetic');
    assert.ok(url.searchParams.get('_r'));assert.equal(removed,fail?0:1);assert.equal(unregistered,fail?0:1);
  }
});
test('明确刷新恢复统计和持仓父组展开、长短子区折叠，普通定位不复位',()=>{
  const source=fs.readFileSync(path.join(__dirname,'..','page_assets.py'),'utf8');
  const start=source.indexOf('(function resetBoardOnRefresh()'),end=source.indexOf('\n})();',start)+6;
  const code=source.slice(start,end);assert.ok(start>=0&&end>start);
  for(const requested of [false,true]){
    const stats={id:'signalCounts',open:false},group={id:'group_positions',open:false};
    const macro={id:'macroCard',open:true},cost={id:'costManage',open:true},long={open:true},short={open:true};
    const events=[],frames=[],scrolls=[],normalizations=[],replaced=[];
    const href='https://example.test/index.html?keep=1&mm_refreshed_run=2'+(requested?'&_mm_reset_view=1':'')+'#s-aaa';
    const history={state:{saved:true},scrollRestoration:'auto',replaceState:(state,unused,url)=>replaced.push({state,url})};
    const context={URL,history,document:{querySelectorAll:selector=>{
      if(selector==='details.card,details.cost-manage')return [stats,group,macro,cost];
      assert.equal(selector,'#group_positions_long,#group_positions_short');return [long,short];
    },getElementById:id=>id==='signalCounts'?stats:null},
      normalizeSignalCard:card=>normalizations.push(card),requestAnimationFrame:fn=>frames.push(fn),
      window:{location:{href},scrollTo:(x,y)=>scrolls.push([x,y]),addEventListener:(name,fn,options)=>events.push({name,fn,options})}};
    vm.runInNewContext(code,context);
    if(!requested){assert.equal(group.open,false);assert.equal(long.open,true);assert.equal(short.open,true);assert.equal(events.length,0);assert.equal(scrolls.length,0);continue;}
    assert.equal(stats.open,true);assert.equal(group.open,true);assert.equal(macro.open,false);assert.equal(cost.open,false);
    assert.equal(long.open,false);assert.equal(short.open,false);assert.equal(history.scrollRestoration,'manual');
    assert.equal(events[0].name,'pageshow');assert.equal(events[0].options.once,true);
    group.open=false;stats.open=false;long.open=true;short.open=true;events[0].fn();frames[0]();
    assert.equal(group.open,true);assert.equal(stats.open,true);assert.equal(long.open,false);assert.equal(short.open,false);
    assert.equal(history.scrollRestoration,'auto');
    const url=new URL(replaced[0].url);assert.equal(url.searchParams.has('_mm_reset_view'),false);
    assert.equal(url.searchParams.get('keep'),'1');assert.equal(url.searchParams.get('mm_refreshed_run'),'2');
    assert.deepEqual(replaced[0].state,{saved:true});assert.ok(scrolls.every(p=>p[0]===0&&p[1]===0));
    assert.equal(normalizations.length,2);
  }
});
test('目标名单跳转执行真实页面监听打开祖先及滚动到成员',()=>{
  const assets=fs.readFileSync(path.join(__dirname,'..','page_assets.py'),'utf8');
  const pos=assets.indexOf('const link = event.target.closest("a.signal-member-jump")');
  const start=assets.lastIndexOf('document.addEventListener(',pos),end=assets.indexOf('\n});',pos)+4;
  assert.ok(pos>=0&&start>=0&&end>pos,'真实页面必须提供成员跳转监听');
  const body={},group={tagName:'DETAILS',open:false,parentElement:body},quiet={tagName:'DETAILS',open:false,parentElement:group};
  const classes=new Set(),scrolls=[],timers=[],hashes=[];
  const row={tagName:'TR',parentElement:quiet,offsetWidth:1,classList:{add:c=>classes.add(c),remove:c=>classes.delete(c)},scrollIntoView:opts=>scrolls.push(opts)};
  let listener;
  const document={body,addEventListener:(name,fn)=>{assert.equal(name,'click');listener=fn;},getElementById:id=>id==='s-both'?row:null};
  vm.runInNewContext(assets.slice(start,end),{document,Element,history:{replaceState:(a,b,hash)=>hashes.push(hash)},window:{setTimeout:fn=>timers.push(fn)}});
  let prevented=false;
  const eventTarget=new Element('a',document);
  eventTarget.closest=selector=>{assert.equal(selector,'a.signal-member-jump');return {getAttribute:()=> '#s-both'};};
  listener({target:eventTarget,preventDefault:()=>{prevented=true;}});
  assert.equal(prevented,true);assert.equal(group.open,true);assert.equal(quiet.open,true);
  assert.deepEqual(copy(scrolls),[{block:'center',behavior:'smooth'}]);assert.deepEqual(hashes,['#s-both']);
  assert.ok(classes.has('stk-flash'));timers[0]();assert.equal(classes.size,0);
});
test('目标失败旧价灰胶囊不因面板重绘变成空间或达到',async()=>{
  const b=await boot({token:false,prepareDocument:doc=>targetDocument(doc,'bad',{noTriggers:true})});
  for(let i=0;i<3;i++)b.paint();
  const row=b.doc.getElementById('s-aaa');
  assert.equal(row.className,'green');
  assert.equal(row.querySelector('.lvtag.tgt.bad').textContent,'250 获取失败');
  assert.equal(row.querySelector('.lvtag.tgt.high,.lvtag.tgt.space,.lvtag.tgt.hit'),null);
});

// 构造与正式页面同位置的统计卡；保留其它筛选数据，不能把 data-filter 误放到 signalCounts。
const LOSS_FILTER_BASE={
  chg:{label:'涨跌幅',ok:true,members:[{id:'AAA',s:'AAA',n:'测试名称',g:'短持仓',l:'red',d:['涨跌幅 -5.0%']},{id:'BBB',s:'BBB',d:['涨跌幅 +5.0%']}]},
  target_purple:{label:'空间/达到',ok:true,members:[{id:'AAA',s:'AAA',d:[{l:'purple',t:'高空间 / 今日跌破共识价 170'}]},{id:'CCC',s:'CCC',d:[{l:'purple',t:'今日上穿共识价 250'}]}]},
  earnings_week:{label:'财报',ok:false,members:[]}
};
function lossStatDocument(doc,opts={}){
  const old=doc.getElementById('signalCounts'),card=doc.createElement('details');
  card.id='signalCounts';card.className='card group-card';card.open=true;
  card.innerHTML='<summary><span class="grp-name">统计</span><span class="group-stats"><span class="stat-badge red">99</span><span class="stat-badge yellow">99</span><span class="stat-badge purple">7</span></span></summary>'
    +'<table>'
    +'<tr><td><button class="signal-filter-toggle" data-kind="chg" aria-pressed="false">涨跌幅</button></td><td class="num">2</td><td class="num">—</td><td class="num">—</td></tr></table>'
    +'<div class="purple-extra"><button class="signal-filter-toggle" data-kind="target_purple" aria-pressed="false">空间/达到</button></div>'
    +'<div id="signalFilterResult" class="signal-filter" role="status"></div><div class="group-bottom"><button class="collapse-group" data-target="signalCounts">收起</button></div>';
  card.setAttribute('data-filter',JSON.stringify({sentinel:'不要更新这个属性'}));
  old.parentNode.replaceChild(card,old);
  const box=doc.getElementById('signalFilterResult');box.hidden=true;
  box.setAttribute('data-filter',JSON.stringify(opts.filter||LOSS_FILTER_BASE));
  for(const row of doc.querySelectorAll('tr[id^="s-"]')){
    const sym=row.id.slice(2).toUpperCase();
    row.querySelector('.px-price').textContent=String(opts.prices?.[sym]??(sym==='AAA'?90:100));
    row.querySelector('.note').textContent=opts.notes?.[sym]??'测试名称';
    row.className=opts.levels?.[sym]||'green';
    // 单独验证浮亏，不让默认加仓价95在 price90 时额外报红。
    row.querySelector('.lvtag.add')?.remove();row.querySelector('.lvtag.cut')?.remove();
  }
  for(const sym of opts.longs||[]){
    let sub=doc.getElementById('group_positions_long');
    if(!sub){sub=doc.createElement('details');sub.id='group_positions_long';sub.innerHTML='<summary>长持仓</summary><table class="stk"></table>';doc.getElementById('group_positions').appendChild(sub);}
    moveRow(doc,sym,'positions_long');
  }
  if(opts.prepareDocument)opts.prepareDocument(doc);
}
async function bootLoss(opts={}){
  const {prices,notes,levels,longs,filter,prepareDocument,...rest}=opts;
  return boot({...rest,noLocks:true,protocol:'file:',forbidRequests:true,
    prepareDocument:doc=>lossStatDocument(doc,{prices,notes,levels,longs,filter,prepareDocument})});
}






function setLossPrice(b,sym,price){
  // 行情在启动时被真实脚本扫描为行对象；模拟下一轮价格，不复制浮亏判断公式。
  for(const row of b.rows.filter(r=>r.sym===sym)){
    row.price=price;row.tr.querySelector('.px-price').textContent=String(price);
    row.quoteDate=new Date(Date.parse(row.quoteDate+'T00:00:00Z')+86400000).toISOString().slice(0,10);
  }
  b.paint();
}


function realIntersectSignals(){
  const assets=fs.readFileSync(path.join(__dirname,'..','page_assets.py'),'utf8');
  const start=assets.indexOf('function intersectSignals('),end=assets.indexOf('function renderSignalFilter()',start);
  assert.ok(start>=0&&end>start,'真实页面必须提供交集函数');
  const context={};vm.createContext(context);vm.runInContext(assets.slice(start,end),context);
  return context.intersectSignals;
}

function loadSignalFilterPage(doc){
  const assets=fs.readFileSync(path.join(__dirname,'..','page_assets.py'),'utf8');
  const start=assets.indexOf('function signalFilterData('),end=assets.indexOf('document.addEventListener("click"',start);
  assert.ok(start>=0&&end>start,'执行真实筛选数据读取、交集、箭头及DOM渲染函数');
  const context={document:doc,Element};context.window=context;
  vm.createContext(context);vm.runInContext(assets.slice(start,end),context);
  return context;
}

test('结构化交集扁平保留每项红灰黄与同条件双事实，顺序不借整股灯',()=>{
  const intersect=realIntersectSignals(),entry=(d)=>({id:'AAA',s:'AAA',l:'red',d});
  const data={
    amp:{label:'振幅',ok:true,members:[entry([{l:'yellow',t:'振幅 7.0%'}])]},
    boll_rsi:{label:'布林+RSI',ok:true,members:[entry([{l:'gray',t:'布林上轨▲ · RSI 3条 ↓超卖'}])]},
    chg:{label:'涨跌幅',ok:true,members:[entry([{l:'red',t:'涨跌幅 +6.0%'}])]},
    vol_ratio:{label:'量比',ok:true,members:[entry([{l:'yellow',t:'量比 2.0×'}])]},
    target_purple:{label:'空间/达到',ok:true,members:[entry([{l:'purple',t:'高空间'},{l:'purple',t:'今日上穿共识价 150'}])]},
    earnings_week:{label:'财报',ok:true,members:[entry([{l:'',t:'财报 2026-10-30 盘后'}])]}
  };
  const before=copy(data),kinds=Object.keys(data);
  const expected=kinds.flatMap(k=>data[k].members[0].d.map(d=>({...d,k})));
  for(const selected of [kinds,[...kinds].reverse()]){
    const result=intersect(data,selected);
    assert.deepEqual(copy(result.members.map(m=>m.id)),['AAA']);
    assert.deepEqual(copy(result.members[0].details),selected===kinds?expected:
      selected.flatMap(k=>data[k].members[0].d.map(d=>({...d,k}))));
  }
  assert.equal(expected.filter(d=>d.k==='target_purple').length,2,'不能把同条件双事实拼成一段或丢失第二项');
  assert.deepEqual(data,before,'交集不修改来源数组、结构化事实或原始整股灯');
  assert.deepEqual(copy(intersect({...data,vol_ratio:{...data.vol_ratio,ok:false}},['amp','vol_ratio'])),{pending:['量比']});
  assert.deepEqual(copy(intersect({...data,vol_ratio:{...data.vol_ratio,members:[{id:'BBB',s:'BBB',d:[]}]}},['amp','vol_ratio']).members),[]);
});

test('真实多选DOM无圆点且每段级别独立，箭头斜线与安全文本保留、财报不借灯',()=>{
  const doc=makeDocument({symbols:[]}),box=doc.createElement('div');box.id='signalFilterResult';doc.body.appendChild(box);
  const unsafe='<img src=x onerror=bad()> & <script>bad()</script>';
  const facts={amp:[{l:'yellow',t:'振幅 7.0%'}],boll_rsi:[{l:'gray',t:'布林上轨▲ · RSI 3条 ↓超卖'}],
    chg:[{l:'red',t:'涨跌幅 +6.0%'}],vol_ratio:[{l:'yellow',t:'量比 2.0×'}],
    target_purple:[{l:'purple',t:'高空间'},{l:'purple',t:'今日上穿共识价 150'}],
    earnings_week:[{l:'',t:'财报 '+unsafe}]};
  const data={};
  for(const [k,d] of Object.entries(facts)){
    data[k]={label:k,ok:true,members:[{id:'AAA',s:'AAA',n:unsafe,g:'短持仓',a:'s-aaa',l:'red',d}]};
    const button=doc.createElement('button');button.className='signal-filter-toggle';
    button.setAttribute('data-kind',k);button.setAttribute('aria-pressed','true');doc.body.appendChild(button);
  }
  const expected=Object.entries(facts).flatMap(([k,parts])=>parts.map(part=>({...part,k})));
  const json=JSON.stringify(data);box.setAttribute('data-filter',json);
  const context=loadSignalFilterPage(doc),renders=doc.renders;
  for(let i=0;i<3;i++){
    context.renderSignalFilter();
    const li=box.querySelector('li'),detail=li.querySelector('.signal-filter-detail');
    const conditions=detail.querySelectorAll('.signal-filter-condition');
    assert.equal(box.hidden,false);assert.equal(box.querySelectorAll('li').length,1);
    assert.equal(li.firstChild.className,'signal-name-lamp red','名称前竖条保留既有整股灯色，不能覆盖分条件文字级别');
    assert.equal(li.querySelectorAll('.signal-name-lamp').length,1,'重复渲染不叠加名称竖条');
    assert.equal(li.firstChild.getAttribute('role'),'img');assert.equal(li.firstChild.getAttribute('aria-label'),'红灯');
    assert.equal(box.querySelectorAll('.signal-dot').length,0,'全部指标和财报段均无圆点');
    assert.equal(detail.querySelector('[role="img"]'),null,'描述中不保留原圆点的无障碍图像节点');
    assert.deepEqual(conditions.map(c=>({k:c.dataset.signalKind,l:c.dataset.signalLevel,t:c.textContent})),expected,
      '红黄灰、两条目标紫和财报空级逐段准确，不能借整股red');
    for(const condition of conditions){
      assert.equal(condition.getAttribute('data-signal-kind'),condition.dataset.signalKind);
      assert.equal(condition.getAttribute('data-signal-level'),condition.dataset.signalLevel,'dataset写入真实属性供CSS匹配，空级也保留');
    }
    assert.deepEqual(detail.childNodes.filter(n=>n.nodeType===3).map(n=>n.textContent),Array(expected.length-1).fill(' / '));
    assert.equal(detail.textContent,expected.map(d=>d.t).join(' / '),'移除圆点不增删事实、斜线或文本空格');
    assert.equal(detail.querySelector('.arrow-rsi-down').textContent,'↓');
    assert.equal(box.querySelector('img,script'),null,'恶意数据不能形成元素');
    assert.equal(li.querySelector('.note').textContent,unsafe);
    assert.doesNotMatch(li.textContent,/灰灯|白灯|红灯|黄灯|紫灯|布林\+RSI|\[object Object\]/);
    assert.equal(box.getAttribute('data-filter'),json);
  }
  facts.boll_rsi[0]={l:'yellow',t:'布林下轨▼ · RSI 2条 ↑超买'};
  data.boll_rsi.members[0].d=facts.boll_rsi;box.setAttribute('data-filter',JSON.stringify(data));
  context.renderSignalFilter();
  const updated=box.querySelector('.signal-filter-condition[data-signal-kind="boll_rsi"]');
  assert.equal(updated.dataset.signalLevel,'yellow');assert.equal(updated.textContent,facts.boll_rsi[0].t);
  assert.equal(updated.querySelector('.arrow-rsi-up').textContent,'↑');
  assert.equal(box.querySelector('.arrow-rsi-down,.signal-dot'),null,'再次渲染不残留旧箭头或圆点');
  assert.equal(doc.renders,renders,'真实多选渲染不通过innerHTML解析任何数据');
});

test('旧字符串交集仍兼容，技术灰与目标紫及无交易灯财报按条件归因',()=>{
  const intersect=realIntersectSignals(),entry=(l,d)=>({id:'AAA',s:'AAA',l,d});
  const data={
    amp:{label:'振幅',ok:true,members:[entry('red',['灰灯 · 振幅 · 振幅 1.5%'])]},
    boll_rsi:{label:'布林+RSI',ok:true,members:[entry('red',['灰灯 · 布林+RSI · 上轨▲ · RSI 3条 ↓超卖'])]},
    chg:{label:'涨跌幅',ok:true,members:[entry('yellow',['涨跌幅 +3.0%'])]},
    target_purple:{label:'空间/达到',ok:true,members:[entry('purple',['高空间','今日跌破共识价 170'])]},
    earnings_week:{label:'财报',ok:true,members:[entry('',['财报日期'])]}
  },before=copy(data);
  const details=copy(intersect(data,Object.keys(data)).members[0].details);
  assert.deepEqual(details,[
    {l:'gray',t:'振幅 1.5%',k:'amp'},
    {l:'gray',t:'布林上轨▲ · RSI 3条 ↓超卖',k:'boll_rsi'},
    {l:'yellow',t:'涨跌幅 +3.0%',k:'chg'},
    {l:'purple',t:'高空间',k:'target_purple'},
    {l:'purple',t:'今日跌破共识价 170',k:'target_purple'},
    {l:'',t:'财报日期',k:'earnings_week'}]);
  assert.deepEqual(data,before);
  for(const prefix of ['灰灯','白灯']){
    const compatible=copy(data);
    for(const kind of ['amp','boll_rsi']){
      compatible[kind].members[0].d=compatible[kind].members[0].d.map(text=>text.replace(/^灰灯/,prefix));
    }
    const compatibleBefore=copy(compatible),result=copy(intersect(compatible,Object.keys(compatible)).members[0].details);
    assert.deepEqual(result,details,prefix+'前缀保留同一交集事实及条件归因');
    assert.deepEqual(result.filter(fact=>['amp','boll_rsi'].includes(fact.k)).map(fact=>fact.l),['gray','gray']);
    assert.deepEqual(compatible,compatibleBefore,prefix+'输入的旧字符串、成员与整股等级均不突变');
  }
});

test('终稿已合并均线单一身份交集保留不同颜色事实且不突变',()=>{
  // ma_cross/ma_band在服务端合并，成本脚本不再维护第二份alias/merge算法。
  const intersect=realIntersectSignals(),cross={l:'yellow',t:'上穿短均线'},touch={l:'yellow',t:'日内触及长均线'};
  const data={ma:{ok:true,members:[{id:'BRK-B',s:'BRK-B',l:'red',d:[cross,touch,{l:'red',t:cross.t}]}]},
    chg:{ok:true,members:[{id:'BRK-B',s:'BRK-B',l:'red',d:[{l:'red',t:'涨跌幅 +6.0%'}]}]}};
  const before=copy(data),result=copy(intersect(data,['ma','chg']));
  assert.equal(result.members.length,1);assert.equal(result.members[0].id,'BRK-B');
  assert.deepEqual(result.members[0].details,[{...cross,k:'ma'},{...touch,k:'ma'},{l:'red',t:cross.t,k:'ma'},{l:'red',t:'涨跌幅 +6.0%',k:'chg'}]);
  assert.deepEqual(data,before);assert.doesNotMatch(SOURCE,/function\s+mergeMaFilters/);
});



test('DOM insertBefore只接受直接子节点，NotFoundError前不移动待插入节点',()=>{
  const doc=makeDocument(),parent=doc.createElement('div'),origin=doc.createElement('div'),node=doc.createElement('span');
  parent.innerHTML='<div class="group-bottom"><button class="collapse-group">收起</button></div>';origin.appendChild(node);
  const bottom=parent.querySelector('.group-bottom'),button=bottom.querySelector('button');
  assert.equal(parent.contains(button),true,'参照按钮确实是后代，但不是直接子节点');
  assert.throws(()=>parent.insertBefore(node,button),{name:'NotFoundError'});
  assert.equal(node.parentNode,origin);assert.deepEqual(origin.children,[node]);assert.deepEqual(parent.children,[bottom]);
  assert.equal(parent.insertBefore(node,bottom),node);assert.deepEqual(parent.children,[node,bottom]);
  assert.equal(parent.insertBefore(node,node),node);assert.deepEqual(parent.children,[node,bottom]);
  assert.equal(parent.insertBefore(node,null),node);assert.deepEqual(parent.children,[bottom,node]);
});

















test('主表不再挂成本价，成本价只在仓位面板里编辑与查看',async()=>{
  const d=copy(SEED);d.items={AAA:{cost:80,qty:7.004}};
  const b=await boot({local:d});await b.tick(16);
  const tr=b.doc.getElementById('s-aaa');
  assert.equal(tr.querySelector('.cost-inline'),null,'主表行不再注入成本价节点');
  assert.equal(tr.querySelector('td.sym .lvs'),null,'代码那一列不再有成本价／胶囊容器');
  // 成本价归仓位面板：那一行仍有成本价输入框
  await b.enter();const row=b.doc.querySelector('#costMembers .ep-grid [data-cost-row][data-sym="AAA"]');
  assert.ok(row,'终稿四列编辑器里仍有这只标的');
  await b.enter();
  assert.equal(b.doc.querySelector('input[data-sym="AAA"][data-f="cost"]').value,'80');
  assert.equal(b.doc.querySelector('input[data-sym="AAA"][data-f="qty"]').value,'7.004');
  assert.equal(b.doc.querySelectorAll('.ep-grid .ep-h').length,4);
  await b.dispose();
});
test('没有持仓成本时主表不出现成本价节点，也不留空容器',async()=>{
  const b=await boot({local:{...copy(SEED),items:{}}});await b.tick(16);
  for(const sym of ['aaa','bbb']){
    const tr=b.doc.getElementById('s-'+sym);
    assert.equal(tr.querySelector('.cost-inline'),null,sym+' 主表不再有成本价节点');
    assert.equal(tr.querySelector('td.sym .lvs'),null,sym+' 不新建空容器占位');
  }
  await b.dispose();
});





// 双现金测试仅使用内存存储和 mock 下载，禁止真实网络和浏览器存储。
async function bootCash(opts={}){return boot({noLocks:true,protocol:'file:',forbidRequests:true,...opts});}
function cashInput(b,field){return b.doc.querySelector('input[data-sym="USD"][data-f="'+field+'"]');}
function assertCash(b,first,second,total){
  const d=b.state().DATA;assert.equal(d.cash,total);assert.equal(d.cash_extra,second);assert.equal(b.cashFirst(d),first);
  assert.equal(b.sessionLocal().cash,total);assert.equal(b.sessionLocal().cash_extra,second);
}
for(const [name,first,second,total] of [
  ['只填第一','12.3456','',12.3456],['只填第二','','12.3456',12.3456],
  ['两框都填','12.3456','7.6543',19.9999],['小数相加','0.1','0.2',0.3],['最小精度','0.0001','0.0002',0.0003]
])test('双现金'+name+'合计四位且非编辑态只展示总额',async()=>{
  const b=await bootCash({local:{...copy(SEED),cash:0}});await b.enter();
  assert.equal(cashInput(b,'cash').value,'');assert.equal(cashInput(b,'cash_extra').value,'');
  b.edit('USD','cash',first);b.edit('USD','cash_extra',second);await b.tick(16);
  assertCash(b,Number(first),Number(second),total);
  assert.equal(b.doc.querySelector('.ep-cash-total').textContent,'现金合计 USD$'+String(total));
  assert.equal(b.doc.querySelectorAll('.ep-cash label').length,3);
  assert.deepEqual(b.doc.querySelectorAll('.ep-cash label').map(el=>el.querySelector('span').textContent),['现金 1','现金 2','固收成本']);
  await b.save();assert.equal(b.state().editing,false);assert.equal(b.doc.getElementById('costManage').classList.contains('cost-edit-open'),false);
  assert.equal(b.doc.querySelector('.cashbond-row .cb-cash .cb-amt').textContent,'$'+String(Math.round(total)));
  assert.equal(b.requests.length,0);assert.equal(b.store.writes.length,0);
});
test('双现金第二先填再填第一，不重复加回旧合计',async()=>{
  const b=await bootCash({local:{...copy(SEED),cash:0}});await b.enter();
  b.edit('USD','cash_extra','0.2');assertCash(b,0,0.2,0.2);
  b.edit('USD','cash','0.1');assertCash(b,0.1,0.2,0.3);
  for(const extra of ['0.3','0.4','0.2']){b.edit('USD','cash_extra',extra);assert.equal(b.cashFirst(b.state().DATA),0.1);}
  await b.tick(16);assert.equal(cashInput(b,'cash').value,'0.1');assertCash(b,0.1,0.2,0.3);
});
test('双现金cashFirst扣减消除四位浮点尾差且不会负零',async()=>{
  const b=await bootCash({local:{...copy(SEED),cash:0.30000000000000004,cash_extra:0.2}});await b.enter();
  assert.equal(b.cashFirst(b.state().DATA),0.1);assert.equal(cashInput(b,'cash').value,'0.1');
  b.edit('USD','cash_extra','0.4');assertCash(b,0.1,0.4,0.5);
  assert.equal(b.cashFirst({cash:0.2,cash_extra:0.2}),0);assert.equal(Object.is(b.cashFirst({cash:0.2,cash_extra:0.2}),-0),false);
});
for(const field of ['cash','cash_extra'])test('双现金清空'+field+'只减对应一笔，再清空全部为零',async()=>{
  const b=await bootCash({local:{...copy(SEED),cash:0.3,cash_extra:0.2}});await b.enter();
  b.edit('USD',field,'');await b.tick(16);
  assertCash(b,field==='cash'?0:0.1,field==='cash'?0.2:0,field==='cash'?0.2:0.1);
  b.edit('USD',field==='cash'?'cash_extra':'cash','');await b.tick(16);assertCash(b,0,0,0);
  await b.save();await b.dispose();const c=await bootCash({store:b.store,session:b.session});await c.enter();
  assertCash(c,0,0,0);assert.equal(cashInput(c,'cash').value,'');assert.equal(cashInput(c,'cash_extra').value,'');
});
test('双现金同总额改拆分取消能恢复两笔并恢复原dirty',async()=>{
  const b=await bootCash({local:{...copy(SEED),cash:1,cash_extra:0.2}});await b.enter();
  b.edit('USD','cash','0.6');b.edit('USD','cash_extra','0.4');assertCash(b,0.6,0.4,1);
  b.doc.getElementById('costCancel').click();assertCash(b,0.8,0.2,1);assert.equal(b.state().dirty,false);
  await b.enter();assert.equal(cashInput(b,'cash').value,'0.8');assert.equal(cashInput(b,'cash_extra').value,'0.2');
});
test('双现金折叠保留拆分草稿，刷新不合并成第一笔也不上传',async()=>{
  const b=await bootCash({local:{...copy(SEED),cash:0.3,cash_extra:0.2}});await b.enter();
  b.edit('USD','cash','0.1234');b.edit('USD','cash_extra','0.5678');
  const panel=b.doc.getElementById('costManage');panel.open=false;panel.fire('toggle');await b.tick(90000);
  assertCash(b,0.1234,0.5678,0.6912);assert.equal(b.state().editing,false);assert.equal(b.state().savePaused,true);
  await b.dispose();const c=await bootCash({store:b.store,session:b.session});await c.enter();
  assertCash(c,0.1234,0.5678,0.6912);assert.equal(cashInput(c,'cash').value,'0.1234');assert.equal(cashInput(c,'cash_extra').value,'0.5678');
  assert.equal(b.requests.length+c.requests.length,0);assert.equal(b.store.writes.length,0);
});
for(const [field,value] of [['cash','0.00001'],['cash_extra','-1'],['cash_extra','2oops'],['cash','1e309']])test('双现金拒绝'+field+'='+value+'并保留原拆分',async()=>{
  const b=await bootCash({local:{...copy(SEED),cash:0.3,cash_extra:0.2}});await b.enter();
  const before=b.canonical(b.state().DATA),input=b.edit('USD',field,value);assert.ok(input.validationMessage);
  b.paint();await b.save();assert.equal(b.state().editing,true);assert.equal(b.canonical(b.state().DATA),before);
});
for(const version of [1,2])test('双现金v'+version+'旧格式extra缺失迁移为零，不从seed重复补第二笔',async()=>{
  const seed={...copy(SEED),cash:90,cash_extra:80};
  const b=await bootCash({local:{...copy(SEED),schema_version:version,cash:12.3456},seed});await b.enter();
  assertCash(b,12.3456,0,12.3456);assert.equal(cashInput(b,'cash').value,'12.3456');assert.equal(cashInput(b,'cash_extra').value,'');
});
for(const value of [-1,0.4,'0.2',null,Infinity])test('双现金拒绝非法cash_extra '+String(value)+'导入且原值不动',async()=>{
  const b=await bootCash({local:{...copy(SEED),cash:0.3,cash_extra:0.2}}),before=b.canonical(b.state().DATA);
  assert.throws(()=>b.importData({...copy(SEED),cash:0.3,cash_extra:value}));assert.equal(b.canonical(b.state().DATA),before);
});
test('双现金导入导出刷新等值，不导出local事务字段且旧备份恢复第一框',async()=>{
  const b=await bootCash();b.importData({...copy(SEED),schema_version:2,cash:0.3,cash_extra:0.2});
  b.exportData();assert.equal(b.blobs.length,1);
  const exported=JSON.parse(await b.blobs[0].text());assert.equal(exported.cash,0.3);assert.equal(exported.cash_extra,0.2);assert.equal(exported._local,undefined);
  assert.equal(b.canonical(exported),b.canonical(b.state().DATA));
  await b.dispose();const c=await bootCash({store:b.store,session:b.session});assertCash(c,0.1,0.2,0.3);
  c.importData({...copy(SEED),cash:7});await c.enter();assertCash(c,7,0,7);assert.equal(cashInput(c,'cash_extra').value,'');
});
test('双现金clean云端拆分整份替换，旧云端只还原第一框',async()=>{
  const b=await boot({local:{...copy(SEED),cash:1,cash_extra:0.2},token:false});
  const rev=b.state().revision;b.mergeCloud({...copy(SEED),cash:1,cash_extra:0.4});b.paint();
  assert.equal(b.state().DATA.cash,1);assert.equal(b.state().DATA.cash_extra,0.4);assert.ok(b.state().revision>rev);
  b.mergeCloud({...copy(SEED),cash:1});b.paint();assert.equal(b.state().DATA.cash_extra,0);
  await b.enter();assert.equal(cashInput(b,'cash').value,'1');assert.equal(cashInput(b,'cash_extra').value,'');
});
test('双现金同总额改拆分必须mock PUT，不因canonical相同漏同步',async()=>{
  const d={...copy(SEED),cash:1,cash_extra:0.2},b=await boot({local:d});b.respond(b.requests[0],meta(d));await settle();
  const before=b.canonical(b.state().DATA);await b.enter();b.edit('USD','cash','0.6');b.edit('USD','cash_extra','0.4');
  assert.equal(b.state().DATA.cash,1);assert.notEqual(b.canonical(b.state().DATA),before);await b.save();
  b.respond(b.requests.at(-1),meta(d));await settle();const put=b.puts()[0];assert.ok(put);
  assert.equal(b.payload(put).cash,1);assert.equal(b.payload(put).cash_extra,0.4);b.respond(put,{});await settle();assert.equal(b.state().dirty,false);
  await b.dispose();const c=await boot({store:b.store,token:false});assert.equal(c.cashFirst(c.state().DATA),0.6);assert.equal(c.state().DATA.cash_extra,0.4);
});
test('双现金在途旧拆分回执不清除同总额新拆分dirty',async()=>{
  const d={...copy(SEED),cash:1,cash_extra:0.2},b=await boot({local:d});b.respond(b.requests[0],meta(d));await settle();
  await b.enter();b.edit('USD','cash','0.6');b.edit('USD','cash_extra','0.4');await b.save();b.respond(b.requests.at(-1),meta(d));await settle();const old=b.puts()[0];
  await b.enter();b.edit('USD','cash','0.7');b.edit('USD','cash_extra','0.3');assert.equal(b.state().DATA.cash,1);
  b.respond(old,{});await settle();assert.equal(b.state().CLOUD.cash_extra,0.4);assert.equal(b.state().DATA.cash_extra,0.3);assert.equal(b.state().dirty,true);
});


test('加减精确比较支持科学计数、小数阈值和零阈值，不以epsilon扩大范围',async()=>{
  const b=await bootCash();
  // 终稿以现价为分母，对称±范围；不再使用已删除的单向越界语义。
  for(const [p,t,g,on] of [[1e-7,1.03e-7,3,true],[1e-7,1.0300000001e-7,3,false],
    [1e-7,9.7e-8,3,true],[1e-7,9.699999999e-8,3,false],
    [100,102.5,2.5,true],[100,97.5,2.5,true],[100,100,0,true],
    [100,100.0000000001,0,false],[100,99.9999999999,0,false]])assert.equal(b.nearPrice(p,t,g),on);
});
function signalCaps(doc,symbols=['AAA'],target=100){
  for(const sym of symbols){
    const td=doc.getElementById('s-'+sym.toLowerCase()).querySelector('td.sig');
    td.innerHTML='<span class="lvtag add">加 '+target+'</span><span class="lvtag cut">减 '+target+'</span>';
  }
}




test('成本多次paint保留服务端价格主次角色，不重排静态描述或新增灰灯',async()=>{
  for(const [chgLevel,ampLevel,primaryKind] of [['red','red','chg'],['red','yellow','chg'],['yellow','red','amp'],['yellow','yellow','chg']]){
    let original;
    const other=primaryKind==='chg'?'amp':'chg';
    const b=await bootLoss({prices:{AAA:100},levels:{AAA:chgLevel==='red'||ampLevel==='red'?'red':'yellow'},prepareDocument(doc){
      const td=doc.getElementById('s-aaa').querySelector('td.sig');
      const make=(kind,level,role)=>'<span class="signal-item" data-signal-kind="'+kind+'" data-signal-level="'+level+'"'+(role?' data-signal-role="'+role+'"':'')+'>'+kind+'</span>';
      td.innerHTML=make('amp',ampLevel,primaryKind==='amp'?'primary':'supplement')+' / '+make('ma','red','')+' / '
        +make('chg',chgLevel,primaryKind==='chg'?'primary':'supplement')+' / '+make('target','purple','');
      original=[...td.querySelectorAll('.signal-item')];
    }});
    const td=b.rows[0].tr.querySelector('td.sig');
    for(let i=0;i<4;i++){
      b.paint();
      const items=td.children.filter(el=>el.matches('.signal-item')&&el.style.display!=='none');
      const kinds=items.map(el=>el.getAttribute('data-signal-kind'));
      assert.deepEqual(kinds,['amp','ma','chg','target']);
      assert.equal(items.find(el=>el.getAttribute('data-signal-kind')===primaryKind).getAttribute('data-signal-role'),'primary');
      assert.equal(items.find(el=>el.getAttribute('data-signal-kind')===other).getAttribute('data-signal-role'),'supplement');
      for(const [kind,level] of [['chg',chgLevel],['amp',ampLevel]])assert.equal(items.find(el=>el.getAttribute('data-signal-kind')===kind).getAttribute('data-signal-level'),level);
      assert.equal(items.filter(el=>el.getAttribute('data-signal-level')==='gray').length,0);
      assert.equal(items.at(-1).getAttribute('data-signal-kind'),'target');
      // 终稿双列流不注入旧版斜线/逗号分隔节点。
      assert.equal(td.querySelectorAll('.signal-separator').length,0);
      for(const el of original)assert.equal(el.parentNode,td);
    }
    assert.equal(b.requests.length,0);assert.equal(b.store.writes.length,0);
  }
});




// 折叠口径仅来自生成位置或明确 data-quiet-keep；不从胶囊、class猜主表保留资格。










test('成本重绘不借未知跳转伪造主表灯，既有静态名单不被重写',async()=>{
  const b=await bootCash({prepareDocument(doc){
    const section=doc.createElement('div');section.id='lamp-test-yellow';doc.body.appendChild(section);
    const yellow=doc.getElementById('lamp-test-yellow'),node=doc.createElement('div');node.id='s-notrow';node.className='red';doc.body.appendChild(node);
    yellow.innerHTML='<ul>'+['#s-missing','#s-notrow','#group_positions','https://example.test/#s-aaa'].map(href=>
      '<li class="keep lamp-yellow"><a class="signal-member-jump" href="'+href+'">未知</a></li>').join('')+'</ul>';
  }});
  for(let i=0;i<3;i++){
    b.paint();
    assert.deepEqual(b.doc.querySelectorAll('#lamp-test-yellow li').map(li=>li.className),Array(4).fill('keep lamp-yellow'));
  }
  assert.equal(b.requests.length,0);assert.equal(b.store.writes.length,0);
});

// 与新发布页一致：整行初始red，但data-base-level明确排除了可已读价位。











test('双现金dirty同总额拆分不被旧云端覆盖或合并',async()=>{
  const old={...copy(SEED),cash:1,cash_extra:0.2},b=await boot({local:old,token:false});await b.enter();
  b.edit('USD','cash','0.6');b.edit('USD','cash_extra','0.4');
  assert.equal(b.mergeCloud(old),false);assert.equal(b.state().DATA.cash,1);assert.equal(b.state().DATA.cash_extra,0.4);
  assert.equal(b.state().dirty,true);assert.equal(b.cashFirst(b.state().DATA),0.6);
});

test('成本面板JS和回归文件语法有效',()=>{
  new vm.Script(SOURCE.replace(/^<script>\s*/,'').replace(/\s*<\/script>\s*$/,''),{filename:'cost_panel.js'});
  new vm.Script(fs.readFileSync(__filename,'utf8'),{filename:'tests/mm_test_cost.js'});
});

for(const [rawPrice,rawPrev,text,chg] of [
  ['100.0001','100','100.00','+0.00%'],['99.9999','100','100.00','+1.00%'],
  ['103.0001','101.234567','103.00','+1.00%'],['100.0001','100','100.00','-100.00%']
])test('原昨收计算当日盈亏和分母，不回推舍入% '+rawPrice+'/'+rawPrev+'/'+chg,async()=>{
  const qty=10000,price=Number(rawPrice),prev=Number(rawPrev);
  const b=await bootCash({local:{...copy(SEED),items:{AAA:{cost:price,qty},BBB:{cost:90,qty:0}}},prepareDocument(doc){
    const tr=doc.getElementById('s-aaa');tr.setAttribute('data-price',rawPrice);tr.setAttribute('data-prev-close',rawPrev);
    tr.querySelector('.px-price').textContent=text;tr.querySelector('.px-chg').textContent=chg;
    tr.querySelector('.lvtag.add').remove();tr.querySelector('.lvtag.cut').remove();
  }}),r=b.rows.find(r=>r.sym==='AAA'),amt=(price-prev)*qty,dayPct=amt/(prev*qty)*100;
  assert.equal(r.price,price);assert.equal(r.prevClose,prev);assert.equal(r.dayAmt,amt);
  assert.equal(r.chg,(price/prev-1)*100);
  // 直接用 cost_panel.js 真源的 money()（2026-10-29 起：一位小数，大额自动降档）
  const money=b.money(amt);
  assert.equal(b.doc.querySelector('#dayPnlCard .dp-amt').textContent,money);
  assert.equal(b.doc.querySelector('#dayPnlCard .dp-pct').textContent,(dayPct>=0?'+':'')+dayPct.toFixed(1)+'%');
  assert.equal(b.doc.querySelector('.cost-contrib .ca').textContent,money);
  assert.equal(b.doc.querySelector('.cost-contrib .cv').textContent,(r.chg>=0?'+':'')+r.chg.toFixed(1)+'%');
  assert.equal(r.tr.querySelector('.px-chg').textContent,chg);
  assert.equal(b.requests.length,0);assert.equal(b.store.writes.length,0);
});
for(const raw of [undefined,'','0','-1','Infinity','100oops'])test('旧页或无效原值'+String(raw)+'回退显示价格涨跌幅',async()=>{
  const b=await bootCash({prepareDocument(doc){
    const tr=doc.getElementById('s-aaa');tr.removeAttribute('data-price');tr.removeAttribute('data-prev-close');if(raw!==undefined){tr.setAttribute('data-price',raw);tr.setAttribute('data-prev-close',raw);}
    tr.querySelector('.px-price').textContent='1,234.56';tr.querySelector('.px-chg').textContent='+1.23%';
  }}),r=b.rows.find(r=>r.sym==='AAA');
  assert.equal(r.price,1234.56);assert.equal(r.prevClose,0);assert.equal(r.chg,1.23);
  assert.equal(r.dayAmt,(1234.56-1234.56/(1+1.23/100))*r.qty);
});
test('data-price有效但旧页无昨收原值时从原价及显示涨幅回推',async()=>{
  const b=await bootCash({prepareDocument(doc){
    const tr=doc.getElementById('s-aaa');tr.setAttribute('data-price','103.0001');tr.querySelector('.px-price').textContent='103.00';
    tr.querySelector('.px-chg').textContent='+3.00%';
  }}),r=b.rows.find(r=>r.sym==='AAA');
  assert.equal(r.price,103.0001);assert.equal(r.prevClose,0);assert.equal(r.dayAmt,(103.0001-103.0001/1.03)*r.qty);
});
test('data-prev-close单独有效时使用显示价格与原昨收，不使用显示涨幅',async()=>{
  const b=await bootCash({prepareDocument(doc){
    const tr=doc.getElementById('s-aaa');tr.setAttribute('data-prev-close','99.9999');tr.querySelector('.px-price').textContent='100.00';
    tr.querySelector('.px-chg').textContent='+1.00%';
  }}),r=b.rows.find(r=>r.sym==='AAA');assert.equal(r.price,100);assert.equal(r.prevClose,99.9999);assert.equal(r.dayAmt,(100-99.9999)*r.qty);
});


/* ===== 金额宽度护栏（2026-10-29）=====
   手机上盈亏格最窄只有 55～57px（≤365 视口 .cost-block-title 的 .t-pnl），11.5px 加粗下
   「+$1,247.3」= 9 字符 = 55px 已到边。金额每多一位整数就必须少一位精度：
     <1万 一位小数 → <10万 整数 → <100万 k 缩写 → ≥100万 M 缩写。
   下面同时断言「真源函数的分档」与「渲染出来的每一格金额不超过 9 个字符」。 */
for(const [v,want] of [
  [0,'+$0.0'],[0.04,'+$0.0'],[-0.04,'+$0.0'],[-0.06,'-$0.1'],
  [1247.34,'+$1,247.3'],[-1247.34,'-$1,247.3'],
  [9999.94,'+$9,999.9'],[9999.96,'+$10,000'],
  [12347.3,'+$12,347'],[-12347.3,'-$12,347'],
  [99999.6,'+$100,000'],
  [123457,'+$123.5k'],[-123457,'-$123.5k'],
  [999999,'+$1.00M'],[999949,'+$999.9k'],[999950,'+$1.00M'],
  [1234567,'+$1.23M'],[-12345678,'-$12.35M']
])test('金额分档 '+v+' → '+want,async()=>{
  const b=await bootCash();assert.equal(b.money(v),want);
});
for(const [v,want] of [[0,'$0'],[64526,'$64,526'],[123456,'$123,456'],[1234567,'$1.23M'],[-1234567,'$-1.23M']])
test('plain 分档 '+v+' → '+want,async()=>{
  const b=await bootCash();assert.equal(b.plain(v),want);
});
/* 价格（成本价 / 加仓价 / 减仓价）与数量同样有显示上限：窄屏这两列的内容区只有
   34～42px，4 位小数（「12345.6789」墨宽 57px）、六位股数（「123,456」墨宽 39px）都会顶出格子
   压到相邻列上。2026-10-30 起按整数位数降精度，显示恒为 6 字符以内；
   金额（现金合计）走 trim4 保留录入精度，编辑框也用 trim4，避免把 12.3456 截成 12.346 存回去。 */
for(const [v,want] of [[364.92,'364.92'],[32.88,'32.88'],[0.0001,'0.0001'],[5.1234,'5.1234'],
  [12.3456,'12.346'],[364.1234,'364.12'],[1234.5678,'1234.6'],[12345.6789,'12346'],
  [12340,'12340'],[-364.92,'-364.92']])
test('价格降档 trim '+v+' → '+want,async()=>{
  const b=await bootCash();assert.equal(b.trim(v),want);
});
for(const [v,want] of [[12.3456,'12.3456'],[12340,'12340'],[0.0001,'0.0001']])
test('金额不降档 trim4 '+v+' → '+want,async()=>{
  const b=await bootCash();assert.equal(b.trim4(v),want);
});
for(const [v,want] of [[46,'46'],[1234,'1,234'],[99999,'99,999'],[123456,'123k'],[1234567,'1.23M']])
test('数量降档 qtyInt '+v+' → '+want,async()=>{
  const b=await bootCash();assert.equal(b.qtyInt(v),want);
});
test('渲染出的每一格金额都不超过 9 个字符（最窄格 55px 的硬上限）',async()=>{
  const b=await bootCash({local:{...copy(SEED),items:{AAA:{cost:1,qty:200000},BBB:{cost:1,qty:0}}},prepareDocument(doc){
    const tr=doc.getElementById('s-aaa');tr.setAttribute('data-price','1.0617');tr.setAttribute('data-prev-close','1');
    tr.querySelector('.px-price').textContent='1.06';tr.querySelector('.px-chg').textContent='+6.17%';
  }});
  // 盈亏 200000×(1.0617-1)=12,340 → 触发「整数」档；同时把市值推到 21 万 → k 档
  const cells=[...b.doc.querySelectorAll('.cost-block-title .amt,.cost-cell .amt,.cost-contrib .ca')];
  assert.ok(cells.length,'必须渲染出金额格');
  for(const el of cells){
    const t=(el.textContent||'').trim();
    if(!/[$]/.test(t))continue;
    assert.ok(t.length<=9,'金额「'+t+'」共 '+t.length+' 个字符，超过最窄格上限');
  }
  // 标题行「总资产 · 盈亏（±N%）」：只取里面的金额片段来量宽度
  for(const el of b.doc.querySelectorAll('.cost-sum b')){
    const m=(el.textContent||'').match(/[-+]?\$[\d,.]+[kM]?/g)||[];
    for(const t of m)assert.ok(t.length<=9,'标题行金额「'+t+'」共 '+t.length+' 个字符，超过上限');
  }
  assert.equal(b.money(12340),'+$12,340');
});

test('优化回归：未变化不读取或重建DOM，展开态独立保留，变化才重建一次',async()=>{
  const b=await boot();await b.enter();const host=b.doc.getElementById('costMembers');
  const write=Object.getOwnPropertyDescriptor(Element.prototype,'innerHTML').set;
  Object.defineProperty(host,'innerHTML',{
    get(){throw Error('禁止用DOM序列化字符串比较内容');},set(value){write.call(this,value);}
  });
  const fold=b.doc.getElementById('dayPnlCard'),row=host.querySelector('[data-cost-row][data-sym="AAA"]');
  const writes=host.htmlWrites;fold.open=true;
  b.paint();b.paint();
  assert.equal(host.htmlWrites,writes);assert.equal(b.doc.getElementById('dayPnlCard'),fold);
  assert.equal(host.querySelector('[data-cost-row][data-sym="AAA"]'),row);assert.equal(fold.open,true);
  fold.open=false;b.paint();assert.equal(host.htmlWrites,writes);assert.equal(fold.open,false);
  fold.open=true;b.rows[0].price=102;b.paint();
  assert.equal(host.htmlWrites,writes+1);assert.equal(fold.open,true);
  b.paint();assert.equal(host.htmlWrites,writes+1);assert.equal(b.puts().length,0);
});
test('优化回归：编辑无变化保留输入节点光标，容器替换后重建缓存',async()=>{
  const b=await boot();await b.enter();
  const host=b.doc.getElementById('costMembers'),input=host.querySelector('input[data-sym="AAA"][data-f="qty"]');
  input.value='5.';input.focus();input.setSelectionRange(1,2,'backward');
  const writes=host.htmlWrites;b.paint();
  assert.equal(host.htmlWrites,writes);assert.equal(host.querySelector('input[data-sym="AAA"][data-f="qty"]'),input);
  assert.equal(input.value,'5.');assert.equal(input.selectionStart,1);assert.equal(input.selectionEnd,2);
  const replacement=b.doc.createElement('div');replacement.id='costMembers';host.parentNode.replaceChild(replacement,host);
  b.paint();assert.equal(replacement.htmlWrites,1);assert.ok(replacement.querySelector('input[data-sym="AAA"]'));
  assert.equal(b.puts().length,0);
});

test('优化回归：API解码错误不被raw网络失败吞掉，不重复请求或上传',async()=>{
  const b=await boot(),before=b.canonical(b.state().DATA);
  b.respond(b.requests[0],{content:Buffer.from('not JSON').toString('base64')});await settle();
  b.requests.at(-1).reject(Error('网络断开'));await settle();
  assert.match(b.state().NOTICE,/GitHub API.*不是合法 JSON/);assert.match(b.state().NOTICE,/GitHub raw.*网络断开/);
  assert.equal(b.doc.getElementById('costNotice').textContent,b.state().NOTICE);
  await b.tick(600000);assert.equal(b.requests.length,2);assert.equal(b.puts().length,0);
  assert.equal(b.canonical(b.state().DATA),before);
});
for(const invalid of ['syntax','shape'])test('优化回归：raw格式错误立即提示且无退避 '+invalid,async()=>{
  const b=await boot({token:false}),r=b.requests[0];
  if(invalid==='syntax')r.resolve({ok:true,status:200,json:()=>Promise.reject(new SyntaxError('bad JSON'))});
  else b.respond(r,{items:[]});
  await settle();assert.match(b.state().NOTICE,invalid==='syntax'?/不是合法 JSON/:/items 对象/);
  await b.tick(600000);assert.equal(b.requests.length,1);assert.equal(b.puts().length,0);
});
test('优化回归：暂时失败与404并存仍能退避，成功清除错误和旧重试计时器',async()=>{
  const b=await boot();b.respond(b.requests[0],{},503);await settle();
  b.respond(b.requests.at(-1),{},404);await settle();assert.match(b.state().NOTICE,/HTTP 503.*HTTP 404/);
  const before=b.requests.length;b.startPull();await settle();
  b.respond(b.requests.at(-1),meta(SEED));await settle();assert.equal(b.state().NOTICE,'');
  assert.equal(b.state().SYNC,'synced');await b.tick(25000);
  assert.equal(b.requests.length,before+1);assert.equal(b.puts().length,0);
});
test('优化回归：数据源兜底成功可用，HTTP非JSON错误页保持原始状态',async()=>{
  const b=await boot();b.requests[0].resolve({ok:false,status:503,json:()=>Promise.reject(new SyntaxError('HTML'))});
  await settle();b.respond(b.requests.at(-1),SEED);await settle();
  assert.equal(b.state().SYNC,'synced');assert.equal(b.state().NOTICE,'');assert.equal(b.puts().length,0);
});

test('网络分类回归：响应体传输中断允许退避，不误报数据损坏',async()=>{
  const b=await boot({token:false});
  b.requests[0].resolve({ok:true,status:200,json:()=>Promise.reject(new TypeError('连接中断'))});
  await settle();assert.match(b.state().NOTICE,/响应体读取失败.*连接中断/);
  assert.doesNotMatch(b.state().NOTICE,/不是合法 JSON/);
  await b.tick(5000);assert.equal(b.requests.length,2);
  b.respond(b.requests.at(-1),SEED);await settle();assert.equal(b.state().SYNC,'synced');
  assert.equal(b.state().NOTICE,'');assert.equal(b.puts().length,0);
});
test('网络分类回归：403限流允许退避，普通权限拒绝不反复请求',async()=>{
  for(const limited of [true,false]){
    const b=await boot();b.respond(b.requests[0],{message:limited?'API rate limit exceeded':'Resource not accessible'},403);
    await settle();b.respond(b.requests.at(-1),{},404);await settle();
    assert.match(b.state().NOTICE,/HTTP 403/);await b.tick(5000);
    assert.equal(b.requests.length,limited?3:2);
    if(limited){b.respond(b.requests.at(-1),meta(SEED));await settle();assert.equal(b.state().NOTICE,'');}
    assert.equal(b.puts().length,0);await b.dispose();
  }
});

test('刷新首屏：未执行脚本就有完整仓位骨架，编辑在绑定前禁用',async()=>{
  const doc=makeDocument(),panel=doc.getElementById('costManage');
  assert.ok(panel);assert.equal(panel.open,false);
  assert.equal(panel.querySelector('.grp-name').textContent,'仓位');
  // 「编辑」在顶栏（服务端输出，先于脚本执行），绑定前必须是禁用的
  assert.notEqual(doc.getElementById('costEditBtn').getAttribute('disabled'),null);
  for(const id of ['costState','costMembers','costNotice','costSave','costCancel','costFile','costFoot'])
    assert.equal(panel.querySelectorAll('#'+id).length,1);
});
test('刷新首屏：原地接管骨架，云端延迟三秒也不删卡、不重建标题、不上传',async()=>{
  let original,summary,button,writes;
  const b=await boot({prepareDocument:doc=>{
    original=doc.getElementById('costManage');summary=original.querySelector('summary');
    button=doc.getElementById('costEditBtn');writes=original.htmlWrites||0;original.open=true;
  }});
  for(const elapsed of [0,3000]){
    await b.tick(elapsed);
    assert.equal(b.doc.querySelectorAll('#costManage').length,1);
    assert.equal(b.doc.getElementById('costManage'),original);
    assert.equal(original.querySelector('summary'),summary);assert.equal(original.open,true);
    assert.equal(original.htmlWrites||0,writes);assert.equal(button.getAttribute('disabled'),null);
    assert.ok(original.querySelector('.cost-sum').textContent.includes('$'));
    assert.equal(b.state().SYNC,'pulling');assert.equal(b.puts().length,0);
  }
  b.respond(b.requests[0],meta({...copy(SEED),cash:99}));await settle();
  assert.equal(b.doc.getElementById('costManage'),original);assert.equal(b.state().DATA.cash,99);
  assert.equal(original.querySelector('summary'),summary);assert.equal(b.puts().length,0);
});
test('刷新首屏：成本绘制不再动态重建静态统计区',async()=>{
  const b=await boot({prepareDocument:doc=>doc.getElementById('signalCounts').remove()});
  assert.equal(b.doc.getElementById('costManage').nextElementSibling.id,'group_positions');
  assert.equal(b.doc.getElementById('signalCounts'),null);
  assert.equal(b.doc.querySelectorAll('#costManage').length,1);assert.equal(b.puts().length,0);
});

// 审核边界用例只运行内存 DOM、存储及 fetch mock。
for(const [sym,field,raw] of [
  ['AAA','qty','2oops'],['AAA','cost','-1'],['USD','cash','0.00001'],
  ['USD','cash_extra','1e309'],['BOND','fixed_cost','NaN'],['BOND','fixed_cost','-2'],
  ['USD','cash','9'.repeat(305)]
])test('审核边界：跨格重绘保留完整无效草稿 '+sym+'/'+field+'/'+(raw.length>100?'溢出':raw),async()=>{
  const b=await boot();b.respond(b.requests[0],meta(SEED));await settle();await b.enter();
  const before=cloudNumbers(b.state().DATA);b.edit(sym,field,raw);b.edit('BBB','qty','3.0000');await b.tick(16);
  const selector='input[data-sym="'+sym+'"][data-f="'+field+'"]';
  let input=b.doc.querySelector(selector);assert.equal(input.value,raw);assert.ok(input.validationMessage);
  assert.equal(b.doc.querySelector('input[data-sym="BBB"][data-f="qty"]').value,'3.0000');
  assert.equal(b.local()._local.editDraft[sym+'|'+field].raw,raw);assert.ok(b.local()._local.editDraft[sym+'|'+field].error);
  assert.equal(b.state().draftPending,true);assert.equal(b.state().positionsAuthorized,false);
  // 即使 DOM 校验状态被清空，保存仍核对独立草稿，不提交上次有效值。
  input.setCustomValidity('');await b.save();
  assert.equal(b.state().editing,true);assert.equal(b.puts().length,0);assert.equal(b.requests.length,1);
  assert.equal(b.state().positionsAuthorized,false);assert.match(b.doc.getElementById('costNotice').textContent,/无效输入/);
  b.edit(sym,field,'1');await b.save();b.respond(b.requests.at(-1),meta(SEED));await settle();
  const put=b.puts()[0];assert.ok(put);const payload=b.payload(put);
  assert.equal(payload.items.BBB.qty,3);assert.equal(payload._local,undefined);
  if(field==='cost'||field==='qty')assert.equal(payload.items[sym][field],1);
  else if(field==='cash')assert.equal(payload.cash,1);
  else if(field==='cash_extra'){assert.equal(payload.cash_extra,1);assert.equal(payload.cash,before.cash+1);}
  else assert.equal(payload[field],1);
  assert.deepEqual(b.local()._local.editDraft,{});b.respond(put,{});await settle();await b.dispose();
});
test('审核边界：多格错误逐个修正，折叠重进和取消不吞草稿',async()=>{
  const b=await boot();b.respond(b.requests[0],meta(SEED));await settle();await b.enter();
  b.edit('AAA','qty','bad');b.edit('USD','cash_extra','-3');b.edit('BBB','qty','4');await b.tick(16);
  const panel=b.doc.getElementById('costManage');panel.open=false;panel.fire('toggle');await b.tick(60000);
  assert.equal(b.puts().length,0);await b.enter();
  assert.equal(b.doc.querySelector('input[data-sym="AAA"][data-f="qty"]').value,'bad');
  b.edit('AAA','qty','6');await b.save();assert.equal(b.state().editing,true);assert.equal(b.puts().length,0);
  b.doc.getElementById('costCancel').click();await b.enter();
  assert.equal(b.doc.querySelector('input[data-sym="AAA"][data-f="qty"]').value,'bad');
  assert.equal(b.doc.querySelector('input[data-f="cash_extra"]').value,'-3');
  b.edit('AAA','qty','6');b.edit('USD','cash_extra','2');await b.save();
  const put=await finishPush(b);assert.equal(b.payload(put).items.AAA.qty,6);assert.equal(b.payload(put).cash_extra,2);
  await b.dispose();
});
test('审核边界：只有非法输入也标脏，取消恢复干净且不上传',async()=>{
  const b=await boot();b.respond(b.requests[0],meta(SEED));await settle();await b.enter();b.edit('AAA','qty','bad');
  assert.equal(b.state().dirty,true);assert.equal(b.local().items.AAA.qty,5);
  b.doc.getElementById('costCancel').click();assert.equal(b.state().dirty,false);assert.deepEqual(b.local()._local.editDraft,{});
  await b.enter();assert.equal(b.doc.querySelector('input[data-sym="AAA"][data-f="qty"]').value,'5');
  assert.equal(b.doc.querySelector('input:invalid'),null);assert.equal(b.puts().length,0);await b.dispose();
});
test('审核边界：隔离刷新恢复错误草稿，正式新页远端仍覆盖dirty草稿',async()=>{
  const b=await boot({noLocks:true,forbidRequests:true});await b.enter();b.edit('AAA','qty','bad');await b.dispose();
  const c=await boot({noLocks:true,forbidRequests:true,store:b.store,session:b.session});await c.enter();
  assert.equal(c.doc.querySelector('input[data-sym="AAA"][data-f="qty"]').value,'bad');await c.save();
  assert.equal(c.state().editing,true);assert.equal(c.requests.length,0);assert.equal(c.store.writes.length,0);await c.dispose();
  const d=await boot({local:{...copy(SEED),_local:{dirty:true,revision:9,editDraft:{'AAA|qty':{raw:'bad',error:'旧错误'}}}}});
  const remote={...copy(SEED),cash:999,items:{AAA:{cost:0,qty:3}}};d.respond(d.requests[0],meta(remote));await settle();
  assert.deepEqual(cloudNumbers(d.local()),cloudNumbers(d.normalize(remote)));assert.deepEqual(d.local()._local.editDraft,{});
  assert.equal(d.state().dirty,false);await d.tick(60000);assert.equal(d.puts().length,0);await d.dispose();
});


for(const missing of [true,false])test('审核边界：'+(missing?'未知':'零')+'成本独立于数量，零分母不伪造收益率',async()=>{
  const local={schema_version:2,items:{AAA:missing?{qty:5}:{cost:0,qty:5}},cash:0,fixed:0,fixed_cost:0};
  const b=await boot({local,noLocks:true,forbidRequests:true}),r=b.rows[0];
  assert.equal(r.has,true);assert.equal(r.costKnown,!missing);assert.equal(r.mv,500);assert.equal(r.pnlPct,null);
  assert.equal(r.pnlAmt,missing?null:500);assert.equal(b.doc.querySelector('.cost-sum').textContent,missing?'$500·—（—）':'$500·+$500.0（—）');
  await b.enter();const row=b.doc.querySelector('#costMembers [data-cost-row][data-sym="AAA"]');assert.ok(row);
  assert.equal(b.doc.querySelector('input[data-sym="AAA"][data-f="cost"]').value,missing?'':'0');
  await b.enter();b.edit('AAA','qty','6');await b.save();
  assert.equal(Object.hasOwn(b.sessionLocal().items.AAA,'cost'),!missing);assert.equal(b.sessionLocal().items.AAA.qty,6);
  b.exportData();const exported=JSON.parse(await b.blobs.at(-1).text());assert.equal(Object.hasOwn(exported.items.AAA,'cost'),!missing);
  assert.doesNotMatch(b.doc.getElementById('costMembers').textContent,/NaN|Infinity/);await b.dispose();
});
test('审核边界：未知成本与已知成本混合不冒充完整合计，明确录入零后可计算',async()=>{
  const b=await boot({local:{...copy(SEED),schema_version:2,items:{AAA:{qty:5},BBB:{cost:90,qty:2}}},noLocks:true,forbidRequests:true});
  assert.equal(b.doc.querySelector('.cost-sum').textContent,'$750·—（—）');assert.match(b.doc.querySelector('.cost-sum').getAttribute('title'),/成本未知/);
  assert.equal(b.doc.querySelector('.cashbond-row .cb-total-pnl').textContent,'—（—）');
  await b.enter();b.edit('AAA','cost','0');await b.save();assert.equal(b.sessionLocal().items.AAA.cost,0);
  assert.equal(b.doc.querySelector('.cost-sum').textContent,'$750·+$520.0（+288.9%）');await b.dispose();
});











for(const known of [true,false])for(const blank of ['', '  '])test('成本空白修复：'+(known?'清空旧成本':'原未知成本')+'/'+(blank?'空格':'空串')+'不当零且保存刷新不复活',async()=>{
  const remote={...copy(SEED),schema_version:2,items:{AAA:known?{cost:100,qty:5}:{qty:5},BBB:{cost:90,qty:2}}};
  const b=await boot({local:remote});b.respond(b.requests[0],meta(remote));await settle();await b.enter();
  b.edit('AAA','cost','bad');await b.save();assert.equal(b.state().editing,true);assert.equal(b.puts().length,0);
  const input=b.edit('AAA','cost',blank);assert.equal(input.validationMessage,'');
  assert.equal(Object.hasOwn(b.local().items.AAA,'cost'),false,'input即时落盘应删除成本字段');
  b.edit('BBB','qty','3');await b.tick(16);const row=b.rows.find(r=>r.sym==='AAA');
  assert.equal(b.doc.querySelector('input[data-sym="AAA"][data-f="cost"]').value,blank);
  assert.equal(row.has,true);assert.equal(row.costKnown,false);assert.equal(row.mv,500);assert.equal(row.pnlAmt,null);assert.equal(row.pnlPct,null);
  assert.equal(b.doc.querySelector('.cost-sum').textContent,'$850·—（—）');
  assert.equal(b.doc.getElementById('costNotice').hidden,true);assert.equal(b.puts().length,0);
  await b.save();b.respond(b.requests.at(-1),meta(remote));await settle();const put=b.puts()[0];assert.ok(put);
  const payload=b.payload(put);assert.equal(Object.hasOwn(payload.items.AAA,'cost'),false,'checkedDraft保存不得把空白转成0');
  assert.equal(payload.items.AAA.qty,5);assert.equal(payload.items.BBB.qty,3);assert.equal(payload._local,undefined);
  b.respond(put,{});await settle();assert.equal(b.state().dirty,false);await b.dispose();
  const c=await boot({store:b.store,seed:{...copy(SEED),items:{AAA:{cost:999,qty:5},BBB:{cost:90,qty:2}}}});
  assert.equal(Object.hasOwn(c.local().items.AAA,'cost'),false,'normalize(j,REAL)不能复活用户清空的成本');
  c.respond(c.requests[0],meta(payload));await settle();assert.equal(Object.hasOwn(c.state().DATA.items.AAA,'cost'),false);
  await c.enter();assert.equal(c.doc.querySelector('input[data-sym="AAA"][data-f="cost"]').value,'');
  assert.equal(c.rows.find(r=>r.sym==='AAA').pnlAmt,null);assert.equal(c.puts().length,0);await c.dispose();
});
test('成本空白修复：明确0仍是零成本且计市值，payload与刷新保留0',async()=>{
  const remote={...copy(SEED),schema_version:2,items:{AAA:{qty:5},BBB:{cost:90,qty:2}}};
  const b=await boot({local:remote});b.respond(b.requests[0],meta(remote));await settle();await b.enter();
  b.edit('AAA','cost','bad');b.edit('AAA','cost','');b.edit('AAA','cost','0');await b.tick(16);
  const row=b.rows.find(r=>r.sym==='AAA');assert.equal(row.costKnown,true);assert.equal(row.mv,500);assert.equal(row.pnlAmt,500);
  assert.equal(row.pnlPct,null);assert.equal(b.doc.querySelector('.cost-sum').textContent,'$750·+$520.0（+288.9%）');
  await b.save();b.respond(b.requests.at(-1),meta(remote));await settle();const put=b.puts()[0];assert.ok(put);
  assert.equal(b.payload(put).items.AAA.cost,0);b.respond(put,{});await settle();await b.dispose();
  const c=await boot({store:b.store});assert.equal(c.local().items.AAA.cost,0);assert.equal(c.rows[0].mv,500);await c.dispose();
});
for(const version of [1,2])test('成本空白修复：v'+version+'缺失成本不从REAL回填，明确0不受影响',async()=>{
  const b=await boot({local:{schema_version:version,items:{AAA:{qty:5},BBB:{cost:0,qty:2}},cash:0,fixed:0,fixed_cost:0},noLocks:true,forbidRequests:true});
  assert.equal(Object.hasOwn(b.state().DATA.items.AAA,'cost'),false);assert.equal(b.state().DATA.items.BBB.cost,0);
  const normalized=b.normalize({schema_version:version,items:{AAA:{qty:5},BBB:{cost:0,qty:2}}},SEED);
  assert.equal(Object.hasOwn(normalized.items.AAA,'cost'),false);assert.equal(normalized.items.BBB.cost,0);
  await b.dispose();
});
test('成本空白修复：现金数量空白继续为0，成本清空取消恢复原值',async()=>{
  const b=await boot({local:{...copy(SEED),cash:40,cash_extra:10},noLocks:true,forbidRequests:true});
  await b.enter();b.edit('AAA','cost','');assert.equal(Object.hasOwn(b.sessionLocal().items.AAA,'cost'),false);
  b.doc.getElementById('costCancel').click();assert.equal(b.sessionLocal().items.AAA.cost,100);
  await b.enter();b.edit('AAA','cost','bad');await b.save();b.doc.getElementById('costCancel').click();
  assert.equal(b.doc.getElementById('costNotice').hidden,true,'取消无效草稿不得残留输入错误提示');
  await b.enter();b.edit('AAA','qty',' ');b.edit('USD','cash','');b.edit('USD','cash_extra',' ');await b.save();
  assert.equal(b.sessionLocal().items.AAA.qty,0);assert.equal(b.sessionLocal().items.AAA.cost,100);
  assert.equal(b.sessionLocal().cash,0);assert.equal(b.sessionLocal().cash_extra,0);assert.equal(b.requests.length,0);await b.dispose();
});

// 终稿新契约回归：退役ACK之外，全部正常保存/取消/未知PUT测试仍运行。
function qaAssertNoAck(b){
  for(const key of ['acks','ack_tombstones','ack_contexts'])assert.equal(Object.hasOwn(b.state().DATA,key),false,key+'不进入内存/导出数据');
  assert.equal(b.doc.querySelectorAll('[data-ack],.ack-btn,.cost-warn,[data-kind="cost_loss"],[id^="signal-members-acked-"]').length,0);
}
function qaPnlRows(b,kind){return b.doc.querySelectorAll('.pnl-extra ul[data-g="'+kind+'"] li');}
function qaQuote(doc,sym,price,target){
  const row=doc.getElementById(fixtureAnchor(sym));row.setAttribute('data-price',String(price));row.querySelector('.px-price').textContent=String(price);
  for(const tag of row.querySelectorAll('.lvtag.add,.lvtag.cut')){tag.setAttribute('data-lv',String(target));tag.setAttribute('data-near','0');}
  return row;
}
for(const field of ['acks','ack_tombstones','ack_contexts'])for(const value of [null,[],true,42,'old',{'bad-key':'invalid'},{'AAA|loss20':'2026-10-20T00:00:00Z'}])test('终稿旧JSON忽略'+field+'形状 '+JSON.stringify(value),async()=>{
  const old={...copy(SEED),cash:77,[field]:value};const b=await boot({local:old});
  qaAssertNoAck(b);assert.equal(b.state().DATA.cash,77);
  const remote={...old,cash:123,items:{AAA:{cost:0,qty:0}}};b.respond(b.requests[0],meta(remote));await settle();await b.tick(180000);
  qaAssertNoAck(b);assert.equal(b.state().DATA.cash,123);assert.equal(b.state().DATA.items.AAA.cost,0);assert.equal(b.state().DATA.items.BBB,undefined);
  assert.equal(b.puts().length,0);assert.equal(b.state().dirty,false);
  b.exportData();const exported=JSON.parse(await b.blobs.at(-1).text());for(const k of ['acks','ack_tombstones','ack_contexts'])assert.equal(Object.hasOwn(exported,k),false);
});
test('终稿normalize不访问旧ACK getter且数值字段仍严格校验',async()=>{
  const b=await boot(),old=copy(SEED);for(const key of ['acks','ack_tombstones','ack_contexts'])Object.defineProperty(old,key,{get(){throw Error('禁止读取已退役字段');},enumerable:true});
  assert.equal(b.normalize(old).cash,30);assert.throws(()=>b.normalize({...SEED,items:{AAA:{qty:-1}}}));
});
for(const state of ['dirty','maintenance','unknown-put'])test('终稿旧'+state+'残留刷新只GET不维护上传',async()=>{
  const old={...copy(SEED),acks:{'AAA|loss20':'2026-10-01'},_local:{dirty:true,revision:19,maintenanceOnly:true,maintenancePending:true}};
  if(state==='unknown-put')old._local.uncertain={payload:copy(SEED),baseSha:'old',revision:18};
  const b=await boot({local:old});assert.equal(b.state().positionsAuthorized,false);assert.equal(b.state().uncertain,null);
  b.respond(b.requests[0],meta({...SEED,cash:999}));await settle();await b.tick(600000);
  qaAssertNoAck(b);assert.equal(b.state().DATA.cash,999);assert.equal(b.puts().length,0);assert.equal(b.requests.length,1);
});
test('终稿无ACK点击入口且重复重绘不创建保存授权',async()=>{
  const b=await boot();b.respond(b.requests[0],meta(SEED));await settle();const revision=b.state().revision;
  for(let i=0;i<4;i++){const old=b.doc.createElement('button');old.setAttribute('data-ack','AAA|loss20');b.doc.fire('click',{target:old});b.paint();}
  await b.tick(180000);qaAssertNoAck(b);assert.equal(b.state().revision,revision);assert.equal(b.puts().length,0);assert.equal(b.state().positionsAuthorized,false);
});
test('终稿旧JSON导入允许正常明确上传但payload不含ACK',async()=>{
  const b=await boot();b.respond(b.requests[0],meta(SEED));await settle();
  b.importData({...copy(SEED),cash:77,acks:[],ack_tombstones:{bad:'x'},ack_contexts:true});await b.tick(0);
  const put=await finishPush(b);assert.equal(b.payload(put).cash,77);for(const key of ['acks','ack_tombstones','ack_contexts'])assert.equal(Object.hasOwn(b.payload(put),key),false);
  assert.equal(b.state().dirty,false);qaAssertNoAck(b);
});
test('终稿明确保存成功后取消新编辑不复活旧授权',async()=>{
  const b=await boot();b.respond(b.requests[0],meta(SEED));await settle();await b.enter();b.edit('AAA','qty','7');await b.save();
  b.respond(b.requests.at(-1),meta(SEED));await settle();const put=b.puts()[0];await b.enter();b.edit('AAA','qty','88');b.respond(put,{});await settle();
  b.doc.getElementById('costCancel').click();await b.tick(180000);assert.equal(b.state().DATA.items.AAA.qty,7);assert.equal(b.state().positionSave,null);assert.equal(b.state().positionsAuthorized,false);assert.equal(b.puts().length,1);
});
for(const [kind,price,target,on] of [
  ['add',100,103,true],['add',100,103.0000000001,false],['add',100,102.9999999999,true],
  ['cut',100,97,true],['cut',100,96.9999999999,false],['cut',100,97.0000000001,true],
  ['add',123.45,127.1535,true],['add',123.45,127.1535000001,false],
  ['cut',123.45,119.7465,true],['cut',123.45,119.7464999999,false],
  ['add',1e-7,1.03e-7,true],['add',1e-7,1.030000001e-7,false],
  ['cut',1e-7,9.7e-8,true],['cut',1e-7,9.69999999e-8,false],
  ['add',50,100,false],['cut',200,100,false]
])test('终稿±3%常驻提示边界 '+kind+' '+price+'/'+target,async()=>{
  const b=await bootCash({local:{...copy(SEED),items:{AAA:{cost:price,qty:5}}},prepareDocument(doc){qaQuote(doc,'AAA',price,target);}});
  const r=b.rows.find(r=>r.sym==='AAA'),tag=kind==='add'?r.cap:r.cutCap;
  assert.equal(b.nearPrice(price,target,3),on);assert.equal(tag.getAttribute('data-near'),on?'1':'0');assert.equal(tag.hidden,false);
  assert.equal(r.tr.className,'green','价位提示不造红灯');assert.match(tag.textContent,kind==='add'?/^加仓价 /:/^减仓价 /);
  assert.equal(qaPnlRows(b,kind).some(li=>li.querySelector('.signal-member-symbol')?.textContent==='AAA'),on);
  for(let i=0;i<4;i++)b.paint();assert.equal(r.tr.className,'green');assert.equal(tag.getAttribute('data-near'),on?'1':'0');qaAssertNoAck(b);assert.equal(b.requests.length,0);
});
for(const [p,t,g,on]of [[100,100,0,true],[100,100.0000000001,0,false],[100,99.9999999999,0,false],[100,102.5,2.5,true],[100,102.500000001,2.5,false]])test('终稿价差零和小数阈值 '+[p,t,g],async()=>{const b=await bootCash();assert.equal(b.nearPrice(p,t,g),on);});
for(const [price,tone]of [[90.0000000001,''],[90,'loss'],[89.9999999999,'loss'],[80,'loss'],[79.9999999999,'loss'],[109.9999999999,''],[110,'gain'],[110.0000000001,'gain'],[100,'']])test('终稿盈亏±10淡底而非红灯 '+price,async()=>{
  const b=await bootCash({local:{...copy(SEED),items:{AAA:{cost:100,qty:5}}},prepareDocument(doc){qaQuote(doc,'AAA',price,1000);}}),r=b.rows.find(r=>r.sym==='AAA');
  assert.equal(b.pnlTone(price,100),tone);assert.equal(r.costLine.getAttribute('data-pnl')||'',tone);assert.equal(r.tr.className,'green');
  for(const g of ['gain','loss'])assert.equal(qaPnlRows(b,g).some(li=>li.querySelector('.signal-member-symbol')?.textContent==='AAA'),g===tone);
  qaAssertNoAck(b);assert.equal(b.requests.length,0);
});
for(const item of [{qty:5},{cost:0,qty:5},{cost:100,qty:0}])test('终稿未知成本零成本清仓不伪造盈亏提示 '+JSON.stringify(item),async()=>{
  const b=await bootCash({local:{...copy(SEED),items:{AAA:item}},prepareDocument(doc){qaQuote(doc,'AAA',80,1000);}}),r=b.rows.find(r=>r.sym==='AAA');
  assert.equal(r.costLine.getAttribute('data-pnl'),null);assert.equal(qaPnlRows(b,'loss').length,0);assert.equal(qaPnlRows(b,'gain').length,0);assert.equal(r.tr.className,'green');
  if(item.qty===0)assert.equal(r.costLine.hidden,true);else assert.equal(r.costLine.hidden,false);
});
test('终稿移出持仓只保留加仓提示且不移表不造红',async()=>{
  const b=await bootCash({local:{...copy(SEED),items:{AAA:{cost:200,qty:5}}},prepareDocument(doc){qaQuote(doc,'AAA',100,100);}}),r=b.rows.find(r=>r.sym==='AAA');
  const parent=r.tr.parentNode;b.paint();assert.equal(r.tr.parentNode,parent);b.move('AAA','watch');
  assert.equal(r.cap.hidden,false);assert.equal(r.cutCap.hidden,true);assert.equal(r.costLine.hidden,true);assert.equal(r.tr.className,'green');
  assert.equal(qaPnlRows(b,'loss').length,0);assert.equal(qaPnlRows(b,'cut').some(li=>li.querySelector('.signal-member-symbol')?.textContent==='AAA'),false);
  assert.equal(qaPnlRows(b,'add').some(li=>li.querySelector('.signal-member-symbol')?.textContent==='AAA'),true);
});
for(const level of ['red','yellow','green','gray'])test('终稿盈亏到价名单沿用个股基础灯 '+level,async()=>{
  const b=await bootCash({local:{...copy(SEED),items:{AAA:{cost:125,qty:5}}},prepareDocument(doc){qaQuote(doc,'AAA',100,100).className=level;}}),r=b.rows.find(r=>r.sym==='AAA');
  for(const kind of ['loss','add','cut']){const li=qaPnlRows(b,kind).find(li=>li.querySelector('.signal-member-symbol')?.textContent==='AAA');assert.ok(li);assert.equal(li.className,'lamp-'+level);}
  for(let i=0;i<4;i++)b.paint();assert.equal(r.tr.className,level);qaAssertNoAck(b);
});
for(const kind of ['gain','loss','cut','add'])test('统计空分类 '+kind+' 空→有→空保留标题骨架且重绘不复位',async()=>{
  const empty={...copy(SEED),items:{AAA:{cost:100,qty:5}}};
  const b=await boot({local:empty,prepareDocument(doc){qaQuote(doc,'AAA',100,1000);doc.querySelector('.pnl-count').setAttribute('data-pnl-count','');}});
  b.respond(b.requests[0],meta(empty));await settle();
  const extra=b.doc.querySelector('.pnl-extra'),details=extra.querySelector('details'),summary=extra.querySelector('summary'),chip=extra.querySelector('.pnl-chip');
  const lists=new Map(['gain','loss','cut','add'].map(g=>[g,extra.querySelector('ul[data-g="'+g+'"]')]));
  const r=b.rows.find(row=>row.sym==='AAA');details.open=true;
  const checkGroups=active=>{for(const [g,ul]of lists){assert.equal(extra.querySelector('ul[data-g="'+g+'"]'),ul);assert.equal(ul.closest('.signal-member-block').hidden,g!==active);assert.equal(ul.querySelectorAll('li').length,g===active?1:0);}
    assert.equal(details.open,true);assert.equal(extra.querySelector('summary'),summary);assert.equal(extra.querySelector('.pnl-chip'),chip);assert.equal(summary.querySelector('[data-pnl-count]').textContent,active?'1':'0');};
  checkGroups('');
  if(kind==='gain'||kind==='loss')b.mergeCloud({...empty,items:{AAA:{cost:kind==='gain'?80:125,qty:5}}});
  else if(kind==='add')r.trig=100;else r.cut=100;
  for(let i=0;i<4;i++){b.paint();checkGroups(kind);}
  b.mergeCloud(empty);r.trig=1000;r.cut=1000;
  for(let i=0;i<4;i++){b.paint();checkGroups('');}
  assert.equal(b.requests.length,1);assert.equal(b.puts().length,0);qaAssertNoAck(b);await b.dispose();
});
for(const [kind,cost,expected]of [['gain',80,'+25.0% · +$50.0'],['loss',125,'-20.0% · -$62.5']])test('统计 '+kind+' 真实百分比和金额采用原始价格成本及小数数量',async()=>{
  const b=await bootCash({local:{...copy(SEED),items:{AAA:{cost,qty:2.5}}},prepareDocument(doc){qaQuote(doc,'AAA',100,1000);}});
  const r=b.rows.find(row=>row.sym==='AAA'),ul=b.doc.querySelector('.pnl-extra ul[data-g="'+kind+'"]');
  assert.equal(r.pnlAmt,(100-cost)*2.5);assert.equal(r.pnlPct,(100-cost)/cost*100);
  for(let i=0;i<4;i++){b.paint();assert.equal(ul.querySelector('.signal-member-metric').textContent,expected);assert.equal(ul.closest('.signal-member-block').hidden,false);assert.equal(r.tr.className,'green');}
  assert.equal(b.requests.length,0);await b.dispose();
});
test('终稿四组pnl静态骨架重绘保留summary胶囊与展开状态',async()=>{
  const b=await bootCash(),extra=b.doc.querySelector('.pnl-extra'),details=extra.querySelector('details'),chip=extra.querySelector('.pnl-chip');
  assert.ok(chip,'必须保留summary静态胶囊，不以textContent删除');details.open=true;
  for(let i=0;i<4;i++)b.paint();assert.equal(details.open,true);assert.equal(extra.querySelector('.pnl-chip'),chip);
  for(const g of ['gain','loss','cut','add'])assert.equal(extra.querySelectorAll('ul[data-g="'+g+'"]').length,1);
});
for(const state of ['low','space','high','hit','bad'])test('终稿目标'+state+'不受成本淡底和价位重绘影响',async()=>{
  const b=await bootCash({local:{...copy(SEED),items:{AAA:{cost:125,qty:5}}},prepareDocument(doc){const tr=qaQuote(doc,'AAA',100,100);tr.querySelector('.tgs').innerHTML='<span class="lvtag tgt '+state+'"><span>+20%</span><span>120</span><span title="10月">⑩</span></span>';}});
  const tr=b.doc.getElementById('s-aaa'),target=tr.querySelector('.tgt'),parent=tr.parentNode;for(let i=0;i<4;i++)b.paint();
  assert.equal(target.className,'lvtag tgt '+state);assert.equal(tr.className,'green');assert.equal(tr.parentNode,parent);assert.equal(target.textContent,'+20%120⑩');
});
test('终稿每次动态绘制通过document发cost:painted并已完成名单更新',async()=>{
  const b=await bootCash({local:{...copy(SEED),items:{AAA:{cost:125,qty:5}}},prepareDocument(doc){qaQuote(doc,'AAA',100,100);}});let n=0;
  b.doc.addEventListener('cost:painted',()=>{n++;assert.equal(qaPnlRows(b,'loss').length,1);qaAssertNoAck(b);});b.paint();assert.equal(n,1);
});
test('终稿生产成本源码无ACK、DEMO模拟或浮亏造红链路',()=>{
  assert.doesNotMatch(SOURCE,/\b(?:setAck|pruneAck|ackStatEntries|renderAckGray|LOSS_STAT|DEMO|injectDemo|injectLmtEarnings)\b|data-ack|ack-btn|cost-warn|ack_tombstones|ack_contexts|\.acks\b|r48-demo|data-r58-key/);
});

// 红叉清仓：全部网络由内存请求夹具接管，绝不发真实GET/PUT。
const CLEAR_KEY='mm_position_clear_v1';
const CLEAR_GROUPS=[{key:'positions',label:'持仓'},{key:'focus',label:'重点'},
  {key:'technology',label:'IT · AI',sector_etf:'XLK'},{key:'index_funds',label:'指数基'}];
const CLEAR_CONFIG={_说明:['保留原配置'],group_monitoring:{technology:true},
  positions:{AAA:{note:'测试',cost:100,hold:'short',reduce:120,target:150,target_at:'2026-10'},BBB:{note:'另一只',cost:90}},
  focus:{AAA:{note:'已有重点',trigger:95}},technology:{AAA:{note:'已有板块',trigger:80}},index_funds:{}};
const clearMeta=v=>({sha:'a'.repeat(40),content:Buffer.from(JSON.stringify(v)).toString('base64')});
async function bootClear(opts={}){
  const b=await boot(opts);if(b.requests[0])b.respond(b.requests[0],meta(opts.local||SEED));await settle();await b.enter();
  b.ctx.MMRunStatus={watchConfig:async receipt=>{b.clearReceipts.push(receipt);}};b.clearReceipts=[];b.openClear('AAA');return b;
}
async function clearReply(b,file,value,method='GET',status=200){
  await settle();const req=b.requests.at(-1);assert.ok(req.url.includes('/'+file),req.url+' 应为 '+file);
  assert.equal(req.init.method||'GET',method);assert.equal(req._answered,undefined,'不得误响应旧请求');req._answered=true;
  b.respond(req,method==='GET'?clearMeta(value):value,status);await settle();return req;
}
async function clearToQuantity(b,config=CLEAR_CONFIG){
  await clearReply(b,'groups.json',CLEAR_GROUPS);await clearReply(b,'holdings.json',config);await clearReply(b,'position-costs.json',b.state().CLOUD);
  await clearReply(b,'position-costs.json',b.state().CLOUD);
  const put=b.requests.at(-1);assert.equal(put.init.method,'PUT');return put;
}
async function finishClear(b,op,put,config=CLEAR_CONFIG,status=200){
  const zero=b.payload(put);await clearReply(b,'position-costs.json',{},'PUT');await clearReply(b,'position-costs.json',zero);
  await clearReply(b,'holdings.json',config);await clearReply(b,'position-costs.json',zero);
  const move=b.requests.at(-1),next=b.payload(move);
  await clearReply(b,'holdings.json',{content:{sha:'b'.repeat(40)},commit:{sha:'c'.repeat(40)}},'PUT',status);
  if(status===200)await clearReply(b,'holdings.json',next);await op;return {zero,move,next};
}
for(const choice of ['default','focus'])test('红叉清仓 '+choice+' 只归零目标、现金不变、远端移组且取消不复活数量',async()=>{
  const b=await bootClear();const buttons=b.doc.querySelectorAll('[data-cost-clear]');assert.equal(buttons.length,2);
  assert.equal(buttons[0].textContent,'×');assert.equal(b.doc.querySelector('.cost-clear-dialog').querySelectorAll('input').length,2);
  const op=b.executeClear('AAA',choice),put=await clearToQuantity(b);
  const sent=b.payload(put);assert.equal(sent.items.AAA.qty,0);assert.deepEqual(sent.items.BBB,SEED.items.BBB);
  for(const k of ['cash','cash_extra','fixed','fixed_cost'])assert.equal(sent[k],b.state().CLOUD[k]);
  b.edit('BBB','qty','999');assert.equal(b.state().DATA.items.BBB.qty,2,'清仓处理中不接受其它编辑');
  const {next}=await finishClear(b,op,put);
  assert.equal(next.positions.AAA,undefined);assert.deepEqual(next.positions.BBB,CLEAR_CONFIG.positions.BBB);
  assert.deepEqual(next._说明,CLEAR_CONFIG._说明);assert.deepEqual(next.group_monitoring,CLEAR_CONFIG.group_monitoring);
  const target=choice==='focus'?'focus':'technology';assert.equal(next[target].AAA.note,choice==='focus'?'已有重点':'已有板块');
  assert.equal(next[target].AAA.trigger,choice==='focus'?95:80);for(const k of ['hold','reduce','cost'])assert.equal(next[target].AAA[k],undefined);
  if(choice==='default')assert.equal(next.focus.AAA,undefined);
  assert.equal(b.clearState().record.step,'done');assert.equal(b.state().DATA.items.AAA.qty,0);assert.equal(b.clearReceipts.length,1);
  b.exitEdit(true);assert.equal(b.state().DATA.items.AAA.qty,0);assert.equal(b.puts().length,2);
  await b.enter();b.openClear('AAA');const count=b.requests.length;
  b.doc.querySelector('[data-clear-confirm]').click();await settle();
  assert.equal(b.requests.length,count,'红叉确认仅恢复草稿，不得直接执行或重复写入');await b.dispose();
});
for(const mode of ['no-token','isolated','readonly','storage','cloud-changed'])test('统一保存拒绝 '+mode+' 不越权或覆盖冲突',async()=>{
  const b=await bootClear(mode==='isolated'?{noLocks:true}:mode==='no-token'?{token:false}:{});
  if(mode==='draft')b.edit('BBB','qty','999');
  if(mode==='readonly')await b.dispose();
  if(mode==='storage'){b.store.failSet.add(CLEAR_KEY);b.store.failSet.add(LOCAL);b.edit('BBB','qty','3');}
  const count=b.requests.length,op=b.executeClear('AAA','default');
  if(mode==='cloud-changed'){
    await clearReply(b,'groups.json',CLEAR_GROUPS);await clearReply(b,'holdings.json',CLEAR_CONFIG);
    await clearReply(b,'position-costs.json',{...SEED,items:{...SEED.items,AAA:{qty:9,cost:100}}});
  }
  await op;assert.equal(b.puts().length,0);assert.equal(b.state().DATA.items.AAA.qty,5);
  if(mode!=='cloud-changed')assert.equal(b.requests.length,count);if(mode==='draft')assert.equal(b.state().DATA.items.BBB.qty,999);
});
test('红叉清仓移组失败后只重试移组、不重复归零，刷新不自动上传',async()=>{
  const b=await bootClear(),op=b.executeClear('AAA','default'),put=await clearToQuantity(b);
  const {zero}=await finishClear(b,op,put,CLEAR_CONFIG,409);assert.equal(b.clearState().record.step,'move');
  assert.equal(b.state().DATA.items.AAA.qty,0);assert.match(b.state().NOTICE,/数值已保存.*移组未完成/);
  b.exitEdit(true);assert.equal(b.state().DATA.items.AAA.qty,0);await b.dispose();
  const c=await boot({store:b.store});assert.equal(c.puts().length,0);c.respond(c.requests[0],meta(zero));await settle();await c.enter();c.openClear('AAA');
  const retry=c.executeClear('AAA','default');await clearReply(c,'groups.json',CLEAR_GROUPS);await clearReply(c,'holdings.json',CLEAR_CONFIG);
  await clearReply(c,'position-costs.json',zero);await clearReply(c,'holdings.json',CLEAR_CONFIG);await clearReply(c,'position-costs.json',zero);
  const move=c.requests.at(-1),next=c.payload(move);assert.equal(move.init.method,'PUT');assert.ok(move.url.endsWith('/holdings.json'));
  await clearReply(c,'holdings.json',{content:{sha:'b'.repeat(40)},commit:{sha:'c'.repeat(40)}},'PUT');await clearReply(c,'holdings.json',next);await retry;
  assert.equal(c.puts().length,1);assert.equal(c.clearState().record.step,'done');await c.dispose();
});
test('红叉数量PUT超时结果未知，用户重试先GET核验已归零后移组',async()=>{
  const b=await bootClear(),op=b.executeClear('AAA','default'),put=await clearToQuantity(b),zero=b.payload(put);
  put.reject(Error('网络中断'));await op;assert.equal(b.clearState().record.step,'quantity');assert.equal(b.puts().length,1);
  const retry=b.executeClear('AAA','default');await clearReply(b,'groups.json',CLEAR_GROUPS);await clearReply(b,'holdings.json',CLEAR_CONFIG);
  await clearReply(b,'position-costs.json',zero);await clearReply(b,'holdings.json',CLEAR_CONFIG);await clearReply(b,'position-costs.json',zero);
  const next=b.payload(b.requests.at(-1));
  await clearReply(b,'holdings.json',{content:{sha:'b'.repeat(40)},commit:{sha:'c'.repeat(40)}},'PUT');await clearReply(b,'holdings.json',next);await retry;
  assert.equal(b.puts().filter(r=>r.url.endsWith('/position-costs.json')).length,1);assert.equal(b.state().DATA.items.AAA.qty,0);
});
for(const sym of ['SPYM','QQQM','SCHD'])test('红叉默认归属 '+sym+' 清仓后回指数基',async()=>{
  const b=await bootCash(),config=copy(CLEAR_CONFIG);config.positions[sym]={cost:100,hold:'long',note:'基金'};
  const dest=b.clearDestination(config,CLEAR_GROUPS,sym,'default');assert.equal(dest.key,'index_funds');
  const next=b.clearConfig(config,dest,sym);assert.deepEqual(JSON.parse(JSON.stringify(next.index_funds[sym])),{note:'基金'});assert.equal(next.positions[sym],undefined);
});
test('红叉等待15秒期间远端新字段保留，不把旧整份payload强行覆盖',async()=>{
  const b=await bootClear();b.store.values.set(PUSH_AT,JSON.stringify(NOW));const op=b.executeClear('AAA','default');
  await clearReply(b,'groups.json',CLEAR_GROUPS);await clearReply(b,'holdings.json',CLEAR_CONFIG);await clearReply(b,'position-costs.json',SEED);
  assert.equal(b.puts().length,0);await b.tick(15000);const newer={...copy(SEED),cash:222,items:{AAA:{cost:100,qty:5},BBB:{cost:88,qty:9}}};
  await clearReply(b,'position-costs.json',newer);const put=b.requests.at(-1),sent=b.payload(put);
  assert.equal(sent.cash,222);assert.deepEqual(sent.items.BBB,newer.items.BBB);assert.equal(sent.items.AAA.qty,0);
  await finishClear(b,op,put);assert.equal(b.state().DATA.cash,222);
});
test('红叉未知PUT后目标重新建仓和版本变化，不再次归零',async()=>{
  const b=await bootClear(),op=b.executeClear('AAA','default'),put=await clearToQuantity(b);
  put.reject(Error('回执丢失'));await op;
  const retry=b.executeClear('AAA','default');await clearReply(b,'groups.json',CLEAR_GROUPS);await clearReply(b,'holdings.json',CLEAR_CONFIG);
  await settle();const r=b.requests.at(-1),newer={...copy(SEED),items:{AAA:{cost:100,qty:9},BBB:SEED.items.BBB}};
  b.respond(r,{...clearMeta(newer),sha:'d'.repeat(40)});await retry;
  assert.equal(b.puts().length,1);assert.match(b.state().NOTICE,/停止旧清仓事务/);
});
test('红叉15秒期间目标数量改变，不清掉新数量',async()=>{
  const b=await bootClear();b.store.values.set(PUSH_AT,JSON.stringify(NOW));const op=b.executeClear('AAA','default');
  await clearReply(b,'groups.json',CLEAR_GROUPS);await clearReply(b,'holdings.json',CLEAR_CONFIG);await clearReply(b,'position-costs.json',SEED);
  await b.tick(15000);await clearReply(b,'position-costs.json',{...copy(SEED),items:{AAA:{cost:100,qty:9},BBB:SEED.items.BBB}});await op;
  assert.equal(b.puts().length,0);assert.match(b.state().NOTICE,/仓位已变化/);
});
test('红叉归零后移组前重新建仓，停止规则PUT',async()=>{
  const b=await bootClear(),op=b.executeClear('AAA','default'),put=await clearToQuantity(b),zero=b.payload(put);
  await clearReply(b,'position-costs.json',{},'PUT');await clearReply(b,'position-costs.json',zero);
  await clearReply(b,'holdings.json',CLEAR_CONFIG);await clearReply(b,'position-costs.json',{...zero,items:{...zero.items,AAA:{cost:100,qty:9}}});await op;
  assert.equal(b.puts().length,1);assert.match(b.state().NOTICE,/已重新建仓/);
});
test('红叉旧done在新持仓里允许新确认，不能永久禁用清仓',async()=>{
  const old={sym:'AAA',choice:'default',step:'done',receipt:{file:'holdings.json',blobSHA:'b'.repeat(40),commitSHA:'c'.repeat(40)}};
  const b=await bootClear({store:shared({[LOCAL]:SEED,[TOKEN]:'test-token',[CLEAR_KEY]:old})});
  assert.equal(b.doc.querySelector('[data-clear-confirm]').disabled,false);
  const op=b.executeClear('AAA','focus'),put=await clearToQuantity(b);await finishClear(b,op,put);assert.equal(b.clearState().record.targets[0].choice,'focus');
});
test('红叉200回执缺失先取提交证据、不重复移组PUT',async()=>{
  const b=await bootClear(),op=b.executeClear('AAA','default'),put=await clearToQuantity(b),zero=b.payload(put);
  await clearReply(b,'position-costs.json',{},'PUT');await clearReply(b,'position-costs.json',zero);
  await clearReply(b,'holdings.json',CLEAR_CONFIG);await clearReply(b,'position-costs.json',zero);
  const move=b.requests.at(-1),next=b.payload(move);await clearReply(b,'holdings.json',{},'PUT');await clearReply(b,'holdings.json',next);
  const head=b.requests.at(-1);assert.ok(head.url.endsWith('/git/ref/heads/main'));b.respond(head,{object:{sha:'c'.repeat(40)}});await settle();
  await clearReply(b,'holdings.json',next);await op;assert.equal(b.puts().length,2);assert.equal(b.clearState().record.step,'done');assert.equal(b.clearReceipts.length,1);
});
test('红叉移组前退出页面不继续PUT、不释放后后台接管',async()=>{
  const b=await bootClear(),op=b.executeClear('AAA','default');await clearReply(b,'groups.json',CLEAR_GROUPS);
  const req=b.requests.at(-1);await b.dispose();b.respond(req,clearMeta(CLEAR_CONFIG));await op;assert.equal(b.puts().length,0);
});

// 统一保存新契约：确认只改草稿，面板保存才创建批量事务。
function confirmMark(b,sym='AAA',choice='default'){
  b.openClear(sym);const dialog=b.doc.querySelector('.cost-clear-dialog');
  for(const input of dialog.querySelectorAll('input'))input.checked=input.value===choice;
  dialog.querySelector('[data-clear-confirm]').click();
}
async function beginBatch(b,config=CLEAR_CONFIG,remote=b.state().CLOUD){
  await b.save();await clearReply(b,'groups.json',CLEAR_GROUPS);await clearReply(b,'holdings.json',config);
  await clearReply(b,'position-costs.json',remote);await clearReply(b,'position-costs.json',remote);
  const put=b.requests.at(-1);assert.equal(put.init.method,'PUT');return put;
}
async function finishBatch(b,put,config=CLEAR_CONFIG,status=200){
  const result=await finishClear(b,Promise.resolve(),put,config,status);await settle();return result;
}
test('确认红叉0请求，可继续改数值、重选清仓去向、撤销且面板取消全撤销',async()=>{
  const b=await bootClear(),n=b.requests.length;confirmMark(b);
  assert.equal(b.requests.length,n);assert.equal(b.state().DATA.items.AAA.qty,5);
  assert.deepEqual(b.clearState().draft,{AAA:'default'});assert.equal(b.state().positionsAuthorized,false);
  assert.match(b.doc.querySelector('[data-cost-row][data-sym="AAA"]').textContent,/待清仓.*默认/);
  assert.doesNotMatch(b.doc.querySelector('.cost-clear-dialog').textContent,/点击面板保存后生效|不在券商卖出|不自动增加现金/);
  assert.equal(b.state().NOTICE,'');
  b.edit('BBB','qty','9');b.edit('USD','cash','77');confirmMark(b,'AAA','focus');
  assert.equal(b.clearState().draft.AAA,'focus');assert.equal(b.requests.length,n);
  b.openClear('AAA');b.doc.querySelector('[data-clear-undo]').click();assert.deepEqual(b.clearState().draft,{});
  confirmMark(b);b.doc.getElementById('costCancel').click();await settle();
  assert.deepEqual(b.clearState().draft,{});assert.equal(b.state().DATA.items.BBB.qty,2);assert.equal(b.state().DATA.cash,30);
  assert.equal(b.puts().length,0);await b.dispose();
});
test('多股清仓加其它数值只一次data与一次main提交，明确现金而非自动回款',async()=>{
  const b=await bootClear();confirmMark(b);confirmMark(b,'BBB','focus');b.edit('AAA','cost','101');b.edit('USD','cash','31');
  const put=await beginBatch(b),sent=b.payload(put);assert.equal(sent.items.AAA.qty,0);assert.equal(sent.items.BBB.qty,0);
  assert.equal(sent.items.AAA.cost,101);assert.equal(sent.cash,31);const {next}=await finishBatch(b,put);
  assert.deepEqual(next.positions,{});assert.equal(next.focus.BBB.note,'另一只');assert.equal(next.technology.AAA.trigger,80);
  assert.equal(b.puts().length,2);assert.equal(b.clearState().record.step,'done');assert.equal(b.clearState().busy,false);await b.dispose();
});
test('清仓加非法数值保存0请求，无授权且保留错误供修正',async()=>{
  const b=await bootClear();confirmMark(b);b.edit('BBB','qty','2oops');const n=b.requests.length;await b.save();
  assert.equal(b.requests.length,n);assert.equal(b.puts().length,0);assert.equal(b.clearState().record,null);
  assert.equal(b.state().editing,true);assert.match(b.state().NOTICE,/无效输入/);await b.dispose();
});
for(const field of ['qty','cost'])test('清仓加其它标的同字段'+field+'冲突停止，不写旧值',async()=>{
  const b=await bootClear();confirmMark(b);b.edit('BBB',field,'7');await b.save();
  await clearReply(b,'groups.json',CLEAR_GROUPS);await clearReply(b,'holdings.json',CLEAR_CONFIG);
  const remote=copy(SEED);remote.items.BBB[field]=8;await clearReply(b,'position-costs.json',remote);
  assert.equal(b.puts().length,0);assert.match(b.state().NOTICE,/同字段.*停止/);
  b.doc.getElementById('costCancel').click();await settle();assert.equal(b.clearState().record,null);await b.dispose();
});
test('只编辑第一笔现金合并其它设备第二笔与其它股票成本，不能整份旧DATA覆盖',async()=>{
  const initial={...copy(SEED),cash:40,cash_extra:10},b=await bootClear({local:initial});
  confirmMark(b);b.edit('USD','cash','33');b.edit('BBB','qty','3');
  const remote={...copy(initial),cash:50,cash_extra:20,items:{...initial.items,BBB:{qty:2,cost:88}}};
  const put=await beginBatch(b,CLEAR_CONFIG,remote),sent=b.payload(put);
  assert.equal(sent.cash,53);assert.equal(sent.cash_extra,20);assert.deepEqual(sent.items.BBB,{qty:3,cost:88});
  await finishBatch(b,put);await b.dispose();
});
test('未提交清仓与数值刷新恢复草稿不恢复上传授权，用户面板保存才提交',async()=>{
  const b=await bootClear();confirmMark(b,'AAA','focus');b.edit('BBB','cost','91');await b.dispose();
  const c=await boot({store:b.store});c.respond(c.requests[0],meta(SEED));await settle();await c.tick(180000);
  assert.equal(c.puts().length,0);assert.equal(c.state().positionsAuthorized,false);assert.equal(c.state().DATA.items.BBB.cost,91);
  assert.deepEqual(c.clearState().draft,{AAA:'focus'});await c.enter();const put=await beginBatch(c);await finishBatch(c,put);await c.dispose();
});
test('数值PUT失败未移组；未知数量跨刷新仅核验已提交payload，不盲写',async()=>{
  const b=await bootClear();confirmMark(b);b.edit('BBB','qty','6');const put=await beginBatch(b),zero=b.payload(put);
  put.reject(Error('未知PUT'));await settle();assert.equal(b.clearState().record.step,'quantity');assert.equal(b.puts().length,1);await b.dispose();
  const c=await boot({store:b.store});c.respond(c.requests[0],meta(zero));await settle();await c.tick(180000);assert.equal(c.puts().length,0);
  await c.enter();await c.save();await clearReply(c,'groups.json',CLEAR_GROUPS);await clearReply(c,'holdings.json',CLEAR_CONFIG);
  await clearReply(c,'position-costs.json',zero);await clearReply(c,'holdings.json',CLEAR_CONFIG);await clearReply(c,'position-costs.json',zero);
  const move=c.requests.at(-1),next=c.payload(move);await clearReply(c,'holdings.json',{content:{sha:'b'.repeat(40)},commit:{sha:'c'.repeat(40)}},'PUT');
  await clearReply(c,'holdings.json',next);assert.equal(c.puts().length,1);assert.equal(c.state().DATA.items.BBB.qty,6);await c.dispose();
});
test('quantity已成功move失败，取消不回滚其它数值，刷新只续main',async()=>{
  const b=await bootClear();confirmMark(b);b.edit('BBB','qty','6');b.edit('USD','cash','99');const put=await beginBatch(b);
  const {zero}=await finishBatch(b,put,CLEAR_CONFIG,500);b.doc.getElementById('costCancel').click();await settle();
  assert.equal(b.state().DATA.items.AAA.qty,0);assert.equal(b.state().DATA.items.BBB.qty,6);assert.equal(b.state().DATA.cash,99);
  assert.equal(b.clearState().record.step,'move');assert.deepEqual(b.clearState().draft,{});await b.dispose();
  const c=await boot({store:b.store});c.respond(c.requests[0],meta(zero));await settle();await c.enter();await c.save();
  await clearReply(c,'groups.json',CLEAR_GROUPS);await clearReply(c,'holdings.json',CLEAR_CONFIG);await clearReply(c,'position-costs.json',zero);
  await clearReply(c,'holdings.json',CLEAR_CONFIG);await clearReply(c,'position-costs.json',zero);
  const move=c.requests.at(-1),next=c.payload(move);await clearReply(c,'holdings.json',{content:{sha:'b'.repeat(40)},commit:{sha:'c'.repeat(40)}},'PUT');
  await clearReply(c,'holdings.json',next);assert.equal(c.puts().length,1);await c.dispose();
});
test('未知move PUT已成功，重试GET核验后只读取证，不重复main PUT',async()=>{
  const b=await bootClear();confirmMark(b);const put=await beginBatch(b),zero=b.payload(put);
  await clearReply(b,'position-costs.json',{},'PUT');await clearReply(b,'position-costs.json',zero);
  await clearReply(b,'holdings.json',CLEAR_CONFIG);await clearReply(b,'position-costs.json',zero);
  const move=b.requests.at(-1),next=b.payload(move);move.reject(Error('main回执丢失'));await settle();
  await b.save();await clearReply(b,'groups.json',CLEAR_GROUPS);await clearReply(b,'holdings.json',next);
  await clearReply(b,'position-costs.json',zero);await clearReply(b,'holdings.json',next);await clearReply(b,'position-costs.json',zero);
  const head=b.requests.at(-1);assert.ok(head.url.endsWith('/git/ref/heads/main'));b.respond(head,{object:{sha:'c'.repeat(40)}});await settle();
  await clearReply(b,'holdings.json',next);assert.equal(b.puts().length,2);assert.equal(b.clearState().record.step,'done');await b.dispose();
});
test('同股规则变动停止移组且不清掉已保存数值',async()=>{
  const b=await bootClear();confirmMark(b);const put=await beginBatch(b),zero=b.payload(put);
  await clearReply(b,'position-costs.json',{},'PUT');await clearReply(b,'position-costs.json',zero);
  const changed=copy(CLEAR_CONFIG);changed.technology.AAA.trigger=81;
  await clearReply(b,'holdings.json',changed);await clearReply(b,'position-costs.json',zero);
  assert.equal(b.puts().length,1);assert.match(b.state().NOTICE,/同股分组规则/);assert.equal(b.state().DATA.items.AAA.qty,0);await b.dispose();
});
test('固定提交取证失败跨刷新继续仅读同一commit，绝不转查新main或PUT',async()=>{
  const b=await bootClear();confirmMark(b);const put=await beginBatch(b),zero=b.payload(put);
  await clearReply(b,'position-costs.json',{},'PUT');await clearReply(b,'position-costs.json',zero);
  await clearReply(b,'holdings.json',CLEAR_CONFIG);await clearReply(b,'position-costs.json',zero);
  const next=b.payload(b.requests.at(-1));await clearReply(b,'holdings.json',{},'PUT');await clearReply(b,'holdings.json',next);
  b.respond(b.requests.at(-1),{object:{sha:'c'.repeat(40)}});await settle();b.requests.at(-1).reject(Error('固定提交读取失败'));await settle();
  assert.equal(b.clearState().record.evidenceCommit,'c'.repeat(40));await b.dispose();
  const c=await boot({store:b.store});c.respond(c.requests[0],meta(zero));await settle();await c.enter();const n=c.requests.length;await c.save();
  assert.equal(c.requests.length,n+1);assert.ok(c.requests.at(-1).url.includes('ref='+'c'.repeat(40)));
  await clearReply(c,'holdings.json',next);assert.equal(c.puts().length,0);assert.equal(c.clearState().record.step,'done');await c.dispose();
});
test('旧单股未完成LS_CLEAR安全迁移，面板保存恢复quantity核验而非红叉独立执行',async()=>{
  const zero={...copy(SEED),items:{...SEED.items,AAA:{qty:0,cost:100}}};
  const old={sym:'AAA',choice:'default',step:'quantity',started:true,submitted:true,baseSHA:'a'.repeat(40),zero,
    source:JSON.stringify(CLEAR_CONFIG.positions.AAA),destination:'technology'};
  const b=await bootClear({store:shared({[LOCAL]:SEED,[TOKEN]:'test-token',[CLEAR_KEY]:old})});
  assert.equal(b.clearState().record.legacy,true);assert.equal(b.puts().length,0);await b.save();
  await clearReply(b,'groups.json',CLEAR_GROUPS);await clearReply(b,'holdings.json',CLEAR_CONFIG);await clearReply(b,'position-costs.json',zero);
  await clearReply(b,'holdings.json',CLEAR_CONFIG);await clearReply(b,'position-costs.json',zero);
  const next=b.payload(b.requests.at(-1));await clearReply(b,'holdings.json',{content:{sha:'b'.repeat(40)},commit:{sha:'c'.repeat(40)}},'PUT');
  await clearReply(b,'holdings.json',next);assert.equal(b.puts().length,1);assert.equal(b.clearState().record.step,'done');await b.dispose();
});
test('新增入口真实dialog仅加草稿，代码规范化，无行情不捏造市值，允许继续编辑和撤销',async()=>{
  const b=await bootClear(),n=b.requests.length;b.doc.querySelector('[data-cost-add]').click();const dialog=b.doc.querySelector('.cost-add-dialog');
  dialog.querySelector('[data-add-symbol]').value='  xyz.a  ';dialog.querySelector('[data-add-qty]').value='1.2345';
  dialog.querySelector('[data-add-cost]').value='2.3456';dialog.querySelector('[data-add-confirm]').click();
  assert.equal(b.requests.length,n);assert.equal(dialog.open,false);assert.ok(b.clearState().additions['XYZ.A']);
  assert.equal(b.state().DATA.items['XYZ.A'].qty,1.2345);assert.match(b.doc.querySelector('[data-cost-row][data-sym="XYZ.A"]').textContent,/待新增/);
  assert.equal(b.doc.querySelectorAll('.ep-val').at(-1).textContent,'—');b.edit('XYZ.A','qty','2.3456');assert.equal(b.state().DATA.items['XYZ.A'].qty,2.3456);
  b.doc.querySelector('[data-cost-remove-added="XYZ.A"]').click();assert.equal(b.state().DATA.items['XYZ.A'],undefined);
  assert.equal(b.puts().length,0);await b.dispose();
});
for(const [sym,hold,qty,cost] of [['AAA','long','1','2'],['SGOV','long','1','2'],['USD','long','1','2'],['BOND','long','1','2'],['^GSPC','long','1','2'],
  ['bad code','long','1','2'],['CCC','bad','1','2'],['CCC','long','0','2'],['CCC','short','1e2','2'],['CCC','long','1','-2']])
test('新增拒绝非法/重复/专属口径 '+[sym,hold,qty,cost].join('/'),async()=>{
  const b=await bootClear();assert.throws(()=>b.addPosition(sym,hold,qty,cost));assert.deepEqual(b.clearState().additions,{});assert.equal(b.puts().length,0);await b.dispose();
});
test('新增only一次data一次main且记录hold，既有普通监控配置备注trigger不丢',async()=>{
  const b=await bootClear(),config=copy(CLEAR_CONFIG);config.technology.CCC={note:'已有代码',trigger:12,target:20};
  b.addPosition('ccc','short','3','11');const put=await beginBatch(b,config),sent=b.payload(put);
  assert.deepEqual(sent.items.CCC,{qty:3,cost:11});assert.deepEqual(sent.items.AAA,SEED.items.AAA);
  const {next}=await finishBatch(b,put,config);assert.deepEqual(next.positions.CCC,{note:'已有代码',trigger:12,target:20,hold:'short'});
  assert.deepEqual(next.technology.CCC,config.technology.CCC);assert.equal(b.puts().length,2);assert.equal(b.state().DATA.cash,30);
  await b.enter();assert.match(b.doc.querySelector('[data-cost-row][data-sym="CCC"]').textContent,/已保存.*行情/);await b.dispose();
});
test('新增清仓数值同批两次PUT且取消未保存全部撤销',async()=>{
  const b=await bootClear();b.addPosition('CCC','long','4','');confirmMark(b);b.edit('BBB','cost','92');
  const put=await beginBatch(b),sent=b.payload(put);assert.deepEqual(sent.items.CCC,{qty:4});assert.equal(sent.items.AAA.qty,0);assert.equal(sent.items.BBB.cost,92);
  const {next}=await finishBatch(b,put);assert.equal(next.positions.CCC.hold,'long');assert.equal(next.positions.AAA,undefined);assert.equal(b.puts().length,2);
  await b.enter();b.addPosition('DDD','short','1','2');confirmMark(b,'BBB','focus');b.edit('USD','cash','88');b.doc.getElementById('costCancel').click();
  assert.equal(b.state().DATA.items.DDD,undefined);assert.deepEqual(b.clearState().additions,{});assert.deepEqual(b.clearState().draft,{});
  assert.equal(b.state().DATA.cash,30);assert.equal(b.state().DATA.items.CCC.qty,4);await b.dispose();
});
test('新增草稿刷新保留行和数值，0自动PUT，取消删除新股不清仓远端',async()=>{
  const b=await bootClear();b.addPosition('CCC','long','1.1234','9');await b.dispose();
  const c=await boot({store:b.store});c.respond(c.requests[0],meta(SEED));await settle();await c.tick(180000);assert.equal(c.puts().length,0);await c.enter();
  assert.ok(c.doc.querySelector('[data-cost-row][data-sym="CCC"]'));assert.equal(c.state().DATA.items.CCC.qty,1.1234);
  c.doc.getElementById('costCancel').click();assert.equal(c.state().DATA.items.CCC,undefined);assert.deepEqual(c.clearState().additions,{});assert.equal(c.puts().length,0);await c.dispose();
});
test('新增远端已持仓拒绝，data0PUT，不覆盖另一设备新增配置',async()=>{
  const b=await bootClear();b.addPosition('CCC','long','1','2');await b.save();await clearReply(b,'groups.json',CLEAR_GROUPS);
  const config=copy(CLEAR_CONFIG);config.positions.CCC={hold:'short',note:'远端新仓'};await clearReply(b,'holdings.json',config);
  await clearReply(b,'position-costs.json',SEED);assert.equal(b.puts().length,0);assert.match(b.state().NOTICE,/远端已有持仓/);await b.dispose();
});
test('新增数值已保存后普通监控同股规则变化，停止main写且取消不删除新数量',async()=>{
  const b=await bootClear(),config=copy(CLEAR_CONFIG);config.technology.CCC={note:'原备注',trigger:3};b.addPosition('CCC','long','1','2');
  const put=await beginBatch(b,config),sent=b.payload(put);await clearReply(b,'position-costs.json',{},'PUT');await clearReply(b,'position-costs.json',sent);
  const changed=copy(config);changed.technology.CCC.trigger=4;await clearReply(b,'holdings.json',changed);await clearReply(b,'position-costs.json',sent);
  assert.equal(b.puts().length,1);assert.match(b.state().NOTICE,/同股新增规则/);b.doc.getElementById('costCancel').click();
  assert.equal(b.state().DATA.items.CCC.qty,1);assert.ok(b.clearState().added.CCC);await b.dispose();
});

test('数值已收到200但后续GET失败，取消不恢复旧数值或删除已保存新增仓位',async()=>{
  const b=await bootClear();confirmMark(b);b.addPosition('CCC','long','2','7');b.edit('BBB','cost','91');const put=await beginBatch(b);
  await clearReply(b,'position-costs.json',{},'PUT');b.requests.at(-1).reject(Error('数值核验GET失败'));await settle();
  assert.equal(b.clearState().record.quantityAccepted,true);assert.equal(b.clearState().record.step,'quantity');
  b.doc.getElementById('costCancel').click();assert.equal(b.state().DATA.items.AAA.qty,0);assert.equal(b.state().DATA.items.BBB.cost,91);
  assert.equal(b.state().DATA.items.CCC.qty,2);assert.ok(b.clearState().added.CCC);assert.equal(b.puts().length,1);await b.dispose();
});
test('未知quantity同baseSHA仅重发原冻结payload，不带失败后本地草稿',async()=>{
  const b=await bootClear();confirmMark(b);b.edit('BBB','qty','6');const first=await beginBatch(b),sent=b.payload(first);
  first.reject(Error('quantity结果未知'));await settle();b.edit('BBB','qty','999');assert.equal(b.state().DATA.items.BBB.qty,6);
  await b.tick(15000);await b.save();await clearReply(b,'groups.json',CLEAR_GROUPS);await clearReply(b,'holdings.json',CLEAR_CONFIG);
  await clearReply(b,'position-costs.json',SEED);await clearReply(b,'position-costs.json',SEED);
  const second=b.requests.at(-1);assert.equal(second.init.method,'PUT');assert.deepEqual(b.payload(second),sent);
  await finishBatch(b,second);assert.equal(b.puts().filter(r=>r.url.endsWith('/position-costs.json')).length,2);await b.dispose();
});
test('未知move结果后远端main新版本不得重发旧movePayload',async()=>{
  const b=await bootClear();confirmMark(b);const put=await beginBatch(b),zero=b.payload(put);
  await clearReply(b,'position-costs.json',{},'PUT');await clearReply(b,'position-costs.json',zero);
  await clearReply(b,'holdings.json',CLEAR_CONFIG);await clearReply(b,'position-costs.json',zero);b.requests.at(-1).reject(Error('main结果未知'));await settle();
  await b.save();await clearReply(b,'groups.json',CLEAR_GROUPS);await clearReply(b,'holdings.json',CLEAR_CONFIG);await clearReply(b,'position-costs.json',zero);
  const changed=copy(CLEAR_CONFIG);changed.group_monitoring.technology=false;
  await settle();b.respond(b.requests.at(-1),{...clearMeta(changed),sha:'d'.repeat(40)});await settle();await clearReply(b,'position-costs.json',zero);
  assert.equal(b.puts().length,2);assert.match(b.state().NOTICE,/未知移组PUT.*新版本/);await b.dispose();
});
for(const mode of ['no-token','isolated','storage','readonly'])test('新增only保存边界 '+mode+' 禁止越权或存储失败提交',async()=>{
  const b=await bootClear(mode==='no-token'?{token:false}:mode==='isolated'?{noLocks:true}:{});b.addPosition('CCC','short','2','3');
  if(mode==='storage')b.store.failSet.add(CLEAR_KEY);if(mode==='readonly')await b.dispose();const n=b.requests.length;await b.save();
  assert.equal(b.requests.length,n);assert.equal(b.puts().length,0);await b.dispose();
});
test('新增行非法数量阻断整批保存，保留新增草稿与可见错误',async()=>{
  const b=await bootClear();b.addPosition('CCC','long','1','2');confirmMark(b);b.edit('CCC','qty','1oops');const n=b.requests.length;await b.save();
  assert.equal(b.requests.length,n);assert.equal(b.clearState().record,null);assert.ok(b.clearState().additions.CCC);
  assert.ok(b.doc.querySelector('input[data-sym="CCC"][data-f="qty"]').validationMessage);await b.dispose();
});

// 最后四项闭环：取消提示、ETF重建仓、非法草稿新增、预检及冲突恢复。
test('取消仅草稿清仓和新增后清空瞬态NOTICE，面板与操作按钮均隐藏',async()=>{
  const b=await bootClear();confirmMark(b);b.addPosition('CCC','long','1','2');b.edit('BBB','qty','3');
  b.doc.getElementById('costCancel').click();await settle();
  assert.equal(b.state().NOTICE,'');assert.equal(b.state().editing,false);
  assert.equal(b.doc.getElementById('costManage').classList.contains('cost-edit-open'),false);
  assert.equal(b.doc.getElementById('costNotice').hidden,true);assert.equal(b.doc.getElementById('costSave').hidden,true);
  assert.equal(b.doc.getElementById('costCancel').hidden,true);assert.equal(b.puts().length,0);await b.dispose();
});
for(const sym of ['SPYM','QQQM','SCHD','VTI'])test('指数基分组普通ETF '+sym+' 清仓后允许新增重建仓且保留备注',async()=>{
  const local={...copy(SEED),items:{...SEED.items,[sym]:{qty:0,cost:10}}},config=copy(CLEAR_CONFIG);
  config.index_funds[sym]={note:'已有ETF',trigger:9};
  const b=await bootClear({local,symbols:['AAA','BBB',sym],groups:{[sym]:'index_funds'}});
  b.addPosition(sym.toLowerCase(),'long','2','11');const put=await beginBatch(b,config),sent=b.payload(put);
  assert.deepEqual(sent.items[sym],{qty:2,cost:11});const {next}=await finishBatch(b,put,config);
  assert.deepEqual(next.positions[sym],{note:'已有ETF',trigger:9,hold:'long'});assert.deepEqual(next.index_funds[sym],config.index_funds[sym]);
  assert.equal(b.puts().length,2);await b.dispose();
});
test('已有其它非法输入时新增明确拒绝，直接接口和真实dialog均不把DATA置null',async()=>{
  const b=await bootClear();b.edit('BBB','qty','bad');const before=b.canonical(b.state().DATA),n=b.requests.length;
  assert.throws(()=>b.addPosition('CCC','long','1','2'),/已有无效输入/);
  assert.equal(b.canonical(b.state().DATA),before);assert.deepEqual(b.clearState().additions,{});
  b.doc.querySelector('[data-cost-add]').click();const dialog=b.doc.querySelector('.cost-add-dialog');
  dialog.querySelector('[data-add-symbol]').value='CCC';dialog.querySelector('[data-add-qty]').value='1';dialog.querySelector('[data-add-cost]').value='2';
  dialog.querySelector('[data-add-confirm]').click();assert.equal(dialog.open,true);assert.match(dialog.querySelector('[data-add-message]').textContent,/已有无效输入/);
  assert.equal(b.canonical(b.state().DATA),before);assert.equal(b.local()._local.editDraft['BBB|qty'].raw,'bad');
  assert.ok(b.doc.querySelector('input[data-sym="BBB"][data-f="qty"]').validationMessage);
  assert.equal(b.requests.length,n);assert.equal(b.puts().length,0);b.edit('BBB','qty','3');dialog.querySelector('[data-add-confirm]').click();
  assert.equal(dialog.open,false);assert.equal(b.state().DATA.items.CCC.qty,1);assert.equal(b.state().DATA.items.BBB.qty,3);await b.dispose();
});
test('默认无归属预检失败释放冻结保留全部草稿，允许改选focus后统一重存',async()=>{
  const b=await bootClear();confirmMark(b,'BBB','default');b.addPosition('CCC','long','1','2');b.edit('AAA','cost','101');await b.save();
  await clearReply(b,'groups.json',CLEAR_GROUPS);await clearReply(b,'holdings.json',CLEAR_CONFIG);await clearReply(b,'position-costs.json',SEED);
  assert.equal(b.puts().length,0);assert.equal(b.clearState().record,null);assert.equal(b.clearState().draft.BBB,'default');
  assert.ok(b.clearState().additions.CCC);assert.equal(b.state().DATA.items.AAA.cost,101);
  assert.equal(b.doc.querySelector('input[data-sym="AAA"][data-f="qty"]').disabled,false);
  confirmMark(b,'BBB','focus');assert.equal(b.clearState().draft.BBB,'focus');
  const put=await beginBatch(b),sent=b.payload(put);assert.equal(sent.items.BBB.qty,0);assert.equal(sent.items.AAA.cost,101);assert.equal(sent.items.CCC.qty,1);
  const {next}=await finishBatch(b,put);assert.ok(next.focus.BBB);assert.ok(next.positions.CCC);assert.equal(b.puts().length,2);await b.dispose();
});
test('已提交冲突面板取消先明确确认并只读核验，采用远端新仓后允许重新编辑',async()=>{
  const b=await bootClear({confirm:true});confirmMark(b);const put=await beginBatch(b);put.reject(Error('数量PUT结果未知'));await settle();
  await b.save();await clearReply(b,'groups.json',CLEAR_GROUPS);await clearReply(b,'holdings.json',CLEAR_CONFIG);
  const remote={...copy(SEED),items:{...SEED.items,AAA:{qty:9,cost:101}}};b.respond(b.requests.at(-1),{...clearMeta(remote),sha:'d'.repeat(40)});await settle();
  assert.equal(b.clearState().record.conflict,true);const n=b.requests.length;b.ctx.confirm=()=>false;b.doc.getElementById('costCancel').click();await settle();
  assert.equal(b.requests.length,n);assert.equal(b.clearState().record.conflict,true);
  b.ctx.confirm=text=>{assert.match(text,/先只读核验/);return true;};b.doc.getElementById('costCancel').click();await settle();
  b.respond(b.requests.at(-1),{...clearMeta(remote),sha:'d'.repeat(40)});await settle();await clearReply(b,'holdings.json',CLEAR_CONFIG);
  assert.equal(b.puts().length,1);assert.equal(b.clearState().record.abandoned,true);assert.equal(b.clearState().record.step,'done');
  assert.equal(b.state().DATA.items.AAA.qty,9);assert.equal(b.state().DATA.items.AAA.cost,101);assert.equal(b.state().NOTICE,'');
  assert.equal(b.doc.getElementById('costManage').classList.contains('cost-edit-open'),false);await b.enter();confirmMark(b,'AAA','focus');
  assert.equal(b.clearState().draft.AAA,'focus');assert.equal(b.doc.querySelector('input[data-sym="AAA"][data-f="qty"]').disabled,false);await b.dispose();
});
test('已成功数值后的规则冲突可核验结束，取消不回滚已归零和其它保存值',async()=>{
  const b=await bootClear({confirm:true});confirmMark(b);b.edit('BBB','qty','6');b.edit('USD','cash','99');const put=await beginBatch(b),zero=b.payload(put);
  await clearReply(b,'position-costs.json',{},'PUT');await clearReply(b,'position-costs.json',zero);
  const changed=copy(CLEAR_CONFIG);changed.technology.AAA.trigger=81;await clearReply(b,'holdings.json',changed);await clearReply(b,'position-costs.json',zero);
  assert.equal(b.clearState().record.conflict,true);b.doc.getElementById('costCancel').click();await settle();
  await clearReply(b,'position-costs.json',zero);await clearReply(b,'holdings.json',changed);
  assert.equal(b.puts().length,1);assert.equal(b.state().DATA.items.AAA.qty,0);assert.equal(b.state().DATA.items.BBB.qty,6);
  assert.equal(b.state().DATA.cash,99);assert.equal(b.clearState().record.abandoned,true);assert.equal(b.state().positionsAuthorized,false);await b.dispose();
});
test('未知quantity仍同base版本时取消不盲丢，后来GET匹配提交payload才允许结束',async()=>{
  const b=await bootClear({confirm:true});confirmMark(b);const put=await beginBatch(b),zero=b.payload(put);put.reject(Error('未知PUT'));await settle();
  const changed=copy(CLEAR_CONFIG);changed.technology.AAA.trigger=81;await b.save();await clearReply(b,'groups.json',CLEAR_GROUPS);
  await clearReply(b,'holdings.json',changed);await clearReply(b,'position-costs.json',SEED);assert.equal(b.clearState().record.conflict,true);
  b.doc.getElementById('costCancel').click();await settle();await clearReply(b,'position-costs.json',SEED);await clearReply(b,'holdings.json',changed);
  assert.equal(b.clearState().record.step,'quantity');assert.equal(b.clearState().record.conflict,true);assert.match(b.state().NOTICE,/仍未知.*不能安全放弃/);
  assert.equal(b.puts().length,1);b.doc.getElementById('costCancel').click();await settle();await clearReply(b,'position-costs.json',zero);await clearReply(b,'holdings.json',changed);
  assert.equal(b.clearState().record.abandoned,true);assert.equal(b.state().DATA.items.AAA.qty,0);assert.equal(b.puts().length,1);await b.dispose();
});
test('冲突取消核验GET失败仍保留记录与真实错误，不解除保护或PUT',async()=>{
  const b=await bootClear({confirm:true});confirmMark(b);const put=await beginBatch(b);put.reject(Error('未知PUT'));await settle();
  const changed=copy(CLEAR_CONFIG);changed.technology.AAA.trigger=81;await b.save();await clearReply(b,'groups.json',CLEAR_GROUPS);
  await clearReply(b,'holdings.json',changed);await clearReply(b,'position-costs.json',SEED);const before=b.clearState().record;
  b.doc.getElementById('costCancel').click();await settle();b.requests.at(-1).reject(Error('核验GET失败'));await settle();
  assert.deepEqual(b.clearState().record,before);assert.match(b.state().NOTICE,/核验GET失败/);assert.equal(b.state().editing,true);
  assert.equal(b.puts().length,1);assert.equal(b.doc.getElementById('costNotice').hidden,false);await b.dispose();
});

test('新增成功到持仓产物后清占位，清仓成功再回普通板块允许重新建仓',async()=>{
  const config=copy(CLEAR_CONFIG);config.technology.CCC={note:'可重复建仓',trigger:3};
  const b=await bootClear({symbols:['AAA','BBB','CCC'],groups:{CCC:'technology'}});b.addPosition('CCC','long','1','2');
  const first=await beginBatch(b,config),added=b.payload(first),{next:holding}=await finishBatch(b,first,config);
  assert.ok(b.clearState().added.CCC);await b.dispose();
  const c=await bootClear({store:b.store,local:added,symbols:['AAA','BBB','CCC']});
  assert.equal(c.clearState().added.CCC,undefined,'新持仓DOM到位只清旧占位，不删真实数据');assert.equal(c.state().DATA.items.CCC.qty,1);
  await c.tick(15000);confirmMark(c,'CCC','default');const clearPut=await beginBatch(c,holding);
  const {next:ordinary}=await finishBatch(c,clearPut,holding);assert.equal(c.state().DATA.items.CCC.qty,0);
  assert.equal(c.clearState().added.CCC,undefined);assert.equal(c.local()._local.addedPositions.CCC,undefined);
  c.move('CCC','technology');await c.enter();assert.equal(c.doc.querySelector('[data-cost-row][data-sym="CCC"]'),null,'退持仓后没有零数量等行情幽灵行');
  c.addPosition('CCC','short','2','3');assert.ok(c.clearState().additions.CCC);await c.tick(15000);
  const rebuilt=await beginBatch(c,ordinary),{next:again}=await finishBatch(c,rebuilt,ordinary);
  assert.deepEqual(c.payload(rebuilt).items.CCC,{qty:2,cost:3});assert.equal(again.positions.CCC.hold,'short');
  assert.equal(again.positions.CCC.note,'可重复建仓');assert.equal(c.puts().length,4);await c.dispose();
});

// 开工混合块覆盖补回：仅剥离ACK生产行为，不剥离其事务、数值、原始精度和身份断言。
test('混合块复核：连续两次保存迟到回执不携带第三份未保存草稿',async()=>{
  const b=await boot(),initial=b.requests[0];await b.enter();b.edit('AAA','qty','7');await b.save();
  const first=await finishPush(b);await b.enter();b.edit('AAA','qty','9');await b.save();await b.tick(30000);
  b.respond(b.requests.at(-1),meta(b.payload(first),'first'));await settle();const second=b.puts()[1];
  assert.equal(b.payload(second).items.AAA.qty,9);await b.enter();b.edit('AAA','qty','88');b.ack('AAA|add|95');
  b.respond(second,{});await settle();b.respond(initial,meta(SEED));await settle();await b.tick(180000);
  assert.equal(b.state().CLOUD.items.AAA.qty,9);assert.equal(b.state().DATA.items.AAA.qty,88);
  assert.equal(b.state().editing,true);assert.equal(b.state().dirty,true);assert.equal(b.puts().length,2);qaAssertNoAck(b);
});
test('混合块复核：相同内容明确保存消费授权，旧ACK入口不触发任何维护',async()=>{
  const b=await boot();b.respond(b.requests[0],meta(SEED));await settle();await b.enter();b.edit('AAA','qty','5');await b.save();
  assert.equal(b.state().positionsAuthorized,false);assert.equal(b.state().positionSave,null);const n=b.requests.length;
  b.ack('AAA|loss10');await b.tick(180000);assert.equal(b.requests.length,n);assert.equal(b.puts().length,0);assert.equal(b.state().dirty,false);
});
for(const key of [LOCAL,CLOUD])test('混合块复核：成功PUT后'+key+'缓存失败不得复活授权重传',async()=>{
  const b=await boot();b.respond(b.requests[0],meta(SEED));await settle();await b.enter();b.edit('AAA','qty','7');await b.save();
  b.respond(b.requests.at(-1),meta(SEED));await settle();const put=b.puts()[0];b.store.failSet.add(key);b.respond(put,{});await settle();
  assert.equal(b.state().positionsAuthorized,false);assert.equal(b.state().positionSave,null);assert.equal(b.state().dirty,true);assert.notEqual(b.state().SYNC,'synced');
  b.store.failSet.delete(key);const n=b.requests.length;b.ack('AAA|loss20');await b.tick(180000);assert.equal(b.requests.length,n);assert.equal(b.puts().length,1);
});
test('混合块复核：主动关闭编辑保留草稿，新GET不解除暂停，bfCache重核验',async()=>{
  const b=await boot();b.respond(b.requests[0],meta(SEED));await settle();await b.enter();b.edit('AAA','qty','88');
  const panel=b.doc.getElementById('costManage');panel.open=false;panel.fire('toggle');b.ack('AAA|add|95');b.startPull();await settle();b.respond(b.requests.at(-1),meta({...SEED,cash:999}));await settle();await b.tick(180000);
  assert.equal(b.state().savePaused,true);assert.equal(b.state().DATA.items.AAA.qty,88);assert.equal(b.puts().length,0);
  await b.dispose();b.fireWindow('pageshow',{persisted:true});await settle();assert.equal(b.state().savePaused,false);
  b.respond(b.requests.at(-1),meta(SEED));await settle();assert.equal(b.state().DATA.items.AAA.qty,5);assert.equal(b.puts().length,0);
});
test('混合块复核：导入含旧ACK及零固收，上传导出刷新均保持真实零值',async()=>{
  const b=await boot();b.respond(b.requests[0],meta(SEED));await settle();
  const d={schema_version:2,items:{AAA:{cost:0,qty:0}},cash:0,cash_extra:0,fixed:0,fixed_cost:0,acks:{bad:true}};
  b.importData(d);await b.tick(0);const put=await finishPush(b);assert.deepEqual(cloudNumbers(b.payload(put)),cloudNumbers(b.normalize(d)));
  b.exportData();const exported=JSON.parse(await b.blobs.at(-1).text());assert.equal(exported.fixed,0);assert.equal(exported.fixed_cost,0);assert.equal(exported.items.AAA.cost,0);qaAssertNoAck(b);
  await b.dispose();const c=await boot({store:b.store,seed:{...SEED,fixed:999,fixed_cost:999}});c.respond(c.requests[0],meta(exported));await settle();
  assert.equal(c.state().DATA.fixed,0);assert.equal(c.state().DATA.fixed_cost,0);assert.equal(c.state().DATA.items.AAA.qty,0);assert.equal(c.puts().length,0);
});
for(const legacySymbols of [false,true])test('混合块复核：含点及等号代码原键估值编辑 legacy='+legacySymbols,async()=>{
  const symbols=['0700.HK','600519.SS','ES=F'],local={items:Object.fromEntries(symbols.map((s,i)=>[s,{cost:100,qty:i+1}])),cash:0,fixed:0};
  const b=await boot({symbols,legacySymbols,local});assert.deepEqual(Array.from(b.rows,r=>r.sym),symbols);assert.deepEqual(Array.from(b.rows,r=>r.mv),[100,200,300]);
  assert.equal(b.doc.querySelector('.cost-sum b').textContent,'$600');await b.enter();
  for(const sym of symbols)assert.ok(b.doc.querySelector('#costMembers [data-cost-row][data-sym="'+sym+'"]'));
  b.edit('0700.HK','qty','4');await b.tick(16);assert.equal(b.state().DATA.items['0700.HK'].qty,4);assert.equal(b.state().DATA.items['0700-HK'],undefined);assert.equal(b.puts().length,0);
});
test('混合块复核：非持仓旧数量不计市值贡献但不删除保存数据',async()=>{
  const d=copy(SEED);d.items.AAA={cost:200,qty:1000};const b=await boot({local:d,groups:{AAA:'focus'}}),r=b.rows.find(r=>r.sym==='AAA');
  assert.equal(b.doc.querySelector('.cost-sum>b').textContent,'$250');assert.equal(r.has,false);assert.equal(r.costLine.hidden,true);
  assert.equal(b.doc.querySelector('.cost-contrib').textContent.includes('AAA'),false);assert.equal(b.state().DATA.items.AAA.qty,1000);assert.equal(r.tr.className,'green');
});
test('混合块复核：零成本正数量计市值和日贡献，SGOV非持仓不混入',async()=>{
  const local={schema_version:2,items:{AAA:{cost:0,qty:5},BBB:{cost:90,qty:2},SGOV:{cost:1,qty:999}},cash:30,fixed:20,fixed_cost:15};
  const b=await boot({local,symbols:['AAA','BBB','SGOV'],groups:{SGOV:'focus'},noLocks:true,forbidRequests:true,prepareDocument:doc=>doc.getElementById('s-aaa').setAttribute('data-prev-close','80')}),r=b.rows[0];
  assert.equal(r.has,true);assert.equal(r.costKnown,true);assert.equal(r.mv,500);assert.equal(r.pnlAmt,500);assert.equal(r.pnlPct,null);assert.equal(r.dayAmt,100);
  assert.equal(b.doc.querySelector('.cost-sum').textContent,'$750·+$520.0（+288.9%）');assert.equal(b.doc.querySelector('.cb-cash .cb-amt').textContent,'$30');
  assert.equal(b.doc.querySelector('.cb-bond .cb-amt').textContent,'$20');assert.match(b.doc.querySelector('.cost-contrib').textContent,/AAA/);assert.doesNotMatch(b.doc.querySelector('.cost-contrib').textContent,/SGOV/);
  assert.doesNotMatch(b.doc.getElementById('costMembers').textContent,/NaN|Infinity/);assert.equal(b.requests.length,0);
});
for(const [price,target,text,on] of [[100,103,'99.00',true],[100,103.0001,'103.00',false],[100,97,'103.01',true],[100,96.9999,'97.00',false],[123.45,127.1535,'127.15',true],[123.45,127.1536,'127.15',false],[123.45,119.7465,'119.75',true],[123.45,119.7464,'119.75',false]])for(const kind of ['add','cut'])test('混合块复核：现价与设置价使用原值非显示舍入 '+[kind,price,target,text],async()=>{
  const b=await bootCash({prepareDocument(doc){const tr=qaQuote(doc,'AAA',price,target);tr.querySelector('.px-price').textContent=text;}}),r=b.rows.find(r=>r.sym==='AAA'),cap=kind==='add'?r.cap:r.cutCap;
  assert.equal(r.price,price);assert.equal(r.mv,price*5);assert.equal(cap.getAttribute('data-near'),on?'1':'0');assert.equal(r.tr.querySelector('.px-price').textContent,text);
  assert.equal(r.tr.className,'green');assert.equal(b.requests.length,0);assert.equal(b.store.writes.length,0);
});
test('混合块复核：缺失52周轨道仍计算持仓且重绘不拆分行情兄弟行',async()=>{
  const b=await bootCash({prepareDocument(doc){const rail=doc.getElementById('s-aaa').nextElementSibling;rail.innerHTML='<td class="range52-missing">52周数据待更新</td>';}}),r=b.rows.find(r=>r.sym==='AAA');
  const range=r.tr.nextElementSibling,parent=r.tr.parentNode;assert.equal(r.mv,500);assert.match(range.textContent,/数据待更新/);
  for(let i=0;i<4;i++)b.paint();assert.equal(r.tr.parentNode,parent);assert.equal(r.tr.nextElementSibling,range);assert.equal(range.parentNode,parent);assert.equal(b.doc.querySelectorAll('#s-aaa').length,1);
  assert.equal(b.requests.length,0);assert.equal(b.store.writes.length,0);
});
test('混合块复核：非法草稿期间旧ACK入口不读写也不吞错误',async()=>{
  const b=await boot();b.respond(b.requests[0],meta(SEED));await settle();await b.enter();b.edit('AAA','qty','bad');const n=b.requests.length;
  b.ack('AAA|add|95');await b.tick(180000);assert.equal(b.requests.length,n);assert.equal(b.puts().length,0);
  assert.equal(b.local()._local.editDraft['AAA|qty'].raw,'bad');assert.equal(b.state().dirty,true);assert.equal(b.state().positionsAuthorized,false);
  await b.save();assert.equal(b.state().editing,true);assert.equal(b.puts().length,0);b.doc.getElementById('costCancel').click();assert.equal(b.local().items.AAA.qty,5);
});
// 单框固收：实际输入事件驱动双字段，保存仍使用现有普通与批量链路。
test('固收单框DOM仅三项，打开和未编辑保存不改变旧云两字段',async()=>{
  const b=await boot();b.respond(b.requests[0],meta(SEED));await settle();await b.enter();
  const labels=b.doc.querySelectorAll('.ep-cash label');
  assert.deepEqual(labels.map(el=>el.querySelector('span').textContent),['现金 1','现金 2','固收成本']);
  assert.equal(b.doc.querySelectorAll('input[data-sym="BOND"]').length,1);
  assert.equal(b.doc.querySelector('input[data-f="fixed"]'),null);
  assert.equal(b.doc.querySelector('input[data-f="fixed_cost"]').value,'15');
  b.paint();await b.save();assert.equal(b.state().DATA.fixed,20);assert.equal(b.state().DATA.fixed_cost,15);
  assert.equal(b.puts().length,0);await b.dispose();
});
for(const raw of ['12.3456','20','15','0',''])test('固收单框普通保存双字段同步 '+(raw||'空框'),async()=>{
  const b=await boot();b.respond(b.requests[0],meta(SEED));await settle();await b.enter();
  b.edit('BOND','fixed_cost',raw);await b.tick(16);
  const value=Number(raw);assert.equal(b.state().DATA.fixed,value);assert.equal(b.state().DATA.fixed_cost,value);
  for(const f of ['fixed','fixed_cost'])assert.equal(b.local()._local.editDraft['BOND|'+f].raw,raw);
  assert.equal(b.state().positionsAuthorized,false);assert.equal(b.puts().length,0);
  await b.save();b.respond(b.requests.at(-1),meta(SEED));await settle();const put=b.puts()[0];assert.ok(put);
  assert.equal(b.payload(put).fixed,value);assert.equal(b.payload(put).fixed_cost,value);
  b.respond(put,{});await settle();assert.equal(b.state().CLOUD.fixed,value);assert.equal(b.state().CLOUD.fixed_cost,value);
  assert.equal(b.state().positionsAuthorized,false);await b.dispose();
});
test('固收单框普通其它字段保存保留旧固收差额',async()=>{
  const b=await boot();b.respond(b.requests[0],meta(SEED));await settle();await b.enter();b.edit('BBB','qty','3');
  await b.save();const put=await finishPush(b);assert.equal(b.payload(put).fixed,20);assert.equal(b.payload(put).fixed_cost,15);await b.dispose();
});
for(const batch of [false,true])test('固收单框取消恢复差异旧值与全部草稿 batch='+batch,async()=>{
  const b=await bootClear();if(batch)confirmMark(b);b.edit('BOND','fixed_cost','77.1234');
  b.doc.getElementById('costCancel').click();await settle();assert.equal(b.state().DATA.fixed,20);assert.equal(b.state().DATA.fixed_cost,15);
  assert.equal(b.local()._local.editDraft['BOND|fixed'],undefined);assert.equal(b.local()._local.editDraft['BOND|fixed_cost'],undefined);
  assert.deepEqual(b.clearState().draft,{});assert.equal(b.puts().length,0);await b.enter();
  assert.equal(b.doc.querySelector('input[data-f="fixed_cost"]').value,'15');await b.dispose();
});
for(const raw of ['-1','1.23456','bad','1e309'])for(const batch of [false,true])test('固收单框非法值保留旧双字段并阻止保存 '+raw+' batch='+batch,async()=>{
  const b=await bootClear();if(batch)confirmMark(b);const input=b.edit('BOND','fixed_cost',raw),n=b.requests.length;
  assert.ok(input.validationMessage);assert.equal(b.state().DATA.fixed,20);assert.equal(b.state().DATA.fixed_cost,15);
  await b.save();assert.equal(b.requests.length,n);assert.equal(b.puts().length,0);assert.equal(b.clearState().record,null);
  assert.ok(b.doc.querySelector('input[data-f="fixed_cost"]').validationMessage);assert.match(b.doc.getElementById('costNotice').textContent,/无效输入/);
  assert.equal(b.state().positionsAuthorized,false);await b.dispose();
});
for(const mode of ['清仓','新增'])for(const raw of ['20','15','0'])test('固收单框'+mode+'批量保存冻结两字段含未变化值 '+raw,async()=>{
  const b=await bootClear();if(mode==='清仓')confirmMark(b);else b.addPosition('CCC','long','1','2');
  b.edit('BOND','fixed_cost',raw);const put=await beginBatch(b),sent=b.payload(put),fields=b.clearState().record.fields;
  for(const f of ['fixed','fixed_cost']){assert.equal(fields['BOND|'+f],Number(raw));assert.equal(sent[f],Number(raw));}
  assert.equal(b.doc.querySelector('input[data-f="fixed_cost"]').disabled,true);
  await finishBatch(b,put);assert.equal(b.puts().length,2);assert.equal(b.state().DATA.fixed,Number(raw));assert.equal(b.state().DATA.fixed_cost,Number(raw));await b.dispose();
});
test('固收未编辑批量保存合并远端不同两字段而不强制相等',async()=>{
  const b=await bootClear();confirmMark(b);const remote={...copy(SEED),fixed:50,fixed_cost:22};
  const put=await beginBatch(b,CLEAR_CONFIG,remote);assert.equal(b.payload(put).fixed,50);assert.equal(b.payload(put).fixed_cost,22);
  assert.equal(b.clearState().record.fields['BOND|fixed'],undefined);assert.equal(b.clearState().record.fields['BOND|fixed_cost'],undefined);
  await finishBatch(b,put);await b.dispose();
});
for(const field of ['fixed','fixed_cost'])for(const batch of [false,true])test('固收单框远端任一同步字段冲突禁止覆盖 '+field+' batch='+batch,async()=>{
  const b=await bootClear();if(batch)confirmMark(b);b.edit('BOND','fixed_cost',field==='fixed'?'15':'20');await b.save();
  if(batch){await clearReply(b,'groups.json',CLEAR_GROUPS);await clearReply(b,'holdings.json',CLEAR_CONFIG);}
  const remote={...copy(SEED),[field]:SEED[field]+1};b.respond(b.requests.at(-1),batch?clearMeta(remote):meta(remote));await settle();
  assert.equal(b.puts().length,0);assert.match(b.doc.getElementById('costNotice').textContent,/同字段/);
  assert.equal(b.state().uncertain,null);await b.tick(180000);assert.equal(b.puts().length,0);
  if(!batch){assert.equal(b.state().positionsAuthorized,false);assert.equal(b.state().positionSave,null);}
  await b.dispose();
});
test('固收普通PUT已成功但回执超时先核验自己payload，不误判旧基线或重复写',async()=>{
  const b=await bootClear();b.edit('BOND','fixed_cost','12.3456');await b.save();
  b.respond(b.requests.at(-1),meta(SEED,'base-sha'));await settle();const put=b.puts()[0],sent=b.payload(put);
  assert.deepEqual(copy(b.state().inFlight.fixedBaseline),{fixed:20,fixed_cost:15});
  const resolve=pendingBody(b,put);await settle();await b.tick(b.state().deadline);
  assert.equal(b.state().uncertain.baseSha,'base-sha');assert.equal(b.state().uncertain.payload.fixed,12.3456);
  assert.equal(b.state().uncertain.payload.fixed_cost,12.3456);assert.equal(put.init.signal.aborted,true);
  await b.tick(15000);const recovery=b.requests.at(-1);assert.notEqual(recovery.init.method,'PUT');
  b.respond(recovery,meta(sent,'committed-sha'));await settle();
  assert.equal(b.puts().length,1);assert.equal(b.state().uncertain,null);assert.equal(b.state().dirty,false);
  assert.equal(b.state().positionsAuthorized,false);assert.equal(b.state().CLOUD.fixed,12.3456);assert.equal(b.state().CLOUD.fixed_cost,12.3456);
  assert.doesNotMatch(b.doc.getElementById('costNotice').textContent,/同字段/);
  resolve({});await settle();assert.equal(b.state().DATA.fixed,12.3456);assert.equal(b.state().DATA.fixed_cost,12.3456);
  assert.equal(b.puts().length,1);await b.dispose();
});
test('固收普通冲突后只有重新明确编辑保存才使用新基线',async()=>{
  const b=await bootClear();b.edit('BOND','fixed_cost','12');await b.save();
  const remote={...copy(SEED),fixed:23};b.respond(b.requests.at(-1),meta(remote));await settle();assert.equal(b.puts().length,0);
  await b.enter();b.edit('BOND','fixed_cost','13');await b.save();await b.tick(15000);b.respond(b.requests.at(-1),meta(remote));await settle();
  const put=b.puts()[0];assert.ok(put);assert.equal(b.payload(put).fixed,13);assert.equal(b.payload(put).fixed_cost,13);b.respond(put,{});await settle();await b.dispose();
});
test('新增清仓dialog仅保留必要选项和真实错误，无常驻解释或重复NOTICE',async()=>{
  const b=await bootClear(),clear=b.doc.querySelector('.cost-clear-dialog');
  assert.equal(clear.querySelectorAll('p').length,1);assert.equal(clear.querySelector('p').hasAttribute('data-clear-message'),true);
  assert.equal(clear.querySelector('[data-clear-undo]').textContent,'撤销标记');confirmMark(b);assert.equal(b.state().NOTICE,'');
  b.doc.querySelector('[data-cost-add]').click();const add=b.doc.querySelector('.cost-add-dialog');
  assert.equal(add.querySelectorAll('p').length,1);assert.equal(add.querySelector('p').hasAttribute('data-add-message'),true);
  assert.equal(add.querySelector('.cost-add-hold').querySelectorAll('label').length,2);
  add.querySelector('[data-add-symbol]').value='CCC';add.querySelector('[data-add-qty]').value='bad';add.querySelector('[data-add-confirm]').click();
  assert.match(add.querySelector('[data-add-message]').textContent,/数量必须/);assert.equal(add.open,true);
  add.querySelector('[data-add-qty]').value='1';add.querySelector('[data-add-confirm]').click();assert.equal(b.state().NOTICE,'');await b.dispose();
});
test('编辑字号与手机44像素红叉独立命中区，长代码限制在四列表格内',async()=>{
  const sym='A'.repeat(32),b=await bootClear({symbols:[sym,'BBB'],local:{...copy(SEED),items:{[sym]:{qty:1,cost:2},BBB:SEED.items.BBB}}});
  const row=b.doc.querySelector('[data-cost-row][data-sym="'+sym+'"]');
  assert.equal(row.querySelector('.ep-name').textContent.startsWith(sym),true);assert.equal(row.querySelector('.ep-clear').parentElement,row);
  assert.equal(row.nextElementSibling.getAttribute('data-f'),'cost');assert.equal(row.nextElementSibling.nextElementSibling.getAttribute('data-f'),'qty');
  assert.match(CSS,/#costManage \.ep-sym\{[^}]*display:grid;[^}]*grid-template-columns:auto minmax\(0,1fr\);[^}]*min-width:0;[^}]*font-size:16px/);
  assert.match(CSS,/#costManage \.ep-name\{[^}]*min-width:0;overflow:hidden;text-overflow:ellipsis/);
  const mobile=CSS.slice(CSS.indexOf('@media(max-width:600px)'));
  assert.match(mobile,/#costManage \.ep-clear\{width:44px;height:44px;font-size:24px/);assert.doesNotMatch(CSS,/position:absolute[^}]*44px/);
  assert.match(CSS,/#costManage \.ep-h\{font-size:15px/);assert.match(CSS,/#costManage \.ep-val\{font-size:16px/);
  assert.match(CSS,/#costManage \.ep-grid input,#costManage \.ep-cash input\{[^}]*font-size:16px/);
  assert.match(CSS,/#costManage \.ep-cash label\{[^}]*font-size:16px/);assert.match(CSS,/\.cost-acts button\{font-size:16px/);
  assert.match(CSS,/#costManage \[data-cost-add\]\{[^}]*margin:0 auto 0 0;[^}]*font-size:16px/);
  assert.match(CSS,/#costManage \.cost-clear-dialog h3\{[^}]*font-size:18px/);assert.match(CSS,/#costManage \.cost-clear-dialog label\{display:block;[^}]*font-size:16px/);
  assert.match(CSS,/#costManage \.cost-clear-dialog p\{[^}]*font-size:15px/);assert.match(CSS,/#costManage \.cost-add-dialog input:not\(\[type="radio"\]\)\{[^}]*font-size:16px/);
  assert.match(CSS,/#costManage \.cost-add-hold,#costManage \.cost-clear-choice\{display:flex;[^}]*gap:16px/);assert.match(CSS,/#costManage \.cost-add-hold label,#costManage \.cost-clear-choice label\{display:inline-flex/);
  assert.equal((CSS.match(/@media/g)||[]).length,1);assert.doesNotMatch(CSS,/@media\(max-width:(?!600)/);await b.dispose();
});

test('新增入口固定在保存操作栏左侧，关闭编辑或错误只读时隐藏且重绘不重复',async()=>{
  const b=await bootClear(),panel=b.doc.getElementById('costManage'),add=panel.querySelector('[data-cost-add]');
  assert.equal(add.parentElement,b.doc.getElementById('costActs'));assert.equal(add.hidden,false);
  assert.equal(panel.querySelectorAll('[data-cost-add]').length,1);assert.equal(b.doc.querySelector('#costMembers [data-cost-add]'),null);
  b.ctx.__costTest.paint();assert.equal(panel.querySelector('[data-cost-add]'),add);
  b.doc.getElementById('costCancel').click();assert.equal(add.hidden,true);await b.enter();assert.equal(add.hidden,false);
  assert.equal(panel.querySelectorAll('[data-cost-add]').length,1);await b.dispose();
});
test('清仓目的地使用独立横排容器且保留确认仅草稿',async()=>{
  const b=await bootClear(),d=b.doc.querySelector('.cost-clear-dialog'),choice=d.querySelector('.cost-clear-choice');
  assert.ok(choice);assert.equal(choice.querySelectorAll('label').length,2);
  assert.equal(choice.querySelector('input[value="focus"]').parentElement.textContent,'进重点');
  const n=b.requests.length;confirmMark(b,'focus');assert.equal(b.requests.length,n);await b.dispose();
});

const selectedTests=process.env.MM_COST_TEST_FILTER?tests.filter(t=>t.name.includes(process.env.MM_COST_TEST_FILTER)):tests;
(async()=>{let passed=0;assert.ok(selectedTests.length,'定向测试必须匹配真实用例');for(const t of selectedTests){try{await t.fn();passed++;console.log('PASS '+t.name);}catch(e){console.error('FAIL '+t.name+'\n'+e.stack);process.exitCode=1;}}console.log('\n仓位面板离线测试：'+passed+'/'+selectedTests.length+' 通过');})();
