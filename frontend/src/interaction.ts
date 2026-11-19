import {createContext} from 'react';
export const UIInteraction = createContext<{begin:(label:string)=>()=>void;change:()=>void;completed:(label:string)=>boolean}>({begin:()=>()=>{},change:()=>{},completed:()=>false});
