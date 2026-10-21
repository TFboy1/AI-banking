<script setup lang="ts">
import type {BillTransaction} from '../types';
import {money} from '../types';
defineProps<{transactions:BillTransaction[]}>();
const date=(value:string)=>new Date(value).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false});
</script>
<template><div class="bill-evidence"><article v-for="transaction in transactions" :key="transaction.id"><strong>{{transaction.counterparty}} · {{transaction.direction==='out'?'支出':'收入'}} ¥{{money(transaction.amount_minor)}}</strong><span>{{date(transaction.occurred_at)}} · {{transaction.category}} · 北京时间</span><p v-if="transaction.description">{{transaction.description}}</p><small>流水编号 {{transaction.id}}</small></article></div></template>
<style scoped>.bill-evidence{margin:8px 0;min-width:0}.bill-evidence article{border-top:1px solid #dfdfd3;padding:10px 0;display:grid;gap:5px;overflow-wrap:anywhere}.bill-evidence strong{font-size:12px;font-weight:600}.bill-evidence span,.bill-evidence p,.bill-evidence small{font-size:11px;line-height:1.7;margin:0}.bill-evidence small{color:#8b8e7d;word-break:break-all}</style>
