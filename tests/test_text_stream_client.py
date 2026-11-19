"""Execute the actual generated page's receipt client with a small DOM/SSE harness."""
import json
from pathlib import Path
import shutil
import subprocess
import pytest
from xclaw.channels.web import _build_chat_page_html


def test_actual_client_preview_reset_terminal_and_receipt():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node required for actual client execution')
    html = _build_chat_page_html(auth_enabled=True, model_name='test', provider_name='test', supports_receipts=True)
    source = html[html.index('    function finishInline'):html.index('    async function pollProgress')]
    controller = html[html.index('    let followingTail'):html.index('}).observe(messagesEl, {childList:true});') + len('}).observe(messagesEl, {childList:true});')]
    stop = html[html.index('    function stopObservation'):html.index('    function pauseObservation')]
    harness = r'''
const recordStreamDiagnostic=()=>{};
const listeners={},frames=[];let resize;
const requestAnimationFrame=cb=>frames.push(cb),flush=()=>{while(frames.length)frames.shift()()};
class ResizeObserver{constructor(cb){resize=cb} observe(){}} class MutationObserver{observe(){}}
const assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
const parser=vm.createContext({atob});
vm.runInContext(fs.readFileSync('xclaw/channels/static/markdown-it.min.js','utf8'),parser);
vm.runInContext(fs.readFileSync('xclaw/channels/static/markdown.js','utf8'),parser);
const window={addEventListener(){},renderChatMarkdown:parser.renderChatMarkdown,XClawOpenUIHost:{attach(){throw Error('preview must not attach OpenUI')}}};
let active,queue=[],waiting,posts=0,opens=0,finals=[],drafts=0;
const messagesEl={addEventListener:(name,cb)=>listeners[name]=cb,scrollHeight:2000,scrollTop:1600,clientHeight:400,children:[],appendChild(x){this.children.push(x)},get lastElementChild(){return this.children.at(-1)}};
const document={getElementById(){return {addEventListener(){}}},createElement(){return {children:[],_text:'',_html:'',set innerHTML(v){this._html=v;messagesEl.scrollHeight+=200},get innerHTML(){return this._html},appendChild(x){this.children.push(x)},set textContent(t){this._text=t},get textContent(){return this._text+this.children.map(c=>c.textContent).join('')},remove(){messagesEl.children=messagesEl.children.filter(x=>x!==this)}}}};
const current=ctx=>ctx===active, headers=()=>({Authorization:'Bearer secret'});
const stageEl={},progressMessageEl={},timeEl={},resumeBtn={};
const TERMINAL=new Set(['failed','completed','cancelled']);
const syncPresentationStatus=()=>{},loadWriteRecovery=()=>{},setStatus=()=>{},controls=()=>{},clearAcceptedDraft=()=>{},saveDraft=()=>drafts++;
const applySnapshot=()=>{throw Error('unexpected progress branch')};
const appendMessage=(role,text,a,b,ui)=>{finals.push({role,text,ui});if(ui)opens++;messagesEl.appendChild({className:'message '+role,textContent:text})};
const fetch=async(url,options)=>{
 assert.match(url,/\/receipts\/r1\/events\?/);assert.equal(options.headers.Authorization,'Bearer secret');
 if(options.method==='POST')posts++;
 return {ok:true,body:{getReader:()=>({read:()=>new Promise(resolve=>{if(queue.length)resolve(queue.shift());else waiting=resolve})})}};
};

function send(event){const p={done:false,value:new TextEncoder().encode('event: snapshot\ndata: '+JSON.stringify(event)+'\n\n')};if(waiting){const w=waiting;waiting=null;w(p)}else queue.push(p)}
const tick=()=>new Promise(r=>setImmediate(r));
const ctx=()=>({chatId:'room',pending:{requestId:'r1',settled:false},epoch:0});
async function start(c){active=c;void observeLiveText(c);await tick()}
async function event(e){send(e);await tick()}
(async()=>{
 let c=ctx();await start(c);
 await event({seq:1,round:1,text:'真实首字'});
 flush();assert.equal(messagesEl.scrollTop,messagesEl.scrollHeight-messagesEl.clientHeight);assert.equal(messagesEl.children.length,1);assert.match(c.liveBubble.textContent,/正在生成/);assert.match(c.liveContent.innerHTML,/真实首字/);assert.equal(opens,0);assert.equal(finals.length,0);
 messagesEl.scrollHeight+=500;resize();flush();assert.equal(messagesEl.scrollTop,2300);
 resize();listeners.wheel({deltaY:-5,deltaX:0});messagesEl.scrollTop=300;listeners.scroll();flush();await event({seq:2,round:1,text:'真实首字继续'});assert.equal(messagesEl.scrollTop,300);assert.equal(messagesEl.children.length,1);
 messagesEl.scrollHeight=700;resize();messagesEl.scrollTop=300;listeners.scroll();flush();assert.equal(followingTail,false);
 listeners.wheel({deltaY:4,deltaX:0});flush();assert.equal(followingTail,true);messagesEl.scrollHeight+=200;resize();flush();assert.equal(messagesEl.scrollTop,500);
 const link={parentElement:messagesEl,closest:()=>null};
 listeners.keydown({key:'PageUp',target:{closest:()=>({})}});assert.equal(followingTail,true);
 listeners.keydown({key:'Home',target:link,defaultPrevented:true});assert.equal(followingTail,true);
 listeners.keydown({key:'PageUp',target:link});assert.equal(followingTail,false);
 messagesEl.scrollTop=250;listeners.scroll();messagesEl.scrollHeight+=100;resize();flush();assert.equal(messagesEl.scrollTop,250);
 await event({seq:1,round:1,text:'过期'});assert.match(c.liveContent.innerHTML,/继续/);
 await event({seq:3,round:1,text:'### 标题\n\n**重要**\n\n| 股票 | 价格 |\n| --- | --- |\n| 测试 | 1 |\n\n<script>alert(1)</script><img src=x onerror=alert(1)>[危险](javascript:alert(1))'});
 assert.match(c.liveContent.innerHTML,/<h3>标题<\/h3>/);assert.match(c.liveContent.innerHTML,/<strong>重要<\/strong>/);assert.match(c.liveContent.innerHTML,/<table>/);
 assert.ok(!/<script|<img|href="javascript:/i.test(c.liveContent.innerHTML));assert.match(c.liveContent.innerHTML,/&lt;script&gt;/);assert.equal(opens,0);
 await event({seq:4,round:1,text:'**未闭合'});assert.match(c.liveContent.innerHTML,/\*\*未闭合/);assert.ok(!c.liveContent.innerHTML.includes('<strong>'));
 await event({seq:5,round:2,text:''});assert.equal(messagesEl.children.length,0);
 await event({seq:6,round:2,text:'终轮预览',truncated:true});assert.match(c.liveBubble.textContent,/预览已达上限/);
 await event({seq:7,terminal:true,text:'终轮预览末批'});
 assert.equal(messagesEl.children.length,1);assert.match(c.liveContent.innerHTML,/终轮预览末批/);assert.equal(c.liveLabel.textContent,'正在确认结果…');assert.equal(finals.length,0);assert.equal(opens,0);
 // Durable receipt may arrive several polls later: preview must not disappear.
 await tick();await tick();assert.equal(messagesEl.children.length,1);
 applyReceipt(c,{request_id:'r1',status:'completed',reply:'完整正文'});
 applyReceipt(c,{request_id:'r1',status:'completed',reply:'完整正文'});
 assert.equal(finals.length,1);assert.equal(opens,1);assert.equal(messagesEl.children.length,1);
 messagesEl.children=[];c=ctx();await start(c);await event({seq:1,text:'失败前预览'});
 applyReceipt(c,{request_id:'r1',status:'failed',reply:'未完成'});
 assert.equal(c.liveBubble,null);assert.equal(messagesEl.children.length,1);assert.equal(finals.at(-1).ui,false);assert.equal(opens,1);assert.equal(drafts,1);
 // Resolve the disconnected reader after failure; it must not recreate a preview.
 await event({seq:2,text:'迟到文本'});assert.equal(messagesEl.children.length,1);
 messagesEl.children=[];c=ctx();await start(c);await event({seq:1,text:'取消前'});
 stopObservation(c);assert.equal(c.liveBubble,null);assert.equal(messagesEl.children.length,0);
 await event({seq:2,text:'取消后迟到'});assert.equal(messagesEl.children.length,0);
 // Late subscription: reset+delta+terminal coalesced into the first snapshot.
 messagesEl.children=[];c=ctx();await start(c);
 await event({seq:3,round:1,text:'已结束但未持久确认的真实文本',terminal:true});
 assert.equal(messagesEl.children.length,1);assert.match(c.liveContent.innerHTML,/真实文本/);assert.equal(c.liveLabel.textContent,'正在确认结果…');assert.equal(opens,1);
 // Failure resets may coalesce with terminal: never retain the previous draft.
 clearLiveText(c);messagesEl.children=[];c=ctx();await start(c);
 await event({seq:1,round:1,text:'后来失败的草稿'});assert.equal(messagesEl.children.length,1);
 await event({seq:3,round:2,text:'',terminal:true});assert.equal(messagesEl.children.length,0);assert.equal(c.liveBubble,null);assert.equal(opens,1);
 // A failed inline parse can include a newly accepted prefix in this snapshot.
 messagesEl.children=[];c=ctx();await start(c);await event({seq:1,text:'旧前缀'});
 c.inlinePresentation={version:'xclaw-live-1',blocks:[]};c.liveContent.hidden=true;
 await event({seq:2,text:'旧前缀新合法文字',draft_failed:true,terminal:true});
 assert.equal(c.liveContent.hidden,false);assert.match(c.liveContent.innerHTML,/旧前缀新合法文字/);assert.match(c.liveLabel.textContent,/未完成/);
 applyReceipt(c,{request_id:'r1',status:'failed',reply:'失败'});await tick();
 assert.equal(messagesEl.children.length,1);assert.match(messagesEl.children[0].children[1].innerHTML,/新合法文字/);
 assert.equal(posts,0);process.stdout.write('client assertions passed');
})().catch(e=>{console.error(e);process.exitCode=1});
'''
    result = subprocess.run([node, '-e', source + stop + harness.replace('const current=ctx', controller + '\nconst current=ctx')], text=True, capture_output=True, timeout=15, cwd=Path(__file__).resolve().parents[1])
    assert result.returncode == 0, result.stderr
    assert result.stdout == 'client assertions passed'


def test_actual_page_layout_status_outlives_receipt_and_is_context_scoped():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node required')
    html = _build_chat_page_html(auth_enabled=True,model_name='test',provider_name='test',supports_receipts=True,supports_openui=True)
    source = html[html.index('    function syncPresentationStatus'):html.index('    function controls')]
    source += html[html.index('    function finishInline'):html.index('    async function pollProgress')]
    harness = r'''
const recordStreamDiagnostic=()=>{};
const followMessages=()=>{};
const assert=require('node:assert/strict');
let active,status='',callbacks=[];
const SUPPORTS_OPENUI=true,TERMINAL=new Set(['failed','completed','cancelled']);
const current=c=>c===active,setStatus=s=>status=s;
const stageEl={},timeEl={},progressMessageEl={},resumeBtn={};
const messagesEl={children:[],scrollHeight:0,scrollTop:0,clientHeight:0,offsetTop:0,appendChild(x){this.children.push(x)},get lastElementChild(){return this.children.at(-1)}};
const document={createElement(){return {children:[],offsetTop:0,textContent:'',appendChild(c){this.children.push(c)}}}};
const window={XClawOpenUIHost:{attach(item,text,options){callbacks.push(options.onPresentationStatus)}}};
const loadWriteRecovery=()=>{},controls=()=>{},saveDraft=()=>{},clearAcceptedDraft=()=>{};
const applySnapshot=()=>{throw Error('unexpected')};
const c={chatId:'test',pending:{requestId:'one',settled:false}};active=c;
const result={request_id:'one',status:'completed',reply:'正文已完成'};
applyReceipt(c,result);assert.equal(status,'响应完成');assert.equal(callbacks.length,1);
callbacks[0]('generating');assert.equal(status,'正在整理图表');
applyReceipt(c,result);assert.equal(status,'正在整理图表');assert.equal(messagesEl.children.length,1);
callbacks[0]('failed');assert.equal(status,'文字已完成，图表未完成');
applyReceipt(c,result);assert.equal(status,'文字已完成，图表未完成');
callbacks[0]('completed');assert.equal(status,'响应完成');
appendMessage('assistant','历史',[],true);callbacks[1]('generating');assert.equal(status,'响应完成');
c.pending={requestId:'two',settled:false};status='新任务处理中';callbacks[0]('failed');assert.equal(status,'新任务处理中');
appendMessage('assistant','第二条');callbacks[2]('generating');assert.equal(status,'正在整理图表');
appendMessage('user','更新的问题');status='新问题处理中';callbacks[2]('completed');assert.equal(status,'新问题处理中');
appendMessage('assistant','第三条');active={chatId:'other'};status='其他会话';callbacks[3]('failed');assert.equal(status,'其他会话');
process.stdout.write('layout status assertions passed');
'''
    result = subprocess.run([node,'-e',source+harness],capture_output=True,text=True,timeout=15)
    assert result.returncode == 0, result.stderr
    assert result.stdout == 'layout status assertions passed'


def test_opt_in_diagnostic_uses_two_frames_and_only_metrics():
    node=shutil.which('node')
    if not node: pytest.skip('Node required')
    html=_build_chat_page_html(auth_enabled=False,model_name='t',provider_name='t')
    source=html[html.index('    function clearStreamDiagnostic'):html.index('    function onStreamDiagnosticEvent')]
    script=r'''
const assert=require('node:assert/strict');
let frames=[],now=0,panels=[];
const requestAnimationFrame=f=>frames.push(f),performance={now:()=>now};
const window={location:{search:''},innerHeight:800,innerWidth:900};
const document={visibilityState:'visible',createElement:()=>({children:[],appendChild(x){this.children.push(x)},remove(){panels=panels.filter(p=>p!==this)},textContent:''})};
const messagesEl={insertAdjacentElement:(where,p)=>panels.push(p),getBoundingClientRect:()=>({top:50,bottom:750,left:0,right:900})};
const ctx={pending:{requestId:'private-id'},liveBubble:{isConnected:true,getBoundingClientRect:()=>({top:600,bottom:780,left:20,right:800})}},current=c=>c===ctx;
recordStreamDiagnostic(ctx,'snapshot',{seq:1,text:'SECRET'});assert.equal(panels.length,0);assert.equal(frames.length,0);
window.location.search='?stream_diagnostics=1';recordStreamDiagnostic(ctx,'snapshot',{seq:2,round:1,text:'SECRET'});
assert.equal(frames.length,1);frames.shift()();assert.equal(ctx.streamDiagnostic.rows.textContent,'');now=32;frames.shift()();
assert.match(ctx.streamDiagnostic.rows.textContent,/32ms snapshot seq=2 round=1 chars=6 visibility=visible intersects=true/);
assert.ok(!ctx.streamDiagnostic.rows.textContent.includes('SECRET'));assert.ok(!ctx.streamDiagnostic.rows.textContent.includes('private-id'));
ctx.liveBubble=null;recordStreamDiagnostic(ctx,'final-replaced');frames.shift()();frames.shift()();
assert.match(ctx.streamDiagnostic.rows.textContent,/final-replaced.*intersects=false/);
// Same run updates one panel; a new request replaces it.
recordStreamDiagnostic(ctx,'snapshot');assert.equal(panels.length,1);
ctx.pending.requestId='next-request';recordStreamDiagnostic(ctx,'snapshot');assert.equal(panels.length,1);
// Teardown removes the outside-message panel and invalidates queued frames.
clearStreamDiagnostic(ctx);assert.equal(panels.length,0);assert.equal(ctx.streamDiagnostic,null);
while(frames.length)frames.shift()();assert.equal(panels.length,0);
recordStreamDiagnostic(ctx,'snapshot');assert.equal(panels.length,1);
clearStreamDiagnostic(ctx);assert.equal(panels.length,0);
process.stdout.write('diagnostic assertions passed');
'''
    result=subprocess.run([node,'-e',source+script],text=True,capture_output=True,timeout=15)
    assert result.returncode==0,result.stderr
    assert result.stdout=='diagnostic assertions passed'


def test_query_diagnostic_metadata_whitelist():
    node=shutil.which('node')
    if not node: pytest.skip('Node required')
    html=_build_chat_page_html(auth_enabled=False,model_name='t',provider_name='t')
    source=html[html.index('    function onStreamDiagnosticEvent'):html.index('    function finishInline')]
    script=r'''
const assert=require('node:assert/strict'),records=[];
let active={};let listener;
const window={location:{search:''},addEventListener:(name,fn)=>{assert.equal(name,'xclaw-stream-diagnostic');listener=fn}};
const recordStreamDiagnostic=(ctx,phase)=>records.push(phase);
'''
    checks=r'''
listener({detail:{phase:'query-start',tool:'stock_quote'}});assert.equal(records.length,0);
window.location.search='?stream_diagnostics=1';
listener({detail:{phase:'query-start',tool:'stock_quote',args:'SECRET',content:'PRIVATE'}});
listener({detail:{phase:'query-end',tool:'stock_quote'}});listener({detail:{phase:'provider-ready'}});
assert.deepEqual(records,['query-start tool=stock_quote','query-end tool=stock_quote','provider-ready']);
for(const detail of [{phase:'evil SECRET'}, {phase:'query-start',tool:'token=SECRET'}, {phase:'query-start',tool:'a'.repeat(65)}, {phase:'query-start',tool:{x:'SECRET'}}])listener({detail});
assert.equal(records.length,3);
listener({detail:{phase:'card-data-visible',cardType:'HistoryChart',documentVisible:true,inViewport:false,content:'SECRET'}});
assert.equal(records[3],'card-data-visible card=HistoryChart documentVisible=true inViewport=false');
listener({detail:{phase:'card-data-visible',cardType:'HistoryChart',documentVisible:'SECRET',inViewport:true}});assert.equal(records.length,4);
active=null;listener({detail:{phase:'provider-ready'}});assert.equal(records.length,4);
process.stdout.write('metadata assertions passed');
'''
    result=subprocess.run([node,'-e',script+source+checks],text=True,capture_output=True,timeout=15)
    assert result.returncode==0,result.stderr
    assert result.stdout=='metadata assertions passed'


def test_recovery_summary_preserves_mixed_write_warning():
    html=_build_chat_page_html(auth_enabled=False,model_name='t',provider_name='t')
    source=html[html.index('    function recoverySummary'):html.index('    async function loadWriteRecovery')]
    script="""
const assert=require('node:assert/strict');
assert.equal(recoverySummary([{tool_name:'strategy_scan'}]),'上次策略分析需要核对');
assert.equal(recoverySummary([{tool_name:'strategy_scan'},{tool_name:'portfolio'}]),'有写操作需要人工核对');
assert.equal(recoverySummary([]),'有写操作需要人工核对');
"""
    node=shutil.which('node')
    if not node: pytest.skip('Node required')
    result=subprocess.run([node,'-e',source+script],text=True,capture_output=True,timeout=15)
    assert result.returncode==0,result.stderr
