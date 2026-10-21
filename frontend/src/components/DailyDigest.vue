<script setup lang="ts">
import type {ToolResult} from '../types';
import {computed} from 'vue';
import AnalysisChart from './AnalysisChart.vue';
import {dailyChart} from '../dailyCharts';
const props=defineProps<{report:NonNullable<ToolResult['daily_digest']>}>();
const sections=computed(()=>props.report.sections.map(section=>({...section,chart:dailyChart(section)})));
</script>
<template>
 <section class="result-card daily-digest" aria-label="每日早报">
  <header><span class="small-eyebrow">每日早报</span><h3>{{report.greeting}}</h3><p>{{report.subtitle}}</p></header>
  <section v-for="(section,i) in sections" :key="i" class="digest-section">
   <h4>{{section.title}}</h4>
   <div v-if="section.metrics.length" class="digest-metrics"><div v-for="metric in section.metrics" :key="metric.label"><span>{{metric.label}}</span><strong>{{metric.value}}</strong></div></div>
   <AnalysisChart v-if="section.chart" :chart="section.chart"/>
   <component :is="section.chart?'details':'div'" v-if="section.rows.length" class="digest-details">
    <summary v-if="section.chart">查看{{section.title==='账户余额'?'账户':'支出'}}明细</summary>
    <div class="table-scroll" tabindex="0" :aria-label="section.title"><table><thead><tr><th v-for="column in section.columns" :key="column">{{column}}</th></tr></thead><tbody><tr v-for="(row,j) in section.rows" :key="j"><td v-for="(value,k) in row" :key="k">{{value}}</td></tr></tbody></table></div>
   </component>
   <p v-if="section.note" class="digest-note">{{section.note}}</p>
  </section>
 </section>
</template>
<style scoped>
.daily-digest{overflow:hidden}.daily-digest header h3{margin:12px 0;font-size:19px;line-height:1.6}.daily-digest header p,.digest-note{font-size:11px;line-height:1.9;color:#829078;overflow-wrap:anywhere}.daily-digest header p{margin:0}.digest-section{padding-top:22px;margin-top:22px;border-top:1px solid #e4e9dd;min-width:0}.digest-section h4{margin:0 0 14px;color:#3a5c44;font-size:13px;font-weight:600}.digest-metrics{display:flex;flex-wrap:wrap;gap:18px;margin-bottom:14px}.digest-metrics>div{min-width:100px;max-width:100%;overflow-wrap:anywhere}.digest-metrics span{display:block;font-size:11px;color:#89967e;margin-bottom:6px}.digest-metrics strong{font-size:21px;font-weight:500;color:#345b43}.daily-digest table{min-width:100%;font-size:11px}.daily-digest th,.daily-digest td{white-space:nowrap}.digest-note{margin:10px 0 0}@media(max-width:600px){.daily-digest header h3{font-size:17px}.digest-section{padding-top:18px;margin-top:18px}.digest-metrics strong{font-size:19px}.daily-digest header p,.digest-note{font-size:12px}}
</style>
<style scoped>.digest-details summary{font-size:11px;line-height:2;color:#71866b;cursor:pointer;min-height:36px}.digest-details[open] summary{margin-bottom:8px}@media(max-width:600px){.digest-details summary{min-height:44px;font-size:12px}}</style>
