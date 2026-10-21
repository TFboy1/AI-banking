<script setup lang="ts">
import { computed, ref } from "vue";
import ScheduledTransfers from "./ScheduledTransfers.vue";
import {
  ArrowDownLeft,
  ArrowUpRight,
  CreditCard,
  ChevronDown,
  RotateCcw,
  Wallet,
  X,
  Plus,
  Settings,
} from "@lucide/vue";
import type { BankSnapshot, InspectTool, Subscription } from "../types";
import { api, money } from "../types";

const props = defineProps<{ bank: BankSnapshot; busy: boolean; resetting: boolean; conversationId: string; subscriptionView?: {items:Subscription[];asOf:string}|null }>();
const emit = defineEmits<{
  close: [];
  inspect: [tool: InspectTool];
  reset: [];
  changed: [];
}>();
const cashAmount=computed(()=>props.bank.accounts.reduce((sum,a)=>sum+a.balance_minor,0));
const investmentAmount=computed(()=>props.bank.investment_assets_minor ?? (props.bank.holdings.reduce((sum,p)=>sum+p.value_minor,0)+(props.bank.securities_positions||[]).reduce((sum,p)=>sum+(p.value_minor??p.cost_minor),0)));
const cashAccounts=computed(()=>props.bank.accounts.filter(a=>['checking','savings','category'].includes(a.kind)));
const cardAccounts=computed(()=>props.bank.accounts.filter(a=>a.kind==='bank_card'));
const categoryRows=computed(()=>{const daily=cashAccounts.value.find(a=>a.id==='daily');return [...(daily?[daily]:[]),{id:'investment_summary',name:'投资账户',kind:'investment_summary',balance_minor:investmentAmount.value},...cashAccounts.value.filter(a=>a.id!=='daily')];});
const movementSource=ref('');
const movementOpen=ref(false);const movementDirection=ref('card_to_account');const movementCard=ref('');const movementAccount=ref('');const movementAmount=ref('');const movementError=ref('');const movementBusy=ref(false);
function openMovement(direction:string, account='', card=''){movementDirection.value=direction;movementSource.value=direction==='account_to_account'?account:'';movementAccount.value=direction==='account_to_account'?'':account;movementCard.value=card;movementAmount.value='';movementError.value='';movementOpen.value=true;}
async function submitMovement(){
  movementBusy.value=true;movementError.value='';
  try{const incoming=movementDirection.value==='card_to_account';await api('/account-transfers',{conversation_id:props.conversationId,account_id:movementDirection.value==='account_to_account'?movementSource.value:incoming?movementCard.value:movementAccount.value,target_account_id:movementDirection.value==='account_to_account'?movementAccount.value:incoming?movementAccount.value:movementCard.value,amount_yuan:movementAmount.value});movementOpen.value=false;emit('changed');emit('close');}catch(e){movementError.value=(e as Error).message;}finally{movementBusy.value=false;}
}
const collapsed = ref({ cards: true, subscriptions: true, transactions: true });
const cancellingSubscription=ref("");const subscriptionError=ref("");
async function cancelSubscription(id:string){
  cancellingSubscription.value=id;subscriptionError.value="";
  try{await api(`/subscriptions/${id}/cancel`,{conversation_id:props.conversationId});emit("changed");emit("close");}catch(e){subscriptionError.value=(e as Error).message;}finally{cancellingSubscription.value="";}
}
const cardEditor = ref(false);
const cardBank = ref("");
const cardNumber = ref("");
const deletingCard = ref("");
const cardWorking = ref(false);
const cardError = ref("");
async function addCard() {
  if (cardWorking.value) return;
  cardWorking.value = true; cardError.value = "";
  try { await api("/cards", {bank_name: cardBank.value, card_number: cardNumber.value, conversation_id:props.conversationId}); cardEditor.value = false; cardBank.value = ""; cardNumber.value = ""; emit("changed"); emit("close"); }
  catch (e) { cardError.value = (e as Error).message; }
  finally { cardWorking.value = false; }
}
async function deleteCard(id: string) {
  if (cardWorking.value) return;
  cardWorking.value = true; cardError.value = "";
  try { await api(`/cards/${id}`, {confirmation: "DELETE"}, "DELETE"); deletingCard.value = ""; emit("changed"); }
  catch (e) { cardError.value = (e as Error).message; }
  finally { cardWorking.value = false; }
}
const resetConfirmation = ref(false);
const accountSettings = ref("");
function closeAccountSettings(event: MouseEvent) {
  if (!(event.target instanceof Element) || !event.target.closest('.account-settings-trigger,.account-settings-menu')) accountSettings.value = '';
}
const editing = ref<"create" | "rename" | "delete" | "allocate" | null>(null);
const selected = ref("");
const name = ref("");
const amount = ref("");
const direction = ref("in");
const working = ref(false);
const status = ref("");
function edit(mode: "create" | "rename" | "delete" | "allocate", id = "", label = "") {
  accountSettings.value = "";
  editing.value = mode; selected.value = id; name.value = label; amount.value = ""; direction.value = "in"; status.value = "";
}
async function save() {
  if (working.value) return;
  working.value = true; status.value = "";
  try {
    if (editing.value === "create") await api("/account-categories", {name: name.value});
    else if (editing.value === "rename") await api(`/account-categories/${selected.value}`, {name: name.value}, "PATCH");
    else if (editing.value === "delete") await api(`/account-categories/${selected.value}`, {confirmation: "DELETE"}, "DELETE");
    else if (editing.value === "allocate") await api(`/account-categories/${selected.value}/allocation`, {conversation_id: props.conversationId, amount_yuan: amount.value, direction: direction.value});
    const allocation = editing.value === "allocate";
    editing.value = null; emit("changed");
    if (allocation) { emit("close"); }
  } catch (e) { status.value = (e as Error).message; }
  finally { working.value = false; }
}
</script>

<template>
  <aside class="bank-panel" aria-label="模拟账户面板" @click="closeAccountSettings" @keydown.esc="accountSettings = ''">
    <div class="panel-heading">
      <div>
        <span class="small-eyebrow">你的财务一览</span>
        <h2>模拟账户</h2>
      </div>
      <button
        class="icon-button"
        aria-label="收起模拟账户"
        @click="emit('close')"
      >
        <X :size="19" />
      </button>
    </div>
    <div class="panel-content">
      <section class="asset-summary">
        <div class="asset-topline">
          <span>账户总余额</span><span class="demo-mark">模拟账户</span>
        </div>
        <div class="asset-amount">
          <span>¥</span>{{ money(cashAmount) }}
        </div>
        <div class="asset-allocation"><span>已安排 ¥{{ money(bank.protected_minor || 0) }}</span><span>可自由安排 ¥{{ money(bank.free_minor || 0) }}</span></div>

        <div class="asset-decoration" aria-hidden="true"></div>
      </section>
      <section class="panel-section">
        <div class="section-heading">
          <h3>账户分类</h3>
          <button :disabled="busy || working" @click="edit('create')"><Plus :size="14" />创建账户</button>
        </div>
        <p class="empty-hint">把资金按用途分类，总资产不变。删除分类后余额自动归回日常账户。</p>
        <form v-if="editing" class="category-form" @submit.prevent="save">
          <strong>{{ editing === 'create' ? '创建账户' : editing === 'rename' ? '修改账户名称' : editing === 'allocate' ? '调整资金分类' : '删除账户' }}</strong>
          <label v-if="editing === 'create' || editing === 'rename'">账户名称<input v-model="name" maxlength="24" required placeholder="例如：旅行基金" :disabled="working" /></label>
          <template v-if="editing === 'allocate'">
            <label>分配方向<select v-model="direction" :disabled="working"><option value="in">从日常账户划入</option><option value="out">划回日常账户</option></select></label>
            <label>金额（元）<input v-model="amount" inputmode="decimal" pattern="[0-9]+(\.[0-9]{1,2})?" required placeholder="请输入金额" :disabled="working" /></label>
            <p>下一步生成确认卡，确认并完成支付验证后才调整余额。</p>
          </template>
          <p v-if="editing === 'delete'">确定删除“{{ name }}”？当前全部余额将归回日常账户，已安排资金会一同保留，历史流水不会删除。</p>
          <div><button type="submit" :disabled="busy || working">{{ working ? '处理中…' : editing === 'allocate' ? '生成确认卡' : '确认' }}</button><button type="button" :disabled="working" @click="editing = null; status = ''">取消</button></div>
        </form>
        <p v-if="status" role="alert" class="category-error">{{ status }}</p>
        <div
          v-for="account in categoryRows"
          :key="account.id"
          class="account-row"
          :class="{'investment-category-summary': account.kind === 'investment_summary'}"
        >
          <div
            class="account-symbol"
            :class="{ savings: account.kind === 'savings' || account.kind === 'investment_summary' }"
          >
            <template v-if="account.id !== 'daily' && account.kind !== 'investment_summary'">
              <button type="button" class="account-settings-trigger" :aria-label="account.name + '账户设置'" :aria-expanded="accountSettings === account.id" :aria-controls="'account-settings-' + account.id" :disabled="busy || working" @click="accountSettings = accountSettings === account.id ? '' : account.id"><Settings :size="13" /></button>
              <div v-if="accountSettings === account.id" :id="'account-settings-' + account.id" class="account-settings-menu" role="group" :aria-label="account.name + '账户设置选项'">
                <button type="button" :disabled="busy || working" @click="edit('allocate', account.id, account.name)">分配</button>
                <button type="button" :disabled="busy || working" @click="edit('rename', account.id, account.name)">改名</button>
                <button type="button" :disabled="busy || working" @click="edit('delete', account.id, account.name)">删除</button>
              </div>
            </template>
            <Wallet v-if="account.kind === 'savings' || account.kind === 'investment_summary'" :size="17" /><CreditCard
              v-else
              :size="17"
            />
          </div>
          <div>
            <strong>{{ account.name }}</strong
            ><span>{{
              account.kind === "investment_summary" ? "理财与证券持仓" : account.id === "daily" ? "日常收支 · 默认账户" : "资金分类"
            }}</span>
          </div>
          <div class="category-balance"><b>¥{{ money(account.balance_minor) }}</b>
            <div v-if="account.kind !== 'investment_summary'" class="category-actions">
              <button :disabled="busy || working || cashAccounts.length<2" @click="openMovement('account_to_account', account.id)">转账</button>
              <button :disabled="busy || working || !cardAccounts.length" @click="openMovement('card_to_account', account.id)">转入</button><button :disabled="busy || working || !cardAccounts.length" @click="openMovement('account_to_card', account.id)">提现</button>
            </div>
          </div>
        </div>
      </section>
      <ScheduledTransfers :conversation-id="conversationId" :busy="busy" @changed="emit('changed')" @close="emit('close')" />
      <section class="panel-section cards-section" aria-label="卡片">
        <div class="section-heading"><h3>卡片</h3><div class="section-actions"><template v-if="!collapsed.cards"><button :disabled="busy || cardWorking" @click="cardEditor = !cardEditor; cardError = ''; deletingCard = ''"><Plus :size="14" />添加卡片</button></template>
          <button class="section-collapse" type="button" :aria-expanded="!collapsed.cards" aria-controls="bank-cards-content" :aria-label="(collapsed.cards ? '展开' : '收起') + '卡片'" @click="collapsed.cards = !collapsed.cards">{{ collapsed.cards ? '展开' : '收起' }}<ChevronDown :size="14" :class="{ 'section-chevron-open': !collapsed.cards }" /></button>
        </div></div>
        <div id="bank-cards-content" v-show="!collapsed.cards">
        <form v-if="cardEditor" class="category-form" @submit.prevent="addCard">
          <label>银行名称<input v-model="cardBank" required maxlength="24" placeholder="例如：中国工商银行" :disabled="cardWorking" /></label>
          <label>银行卡号<input v-model="cardNumber" required inputmode="numeric" pattern="[0-9]{16,19}" minlength="16" maxlength="19" autocomplete="off" placeholder="请输入16至19位模拟卡号" :disabled="cardWorking" /></label>
          <p>首次添加默认模拟余额 ¥5,000，重新添加同一卡不会重复增加余额。</p>
          <div><button type="submit" :disabled="busy || cardWorking">{{ cardWorking ? '添加中…' : '确认添加' }}</button><button type="button" :disabled="cardWorking" @click="cardEditor = false; cardNumber = ''">取消</button></div>
        </form>
        <p v-if="cardError" role="alert" class="category-error">{{ cardError }}</p>
        <p v-if="!bank.cards.length" class="empty-hint">暂无银行卡，添加后可在付款时选择。</p>
        <div v-for="card in bank.cards" :key="card.id" class="linked-card">
          <div class="mini-detail"><strong>{{ card.name }}</strong><b v-if="bank.accounts.find(a=>a.id===card.account_id)?.kind === 'bank_card'">¥{{ money(card.balance_minor) }}</b><span v-else>共用账户余额</span></div>
          <p v-if="bank.accounts.find(a=>a.id===card.account_id)?.kind !== 'bank_card'" class="empty-hint">余额已计入 {{ bank.accounts.find(a=>a.id===card.account_id)?.name }}，不重复相加。</p><div class="mini-detail"><span>尾号 {{ card.last4 }} · {{ card.status === 'active' ? '正常' : '已冻结' }}</span><button class="card-remove" :disabled="busy || cardWorking" @click="deletingCard = card.id; cardError = ''">删除</button></div>
          <div v-if="bank.accounts.find(a=>a.id===card.account_id)?.kind === 'bank_card'" class="card-fund-actions"><button :disabled="busy || cardWorking || card.status!=='active' || !cashAccounts.length" @click="openMovement('card_to_account','',card.account_id)">转入账户</button><button :disabled="busy || cardWorking || card.status!=='active' || !cashAccounts.length" @click="openMovement('account_to_card','',card.account_id)">从账户转入</button></div>
          <div v-if="deletingCard === card.id" class="category-form">
            <p>确定删除这张卡？只解除绑定，余额仍保留在原银行卡，不会转入日常账户。解绑后不再计入当前账户余额，重新添加同一卡恢复原余额；历史流水保留。</p>
            <div><button :disabled="busy || cardWorking" @click="deleteCard(card.id)">确认删除</button><button :disabled="cardWorking" @click="deletingCard = ''">取消</button></div>
          </div>
        </div>
              </div>
      </section>
      <section class="panel-section subscriptions-section" aria-label="订阅">
        <div class="section-heading"><h3>订阅</h3><div class="section-actions"><template v-if="!collapsed.subscriptions"><button @click="emit('inspect', 'list_subscriptions')">查看详情 <ArrowUpRight :size="13" /></button></template>
          <button class="section-collapse" type="button" :aria-expanded="!collapsed.subscriptions" aria-controls="bank-subscriptions-content" :aria-label="(collapsed.subscriptions ? '展开' : '收起') + '订阅'" @click="collapsed.subscriptions = !collapsed.subscriptions">{{ collapsed.subscriptions ? '展开' : '收起' }}<ChevronDown :size="14" :class="{ 'section-chevron-open': !collapsed.subscriptions }" /></button>
        </div></div>
        <div id="bank-subscriptions-content" v-show="!collapsed.subscriptions">
        <p v-if="!subscriptionView" class="empty-hint">暂无订阅数据，请点击查看详情刷新。</p><p v-else class="empty-hint">查询时间：{{new Date(subscriptionView.asOf).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false})}}<span v-if="!subscriptionView.items.length"> · 暂无订阅</span></p><p v-if="subscriptionError" class="auth-error" role="alert">{{ subscriptionError }}</p>
        <div v-for="subscription in subscriptionView?.items||[]" :key="subscription.id" class="subscription-item">
          <div class="mini-detail"><strong>{{ subscription.name }}</strong><b>¥{{ money(subscription.amount_minor) }}/月</b></div>
          <div class="mini-detail"><span>{{ subscription.status === 'active' ? '下次扣费：' + subscription.next_charge_date : '已取消 · 不再扣费' }}</span><span>{{ subscription.status === 'active' ? '代扣中' : '已取消' }}</span></div><p class="empty-hint">当前服务到期：{{ subscription.expires_on || '未知' }}（按模拟续费日推算）</p><button v-if="subscription.status === 'active'" class="quiet-button" :disabled="busy || !!cancellingSubscription" @click="cancelSubscription(subscription.id)">{{ cancellingSubscription===subscription.id ? '正在准备确认…' : '取消订阅' }}</button>
        </div>
              </div>
      </section>
      <section class="panel-section transactions-section">
        <div class="section-heading">
          <h3>最近流水</h3>
          <div class="section-actions"><template v-if="!collapsed.transactions"><button @click="emit('inspect', 'analyze_bills')">
            分析账单 <ArrowUpRight :size="13" />
          </button>
        </template>
          <button class="section-collapse" type="button" :aria-expanded="!collapsed.transactions" aria-controls="bank-transactions-content" :aria-label="(collapsed.transactions ? '展开' : '收起') + '最近流水'" @click="collapsed.transactions = !collapsed.transactions">{{ collapsed.transactions ? '展开' : '收起' }}<ChevronDown :size="14" :class="{ 'section-chevron-open': !collapsed.transactions }" /></button>
        </div></div>
        <div id="bank-transactions-content" v-show="!collapsed.transactions">
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
              </div>
      </section>

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
    <div v-if="movementOpen" class="investment-dialog-backdrop"><form class="investment-dialog category-form" role="dialog" aria-modal="true" aria-label="本人账户划转" @submit.prevent="submitMovement"><h3>本人账户划转</h3><label>划转方向<select v-model="movementDirection" :disabled="movementBusy"><option value="account_to_account">账户转到账户</option><option value="card_to_account">银行卡转入账户</option><option value="account_to_card">账户提现至银行卡</option></select></label><label v-if="movementDirection!=='account_to_account'">银行卡<select v-model="movementCard" aria-label="划转银行卡" required :disabled="movementBusy"><option value="" disabled>请选择银行卡</option><option v-for="card in cardAccounts" :key="card.id" :value="card.id" :disabled="bank.cards.find(c=>c.account_id===card.id)?.status!=='active'">{{ card.name }} · ¥{{ money(card.balance_minor) }}</option></select></label><label v-if="movementDirection==='account_to_account'">付款账户<select v-model="movementSource" aria-label="划转付款账户" required :disabled="movementBusy"><option value="" disabled>请选择付款账户</option><option v-for="account in cashAccounts" :key="account.id" :value="account.id">{{account.name}} · ¥{{money(account.balance_minor)}}</option></select></label><label>{{movementDirection==='account_to_account'?'到账账户':'分类账户'}}<select v-model="movementAccount" aria-label="划转分类账户" required :disabled="movementBusy"><option value="" disabled>请选择账户</option><option v-for="account in cashAccounts" :key="account.id" :value="account.id" :disabled="movementDirection==='account_to_account' && account.id===movementSource">{{ account.name }} · ¥{{ money(account.balance_minor) }}</option></select></label><label>金额（元）<input v-model="movementAmount" aria-label="划转金额" required inputmode="decimal" pattern="(0|[1-9][0-9]{0,8})([.][0-9]{1,2})?" :disabled="movementBusy" /></label><p>下一步核对确认卡，交易验证后模拟到账。</p><p v-if="movementError" class="category-error" role="alert">{{ movementError }}</p><div class="investment-buttons"><button type="button" :disabled="movementBusy" @click="movementOpen=false">取消</button><button class="investment-primary" type="submit" :disabled="movementBusy || busy">{{ movementBusy?'正在核对…':'生成确认卡' }}</button></div></form></div>
  </aside>
</template>

<style scoped>
.section-actions { display: flex; align-items: center; gap: 12px; }
.section-actions button { display: inline-flex; align-items: center; gap: 3px; padding: 2px 0; border: 0; background: none; color: #8b9e79; font-size: 11px; cursor: pointer; }
.section-actions button:disabled { opacity: .5; cursor: default; }
.section-collapse { white-space: nowrap; }
.section-collapse svg { transition: transform .15s; }
.section-chevron-open { transform: rotate(180deg); }

.linked-card,.subscription-item { padding: 10px 0; border-bottom: 1px solid #e6ece8; }
.card-remove { background: transparent; border: 0; color: #237a58; cursor: pointer; font-size: 12px; }
.linked-card .mini-detail,.subscription-item .mini-detail { gap: 8px; }
.category-form { display: grid; gap: 12px; padding: 14px; border: 1px solid #dfe7e3; border-radius: 12px; margin: 12px 0; }
.category-form label { display: grid; gap: 6px; font-size: 12px; }
.category-form input,.category-form select { width: 100%; min-width: 0; box-sizing: border-box; padding: 9px; border: 1px solid #cad6cf; border-radius: 7px; background: white; }
.category-form p { font-size: 12px; line-height: 1.6; margin: 0; }
.category-form button,.category-actions button { border: 0; background: transparent; color: #237a58; cursor: pointer; padding: 4px 7px; font-size: 12px; }
.category-form button:disabled,.category-actions button:disabled { opacity: .5; cursor: default; }
.category-balance { margin-left: auto; text-align: right; flex-shrink: 0; }
.category-actions { display: flex; justify-content: flex-end; margin-top: 5px; }
.category-error { color: #aa342c; font-size: 12px; }
.account-symbol{position:relative;flex-shrink:0}
.account-settings-trigger{position:absolute;left:-7px;top:-7px;width:21px;height:21px;display:grid;place-items:center;padding:0;border:1px solid #dce5da;border-radius:7px;background:#fafcf8;color:#6b8566;cursor:pointer}
.account-settings-trigger:disabled{opacity:.5;cursor:default}
.account-settings-menu{position:absolute;left:-7px;top:19px;z-index:12;display:flex;flex-direction:column;align-items:stretch;width:112px;padding:5px;border:1px solid #dce5da;border-radius:9px;background:#fff;box-shadow:0 8px 24px #183c201c}
.account-settings-menu button{padding:8px 11px;border:0;background:transparent;border-radius:5px;text-align:left;font-size:12px;color:#237a58;cursor:pointer}
.account-settings-menu button:hover,.account-settings-menu button:focus-visible{background:#edf3e9}
</style>

<style scoped>
.linked-card .mini-detail:first-child{color:inherit}.linked-card .mini-detail:first-child strong{font-size:11px;font-weight:500}.linked-card .mini-detail:first-child b{font-size:12px;font-weight:600;letter-spacing:-0.2px;font-variant-numeric:tabular-nums}
</style>

<style scoped>
.category-actions{flex-wrap:wrap;max-width:180px}.card-fund-actions{display:flex;gap:14px;justify-content:flex-end;margin:8px 0}.card-fund-actions button{border:0;background:transparent;color:#237a58;font-size:12px;cursor:pointer}.card-fund-actions button:disabled{opacity:.45;cursor:default}
</style>
