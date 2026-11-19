const trusted = new WeakSet();
const arrayPayloads = new WeakMap();
export function trust(value){if(Array.isArray(value)){const envelope=Object.freeze({kind:'xclaw-query-array'});arrayPayloads.set(envelope,value);trusted.add(envelope);return envelope}if(value&&typeof value==='object')trusted.add(value);return value}
export function trustedPayload(value){if(!isTrusted(value))throw Error('Untrusted business data');return arrayPayloads.get(value)??value}
export function isTrusted(value){return !!value&&typeof value==='object'&&trusted.has(value)}
export async function stateScope(token){const b=await crypto.subtle.digest('SHA-256',new TextEncoder().encode(token));return Array.from(new Uint8Array(b),v=>v.toString(16).padStart(2,'0')).join('')}
export function historyRows(content){const rows=content.split('\n').filter(l=>/^\d{4}-\d{2}-\d{2}\s/.test(l)).map(l=>l.trim().split(/\s+/));const closes=rows.map(r=>Number(r[2]));return {rows,closes,valid:rows.length>1&&closes.every(Number.isFinite)}}
export function safeNewsLink(line){const m=line.match(/^\s*链接:\s*(https?:\/\/\S+)$/);return m?.[1]??null}
export function watchlistLines(content){return content.split('\n').filter(line=>line.trim()&&!/^===.*===$/.test(line.trim())).map(line=>line.replace(/^•\s*/,''))}
export function assertKnownAttempt(attempt){if(attempt?.unknown)throw Error('outcome_unknown：上次提交结果未知，请先查询业务状态，勿重复提交')}
export const domainNames=['FundFlowCard','QuoteCard','HistoryChart','IndicatorsCard','FundamentalsCard','NewsList','Watchlist','Portfolio','BacktestCard','StrategyScanCard','ReportCard','TaskList'];
