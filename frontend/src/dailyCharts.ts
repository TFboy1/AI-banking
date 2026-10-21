import type {AnalysisChartSpec,Display} from './types';

// Read only the exact formatted amounts already present in the saved report.
// Invalid or missing amounts suppress the chart instead of becoming zero.
function cents(value:string):number|null{
 const match=/^¥(-?)([\d,]+)\.(\d{2})$/.exec(value);
 if(!match)return null;
 const amount=(Number(match[2].replaceAll(',',''))*100+Number(match[3]))*(match[1]?-1:1);
 return Number.isSafeInteger(amount)?amount:null;
}
export function dailyChart(section:Display):AnalysisChartSpec|undefined{
 if(section.title==='账户余额'){
  const values=section.rows.map(row=>cents(row[1]||''));
  if(!values.length||values.some(v=>v===null)||values.every(v=>v===0))return;
  return {title:'账户资金分布',type:values.some(v=>v!==null&&v<0)?'bar':'donut',labels:section.rows.map(row=>row[0]),series:[{name:'账户余额',values:values as number[]}]};
 }
 if(section.title==='昨日支出'){
  const amountIndex=section.columns.indexOf('金额'),categoryIndex=section.columns.indexOf('类别');
  if(amountIndex<0||categoryIndex<0||!section.rows.length)return;
  const groups=new Map<string,number>();
  for(const row of section.rows){
   const value=cents(row[amountIndex]||''),category=row[categoryIndex];
   if(value===null||!category)return;
   const next=(groups.get(category)||0)+value;if(!Number.isSafeInteger(next))return;
   groups.set(category,next);
  }
  const ordered=[...groups].sort((a,b)=>b[1]-a[1]);
  if(ordered.every(([,v])=>v===0))return;
  return {title:'昨日支出分类',type:'bar',labels:ordered.map(([name])=>name),series:[{name:'支出',values:ordered.map(([,value])=>value)}]};
 }
}
