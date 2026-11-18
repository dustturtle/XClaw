"""Execute the actual embedded model selection client, without a running service."""
import json
from pathlib import Path
import shutil
import subprocess
import pytest
from xclaw.channels.web import _build_chat_page_html


def test_model_selector_binding_and_availability():
    if not shutil.which('node'):
        pytest.skip('node required')
    page = _build_chat_page_html(auth_enabled=True, model_name='codex/fixture-default', provider_name='native')
    load = page[page.index('    async function loadModels'):page.index('    async function openChat')]
    send = page[page.index('    async function sendMessage'):page.index('    async function cancelRun')]
    harness = r'''
const assert = require('node:assert/strict');
let active = {token:'t',chatId:'one'}, response;
const current = c => c===active, headers = c => ({Authorization:'Bearer '+c.token});
const controls=()=>{};
const modelInput={value:'',children:[],replaceChildren(){this.children=[]},appendChild(x){this.children.push(x)}};
const document={createElement(){return {}}};
let fetch=async(url, options)=>{assert.equal(url,'/api/chat/models');assert.equal(options.headers.Authorization,'Bearer t');return response};
'''+load+r'''
(async()=>{
response={ok:true,json:async()=>({model_options:['codex/fixture-default','codex/fixture-b','codex/fixture-a'],default_model:'codex/fixture-default'})};
await loadModels(active);assert.equal(modelInput.value,'codex/fixture-default');assert.equal(active.modelsReady,true);
await loadModels(active,'codex/fixture-a');assert.equal(modelInput.value,'codex/fixture-a');
await loadModels(active,'removed');assert.equal(modelInput.value,'removed');assert.equal(active.modelOptions.includes('removed'),false);assert.match(modelInput.children.at(-1).textContent,/不可用/);
response={ok:false};await loadModels(active);assert.equal(active.modelsReady,false);assert.equal(modelInput.value,'removed');assert.match(modelInput.children[0].textContent,/不可用/);
// A response for another chat must not overwrite the visible chat's model.
let resolve;fetch=()=>new Promise(r=>resolve=r);const old=active;const late=loadModels(old);active={token:'t',chatId:'two'};modelInput.value='second';resolve({ok:true,json:async()=>({model_options:['bad'],default_model:'bad'})});await late;assert.equal(modelInput.value,'second');
})().catch(e=>{console.error(e);process.exitCode=1});
'''
    result=subprocess.run(['node','-e',harness],capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    # Frozen before image reads, and payload / fingerprint uses the snapshot only.
    assert send.index('model: modelInput.value') < send.index('await Promise.all')
    assert 'attachments, model: pending.model' in send
    assert send.index('model: pending.model') < send.index('crypto.subtle.digest')
    assert '重试须保持原文字、模型、模式和图片' in send
    assert 'model: p.model' in page and 'model: receipt.model' in page
    assert 'ctx.pending?.model || saved?.model' in page
    assert 'busy || !ctx.modelsReady || status === "waiting_input"' in page
    assert 'ctx.pending?.requestId && !ctx.pending.receiptFinished' in page
    assert '!ctx.modelOptions?.includes(modelInput.value)' in page
    assert 'rejection ? "发送失败：" + rejection' in page
