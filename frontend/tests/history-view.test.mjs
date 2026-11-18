import test from 'node:test';
import assert from 'node:assert/strict';
import {build} from 'esbuild';
const compiled=await build({stdin:{contents:"import {renderToStaticMarkup} from 'react-dom/server'; import {createElement} from 'react'; import {HistoryView} from './src/HistoryView.tsx'; import {IntradayView,FinancialView} from './src/FinancialViews.tsx'; export const render=content=>renderToStaticMarkup(createElement(HistoryView,{content})); export const intraday=data=>renderToStaticMarkup(createElement(IntradayView,{data})); export const financial=data=>renderToStaticMarkup(createElement(FinancialView,{data}));",resolveDir:process.cwd(),loader:'tsx'},bundle:true,write:false,platform:'node',format:'cjs'});
// In-memory bundle: never overwrites the application build during parallel work.
import {createRequire} from 'node:module';
const mod={exports:{}};
new Function('module','exports','require',compiled.outputFiles[0].text)(mod,mod.exports,createRequire(import.meta.url));
const render=mod.exports.render;
const sample='数据源: test\n数据截至: 2026-11-16\n获取时间: 2026-11-17T01:57:46Z\n日期 开盘 收盘 最高 最低\n2026-10-16 1311 1329 1330 1300\n2026-11-16 1426 1453.9 1455 1425';
test('history shows date/price axes, trusted close values and keyboard point labels',()=>{
 const html=render(sample);assert.match(html,/2026-10-16 至 2026-11-16/);assert.match(html,/收盘价/);assert.match(html,/aria-label="2026-10-16 收盘 1329.00"/);assert.match(html,/tabindex="0"/);assert.match(html,/腾讯|数据源: test/);assert.match(html,/复权口径以数据源为准/);
});
test('raw service output is preserved but collapsed by default',()=>{
 const html=render(sample);assert.match(html,/<details class="xcui-raw-data">/);assert.doesNotMatch(html,/<details[^>]*open/);assert.match(html,/行情明细（2 条）/);assert.match(html,/<pre>数据源: test/);
});
test('empty or malformed data has an explicit state and no invented line',()=>{
 for(const content of ['暂无数据','2026-11-16 1 invalid','2026-11-16 1 2']){const html=render(content);assert.match(html,/暂无历史行情/);assert.doesNotMatch(html,/<polyline/);}
});
test('flat series produces finite geometry',()=>{
 const html=render('2026-11-13 10 10\n2026-11-16 10 10');assert.match(html,/<polyline/);assert.doesNotMatch(html,/NaN|Infinity/);
});
test('raw source cannot inject markup',()=>{
 const html=render('<img src=x onerror=alert(1)>');assert.doesNotMatch(html,/<img/);assert.match(html,/&lt;img/);
});

test('minute chart uses returned date/close and discloses no previous close',()=>{
 const html=mod.exports.intraday({date:'2026-11-17',timezone:'Asia/Shanghai',adjustment:'未复权',bars:[{time:'2026-11-17 09:31:00',close:'16.94'},{time:'2026-11-17 09:32:00',close:'16.93'}]});
 assert.match(html,/一分钟收盘价/);assert.match(html,/收盘 16.94/);assert.match(html,/未提供昨收/);assert.match(html,/09:31/);assert.doesNotMatch(html,/<details[^>]*open/);
});
test('minute chart rejects wrong day, null and nonnumeric values',()=>{
 for(const close of [null,'','garbage'])assert.doesNotMatch(mod.exports.intraday({date:'2026-11-17',bars:[{time:'2026-11-17 09:31:00',close},{time:'2026-11-17 09:32:00',close:'16'}]}),/<polyline/);
 assert.doesNotMatch(mod.exports.intraday({date:'2026-11-17',bars:[{time:'2026-11-16 09:31:00',close:'16'},{time:'2026-11-16 09:32:00',close:'17'}]}),/<polyline/);
});
test('financial reports preserve decimal precision, scope and missing-vs-zero',()=>{
 const html=mod.exports.financial({symbol:'600519.SH',source:'测试源',reports:[{'报告期':'20260930','币种':'CNY','金额单位':'元','口径':'年初至报告期末累计，非单季','营业收入':'12345678901234567.89','净利润':'0','经营活动产生的现金流量净额':null}]});
 assert.match(html,/12,345,678,901,234,567.89/);assert.match(html,/>0</);assert.match(html,/未提供/);assert.match(html,/非单季/);assert.match(html,/CNY/);assert.match(html,/<table>/);assert.doesNotMatch(html,/<details[^>]*open/);
});
test('financial presentation trims insignificant zeros and formats source dates',()=>{
 const html=mod.exports.financial({reports:[{'报告期':'20260930','公告日期':'20261031','营业收入':'130970743649.040000'}]});
 assert.match(html,/2026-09-30/);assert.match(html,/2026-10-31/);assert.match(html,/<td>130,970,743,649.04<\/td>/);assert.match(html,/130970743649.040000/);
});
test('cards keep sources and times without implementation narration',()=>{
 const html=mod.exports.intraday({date:'2026-11-17',source:'新浪财经一分钟K线',fetched_at:'2026-11-17T10:28:47+08:00',data_as_of:'2026-11-17 10:27:00',notice:'保守剔除当前分钟',bars:[{time:'2026-11-17 10:26:00',close:'17'},{time:'2026-11-17 10:27:00',close:'17.01'}]});
 assert.match(html,/新浪财经/);assert.match(html,/数据截至：/);assert.match(html,/10:27:00/);assert.match(html,/非实时推送/);assert.doesNotMatch(html,/分析依据|分析证据快照|已返回分钟线/);assert.match(html,/<details[\s\S]*保守剔除当前分钟/);
});

test('balance sheets remain point-in-time and each publication date is distinct',()=>{
 const h=mod.exports.financial({reports:[{'报告期':'20251231','资产总计':'123456.00','负债合计':'7890','公告日期':'20260430','资产负债表公告日期':'20261028'}]});
 assert.match(h,/期末余额/);assert.match(h,/资产总计/);assert.match(h,/123,456/);assert.match(h,/2026-04-30/);assert.match(h,/2026-10-28/);
});
test('financial display rounds source tails in decimal cents without changing raw data',()=>{
 const h=mod.exports.financial({reports:[{'报告期':'20251231','营业收入':'174144069958.24997','资产总计':'188252218704.16998'}]});assert.match(h,/<td>174,144,069,958.25<\/td>/);assert.match(h,/<td>188,252,218,704.17<\/td>/);assert.match(h,/174144069958.24997/);assert.match(h,/金额展示至分/);
});
test('verified disclosure precedes comparisons with safe official citation and scope',()=>{
 const h=mod.exports.financial({reports:[],verified_disclosures:[{summary:'收入确认调整影响同比可比性',interpretation_limit:'不等同经营规模下降',sources:[{title:'官方年报',url:'https://example.com/report.pdf',pages:[2,3]},{title:'不安全',url:'javascript:alert(1)'}]}]});assert.match(h,/收入确认调整/);assert.match(h,/不等同经营规模/);assert.match(h,/href="https:\/\/example.com\/report.pdf"/);assert.doesNotMatch(h,/href="javascript/);assert.match(h,/不代表完整附注/);
});
