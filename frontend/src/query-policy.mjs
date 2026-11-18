// Only host-observed user events can opt a query out of its initial evidence snapshot.
export function queryMode({manual=false,changed=false,active=0,dirty=new Set()},name){
 return manual||changed||active>0||dirty.has(name)?'live':'snapshot';
}
