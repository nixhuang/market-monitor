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
  set textContent(v){for(const n of this.children)n.parentNode=null;this.children=[];if(String(v)){let n=new Element('#text',this.doc);n.text=String(v);this.appendChild(n);}}
  get innerHTML(){return this._html||'';}
  set innerHTML(html){
    this._html=html;for(const n of this.children)n.parentNode=null;this.children=[];this.doc.renders++;
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
  dispatchEvent(e){if(!e.target)e.target=this;for(const fn of this.events[e.type]||[])fn.call(this,e);return !e.defaultPrevented;}
  fire(k,props={}){return this.dispatchEvent(Object.assign(new MockCustomEvent(k),props));}
  click(){this.fire('click');}
  focus(){this.doc.activeElement=this;}
  setSelectionRange(a,b,d){this.selectionStart=a;this.selectionEnd=b;this.selectionDirection=d;}
  setCustomValidity(s){this.validationMessage=s;}
}
class MockCustomEvent {
  constructor(type,opts={}){this.type=type;this.detail=opts.detail??null;this.cancelable=!!opts.cancelable;this.defaultPrevented=false;}
  preventDefault(){if(this.cancelable)this.defaultPrevented=true;}
  stopPropagation(){}
}
function makeDocument(opts={}){
  const doc={renders:0,activeElement:null,events:{}};
  doc.body=new Element('body',doc);doc.documentElement=doc.body;
  doc.createElement=t=>new Element(t,doc);
  doc.querySelectorAll=s=>doc.body.querySelectorAll(s);doc.querySelector=s=>doc.body.querySelector(s);
  doc.getElementById=id=>doc.querySelector('#'+id);
  doc.addEventListener=(k,f)=>(doc.events[k]||=[]).push(f);
  doc.dispatchEvent=Element.prototype.dispatchEvent;
  doc.fire=(k,e={})=>doc.dispatchEvent(Object.assign(new MockCustomEvent(k),e));
  doc.body.innerHTML='<details id="group_positions"><summary><span class="grp-name"></span><span class="group-stats"></span></summary><table></table></details><div id="signalCounts"></div>';
  const table=doc.querySelector('table');table.className='stk';
  for(const sym of opts.symbols||['AAA','BBB']){
    const tr=new Element('tr',doc);tr.id='s-'+sym.toLowerCase();tr.className='green';
    tr.innerHTML='<td class="sym">'+sym+'<span class="note">测试名称</span></td><td><span class="px-price">100.00</span><span class="px-chg">+1.00%</span></td><td class="sig"><span class="lvtag add">加 95</span><span class="lvtag cut">减 104</span></td>';
    table.appendChild(tr);
    const rail=new Element('tr',doc);rail.className='range52-row';rail.innerHTML='<td><span class="range52-rail" aria-label="50.00低，本轮价 100.00 位于区间 50%，本轮高 150.00"><span class="range52-track"></span></span></td>';table.appendChild(rail);
  }
  for(const [sym,group] of Object.entries(opts.groups||{}))moveRow(doc,sym,group);
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
    alert:s=>alerts.push(s),prompt:()=>null,
    __MM_RULES__:opts.rules};
  ctx.window=ctx;
  const hooks=`globalThis.__costTest={normalize,canonical,mergeCloud,requestSave,autoPush,doPush,startPull,enterEdit,exitEdit,importData,exportData,cashFirst,nearPrice,ackStatEntries,paint,setAck,pruneAck,
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
  const api={...t,doc,ctx,store,session,requests,readers,alerts,blobs,tick,respond,edit,events,dispatched,
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
  let b=await boot({token:false});let d={schema_version:2,real:true,updated_at:'2026-10-20T10:00:00Z',items:{AAA:{cost:200,qty:7}},cash:0,fixed:123,fixed_cost:100,acks:{'AAA|loss20':'2026-10-20T09:00:00Z','AAA|fund|20260630':'2026-10-20T09:30:00Z'},ack_tombstones:{'AAA|loss10':'2026-10-20T08:00:00Z'}};
  b.importData(d);assert.ok(b.state().NOTICE.includes('尚未上传'));let expected=b.canonical(b.state().DATA);await b.dispose();let c=await boot({store:b.store});assert.equal(c.canonical(c.state().DATA),expected);assert.equal(c.state().DATA.acks['AAA|fund|20260630'],undefined);assert.ok(c.state().DATA.acks['AAA|loss20']);assert.equal(c.state().dirty,true);
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
test('退役fund ACK与撤销记录静默剔除且不制造dirty或提交风暴',async()=>{
  const old='AAA|fund|20260630',hash='BBB|fund|abcd1234';
  let d={...copy(SEED),acks:{[old]:'2026-10-20T09:00:00Z'},ack_tombstones:{[hash]:'2026-10-20T09:00:00Z'}};
  let b=await boot({local:d});
  assert.deepEqual(copy(b.state().DATA.acks),{});assert.deepEqual(copy(b.state().DATA.ack_tombstones),{});
  assert.equal(b.state().dirty,false);assert.equal(b.state().revision,0);assert.equal(b.state().maintenancePending,false);
  const before=b.canonical(b.state().DATA),writes=b.store.writes.length;
  b.ack(old);b.setAck(hash,true);b.paint();
  assert.equal(b.canonical(b.state().DATA),before);assert.equal(b.store.writes.length,writes);
  b.respond(b.requests[0],meta(d));await settle();await b.tick(90000);
  assert.equal(b.state().dirty,false);assert.equal(b.state().maintenancePending,false);assert.equal(b.puts().length,0);
  await b.dispose();let c=await boot({store:b.store});
  c.mergeCloud(d);c.paint();await c.tick(90000);
  assert.deepEqual(copy(c.state().DATA.acks),{});assert.equal(c.puts().length,0);assert.equal(c.state().dirty,false);
});
test('旧fund专用待维护刷新后取消空维护且不影响普通ACK维护',async()=>{
  const key='AAA|fund|20260630',at='2026-10-20T09:00:00Z';
  const old={...copy(SEED),ack_tombstones:{[key]:at},_local:{dirty:true,revision:4,maintenancePending:true,maintenanceOnly:true}};
  let b=await boot({local:old});
  assert.equal(b.state().dirty,false);assert.equal(b.state().maintenanceOnly,false);assert.equal(b.state().maintenancePending,false);
  assert.equal(b.state().revision,4);b.respond(b.requests[0],meta({...SEED,cash:888}));await settle();await b.tick(90000);
  assert.equal(b.state().DATA.cash,888);assert.equal(b.puts().length,0);
  let mixed={...old,ack_tombstones:{[key]:at,'AAA|loss10':at}},c=await boot({local:mixed});
  assert.equal(c.state().dirty,false);assert.equal(c.state().maintenanceOnly,true);assert.equal(c.state().maintenancePending,true);
  assert.ok(c.state().DATA.ack_tombstones['AAA|loss10']);assert.equal(c.state().DATA.ack_tombstones[key],undefined);
  let dirty={...old,_local:{dirty:true,revision:8}},u=await boot({local:dirty});
  assert.equal(u.state().dirty,true);assert.equal(u.state().revision,8);
});
test('退役fund键不会影响加减仓浮亏ACK及普通无异动折叠',async()=>{
  const old='AAA|fund|20260630',at='2026-10-20T09:00:00Z';
  let d=copy(SEED);d.items.AAA.cost=200;
  d.acks={[old]:at,'AAA|add|95':at,'AAA|cut|104':at,'AAA|loss20':at};
  d.ack_tombstones={'BBB|fund|abcd1234':at,'AAA|loss10':at};
  let b=await boot({local:d,rules:{trigger_gap_pct:6,reduce_gap_pct:4},prepareDocument(doc){
    const table=doc.getElementById('s-aaa').closest('table.stk');
    const q=doc.createElement('details');q.className='quiet-list';q.open=true;
    q.innerHTML='<summary>无异动 1 只 · 点击查看</summary><table class="stk"></table>';
    table.parentNode.insertBefore(q,table.nextElementSibling);
    const r=doc.getElementById('s-bbb'),rail=r.nextElementSibling;q.querySelector('table').appendChild(r);q.querySelector('table').appendChild(rail);
    doc.getElementById('signalCounts').innerHTML='<table></table>';
  }});
  const row=b.doc.getElementById('s-aaa'),parent=row.parentNode,q=b.doc.querySelector('details.quiet-list');
  assert.equal(b.rows[0].tr.className,'green');assert.equal(b.rows[0].lossAcked,true);
  for(const key of ['AAA|add|95','AAA|cut|104','AAA|loss20'])assert.ok(b.state().DATA.acks[key]);
  assert.ok(b.state().DATA.ack_tombstones['AAA|loss10']);assert.equal(b.state().DATA.acks[old],undefined);
  b.ack(old);b.paint();
  assert.equal(row.parentNode,parent);assert.equal(q.open,true);assert.equal(q.querySelector('summary').textContent,'无异动 0 只 · 点击查看');
  assert.equal(q.hidden,true);assert.equal(b.doc.getElementById('s-bbb').closest('.quiet-list'),null,'有效价格提醒必须即时移出无异动');
  assert.equal(b.doc.querySelectorAll('.fund-ack-list').length,0);
  assert.equal(b.doc.querySelector('.signal-filter-toggle[data-kind="fund"]'),null);
  assert.ok(b.doc.getElementById('signal-members-acked-add_price'));
  assert.ok(b.doc.getElementById('signal-members-acked-reduce_price'));
  assert.equal(b.state().dirty,false);assert.equal(b.state().maintenancePending,false);
  assert.doesNotMatch(SOURCE,/FUND_ROWS|FUND_BUCKETS|refreshFundUI|fundStatBase|moveFundRow|data-fund-|基本面|mm:fund-ack-change/);
  assert.doesNotMatch(CSS,/fund[_-]|基本面/);
});
test('退役基本面不影响ETF标签和财报日期元素',async()=>{
  let b=await boot({prepareDocument(doc){
    const row=doc.getElementById('s-aaa'),sd=row.querySelector('td.sym');
    const tag=doc.createElement('span');tag.className='tag';tag.textContent='ETF';sd.appendChild(tag);
    const date=doc.createElement('span');date.className='earn urgent';date.textContent='10/21 美东盘后 · 北京10/22 04:00';row.children[1].appendChild(date);
  }});
  assert.equal(b.rows[0].isEtf,true);
  assert.equal(b.doc.querySelector('#costMembers li[data-sym="AAA"] .tag').textContent,'ETF');
  assert.equal(b.doc.querySelector('#s-aaa .earn.urgent').textContent,'10/21 美东盘后 · 北京10/22 04:00');
  b.paint();assert.ok(b.doc.querySelector('#s-aaa .earn.urgent'));
});
test('导入与新版本正常保存不再带出退役fund键',async()=>{
  const old='AAA|fund|20260630',at='2026-10-20T09:00:00Z';
  let b=await boot();b.respond(b.requests[0],meta(SEED));await settle();
  let d={...copy(SEED),cash:77,acks:{[old]:true},ack_tombstones:{'BBB|fund|abcd1234':'旧值'}};
  b.importData(d);await b.tick(0);b.respond(b.requests.at(-1),meta(SEED));await settle();
  const put=b.puts()[0];assert.ok(put);assert.deepEqual(b.payload(put).acks,{});assert.deepEqual(b.payload(put).ack_tombstones,{});
  b.respond(put,{});await settle();assert.equal(b.state().dirty,false);
  b.mergeCloud({...d,acks:{[old]:at}});b.paint();await b.tick(90000);
  assert.equal(b.state().DATA.acks[old],undefined);assert.equal(b.puts().length,1);assert.equal(b.state().dirty,false);
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
test('移出再移回后的亏损及减仓重新生效，仅保留仍有效的加仓ACK',async()=>{
  let d=copy(SEED);d.items.AAA.cost=200;d.acks={'AAA|loss20':'2026-10-19T00:00:00Z','AAA|add|95':'2026-10-19T00:00:00Z','AAA|cut|104':'2026-10-19T00:00:00Z'};
  let b=await boot({local:d,rules:{trigger_gap_pct:6,reduce_gap_pct:4},token:false});b.move('AAA','focus');
  assert.equal(b.state().DATA.acks['AAA|loss20'],undefined);assert.ok(b.state().DATA.ack_tombstones['AAA|loss20']);
  assert.ok(b.state().DATA.acks['AAA|add|95']);assert.equal(b.state().DATA.acks['AAA|cut|104'],undefined);
  assert.ok(b.state().DATA.ack_tombstones['AAA|cut|104']);b.move('AAA','positions');
  assert.equal(b.rows[0].lossLv,2);assert.equal(b.rows[0].lossAcked,false);assert.equal(b.rows[0].tr.className,'red');
});
test('非持仓重点组只保留加仓提醒，残留减仓胶囊隐藏且不计灰',async()=>{
  let b=await boot({groups:{AAA:'focus'},rules:{trigger_gap_pct:6,reduce_gap_pct:4}});let r=b.rows.find(r=>r.sym==='AAA');
  assert.equal(r.warnAdd.style.display,'');assert.equal(r.warnCut.style.display,'none');assert.equal(r.warn.style.display,'none');
  assert.equal(r.cutCap.style.display,'none');b.ack('AAA|add|95');assert.equal(r.tr.className,'green');
  b.ack('AAA|cut|104');assert.equal(r.tr.className,'green');assert.equal(b.state().DATA.acks['AAA|cut|104'],undefined);
  assert.deepEqual(copy(b.ackStatEntries('reduce_price')),[]);
  assert.ok(b.doc.getElementById('s-aaa'));assert.equal(b.state().DATA.items.AAA.cost,100);
});
test('非持仓只有减仓胶囊时不提醒，不制造红灯',async()=>{
  let b=await boot({groups:{AAA:'focus'},rules:{trigger_gap_pct:6,reduce_gap_pct:4},prepareDocument:d=>d.querySelector('#s-aaa .lvtag.add').remove()});
  let r=b.rows.find(r=>r.sym==='AAA');assert.equal(r.trig,0);assert.equal(r.warnCut.style.display,'none');assert.equal(r.tr.className,'green');
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
test('缺失52周范围不漏算持仓，重点组仍不报减仓提醒',async()=>{
  let b=await boot({groups:{AAA:'focus'},rules:{reduce_gap_pct:4},prepareDocument:d=>{d.getElementById('s-aaa').nextElementSibling.remove();d.getElementById('s-bbb').nextElementSibling.remove();}});
  assert.equal(b.doc.querySelector('.cost-sum>b').textContent,'$250');assert.equal(b.rows.find(r=>r.sym==='AAA').warnCut.style.display,'none');
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
for(const state of ['low','space','high','hit','bad'])test('目标'+state+'状态在浮亏叠加及已读后保持独立',async()=>{
  const d=copy(SEED);d.items.AAA.cost=200;
  const b=await boot({local:d,token:false,prepareDocument:doc=>targetDocument(doc,state)});
  const row=b.doc.getElementById('s-aaa'),original=b.rows[0].origCls.split(/\s+/).filter(c=>c!=='green');
  assert.ok(row.className.split(/\s+/).includes('red'));
  for(const cls of original)assert.ok(row.className.split(/\s+/).includes(cls));
  const capsule=row.querySelector('.lvtag.tgt');assert.equal(capsule.className,'lvtag tgt '+state);
  b.ack('AAA|loss20');b.paint();
  assert.ok(row.className.split(/\s+/).includes('green'));
  for(const cls of original)assert.ok(row.className.split(/\s+/).includes(cls));
  assert.equal(capsule.className,'lvtag tgt '+state);
  assert.equal(b.doc.getElementById('s-aaa').nextElementSibling.className,'range52-row');
  await b.dispose();
});
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
test('目标条纹移组及移回不被成本浮亏重绘抹掉',async()=>{
  const d=copy(SEED);d.items.AAA.cost=200;
  const b=await boot({local:d,token:false,prepareDocument:doc=>targetDocument(doc,'high')});
  b.move('AAA','focus');
  assert.equal(b.rows[0].lossLv,0);assert.ok(b.rows[0].tr.className.split(/\s+/).includes('pur-high'));
  assert.ok(b.rows[0].tr.className.split(/\s+/).includes('green'));
  b.move('AAA','positions');
  assert.equal(b.rows[0].lossLv,2);assert.ok(b.rows[0].tr.className.split(/\s+/).includes('pur-high'));
  assert.ok(b.rows[0].tr.className.split(/\s+/).includes('red'));
  assert.equal(b.rows[0].tr.querySelector('.lvtag.tgt').className,'lvtag tgt high');
});
test('目标两段计数成员跳转union数据重绘保持不变',async()=>{
  const b=await boot({token:false,rules:{trigger_gap_pct:6,reduce_gap_pct:4},prepareDocument:doc=>targetDocument(doc,'hit')});
  const panel=b.doc.querySelector('.target-purple'),summary=panel.querySelector('summary').textContent;
  const data=b.doc.getElementById('signalCounts').getAttribute('data-filter');panel.open=true;
  b.ack('AAA|add|95');b.ack('AAA|cut|104');b.paint();
  assert.equal(panel.open,true);assert.equal(panel.querySelector('summary').textContent,summary);
  assert.equal(panel.querySelectorAll('ul.signal-member-list').length,2);
  assert.equal(panel.querySelectorAll('a[href="#s-aaa"]').length,2);
  assert.match(panel.textContent,/今日跌破共识价 170/);
  assert.equal(b.doc.getElementById('signalCounts').getAttribute('data-filter'),data);
  assert.equal(JSON.parse(data).target_purple.members.length,1);
  assert.ok(b.doc.querySelector('.signal-filter-toggle[data-kind="target_purple"]'));
  assert.equal(b.doc.querySelector('.signal-filter-toggle[data-kind="target"]'),null);
});
test('目标高空间与强红信号叠加已读不撤其它红灯',async()=>{
  const d=copy(SEED);d.items.AAA.cost=200;
  const b=await boot({local:d,token:false,rules:{trigger_gap_pct:6,reduce_gap_pct:4},
    prepareDocument:doc=>targetDocument(doc,'high',{level:'red'})});
  b.ack('AAA|loss20');b.ack('AAA|add|95');b.ack('AAA|cut|104');b.paint();
  const row=b.doc.getElementById('s-aaa');
  assert.ok(row.className.split(/\s+/).includes('red'));
  assert.ok(row.className.split(/\s+/).includes('pur-high'));
  assert.equal(row.querySelector('.lvtag.tgt').className,'lvtag tgt high');
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
  assert.equal(result.members.length,1);assert.match(result.members[0].details[0],/高空间.*跌破/);
  assert.deepEqual(data,before);
  const pending=context.intersectSignals({...data,target_purple:{...data.target_purple,ok:false}},['target_purple','chg']);
  assert.deepEqual(copy(pending.pending),['空间/达到']);
  assert.deepEqual(copy(context.intersectSignals(data,['target_purple','earnings_week']).members.map(m=>m.id)),['BOTH','HIT']);
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
  target_purple:{label:'空间/达到',ok:true,members:[{id:'AAA',s:'AAA',d:['高空间 / 今日跌破共识价 170']},{id:'CCC',s:'CCC',d:['今日上穿共识价 250']}]},
  earnings_week:{label:'财报',ok:false,members:[]}
};
function lossStatDocument(doc,opts={}){
  const old=doc.getElementById('signalCounts'),card=doc.createElement('details');
  card.id='signalCounts';card.className='card group-card';card.open=true;
  card.innerHTML='<summary><span class="grp-name">统计</span><span class="group-stats"><span class="stat-badge red">99</span><span class="stat-badge yellow">99</span><span class="stat-badge purple">7</span></span></summary>'
    +'<table><tr class="gray mh"><td>信号</td><td class="num">红</td><td class="num">黄</td><td class="num">灰</td></tr>'
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
function lossFilter(b){return JSON.parse(b.doc.getElementById('signalFilterResult').getAttribute('data-filter'));}
function lossToggle(b){return b.doc.querySelector('.signal-filter-toggle[data-kind="cost_loss"]');}
function assertLossStats(b,red,gray,ids,{filter=LOSS_FILTER_BASE,cardFilter=JSON.stringify({sentinel:'不要更新这个属性'})}={}){
  const toggle=lossToggle(b),count=red+gray;
  if(!toggle){assert.equal(count,0);}else{
    const row=toggle.closest('tr');assert.equal(row.style.display,count?'':'none');assert.equal(row.nextElementSibling.style.display,count?'':'none');
    for(const [index,n] of [[1,red],[3,gray]]){
      const cell=row.children[index],button=cell.querySelector('.signal-count-toggle');
      assert.equal(button?Number(button.textContent):0,n);if(!n)assert.equal(cell.textContent,'—');
    }
    assert.equal(row.children[2].textContent,'—','浮亏10/20%均为红，不记黄');
    assert.equal(b.doc.querySelectorAll('.signal-filter-toggle[data-kind="cost_loss"]').length,1,'重绘不能重复追加统计行');
  }
  const data=lossFilter(b);assert.equal(data.cost_loss.ok,true);assert.equal(data.cost_loss.label,'成本浮亏');
  assert.deepEqual(data.cost_loss.members.map(m=>m.id),ids);
  assert.equal(new Set(ids).size,ids.length,'筛选成员按身份去重');
  for(const kind of Object.keys(filter))assert.deepEqual(data[kind],filter[kind],'保持其它筛选数据 '+kind);
  assert.equal(b.doc.getElementById('signalCounts').getAttribute('data-filter'),cardFilter);
}
function assertLossBadges(b,red,yellow){
  for(const [level,n,label] of [['red',red,'红'],['yellow',yellow,'黄']]){
    const badge=b.doc.querySelector('#signalCounts summary .stat-badge.'+level);
    assert.equal(badge.textContent,String(n));assert.equal(badge.getAttribute('aria-label'),label+' '+n+' 项');
  }
  assert.equal(b.doc.querySelector('#signalCounts summary .stat-badge.purple').textContent,'7');
}
function ackCurrentLoss(b,sym='AAA'){
  const row=b.rows.find(r=>r.sym===sym),button=row.warn.querySelector('button[data-ack]');
  assert.ok(button,'真实浮亏提醒必须有已读按钮');
  b.doc.fire('click',{target:button});
}
function setLossPrice(b,sym,price){
  // 行情在启动时被真实脚本扫描为行对象；模拟下一轮价格，不复制浮亏判断公式。
  for(const row of b.rows.filter(r=>r.sym===sym)){row.price=price;row.tr.querySelector('.px-price').textContent=String(price);}
  b.paint();
}
function lossGrayDocument(doc,symbols,count=symbols?.length??0){
  const badge=doc.createElement('span');badge.className='stat-badge gray';badge.textContent=String(count);
  doc.querySelector('#signalCounts summary .group-stats').appendChild(badge);
  if(symbols!==null){
    const snapshot=doc.createElement('script');snapshot.id='snapshotData';
    snapshot.setAttribute('type','application/json');snapshot.textContent=JSON.stringify({text_only:symbols.map(symbol=>({symbol}))});
    doc.body.appendChild(snapshot);
  }
}
function assertLossGrayBadge(b,n){
  const badges=b.doc.querySelectorAll('#signalCounts summary .stat-badge.gray');assert.equal(badges.length,1,'灰标题不能重复追加');
  assert.equal(badges[0].parentNode,b.doc.querySelector('#signalCounts summary .group-stats'));
  assert.equal(badges[0].textContent,String(n));assert.equal(badges[0].getAttribute('aria-label'),'灰 '+n+' 项');
}
function realIntersectSignals(){
  const assets=fs.readFileSync(path.join(__dirname,'..','page_assets.py'),'utf8');
  const start=assets.indexOf('function intersectSignals('),end=assets.indexOf('function renderSignalFilter()',start);
  assert.ok(start>=0&&end>start,'真实页面必须提供交集函数');
  const context={};vm.createContext(context);vm.runInContext(assets.slice(start,end),context);
  return context.intersectSignals;
}

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
test('成本浮亏启动没有signalCounts仍补details骨架、统计表和filter',async()=>{
  const b=await bootLoss({prices:{AAA:80},prepareDocument(doc){
    doc.getElementById('signalCounts').remove();
    assert.equal(doc.getElementById('signalCounts'),null);assert.equal(doc.getElementById('signalFilterResult'),null);
  }});
  const card=b.doc.getElementById('signalCounts'),bottom=card.querySelector('.group-bottom'),table=card.querySelector('table'),box=b.doc.getElementById('signalFilterResult');
  assert.equal(card.tagName,'DETAILS');assert.equal(card.open,true);assert.equal(card.className,'card group-card');
  assert.equal(card.querySelector('.grp-name').textContent,'统计');assert.equal(bottom.parentNode,card);
  assert.equal(bottom.querySelector('.collapse-group').getAttribute('data-target'),'signalCounts');
  assert.equal(b.doc.getElementById('costManage').nextElementSibling,card,'仓位管理与补出的统计卡同级且位于其上方');
  assert.equal(table.parentNode,card);assert.equal(box.parentNode,card);assert.equal(box.nextElementSibling,bottom);
  assert.ok(card.children.indexOf(table)<card.children.indexOf(box));assert.equal(table.querySelector('.mh').children.length,4);
  assert.equal(box.className,'signal-filter');assert.equal(box.getAttribute('role'),'status');assert.equal(box.hidden,true);
  assertLossStats(b,1,0,['AAA'],{filter:{},cardFilter:null});assert.deepEqual(Object.keys(lossFilter(b)),['cost_loss']);
  ackCurrentLoss(b);assertLossStats(b,0,1,['AAA'],{filter:{},cardFilter:null});assertLossGrayBadge(b,1);
  for(let i=0;i<3;i++)b.paint();
  assert.equal(b.doc.querySelectorAll('#signalCounts').length,1);assert.equal(card.querySelectorAll('table').length,1);
  assert.equal(b.doc.querySelectorAll('#signalFilterResult').length,1);assert.equal(b.requests.length,0);assert.equal(b.store.writes.length,0);
});
for(const kinds of [['target_purple'],['earnings_week'],['target_purple','earnings_week']])test('成本浮亏只有'+kinds.join('/')+'卡且无table，插入group-bottom前不抛NotFoundError',async()=>{
  let originalBox,originalBottom,originalButton,extras;
  const keepBox=kinds.length===2;
  const b=await bootLoss({prepareDocument(doc){
    const card=doc.getElementById('signalCounts');card.querySelector('table').remove();card.querySelector('.purple-extra').remove();
    if(!keepBox)doc.getElementById('signalFilterResult').remove();
    extras=kinds.map(kind=>{
      const extra=doc.createElement('div');extra.className='signal-extra-row';
      extra.innerHTML='<button class="signal-filter-toggle" data-kind="'+kind+'" aria-pressed="false">'+kind+'</button>';
      card.insertBefore(extra,card.querySelector('.group-bottom'));return extra;
    });
    originalBox=doc.getElementById('signalFilterResult');originalBottom=card.querySelector('.group-bottom');originalButton=originalBottom.querySelector('.collapse-group');
    assert.equal(card.querySelector('table'),null);assert.equal(originalButton.parentNode,originalBottom);assert.notEqual(originalButton.parentNode,card);
  }});
  const card=b.doc.getElementById('signalCounts'),table=card.querySelector('table'),box=b.doc.getElementById('signalFilterResult');
  assert.equal(table.parentNode,card);assert.equal(box.parentNode,card);
  assert.ok(card.children.indexOf(table)<card.children.indexOf(originalBottom));assert.ok(card.children.indexOf(box)<card.children.indexOf(originalBottom));
  assert.equal(originalBottom.parentNode,card);assert.equal(originalButton.parentNode,originalBottom);
  if(keepBox)assert.equal(box,originalBox);else assert.equal(box.nextElementSibling,originalBottom);
  for(const extra of extras){assert.equal(extra.parentNode,card);assert.equal(extra.querySelector('.signal-filter-toggle').getAttribute('aria-pressed'),'false');}
  const options={filter:keepBox?LOSS_FILTER_BASE:{}};
  assertLossStats(b,1,0,['AAA'],options);ackCurrentLoss(b);assertLossStats(b,0,1,['AAA'],options);
  for(let i=0;i<3;i++)b.paint();
  assert.equal(card.querySelectorAll('table').length,1);assert.equal(b.doc.querySelectorAll('#signalFilterResult').length,1);
  assert.equal(card.querySelectorAll('.collapse-group').length,1);assert.equal(b.requests.length,0);assert.equal(b.store.writes.length,0);
});
test('成本浮亏snapshot文字灰AAA/BBB按最终灯色移出和回灰，已读合并不重复',async()=>{
  const b=await bootLoss({symbols:['AAA','BBB','CCC'],prices:{AAA:80,BBB:100,CCC:100},prepareDocument:doc=>lossGrayDocument(doc,['AAA','BBB'])});
  const snapshot=b.doc.getElementById('snapshotData').textContent;
  assert.equal(b.rows.find(r=>r.sym==='BBB').tr.className,'green');assert.equal(b.rows.find(r=>r.sym==='CCC').tr.className,'green');
  assertLossStats(b,1,0,['AAA']);assertLossBadges(b,1,0);assertLossGrayBadge(b,1);
  ackCurrentLoss(b);assertLossStats(b,0,1,['AAA']);assertLossBadges(b,0,0);assertLossGrayBadge(b,2);
  assert.equal(b.rows[0].tr.className,'green');
  for(let i=0;i<4;i++){b.paint();assertLossGrayBadge(b,2);assertLossStats(b,0,1,['AAA']);}
  ackCurrentLoss(b);assertLossGrayBadge(b,1);assertLossStats(b,1,0,['AAA']);
  setLossPrice(b,'AAA',100);assertLossGrayBadge(b,2);assertLossStats(b,0,0,[]);
  setLossPrice(b,'AAA',80);assertLossGrayBadge(b,1);ackCurrentLoss(b);assertLossGrayBadge(b,2);
  assert.equal(b.doc.getElementById('snapshotData').textContent,snapshot,'不能改写原始text_only');
  assert.equal(b.doc.getElementById('signal-members-acked-cost_loss').querySelectorAll('li').length,1);
  assert.equal(b.requests.length,0);assert.equal(b.store.writes.length,0);
});
test('成本浮亏技术原红和原黄已读仍计灰总，红黄标题保留技术灯',async()=>{
  const b=await bootLoss({local:{...copy(SEED),items:{AAA:{cost:100,qty:5},BBB:{cost:100,qty:2}}},prices:{AAA:80,BBB:90},levels:{AAA:'red pur-high',BBB:'yellow'},
    prepareDocument:doc=>lossGrayDocument(doc,[])});
  assertLossBadges(b,2,0);assertLossGrayBadge(b,0);
  ackCurrentLoss(b);assertLossStats(b,1,1,['AAA','BBB']);assertLossBadges(b,2,0);assertLossGrayBadge(b,1);
  assert.ok(b.rows[0].tr.className.split(/\s+/).includes('red'),'技术红不能被浮亏已读消除');
  assert.equal(lossFilter(b).cost_loss.members.find(m=>m.id==='AAA').l,'gray');
  ackCurrentLoss(b,'BBB');assertLossStats(b,0,2,['AAA','BBB']);assertLossBadges(b,1,1);assertLossGrayBadge(b,2);
  for(let i=0;i<4;i++){b.paint();assertLossGrayBadge(b,2);assertLossBadges(b,1,1);}
  ackCurrentLoss(b);assertLossGrayBadge(b,1);assertLossBadges(b,1,1);
});
test('成本浮亏没有snapshot时保留灰原标题并合并已读，重复paint不累加',async()=>{
  const b=await bootLoss({prepareDocument:doc=>lossGrayDocument(doc,null,4)});
  assert.equal(b.doc.getElementById('snapshotData'),null);assertLossGrayBadge(b,4);
  ackCurrentLoss(b);assertLossStats(b,0,1,['AAA']);assertLossGrayBadge(b,5);
  for(let i=0;i<4;i++){b.paint();assertLossGrayBadge(b,5);}
  ackCurrentLoss(b);assertLossStats(b,1,0,['AAA']);assertLossGrayBadge(b,4);
  ackCurrentLoss(b);assertLossGrayBadge(b,5);setLossPrice(b,'AAA',100);assertLossStats(b,0,0,[]);assertLossGrayBadge(b,4);
  assert.equal(b.requests.length,0);assert.equal(b.store.writes.length,0);
});
test('成本浮亏mm:ack-change同步回调已看到红黄灰标题、统计行和filter最终状态',async()=>{
  const b=await bootLoss({levels:{AAA:'yellow'},prepareDocument:doc=>lossGrayDocument(doc,['BBB'])}),seen=[];
  b.ctx.addEventListener('mm:ack-change',event=>{
    const badges={};
    for(const level of ['red','yellow','gray']){
      const badge=b.doc.querySelector('#signalCounts summary .stat-badge.'+level);
      badges[level]={text:badge.textContent,label:badge.getAttribute('aria-label')};
    }
    const row=lossToggle(b).closest('tr');
    seen.push({type:event.type,badges,data:lossFilter(b),counts:[row.children[1].textContent,row.children[3].textContent],display:row.style.display});
  });
  const expected=(red,yellow,gray,members,counts,display='')=>({type:'mm:ack-change',badges:{
    red:{text:String(red),label:'红 '+red+' 项'},yellow:{text:String(yellow),label:'黄 '+yellow+' 项'},gray:{text:String(gray),label:'灰 '+gray+' 项'}},
    data:{...copy(LOSS_FILTER_BASE),cost_loss:{label:'成本浮亏',ok:true,members}},counts,display});
  const member={id:'AAA',s:'AAA',n:'测试名称',g:'短持仓',l:'gray',d:['浮亏 10.0%（已达 10%） · 已读']};
  assertLossBadges(b,1,0);assertLossGrayBadge(b,1);
  ackCurrentLoss(b);assert.deepEqual(seen,[expected(0,1,2,[member],['—','1'])],'事件不能先于红黄灰标题刷新');
  b.paint();assert.equal(seen.length,1,'无变化不重复派发');
  ackCurrentLoss(b);assert.equal(seen.length,2);
  assert.deepEqual(seen[1],expected(1,0,1,[{...member,l:'red',d:['浮亏 10.0%（已达 10%）']}],['1','—']));
  setLossPrice(b,'AAA',100);assert.equal(seen.length,3);assert.deepEqual(seen[2],expected(0,1,1,[],['—','—'],'none'));
  b.paint();assert.equal(seen.length,3);assert.equal(b.requests.length,0);assert.equal(b.store.writes.length,0);
});

for(const [price,lv] of [[100,0],[90.01,0],[90,1],[89.99,1],[80.01,1],[80,2],[79.99,2],[75,2]])test('成本浮亏相等边界与相邻值 price'+price+'/cost100',async()=>{
  const b=await bootLoss({prices:{AAA:price}}),row=b.rows.find(r=>r.sym==='AAA');
  assert.equal(row.cost,100,'不能依赖默认SEED误构造边界');assert.equal(row.lossLv,lv);
  assertLossStats(b,lv?1:0,0,lv?['AAA']:[]);assertLossBadges(b,lv?1:0,0);
  if(lv){
    const member=lossFilter(b).cost_loss.members[0];assert.equal(member.l,'red');
    assert.match(member.d[0],new RegExp('已达 '+(lv===2?20:10)+'%'));
    const panel=b.doc.getElementById('signal-members-cost_loss-red');
    assert.equal(panel.querySelectorAll('li').length,1,'20%不能再叠加计一次10%');
    assert.equal(panel.querySelector('a').getAttribute('href'),'#s-aaa');
    assert.equal(panel.querySelector('.signal-member-symbol').textContent,'AAA');
    assert.equal(panel.querySelector('.note').textContent,'测试名称');
    assert.equal(panel.querySelector('.signal-member-belong').textContent,'（短持仓）');
    assert.equal(panel.querySelector('.signal-member-metric').textContent,member.d[0]);
  }
  for(let i=0;i<3;i++)b.paint();assertLossStats(b,lv?1:0,0,lv?['AAA']:[]);
  assert.equal(b.requests.length,0);assert.equal(b.store.writes.length,0);
});
for(const [price,key] of [[90,'loss10'],[80,'loss20']])test('成本浮亏'+key+'已读转灰、撤销回红并刷新事件',async()=>{
  const b=await bootLoss({prices:{AAA:price}}),seen=[];
  b.ctx.addEventListener('mm:ack-change',event=>seen.push({event,data:lossFilter(b)}));
  assert.equal(b.dispatched.length,1,'首次paint更新真实筛选数据并派发事件');
  b.paint();assert.equal(b.dispatched.length,1,'未改变的筛选数据不重复派发');
  ackCurrentLoss(b);assertLossStats(b,0,1,['AAA']);assertLossBadges(b,0,0);
  assert.ok(b.state().DATA.acks['AAA|'+key]);assert.equal(b.rows[0].lossAcked,true);assert.equal(b.rows[0].tr.className,'green');
  const gray=b.doc.getElementById('signal-members-acked-cost_loss');
  assert.equal(gray.querySelectorAll('li').length,1);
  assert.equal(gray.querySelector('a').getAttribute('href'),'#s-aaa');
  for(const text of ['AAA','测试名称','短持仓','浮亏'])assert.ok(gray.textContent.includes(text),'灰名单包含 '+text);
  assert.equal(lossFilter(b).cost_loss.members[0].l,'gray');assert.match(lossFilter(b).cost_loss.members[0].d[0],/已读/);
  assert.equal(b.doc.getElementById('signal-members-cost_loss-red').hidden,true);
  assert.equal(seen.length,1);assert.ok(seen[0].event instanceof b.ctx.CustomEvent);assert.equal(seen[0].event.type,'mm:ack-change');
  assert.equal(seen[0].data.cost_loss.members[0].l,'gray','事件到达前已写入筛选数据');
  ackCurrentLoss(b);assertLossStats(b,1,0,['AAA']);assertLossBadges(b,1,0);
  assert.equal(b.state().DATA.acks['AAA|'+key],undefined);assert.ok(b.state().DATA.ack_tombstones['AAA|'+key]);
  assert.equal(b.rows[0].tr.className,'red');assert.equal(seen.length,2);assert.equal(seen[1].data.cost_loss.members[0].l,'red');
  const before=b.dispatched.length;b.paint();assert.equal(b.dispatched.length,before);
});
test('成本浮亏10%已读升级20%需重新确认，两条ack只记当前档一次',async()=>{
  const b=await bootLoss();ackCurrentLoss(b);assertLossStats(b,0,1,['AAA']);
  setLossPrice(b,'AAA',80);assert.equal(b.rows[0].lossLv,2);assertLossStats(b,1,0,['AAA']);assertLossBadges(b,1,0);
  assert.ok(b.state().DATA.acks['AAA|loss10']);assert.equal(b.rows[0].lossAcked,false);
  assert.equal(b.rows[0].warn.querySelector('button').getAttribute('data-ack'),'AAA|loss20');
  ackCurrentLoss(b);assert.ok(b.state().DATA.acks['AAA|loss10']);assert.ok(b.state().DATA.acks['AAA|loss20']);
  assertLossStats(b,0,1,['AAA']);assertLossBadges(b,0,0);
  const panels=b.doc.querySelectorAll('.signal-members-panel').filter(p=>p.id==='signal-members-acked-cost_loss');
  assert.equal(panels.length,1,'灰名单清空再生成必须复用或移除旧面板，不能留下重复ID');
  const panel=panels[0];assert.equal(panel.querySelectorAll('li').length,1);assert.match(panel.textContent,/已达 20%/);
  setLossPrice(b,'AAA',90);assertLossStats(b,0,1,['AAA']);assert.equal(b.state().DATA.acks['AAA|loss20'],undefined);
  assert.ok(b.state().DATA.ack_tombstones['AAA|loss20']);assert.match(lossFilter(b).cost_loss.members[0].d[0],/已达 10%.*已读/);
});
test('成本浮亏20%降到10%不能沿用20%已读，离开10%后整行隐藏取消筛选',async()=>{
  const b=await bootLoss({prices:{AAA:80}});ackCurrentLoss(b);assertLossStats(b,0,1,['AAA']);
  setLossPrice(b,'AAA',90);assertLossStats(b,1,0,['AAA']);assertLossBadges(b,1,0);
  assert.equal(b.rows[0].lossLv,1);assert.equal(b.rows[0].lossAcked,false);assert.equal(b.state().DATA.acks['AAA|loss20'],undefined);
  ackCurrentLoss(b);assertLossStats(b,0,1,['AAA']);
  const toggle=lossToggle(b);toggle.setAttribute('aria-pressed','true');
  const red=b.doc.getElementById('signal-members-cost_loss-red');
  const grayPanels=b.doc.querySelectorAll('.signal-members-panel').filter(p=>p.id==='signal-members-acked-cost_loss');
  const gray=grayPanels.at(-1);gray.hidden=false;const n=b.dispatched.length;
  setLossPrice(b,'AAA',90.01);assertLossStats(b,0,0,[]);assertLossBadges(b,0,0);
  assert.equal(toggle.getAttribute('aria-pressed'),'false');assert.equal(red.hidden,true);assert.equal(gray.hidden,true);
  assert.equal(b.state().DATA.acks['AAA|loss10'],undefined);assert.ok(b.state().DATA.ack_tombstones['AAA|loss10']);
  assert.equal(b.dispatched.length,n+1);assert.equal(b.rows[0].tr.className,'green');
  setLossPrice(b,'AAA',90);assertLossStats(b,1,0,['AAA']);assert.equal(b.rows[0].lossAcked,false);
  const panels=b.doc.querySelectorAll('.signal-members-panel').filter(p=>p.id==='signal-members-acked-cost_loss');
  assert.equal(panels.length,1,'降档重新确认时不能遗留同ID灰名单');
});
for(const field of ['qty','cost'])test('成本浮亏'+field+'归零清空统计和已读，恢复后重新报红',async()=>{
  const b=await bootLoss({prices:{AAA:80}});ackCurrentLoss(b);await b.enter();
  b.edit('AAA',field,'0');await b.tick(16);assertLossStats(b,0,0,[]);assertLossBadges(b,0,0);
  assert.equal(b.rows[0].lossLv,0);assert.equal(b.state().DATA.acks['AAA|loss20'],undefined);assert.equal(b.rows[0].warn.style.display,'none');
  b.edit('AAA',field,field==='qty'?'5':'100');await b.tick(16);assertLossStats(b,1,0,['AAA']);assertLossBadges(b,1,0);
  assert.equal(b.rows[0].lossAcked,false);assert.equal(b.requests.length,0);assert.equal(b.store.writes.length,0);
});
test('成本浮亏移组隐藏、移回恢复，短长归属按当前DOM刷新',async()=>{
  const b=await bootLoss();ackCurrentLoss(b);lossToggle(b).setAttribute('aria-pressed','true');
  b.move('AAA','focus');assertLossStats(b,0,0,[]);assertLossBadges(b,0,0);
  assert.equal(lossToggle(b).getAttribute('aria-pressed'),'false');assert.equal(b.state().DATA.acks['AAA|loss10'],undefined);
  b.move('AAA','positions');assertLossStats(b,1,0,['AAA']);assertLossBadges(b,1,0);
  assert.equal(lossFilter(b).cost_loss.members[0].g,'短持仓');
  const sub=b.doc.createElement('details');sub.id='group_positions_long';sub.innerHTML='<table class="stk"></table>';b.doc.getElementById('group_positions').appendChild(sub);
  b.move('AAA','positions_long');assertLossStats(b,1,0,['AAA']);assert.equal(lossFilter(b).cost_loss.members[0].g,'长持仓');
  assert.equal(b.doc.getElementById('signal-members-cost_loss-red').querySelector('.signal-member-belong').textContent,'（长持仓）');
  b.move('AAA','positions');assert.equal(lossFilter(b).cost_loss.members[0].g,'短持仓');assert.equal(b.state().DATA.items.AAA.cost,100);
});
test('成本浮亏从页面移除不计残留ROW和DATA，已读名单也立即清空',async()=>{
  const b=await bootLoss({local:{...copy(SEED),items:{AAA:{cost:100,qty:5},BBB:{cost:100,qty:2}}},prices:{AAA:80,BBB:90}});
  ackCurrentLoss(b);assertLossStats(b,1,1,['AAA','BBB']);
  b.move('AAA',null);assertLossStats(b,1,0,['BBB']);assertLossBadges(b,1,0);
  assert.equal(b.doc.getElementById('s-aaa'),null);assert.equal(b.state().DATA.items.AAA.qty,5);
  b.move('BBB',null);assertLossStats(b,0,0,[]);assertLossBadges(b,0,0);
});
test('成本浮亏多人、重复身份与双档ack去重，长短名单展示真实亏损百分比',async()=>{
  const d={...copy(SEED),items:{AAA:{cost:100,qty:5},BBB:{cost:100,qty:2},CCC:{cost:100,qty:3}},
    acks:{'AAA|loss10':'2026-10-19T00:00:00Z','AAA|loss20':'2026-10-19T01:00:00Z'}};
  const b=await bootLoss({symbols:['AAA','BBB','CCC'],local:d,prices:{AAA:75,BBB:85,CCC:80},longs:['BBB'],notes:{BBB:'长线备注'},
    prepareDocument(doc){
      const row=doc.getElementById('s-ccc'),duplicate=doc.createElement('tr');duplicate.id='s-ccc';duplicate.className='green';
      duplicate.innerHTML='<td class="sym">CCC<span class="note">测试名称</span></td><td><span class="px-price">80</span><span class="px-chg">+1.00%</span></td><td class="sig"></td>';
      const rail=doc.createElement('tr');rail.className='range52-row';
      rail.innerHTML='<td><span class="range52-rail" aria-label="50.00低，本轮价 80.00 位于区间 30%，本轮高 150.00"><span class="range52-track"></span></span></td>';
      row.parentNode.appendChild(duplicate);row.parentNode.appendChild(rail);
    }});
  assert.equal(b.rows.filter(r=>r.sym==='CCC').length,2,'确实构造重复行情身份');
  assertLossStats(b,2,1,['BBB','AAA','CCC']);assertLossBadges(b,2,0);
  const red=b.doc.getElementById('signal-members-cost_loss-red'),gray=b.doc.getElementById('signal-members-acked-cost_loss');
  assert.equal(red.querySelectorAll('li').length,2);assert.equal(gray.querySelectorAll('li').length,1);
  assert.match(red.textContent,/BBB.*长线备注.*长持仓.*浮亏 15\.0%.*已达 10%/);
  assert.match(red.textContent,/CCC.*短持仓.*浮亏 20\.0%.*已达 20%/);
  assert.match(gray.textContent,/AAA.*短持仓.*浮亏 25\.0%.*已达 20%/);
  for(let i=0;i<4;i++)b.paint();assertLossStats(b,2,1,['BBB','AAA','CCC']);
  ackCurrentLoss(b,'CCC');assertLossStats(b,1,2,['BBB','AAA','CCC']);assertLossBadges(b,1,0);
});
test('成本浮亏技术原红不重复计数，已读仍红；原黄被浮亏提升后恢复黄色',async()=>{
  const b=await bootLoss({local:{...copy(SEED),items:{AAA:{cost:100,qty:5},BBB:{cost:100,qty:2}}},prices:{AAA:80,BBB:90},levels:{AAA:'red pur-high',BBB:'yellow'}});
  assertLossStats(b,2,0,['AAA','BBB']);assertLossBadges(b,2,0);
  ackCurrentLoss(b);assertLossStats(b,1,1,['AAA','BBB']);assertLossBadges(b,2,0);
  assert.ok(b.rows[0].tr.className.split(/\s+/).includes('red'));assert.ok(b.rows[0].tr.className.split(/\s+/).includes('pur-high'));
  ackCurrentLoss(b,'BBB');assertLossStats(b,0,2,['AAA','BBB']);assertLossBadges(b,1,1);
  assert.equal(b.rows[1].tr.className,'yellow');
  b.move('AAA',null);assertLossStats(b,0,1,['BBB']);assertLossBadges(b,0,1);
});
test('成本浮亏灰数只取当前命中，不读历史失效和移除标的ack',async()=>{
  const at='2026-10-19T00:00:00Z',d={...copy(SEED),acks:{'AAA|loss10':at,'AAA|loss20':at,'BBB|loss10':at,'REMOVED|loss20':at}};
  const store=shared({[LOCAL]:d,[TOKEN]:'test-token'});store.holder=true;
  const b=await boot({store,forbidRequests:true,prepareDocument:doc=>lossStatDocument(doc,{prices:{AAA:90,BBB:100}})});
  assert.equal(b.state().writer,false,'只读标签页不执行prune，历史ACK确实留在内存');
  assert.equal(Object.keys(b.state().DATA.acks).length,4);
  assertLossStats(b,0,1,['AAA']);assertLossBadges(b,0,0);
  assert.equal(b.doc.getElementById('signal-members-acked-cost_loss').querySelectorAll('li').length,1);
  setLossPrice(b,'AAA',100);assert.equal(Object.keys(b.state().DATA.acks).length,4);assertLossStats(b,0,0,[]);
  assert.equal(b.requests.length,0);await b.dispose();
});
test('成本浮亏与现有chg/target筛选执行真实intersectSignals，已读仍参与交集',async()=>{
  const b=await bootLoss({local:{...copy(SEED),items:{AAA:{cost:100,qty:5},BBB:{cost:100,qty:2},CCC:{cost:100,qty:1}}},symbols:['AAA','BBB','CCC'],prices:{AAA:80,BBB:90,CCC:85}});
  const intersect=realIntersectSignals();
  function ids(kinds){return copy(intersect(lossFilter(b),kinds).members.map(m=>m.id));}
  assert.deepEqual(ids(['cost_loss','chg']),['AAA','BBB']);assert.deepEqual(ids(['cost_loss','target_purple']),['AAA','CCC']);
  assert.deepEqual(ids(['cost_loss','chg','target_purple']),['AAA']);
  ackCurrentLoss(b);const data=lossFilter(b),before=copy(data),result=intersect(data,['cost_loss','chg','target_purple']);
  assert.deepEqual(copy(result.members.map(m=>m.id)),['AAA']);assert.equal(result.members[0].l,'gray');
  assert.deepEqual(copy(result.members[0].details),['浮亏 20.0%（已达 20%） · 已读','涨跌幅 -5.0%','高空间 / 今日跌破共识价 170']);
  assert.deepEqual(data,before,'真实交集不改写原始筛选数据');
  assert.deepEqual(copy(intersect(data,['cost_loss','earnings_week']).pending),['财报']);
  b.move('AAA','focus');assert.deepEqual(ids(['cost_loss','chg','target_purple']),[]);
  b.move('AAA','positions');assert.deepEqual(ids(['cost_loss','chg','target_purple']),['AAA']);
});
test('成本浮亏隔离预览编辑已读导入重试不发请求，不写共享存储',async()=>{
  const b=await bootLoss(),original=new Map(b.store.values);
  ackCurrentLoss(b);await b.enter();b.edit('AAA','cost','120');await b.save();
  assertLossStats(b,1,0,['AAA']);assert.equal(b.rows[0].lossLv,2);
  b.importData({...copy(SEED),items:{AAA:{cost:120,qty:5},BBB:{cost:90,qty:2}},cash:77,acks:{'AAA|loss20':'2026-10-20T00:00:00Z'}});
  assertLossStats(b,0,1,['AAA']);b.requestSave(0,true);b.autoPush();b.startPull();await b.tick(90000);
  assert.equal(b.requests.length,0);assert.equal(b.puts().length,0);assert.equal(b.store.writes.length,0);assert.deepEqual(b.store.values,original);
  assert.equal(b.sessionLocal().cash,77);assert.ok(b.session.writes.length>0);
  assert.ok(b.dispatched.some(e=>e.type==='mm:ack-change'));
  await b.dispose();const c=await bootLoss({store:b.store,session:b.session});assertLossStats(c,0,1,['AAA']);assert.equal(c.requests.length,0);
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
  assert.equal(b.doc.querySelector('.cash-total').textContent,'合计 USD$'+String(total));
  assert.equal(b.doc.querySelectorAll('.cash-inputs label').length,2);
  assert.deepEqual(b.doc.querySelectorAll('.cash-inputs label').map(el=>el.querySelector('span').textContent),['现金 1 · USD$','现金 2 · USD$']);
  await b.save();assert.equal(b.doc.querySelector('.cash-inputs'),null);assert.equal(b.doc.querySelector('.blk-cash .t-mv').textContent,'$'+Math.round(total));
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
  b.doc.getElementById('costExport').click();assert.equal(b.blobs.length,1);
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

for(const [kind,target,price,on] of [
  ['add',100,103,true],['add',100,103.0000000001,false],['add',100,102.9999999999,true],
  ['cut',100,97,true],['cut',100,96.9999999999,false],['cut',100,97.0000000001,true],
  ['add',123.45,127.1535,true],['add',123.45,127.1535000001,false],
  ['cut',123.45,119.7465,true],['cut',123.45,119.7464999999,false],
  ['add',0.1,0.103,true],['add',0.1,0.1030000001,false],
  ['cut',0.1,0.097,true],['cut',0.1,0.0969999999,false],
  ['add',100,50,true],['cut',100,200,true]
])test('加减3%十进制精确边界 '+kind+' '+target+'→'+price,async()=>{
  const d=copy(SEED);d.items.AAA.cost=price;
  const b=await bootCash({local:d,prepareDocument(doc){
    const row=doc.getElementById('s-aaa');row.querySelector('.px-price').textContent=String(price);
    row.querySelector(kind==='add'?'.lvtag.cut':'.lvtag.add').remove();
    row.querySelector(kind==='add'?'.lvtag.add':'.lvtag.cut').textContent=(kind==='add'?'加 ':'减 ')+target;
  }}),r=b.rows[0];
  assert.equal((kind==='add'?r.warnAdd:r.warnCut).style.display,on?'':'none');assert.equal(r.tr.className,on?'red':'green');
  assert.equal(b.nearPrice(price,target,3,kind==='add'?1:-1),on);assert.equal(b.requests.length,0);
});
test('加减精确比较支持科学计数、小数阈值和零阈值，不以epsilon扩大范围',async()=>{
  const b=await bootCash();
  for(const [p,t,g,side,on] of [[1.03e-7,1e-7,3,1,true],[1.0300000001e-7,1e-7,3,1,false],
    [9.7e-8,1e-7,3,-1,true],[9.699999999e-8,1e-7,3,-1,false],
    [102.5,100,2.5,1,true],[97.5,100,2.5,-1,true],[100,100,0,1,true],[100,100,0,-1,true],
    [100.0000000001,100,0,1,false],[99.9999999999,100,0,-1,false]])assert.equal(b.nearPrice(p,t,g,side),on);
});
function signalCaps(doc,symbols=['AAA'],target=100){
  for(const sym of symbols){
    const td=doc.getElementById('s-'+sym.toLowerCase()).querySelector('td.sig');
    td.innerHTML='<span class="lvtag add">加 '+target+'</span><span class="lvtag cut">减 '+target+'</span>';
  }
}
test('顶部灰为技术text_only、浮亏已读、加减已读并集，单双提醒重复paint不累加',async()=>{
  const d={...copy(SEED),items:{AAA:{cost:125,qty:5},BBB:{cost:100,qty:2},CCC:{cost:100,qty:1}}};
  const b=await bootLoss({local:d,symbols:['AAA','BBB','CCC'],prices:{AAA:100,BBB:100,CCC:100},prepareDocument(doc){
    signalCaps(doc,['AAA','CCC']);lossGrayDocument(doc,['AAA','BBB']);
  }});
  assertLossGrayBadge(b,1);b.ack('AAA|add|100');assertLossGrayBadge(b,2);
  b.ack('AAA|cut|100');assertLossGrayBadge(b,2);ackCurrentLoss(b);assertLossGrayBadge(b,2);
  b.ack('CCC|add|100');b.ack('CCC|cut|100');assertLossGrayBadge(b,3);
  for(let i=0;i<5;i++){b.paint();assertLossGrayBadge(b,3);}
  b.ack('AAA|add|100');assertLossGrayBadge(b,3);b.ack('AAA|cut|100');ackCurrentLoss(b);assertLossGrayBadge(b,2);
  b.ack('CCC|add|100');b.ack('CCC|cut|100');assertLossGrayBadge(b,1);
  for(const r of b.rows){r.trig=0;r.cut=0;}setLossPrice(b,'AAA',125);assertLossGrayBadge(b,2);
  assert.equal(b.requests.length,0);assert.equal(b.store.writes.length,0);
});
test('加减已读与其他原红黄灯并存也计入灰标题，每股一次且先更新后通知',async()=>{
  const b=await bootLoss({prices:{AAA:100,BBB:100},levels:{AAA:'red pur-high',BBB:'yellow'},prepareDocument(doc){signalCaps(doc,['AAA','BBB']);lossGrayDocument(doc,[]);}}),seen=[];
  b.ctx.addEventListener('mm:ack-change',()=>seen.push(b.doc.querySelector('#signalCounts summary .stat-badge.gray').textContent));
  b.ack('AAA|add|100');assertLossGrayBadge(b,1);assert.equal(seen.at(-1),'1');b.ack('AAA|cut|100');assertLossGrayBadge(b,1);
  b.ack('BBB|add|100');b.ack('BBB|cut|100');assertLossGrayBadge(b,2);assertLossBadges(b,1,1);
  const n=seen.length;for(let i=0;i<4;i++)b.paint();assert.equal(seen.length,n);
  assert.equal(b.rows[0].tr.className,'pur-high red');assert.equal(b.rows[1].tr.className,'yellow');
});
test('只读旧ACK不清理时加减灰统计仍排除错价、失效、非持仓及已移除标的',async()=>{
  const at='2026-10-19T00:00:00Z',d={...copy(SEED),acks:{'AAA|add|100':at,'AAA|add|95':at,'AAA|cut|100':at,'BBB|cut|100':at,'REMOVED|add|100':at}};
  const store=shared({[LOCAL]:d});store.holder=true;
  const b=await boot({store,forbidRequests:true,groups:{BBB:'focus'},prepareDocument(doc){
    lossStatDocument(doc,{prices:{AAA:100,BBB:100},prepareDocument(doc){signalCaps(doc,['AAA','BBB']);lossGrayDocument(doc,[]);}});
  }});
  assert.equal(b.state().writer,false);assert.equal(Object.keys(b.state().DATA.acks).length,5);
  assertLossGrayBadge(b,1);assert.deepEqual(copy(b.ackStatEntries('reduce_price').map(e=>e.sym)),['AAA']);
  assert.deepEqual(copy(b.ackStatEntries('add_price').map(e=>e.label)),['加 100']);
  setLossPrice(b,'AAA',104);assert.deepEqual(copy(b.ackStatEntries('add_price')),[]);assertLossGrayBadge(b,1);
  b.move('AAA',null);assertLossGrayBadge(b,0);assert.equal(Object.keys(b.state().DATA.acks).length,5);
});
test('加减条件消失、价位变更及移除清ACK写撤销，恢复后重新提醒',async()=>{
  const b=await bootLoss({prices:{AAA:100},prepareDocument:doc=>signalCaps(doc)}),r=b.rows[0];
  b.ack('AAA|add|100');b.ack('AAA|cut|100');setLossPrice(b,'AAA',104);
  assert.equal(b.state().DATA.acks['AAA|add|100'],undefined);assert.ok(b.state().DATA.ack_tombstones['AAA|add|100']);
  setLossPrice(b,'AAA',96);assert.equal(b.state().DATA.acks['AAA|cut|100'],undefined);assert.ok(b.state().DATA.ack_tombstones['AAA|cut|100']);
  setLossPrice(b,'AAA',100);assert.equal(r.warnAdd.querySelector('.ack-btn').getAttribute('aria-label'),'标记已读');
  b.ack('AAA|add|100');r.trig=101;b.paint();assert.equal(b.state().DATA.acks['AAA|add|100'],undefined);
  b.ack('AAA|add|101');b.move('AAA',null);assert.equal(b.state().DATA.acks['AAA|add|101'],undefined);assertLossGrayBadge(b,0);
});
// 折叠口径仅来自生成位置或明确 data-quiet-keep；不从胶囊、class猜主表保留资格。
function quietDocument(doc,{quietSymbols=['AAA'],keep,level='green',onlyQuiet=false}={}){
  const row=doc.getElementById('s-aaa'),main=row.closest('table.stk'),root=main.parentNode;
  row.className=level;if(keep!==undefined)row.setAttribute('data-quiet-keep',String(keep));
  const fold=doc.createElement('details');fold.className='quiet-list';
  fold.innerHTML='<summary>无异动 '+quietSymbols.length+' 只 · 点击查看</summary><table class="stk"><colgroup><col><col></colgroup><tr class="mh"><td>标的</td><td>价格</td></tr></table>';
  root.insertBefore(fold,main.nextSibling);
  for(const sym of quietSymbols){const r=doc.getElementById('s-'+sym.toLowerCase()),rail=r.nextElementSibling;fold.querySelector('table').appendChild(r);if(rail)fold.querySelector('table').appendChild(rail);}
  if(onlyQuiet){const b=doc.getElementById('s-bbb'),rail=b.nextElementSibling;b.remove();rail.remove();main.remove();}
  return fold;
}
function assertRowPair(b,sym,quiet){
  const r=b.rows.find(r=>r.sym===sym);assert.equal(!!r.tr.closest('.quiet-list'),quiet);
  assert.equal(r.tr.nextElementSibling,r.rangeTr);assert.equal(r.rangeTr.parentNode,r.tr.parentNode);
}
test('双价格提醒即时移出无异动，单已读仍主表、两已读回原折叠表并保留range52',async()=>{
  const b=await bootCash({rules:{trigger_gap_pct:6,reduce_gap_pct:4},prepareDocument:doc=>quietDocument(doc)}),fold=b.doc.querySelector('.quiet-list');
  assertRowPair(b,'AAA',false);assert.equal(fold.hidden,true);assert.equal(fold.open,false);
  b.ack('AAA|add|95');assertRowPair(b,'AAA',false);assert.equal(b.rows[0].tr.className,'red');
  b.ack('AAA|cut|104');assertRowPair(b,'AAA',true);assert.equal(fold.hidden,false);assert.equal(fold.open,false);
  assert.equal(fold.querySelector('summary').textContent,'无异动 1 只 · 点击查看');
  b.ack('AAA|cut|104');assertRowPair(b,'AAA',false);assert.equal(fold.hidden,true);assert.equal(fold.open,false);
  for(let i=0;i<3;i++)b.paint();assert.equal(b.doc.querySelectorAll('.quiet-list').length,1);
});
test('即时追加浮亏灯移出原无异动，已读回表、撤销再移出并保留展开态',async()=>{
  const b=await bootLoss({prices:{AAA:100},prepareDocument:doc=>quietDocument(doc)}),fold=b.doc.querySelector('.quiet-list');fold.open=true;
  assertRowPair(b,'AAA',true);await b.enter();b.edit('AAA','cost','125');await b.tick(16);assertRowPair(b,'AAA',false);
  assert.equal(fold.open,true);ackCurrentLoss(b);assertRowPair(b,'AAA',true);assert.equal(fold.open,true);
  ackCurrentLoss(b);assertRowPair(b,'AAA',false);assert.equal(fold.open,true);
  b.edit('AAA','cost','100');await b.tick(16);assertRowPair(b,'AAA',true);assert.equal(b.state().DATA.acks['AAA|loss20'],undefined);
});
for(const level of ['red','yellow','gray'])test('生成原'+level+'灯及目标叠加保留，价格与浮亏已读不撤原灯也不回无异动',async()=>{
  const d=copy(SEED);d.items.AAA.cost=200;
  const b=await bootCash({local:d,rules:{trigger_gap_pct:6,reduce_gap_pct:4},prepareDocument(doc){
    const row=doc.getElementById('s-aaa');row.className=level+' pur-high';row.setAttribute('data-quiet-keep','false');
  }});
  b.ack('AAA|add|95');b.ack('AAA|cut|104');b.ack('AAA|loss20');assertRowPair(b,'AAA',false);
  assert.ok(b.rows[0].tr.className.split(/\s+/).includes(level));assert.ok(b.rows[0].tr.className.split(/\s+/).includes('pur-high'));
  assert.equal(b.doc.querySelector('#costMembers li[data-sym="AAA"]').className,'lv-'+level);
});
test('明确quiet_keep保留与未标记主表行不擅自折叠，false才允许回无异动',async()=>{
  for(const keep of [true,undefined,false]){
    const b=await bootLoss({prices:{AAA:100},prepareDocument(doc){if(keep!==undefined)doc.getElementById('s-aaa').setAttribute('data-quiet-keep',String(keep));}});
    assertRowPair(b,'AAA',keep===false);await b.enter();b.edit('AAA','cost','125');await b.tick(16);assertRowPair(b,'AAA',false);
    ackCurrentLoss(b);assertRowPair(b,'AAA',keep===false);
    if(keep===false){const fold=b.doc.querySelector('.quiet-list');assert.equal(fold.open,false);assert.equal(fold.querySelector('summary').textContent,'无异动 1 只 · 点击查看');}
  }
});
test('只有无异动表时动态创建主表保留列头及colgroup，撤灯回原表不复制行情行',async()=>{
  const b=await bootCash({rules:{trigger_gap_pct:6,reduce_gap_pct:4},prepareDocument:doc=>quietDocument(doc,{onlyQuiet:true})});
  const root=b.doc.getElementById('group_positions'),main=root.children.find(el=>el.tagName==='TABLE');assert.ok(main);
  assert.equal(main.querySelectorAll('colgroup').length,1);assert.equal(main.querySelector('.mh').textContent,'标的价格');assertRowPair(b,'AAA',false);
  b.ack('AAA|add|95');b.ack('AAA|cut|104');assertRowPair(b,'AAA',true);
  assert.equal(b.doc.querySelectorAll('#s-aaa').length,1);assert.equal(b.doc.querySelectorAll('.range52-row').length,1);
});
test('长持仓内部折叠移行不跨到短持仓，移组后仅用当前区主表',async()=>{
  const b=await bootLoss({prices:{AAA:100},longs:['AAA'],prepareDocument:doc=>quietDocument(doc)}),r=b.rows.find(row=>row.sym==='AAA');
  assertRowPair(b,'AAA',true);await b.enter();b.edit('AAA','cost','125');await b.tick(16);
  assertRowPair(b,'AAA',false);assert.ok(r.tr.closest('#group_positions_long'));ackCurrentLoss(b);assertRowPair(b,'AAA',true);
  b.move('AAA','focus');assert.equal(r.lossLv,0);assert.ok(r.tr.closest('#group_focus'));assert.equal(r.tr.closest('#group_positions'),null);
});
test('已读折叠刷新恢复生成位置口径，再撤销可即时移回主表',async()=>{
  const b=await bootCash({rules:{trigger_gap_pct:6,reduce_gap_pct:4},prepareDocument:doc=>quietDocument(doc)});
  b.ack('AAA|add|95');b.ack('AAA|cut|104');assertRowPair(b,'AAA',true);await b.dispose();
  const c=await bootCash({store:b.store,session:b.session,rules:{trigger_gap_pct:6,reduce_gap_pct:4},prepareDocument:doc=>quietDocument(doc)});
  assertRowPair(c,'AAA',true);c.ack('AAA|add|95');assertRowPair(c,'AAA',false);assert.equal(c.requests.length,0);
});

test('缺失52周轨道的range52占位行也随主行情移行，不能遗留错表',async()=>{
  const b=await bootCash({rules:{trigger_gap_pct:6,reduce_gap_pct:4},prepareDocument(doc){
    const r=doc.getElementById('s-aaa');r.nextElementSibling.innerHTML='<td><span class="range52-missing">52周区间：数据待更新</span></td>';
    quietDocument(doc);
  }});
  const r=b.rows.find(row=>row.sym==='AAA');
  assert.equal(r.rail,null);assertRowPair(b,'AAA',false);assert.match(r.rangeTr.textContent,/数据待更新/);
  b.ack('AAA|add|95');b.ack('AAA|cut|104');assertRowPair(b,'AAA',true);
  assert.equal(b.doc.querySelectorAll('.range52-missing').length,1);
});
test('原折叠资格不跨移组扩散，新的主表缺保留元数据时保持原表',async()=>{
  const b=await bootLoss({prices:{AAA:100},prepareDocument:doc=>quietDocument(doc)});
  const focus=b.doc.createElement('details');focus.id='group_focus';focus.innerHTML='<summary>重点</summary><table class="stk"></table>';b.doc.body.appendChild(focus);
  b.move('AAA','focus');assertRowPair(b,'AAA',false);assert.ok(b.rows.find(r=>r.sym==='AAA').tr.closest('#group_focus'));
  assert.equal(focus.querySelector('.quiet-list'),null,'不可把旧组的折叠资格当作新组证据');
});
test('tbody真实层级移行后range52相邻，创建表不重复表头',async()=>{
  const b=await bootCash({rules:{trigger_gap_pct:6,reduce_gap_pct:4},prepareDocument(doc){
    const main=doc.getElementById('s-aaa').closest('table'),body=doc.createElement('tbody');
    for(const child of [...main.children])body.appendChild(child);main.appendChild(body);
    const fold=quietDocument(doc),table=fold.querySelector('table'),tbody=doc.createElement('tbody');
    for(const child of [...table.children])if(child.tagName==='TR')tbody.appendChild(child);table.appendChild(tbody);
  }});
  assertRowPair(b,'AAA',false);assert.equal(b.rows[0].tr.parentNode.tagName,'TBODY');
  b.ack('AAA|add|95');b.ack('AAA|cut|104');assertRowPair(b,'AAA',true);assert.equal(b.rows[0].tr.parentNode.tagName,'TBODY');
  assert.equal(b.doc.querySelector('.quiet-list').querySelectorAll('.mh').length,1);
});

test('加减价撤掉后胶囊提醒ACK同步清空，恢复价位重新亮灯',async()=>{
  const b=await bootLoss({prices:{AAA:100},prepareDocument:doc=>signalCaps(doc)}),r=b.rows.find(row=>row.sym==='AAA');
  b.ack('AAA|add|100');b.ack('AAA|cut|100');r.trig=0;r.cut=0;b.paint();
  assert.equal(r.warnAdd.style.display,'none');assert.equal(r.warnCut.style.display,'none');assert.equal(r.cap.style.display,'none');
  assert.equal(b.state().DATA.acks['AAA|add|100'],undefined);assert.equal(b.state().DATA.acks['AAA|cut|100'],undefined);assertLossGrayBadge(b,0);
  r.trig=100;r.cut=100;b.paint();assert.equal(r.cap.style.display,'');assert.equal(r.warnAdd.style.display,'');assert.equal(r.warnCut.style.display,'');
  assert.equal(r.tr.className,'red');assert.equal(r.warnAdd.querySelector('.ack-btn').getAttribute('aria-label'),'标记已读');
});
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
for(const [kind,raw,text,on] of [
  ['add','103.0001','103.00',false],['cut','96.9999','97.00',false],
  ['add','103','103.01',true],['cut','97','96.99',true],
  ['add','127.1535','127.15',true],['add','127.1536','127.15',false],
  ['cut','119.7465','119.75',true],['cut','119.7464','119.75',false]
])test('DOM原价优先于舍入text边界 '+kind+' '+raw+'/'+text,async()=>{
  const target=Number(raw)>110?123.45:100;
  const b=await bootCash({local:{...copy(SEED),items:{AAA:{cost:Number(raw),qty:5}}},prepareDocument(doc){
    const tr=doc.getElementById('s-aaa');tr.setAttribute('data-price',raw);tr.setAttribute('data-prev-close','100');
    tr.querySelector('.px-price').textContent=text;
    tr.querySelector(kind==='add'?'.lvtag.cut':'.lvtag.add').remove();
    tr.querySelector(kind==='add'?'.lvtag.add':'.lvtag.cut').textContent=(kind==='add'?'加 ':'减 ')+target;
  }}),r=b.rows.find(r=>r.sym==='AAA');
  assert.equal(r.price,Number(raw));assert.equal(r.prevClose,100);assert.equal(r.mv,Number(raw)*5);
  assert.equal((kind==='add'?r.warnAdd:r.warnCut).style.display,on?'':'none');assert.equal(r.tr.className,on?'red':'green');
  assert.equal(r.tr.querySelector('.px-price').textContent,text,'不能改写显示价格');
  assert.equal(b.requests.length,0);assert.equal(b.store.writes.length,0);
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
  const money=(amt<0?'-':'+')+'$'+Math.abs(Math.round(amt)).toLocaleString('en-US');
  const summary=b.doc.querySelector('.blk-pnl-fold .cnt b');
  assert.equal(summary.textContent,money+' '+(dayPct>=0?'+':'')+dayPct.toFixed(1)+'%');
  assert.equal(b.doc.querySelector('.cost-contrib .ca').textContent,money);
  assert.equal(b.doc.querySelector('.cost-contrib .cv').textContent,(r.chg>=0?'+':'')+r.chg.toFixed(1)+'%');
  assert.equal(r.tr.querySelector('.px-chg').textContent,chg);
  assert.equal(b.requests.length,0);assert.equal(b.store.writes.length,0);
});
for(const raw of [undefined,'','0','-1','Infinity','100oops'])test('旧页或无效原值'+String(raw)+'回退显示价格涨跌幅',async()=>{
  const b=await bootCash({prepareDocument(doc){
    const tr=doc.getElementById('s-aaa');if(raw!==undefined){tr.setAttribute('data-price',raw);tr.setAttribute('data-prev-close',raw);}
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
for(const original of ['red','green'])test('ACKCASE原'+original+'三条ACK只撤追加灯，原red留主表原green回折叠',async()=>{
  const b=await bootCash({local:{...copy(SEED),items:{AAA:{cost:125,qty:5}}},prepareDocument(doc){
    const tr=doc.getElementById('s-aaa');tr.className=original;tr.setAttribute('data-price','100.0001');tr.setAttribute('data-prev-close','100');
    tr.setAttribute('data-quiet-keep','false');tr.querySelector('.px-price').textContent='100.00';
    signalCaps(doc,['AAA'],100);
  }}),r=b.rows.find(r=>r.sym==='AAA');
  assert.equal(r.origCls,original);assert.equal(r.lossLv,1);assertRowPair(b,'AAA',false);assert.equal(r.tr.className,'red');
  b.ack('AAA|add|100');b.ack('AAA|cut|100');b.ack('AAA|loss10');
  for(let i=0;i<3;i++)b.paint();assert.equal(r.origCls,original);assert.equal(r.tr.className,original);
  assertRowPair(b,'AAA',original==='green');
  for(const k of ['AAA|add|100','AAA|cut|100','AAA|loss10'])assert.ok(b.state().DATA.acks[k]);
  b.ack('AAA|loss10');assert.equal(r.tr.className,'red');assertRowPair(b,'AAA',false);
});

(async()=>{let passed=0;for(const t of tests){try{await t.fn();passed++;console.log('PASS '+t.name);}catch(e){console.error('FAIL '+t.name+'\n'+e.stack);process.exitCode=1;}}console.log('\n仓位面板离线测试：'+passed+'/'+tests.length+' 通过');})();
