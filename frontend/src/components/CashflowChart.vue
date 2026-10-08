<script setup lang="ts">
import { computed } from "vue";
import type { PlanVariant } from "../types";
import { money } from "../types";

const props = defineProps<{ variant: PlanVariant; asOf: string }>();
const left = 34;
const right = 612;
const top = 18;
const bottom = 148;
const formatDate = (date: string) => date.slice(5).replace("-", "/");

const chart = computed(() => {
  const result = props.variant.result;
  const events = result.timeline || [];
  const values = [result.opening_available_minor ?? result.lowest_available_minor, ...events.map((event) => event.available_minor)];
  const minimum = Math.min(0, ...values);
  const maximum = Math.max(0, ...values);
  const gap = Math.max(1, maximum - minimum);
  const padding = gap * 0.08;
  const y = (value: number) => top + ((maximum + padding - value) / (gap + 2 * padding)) * (bottom - top);
  const x = (index: number) => left + ((right - left) * index) / Math.max(1, events.length);
  const points = values.map((value, index) => ({ x: x(index), y: y(value), value }));
  let path = `M ${points[0].x} ${points[0].y}`;
  for (let index = 1; index < points.length; index += 1) {
    path += ` H ${points[index].x} V ${points[index].y}`;
  }
  const failure = result.first_failure;
  const matchingEvent = failure
    ? events.findIndex((event) => event.date === failure.date && event.label === failure.event && event.available_minor < 0)
    : -1;
  const failureIndex = failure?.event === "预演起点" ? 0 : matchingEvent >= 0 ? matchingEvent + 1 : -1;
  return { events, points, path, zeroY: y(0), failureIndex };
});
</script>

<template>
  <section class="cashflow-chart" :aria-label="`${variant.label}的现金流预演`">
    <div class="cashflow-chart-heading">
      <div><span>最低可自由安排</span><strong :class="{ negative: variant.result.lowest_available_minor < 0 }">¥{{ money(variant.result.lowest_available_minor) }}</strong></div>
      <span class="cashflow-chart-key">事件序列 · 自 {{ formatDate(asOf) }} 起</span>
    </div>
    <svg viewBox="0 0 640 176" role="img" :aria-label="`${variant.label}：最低可自由安排 ${money(variant.result.lowest_available_minor)} 元${variant.result.first_failure ? `，${variant.result.first_failure.date} 首次出现缺口` : '，没有资金缺口'}`">
      <line :x1="left" :x2="right" :y1="chart.zeroY" :y2="chart.zeroY" class="cashflow-zero-line" />
      <text :x="left" :y="Math.max(13, chart.zeroY - 5)" class="cashflow-zero-label">¥0</text>
      <path :d="chart.path" class="cashflow-line" />
      <circle v-for="(point, index) in chart.points" :key="index" :cx="point.x" :cy="point.y" :r="chart.failureIndex >= 0 && index === chart.failureIndex ? 6 : 3.5" :class="['cashflow-point', { negative: point.value < 0, first: index === chart.failureIndex && chart.failureIndex >= 0 }]" />
      <text :x="left" y="169" class="cashflow-axis-label">{{ formatDate(asOf) }}</text>
      <text :x="right" y="169" text-anchor="end" class="cashflow-axis-label">{{ chart.events.length ? formatDate(chart.events[chart.events.length - 1].date) : '暂无后续事件' }}</text>
    </svg>
    <p v-if="variant.result.first_failure" class="cashflow-failure">
      首次缺口：{{ formatDate(variant.result.first_failure.date) }} · {{ variant.result.first_failure.event }} · ¥{{ money(variant.result.first_failure.shortfall_minor) }}
    </p>
    <p v-else class="cashflow-safe">当前预演范围内未出现资金缺口。</p>
    <details class="cashflow-events">
      <summary>查看逐项事件（{{ chart.events.length }}）</summary>
      <ol v-if="chart.events.length">
        <li v-for="event in chart.events" :key="event.key">
          <span>{{ formatDate(event.date) }} · {{ event.label }}</span>
          <span>{{ event.kind === "income" ? "+" : "−" }}¥{{ money(event.amount_minor) }}</span>
          <strong :class="{ negative: event.available_minor < 0 }">余 ¥{{ money(event.available_minor) }}</strong>
        </li>
      </ol>
      <p v-else>这个范围内没有后续现金流事件。</p>
    </details>
  </section>
</template>
