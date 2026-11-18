import {allowedEvidence,type Evidence} from './streaming';
import {trust} from './catalog';
export type InlineEvidence=Evidence&{content:string;is_error:false};
/** Receipt-authenticated, read-only snapshot transport. Never contacts a tool. */
export function inlineProvider(evidence:InlineEvidence[]){
 const provider:Record<string,(args:Record<string,unknown>)=>Promise<unknown>>=Object.create(null);
 for(const e of evidence){
  if(e.is_error!==false||typeof e.content!=='string')continue;
  provider[e.name]=async args=>{
   const matched=evidence.filter(x=>x.is_error===false&&allowedEvidence(e.name,args,[x]));
   if(matched.length!==1)throw Error('当前证据不唯一或不匹配');
   let data:unknown;try{data=JSON.parse(matched[0].content)}catch{data={content:matched[0].content}}
   return trust(data);
  };
 }
 return provider;
}
