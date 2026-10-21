<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import { X } from "@lucide/vue";
import { api } from "../types";

interface Day { date: string; nav: string; previous_nav: string; change: string; change_percent: string; simulated: boolean }
interface Details { product: { id: string; name: string; risk_level: number; nav: string; description: string }; history: Day[]; daily_limit_percent: string; note: string }
const props = defineProps<{ productId: string }>();
const emit = defineEmits<{ close: [] }>();
const data = ref<Details | null>(null);
const error = ref("");
const loading = ref(false);
const days = ref(90);
const selected = ref<Day | null>(null);
let request = 0;
async function load() {
  const current = ++request; loading.value = true; error.value = ""; data.value = null; selected.value = null;
  try { const value = await api<Details>(`/investments/products/${encodeURIComponent(props.productId)}/history`); if (current === request) data.value = value; }
  catch (e) { if (current === request) error.value = (e as Error).message; }
  finally { if (current === request) loading.value = false; }
}
const visible = computed(() => data.value?.history.slice(-days.value) || []);
const current = computed(() => data.value?.history.at(-1));
const low = computed(() => Math.min(...visible.value.map(d => Number(d.nav))));
const high = computed(() => Math.max(...visible.value.map(d => Number(d.nav))));
const span = computed(() => Math.max(high.value - low.value, .0002));
const points = computed(() => visible.value.map((d, i) => ({ day: d, x: 58 + i * 520 / Math.max(1, visible.value.length - 1), y: 20 + (high.value - Number(d.nav)) / span.value * 170 })));
const line = computed(() => points.value.map(p => `${p.x},${p.y}`).join(" "));
const inspected = computed(() => selected.value || current.value);
const percent = (value: string) => `${Number(value) > 0 ? '+' : ''}${Number(value).toFixed(2)}%`;
function hover(event: MouseEvent) {
  const box = (event.currentTarget as SVGSVGElement).getBoundingClientRect();
  const index = Math.round(((event.clientX - box.left) / box.width * 600 - 58) / 520 * (visible.value.length - 1));
  selected.value = visible.value[Math.max(0, Math.min(index, visible.value.length - 1))] || null;
}
watch(() => props.productId, load);
watch(days, () => { selected.value = null; });
onMounted(load);
</script>

<template>
  <Teleport to="body">
  <div class="investment-dialog-backdrop" @click.self="emit('close')" @keydown.esc="emit('close')">
    <section class="product-details" role="dialog" aria-modal="true" aria-label="理财产品详情" tabindex="-1">
      <header><div><span class="detail-eyebrow">模拟理财 · 每日净值</span><h2>{{ data?.product.name || '产品详情' }} <small v-if="data">R{{ data.product.risk_level }}</small></h2></div><button class="icon-button" aria-label="关闭产品详情" @click="emit('close')"><X :size="20" /></button></header>
      <p v-if="loading" role="status">正在加载每日数据…</p>
      <div v-if="error" role="alert"><p>{{ error }}</p><button @click="load">重试</button></div>
      <template v-if="data && current">
        <div class="detail-metrics"><div><span>最新模拟净值</span><strong>¥{{ Number(current.nav).toFixed(4) }}</strong></div><div><span>{{ current.date }} 涨跌幅</span><strong :class="{ positive: Number(current.change_percent) > 0, negative: Number(current.change_percent) < 0 }">{{ percent(current.change_percent) }}</strong></div></div>
        <p class="simulation-note">每日随机波动范围约 ±{{ data.daily_limit_percent }}%，可涨可跌。{{ data.note }}</p>
        <div class="chart-heading"><h3>净值走势图</h3><div class="chart-ranges"><button v-for="n in [7, 30, 90]" :key="n" :aria-pressed="days === n" :class="{ active: days === n }" @click="days = n">近{{ n }}日</button></div></div>
        <svg v-if="points.length" viewBox="0 0 600 225" role="img" aria-label="每日模拟净值走势图" @mousemove="hover" @mouseleave="selected = null">
          <title>{{ data.product.name }}近{{ days }}日模拟净值</title>
          <line x1="58" y1="20" x2="578" y2="20" stroke="#e3e9e2" /><line x1="58" y1="105" x2="578" y2="105" stroke="#e3e9e2" /><line x1="58" y1="190" x2="578" y2="190" stroke="#e3e9e2" />
          <text x="4" y="24">{{ high.toFixed(4) }}</text><text x="4" y="109">{{ (high - span / 2).toFixed(4) }}</text><text x="4" y="194">{{ (high - span).toFixed(4) }}</text>
          <polyline :points="line" fill="none" stroke="#28775c" stroke-width="2.3" stroke-linejoin="round" />
          <circle v-for="point in points" :key="point.day.date" :cx="point.x" :cy="point.y" :r="inspected?.date === point.day.date ? 4 : 2" fill="#28775c" tabindex="0" role="button" :aria-label="`${point.day.date} 净值 ${point.day.nav} 涨跌幅 ${percent(point.day.change_percent)}`" @focus="selected = point.day" @click="selected = point.day" />
          <text x="58" y="215">{{ visible[0]?.date }}</text><text x="578" y="215" text-anchor="end">{{ visible.at(-1)?.date }}</text>
        </svg>
        <p v-if="inspected" class="chart-reading">{{ inspected.date }}　净值 ¥{{ Number(inspected.nav).toFixed(4) }}　涨跌幅 {{ percent(inspected.change_percent) }}</p>
        <h3>每日数据 · 最近{{ visible.length }}日</h3>
        <div class="history-table"><table><thead><tr><th>日期</th><th>模拟净值</th><th>涨跌额</th><th>涨跌幅</th></tr></thead><tbody><tr v-for="d in [...visible].reverse()" :key="d.date"><td>{{ d.date }}</td><td>{{ Number(d.nav).toFixed(4) }}</td><td>{{ Number(d.change) > 0 ? '+' : '' }}{{ Number(d.change).toFixed(4) }}</td><td :class="{ positive: Number(d.change_percent) > 0, negative: Number(d.change_percent) < 0 }">{{ percent(d.change_percent) }}</td></tr></tbody></table></div>
      </template>
    </section>
  </div>
  </Teleport>
</template>

<style scoped>
.product-details { width: min(720px, 100%); max-height: calc(100dvh - 40px); overflow: auto; box-sizing: border-box; background: #fffefa; border-radius: 18px; padding: 24px; box-shadow: 0 20px 70px #18241333; }
header,.chart-heading { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
h2 { margin: 8px 0; font-size: 21px; } h2 small { font-size: 12px; color: #6c806c; } h3 { font-size: 14px; margin: 18px 0 12px; }
.detail-eyebrow,.detail-metrics span { color: #6d806e; font-size: 12px; }
.detail-metrics { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin: 20px 0 12px; } .detail-metrics div { display: grid; gap: 7px; } .detail-metrics strong { font-size: 25px; }
.simulation-note { color: #71816d; font-size: 12px; line-height: 1.7; background: #f0f4eb; padding: 10px; border-radius: 8px; }
.chart-ranges { display: flex; gap: 5px; } .chart-ranges button { border: 1px solid #dce5d5; border-radius: 6px; padding: 6px; background: transparent; font-size: 12px; color: #4d6e54; cursor: pointer; } .chart-ranges button.active { background: #e6eedf; }
svg { display: block; width: 100%; height: auto; } svg text { font-size: 10px; fill: #7a8b78; } circle { cursor: pointer; } circle:focus { outline: none; stroke: #163f30; stroke-width: 3; }
.chart-reading { font-size: 12px; color: #42654e; min-height: 20px; }
.history-table { max-height: 230px; overflow: auto; border: 1px solid #e2e8dd; border-radius: 9px; } table { width: 100%; border-collapse: collapse; font-size: 12px; white-space: nowrap; } th,td { text-align: right; padding: 10px 12px; border-bottom: 1px solid #e7ece3; } th:first-child,td:first-child { text-align: left; } th { position: sticky; top: 0; background: #f2f5ee; color: #647660; }
.positive { color: #b94e45; } .negative { color: #25846a; }
@media (max-width: 480px) { .product-details { padding: 16px; } h2 { font-size: 18px; } .detail-metrics strong { font-size: 21px; } .chart-heading { align-items: flex-start; flex-direction: column; gap: 0; } th,td { padding: 9px 7px; } }
</style>
