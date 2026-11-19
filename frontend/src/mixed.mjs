// Preserve incomplete fences as text: they must not activate a tool.
export function parseMixed(source) {
 const parts=[]; const re=/^```openui[^\S\n]*\n([\s\S]*?)^```[^\S\n]*(?:\n|$)/gm; let end=0,m;
 while((m=re.exec(source))){if(m.index>end)parts.push({type:'text',source:source.slice(end,m.index)});parts.push({type:'openui',source:m[1]});end=re.lastIndex;}
 if(end<source.length)parts.push({type:'text',source:source.slice(end)});return parts;
}
