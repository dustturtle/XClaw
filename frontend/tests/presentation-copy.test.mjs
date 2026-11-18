import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
const source=readFileSync(new URL('../src/index.tsx',import.meta.url),'utf8');
test('initial island does not narrate internal evidence/state machinery',()=>{
 for(const text of ['分析证据快照','分析依据保留原快照','xcui-evidence-mode','正在连接业务能力'])assert.equal(source.includes(text),false);
 assert.match(readFileSync(new URL('../src/refresh-batch.mjs',import.meta.url),'utf8'),/已更新至最新数据/);assert.match(source,/结果未知/);assert.match(source,/刷新数据/);
});
