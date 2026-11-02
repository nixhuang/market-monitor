(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.MMListImport = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';
  const special = /^(?:\.[A-Z0-9]+|BD#[A-Z0-9]+|2USDCNY|2XAUUSD)$/;
  function normalize(value) {
    let s = String(value || '').replace(/^\uFEFF/, '').trim().toUpperCase().replace(/^\$/, '');
    if (s.startsWith('31#')) s = s.slice(3);
    if (special.test(s)) return s;
    const suffix = s.match(/^(.+?)[-./](US|HK|SH|SZ|SS|SG|JP|UK|AU|CA)$/);
    if (suffix) {
      const [, code, market] = suffix;
      if (market === 'US') return code.replace(/[./]/g, '-');
      return code + '.' + (market === 'SH' ? 'SS' : market);
    }
    return s.replace(/[./]/g, '-');
  }
  function ignoredReason(value) {
    const raw = String(value || '').trim().toUpperCase();
    const s = normalize(raw);
    if (s === 'SGOV') return 'SGOV';
    if (/^74#/.test(raw) || /^\d+\.HK$/.test(s)) return '港股';
    if (/^[01]\d{6}$/.test(raw) || /^\d+\.(SS|SZ)$/.test(s) || /^\d{5,6}$/.test(raw)) return 'A股/港股';
    return '';
  }
  function valid(value) {
    const s = normalize(value);
    return !ignoredReason(value) && (special.test(s) || /^[A-Z][A-Z0-9-]{0,11}$/.test(s));
  }
  function parseEbk(text) {
    const symbols = [], ignored = [], rejected = [], seen = new Set();
    let duplicates = 0;
    for (const line of String(text).replace(/^\uFEFF/, '').split(/\r?\n/)) {
      const raw = line.trim();
      if (!raw) continue;
      const reason = ignoredReason(raw);
      if (reason) { ignored.push({raw, reason}); continue; }
      if (!/^(31#|BD#|2USDCNY$|2XAUUSD$)/i.test(raw) || !valid(raw)) {
        rejected.push(raw); continue;
      }
      const s = normalize(raw);
      if (seen.has(s)) { duplicates++; continue; }
      seen.add(s); symbols.push(s);
    }
    return {symbols, names: {}, ignored, rejected, duplicates, format: 'EBK', how: '富途 EBK 市场标识识别'};
  }
  function looksLikeEbk(text) {
    const lines = String(text).replace(/^\uFEFF/, '').split(/\r?\n/).map(line => line.trim()).filter(Boolean);
    return lines.length > 0 && lines.every(line => /^(?:31#|74#|BD#|[01]\d{6}$|2USDCNY$|2XAUUSD$)/i.test(line));
  }
  // 按完整记录扫描；引号内的换行、分隔符和双引号均属于字段内容。
  function csvRecords(text) {
    const source = String(text).replace(/^\uFEFF/, '');
    function scan(delimiter) {
      const rows = []; let row = [], field = '', quoted = false, closed = false, started = false;
      const pushField = () => { row.push(field); field = ''; closed = false; started = false; };
      const pushRow = () => { pushField(); if (row.some(value => value.trim()) || row.length > 1) rows.push(row); row = []; };
      for (let i = 0; i < source.length; i++) {
        const c = source[i];
        if (quoted) {
          if (c === '"') {
            if (source[i + 1] === '"') { field += '"'; i++; }
            else { quoted = false; closed = true; }
          } else field += c;
        } else if (c === delimiter) pushField();
        else if (c === '\r' || c === '\n') {
          pushRow(); if (c === '\r' && source[i + 1] === '\n') i++;
        } else if (c === '"' && !started && !closed) { quoted = true; started = true; }
        else {
          if (closed || c === '"') throw new Error('CSV 引号格式错误：双引号应包住整个字段，字段内双引号需写成两个双引号');
          field += c; started = true;
        }
      }
      if (quoted) throw new Error('CSV 引号未闭合，请检查跨行字段');
      if (row.length || started || closed || field) pushRow();
      return rows;
    }
    // 两种分隔符分别验证，避免把引号内的逗号数量当成分隔符证据。
    const candidates = [], errors = [];
    for (const delimiter of [',', '\t']) {
      try { candidates.push(scan(delimiter)); } catch (error) { errors.push(error); }
    }
    if (!candidates.length) throw errors[0];
    return candidates.sort((a, b) => (b[0]?.length || 0) - (a[0]?.length || 0))[0];
  }
  return {normalize, ignoredReason, valid, parseEbk, looksLikeEbk, csvRecords};
});
