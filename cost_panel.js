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
      LOCK_NAME = 'mm-position-writer-v2';
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
        if (own(j.items[k], 'cost')) d.items[k].cost = amount(j.items[k], 'cost');
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
      snapshotDraft = null;
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
    function draftError(raw) {
      var t = raw.trim(),
        v = t === '' ? 0 : Number(t);
      return /^(?:\d+(?:\.\d{0,4})?|\.\d{1,4})?$/.test(t) && isFinite(v) && v >= 0
        ? ''
        : '请输入非负数，最多四位小数';
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
        e.error = draftError(e.raw);
        if (e.error) {
          invalid = true;
          return;
        }
        var v = e.raw.trim() === '' ? 0 : Number(e.raw);
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
    function loadLocal() {
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
            )
              editDraft[k] = { raw: e.raw, error: draftError(e.raw) };
          });
        checkedDraft();
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
      saved._local = { revision: revision, dirty: dirty, uncertain: uncertain, editDraft: editDraft };
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
    function authorizePositions() {
      positionSave = { generation: ++positionGeneration, payload: clone(DATA) };
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
      if (editing || draftPending) return false;
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
      if (different) revision++;
      dirty = !cached;
      if (!persist()) dirty = true;
      return different;
    }
    function startPull() {
      if (isolated || !writer || disposed) return;
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
          if (meta && !meta.sha) throw new Error('缺少远端 SHA，禁止覆盖');
          uncertain = {
            payload: clone(sent.payload),
            revision: sent.revision,
            baseSha: sent.recovery ? sent.baseSha : meta ? meta.sha : null,
            positionGeneration: sent.positionGeneration,
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
        if (outcome === 'superseded') {
          uncertain = null;
          cloudVerified = false;
          dirty = true;
          persist();
          SYNC = 'local';
          SYNC_MSG = '';
          requestSave(0, false);
        } else if (ok) {
          uncertain = null;
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
      clearSaveTimers();
      editGeneration++;
      pullGeneration++;
      if (restore && snapshot && canonical(DATA) !== canonical(snapshot)) {
        DATA = clone(snapshot);
        changed();
      }
      if (restore) {
        editDraft = snapshotDraft || {};
        draftPending = prevDraftPending;
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
      if (cloudFirstPending && !draftPending && !positionsAuthorized) startPull();
      if (dirty || uncertain) requestSave(0, false);
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
              '#costRetry,#costImportBak,#costSave,#costCancel,#costState,[data-cost-export]',
            )
          : null;
      if (!t) return;
      e.preventDefault();
      e.stopPropagation();
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
        var next = checkedDraft();
        if (!next) {
          persist();
          paint();
          showNotice(draftNotice());
          return;
        }
        DATA = next;
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
        authorizePositions();
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
      if (!writer || !editing || disposed || !el || !el.getAttribute) return;
      var f = el.getAttribute('data-f'),
        sym = el.getAttribute('data-sym');
      if (['cash', 'cash_extra', 'fixed', 'fixed_cost', 'cost', 'qty'].indexOf(f) < 0) return;
      if ((f === 'cost' || f === 'qty') && !isPosition(rowBySym(sym))) return;
      var key = sym + '|' + f;
      editDraft[key] = { raw: el.value, error: draftError(el.value) };
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
    function editorHtml(rows, cash, fixed, fixedCost) {
      var heading = ['标的', '成本价', '数量', '持仓市值']
        .map(function (label) {
          return '<span class="ep-h">' + label + '</span>';
        })
        .join('');
      var stocks = rows
        .map(function (r) {
          return (
            '<span class="ep-sym" data-cost-row data-sym="' +
            esc(r.sym) +
            '">' +
            esc(r.sym) +
            (r.note ? ' ' + esc(r.note) : '') +
            '</span>' +
            inp(r.sym, 'cost', r.costKnown ? vt(r.cost) : '', '成本价') +
            inp(r.sym, 'qty', vt(r.qty), '数量') +
            '<span class="ep-val">' +
            (r.has ? plain(r.mv) : '—') +
            '</span>'
          );
        })
        .join('');
      var fields = [
        ['现金 1', 'USD', 'cash', cashFirst(DATA)],
        ['固收', 'BOND', 'fixed', fixed],
        ['现金 2', 'USD', 'cash_extra', DATA.cash_extra],
        ['固收成本', 'BOND', 'fixed_cost', fixedCost],
      ]
        .map(function (f) {
          return '<label><span>' + f[0] + '</span>' + inp(f[1], f[2], f[3] ? vt(f[3]) : '', f[0]) + '</label>';
        })
        .join('');
      return (
        '<div class="ep-grid">' +
        heading +
        stocks +
        '</div><div class="ep-cash">' +
        fields +
        '</div>' +
        '<div class="ep-cash-total">现金合计 USD$' +
        trim4(cash) +
        '</div>'
      );
    }
    function fmt1(n) {
      return (n >= 0 ? '+' : '') + n.toFixed(1);
    }
    var rsEl = document.querySelector('#group_positions>summary .group-stats .rs-spy');
    if (!rsEl) {
      var st = document.querySelector('#group_positions>summary .group-stats');
      if (st) {
        rsEl = document.createElement('span');
        rsEl.className = 'rs-spy';
        st.appendChild(rsEl);
      }
    }
    function renderRs() {
      if (!rsEl) return;
      rsEl.className = 'rs-spy';
      rsEl.textContent = '';
      rsEl.removeAttribute('title');
      var rs = REAL && REAL.rs20;
      if (!rs || !rs.items) return;
      var v1 = 0,
        v0 = 0,
        n = 0,
        miss = [];
      ROWS.forEach(function (r) {
        if (!isPosition(r)) return;
        var it = DATA.items[r.sym],
          q = it ? num(it.qty) : 0;
        if (!(q > 0)) return;
        var ri = rs.items[r.sym];
        if (typeof ri !== 'number') {
          miss.push(r.sym);
          return;
        }
        var mv = q * r.price;
        v1 += mv;
        v0 += mv / (1 + ri / 100);
        n++;
      });
      if (!n || !(v0 > 0)) return;
      var R = (v1 / v0 - 1) * 100,
        diff = R - num(rs.bench),
        cls = diff >= 0.5 ? 'up' : diff <= -0.5 ? 'down' : '';
      rsEl.className = 'rs-spy' + (cls ? ' ' + cls : '');
      rsEl.textContent = (rs.days || 20) + '日对.SPX ' + fmt1(diff) + '%';
      rsEl.setAttribute(
        'aria-label',
        '持仓 20 日 ' +
          fmt1(R) +
          '% − ' +
          (rs.bench_name || '标普500') +
          ' ' +
          fmt1(num(rs.bench)) +
          '%（' +
          (rs.start || '') +
          '→' +
          (rs.end || '') +
          '，' +
          n +
          ' 只按市值加权）' +
          (miss.length ? '；' + miss.join('、') + ' 无 20 日数据未计入' : ''),
      );
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
              value: pct(r.pnlPct),
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
      updateText('#group_positions>.cashbond-row .cb-total-amt', plain(totalAsset));
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
      renderRs();
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
      var html = editing ? editorHtml(longs.concat(shorts), cash, fixed, fixedCost) : '';
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
              w = mx > 0 ? Math.min((Math.abs(x.share) / mx) * 50, 50) : 0;
            return (
              '<li class="lv-' +
              x.lv +
              '" aria-label="占当日盈亏绝对值 ' +
              fmt1(Math.abs(x.share)) +
              '%"><span class="cn-name"><span class="cnsym">' +
              esc(x.sym) +
              '</span>' +
              (x.cn ? ' <span class="cncn">' + esc(x.cn) + '</span>' : '') +
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
