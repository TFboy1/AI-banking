<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import { X, ShieldCheck, Wallet } from "@lucide/vue";
import ProductDetails from "./ProductDetails.vue";
import { api, money } from "../types";
import type { BankSnapshot, Message, Field, Holding } from "../types";

interface Product { id: string; name: string; risk_level: number; nav: string; min_purchase_minor: number; description: string; eligible: boolean }
interface Question { key: string; label: string; options: { value: string; label: string }[] }
interface InvestmentData { products: Product[]; holdings: Holding[]; risk_level: number | null; questionnaire: { questions: Question[]; current_answers: Record<string, string> | null } }
const props = defineProps<{ bank: BankSnapshot; conversationId: string; busy: boolean }>();
const emit = defineEmits<{ close: []; changed: [] }>();
const data = ref<InvestmentData | null>(null);
const tab = ref("holdings");
const retaking = ref(false);
function retakeRisk() { retaking.value = true; answers.value = {}; error.value = ""; status.value = ""; action.value = null; }
const detailsId = ref("");
const answers = ref<Record<string, string>>({});
const loading = ref(false);
const processing = ref(false);
const error = ref("");
const status = ref("");
const action = ref<{ product: Product; sell: boolean } | null>(null);
const amount = ref("");
const accountId = ref("");
const approval = ref<Message | null>(null);
const stockAction = ref<NonNullable<BankSnapshot["securities_positions"]>[number] | null>(null);
const stockShares = ref("");
function selectStock(position: NonNullable<BankSnapshot["securities_positions"]>[number]) { stockAction.value=position;stockShares.value=String(position.shares);accountId.value="";error.value=""; }
async function submitStockSale() {
  if(!stockAction.value)return;const shares=Number(stockShares.value);
  if(!Number.isSafeInteger(shares)||shares<=0||shares%100!==0||shares>stockAction.value.shares){error.value="卖出股数须为100的整数倍，且不能超过持仓。";return;}
  if(!accountId.value){error.value="请选择到账账户。";return;}
  processing.value=true;error.value="";try{approval.value=await api<Message>("/securities/sell",{conversation_id:props.conversationId,symbol:stockAction.value.symbol,shares,account_id:accountId.value});}catch(e){error.value=(e as Error).message;}finally{processing.value=false;}
}
const eligible = (p: Product) => props.bank.risk_level !== null && p.risk_level <= props.bank.risk_level;
const stockPositions = computed(() => props.bank.securities_positions || []);
const stockValue = computed(() => stockPositions.value.reduce((sum, p) => sum + (p.value_minor ?? p.cost_minor), 0));
const total = computed(() => props.bank.holdings.reduce((sum, h) => sum + h.value_minor, 0) + stockValue.value);
const complete = computed(() => !!data.value && data.value.questionnaire.questions.every(q => !!answers.value[q.key]));
const holding = computed(() => props.bank.holdings.find(h => h.product_id === action.value?.product.id));
async function load() {
  loading.value = true;
  try { data.value = await api<InvestmentData>("/investments"); }
  catch (e) { error.value = (e as Error).message; }
  finally { loading.value = false; }
}
function select(product: Product, sell = false) {
  error.value = ""; status.value = "";
  action.value = { product, sell }; amount.value = ""; accountId.value = "";
}
function productFor(id: string) { return data.value?.products.find(p => p.id === id); }
async function prepare(tool: string, args: Record<string, unknown>) {
  processing.value = true; error.value = ""; status.value = "";
  try {
    approval.value = await api<Message>("/investments/approvals", { conversation_id: props.conversationId, tool, arguments: args });
  } catch (e) { error.value = (e as Error).message; }
  finally { processing.value = false; }
}
async function submitTrade() {
  if (!action.value) return;
  const sell = action.value.sell;
  const pattern = sell ? /^(0|[1-9]\d{0,9})(\.\d{1,4})?$/ : /^(0|[1-9]\d{0,8})(\.\d{1,2})?$/;
  if (!pattern.test(amount.value) || Number(amount.value) <= 0) { error.value = sell ? "请输入大于零的份额，最多四位小数。" : "请输入大于零的金额，最多两位小数。"; return; }
  if (sell && !accountId.value) { error.value = "请选择到账账户。"; return; }
  await prepare(sell ? "redeem_product" : "purchase_product", { product_id: action.value.product.id, ...(sell ? {account_id: accountId.value} : {}), [sell ? "units" : "amount_yuan"]: amount.value });
}
async function decide(decision: "confirm" | "cancel") {
  if (!approval.value?.data.approval_id) return;
  processing.value = true; error.value = "";
  try {
    const result = await api<{ status: string; error?: string }>(`/approvals/${approval.value.data.approval_id}/decision`, { decision, conversation_id: approval.value.conversation_id });
    if (result.status === "executed") { status.value = "操作已完成，账户已更新。"; action.value = null; stockAction.value = null; answers.value = {}; retaking.value = false; }
    else if (result.status === "cancelled") status.value = "操作已取消。";
    else error.value = result.error || "操作未执行，确认可能已过期，请重新发起。";
    approval.value = null;
    emit("changed"); await load();
  } catch (e) { error.value = (e as Error).message; }
  finally { processing.value = false; }
}
watch(() => [props.bank.risk_level, props.bank.total_assets_minor], () => { void load(); });
watch(() => props.conversationId, () => { if (!approval.value) action.value = null; });
onMounted(load);
</script>

<template>
  <aside class="bank-panel investment-panel" aria-label="投资账户面板">
    <div class="panel-heading"><div><span class="small-eyebrow">模拟投资 · 由你确认</span><h2>投资账户</h2></div><button class="icon-button" aria-label="收起投资账户" @click="emit('close')"><X :size="19" /></button></div>
    <div class="panel-content">
      <p v-if="error" class="investment-error" role="alert">{{ error }}</p>
      <p v-if="status" class="investment-status" role="status">{{ status }}</p>
      <p v-if="loading && !data">正在加载投资账户…</p>
      <button v-if="!data && !loading" class="outline-button" @click="load">重新加载</button>
      <form v-if="data && (bank.risk_level === null || retaking)" class="risk-form" @submit.prevent="prepare('save_risk_profile', answers)">
        <div class="investment-intro"><ShieldCheck :size="24" /><h3>{{ retaking ? "重新评估风险偏好" : "先了解你的风险偏好" }}</h3><p>请按实际情况选择，等级由系统计算。重新测评在确认提交后生效，取消不会改变原等级或已有持仓。</p></div>
        <fieldset v-for="(question, index) in data.questionnaire.questions" :key="question.key" :disabled="processing || !!approval">
          <legend>{{ index + 1 }}. {{ question.label }}</legend>
          <label v-for="option in question.options" :key="option.value" :class="{ chosen: answers[question.key] === option.value }"><input v-model="answers[question.key]" type="radio" :name="question.key" :value="option.value" required />{{ option.label }}</label>
        </fieldset>
        <button v-if="retaking" type="button" class="outline-button" :disabled="processing || !!approval" @click="retaking = false; answers = {}">返回，不修改测评</button>
        <button class="investment-primary" :disabled="!complete || busy || processing || !!approval" type="submit">提交测评并核对</button>
      </form>
      <template v-else-if="data">
        <section class="asset-summary"><div class="asset-topline"><span>投资资产合计</span><span class="demo-mark">风险等级 R{{ bank.risk_level }}</span></div><div class="asset-amount"><span>¥</span>{{ money(total) }}</div><p>理财按模拟净值，股票与证券平台共用行情估值。股票在工作日开盘后、收盘后各同步一次，非实时成交价格。</p></section>
        <p v-if="bank.securities_valuation_stale || bank.securities_valuation_complete === false" class="investment-error">部分股票行情暂不可用：保留上次估值；无历史估值时按成本暂估。</p><button class="outline-button" :disabled="busy || processing || !!approval" @click="retakeRisk">重新测评风险等级</button>
        <div class="investment-tabs" role="tablist" aria-label="投资账户页面"><button role="tab" :aria-selected="tab === 'holdings'" :class="{ active: tab === 'holdings' }" @click="tab = 'holdings'">我的理财</button><button role="tab" :aria-selected="tab === 'market'" :class="{ active: tab === 'market' }" @click="tab = 'market'">理财市场</button></div>
        <section v-if="tab === 'holdings'" role="tabpanel" aria-label="我的理财">
          <div v-if="!bank.holdings.length && !stockPositions.length" class="investment-empty"><Wallet :size="28" /><p>还没有投资持仓</p><button class="outline-button" @click="tab = 'market'">去理财市场看看</button></div>
          <h3 v-if="bank.holdings.length && stockPositions.length" class="holdings-group-title">理财产品</h3>
          <article v-for="h in bank.holdings" :key="h.product_id" class="investment-product"><div class="investment-product-title"><h3>{{ h.name }}</h3><span>R{{ h.risk_level }}</span></div><dl><div><dt>持有份额</dt><dd>{{ h.units }} 份</dd></div><div><dt>当前市值</dt><dd>¥{{ money(h.value_minor) }}</dd></div></dl><div class="investment-buttons"><button @click="detailsId = h.product_id">详情</button><button :disabled="busy || processing || !!approval || !eligible(productFor(h.product_id)!)" @click="select(productFor(h.product_id)!)">{{ h.risk_level > bank.risk_level! ? '超出风险等级' : '买入' }}</button><button :disabled="busy || processing || !!approval" @click="select(productFor(h.product_id)!, true)">卖出</button></div></article>
          <template v-if="stockPositions.length">
            <h3 class="holdings-group-title">证券股票 <span>已同步</span></h3>
            <article v-for="position in stockPositions" :key="position.symbol" class="investment-product securities-holding">
              <div class="investment-product-title"><h3>{{ position.name }}</h3><span>股票 · R4</span></div>
              <p>{{ position.symbol }} · 有数证券模拟持仓</p>
              <dl><div><dt>持有股数</dt><dd>{{ position.shares }} 股</dd></div><div><dt>平均买入价</dt><dd>¥{{ (position.cost_minor / 100 / position.shares).toFixed(4) }}</dd></div><div><dt>买入成本</dt><dd>¥{{ money(position.cost_minor) }}</dd></div><div><dt>{{position.market_value_minor == null ? '成本暂估' : '当前估值'}}</dt><dd>¥{{money(position.value_minor ?? position.cost_minor)}}</dd></div><div><dt>浮动盈亏</dt><dd>{{position.profit_minor == null ? '暂无行情' : (position.profit_minor > 0 ? '+' : '') + '¥' + money(position.profit_minor)}} {{position.profit_percent == null ? '' : '(' + (position.profit_percent > 0 ? '+' : '') + position.profit_percent.toFixed(2) + '%)'}}</dd></div></dl><p v-if="position.source" class="stock-valuation-source">{{position.price_kind}} · {{position.quote_date}} · {{position.source}}{{position.valuation_stale ? ' · 上次可用估值' : ''}}</p>
              <p>持仓数量按成交记录同步，估值随证券平台行情更新。</p><div class="investment-buttons"><button :disabled="busy || processing || !!approval" @click="selectStock(position)">卖出股票</button></div>
            </article>
          </template>
        </section>
        <section v-else role="tabpanel" aria-label="理财市场">
          <article v-for="p in data.products" :key="p.id" class="investment-product"><div class="investment-product-title"><h3>{{ p.name }}</h3><span :class="{ excessive: !eligible(p) }">R{{ p.risk_level }}</span></div><p>{{ p.description }}</p><dl><div><dt>起购金额</dt><dd>¥{{ money(p.min_purchase_minor) }}</dd></div><div><dt>模拟净值</dt><dd>¥{{ p.nav }}</dd></div></dl><div class="investment-buttons"><button @click="detailsId = p.id">详情</button><button :disabled="!eligible(p) || busy || processing || !!approval" @click="select(p)">{{ eligible(p) ? '买入' : '超出风险等级' }}</button><button v-if="bank.holdings.some(h => h.product_id === p.id)" :disabled="busy || processing || !!approval" @click="select(p, true)">卖出</button></div></article>
        </section>
      </template>
      <p class="investment-footnote">所有产品与资金均为模拟数据。买卖需核对后确认；超出测评等级的产品无法买入，已有持仓仍可卖出。</p>
    </div>
    <div v-if="action || stockAction || approval" class="investment-dialog-backdrop">
      <section class="investment-dialog" role="dialog" aria-modal="true" :aria-label="approval ? '核对投资操作' : '填写投资操作'">
        <template v-if="approval"><h3>{{ approval.data.title }}</h3><dl><div v-for="(field, index) in (approval.data.fields as Field[])" :key="index"><dt>{{ field.label }}</dt><dd>{{ field.value }}</dd></div></dl><p>确认后执行，确认卡 15 分钟有效。</p><div class="investment-buttons"><button :disabled="processing" @click="decide('cancel')">取消操作</button><button class="investment-primary" :disabled="processing" @click="decide('confirm')">{{ processing ? '处理中…' : '确认执行' }}</button></div></template>
        <form v-else-if="stockAction" @submit.prevent="submitStockSale"><h3>卖出股票 · {{ stockAction.name }}</h3><p>{{ stockAction.symbol }} · 当前持有 {{ stockAction.shares }} 股</p><label>卖出股数<input v-model="stockShares" inputmode="numeric" required :disabled="processing" /></label><label>到账账户<select v-model="accountId" required :disabled="processing"><option value="" disabled>请选择到账账户</option><option v-for="account in bank.accounts" :key="account.id" :value="account.id">{{ account.name }}</option></select></label><p>按最新可用交易日收盘价模拟卖出；报价后核对，再验证密码及短信。</p><div class="investment-buttons"><button type="button" :disabled="processing" @click="stockAction=null">返回</button><button type="submit" class="investment-primary" :disabled="processing || busy">{{ processing?'正在获取报价…':'核对股票卖出' }}</button></div></form>
        <form v-else-if="action" @submit.prevent="submitTrade"><h3>{{ action.sell ? '卖出' : '买入' }} · {{ action.product.name }}</h3><p v-if="action.sell">可卖出 {{ holding?.units || '0' }} 份</p><p v-else>¥{{ money(action.product.min_purchase_minor) }} 起购 · R{{ action.product.risk_level }}</p><label>{{ action.sell ? '卖出份额' : '买入金额（元）' }}<input v-model="amount" inputmode="decimal" required :disabled="processing" /></label><label v-if="action.sell">到账账户<select v-model="accountId" required :disabled="processing"><option value="" disabled>请选择账户</option><option v-for="account in bank.accounts" :key="account.id" :value="account.id">{{ account.name }} · ¥{{ money(account.balance_minor) }}</option></select></label><div class="investment-buttons"><button type="button" :disabled="processing" @click="action = null">返回</button><button class="investment-primary" :disabled="processing || busy" type="submit">核对{{ action.sell ? '卖出' : '买入' }}</button></div></form>
        <p v-if="error" class="investment-error" role="alert">{{ error }}</p>
      </section>
    </div>
    <ProductDetails v-if="detailsId" :product-id="detailsId" @close="detailsId = ''" />
  </aside>
</template>

<style scoped>
.holdings-group-title{display:flex;align-items:center;justify-content:space-between;font-size:13px;margin:22px 0 12px;color:#315c4e}.holdings-group-title span{font-size:11px;font-weight:400;color:#648273}.securities-holding>p:last-child{font-size:11px;line-height:1.7}
.stock-valuation-source{font-size:10px;color:#89917e;line-height:1.6;overflow-wrap:anywhere}
</style>
