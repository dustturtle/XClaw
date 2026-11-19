import {useState} from 'react';
import {historyRows} from './safety.mjs';

export function selectHistoryRange<T>(rows:T[],count:number){return count>0?rows.slice(-count):rows;}

/** Read-only evidence chart. Dates and values come only from validated tool rows. */
export function HistoryView({content,assetType}:{content:string;assetType?:string}) {
  const asset=assetType??content.match(/(?:^|[|\n])\s*资产类型[:：]\s*(\w+)/)?.[1];
  const axisLabel=asset==='index'||asset==='global_index'?'收盘点位（点）':asset==='stock'?'收盘价（元）':'收盘值';
  const parsed=historyRows(content);
  const period=content.match(/周期[:：]\s*(daily|weekly|monthly)\b/)?.[1];
  const unit=period==='monthly'?'月度采样':period==='weekly'?'周度采样':period==='daily'?'交易日':'行情记录';
  const periodNote=period==='monthly'?'月线行情，未结束月份数据仍会变化。':period==='weekly'?'周线行情，未结束交易周数据仍会变化。':period==='daily'?'日线行情，未收盘日数据仍会变化。':'历史行情，周期以来源说明为准。';
  const [range,setRange]=useState(0);
  const rows=selectHistoryRange<string[]>(parsed.rows,range);
  const closes=selectHistoryRange<number>(parsed.closes,range);
  const valid=parsed.valid;
  const [active,setActive]=useState<number|null>(null);
  const metadata=content.split('\n').filter(line=>/^(数据源|数据截至|获取时间)[:：]/.test(line));
  const min=Math.min(...closes), max=Math.max(...closes);
  const pad=max===min?Math.max(Math.abs(min)*.01,1):(max-min)*.08;
  const low=min-pad, high=max+pad;
  const x=(i:number)=>70+i/(closes.length-1)*620;
  const y=(v:number)=>180-(v-low)/(high-low)*145;
  const ticks=[high,(high+low)/2,low];
  const dateIndices=[...new Set([0,Math.floor((rows.length-1)/2),rows.length-1])];
  const summary=valid?`${rows[0][0]} 至 ${rows.at(-1)![0]}，${rows.length} 个${unit}；收盘价 ${closes[0].toFixed(2)} 至 ${closes.at(-1)!.toFixed(2)}`:'';
  return <div className="xcui-history">
    {valid?<>
      <p className="xcui-chart-summary">{summary}</p>
      {parsed.rows.length>3&&<label className="xcui-chart-picker">显示区间<select aria-label="历史行情显示区间" value={range} onChange={e=>{setRange(Number(e.target.value));setActive(null)}}><option value={0}>全部已加载区间</option>{[3,5,20].filter(n=>n<parsed.rows.length).map(n=><option key={n} value={n}>最近 {n} 个{unit}</option>)}</select><small>仅筛选本次已加载区间，不重新取数。</small></label>}
      <div className="xcui-chart-scroll" tabIndex={0} role="region" aria-label="收盘折线图，可横向滚动"><svg viewBox="0 0 720 230" role="img" aria-label={`历史收盘折线（非蜡烛 K 线）：${summary}`}>
        <title>历史收盘折线（非蜡烛 K 线）</title>
        <desc>横轴为交易日期，纵轴为收盘价。点按、鼠标停留或键盘聚焦数据点查看日期与价格；完整数据可在下方展开。</desc>
        <text x="70" y="16" className="xcui-axis-label">{axisLabel}</text>
        {ticks.map((v,i)=><g key={i}><line x1="70" x2="690" y1={y(v)} y2={y(v)} className="xcui-grid"/><text x="61" y={y(v)+4} textAnchor="end" className="xcui-axis-label">{v.toFixed(2)}</text></g>)}
        <polyline fill="none" stroke="#2166ab" strokeWidth="2" points={closes.map((v:number,i:number)=>`${x(i)},${y(v)}`).join(' ')}/>
        {closes.map((v:number,i:number)=><circle key={rows[i][0]} cx={x(i)} cy={y(v)} r="10" tabIndex={0} onClick={()=>setActive(i)} onKeyDown={e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();setActive(i)}}} onMouseEnter={()=>setActive(i)} onFocus={()=>setActive(i)} onBlur={()=>setActive(null)} onMouseLeave={()=>setActive(null)} className="xcui-chart-point" aria-label={`${rows[i][0]} 收盘 ${v.toFixed(2)}`}><title>{`${rows[i][0]} 收盘 ${v.toFixed(2)}`}</title></circle>)}
        {dateIndices.map(i=><text key={i} x={x(i)} y="205" textAnchor={i===0?'start':i===rows.length-1?'end':'middle'} className="xcui-axis-label">{rows[i][0]}</text>)}
      </svg></div>
      <label className="xcui-chart-picker">选择{unit}读值<input type="range" min="0" max={rows.length-1} value={active!==null&&active<rows.length?active:0} onChange={e=>setActive(Number(e.target.value))} aria-label={`选择${unit}`} aria-valuetext={`${rows[active!==null&&active<rows.length?active:0][0]} 收盘 ${closes[active!==null&&active<rows.length?active:0].toFixed(2)}`}/></label><p className="xcui-chart-readout" role="status">{active!==null&&rows[active]?`${rows[active][0]} · 收盘 ${closes[active].toFixed(2)}`:"点按图中数据点，或拖动滑块查看收盘价"}</p>
      <p className="xcui-chart-note">{periodNote}复权口径以数据源为准。</p>
    </>:<p role="status">暂无历史行情。</p>}
    {metadata.length>0&&<div className="xcui-provenance">{metadata.map(line=><div key={line}>{line}</div>)}</div>}
    <details className="xcui-raw-data"><summary>{valid?`行情明细（${rows.length} 条）`:'数据说明'}</summary><pre>{content}</pre></details>
  </div>;
}
