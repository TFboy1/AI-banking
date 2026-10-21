<script setup lang="ts">
import {computed,ref} from 'vue';
import AnalysisChart from './AnalysisChart.vue';
import type {BillReport} from '../types';
import {money} from '../types';
const emit=defineEmits<{suggest:[text:string]}>();
const props=defineProps<{report:BillReport}>();
const followupOpen=ref(false);
const maximum=computed(()=>Math.max(1,...props.report.trend.flatMap(m=>[m.income_minor,m.expense_minor])));
const width=(amount:number)=>`${amount ? Math.max(1,amount/maximum.value*100) : 0}%`;
</script>
<template>
  <div class="bill-report">
    <section v-if="report.trend.length" aria-label="年度月度收支趋势">
      <div class="bill-heading"><h4>每月收支</h4><span><i class="income-dot"></i>收入 <i class="expense-dot"></i>支出</span></div>
      <div class="bill-trend" role="img" aria-label="全年12个月收支对比，具体金额可在下方每月明细中查看">
        <div v-for="month in report.trend" :key="month.month" class="bill-trend-row" :title="`${month.month} 收入¥${money(month.income_minor)}，支出¥${money(month.expense_minor)}`">
          <span>{{month.month.slice(5)}}月</span><div class="bill-bars"><i class="income-bar" :style="{width:width(month.income_minor)}"></i><i class="expense-bar" :style="{width:width(month.expense_minor)}"></i></div>
          <small v-if="month.status!=='recorded'">{{month.status==='future'?'未到月份':'无流水记录'}}</small><small v-else>支出 ¥{{money(month.expense_minor)}}</small>
        </div>
      </div>
      <details class="bill-month-details"><summary>查看每月明细</summary><div class="table-scroll" tabindex="0" aria-label="年度每月收支明细"><table><thead><tr><th>月份</th><th>收入</th><th>支出</th><th>净收支</th><th>数据</th></tr></thead><tbody><tr v-for="month in report.trend" :key="month.month"><td>{{month.month}}</td><td>¥{{money(month.income_minor)}}</td><td>¥{{money(month.expense_minor)}}</td><td>¥{{money(month.net_minor)}}</td><td>{{month.status==='future'?'未到月份':month.record_count+'笔记录'}}</td></tr></tbody></table></div></details>
    </section>
    <AnalysisChart v-for="chart in report.charts||[]" :key="chart.title" :chart="chart"/>
    <button class="analysis-followup" :aria-expanded="followupOpen" @click="followupOpen=!followupOpen">继续分析</button>
    <div v-if="followupOpen" class="analysis-questions">
      <button @click="emit('suggest','沿用刚才这份账单的时间范围，分析各类别支出金额与占比，看看钱主要花在哪里。')">钱主要花在哪里？</button>
      <button @click="emit('suggest','沿用刚才这份账单的时间范围，按周分析消费变化。')">每周花钱有什么变化？</button>
      <button @click="emit('suggest','请把刚才这份账单与上一个同等时长区间比较，看看哪些类别支出变化最大；若对比日期有歧义，请先和我确认。')">哪些支出变化最大？</button>
    </div>
    <section v-if="report.top_merchants.length" class="bill-merchants" aria-label="主要支出对象">
      <h4>主要支出对象</h4><article v-for="merchant in report.top_merchants" :key="merchant.name"><div><strong>{{merchant.name}}</strong><small>{{merchant.count}}笔支出</small></div><b>¥{{money(merchant.amount_minor)}}</b></article>
    </section>
    <section class="bill-coverage" aria-label="异常识别范围"><h4>异常核对 · {{report.anomaly_count}}条线索</h4><p v-if="!report.record_count">所选期间没有可用流水，无法据此判断实际收支或交易是否异常。</p><p>{{report.historical_coverage}}</p><p>规则：单笔支出达到3000元；同月同订阅同金额重复扣费；同对象同金额10分钟内重复支出；10分钟内至少3笔对外转账；历史比较需至少5笔此前同类消费。</p><p>历史比较使用此前90天最近20笔，金额至少200元且达到中位数3倍时提示。异常线索可能对应正常交易，请先核对。</p></section>
  </div>
</template>
<style scoped>
.bill-report{margin:20px 0;color:#435746;font-size:12px}.bill-report section+section{margin-top:22px}.bill-report h4{font-size:13px;margin:0 0 12px;color:#304b39}.bill-heading{display:flex;align-items:center;justify-content:space-between;gap:12px}.bill-heading span{font-size:10px;white-space:nowrap}.bill-heading i{display:inline-block;width:7px;height:7px;border-radius:3px;margin:0 3px 0 8px}.income-dot,.income-bar{background:#5b8b70}.expense-dot,.expense-bar{background:#c69b60}.bill-trend{display:grid;gap:11px}.bill-trend-row{display:grid;grid-template-columns:32px minmax(0,1fr) 105px;gap:10px;align-items:center;font-size:11px}.bill-trend-row small{text-align:right;color:#6a7867;font-size:10px}.bill-bars{display:grid;gap:4px;min-width:0;background:#f1f3ed;border-radius:4px}.bill-bars i{height:6px;border-radius:3px}.bill-month-details{margin-top:16px}.bill-report summary{cursor:pointer;line-height:2}.bill-merchants article{display:flex;justify-content:space-between;gap:12px;border-bottom:1px solid #e9ece4;padding:11px 0}.bill-merchants article>div{min-width:0;overflow-wrap:anywhere}.bill-merchants strong{font-weight:500}.bill-merchants small{display:block;color:#83917e;font-size:10px;margin-top:5px}.bill-merchants b{white-space:nowrap;font-weight:600}.bill-coverage{padding:15px;background:#f5f5ef;border-radius:10px;line-height:1.9}.bill-coverage p{margin:8px 0;font-size:11px}@media(max-width:600px){.bill-trend-row{grid-template-columns:28px minmax(0,1fr) 95px;gap:7px}.bill-coverage{padding:12px}.bill-coverage p{font-size:12px}}
</style>
<style scoped>
.bill-month-details table{min-width:480px}
.bill-month-details th,.bill-month-details td{white-space:nowrap;word-break:normal;overflow-wrap:normal;font-variant-numeric:tabular-nums}
</style>

<style scoped>.analysis-followup{font:inherit;background:#f5f7ef;color:#4c7255;border:1px solid #dce6d5;border-radius:8px;padding:10px 14px;cursor:pointer;margin:4px 0 18px;min-height:44px}</style>

<style scoped>.analysis-questions{display:flex;flex-wrap:wrap;gap:8px;margin:0 0 18px}.analysis-questions button{font:inherit;font-size:11px;border:1px solid #e1e7db;background:#fafbf6;color:#577252;border-radius:8px;cursor:pointer;padding:10px 12px;min-height:44px;text-align:left}</style>
