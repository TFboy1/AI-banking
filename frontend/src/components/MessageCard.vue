<script setup lang="ts">
import { computed, ref } from "vue";
import {
  ArrowUpRight,
  Check,
  CircleAlert,
  Clock3,
  ShieldCheck,
  X,
  CalendarClock,
  FlaskConical,
  Info,
} from "@lucide/vue";
import { money, timeLabel } from "../types";
import type { Challenge, Message, Plan } from "../types";
import CashflowChart from "./CashflowChart.vue";
import DecisionEvidence from "./DecisionEvidence.vue";

const props = defineProps<{ message: Message; deciding: string | null; acting?: string | null }>();
const emit = defineEmits<{
  decision: [id: string, decision: "confirm" | "cancel"];
  planAction: [planId: string, action: string, version: number, variant?: string];
  adoptTrigger: [notificationId: string];
  suggest: [prompt: string];
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
const plan = computed(() => props.message.data.plan as Plan | undefined);
const selectedVariantKey = ref<"now" | "after_salary">("now");
const selectedVariant = computed(() =>
  plan.value?.variants.find((variant) => variant.key === selectedVariantKey.value) || plan.value?.variants[0],
);
const challenge = computed(() => props.message.data.challenge as Challenge | undefined);
const diagnosis = computed(() => props.message.data.result?.diagnosis);
const isPlan = computed(() => props.message.kind === "plan");
const fmtDate = (value?: string) => value ? value.slice(0, 10).replaceAll("-", "/") : "—";
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
    <DecisionEvidence v-if="message.data.result?.explanation" :explanation="message.data.result.explanation" />
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
    <DecisionEvidence v-if="message.data.explanation" :explanation="message.data.explanation" />
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

  <section v-else-if="message.kind === 'diagnosis' && diagnosis" class="diagnosis-card" :class="`diagnosis-${diagnosis.status}`">
    <div class="diagnosis-heading"><span class="small-eyebrow">业务受阻诊断 · 只读检查</span><h3>{{ diagnosis.title }}</h3><strong>{{ diagnosis.status === "ready" ? "可继续" : diagnosis.status === "blocked" ? "暂不能办理" : "信息不足" }}</strong></div>
    <div class="diagnosis-checks">
      <div v-for="item in diagnosis.checks" :key="item.key || item.label" class="diagnosis-check">
        <span :class="`diagnosis-mark-${item.status}`">{{ item.status === "pass" ? "已通过" : item.status === "block" ? "未通过" : "待确认" }}</span>
        <div><b>{{ item.label }}</b><p>{{ item.detail }}</p></div>
      </div>
    </div>
    <div v-if="diagnosis.next_steps.length" class="diagnosis-actions">
      <span>下一步</span>
      <button v-for="step in diagnosis.next_steps" :key="step.prompt" class="outline-button" @click="emit('suggest', step.prompt)">{{ step.label }}</button>
      <small>点击只会填入输入框，由你决定是否发送。</small>
    </div>
    <p v-else class="card-note">当前检查通过。如需办理，请在聊天中明确提出，后续操作仍需确认。</p>
    <DecisionEvidence v-if="message.data.result?.explanation" :explanation="message.data.result.explanation" />
  </section>

  <section v-else-if="isPlan && plan" class="plan-card">
    <div class="plan-heading">
      <div class="plan-icon"><FlaskConical :size="18" /></div>
      <div><span class="small-eyebrow">资金预演 · v{{ plan.version }}</span><h3>{{ plan.title }}</h3></div>
      <span class="plan-status">{{ plan.status === "active" ? "已采用" : "预演草稿" }}</span>
    </div>
    <div class="plan-metrics">
      <div><span>计划金额</span><strong>¥{{ money(plan.amount_minor) }}</strong></div>
      <div><span>生活费预留</span><strong>¥{{ money(plan.living_reserve_minor) }}</strong></div>
      <div><span>目标期限</span><strong>{{ fmtDate(plan.deadline) }}</strong></div>
    </div>
    <div class="variant-grid">
      <div v-for="variant in plan.variants" :key="variant.key" class="variant-option" :class="{ feasible: variant.result.feasible }">
        <div><span>{{ variant.label }}</span><CalendarClock :size="14" /></div>
        <strong>{{ fmtDate(variant.action_date) }}</strong>
        <small>{{ variant.result.feasible ? "现金流可行" : `缺口 ¥${money(variant.result.first_failure?.shortfall_minor || 0)}` }}</small>
        <em>最低可支配 ¥{{ money(variant.result.lowest_available_minor) }}</em>
        <div v-if="variant.result.feasible" class="variant-actions">
          <button v-if="plan.status !== 'active'" @click="emit('planAction', plan.plan_id, 'activate', plan.version, variant.key)">采用方案</button>
          <button v-else @click="emit('planAction', plan.plan_id, 'execute', plan.version, variant.key)">生成执行确认</button>
        </div>
      </div>
    </div>
    <div v-if="plan.variants.length" class="cashflow-variant-switch" aria-label="查看预演方案的现金流曲线">
      <button v-for="variant in plan.variants" :key="variant.key" :class="{ active: selectedVariant?.key === variant.key }" :aria-pressed="selectedVariant?.key === variant.key" @click="selectedVariantKey = variant.key">{{ variant.label }}</button>
    </div>
    <CashflowChart v-if="selectedVariant?.result.timeline" :variant="selectedVariant" :as-of="plan.assumptions.as_of" />
    <p v-else class="card-note">这份历史方案没有逐事件数据，可重新预演后查看曲线。</p>
    <DecisionEvidence v-if="plan.explanation" :explanation="plan.explanation" />
    <details v-else class="plan-evidence">
      <summary><Info :size="14" />查看计算依据</summary>
      <p>按 {{ fmtDate(plan.assumptions.as_of) }} 的余额开始推演；工资和房租来自历史周期，协议订阅按有效代扣计算。</p>
      <p v-if="plan.assumptions.salary">工资：每月 {{ plan.assumptions.salary.day }} 日，¥{{ money(plan.assumptions.salary.amount_minor) }}；依据 {{ plan.assumptions.salary.evidence_ids.length }} 笔历史流水。</p>
      <p v-if="plan.assumptions.rent">房租：每月 {{ plan.assumptions.rent.day }} 日，¥{{ money(plan.assumptions.rent.amount_minor) }}；依据 {{ plan.assumptions.rent.evidence_ids.length }} 笔历史流水。</p>
      <p v-if="plan.installments?.length">储蓄分期：<span v-for="(step,index) in plan.installments" :key="step.date">{{ index ? "；" : "" }}{{ fmtDate(step.date) }} ¥{{ money(step.amount_minor) }}</span></p>
    </details>
    <div class="plan-actions">
      <button class="outline-button" :disabled="!!acting" @click="emit('planAction', plan.plan_id, 'challenge_salary', plan.version)"><FlaskConical :size="14" />挑战工资延期</button>
      <button class="outline-button" :disabled="!!acting" @click="emit('planAction', plan.plan_id, 'challenge_expense', plan.version)">测试突发支出</button>
      <button class="outline-button" :disabled="!!acting" @click="emit('planAction', plan.plan_id, 'create_rule', plan.version, 'after_salary')">工资到账时提醒</button>
    </div>
    <p class="card-note">采用方案只保存安排；真正消费或转存仍会单独弹出确认卡。</p>
  </section>

  <section v-else-if="isPlan && challenge" class="challenge-card">
    <div class="plan-heading"><div class="plan-icon warning"><FlaskConical :size="18" /></div><div><span class="small-eyebrow">方案压力测试</span><h3>{{ challenge.found ? "找到了最小失效边界" : "本次范围内通过" }}</h3></div></div>
    <div v-if="challenge.found && challenge.dimension === 'salary_delay'" class="challenge-result">
      <strong>工资晚发 {{ challenge.minimum_days }} 天时，原方案第一次失效</strong>
      <span v-if="challenge.failure">{{ fmtDate(challenge.failure.date) }} · {{ challenge.failure.event }} · 缺口 ¥{{ money(challenge.failure.shortfall_minor) }}</span>
      <span>测试范围：{{ challenge.scope }}</span>
      <div v-if="challenge.repair" class="repair-note"><Check :size="15" />建议延后到 {{ fmtDate(challenge.repair.date) }}，仍保留原目标。</div>
      <button v-if="challenge.repair" class="primary-button" :disabled="!!acting" @click="emit('planAction', message.data.plan_id!, 'apply_repair', message.data.version!)">采用修复并重新预演</button>
    </div>
    <div v-else-if="challenge.found" class="challenge-result">
      <strong>预算外支出最多可承受 ¥{{ money(challenge.maximum_minor || 0) }}</strong>
      <span>{{ fmtDate(challenge.date) }} 支出 ¥{{ money(challenge.first_failure_minor || 0) }} 时首次出现 ¥0.01 缺口。</span>
      <span>测试范围：{{ challenge.scope }}</span>
    </div>
    <p v-else class="card-note">{{ challenge.reason }}</p>
  </section>

  <section v-else-if="isPlan && message.data.card_type === 'trigger'" class="plan-card trigger-card">
    <div class="plan-heading"><div class="plan-icon"><CalendarClock :size="18" /></div><div><span class="small-eyebrow">条件触发助手</span><h3>工资到账，方案已重算</h3></div></div>
    <div class="trigger-amount"><span>本次建议转存</span><strong>¥{{ money(message.data.amount_minor || 0) }}</strong></div>
    <p class="card-note">已扣除当前已安排资金；这是待确认建议，不会自动转账。</p>
    <button class="primary-button" :disabled="!!acting" @click="emit('adoptTrigger', message.data.notification_id!)"><Check :size="15" />采纳并核对转存</button>
  </section>
</template>
