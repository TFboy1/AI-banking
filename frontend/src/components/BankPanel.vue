<script setup lang="ts">
import { ref } from "vue";
import {
  ArrowDownLeft,
  ArrowUpRight,
  ChevronDown,
  CreditCard,
  RotateCcw,
  Wallet,
  X,
  Radio,
} from "@lucide/vue";
import type { BankSnapshot, InspectTool } from "../types";
import { money } from "../types";

const props = defineProps<{ bank: BankSnapshot; busy: boolean; resetting: boolean; eventBusy?: boolean }>();
const emit = defineEmits<{
  close: [];
  inspect: [tool: InspectTool];
  reset: [];
  salaryEvent: [];
  demoScenario: [];
}>();
const resetConfirmation = ref(false);
const eventConfirmation = ref(false);
</script>

<template>
  <aside class="bank-panel" aria-label="模拟银行面板">
    <div class="panel-heading">
      <div>
        <span class="small-eyebrow">你的财务一览</span>
        <h2>模拟银行</h2>
      </div>
      <button
        class="icon-button"
        aria-label="收起模拟银行"
        @click="emit('close')"
      >
        <X :size="19" />
      </button>
    </div>
    <div class="panel-content">
      <section class="asset-summary">
        <div class="asset-topline">
          <span>总资产</span><span class="demo-mark">模拟账户</span>
        </div>
        <div class="asset-amount">
          <span>¥</span>{{ money(bank.total_assets_minor) }}
        </div>
        <div class="asset-allocation"><span>已安排 ¥{{ money(bank.protected_minor || 0) }}</span><span>可自由安排 ¥{{ money(bank.free_minor || 0) }}</span></div>
        <p>随每次操作更新 · 人民币</p>
        <div class="asset-decoration" aria-hidden="true"></div>
      </section>
      <section class="panel-section event-section">
        <div class="section-heading"><h3>条件触发演示</h3><span>不自动转账</span></div>
        <p class="event-copy">模拟一笔工资实际到账，助手会重新计算可转存金额并在聊天里等待采纳。</p>
        <button class="event-button" :disabled="busy || eventBusy" @click="eventConfirmation = true"><Radio :size="14" />模拟工资到账</button>
        <button class="scenario-link" :disabled="busy || eventBusy" @click="emit('demoScenario')">载入缺口演示场景</button>
        <div v-if="eventConfirmation" class="event-confirmation">
          <span>按当前预测工资 ¥18,000、周期 {{ bank.next_salary_period }} 触发一次。</span>
          <div><button :disabled="eventBusy" @click="emit('salaryEvent'); eventConfirmation = false">{{ eventBusy ? "处理中…" : "确认触发" }}</button><button @click="eventConfirmation = false">取消</button></div>
        </div>
      </section>
      <section class="panel-section">
        <div class="section-heading">
          <h3>我的账户</h3>
          <span>{{ bank.accounts.length }} 个账户</span>
        </div>
        <div
          v-for="account in bank.accounts"
          :key="account.id"
          class="account-row"
        >
          <div
            class="account-symbol"
            :class="{ savings: account.kind === 'savings' }"
          >
            <Wallet v-if="account.kind === 'savings'" :size="17" /><CreditCard
              v-else
              :size="17"
            />
          </div>
          <div>
            <strong>{{ account.name }}</strong
            ><span>{{
              account.kind === "savings" ? "储蓄备用金" : "日常收支"
            }}</span>
          </div>
          <b>¥{{ money(account.balance_minor) }}</b>
        </div>
      </section>
      <section class="panel-section">
        <div class="section-heading">
          <h3>理财持仓</h3>
          <button @click="emit('inspect', 'list_products')">
            查看产品 <ArrowUpRight :size="13" />
          </button>
        </div>
        <div v-if="!bank.holdings.length" class="empty-hint">暂时没有持仓</div>
        <div
          v-for="holding in bank.holdings"
          :key="holding.product_id"
          class="holding-row"
        >
          <div>
            <strong
              >{{ holding.name }}
              <small>R{{ holding.risk_level }}</small></strong
            ><span>{{ holding.units }} 份</span>
          </div>
          <b>¥{{ money(holding.value_minor) }}</b>
        </div>
      </section>
      <section class="panel-section">
        <div class="section-heading">
          <h3>最近流水</h3>
          <button @click="emit('inspect', 'analyze_bills')">
            分析账单 <ArrowUpRight :size="13" />
          </button>
        </div>
        <div v-if="!bank.transactions.length" class="empty-hint">
          暂时没有交易
        </div>
        <div
          v-for="transaction in bank.transactions"
          :key="transaction.id"
          class="transaction-row"
        >
          <div
            class="transaction-direction"
            :class="{ income: transaction.direction === 'in' }"
          >
            <ArrowDownLeft
              v-if="transaction.direction === 'in'"
              :size="14"
            /><ArrowUpRight v-else :size="14" />
          </div>
          <div class="transaction-description">
            <strong>{{ transaction.counterparty }}</strong
            ><span
              >{{ transaction.occurred_at.slice(5, 10).replace("-", "/") }} ·
              {{ transaction.category }}</span
            >
          </div>
          <b :class="{ income: transaction.direction === 'in' }"
            >{{ transaction.direction === "in" ? "+" : "−"
            }}{{ money(transaction.amount_minor) }}</b
          >
        </div>
      </section>
      <details class="bank-details">
        <summary>卡片与订阅 <ChevronDown :size="15" /></summary>
        <div v-for="card in bank.cards" :key="card.id" class="mini-detail">
          <span>{{ card.name }} · {{ card.last4 }}</span
          ><span :class="{ 'frozen-label': card.status === 'frozen' }">{{
            card.status === "active" ? "正常" : "已冻结"
          }}</span>
        </div>
        <div class="mini-detail">
          <span>有效代扣协议</span
          ><span
            >{{
              bank.subscriptions.filter((item) => item.status === "active")
                .length
            }}
            项</span
          >
        </div>
        <div class="panel-small-actions">
          <button @click="emit('inspect', 'list_cards')">查看卡片</button
          ><button @click="emit('inspect', 'list_subscriptions')">
            查看订阅
          </button>
        </div>
      </details>
      <div class="panel-footer">
        <p>这是你的银行演示环境。所有账户、产品及交易均为模拟数据。</p>
        <button
          v-if="!resetConfirmation"
          class="reset-link"
          :disabled="busy"
          @click="resetConfirmation = true"
        >
          <RotateCcw :size="13" />重置演示数据
        </button>
        <div v-else class="reset-confirmation">
          <p>将恢复初始资金并清空聊天与操作记录。</p>
          <button
            :disabled="busy || resetting"
            @click="
              emit('reset');
              resetConfirmation = false;
            "
          >
            {{ resetting ? "重置中…" : "确认重置" }}</button
          ><button @click="resetConfirmation = false">保留数据</button>
        </div>
      </div>
    </div>
  </aside>
</template>
