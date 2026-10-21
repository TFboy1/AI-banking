<script setup lang="ts">
import type {CustomBillAnalysis} from '../types';
import {money} from '../types';
import AnalysisChart from './AnalysisChart.vue';
defineProps<{analysis:CustomBillAnalysis}>();
</script>
<template><div class="custom-analysis">
 <div class="analysis-status"><span>账单自定义分析</span><b>✓ 金额已核对</b></div>
 <div class="analysis-metrics"><div v-for="metric in analysis.metrics" :key="metric.label"><span>{{metric.label}}</span><strong>{{metric.value}}</strong></div></div>
 <p class="analysis-date" v-for="p in analysis.coverage" :key="p.label">{{p.label}}：{{p.start}} 至 {{p.end}} · {{p.count}}笔匹配流水<span v-if="p.partial"> · 尚未结束</span></p>
 <AnalysisChart :chart="{title:analysis.title,type:analysis.chart_type,labels:analysis.labels,series:analysis.series}"/>
 <p v-if="analysis.difference" class="analysis-change">两期差额 ¥{{money(analysis.difference.amount_minor)}}<span v-if="analysis.difference.percent!==null"> · 相对对比期 {{analysis.difference.percent>0?'+':''}}{{analysis.difference.percent}}%</span><span v-else> · 对比期无正金额，不计算增长率</span></p>
 <p class="analysis-note">{{analysis.note}}</p>
 <details><summary>查看数据明细与分析依据</summary><div class="table-scroll"><table><thead><tr><th>分组</th><th v-for="s in analysis.series" :key="s.name">{{s.name}}</th></tr></thead><tbody><tr v-for="row in analysis.rows" :key="row.label"><td>{{row.label}}</td><td v-for="(value,i) in row.values" :key="i">¥{{money(value)}}</td></tr></tbody></table></div><p>截至 {{new Date(analysis.as_of).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false})}} · {{analysis.record_count}}笔账单依据</p><p>AI生成的受限计算已运行，分组和金额均经后台独立核对。用时 {{analysis.sandbox.elapsed_ms}}ms，执行 {{analysis.sandbox.steps}}步；无余额变更。</p><p>计算记录 {{analysis.sandbox.run_id}} · 代码校验 {{analysis.sandbox.code_hash.slice(0,12)}}</p></details>
 </div></template>
<style scoped>
.custom-analysis{margin-top:16px;color:#405941}.analysis-status{display:flex;justify-content:space-between;align-items:center;gap:12px;font-size:10px;color:#89947c;letter-spacing:1px}.analysis-status b{font-size:10px;font-weight:500;color:#447557;letter-spacing:0}.analysis-metrics{display:flex;flex-wrap:wrap;gap:15px;padding:20px 0 14px}.analysis-metrics>div{flex:1;min-width:110px}.analysis-metrics span{display:block;font-size:11px;color:#8c977f;margin-bottom:8px}.analysis-metrics strong{font-weight:500;font-size:24px;color:#345b43;white-space:nowrap}.analysis-date,.analysis-note,.analysis-change{font-size:11px;line-height:1.9;color:#819075;margin:7px 0}.analysis-change{color:#436b4e}.analysis-note{padding:12px;background:#f7f7ef;border-radius:8px}.custom-analysis details{font-size:11px;line-height:1.8;color:#849078;margin-top:14px;overflow-wrap:anywhere}.custom-analysis summary{cursor:pointer;min-height:32px}.custom-analysis table{min-width:280px}.custom-analysis th,.custom-analysis td{white-space:nowrap}.custom-analysis details p{margin:10px 0}@media(max-width:600px){.analysis-metrics strong{font-size:22px}.custom-analysis summary{min-height:44px}.analysis-date,.analysis-note{font-size:12px}}
</style>
