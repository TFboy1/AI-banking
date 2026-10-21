import type {Message} from './types';

// Render legacy multi-message reports as one card without rewriting saved history.
export function compactDailyReports(messages:Message[]):Message[]{
 const output:Message[]=[];
 for(let i=0;i<messages.length;i++){
  const first=messages[i];
  if(!first.data.daily_report||first.role!=='assistant'||first.data.result?.daily_digest){output.push(first);continue;}
  const batch:Message[]=[first];
  while(i+1<messages.length&&messages[i+1].data.daily_report&&messages[i+1].role==='assistant'&&!messages[i+1].data.result?.daily_digest)batch.push(messages[++i]);
  const sections=batch.flatMap(m=>m.data.result?.display?[m.data.result.display]:[]);
  if(!sections.length){output.push(...batch);continue;}
  const lines=(batch.find(m=>m.kind==='text')?.text||'每日早报').split('\n');
  output.push({...first,kind:'data',data:{...first.data,result:{daily_digest:{greeting:lines[0],subtitle:lines.slice(1).join('\n'),sections}}}});
 }
 return output;
}
