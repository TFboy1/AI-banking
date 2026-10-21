<script setup lang="ts">
import { nextTick, ref } from "vue";
import { Check, MessageSquare, Pencil, Plus, Trash2, X, Phone } from "@lucide/vue";
import type { Conversation } from "../types";

defineProps<{
  conversations: Conversation[];
  selectedId: string;
  disabled: boolean;
}>();
const emit = defineEmits<{
  create: [];
  select: [id: string];
  rename: [id: string, title: string];
  delete: [id: string];
  close: [];
  handoff: [];
}>();
const editing = ref("");
const title = ref("");
const deleting = ref("");

async function startRename(item: Conversation) {
  editing.value = item.id;
  title.value = item.title;
  deleting.value = "";
  await nextTick();
  const input = document.getElementById(
    `title-${item.id}`,
  ) as HTMLInputElement | null;
  input?.focus();
  input?.select();
}
function saveTitle(id: string) {
  if (!title.value.trim()) return;
  emit("rename", id, title.value.trim());
  editing.value = "";
}
function dateLabel(value: string) {
  return new Date(value).toLocaleDateString("zh-CN", {
    month: "numeric",
    day: "numeric",
    timeZone: "Asia/Shanghai",
  });
}
</script>

<template>
  <aside class="conversation-panel" aria-label="历史会话">
    <div class="history-heading">
      <span
        >我的会话 <small>{{ conversations.length }}</small></span
      >
      <button
        class="icon-button"
        aria-label="收起会话列表"
        @click="emit('close')"
      >
        <X :size="17" />
      </button>
    </div>
    <button
      class="new-conversation-button"
      :disabled="disabled"
      @click="emit('create')"
    >
      <Plus :size="17" />新建会话
    </button>
    <div class="history-list">
      <div
        v-for="item in conversations"
        :key="item.id"
        class="history-item"
        :class="{ active: item.id === selectedId }"
      >
        <form
          v-if="editing === item.id"
          class="history-rename"
          @submit.prevent="saveTitle(item.id)"
        >
          <label :for="`title-${item.id}`" class="sr-only">会话名称</label>
          <input
            :id="`title-${item.id}`"
            v-model="title"
            maxlength="60"
            :disabled="disabled"
            @keydown.esc="editing = ''"
          />
          <button
            class="icon-button"
            :disabled="disabled || !title.trim()"
            aria-label="保存会话名称"
          >
            <Check :size="16" />
          </button>
          <button
            type="button"
            class="icon-button"
            aria-label="取消重命名"
            @click="editing = ''"
          >
            <X :size="16" />
          </button>
        </form>
        <template v-else>
          <button
            class="history-select"
            :disabled="disabled"
            :aria-current="item.id === selectedId ? 'true' : undefined"
            @click="emit('select', item.id)"
          >
            <span class="history-title"
              ><MessageSquare :size="14" /><strong>{{
                item.title
              }}</strong></span
            >
            <span class="history-preview">{{
              item.preview || "开始一个新的话题"
            }}</span>
          </button>
          <div class="history-meta">
            <span
              v-if="item.task_status === 'waiting_confirmation'"
              class="history-pending"
              >待确认</span
            >
            <span v-else-if="item.unread_count" class="history-pending">新建议 {{ item.unread_count }}</span>
            <span v-else-if="item.task_status" class="history-running"
              >处理中…</span
            >
            <time v-else :datetime="item.updated_at">{{
              dateLabel(item.updated_at)
            }}</time>
            <div class="history-actions">
              <button
                class="icon-button"
                :disabled="disabled"
                :aria-label="`重命名 ${item.title}`"
                @click="startRename(item)"
              >
                <Pencil :size="13" />
              </button>
              <button
                class="icon-button"
                :disabled="
                  disabled ||
                  ['queued', 'running'].includes(item.task_status || '')
                "
                :aria-label="`删除 ${item.title}`"
                @click="deleting = deleting === item.id ? '' : item.id"
              >
                <Trash2 :size="13" />
              </button>
            </div>
          </div>
        </template>
        <div v-if="deleting === item.id" class="history-delete" role="alert">
          <p>删除这段会话？待确认操作将取消，已执行的交易记录保留。</p>
          <div>
            <button
              :disabled="disabled"
              @click="
                emit('delete', item.id);
                deleting = '';
              "
            >
              删除会话</button
            ><button @click="deleting = ''">保留</button>
          </div>
        </div>
      </div>
    </div>
    <div class="history-footer"><p class="history-footnote">每段对话独立记录<br />共用你的模拟账户</p><button type="button" class="icon-button" aria-label="转人工" title="转人工" :disabled="disabled" @click="emit('handoff')"><Phone :size="18" /></button></div>
  </aside>
</template>

<style scoped>
.history-footer{display:flex;align-items:center;justify-content:space-between;padding-right:16px}.history-footer .history-footnote{flex:1}.history-footer .icon-button{flex-shrink:0;color:#64816c}
</style>
