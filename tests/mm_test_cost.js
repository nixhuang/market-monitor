'use strict';
// 完整执行真实面板脚本；仅注入可观察接口，不复制被测函数，不访问网络或真实浏览器存储。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { performance } = require('node:perf_hooks');
const SOURCE = fs.readFileSync(path.join(__dirname, '..', 'cost_panel.js'), 'utf8');
const CSS = fs.readFileSync(path.join(__dirname, '..', 'cost_panel.css'), 'utf8');
const LOCAL = 'mm_position_costs_local_v1';
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
    this.classList={remove:c=>{this.className=this.className.split(/\s+/).filter(x=>x!==c).join(' ');}};
  }
  get id(){return this.attrs.id||'';} set id(v){this.attrs.id=v;}
  get className(){return this.attrs.class||'';} set className(v){this.attrs.class=v;}
  get open(){return Object.hasOwn(this.attrs,'open');} set open(v){if(v)this.attrs.open='';else delete this.attrs.open;}
  get firstChild(){return this.children[0]||null;}
  get nextSibling(){if(!this.parentNode)return null;return this.parentNode.children[this.parentNode.children.indexOf(this)+1]||null;}
  get nextElementSibling(){let n=this.nextSibling;while(n&&n.tagName==='#TEXT')n=n.nextSibling;return n;}
  get previousElementSibling(){if(!this.parentNode)return null;return this.parentNode.children[this.parentNode.children.indexOf(this)-1]||null;}
  appendChild(n){if(n.parentNode)n.parentNode.removeChild(n);this.children.push(n);n.parentNode=this;return n;}
  insertBefore(n,b){if(!b)return this.appendChild(n);if(n.parentNode)n.parentNode.removeChild(n);let i=this.children.indexOf(b);assert.ok(i>=0);this.children.splice(i,0,n);n.parentNode=this;return n;}
  removeChild(n){this.children.splice(this.children.indexOf(n),1);n.parentNode=null;}
  replaceChild(n,o){this.insertBefore(n,o);this.removeChild(o);}
  remove(){if(this.parentNode)this.parentNode.removeChild(this);}
  setAttribute(k,v){this.attrs[k]=String(v);if(k==='value')this.value=decode(String(v));}
  getAttribute(k){return Object.hasOwn(this.attrs,k)?this.attrs[k]:null;}
  hasAttribute(k){return Object.hasOwn(this.attrs,k);}
  removeAttribute(k){delete this.attrs[k];}
  get textContent(){return this.tagName==='#TEXT'?this.text:this.children.map(c=>c.textContent).join('');}
  set textContent(v){this.children=[];if(String(v)){let n=new Element('#text',this.doc);n.text=String(v);this.appendChild(n);}}
  get innerHTML(){return this._html||'';}
  set innerHTML(html){
    this._html=html;this.children=[];this.doc.renders++;
    const stack=[this];
    for(const t of String(html).match(/<[^>]+>|[^<]+/g)||[]){
      if(t.startsWith('</')){if(stack.length>1)stack.pop();continue;}
      if(t.startsWith('<')){
        const name=t.match(/^<([\w-]+)/);if(!name)continue;
        const n=new Element(name[1],this.doc);
        const attrs=t.slice(name[0].length,-1);
        for(const m of attrs.matchAll(/([\w-]+)(?:="([^"]*)")?/g))n.setAttribute(m[1],decode(m[2]||''));
        stack.at(-1).appendChild(n);
        if(!['input','br','img','hr'].includes(name[1]))stack.push(n);
      }else{const n=new Element('#text',this.doc);n.text=decode(t);stack.at(-1).appendChild(n);}
    }
  }
  matches(selector){
    return selector.split(',').some(s=>{
      s=s.trim();let invalid=false;
      if(s.endsWith(':invalid')){invalid=true;s=s.slice(0,-8);}
      if(invalid&&!this.validationMessage)return false;
      const attrs=[...s.matchAll(/\[([\w-]+)(\^?=)?(?:"([^"]*)")?\]/g)];
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
  fire(k,props={}){let e={target:this,preventDefault(){},stopPropagation(){},...props};for(const fn of this.events[k]||[])fn(e);}
  click(){this.fire('click');}
  focus(){this.doc.activeElement=this;}
  setSelectionRange(a,b,d){this.selectionStart=a;this.selectionEnd=b;this.selectionDirection=d;}
  setCustomValidity(s){this.validationMessage=s;}
}
function makeDocument(opts={}){
  const doc={renders:0,activeElement:null,events:{}};
  doc.body=new Element('body',doc);doc.documentElement=doc.body;
  doc.createElement=t=>new Element(t,doc);
  doc.querySelectorAll=s=>doc.body.querySelectorAll(s);doc.querySelector=s=>doc.body.querySelector(s);
  doc.getElementById=id=>doc.querySelector('#'+id);
  doc.addEventListener=(k,f)=>(doc.events[k]||=[]).push(f);
  doc.fire=(k,e)=>{for(const f of doc.events[k]||[])f({preventDefault(){},stopPropagation(){},...e});};
  doc.body.innerHTML='<details id="group_positions"><summary><span class="grp-name"></span><span class="group-stats"></span></summary><table></table></details><div id="signalCounts"></div>';
  const table=doc.querySelector('table');table.className='stk';
  for(const sym of opts.symbols||['AAA','BBB']){
    const tr=new Element('tr',doc);tr.id='s-'+sym.toLowerCase();tr.className='green';
    tr.innerHTML='<td class="sym">'+sym+'<span class="note">测试名称</span></td><td><span class="px-price">100.00</span><span class="px-chg">+1.00%</span></td><td class="sig"><span class="lvtag add">加 95</span><span class="lvtag cut">减 104</span></td>';
    table.appendChild(tr);
    const rail=new Element('tr',doc);rail.className='range52-row';rail.innerHTML='<td><span class="range52-rail" aria-label="50.00低，本轮价 100.00 位于区间 50%，本轮高 150.00"><span class="range52-track"></span></span></td>';table.appendChild(rail);
  }
  for(const [sym,group] of Object.entries(opts.groups||{}))moveRow(doc,sym,group);
  if(opts.funds)addFundFixture(doc,opts.funds);
  if(opts.prepareDocument)opts.prepareDocument(doc);
  return doc;
}
function moveRow(doc,sym,group){
  let tr=doc.getElementById('s-'+sym.toLowerCase());assert.ok(tr);
  let rail=tr.nextElementSibling;
  if(group===null){tr.remove();if(rail)rail.remove();return;}
  let root=doc.getElementById('group_'+group);
  if(!root){root=doc.createElement('details');root.id='group_'+group;root.innerHTML='<summary></summary><table></table>';doc.body.appendChild(root);}
  let table=root.querySelector('table');table.appendChild(tr);if(rail)table.appendChild(rail);
}
function addFundFixture(doc, funds){
  (funds||[]).forEach(f=>{
    const tr=doc.getElementById('s-'+f.sym.toLowerCase());assert.ok(tr,'基本面 fixture 行必须存在 '+f.sym);
    if(f.marketLevel)tr.className=f.marketLevel;
    if(f.foldable)tr.setAttribute('data-fund-foldable','1');
    const cell=tr.querySelector('td.sig');const box=doc.createElement('div');
    box.className='fund-alert';box.setAttribute('data-fund-key',f.key||f.sym+'|fund|20260630');
    box.setAttribute('data-fund-symbol',f.sym);box.setAttribute('data-fund-level',f.level||'red');
    box.innerHTML='<details class="fund-detail"><summary class="tag f-red">基本面</summary><div class="fund-body">报告期风险详情</div></details>'
      +'<div class="cost-warn fund-ack-line" data-level="'+(f.level||'red')+'"><span class="w">⚠ 基本面警示</span>'
      +'<button type="button" class="ack-btn" data-ack="'+(f.key||f.sym+'|fund|20260630')+'">✓</button></div>';
    cell.appendChild(box);
  });
  if(!funds||!funds.length)return;
  const root=doc.createElement('details');root.id='signalCounts';const stats=doc.createElement('table');
  const body=doc.createElement('tbody');const row=doc.createElement('tr');
  row.innerHTML='<td><button type="button" class="signal-filter-toggle" data-kind="fund" aria-pressed="false">基本面</button></td>'
    +'<td class="num"><button type="button" class="signal-count-toggle" data-target="fundRed" aria-expanded="false">'
    +funds.filter(f=>(f.level||'red')==='red').length+'</button></td>'
    +'<td class="num"><button type="button" class="signal-count-toggle" data-target="fundYellow" aria-expanded="false">'
    +funds.filter(f=>f.level==='yellow').length+'</button></td><td class="num">—</td>';
  body.appendChild(row);
  const members=doc.createElement('tr');members.className='signal-members-row';members.innerHTML='<td colspan="4"></td>';
  for(const level of ['red','yellow']){
    const panel=doc.createElement('div');panel.className='signal-members-panel';panel.id='fund'+(level==='red'?'Red':'Yellow');
    const list=funds.filter(f=>(f.level||'red')===level).map(f=>'<li><a class="signal-member-jump" href="#s-'+f.sym.toLowerCase()+'">'+f.sym+'</a></li>').join('');
    panel.innerHTML='<ul>'+list+'</ul>';panel.hidden=true;members.children[0].appendChild(panel);
  }
  body.appendChild(members);stats.appendChild(body);root.appendChild(stats);doc.body.appendChild(root);
  const filter=doc.createElement('div');filter.id='signalFilterResult';filter.hidden=true;
  filter.setAttribute('data-filter',JSON.stringify({fund:{label:'基本面',members:funds.map(f=>({id:'s-'+f.sym.toLowerCase(),s:f.sym,n:'',g:'持仓',l:f.level||'red',d:['风险']}))}}));
  doc.body.appendChild(filter);
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
  const doc=makeDocument(opts), events={}, timers=new Map(), requests=[], readers=[], alerts=[];
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
    TextEncoder,TextDecoder,Uint8Array,Blob,AbortController,URL:{createObjectURL:()=> 'blob:mock',revokeObjectURL:()=>{}},
    btoa:s=>Buffer.from(s,'binary').toString('base64'),atob:s=>Buffer.from(s,'base64').toString('binary'),
    setTimeout,clearTimeout:id=>timers.delete(id),requestAnimationFrame:fn=>setTimeout(fn,16),cancelAnimationFrame:id=>timers.delete(id),
    FileReader:class{constructor(){readers.push(this);}readAsText(){}},
    fetch:(url,init={})=>new Promise((resolve,reject)=>requests.push({url,init,resolve,reject,at:now})),
    addEventListener:(k,f)=>(events[k]||=[]).push(f),alert:s=>alerts.push(s),prompt:()=>null,
    __MM_RULES__:opts.rules};
  ctx.window=ctx;
  const hooks=`globalThis.__costTest={normalize,canonical,mergeCloud,requestSave,autoPush,doPush,startPull,enterEdit,exitEdit,importData,paint,setAck,pruneAck,
    state:()=>({DATA:clone(DATA),CLOUD:CLOUD&&clone(CLOUD),revision,dirty,maintenanceOnly,maintenancePending,cloudVerified,uncertain:uncertain&&clone(uncertain),deadline:REQUEST_DEADLINE,activeRequests:activeRequests.length,editing,inFlight:inFlight&&clone(inFlight),queued,writer,SYNC,NOTICE,savePaused,storageErrors:clone(storageErrors)}),
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
  const api={...t,doc,ctx,store,session,requests,readers,alerts,tick,respond,edit,events,
    move:(sym,group)=>{moveRow(doc,sym,group);t.paint();},
    sessionLocal:()=>JSON.parse(session.values.get(SESSION_LOCAL)),
    fireWindow:(k,e={})=>{for(const f of events[k]||[])f(e);},
    enter:async()=>{t.enterEdit();await settle();},
    save:async()=>{doc.getElementById('costSave').click();await tick(0);},
    ack:(key,clear=false)=>{let el=new Element('button',doc);el.setAttribute(clear?'data-ack-clear':'data-ack',key);doc.fire('click',{target:el});},
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
const meta = (data,sha='remote-sha') => ({sha,content:Buffer.from(JSON.stringify(data)).toString('base64')});
const staleAck = () => ({...copy(SEED),acks:{'AAA|loss10':'2026-10-19T00:00:00Z'}});
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
test('dirty整份保留，云端不补零或缺项',async()=>{let b=await boot();await b.enter();b.edit('AAA','qty','0');b.edit('USD','cash','0');b.exitEdit(false);b.mergeCloud({...SEED,items:{AAA:{cost:555,qty:9},CCC:{cost:1,qty:1}}});assert.equal(b.state().DATA.items.AAA.qty,0);assert.equal(b.state().DATA.cash,0);assert.equal(b.state().DATA.items.CCC,undefined);});
test('clean云端仍按整份替换而不是合并持仓',async()=>{let b=await boot();b.mergeCloud({items:{AAA:{cost:1,qty:0}},cash:0});assert.equal(b.state().DATA.items.BBB,undefined);assert.equal(b.state().DATA.cash,0);});
test('导入ACK、固定收益、零值并刷新等值',async()=>{
  const funds=[{sym:'AAA',level:'red'}];let b=await boot({token:false,funds});let d={schema_version:2,real:true,updated_at:'2026-10-20T10:00:00Z',items:{AAA:{cost:200,qty:7}},cash:0,fixed:123,fixed_cost:100,acks:{'AAA|loss20':'2026-10-20T09:00:00Z','AAA|fund|20260630':'2026-10-20T09:30:00Z'},ack_tombstones:{'AAA|loss10':'2026-10-20T08:00:00Z'}};
  b.importData(d);assert.ok(b.state().NOTICE.includes('尚未上传'));let expected=b.canonical(b.state().DATA);await b.dispose();let c=await boot({store:b.store,funds});assert.equal(c.canonical(c.state().DATA),expected);assert.ok(c.state().DATA.acks['AAA|fund|20260630']);assert.equal(c.state().dirty,true);
});
const invalids=[
  ['数组items',{items:[]}],['null items',{items:null}],['字符串金额',{items:{},cash:'1'}],['负数量',{items:{AAA:{cost:1,qty:-1}}}],
  ['负成本',{items:{AAA:{cost:-1,qty:1}}}],['非有限数',{items:{},fixed:Infinity}],['null数值',{items:{},cash:null}],
  ['非法版本',{items:{},schema_version:99}],['字符串版本',{items:{},schema_version:'2'}],['数组acks',{items:{},acks:[]}],
  ['非法ACK值',{items:{},acks:{'AAA|loss10':true}}],['非法ACK键',{items:{},acks:{'bad':'2026-10-20'}}],
  ['属性注入日期',{items:{},updated_at:'2026-10-20" onmouseover="x'}],['原型标的',JSON.parse('{"items":{"__proto__":{"qty":1,"cost":1}}}')],
  ['非法删除记录',{items:{},ack_tombstones:{'AAA|loss10':'yesterday'}}]
];
for(const [name,data] of invalids)test('拒绝导入'+name+'且保留原值',async()=>{let b=await boot();const before=b.canonical(b.state().DATA);assert.throws(()=>b.importData(data));assert.equal(b.canonical(b.state().DATA),before);assert.throws(()=>b.mergeCloud(data));});
test('导入立即进统一队列，编辑会话退出',async()=>{let b=await boot();await b.enter();b.importData({...SEED,cash:99});await b.tick(0);assert.equal(b.state().editing,false);assert.ok(b.state().inFlight);assert.ok(b.state().NOTICE.includes('上传队列'));let p=await finishPush(b);assert.equal(b.payload(p).cash,99);assert.equal(b.state().dirty,false);});
test('文件读取迟到不覆盖之后输入',async()=>{let b=await boot();await b.enter();let input=b.doc.getElementById('costFile');input.fire('change',{target:{files:[{}],value:'x'}});b.edit('AAA','qty','9');let fr=b.readers[0];fr.result=JSON.stringify(SEED);fr.onload();assert.equal(b.state().DATA.items.AAA.qty,9);assert.match(b.state().NOTICE,/已变化/);});
test('文件读取失败有明确提示',async()=>{let b=await boot();b.doc.getElementById('costFile').fire('change',{target:{files:[{}],value:'x'}});b.readers[0].onerror();assert.match(b.state().NOTICE,/无法读取/);});
test('GET等待期间修改，PUT仍为最初快照',async()=>{let b=await boot();await b.enter();b.edit();await b.save();await b.enter();b.edit('AAA','qty','2');let p=await finishPush(b);assert.equal(b.payload(p).items.AAA.qty,1);assert.equal(b.state().CLOUD.items.AAA.qty,1);assert.equal(b.state().DATA.items.AAA.qty,2);assert.equal(b.state().dirty,true);assert.equal(b.state().editing,true);});
test('PUT等待期间修改不清dirty且不关闭新编辑',async()=>{let b=await boot();await b.enter();b.edit();await b.save();b.respond(b.requests.at(-1),{sha:'s'});await settle();await b.enter();b.edit('AAA','qty','2');b.respond(b.puts()[0],{});await settle();assert.equal(b.state().CLOUD.items.AAA.qty,1);assert.equal(b.state().dirty,true);assert.equal(b.state().editing,true);});
test('手动自动重试并发入口只有一个上传',async()=>{let b=await boot();await b.enter();b.edit();await b.save();b.autoPush();b.requestSave(0,true);b.doPush();await b.tick(5000);assert.equal(b.requests.length,2);await finishPush(b);assert.equal(b.puts().length,1);});
test('所有手动保存服从30秒节流并上传最新revision',async()=>{let b=await boot();await b.enter();b.edit();await b.save();let p=await finishPush(b);await b.enter();b.edit('AAA','qty','3');await b.save();await b.tick(29999);assert.equal(b.puts().length,1);await b.tick(1);let q=await finishPush(b);assert.equal(q.at-p.at,30000);assert.equal(b.payload(q).items.AAA.qty,3);});
test('刷新不重置30秒持久化节流',async()=>{let b=await boot();await b.enter();b.edit();await b.save();await finishPush(b);await b.dispose();let c=await boot({store:b.store,now:NOW+1000});await c.enter();c.edit('AAA','qty','8');await c.save();assert.equal(c.requests.length,1);await c.tick(29000);assert.equal(c.requests.length,2);});
test('失败重试受节流约束并读取最新待保存版本',async()=>{let b=await boot();await b.enter();b.edit();await b.save();await finishPush(b,500);assert.equal(b.state().dirty,true);await b.tick(15000);assert.equal(b.puts().length,1);await b.tick(15000);await finishPush(b);assert.equal(b.puts().length,2);assert.equal(b.state().dirty,false);});
test('GET失败不得当成不存在并发送PUT',async()=>{let b=await boot();await b.enter();b.edit();await b.save();b.respond(b.requests.at(-1),{message:'forbidden'},403);await settle();assert.equal(b.puts().length,0);assert.equal(b.state().SYNC,'error');});
test('canonical忽略对象键顺序和updated_at',async()=>{let b=await boot();let a=b.normalize(SEED),d=copy(a);d.items={BBB:{qty:2,cost:90},AAA:{qty:5,cost:100}};d.updated_at='2020-01-01';assert.equal(b.canonical(a),b.canonical(d));b.mergeCloud(d);await b.enter();b.edit('AAA','qty','5');await b.save();assert.equal(b.puts().length,0);assert.equal(b.state().dirty,false);});
test('旧pull在编辑后迟到不覆盖',async()=>{let b=await boot();let pull=b.requests[0];await b.enter();b.edit('AAA','qty','9');b.respond(pull,{content:Buffer.from(JSON.stringify(SEED)).toString('base64')});await settle();assert.equal(b.state().DATA.items.AAA.qty,9);});
test('旧pull在上传成功后迟到不覆盖',async()=>{let b=await boot();let pull=b.requests[0];await b.enter();b.edit('AAA','qty','9');await b.save();await finishPush(b);b.respond(pull,{content:Buffer.from(JSON.stringify(SEED)).toString('base64')});await settle();assert.equal(b.state().DATA.items.AAA.qty,9);assert.equal(b.state().dirty,false);});
test('拉取失败不上传内置快照',async()=>{let b=await boot();b.respond(b.requests[0],{},500);await settle();b.respond(b.requests.at(-1),{},404);await settle();await b.tick(1200);assert.equal(b.puts().length,0);assert.equal(b.state().inFlight,null);});
test('取消编辑恢复并立即落盘，不上传取消的草稿',async()=>{let b=await boot();await b.enter();b.edit('AAA','qty','8');let revision=b.state().revision;b.doc.getElementById('costCancel').click();await b.tick(60000);assert.equal(b.local().items.AAA.qty,5);assert.ok(b.state().revision>revision);assert.equal(b.puts().length,0);assert.equal(b.state().dirty,false);});
test('旧上传回执不会污染取消后的状态',async()=>{let b=await boot();await b.enter();b.edit('AAA','qty','1');await b.save();await b.enter();b.edit('AAA','qty','2');b.doc.getElementById('costCancel').click();await finishPush(b);assert.equal(b.state().DATA.items.AAA.qty,1);assert.equal(b.state().CLOUD.items.AAA.qty,1);await b.tick(0);assert.equal(b.state().dirty,false);});
test('折叠保留草稿并暂停待上传任务',async()=>{let b=await boot();await b.enter();b.edit('AAA','qty','8');let p=b.doc.getElementById('costManage');p.open=false;p.fire('toggle');await b.tick(60000);assert.equal(b.local().items.AAA.qty,8);assert.equal(b.state().dirty,true);assert.equal(b.state().editing,false);assert.equal(b.puts().length,0);});
test('上传中再次编辑并折叠，旧回执不自动上传草稿',async()=>{let b=await boot();await b.enter();b.edit();await b.save();await b.enter();b.edit('AAA','qty','8');let p=b.doc.getElementById('costManage');p.open=false;p.fire('toggle');await finishPush(b);await b.tick(60000);assert.equal(b.puts().length,1);assert.equal(b.state().dirty,true);});
test('基本面ACK按报告期键持久化且可撤销',async()=>{let b=await boot({funds:[{sym:'AAA',level:'red'}]});let key='AAA|fund|20260630';b.ack(key);assert.ok(b.state().DATA.acks[key]);b.ack(key);assert.ok(b.local().ack_tombstones[key]);assert.equal(b.state().DATA.acks[key],undefined);});
test('基本面已读折叠并从红黄统计名单筛选排除，其他行情警示保留',async()=>{
  const funds=[{sym:'AAA',level:'red',foldable:true},{sym:'BBB',level:'yellow',foldable:false}];
  let b=await boot({funds});const key='AAA|fund|20260630',redRow=b.doc.querySelector('.signal-filter-toggle[data-kind="fund"]').closest('tr');
  const aaa=b.doc.getElementById('s-aaa'),bbb=b.doc.getElementById('s-bbb'),table=aaa.closest('table.stk');
  b.ack(key);
  assert.equal(aaa.parentNode.tagName,'TBODY');assert.equal(aaa.parentNode.closest('.fund-ack-list').querySelectorAll('tr[id^="s-"]').length,1);
  assert.equal(b.doc.querySelectorAll('.fund-ack-list').length,1);assert.equal(bbb.parentNode,table);
  assert.equal(redRow.children[1].textContent,'—');
  assert.equal(redRow.children[2].querySelector('.signal-count-toggle').textContent,'1');
  const redPanel=b.doc.getElementById('fundRed');assert.equal(redPanel.hidden,true);
  assert.deepEqual(JSON.parse(b.doc.getElementById('signalFilterResult').getAttribute('data-filter')).fund.members.map(m=>m.s),['BBB']);
  b.ack(key);
  assert.equal(aaa.parentNode,table);assert.equal(b.doc.querySelectorAll('.fund-ack-list').length,0);
  assert.equal(b.doc.querySelectorAll('#signalCounts .signal-count-toggle')[0].textContent,'1');
  assert.equal(JSON.parse(b.doc.getElementById('signalFilterResult').getAttribute('data-filter')).fund.members.length,2);
  assert.ok(b.local().ack_tombstones[key]);assert.equal(aaa.className,'green');assert.equal(bbb.className,'green');
  assert.ok(redRow);
});
test('多个已读基本面共用单个折叠区并保持原序',async()=>{
  const funds=[{sym:'AAA',level:'red',foldable:true},{sym:'BBB',level:'red',foldable:true}];let b=await boot({funds});
  b.ack('AAA|fund|20260630');b.ack('BBB|fund|20260630');
  const buckets=b.doc.querySelectorAll('.fund-ack-list');assert.equal(buckets.length,1);
  const rows=buckets[0].querySelectorAll('tr[id^="s-"]');assert.deepEqual(rows.map(r=>r.id),['s-aaa','s-bbb']);
});
test('已读基本面并入本组已有「无异动」折叠表并更新计数，撤销后复原',async()=>{
  const funds=[{sym:'AAA',level:'red',foldable:true}];
  let b=await boot({funds,prepareDocument(doc){
    const table=doc.getElementById('s-aaa').closest('table.stk');
    const q=doc.createElement('details');q.className='quiet-list';
    q.innerHTML='<summary>无异动 3 只 · 点击查看</summary><table class="stk"><tbody></tbody></table>';
    table.parentNode.insertBefore(q,table.nextElementSibling);
  }});
  const key='AAA|fund|20260630',aaa=b.doc.getElementById('s-aaa');
  b.ack(key);
  const q=b.doc.querySelectorAll('details.quiet-list');assert.equal(q.length,1);
  assert.equal(b.doc.querySelectorAll('.fund-ack-list').length,0);
  assert.equal(q[0].querySelector('summary').textContent,'无异动 4 只 · 点击查看');
  assert.equal(aaa.closest('details.quiet-list'),q[0]);
  b.ack(key);
  assert.equal(q[0].querySelector('summary').textContent,'无异动 3 只 · 点击查看');
  assert.equal(aaa.closest('details.quiet-list'),null);
});
test('报告期变化或基本面风险消失时自动撤销旧ACK',async()=>{
  const old='AAA|fund|20260630',next='AAA|fund|20260930';
  let b=await boot({local:{...SEED,acks:{[old]:'2026-10-20T09:00:00Z'}},funds:[{sym:'AAA',key:next,level:'yellow'}]});
  assert.equal(b.state().DATA.acks[old],undefined);assert.ok(b.local().ack_tombstones[old]);assert.equal(b.state().DATA.acks[next],undefined);
  let c=await boot({local:{...SEED,acks:{[old]:'2026-10-20T09:00:00Z'}}});
  assert.equal(c.state().DATA.acks[old],undefined);assert.ok(c.local().ack_tombstones[old]);
});
test('ACK撤销写tombstone，旧云端并集不能复活',async()=>{let b=await boot();let key='AAA|add|95';b.ack(key);b.ack(key);assert.ok(b.local().ack_tombstones[key]);assert.equal(b.state().DATA.acks[key],undefined);let old=copy(SEED);old.acks={[key]:'2026-10-19T00:00:00Z'};b.mergeCloud(old);assert.equal(b.state().DATA.acks[key],undefined);});
test('clean拉取也保留较新的ACK撤销记录',async()=>{let key='AAA|loss10';let d={...SEED,ack_tombstones:{[key]:'2026-10-20T00:00:00Z'}};let b=await boot({local:d});b.mergeCloud({...SEED,acks:{[key]:'2026-10-19T00:00:00Z'}});assert.equal(b.state().DATA.acks[key],undefined);assert.ok(b.state().DATA.ack_tombstones[key]);assert.equal(b.state().dirty,true);});
test('较新的明确ACK可以再次确认',async()=>{let key='AAA|loss20';let b=await boot({local:{...SEED,ack_tombstones:{[key]:'2026-10-20T00:00:00Z'}}});let d=copy(SEED);d.items.AAA.cost=200;d.acks={[key]:'2026-10-20T01:00:00Z'};b.mergeCloud(d);assert.ok(b.state().DATA.acks[key]);});
test('自动清理已读持久化待维护操作而非未核验整份dirty',async()=>{
  let b=await boot({local:{...SEED,acks:{'AAA|loss10':'2026-10-19T00:00:00Z'}},token:false});
  assert.ok(b.local().ack_tombstones['AAA|loss10']);assert.equal(b.state().dirty,false);
  assert.equal(b.local()._local.maintenancePending,true);assert.equal(b.local()._local.maintenanceOnly,true);
  assert.equal(b.state().revision,0);assert.match(b.doc.getElementById('costState').textContent,/待云端核验/);
});
test('清仓后重绘成本数量和loss状态不会残留',async()=>{let d=copy(SEED);d.items.AAA.cost=200;let b=await boot({local:d});await b.enter();b.edit('AAA','qty','0');await b.tick(16);assert.equal(b.rows[0].qty,0);assert.equal(b.rows[0].lossLv,0);assert.equal(b.doc.querySelector('input[data-sym="AAA"][data-f="qty"]').value,'0');});
test('零固定收益也能在编辑态输入',async()=>{let b=await boot({local:{...SEED,fixed:0,fixed_cost:0}});await b.enter();b.edit('BOND','fixed','12.3456');assert.equal(b.local().fixed,12.3456);});
test('本地写失败显式提示且不清dirty不上传',async()=>{let b=await boot();b.store.failSet.add(LOCAL);await b.enter();b.edit();await b.save();assert.equal(b.state().dirty,true);assert.match(b.doc.getElementById('costState').textContent,/未持久化/);assert.equal(b.puts().length,0);assert.ok(b.doc.querySelector('[data-cost-export]'));});
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
test('不支持Web Locks安全降级为隔离会话编辑',async()=>{
  let b=await boot({noLocks:true,protocol:'file:'});let original=b.store.values.get(LOCAL);
  await b.enter();assert.equal(b.state().editing,true);b.edit('AAA','qty','9');await b.save();
  assert.equal(b.sessionLocal().items.AAA.qty,9);assert.equal(b.store.values.get(LOCAL),original);assert.equal(b.requests.length,0);
  assert.match(b.doc.getElementById('costState').textContent,/隔离预览/);assert.match(b.doc.getElementById('costFoot').textContent,/关闭标签页前请导出/);
});
test('跨tab取得写权后依旧遵守持久化节流',async()=>{let b=await boot();await b.enter();b.edit();await b.save();await finishPush(b);let c=await boot({store:b.store,now:NOW+1000});await b.dispose();await c.enter();c.edit('AAA','qty','7');await c.save();assert.equal(c.requests.length,0);await c.tick(29000);assert.equal(c.requests.length,1);});
test('输入每次立即落盘但同帧只重绘一次并保留选区',async()=>{
  let b=await boot();await b.enter();let before=b.doc.renders,writes=b.store.writes.filter(x=>x.k===LOCAL).length;
  let start=performance.now();for(let i=1;i<=20;i++)b.edit('AAA','qty',String(i));let inputMs=performance.now()-start;
  const el=b.doc.activeElement;el.value='20.0000';el.setSelectionRange(1,5,'backward');
  assert.equal(b.doc.renders,before);assert.equal(b.store.writes.filter(x=>x.k===LOCAL).length-writes,20);assert.equal(b.local().items.AAA.qty,20);
  await b.tick(16);let renders=b.doc.renders-before;assert.ok(renders>0);assert.equal(b.doc.activeElement.value,'20.0000');assert.equal(b.doc.activeElement.selectionStart,1);assert.equal(b.doc.activeElement.selectionEnd,5);assert.equal(b.doc.activeElement.selectionDirection,'backward');
  console.log('  输入微基准：20次即时持久化 '+inputMs.toFixed(2)+'ms；合帧渲染写入 '+renders+' 处（mock环境，非浏览器性能结论）');
});
test('拒绝非法输入而不把前缀数字当有效值',async()=>{let b=await boot();await b.enter();let e=b.edit('AAA','qty','2oops');assert.ok(e.validationMessage);assert.equal(b.local().items.AAA.qty,5);});
test('加减阈值分别读取且零阈值不fallback',async()=>{let b=await boot({rules:{trigger_gap_pct:6,reduce_gap_pct:0}});assert.equal(b.rows[0].warnAdd.style.display,'');assert.equal(b.rows[0].warnCut.style.display,'none');let c=await boot({rules:{trigger_gap_pct:0,reduce_gap_pct:4}});assert.equal(c.rows[0].warnAdd.style.display,'none');assert.equal(c.rows[0].warnCut.style.display,'');});
test('旧页面规则缺失才使用3%',async()=>{let b=await boot();assert.equal(b.rows[0].warnAdd.style.display,'none');assert.equal(b.rows[0].warnCut.style.display,'none');});
test('慢GET后两次实际PUT也必须间隔30秒',async()=>{
  let b=await boot();await b.enter();b.edit();await b.save();await b.tick(45000);let p=await finishPush(b);
  await b.enter();b.edit('AAA','qty','2');await b.save();await b.tick(29999);assert.equal(b.puts().length,1);
  await b.tick(1);let q=await finishPush(b);assert.equal(q.at-p.at,30000);
});
test('旧cloud缓存未核验时不能直接清dirty',async()=>{
  let s=shared({[LOCAL]:{...SEED,_local:{dirty:true,revision:3}},[CLOUD]:SEED,[TOKEN]:'test-token'});
  let b=await boot({store:s});b.requestSave(0,true);await b.tick(0);assert.equal(b.state().dirty,true);assert.ok(b.state().inFlight);
  let remote={...SEED,cash:123};b.respond(b.requests.at(-1),{sha:'s',content:Buffer.from(JSON.stringify(remote)).toString('base64')});await settle();assert.equal(b.puts().length,1);
});
test('GET核验canonical相同则不产生重复PUT',async()=>{
  let b=await boot();await b.enter();b.edit('AAA','qty','7');await b.save();let d=b.state().DATA;d.items={BBB:{qty:2,cost:90},AAA:{qty:7,cost:100}};
  b.respond(b.requests.at(-1),{sha:'s',content:Buffer.from(JSON.stringify(d)).toString('base64')});await settle();assert.equal(b.puts().length,0);assert.equal(b.state().dirty,false);
});
test('损坏本地存储在云端返回后也不自动被覆盖',async()=>{
  let s=shared({[LOCAL]:'{invalid',[TOKEN]:'test-token'});let b=await boot({store:s});b.respond(b.requests[0],{content:Buffer.from(JSON.stringify(SEED)).toString('base64')});await settle();assert.equal(s.values.get(LOCAL),'{invalid');assert.equal(b.state().dirty,true);
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
test('自动ACK上传中撤销不会让旧回执清除dirty',async()=>{
  let b=await boot({rules:{trigger_gap_pct:6,reduce_gap_pct:3}});let key='AAA|add|95';b.ack(key);await b.tick(1200);b.ack(key);let p=await finishPush(b);assert.ok(b.payload(p).acks[key]);assert.equal(b.state().DATA.acks[key],undefined);assert.equal(b.state().dirty,true);await b.tick(30000);let q=await finishPush(b);assert.ok(b.payload(q).ack_tombstones[key]);assert.equal(b.state().dirty,false);
});
test('无效输入经过重绘仍阻止保存',async()=>{
  let b=await boot();await b.enter();b.edit('AAA','qty','-1');b.paint();await b.save();assert.equal(b.state().editing,true);assert.equal(b.puts().length,0);assert.equal(b.local().items.AAA.qty,5);
});
test('初始非持仓残留旧成本数量不计市值权重及贡献',async()=>{
  let d=copy(SEED);d.items.AAA={cost:200,qty:1000};let b=await boot({local:d,groups:{AAA:'focus'}});
  assert.equal(b.doc.querySelector('.cost-sum>b').textContent,'$250');
  assert.equal(b.doc.querySelector('#costMembers li[data-sym="AAA"]'),null);
  assert.match(b.doc.querySelector('.blk-cash .t-wt').textContent,/12\.0%/);
  assert.equal(b.doc.querySelector('.cost-contrib').textContent.includes('AAA'),false);
  assert.equal(b.rows.find(r=>r.sym==='AAA').lossLv,0);assert.equal(b.state().DATA.items.AAA.qty,1000);
  assert.equal(b.doc.getElementById('s-aaa').className,'green');
});
test('持仓移到其他组即时排除并清除成本亏损显示',async()=>{
  let d=copy(SEED);d.items.AAA={cost:200,qty:5};let b=await boot({local:d});let r=b.rows[0];assert.equal(r.lossLv,2);
  b.move('AAA','focus');assert.equal(r.lossLv,0);assert.equal(r.has,false);assert.equal(r.warn.style.display,'none');assert.equal(r.info.textContent,'');
  assert.equal(r.tick.style.display,'none');assert.equal(r.rail.getAttribute('aria-label'),r.lab0);assert.equal(r.tr.className,'green');
  assert.equal(b.doc.querySelector('.cost-sum>b').textContent,'$250');assert.equal(b.state().DATA.items.AAA.cost,200);assert.equal(b.state().DATA.cash,30);
});
test('移出全部持仓仍显示现金固定收益且持仓数量归零',async()=>{
  let b=await boot({groups:{AAA:'focus',BBB:'focus'}});assert.equal(b.doc.querySelector('.cost-sum>b').textContent,'$50');
  assert.match(b.doc.querySelector('#group_positions>summary .grp-name').textContent,/持仓 \(0\)/);
  assert.match(b.doc.querySelector('.is-total').textContent,/0 只/);assert.equal(b.doc.querySelector('.cost-contrib'),null);
  assert.match(b.doc.querySelector('.blk-cash .t-wt').textContent,/60\.0%/);await b.enter();assert.ok(b.doc.querySelector('input[data-f="cash"]'));
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
test('移出再移回后的亏损提醒重新生效且不删除加减仓ACK',async()=>{
  let d=copy(SEED);d.items.AAA.cost=200;d.acks={'AAA|loss20':'2026-10-19T00:00:00Z','AAA|add|95':'2026-10-19T00:00:00Z','AAA|cut|104':'2026-10-19T00:00:00Z'};
  let b=await boot({local:d,rules:{trigger_gap_pct:6,reduce_gap_pct:4},token:false});b.move('AAA','focus');
  assert.equal(b.state().DATA.acks['AAA|loss20'],undefined);assert.ok(b.state().DATA.ack_tombstones['AAA|loss20']);
  assert.ok(b.state().DATA.acks['AAA|add|95']);assert.ok(b.state().DATA.acks['AAA|cut|104']);b.move('AAA','positions');
  assert.equal(b.rows[0].lossLv,2);assert.equal(b.rows[0].lossAcked,false);assert.equal(b.rows[0].tr.className,'red');
});
test('非持仓重点组保留独立加仓和减仓提醒',async()=>{
  let b=await boot({groups:{AAA:'focus'},rules:{trigger_gap_pct:6,reduce_gap_pct:4}});let r=b.rows.find(r=>r.sym==='AAA');
  assert.equal(r.warnAdd.style.display,'');assert.equal(r.warnCut.style.display,'');assert.equal(r.warn.style.display,'none');
  b.ack('AAA|add|95');assert.equal(r.tr.className,'red');b.ack('AAA|cut|104');assert.equal(r.tr.className,'green');
  assert.ok(b.doc.getElementById('s-aaa'));assert.equal(b.state().DATA.items.AAA.cost,100);
});
test('非持仓只有减仓胶囊时依旧提醒不依赖加仓胶囊',async()=>{
  let b=await boot({groups:{AAA:'focus'},rules:{trigger_gap_pct:6,reduce_gap_pct:4},prepareDocument:d=>d.querySelector('#s-aaa .lvtag.add').remove()});
  let r=b.rows.find(r=>r.sym==='AAA');assert.equal(r.trig,0);assert.equal(r.warnCut.style.display,'');assert.equal(r.tr.className,'red');
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
test('缺失52周范围不漏算持仓也不影响重点组减仓提醒',async()=>{
  let b=await boot({groups:{AAA:'focus'},rules:{reduce_gap_pct:4},prepareDocument:d=>{d.getElementById('s-aaa').nextElementSibling.remove();d.getElementById('s-bbb').nextElementSibling.remove();}});
  assert.equal(b.doc.querySelector('.cost-sum>b').textContent,'$250');assert.equal(b.rows.find(r=>r.sym==='AAA').warnCut.style.display,'');
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
test('无锁隔离导入ACK自动保存重试全部禁止上传',async()=>{
  let b=await boot({noLocks:true,rules:{trigger_gap_pct:6,reduce_gap_pct:4}});b.importData({...SEED,cash:77});b.ack('AAA|add|95');
  b.requestSave(0,true);b.autoPush();b.startPull();await b.tick(90000);assert.equal(b.requests.length,0);assert.equal(b.store.writes.length,0);
  assert.equal(b.sessionLocal().cash,77);assert.match(b.state().NOTICE,/隔离预览不上传/);
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
  assert.match(b.doc.querySelectorAll('.blk-long .cost-block-title')[1].querySelector('.t-mv').textContent,/\$500/);
  b.move('AAA','focus');assert.equal(b.doc.querySelector('.cost-sum>b').textContent,'$250');assert.equal(b.doc.querySelectorAll('.blk-long .cost-block-title')[1].querySelector('.t-mv').textContent,'$0');
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
test('启动过期ACK清理不使初始pull失效且不覆盖新云端数值',async()=>{
  let b=await boot({local:staleAck()}), remote=freshRemote();
  assert.equal(b.state().dirty,false);assert.equal(b.state().revision,0);assert.equal(b.state().CLOUD,null);
  b.respond(b.requests[0],meta(remote));await settle();
  assert.equal(b.state().DATA.items.AAA.qty,50);assert.equal(b.state().DATA.cash,9876);
  assert.equal(b.state().CLOUD.items.AAA.qty,50);assert.ok(b.state().DATA.ack_tombstones['AAA|loss10']);
  await b.tick(1200);b.respond(b.requests.at(-1),meta(remote));await settle();let put=b.puts()[0];
  assert.ok(put);assert.equal(b.payload(put).items.AAA.qty,50);assert.equal(b.payload(put).cash,9876);
  b.respond(put,{});await settle();assert.equal(b.state().dirty,false);
});
test('ACK维护初始pull永久pending也绝不上传旧数值',async()=>{
  let b=await boot({local:staleAck()});await b.tick(b.state().deadline*8);
  assert.equal(b.puts().length,0);assert.equal(b.state().inFlight,null);assert.equal(b.state().dirty,false);
  assert.equal(b.state().maintenancePending,true);assert.equal(b.local().items.AAA.qty,5);
});
test('ACK待维护跨刷新保持clean拉取资格',async()=>{
  let b=await boot({local:staleAck()});await b.dispose();let c=await boot({store:b.store});
  assert.equal(c.state().dirty,false);c.respond(c.requests[0],meta(freshRemote()));await settle();
  assert.equal(c.state().DATA.items.AAA.qty,50);assert.equal(c.state().DATA.cash,9876);assert.ok(c.state().DATA.ack_tombstones['AAA|loss10']);
});
test('ACK维护上传GET必须再次以更晚远端数值为基底',async()=>{
  let b=await boot({local:staleAck()});b.respond(b.requests[0],meta(freshRemote()));await settle();await b.tick(1200);
  let remote=freshRemote();remote.items.AAA.qty=88;remote.cash=12345;b.respond(b.requests.at(-1),meta(remote,'new-sha'));await settle();
  let put=b.puts()[0];assert.equal(b.payload(put).items.AAA.qty,88);assert.equal(b.payload(put).cash,12345);
  assert.equal(JSON.parse(put.init.body).sha,'new-sha');b.respond(put,{});await settle();assert.equal(b.state().DATA.cash,12345);assert.equal(b.state().dirty,false);
});
test('ACK维护远端缺失不创建旧仓位文件',async()=>{
  let b=await boot({local:staleAck()});b.respond(b.requests[0],meta(freshRemote()));await settle();await b.tick(1200);
  b.respond(b.requests.at(-1),{},404);await settle();assert.equal(b.puts().length,0);assert.equal(b.state().SYNC,'error');
  assert.equal(b.state().maintenanceOnly,true);assert.equal(b.state().DATA.items.AAA.qty,50);
});
test('未核验ACK维护仅点保存仍先拉取而非升格为用户数值编辑',async()=>{
  let b=await boot({local:staleAck()});await b.enter();await b.save();
  assert.equal(b.state().maintenanceOnly,true);assert.equal(b.state().dirty,false);assert.equal(b.puts().length,0);
  b.respond(b.requests.at(-1),meta(freshRemote()));await settle();assert.equal(b.state().DATA.cash,9876);
});
test('ACK维护期间取消数值编辑恢复原待维护语义',async()=>{
  let b=await boot({local:staleAck()});await b.enter();b.edit('AAA','qty','7');b.doc.getElementById('costCancel').click();await settle();
  assert.equal(b.state().maintenanceOnly,true);assert.equal(b.state().dirty,false);
  b.respond(b.requests.at(-1),meta(freshRemote()));await settle();assert.equal(b.state().DATA.items.AAA.qty,50);
});
test('ACK维护与实际用户输入并存时仍保护用户dirty数值',async()=>{
  let b=await boot({local:staleAck()});await b.enter();b.edit('AAA','qty','7');
  b.respond(b.requests[0],meta(freshRemote()));await settle();assert.equal(b.state().DATA.items.AAA.qty,7);
  assert.equal(b.state().maintenanceOnly,false);assert.equal(b.state().dirty,true);
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
test('PUT未确认事务跨刷新保存且新页先核验不盲目重传',async()=>{
  let b=await boot();b.respond(b.requests[0],meta(SEED));await settle();let put=await beginPut(b);await b.dispose();assert.equal(put.init.signal.aborted,true);
  let c=await boot({store:b.store,now:NOW+61000});assert.ok(c.state().uncertain);c.respond(c.requests[0],meta(b.payload(put),'done-sha'));await settle();
  assert.equal(c.state().dirty,true);c.requestSave(0,true);await c.tick(0);c.respond(c.requests.at(-1),meta(b.payload(put),'done-sha'));await settle();
  assert.equal(c.puts().length,0);assert.equal(c.state().dirty,false);assert.equal(c.state().uncertain,null);
});
test('PUT在途pagehide取消等待释放锁而接管页仍执行SHA核验',async()=>{
  let b=await boot();b.respond(b.requests[0],meta(SEED));await settle();let put=await beginPut(b),c=await boot({store:b.store,now:NOW+61000});
  await b.dispose();assert.equal(b.state().activeRequests,0);assert.equal(b.state().inFlight,null);await c.enter();assert.equal(c.state().writer,true);assert.ok(c.state().uncertain);
  await c.save();let get=c.requests.at(-1);assert.notEqual(get.init.method,'PUT');c.respond(get,meta(b.payload(put),'finished-sha'));await settle();
  assert.equal(c.puts().length,0);assert.equal(c.state().dirty,false);
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
test('纯ACK维护PUT超时后外部更新不会再次带回旧仓位值',async()=>{
  let b=await boot({local:staleAck()}),remote=freshRemote();b.respond(b.requests[0],meta(remote,'base-sha'));await settle();await b.tick(1200);
  b.respond(b.requests.at(-1),meta(remote,'base-sha'));await settle();let old=b.puts()[0];await b.tick(b.state().deadline);
  let newer=freshRemote();newer.cash=22222;newer.items.AAA.qty=77;b.requestSave(0,true);await b.tick(0);
  b.respond(b.requests.at(-1),meta(newer,'external-sha'));await settle();assert.equal(b.state().DATA.cash,22222);
  await b.tick(30000);b.respond(b.requests.at(-1),meta(newer,'external-sha'));await settle();let put=b.puts()[1];
  assert.equal(b.payload(put).cash,22222);assert.equal(b.payload(put).items.AAA.qty,77);b.respond(put,{});await settle();
  b.respond(old,{});await settle();assert.equal(b.state().CLOUD.cash,22222);assert.equal(b.state().dirty,false);
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
test('所有数值CSS居中，列宽字号灯级与名称靠左保留',()=>{
  assert.equal(CSS.includes('text-align:right'),false);
  for(const selector of ['.cost-colhead span','.cost-block-title>span','.cost-cell','.cost-cell input','.cost-mini .c-cost','.cost-mini .t-mv','.cost-mini .t-pnl','.cost-mini .t-wt','.cost-mini input','.cost-contrib .cv,.cost-contrib .ca']){
    const escaped=selector.replace(/[.*+?^${}()|[\]\\]/g,'\\$&');assert.match(CSS,new RegExp(escaped+'\\{[^}]*text-align:center'));
  }
  assert.match(CSS,/\.cost-name\{[^}]*text-align:left/);assert.match(CSS,/\.cost-contrib \.cn-name\{[^}]*text-align:left/);
  assert.match(CSS,/grid-template-columns:minmax\(110px,1\.55fr\) \.9fr \.85fr 1\.05fr 1\.45fr \.8fr/);
  assert.match(CSS,/\.cost-cell\.up\{color:var\(--up\)\}/);assert.match(CSS,/#costMembers li\.lv-red::marker\{color:var\(--red\)\}/);
});

(async()=>{let passed=0;for(const t of tests){try{await t.fn();passed++;console.log('PASS '+t.name);}catch(e){console.error('FAIL '+t.name+'\n'+e.stack);process.exitCode=1;}}console.log('\n仓位面板离线测试：'+passed+'/'+tests.length+' 通过');})();
