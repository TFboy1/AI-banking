<script setup lang="ts">
import type {PartialCompletion} from '../types';
import {money} from '../types';
defineProps<{report:PartialCompletion;busy:boolean}>();
const emit=defineEmits<{choose:[taskId:string,action:string,version:number,orderId?:string]}>();
</script>
<template><section class="result-card partial-card" aria-label="部分完成后的处理">
 <span class="small-eyebrow">部分操作已完成</span><h3>接下来怎么处理？</h3>
 <p class="partial-reason">{{report.reason}}</p>
 <div class="partial-money"><span>原任务已扣款<strong>¥{{money(report.debit_minor)}}</strong></span><span>原任务已到账<strong>¥{{money(report.credit_minor)}}</strong></span></div>
 <h4>已完成 · 保留实际回执</h4>
 <article v-for="item in report.completed" :key="item.approval_id" class="partial-item">
  <strong>{{item.title}}</strong><p>{{item.settlement}}</p>
  <p v-if="item.order_status==='cancelled'" class="refund-done">订单已退订 · 退款已入账 ¥{{money(item.refunded_minor)}}</p>
  <p v-else-if="item.refund_status==='pending'">退订待确认，当前尚未退款。</p>
  <button v-if="report.decision==='pending'&&item.can_cancel" :disabled="busy" @click="emit('choose',report.task_id,'cancel_order',report.version,item.order_id||undefined)">退订此整单</button>
  <small v-if="item.order_id">{{item.order_id}}</small>
  <small v-else>此项不提供自动撤销；如需处理，请转人工核对。</small>
 </article>
 <div v-if="report.unfinished.length"><h4>未完成</h4><ul><li v-for="(item,i) in report.unfinished" :key="i">{{item.title}}</li></ul></div>
 <p class="partial-note">{{report.note}}</p>
 <div v-if="report.decision==='pending'" class="partial-actions">
  <button :disabled="busy" @click="emit('choose',report.task_id,'keep',report.version)">保留已完成，结束剩余步骤</button>
  <button :disabled="busy||!report.can_replan" @click="emit('choose',report.task_id,'replan',report.version)">重新选择未完成项目</button>
 </div>
 <p v-else class="partial-status">{{report.decision==='kept'?'已保留完成项目，剩余步骤已结束。':'正在重新核对未完成项目；请查看后续方案与确认卡。'}}</p>
 </section></template>
<style scoped>
.partial-card{color:#405941}.partial-reason,.partial-note,.partial-item p,.partial-status{font-size:12px;line-height:1.9;overflow-wrap:anywhere}.partial-reason{color:#967446}.partial-money{display:flex;flex-wrap:wrap;gap:28px;margin:20px 0}.partial-money span{font-size:11px;color:#8a967e}.partial-money strong{display:block;margin-top:6px;font-size:23px;font-weight:500;color:#365d43}.partial-card h4{font-size:12px;margin:20px 0 10px}.partial-item{padding:12px 0;border-bottom:1px solid #e6ebdf}.partial-item>strong{font-size:13px;font-weight:500}.partial-item p{margin:5px 0;color:#71836b}.partial-item small{display:block;font-size:10px;color:#95a08c;overflow-wrap:anywhere;margin:8px 0}.partial-card button{font:inherit;font-size:12px;background:#f5f7ef;color:#456a4c;border:1px solid #dce5d4;border-radius:8px;padding:10px 12px;cursor:pointer;min-height:44px}.partial-card button:disabled{opacity:.5;cursor:default}.partial-actions{display:flex;flex-wrap:wrap;gap:10px;margin-top:16px}.partial-actions button:last-child{background:#315d46;color:white}.partial-note{color:#89967f;margin-top:16px}.partial-card ul{padding-left:18px;font-size:12px;line-height:2}.refund-done,.partial-status{color:#386c4d!important}
</style>
