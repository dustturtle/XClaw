"""Exercise actual page finalization functions, including receipt/stream ordering."""
from test_chat_progress_web import run_page_script

SETUP = r'''
await initial();
evaluate(`
 window.XClawOpenUIHost={calls:[],updateLive(item,p){this.calls.push(['update',p]);return true},failLive(){this.calls.push(['fail'])},disposeLive(){this.calls.push(['dispose'])}};
 active.liveRequestId='request-inline';active.liveBubble=document.createElement('article');active.liveBubble.classList={remove(){}};
 active.liveLabel={remove(){}};active.liveNote={remove(){}};
 active.inlinePresentation={version:'xclaw-live-1',round:1,blocks:[{id:'p0',type:'text',source:'正文'}],evidence:[],provisional:true};
`);
'''

def test_validated_inline_commit_uses_same_bubble_without_layout_request():
    run_page_script(SETUP + r'''
evaluate(`active.inlinePresentation.provisional=false;active.inlinePresentation.validated=true;finishInline(active,'正文','completed')`);
await flush();
assert.equal(evaluate('window.XClawOpenUIHost.calls[0][0]'),'update');
assert.equal(evaluate('active.liveBubble'),null);
assert.equal(evaluate('window.XClawOpenUIHost.calls[0][1].reply_hash'),require('node:crypto').createHash('sha256').update('正文').digest('hex'));
assert.ok(!requests.some(r=>r.url.includes('/api/openui/')));
''')


def test_receipt_before_commit_reads_persisted_presentation_never_generates():
    run_page_script(SETUP + r'''
evaluate(`finishInline(active,'正文','completed')`);await flush();
request('/api/openui/presentation','POST').reply({source:JSON.stringify({version:'xclaw-narrative-1',blocks:[{id:'p0',type:'text',source:'正文'}]})});await flush();
assert.equal(evaluate('window.XClawOpenUIHost.calls[0][0]'),'update');
assert.ok(!requests.some(r=>r.url.includes('/generate')));
''')


def test_cancelled_inline_never_promotes_or_requests_final_layout():
    run_page_script(SETUP + r'''
evaluate(`finishInline(active,'正文','cancelled')`);await flush();
assert.equal(evaluate('window.XClawOpenUIHost.calls[0][0]'),'fail');
assert.ok(!requests.some(r=>r.url.includes('/api/openui/')));
''')


def test_mismatched_final_prose_retracts_cards_and_preserves_authoritative_reply():
    run_page_script(SETUP + r'''
evaluate(`window.renderChatMarkdown=s=>s;active.inlinePresentation.provisional=false;active.inlinePresentation.validated=true;finishInline(active,'正式正文','completed')`);await flush();
assert.equal(evaluate('window.XClawOpenUIHost.calls[0][0]'),'fail');
assert.equal(evaluate('window.XClawOpenUIHost.calls[1][0]'),'dispose');
assert.ok(!evaluate('window.XClawOpenUIHost.calls.some(x=>x[0]==="update")'));
''')


def test_normal_commit_rejects_wrong_top_level_hash():
    run_page_script(SETUP + r'''
evaluate(`window.renderChatMarkdown=s=>s;active.inlinePresentation.provisional=false;active.inlinePresentation.validated=true;active.inlinePresentation.reply_hash='bad';finishInline(active,'正文','completed')`);await flush();
assert.equal(evaluate('window.XClawOpenUIHost.calls[0][0]'),'fail');
assert.ok(!evaluate('window.XClawOpenUIHost.calls.some(x=>x[0]==="update")'));
''')


def test_switch_chat_tears_down_previous_diagnostic_panel():
    run_page_script(r'''
await initial();
evaluate(`window.diagnosticRemovals=0;active.streamDiagnostic={panel:{remove(){window.diagnosticRemovals++}}};void openChat()`);
await flush();assert.equal(evaluate('window.diagnosticRemovals'),1);
assert.equal(evaluate('active.streamDiagnostic'),undefined);
''')
