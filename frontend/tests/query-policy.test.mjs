import test from 'node:test';
import assert from 'node:assert/strict';
import {queryMode} from '../src/query-policy.mjs';
test('initial rendering never refetches evidence',()=>assert.equal(queryMode({},'stock_quote'),'snapshot'));
test('explicit input changes and action queries can read fresh data',()=>{
 assert.equal(queryMode({changed:true},'stock_history'),'live');
 assert.equal(queryMode({active:1},'watchlist_manage'),'live');
});
test('write invalidation is scoped to its business data',()=>{
 const state={dirty:new Set(['portfolio_manage'])};
 assert.equal(queryMode(state,'portfolio_manage'),'live');
 assert.equal(queryMode(state,'stock_quote'),'snapshot');
});
