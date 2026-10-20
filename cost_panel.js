<script>
(function(){
  var LS_LOCAL='mm_position_costs_local_v1';
  var LS_CLOUD='mm_position_costs_cloud_v1';
  var isolated=location.protocol==='file:' || !(navigator.locks && navigator.locks.request);
  var SESSION_LOCAL='mm_position_costs_preview_v2';
  var ISOLATED_NOTICE='隔离预览：仅保存在当前标签页会话中，刷新可恢复；关闭标签页前请导出。不会修改共享本地仓位或上传云端。';
  /* 真实持仓（券商截图 2026-10-19）：成本价来自 holdings.json、数量来自 position-costs.local.json */
  var REAL=__MM_REAL__;

  function money(n){ return (n<0?'-':'+')+'$'+Math.abs(Math.round(n)).toLocaleString('en-US'); }
  function plain(n){ return '$'+Math.round(n).toLocaleString('en-US'); }
  function pct(n){ return (n>=0?'+':'')+n.toFixed(1)+'%'; }
  /* 最多 4 位小数、去掉多余的 0：录入允许 0.0001 这种精度，显示不拖尾 */
  function trim(n){ var s=(Math.round(n*10000)/10000).toFixed(4).replace(/0+$/,'').replace(/\.$/,'');
    return s===''?'0':s; }
  function nowStr(){ var d=new Date(),p=function(x){return (x<10?'0':'')+x;};
    return d.getFullYear()+'-'+p(d.getMonth()+1)+'-'+p(d.getDate())+' '+p(d.getHours())+':'+p(d.getMinutes()); }
  var storageErrors={}, NOTICE='';
  function read(k){
    try{
      if(isolated && k===LS_CLOUD) return null;
      var v=isolated && k===LS_LOCAL?sessionStorage.getItem(SESSION_LOCAL):localStorage.getItem(k);
      // 隔离会话第一次打开可只读参考本机旧值，之后只读写本 tab 的 sessionStorage。
      if(isolated && k===LS_LOCAL && v===null) v=localStorage.getItem(k);
      var parsed=v===null?null:JSON.parse(v); delete storageErrors[k]; return parsed;
    }
    catch(e){ storageErrors[k]='本地数据读取失败'; return null; }
  }
  function write(k,v){
    try{
      if(isolated){
        if(k!==LS_LOCAL) return false;
        sessionStorage.setItem(SESSION_LOCAL,JSON.stringify(v));
      }else localStorage.setItem(k,JSON.stringify(v));
      delete storageErrors[k]; return true;
    }
    catch(e){ storageErrors[k]='本地保存失败，尚未持久化；请导出备份，刷新可能丢失改动'; return false; }
  }
  function num(v){ var n=parseFloat(v); return isFinite(n)?n:0; }
  function esc(s){ return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;')
    .replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;'); }
  function clone(v){ return JSON.parse(JSON.stringify(v)); }
  function own(o,k){ return Object.prototype.hasOwnProperty.call(o,k); }

  /* 备注名个别修正：名字太长或带错的前缀，这里按代码替换（正式实现改 holdings.json） */
  var NOTE_FIX={'景顺纳斯达克100指数ETF':'纳斯达克100指数ETF'};

  /* ---------- 0. 页底：美联储块 →「完整报灯规则」 ---------- */
  (function(){
    var old=document.getElementById('policyAlertDetails');
    var tpl=document.getElementById('rulesTpl');
    if(!old || !tpl) return;
    var box=document.createElement('details');
    box.className='macro-help'; box.id='signalRules';
    box.innerHTML=tpl.innerHTML;
    old.parentNode.replaceChild(box, old);
  })();

  /* 数据源说明（.foot：行情／垃圾债利差／市场宽度／共识价／查看完整规则与阈值）整块沉到
     页面最底部——放在「规则与数据说明」卡片之后（与 monitor.py 2026-10-19 的改动同步） */
  (function(){
    var foot=document.querySelector('.foot'), notes=document.getElementById('pageNotes');
    if(!foot || !notes || foot.previousElementSibling===notes) return;
    notes.parentNode.insertBefore(foot, notes.nextSibling);
  })();

  /* ---------- 1. 扫描行情行；是否持仓只由实际 positions 分组决定 ---------- */
  function isPosition(r){
    // 后端 group=position 渲染为 #group_positions，含长持仓子组；DATA.items 仅是成本档案。
    return document.documentElement.contains(r.tr) && !!r.tr.closest('#group_positions');
  }
  // ETF 胶囊必须在代码后面（第一行），不能留到中文备注后面。全站所有个股行都处理。
  // 正式实现改 monitor.py 的个股行：<td class="sym">CODE<span class="tag">ETF</span><span class="note">…</span></td>
  Array.prototype.forEach.call(document.querySelectorAll('td.sym'), function(sd){
    var t=sd.querySelector('.tag');
    if(t && sd.firstChild) sd.insertBefore(t, sd.firstChild.nextSibling);
  });
  var ROWS=[];
  /* 加仓价 / 减仓价只认 holdings.json 里真实填的 trigger / reduce：没填就不画胶囊、不报灯。
     2026-10-19 修：此前这段会给每只持仓按现价 ×0.985/0.86/0.78 造一个「示意加仓价」，
     线上每只票都挂着一个假价位，还有 1/3 直接落进触发区报红灯 —— 全部去掉 */
  /* 行灯级合成：一只标的可能同时有几条规则命中（页面原有灯 + 加仓价 + 浮亏 10%），
     取最严重的那个；关掉其中一条已读，其它规则该红还红、该黄还黄。 */
  var LV_ORDER=['red','yellow','green','gray'];
  function maxLv(a,b){ if(!a) return b; if(!b) return a;
    return LV_ORDER.indexOf(a)<=LV_ORDER.indexOf(b)?a:b; }
  function withLv(cls, want){
    var parts=(cls||'').split(/\s+/).filter(Boolean), cur=null;
    parts=parts.filter(function(p){ if(LV_ORDER.indexOf(p)>=0){ cur=p; return false; } return true; });
    var lv=maxLv(cur, want);
    if(lv) parts.push(lv);
    return parts.join(' ');
  }
  var RULES=window.__MM_RULES__||{};
  var GAP_TRIGGER=own(RULES,'trigger_gap_pct')?RULES.trigger_gap_pct:3;
  var GAP_REDUCE=own(RULES,'reduce_gap_pct')?RULES.reduce_gap_pct:3;
  // 清仓通过移组完成，不能按代码硬删除行；移回 positions 时沿用保留的成本/数量供用户复核。
  Array.prototype.forEach.call(document.querySelectorAll('tr[id^="s-"]'), function(tr){
    var sym=tr.id.slice(2).toUpperCase();
    var pxEl=tr.querySelector('.px-price'); if(!pxEl) return;
    var price=num((pxEl.textContent||'').replace(/,/g,'')); if(!(price>0)) return;
    // 当日涨跌%（页面自带 .px-chg，如 "-0.55%"）：当日盈亏＝(现价−昨收)×数量
    var chgEl=tr.querySelector('.px-chg');
    var chg=chgEl?num((chgEl.textContent||'').replace(/[%,+ ]/g,'')):0;
    if(isNaN(chg)) chg=0;
    var sd=tr.querySelector('td.sym'); if(!sd) return;
    // 长/短看它落在哪个区：长持仓子区里的才是长，持仓卡主体表格是短
    var isLong=!!tr.closest('#group_positions_long');
    var noteEl=sd.querySelector('.note');
    var note=noteEl?(noteEl.textContent||'').trim():'';
    if(note && NOTE_FIX[note]) note=NOTE_FIX[note];
    // ETF 胶囊：个股行里就是 td.sym 内的 .tag，文本为 ETF
    var isEtf=false;
    Array.prototype.forEach.call(sd.querySelectorAll('.tag'), function(t){
      if((t.textContent||'').trim().toUpperCase()==='ETF') isEtf=true;
    });
    // 名称后面的「长」标签没用处，直接去掉（正式实现就是从 monitor.py 里删掉那段 hold_html）
    Array.prototype.forEach.call(sd.querySelectorAll('.unit'), function(u){
      if((u.textContent||'').trim()==='长') u.parentNode.removeChild(u);
    });
    // ETF 胶囊的位置已在上面统一前移，这里不再重复处理
    // 灯级：行 class 的第一个词就是红/黄/绿/灰，用来给仓位面板的行首圆点上色
    var lv=(tr.className||'').split(/\s+/)[0];
    if(!lv) lv='green';
    var railTr=tr.nextElementSibling;
    var rail=railTr&&railTr.querySelector('.range52-rail');
    var lab=rail?(rail.getAttribute('aria-label')||''):'';
    var m=lab.match(/([\d,]+\.\d{2})[低高]*，本轮价 ([\d,]+\.\d{2}) 位于区间 ([\d.]+)%，[^，]*高 ([\d,]+\.\d{2})/);
    // 52 周范围缺失不能把真实持仓从市值统计里丢掉，只禁用范围内成本刻度。
    var low=m?num(m[1].replace(/,/g,'')):0, high=m?num(m[4].replace(/,/g,'')):0;

    /* 把旧条重建为新版结构：左端写最低价、右端写最高价，百分比与紫方块都挪进灰轨里。
       快照的 rail 只有「百分比 + 轨道」两个孩子，百分比还挂在 rail 上（旧版式），
       新版要用轨道做定位基准，所以先把它挪进去，不然 left% 会按整条宽度算 */
    var track=rail&&rail.querySelector('.range52-track');
    if(track&&m){
      if(!rail.querySelector('.range52-lo')){
        var lo=document.createElement('span');
        lo.className='range52-lo'; lo.setAttribute('aria-hidden','true'); lo.textContent=m[1];
        rail.insertBefore(lo, track);
      }
      if(!rail.querySelector('.range52-hi')){
        var hi=document.createElement('span');
        hi.className='range52-hi'; hi.setAttribute('aria-hidden','true'); hi.textContent=m[4];
        rail.appendChild(hi);
      }
      var pcEl=rail.querySelector('.range52-percent');
      if(pcEl&&pcEl.parentNode!==track) track.insertBefore(pcEl, track.firstChild);
    }
    /* 加仓价 / 减仓价只认页面里已带上的真实胶囊（monitor.py 读 holdings.json 的 trigger / reduce 生成），
       这里不再按现价造示意值。持仓没填 trigger 就是 0：不画胶囊、不报灯 */
    var cap=tr.querySelector('.lvtag.add,.lvtag.add-ck');
    var cutCap=tr.querySelector('.lvtag.reduce,.lvtag.cut');
    var trig=cap?num(cap.textContent.replace(/[^\d.]/g,'')):0;
    var cut=cutCap?num(cutCap.textContent.replace(/[^\d.]/g,'')):0;
    var sigAdd=null;
    var tick=document.createElement('span');
    tick.className='range52-cost'; tick.style.display='none';
    // 挂到灰轨上（而不是整条 rail）：rail 里还夹着左右两个价格文字，
    // 挂 rail 的话 left% 会按整条宽度算，跟紫方块对不上
    if(rail) (track||rail).appendChild(tick);
    var info=document.createElement('span');
    info.className='cost-info';
    /* 三条可手动关闭的提醒，模式完全一致：一行文字 + ✓ 按钮（再点一下撤销）
       ① 加仓价 ② 减仓价 ③ 浮亏 ≥10%。位置统一在成本价那一排下面 */
    var warnAdd=document.createElement('span');
    warnAdd.className='cost-warn'; warnAdd.style.display='none';
    var warnCut=document.createElement('span');
    warnCut.className='cost-warn'; warnCut.style.display='none';
    var warn=document.createElement('span');
    warn.className='cost-warn'; warn.style.display='none';
    var td=rail?rail.parentNode:(tr.querySelector('td.sig')||sd);
    td.insertBefore(info,rail?rail.nextSibling:null);
    td.insertBefore(warnAdd, info.nextSibling);
    td.insertBefore(warnCut, warnAdd.nextSibling);
    td.insertBefore(warn, warnCut.nextSibling);
    ROWS.push({sym:sym,note:note,isLong:isLong,isEtf:isEtf,lv:lv,price:price,chg:chg,low:low,high:high,
               rail:rail,lab0:lab,lowTxt:m?m[1]:'',highTxt:m?m[4]:'',tr:tr,origCls:tr.className||'',
               trig:trig,cut:cut,cap:cap,cutCap:cutCap,sigAdd:sigAdd,
               tick:tick,info:info,warnAdd:warnAdd,warnCut:warnCut,warn:warn});
  });

  /* ---------- 1.5 「已读」：临时提醒可以手动关 ----------
     规则：点一下＝已知晓，这条规则本轮不再亮红灯；**条件消失后自动失效**
     （价格回到加仓价 3% 之外、或浮亏收窄到 10% 以内），下次再触发会重新亮，
     不会「关一次就永远不提醒」。已读及撤销记录随 DATA 保存/导出/上传，旧已读不能覆盖撤销。 */
  /* 灯级合成：页面原有灯 + 加仓价规则 + 浮亏 10% 规则，取最严重的一个。
     点掉某一条的已读，只撤它自己那一份红灯；别的规则照旧。 */
  /* 三条提醒共用一个渲染函数：一行文字 + ✓ 按钮（已读后再点是撤销），
     跟浮亏 −10% 完全同一套；胶囊只负责显示价位，不再承担「已读」点击 */
  function renderWarn(el, text, key, acked){
    if(!el) return;
    el.style.display='';
    el.className='cost-warn lv1'+(acked?' acked':'');
    el.innerHTML='<span class="w">⚠ '+text+'</span>'
      +(acked
        ? ' 已读<button type="button" class="ack-btn on" data-ack="'+key
          +'" aria-label="撤销已读">✓</button>'
        : '<button type="button" class="ack-btn" data-ack="'+key
          +'" aria-label="标记已读">✓</button>');
  }
  function hideWarn(el){ if(el){ el.style.display='none'; el.textContent=''; } }
  function applyLights(){
    if(!DATA.acks) DATA.acks={};
    var A=DATA.acks;
    ROWS.forEach(function(r){
      var want=null, sigTxt='';
      // ① 加仓价：已跌破、或距加仓价不足 3%（跟 monitor.py 判定一致）
      if(r.trig>0){
        var gap=(r.price/r.trig-1)*100, on=gap<=GAP_TRIGGER;
        var k=r.sym+'|add|'+r.trig;
        if(!on) pruneAck(k);   // 条件消失 → 已读自动失效
        var acked=!!A[k];
        if(r.cap){
          r.cap.className='lvtag add';
          r.cap.removeAttribute('data-ack'); r.cap.removeAttribute('role');
          r.cap.removeAttribute('tabindex'); r.cap.removeAttribute('aria-pressed');
          r.cap.setAttribute('aria-label','加仓价（已读按钮在下面那行提醒里）');
          r.cap.innerHTML='<span class="t">加 '+r.trig+'</span>';
        }
        if(on){
          renderWarn(r.warnAdd, (gap<=0?'已跌破加仓价 '+r.trig
            :'距加仓价 '+r.trig+' 还差 '+gap.toFixed(1)+'%'), k, acked);
          if(!acked) sigTxt+=(gap<=0?'已跌破加仓价 ':'距加仓价 ')+r.trig
            +(gap<=0?'':' 还差 '+gap.toFixed(1)+'%')+' · ';
          if(!acked) want='red';
        } else hideWarn(r.warnAdd);
      }
      // ② 减仓价：已涨到它之上（不管高出多少）→ 红；还差不足 3% → 也红（跟 monitor.py 对称）
      if(r.cut>0){
        var rgap=(r.price/r.cut-1)*100, onR=(rgap>=0)||(-rgap<=GAP_REDUCE);
        var kc=r.sym+'|cut|'+r.cut;
        if(!onR) pruneAck(kc);
        var aR=!!A[kc];
        if(onR){
          renderWarn(r.warnCut, (rgap>=0?'已涨到减仓价 '+r.cut+'（高出 '+rgap.toFixed(1)+'%）'
            :'距减仓价 '+r.cut+' 还差 '+(-rgap).toFixed(1)+'%'), kc, aR);
          if(!aR) sigTxt+=(rgap>=0?'已涨到减仓价 '+r.cut+'（高出 '+rgap.toFixed(1)+'%）'
            :'距减仓价 '+r.cut+' 还差 '+(-rgap).toFixed(1)+'%')+' · ';
          if(!aR) want='red';
        } else hideWarn(r.warnCut);
      }
      if(r.sigAdd) r.sigAdd.textContent=sigTxt;
      // ③ 浮亏 ≥10%：也是红灯（红字 + 行级 red），点掉已读就撤掉这一份
      if(r.lossLv>0 && !r.lossAcked) want='red';
      if(r.tr) r.tr.className=withLv(r.origCls, want);
    });
  }
  document.addEventListener('click', function(e){
    if(!e.target||!e.target.closest) return;
    var cl=e.target.closest('[data-ack-clear]');
    if(cl){
      if(!writer){ window.alert(NOTICE||'本页只读，请先点击编辑接管'); return; }
      Object.keys(DATA.acks).forEach(function(k){ setAck(k,false); });
      changed(); paint(); autoPush(); return;
    }
    var el=e.target.closest('[data-ack]');
    if(!el) return;
    e.preventDefault(); e.stopPropagation();
    var k=el.getAttribute('data-ack');
    if(!writer){ window.alert(NOTICE||'本页只读，请先点击编辑接管'); return; }
    if(!validAckKey(k)) return;
    setAck(k,!DATA.acks[k]); changed(); paint(); autoPush();
  });
  /* 键盘：已读按钮本身就是 <button>，Enter / 空格由浏览器直接触发，不需要额外接管
     （胶囊不再承担点击已读，所以这里不再处理 .lvtag） */

  /* ---------- 1.9 云端同步：GitHub API 写 data 分支的 position-costs.json → 其它设备读回 ---------- */
  /* 10-20 起持仓**不再写 main**：main 的每个提交都会触发一次 Pages 构建，同步频繁时会互相顶掉，
     站点就一直发不出去（10-19 事故）。data 分支不参与 Pages 发布，写多少次都不影响站点。 */
  var GH={owner:'nixhuang', repo:'market-monitor', path:'position-costs.json', branch:'data'};
  var GH_API='https://api.github.com/repos/'+GH.owner+'/'+GH.repo+'/contents/'+GH.path;
  var LS_TOKEN='mm_gh_token_v1';
  /* 本地 dirty 与 revision 一起原子保存；只有确认对应快照且成功落盘才清除。
     缓存用于恢复，但未经本轮远端核验不能作为“无需上传”的依据。 */
  var CLOUD=null, cloudVerified=false, SYNC='synced', SYNC_MSG='';
  function ghToken(){
    if(isolated) return '';
    try{ return (localStorage.getItem(LS_TOKEN)||'').trim(); }catch(e){ return ''; }
  }
  function ghHead(json){
    var t=ghToken(); if(!t) return null;
    var h={Authorization:'Bearer '+t, Accept:'application/vnd.github+json', 'X-GitHub-Api-Version':'2022-11-28'};
    if(json) h['Content-Type']='application/json';
    return h;
  }
  function b64(str){
    var bytes=new TextEncoder().encode(str), s='';
    for(var i=0;i<bytes.length;i++) s+=String.fromCharCode(bytes[i]);
    return btoa(s);
  }
  function unb64(str){
    var bin=atob(String(str||'').replace(/\s+/g,'')), bytes=new Uint8Array(bin.length);
    for(var i=0;i<bin.length;i++) bytes[i]=bin.charCodeAt(i);
    return new TextDecoder().decode(bytes);
  }
  var REQUEST_DEADLINE=60000, activeRequests=[];
  function requestJson(url,options){
    return new Promise(function(resolve,reject){
      var controller=typeof AbortController==='function'?new AbortController():null;
      var finished=false, timer;
      function finish(error,result){
        if(finished) return;
        finished=true; clearTimeout(timer);
        activeRequests=activeRequests.filter(function(x){ return x!==cancel; });
        if(error){ if(controller) controller.abort(); reject(error); } else resolve(result);
      }
      function cancel(){ finish(new Error('页面已退出，请求已取消')); }
      activeRequests.push(cancel);
      timer=setTimeout(function(){ finish(new Error('同步请求超时（含响应体读取），请重试核验')); },REQUEST_DEADLINE);
      var init=Object.assign({},options||{});
      if(controller) init.signal=controller.signal;
      Promise.resolve().then(function(){
        if(finished) return;
        return fetch(url,init);
      }).then(function(response){
        if(finished || !response) return;
        return response.json().then(function(data){
          finish(null,{status:response.status,ok:response.ok,data:data});
        });
      }).catch(function(error){ finish(error); });
    });
  }
  /* 两个候选来源，按顺序试：
     ① 填了 token：GitHub API 直接读 data 分支 —— 没有 CDN 缓存，刚上传完立刻就是新的
        （raw 地址有约 5 分钟 CDN 缓存；本机「干净」时云端会全量覆盖本机，读到旧缓存就会把刚传的数字冲回去）；
     ② 没 token（只读设备）：raw.githubusercontent.com 的 data 分支，带时间戳。
     站点同目录那份已不再更新（main 里的副本已删除），不再读取。 */
  function cloudSources(){
    var list=[], h=ghHead();
    if(h) list.push(function(){
      return requestJson(GH_API+'?ref='+GH.branch+'&t='+Date.now(),{headers:h,cache:'no-store'})
        .then(function(r){ var m=r.ok?r.data:null; return m&&m.content?JSON.parse(unb64(m.content)):null; });
    });
    list.push(function(){
      return requestJson('https://raw.githubusercontent.com/'+GH.owner+'/'+GH.repo+'/'+GH.branch+'/'+GH.path+'?t='+Date.now(),
                   {cache:'no-store'}).then(function(r){ return r.ok?r.data:null; });
    });
    return list;
  }
  function pullCloud(cb){
    var us=cloudSources(), i=0;
    (function next(){
      if(disposed || !writer) return;
      if(i>=us.length){ cb(null); return; }
      us[i++]().then(function(j){ if(j&&j.items) cb(j); else next(); })
        .catch(function(){ next(); });
    })();
  }
  /* data 分支读取遇到弱网时最多尝试三轮；提示条的“重试”可重开一轮，不补传种子。 */
  var _pullTry=0, _pullTimer=null;
  function startPull(){
    if(isolated || !writer || disposed) return;
    var generation=++pullGeneration, rev=revision;
    pullCloud(function(j){
      if(disposed || !writer || generation!==pullGeneration || rev!==revision) return;
      if(j){
        try{ mergeCloud(j); _pullTry=0; SYNC=inFlight?'loading':(dirty?'local':'synced'); }
        catch(e){ SYNC='nocloud'; NOTICE='云端数据校验失败：'+e.message; }
      }else{
        SYNC=inFlight?'loading':(dirty?'local':'nocloud');
        if(_pullTry<2){ _pullTry++; _pullTimer=setTimeout(startPull,_pullTry===1?5000:20000); }
      }
      // 拉取失败不上传内置快照；打开页面不是一次用户保存操作。
      paint();
    });
  }
  function pushCloud(sent, cb){
    var h=ghHead(true);
    if(!h){ cb(false,'未填 token'); return; }
    requestJson(GH_API+'?ref='+GH.branch+'&t='+Date.now(),{headers:ghHead(),cache:'no-store'})
      .then(function(r){
        if(r.status===404) return null;
        if(!r.ok) throw new Error('读取远端版本失败：HTTP '+r.status);
        return r.data;
      }).then(function(meta){
        if(disposed || !writer) throw new Error('页面已退出，上传已取消');
        var remote=meta&&meta.content?normalize(JSON.parse(unb64(meta.content))):null;
        if(sent.recovery){
          if(meta && (!remote || !meta.sha)) throw new Error('上传结果未知，远端内容及 SHA 未完整核验，暂停重传');
          if(remote && canonical(remote)===canonical(sent.payload)){
            cb(true,'已核验上次上传',remote); return null;
          }
          if((meta?meta.sha:null)!==sent.baseSha){
            // 远端已换版本：迟到旧 PUT 的基准 SHA 已失效，才允许释放旧事务、排队新版本。
            cb(false,'远端已更新，上次上传事务结束',remote,'superseded'); return null;
          }
          // SHA 未变时只允许重发同一 payload / 同一 SHA。先完成旧事务，不夹带新编辑。
        }else if(sent.maintenanceOnly){
          if(!remote || !meta.sha) throw new Error('ACK 自动维护等待有效远端快照，不上传未核验本地数值');
          var maintained=clone(remote); reconcileAcks(maintained,sent.payload);
          if(revision===sent.revision && maintenanceOnly && !editing){
            DATA=clone(maintained); revision++; sent.revision=revision; persist();
          }
          sent.payload=maintained;
        }
        if(remote && canonical(remote)===canonical(sent.payload)){
          cb(true,'内容已一致',remote); return null;
        }
        if(meta && !meta.sha) throw new Error('缺少远端 SHA，禁止覆盖');
        // 在网络发送前原子记录未确认事务；超时、退出、刷新后都先核验这一 payload。
        uncertain={payload:clone(sent.payload),revision:sent.revision,
          baseSha:sent.recovery?sent.baseSha:(meta?meta.sha:null),maintenanceOnly:!!sent.maintenanceOnly};
        if(!persist()) throw new Error('上传事务尚未持久化，暂停上传');
        if(!write(LS_PUSH_AT,Date.now())) throw new Error('上传节流状态未持久化');
        var body={message:'持仓同步 '+new Date().toISOString().slice(0,19).replace('T',' '),
          content:b64(JSON.stringify(sent.payload,null,1)),branch:GH.branch};
        if(uncertain.baseSha) body.sha=uncertain.baseSha;
        return requestJson(GH_API,{method:'PUT',headers:h,body:JSON.stringify(body)});
      }).then(function(r){
        if(!r) return;
        var ok=r.status===200||r.status===201;
        cb(ok,ok?'已上传':((r.data&&r.data.message)||('HTTP '+r.status)));
      }).catch(function(e){ cb(false,e.message||'网络失败'); });
  }
  /* clean 仍按云端全量替换；dirty 不混入任何远端数字。撤销记录不做并集复活。 */
  function mergeCloud(j){
    if(isolated || !j || !writer || disposed) return false;
    var remote=normalize(j);
    if(inFlight) return false;
    CLOUD=remote; cloudVerified=true;
    var cached=write(LS_CLOUD,remote);
    if((dirty && !maintenanceOnly) || editing || uncertain) return false;
    var next=clone(remote); reconcileAcks(next,DATA);
    var different=canonical(next)!==canonical(DATA);
    if(different){ DATA=next; revision++; }
    maintenancePending=canonical(next)!==canonical(remote);
    maintenanceOnly=maintenancePending;
    dirty=!cached || maintenancePending;
    if(!persist()) dirty=true;
    if(maintenancePending) autoPush();
    return different;
  }
  /* 自动、手动、重试只进入这一条队列。data 分支不参与 Pages；节流用于减少重复提交。 */
  var _pushTimer=null, _retryN=0, _retryTimer=null, inFlight=null, queued=false, savePaused=false;
  function clearSaveTimers(){
    if(_pushTimer) clearTimeout(_pushTimer);
    if(_retryTimer) clearTimeout(_retryTimer);
    _pushTimer=null; _retryTimer=null; queued=false;
  }
  function sameAsCloud(){ return !uncertain && cloudVerified && !!CLOUD && canonical(DATA)===canonical(CLOUD); }
  function requestSave(delay,resetRetry){
    if(!writer || disposed) return;
    if(isolated){ persist(); SYNC='local'; paint(); return; }
    if(resetRetry){ _retryN=0; if(_retryTimer) clearTimeout(_retryTimer); _retryTimer=null; }
    queued=true;
    if(!ghToken()){ SYNC='local'; SYNC_MSG='未填 GitHub token'; paint(); return; }
    if(editing || inFlight || savePaused) return;
    if(_pushTimer) clearTimeout(_pushTimer);
    _pushTimer=setTimeout(function(){ _pushTimer=null; doPush(); },delay||0);
  }
  function doPush(){
    if(!writer || disposed || editing || savePaused || inFlight || !queued || (!dirty && !uncertain)) return;
    if(maintenanceOnly && !cloudVerified && !uncertain) return;
    if(!ghToken()) return;
    // 先重试本地原子落盘；失败时只允许用户导出，不冒充保存成功。
    if(!persist()){ paint(); return; }
    if(sameAsCloud()){
      if(!write(LS_CLOUD,CLOUD)){ paint(); return; }
      queued=false; _retryN=0; SYNC=setPending(false)?'synced':'local'; paint(); return;
    }
    var last=read(LS_PUSH_AT), wait=typeof last==='number'?30000-(Date.now()-last):0;
    if(storageErrors[LS_PUSH_AT]){ paint(); return; }
    if(wait>0){ _pushTimer=setTimeout(function(){ _pushTimer=null; doPush(); },wait); return; }
    if(!write(LS_PUSH_AT,Date.now())){ paint(); return; }
    // 在任何 await/GET 之前冻结 payload；之后只确认这一 revision，不读取回调时的 DATA。
    var sent=uncertain?Object.assign(clone(uncertain),{recovery:true}):
      {payload:clone(DATA),revision:revision,maintenanceOnly:maintenanceOnly};
    inFlight=sent; queued=false; pullGeneration++;
    SYNC='loading'; paint();
    pushCloud(sent,function(ok,msg,verifiedRemote,outcome){
      if(inFlight!==sent) return;
      inFlight=null;
      if(disposed){
        if(releaseWriter){ writer=false; releaseWriter(); releaseWriter=null; }
        return;
      }
      if(outcome==='superseded'){
        uncertain=null; cloudVerified=false; dirty=true; persist();
        SYNC='local'; SYNC_MSG='';
        if(maintenanceOnly){ if(verifiedRemote) mergeCloud(verifiedRemote); else startPull(); }
        requestSave(0,false);
      }else if(ok){
        uncertain=null;
        CLOUD=verifiedRemote||sent.payload; cloudVerified=true;
        var cached=write(LS_CLOUD,CLOUD);
        _retryN=0; SYNC_MSG='';
        if(cached && revision===sent.revision && canonical(DATA)===canonical(sent.payload)){
          SYNC=setPending(false)?'synced':'local';
        }else { dirty=true; persist(); SYNC='local'; }
        if(dirty) requestSave(0,false);
      }else{
        if(uncertain) cloudVerified=false;
        dirty=true; persist(); SYNC='error';
        SYNC_MSG=(msg||'上传失败')+(uncertain?'；PUT 结果未确认，重试将先核验远端':'');
        scheduleRetry();
      }
      paint();
    });
  }
  function autoPush(){ requestSave(1200,false); }
  function scheduleRetry(){
    if(_retryN>=2 || !ghToken() || disposed) return;
    _retryN++;
    if(_retryTimer) clearTimeout(_retryTimer);
    _retryTimer=setTimeout(function(){ _retryTimer=null; requestSave(0,false); },_retryN===1?15000:60000);
  }

  /* ---------- 2. 版本化状态：0 是值；仅旧版本确实缺少的字段做一次迁移 ---------- */
  var SCHEMA_VERSION=2, LS_PENDING='mm_position_pending_v1';
  var LS_PUSH_AT='mm_position_last_push_v2', LOCK_NAME='mm-position-writer-v2';
  function object(v){ return v!==null && typeof v==='object' && !Array.isArray(v); }
  function stamp(v,field){
    if(typeof v!=='string' || !/^\d{4}-\d\d-\d\d(?:[T ]\d\d:\d\d(?::\d\d(?:\.\d{1,3})?)?(?:Z|[+-]\d\d:\d\d)?)?$/.test(v)
       || !isFinite(Date.parse(v))
       || new Date(v.slice(0,10)+'T00:00:00Z').toISOString().slice(0,10)!==v.slice(0,10))
      throw new Error(field+' 必须是有效日期');
    return v;
  }
  function amount(o,k,fallback){
    var v=own(o,k)?o[k]:fallback;
    if(typeof v!=='number' || !isFinite(v) || v<0) throw new Error(k+' 必须是非负有限数值');
    return v;
  }
  function validAckKey(k){ return /^[A-Z0-9.^=_-]{1,32}\|(?:loss10|loss20|(?:add|cut)\|\d+(?:\.\d+)?)$/.test(k); }
  function normalize(j,seed){
    if(!object(j)||!object(j.items)) throw new Error('items 必须是对象');
    var version=own(j,'schema_version')?j.schema_version:1;
    if(version!==1 && version!==SCHEMA_VERSION) throw new Error('不支持的 schema_version');
    if(own(j,'real') && typeof j.real!=='boolean') throw new Error('real 必须是布尔值');
    var base=version===1 && seed?seed:{};
    var d={schema_version:SCHEMA_VERSION,real:true,
      updated_at:own(j,'updated_at')?stamp(j.updated_at,'updated_at'):new Date().toISOString(),
      cash:amount(j,'cash',num(base.cash)),fixed:amount(j,'fixed',num(base.fixed)),
      fixed_cost:amount(j,'fixed_cost',num(base.fixed_cost)),items:{},acks:{},ack_tombstones:{}};
    Object.keys(j.items).forEach(function(k){
      if(!/^[A-Z0-9.^=_-]{1,32}$/.test(k) || !object(j.items[k])) throw new Error('非法标的或持仓对象：'+k);
      var b=(base.items&&base.items[k])||{};
      d.items[k]={cost:amount(j.items[k],'cost',num(b.cost)),qty:amount(j.items[k],'qty',num(b.qty))};
    });
    ['acks','ack_tombstones'].forEach(function(f){
      if(own(j,f) && !object(j[f])) throw new Error(f+' 必须是对象');
      Object.keys(j[f]||{}).forEach(function(k){
        if(!validAckKey(k)) throw new Error('非法已读规则：'+k);
        d[f][k]=stamp(j[f][k],f);
      });
    });
    reconcileAcks(d,d);
    return d;
  }
  function reconcileAcks(target,other){
    Object.keys(other.ack_tombstones||{}).forEach(function(k){
      var t=other.ack_tombstones[k], old=target.ack_tombstones[k];
      if(!old || Date.parse(t)>Date.parse(old)) target.ack_tombstones[k]=t;
    });
    Object.keys(target.acks).forEach(function(k){
      var t=target.ack_tombstones[k];
      if(t && Date.parse(t)>=Date.parse(target.acks[k])) delete target.acks[k];
    });
  }
  function canonical(d){
    function sorted(v){
      if(!object(v)) return v;
      var o={}; Object.keys(v).sort().forEach(function(k){ o[k]=sorted(v[k]); }); return o;
    }
    return JSON.stringify(sorted({items:d.items,cash:d.cash,fixed:d.fixed,fixed_cost:d.fixed_cost,
      acks:d.acks||{},ack_tombstones:d.ack_tombstones||{}}));
  }
  var local=read(LS_LOCAL), cloud=read(LS_CLOUD);
  var hadOwn=!!(local&&object(local.items));
  var seedAsOf=(REAL&&REAL.asof)?String(REAL.asof).slice(0,10):'';
  var DATA, revision=0, dirty=false, writer=false, releaseWriter=null, acquiring=false, disposed=false;
  var ACK_PRUNED=false, pullGeneration=0;
  var maintenancePending=false, maintenanceOnly=false, uncertain=null;
  function loadLocal(){
    var j=read(LS_LOCAL);
    try{
      if(j){ DATA=normalize(j,REAL); hadOwn=true; }
      else DATA=normalize(cloud||REAL||{items:{}});
      revision=j&&j._local&&Number.isSafeInteger(j._local.revision)?j._local.revision:0;
      dirty=!!(j&&j._local?j._local.dirty:read(LS_PENDING)===1);
      maintenancePending=!!(j&&j._local&&j._local.maintenancePending);
      maintenanceOnly=!!(j&&j._local&&j._local.maintenanceOnly);
      // 纯 ACK 维护跨刷新后仍须先取远端数值，不把它迁移成用户整份待上传。
      if(maintenanceOnly) dirty=false;
      uncertain=null;
      var u=j&&j._local&&j._local.uncertain;
      if(u){
        if(!object(u) || (u.baseSha!==null && (typeof u.baseSha!=='string'||!u.baseSha))
          || !Number.isSafeInteger(u.revision)) throw new Error('未确认上传事务无效');
        uncertain={payload:normalize(u.payload),baseSha:u.baseSha,revision:u.revision,maintenanceOnly:!!u.maintenanceOnly};
        dirty=true;
      }
      CLOUD=cloud?normalize(cloud):null; cloudVerified=false;
      if(storageErrors[LS_LOCAL] || storageErrors[LS_PENDING]){ dirty=true; return false; }
      return true;
    }catch(e){
      storageErrors[LS_LOCAL]='本地数据校验失败：'+e.message+'；原数据未覆盖，请导出或导入有效备份';
      if(!DATA) DATA=normalize(REAL||{items:{}});
      dirty=true; return false;
    }
  }
  loadLocal();
  function persist(){
    if(!writer) return false;
    // 数据、revision、dirty 同一原子写入，避免两个 localStorage key 之间刷新产生分裂状态。
    var saved=clone(DATA); saved._local={revision:revision,dirty:dirty,
      maintenancePending:maintenancePending,maintenanceOnly:maintenanceOnly,uncertain:uncertain};
    return write(LS_LOCAL,saved);
  }
  function setPending(v){
    dirty=!!v;
    if(!dirty){ maintenancePending=false; maintenanceOnly=false; }
    if(!persist()){ dirty=true; return false; }
    return true;
  }
  function changed(){
    maintenanceOnly=false;
    revision++; pullGeneration++; DATA.updated_at=new Date().toISOString(); dirty=true;
    SYNC='local'; SYNC_MSG=''; persist();
  }
  function setAck(k,on){
    var at=Math.max(Date.now(),Date.parse(DATA.acks[k]||'')+1||0,
      Date.parse(DATA.ack_tombstones[k]||'')+1||0);
    if(on) DATA.acks[k]=new Date(at).toISOString();
    else { delete DATA.acks[k]; DATA.ack_tombstones[k]=new Date(at).toISOString(); }
  }
  function pruneAck(k){
    if(writer && DATA.acks[k]){ setAck(k,false); ACK_PRUNED=true; }
  }
  function persistMaintenance(){
    ACK_PRUNED=false; maintenancePending=true;
    if(!dirty || maintenanceOnly){
      maintenanceOnly=true;
      // 尚未核验时只落盘 tombstone，不增加用户 revision，也不使初始 pull 失效。
      if(cloudVerified){ revision++; dirty=true; }
      persist();
    }else changed();
    if(dirty) autoPush();
  }
  function takeOwnership(cb){
    if(writer){ cb(true); return; }
    if(acquiring||disposed){ cb(false); return; }
    if(isolated){
      writer=true; cloud=null;
      if(loadLocal()) persist();
      cb(true); return;
    }
    acquiring=true;
    navigator.locks.request(LOCK_NAME,{mode:'exclusive',ifAvailable:true},function(lock){
      acquiring=false;
      if(!lock || disposed){
        NOTICE='另一标签页正在管理仓位，本页只读；关闭该页后点击编辑接管'; cb(false); return;
      }
      writer=true; pullGeneration++;
      cloud=read(LS_CLOUD); var valid=loadLocal();
      NOTICE='';
      return new Promise(function(resolve){
        releaseWriter=resolve;
        if(valid) persist();
        cb(true);
      });
    }).catch(function(){
      acquiring=false; writer=false; releaseWriter=null;
      NOTICE='无法保持跨标签页写锁，本页只读'; cb(false);
    });
  }
  window.addEventListener('storage',function(e){
    if(isolated || writer || disposed || (e.key!==LS_LOCAL && e.key!==LS_CLOUD)) return;
    cloud=read(LS_CLOUD); loadLocal(); pullGeneration++;
    SYNC=dirty?'local':'synced'; paint();
  });
  window.addEventListener('pagehide',function(){
    disposed=true; pullGeneration++; clearSaveTimers();
    editing=false; snapshot=null; savePaused=true; editGeneration++;
    if(_pullTimer) clearTimeout(_pullTimer);
    if(_paintFrame){ cancelAnimationFrame(_paintFrame); _paintFrame=null; }
    // PUT 发送前已原子记录 uncertain。退出可立即取消客户端等待并释放锁；
    // 接管页只会核验/重发同一 SHA 事务，不会把取消误认为服务器没有写入。
    activeRequests.slice().forEach(function(cancel){ cancel(); });
    inFlight=null;
    if(isolated) writer=false;
    if(releaseWriter){ writer=false; releaseWriter(); releaseWriter=null; }
  });
  window.addEventListener('pageshow',function(e){
    if(!e.persisted) return;
    disposed=false; syncEditUI(); takeOwnership(function(ok){ if(ok) startPull(); paint(); });
  });

  /* ---------- 3. 「仓位管理」独立面板（统计面板上方；编辑功能内置） ---------- */
  var sc=document.getElementById('signalCounts'); if(!sc) return;
  var PID='costMembers';
  var panel=document.createElement('details');
  // 跟统计 / 持仓 / 市场风险完全同级：card + group-card，底部一条整宽收起条
  panel.className='card group-card cost-manage';
  panel.id='costManage';
  // 跟其它分组一致：默认折叠（全站只有「统计」默认展开）
  panel.innerHTML='<summary class="grp">'
    +'<span class="grp-name">仓位管理</span>'
    // 顺序：仓位管理 → 已同步/未同步 → 总资产·盈亏
    // （「20日对.SPX」不放这里，改挂到「持仓」卡标题右侧，跟其他板块同一位置）
    +'<span class="group-stats">'
    +'<span class="cost-state" id="costState"></span>'
    +'<span class="cost-sum"></span>'
    +'</span>'
    // 「编辑」放在表头（标题行）最右侧：跟 10-19 定的标题行口径一致
    // （仓位管理 → 已同步/未同步 → 总资产·盈亏 → 最右「编辑」）
    +'<button type="button" class="cost-edit-btn" id="costEditBtn" aria-pressed="false"'
    +' aria-label="就地编辑成本价与数量">编辑</button></summary>'
    +'<div class="cost-body">'
    +'<div class="signal-members-panel" id="'+PID+'"></div>'
    +'<div class="cost-acts" id="costActs" hidden>'
    +'<button type="button" class="primary" id="costSave">保存并上传</button>'
    +'<button type="button" id="costCancel">取消</button>'
    +'<button type="button" id="costExport">导出</button>'
    +'<button type="button" id="costImport">导入</button>'
    +'<span class="hint">成本价与数量可填到小数点后四位；现金填一个总额（券商余额＋货币市场 ETF）</span>'
    +'</div>'
    // 文件选择框放在 acts 外面：没进编辑态也要能用「导入备份」救急
    +'<input type="file" id="costFile" accept=".json" hidden>'
    +'<div class="cost-foot" id="costFoot"></div>'
    +'</div>'
    +'<div class="group-bottom"><button type="button" class="collapse-group"'
    +' aria-controls="costManage" aria-label="收起仓位管理">'
    +'<span class="collapse-icon" aria-hidden="true">▲</span></button></div>';
  sc.parentNode.insertBefore(panel, sc);
  var stateEl=panel.querySelector('#costState');
  var footEl=panel.querySelector('#costFoot');
  var actsEl=panel.querySelector('#costActs');
  var editBtn=panel.querySelector('#costEditBtn');

  /* 排序：只能按「盈亏」或「仓位」排，固定降序，点一次切换，选择存在本机、下次打开自动保持 */
  var LS_SORT='mm_position_sort_v1';
  var sortKey=(read(LS_SORT)==='wt')?'wt':'pnl';
  // 只为核对：?sort=wt / ?sort=pnl 直接切一次并写进本机存储（等价于点一次列头）
  var sq=(location.search.match(/sort=(\w+)/)||[])[1];
  if(sq==='wt'||sq==='pnl'){ sortKey=sq; write(LS_SORT,sq); }
  function colhead(){
    function h(k,label){
      var on=(sortKey===k);
      // 只有当前生效的那一列才画 ▼，另一列只留可点的文字
      return '<span class="sortable'+(on?' on':'')+'" data-sort="'+k+'"'
        +' role="button" tabindex="0" aria-label="按'+label+'降序排列">'+label
        +(on?'<span class="arw">▼</span>':'')+'</span>';
    }
    return '<div class="cost-colhead"><span>标的</span><span>成本价</span>'
      +'<span>数量</span><span>持仓市值</span>'+h('pnl','盈亏')+h('wt','仓位')+'</div>';
  }
  function setSort(k){
    if(k!=='pnl' && k!=='wt') return;
    if(sortKey===k) return;
    sortKey=k; write(LS_SORT,k); paint();
  }
  // 列头是 paint 每次重画的，所以绑在面板上做事件委托
  panel.addEventListener('click', function(e){
    var t=e.target && e.target.closest ? e.target.closest('.sortable') : null;
    if(t) setSort(t.getAttribute('data-sort'));
  });
  /* 「云端未取到」提示条上的两个出口（paint 每次重画，所以走事件委托）：
     重试＝重开一轮拉取；导入备份＝选一个从别的设备导出的 position-costs.json */
  panel.addEventListener('click', function(e){
    var t=e.target && e.target.closest ? e.target.closest('#costRetry,#costImportBak') : null;
    if(!t) return;
    e.preventDefault(); e.stopPropagation();
    if(t.id==='costRetry'){
      _pullTry=0; if(_pullTimer){ clearTimeout(_pullTimer); _pullTimer=null; }
      SYNC='pulling'; paint(); startPull(); return;
    }
    var f=panel.querySelector('#costFile'); if(f) f.click();
  });
  panel.addEventListener('keydown', function(e){
    if(e.key!=='Enter' && e.key!==' ') return;
    var t=e.target && e.target.closest ? e.target.closest('.sortable') : null;
    if(!t) return;
    e.preventDefault(); setSort(t.getAttribute('data-sort'));
  });

  /* ---------- 4. 编辑态：点「编辑」就地改，点「保存并上传」落盘并同步 ---------- */
  var editing=false, snapshot=null, prevDirty=false, editOrder={}, editGeneration=0;
  var prevMaintenanceOnly=false, prevMaintenancePending=false;
  var _paintFrame=null;
  function syncEditUI(){
    editBtn.textContent=editing?'编辑中':'编辑';
    editBtn.setAttribute('aria-pressed', editing?'true':'false');
    if(editing) editBtn.className='cost-edit-btn on'; else editBtn.className='cost-edit-btn';
    // 编辑中由下面的「保存并上传／取消」接管，表头这颗收起来，免得出现两个「退出」入口
    editBtn.hidden=editing;
    actsEl.hidden=!editing;
    panel.querySelector('#costSave').textContent=isolated?'保存到本标签页':'保存并上传';
  }
  function enterEdit(){
    if(editing || disposed) return;
    takeOwnership(function(ok){
      if(!ok){ paint(); return; }
      clearSaveTimers(); pullGeneration++; editGeneration++;
      snapshot=clone(DATA); prevDirty=dirty;
      prevMaintenanceOnly=maintenanceOnly; prevMaintenancePending=maintenancePending;
      editOrder={};
      Array.prototype.forEach.call(document.querySelectorAll('#'+PID+' li[data-sym]'),
        function(li,i){ editOrder[li.getAttribute('data-sym')]=i; });
      editing=true; panel.open=true; syncEditUI(); paint();
      var first=panel.querySelector('#'+PID+' input'); if(first) first.focus();
    });
  }
  function exitEdit(restore){
    clearSaveTimers(); editGeneration++; pullGeneration++;
    if(restore && snapshot && canonical(DATA)!==canonical(snapshot)){
      var draft=DATA; DATA=clone(snapshot);
      // 取消 ACK 编辑同样是一笔反向操作，不恢复已失效的旧版本戳。
      reconcileAcks(DATA,draft);
      Object.keys(Object.assign({},draft.acks,snapshot.acks)).forEach(function(k){
        if(!!draft.acks[k]!==!!snapshot.acks[k]) setAck(k,!!snapshot.acks[k]);
      });
      changed();
      if(!inFlight && !uncertain){
        dirty=prevDirty; maintenanceOnly=prevMaintenanceOnly; maintenancePending=prevMaintenancePending; persist();
      }
    }
    editing=false; snapshot=null; savePaused=false; syncEditUI(); paint();
    if(maintenanceOnly && !cloudVerified) startPull();
    if(dirty || uncertain) requestSave(0,false);
  }
  editBtn.addEventListener('click', function(e){
    e.preventDefault(); e.stopPropagation();   // 别让点击冒泡上去把整张卡折叠掉
    if(editing) exitEdit(true); else enterEdit();
  });
  panel.querySelector('#costSave').addEventListener('click', function(){
    if(!writer || disposed) return;
    if(panel.querySelector('input:invalid')){ NOTICE='存在无效输入，请修正后保存'; stateEl.textContent=NOTICE; return; }
    // 保存立即结束本轮编辑；异步回执绝不关闭后来开始的编辑会话。
    editing=false; snapshot=null; savePaused=false; editGeneration++; pullGeneration++;
    if(!dirty && !sameAsCloud() && !maintenanceOnly) changed();
    persist(); syncEditUI();
    if(maintenanceOnly && !cloudVerified) startPull();
    requestSave(0,true); paint();
  });
  panel.querySelector('#costCancel').addEventListener('click', function(){ exitEdit(true); });
  // 点标题右侧的「未同步 / 已同步」→ 填 / 改 GitHub token（只存本机 localStorage，每台设备填一次）
  panel.addEventListener('click', function(e){
    var st=e.target&&e.target.closest?e.target.closest('#costState'):null;
    if(!st) return;
    e.preventDefault(); e.stopPropagation();     // 别让点击把整张卡折叠掉
    if(Object.keys(storageErrors).length){
      window.alert(Object.keys(storageErrors).map(function(k){ return storageErrors[k]; }).join('\n')); return;
    }
    if(!writer){ window.alert(NOTICE||'本页只读，请点击编辑接管'); return; }
    if(isolated){ window.alert(ISOLATED_NOTICE); return; }
    if(SYNC==='error'){
      window.alert('上传失败：'+(SYNC_MSG||'未知原因')+'\n可点击提示栏的“重试上传”；本机状态与导出备份仍可使用。');
      return;
    }
    var cur=ghToken();
    var v=window.prompt(
      'GitHub token（只存在这台设备的浏览器里，不进仓库、不上传）\n'
      +'权限：Fine-grained token → 仓库选 market-monitor → Contents 读+写\n'
      +'留空并确定＝取消自动同步', cur||'');
    if(v===null) return;
    try{
      if(v.trim()) localStorage.setItem(LS_TOKEN, v.trim());
      else localStorage.removeItem(LS_TOKEN);
      delete storageErrors[LS_TOKEN];
    }catch(err){ storageErrors[LS_TOKEN]='token 本地保存失败'; paint(); return; }
    SYNC_MSG='';
    // 换设备刚填完 token：立刻重开一轮云端拉取（原来只补传，取不到就是取不到）
    _pullTry=0; if(_pullTimer){ clearTimeout(_pullTimer); _pullTimer=null; }
    SYNC='pulling'; startPull();
    if(dirty){ SYNC='local'; autoPush(); }   // 还有没传上去的改动，顺手补传
    paint();
  });
  // 卡片被收起时自动退出编辑态：已敲进去的数字保留（变成「未同步」），不偷偷丢
  panel.addEventListener('toggle', function(){
    if(!panel.open && editing){
      // 折叠保留每次输入已落盘的草稿，但不替用户执行保存上传。
      clearSaveTimers(); editing=false; snapshot=null; savePaused=true; editGeneration++; pullGeneration++;
      syncEditUI(); paint();
    }
  });
  function exportData(){
    var blob=new Blob([JSON.stringify(DATA,null,1)],{type:'application/json'});
    var a=document.createElement('a'), url=URL.createObjectURL(blob);
    a.href=url; a.download='position-costs.json'; a.click();
    setTimeout(function(){ URL.revokeObjectURL(url); },0);
  }
  panel.querySelector('#costExport').addEventListener('click',exportData);
  panel.querySelector('#costImport').addEventListener('click', function(){
    panel.querySelector('#costFile').click();
  });
  function importData(j){
    var next=normalize(j);
    if(!writer || disposed) throw new Error('本页只读，请先点击编辑接管');
    clearSaveTimers(); pullGeneration++; editGeneration++;
    DATA=next; changed(); hadOwn=true;
    editing=false; snapshot=null; savePaused=false; syncEditUI();
    NOTICE=storageErrors[LS_LOCAL]?'导入尚未持久化，请立即导出备份':
      (isolated?'导入已保存到当前标签页会话；隔离预览不上传云端':
        (ghToken()?'导入已保存到本机，已加入上传队列':'导入已保存到本机；未填 token，尚未上传'));
    requestSave(0,true); paint();
  }
  panel.querySelector('#costFile').addEventListener('change', function(e){
    var file=e.target.files&&e.target.files[0]; if(!file) return;
    var input=e.target, fr=new FileReader(), generation=editGeneration, rev=revision;
    input.value='';
    fr.onload=function(){
      if(disposed || generation!==editGeneration || rev!==revision){
        NOTICE='读取备份期间数据或编辑会话已变化，请重新选择文件'; paint(); return;
      }
      try{ importData(JSON.parse(fr.result)); }
      catch(err){ NOTICE='导入失败：'+err.message+'；现有数据未修改'; paint(); }
    };
    fr.onerror=function(){ NOTICE='导入失败：无法读取文件'; paint(); };
    fr.readAsText(file);
  });

  /* 每次有效输入同步落盘，只把重算合并到下一帧；paint 统一保存当前输入值和完整选区。 */
  panel.addEventListener('input', function(e){
    var el=e.target;
    if(!writer || !editing || disposed || !el || !el.getAttribute) return;
    var f=el.getAttribute('data-f'), sym=el.getAttribute('data-sym');
    if(['cash','fixed','fixed_cost','cost','qty'].indexOf(f)<0) return;
    if((f==='cost'||f==='qty') && !ROWS.some(function(r){ return r.sym===sym && isPosition(r); })) return;
    var raw=el.value.trim(), value=raw===''?0:Number(raw);
    if(!/^(?:\d+(?:\.\d{0,4})?|\.\d{1,4})?$/.test(raw) || !isFinite(value) || value<0){
      el.setCustomValidity('请输入非负数，最多四位小数');
      NOTICE='输入无效，保留上一次有效数值；请输入非负数，最多四位小数';
      if(stateEl) stateEl.textContent=NOTICE;
      return;
    }
    el.setCustomValidity(''); NOTICE='';
    if(f==='cash'||f==='fixed'||f==='fixed_cost') DATA[f]=value;
    else { if(!DATA.items[sym]) DATA.items[sym]={cost:0,qty:0}; DATA.items[sym][f]=value; }
    changed();
    if(!_paintFrame) _paintFrame=requestAnimationFrame(function(){ _paintFrame=null; paint(); });
  });

  function vt(v){ return trim(num(v)); }
  function inp(sym, f, val, ph){
    return '<input data-sym="'+esc(sym)+'" data-f="'+f+'" inputmode="decimal" step="0.0001"'
      +' value="'+esc(val)+'" placeholder="'+ph+'" aria-label="'+esc(sym)+' '+ph+'">';
  }
  function row(it, totalAsset){
    var c=it.pnlPct>=0?'up':'down';
    var wv=it.has&&totalAsset>0?it.mv/totalAsset*100:0;
    var w=it.has&&totalAsset>0?wv.toFixed(1)+'%':'—';
    /* 浮亏 10%／20%：盈亏列下面挂一行小字提醒（跟持仓个股行同一口径，不报灯） */
    var lvl=it.has&&it.pnlPct<=-10?(it.pnlPct<=-20?2:1):0;
    var lossTag=lvl?('<span class="loss-tag'+(lvl===2?' big':'')+'">亏损超'+(lvl===2?'20':'10')+'%</span>'):'';
    var marks=(it.isEtf?'<span class="tag">ETF</span>':'');
    var name='<span class="cost-name">'
      +'<span class="syrow"><span class="sy">'+esc(it.sym)+'</span>'+marks+'</span>'
      +(it.note?'<span class="cn">'+esc(it.note)+'</span>':'')
      +lossTag
      +'</span>';
    // 编辑态下名称不再做成跳转链接，免得点一下就跳走、改的东西还没保存
    var nameCell=editing?name
      :'<a class="signal-member-jump" href="#s-'+it.sym.toLowerCase()+'" style="min-width:0">'+name+'</a>';
    return '<li class="lv-'+(it.lv||'green')+'" data-sym="'+esc(it.sym)+'"><span class="cost-grid">'
      +nameCell
      +'<span class="cost-cell">'+(editing?inp(it.sym,'cost',vt(it.cost),'成本价'):trim(it.cost))+'</span>'
      +'<span class="cost-cell">'+(editing?inp(it.sym,'qty',vt(it.qty),'数量'):trim(it.qty))+'</span>'
      +'<span class="cost-cell'+(it.has?'':' dim')+'">'+(it.has?plain(it.mv):'—')+'</span>'
      +'<span class="cost-cell '+(it.has?c:'dim')+'">'
      +(it.has?('<span class="pct">'+pct(it.pnlPct)+'</span> '
        +'<span class="amt">'+money(it.pnlAmt)+'</span>'):'—')
      +'</span>'
      +'<span class="cost-cell wt'+(wv>10?' hi':'')+'">'+w+'</span></span></li>';
  }
  /* 框标题＝合计行：名称落在第 1 列，市值／盈亏／仓位各自落在第 4／5／6 列，
     跟下面列表共用同一套列宽（CSS 里的 .cost-block-title），右边缘对齐 */
  function titleRow(name, cnt, mv, pnl, wt, cls){
    var h='<h5 class="cost-block-title'+(cls?' '+cls:'')+'"><span class="t-name">'+esc(name);
    if(cnt) h+=' <span class="cnt">'+esc(cnt)+'</span>';
    h+='</span>';
    if(mv) h+='<span class="t-mv">'+mv+'</span>';
    if(pnl) h+='<span class="t-pnl">'+pnl+'</span>';
    if(wt) h+='<span class="t-wt">'+wt+'</span>';
    return h+'</h5>';
  }
  /* 排布：栏目名称那一排在上，带底色的合计行（框标题）紧跟在它下面，列表在最后。
     head===false → 这一框不画「标的／成本价／数量／持仓市值／盈亏／仓位」那一排。
     只在长持仓写一次：短持仓列头跟它完全一样，现金／固定收益各只有一行，写了就是废话。
     cls = 这一块的边框与列头底色（长持仓深绿 / 短持仓浅绿 / 现金青 / 固定收益橙）；
     extra = 插在栏目名称与合计行之间的附加行（长持仓上面那条「总持仓」） */
  function blockHtml(title, html, head, cls, extra){
    return '<div class="signal-member-block'+(cls?' '+cls:'')+'">'
      +(head===false?'':colhead())+(extra||'')+title
      +'<ul class="signal-member-list">'+html+'</ul></div>';
  }
  /* 现金 / 固定收益：整块就是一排带底色的信息，不再「分类名 + 下面再列一行」 */
  function miniHtml(cls, html){
    return '<div class="signal-member-block '+cls+'">'+html+'</div>';
  }
  function sumOf(a, f){ var s=0; a.forEach(function(x){ s+=f(x); }); return s; }
  /* 框标题＝合计行：只数 · 持仓市值 · 盈亏 · 仓位，各自压在对应的列上，绿涨红跌 */
  function block(name, arr, totalAsset, head, cls, extra){
    var mv=sumOf(arr,function(x){ return x.mv; });
    var pnl=sumOf(arr,function(x){ return x.pnlAmt; });
    var cst=sumOf(arr,function(x){ return x.cost*x.qty; });
    var pnlCls=pnl>=0?'up':'down';
    var wt=totalAsset>0?(mv/totalAsset*100).toFixed(1)+'%':'0.0%';
    /* 合计行的仓位不变黄：>10% 变黄是「单只过度集中」的提示，
       长／短合计、现金、固定收益都不是单只，黄了没有可执行的动作 */
    var title=titleRow(name, arr.length+' 只', plain(mv),
      // 顺序跟列表的盈亏列一致：先比例、再金额
      '<b class="'+pnlCls+'"><span class="pct">'+pct(cst>0?pnl/cst*100:0)+'</span> '
        +'<span class="amt">'+money(pnl)+'</span></b>', wt, '');
    return blockHtml(title, arr.map(function(r){ return row(r,totalAsset); }).join(''), head, cls, extra);
  }

  /* ---------- 5. 统一重绘 ---------- */
  function fmt1(n){ return (n>=0?'+':'')+n.toFixed(1); }

  /* 「20日对 .SPX」＝持仓组合 20 日收益 − 标普500 20 日收益（百分点），市值加权。
     每只的 20 日收益率 r 来自抓数产物（一天一次就够），数量在本机浏览器里：
     改数量／成本即时重算，不必重跑抓数。现金与固定收益不参与（跟标普没有可比性）。
       V1 = Σ q·P（P＝页面现价）   V0 = Σ q·P/(1+r)   R = V1/V0 − 1
     取不到 20 日收益率的标的整只剔除，并在悬停说明里点名，不拿别的数凑。 */
  /* 「20日对.SPX」挂在「持仓」卡标题的 .group-stats 里（跟 IT软硬Ai / 医疗保健 等板块
     完全同一位置、同一套 .rs-spy 样式），不再写在仓位管理表头上。找不到持仓卡就整段不显示。 */
  var rsEl=null;
  (function(){
    var st=document.querySelector('#group_positions>summary .group-stats');
    if(!st) return;
    rsEl=document.createElement('span');
    rsEl.className='rs-spy';
    st.appendChild(rsEl);
  })();
  function renderRs(){
    var el=rsEl;
    if(!el) return;
    el.className='rs-spy'; el.textContent=''; el.removeAttribute('title');
    var rs=(REAL && REAL.rs20) || null;
    if(!rs || !rs.items) return;
    var v1=0, v0=0, n=0, miss=[];
    ROWS.forEach(function(r){
      if(!isPosition(r)) return;
      var it=DATA.items[r.sym]; if(!it) return;
      var q=num(it.qty); if(!(q>0) || !(r.price>0)) return;
      var ri=rs.items[r.sym];
      if(typeof ri!=='number'){ miss.push(r.sym); return; }
      var mv=q*r.price; v1+=mv; v0+=mv/(1+ri/100); n++;
    });
    if(!n || !(v0>0)) return;
    var R=(v1/v0-1)*100, diff=R-num(rs.bench);
    // 与板块标题那一列同口径：不到 0.5 个百分点算持平，不着色
    var cls=diff>=0.5?'up':(diff<=-0.5?'down':'');
    var tip='持仓 20 日 '+fmt1(R)+'% − '+(rs.bench_name||'标普500')+' '+fmt1(num(rs.bench))+'%'
      +'（'+(rs.start||'')+'→'+(rs.end||'')+'，'+n+' 只按市值加权）'
      +(miss.length?'；'+miss.join('、')+' 无 20 日数据未计入':'');
    el.className='rs-spy'+(cls?' '+cls:'');
    el.textContent=(rs.days||20)+'日对.SPX '+fmt1(diff)+'%';
    el.setAttribute('aria-label',tip);
    el.setAttribute('aria-label',tip);
  }

  function paint(){
    if(!panel || disposed) return;
    if(_paintFrame){ cancelAnimationFrame(_paintFrame); _paintFrame=null; }
    var active=document.activeElement, focus=null;
    if(editing && active && panel.contains(active) && active.getAttribute('data-f')){
      focus={sym:active.getAttribute('data-sym'),field:active.getAttribute('data-f'),value:active.value,
        start:active.selectionStart,end:active.selectionEnd,direction:active.selectionDirection,
        error:active.validationMessage};
    }
    ACK_PRUNED=false;
    var positionCount=document.querySelectorAll('#group_positions tr[id^="s-"]').length;
    var positionName=document.querySelector('#group_positions>summary .grp-name');
    if(positionName) positionName.textContent='持仓 ('+positionCount+')';
    // 移组只让持仓亏损条件失效；成本/数量保留，加减仓 ACK 不受组别影响。
    Object.keys(DATA.acks).forEach(function(k){
      if(/\|loss(?:10|20)$/.test(k) && !ROWS.some(function(r){
        return r.sym===k.split('|')[0] && isPosition(r);
      })) pruneAck(k);
    });
    var cash=num(DATA.cash);            // 现金总额（券商余额＋货币市场 ETF，一个数字）
    var fixed=num(DATA.fixed);          // 固定收益市值
    var fixedCost=num(DATA.fixed_cost);
    // 固定收益的盈亏（按券商给的总成本算），表头的总盈亏要把它算进来
    var fPnl=(fixed>0&&fixedCost>0)?fixed-fixedCost:0;
    var fPct=(fixedCost>0&&fixed>0)?fPnl/fixedCost*100:0;
    var fCls=fPnl>=0?'up':'down';
    var longs=[], shorts=[];
    var totalMv=0, totalPnl=0, totalCost=0;
    ROWS.forEach(function(r){
      var it=DATA.items[r.sym]||{cost:0,qty:0};
      var cost=num(it.cost), qty=num(it.qty);
      r.isLong=!!r.tr.closest('#group_positions_long');
      var held=isPosition(r), has=held&&cost>0&&qty>0;
      r.has=has; r.cost=cost; r.qty=qty;
      if(!has){
        r.lossLv=0; r.lossAcked=false;
        pruneAck(r.sym+'|loss10'); pruneAck(r.sym+'|loss20');
        r.tick.style.display='none'; r.info.textContent='';
        if(r.rail) r.rail.setAttribute('aria-label',r.lab0);
        if(r.warn){ r.warn.style.display='none'; r.warn.textContent=''; }
        r.mv=0; r.pnlPct=0; r.pnlAmt=0;
        // 编辑态下没填过的标的也要露出来，否则没法补录
        if(editing && held){ (r.isLong?longs:shorts).push(r); }
        return;
      }
      var mv=r.price*qty;
      /* 长/短都是多头持有：短持仓只是持有周期短，不是做空，
         盈亏一律 (现价−成本)×数量，绿涨红跌 */
      var pnlPct=(r.price-cost)/cost*100;
      var pnlAmt=(r.price-cost)*qty;
      r.cost=cost; r.qty=qty; r.mv=mv; r.pnlPct=pnlPct; r.pnlAmt=pnlAmt;
      totalMv+=mv; totalPnl+=pnlAmt; totalCost+=cost*qty;
      (r.isLong?longs:shorts).push(r);

      /* 成本价在区间外就整条贴到端点并加朝外箭头（区分「成本＝最高/最低价」），
         区间内才按实际位置画 */
      if(!r.rail || !(r.high>r.low)){ r.tick.style.display='none'; return; }
      var pos=(cost-r.low)/(r.high-r.low)*100;
      var outside=pos<0?'out-lo':(pos>100?'out-hi':'');
      r.tick.style.display='';
      r.tick.className='range52-cost'+(outside?' '+outside:'');
      if(outside){ r.tick.style.left=''; } else { r.tick.style.left=pos.toFixed(1)+'%'; }
      // 读屏标签跟 monitor.py 的 caption 一致：区间外要写清实际数值与方向，
      // 别让人以为是「成本刚好等于最高价／最低价」
      if(r.rail){
        var cap=r.lab0;
        if(outside==='out-lo') cap+='；成本价 '+trim(cost)+' 低于52周低点 '+r.lowTxt;
        else if(outside==='out-hi') cap+='；成本价 '+trim(cost)+' 高于52周高点 '+r.highTxt;
        else cap+='；成本价 '+trim(cost)+' 位于区间 '+pos.toFixed(1)+'%';
        r.rail.setAttribute('aria-label',cap);
      }
    });
    var totalAsset=totalMv+cash+fixed;
    /* 成本那一排：要等总资产算出来才知道仓位，所以放在第二遍循环里一次写完。
       ① 字号跟报警描述一致（CSS）② 盈亏比例＋金额绿涨红跌 ③ 仓位 >10% 黄字
       ④ 浮亏到 10%／20% 另起一行提醒（只出文字，不报灯、不进统计） */
    ROWS.forEach(function(r){
      if(!r.has){ if(r.info) r.info.innerHTML=''; return; }
      var w=totalAsset>0?r.mv/totalAsset*100:0;
      var cls=r.pnlPct>=0?'up':'down';
      /* 三组各自成块（inline-block + nowrap）：手机放不下时**整组换行**，
         不会像以前那样把「仓位 2.6%」截成「仓位 2....」；组内数字间留 5px，组间用细点分隔 */
      r.info.innerHTML='<span class="cg"><span class="k">成本</span> <b>'+trim(r.cost)+'</b>'
        +'<b class="pct '+cls+'">'+pct(r.pnlPct)+'</b>'
        +'<b class="amt '+cls+'">'+money(r.pnlAmt)+'</b></span>'
        +'<span class="sep">·</span><span class="cg"><span class="k">市值</span> <b>'+plain(r.mv)+'</b></span>'
        +'<span class="sep">·</span><span class="cg"><span class="k">仓位</span> <b'
        +(w>10?' class="wt-hi"':'')+'>'+(totalAsset>0?w.toFixed(1)+'%':'—')+'</b></span>';
      /* 浮亏 10%/20%：这里也做成可手动关闭的临时提醒——按钮挂在成本价那一排的提醒后面。
         浮亏收窄到阈值以内时 key 自动清掉，下次再跌破会重新提醒 */
      var lv=r.pnlPct<=-20?2:(r.pnlPct<=-10?1:0);
      if(!DATA.acks) DATA.acks={};
      if(lv===0) pruneAck(r.sym+'|loss10');
      if(lv<2) pruneAck(r.sym+'|loss20');
      var lk=r.sym+'|loss'+(lv===2?'20':'10');
      // 记到行对象上：灯级合成时要知道「这条规则现在该不该亮红灯」
      r.lossLv=lv; r.lossAcked=!!DATA.acks[lk];
      if(!r.warn) return;
      if(lv){
        var lAcked=!!DATA.acks[lk];
        r.warn.style.display='';
        r.warn.className='cost-warn lv'+lv+(lAcked?' acked':'');
        r.warn.innerHTML='<span class="w">⚠ 浮亏 '+Math.abs(r.pnlPct).toFixed(1)+'%，已超 '
          +(lv===2?'20':'10')+'%</span>'
          +(lAcked
            ? ' 已读<button type="button" class="ack-btn on" data-ack="'+lk
              +'" aria-label="撤销已读">✓</button>'
            : '<button type="button" class="ack-btn" data-ack="'+lk
              +'" aria-label="标记已读">✓</button>');
      } else { r.warn.style.display='none'; r.warn.textContent=''; }
    });

    var tcls=totalPnl>=0?'up':'down';
    var tPct=totalCost>0?totalPnl/totalCost*100:0;
    var tWt=totalAsset>0?(totalMv/totalAsset*100).toFixed(1)+'%':'—';
    /* 卡片表头只留两个数：总资产（只写数值，不写「总资产」四个字）
       ＋ 相对成本的盈亏（金额＋比例，含固定收益） */
    var allPnl=totalPnl+fPnl, allCost=totalCost+fixedCost;
    var allPct=allCost>0?allPnl/allCost*100:0;
    var sumEl=panel.querySelector('.cost-sum');
    sumEl.innerHTML='<b>'+plain(totalAsset)+'</b>'
      +'<span class="sep">·</span><b class="'+(allPnl>=0?'up':'down')+'">'
      +money(allPnl)+'（'+pct(allPct)+'）</b>';
    renderRs();

    // 固定降序：按盈亏（百分比）或按仓位（市值）排，点列头切换。
    // 编辑态下锁成进入编辑时的顺序，不然边打字边重排，行会跳走
    var cmp;
    if(editing){
      cmp=function(a,b){
        var ia=editOrder.hasOwnProperty(a.sym)?editOrder[a.sym]:999;
        var ib=editOrder.hasOwnProperty(b.sym)?editOrder[b.sym]:999;
        return ia-ib;
      };
    } else {
      cmp=(sortKey==='wt')
        ? function(a,b){ return b.mv-a.mv; }
        : function(a,b){ return b.pnlPct-a.pnlPct; };
    }
    longs.sort(cmp); shorts.sort(cmp);
    var html='';
    /* 「总持仓」＝长＋短合计（不含现金／固定收益），放在长持仓的栏目名称下面一行，
       用的还是同一套列宽：名称第 1 列、持仓市值第 4 列、盈亏第 5 列、持仓仓位第 6 列 */
    var totalRow=titleRow('总持仓', (longs.length+shorts.length)+' 只', plain(totalMv),
      '<b class="'+tcls+'"><span class="pct">'+pct(tPct)+'</span> '
        +'<span class="amt">'+money(totalPnl)+'</span></b>', tWt, 'is-total');
    // 栏目名称只在第一个框（长持仓）上面写一次；短持仓紧挨着，再抄一遍没意义
    html+=block('长持仓', longs, totalAsset, true, 'blk-long', totalRow);
    html+=block('短持仓', shorts, totalAsset, false, 'blk-short');
    var share=function(v){ return totalAsset>0?(v/totalAsset*100).toFixed(1)+'%':'—'; };
    /* 现金／固定收益的仓位不变黄：>10% 变黄是「单只过度集中」的提示，
       这两个是刻意留的弹药，占比高不等于风险，另外黄也没有可执行的动作 */
    var wtCell=function(v){
      return '<span class="t-wt"><span class="k">仓位</span>'+share(v)+'</span>';
    };
    /* 现金：整块就是一排带底色的信息——名称 + 金额 + 仓位，
       不再「分类名 + 下面再列一行 USD / 现金（含货币市场ETF）」（编辑态下金额变输入框） */
    html+=miniHtml('blk-cash',
      '<div class="cost-mini">'
      +'<span class="t-name">现金<span class="cnt">含货币市场ETF</span></span>'
      +'<span class="c-cost"></span>'
      +'<span class="t-mv">'+(editing?inp('USD','cash',vt(cash),'金额'):plain(cash))+'</span>'
      +'<span class="t-pnl"></span>'
      +wtCell(cash)+'</div>');
    if(fixed>0 || editing){
      /* 固定收益：同样是单排信息。成本是券商给的总成本（没有单价），平时也显示，
         不再只在编辑态露出来；编辑态下成本与市值各自变输入框 */
      html+=miniHtml('blk-bond',
        '<div class="cost-mini">'
        +'<span class="t-name">固定收益<span class="cnt">国债等</span></span>'
        +'<span class="c-cost'+(fixedCost>0&&!editing?'':' dim')+'">'
        +(editing?inp('BOND','fixed_cost',vt(fixedCost),'成本')
          :(fixedCost>0?'<span class="k">成本</span>'+plain(fixedCost):'—'))+'</span>'
        +'<span class="t-mv">'+(editing?inp('BOND','fixed',vt(fixed),'市值'):plain(fixed))+'</span>'
        +'<span class="t-pnl '+fCls+'"><span class="pct">'+pct(fPct)+'</span> '
        +'<span class="amt">'+money(fPnl)+'</span></span>'
        +wtCell(fixed)+'</div>');
    }
    /* ---------- 当日盈亏贡献（默认收起） ----------
       只算今天：当日盈亏＝(现价−昨收)×数量，昨收＝现价÷(1+当日涨跌%)。
       占比 = 该只当日盈亏 ÷ 全部持仓当日盈亏的「绝对值之和」（正负分开算，
       所以贡献加起来 100%、拖累加起来也是 100%）：正=贡献（绿）、负=拖累（红）。
       累计总盈亏不在这里重复——券商账户里能看到，页面只回答「今天谁在赚、谁在亏」。
       现金与固定收益没有行情涨跌，不进这张表（现金在上面的资产结构横条里）。 */
    var cl=[];
    ROWS.forEach(function(r){
      if(!r.has) return;
      var r0=(r.chg||0)/100;
      if(r0<=-1) return;                       // 涨跌幅缺失/异常就不画
      var prev=r.price/(1+r0);                 // 昨收
      r.dayAmt=(r.price-prev)*r.qty;
      cl.push({sym:r.sym,cn:r.note||'',amt:r.dayAmt,chg:r.chg||0});
    });
    if(cl.length){
      var gross=0; cl.forEach(function(x){ gross+=Math.abs(x.amt); });
      cl.forEach(function(x){ x.share=gross>0?x.amt/gross*100:0; });
      cl.sort(function(a,b){ return b.share-a.share; });
      var mx=0; cl.forEach(function(x){ mx=Math.max(mx,Math.abs(x.share)); });
      var crows=cl.map(function(x){
        var c=x.share>=0?'up':'down';
        // 条长按最大那条的绝对值归一：最长刚好从基线画到格子端点（半个格子）
        var w=mx>0?Math.min(Math.abs(x.share)/mx*50,50):0;
        var cg=x.chg>=0?'up':'down';
        return '<li aria-label="占当日盈亏绝对值 '+fmt1(Math.abs(x.share))+'%">'
          +'<span class="cn-name">'+esc(x.sym)
          +(x.cn?' <span style="color:var(--dim)">'+esc(x.cn)+'</span>':'')+'</span>'
          +'<span class="cbar"><i class="'+c+'" style="width:'+w.toFixed(1)+'%"></i></span>'
          +'<span class="cv '+cg+'">'+pct(x.chg)+'</span>'
          +'<span class="ca '+c+'">'+money(x.amt)+'</span></li>';
      }).join('');
      // 合计：今日净盈亏金额 + 相对「昨日持仓市值」的百分比
      var dayNet=0; cl.forEach(function(x){ dayNet+=x.amt; });
      var prevMv=0; ROWS.forEach(function(r){
        if(!r.has) return;
        var r0=(r.chg||0)/100; if(r0<=-1) return;
        prevMv+=r.price/(1+r0)*r.qty;
      });
      var dayPct=prevMv>0?dayNet/prevMv*100:0;
      var dCls=dayNet>=0?'up':'down';
      var prevPnl=document.getElementById(PID).querySelector('.blk-pnl-fold');
      var pnlOpen=!!(prevPnl&&prevPnl.hasAttribute('open'));
      html+='<details class="signal-member-block blk-pnl blk-pnl-fold"'+(pnlOpen?' open':'')+'>'
        +'<summary class="cost-subhead" aria-label="当日盈亏＝(现价−昨收)×数量；占比按当日盈亏绝对值（正负分开算）">'
        +'当日盈亏贡献'
        +'<span class="cnt">今日净 <b class="'+dCls+'">'+money(dayNet)+' '+pct(dayPct)+'</b></span></summary>'
        +'<ul class="cost-contrib">'+crows+'</ul></details>';
    }
    var ackN=Object.keys(DATA.acks||{}).length;
    if(ackN){
      html+='<div class="ack-bar">已读提醒 '+ackN+' 条 · 条件消失后自动恢复'
        +'<button type="button" data-ack-clear="1" aria-label="把已读全部清掉，重新提醒">恢复全部</button></div>';
    }
    /* 换设备／清了缓存后第一次打开，又没取到云端：眼前这份是构建时注入的内置快照，
       不是你的最新持仓 —— 必须在页面里说清楚，并给「重试」「导入备份」两个出口 */
    if(SYNC==='nocloud' && !hadOwn){
      html='<div class="cost-nocloud">'
        +'<span>本机没有持仓数据，也没取到云端 —— 下面是内置快照'
        +(seedAsOf?('（'+esc(seedAsOf)+'）'):'')+'，可能不是最新。</span>'
        +'<span class="nb-acts">'
        +'<button type="button" id="costRetry">重试</button>'
        +'<button type="button" id="costImportBak">导入备份</button>'
        +'</span></div>'+html;
    }
    document.getElementById(PID).innerHTML=html;
    applyLights();
    // 自动失效只记录维护 tombstone；初次核验前不冒充用户整份编辑，也不失效 pull。
    if(ACK_PRUNED) persistMaintenance();
    if(focus && editing){
      var again=panel.querySelector('input[data-sym="'+focus.sym+'"][data-f="'+focus.field+'"]');
      if(again){
        again.value=focus.value; again.setCustomValidity(focus.error||''); again.focus();
        try{ again.setSelectionRange(focus.start,focus.end,focus.direction); }catch(e){}
      }
    }

    // 标题行只写「已同步 / 未同步」+ 总资产·盈亏；底部横条下面只留比例图例，不再重复总资产
    // 标题右侧：同步状态。点一下可以填 / 改 GitHub token（只存本机 localStorage）
    var tk=ghToken(), msg=String(SYNC_MSG||'').replace(/[<>&"']/g,'');
    /* 「云端未取到」要不要刺眼，看这台设备原本有没有自己的数据：
       新设备（第一次打开、本机没存过）取不到云端 → 黄字，因为显示的不是你的持仓；
       老设备本机数据完整 → 灰字小注，知道一声就行 */
    var ncCls=hadOwn?'dim':'warn';
    var ncTip='没取到云端持仓数据：'+(tk?'点这里更换 GitHub token':'点这里填 GitHub token')
      +'；本机数据不受影响';
    var okTip='已同步'+(CLOUD&&CLOUD.updated_at?('：云端数据 '+esc(String(CLOUD.updated_at).slice(0,16))):'');
    stateEl.innerHTML = Object.keys(storageErrors).length ? '<span class="warn">本地未持久化</span>'
      : isolated ? '<span class="warn">隔离预览 · 不上传</span>'
      : !writer ? '<span class="warn">只读标签页</span>'
      : SYNC==='pulling' ? '<span class="warn">拉取中…</span>'
      : SYNC==='loading' ? '<span class="warn">上传中…</span>'
      : SYNC==='error' ? '<span class="warn" aria-label="'+msg+'">上传失败</span>'
      : uncertain ? '<span class="warn">上传结果待核验</span>'
      : maintenancePending && !cloudVerified ? '<span class="warn">ACK 维护待云端核验</span>'
      : dirty ? '<span class="warn">未同步'+(tk?'':' · 未填 token')+'</span>'
      : SYNC==='nocloud' ? '<span class="'+ncCls+'" aria-label="'+ncTip+'">云端未取到</span>'
      : '<span class="ok" aria-label="'+okTip+'">已同步</span>';
    /* 底部：一条横条画资产结构占比（长持仓深绿 / 短持仓浅绿 / 现金青 / 固定收益橙），
       下面只留「颜色图标 + 比例」，不再写名称和金额——横条本身已经用颜色分段，
       读屏标签里仍保留完整说明 */
    var longMv=sumOf(longs,function(x){ return x.mv; });
    var shortMv=sumOf(shorts,function(x){ return x.mv; });
    var parts=[{c:'s-long',n:'长持仓',v:longMv},{c:'s-short',n:'短持仓',v:shortMv},
      {c:'s-cash',n:'现金',v:cash},{c:'s-bond',n:'固定收益',v:fixed}];
    var bar='', leg='', aria=[];
    parts.forEach(function(p){
      if(p.v<=0) return;
      var pc=totalAsset>0?p.v/totalAsset*100:0;
      bar+='<span class="seg '+p.c+'" style="width:'+pc.toFixed(3)+'%"></span>';
      leg+='<span><i class="'+p.c+'"></i><b>'+pc.toFixed(1)+'%</b></span>';
      aria.push(p.n+' '+pc.toFixed(1)+'%');
      /* 「所有持仓」＝长持仓＋短持仓：跟在浅绿方块后面再加一个深绿／浅绿拼接的方块，
         只出现在图例里（横条上长＋短本来就是前后两段，再画一遍会重复） */
      if(p.c==='s-short' && (longMv+shortMv)>0){
        var hp=totalAsset>0?(longMv+shortMv)/totalAsset*100:0;
        leg+='<span><i class="seg-all"></i><span class="ln">所有持仓</span>'
          +'<b>'+hp.toFixed(1)+'%</b></span>';
      }
    });
    footEl.innerHTML='<div class="cost-bar" role="img" aria-label="资产结构：'
      +aria.join('，')+'">'+bar+'</div>'
      // 2026-10-19：横条下面不再写「总资产 $xx」——总资产已经在卡片表头（标题行）显示了，
      // 这里重复一份反而抢视线。下面只留「颜色方块 + 比例」。
      +'<div class="cost-footrow"><div class="cost-legend">'+leg+'</div></div>'
      +'<div class="cost-membership-note">持仓统计仅含持仓分组；移组不自动折算现金、不删除成本和数量。移回后沿用旧值，请复核。</div>'
      +(isolated?'<div class="cost-notice">'+ISOLATED_NOTICE+' <button type="button" data-cost-export>导出备份</button></div>':'')
      +((NOTICE||Object.keys(storageErrors).length||SYNC==='error')
        ? '<div class="cost-notice" role="status">'+esc(Object.keys(storageErrors).map(function(k){
          return storageErrors[k]; }).join('；')||NOTICE||SYNC_MSG)
          +' <button type="button" data-cost-export>导出备份</button>'
          +(writer?'<button type="button" data-cost-retry>'+(isolated?'重试会话保存':'重试保存/上传')+'</button>':'')+'</div>' : '');
  }

  panel.addEventListener('click',function(e){
    if(e.target.closest('[data-cost-export]')) exportData();
    if(e.target.closest('[data-cost-retry]')){
      if(editing){ NOTICE='请先保存或取消当前编辑，再重试上传'; paint(); return; }
      savePaused=false; requestSave(0,true);
    }
  });
  paint();
  syncEditUI();
  takeOwnership(function(ok){ if(ok) startPull(); paint(); });

  /* 默认展开的只有「统计」（monitor.py 生成时就带 open），持仓 / 重点 / 市场风险 / 仓位管理
     全部默认折叠——10-14 定的口径。2026-10-19 修：这段 DEMO 残留会把持仓和仓位管理强行展开，
     还定时重试 5 次，用户手动收起后又被弹开。*/

  // 只为截图核对：?focus=panel 落到「仓位管理」卡片，?focus=shorts 落到短持仓框，
  //               ?focus=editor 落到编辑后的按钮区，?focus=rules 落到页底报灯规则，
  //               ?focus=pos 落到持仓，?edit=1 直接进编辑态
  var fq=(location.search.match(/focus=(\w+)/)||[])[1];
  if(/edit=1/.test(location.search)) setTimeout(enterEdit, 400);
  if(fq){
    setTimeout(function(){
      var blocks=panel.querySelectorAll('.signal-member-block');
      var t;
      if(fq==='editor'){ if(actsEl.hidden) enterEdit(); t=actsEl; }
      else if(fq==='pos') t=document.getElementById('group_positions');
      else if(fq==='rules') t=document.getElementById('signalRules');
      else if(fq==='shorts') t=blocks[1]||panel;
      else t=panel;
      if(t && t.scrollIntoView) t.scrollIntoView({block:'start'});
    }, 900);
  }
  if(/dbg=1/.test(location.search)){
    setTimeout(function(){
      var g=document.querySelector('#costMembers .cost-grid');
      var cn=document.querySelector('#costMembers .cost-name');
      document.title='SW='+document.documentElement.scrollWidth
        +' body='+document.body.getBoundingClientRect().width
        +' card='+panel.getBoundingClientRect().width
        +' grid='+(g?g.getBoundingClientRect().width:0)
        +' name='+(cn?cn.getBoundingClientRect().width:0)
        +' sum='+panel.querySelector('summary').getBoundingClientRect().width;
    },1500);
  }
  /* DEMO 预览同步正式版（monitor.py 已改）：顶栏按钮改名「运行 / 状态 / 刷新 / 设置」，
     并把「定投：N」从市场风险标题挪到顶栏最右侧 */
  (function(){
    var map={btnRunNow:'运行', btnCheckStatus:'状态', btnHardRefresh:'刷新'};
    Object.keys(map).forEach(function(id){
      var b=document.getElementById(id); if(b) b.textContent=map[id];
    });
    var dca=document.querySelector('.dca-badge'), bar=document.querySelector('.runbar');
    if(dca && bar && dca.parentNode!==bar){
      dca.parentNode.removeChild(dca);
      bar.insertBefore(dca, bar.querySelector('script'));
    }
  })();
  // ?only=blocks → 只留「仓位管理＋统计＋持仓」，用来单独看这几块
  // （持仓要留着：「20日对.SPX」挂在它的标题右侧，删了就没地方看）
  if(/only=blocks/.test(location.search)){
    Array.prototype.forEach.call(document.querySelectorAll('.group-card,.macro-card'), function(c){
      if(c.id!=='signalCounts' && c.id!=='costManage' && c.id!=='group_positions') c.remove();
    });
    var top=panel; while(top.parentNode && top.parentNode!==document.body){ top=top.parentNode; }
    var prev=[];
    for(var n=top.previousElementSibling; n; n=n.previousElementSibling){ prev.push(n); }
    prev.forEach(function(n){ n.remove(); });
  }
})();
</script>