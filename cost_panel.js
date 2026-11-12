<script>
  (function () {
    'use strict';
    var REAL = __MM_REAL__;
    var LS_LOCAL = 'mm_position_costs_local_v1',
      LS_CLOUD = 'mm_position_costs_cloud_v1';
    var LS_PENDING = 'mm_position_pending_v1',
      LS_PUSH_AT = 'mm_position_last_push_v2';
    var LS_TOKEN = 'mm_gh_token_v1',
      LS_SORT = 'mm_position_sort_v1',
      LOCK_NAME = 'mm-position-writer-v2',
      LS_CLEAR = 'mm_position_clear_v1';
    var READ_ONLY_NOTICE = '本页只读';
    var SCHEMA_VERSION = 2,
      SESSION_LOCAL = 'mm_position_costs_preview_v2';
    var isolated = location.protocol === 'file:' || !(navigator.locks && navigator.locks.request);
    var ISOLATED_NOTICE =
      '隔离预览：仅保存在当前标签页会话中，刷新可恢复；关闭标签页前请导出。不会修改共享本地仓位或上传云端。';
    var storageErrors = {},
      NOTICE = '',
      LV_ORDER = ['red', 'yellow', 'green', 'gray'];
    function own(o, k) {
      return Object.prototype.hasOwnProperty.call(o, k);
    }
    function object(v) {
      return v !== null && typeof v === 'object' && !Array.isArray(v);
    }
    function clone(v) {
      return JSON.parse(JSON.stringify(v));
    }
    function num(v) {
      var n = parseFloat(v);
      return isFinite(n) ? n : 0;
    }
    function esc(s) {
      return String(s == null ? '' : s)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
    }
    function all(s, root) {
      return Array.prototype.slice.call((root || document).querySelectorAll(s));
    }
    function read(k) {
      try {
        if (isolated && k === LS_CLOUD) return null;
        var v = isolated && k === LS_LOCAL ? sessionStorage.getItem(SESSION_LOCAL) : localStorage.getItem(k);
        if (isolated && k === LS_LOCAL && v === null) v = localStorage.getItem(k);
        var parsed = v === null ? null : JSON.parse(v);
        delete storageErrors[k];
        return parsed;
      } catch (e) {
        storageErrors[k] = '本地数据读取失败';
        return null;
      }
    }
    function write(k, v) {
      try {
        if (isolated) {
          if (k !== LS_LOCAL) return false;
          sessionStorage.setItem(SESSION_LOCAL, JSON.stringify(v));
        } else localStorage.setItem(k, JSON.stringify(v));
        delete storageErrors[k];
        return true;
      } catch (e) {
        storageErrors[k] = '本地保存失败，尚未持久化；请导出备份，刷新可能丢失改动';
        return false;
      }
    }
    function stamp(v, field) {
      if (
        typeof v !== 'string' ||
        !/^\d{4}-\d\d-\d\d(?:[T ]\d\d:\d\d(?::\d\d(?:\.\d{1,3})?)?(?:Z|[+-]\d\d:\d\d)?)?$/.test(v) ||
        !isFinite(Date.parse(v)) ||
        new Date(v.slice(0, 10) + 'T00:00:00Z').toISOString().slice(0, 10) !== v.slice(0, 10)
      )
        throw new Error(field + ' 必须是有效日期');
      return v;
    }
    function amount(o, k, fallback) {
      var v = own(o, k) ? o[k] : fallback;
      if (typeof v !== 'number' || !isFinite(v) || v < 0) throw new Error(k + ' 必须是非负有限数值');
      return v;
    }
    function normalize(j, seed) {
      if (!object(j) || !object(j.items)) throw new Error('items 必须是对象');
      var version = own(j, 'schema_version') ? j.schema_version : 1;
      if (version !== 1 && version !== SCHEMA_VERSION) throw new Error('不支持的 schema_version');
      if (own(j, 'real') && typeof j.real !== 'boolean') throw new Error('real 必须是布尔值');
      var base = version === 1 && seed ? seed : {};
      // 白名单构造：只消费仓位字段，不读取、迁移或上传其他历史字段。
      var d = {
        schema_version: SCHEMA_VERSION,
        real: true,
        updated_at: own(j, 'updated_at') ? stamp(j.updated_at, 'updated_at') : new Date().toISOString(),
        cash: amount(j, 'cash', num(base.cash)),
        cash_extra: amount(j, 'cash_extra', 0),
        fixed: amount(j, 'fixed', num(base.fixed)),
        fixed_cost: amount(j, 'fixed_cost', num(base.fixed_cost)),
        items: {},
      };
      if (d.cash_extra > d.cash) throw new Error('第二笔现金不能大于现金合计');
      Object.keys(j.items).forEach(function (k) {
        if (!/^[A-Z0-9.^=_-]{1,32}$/.test(k) || !object(j.items[k]))
          throw new Error('非法标的或持仓对象：' + k);
        var b = (base.items && base.items[k]) || {};
        d.items[k] = { qty: amount(j.items[k], 'qty', num(b.qty)) };
        if (own(j.items[k], 'cost')) {
          if (typeof j.items[k].cost !== 'number' || !isFinite(j.items[k].cost)) throw new Error('cost 必须是有限数值');
          d.items[k].cost = j.items[k].cost;
        }
      });
      return d;
    }
    function canonical(d) {
      function sorted(v) {
        if (!object(v)) return v;
        var o = {};
        Object.keys(v)
          .sort()
          .forEach(function (k) {
            o[k] = sorted(v[k]);
          });
        return o;
      }
      return JSON.stringify(
        sorted({
          items: d.items,
          cash: d.cash,
          cash_extra: d.cash_extra || 0,
          fixed: d.fixed,
          fixed_cost: d.fixed_cost,
        }),
      );
    }
    function money(n) {
      if (n === null) return '—';
      var r = Math.round(num(n) * 10) / 10,
        a = Math.abs(r),
        s = r < 0 ? '-' : '+';
      if (a >= 999950) return s + '$' + (a / 1e6).toFixed(2) + 'M';
      if (a >= 1e5) return s + '$' + (a / 1e3).toFixed(1) + 'k';
      return (
        s +
        '$' +
        a.toLocaleString('en-US', {
          minimumFractionDigits: a >= 1e4 ? 0 : 1,
          maximumFractionDigits: a >= 1e4 ? 0 : 1,
        })
      );
    }
    function plain(n) {
      var r = Math.round(n);
      return Math.abs(r) >= 1e6
        ? '$' + (r < 0 ? '-' : '') + (Math.abs(r) / 1e6).toFixed(2) + 'M'
        : '$' + r.toLocaleString('en-US');
    }
    function qtyInt(n) {
      var r = Math.round(num(n)),
        a = Math.abs(r),
        s = r < 0 ? '-' : '';
      return a >= 1e6
        ? s + (a / 1e6).toFixed(2) + 'M'
        : a >= 1e5
          ? s + Math.round(a / 1e3) + 'k'
          : s + a.toLocaleString('en-US');
    }
    function pct(n) {
      return typeof n === 'number' && isFinite(n) ? (n >= 0 ? '+' : '') + n.toFixed(1) + '%' : '—';
    }
    function trim(n) {
      var v = num(n),
        a = Math.abs(v),
        d = a >= 10000 ? 0 : a >= 1000 ? 1 : a >= 100 ? 2 : a >= 10 ? 3 : 4,
        p = Math.pow(10, d),
        s = (Math.round(v * p) / p).toFixed(d);
      return s.indexOf('.') >= 0 ? s.replace(/0+$/, '').replace(/\.$/, '') : s;
    }
    function trim4(n) {
      return (Math.round(num(n) * 10000) / 10000).toFixed(4).replace(/0+$/, '').replace(/\.$/, '');
    }
    function cashFirst(d) {
      return Math.max(0, Math.round((num(d.cash) - num(d.cash_extra)) * 10000) / 10000);
    }
    function sumOf(a, f) {
      return a.reduce(function (s, x) {
        return s + f(x);
      }, 0);
    }
    // 所有阈值以十进制交叉相乘判断，显示舍入不能参与边界判定。
    function scaled(values) {
      var parts = values.map(function (v) {
        var s = String(v).toLowerCase().split('e'),
          p = s[0].split('.');
        return { n: BigInt(p.join('')), p: (p[1] || '').length - (Number(s[1]) || 0) };
      });
      var places = Math.max.apply(
        null,
        [0].concat(
          parts.map(function (p) {
            return p.p;
          }),
        ),
      );
      return parts.map(function (p) {
        return p.n * 10n ** BigInt(places - p.p);
      });
    }
    function nearPrice(price, target, limit) {
      var v = scaled([price, target]),
        d = v[0] - v[1];
      if (d < 0n) d = -d;
      var g = scaled([limit, 100]);
      return d * g[1] <= v[0] * g[0];
    }
    function pnlTone(price, cost) {
      if (!(cost > 0)) return '';
      var v = scaled([price, cost]),
        d = v[0] - v[1];
      return d * 100n >= v[1] * 10n ? 'gain' : -d * 100n >= v[1] * 10n ? 'loss' : '';
    }

    var ROWS = [],
      ROW_BY_SYM = Object.create(null);
    function live(r) {
      return !!(r && document.documentElement.contains(r.tr));
    }
    function isPosition(r) {
      return live(r) && !!r.tr.closest('#group_positions');
    }
    function rowBySym(sym) {
      return ROW_BY_SYM[sym] || null;
    }
    all('tr[data-symbol],tr[id^="s-"]').forEach(function (tr) {
      var sd = tr.querySelector('td.sym'),
        px = tr.querySelector('.px-price');
      if (!sd || !px) return;
      // 原始行情优先；仅旧页缺失或无效原值时退回显示值。
      var rawPrice = Number(tr.getAttribute('data-price'));
      var price = isFinite(rawPrice) && rawPrice > 0 ? rawPrice : num(px.textContent.replace(/,/g, ''));
      if (!(price > 0)) return;
      var sym = tr.getAttribute('data-symbol');
      if (!sym) {
        var source = sd.querySelector('.sym-box') || sd;
        sym = Array.prototype.map
          .call(source.childNodes, function (n) {
            return n.nodeType === 3 ? n.textContent : '';
          })
          .join('')
          .trim();
      }
      sym = String(sym || tr.id.slice(2))
        .trim()
        .toUpperCase();
      var note = sd.querySelector('.note'),
        prev = Number(tr.getAttribute('data-prev-close')),
        chg = tr.querySelector('.px-chg');
      if (!isFinite(prev) || prev <= 0) prev = 0;
      var railRow = tr.nextElementSibling,
        rail = railRow && railRow.querySelector('.range52-rail'),
        lab = rail ? rail.getAttribute('aria-label') || '' : '';
      var m = lab.match(
        /([\d,]+\.\d{2})[低高]*，本轮价 ([\d,]+\.\d{2}) 位于区间 ([\d.]+)%，[^，]*高 ([\d,]+\.\d{2})/,
      );
      var tick = rail && rail.querySelector('.range52-cost');
      if (rail && !tick) {
        tick = document.createElement('span');
        tick.className = 'range52-cost';
        (rail.querySelector('.range52-track') || rail).appendChild(tick);
      }
      var cap = tr.querySelector('.lvtag.add'),
        cutCap = tr.querySelector('.lvtag.cut');
      var r = {
        sym: sym,
        tr: tr,
        note: note ? note.getAttribute('data-full') || note.textContent.trim() : '',
        isEtf: !!sd.querySelector('.tag'),
        isLong: !!tr.closest('#group_positions_long'),
        price: price,
        prevClose: prev > 0 ? prev : 0,
        chg: prev > 0 ? (price / prev - 1) * 100 : num(chg && chg.textContent),
        lv:
          LV_ORDER.filter(function (l) {
            return tr.classList.contains(l);
          })[0] || 'gray',
        rail: rail,
        tick: tick,
        lab0: lab,
        low: m ? num(m[1].replace(/,/g, '')) : 0,
        high: m ? num(m[4].replace(/,/g, '')) : 0,
        lowTxt: m ? m[1] : '',
        highTxt: m ? m[4] : '',
        costLine: tr.querySelector('td.cost-line'),
        cap: cap,
        cutCap: cutCap,
        trig: cap ? num(cap.getAttribute('data-lv')) : 0,
        cut: cutCap ? num(cutCap.getAttribute('data-lv')) : 0,
      };
      ROWS.push(r);
      if (!ROW_BY_SYM[sym]) ROW_BY_SYM[sym] = r;
    });

    var GH = { owner: 'nixhuang', repo: 'market-monitor', path: 'position-costs.json', branch: 'data' };
    var GH_API = 'https://api.github.com/repos/' + GH.owner + '/' + GH.repo + '/contents/' + GH.path;
    var CLOUD = null,
      cloudVerified = false,
      SYNC = 'pulling',
      SYNC_MSG = '',
      cloudFirstPending = true;
    var positionsAuthorized = false,
      positionSave = null,
      positionGeneration = 0,
      draftPending = false,
      editDraft = {},
      snapshotDraft = null,
      clearDraft = {},
      addDraft = {},
      addedPositions = {},
      editBaseline = null;
    var DATA,
      revision = 0,
      dirty = false,
      writer = false,
      releaseWriter = null,
      acquiring = false,
      disposed = false,
      uncertain = null,
      pullGeneration = 0;
    var _pushTimer = null,
      _retryN = 0,
      _retryTimer = null,
      inFlight = null,
      queued = false,
      savePaused = false,
      _pullTry = 0,
      _pullTimer = null;
    var editing = false,
      snapshot = null,
      prevDirty = false,
      prevDraftPending = false,
      editOrder = {},
      editGeneration = 0,
      _paintFrame = null;
    var cloud = read(LS_CLOUD),
      local = read(LS_LOCAL),
      hadOwn = !!(local && object(local.items));
    var seedAsOf = REAL && REAL.asof ? String(REAL.asof).slice(0, 10) : '';
    function draftError(raw, field) {
      var t = raw.trim(),
        v = t === '' ? 0 : Number(t),
        decimal = field === 'cost' ? /^(?:-?(?:\d+(?:\.\d{0,4})?|\.\d{1,4}))?$/ : /^(?:\d+(?:\.\d{0,4})?|\.\d{1,4})?$/;
      return decimal.test(t) && isFinite(v) && (field === 'cost' || v >= 0)
        ? ''
        : (field === 'cost' ? '请输入有效成本，最多四位小数' : '请输入非负数，最多四位小数');
    }
    function draftEntryError(entry, field) {
      var error = draftError(entry.raw, field);
      if (!error && own(entry, 'exact') &&
          (field !== 'cost' || typeof entry.exact !== 'number' || !isFinite(entry.exact) ||
           entry.raw.trim() === '' || entry.raw.trim() !== trim4(entry.exact)))
        error = '自动成本草稿原值无效或与显示值不一致，请手填修正';
      return error;
    }
    function checkedDraft() {
      var next = clone(DATA),
        invalid = false,
        first = cashFirst(DATA),
        second = DATA.cash_extra;
      Object.keys(editDraft).forEach(function (k) {
        var e = editDraft[k],
          p = k.split('|'),
          f = p[1];
        e.error = draftEntryError(e, f);
        if (e.error) {
          invalid = true;
          return;
        }
        var v = own(e, 'exact') ? e.exact : e.raw.trim() === '' ? 0 : Number(e.raw);
        if (f === 'cash') first = v;
        else if (f === 'cash_extra') second = v;
        else if (f === 'fixed' || f === 'fixed_cost') next[f] = v;
        else {
          if (!next.items[p[0]]) next.items[p[0]] = { qty: 0 };
          if (f === 'cost' && e.raw.trim() === '') delete next.items[p[0]].cost;
          else next.items[p[0]][f] = v;
        }
      });
      if (own(editDraft, 'USD|cash') || own(editDraft, 'USD|cash_extra')) {
        var total = (Math.round(first * 10000) + Math.round(second * 10000)) / 10000;
        if (!isFinite(total)) {
          ['USD|cash', 'USD|cash_extra'].forEach(function (k) {
            if (editDraft[k]) editDraft[k].error = '现金合计过大';
          });
          invalid = true;
        }
        next.cash = total;
        next.cash_extra = second;
      }
      return invalid ? null : next;
    }
    function draftNotice() {
      return Object.keys(editDraft).some(function (k) {
        return !!editDraft[k].error;
      })
        ? '存在无效输入，请修正后保存'
        : '';
    }
    function clearSaveTimers() {
      if (_pushTimer) clearTimeout(_pushTimer);
      if (_retryTimer) clearTimeout(_retryTimer);
      _pushTimer = null;
      _retryTimer = null;
      queued = false;
    }
    function pruneAddedPositions() {
      Object.keys(addedPositions).forEach(function (sym) {
        if (isPosition(rowBySym(sym)) || !(DATA.items[sym] && DATA.items[sym].qty > 0)) delete addedPositions[sym];
      });
    }
    function loadLocal() {
      reloadClearRecord();
      // 每个生命周期撤销上传授权；刷新、bfCache、接管均不得复活旧保存事务。
      uncertain = null;
      cloudFirstPending = true;
      positionSave = null;
      positionsAuthorized = false;
      draftPending = false;
      editDraft = {};
      snapshotDraft = null;
      CLOUD = null;
      cloudVerified = false;
      clearSaveTimers();
      _retryN = 0;
      var j = read(LS_LOCAL);
      try {
        DATA = normalize(j || cloud || REAL || { items: {} }, j ? REAL : null);
        if (j) hadOwn = true;
        var saved = j && j._local && j._local.editDraft;
        if (object(saved))
          Object.keys(saved).forEach(function (k) {
            var e = saved[k];
            if (
              /^(?:[A-Z0-9.^=_-]{1,32}\|(?:cost|qty)|USD\|cash(?:_extra)?|BOND\|fixed(?:_cost)?)$/.test(k) &&
              object(e) &&
              typeof e.raw === 'string'
            ) {
              editDraft[k] = { raw: e.raw, error: '' };
              if (own(e, 'exact')) editDraft[k].exact = e.exact;
              editDraft[k].error = draftEntryError(editDraft[k], k.split('|')[1]);
            }
          });
        clearDraft = {};
        var marks = j && j._local && j._local.clearDraft;
        if (object(marks)) Object.keys(marks).forEach(function (sym) {
          if (/^[A-Z0-9.^=_-]{1,32}$/.test(sym) && ['default', 'focus'].indexOf(marks[sym]) >= 0)
            clearDraft[sym] = marks[sym];
        });
        addDraft = {}; addedPositions = {};
        ['addDraft', 'addedPositions'].forEach(function (key) {
          var entries = j && j._local && j._local[key];
          if (object(entries)) Object.keys(entries).forEach(function (sym) {
            if (/^[A-Z0-9.^=_-]{1,32}$/.test(sym) && object(entries[sym]) && ['long', 'short'].indexOf(entries[sym].hold) >= 0)
              (key === 'addDraft' ? addDraft : addedPositions)[sym] = clone(entries[sym]);
            if (key === 'addDraft' && own(entries[sym], 'extra') && !validExtra(entries[sym].extra)) delete addDraft[sym].extra;
          });
        });
        editBaseline = null;
        if ((Object.keys(clearDraft).length || Object.keys(addDraft).length || (clearRecord && clearRecord.step !== 'done')) && j && j._local && j._local.editBaseline)
          editBaseline = normalize(j._local.editBaseline);
        draftPending = Object.keys(clearDraft).length + Object.keys(addDraft).length > 0;
        checkedDraft();
        pruneAddedPositions();
        revision = j && j._local && Number.isSafeInteger(j._local.revision) ? j._local.revision : 0;
        dirty = !!(j && j._local ? j._local.dirty : read(LS_PENDING) === 1);
        CLOUD = cloud ? normalize(cloud) : null;
        if (storageErrors[LS_LOCAL] || storageErrors[LS_PENDING]) {
          dirty = true;
          return false;
        }
        return true;
      } catch (e) {
        storageErrors[LS_LOCAL] = '本地数据校验失败：' + e.message + '；原数据未覆盖，请导出或导入有效备份';
        if (!DATA) DATA = normalize(REAL || { items: {} });
        dirty = true;
        return false;
      }
    }
    loadLocal();
    function persist() {
      if (!writer) return false;
      var saved = clone(DATA);
      saved._local = { revision: revision, dirty: dirty, uncertain: uncertain, editDraft: editDraft,
        clearDraft: clearDraft, addDraft: addDraft, addedPositions: addedPositions, editBaseline: editBaseline };
      return write(LS_LOCAL, saved);
    }
    function setPending(v) {
      dirty = !!v;
      if (!persist()) {
        dirty = true;
        return false;
      }
      return true;
    }
    function changed() {
      revision++;
      pullGeneration++;
      DATA.updated_at = new Date().toISOString();
      dirty = true;
      SYNC = 'local';
      SYNC_MSG = '';
      persist();
    }
    function authorizePositions(fixedBaseline, fieldBaseline) {
      positionSave = { generation: ++positionGeneration, payload: clone(DATA) };
      if (fixedBaseline) positionSave.fixedBaseline = fixedBaseline;
      if (fieldBaseline) positionSave.fieldBaseline = fieldBaseline;
      positionsAuthorized = true;
    }
    function consumePositions(g) {
      if (positionSave && positionSave.generation === g) {
        positionSave = null;
        positionsAuthorized = false;
      }
    }
    function sameAsCloud() {
      return !uncertain && cloudVerified && !!CLOUD && canonical(DATA) === canonical(CLOUD);
    }
    function ghToken() {
      if (isolated) return '';
      try {
        return (localStorage.getItem(LS_TOKEN) || '').trim();
      } catch (e) {
        return '';
      }
    }
    function ghHead(json) {
      var t = ghToken();
      if (!t) return null;
      var h = {
        Authorization: 'Bearer ' + t,
        Accept: 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
      };
      if (json) h['Content-Type'] = 'application/json';
      return h;
    }
    function b64(str) {
      var bytes = new TextEncoder().encode(str),
        s = '';
      for (var i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
      return btoa(s);
    }
    function unb64(str) {
      var bin = atob(String(str || '').replace(/\s+/g, '')),
        bytes = new Uint8Array(bin.length);
      for (var i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
      return new TextDecoder().decode(bytes);
    }
    function cloudReadError(msg, retryable, kind) {
      var e = new Error(msg);
      e.retryable = retryable;
      e.kind = kind;
      return e;
    }
    var REQUEST_DEADLINE = 60000,
      activeRequests = [];
    function requestJson(url, options) {
      return new Promise(function (resolve, reject) {
        var controller = typeof AbortController === 'function' ? new AbortController() : null,
          finished = false,
          timer;
        function finish(error, result) {
          if (finished) return;
          finished = true;
          clearTimeout(timer);
          activeRequests = activeRequests.filter(function (x) {
            return x !== cancel;
          });
          if (error) {
            if (controller) controller.abort();
            reject(error);
          } else resolve(result);
        }
        function cancel() {
          finish(new Error('页面已退出，请求已取消'));
        }
        activeRequests.push(cancel);
        timer = setTimeout(function () {
          finish(new Error('同步请求超时（含响应体读取），请重试核验'));
        }, REQUEST_DEADLINE);
        var init = Object.assign({}, options || {});
        if (controller) init.signal = controller.signal;
        Promise.resolve()
          .then(function () {
            if (!finished) return fetch(url, init);
          })
          .then(function (r) {
            if (finished || !r) return;
            return r.json().then(
              function (data) {
                finish(null, { status: r.status, ok: r.ok, data: data });
              },
              function (e) {
                if (!r.ok) finish(null, { status: r.status, ok: false, data: null });
                else if (e && e.name === 'SyntaxError')
                  finish(cloudReadError('响应内容不是合法 JSON', false, 'data'));
                else
                  finish(
                    cloudReadError('响应体读取失败：' + ((e && e.message) || '网络中断'), true, 'network'),
                  );
              },
            );
          })
          .catch(function (e) {
            finish(e);
          });
      });
    }
    function parseCloud(content, from) {
      var raw;
      try {
        raw = unb64(content);
      } catch (e) {
        throw cloudReadError(from + '返回的内容不是合法 base64', false, 'data');
      }
      try {
        return JSON.parse(raw);
      } catch (e) {
        throw cloudReadError(from + '内容解析失败，不是合法 JSON', false, 'data');
      }
    }
    function readCloudSource(url, options, from, encoded) {
      return requestJson(url, options)
        .then(function (r) {
          if (!r.ok)
            throw cloudReadError(
              'HTTP ' + r.status,
              r.status === 408 ||
                r.status === 429 ||
                r.status >= 500 ||
                (r.status === 403 &&
                  /rate limit|secondary rate|abuse detection/i.test((r.data && r.data.message) || '')),
              'http',
            );
          var j = encoded ? (r.data && r.data.content ? parseCloud(r.data.content, from) : null) : r.data;
          if (!j || !object(j.items)) throw cloudReadError('持仓内容缺少有效 items 对象', false, 'data');
          return j;
        })
        .catch(function (e) {
          var m = (e && e.message) || '网络请求失败';
          throw cloudReadError(
            m.indexOf(from) === 0 ? m : from + '：' + m,
            !e || e.retryable !== false,
            (e && e.kind) || 'network',
          );
        });
    }
    function cloudSources() {
      var list = [],
        h = ghHead();
      if (h)
        list.push(function () {
          return readCloudSource(
            GH_API + '?ref=' + GH.branch + '&t=' + Date.now(),
            { headers: h, cache: 'no-store' },
            'GitHub API',
            true,
          );
        });
      list.push(function () {
        return readCloudSource(
          'https://raw.githubusercontent.com/' +
            GH.owner +
            '/' +
            GH.repo +
            '/' +
            GH.branch +
            '/' +
            GH.path +
            '?t=' +
            Date.now(),
          { cache: 'no-store' },
          'GitHub raw',
          false,
        );
      });
      return list;
    }
    function pullCloud(cb) {
      var sources = cloudSources(),
        i = 0,
        errors = [];
      (function next() {
        if (disposed || !writer) return;
        if (i >= sources.length) {
          cb(
            null,
            cloudReadError(
              errors
                .map(function (e) {
                  return e.message;
                })
                .join('；'),
              !errors.some(function (e) {
                return e.kind === 'data';
              }) &&
                errors.some(function (e) {
                  return e.retryable !== false;
                }),
            ),
          );
          return;
        }
        sources[i++]().then(
          function (j) {
            cb(j, null);
          },
          function (e) {
            errors.push(e);
            next();
          },
        );
      })();
    }
    function mergeCloud(j, cloudWins) {
      if (isolated || !j || !writer || disposed || inFlight) return false;
      var remote = normalize(j);
      CLOUD = remote;
      cloudVerified = true;
      var cached = write(LS_CLOUD, remote);
      if (editing || draftPending || Object.keys(addDraft).length || (clearRecord && clearRecord.step !== 'done')) return false;
      if (cloudWins) {
        if (positionsAuthorized) return false;
        clearSaveTimers();
        _retryN = 0;
        uncertain = null;
        dirty = false;
        if (!write(LS_PENDING, 0)) cached = false;
      } else if (dirty || uncertain) return false;
      cloudFirstPending = false;
      var different = canonical(remote) !== canonical(DATA);
      DATA = clone(remote);
      editDraft = {};
      pruneAddedPositions();
      if (different) revision++;
      dirty = !cached;
      if (!persist()) dirty = true;
      return different;
    }
    function startPull() {
      if (isolated || !writer || disposed || clearBusy) return;
      if (_pullTimer) {
        clearTimeout(_pullTimer);
        _pullTimer = null;
      }
      if (navigator.onLine === false) {
        SYNC = inFlight ? 'loading' : 'nocloud';
        NOTICE = inFlight ? '' : '设备离线，暂不读取云端';
        paint();
        return;
      }
      var generation = ++pullGeneration;
      SYNC = 'pulling';
      pullCloud(function (j, error) {
        if (disposed || !writer || generation !== pullGeneration) return;
        if (j) {
          try {
            mergeCloud(j, cloudFirstPending);
            _pullTry = 0;
            NOTICE = '';
            SYNC = inFlight ? 'loading' : !dirty && sameAsCloud() ? 'synced' : 'local';
          } catch (e) {
            SYNC = 'nocloud';
            NOTICE = '云端数据校验失败：' + e.message;
          }
        } else {
          SYNC = inFlight ? 'loading' : 'nocloud';
          NOTICE = '云端读取失败：' + ((error && error.message) || '未取得有效持仓数据');
          if ((!error || error.retryable !== false) && _pullTry < 2) {
            _pullTry++;
            _pullTimer = setTimeout(startPull, _pullTry === 1 ? 5000 : 20000);
          }
        }
        paint();
      });
    }
    function pushCloud(sent, cb) {
      var h = ghHead(true);
      if (!h) {
        cb(false, '未填 token');
        return;
      }
      requestJson(GH_API + '?ref=' + GH.branch + '&t=' + Date.now(), { headers: ghHead(), cache: 'no-store' })
        .then(function (r) {
          if (r.status === 404) return null;
          if (!r.ok) throw new Error('读取远端版本失败：HTTP ' + r.status);
          return r.data;
        })
        .then(function (meta) {
          if (disposed || !writer) throw new Error('页面已退出，上传已取消');
          var remote = meta && meta.content ? normalize(parseCloud(meta.content, '远端版本')) : null;
          if (sent.recovery) {
            if (meta && (!remote || !meta.sha))
              throw new Error('上传结果未知，远端内容及 SHA 未完整核验，暂停重传');
            if (remote && canonical(remote) === canonical(sent.payload)) {
              cb(true, '已核验上次上传', remote);
              return null;
            }
            if ((meta ? meta.sha : null) !== sent.baseSha) {
              cb(false, '远端已更新，上次上传事务结束', remote, 'superseded');
              return null;
            }
          }
          if (remote && canonical(remote) === canonical(sent.payload)) {
            cb(true, '内容已一致', remote);
            return null;
          }
          if (sent.fixedBaseline && remote && ['fixed', 'fixed_cost'].some(function (f) {
            return remote[f] !== sent.fixedBaseline[f];
          })) {
            cb(false, '固收同字段已被其它设备修改，请核对后重新编辑保存', remote, 'fixed-conflict');
            return null;
          }
          if (sent.fieldBaseline && remote && Object.keys(sent.fieldBaseline).some(function (key) {
            return clearField(remote, key) !== sent.fieldBaseline[key];
          })) {
            cb(false, '同字段已被其它设备修改，请核对后重新编辑保存', remote, 'field-conflict');
            return null;
          }
          if (meta && !meta.sha) throw new Error('缺少远端 SHA，禁止覆盖');
          if (sent.fieldBaseline && remote && !sent.recovery) {
            var merged = clone(remote), first = cashFirst(remote), second = remote.cash_extra;
            Object.keys(sent.fieldBaseline).forEach(function (key) {
              var p = key.split('|'), f = p[1], value = clearField(sent.payload, key);
              if (f === 'cash') first = value;
              else if (f === 'cash_extra') second = value;
              else if (f === 'fixed' || f === 'fixed_cost') merged[f] = value;
              else {
                if (!merged.items[p[0]]) merged.items[p[0]] = { qty: 0 };
                if (value === null) delete merged.items[p[0]][f]; else merged.items[p[0]][f] = value;
              }
            });
            merged.cash = (Math.round(first * 10000) + Math.round(second * 10000)) / 10000;
            merged.cash_extra = second; merged.updated_at = new Date().toISOString();
            sent.payload = normalize(merged);
          }
          uncertain = {
            payload: clone(sent.payload),
            revision: sent.revision,
            baseSha: sent.recovery ? sent.baseSha : meta ? meta.sha : null,
            positionGeneration: sent.positionGeneration,
            fieldBaseline: sent.fieldBaseline || null,
            fixedBaseline: sent.fixedBaseline || null,
          };
          if (!persist()) throw new Error('上传事务尚未持久化，暂停上传');
          if (!write(LS_PUSH_AT, Date.now())) throw new Error('上传节流状态未持久化');
          var body = {
            message: '持仓同步 ' + new Date().toISOString().slice(0, 19).replace('T', ' '),
            content: b64(JSON.stringify(sent.payload, null, 1)),
            branch: GH.branch,
          };
          if (uncertain.baseSha) body.sha = uncertain.baseSha;
          return requestJson(GH_API, { method: 'PUT', headers: h, body: JSON.stringify(body) });
        })
        .then(function (r) {
          if (!r) return;
          var ok = r.status === 200 || r.status === 201;
          cb(ok, ok ? '已上传' : (r.data && r.data.message) || 'HTTP ' + r.status);
        })
        .catch(function (e) {
          cb(false, e.message || '网络失败');
        });
    }
    function requestSave(delay, resetRetry) {
      if (clearBusy) return;
      if (!writer || disposed) return;
      if (isolated) {
        persist();
        SYNC = 'local';
        paint();
        return;
      }
      if (resetRetry) {
        _retryN = 0;
        if (_retryTimer) clearTimeout(_retryTimer);
        _retryTimer = null;
      }
      queued = true;
      if (!ghToken()) {
        SYNC = 'local';
        SYNC_MSG = '未填 GitHub token';
        paint();
        return;
      }
      if (editing || inFlight || savePaused || (!positionsAuthorized && !uncertain)) return;
      if (_pushTimer) clearTimeout(_pushTimer);
      _pushTimer = setTimeout(function () {
        _pushTimer = null;
        doPush();
      }, delay || 0);
    }
    function scheduleRetry() {
      if (_retryN >= 2 || !ghToken() || disposed) return;
      _retryN++;
      if (_retryTimer) clearTimeout(_retryTimer);
      _retryTimer = setTimeout(
        function () {
          _retryTimer = null;
          requestSave(0, false);
        },
        _retryN === 1 ? 15000 : 60000,
      );
    }
    function doPush() {
      if (
        !writer ||
        disposed ||
        editing ||
        clearBusy ||
        savePaused ||
        inFlight ||
        !queued ||
        (!dirty && !uncertain) ||
        (!positionsAuthorized && !uncertain) ||
        !ghToken()
      )
        return;
      if (!persist()) {
        paint();
        return;
      }
      if (
        !draftPending &&
        sameAsCloud() &&
        (!positionSave || canonical(positionSave.payload) === canonical(DATA))
      ) {
        if (!write(LS_CLOUD, CLOUD)) {
          paint();
          return;
        }
        if (positionSave) consumePositions(positionSave.generation);
        queued = false;
        _retryN = 0;
        SYNC = setPending(false) ? 'synced' : 'local';
        paint();
        return;
      }
      var last = read(LS_PUSH_AT),
        wait = typeof last === 'number' ? 15000 - (Date.now() - last) : 0;
      if (storageErrors[LS_PUSH_AT]) {
        paint();
        return;
      }
      if (wait > 0) {
        _pushTimer = setTimeout(function () {
          _pushTimer = null;
          doPush();
        }, wait);
        return;
      }
      if (!write(LS_PUSH_AT, Date.now())) {
        paint();
        return;
      }
      // 冻结用户本页明确保存的快照。GET、重绘、换token均不能创建上传权限。
      var sent = uncertain
        ? Object.assign(clone(uncertain), { recovery: true })
        : {
            payload: clone(positionSave.payload),
            revision: revision,
            positionGeneration: positionSave.generation,
            fixedBaseline: positionSave.fixedBaseline || null,
            fieldBaseline: positionSave.fieldBaseline || null,
          };
      inFlight = sent;
      queued = false;
      pullGeneration++;
      SYNC = 'loading';
      paint();
      pushCloud(sent, function (ok, msg, verifiedRemote, outcome) {
        if (inFlight !== sent) return;
        inFlight = null;
        if (disposed) {
          if (releaseWriter) {
            writer = false;
            releaseWriter();
            releaseWriter = null;
          }
          return;
        }
        if (outcome === 'fixed-conflict' || outcome === 'field-conflict') {
          consumePositions(sent.positionGeneration);
          CLOUD = verifiedRemote; cloudVerified = true;
          write(LS_CLOUD, CLOUD);
          dirty = true; persist();
          SYNC = 'error'; SYNC_MSG = msg;
        } else if (outcome === 'superseded') {
          uncertain = null;
          cloudVerified = false;
          dirty = true;
          persist();
          SYNC = 'local';
          SYNC_MSG = '';
          requestSave(0, false);
        } else if (ok) {
          uncertain = null;
          // 在途旧保存成功时，仅将新保存仍沿用的同字段基线推进到自己的已提交值。
          if (positionSave && positionSave.generation !== sent.positionGeneration && positionSave.fieldBaseline && sent.fieldBaseline)
            Object.keys(positionSave.fieldBaseline).forEach(function (key) {
              if (own(sent.fieldBaseline, key) && positionSave.fieldBaseline[key] === sent.fieldBaseline[key])
                positionSave.fieldBaseline[key] = clearField(verifiedRemote || sent.payload, key);
            });
          if (positionSave && positionSave.generation !== sent.positionGeneration && positionSave.fixedBaseline && sent.fixedBaseline)
            ['fixed', 'fixed_cost'].forEach(function (field) {
              if (positionSave.fixedBaseline[field] === sent.fixedBaseline[field])
                positionSave.fixedBaseline[field] = (verifiedRemote || sent.payload)[field];
            });
          if (editing && editBaseline && sent.fieldBaseline) {
            var rebased = clone(editBaseline), first = cashFirst(rebased), second = rebased.cash_extra;
            Object.keys(sent.fieldBaseline).forEach(function (key) {
              if (clearField(editBaseline, key) !== sent.fieldBaseline[key]) return;
              var p = key.split('|'), f = p[1], value = clearField(verifiedRemote || sent.payload, key);
              if (f === 'cash') first = value;
              else if (f === 'cash_extra') second = value;
              else if (f === 'fixed' || f === 'fixed_cost') rebased[f] = value;
              else {
                if (!rebased.items[p[0]]) rebased.items[p[0]] = { qty: 0 };
                if (value === null) delete rebased.items[p[0]][f]; else rebased.items[p[0]][f] = value;
              }
            });
            rebased.cash = (Math.round(first * 10000) + Math.round(second * 10000)) / 10000;
            rebased.cash_extra = second; editBaseline = rebased;
          }
          consumePositions(sent.positionGeneration);
          CLOUD = verifiedRemote || sent.payload;
          cloudVerified = true;
          var cached = write(LS_CLOUD, CLOUD);
          _retryN = 0;
          SYNC_MSG = '';
          if (!positionsAuthorized && !draftPending && !editing) DATA = clone(CLOUD);
          if (cached && !positionsAuthorized && !draftPending && canonical(DATA) === canonical(CLOUD))
            SYNC = setPending(false) ? 'synced' : 'local';
          else {
            dirty = true;
            persist();
            SYNC = 'local';
          }
          if (dirty) requestSave(0, false);
        } else {
          if (uncertain) cloudVerified = false;
          dirty = true;
          persist();
          SYNC = 'error';
          SYNC_MSG = (msg || '上传失败') + (uncertain ? '；PUT 结果未确认，重试将先核验远端' : '');
          scheduleRetry();
        }
        paint();
      });
    }
    function takeOwnership(cb) {
      if (writer) {
        cb(true);
        return;
      }
      if (acquiring || disposed) {
        cb(false);
        return;
      }
      if (isolated) {
        writer = true;
        cloud = null;
        savePaused = false;
        if (loadLocal()) persist();
        cb(true);
        return;
      }
      acquiring = true;
      navigator.locks
        .request(LOCK_NAME, { mode: 'exclusive', ifAvailable: true }, function (lock) {
          acquiring = false;
          if (!lock || disposed) {
            NOTICE = READ_ONLY_NOTICE;
            cb(false);
            return;
          }
          writer = true;
          pullGeneration++;
          savePaused = false;
          cloud = read(LS_CLOUD);
          var valid = loadLocal();
          NOTICE = '';
          SYNC = 'pulling';
          _pullTry = 0;
          if (_pullTimer) {
            clearTimeout(_pullTimer);
            _pullTimer = null;
          }
          return new Promise(function (resolve) {
            releaseWriter = resolve;
            if (valid) persist();
            cb(true);
            startPull();
            paint();
          });
        })
        .catch(function () {
          acquiring = false;
          writer = false;
          releaseWriter = null;
          NOTICE = READ_ONLY_NOTICE;
          cb(false);
        });
    }
    window.addEventListener('storage', function (e) {
      if (isolated || writer || disposed || (e.key !== LS_LOCAL && e.key !== LS_CLOUD)) return;
      cloud = read(LS_CLOUD);
      loadLocal();
      pullGeneration++;
      SYNC = 'pulling';
      paint();
    });
    window.addEventListener('online', function () {
      if (!isolated && writer && !disposed) {
        NOTICE = '';
        _pullTry = 0;
        startPull();
      }
    });
    window.addEventListener('pagehide', function () {
      disposed = true;
      pullGeneration++;
      clearSaveTimers();
      editing = false;
      snapshot = null;
      savePaused = true;
      editGeneration++;
      if (_pullTimer) clearTimeout(_pullTimer);
      if (_paintFrame) {
        cancelAnimationFrame(_paintFrame);
        _paintFrame = null;
      }
      activeRequests.slice().forEach(function (cancel) {
        cancel();
      });
      inFlight = null;
      if (isolated) writer = false;
      if (releaseWriter) {
        writer = false;
        releaseWriter();
        releaseWriter = null;
      }
    });
    window.addEventListener('pageshow', function (e) {
      if (e.persisted) {
        disposed = false;
        syncEditUI();
        takeOwnership(function () {
          paint();
        });
      }
    });

    var panel = document.getElementById('costManage');
    if (!panel) return;
    var PID = 'costMembers',
      membersNode = null,
      membersHtml = null,
      stateEl = panel.querySelector('#costState'),
      syncMirror = null;
    var footEl = panel.querySelector('#costFoot'),
      actsEl = panel.querySelector('#costActs'),
      editBtn = document.getElementById('costEditBtn');
    // 红叉只标记草稿；面板保存冻结本次明确字段，data 批量提交后再 main 批量移组。
    // 恢复记录只供面板保存核验，不恢复上传授权。
    var clearBusy = false, clearDialog = null, clearChoice = null;
    var clearRecord = null;
    // 新建仓位弹框里选填的备注、加仓价、减仓价、目标价（目标价 0＝关闭「达到」报灯，与设置页一致）。
    // 只在填了时才出现在草稿/事务记录里，没填的字段不写、不覆盖已有分组里复制来的值。
    function validExtra(x) {
      if (!object(x)) return false;
      var keys = Object.keys(x);
      if (!keys.length) return false;
      return keys.every(function (k) {
        var v = x[k];
        if (k === 'note') return typeof v === 'string' && v.trim() === v && v.length > 0 && Array.from(v).length <= 7;
        if (typeof v !== 'number' || !isFinite(v)) return false;
        if (k === 'trigger') return true;
        if (k === 'reduce') return v > 0;
        if (k === 'target') return v >= 0;
        return false;
      });
    }
    function monthBJ() { return new Date(Date.now() + 8 * 3600 * 1000).toISOString().slice(0, 7); }
    function parseExtra(note, trigger, reduce, target) {
      var out = {};
      note = String(note || '').trim();
      if (note) {
        if (Array.from(note).length > 7) throw new Error('备注最多7个字');
        out.note = note;
      }
      [['trigger', trigger, '加仓价'], ['reduce', reduce, '减仓价'], ['target', target, '目标价']].forEach(function (f) {
        var raw = String(f[1] == null ? '' : f[1]).trim();
        if (!raw) return;
        var n = Number(raw);
        if (!isFinite(n) || (f[0] === 'reduce' && n <= 0) || (f[0] === 'target' && n < 0))
          throw new Error(f[2] + (f[0] === 'trigger' ? '请填有限数字或留空' : f[0] === 'target' ? '请填大于或等于0的数字（0＝关闭达到报灯）或留空' : '请填大于0的数字或留空'));
        out[f[0]] = n;
      });
      return Object.keys(out).length ? out : null;
    }
    function reloadClearRecord() {
      var record = isolated ? null : read(LS_CLEAR);
      if (record === null) { clearRecord = null; return; }
      if (!object(record) || ['quantity', 'move', 'done'].indexOf(record.step) < 0) {
        storageErrors[LS_CLEAR] = '清仓事务记录无效，请保留备份并核对，禁止覆盖'; clearRecord = null; return;
      }
      if (record.version === 2 && Array.isArray(record.targets) && (record.targets.length || (Array.isArray(record.additions) && record.additions.length)) &&
          (!own(record, 'additions') || (Array.isArray(record.additions) && record.additions.every(function (t) {
            return object(t) && /^[A-Z0-9.^=_-]{1,32}$/.test(t.sym || '') && ['long', 'short'].indexOf(t.hold) >= 0 &&
              (!own(t, 'extra') || validExtra(t.extra));
          }))) &&
          record.targets.every(function (t) { return object(t) && /^[A-Z0-9.^=_-]{1,32}$/.test(t.sym || '') && ['default', 'focus'].indexOf(t.choice) >= 0; }))
        clearRecord = record;
      else if (/^[A-Z0-9.^=_-]{1,32}$/.test(record.sym || '') && ['default', 'focus'].indexOf(record.choice) >= 0)
        clearRecord = Object.assign({}, record, { version: 2, legacy: true, targets: [{ sym: record.sym, choice: record.choice,
          source: record.source, destination: record.destination }], payload: record.zero });
      else { storageErrors[LS_CLEAR] = '清仓事务记录无效，请保留备份并核对，禁止覆盖'; clearRecord = null; }
    }
    reloadClearRecord();
    function clearMessage(text) {
      NOTICE = text;
      if (clearDialog) clearDialog.querySelector('[data-clear-message]').textContent = text;
      paint();
    }
    function clearGuard() {
      reloadClearRecord();
      if (disposed || !writer) throw new Error('本页只读，请先点击编辑接管');
      if (isolated) throw new Error('隔离预览不执行真实清仓');
      if (!ghToken()) throw new Error('未填 token，不能保存清仓和分组');
      if (Object.keys(storageErrors).length) throw new Error('本地数据未持久化，暂停清仓');
      if (inFlight || uncertain || positionsAuthorized) throw new Error('有保存事务尚未完成，请先等待核验');
      if (clearRecord && clearRecord.conflict && !clearRecord.evidenceCommit) throw new Error('旧清仓事务存在冲突，禁止重传旧数量；请点面板取消并确认读取远端，安全结束旧事务');
    }
    function saveClearRecord(record) {
      if (!write(LS_CLEAR, record)) throw new Error('清仓进度尚未持久化，未继续写入');
      clearRecord = record;
    }
    function clearConflict(message) {
      saveClearRecord(Object.assign({}, clearRecord, { conflict: true }));
      throw new Error(message + '；可点面板取消，确认读取远端并结束旧事务；已保存数值不回滚');
    }
    function clearActive() {
      if (disposed || !writer || !clearBusy) throw new Error('清仓会话已结束，请重新核验');
    }
    var CONFIG_API = 'https://api.github.com/repos/' + GH.owner + '/' + GH.repo + '/contents/';
    async function clearRead(file, branch) {
      clearActive();
      var r = await requestJson(CONFIG_API + file + '?ref=' + branch + '&t=' + Date.now(),
        { headers: ghHead(), cache: 'no-store' });
      clearActive();
      if (!r.ok || !r.data || !r.data.content || !/^[a-f0-9]{40,64}$/i.test(r.data.sha || ''))
        throw new Error(file + ' 版本读取失败：HTTP ' + r.status + '；未覆盖远端');
      return { sha: r.data.sha, value: parseCloud(r.data.content, file) };
    }
    function clearDestination(config, groups, sym, choice) {
      if (!object(config) || !object(config.positions) || !object(config.focus) || !Array.isArray(groups))
        throw new Error('分组配置无效，未修改');
      var ordinary = groups.filter(function (g) {
        return object(g) && /^[a-z_]+$/.test(g.key || '') && ['positions', 'focus'].indexOf(g.key) < 0;
      });
      var key = choice === 'focus' ? 'focus' :
        (ordinary.find(function (g) { return object(config[g.key]) && own(config[g.key], sym); }) || {}).key;
      // 用户明确：三只宽基/红利ETF没有旧分类时，默认回指数基。
      if (!key && ['SPYM', 'QQQM', 'SCHD'].indexOf(sym) >= 0) key = 'index_funds';
      if (!key) key = (ordinary.find(function (g) { return g.sector_etf === sym; }) || {}).key;
      var group = groups.find(function (g) { return g.key === key; });
      if (!group || !object(config[key])) throw new Error('该股没有有效默认分组，请选择重点');
      return { key: key, label: group.label || key };
    }
    function clearConfig(config, destination, sym) {
      var next = clone(config), source = next.positions[sym];
      if (!object(source) && !object(next[destination.key][sym]))
        throw new Error('该股持仓配置已变化，请核对后重试');
      var rec = clone(next[destination.key][sym] || source);
      delete rec.cost; delete rec.reduce; delete rec.hold;
      if (destination.key !== 'focus') { delete rec.target; delete rec.target_at; }
      next[destination.key][sym] = rec;
      delete next.positions[sym];
      // 选择回默认分组就是退出重点；其它板块记录原样保留，不覆盖其备注/触发价。
      if (destination.key !== 'focus') delete next.focus[sym];
      return next;
    }
    async function clearPut(file, branch, meta, value) {
      clearActive();
      var r = await requestJson(CONFIG_API + file, { method: 'PUT', headers: ghHead(true),
        body: JSON.stringify({ branch: branch, sha: meta.sha,
          message: '持仓统一保存 ' + new Date().toISOString(),
          content: b64(JSON.stringify(value, null, 2)) }) });
      clearActive();
      if (r.status !== 200 && r.status !== 201) {
        if ([400, 401, 403, 404, 409, 422].indexOf(r.status) >= 0) {
          clearRecord[file === GH.path ? 'quantityRejected' : 'moveRejected'] = true;
          saveClearRecord(clearRecord);
        }
        throw new Error(file + ' 写入未成功：HTTP ' + r.status + '；重试会先读取远端，不盲目覆盖');
      }
      return r.data || {};
    }
    function clearTrack(receipt) {
      if (receipt && /^[a-f0-9]{40,64}$/i.test(receipt.blobSHA || '') &&
          /^[a-f0-9]{40,64}$/i.test(receipt.commitSHA || '') && window.MMRunStatus && window.MMRunStatus.watchConfig)
        Promise.resolve(window.MMRunStatus.watchConfig(receipt)).catch(function () {
          if (!disposed) clearMessage('清仓已写入，规则更新跟踪未完成；请点状态核验');
        });
    }
    function clearField(d, key) {
      var p = key.split('|'), f = p[1];
      if (f === 'cash') return cashFirst(d);
      if (f === 'cash_extra' || f === 'fixed' || f === 'fixed_cost') return d[f];
      return d.items[p[0]] && own(d.items[p[0]], f) ? d.items[p[0]][f] : null;
    }
    function clearMerge(remote) {
      var rec = clearRecord, next = clone(remote), first = cashFirst(remote), second = remote.cash_extra;
      rec.targets.concat(rec.additions || []).forEach(function (t) {
        if (JSON.stringify(remote.items[t.sym] || null) !== JSON.stringify(rec.baseline.items[t.sym] || null))
          clearConflict(t.sym + ' 仓位已变化，停止旧清仓事务');
      });
      Object.keys(rec.fields).forEach(function (key) {
        if (clearField(remote, key) !== clearField(rec.baseline, key))
          clearConflict(key + ' 同字段已被其它设备修改，停止旧清仓事务');
        var p = key.split('|'), f = p[1], value = rec.fields[key];
        if (f === 'cash') first = value;
        else if (f === 'cash_extra') second = value;
        else if (f === 'fixed' || f === 'fixed_cost') next[f] = value;
        else {
          if (!next.items[p[0]]) next.items[p[0]] = { qty: 0 };
          if (value === null) delete next.items[p[0]][f]; else next.items[p[0]][f] = value;
        }
      });
      next.cash = (Math.round(first * 10000) + Math.round(second * 10000)) / 10000;
      next.cash_extra = second;
      rec.targets.forEach(function (t) { if (!next.items[t.sym]) next.items[t.sym] = { qty: 0 }; next.items[t.sym].qty = 0; });
      next.updated_at = new Date().toISOString();
      return normalize(next);
    }
    function clearRule(config, groups, t) {
      var dest = clearDestination(config, groups, t.sym, t.choice);
      return { dest: dest, signature: JSON.stringify({ source: config.positions[t.sym] || null,
        destination: config[dest.key][t.sym] || null, focus: config.focus[t.sym] || null,
        group: groups.find(function (g) { return g.key === dest.key; }) }) };
    }
    function additionRule(config, groups, t) {
      if (!object(config.positions) || !Array.isArray(groups)) throw new Error('持仓分组配置无效');
      if (own(config.positions, t.sym)) clearConflict(t.sym + ' 远端已有持仓，禁止重复新增');
      if (t.sym.charAt(0) === '^' || ['SGOV', 'USD', 'BOND'].indexOf(t.sym) >= 0)
        throw new Error(t.sym + ' 是指数、固收或现金，请使用既有专属口径');
      var records = {}, source = null;
      groups.forEach(function (g) {
        if (object(config[g.key]) && object(config[g.key][t.sym])) {
          records[g.key] = config[g.key][t.sym];
          if (!source) source = config[g.key][t.sym];
        }
      });
      var record = clone(source || {}); record.hold = t.hold;
      if (t.extra) {
        // 填了才覆盖；没填保留已有分组里复制来的值
        Object.keys(t.extra).forEach(function (k) { record[k] = t.extra[k]; });
        if (own(t.extra, 'target')) { if (t.extra.target > 0) record.target_at = monthBJ(); else delete record.target_at; }
      }
      return { record: record, signature: JSON.stringify(records) };
    }
    function clearAccept(remote) {
      (clearRecord.additions || []).forEach(function (t) { addedPositions[t.sym] = { hold: t.hold }; });
      DATA = clone(remote); CLOUD = clone(remote); cloudVerified = true;
      dirty = false; draftPending = false; editDraft = {}; cloudFirstPending = false;
      editBaseline = clone(remote);
      if (editing) { snapshot = clone(remote); snapshotDraft = {}; prevDirty = false; prevDraftPending = false; }
      if (!write(LS_CLOUD, CLOUD) || !persist()) throw new Error('数值已保存，本地缓存未持久化；暂停移组');
      SYNC = 'synced';
    }
    function completeClear(receipt) {
      saveClearRecord(Object.assign({}, clearRecord, { step: 'done', receipt: receipt }));
      clearDraft = {}; addDraft = {}; editDraft = {}; editBaseline = null; draftPending = false;
      clearRecord.targets.forEach(function (t) { delete addedPositions[t.sym]; });
      persist(); clearTrack(receipt);
      clearMessage('本次数值已保存，持仓分组已统一更新，等待规则更新；现金仅按明确编辑保存');
      clearRecord.targets.forEach(function (t) {
        document.dispatchEvent(new CustomEvent('cost:cleared', { detail: { symbol: t.sym, group: t.destination } }));
      });
      editing = false; snapshot = null; snapshotDraft = null; syncEditUI();
    }
    function markClear(sym, choice) {
      if (!editing || disposed || !writer || clearBusy || !isPosition(rowBySym(sym))) return;
      if (clearRecord && clearRecord.step !== 'done') { clearMessage('有已保存事务未完成，请点击面板保存重试核验；不能重选或撤销已提交目标'); return; }
      if (choice === 'undo') delete clearDraft[sym]; else clearDraft[sym] = choice;
      draftPending = true;
      changed();
      if (clearDialog) clearDialog.close();
      clearMessage('');
    }
    function freezeClear(next) {
      var baseline = clone(editBaseline || CLOUD || snapshot || DATA), fields = {};
      Object.keys(editDraft).forEach(function (key) { fields[key] = clearField(next, key); });
      // 含此前尚未上传的有效草稿，但不把未触及字段当成整份覆盖授权。
      ['USD|cash', 'USD|cash_extra', 'BOND|fixed', 'BOND|fixed_cost'].concat(
        Object.keys(next.items).flatMap(function (sym) { return [sym + '|qty', sym + '|cost']; })
      ).forEach(function (key) { if (clearField(next, key) !== clearField(baseline, key)) fields[key] = clearField(next, key); });
      var targets = Object.keys(clearDraft).map(function (sym) { return { sym: sym, choice: clearDraft[sym] }; });
      var additions = Object.keys(addDraft).map(function (sym) {
        var a = { sym: sym, hold: addDraft[sym].hold };
        if (addDraft[sym].extra) a.extra = clone(addDraft[sym].extra);
        return a;
      });
      saveClearRecord({ version: 2, targets: targets, additions: additions, step: 'quantity', baseline: baseline, fields: fields });
    }
    async function executeClear(sym, choice) {
      if (clearBusy) return;
      try {
        // 内部兼容入口同样代表面板明确保存；确认框不调用此接口。
        if (sym && (!clearRecord || clearRecord.step === 'done')) markClear(sym, choice);
        clearGuard();
        var nextDraft = checkedDraft();
        if (!nextDraft) throw new Error(draftNotice());
        if (!clearRecord || clearRecord.step === 'done') {
          if (!Object.keys(clearDraft).length && !Object.keys(addDraft).length) return;
          freezeClear(nextDraft);
        }
        // 冻结事务由面板保存继续；原草稿不创建第二个上传授权。
        clearBusy = true;
        pullGeneration++; clearSaveTimers(); clearDialogControls();
        clearMessage('正在核对本次数值和清仓分组…');
        if (clearRecord.evidenceCommit) {
          var evidence = await clearRead('holdings.json', clearRecord.evidenceCommit);
          if (JSON.stringify(evidence.value) !== JSON.stringify(clearRecord.movePayload))
            throw new Error('固定提交内容与移组不一致，停止取证；请核对');
          completeClear({ file: 'holdings.json', blobSHA: evidence.sha, commitSHA: clearRecord.evidenceCommit });
          return;
        }
        var groups = (await clearRead('groups.json', 'main')).value;
        var config = await clearRead('holdings.json', 'main');
        var pos = await clearRead(GH.path, GH.branch), remote = normalize(pos.value);
        var alreadyMoved = clearRecord.moveSubmitted && JSON.stringify(config.value) === JSON.stringify(clearRecord.movePayload);
        clearRecord.targets.forEach(function (t) {
          if (alreadyMoved) return;
          var rule = clearRule(config.value, groups, t);
          clearConfig(config.value, rule.dest, t.sym);
          if (t.rule && t.rule !== rule.signature) clearConflict(t.sym + ' 同股分组规则已被修改，停止旧清仓事务');
          if (t.source && JSON.stringify(config.value.positions[t.sym] || null) !== t.source)
            clearConflict(t.sym + ' 持仓规则已被修改，停止旧清仓事务');
          t.rule = rule.signature; t.destination = rule.dest.key; t.label = rule.dest.label;
        });
        (clearRecord.additions || []).forEach(function (t) {
          if (alreadyMoved) return;
          var rule = additionRule(config.value, groups, t);
          if (t.rule && t.rule !== rule.signature) clearConflict(t.sym + ' 同股新增规则已被修改，停止旧事务');
          t.rule = rule.signature; t.record = rule.record;
        });
        saveClearRecord(clearRecord);
        if (clearRecord.step === 'quantity') {
          if (clearRecord.legacy && !clearRecord.baseline && !clearRecord.submitted)
            clearConflict('旧单股清仓缺少编辑基线，禁止恢复上传；请核对备份');
          if (clearRecord.submitted && (!clearRecord.payload ||
              (canonical(remote) !== canonical(clearRecord.payload) && pos.sha !== clearRecord.baseSHA)))
            clearConflict('上次写入后远端已有新版本，停止旧清仓事务；不会清掉重新建仓的数量');
          var payload = clearRecord.submitted ? clearRecord.payload : clearMerge(remote);
          if (canonical(remote) !== canonical(payload)) {
            var last = read(LS_PUSH_AT), wait = typeof last === 'number' ? Math.max(0, 15000 - (Date.now() - last)) : 0;
            if (storageErrors[LS_PUSH_AT]) throw new Error('保存间隔读取失败，暂停提交');
            if (wait) { clearMessage('等待仓位保存间隔…'); await new Promise(function (resolve) { setTimeout(resolve, wait); }); }
            clearActive();
            pos = await clearRead(GH.path, GH.branch); remote = normalize(pos.value);
            if (clearRecord.submitted) {
              if (canonical(remote) !== canonical(payload) && pos.sha !== clearRecord.baseSHA)
                clearConflict('等待期间远端版本变化，停止旧清仓事务');
            } else payload = clearMerge(remote);
            if (canonical(remote) !== canonical(payload)) {
              saveClearRecord(Object.assign({}, clearRecord, { submitted: true, quantityRejected: false, baseSHA: pos.sha, payload: payload }));
              if (!write(LS_PUSH_AT, Date.now())) throw new Error('保存间隔未持久化，暂停提交');
              clearMessage('正在统一保存数值和清仓数量…');
              await clearPut(GH.path, GH.branch, pos, payload);
              // 已收到数值成功回执，后续GET/移组失败也不能由取消恢复旧数值。
              clearRecord.quantityAccepted = true;
              clearAccept(payload);
              saveClearRecord(clearRecord);
              remote = normalize((await clearRead(GH.path, GH.branch)).value);
              if (canonical(remote) !== canonical(payload)) throw new Error('数值写入结果尚未完整核验，未继续移组');
            }
          }
          saveClearRecord(Object.assign({}, clearRecord, { step: 'move', payload: payload }));
        }
        clearRecord.targets.forEach(function (t) {
          if ((remote.items[t.sym] && remote.items[t.sym].qty > 0) ||
              (clearRecord.payload && JSON.stringify(remote.items[t.sym] || null) !== JSON.stringify(clearRecord.payload.items[t.sym] || null)))
            clearConflict(t.sym + ' 已重新建仓或仓位变化，停止移组');
        });
        (clearRecord.additions || []).forEach(function (t) {
          if (JSON.stringify(remote.items[t.sym] || null) !== JSON.stringify(clearRecord.payload.items[t.sym] || null))
            clearConflict(t.sym + ' 新增仓位被远端修改，停止规则提交');
          addedPositions[t.sym] = { hold: t.hold };
        });
        clearAccept(remote);
        clearMessage('数值已保存，正在批量更新持仓分组…');
        config = await clearRead('holdings.json', 'main');
        var latest = normalize((await clearRead(GH.path, GH.branch)).value);
        clearRecord.targets.forEach(function (t) {
          if (JSON.stringify(latest.items[t.sym] || null) !== JSON.stringify(remote.items[t.sym] || null))
            clearConflict(t.sym + ' 移组前已重新建仓或仓位变化，停止移组');
        });
        (clearRecord.additions || []).forEach(function (t) {
          if (JSON.stringify(latest.items[t.sym] || null) !== JSON.stringify(remote.items[t.sym] || null))
            clearConflict(t.sym + ' 规则提交前仓位被修改，停止旧事务');
        });
        var movePayload;
        if (clearRecord.moveSubmitted) {
          movePayload = clearRecord.movePayload;
          if (JSON.stringify(config.value) !== JSON.stringify(movePayload) && config.sha !== clearRecord.moveBaseSHA)
            clearConflict('未知移组PUT后远端规则已有新版本，停止旧清仓事务');
        } else {
          movePayload = clone(config.value);
          clearRecord.targets.forEach(function (t) {
            var rule = clearRule(config.value, groups, t);
            if (t.rule && rule.signature !== t.rule) clearConflict(t.sym + ' 同股分组规则已被修改，停止移组');
            if (t.source && JSON.stringify(config.value.positions[t.sym] || null) !== t.source)
              clearConflict(t.sym + ' 持仓规则已被修改，停止移组');
            movePayload = clearConfig(movePayload, rule.dest, t.sym);
          });
          (clearRecord.additions || []).forEach(function (t) {
            var rule = additionRule(config.value, groups, t);
            if (rule.signature !== t.rule) clearConflict(t.sym + ' 同股新增规则已被修改，停止规则提交');
            movePayload.positions[t.sym] = clone(rule.record);
          });
        }
        if (JSON.stringify(config.value) !== JSON.stringify(movePayload)) {
          saveClearRecord(Object.assign({}, clearRecord, { moveSubmitted: true, moveRejected: false, moveBaseSHA: config.sha, movePayload: movePayload }));
          var response = await clearPut('holdings.json', 'main', config, movePayload);
          if (response.content && response.commit && /^[a-f0-9]{40,64}$/i.test(response.content.sha || '') &&
              /^[a-f0-9]{40,64}$/i.test(response.commit.sha || ''))
            saveClearRecord(Object.assign({}, clearRecord, { receipt: {
              file: 'holdings.json', blobSHA: response.content.sha, commitSHA: response.commit.sha } }));
          var verified = await clearRead('holdings.json', 'main');
          if (JSON.stringify(verified.value) !== JSON.stringify(movePayload))
            throw new Error('分组写入结果尚未核验；数值不回滚，请点击面板保存重试核验');
        }
        var receipt = clearRecord.receipt;
        if (!receipt) {
          // 回执缺失只读固定提交取证；一旦固定，重试不能换成更新的 main 提交。
          var commit = clearRecord.evidenceCommit;
          if (!commit) {
            var head = await requestJson('https://api.github.com/repos/' + GH.owner + '/' + GH.repo + '/git/ref/heads/main',
              { headers: ghHead(), cache: 'no-store' });
            clearActive();
            commit = head.ok && head.data && head.data.object && head.data.object.sha;
            if (!/^[a-f0-9]{40,64}$/i.test(commit || '')) throw new Error('移组内容已核验，但提交证据未取得；请点击面板保存重试核验');
            saveClearRecord(Object.assign({}, clearRecord, { evidenceCommit: commit }));
          }
          var committed = await clearRead('holdings.json', commit);
          if (JSON.stringify(committed.value) !== JSON.stringify(movePayload)) throw new Error('固定提交内容与移组不一致，停止取证；请核对');
          receipt = { file: 'holdings.json', blobSHA: committed.sha, commitSHA: commit };
        }
        completeClear(receipt);
      } catch (e) {
        if (!disposed && clearRecord && clearRecord.step === 'quantity' && !clearRecord.submitted &&
            !clearRecord.moveSubmitted && !clearRecord.quantityAccepted) {
          // 预检未写入远端，释放冻结但保留草稿，允许改选目的地或修正后重存。
          clearRecord.targets.forEach(function (t) { clearDraft[t.sym] = t.choice; });
          (clearRecord.additions || []).forEach(function (t) {
            if (!addDraft[t.sym]) {
              addDraft[t.sym] = { hold: t.hold, before: (clearRecord.baseline && clearRecord.baseline.items[t.sym]) || null };
              if (t.extra) addDraft[t.sym].extra = clone(t.extra);
            }
          });
          if (write(LS_CLEAR, null)) clearRecord = null;
          draftPending = true; persist();
        }
        if (!disposed) clearMessage((clearRecord && (clearRecord.step === 'move' || clearRecord.quantityAccepted) ? '数值已保存，移组未完成：' : '统一保存未完成：') + e.message);
      } finally {
        clearBusy = false;
        if (!disposed) { clearDialogControls(); paint(); }
      }
    }
    async function abandonClear() {
      if (clearBusy || !clearRecord || !clearRecord.conflict || disposed || !writer) return;
      if (!window.confirm('结束旧保存事务？\n将先只读核验远端仓位和分组，采用最新远端数值作为新基线，不回滚任何已保存数值、不继续PUT。\n结果仍未知或读取失败时保留事务保护；完成后需重新编辑并确认清仓/新增。')) return;
      try {
        if (isolated || !ghToken()) throw new Error('请在真实可写页面填入token后核验，尚未结束旧事务');
        if (Object.keys(storageErrors).length || inFlight || uncertain || positionsAuthorized)
          throw new Error('其它保存或持久化异常尚未解决，保留旧事务保护');
        clearBusy = true; pullGeneration++; clearSaveTimers(); clearDialogControls();
        clearMessage('正在只读核验远端，尚未结束旧事务…');
        var pos = await clearRead(GH.path, GH.branch), remote = normalize(pos.value);
        var config = await clearRead('holdings.json', 'main');
        var quantityMatch = clearRecord.payload && canonical(remote) === canonical(clearRecord.payload);
        var moveMatch = clearRecord.movePayload && JSON.stringify(config.value) === JSON.stringify(clearRecord.movePayload);
        if (clearRecord.submitted && !quantityMatch && pos.sha === clearRecord.baseSHA &&
            !clearRecord.quantityAccepted && !clearRecord.quantityRejected)
          throw new Error('数量PUT仍未知且远端仍是原版本，不能安全放弃；保留事务，可再次点面板取消只读核验');
        if (clearRecord.moveSubmitted && !moveMatch && config.sha === clearRecord.moveBaseSHA &&
            !clearRecord.moveRejected && !clearRecord.receipt && !clearRecord.evidenceCommit)
          throw new Error('分组PUT仍未知且远端仍是原版本，不能安全放弃；保留事务，可再次点面板取消只读核验');
        // 仅采用已读取的远端值，绝不恢复进入编辑时的旧本地快照。
        clearAccept(remote);
        (clearRecord.additions || []).forEach(function (t) {
          if (!(remote.items[t.sym] && remote.items[t.sym].qty > 0) && !(config.value.positions && own(config.value.positions, t.sym)))
            delete addedPositions[t.sym];
        });
        saveClearRecord(Object.assign({}, clearRecord, { step: 'done', abandoned: true, conflict: false,
          quantityResult: quantityMatch ? 'verified' : 'remote-version', moveResult: moveMatch ? 'verified' : 'remote-version' }));
        clearDraft = {}; addDraft = {}; editDraft = {}; editBaseline = null; draftPending = false;
        if (!persist()) throw new Error('核验值尚未持久化，请保留备份并重试');
        NOTICE = ''; SYNC_MSG = ''; editing = false; snapshot = null; snapshotDraft = null; savePaused = false;
        syncEditUI();
      } catch (e) {
        if (!disposed) clearMessage('旧事务尚未安全结束：' + e.message);
      } finally {
        clearBusy = false;
        if (!disposed) { clearDialogControls(); paint(); }
      }
    }
    function clearDialogControls() {
      var pending = !!(clearRecord && clearRecord.step !== 'done');
      if (clearDialog) {
        all('input', clearDialog).forEach(function (input) { input.disabled = clearBusy || pending; });
        clearDialog.querySelector('[data-clear-confirm]').disabled = clearBusy || pending;
        clearDialog.querySelector('[data-clear-cancel]').disabled = clearBusy;
        clearDialog.querySelector('[data-clear-undo]').disabled = clearBusy || pending || !own(clearDraft, clearChoice);
      }
      all('#costSave,#costCancel', panel).forEach(function (button) { button.disabled = clearBusy; });
      var add = panel.querySelector('[data-cost-add]');
      if (add) add.disabled = clearBusy || pending;
      all('input[data-f],[data-trade-action]', panel).forEach(function (input) { input.disabled = clearBusy || pending; });
      if (tradeDialog) all('input,[data-trade-confirm]', tradeDialog).forEach(function (input) { input.disabled = clearBusy || pending || !writer || !editing; });
      if (editBtn) editBtn.disabled = clearBusy;
    }
    function openClear(sym) {
      if (!editing || !isPosition(rowBySym(sym)) || clearBusy) return;
      reloadClearRecord();
      if (!clearDialog) {
        clearDialog = document.createElement('dialog');
        clearDialog.className = 'cost-clear-dialog';
        clearDialog.innerHTML = '<h3></h3><div class="cost-clear-choice"><label><input type="radio" name="clearDestination" value="default" checked>回默认分组</label>' +
          '<label><input type="radio" name="clearDestination" value="focus">进重点</label></div>' +
          '<p data-clear-message role="status"></p><div class="cost-acts"><button type="button" data-clear-cancel>取消</button>' +
          '<button type="button" data-clear-undo>撤销标记</button><button type="button" class="primary" data-clear-confirm>确认清仓</button></div>';
        panel.appendChild(clearDialog);
        clearDialog.addEventListener('cancel', function (e) { if (clearBusy) e.preventDefault(); });
        clearDialog.addEventListener('click', function (e) {
          if (e.target.closest('[data-clear-cancel]')) { if (!clearBusy) clearDialog.close(); }
          else if (e.target.closest('[data-clear-undo]')) markClear(clearChoice, 'undo');
          else if (e.target.closest('[data-clear-confirm]')) {
            var selected = clearDialog.querySelector('input:checked');
            if (selected) markClear(clearChoice, selected.value);
          }
        });
      }
      clearChoice = sym;
      clearDialog.querySelector('h3').textContent = '清仓 ' + sym + '？';
      all('input', clearDialog).forEach(function (input) {
        input.checked = input.value === (clearDraft[sym] || 'default');
      });
      clearDialog.querySelector('[data-clear-message]').textContent = clearRecord && clearRecord.step !== 'done' ?
        '上次保存未完成；请点击面板保存核验，不自动重传' : '';
      clearDialogControls(); clearDialog.showModal();
    }
    var tradeDialog = null, tradeActive = null;
    function tradeGuard(sym) {
      if (!editing || disposed || !writer || clearBusy || (clearRecord && clearRecord.step !== 'done'))
        throw new Error('编辑会话不可用，请先完成保存核验或重新接管');
      if (!isPosition(rowBySym(sym)) && !own(addDraft, sym) && !own(addedPositions, sym))
        throw new Error('当前标的已不在持仓中，请重新核对');
    }
    function applyTrade(sym, side, quantity, price, destination) {
      tradeGuard(sym);
      if (['buy', 'sell'].indexOf(side) < 0) throw new Error('无效交易方向');
      var qraw = String(quantity).trim(), praw = String(price).trim();
      if (draftError(qraw) || !(Number(qraw) > 0)) throw new Error('成交数量必须大于0，最多四位小数');
      if (draftError(praw)) throw new Error('成交价必须是非负数，最多四位小数');
      if (side === 'sell' && praw === '') throw new Error('卖出请输入成交价');
      var next = checkedDraft();
      if (!next) throw new Error('存在无效输入，请修正后交易');
      var item = next.items[sym] || { qty: 0 }, oldQty = item.qty, known = own(item, 'cost'), oldCost = item.cost,
        qty = Number(qraw), px = praw === '' ? null : Number(praw), quantities = scaled([oldQty, qty]);
      if (side === 'sell' && quantities[1] > quantities[0]) throw new Error('卖出数量不能超过当前持仓');
      if (oldQty > 0 && !known && px !== null && (side === 'buy' || quantities[0] !== quantities[1]))
        throw new Error('现有成本为空，请先手填成本，或买入留空成交价只调整股数');
      var sumQty = side === 'buy' ? oldQty + qty : oldQty - qty, nextCost = null;
      if (!isFinite(sumQty * 10000)) throw new Error('交易后数量过大');
      var nextQty = Number(trim4(sumQty));
      if (nextQty === 0) nextCost = 0;
      else if (known) nextCost = (oldQty * oldCost + (side === 'buy' ? (px === null ? 0 : qty * px) : -qty * px)) / nextQty;
      else if (side === 'buy' && oldQty === 0 && px !== null) nextCost = px;
      if (nextCost !== null && !isFinite(nextCost * 10000)) throw new Error('交易后成本过大');
      var qtyText = trim4(nextQty), costText = nextCost === null ? '' : trim4(nextCost);
      if (draftError(qtyText) || draftError(costText, 'cost')) throw new Error('交易结果超出四位小数输入范围');
      if (side === 'sell' && nextQty === 0) {
        if (own(addDraft, sym)) { removeAdded(sym); return; }
        if (['default', 'focus'].indexOf(destination) < 0) throw new Error('请选择全卖后的清仓分组');
        clearDraft[sym] = destination;
      } else if (nextQty > 0) delete clearDraft[sym];
      editDraft[sym + '|qty'] = { raw: qtyText, error: '' };
      editDraft[sym + '|cost'] = { raw: costText, error: '' };
      // 自动回算保存原始有限成本；四位仅用于展示，手填 input 重建条目以覆盖该原值。
      if (nextCost !== null) editDraft[sym + '|cost'].exact = nextCost;
      DATA = checkedDraft(); draftPending = true; NOTICE = ''; changed(); paint();
    }
    function openTrade(sym, side) {
      try { tradeGuard(sym); } catch (err) { clearMessage(err.message); return; }
      if (!tradeDialog) {
        tradeDialog = document.createElement('dialog'); tradeDialog.className = 'cost-clear-dialog cost-trade-dialog';
        tradeDialog.innerHTML = '<h3></h3><label>成交数量<input data-trade-qty inputmode="decimal" step="0.0001" autocomplete="off"></label>' +
          '<label>成交价<input data-trade-price inputmode="decimal" step="0.0001" autocomplete="off"></label><p data-trade-note></p>' +
          '<div class="cost-clear-choice" data-trade-destination><label><input type="radio" name="tradeDestination" value="default">全卖回默认</label>' +
          '<label><input type="radio" name="tradeDestination" value="focus">全卖进重点</label></div><p data-trade-error role="status"></p>' +
          '<div class="cost-acts"><button type="button" data-trade-cancel>取消</button><button type="button" class="primary" data-trade-confirm>加入草稿</button></div>';
        panel.appendChild(tradeDialog);
        tradeDialog.addEventListener('cancel', function () { tradeActive = null; });
        tradeDialog.addEventListener('click', function (e) {
          if (e.target.closest('[data-trade-cancel]')) { tradeDialog.close(); tradeActive = null; }
          else if (e.target.closest('[data-trade-confirm]')) {
            try {
              if (!tradeActive || tradeActive.generation !== editGeneration) throw new Error('编辑会话已变化，请重新打开交易');
              var choice = tradeDialog.querySelector('input[name="tradeDestination"]:checked');
              applyTrade(tradeActive.sym, tradeActive.side, tradeDialog.querySelector('[data-trade-qty]').value,
                tradeDialog.querySelector('[data-trade-price]').value, choice ? choice.value : 'default');
              tradeDialog.close(); tradeActive = null;
            } catch (err) { tradeDialog.querySelector('[data-trade-error]').textContent = err.message; }
          }
        });
      }
      tradeActive = { sym: sym, side: side, generation: editGeneration };
      tradeDialog.querySelector('h3').textContent = (side === 'buy' ? '买入 ' : '卖出 ') + sym;
      tradeDialog.querySelector('[data-trade-qty]').value = ''; tradeDialog.querySelector('[data-trade-price]').value = '';
      tradeDialog.querySelector('[data-trade-error]').textContent = '';
      tradeDialog.querySelector('[data-trade-note]').textContent = side === 'buy' ? '买入价可空用于拆股；不改现金，忽略费用。' : '卖出价必填；全卖在面板保存后归零移组，不改现金，忽略费用。';
      tradeDialog.querySelector('[data-trade-destination]').hidden = side !== 'sell';
      all('input[name="tradeDestination"]', tradeDialog).forEach(function (input) { input.checked = input.value === (clearDraft[sym] || 'default'); });
      clearDialogControls(); tradeDialog.showModal(); tradeDialog.querySelector('[data-trade-qty]').focus();
    }
    var addDialog = null;
    function addPosition(sym, hold, qty, cost, note, trigger, reduce, target) {
      if (!editing || !writer || disposed || clearBusy) return;
      if (clearRecord && clearRecord.step !== 'done') throw new Error('请先完成面板保存事务核验，再新增仓位');
      sym = String(sym || '').trim().toUpperCase();
      if (!/^[A-Z0-9.^=_-]{1,32}$/.test(sym)) throw new Error('请输入有效代码：仅字母、数字、点、横线等，最多32字符');
      if (sym.charAt(0) === '^' || ['SGOV', 'USD', 'BOND'].indexOf(sym) >= 0)
        throw new Error('指数代码、固收和现金不通过仓位新增；普通ETF可新增');
      if (isPosition(rowBySym(sym)) || own(addDraft, sym) ||
          (own(addedPositions, sym) && DATA.items[sym] && DATA.items[sym].qty > 0) ||
          (DATA.items[sym] && DATA.items[sym].qty > 0)) throw new Error('该代码已有持仓，不能重复新增');
      if (['long', 'short'].indexOf(hold) < 0) throw new Error('请选择长持仓或短持仓');
      qty = String(qty || '').trim(); cost = String(cost || '').trim();
      if (draftError(qty) || draftError(cost, 'cost') || !(Number(qty) > 0)) throw new Error('数量必须大于0；数量与成本最多四位小数，成本可留空');
      if (!checkedDraft()) { persist(); paint(); throw new Error('已有无效输入，请先修正后新增仓位'); }
      var extra = parseExtra(note, trigger, reduce, target);
      addDraft[sym] = { hold: hold, before: DATA.items[sym] ? clone(DATA.items[sym]) : null };
      if (extra) addDraft[sym].extra = extra;
      editDraft[sym + '|qty'] = { raw: qty, error: '' };
      editDraft[sym + '|cost'] = { raw: cost, error: '' };
      DATA = checkedDraft(); draftPending = true; changed();
      if (addDialog) addDialog.close();
      clearMessage('');
    }
    function removeAdded(sym) {
      if (!own(addDraft, sym) || clearBusy || (clearRecord && clearRecord.step !== 'done')) return;
      var before = addDraft[sym].before;
      if (before) DATA.items[sym] = clone(before); else delete DATA.items[sym];
      delete addDraft[sym]; delete editDraft[sym + '|qty']; delete editDraft[sym + '|cost'];
      draftPending = true; changed(); paint();
    }
    function openAdd() {
      if (!editing || !writer || disposed || clearBusy) return;
      if (!addDialog) {
        addDialog = document.createElement('dialog'); addDialog.className = 'cost-clear-dialog cost-add-dialog';
        addDialog.innerHTML = '<h3>新增仓位</h3><label>代码 <input data-add-symbol maxlength="32" aria-label="新仓位代码"></label>' +
          '<div class="cost-add-hold"><label><input type="radio" name="addHold" data-add-hold value="long" checked>长持仓</label>' +
          '<label><input type="radio" name="addHold" data-add-hold value="short">短持仓</label></div>' +
          '<label>数量 <input data-add-qty inputmode="decimal" aria-label="新仓位数量"></label>' +
          '<label>成本 <input data-add-cost inputmode="decimal" aria-label="新仓位成本"></label>' +
          '<label>备注 <input data-add-note maxlength="7" placeholder="中文名，选填" aria-label="新仓位备注名"></label>' +
          '<label>加仓价 <input data-add-trigger inputmode="decimal" placeholder="选填" aria-label="新仓位加仓价"></label>' +
          '<label>减仓价 <input data-add-reduce inputmode="decimal" placeholder="选填" aria-label="新仓位减仓价"></label>' +
          '<label>目标价 <input data-add-target inputmode="decimal" placeholder="选填，0＝关闭达到报灯" aria-label="新仓位目标价"></label>' +
          '<p data-add-message role="status"></p>' +
          '<div class="cost-acts"><button type="button" data-add-cancel>取消</button><button type="button" class="primary" data-add-confirm>加入草稿</button></div>';
        panel.appendChild(addDialog);
        addDialog.addEventListener('click', function (e) {
          if (e.target.closest('[data-add-cancel]')) addDialog.close();
          else if (e.target.closest('[data-add-confirm]')) {
            try { addPosition(addDialog.querySelector('[data-add-symbol]').value,
              addDialog.querySelector('[data-add-hold]:checked').value,
              addDialog.querySelector('[data-add-qty]').value, addDialog.querySelector('[data-add-cost]').value,
              addDialog.querySelector('[data-add-note]').value, addDialog.querySelector('[data-add-trigger]').value,
              addDialog.querySelector('[data-add-reduce]').value, addDialog.querySelector('[data-add-target]').value); }
            catch (err) { addDialog.querySelector('[data-add-message]').textContent = err.message; }
          }
        });
      }
      addDialog.querySelector('[data-add-message]').textContent = '';
      ['note', 'trigger', 'reduce', 'target'].forEach(function (k) { addDialog.querySelector('[data-add-' + k + ']').value = ''; });
      all('[data-add-hold]', addDialog).forEach(function (input) { input.checked = input.value === 'long'; });
      addDialog.showModal(); addDialog.querySelector('[data-add-symbol]').focus();
    }
    var sortKey = read(LS_SORT) === 'wt' ? 'wt' : 'pnl';
    function syncMirrorEl() {
      if (syncMirror) return syncMirror;
      var sub = document.querySelector('.sub');
      if (!sub) return null;
      syncMirror = document.createElement('span');
      syncMirror.className = 'cost-state';
      syncMirror.id = 'costSyncState';
      syncMirror.setAttribute('aria-live', 'polite');
      syncMirror.setAttribute('role', 'button');
      syncMirror.tabIndex = 0;
      syncMirror.addEventListener('click', function (e) {
        e.preventDefault();
        e.stopPropagation();
        if (stateEl) stateEl.click();
      });
      syncMirror.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          syncMirror.click();
        }
      });
      sub.appendChild(syncMirror);
      return syncMirror;
    }
    function showNotice(t) {
      if (stateEl) stateEl.textContent = t;
      if (syncMirror) syncMirror.textContent = '';
    }
    function syncCostNotice() {
      var text =
        Object.keys(storageErrors)
          .map(function (k) {
            return storageErrors[k];
          })
          .join('；') ||
        draftNotice() ||
        (NOTICE === READ_ONLY_NOTICE ? '' : NOTICE) ||
        (SYNC === 'error' ? SYNC_MSG : '');
      var notice = panel.querySelector('#costNotice');
      if (notice) {
        notice.textContent = text;
        notice.hidden = !text;
      }
      // 锁占用或锁异常只读仅用顶部小状态；真实校验、离线及持久化错误仍须可见。
      panel.classList.toggle('cost-edit-open', editing || !!text);
      if (text && !panel.open) panel.open = true;
      if (actsEl) actsEl.hidden = !editing && !text;
      var save = panel.querySelector('#costSave'),
        cancel = panel.querySelector('#costCancel');
      if (save) save.hidden = !editing;
      if (cancel) cancel.hidden = !editing;
      var add = panel.querySelector('[data-cost-add]');
      if (add) add.hidden = !editing;
    }
    function syncEditUI() {
      if (editBtn) {
        editBtn.textContent = editing ? '编辑中' : '编辑';
        editBtn.setAttribute('aria-pressed', String(editing));
        editBtn.className = 'cost-edit-btn' + (editing ? ' on' : '');
      }
      syncCostNotice();
      var save = panel.querySelector('#costSave');
      if (save) save.textContent = isolated ? '保存到本标签页' : '保存并上传';
    }
    function enterEdit() {
      if (editing || disposed) return;
      takeOwnership(function (ok) {
        if (!ok) {
          paint();
          return;
        }
        clearSaveTimers();
        pullGeneration++;
        editGeneration++;
        snapshot = clone(DATA);
        if (!editBaseline) editBaseline = clone(CLOUD || DATA);
        snapshotDraft = clone(editDraft);
        prevDirty = dirty;
        prevDraftPending = draftPending;
        editOrder = {};
        ROWS.filter(isPosition)
          .slice()
          .sort(function (a, b) {
            if (a.isLong !== b.isLong) return a.isLong ? -1 : 1;
            return sortKey === 'wt'
              ? b.mv - a.mv
              : (b.pnlPct === null ? -Infinity : b.pnlPct) - (a.pnlPct === null ? -Infinity : a.pnlPct);
          })
          .forEach(function (r, i) {
            editOrder[r.sym] = i;
          });
        editing = true;
        panel.open = true;
        syncEditUI();
        paint();
        var first = panel.querySelector('#' + PID + ' input');
        if (first) first.focus();
      });
    }
    function exitEdit(restore) {
      if (clearBusy) return;
      if (tradeDialog) tradeDialog.close();
      tradeActive = null;
      if (restore && clearRecord && clearRecord.step !== 'done' && clearRecord.conflict) { void abandonClear(); return; }
      clearSaveTimers();
      editGeneration++;
      pullGeneration++;
      if (restore && snapshot && canonical(DATA) !== canonical(snapshot)) {
        DATA = clone(snapshot);
        changed();
      }
      var canceledTransaction = false;
      if (restore && clearRecord && clearRecord.step === 'quantity' && !clearRecord.submitted && !clearRecord.quantityAccepted) {
        if (write(LS_CLEAR, null)) { clearRecord = null; canceledTransaction = true; }
      }
      if (restore) {
        if (!(clearRecord && clearRecord.step !== 'done') &&
            (canceledTransaction || /^(?:待清仓标记仅|已撤销清仓标记$|新增仓位仅)/.test(NOTICE))) NOTICE = '';
        if (clearRecord && (clearRecord.step === 'move' || clearRecord.quantityAccepted) && clearRecord.payload) DATA = clone(clearRecord.payload);
        Object.keys(addDraft).forEach(function (sym) { if (!addedPositions[sym] && (!editBaseline || !editBaseline.items[sym])) delete DATA.items[sym]; });
        clearDraft = {}; addDraft = {};
        editDraft = clearRecord && clearRecord.step !== 'done' ? {} : snapshotDraft || {};
        draftPending = clearRecord && clearRecord.step !== 'done' ? false : prevDraftPending;
        if (!inFlight && !uncertain) dirty = prevDirty;
        persist();
      }
      snapshotDraft = null;
      if (restore && !positionsAuthorized && !draftPending && !inFlight && sameAsCloud()) setPending(false);
      editing = false;
      snapshot = null;
      savePaused = false;
      syncEditUI();
      paint();
      if ((cloudFirstPending || canceledTransaction) && !draftPending && !positionsAuthorized) startPull();
      if (!(clearRecord && clearRecord.step !== 'done') && (dirty || uncertain)) requestSave(0, false);
      if (!(clearRecord && clearRecord.step !== 'done')) editBaseline = null;
    }
    function exportData() {
      var blob = new Blob([JSON.stringify(DATA, null, 1)], { type: 'application/json' }),
        a = document.createElement('a'),
        url = URL.createObjectURL(blob);
      a.href = url;
      a.download = 'position-costs.json';
      a.click();
      setTimeout(function () {
        URL.revokeObjectURL(url);
      }, 0);
    }
    function importData(j) {
      if (clearBusy || (clearRecord && clearRecord.step !== 'done')) throw new Error('请先完成清仓核验，再导入');
      var next = normalize(j);
      if (!writer || disposed) throw new Error('本页只读，请先点击编辑接管');
      clearSaveTimers();
      pullGeneration++;
      editGeneration++;
      DATA = next;
      editDraft = {};
      snapshotDraft = null;
      draftPending = false;
      cloudFirstPending = false;
      changed();
      authorizePositions();
      hadOwn = true;
      editing = false;
      snapshot = null;
      savePaused = false;
      syncEditUI();
      NOTICE = storageErrors[LS_LOCAL]
        ? '导入尚未持久化，请立即导出备份'
        : isolated
          ? '导入已保存到当前标签页会话；隔离预览不上传云端'
          : ghToken()
            ? '导入已保存到本机，已加入上传队列'
            : '导入已保存到本机；未填 token，尚未上传';
      requestSave(0, true);
      paint();
    }
    if (editBtn)
      editBtn.addEventListener('click', function (e) {
        e.preventDefault();
        e.stopPropagation();
        if (editing) exitEdit(true);
        else enterEdit();
      });
    panel.addEventListener('click', function (e) {
      var t =
        e.target && e.target.closest
          ? e.target.closest(
              '#costRetry,#costImportBak,#costSave,#costCancel,#costState,[data-cost-export],[data-cost-clear],[data-cost-add],[data-cost-remove-added],[data-trade-action]',
            )
          : null;
      if (!t) return;
      e.preventDefault();
      e.stopPropagation();
      if (clearBusy && !t.hasAttribute('data-cost-export')) return;
      if (t.hasAttribute('data-trade-action')) { openTrade(t.getAttribute('data-sym'), t.getAttribute('data-trade-action')); return; }
      if (t.hasAttribute('data-cost-clear')) { openClear(t.getAttribute('data-cost-clear')); return; }
      if (t.hasAttribute('data-cost-add')) { openAdd(); return; }
      if (t.hasAttribute('data-cost-remove-added')) { removeAdded(t.getAttribute('data-cost-remove-added')); return; }
      if (t.hasAttribute('data-cost-export')) {
        exportData();
        return;
      }
      if (t.id === 'costRetry') {
        _pullTry = 0;
        SYNC = 'pulling';
        paint();
        startPull();
        return;
      }
      if (t.id === 'costImportBak') {
        var f = panel.querySelector('#costFile');
        if (f) f.click();
        return;
      }
      if (t.id === 'costCancel') {
        exitEdit(true);
        return;
      }
      if (t.id === 'costSave') {
        if (!writer || disposed || !editing) return;
        reloadClearRecord();
        if ((clearRecord && clearRecord.step !== 'done') || Object.keys(clearDraft).length || Object.keys(addDraft).length) {
          void executeClear(); return;
        }
        var next = checkedDraft();
        if (!next) {
          persist();
          paint();
          showNotice(draftNotice());
          return;
        }
        DATA = next;
        var baseline = editBaseline || CLOUD || snapshot, fieldBaseline = {};
        Object.keys(editDraft).forEach(function (key) { fieldBaseline[key] = clearField(baseline, key); });
        ['USD|cash', 'USD|cash_extra', 'BOND|fixed', 'BOND|fixed_cost'].concat(
          Object.keys(next.items).flatMap(function (sym) { return [sym + '|qty', sym + '|cost']; })
        ).forEach(function (key) { if (clearField(next, key) !== clearField(baseline, key)) fieldBaseline[key] = clearField(baseline, key); });
        var fixedBaseline = own(editDraft, 'BOND|fixed_cost') ? {
          fixed: (editBaseline || CLOUD || snapshot).fixed,
          fixed_cost: (editBaseline || CLOUD || snapshot).fixed_cost,
        } : null;
        editDraft = {};
        snapshotDraft = null;
        NOTICE = '';
        editing = false;
        snapshot = null;
        savePaused = false;
        editGeneration++;
        pullGeneration++;
        draftPending = false;
        cloudFirstPending = false;
        if (!dirty && !sameAsCloud()) changed();
        authorizePositions(fixedBaseline, fieldBaseline);
        editBaseline = null;
        persist();
        syncEditUI();
        requestSave(0, true);
        paint();
        return;
      }
      if (Object.keys(storageErrors).length) {
        window.alert(
          Object.keys(storageErrors)
            .map(function (k) {
              return storageErrors[k];
            })
            .join('\n'),
        );
        return;
      }
      if (!writer) {
        window.alert(NOTICE || '本页只读，请点击编辑接管');
        return;
      }
      if (isolated) {
        window.alert(ISOLATED_NOTICE);
        return;
      }
      if (SYNC === 'error') {
        window.alert(
          '上传失败：' + (SYNC_MSG || '未知原因') + '\n请重新编辑并保存以重试；本机状态与导出备份仍可使用。',
        );
        return;
      }
      var v = window.prompt(
        'GitHub token（只存在这台设备的浏览器里，不进仓库、不上传）\n权限：Fine-grained token → 仓库选 market-monitor → Contents 读+写\n留空并确定＝取消自动同步',
        ghToken(),
      );
      if (v === null) return;
      try {
        if (v.trim()) localStorage.setItem(LS_TOKEN, v.trim());
        else localStorage.removeItem(LS_TOKEN);
        delete storageErrors[LS_TOKEN];
      } catch (err) {
        storageErrors[LS_TOKEN] = 'token 本地保存失败';
        paint();
        return;
      }
      SYNC_MSG = '';
      _pullTry = 0;
      SYNC = 'pulling';
      startPull();
      if (dirty && positionsAuthorized) requestSave(1200, false);
      paint();
    });
    panel.addEventListener('toggle', function () {
      if (!panel.open && editing) {
        if (tradeDialog) tradeDialog.close();
        tradeActive = null;
        clearSaveTimers();
        editing = false;
        snapshot = null;
        savePaused = true;
        editGeneration++;
        pullGeneration++;
        syncEditUI();
        paint();
      }
    });
    var fileInput = panel.querySelector('#costFile');
    if (fileInput)
      fileInput.addEventListener('change', function (e) {
        var file = e.target.files && e.target.files[0];
        if (!file) return;
        var fr = new FileReader(),
          generation = editGeneration,
          rev = revision;
        e.target.value = '';
        fr.onload = function () {
          if (disposed || generation !== editGeneration || rev !== revision) {
            NOTICE = '读取备份期间数据或编辑会话已变化，请重新选择文件';
            paint();
            return;
          }
          try {
            importData(JSON.parse(fr.result));
          } catch (err) {
            NOTICE = '导入失败：' + err.message + '；现有数据未修改';
            paint();
          }
        };
        fr.onerror = function () {
          NOTICE = '导入失败：无法读取文件';
          paint();
        };
        fr.readAsText(file);
      });
    panel.addEventListener('input', function (e) {
      var el = e.target;
      if (!writer || !editing || disposed || clearBusy || !el || !el.getAttribute) return;
      var f = el.getAttribute('data-f'),
        sym = el.getAttribute('data-sym');
      if (['cash', 'cash_extra', 'fixed', 'fixed_cost', 'cost', 'qty'].indexOf(f) < 0) return;
      if ((f === 'cost' || f === 'qty') && !isPosition(rowBySym(sym)) && !own(addDraft, sym) && !own(addedPositions, sym)) return;
      if (clearBusy || (clearRecord && clearRecord.step !== 'done')) return;
      var key = sym + '|' + f;
      editDraft[key] = { raw: el.value, error: draftError(el.value, f) };
      // 单一固收成本框的明确编辑同时授权固收资产与成本；未编辑时保留旧值。
      if (f === 'fixed_cost') editDraft['BOND|fixed'] = { raw: el.value, error: editDraft[key].error };
      draftPending = true;
      var next = checkedDraft();
      el.setCustomValidity(editDraft[key].error);
      if (!next) {
        changed();
        syncCostNotice();
        showNotice(draftNotice());
        return;
      }
      DATA = next;
      NOTICE = '';
      changed();
      if (!_paintFrame)
        _paintFrame = requestAnimationFrame(function () {
          _paintFrame = null;
          paint();
        });
    });

    function inp(sym, f, val, ph) {
      var e = editDraft[sym + '|' + f];
      return (
        '<input data-sym="' +
        esc(sym) +
        '" data-f="' +
        f +
        '" inputmode="decimal" step="0.0001" value="' +
        esc(e ? e.raw : val) +
        '" placeholder="' +
        ph +
        '" aria-label="' +
        esc(sym) +
        ' ' +
        ph +
        '">'
      );
    }
    function vt(v) {
      return trim4(num(v));
    }
    function editorRow(sym, cost, qty) {
      return '<div class="ep-editor-row" data-cost-row data-sym="' + esc(sym) + '">' +
        '<span class="ep-code">' + esc(sym) + '</span>' +
        ['buy', 'sell'].map(function (side) {
          return '<button type="button" class="ep-trade" data-trade-action="' + side + '" data-sym="' + esc(sym) +
            '" aria-label="' + (side === 'buy' ? '买入 ' : '卖出 ') + esc(sym) + '">' + side.toUpperCase() + '</button>';
        }).join('') + inp(sym, 'cost', cost, '成本价') + inp(sym, 'qty', qty, '数量') + '</div>';
    }
    function editorHtml(rows, cash, fixedCost) {
      var heading = '<div class="ep-editor-head">' + ['代码', '买入', '卖出', '成本价', '数量']
        .map(function (label) { return '<span class="ep-h">' + label + '</span>'; }).join('') + '</div>';
      var stocks = rows.map(function (r) {
        return editorRow(r.sym, r.costKnown ? vt(r.cost) : '', vt(r.qty));
      }).join('');
      var fields = [
        ['现金 1', 'USD', 'cash', cashFirst(DATA)],
        ['现金 2', 'USD', 'cash_extra', DATA.cash_extra],
        ['固收', 'BOND', 'fixed_cost', fixedCost],
      ]
        .map(function (f) {
          return '<label><span>' + f[0] + '</span>' + inp(f[1], f[2], f[3] ? vt(f[3]) : '', f[0]) + '</label>';
        })
        .join('');
      return (
        '<div class="ep-grid">' +
        heading +
        stocks +
        Object.keys(Object.assign({}, addedPositions, addDraft)).filter(function (sym) { return !isPosition(rowBySym(sym)); }).map(function (sym) {
          var item = DATA.items[sym] || { qty: 0 };
          return editorRow(sym, own(item, 'cost') ? vt(item.cost) : '', vt(item.qty));
        }).join('') +
        '</div><div class="ep-cash">' +
        fields +
        '<div class="ep-cash-total">现金合计 $' +
        trim4(cash) +
        '</div>' +
        '</div>'
      );
    }
    function fmt1(n) {
      return (n >= 0 ? '+' : '') + n.toFixed(1);
    }
    // 持仓标题行右侧：总资产（含现金、固收）。原相对标普强弱项已于 2026-11-12 删除。
    var totEl = document.querySelector('#group_positions>summary .group-stats .pos-total');
    if (!totEl) {
      var st = document.querySelector('#group_positions>summary .group-stats');
      if (st) {
        totEl = document.createElement('span');
        totEl.className = 'pos-total';
        st.appendChild(totEl);
      }
    }
    function renderPosTotal(totalAsset) {
      if (!totEl) return;
      var text = totalAsset > 0 ? plain(totalAsset) : '';
      if (totEl.textContent !== text) totEl.textContent = text;
      if (text) totEl.setAttribute('aria-label', '总资产 ' + text + '（含现金及固收）');
      else totEl.removeAttribute('aria-label');
    }
    function groupName(r) {
      var z = r.tr.closest('details.zone-card'),
        h = z && z.querySelector('summary .zone-name');
      if (!h) {
        z = r.tr.closest('details.group-card');
        h = z && z.querySelector('summary .grp-name');
      }
      return h ? h.textContent.replace(/\s*\(\d+\)\s*/g, '').trim() : '';
    }
    function setDirectText(el, text) {
      if (!el) return;
      var node = Array.prototype.find.call(el.childNodes, function (n) {
        return n.nodeType === 3 && n.textContent.trim();
      });
      if (node) {
        if (node.textContent !== text) node.textContent = text;
      } else el.appendChild(document.createTextNode(text));
    }
    function renderPnlPrices() {
      var groups = { gain: [], loss: [], cut: [], add: [] };
      ROWS.forEach(function (r) {
        if (!live(r)) return;
        r.lv =
          LV_ORDER.filter(function (l) {
            return r.tr.classList.contains(l);
          })[0] || 'gray';
        if (r.has && r.costKnown && r.cost > 0) {
          var tone = pnlTone(r.price, r.cost);
          if (tone)
            groups[tone].push({
              r: r,
              value: pct(r.pnlPct) + ' · ' + money(r.pnlAmt),
              sort: -Math.abs(r.pnlPct),
              cls: tone === 'gain' ? 'up' : 'down',
            });
        }
        ['add', 'cut'].forEach(function (kind) {
          var tag = kind === 'add' ? r.cap : r.cutCap,
            lv = kind === 'add' ? r.trig : r.cut;
          if (!tag) return;
          var valid = lv > 0 && (kind === 'add' || isPosition(r));
          tag.hidden = !valid;
          if (!valid) return;
          var d = Math.abs(((r.price - lv) / r.price) * 100),
            near = nearPrice(r.price, lv, 3);
          tag.setAttribute('data-near', near ? '1' : '0');
          var text = (kind === 'add' ? '加仓价 ' : '减仓价 ') + lv + ' · 距 ' + d.toFixed(1) + '%';
          setDirectText(tag.querySelector('span') || tag, text);
          if (near)
            groups[kind].push({ r: r, value: '距 ' + lv + ' · ' + d.toFixed(1) + '%', sort: d, cls: kind });
        });
      });
      var extra = document.querySelector('#signalCounts .pnl-extra');
      if (!extra) return;
      var total = 0;
      Object.keys(groups).forEach(function (kind) {
        var seen = {};
        groups[kind] = groups[kind]
          .filter(function (x) {
            if (seen[x.r.sym]) return false;
            seen[x.r.sym] = true;
            return true;
          })
          .sort(function (a, b) {
            return a.sort - b.sort;
          });
        total += groups[kind].length;
        var ul = extra.querySelector('ul[data-g="' + kind + '"]');
        if (!ul) return;
        var block = ul.closest('.signal-member-block');
        if (block) block.hidden = groups[kind].length === 0;
        var html = groups[kind]
          .map(function (x) {
            var r = x.r,
              g = groupName(r);
            return (
              '<li class="lamp-' +
              r.lv +
              '"><a class="signal-member-jump" href="#' +
              esc(r.tr.id) +
              '"><span class="signal-member-symbol">' +
              esc(r.sym) +
              '</span>' +
              (r.note ? '<span class="note">' + esc(r.note) + '</span>' : '') +
              '</a>' +
              (g ? '<span class="signal-member-belong">（' + esc(g) + '）</span>' : '') +
              ' <span class="signal-member-metric ' +
              x.cls +
              '">' +
              esc(x.value) +
              '</span></li>'
            );
          })
          .join('');
        if (ul._rendered !== html) {
          ul.innerHTML = html;
          ul._rendered = html;
        }
      });
      var summary = extra.querySelector('summary');
      var count = summary && summary.querySelector('[data-pnl-count]');
      if (count) setDirectText(count, String(total));
      else setDirectText(summary, '盈亏到价 · ' + total + ' 只');
    }
    function renderCostLine(r, totalAsset) {
      var td = r.costLine;
      if (!td) return;
      td.hidden = !r.has;
      if (!r.has) {
        td.textContent = '';
        td.removeAttribute('data-pnl');
        return;
      }
      var tone = r.costKnown ? pnlTone(r.price, r.cost) : '';
      if (tone) td.setAttribute('data-pnl', tone);
      else td.removeAttribute('data-pnl');
      var w = totalAsset > 0 ? (r.mv / totalAsset) * 100 : 0,
        cls = r.pnlAmt === null ? '' : r.pnlAmt >= 0 ? 'up' : 'down';
      var html =
        '<span class="cost-price">' +
        (r.costKnown ? trim(r.cost) : '—') +
        '</span><span class="cost-qty">' +
        qtyInt(r.qty) +
        '</span>' +
        '<span class="cost-mv">' +
        plain(r.mv) +
        '</span><span class="cost-pnl ' +
        cls +
        '">' +
        money(r.pnlAmt) +
        '（' +
        pct(r.pnlPct) +
        '）</span>' +
        '<span class="cost-weight">' +
        w.toFixed(1) +
        '%</span>';
      if (td._rendered !== html) {
        td.innerHTML = html;
        td._rendered = html;
      }
    }
    // 静态骨架只更新数字与列表内容；折叠状态、父节点和既有事件不重建。
    function updateText(selector, value, root) {
      var el = (root || document).querySelector(selector);
      if (el && el.textContent !== value) el.textContent = value;
      return el;
    }
    function updateTone(el, value) {
      if (!el) return;
      el.classList.toggle('up', value !== null && value >= 0);
      el.classList.toggle('down', value !== null && value < 0);
    }
    function renderPositionTotals(longs, shorts, totalAsset, totalPnl, tPct, cash, fixed) {
      [longs, shorts].forEach(function (arr, i) {
        var zone = document.getElementById(i ? 'group_positions_short' : 'group_positions_long');
        if (!zone) return;
        var mv = sumOf(arr, function (x) {
            return x.mv;
          }),
          unknown = arr.some(function (x) {
            return x.has && !x.costKnown;
          });
        var pnl = unknown
            ? null
            : sumOf(arr, function (x) {
                return x.pnlAmt;
              }),
          cost = sumOf(arr, function (x) {
            return x.cost * x.qty;
          });
        var pp = unknown ? null : cost > 0 ? (pnl / cost) * 100 : mv > 0 ? null : 0,
          stats = zone.querySelector('summary .zone-stats');
        if (stats) {
          var spans = stats.querySelectorAll('.zone-num');
          if (spans.length >= 3) {
            spans[0].textContent = plain(mv);
            spans[1].textContent = money(pnl) + '（' + pct(pp) + '）';
            spans[2].textContent = totalAsset > 0 ? ((mv / totalAsset) * 100).toFixed(1) + '%' : '—';
            updateTone(spans[1], pnl);
          }
        }
      });
      // 现金/固收行最左侧：长短持仓合计市值（股票/ETF），与右侧盈亏同口径；总资产改放持仓标题行。
      updateText('#group_positions>.cashbond-row .cb-total-amt', plain(sumOf(longs.concat(shorts), function (x) { return x.mv; })));
      var el = updateText(
        '#group_positions>.cashbond-row .cb-total-pnl',
        money(totalPnl) + '（' + pct(tPct) + '）',
      );
      if (el) {
        el.classList.toggle('is-up', totalPnl !== null && totalPnl >= 0);
        el.classList.toggle('is-down', totalPnl !== null && totalPnl < 0);
      }
      updateText('#group_positions>.cashbond-row .cb-cash .cb-amt', plain(cash));
      updateText('#group_positions>.cashbond-row .cb-bond .cb-amt', plain(fixed));
    }
    function paint() {
      if (!panel || disposed) return;
      if (_paintFrame) {
        cancelAnimationFrame(_paintFrame);
        _paintFrame = null;
      }
      var pidNode = document.getElementById(PID),
        active = document.activeElement,
        focus = null;
      if (editing && active && panel.contains(active) && active.getAttribute('data-f'))
        focus = {
          sym: active.getAttribute('data-sym'),
          field: active.getAttribute('data-f'),
          value: active.value,
          start: active.selectionStart,
          end: active.selectionEnd,
          direction: active.selectionDirection,
          error: active.validationMessage,
        };
      var cash = num(DATA.cash),
        fixed = num(DATA.fixed),
        fixedCost = num(DATA.fixed_cost),
        longs = [],
        shorts = [],
        totalMv = 0,
        totalPnl = 0,
        totalCost = 0,
        unknownCost = false;
      ROWS.forEach(function (r) {
        var it = DATA.items[r.sym] || { qty: 0 },
          cost = num(it.cost),
          qty = num(it.qty),
          held = isPosition(r);
        r.isLong = !!r.tr.closest('#group_positions_long');
        r.has = held && qty > 0;
        r.costKnown = own(it, 'cost');
        r.cost = cost;
        r.qty = qty;
        if (!r.has) {
          if (r.tick) r.tick.style.display = 'none';
          if (r.rail) r.rail.setAttribute('aria-label', r.lab0);
          r.mv = 0;
          r.pnlPct = 0;
          r.pnlAmt = 0;
          if (editing && held) (r.isLong ? longs : shorts).push(r);
          return;
        }
        r.mv = r.price * qty;
        r.pnlPct = r.costKnown && cost > 0 ? ((r.price - cost) / cost) * 100 : null;
        r.pnlAmt = r.costKnown ? (r.price - cost) * qty : null;
        totalMv += r.mv;
        totalPnl += r.pnlAmt === null ? 0 : r.pnlAmt;
        totalCost += cost * qty;
        if (!r.costKnown) unknownCost = true;
        (r.isLong ? longs : shorts).push(r);
        if (!r.costKnown || !r.rail || !(r.high > r.low)) {
          if (r.tick) r.tick.style.display = 'none';
          if (r.rail) r.rail.setAttribute('aria-label', r.lab0);
          return;
        }
        var pos = ((cost - r.low) / (r.high - r.low)) * 100,
          outside = pos < 0 ? 'out-lo' : pos > 100 ? 'out-hi' : '';
        r.tick.style.display = '';
        r.tick.className = 'range52-cost' + (outside ? ' ' + outside : '');
        r.tick.style.left = outside ? '' : pos.toFixed(1) + '%';
        r.rail.setAttribute(
          'aria-label',
          r.lab0 +
            '；成本价 ' +
            trim(cost) +
            (outside === 'out-lo'
              ? ' 低于52周低点 ' + r.lowTxt
              : outside === 'out-hi'
                ? ' 高于52周高点 ' + r.highTxt
                : ' 位于区间 ' + pos.toFixed(1) + '%'),
        );
      });
      var totalAsset = totalMv + cash + fixed;
      if (unknownCost) totalPnl = null;
      ROWS.forEach(function (r) {
        renderCostLine(r, totalAsset);
      });
      renderPnlPrices();
      var tcls = totalPnl === null ? 'dim' : totalPnl >= 0 ? 'up' : 'down',
        tPct = unknownCost ? null : totalCost > 0 ? (totalPnl / totalCost) * 100 : totalMv > 0 ? null : 0;
      var sumEl = panel.querySelector('.cost-sum');
      if (sumEl) {
        sumEl.innerHTML =
          '<b>' +
          plain(totalAsset) +
          '</b><span class="sep">·</span><b class="' +
          tcls +
          '">' +
          money(totalPnl) +
          '（' +
          pct(tPct) +
          '）</b>';
        sumEl.setAttribute(
          'title',
          '总资产含现金及固收；盈亏仅股票/ETF持仓，不含现金项及固收' +
            (unknownCost
              ? '；部分持仓成本未知，合计盈亏暂不计算'
              : totalMv > 0 && totalCost === 0
                ? '；零成本持仓不计算收益率'
                : ''),
        );
        sumEl.setAttribute(
          'aria-label',
          '总资产 ' + plain(totalAsset) + '；股票/ETF持仓盈亏 ' + money(totalPnl) + '，' + pct(tPct),
        );
      }
      renderPosTotal(totalAsset);
      renderPositionTotals(longs, shorts, totalAsset, totalPnl, tPct, cash, fixed);
      var cmp = editing
        ? function (a, b) {
            return (
              (own(editOrder, a.sym) ? editOrder[a.sym] : 999) -
              (own(editOrder, b.sym) ? editOrder[b.sym] : 999)
            );
          }
        : sortKey === 'wt'
          ? function (a, b) {
              return b.mv - a.mv;
            }
          : function (a, b) {
              return (b.pnlPct === null ? -Infinity : b.pnlPct) - (a.pnlPct === null ? -Infinity : a.pnlPct);
            };
      longs.sort(cmp);
      shorts.sort(cmp);
      var html = editing ? editorHtml(longs.concat(shorts), cash, fixedCost) : '';
      var cl = [],
        prevMv = 0;
      ROWS.forEach(function (r) {
        if (!r.has) return;
        var r0 = (r.chg || 0) / 100;
        if (!(r.prevClose > 0) && r0 <= -1) return;
        var prev = r.prevClose > 0 ? r.prevClose : r.price / (1 + r0);
        r.dayAmt = (r.price - prev) * r.qty;
        prevMv += prev * r.qty;
        cl.push({ sym: r.sym, cn: r.note, amt: r.dayAmt, chg: (r.price / prev - 1) * 100, lv: r.lv });
      });
      var dayList = document.querySelector('.dual-body>ul.cost-contrib[data-owner="day"]');
      if (!cl.length) {
        if (dayList) dayList.textContent = '';
        updateTone(updateText('#dayPnlCard .dp-amt', money(0)), 0);
        updateTone(updateText('#dayPnlCard .dp-pct', pct(0)), 0);
      }
      if (cl.length) {
        var gross = sumOf(cl, function (x) {
            return Math.abs(x.amt);
          }),
          mx = 0;
        cl.forEach(function (x) {
          x.share = gross > 0 ? (x.amt / gross) * 100 : 0;
          mx = Math.max(mx, Math.abs(x.share));
        });
        cl.sort(function (a, b) {
          return b.share - a.share;
        });
        var crows = cl
          .map(function (x) {
            var c = x.share >= 0 ? 'up' : 'down',
              w = mx > 0 ? Math.min((Math.abs(x.share) / mx) * 50, 50) : 0,
              targetRow = all('table.stk tr[data-symbol]').find(function (tr) { return tr.getAttribute('data-symbol') === x.sym; }),
              targetId = targetRow && targetRow.id;
            return (
              '<li class="lv-' +
              x.lv +
              '" aria-label="占当日盈亏绝对值 ' +
              fmt1(Math.abs(x.share)) +
              '%"><span class="cn-name">' +
              (targetId ? '<a class="signal-member-jump" href="#' + esc(targetId) + '">' : '') +
              '<span class="cnsym">' +
              esc(x.sym) +
              '</span>' +
              (x.cn ? ' <span class="cncn">' + esc(x.cn) + '</span>' : '') +
              (targetId ? '</a>' : '') +
              '</span><span class="cv ' +
              (x.chg >= 0 ? 'up' : 'down') +
              '">' +
              pct(x.chg) +
              '</span><span class="ca ' +
              c +
              '">' +
              money(x.amt) +
              '</span><span class="cbar"><i class="' +
              c +
              '" style="width:' +
              w.toFixed(1) +
              '%"></i></span></li>'
            );
          })
          .join('');
        var net = sumOf(cl, function (x) {
          return x.amt;
        });
        if (dayList && dayList._rendered !== crows) {
          dayList.innerHTML = crows;
          dayList._rendered = crows;
        }
        updateTone(updateText('#dayPnlCard .dp-amt', money(net)), net);
        updateTone(updateText('#dayPnlCard .dp-pct', pct(prevMv > 0 ? (net / prevMv) * 100 : 0)), net);
      }
      if (SYNC === 'nocloud' && !hadOwn)
        html =
          '<div class="cost-nocloud"><span>本机没有持仓数据，也没取到云端 —— 下面是内置快照' +
          (seedAsOf ? '（' + esc(seedAsOf) + '）' : '') +
          '，可能不是最新。</span><span class="nb-acts"><button type="button" id="costRetry">重试</button><button type="button" id="costImportBak">导入备份</button></span></div>' +
          html;
      if (pidNode && (pidNode !== membersNode || html !== membersHtml)) {
        pidNode.innerHTML = html;
        membersNode = pidNode;
        membersHtml = html;
      }
      if (editing)
        all('input[data-f]', panel).forEach(function (input) {
          var e = editDraft[input.getAttribute('data-sym') + '|' + input.getAttribute('data-f')];
          if (e) {
            input.value = e.raw;
            input.setCustomValidity(e.error);
          }
        });
      if (focus && editing) {
        var again = panel.querySelector('input[data-sym="' + focus.sym + '"][data-f="' + focus.field + '"]');
        if (again) {
          var de = editDraft[focus.sym + '|' + focus.field];
          again.value = focus.value;
          again.setCustomValidity(de ? de.error : focus.error || '');
          again.focus();
          try {
            again.setSelectionRange(focus.start, focus.end, focus.direction);
          } catch (e) {}
        }
      }
      var tk = ghToken(),
        msg = esc(SYNC_MSG),
        syncChip = Object.keys(storageErrors).length
          ? '<span class="warn">本地未持久化</span>'
          : isolated
            ? '<span class="warn">隔离预览 · 不上传</span>'
            : !writer
              ? '<span class="dim">本页只读</span>'
              : SYNC === 'pulling'
                ? '<span class="warn">拉取中…</span>'
                : SYNC === 'loading'
                  ? '<span class="warn">上传中…</span>'
                  : SYNC === 'error'
                    ? '<span class="warn" aria-label="' + msg + '">上传失败</span>'
                    : SYNC === 'nocloud'
                      ? '<span class="' +
                        (hadOwn ? 'dim' : 'warn') +
                        '" aria-label="没取到云端持仓数据；本机数据不受影响">云端未取到</span>'
                      : uncertain
                        ? '<span class="warn">上传结果待核验</span>'
                        : dirty || !sameAsCloud()
                          ? '<span class="warn">未同步' + (tk ? '' : ' · 未填 token') + '</span>'
                          : '<span class="ok" aria-label="仓位已同步' +
                            (CLOUD && CLOUD.updated_at
                              ? '：云端数据 ' + esc(String(CLOUD.updated_at).slice(0, 16))
                              : '') +
                            '">仓位已同步</span>';
      var mirror = syncMirrorEl();
      if (mirror) {
        mirror.innerHTML = syncChip;
        if (stateEl) stateEl.textContent = '';
      } else if (stateEl) stateEl.innerHTML = syncChip;
      var longMv = sumOf(longs, function (x) {
          return x.mv;
        }),
        shortMv = sumOf(shorts, function (x) {
          return x.mv;
        }),
        bar = '',
        leg = '',
        aria = [];
      [
        { c: 's-long', n: '长持仓', v: longMv },
        { c: 's-short', n: '短持仓', v: shortMv },
        { c: 's-cash', n: '现金', v: cash },
        { c: 's-bond', n: '固定收益', v: fixed },
      ].forEach(function (p) {
        if (p.v <= 0) return;
        var pc = totalAsset > 0 ? (p.v / totalAsset) * 100 : 0;
        bar += '<span class="seg ' + p.c + '" style="width:' + pc.toFixed(3) + '%"></span>';
        leg += '<span><i class="' + p.c + '"></i><b>' + pc.toFixed(1) + '%</b></span>';
        aria.push(p.n + ' ' + pc.toFixed(1) + '%');
        if (p.c === 's-short' && longMv + shortMv > 0) {
          var hp = totalAsset > 0 ? ((longMv + shortMv) / totalAsset) * 100 : 0;
          leg +=
            '<span aria-label="所有持仓 ' +
            hp.toFixed(1) +
            '%"><span class="seg-all" aria-hidden="true"><i class="s-long"></i><i class="s-short"></i></span><b>' +
            hp.toFixed(1) +
            '%</b></span>';
        }
      });
      var footHtml =
        '<div class="cost-bar" role="img" aria-label="资产结构：' +
        aria.join('，') +
        '">' +
        bar +
        '</div><div class="cost-footrow"><div class="cost-legend">' +
        leg +
        '</div></div>';
      if (footEl)
        footEl.innerHTML =
          footHtml +
          (isolated
            ? '<div class="cost-notice">' +
              ISOLATED_NOTICE +
              ' <button type="button" data-cost-export>导出备份</button></div>'
            : '');
      var asset = document.querySelector('#group_positions>.asset-row');
      if (asset && asset._rendered !== footHtml) {
        asset.innerHTML = footHtml;
        asset._rendered = footHtml;
      }
      clearDialogControls();
      syncCostNotice();
      document.dispatchEvent(new CustomEvent('cost:painted'));
    }
    paint();
    syncEditUI();
    if (editBtn) editBtn.removeAttribute('disabled');
    takeOwnership(function () {
      paint();
    });
  })();
</script>
