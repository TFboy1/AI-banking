<script setup lang="ts">
import { Info } from "@lucide/vue";
import type { Explanation } from "../types";

defineProps<{ explanation: Explanation }>();

const statusText = { pass: "已通过", block: "未通过", unknown: "待确认" };
</script>

<template>
  <details class="decision-evidence">
    <summary><Info :size="14" />为什么这样建议</summary>
    <div class="decision-evidence-body">
      <small>依据生成时间 {{ explanation.as_of.replace("T", " ").slice(0, 16) }}</small>
      <div class="decision-stage">
        <strong>数据来源</strong>
        <div v-for="source in explanation.sources" :key="source.label" class="decision-source">
          <span>{{ source.label }}</span>
          <details v-if="source.references.length">
            <summary>查看记录编号（{{ source.references.length }}）</summary>
            <code v-for="reference in source.references" :key="reference">{{ reference }}</code>
          </details>
        </div>
      </div>
      <div class="decision-stage">
        <strong>计算与检查</strong>
        <div v-for="(item, index) in explanation.checks" :key="`${item.label}-${index}`" class="decision-check">
          <span :class="`decision-${item.status}`">{{ statusText[item.status] }}</span>
          <div><b>{{ item.label }}</b><p>{{ item.detail }}</p></div>
        </div>
      </div>
      <div class="decision-stage"><strong>结论</strong><p>{{ explanation.conclusion }}</p></div>
    </div>
  </details>
</template>
