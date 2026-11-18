import {runGuardedAction} from './provider-readiness.mjs';
import {StrategyView,isStrategyRuns} from './StrategyView';
import {createLibrary,defineComponent,reactive,action,useStateField,useTriggerAction,useIsStreaming} from '@openuidev/react-lang';
import {useState,useContext,useEffect,useRef} from 'react';
import {observeCardPaint} from './card-diagnostic.mjs';
import {UIInteraction} from './interaction';
import {ResearchView} from './ResearchView';
import {HistoryView} from './HistoryView';
import {IntradayView,FinancialView} from './FinancialViews';
import {z} from 'zod';
import {trust,isTrusted,trustedPayload,safeNewsLink,watchlistLines,domainNames} from './safety.mjs';
export {trust};
const Col=defineComponent({name:'Col',description:'Vertical group',props:z.object({children:z.array(z.unknown())}),component:({props,renderNode})=><div className="xcui-col">{props.children.map((c,i)=><div key={i}>{renderNode(c)}</div>)}</div>});
const Text=defineComponent({name:'Text',description:'Explanation',props:z.object({text:z.string()}),component:({props})=><p>{props.text}</p>});
const Heading=defineComponent({name:'Heading',description:'Section heading',props:z.object({text:z.string()}),component:({props})=><h3>{props.text}</h3>});
function Data({value,depth=0}:{value:unknown;depth?:number}){if(value==null)return <span>暂无</span>;if(typeof value!=='object')return <span>{String(value)}</span>;if(depth>4)return <span>展开层级已达上限</span>;if(Array.isArray(value))return <ol>{value.slice(0,100).map((v,i)=><li key={i}><Data value={v} depth={depth+1}/></li>)}{value.length>100&&<li>仅展示前 100 条</li>}</ol>;return <dl>{Object.entries(value).slice(0,80).map(([k,v])=><div key={k}><dt>{k}</dt><dd><Data value={v} depth={depth+1}/></dd></div>)}</dl>}
const EvidenceCard=defineComponent({name:'EvidenceCard',description:'Real business evidence. Bind entire Query result, never literal data.',props:z.object({title:z.string(),data:z.unknown().optional()}),component:({props})=><section className="xcui-card"><h4>{props.title.replace(/(?:证据)?快照$/,'').replace(/^分析依据$/,'相关数据')||'相关数据'}</h4>{props.data==null?<p role="status">正在读取…</p>:typeof props.data!=='object'||!isTrusted(props.data)?<p role="alert">数据来源未通过验证</p>:<BusinessView kind="EvidenceCard" data={props.data}/>}</section>});
const Select=defineComponent({name:'Select',description:'Independent reactive selection',props:z.object({value:reactive(z.string()),options:z.array(z.string())}),component:({props})=>{const f=useStateField('selection',props.value);const interaction=useContext(UIInteraction);return <select aria-label="数据选项" value={f.value??''} onChange={e=>{interaction.change();f.setValue(e.target.value)}}>{props.options.map(o=><option key={o}>{o}</option>)}</select>}});
const Button=defineComponent({name:'Button',description:'Explicit user action',props:z.object({label:z.string(),action:action(),disabled:z.boolean().optional()}),component:({props})=>{const run=useTriggerAction();const interaction=useContext(UIInteraction);const streaming=useIsStreaming();const[busy,setBusy]=useState(false);return <button type="button" disabled={busy||streaming||props.disabled||interaction.completed(props.label)} onClick={async()=>{if(busy)return;await runGuardedAction(()=>interaction.begin(props.label),()=>run(props.label,undefined,props.action),setBusy)}}>{interaction.completed(props.label)?'已完成':busy?'处理中…':props.label}</button>}});
const MutationStatus=defineComponent({name:'MutationStatus',description:'Mutation result status',props:z.object({status:z.string().nullish(),error:z.unknown().optional()}),component:({props})=><p role="status">{props.status==='error'?`操作失败：${String(props.error??'请重试')}`:props.status==='success'?'操作成功':props.status==='loading'?'正在提交…':''}</p>});
export function BusinessView({kind,data}:{kind:string;data:unknown}) {
 const ref=useRef<HTMLDivElement>(null);
 useEffect(()=>{if(!isTrusted(data)||!ref.current)return;return observeCardPaint(ref.current,kind,window)},[kind,data]);
 return <div ref={ref}><BusinessContent kind={kind} data={data}/></div>;
}
function BusinessContent({kind,data}:{kind:string;data:unknown}) {
 if(data==null)return <p role="status">正在读取…</p>;
 if(typeof data!=='object'||!isTrusted(data))return <p role="alert">数据来源未通过验证</p>;
 data=trustedPayload(data);
 if(!data||typeof data!=='object')return <p role="alert">业务数据格式错误</p>;
 if(kind==='StrategyScanCard'&&isStrategyRuns(data))return <StrategyView runs={data}/>;
 if(Array.isArray((data as any).bars)&&(data as any).bars.every((r:any)=>typeof r.date==='string')){const d=data as any;return <><p>{d.name} · {d.code} · {d.asset_type==='convertible_bond'?'可转债':d.asset_type==='global_index'?'海外指数':'价格走势'}</p>{d.listing_date&&<p>上市日期：{d.listing_date}（供应商目录，未逐笔核验官方公告）</p>}<HistoryView assetType={d.asset_type} content={['周期: daily','数据源: '+d.source,'数据截至: '+d.data_as_of,'获取时间: '+d.fetched_at,'价格口径: '+(d.price_unit??d.price_basis??'指数点位 / 供应商日线'),'说明: '+d.notice,...d.bars.map((r:any)=>[r.date,r.open,r.close,r.high,r.low,r.volume].join(' '))].join('\n')}/></>};
 if(Array.isArray((data as any).bars))return <IntradayView data={data}/>;
 if(Array.isArray((data as any).reports))return <FinancialView data={data}/>;
 if(kind==='FundFlowCard'||'classification' in (data as object)&&Array.isArray((data as any).rows))return <ResearchView data={data}/>;
 if('revision_history_available' in (data as object)||'pe_ttm_percentile' in (data as object)||'nav_unit' in (data as object))return <ResearchView data={data}/>;
 const content=(data as {content?:unknown}).content;
 if(typeof content!=='string')return <Data value={data}/>;
 const lines=content.split('\n');
 if(kind==='HistoryChart')return <HistoryView content={content}/>;
 if(kind==='NewsList')return <ul>{lines.map((line,i)=>{const match=safeNewsLink(line);return <li key={i}>{match?<a href={match} target="_blank" rel="noopener noreferrer">阅读来源</a>:line}</li>})}</ul>;
 if(kind==='Watchlist'||kind==='Portfolio')return <ul>{watchlistLines(content).map((line:string,i:number)=><li key={i}>{line}</li>)}</ul>;
 return <dl className={kind==='QuoteCard'?'xcui-quote':''}>{lines.filter(Boolean).map((line,i)=>{const m=line.match(/^\s*([^:：]+)[:：]\s*(.*)$/);return m?<div key={i}><dt>{m[1]}</dt><dd className={m[1]==='当前价'?'xcui-price':m[1]==='涨跌幅'?(m[2].startsWith('-')?'xcui-down':'xcui-up'):''}>{m[2]}</dd></div>:<p key={i}>{line}</p>})}</dl>;
}
const domainCards=domainNames.map(name=>defineComponent({name,description:`${name}: render real Query output only; preserves raw service data.`,props:z.object({title:z.string(),data:z.unknown().optional()}),component:({props})=><section className="xcui-card"><h4>{props.title.replace(/(?:证据)?快照$/,'').replace(/^分析依据$/,'相关数据')||'相关数据'}</h4><BusinessView kind={name} data={props.data}/></section>}));
export const library=createLibrary({id:'xclaw-business@1',root:'Col',components:[Col,Text,Heading,EvidenceCard,Select,Button,MutationStatus,...domainCards]});
