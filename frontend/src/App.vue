<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from "vue";
import {
  ArrowDown,
  ArrowRight,
  ArrowUp,
  ArrowUpRight,
  ChartNoAxesCombined,
  Check,
  ChevronDown,
  CircleAlert,
  Command,
  Link2,
  MessageCircle,
  PanelLeft,
  Plus,
  Settings2,
  ShieldCheck,
  Sparkles,
  SquarePen,
  Wallet,
  X,
} from "@lucide/vue";
import BankPanel from "./components/BankPanel.vue";
import ConversationPanel from "./components/ConversationPanel.vue";
import MessageCard from "./components/MessageCard.vue";
import { api, timeLabel } from "./types";
import type {
  AppConfig,
  BankSnapshot,
  Bootstrap,
  Conversation,
  InspectTool,
  Message,
  Receipt,
  Task,
} from "./types";

const messages = ref<Message[]>([]);
const conversations = ref<Conversation[]>([]);
const conversationId = ref("");
const historyOpen = ref(window.innerWidth >= 1120);
const managing = ref(false);
const inspecting = ref(false);
const drafts = new Map<string, string>();
const bank = ref<BankSnapshot | null>(null);
const config = ref<AppConfig | null>(null);
const activeTask = ref<Task | null>(null);
const draft = ref("");
const loading = ref(true);
const error = ref("");
const connected = ref(false);
const submitting = ref(false);
const resetting = ref(false);
const deciding = ref<string | null>(null);
const panelOpen = ref(false);
const settingsOpen = ref(false);
const bindingCode = ref("");
const bindingBusy = ref(false);
const chatViewport = ref<HTMLElement | null>(null);
const textarea = ref<HTMLTextAreaElement | null>(null);
const showScrollDown = ref(false);
const taskStates = new Map<string, string>();
let events: EventSource | null = null;
let bootstrapVersion = 0;
let listRefreshTimer: number | undefined;

const navigationBusy = computed(
  () =>
    loading.value ||
    submitting.value ||
    !!deciding.value ||
    resetting.value ||
    managing.value ||
    inspecting.value,
);
const currentConversation = computed(() =>
  conversations.value.find((item) => item.id === conversationId.value),
);

const busy = computed(
  () =>
    submitting.value ||
    ["queued", "running"].includes(activeTask.value?.status || ""),
);
const pending = computed(
  () => activeTask.value?.status === "waiting_confirmation",
);
const welcome = computed(() => messages.value.length <= 1);
const examples = [
  {
    icon: ChartNoAxesCombined,
    title: "看看我的账单",
    text: "这个月的钱都花在哪了？有没有值得注意的扣费？",
    hint: "让收支一目了然",
  },
  {
    icon: ArrowUpRight,
    title: "帮我转一笔钱",
    text: "给张三转 200 元，备注晚餐 AA。",
    hint: "确认后，交给我处理",
  },
  {
    icon: Sparkles,
    title: "整理自动续费",
    text: "查一下我的订阅支出，取消星河视频会员，再转 500 元到储蓄账户。",
    hint: "把几件小事一起办好",
  },
];

async function scrollToBottom(force = false) {
  await nextTick();
  if (chatViewport.value && (force || !showScrollDown.value))
    chatViewport.value.scrollTo({
      top: chatViewport.value.scrollHeight,
      behavior: force ? "smooth" : "auto",
    });
}
function onScroll() {
  const el = chatViewport.value;
  if (el)
    showScrollDown.value =
      el.scrollHeight - el.scrollTop - el.clientHeight > 160;
}
function upsertMessage(message: Message) {
  if (message.conversation_id !== conversationId.value) return;
  const index = messages.value.findIndex((item) => item.id === message.id);
  if (index >= 0) messages.value[index] = message;
  else messages.value.push(message);
  void scrollToBottom(message.role === "user");
}
async function refreshBank() {
  try {
    bank.value = await api<BankSnapshot>("/bank");
  } catch (e) {
    error.value = (e as Error).message;
  }
}
async function refreshConversations() {
  try {
    conversations.value = await api<Conversation[]>("/conversations");
  } catch (e) {
    error.value = (e as Error).message;
  }
}
function openEvents(after: number, id: string) {
  events?.close();
  const source = new EventSource(
    `/api/events?after=${after}&conversation_id=${encodeURIComponent(id)}`,
  );
  events = source;
  const isCurrent = () => events === source && conversationId.value === id;
  source.onopen = () => {
    if (!isCurrent()) return;
    connected.value = true;
  };
  source.onerror = () => {
    if (!isCurrent()) return;
    connected.value = false;
  };
  source.addEventListener(
    "message",
    (e) => isCurrent() && upsertMessage(JSON.parse((e as MessageEvent).data)),
  );
  source.addEventListener("task", (e) => {
    if (!isCurrent()) return;
    const task = JSON.parse((e as MessageEvent).data) as Task;
    if (task.conversation_id !== id) return;
    taskStates.set(task.id, task.status);
    if (["queued", "running", "waiting_confirmation"].includes(task.status))
      activeTask.value = task;
    else if (activeTask.value?.id === task.id) activeTask.value = null;
    void scrollToBottom();
  });
  source.addEventListener("bank.updated", () => {
    if (!isCurrent()) return;
    void refreshBank();
  });
  source.addEventListener("binding.updated", () => {
    if (!isCurrent()) return;
    if (config.value) config.value.feishu_bound = true;
  });
  source.addEventListener("conversations.updated", (e) => {
    if (!isCurrent()) return;
    const change = JSON.parse((e as MessageEvent).data) as {
      conversation_id: string;
      deleted?: boolean;
    };
    if (change.deleted && change.conversation_id === conversationId.value) {
      void bootstrap();
      return;
    }
    window.clearTimeout(listRefreshTimer);
    listRefreshTimer = window.setTimeout(() => {
      void refreshConversations();
    }, 100);
  });
  source.addEventListener("reset", () => {
    if (!isCurrent()) return;
    drafts.clear();
    draft.value = "";
    void bootstrap();
  });
}
async function bootstrap(id?: string) {
  const version = ++bootstrapVersion;
  if (conversationId.value) drafts.set(conversationId.value, draft.value);
  events?.close();
  events = null;
  connected.value = false;
  loading.value = true;
  try {
    const initial = await api<Bootstrap>(
      "/bootstrap" + (id ? `?conversation_id=${encodeURIComponent(id)}` : ""),
    );
    if (version !== bootstrapVersion) return;
    conversationId.value = initial.conversation_id;
    conversations.value = initial.conversations;
    draft.value = drafts.get(initial.conversation_id) || "";
    messages.value = initial.messages;
    bank.value = initial.bank;
    config.value = initial.config;
    activeTask.value = initial.active_task;
    taskStates.clear();
    if (initial.active_task)
      taskStates.set(initial.active_task.id, initial.active_task.status);
    error.value = "";
    openEvents(initial.event_id, initial.conversation_id);
    showScrollDown.value = false;
    resizeInput();
    void scrollToBottom(true);
  } catch (e) {
    if (version !== bootstrapVersion) return;
    error.value =
      (e as Error).message || "暂时无法连接银行服务，请确认后端已启动。";
  } finally {
    if (version === bootstrapVersion) loading.value = false;
  }
}
async function selectConversation(id: string) {
  if (navigationBusy.value || id === conversationId.value) return;
  await bootstrap(id);
  if (window.innerWidth < 1120) historyOpen.value = false;
}
async function createConversation() {
  if (navigationBusy.value) return;
  managing.value = true;
  try {
    const created = await api<{ conversation_id: string }>(
      "/conversations",
      {},
    );
    await bootstrap(created.conversation_id);
    if (window.innerWidth < 1120) historyOpen.value = false;
    textarea.value?.focus();
  } catch (e) {
    error.value = (e as Error).message;
  } finally {
    managing.value = false;
  }
}
async function renameConversation(id: string, title: string) {
  managing.value = true;
  try {
    await api(`/conversations/${id}`, { title }, "PATCH");
    await refreshConversations();
  } catch (e) {
    error.value = (e as Error).message;
  } finally {
    managing.value = false;
  }
}
async function deleteConversation(id: string) {
  managing.value = true;
  try {
    const result = await api<{ conversation_id: string }>(
      `/conversations/${id}`,
      undefined,
      "DELETE",
    );
    drafts.delete(id);
    if (id === conversationId.value) {
      draft.value = "";
      await bootstrap(result.conversation_id);
    } else await refreshConversations();
  } catch (e) {
    error.value = (e as Error).message;
  } finally {
    managing.value = false;
  }
}
async function send(text = draft.value) {
  if (!text.trim() || busy.value || navigationBusy.value || !connected.value)
    return;
  submitting.value = true;
  error.value = "";
  const previous = draft.value;
  draft.value = "";
  resizeInput();
  try {
    const result = await api<{ task_id: string }>("/messages", {
      text: text.trim(),
      conversation_id: conversationId.value,
    });
    const knownStatus = taskStates.get(result.task_id);
    if (
      !knownStatus ||
      ["queued", "running", "waiting_confirmation"].includes(knownStatus)
    )
      activeTask.value = {
        id: result.task_id,
        status: knownStatus || "queued",
      };
    void scrollToBottom(true);
  } catch (e) {
    error.value = (e as Error).message;
    draft.value = previous || text;
    resizeInput();
  } finally {
    submitting.value = false;
    textarea.value?.focus();
  }
}
async function decide(id: string, decision: "confirm" | "cancel") {
  if (navigationBusy.value) return;
  deciding.value = id;
  error.value = "";
  try {
    const result = await api<{
      status: string;
      receipt?: Receipt;
      error?: string;
    }>(`/approvals/${id}/decision`, {
      decision,
      conversation_id: conversationId.value,
    });
    const message = messages.value.find((item) => item.data.approval_id === id);
    if (message)
      message.data = {
        ...message.data,
        status: result.status,
        receipt: result.receipt,
        error: result.error,
      };
    await refreshBank();
  } catch (e) {
    error.value = (e as Error).message;
  } finally {
    deciding.value = null;
  }
}
async function inspect(tool: InspectTool) {
  if (navigationBusy.value) return;
  inspecting.value = true;
  try {
    upsertMessage(
      await api<Message>("/sandbox/inspect", {
        tool,
        conversation_id: conversationId.value,
      }),
    );
    if (window.innerWidth < 900) panelOpen.value = false;
    void scrollToBottom(true);
  } catch (e) {
    error.value = (e as Error).message;
  } finally {
    inspecting.value = false;
  }
}
async function reset() {
  resetting.value = true;
  try {
    events?.close();
    await api("/sandbox/reset", { confirmation: "RESET" });
    drafts.clear();
    draft.value = "";
    await bootstrap();
  } catch (e) {
    error.value = (e as Error).message;
    await bootstrap();
  } finally {
    resetting.value = false;
  }
}
async function generateBinding() {
  bindingBusy.value = true;
  try {
    const result = await api<{ instruction: string }>(
      "/feishu/binding-code",
      {},
    );
    bindingCode.value = result.instruction;
  } catch (e) {
    error.value = (e as Error).message;
  } finally {
    bindingBusy.value = false;
  }
}
function resizeInput() {
  void nextTick(() => {
    if (textarea.value) {
      textarea.value.style.height = "auto";
      textarea.value.style.height =
        Math.min(textarea.value.scrollHeight, 160) + "px";
    }
  });
}
function onKeydown(event: KeyboardEvent) {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    void send();
  }
}
function toggleHistory() {
  historyOpen.value = !historyOpen.value;
  if (historyOpen.value && window.innerWidth < 1120) panelOpen.value = false;
}
function toggleBank() {
  panelOpen.value = !panelOpen.value;
  if (panelOpen.value && window.innerWidth < 1120) historyOpen.value = false;
}
function globalKeydown(event: KeyboardEvent) {
  if (event.key === "Escape") {
    settingsOpen.value = false;
    panelOpen.value = false;
    historyOpen.value = false;
  }
}
onMounted(() => {
  void bootstrap();
  document.addEventListener("keydown", globalKeydown);
});
onBeforeUnmount(() => {
  bootstrapVersion++;
  window.clearTimeout(listRefreshTimer);
  events?.close();
  document.removeEventListener("keydown", globalKeydown);
});
</script>

<template>
  <div class="app-shell">
    <header class="app-header">
      <a href="/" class="brand" aria-label="有数首页"
        ><span class="brand-symbol" aria-hidden="true"
          ><i></i><i></i><i></i></span
        ><span class="brand-name">有数<span>AI BANKING</span></span></a
      >
      <div class="header-middle">
        <span class="status-dot"></span>你的个人银行助手
      </div>
      <div class="header-actions">
        <button
          class="icon-button history-toggle"
          aria-label="历史会话"
          :aria-expanded="historyOpen"
          @click="toggleHistory"
        >
          <PanelLeft :size="18" />
        </button>
        <button
          class="panel-toggle new-chat-trigger"
          aria-label="新建会话"
          :disabled="navigationBusy"
          @click="createConversation"
        >
          <SquarePen :size="16" /><span>新会话</span>
        </button>
        <button
          class="panel-toggle"
          :class="{ selected: panelOpen }"
          :aria-expanded="panelOpen"
          @click="toggleBank"
        >
          <Wallet :size="16" /><span>模拟银行</span
          ><ChevronDown :size="14" :class="{ rotated: panelOpen }" /></button
        ><button
          class="icon-button settings-trigger"
          aria-label="连接设置"
          @click="settingsOpen = true"
        >
          <Settings2 :size="18" />
        </button>
      </div>
    </header>

    <main class="workspace" :class="{ 'panel-is-open': panelOpen }">
      <button
        v-if="historyOpen"
        class="history-backdrop"
        aria-label="关闭会话列表"
        @click="historyOpen = false"
      ></button>
      <ConversationPanel
        v-if="historyOpen"
        :conversations="conversations"
        :selected-id="conversationId"
        :disabled="navigationBusy"
        @create="createConversation"
        @select="selectConversation"
        @rename="renameConversation"
        @delete="deleteConversation"
        @close="historyOpen = false"
      />
      <section class="chat-workspace" aria-label="银行助手对话">
        <div
          v-if="!welcome && currentConversation"
          class="current-conversation-title"
        >
          <span>{{ currentConversation.title }}</span
          ><small>对话记录已保存</small>
        </div>
        <div
          v-if="!loading && !config?.model_configured"
          class="connection-notice"
        >
          <span
            ><span class="notice-dot"></span
            >模拟银行已就绪，连接模型后即可开始对话</span
          ><button @click="settingsOpen = true">
            连接模型 <ArrowRight :size="14" />
          </button>
        </div>
        <div v-else-if="!loading && !connected" class="connection-notice">
          <span><CircleAlert :size="14" />连接暂时中断，正在恢复…</span
          ><button @click="bootstrap()">重新连接</button>
        </div>
        <div ref="chatViewport" class="chat-viewport" @scroll="onScroll">
          <div v-if="loading" class="loading-state">
            <span class="loading-orbit"></span>
            <p>正在准备你的银行助手…</p>
          </div>
          <div v-else-if="!bank" class="loading-state">
            <CircleAlert :size="28" />
            <p>银行服务暂未连接</p>
            <button class="primary-button" @click="bootstrap()">
              重新连接
            </button>
          </div>
          <div v-else class="conversation" :class="{ 'is-welcome': welcome }">
            <section v-if="welcome" class="welcome-section">
              <div class="welcome-kicker">
                <span class="mini-brand" aria-hidden="true"
                  ><i></i><i></i><i></i></span
                >少一点操作，多一点从容
              </div>
              <h1>把银行的事，<br /><span>交给一句话。</span></h1>
              <p class="welcome-description">
                查清每一笔收支，安排下一步计划。<br
                  class="mobile-break"
                />和有数聊聊，剩下的事一起办好。
              </p>
              <div class="example-list">
                <button
                  v-for="example in examples"
                  :key="example.title"
                  :disabled="busy || navigationBusy || !connected"
                  @click="send(example.text)"
                >
                  <span class="example-icon"
                    ><component
                      :is="example.icon"
                      :size="20"
                      :stroke-width="1.6" /></span
                  ><span
                    ><strong>{{ example.title }}</strong
                    ><small>{{ example.hint }}</small></span
                  ><ArrowUpRight :size="17" class="example-arrow" />
                </button>
              </div>
              <div class="welcome-footnote">
                <ShieldCheck :size="14" />每次重要操作，都由你来确认。
              </div>
            </section>
            <div
              v-else
              class="message-list"
              role="log"
              aria-label="对话记录"
              aria-live="polite"
            >
              <article
                v-for="message in messages"
                :key="message.id"
                class="message"
                :class="[
                  `message-${message.role}`,
                  { 'message-error': message.kind === 'error' },
                ]"
              >
                <div
                  v-if="message.role === 'assistant'"
                  class="assistant-avatar"
                  aria-hidden="true"
                >
                  <span class="mini-brand"><i></i><i></i><i></i></span>
                </div>
                <div class="message-content">
                  <div class="message-meta">
                    <span>{{
                      message.role === "assistant" ? "有数" : "你"
                    }}</span
                    ><small v-if="message.data.channel === 'feishu'"
                      >来自飞书</small
                    ><time :datetime="message.created_at">{{
                      timeLabel(message.created_at)
                    }}</time>
                  </div>
                  <MessageCard
                    v-if="
                      message.kind === 'data' || message.kind === 'approval'
                    "
                    :message="message"
                    :deciding="deciding"
                    @decision="decide"
                  />
                  <div v-else class="message-text">
                    <CircleAlert v-if="message.kind === 'error'" :size="16" />{{
                      message.text
                    }}
                  </div>
                </div>
              </article>
            </div>
            <div v-if="busy" class="thinking-row" role="status">
              <span class="thinking-dots"><i></i><i></i><i></i></span
              >正在理解与处理你的需求…
            </div>
          </div>
        </div>
        <div class="composer-area">
          <button
            v-if="showScrollDown"
            class="scroll-bottom"
            aria-label="滚动到最新消息"
            @click="scrollToBottom(true)"
          >
            <ArrowDown :size="17" />
          </button>
          <div v-if="error" class="error-banner" role="alert">
            <CircleAlert :size="16" /><span>{{ error }}</span
            ><button aria-label="关闭提示" @click="error = ''">
              <X :size="15" />
            </button>
          </div>
          <p v-if="pending" class="pending-hint">
            有一项操作等待确认。发送新消息会取消尚未确认的操作。
          </p>
          <form
            class="composer"
            :class="{ 'composer-busy': busy }"
            @submit.prevent="send()"
          >
            <label for="chat-input" class="sr-only"
              >向有数描述你的银行需求</label
            >
            <textarea
              id="chat-input"
              ref="textarea"
              v-model="draft"
              maxlength="4000"
              rows="1"
              placeholder="说说你想办的事，比如“这个月花了多少钱？”"
              :disabled="loading"
              @input="resizeInput"
              @keydown="onKeydown"
            ></textarea>
            <div class="composer-bottom">
              <div class="composer-tools">
                <span class="tool-label"
                  ><span
                    class="status-dot"
                    :class="{ muted: !config?.model_configured }"
                  ></span
                  >有数助手</span
                ><span class="tool-divider"></span
                ><button
                  type="button"
                  class="context-button"
                  :aria-expanded="panelOpen"
                  @click="toggleBank"
                >
                  <Plus :size="15" />银行数据
                </button>
              </div>
              <button
                type="submit"
                class="send-button"
                :disabled="
                  !draft.trim() || busy || navigationBusy || !connected
                "
                aria-label="发送消息"
              >
                <ArrowUp :size="20" />
              </button>
            </div>
          </form>
          <div class="composer-caption">
            <span>模拟环境 · 所有资金与交易均为演示数据</span
            ><span class="keyboard-hint"
              ><Command :size="11" />Enter 发送 · Shift + Enter 换行</span
            >
          </div>
        </div>
      </section>
      <button
        v-if="panelOpen"
        class="panel-backdrop"
        aria-label="关闭银行面板"
        @click="panelOpen = false"
      ></button>
      <BankPanel
        v-if="panelOpen && bank"
        :bank="bank"
        :busy="busy"
        :resetting="resetting"
        @close="panelOpen = false"
        @inspect="inspect"
        @reset="reset"
      />
    </main>

    <div
      v-if="settingsOpen"
      class="modal-backdrop"
      @click.self="settingsOpen = false"
    >
      <section
        role="dialog"
        aria-modal="true"
        aria-labelledby="settings-title"
        class="settings-dialog"
        tabindex="-1"
      >
        <div class="dialog-heading">
          <div>
            <span class="small-eyebrow">让助手准备就绪</span>
            <h2 id="settings-title">连接设置</h2>
          </div>
          <button
            class="icon-button"
            aria-label="关闭设置"
            @click="settingsOpen = false"
          >
            <X :size="20" />
          </button>
        </div>
        <div class="setting-section">
          <div class="setting-title">
            <Sparkles :size="18" />
            <h3>大模型</h3>
            <span
              class="connection-state"
              :class="{ ready: config?.model_configured }"
              ><Check v-if="config?.model_configured" :size="12" />{{
                config?.model_configured ? "已配置" : "未配置"
              }}</span
            >
          </div>
          <p v-if="config?.model_configured">
            当前模型：<strong>{{ config.model_name }}</strong
            >。实际连接结果会在请求时显示。
          </p>
          <template v-else
            ><p>
              在项目根目录将 <code>.env.example</code> 复制为
              <code>.env</code>，填写兼容接口配置，然后重启后端。
            </p>
            <pre>
LLM_BASE_URL=https://你的服务地址/v1
LLM_API_KEY=你的密钥
LLM_MODEL=你的模型名</pre
            >
            <p class="setting-note">
              接口需要支持工具调用。密钥保存在本地服务端。
            </p></template
          >
        </div>
        <div class="setting-section">
          <div class="setting-title">
            <MessageCircle :size="18" />
            <h3>飞书私聊</h3>
            <span
              class="connection-state"
              :class="{ ready: config?.feishu_bound }"
              >{{ config?.feishu_bound ? "已绑定" : "可选" }}</span
            >
          </div>
          <p>在飞书里操作同一套模拟账户。接入步骤见项目的飞书配置说明。</p>
          <button
            v-if="config?.feishu_enabled"
            class="outline-button"
            :disabled="bindingBusy"
            @click="generateBinding"
          >
            <Link2 :size="15" />{{
              bindingBusy ? "正在生成…" : "生成私聊绑定码"
            }}
          </button>
          <p v-else class="setting-note">
            当前未启用，Web 可以独立使用全部能力。
          </p>
          <div v-if="bindingCode" class="binding-code">
            {{ bindingCode }}<small>10 分钟内有效，仅可使用一次。</small>
          </div>
        </div>
        <div class="dialog-footer">
          <ShieldCheck :size="15" /><span>操作凭证与确认均由后端校验</span
          ><button class="quiet-button" @click="settingsOpen = false">
            知道了
          </button>
        </div>
      </section>
    </div>
  </div>
</template>
