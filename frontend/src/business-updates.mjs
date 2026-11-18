// Retain only invalidation metadata so late-mounted cards cannot miss recovery.
const updates=new Map();
export function publishBusinessUpdate(detail){
 if(detail.presentationId){const key=JSON.stringify([detail.scope,detail.chat,detail.presentationId,detail.tool]);updates.set(key,detail);if(updates.size>1000)updates.delete(updates.keys().next().value)}
 window.dispatchEvent(new CustomEvent('xclaw-business-updated',{detail}));
}
export function matchingBusinessUpdates(scope,chat,presentationId,names){return [...updates.values()].filter(d=>d.scope===scope&&d.chat===chat&&d.presentationId===presentationId&&['watchlist_manage','portfolio_manage'].includes(d.tool)&&names.includes(d.tool));}
