<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import type { Task } from '../types';
const props = defineProps<{ task: Task | null }>();
const expanded = ref(false);
watch(() => props.task?.id, () => { expanded.value = false; });
const nodes = computed(() => props.task?.dag?.nodes || []);
const names: Record<string,string> = {uncertain:'结果待核查',pending:'待执行',running:'正在处理',waiting_confirmation:'等待你确认',succeeded:'已完成',failed:'未完成',blocked:'前置步骤未完成',cancelled:'已停止',needs_input:'等待补充信息'};
const finished = computed(() => nodes.value.filter(n=>n.status==='succeeded').length);
const index = (id: string) => nodes.value.findIndex(n=>n.id===id)+1;
</script>

<template>
  <section v-if="nodes.length" class="task-plan" aria-label="任务执行计划">
    <button class="plan-heading" :aria-expanded="expanded" @click="expanded=!expanded"><span>处理步骤 <small>{{task?.interruption?.interrupted ? '已中断 · ' : ''}}{{finished}} / {{nodes.length}} 已完成</small></span><span>{{expanded?'收起':'展开'}}</span></button>
    <div v-if="expanded" class="plan-body">
      <p class="plan-note">按依赖顺序处理，涉及变更的步骤仍需你确认。计划会随查询结果补充。</p>
      <ol>
        <li v-for="(node,i) in nodes" :key="node.id" :data-status="node.status">
          <span class="step-number">{{i+1}}</span>
          <div><strong>{{node.title}}</strong><small v-if="node.depends_on.length">依赖步骤 {{node.depends_on.map(index).join('、')}}</small><small v-else>独立步骤</small><p v-if="node.error">{{node.error}}</p></div>
          <span class="step-state">{{names[node.status] || node.status}}</span>
        </li>
      </ol>
      <p v-if="task?.error" class="plan-error">{{task.error}}</p>
    </div>
  </section>
</template>

<style scoped>
.task-plan{border:1px solid #e2e6e1;border-radius:14px;background:#fafbf9;margin:12px 0;overflow:hidden;color:#304737}.plan-heading{width:100%;display:flex;justify-content:space-between;border:0;background:transparent;padding:14px 16px;text-align:left;color:inherit;font-size:13px;cursor:pointer}.plan-heading small{margin-left:9px;color:#778477;font-size:11px}.plan-heading>span:last-child{font-size:11px}.plan-body{padding:0 16px 12px;max-height:300px;overflow:auto}.plan-note{font-size:11px;color:#7a847c;margin:0 0 12px;line-height:1.6}ol{padding:0;margin:0;list-style:none}li{display:flex;align-items:flex-start;gap:10px;padding:9px 0;border-top:1px solid #edf0eb}.step-number{border-radius:50%;width:22px;height:22px;display:grid;place-items:center;background:#e8ede7;font-size:11px;flex-shrink:0}li>div{flex:1;min-width:0}strong{font-size:12px;font-weight:500}li small{display:block;margin-top:4px;color:#859084;font-size:10px}.step-state{font-size:10px;color:#879182;flex-shrink:0;padding-top:4px}li[data-status=succeeded] .step-number{background:#dcebdc;color:#34723c}li[data-status=running] .step-state,li[data-status=waiting_confirmation] .step-state{color:#966b18}li[data-status=failed] .step-state,.plan-error{color:#ad534b}li p,.plan-error{font-size:11px;margin:6px 0 0;line-height:1.6}
</style>
