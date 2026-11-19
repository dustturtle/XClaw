/** Sample committed DOM only after two browser animation frames; never a data-ready proxy. */
export function observeCardPaint(element,cardType,env){
 if(new URLSearchParams(env.location.search).get('stream_diagnostics')!=='1')return ()=>{};
 let cancelled=false,second=0;
 const first=env.requestAnimationFrame(()=>{second=env.requestAnimationFrame(()=>{
  if(cancelled||!element.isConnected)return;
  const r=element.getBoundingClientRect();
  if(r.width<=0||r.height<=0)return;
  env.dispatchEvent(new env.CustomEvent('xclaw-stream-diagnostic',{detail:{phase:'card-data-visible',cardType,documentVisible:env.document.visibilityState==='visible',inViewport:r.bottom>0&&r.right>0&&r.top<env.innerHeight&&r.left<env.innerWidth}}));
 })});
 return ()=>{cancelled=true;env.cancelAnimationFrame(first);if(second)env.cancelAnimationFrame(second)};
}
