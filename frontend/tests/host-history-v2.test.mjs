import {test} from 'node:test';import assert from 'node:assert/strict';import vm from 'node:vm';import fs from 'node:fs';import {webcrypto} from 'node:crypto';
const code=fs.readFileSync(new URL('../../xclaw/channels/static/openui-host.js',import.meta.url),'utf8');
class El{constructor(){this.children=[];this.isConnected=true;}append(e){this.insertBefore(e,null)}insertBefore(e,b){e.remove();this.children.splice(b?this.children.indexOf(b):this.children.length,0,e);e.parent=this}remove(){if(this.parent){this.parent.children.splice(this.parent.children.indexOf(this),1);this.parent=null;}}querySelector(){return null}setAttribute(){}}
const pid='a'.repeat(32),aid=pid+'_a0';
async function history(overrides={}){
 const item=new El(),updates=[],requests=[];
 const window={addEventListener(){},renderChatMarkdown:s=>s,XClawOpenUI:{update:(el,o)=>updates.push(o),unmount(){}}};
 const saved={presentation_id:pid,message_id:7,evidence:[],source:JSON.stringify({version:'xclaw-narrative-1',presentation_id:pid,blocks:[{id:'p0',type:'text',source:'确认说明'},{id:'action-'+aid,type:'action',source:'root=Button("确认",Action([]))',action_plan_id:aid,summary:'确认'}]}),...overrides};
 vm.runInNewContext(code,{window,TextEncoder,crypto:webcrypto,document:{createElement:()=>new El()},fetch:async(url,o)=>{requests.push({url,...o});return {ok:true,json:async()=>saved}}});
 const result=await window.XClawOpenUIHost.attach(item,'确认说明',{history:true,chatId:'chat',presentationId:pid,messageId:7,getToken:()=>'secret',isCurrent:()=>true});
 return {result,updates,requests,item};
}
test('history binds exact presentation and message IDs and restores final action without generate',async()=>{
 const h=await history();assert.equal(h.result,true);assert.equal(h.requests.length,1);assert.equal(h.requests[0].url,'/api/openui/presentation');
 const payload=JSON.parse(h.requests[0].body);assert.equal(payload.presentation_id,pid);assert.equal(payload.message_id,7);assert.match(payload.reply_hash,/^[a-f0-9]{64}$/);
 assert.equal(h.updates[0].actionPlanId,aid);assert.equal(h.updates[0].inlineEvidence,undefined);
});
for(const overrides of [{presentation_id:'b'.repeat(32)},{message_id:8}])test('mismatched history identity never mounts action or falls back to hash',async()=>{
 const h=await history(overrides);assert.equal(h.result,false);assert.equal(h.updates.length,0);assert.equal(h.requests.length,1);
});
