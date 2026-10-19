<script>
(function(){
  var LS_LOCAL='mm_position_costs_local_v1';
  var LS_CLOUD='mm_position_costs_cloud_v1';
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
  function read(k){ try{ return JSON.parse(localStorage.getItem(k)||'null'); }catch(e){ return null; } }
  function write(k,v){ try{ localStorage.setItem(k, JSON.stringify(v)); }catch(e){} }
  function num(v){ var n=parseFloat(v); return isFinite(n)?n:0; }
  function esc(s){ return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

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

  /* ---------- 1. 扫描持仓行，收集结构 ---------- */
  var FACTORS=[0.72,0.83,0.88,0.94,1.05,1.12,1.21,0.91,0.97,1.02,0.79,1.15,0.86,1.08,0.68,1.25,0.99,1.03];
  var QTYS=[40,25,15,60,12,30,18,22,35,10,28,16,50,14,20,26,44,13];
  // ETF 胶囊必须在代码后面（第一行），不能留到中文备注后面。全站所有个股行都处理。
  // 正式实现改 monitor.py 的个股行：<td class="sym">CODE<span class="tag">ETF</span><span class="note">…</span></td>
  Array.prototype.forEach.call(document.querySelectorAll('td.sym'), function(sd){
    var t=sd.querySelector('.tag');
    if(t && sd.firstChild) sd.insertBefore(t, sd.firstChild.nextSibling);
  });
  var ROWS=[], idx=0;
  /* 加仓价 / 减仓价只认 holdings.json 里真实填的 trigger / reduce：没填就不画胶囊、不报灯。
     2026-10-19 修：此前这段会给每只持仓按现价 ×0.985/0.86/0.78 造一个「示意加仓价」，
     线上每只票都挂着一个假价位，还有 1/3 直接落进触发区报红灯 —— 全部去掉 */
  var EXTRA=[];           // 非持仓但带真实加仓价胶囊的行（AAPL）
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
  var GAP_ADD=3;                 // settings.json 里 trigger_gap_pct 的生效值就是 3（默认 5 已被覆盖）
  /* 已清仓的标的：正式实现靠 holdings.json 删掉，DEMO 快照是旧页面，这里手动剔除 */
  var SKIP=['GEHC'];
  SKIP.forEach(function(s){
    var tr=document.getElementById('s-'+s.toLowerCase());
    // 统计面板/名单里指向它的行一并去掉，避免留下点不开的死链
    Array.prototype.forEach.call(document.querySelectorAll('a[href="#s-'+s.toLowerCase()+'"]'), function(a){
      var li=a.closest('li');
      if(li&&li.parentNode) li.parentNode.removeChild(li);
    });
    if(!tr) return;
    var nx=tr.nextElementSibling;
    if(nx&&nx.querySelector&&nx.querySelector('.range52-rail')) nx.parentNode.removeChild(nx);
    tr.parentNode.removeChild(tr);
  });
  // 快照里的「持仓 (18)」是旧数字，剔除已清仓标的后按实际行数重算
  (function(){
    var sm=document.querySelector('#group_positions>summary .grp-name'); if(!sm) return;
    var n=document.querySelectorAll('#group_positions tr[id^="s-"]').length;
    if(n) sm.textContent='持仓 ('+n+')';
  })();
  Array.prototype.forEach.call(document.querySelectorAll('#group_positions tr[id^="s-"]'), function(tr){
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
    var rail=railTr&&railTr.querySelector('.range52-rail'); if(!rail) return;
    var lab=rail.getAttribute('aria-label')||'';
    var m=lab.match(/([\d,]+\.\d{2})[低高]*，本轮价 ([\d,]+\.\d{2}) 位于区间 ([\d.]+)%，[^，]*高 ([\d,]+\.\d{2})/);
    if(!m) return;
    var low=num(m[1].replace(/,/g,'')), high=num(m[4].replace(/,/g,''));
    var f=FACTORS[idx%FACTORS.length], q=QTYS[idx%QTYS.length]; idx++;
    var seed={cost:Math.round(price*f*100)/100, qty:q};

    /* 把旧条重建为新版结构：左端写最低价、右端写最高价，百分比与紫方块都挪进灰轨里。
       快照的 rail 只有「百分比 + 轨道」两个孩子，百分比还挂在 rail 上（旧版式），
       新版要用轨道做定位基准，所以先把它挪进去，不然 left% 会按整条宽度算 */
    var track=rail.querySelector('.range52-track');
    if(track){
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
    var trig=0, cut=0;
    var cap=null, sigAdd=null;
    var tick=document.createElement('span');
    tick.className='range52-cost'; tick.style.display='none';
    // 挂到灰轨上（而不是整条 rail）：rail 里还夹着左右两个价格文字，
    // 挂 rail 的话 left% 会按整条宽度算，跟紫方块对不上
    (rail.querySelector('.range52-track')||rail).appendChild(tick);
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
    var td=rail.parentNode; td.insertBefore(info, rail.nextSibling);
    td.insertBefore(warnAdd, info.nextSibling);
    td.insertBefore(warnCut, warnAdd.nextSibling);
    td.insertBefore(warn, warnCut.nextSibling);
    ROWS.push({sym:sym,note:note,isLong:isLong,isEtf:isEtf,lv:lv,price:price,chg:chg,low:low,high:high,
               rail:rail,lab0:lab,lowTxt:m[1],highTxt:m[4],tr:tr,origCls:tr.className||'',
               trig:trig,cut:cut,cap:cap,cutCap:null,sigAdd:sigAdd,
               tick:tick,info:info,warnAdd:warnAdd,warnCut:warnCut,warn:warn,seed:seed});
  });
  if(!ROWS.length){ return; }

  /* ---------- 1.5 「已读」：临时提醒可以手动关 ----------
     规则：点一下＝已知晓，这条规则本轮不再亮红灯；**条件消失后自动失效**
     （价格回到加仓价 3% 之外、或浮亏收窄到 10% 以内），下次再触发会重新亮，
     不会「关一次就永远不提醒」。已读随 DATA 一起保存/导出/上传，跨设备取并集。 */
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
  /* 非持仓行（AAPL）没有成本排：提醒挂到它下面那条 52 周条所在格子里，位置跟持仓行对齐 */
  function ensureWarn(tr){
    if(tr.__warnAdd) return tr.__warnAdd;
    var host=null, nx=tr.nextElementSibling;
    if(nx&&nx.querySelector&&nx.querySelector('.range52-rail')) host=nx.querySelector('td')||nx;
    if(!host) host=tr.querySelector('td.sig')||tr;
    var w=document.createElement('span');
    w.className='cost-warn'; w.style.display='none';
    host.appendChild(w); tr.__warnAdd=w;
    return w;
  }
  function applyLights(){
    if(!DATA.acks) DATA.acks={};
    var A=DATA.acks;
    ROWS.forEach(function(r){
      var want=null, sigTxt='';
      // ① 加仓价：已跌破、或距加仓价不足 3%（跟 monitor.py 判定一致）
      if(r.trig>0){
        var gap=(r.price/r.trig-1)*100, on=gap<=GAP_ADD;
        var k=r.sym+'|add|'+r.trig;
        if(!on && A[k]){ delete A[k]; ACK_PRUNED=true; }   // 条件消失 → 已读自动失效
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
        var rgap=(r.price/r.cut-1)*100, onR=(rgap>=0)||(-rgap<=GAP_ADD);
        var kc=r.sym+'|cut|'+r.cut;
        if(!onR && A[kc]){ delete A[kc]; ACK_PRUNED=true; }
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
    EXTRA.forEach(function(e){
      // AAPL 这类「非持仓但有真实加仓价」的行：可开关模拟跌破
      var trig=e.realTrig;   // 只认页面自带的真实加仓价，不再模拟
      var gap=(e.price/trig-1)*100, on=gap<=GAP_ADD;
      var k=e.sym+'|add|'+trig;
      if(!on && A[k]){ delete A[k]; ACK_PRUNED=true; }
      var acked=!!A[k];
      e.cap.className='lvtag add';
      e.cap.removeAttribute('data-ack'); e.cap.removeAttribute('role');
      e.cap.removeAttribute('tabindex'); e.cap.removeAttribute('aria-pressed');
      e.cap.setAttribute('aria-label','加仓价（已读按钮在下面那行提醒里）');
      e.cap.innerHTML='<span class="t">加 '+trig+'</span>';
      if(!e.warnAdd) e.warnAdd=ensureWarn(e.tr);
      if(on){
        renderWarn(e.warnAdd, (gap<=0?'已跌破加仓价 '+trig
          :'距加仓价 '+trig+' 还差 '+gap.toFixed(1)+'%'), k, acked);
        if(e.sigAdd) e.sigAdd.textContent=(!acked)
          ? ((gap<=0?'已跌破加仓价 ':'距加仓价 ')+trig+(gap<=0?'':' 还差 '+gap.toFixed(1)+'%'))+' · ' : '';
      } else { hideWarn(e.warnAdd); if(e.sigAdd) e.sigAdd.textContent=''; }
      e.tr.className=withLv(e.origCls, (on&&!acked)?'red':null);
    });
  }

  /* 非持仓行里带真实加仓价胶囊的（focus 组 AAPL：holdings.json 里写了 trigger 325） */
  var EXTRA=[];
  function collectExtra(){
    EXTRA.length=0;
    Array.prototype.forEach.call(document.querySelectorAll('tr[id^="s-"]'), function(tr){
      var inRows=ROWS.some(function(r){ return r.tr===tr; });
      if(inRows) return;
      var cap=tr.querySelector('.lvtag.add,.lvtag.add-ck'); if(!cap) return;
      var pxEl=tr.querySelector('.px-price');
      var price=num(((pxEl&&pxEl.textContent)||'').replace(/,/g,''));
      /* 原始灯级和真实加仓价都只认第一次读到的值：之后 className 和胶囊文字都会被
         演示层改写（注入红灯、换成模拟价位），再读就等于把改后的值当成原始值了 */
      if(!tr.__origCls) tr.__origCls=tr.className||'';
      if(!tr.__realTrig) tr.__realTrig=num((cap.textContent||'').replace(/[^\d.]/g,''));
      var real=tr.__realTrig;
      if(!(price>0)||!(real>0)) return;
      // 胶囊只显示价位；「已读」统一走下面提醒行里的 ✓ 按钮（跟浮亏 −10% 同一模式）
      cap.classList.remove('add-ck');
      cap.setAttribute('aria-label','加仓价（已读按钮在下面那行提醒里）');
      var sigCell=tr.querySelector('td.sig');
      var sigAdd=sigCell?sigCell.querySelector('.signal-red'):null;
      if(sigCell && !sigAdd){
        sigAdd=document.createElement('span');
        sigAdd.className='signal-red';
        sigCell.insertBefore(sigAdd, sigCell.firstChild);
      }
      EXTRA.push({sym:tr.id.slice(2).toUpperCase(), tr:tr, origCls:tr.__origCls,
                  price:price, realTrig:real, cap:cap, sigAdd:sigAdd});
    });
  }
  document.addEventListener('click', function(e){
    if(!e.target||!e.target.closest) return;
    var cl=e.target.closest('[data-ack-clear]');
    if(cl){ DATA.acks={}; setPending(true); write(LS_LOCAL,DATA); paint(); autoPush(); return; }
    var el=e.target.closest('[data-ack]');
    if(!el) return;
    e.preventDefault(); e.stopPropagation();
    var k=el.getAttribute('data-ack');
    if(!DATA.acks) DATA.acks={};
    if(DATA.acks[k]) delete DATA.acks[k]; else DATA.acks[k]=nowStr();
    setPending(true); write(LS_LOCAL, DATA); paint(); autoPush();   // 已读也自动上传，另一台设备不会再亮同一条
  });
  /* 键盘：已读按钮本身就是 <button>，Enter / 空格由浏览器直接触发，不需要额外接管
     （胶囊不再承担点击已读，所以这里不再处理 .lvtag） */

  /* ---------- 1.9 云端同步：GitHub API 写 position-costs.json → Pages 发布 → 其它设备 fetch ---------- */
  var GH={owner:'nixhuang', repo:'market-monitor', path:'position-costs.json', branch:'main'};
  var LS_TOKEN='mm_gh_token_v1';
  /* 同步状态只看「本机这一轮有没有改过持仓」：
     没动过 → 已同步（页面显示的就是权威数据，不再拿云端比对结果吓人——云端缺文件、
     字段顺序不同、另一台设备先改过，都会让比对永远不相等，状态就永远卡在「未同步」）。
     只有真编辑过（成本价 / 数量 / 现金 / 固定收益 / 已读 / 导入）才标未同步，
     上传成功后立刻回到已同步；上传失败显示「上传失败」。 */
  var CLOUD=null, SYNC='synced', SYNC_MSG='';   // synced(已同步) / local(未同步) / loading / error
  function ghToken(){ try{ return (localStorage.getItem(LS_TOKEN)||'').trim(); }catch(e){ return ''; } }
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
  /* 两个候选地址，按顺序试：
     ① raw.githubusercontent.com —— 文件一提交就能读到（实测 API 上传后立刻 200）；
     ② 站点同目录的 position-costs.json —— Pages 要等下一次构建才会发布新文件，常滞后几分钟。
     都带时间戳绕过 CDN 缓存。 */
  function cloudUrls(){
    var t=Date.now();
    return ['https://raw.githubusercontent.com/'+GH.owner+'/'+GH.repo+'/'+GH.branch+'/'+GH.path+'?t='+t,
            location.href.split(/[?#]/)[0].replace(/[^/]*$/,'')+GH.path+'?t='+t];
  }
  function pullCloud(cb){
    var us=cloudUrls(), i=0;
    (function next(){
      if(i>=us.length){ cb(null); return; }
      var u=us[i++];
      fetch(u,{cache:'no-store'}).then(function(r){ return r.ok?r.json():null; })
        .then(function(j){ if(j&&j.items) cb(j); else next(); })
        .catch(function(){ next(); });
    })();
  }
  /* 取云端失败不能只试一次就认命：Pages 发布有延迟（刚上传完可能要等一会儿才 404 变 200）、
     弱网也会偶发失败 → 最多拉 3 次（立刻 / 5 秒 / 20 秒）。点提示条上的「重试」可重开一轮。 */
  var _pullTry=0, _pullTimer=null;
  function startPull(){
    pullCloud(function(j){
      var changed=mergeCloud(j);
      if(CLOUD){ _pullTry=0; SYNC=dirty?'local':'synced'; }
      else{
        SYNC=dirty?'local':'nocloud';
        // 第一次没取到：顺手静默补传一份（云端要是压根没文件，补上以后别人就取得到了）
        if(_pullTry===0 && DATA.items && Object.keys(DATA.items).length) autoPush(true);
        if(_pullTry<2){ _pullTry++; _pullTimer=setTimeout(startPull, _pullTry===1?5000:20000); }
      }
      /* 有改动没传 → 顺手补传（含「上次改完没保存就关了页面」这种）；
         本机干净时数据就是云端那份，没必要再传一遍 */
      if(dirty) autoPush(true);
      paint();
    });
  }
  function pushCloud(data, cb){
    var h=ghHead(true);
    if(!h){ cb(false,'未填 token'); return; }
    var api='https://api.github.com/repos/'+GH.owner+'/'+GH.repo+'/contents/'+GH.path;
    fetch(api+'?ref='+GH.branch,{headers:ghHead(),cache:'no-store'})
      .then(function(r){ return r.ok?r.json():null; })
      .then(function(meta){
        var body={message:'持仓同步 '+new Date().toISOString().slice(0,19).replace('T',' '),
                  content:b64(JSON.stringify(data,null,1)), branch:GH.branch};
        if(meta&&meta.sha) body.sha=meta.sha;      // 已存在的文件更新时必须带 sha
        return fetch(api,{method:'PUT',headers:h,body:JSON.stringify(body)});
      })
      .then(function(r){
        return r.json().then(function(j){
          var ok=(r.status===200||r.status===201);
          cb(ok, ok?'已上传':((j&&j.message)||('HTTP '+r.status)));
        });
      })
      .catch(function(){ cb(false,'网络失败'); });
  }
  /* 合并云端数据（10-19 晚用户选定「D 简单粗暴」口径）：
     ① 本机「干净」（没有待上传的改动）→ **云端全量覆盖**：以最后上传的那台设备为准，
        换设备/刷新就能拿到别处改的数字（含已读）；
     ② 本机还有没上传的改动（dirty）→ 退回「本机永远优先」，只补本机没有的项，
        绝不静默冲掉用户刚改还没上传的数字。
     判据用 dirty 而不是 updated_at：各设备时钟不一定准，updated_at 会被本机每次按键顶新。 */
  function mergeCloud(j){
    if(!j||!j.items) return false;
    CLOUD=j;
    var changed=false;
    if(!dirty){
      var nd={updated_at:j.updated_at||nowStr(), cash:num(j.cash), fixed:num(j.fixed),
              fixed_cost:num(j.fixed_cost), items:{}, acks:{}, real:true};
      Object.keys(j.items).forEach(function(k){
        nd.items[k]={cost:num(j.items[k].cost), qty:num(j.items[k].qty)};
      });
      Object.keys(j.acks||{}).forEach(function(k){ nd.acks[k]=j.acks[k]; });
      changed = JSON.stringify(nd.items)!==JSON.stringify(DATA.items)
             || num(nd.cash)!==num(DATA.cash) || num(nd.fixed)!==num(DATA.fixed)
             || num(nd.fixed_cost)!==num(DATA.fixed_cost)
             || JSON.stringify(nd.acks)!==JSON.stringify(DATA.acks||{});
      if(changed){ DATA=nd; write(LS_LOCAL,DATA); }
      return changed;
    }
    if(!local&&!DATA.real){
      DATA={updated_at:j.updated_at||nowStr(), cash:num(j.cash), fixed:num(j.fixed),
            fixed_cost:num(j.fixed_cost), items:{}, acks:{}, real:true};
      Object.keys(j.items).forEach(function(k){
        DATA.items[k]={cost:num(j.items[k].cost), qty:num(j.items[k].qty)};
      });
      changed=true;
    }else{
      Object.keys(j.items||{}).forEach(function(k){
        var mine=DATA.items[k], his=j.items[k];
        if(!mine || (!num(mine.qty)&&num(his.qty)) || (!mine.cost&&num(his.cost))){
          DATA.items[k]={cost:num((mine&&mine.cost)||his.cost), qty:num((mine&&mine.qty)||his.qty)};
          changed=true;
        }
      });
    }
    // 现金 / 固定收益：本机没填（0）才用云端的，填过的一律不动
    if(!num(DATA.cash)&&num(j.cash)){ DATA.cash=num(j.cash); changed=true; }
    if(!num(DATA.fixed)&&num(j.fixed)){ DATA.fixed=num(j.fixed); DATA.fixed_cost=num(j.fixed_cost); changed=true; }
    Object.keys(j.acks||{}).forEach(function(k){ if(!DATA.acks[k]){ DATA.acks[k]=j.acks[k]; changed=true; } });
    if(changed){ write(LS_LOCAL,DATA); }
    return changed;
  }
  /* 静默自动上传：点了「已读」这类小改动也要跨设备生效，不用再手动点保存。
     有 token 才发；编辑态不发（还在敲数字）；1.2 秒防抖，连续点几下只发一次。
     silent＝后台补传（用户这轮什么都没改）：失败也不弹「上传失败」，免得每次开页面都报错。 */
  var _pushTimer=null, _retryN=0, _retryTimer=null;
  function autoPush(silent){
    if(!ghToken()||editing) return;
    if(_pushTimer) clearTimeout(_pushTimer);
    _pushTimer=setTimeout(function(){
      _pushTimer=null;
      DATA.updated_at=nowStr(); write(LS_LOCAL, DATA);
      var wasDirty=dirty;
      if(!silent||wasDirty){ SYNC='loading'; paint(); }
      pushCloud(DATA, function(ok,msg){
        if(ok){ CLOUD=JSON.parse(JSON.stringify(DATA)); write(LS_CLOUD,DATA);
                SYNC='synced'; SYNC_MSG=''; setPending(false); _retryN=0; }
        else if(wasDirty||!silent){ SYNC='error'; SYNC_MSG=msg||''; setPending(true);
                if(wasDirty) scheduleRetry(); }   // 用户改过却没传上去 → 报错并自动重试
        /* 后台补传失败：本机数据照样完整，不当成错误；
           但「云端未取到」这个事实不能被它抹掉——同步压根还没建立起来 */
        else if(SYNC!=='nocloud'){ SYNC='synced'; SYNC_MSG=''; }
        paint();
      });
    },1200);
  }
  /* 容错：上传失败后不干等，自动再试 2 次（15 秒 / 60 秒后）。只在「用户真改过」时重试，
     后台补传失败不重试（免得一台没配好的设备反复打 GitHub API）。 */
  function scheduleRetry(){
    if(_retryN>=2 || !ghToken()) return;
    _retryN++;
    if(_retryTimer) clearTimeout(_retryTimer);
    _retryTimer=setTimeout(function(){
      _retryTimer=null;
      if(dirty && !editing) autoPush();     // dirty 为真 → 这次会显示「上传中…」并按失败处理
    }, _retryN===1?15000:60000);
  }

  /* ---------- 2. 数据（本地 / 云端） ---------- */
  var seedItems={}; ROWS.forEach(function(r){ seedItems[r.sym]=r.seed; });
  var SEED={updated_at:nowStr(), cash:5914, fixed:0, fixed_cost:0, items:seedItems};
  var local=read(LS_LOCAL), cloud=read(LS_CLOUD);
  // 这台设备进页面前有没有自己的持仓数据：没有＝新设备（换设备首次打开），
  // 此时如果再取不到云端，看到的就是默认值而不是你的持仓 → 「云端未取到」要标成黄字提醒
  var hadOwn=!!(local&&local.items&&Object.keys(local.items).length);
  // 内置快照的日期（构建时注入）：换设备又取不到云端时，要告诉用户眼前这份数据是哪天的
  var seedAsOf=(REAL&&REAL.asof)?String(REAL.asof).slice(0,10):'';
  var DATA;
  /* 有真实持仓数据时，第一次打开就用它覆盖本机的旧示意值（real 标记只认一次）；
     之后仍以本机/远端为准，用户在页面里改过的数字不会被冲掉。 */
  if(REAL && REAL.items && (!local || !local.real)){
    var ritems={};
    Object.keys(REAL.items).forEach(function(k){
      ritems[k]={cost:num(REAL.items[k].cost), qty:num(REAL.items[k].qty)};
    });
    DATA={updated_at:nowStr(), cash:num(REAL.cash), fixed:num(REAL.fixed),
          fixed_cost:num(REAL.fixed_cost), items:ritems, real:true};
    write(LS_LOCAL, DATA);
  } else {
    DATA=local||cloud||JSON.parse(JSON.stringify(SEED));
  }
  /* 2026-10-19：线上构建环境读不到本机数量文件，早期版本注入的 qty / cash / fixed 全是 0。
     这里只「补 0」——本机已有数字的项一律不动（本机永远优先），只把缺失的补成种子值。 */
  if(REAL && REAL.items){
    var sd_=false;
    Object.keys(REAL.items).forEach(function(k){
      var it=DATA.items[k]||(DATA.items[k]={cost:0,qty:0});
      var rc=num(REAL.items[k].cost), rq=num(REAL.items[k].qty);
      if(!num(it.cost) && rc>0){ it.cost=rc; sd_=true; }
      if(!num(it.qty) && rq>0){ it.qty=rq; sd_=true; }
    });
    if(!num(DATA.cash) && num(REAL.cash)>0){ DATA.cash=num(REAL.cash); sd_=true; }
    if(!num(DATA.fixed) && num(REAL.fixed)>0){ DATA.fixed=num(REAL.fixed); sd_=true; }
    if(!num(DATA.fixed_cost) && num(REAL.fixed_cost)>0){ DATA.fixed_cost=num(REAL.fixed_cost); sd_=true; }
    DATA.real=true;
    if(sd_) write(LS_LOCAL, DATA);
  }
  if(!DATA.items) DATA.items={};
  if(!DATA.acks) DATA.acks={};   // 「已读」状态：随 DATA 一起保存/导出/上传
  /* 只为核对页面：?loss=AVGO,UNH（成本 ×1.3，造出 10% 以上浮亏）、?loss=UNH:1.6（×1.6，20% 以上） */
  var lm=location.search.match(/loss=([^&]+)/);
  if(lm){
    var bits=lm[1].split(':'), fac=parseFloat(bits[1]||'1.3')||1.3;
    bits[0].split(',').forEach(function(s){
      var k=s.toUpperCase();
      if(DATA.items[k]) DATA.items[k].cost=num(DATA.items[k].cost)*fac;
    });
  }
  /* dirty＝本机改过但还没上传成功。刷新/重开页面会重置脚本变量，所以另外单独存一个
     pending 标记：改过没上传就刷新，D 方案也不会拿云端把它冲掉（10-19 晚）。 */
  var LS_PENDING='mm_position_pending_v1';
  function setPending(v){
    dirty=!!v;
    try{ if(dirty) localStorage.setItem(LS_PENDING,'1'); else localStorage.removeItem(LS_PENDING); }catch(e){}
  }
  var dirty=false;
  try{ dirty=(localStorage.getItem(LS_PENDING)==='1'); }catch(e){}

  // 打开页面就拉云端 position-costs.json（Pages 会直接发布它）：本机优先，只补本机没有的项
  try{ startPull(); }catch(e){ SYNC=dirty?'local':'nocloud'; }

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
  var editing=false, snapshot=null, prevDirty=false, editOrder={};
  function syncEditUI(){
    editBtn.textContent=editing?'编辑中':'编辑';
    editBtn.setAttribute('aria-pressed', editing?'true':'false');
    if(editing) editBtn.className='cost-edit-btn on'; else editBtn.className='cost-edit-btn';
    // 编辑中由下面的「保存并上传／取消」接管，表头这颗收起来，免得出现两个「退出」入口
    editBtn.hidden=editing;
    actsEl.hidden=!editing;
  }
  function enterEdit(){
    // 编辑的是真实成本/数量，不能拿演示浮亏反推出来的假成本当输入值 → 进编辑态先切回真实
    snapshot=JSON.parse(JSON.stringify(DATA)); prevDirty=dirty;
    // 记下当前行顺序：编辑期间不再重排，否则边打字边跳行
    editOrder={};
    Array.prototype.forEach.call(document.querySelectorAll('#'+PID+' li[data-sym]'),
      function(li,i){ editOrder[li.getAttribute('data-sym')]=i; });
    editing=true; panel.open=true; syncEditUI(); paint();
    var first=panel.querySelector('#'+PID+' input'); if(first) first.focus();
  }
  function exitEdit(restore){
    if(restore && snapshot){ DATA=snapshot; write(LS_LOCAL, DATA); setPending(prevDirty); }
    editing=false; snapshot=null; syncEditUI(); paint();
  }
  editBtn.addEventListener('click', function(e){
    e.preventDefault(); e.stopPropagation();   // 别让点击冒泡上去把整张卡折叠掉
    if(editing) exitEdit(true); else enterEdit();
  });
  panel.querySelector('#costSave').addEventListener('click', function(){
    DATA.updated_at=nowStr();
    write(LS_LOCAL, DATA);
    if(!ghToken()){
      // 没 token 就上传不了 → 明确告诉用户「未同步 · 未填 token」，点那颗字可以补填
      SYNC='local'; SYNC_MSG='未填 GitHub token';
      editing=false; snapshot=null; setPending(true); syncEditUI(); paint(); return;
    }
    SYNC='loading'; _retryN=0; paint();     // 手动保存＝重新给 2 次自动重试机会
    // position-costs.json 不在 daily.yml 的 push.paths 白名单里 → 不跑行情抓取；Pages 会自动发布
    pushCloud(DATA, function(ok,msg){
      if(ok){ CLOUD=JSON.parse(JSON.stringify(DATA)); write(LS_CLOUD, DATA); SYNC='synced'; SYNC_MSG=''; setPending(false); }
      else  { SYNC='error'; SYNC_MSG=msg||'上传失败'; setPending(true); scheduleRetry(); }
      editing=false; snapshot=null; syncEditUI(); paint();
    });
  });
  panel.querySelector('#costCancel').addEventListener('click', function(){ exitEdit(true); });
  // 点标题右侧的「未同步 / 已同步」→ 填 / 改 GitHub token（只存本机 localStorage，每台设备填一次）
  panel.addEventListener('click', function(e){
    var st=e.target&&e.target.closest?e.target.closest('#costState'):null;
    if(!st) return;
    e.preventDefault(); e.stopPropagation();     // 别让点击把整张卡折叠掉
    /* 上传失败时点这颗字＝看失败原因（手机上没有 hover，不能只塞 title） */
    if(SYNC==='error'){
      window.alert('上传失败：'+(SYNC_MSG||'未知原因')
        +'\n\n改动已经存在本机，不会丢；会自动再重试 2 次。'
        +'也可以点「编辑」改一笔再保存，或用「导出 / 导入」把数据搬到另一台设备。');
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
    }catch(err){}
    SYNC_MSG='';
    // 换设备刚填完 token：立刻重开一轮云端拉取（原来只补传，取不到就是取不到）
    _pullTry=0; if(_pullTimer){ clearTimeout(_pullTimer); _pullTimer=null; }
    SYNC='pulling'; startPull();
    if(dirty){ SYNC='local'; autoPush(); }   // 还有没传上去的改动，顺手补传
    paint();
  });
  // 卡片被收起时自动退出编辑态：已敲进去的数字保留（变成「未同步」），不偷偷丢
  panel.addEventListener('toggle', function(){
    if(!panel.open && editing){ editing=false; snapshot=null; syncEditUI(); paint(); }
  });
  panel.querySelector('#costExport').addEventListener('click', function(){
    var blob=new Blob([JSON.stringify(DATA,null,1)],{type:'application/json'});
    var a=document.createElement('a');
    a.href=URL.createObjectURL(blob); a.download='position-costs.json'; a.click();
  });
  panel.querySelector('#costImport').addEventListener('click', function(){
    panel.querySelector('#costFile').click();
  });
  panel.querySelector('#costFile').addEventListener('change', function(e){
    var file=e.target.files&&e.target.files[0]; if(!file) return;
    var fr=new FileReader();
    fr.onload=function(){
      try{
        var j=JSON.parse(fr.result);
        if(j&&j.items){ DATA={updated_at:j.updated_at||nowStr(),cash:num(j.cash),
          fixed:num(j.fixed),fixed_cost:num(j.fixed_cost),items:j.items};
          setPending(true); write(LS_LOCAL,DATA); paint(); }
      }catch(err){}
    };
    fr.readAsText(file);
  });

  /* 编辑框输入：改完即时重算（整个面板重绘），并把光标原样放回同一个输入框，
     否则每敲一个键都会因为 innerHTML 重建而丢焦点 */
  panel.addEventListener('input', function(e){
    var el=e.target;
    if(!el || !el.getAttribute) return;
    var f=el.getAttribute('data-f'); if(!f) return;
    var sym=el.getAttribute('data-sym');
    var raw=el.value, caret=(el.selectionStart==null?raw.length:el.selectionStart);
    if(f==='cash') DATA.cash=num(raw);
    else if(f==='fixed') DATA.fixed=num(raw);
    else if(f==='fixed_cost') DATA.fixed_cost=num(raw);
    else { if(!DATA.items[sym]) DATA.items[sym]={cost:0,qty:0}; DATA.items[sym][f]=num(raw); }
    DATA.updated_at=nowStr(); setPending(true); write(LS_LOCAL, DATA);
    paint();
    var again=panel.querySelector('input[data-sym="'+sym+'"][data-f="'+f+'"]');
    if(again){
      again.value=raw; again.focus();
      try{ again.setSelectionRange(caret,caret); }catch(err){}
    }
  });

  function vt(v){ var n=num(v); return n>0?trim(n):''; }
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
    ACK_PRUNED=false;
    collectExtra();
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
      var has=cost>0&&qty>0;
      r.has=has;
      if(!has){
        r.tick.style.display='none'; r.info.textContent='';
        if(r.warn){ r.warn.style.display='none'; r.warn.textContent=''; }
        r.mv=0; r.pnlPct=0; r.pnlAmt=0;
        // 编辑态下没填过的标的也要露出来，否则没法补录
        if(editing){ (r.isLong?longs:shorts).push(r); }
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
      r.info.innerHTML='<span class="k">成本</span> <b>'+trim(r.cost)+'</b>'
        +' <b class="'+cls+'">'+pct(r.pnlPct)+'</b>'
        +' <b class="'+cls+'">'+money(r.pnlAmt)+'</b>'
        +' <span class="sep">·</span> <span class="k">市值</span> <b>'+plain(r.mv)+'</b>'
        +' <span class="sep">·</span> <span class="k">仓位</span> <b'
        +(w>10?' class="wt-hi"':'')+'>'+(totalAsset>0?w.toFixed(1)+'%':'—')+'</b>';
      /* 浮亏 10%/20%：这里也做成可手动关闭的临时提醒——按钮挂在成本价那一排的提醒后面。
         浮亏收窄到阈值以内时 key 自动清掉，下次再跌破会重新提醒 */
      var lv=r.pnlPct<=-20?2:(r.pnlPct<=-10?1:0);
      if(!DATA.acks) DATA.acks={};
      if(lv===0 && (DATA.acks[r.sym+'|loss10']||DATA.acks[r.sym+'|loss20'])){
        delete DATA.acks[r.sym+'|loss10']; delete DATA.acks[r.sym+'|loss20']; ACK_PRUNED=true;
      }
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
    if(fixed>0){
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
    // 条件消失自动清掉的「已读」要落盘，否则刷新页面又会亮一次
    if(ACK_PRUNED){ ACK_PRUNED=false; write(LS_LOCAL, DATA); autoPush(); }

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
    stateEl.innerHTML = SYNC==='pulling' ? '<span class="warn">拉取中…</span>'
      : SYNC==='loading' ? '<span class="warn">上传中…</span>'
      : SYNC==='error' ? '<span class="warn" aria-label="'+msg+'">上传失败</span>'
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
      +'<div class="cost-footrow"><div class="cost-legend">'+leg+'</div></div>';
  }

  paint();
  syncEditUI();

  /* 默认展开的只有「统计」（monitor.py 生成时就带 open），持仓 / 重点 / 市场风险 / 仓位管理
     全部默认折叠——10-14 定的口径。2026-10-19 修：这段 DEMO 残留会把持仓和仓位管理强行展开，
     还定时重试 5 次，用户手动收起后又被弹开。*/

  // 只为截图核对：?focus=panel 落到「仓位管理」卡片，?focus=shorts 落到短持仓框，
  //               ?focus=editor 落到编辑后的按钮区，?focus=rules 落到页底报灯规则，
  //               ?focus=pos 落到持仓，?edit=1 直接进编辑态
  var fq=(location.search.match(/focus=(\w+)/)||[])[1];
  if(/edit=1/.test(location.search)) setTimeout(enterEdit, 400);
  // 只为核对：?edit=1&save=1 走一遍「进编辑 → 保存并上传」
  if(/save=1/.test(location.search)){
    setTimeout(function(){
      var i=panel.querySelector('input[data-f="qty"]');
      if(i){ i.value='9'; i.dispatchEvent(new Event('input',{bubbles:true})); }
      var b=panel.querySelector('#costSave'); if(b) b.click();
    }, 700);
  }
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