<script setup lang="ts">
import {computed} from 'vue';
import type {AnalysisChartSpec} from '../types';
import {money} from '../types';
const props=defineProps<{chart:AnalysisChartSpec}>();
const colors=['#346b57','#c49962','#6b899a','#8a8b5c','#a67c83','#91a68c','#b29f83'];
const color=(i:number)=>colors[i%colors.length];
const all=computed(()=>props.chart.series.flatMap(s=>s.values));
const low=computed(()=>Math.min(0,...all.value));
const high=computed(()=>Math.max(1,...all.value));
const y=(v:number)=>150-(v-low.value)/(high.value-low.value)*125;
const x=(i:number)=>props.chart.labels.length<2?175:45+i/(props.chart.labels.length-1)*270;
const total=computed(()=>props.chart.series[0]?.values.reduce((a,b)=>a+b,0)||0);
const segments=computed(()=>{let offset=0;return (props.chart.series[0]?.values||[]).map((v,i)=>{const pct=total.value>0?v/total.value:0;const s={label:props.chart.labels[i],value:v,pct,offset};offset+=pct;return s;});});
const zero=computed(()=>-low.value/(high.value-low.value)*100);
const bar=(value:number)=>({left:Math.min(zero.value,(value-low.value)/(high.value-low.value)*100)+'%',width:Math.abs(value)/(high.value-low.value)*100+'%'});
const compact=(minor:number)=>new Intl.NumberFormat('zh-CN',{notation:'compact',maximumFractionDigits:1}).format(minor/100);
const points=(values:number[])=>values.map((v,i)=>`${x(i)},${y(v)}`).join(' ');
const tick=(i:number)=>i===0||i===props.chart.labels.length-1||i%Math.max(1,Math.ceil(props.chart.labels.length/5))===0;
</script>
<template>
 <figure class="analysis-chart" :aria-label="chart.title"><figcaption><h4>{{chart.title}}</h4><span>人民币 · 元</span></figcaption>
  <p v-if="!chart.labels.length" class="chart-empty">所选范围暂无匹配流水，无法生成图表。</p>
  <template v-else-if="chart.type==='donut' && total>0">
   <div class="donut-layout"><svg viewBox="0 0 200 200" role="img" :aria-label="chart.title+'，具体金额见分类图例'"><circle cx="100" cy="100" r="70" fill="none" stroke="#e9ede4" stroke-width="25"/><circle v-for="(s,i) in segments" :key="s.label" cx="100" cy="100" r="70" fill="none" :stroke="color(i)" stroke-width="25" pathLength="100" :stroke-dasharray="`${s.pct*100} ${100-s.pct*100}`" :stroke-dashoffset="-s.offset*100" transform="rotate(-90 100 100)"><title>{{s.label}} ¥{{money(s.value)}} · {{(s.pct*100).toFixed(1)}}%</title></circle><text x="100" y="91" text-anchor="middle" class="donut-caption">合计</text><text x="100" y="116" text-anchor="middle" class="donut-total"><title>¥{{money(total)}}</title>¥{{money(total).length>12?compact(total):money(total)}}</text></svg>
   <ul><li v-for="(s,i) in segments" :key="s.label"><i :style="{background:color(i)}"></i><span>{{s.label}}<small>{{(s.pct*100).toFixed(1)}}%</small></span><b>¥{{money(s.value)}}</b></li></ul></div>
  </template>
  <template v-else-if="chart.type==='line'">
   <div class="chart-legend"><span v-for="(s,i) in chart.series" :key="s.name"><i :style="{background:color(i)}"></i>{{s.name}}</span></div>
   <svg viewBox="0 0 340 190" role="img" :aria-label="chart.title+'，可展开下方数据明细查看准确金额'">
    <g v-for="level in [low,(low+high)/2,high]" :key="level"><line x1="45" :y1="y(level)" x2="315" :y2="y(level)" stroke="#e2e8dc" stroke-dasharray="3 4"/><text x="40" :y="y(level)+3" text-anchor="end" class="axis-label">{{compact(level)}}</text></g>
    <g v-for="(s,j) in chart.series" :key="s.name"><polyline :points="points(s.values)" fill="none" :stroke="color(j)" stroke-width="2.5" stroke-linejoin="round"/><circle v-for="(v,i) in s.values" :key="i" :cx="x(i)" :cy="y(v)" :r="chart.labels.length>50?1.5:3" :fill="color(j)"><title>{{chart.labels[i]}} · {{s.name}} ¥{{money(v)}}</title></circle></g>
    <template v-for="(label,i) in chart.labels" :key="label"><text v-if="tick(i)" :x="x(i)" y="175" text-anchor="middle" class="axis-label">{{label.length>7?label.slice(5):label}}</text></template>
   </svg>
  </template>
  <template v-else>
   <div class="chart-legend"><span v-for="(s,i) in chart.series" :key="s.name"><i :style="{background:color(i)}"></i>{{s.name}}</span></div>
   <p v-if="low<0" class="chart-empty">竖线为零，左侧为负值，右侧为正值。</p>
   <div class="compare-bars"><div v-for="(label,i) in chart.labels" :key="label" class="compare-group"><strong>{{label}}</strong><div v-for="(s,j) in chart.series" :key="s.name" class="compare-row"><span>{{s.name}}</span><div><em v-if="low<0" class="zero-line" :style="{left:zero+'%'}"></em><i :style="{background:color(j),...bar(s.values[i]||0)}"></i></div><b>¥{{money(s.values[i]||0)}}</b></div></div></div>
  </template>
 </figure>
</template>
<style scoped>
.analysis-chart{margin:18px 0;padding:18px;background:#f7f8f2;border:1px solid #e3e9dc;border-radius:14px;color:#3c5844}.analysis-chart figcaption{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:16px}.analysis-chart h4{font-size:13px;margin:0;font-weight:600}.analysis-chart figcaption>span{font-size:10px;color:#83917b;white-space:nowrap}.analysis-chart svg{display:block;width:100%;max-height:300px}.axis-label{font-size:8px;fill:#84917c}.chart-legend{display:flex;flex-wrap:wrap;gap:14px;font-size:11px;margin-bottom:12px}.chart-legend span{display:flex;gap:6px;align-items:center}.chart-legend i,.donut-layout li>i{width:8px;height:8px;border-radius:50%;flex:none}.donut-layout{display:grid;grid-template-columns:minmax(130px,1fr) minmax(150px,1fr);gap:15px;align-items:center}.donut-caption{font-size:12px;fill:#83917b}.donut-total{font-size:17px;fill:#345840;font-weight:600}.donut-layout ul{padding:0;margin:0;list-style:none}.donut-layout li{display:flex;align-items:center;gap:8px;padding:8px 0;border-bottom:1px solid #e4e9de;font-size:11px}.donut-layout li>span{flex:1;overflow-wrap:anywhere}.donut-layout small{display:block;color:#8b967f;margin-top:3px;font-size:10px}.donut-layout b{font-size:11px;font-weight:500;white-space:nowrap}.compare-group{padding:10px 0}.compare-group>strong{font-size:12px;font-weight:500}.compare-row{display:grid;grid-template-columns:40px minmax(0,1fr) 85px;gap:8px;align-items:center;font-size:10px;margin-top:9px}.compare-row>div{height:9px;background:#e7ecdf;border-radius:5px}.compare-row i{display:block;height:9px;border-radius:5px;min-width:0}.compare-row b{text-align:right;font-weight:500;white-space:nowrap}.chart-empty{font-size:12px;color:#819078;line-height:1.8}@media(max-width:600px){.analysis-chart{padding:13px}.donut-layout{grid-template-columns:1fr}.donut-layout svg{max-height:190px}.donut-layout li{font-size:12px}.compare-row{grid-template-columns:34px minmax(0,1fr) 82px;gap:6px}}
</style>
<style scoped>.compare-row>div{position:relative}.compare-row i{position:absolute;top:0}.zero-line{position:absolute;top:-3px;bottom:-3px;width:1px;background:#71816b;z-index:1}.compare-group>strong{overflow-wrap:anywhere}</style>
