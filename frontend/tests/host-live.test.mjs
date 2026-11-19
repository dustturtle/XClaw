import {test} from 'node:test';import assert from 'node:assert/strict';import vm from 'node:vm';import fs from 'node:fs';
const code=fs.readFileSync(new URL('../../xclaw/channels/static/openui-host.js',import.meta.url),'utf8');
class El{constructor(){this.children=[];this.isConnected=true;this.hidden=false;}append(e){this.insertBefore(e,null)}insertBefore(e,b){e.remove();this.children.splice(b?this.children.indexOf(b):this.children.length,0,e);e.parent=this;}remove(){if(this.parent){const a=this.parent.children;a.splice(a.indexOf(this),1);this.parent=null;}}setAttribute(k,v){this[k]=v}querySelector(){return null}}
function harness(){const updates=[],unmounts=[],item=new El();let current=true;const window={addEventListener(){},renderChatMarkdown:s=>'SAFE:'+s,XClawOpenUI:{update:(el,o)=>updates.push({el,...o}),unmount:el=>unmounts.push(el)}};vm.runInNewContext(code,{window,TextEncoder,document:{createElement:()=>new El()}});return {item,updates,unmounts,host:window.XClawOpenUIHost,options:{chatId:'c',runId:'run',getToken:()=>'',isCurrent:()=>current},stale:()=>current=false}}
const evidence=[{name:'stock_quote',arguments:{symbol:'600519'},content:'真实证据',is_error:false}];
const p=(blocks,extras={})=>({version:'xclaw-live-1',round:0,blocks,evidence,provisional:true,...extras});
const text=(source,id='p0')=>({id,type:'text',source}),card={id:'ui-p0-e0',type:'openui',source:'q=Query("stock_quote",{symbol:"600519"})\nroot=QuoteCard("行情",q)'};
test('live text grows around stable card; final reuses node and inline read-only evidence',()=>{
 const h=harness();assert.ok(h.host.updateLive(h.item,p([text('分析')]),h.options));
 assert.ok(h.host.updateLive(h.item,p([text('分析完成'),card,text('后续','p1')]),h.options));const el=h.updates[0].el;
 assert.equal(h.updates[0].inlineEvidence,evidence);assert.equal(h.updates[0].isStreaming,true);
 h.host.updateLive(h.item,p([text('分析完成'),card,text('后续解释','p1')]),h.options);assert.equal(h.updates.length,1);
 h.host.updateLive(h.item,p([text('分析完成'),card,text('后续解释','p1')],{provisional:false,validated:true}),h.options);
 assert.equal(h.updates.length,2);assert.equal(h.updates[1].el,el);assert.equal(h.updates[1].messageId,'run:ui-p0-e0');assert.equal(h.updates[1].isStreaming,false);assert.equal(h.unmounts.length,0);
 h.host.updateLive(h.item,p([text('分析')]),h.options);assert.equal(h.updates.length,2);
});
test('failure withdraws cards but preserves incomplete prose and cannot be resumed',()=>{
 const h=harness();h.host.updateLive(h.item,p([text('已收到'),card]),h.options);h.host.failLive(h.item);
 const container=h.item.children[0];assert.equal(container.children.length,2);assert.equal(container.children[1].innerHTML,'SAFE:已收到');assert.equal(h.unmounts.length,1);assert.match(container.children[0].textContent,/未完成/);
 h.host.failLive(h.item);assert.equal(container.children.length,2);
 h.host.updateLive(h.item,p([text('已收到新字'),card]),h.options);assert.equal(h.updates.length,1);
});
test('out-of-order old round and shorter prefix ignored, new round resets transient cards',()=>{
 const h=harness();h.host.updateLive(h.item,p([text('完整'),card],{round:1}),h.options);
 h.host.updateLive(h.item,p([text('旧')],{round:0}),h.options);h.host.updateLive(h.item,p([text('完')],{round:1}),h.options);assert.equal(h.unmounts.length,0);
 h.host.updateLive(h.item,p([text('新轮')],{round:2}),h.options);assert.equal(h.unmounts.length,1);assert.equal(h.item.children.length,1);
});
for(const [name,mutate] of [['text',x=>x.blocks[0].source='改写'],['card',x=>x.blocks[1].source='new'],['order',x=>x.blocks.reverse()],['evidence',x=>x.evidence=[]],['duplicate',x=>x.blocks.push({...x.blocks[0]})]])test('conflicting '+name+' fails closed preserving text',()=>{
 const h=harness();h.host.updateLive(h.item,p([text('原文'),card]),h.options);const next=p([text('原文'),{...card}]);mutate(next);assert.equal(h.host.updateLive(h.item,next,h.options),false);assert.equal(h.unmounts.length,1);assert.equal(h.item.children[0].children.at(-1).innerHTML,'SAFE:原文');
});
test('unvalidated final rejected; stale context never updates; clear unmounts',()=>{
 const h=harness();h.host.updateLive(h.item,p([text('原文'),card]),h.options);h.stale();assert.equal(h.host.updateLive(h.item,p([text('原文'),card]),h.options),false);h.host.clear();assert.equal(h.unmounts.length,1);assert.equal(h.item.children.length,0);
 const other=harness();assert.equal(other.host.updateLive(other.item,p([text('原文')],{provisional:false}),other.options),false);
});

test('validated final after disconnect restores withdrawn card in place without replacing text nodes',()=>{
 const h=harness(),blocks=[text('保留原文'),card,text('尾段','p1')];
 h.host.updateLive(h.item,p(blocks),h.options);const container=h.item.children[0],first=container.children[0],last=container.children[2];
 h.host.failLive(h.item);assert.equal(h.host.updateLive(h.item,p(blocks),h.options),false);
 assert.equal(h.host.updateLive(h.item,p(blocks,{provisional:false,validated:true,reply_hash:'a'.repeat(64)}),h.options),true);
 assert.equal(container.children.length,3);assert.equal(container.children[0],first);assert.equal(container.children[2],last);
 assert.equal(container['data-state'],'complete');assert.equal(h.updates.at(-1).isStreaming,false);assert.equal(h.updates.at(-1).inlineEvidence,evidence);
});
test('failed draft with conflicting final stays incomplete and reports false',()=>{
 const h=harness();h.host.updateLive(h.item,p([text('原文'),card]),h.options);h.host.failLive(h.item);
 assert.equal(h.host.updateLive(h.item,p([text('改写'),card],{provisional:false,validated:true}),h.options),false);
 assert.equal(h.item.children[0]['data-state'],'incomplete');assert.equal(h.updates.length,1);
});

test('failed terminal may extend trusted text prefix without retaining cards or rewriting prior words',()=>{
 const h=harness();h.host.updateLive(h.item,p([text('已显示'),card]),h.options);
 assert.equal(h.host.failLive(h.item,'已显示的新合法前缀\n\n后段'),true);
 const container=h.item.children[0];assert.equal(h.unmounts.length,1);assert.equal(container.children.at(-1).innerHTML,'SAFE:已显示的新合法前缀\n\n后段');
 assert.equal(h.host.failLive(h.item,'改写'),false);assert.equal(container.children.at(-1).innerHTML,'SAFE:已显示的新合法前缀\n\n后段');
 assert.equal(h.host.failLive(h.item,'已显示的新合法前缀\n\n后段更多'),true);
 assert.equal(container.children.at(-1).innerHTML,'SAFE:已显示的新合法前缀\n\n后段更多');
 assert.equal(h.host.updateLive(h.item,p([text('已显示'),card],{provisional:false,validated:true}),h.options),false);
 assert.equal(h.host.updateLive(h.item,p([text('已显示的新合法前缀'),card,text('后段更多','p1')],{provisional:false,validated:true}),h.options),true);
 assert.equal(container.children.length,3);assert.equal(container.children[0].hidden,false);
});
const pid='a'.repeat(32),action={id:'action-'+pid+'_a0',type:'action',source:'root=Button("确认", Action([]))',action_plan_id:pid+'_a0',summary:'确认新增记录'};
test('final inserts action by plan identity without remounting existing read card',()=>{
 const h=harness();h.host.updateLive(h.item,p([text('正文'),card]),h.options);const el=h.updates[0].el;
 assert.equal(h.host.updateLive(h.item,p([text('正文'),action,card],{provisional:false,validated:true,presentation_id:pid}),h.options),true);
 const read=h.updates.findLast(u=>u.inlineEvidence),write=h.updates.find(u=>u.actionPlanId);
 assert.equal(read.el,el);assert.equal(read.presentationId,pid);assert.equal(read.readonly,true);
 assert.equal(write.messageId,pid+':'+action.action_plan_id);assert.equal(write.presentationId,pid);assert.equal(write.actionPlanId,action.action_plan_id);assert.equal(write.inlineEvidence,undefined);assert.equal(h.unmounts.length,0);
});
for(const [name,extras,block] of [
 ['provisional',{presentation_id:pid},action],['missing identity',{provisional:false,validated:true},action],
 ['foreign plan',{provisional:false,validated:true,presentation_id:pid},{...action,action_plan_id:'b'.repeat(32)+'_a0'}]
])test('action '+name+' is rejected without mounting write Island',()=>{
 const h=harness();assert.equal(h.host.updateLive(h.item,p([text('正文'),block],extras),h.options),false);assert.equal(h.updates.length,0);
});

test('v2 completion notice requires exact confirmed plan, once, also on history recovery',()=>{
 const h=harness();h.host.updateLive(h.item,p([text('暂无记录'),action],{provisional:false,validated:true,presentation_id:pid}),h.options);
 const notify=h.updates[0].onOperationComplete;
 for(const value of [undefined,{status:'unknown',presentationId:pid,actionPlanId:action.action_plan_id},{status:'completed',presentationId:pid,actionPlanId:'other'},{status:'completed',presentationId:'other',actionPlanId:action.action_plan_id}])notify(value);
 assert.equal(h.item.children.length,1);
 const completed={status:'completed',presentationId:pid,actionPlanId:action.action_plan_id};notify(completed);notify(completed);
 assert.equal(h.item.children.length,2);assert.match(h.item.children[0].textContent,/正文保留操作前/);
 assert.equal(h.item.children[1].children[0].innerHTML,'SAFE:暂无记录');
 h.host.clear();assert.equal(h.item.children.length,0);
 notify(completed);assert.equal(h.item.children.length,0);
});
