<script setup lang="ts">
import { ref } from 'vue';
import { Star } from '@lucide/vue';
const props = defineProps<{ halfStars: number | null; saving: boolean }>();
const emit = defineEmits<{ rate: [halfStars: number] }>();
const preview = ref<number | null>(null);
function keyboard(event: KeyboardEvent) {
  if (!['ArrowLeft','ArrowDown','ArrowRight','ArrowUp','Home','End'].includes(event.key)) return;
  event.preventDefault();
  const delta = ['ArrowLeft','ArrowDown'].includes(event.key) ? -1 : 1;
  const value = event.key === 'Home' ? 1 : event.key === 'End' ? 10 : Math.max(1, Math.min(10, (props.halfStars || 0) + delta));
  if (!props.saving) { preview.value=value; emit('rate', value); }
}
</script>

<template>
  <section class="result-card handoff-rating" aria-label="人工服务评分">
    <span class="small-eyebrow">人工服务 · 演示</span>
    <h3>请给人工打分。</h3>
    <div class="rating-stars" role="radiogroup" aria-label="人工评分，支持半星" @mouseleave="preview=null" @keydown="keyboard">
      <span v-for="star in 5" :key="star" class="rating-star">
        <Star class="star-outline" :size="34" aria-hidden="true" />
        <span class="star-fill" :style="{width: Math.min(100, Math.max(0, ((preview ?? halfStars ?? 0) - (star-1)*2)*50))+'%'}"><Star :size="34" fill="currentColor" aria-hidden="true" /></span>
        <button v-for="half in 2" :key="half" type="button" role="radio" :aria-label="`${((star-1)*2+half)/2}星`" :aria-checked="halfStars === (star-1)*2+half" :title="`${((star-1)*2+half)/2}星`" :disabled="saving" :class="['star-hit',half===1?'left-half':'right-half']" @mouseenter="preview=(star-1)*2+half" @focus="preview=(star-1)*2+half" @blur="preview=null" @click="emit('rate',(star-1)*2+half)"></button>
      </span>
    </div>
    <p class="card-note" role="status">{{saving?'正在保存…':halfStars ? `已评分 ${halfStars/2} 星 · 仅记录在本次对话` : '点击星星评分，支持半星。'}}</p>
  </section>
</template>

<style scoped>
.rating-stars{display:flex;gap:9px;width:fit-content;margin:18px 0 8px}.rating-star{position:relative;display:block;width:34px;height:40px;color:#d7ddd4}.star-outline{position:absolute;left:0;top:3px}.star-fill{position:absolute;left:0;top:3px;overflow:hidden;color:#dda436;pointer-events:none;height:34px}.star-fill svg{display:block;min-width:34px}.star-hit{position:absolute;top:0;bottom:0;width:50%;padding:0;border:0;background:transparent;cursor:pointer;border-radius:4px}.left-half{left:0}.right-half{right:0}.star-hit:focus-visible{outline:2px solid #4b7250;outline-offset:2px}.star-hit:disabled{cursor:wait}
</style>
