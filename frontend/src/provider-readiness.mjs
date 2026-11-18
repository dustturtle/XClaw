export function createWriteGate(){let ready=false;return {get ready(){return ready},confirm(){ready=true},reset(){ready=false},assert(){if(!ready)throw Error('操作状态尚未恢复，暂不能提交')}}}
export function cachedRead(cache,key,fetcher){const old=cache.get(key);if(old)return old;const pending=fetcher().catch(e=>{cache.delete(key);throw e});cache.set(key,pending);return pending}
export function continueWhenReady(gate,streaming,event,callback){if(streaming||!gate.ready||event.type!=='continue_conversation')return false;callback?.(event.humanFriendlyMessage??'继续');return true}
export async function runGuardedAction(begin,run,setBusy){setBusy(true);let finish;try{finish=begin();await run()}finally{finish?.();setBusy(false)}}
export function createRecoveryRefresh(){let pending=false;return {mark(){pending=true},consume(streaming){if(streaming||!pending)return false;pending=false;return true}}}
