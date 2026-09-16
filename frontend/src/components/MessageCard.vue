<script setup lang="ts">
import { computed } from "vue";
import {
  ArrowUpRight,
  Check,
  CircleAlert,
  Clock3,
  ShieldCheck,
  X,
} from "@lucide/vue";
import { money, timeLabel } from "../types";
import type { Message } from "../types";

const props = defineProps<{ message: Message; deciding: string | null }>();
const emit = defineEmits<{
  decision: [id: string, decision: "confirm" | "cancel"];
}>();
const display = computed(() => props.message.data.result?.display);
const isPending = computed(() => props.message.data.status === "pending");
const statusLabel = computed(
  () =>
    ({
      pending: "等待你的确认",
      executed: "操作已完成",
      cancelled: "已取消",
      failed: "操作未完成",
      expired: "确认已过期",
    })[props.message.data.status || ""] || "",
);
</script>

<template>
  <section v-if="message.kind === 'data' && display" class="result-card">
    <div class="result-heading">
      <span class="small-eyebrow">银行数据</span
      ><span v-if="message.data.source === 'sandbox'" class="source-label"
        >模拟面板查询</span
      >
    </div>
    <h3>{{ display.title }}</h3>
    <div v-if="display.metrics.length" class="result-metrics">
      <div v-for="metric in display.metrics" :key="metric.label">
        <span>{{ metric.label }}</span
        ><strong>{{ metric.value }}</strong>
      </div>
    </div>
    <div
      v-if="display.rows.length"
      class="table-scroll"
      tabindex="0"
      :aria-label="display.title"
    >
      <table>
        <thead>
          <tr>
            <th v-for="column in display.columns" :key="column">
              {{ column }}
            </th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(row, index) in display.rows" :key="index">
            <td v-for="(value, cell) in row" :key="cell">{{ value }}</td>
          </tr>
        </tbody>
      </table>
    </div>
    <p v-else class="empty-hint">这个范围内暂时没有记录。</p>
    <div
      v-for="alert in message.data.result?.alerts"
      :key="alert.text"
      class="data-alert"
    >
      <CircleAlert :size="15" />
      <div>
        {{ alert.text }}
        <details>
          <summary>查看交易依据</summary>
          <code v-for="reference in alert.references" :key="reference">{{
            reference
          }}</code>
        </details>
      </div>
    </div>
    <div v-if="message.data.result?.holdings?.length" class="inline-holdings">
      <span class="small-eyebrow">当前持仓</span>
      <div
        v-for="holding in message.data.result.holdings"
        :key="holding.product_id"
      >
        <span>{{ holding.name }} · {{ holding.units }} 份</span
        ><strong>¥{{ money(holding.value_minor) }}</strong>
      </div>
    </div>
    <p v-if="display.note" class="card-note">{{ display.note }}</p>
  </section>

  <section
    v-else-if="message.kind === 'approval'"
    class="approval-card"
    :class="{
      'approval-done': !isPending,
      'approval-failed': ['failed', 'expired'].includes(
        message.data.status || '',
      ),
    }"
  >
    <div class="approval-heading">
      <div class="approval-icon">
        <ArrowUpRight v-if="isPending" :size="21" /><Check
          v-else-if="message.data.status === 'executed'"
          :size="21"
        /><X v-else :size="19" />
      </div>
      <div>
        <span class="small-eyebrow">{{ statusLabel }}</span>
        <h3>{{ message.data.title }}</h3>
      </div>
    </div>
    <div v-if="message.data.amount_minor !== undefined" class="approval-amount">
      <span>¥</span>{{ money(message.data.amount_minor) }}
    </div>
    <dl class="confirmation-fields">
      <div v-for="field in message.data.fields" :key="field.label">
        <dt>{{ field.label }}</dt>
        <dd>{{ field.value }}</dd>
      </div>
    </dl>
    <div v-if="isPending" class="confirmation-bottom">
      <p><ShieldCheck :size="14" />核对后才会执行，确认有效期 15 分钟</p>
      <div class="confirmation-actions">
        <button
          class="primary-button"
          :disabled="deciding === message.data.approval_id"
          @click="emit('decision', message.data.approval_id!, 'confirm')"
        >
          <Check :size="16" />{{
            deciding === message.data.approval_id ? "处理中…" : "确认并执行"
          }}
        </button>
        <button
          class="quiet-button"
          :disabled="deciding === message.data.approval_id"
          @click="emit('decision', message.data.approval_id!, 'cancel')"
        >
          取消
        </button>
      </div>
    </div>
    <p v-if="message.data.error" class="approval-error">
      {{ message.data.error }}
    </p>
    <div v-if="message.data.receipt" class="receipt-footer">
      <span
        ><Check :size="13" />{{
          timeLabel(message.data.receipt.executed_at)
        }}
        已执行</span
      ><span :title="message.data.receipt.action_id"
        >回执 {{ message.data.receipt.action_id.slice(-8).toUpperCase() }}</span
      >
    </div>
    <p v-else-if="message.data.status === 'cancelled'" class="cancel-note">
      <Clock3 :size="13" />这项操作没有执行
    </p>
  </section>
</template>
