import test from 'node:test';
import assert from 'node:assert/strict';
import {handleRenderErrors} from '../src/render-errors.mjs';
test('cleared renderer errors never hide successful content',()=>{
 let calls=0;
 assert.equal(handleRenderErrors([],()=>calls++),'');
 assert.equal(calls,0);
});
test('actual renderer errors restore original prose once',()=>{
 let calls=0;
 assert.equal(handleRenderErrors([{message:'invalid component'}],()=>calls++),'invalid component');
 assert.equal(calls,1);
});
