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
  return {normalize, ignoredReason, valid, parseEbk, looksLikeEbk};
});
