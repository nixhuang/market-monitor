'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const I = require('../list-import.js');
const result = I.parseEbk('\uFEFF31#AAPL\r\n31#BRK.B\r\n31#SGOV\r\n74#00700\r\n1600519\r\n31#.SPX\r\n31#ESmain\r\nBD#US10Y\r\n2USDCNY\r\n2XAUUSD\r\n31#AAPL\r\nUNKNOWN#CODE\r\n');
assert.deepEqual(result.symbols, ['AAPL','BRK-B','.SPX','ESMAIN','BD#US10Y','2USDCNY','2XAUUSD']);
assert.equal(result.duplicates,1);
assert.equal(result.ignored.length,3);
assert.deepEqual(result.rejected,['UNKNOWN#CODE']);
assert.equal(I.normalize('BRK.B-US'),'BRK-B');
assert.equal(I.valid('.NDX'),true);
assert.equal(I.valid('SGOV-US'),false);
assert.equal(I.valid('00700-HK'),false);
assert.equal(I.valid('600519-SH'),false);
assert.equal(I.looksLikeEbk('31#MSFT\n74#00700'),true);
assert.equal(I.looksLikeEbk('symbol,name\nMSFT,Microsoft'),false);
assert.equal(I.looksLikeEbk('symbol\nAAPL\n2USDCNY\nMSFT'),false);
assert.equal(I.valid('NQmain'),true);
const root=path.resolve(__dirname,'..');
const cfg=JSON.parse(fs.readFileSync(path.join(root,'holdings.json'),'utf8'));
const audit=JSON.parse(fs.readFileSync(path.join(root,'import-audit.json'),'utf8'));
const groups=JSON.parse(fs.readFileSync(path.join(root,'groups.json'),'utf8'));
const actual=new Set(groups.flatMap(g=>Object.keys(cfg[g.key])));
const categorized=new Set(groups.slice(1).flatMap(g=>Object.keys(cfg[g.key])));
assert.equal(audit.total_raw,152);
assert.equal(audit.total_eligible,141);
assert.equal(audit.ignored.length,11);
assert.equal(actual.size,134);
assert.deepEqual(audit.missing_after,['TLT','TXN','2USDCNY','IAUM','.DJI','VOO','2XAUUSD']);
assert.deepEqual(audit.extra_after,[]);
assert.deepEqual(audit.supplemented,[]);
for(const [group,source] of Object.entries(audit.source_categories)) {
  assert.deepEqual(Object.keys(cfg[group]),source.symbols,group+' must match original membership and order');
}
assert(!('VOO' in cfg.index_funds));
for(const s of ['SPYM','SCHD','QQQM'])assert(s in cfg.positions,'Holdings must be preserved '+s);
for(const g of groups) for(const [s,c] of Object.entries(cfg[g.key])) {
  assert(!I.ignoredReason(s),'Excluded '+s);
  assert(c.note,'Missing company name '+s);
}
assert.equal(cfg.positions['BRK-B'].note,'伯克希尔哈撒韦');
assert.equal(Object.keys(cfg.focus).length,8);
console.log('全部通过：原十三分组成员和顺序一致，无自动补归；14项持仓保留，134个在册唯一代码。');
