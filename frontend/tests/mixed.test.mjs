import test from 'node:test';import assert from 'node:assert/strict';import {parseMixed} from '../src/mixed.mjs';
test('plain text remains text',()=>assert.deepEqual(parseMixed('hello'),[{type:'text',source:'hello'}]));
test('incomplete fence cannot activate UI',()=>assert.equal(parseMixed('```openui\nroot = Col([])')[0].type,'text'));
test('mixed blocks retain order',()=>assert.deepEqual(parseMixed('hello\n```openui\nroot = Col([])\n```\nbye').map(x=>x.type),['text','openui','text']));
test('non OpenUI fence never executes',()=>assert.equal(parseMixed('```js\nalert(1)\n```')[0].type,'text'));
