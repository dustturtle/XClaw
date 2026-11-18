import {autoClose,parseExpression,split,tokenize,type ASTNode} from '@openuidev/lang-core';
export type Evidence = {name:string;arguments:Record<string,unknown>};
const canonical=(v:unknown):string=>JSON.stringify(v&&typeof v==='object'&&!Array.isArray(v)?Object.fromEntries(Object.entries(v).sort(([a],[b])=>a.localeCompare(b)).map(([k,x])=>[k,JSON.parse(canonical(x))])):Array.isArray(v)?v.map(x=>JSON.parse(canonical(x))):v);
export function allowedEvidence(name:unknown,args:unknown,evidence:Evidence[]){return evidence.some(e=>e.name===name&&canonical(e.arguments)===canonical(args))}
/** Snapshot adapter for SDK 1.0, whose isStreaming flag blocks every Query.
 * Only terminated, balanced, read-only queries with fully known literal/state
 * arguments enter the live snapshot. The presentation-only tail may still stream.
 * The original source is never rewritten or saved as this transient snapshot.
 */
export function streamingSource(source:string,evidence:Evidence[]=[]){
 const code=source.replace(/^\uFEFF/,'').replace(/^\s*```(?:openui(?:-lang)?)?[^\S\n]*\n/i,'').replace(/\n```[^\S\n]*$/,'');
 if(code.trimStart().startsWith('```'))return '';
 // Mask comments without touching quoted content or positions.
 const chars=[...code];let quote='',escape=false,comment=false;
 for(let i=0;i<chars.length;i++){
  const c=chars[i];
  if(comment){if(c==='\n')comment=false;else chars[i]=' ';continue;}
  if(escape){escape=false;continue;}
  if(quote){if(c==='\\')escape=true;else if(c===quote)quote='';continue;}
  if(c==='"'||c==="'"){quote=c;continue;}
  if(c==='#'||c==='/'&&chars[i+1]==='/'){comment=true;chars[i]=' ';}
 }
 const clean=chars.join(''),parts:string[]=[];let start=0,ternary=0,malformed=false;const stack:string[]=[];quote='';escape=false;
 for(let i=0;i<clean.length;i++){
  const c=clean[i];
  if(escape){escape=false;continue;}
  if(quote){if(c==='\\')escape=true;else if(c===quote)quote='';continue;}
  if(c==='"'||c==="'"){quote=c;continue;}
  if('([{'.includes(c))stack.push(c);
  else if(')]}'.includes(c)){if(stack.pop()!=='([{'[')]}'.indexOf(c)]){malformed=true;break;}}
  else if(!stack.length&&c==='?')ternary++;
  else if(!stack.length&&c===':'&&ternary)ternary--;
  else if(c==='\n'&&!stack.length&&!ternary){
   const next=clean.slice(i+1).trimStart();
   // A following token proves this is a boundary, not a continued ternary.
   if(!next||next.startsWith('?'))continue;
   parts.push(clean.slice(start,i));start=i+1;
  }
 }
 // A line terminator proves a balanced final statement is complete. Do not
 // inject a sentinel into an unfinished quoted presentation string.
 if(!malformed&&!quote&&!stack.length&&!ternary&&clean.endsWith("\n")){parts.push(clean.slice(start));start=clean.length;}
 const definitions=new Map<string,ASTNode>();
 const statements=parts.flatMap(text=>split(tokenize(text)).map(s=>({id:s.id,text,ast:parseExpression(s.tokens)})));
 for(const s of statements)definitions.set(s.id,s.ast);
 const resolve=(ast:ASTNode|undefined,seen=new Set<string>()):unknown=>{
  if(!ast)throw new Error('Missing value');
  switch(ast.k){
   case 'Str':case 'Num':case 'Bool':return ast.v;
   case 'Null':return null;
   case 'Obj':return Object.fromEntries(ast.entries.map(([key,value])=>[key,resolve(value,seen)]));
   case 'Arr':return ast.els.map(value=>resolve(value,seen));
   case 'Ref':case 'StateRef':{
    if(seen.has(ast.n))throw new Error('Unresolved or circular dependency');
    return resolve(definitions.get(ast.n),new Set([...seen,ast.n]));
   }
   default:throw new Error('Defer computed or Query-dependent arguments');
  }
 };
 // Walk every AST property, including ternary/binary/call nodes. Unknown
 // expression shapes must not hide effectful nodes or model-authored prose.
 const containsTool=(value:unknown):boolean=>{
  if(!value||typeof value!=='object')return false;
  if(Array.isArray(value))return value.some(containsTool);
  const node=value as Record<string,unknown>;
  if(node.k==='Comp'&&['Query','Mutation','Action','Button','Select','Text','Heading'].includes(String(node.name)))return true;
  return Object.values(node).some(containsTool);
 };

 const duplicateDefinitions=new Set(statements.map(s=>s.id)).size!==statements.length;
 const safe=statements.map(s=>{
  if(s.ast.k!=='Comp'||!['Query','Mutation'].includes(s.ast.name))return containsTool(s.ast)?`${s.id}=null`:s.text;
  if(s.ast.name==='Mutation')return `${s.id}=null`;
  try{
   if(duplicateDefinitions)throw new Error('Duplicate definition');
   if(s.ast.args.length<2||s.ast.args.length>4)throw new Error('Invalid Query arity');
   let argumentNode=s.ast.args[1];const argumentSeen=new Set<string>();
   while(argumentNode?.k==='Ref'){if(argumentSeen.has(argumentNode.n))throw new Error('Circular arguments');argumentSeen.add(argumentNode.n);argumentNode=definitions.get(argumentNode.n)!;}
   if(argumentNode?.k!=='Obj'||new Set(argumentNode.entries.map(([key])=>key)).size!==argumentNode.entries.length)throw new Error('Invalid argument object');
   const hasState=(node:ASTNode,seen=new Set<string>()):boolean=>node.k==='StateRef'||(node.k==='Ref'?(seen.has(node.n)||!definitions.has(node.n)||hasState(definitions.get(node.n)!,new Set([...seen,node.n]))):node.k==='Arr'?node.els.some(v=>hasState(v,seen)):node.k==='Obj'?node.entries.some(([,v])=>hasState(v,seen)):false);
   if(argumentNode.entries.some(([key,node])=>key!=='view'&&hasState(node)))throw new Error('Reactive IDs are forbidden');
   const tool=resolve(s.ast.args[0]),args=resolve(s.ast.args[1]);
   if(!allowedEvidence(tool,args,evidence))throw new Error('Query outside current evidence');
   if(s.ast.args[2]){const fallback=resolve(s.ast.args[2]);if(fallback!==null&&(typeof fallback!=='object'||Array.isArray(fallback)||Object.keys(fallback).length))throw new Error('Unsafe fallback');}
   // Repeating timers and Query-dependent values remain under the final runtime.
   if(s.ast.args[3]&&resolve(s.ast.args[3])!==0)throw new Error('Defer refresh timer');
   return s.text;
  }catch{return `${s.id}=null`;}
 });
 const tail=malformed?'':clean.slice(start).trim();let presentationTail='';let tailId='';
 if(tail){
  try{
   const raw=split(tokenize(autoClose(tail).text));const tailAst=raw.length===1?parseExpression(raw[0].tokens):undefined;
   // Never auto-complete a tool/state statement. Ordinary text/layout may use
   // the SDK's native partial parser, preserving token-by-token presentation.
   if(tailAst?.k==='Comp'&&tailAst.name==='Col'&&raw.length===1&&!raw[0].id.startsWith('$')&&!containsTool(tailAst)&&!statements.some(s=>s.id===raw[0].id)){presentationTail=tail;tailId=raw[0].id;}
  }catch{/* Keep the last complete presentation snapshot. */}
 }
 // Preserve root child slots in the transient snapshot. SDK 1.0 drops
 // unresolved refs from arrays; otherwise a later earlier sibling shifts all
 // following index keys and remounts already visible content. Empty Text nodes
 // carry each original statement ID, add no facts, and never enter saved source.
 const missing=new Set<string>();
 const reserveSlots=(node:ASTNode|undefined,seen=new Set<string>())=>{
  if(node?.k==='Ref'){
   if(seen.has(node.n))return;
   if(!definitions.has(node.n)){if(node.n!==tailId)missing.add(node.n);return;}
   reserveSlots(definitions.get(node.n),new Set([...seen,node.n]));
  }else if(node?.k==='Comp'&&node.name==='Col'){
   const children=node.args[0];if(children?.k==='Arr')children.els.forEach(child=>reserveSlots(child,seen));
  }
 };
 reserveSlots(definitions.get('root'));
 for(const id of missing)safe.push(`${id}=Text("")`);
 if(presentationTail)safe.push(presentationTail);
 return safe.join('\n');
}
