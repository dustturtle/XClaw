import {test} from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import fs from 'node:fs';
import {webcrypto} from 'node:crypto';
const code=fs.readFileSync(new URL('../../xclaw/channels/static/openui-host.js',import.meta.url),'utf8');
class El {
 constructor(){this.children=[];this.hidden=false;this.isConnected=true;this.listeners={};this.parent=null;}
 append(...els){els.forEach(e=>this.insertBefore(e,null));}
 insertBefore(el,anchor){el.remove(); const i=anchor?this.children.indexOf(anchor):this.children.length;this.children.splice(i,0,el);el.parent=this;}
 remove(){if(this.parent){const a=this.parent.children;a.splice(a.indexOf(this),1);this.parent=null;}}
 replaceChildren(){this.children.forEach(e=>e.parent=null);this.children=[];}
 setAttribute(){} addEventListener(k,fn){this.listeners[k]=fn;}
 querySelector(q){return q.includes('markdown')?this.children.find(e=>e.className==='markdown'):null;}
}
async function setup(historySource=null, reply="first\n\nsecond"){
 const item=new El(),original=new El();original.className='markdown';item.append(original);
 const statuses=[],updates=[],unmounts=[],cache=new Map(),frames=[];let waiting,closed=false,current=true,fetchReady=false;
 const reader={read(){if(frames.length)return Promise.resolve({value:frames.shift(),done:false});if(closed)return Promise.resolve({done:true});return new Promise(r=>waiting=r);}};
 const push=raw=>{const value=typeof raw === "string" ? new TextEncoder().encode(raw) : raw;if(waiting){const r=waiting;waiting=null;r({value,done:false});}else frames.push(value);};
 const event=(event,data)=>push(`event: ${event}\r\ndata: ${JSON.stringify(data)}\r\n\r\n`);
 const end=()=>{closed=true;if(waiting){waiting({done:true});waiting=null;}};
 const window={addEventListener(){},renderChatMarkdown:s=>s,XClawOpenUI:{update:(el,opts)=>updates.push({el,...opts}),unmount:el=>unmounts.push(el),parseMixed:s=>[{type:'openui',source:s}]}};
 const context=vm.createContext({window,crypto:webcrypto,TextEncoder,TextDecoder,Uint8Array,Map,Set,AbortController,sessionStorage:{getItem:k=>cache.get(k)??null,setItem:(k,v)=>cache.set(k,v)},document:{createElement:()=>new El()},setTimeout,clearTimeout,fetch:async(_url,opts)=>{fetchReady=true;if(_url === "/api/openui/presentation")return {ok:true,json:async()=>({source:historySource})};opts.signal?.addEventListener('abort',end);return {ok:true,body:{getReader:()=>reader}};}});
 vm.runInContext(code,context);
 const promise=window.XClawOpenUIHost.attach(item,reply,{chatId:'room',history:historySource!==null,onPresentationStatus:s=>statuses.push(s),getToken:()=>'',isCurrent:()=>current});
 const flush=async()=>{for(let i=0;i<8;i++)await new Promise(r=>setImmediate(r));};await flush();
 for(let i=0;!fetchReady && i<200;i++)await new Promise(r=>setTimeout(r,1));
 assert.equal(fetchReady,true,"host request is ready before event assertions");
 const start=()=>event('narrative_start',{reply,paragraphs:['first','second'],evidence:[{name:'stock_quote',arguments:{symbol:'600519'}}]});
 const source='q = Query("stock_quote", {symbol:"600519"})\nroot = Col([QuoteCard("报价", q)])';
 const final=()=>JSON.stringify({version:'xclaw-narrative-1',blocks:[{id:'p0',type:'text',source:'first'},{id:'p0u0',type:'openui',source},{id:'p1',type:'text',source:'second'}]});
 return {statuses,item,original,updates,unmounts,cache,event,end,push,start,source,final,promise,flush,window,setCurrent:v=>current=v};
}
test('partial first card precedes done, keyed DOM survives deltas and final; no partial cache',async()=>{
 const h=await setup();h.start();h.event('placement_start',{id:'p0u0',after:0});h.event('placement_delta',{id:'p0u0',text:h.source.slice(0,30)});await h.flush();
 assert.equal(h.original.hidden,true);assert.ok(h.updates.length);const el=h.updates.at(-1).el;
 assert.equal(h.updates.at(-1).isStreaming,true);assert.equal(h.cache.size,0);
 assert.equal(h.updates.at(-1).evidence[0].name,'stock_quote');
 h.event('placement_delta',{id:'p0u0',text:h.source.slice(30)});h.event('placement_end',{id:'p0u0'});await h.flush();
 assert.equal(h.updates.at(-1).el,el);assert.equal(h.unmounts.length,0);
 h.event('done',{source:h.final()});h.end();await h.promise;
 assert.equal(h.updates.at(-1).isStreaming,false);assert.equal(h.updates.at(-1).el,el);assert.equal(h.cache.size,1);assert.equal(h.unmounts.length,0);
});
for(const mode of ['truncated','cancel','mismatch','duplicate','after_done','switch'])test(mode+' rolls back temporary layout, never caches',async()=>{
 const h=await setup();h.start();h.event('placement_start',{id:'p0u0',after:0});h.event('placement_delta',{id:'p0u0',text:h.source});await h.flush();
 if(mode==='cancel')h.item.children[1].children[2].listeners.click();
 else if(mode==='switch'){h.setCurrent(false);h.window.XClawOpenUIHost.clear();}
 else if(mode==='duplicate')h.event('placement_start',{id:'p0u0',after:0});
 else if(mode==='mismatch'){h.event('placement_end',{id:'p0u0'});h.event('done',{source:h.final().replace('报价','different')});}
 else if(mode==='after_done'){h.event('placement_end',{id:'p0u0'});h.event('done',{source:h.final()});h.event('placement_delta',{id:'p0u0',text:'bad'});}
 h.end();await h.promise;assert.equal(h.original.hidden,false);assert.equal(h.cache.size,0);assert.ok(h.unmounts.length);
});
test('UTF8 and CRLF can split at every byte boundary',async()=>{
 const h=await setup();const events=[['narrative_start',{reply:'first\n\nsecond',paragraphs:['first','second'],evidence:[]}],['placement_start',{id:'p0u0',after:0}],['placement_delta',{id:'p0u0',text:h.source}],['placement_end',{id:'p0u0'}],['done',{source:h.final()}]];
 // Split both multibyte Chinese UTF8 and CRLF across actual byte reads.
 for(const [event,data] of events){const frame=`event: ${event}\r\ndata: ${JSON.stringify(data)}\r\n\r\n`;for(const byte of new TextEncoder().encode(frame))h.push(Uint8Array.of(byte));}
 h.end();await h.promise;assert.equal(h.cache.size,1);assert.equal(h.original.hidden,true);
});

test('two placements at same anchor preserve first node and actions remain locked until final',async()=>{
 const h=await setup();h.start();
 for(let n=0;n<2;n++){h.event('placement_start',{id:'p0u'+n,after:0});h.event('placement_delta',{id:'p0u'+n,text:h.source});h.event('placement_end',{id:'p0u'+n});await h.flush();}
 const first=h.updates[0].el;assert.equal(h.unmounts.length,0);
 const final=JSON.parse(h.final());final.blocks.splice(2,0,{id:'p0u1',type:'openui',source:h.source});
 assert.ok(h.updates.every(u=>u.isStreaming));
 h.event('done',{source:JSON.stringify(final)});h.end();await h.promise;
 assert.equal(h.updates.filter(u=>u.messageId.endsWith(':p0u0')).at(-1).el,first);
 assert.equal(h.unmounts.length,0);assert.equal(h.cache.size,1);
});
test('legacy JSON delta is never passed to DSL renderer',async()=>{
 const h=await setup();h.event('delta',{text:'{"placements":['});await h.flush();assert.equal(h.updates.length,0);
 h.event('done',{source:h.final()});h.end();await h.promise;assert.equal(h.cache.size,1);assert.ok(h.updates.every(u=>!u.isStreaming));
});

test('saved narrative history mounts final trusted blocks without requesting generation',async()=>{
 const source=JSON.stringify({version:'xclaw-narrative-1',blocks:[{id:'p0',type:'text',source:'first'},{id:'p0u0',type:'openui',source:'root = Col([PortfolioCard("记录", q)])'},{id:'p1',type:'text',source:'second'}]});
 const h=await setup(source);await h.promise;
 assert.equal(h.original.hidden,true);assert.equal(h.updates.length,1);assert.equal(h.updates[0].isStreaming,false);assert.ok(h.updates[0].messageId.endsWith(':p0u0'));
 assert.equal(h.cache.size,0);
});

test('empty streamed plan allows server validated final-only fallback without fake early card',async()=>{
 const h=await setup();h.start();await h.flush();assert.equal(h.updates.length,0);
 h.event('done',{source:h.final()});h.end();await h.promise;
 assert.equal(h.updates.length,1);assert.equal(h.updates[0].isStreaming,false);assert.equal(h.cache.size,1);assert.equal(h.original.hidden,true);
});

for(const [name,paragraphs] of [
 ['changed-number',['first 100','second']], ['reordered',['second','first']],
 ['omitted',['first']], ['changed-whitespace',['first ','second']]
]) test('rejects '+name+' prose before hiding original',async()=>{
 const h=await setup();h.event('narrative_start',{reply:'first\n\nsecond',paragraphs,evidence:[]});h.end();await h.promise;
 assert.equal(h.original.hidden,false);assert.equal(h.updates.length,0);assert.equal(h.cache.size,0);
});
for(const [reply,paragraphs] of [
 ['before\n\n```text\na\n\nb\n```\n\nafter',['before','```text\na\n\nb\n```','after']],
 ['- first\n\n- second',['- first\n\n- second']],
 ['[source][s]\n\n[s]: https://example.com',['[source][s]\n\n[s]: https://example.com']],
 ['before\n \nsecond',['before','second']]
]) test('preserves complete markdown parser scope '+JSON.stringify(reply),async()=>{
 const h=await setup(null,reply);h.event('narrative_start',{reply,paragraphs,evidence:[]});await h.flush();
 assert.equal(h.original.hidden,true);assert.equal(h.item.children[1].children[3].children.length,paragraphs.length);
 h.event('done',{source:'root = Col([])'});h.end();await h.promise;assert.equal(h.original.hidden,false);
});
test('rejects splitting a fenced block despite identical words',async()=>{
 const reply='```text\na\n\nb\n```',h=await setup(null,reply);
 h.event('narrative_start',{reply,paragraphs:['```text\na','b\n```'],evidence:[]});h.end();await h.promise;
 assert.equal(h.original.hidden,false);assert.equal(h.updates.length,0);assert.equal(h.cache.size,0);
});

test('new presentation reports generating until authoritative final, then completed',async()=>{
 const h=await setup();assert.deepEqual(h.statuses,[]);h.start();h.event('placement_start',{id:'p0u0',after:0});h.event('placement_delta',{id:'p0u0',text:h.source});await h.flush();
 assert.deepEqual(h.statuses,['generating']);h.event('placement_end',{id:'p0u0'});h.event('done',{source:h.final()});h.end();await h.promise;
 assert.deepEqual(h.statuses,['generating','completed']);
});
test('failure and user cancel report failed, switching context never reports stale terminal',async()=>{
 for(const mode of ['failure','cancel','switch']){
  const h=await setup();h.start();await h.flush();
  if(mode==='cancel')h.item.children[1].children[2].listeners.click();
  if(mode==='switch'){h.setCurrent(false);h.window.XClawOpenUIHost.clear();}
  h.end();await h.promise;
  assert.deepEqual(h.statuses,mode==='switch'?['generating']:['generating','failed']);
 }
});
test('history and text-only do not report fake graph generation',async()=>{
 const history=await setup('root = Col([QuoteCard("行情", q)])');await history.promise;assert.deepEqual(history.statuses,[]);
 const h=await setup();h.event('done',{source:JSON.stringify({version:'xclaw-empty-1',outcome:'text_only'})});h.end();await h.promise;assert.deepEqual(h.statuses,['completed']);
});
