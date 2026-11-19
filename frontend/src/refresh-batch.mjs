// Explicit refresh generations include queries only, never mutations.
export function createRefreshBatch(publish, schedule = callback => setTimeout(callback, 0)) {
 let generation=0, active=false, pending=new Set(), failed=false;
 return {
  begin(){generation++;active=true;pending=new Set();failed=false;publish('正在刷新…');},
  track(){const ticket={generation,tracked:active};if(active)pending.add(ticket);return ticket;},
  settle(ticket,error=false){
   if(ticket.generation!==generation)return true;
   if(!ticket.tracked)return false;
   if(!pending.delete(ticket))return true;
   failed ||= error;
   if(error)publish('部分数据读取失败，请重试刷新。');
   if(!pending.size){const completing=generation;schedule(()=>{if(generation!==completing||!active||pending.size)return;active=false;publish(failed?'部分数据读取失败，请重试刷新。':'已更新至最新数据。');});}
   return true;
  },
  cancel(){generation++;active=false;pending.clear();}
 };
}
