<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import TaskPlan from "./components/TaskPlan.vue";
import { watchMarketCheckpoints } from "./marketSchedule";
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
  Settings,
  ShieldCheck,
  Sparkles,
  SquarePen,
  Square,
  Wallet,
  Users,
  X,
} from "@lucide/vue";
import AuthGate from "./components/AuthGate.vue";
import AppSecuritySettings from "./components/AppSecuritySettings.vue";
import VoiceInput from './components/VoiceInput.vue';
import ContactsPanel from "./components/ContactsPanel.vue";
import InvestmentPanel from "./components/InvestmentPanel.vue";
import BankPanel from "./components/BankPanel.vue";
import ConversationPanel from "./components/ConversationPanel.vue";
import MessageCard from "./components/MessageCard.vue";
import {compactDailyReports} from './dailyDigest';
import { api, money, timeLabel, setTransactionPinRequester, setAuthExpiredHandler, setSystemLockedHandler } from "./types";
import ManualLock from './components/ManualLock.vue';
import type {
  BirthdayNotice,
  PaymentCredentials,
  PaymentContext,
  AppConfig,
  AuthState,
  BankSnapshot,
  Bootstrap,
  Conversation,
  InspectTool,
  Message,
  Receipt,
  Task,
} from "./types";

const auth = ref<AuthState | null>(null);
const authReady = ref(false);
const systemLocked = ref(false);
const forceSms = ref(false);
const transactionPinOpen = ref(false);
const paymentFaceEnabled=ref(true);
const paymentNeedsFace=computed(()=>paymentNeedsSms.value&&paymentFaceEnabled.value);
const paymentFaceToken=ref(''),paymentFaceBusy=ref(false),paymentUsePassword=ref(false),paymentFaceBound=ref(false);
async function verifyPaymentFace(){
 paymentFaceBusy.value=true;paymentSmsError.value='';
 const expected=paymentApprovalId,account=paymentAccount.value,timing=JSON.stringify(timingBody());
 try{
  const {runFace}=await import('./face');
  const result=await runFace(()=>api('/face/challenge',{purpose:'payment',approval_id:paymentApprovalId,conversation_id:paymentConversationId,...timingBody(),...(paymentAccount.value?{account_id:paymentAccount.value}:{}),...(paymentNeedsSms.value?{pin:transactionPin.value,sms_code:paymentSmsCode.value}:{})}));
  if(!transactionPinOpen.value||expected!==paymentApprovalId||account!==paymentAccount.value||timing!==JSON.stringify(timingBody()))return;
  paymentFaceToken.value=result.face_token||'';
  if(!paymentNeedsSms.value)resolvePin('face');
 }catch(e){paymentSmsError.value=(e as Error).message;if(!paymentNeedsSms.value)paymentUsePassword.value=true;}
 finally{paymentFaceBusy.value=false;}
}
const transactionPin = ref("");
const paymentContext = ref<PaymentContext | null>(null);
const paymentAccount = ref("");
const paymentTiming=ref('now'),paymentExecution=ref('');
function timingBody(){return paymentContext.value?.schedule_allowed ? {execute_at:paymentTiming.value==='scheduled' ? paymentExecution.value+(paymentExecution.value.length===16?':00':'')+'+08:00' : ''} : {};}
function timingReady(){return paymentTiming.value!=='scheduled'||!!paymentExecution.value;}

const paymentNeedsSms = computed(() => !!paymentContext.value?.sms_required || (paymentTiming.value==='scheduled' && ['transfer','transfer_own_funds','save_to_goal','schedule_transfer'].includes(paymentContext.value?.tool||'')));
const paymentSmsCode = ref("");
const paymentSms = ref<{demo_code: string; phone_masked: string} | null>(null);
const paymentSmsBusy = ref(false);
const paymentSmsError = ref("");
let paymentApprovalId = "";
let paymentConversationId = "";
async function sendPaymentSms() {
  paymentSmsBusy.value = true; paymentSmsError.value = "";
  try { paymentSms.value = await api(`/approvals/${paymentApprovalId}/sms`, {conversation_id: paymentConversationId, ...timingBody(), ...(paymentContext.value?.account_required ? {account_id: paymentAccount.value} : {})}); }
  catch (e) { paymentSmsError.value = (e as Error).message; }
  finally { paymentSmsBusy.value = false; }
}
function paymentAccountChanged() { paymentFaceToken.value='';transactionPin.value = ""; paymentSmsCode.value = ""; paymentSms.value = null; paymentSmsError.value = ""; }

let pinResolve: ((payment: PaymentCredentials | null) => void) | null = null;
setTransactionPinRequester(async (path, body) => {
  const match = path.match(/^\/approvals\/([^/]+)\/decision$/);
  paymentApprovalId = match?.[1] || ""; paymentConversationId = String(body.conversation_id || conversationId.value); paymentSmsCode.value = ""; paymentSms.value = null; paymentSmsError.value = "";
  paymentContext.value = match ? await api<PaymentContext>(`/approvals/${match[1]}/payment?conversation_id=${encodeURIComponent(String(body.conversation_id || conversationId.value))}`) : null;
  paymentAccount.value = paymentContext.value?.account_id || "";
  paymentFaceToken.value='';paymentFaceBound.value=false;paymentUsePassword.value=false;
  paymentFaceEnabled.value=paymentContext.value?.face_enabled!==false;
  try{const {faceStatus}=await import('./face');const state=await faceStatus();paymentFaceBound.value=state.bound;paymentFaceEnabled.value=state.enabled!==false&&state.bound;}catch{/* Password fallback remains available for yellow operations. */}
  if(!paymentFaceBound.value||!paymentFaceEnabled.value)paymentUsePassword.value=true;
  paymentTiming.value=paymentContext.value?.execute_at?'scheduled':'now';
  paymentExecution.value=paymentContext.value?.execute_at?new Intl.DateTimeFormat('sv-SE',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}).format(new Date(paymentContext.value.execute_at)).replace(' ','T'):'';
  return new Promise<PaymentCredentials | null>(resolve => { pinResolve = resolve; transactionPin.value = ""; transactionPinOpen.value = true; });
});
function resolvePin(value: string | null) {
  if(value&&paymentNeedsFace.value&&!paymentFaceToken.value)return;
  if (value && (!timingReady() || (paymentContext.value?.account_required && !paymentAccount.value))) return;
  const result = value ? {...(value==='face'?{}:{pin:value}),...(paymentFaceToken.value?{face_token:paymentFaceToken.value}:{}), ...timingBody(), ...(paymentContext.value?.account_required ? {account_id: paymentAccount.value} : {}), ...(paymentNeedsSms.value ? {sms_code: paymentSmsCode.value} : {})} : null;
  transactionPinOpen.value = false; transactionPin.value = ""; paymentContext.value = null; paymentAccount.value = "";
  pinResolve?.(result); pinResolve = null;
}
async function authenticated(state: AuthState) { forceSms.value=false; auth.value = state; authReady.value = true; await bootstrap(); }
async function safetyLogin() { forceSms.value=true; await logout(); }
async function logout() { await api("/auth/logout", {}); events?.close(); events = null; authReady.value = false; auth.value = null; messages.value = []; bank.value = null; dailyReportNotice.value=null;birthdayNotice.value=null; settingsOpen.value = false; }
setAuthExpiredHandler(() => { if (!authReady.value) return; resolvePin(null); events?.close(); events = null; authReady.value = false; auth.value = null; messages.value = []; bank.value = null; dailyReportNotice.value=null;birthdayNotice.value=null; settingsOpen.value = false; });
function lockApplication(){systemLocked.value=true;resolvePin(null);events?.close();events=null;authReady.value=false;auth.value=null;messages.value=[];bank.value=null;dailyReportNotice.value=null;birthdayNotice.value=null;settingsOpen.value=false;contactsOpen.value=false;investmentOpen.value=false;panelOpen.value=false;}
setSystemLockedHandler(lockApplication);
const messages = ref<Message[]>([]);
const displayMessages=computed(()=>compactDailyReports(messages.value));
const conversations = ref<Conversation[]>([]);
const conversationId = ref("");
const historyOpen = ref(window.innerWidth >= 1120);
const managing = ref(false);
const inspecting = ref(false);
const drafts = new Map<string, string>();
const bank = ref<BankSnapshot | null>(null);
const config = ref<AppConfig | null>(null);
const activeTask = ref<Task | null>(null);
const stopping = ref(false);
const stopAfterSubmit = ref(false);
let submittingPreviousTaskId: string | undefined;
const latestTaskPlan = ref<Task | null>(null);
const draft = ref("");
const voiceBusy = ref(false);
function insertVoice(text:string){
  const combined=draft.value ? draft.value+'\n'+text : text;
  if(combined.length>4000){error.value='输入内容超过4000字，请先缩短原有文字。识别结果：'+text;return;}
  draft.value=combined;resizeInput();
  void nextTick(()=>textarea.value?.focus());
}
const loading = ref(true);
const error = ref("");
const connected = ref(false);
const submitting = ref(false);
const resetting = ref(false);
const deciding = ref<string | null>(null);
const planningAction = ref<string | null>(null);
const handoffBusy = ref(false);
const humanSession = ref<{id:string;status:string;operator?:string}|null>(null);
const humanActive = computed(()=> ['waiting','active'].includes(humanSession.value?.status || ''));
let humanTimer: ReturnType<typeof setInterval> | undefined;
let humanPolling=false;
async function refreshHuman(){
  if(!authReady.value || !conversationId.value || humanPolling)return;
  humanPolling=true;
  const current=conversationId.value;
  try{const result=await api<{session:typeof humanSession.value;messages:Message[]}>(`/handoff?conversation_id=${encodeURIComponent(current)}`);
    if(current===conversationId.value && authReady.value){humanSession.value=result.session;for(const message of result.messages){const old=messages.value.find(m=>m.id===message.id);if(!old||JSON.stringify(old)!==JSON.stringify(message))upsertMessage(message);}}
  }catch(e){if(current===conversationId.value)error.value=(e as Error).message;}
  finally{humanPolling=false;}
}
async function endHuman(){if(handoffBusy.value)return;handoffBusy.value=true;try{await api('/handoff/end',{conversation_id:conversationId.value});await refreshHuman();}catch(e){error.value=(e as Error).message;}finally{handoffBusy.value=false;}}
const ratingSaving = ref<string | null>(null);


const confirmedSubscriptions=computed(()=> bank.value ? {items:bank.value.subscriptions||[],asOf:new Date().toISOString()} : null);
const panelOpen = ref(false);
const contactsOpen = ref(false);
const contactsRevision = ref(0);
const investmentOpen = ref(false);
const settingsOpen = ref(false);
const birthdayNotice=ref<BirthdayNotice|null>(null);
async function loadBirthdayNotice(){const generation=bootstrapVersion;const owner=auth.value;const result=await api<{items:BirthdayNotice[]}>('/birthday-plans/notices');if(generation===bootstrapVersion&&authReady.value&&auth.value===owner)birthdayNotice.value=result.items.find(i=>i.conversation_id!==conversationId.value)||null;}
async function dismissBirthdayNotice(){if(!birthdayNotice.value)return;const key=birthdayNotice.value.plan_id;await api(`/birthday-plans/${encodeURIComponent(key)}/seen`,{});if(birthdayNotice.value?.plan_id===key)birthdayNotice.value=null;await loadBirthdayNotice();}
async function openBirthdayNotice(){if(!birthdayNotice.value||navigationBusy.value)return;const id=birthdayNotice.value.conversation_id;try{await dismissBirthdayNotice();await selectConversation(id);}catch(e){error.value=(e as Error).message;}}
async function birthdayAction(id:string,action:string,version:number){
 if(planningAction.value||navigationBusy.value)return;planningAction.value=`birthday:${id}`;error.value='';
 try{const message=await api<Message>(`/birthday-plans/${encodeURIComponent(id)}/actions`,{action,version,conversation_id:conversationId.value});upsertMessage(message);void scrollToBottom(true);}
 catch(e){error.value=(e as Error).message;}finally{planningAction.value=null;}
}
const dailyReportNotice=ref<{conversation_id:string;report_day:string}|null>(null);
const seenDailyReports=new Set<string>();
function dismissDailyReport(){if(dailyReportNotice.value)seenDailyReports.add(dailyReportNotice.value.conversation_id);dailyReportNotice.value=null;}
async function openDailyReport(){if(!dailyReportNotice.value||navigationBusy.value)return;const id=dailyReportNotice.value.conversation_id;dismissDailyReport();await selectConversation(id);}
const bindingCode = ref("");
const bindingBusy = ref(false);
const feishuNotifications = ref(true);
watch(settingsOpen, async (open) => {
  if (!open || !authReady.value) return;
  try { const result=await api<{notifications:boolean;bound:boolean}>("/feishu/preferences"); feishuNotifications.value=result.notifications; if(config.value)config.value.feishu_bound=result.bound; } catch { /* Existing connection status remains visible. */ }
});
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
const canStop = computed(() => busy.value || pending.value || !!deciding.value || stopping.value);
const welcome = computed(() => messages.value.length <= 1 && !messages.value.some(message => message.data.source === "contact_selected" || message.data.daily_report || ['data','approval','plan','diagnosis'].includes(message.kind)));
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
    text: "我想转账，请先问我转给谁。",
    hint: "先选择收款人，再确认金额",
  },
  {
    icon: Sparkles,
    title: "整理自动续费",
    text: "查一下我所有的订阅和自动续费，先把完整列表给我，我再决定处理哪些。",
    hint: "先看续费列表，由你选择处理",
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
function upsertGlobalMessage(message: Message) {
  if (message.conversation_id === conversationId.value) upsertMessage(message);
  else void refreshConversations();
}
async function refreshBank(refresh = false) {
  const version=bootstrapVersion;
  try {
    const value = await api<BankSnapshot>(refresh ? "/bank?refresh=true" : "/bank");
    if(version===bootstrapVersion && authReady.value) bank.value=value;
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
  source.addEventListener('system.locked',(event)=>{if(JSON.parse((event as MessageEvent).data).system_locked)lockApplication();});
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
    (e) => isCurrent() && upsertGlobalMessage(JSON.parse((e as MessageEvent).data)),
  );
  source.addEventListener("task", (e) => {
    if (!isCurrent()) return;
    const task = JSON.parse((e as MessageEvent).data) as Task;
    if (task.conversation_id !== id) return;
    latestTaskPlan.value = task;
    taskStates.set(task.id, task.status);
    if (["queued", "running", "waiting_confirmation"].includes(task.status))
      activeTask.value = task;
    else if (activeTask.value?.id === task.id) activeTask.value = null;
    void scrollToBottom();
  });
  source.addEventListener("contacts.updated", () => { if (isCurrent()) { contactsRevision.value++; void loadBirthdayNotice().catch(()=>{}); } });
  source.addEventListener('birthday.ready',()=>{if(isCurrent())void loadBirthdayNotice().catch(()=>{});});
  source.addEventListener('birthday.updated',()=>{if(isCurrent()){void refreshBank();void loadBirthdayNotice().catch(()=>{});}});
  source.addEventListener('daily_report.ready',(e)=>{if(isCurrent()){const report=JSON.parse((e as MessageEvent).data);if(!seenDailyReports.has(report.conversation_id))dailyReportNotice.value=report;}});
  source.addEventListener("bank.updated", () => {
    if (!isCurrent()) return;
    void refreshBank();
  });
  source.addEventListener("binding.updated", (e) => {
    if (!isCurrent()) return;
    if (config.value) config.value.feishu_bound = JSON.parse((e as MessageEvent).data).bound;
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
  source.addEventListener("notification", () => {
    if (!isCurrent()) return;
    void refreshConversations();
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
    humanSession.value = initial.human_session || null;
    conversations.value = initial.conversations;
    draft.value = drafts.get(initial.conversation_id) || "";
    messages.value = initial.messages;
    bank.value = initial.bank;
    config.value = initial.config;
    activeTask.value = initial.active_task;
    latestTaskPlan.value = initial.latest_task_plan || null;
    taskStates.clear();
    if (initial.active_task)
      taskStates.set(initial.active_task.id, initial.active_task.status);
    error.value = "";
    openEvents(initial.event_id, initial.conversation_id);
    void loadBirthdayNotice().catch(()=>{});
    void api<{conversation_id:string|null;report_day?:string}>('/daily-report/today').then(report=>{if(version===bootstrapVersion&&authReady.value){if(report.conversation_id===initial.conversation_id&&report.conversation_id)seenDailyReports.add(report.conversation_id);dailyReportNotice.value=report.conversation_id&&!seenDailyReports.has(report.conversation_id)?{conversation_id:report.conversation_id,report_day:report.report_day||''}:null;}}).catch(()=>{});
    showScrollDown.value = false;
    resizeInput();
    if(initial.messages.some(m=>m.data.daily_report)&&!initial.messages.some(m=>m.role==='user')){
      void nextTick(()=>chatViewport.value?.scrollTo({top:0}));
    }else void scrollToBottom(true);
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
  if (!text.trim() || voiceBusy.value || busy.value || navigationBusy.value || !connected.value)
    return;
  submitting.value = true;
  submittingPreviousTaskId = activeTask.value?.id;
  error.value = "";
  const previous = draft.value;
  draft.value = "";
  resizeInput();
  try {
    const wasHuman=humanActive.value;
    const result = await api<{ task_id: string }>(wasHuman?'/handoff/messages':"/messages", {
      text: text.trim(),
      conversation_id: conversationId.value,
    });
    await refreshHuman();
    const knownStatus = wasHuman || humanActive.value ? 'completed' : taskStates.get(result.task_id);
    if (
      !knownStatus ||
      ["queued", "running", "waiting_confirmation"].includes(knownStatus)
    )
      activeTask.value = {
        id: result.task_id,
        status: knownStatus || "queued",
      };
    if (stopAfterSubmit.value) await stopCurrentTask(result.task_id);
    void scrollToBottom(true);
  } catch (e) {
    error.value = (e as Error).message;
    draft.value = previous || text;
    resizeInput();
  } finally {
    submitting.value = false;
    submittingPreviousTaskId = undefined;
    stopAfterSubmit.value = false;
    textarea.value?.focus();
  }
}
async function stopCurrentTask(taskId = activeTask.value?.id) {
  if (!taskId) { if (submitting.value) stopAfterSubmit.value=true; return; }
  if (submitting.value && taskId===submittingPreviousTaskId) { stopAfterSubmit.value=true; return; }
  if (stopping.value) return;
  stopping.value=true;
  if (transactionPinOpen.value) resolvePin(null);
  const current=conversationId.value;
  try {
    const result=await api<{task:Task;message?:Message}>(`/tasks/${taskId}/interrupt`,{conversation_id:current});
    if (current===conversationId.value) {
      taskStates.set(taskId,result.task.status);
      if (!latestTaskPlan.value || latestTaskPlan.value.id===taskId) latestTaskPlan.value=result.task;
      if (result.message) upsertMessage(result.message);
      if (activeTask.value?.id===taskId) activeTask.value=null;
      void refreshBank();
      void scrollToBottom(true);
    }
  } catch(e) { error.value=(e as Error).message; }
  finally { stopping.value=false;stopAfterSubmit.value=false; }
}
async function partialAction(taskId:string,action:string,version:number,orderId?:string){
 if(planningAction.value||navigationBusy.value)return;
 const current=conversationId.value;planningAction.value=`partial:${taskId}`;error.value='';
 try{await api(`/tasks/${taskId}/partial-resolution`,{conversation_id:current,action,version,order_id:orderId});if(current===conversationId.value)await bootstrap(current);}
 catch(e){error.value=(e as Error).message;}
 finally{planningAction.value=null;}
}
function suggest(prompt: string) {
  draft.value = prompt;
  drafts.set(conversationId.value, prompt);
  resizeInput();
  void nextTick(() => textarea.value?.focus());
}
async function requestHandoff() {
  if (handoffBusy.value || navigationBusy.value) return;
  const current = conversationId.value;
  handoffBusy.value = true;
  error.value = '';
  try {
    await api('/messages', {conversation_id: current, text: '转人工'});
    if (current === conversationId.value) await bootstrap(current);
    if (window.innerWidth < 1120) historyOpen.value = false;
    void scrollToBottom(true);
  } catch (e) { error.value = (e as Error).message; }
  finally { handoffBusy.value = false; }
}
async function rateHandoff(messageId: string, halfStars: number) {
  if (ratingSaving.value) return;
  const current = conversationId.value;
  ratingSaving.value = messageId;
  error.value = '';
  try {
    const message = await api<Message>(`/handoffs/${messageId}/rating`, {conversation_id: current, half_stars: halfStars});
    if (current === conversationId.value) upsertMessage(message);
  } catch (e) { if (current === conversationId.value) error.value = (e as Error).message; }
  finally { ratingSaving.value = null; }
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
async function planAction(
  planId: string,
  action: string,
  version: number,
  variantKey = "now",
) {
  if (planningAction.value || navigationBusy.value) return;
  planningAction.value = `${planId}:${action}`;
  error.value = "";
  try {
    const message = await api<Message>(`/plans/${encodeURIComponent(planId)}/actions`, {
      action,
      version,
      variant_key: variantKey,
      conversation_id: conversationId.value,
    });
    upsertMessage(message);
    void scrollToBottom(true);
  } catch (e) {
    error.value = (e as Error).message;
  } finally {
    planningAction.value = null;
  }
}
async function adoptTrigger(notificationId: string) {
  if (planningAction.value || navigationBusy.value) return;
  planningAction.value = `notice:${notificationId}`;
  try {
    const message = await api<Message>(`/notifications/${encodeURIComponent(notificationId)}/adopt`, {}, "POST");
    upsertMessage(message);
    void scrollToBottom(true);
  } catch (e) {
    error.value = (e as Error).message;
  } finally {
    planningAction.value = null;
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
    if (window.innerWidth < 900 || tool==='list_subscriptions') panelOpen.value = false;
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
async function unbindFeishu() {
  bindingBusy.value=true;
  try { await api('/feishu/unbind',{}); bindingCode.value=''; if(config.value)config.value.feishu_bound=false; }
  catch(e){error.value=(e as Error).message;}finally{bindingBusy.value=false;}
}
async function setFeishuNotifications(event:Event) {
  const enabled=(event.target as HTMLInputElement).checked;
  try { await api('/feishu/preferences',{notifications:enabled}); feishuNotifications.value=enabled; }
  catch(e){feishuNotifications.value=!enabled;error.value=(e as Error).message;}
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
  if (historyOpen.value && window.innerWidth < 1120) { panelOpen.value = false; contactsOpen.value = false; investmentOpen.value = false; }
}
function toggleBank() {
  investmentOpen.value = false;
  contactsOpen.value = false;
  panelOpen.value = !panelOpen.value;
  if (panelOpen.value) void refreshBank();
  if (panelOpen.value && window.innerWidth < 1120) historyOpen.value = false;
}
function toggleInvestment() {
  panelOpen.value = false; contactsOpen.value = false;
  investmentOpen.value = !investmentOpen.value;
  if (investmentOpen.value) void refreshBank();
  if (investmentOpen.value && window.innerWidth < 1120) historyOpen.value = false;
}
function toggleContacts() {
  investmentOpen.value = false;
  panelOpen.value = false;
  contactsOpen.value = !contactsOpen.value;
  if (contactsOpen.value && window.innerWidth < 1120) historyOpen.value = false;
}
async function startContactConversation(id: string) {
  if (navigationBusy.value) return;
  managing.value = true;
  try {
    const created = await api<{ conversation_id: string }>(`/contacts/${encodeURIComponent(id)}/conversation`, {});
    contactsOpen.value = false;
    await bootstrap(created.conversation_id);
    if (window.innerWidth < 1120) historyOpen.value = false;
    textarea.value?.focus();
  } catch (e) { error.value = (e as Error).message; }
  finally { managing.value = false; }
}
function globalKeydown(event: KeyboardEvent) {
  if (event.key === "Escape") {
    settingsOpen.value = false;
    contactsOpen.value = false;
    panelOpen.value = false;
    historyOpen.value = false;
  }
}
let stopMarketSchedule: (() => void) | undefined;
onMounted(() => {
  humanTimer=setInterval(()=>{if(authReady.value && humanActive.value)void refreshHuman();},2000);
  stopMarketSchedule = watchMarketCheckpoints(() => { if (authReady.value) void refreshBank(true); });
  document.addEventListener("keydown", globalKeydown);
});
onBeforeUnmount(() => {
  clearInterval(humanTimer);
  stopMarketSchedule?.();
  resolvePin(null);
  bootstrapVersion++;
  window.clearTimeout(listRefreshTimer);
  events?.close();
  document.removeEventListener("keydown", globalKeydown);
});
</script>

<template>
  <ManualLock v-if="systemLocked"/>
  <AuthGate v-else-if="!authReady" :force-sms="forceSms" @ready="authenticated" />
  <div v-else class="app-shell">
    <header class="app-header">
      <a href="/" class="brand" aria-label="有数首页"
        ><span class="brand-symbol" aria-hidden="true"
          ><i></i><i></i><i></i></span
        ><span class="brand-name">有数<span>AI BANKING</span></span></a
      >
      <div class="header-middle">
        <span class="status-dot"></span>您好，{{ auth?.username }}
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
          <Wallet :size="16" /><span>模拟账户</span
          ><ChevronDown :size="14" :class="{ rotated: panelOpen }" /></button
        >
        <button class="panel-toggle" :class="{ selected: investmentOpen }" :aria-expanded="investmentOpen" @click="toggleInvestment"><ChartNoAxesCombined :size="16" /><span>投资账户</span><ChevronDown :size="14" :class="{ rotated: investmentOpen }" /></button>
        <button class="panel-toggle" :class="{ selected: contactsOpen }" :aria-expanded="contactsOpen" @click="toggleContacts">
          <Users :size="16" /><span>联系人</span><ChevronDown :size="14" :class="{ rotated: contactsOpen }" />
        </button><button
          class="icon-button settings-trigger"
          aria-label="应用设置"
          @click="settingsOpen = true"
        >
          <Settings :size="18" />
        </button>
      </div>
    </header>

    <main class="workspace" :class="{ 'panel-is-open': panelOpen || contactsOpen || investmentOpen }">
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
        :disabled="navigationBusy || handoffBusy"
        @create="createConversation"
        @select="selectConversation"
        @rename="renameConversation"
        @delete="deleteConversation"
        @close="historyOpen = false"
        @handoff="requestHandoff"
      />
      <section class="chat-workspace" aria-label="银行助手对话">
        <div v-if="birthdayNotice" class="connection-notice"><span>{{birthdayNotice.title}}</span><button :disabled="navigationBusy" @click="openBirthdayNotice">查看生日安排 <ArrowRight :size="14"/></button><button aria-label="关闭生日提醒" @click="dismissBirthdayNotice"><X :size="14"/></button></div>
        <div v-if="dailyReportNotice && dailyReportNotice.conversation_id!==conversationId" class="connection-notice"><span>今日日报已生成</span><button :disabled="navigationBusy" @click="openDailyReport">查看日报 <ArrowRight :size="14"/></button><button aria-label="关闭日报提示" @click="dismissDailyReport"><X :size="14"/></button></div>
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
            >模拟账户已就绪，连接模型后即可开始对话</span
          ><button @click="settingsOpen = true">
            连接模型 <ArrowRight :size="14" />
          </button>
        </div>
        <div v-else-if="!loading && !connected" class="connection-notice">
          <span><CircleAlert :size="14" />连接暂时中断，正在恢复…</span
          ><button @click="bootstrap()">重新连接</button>
        </div>
        <TaskPlan :task="latestTaskPlan" />
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
                v-for="message in displayMessages"
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
                      message.role === "assistant" ? (message.data.source==='human'?'人工客服':'有数') : "你"
                    }}</span
                    ><small v-if="message.data.channel === 'feishu'"
                      >来自飞书</small
                    ><time :datetime="message.created_at">{{
                      timeLabel(message.created_at)
                    }}</time>
                  </div>
                  <MessageCard
                    v-if="
                      message.kind === 'data' || message.kind === 'approval' || message.kind === 'plan' || message.kind === 'diagnosis'
                    "
                    :message="message"
                    :deciding="deciding"
                    :acting="ratingSaving ? `rating:${ratingSaving}` : planningAction"
                    @decision="decide"
                    @plan-action="planAction"
                    @birthday-action="birthdayAction"
                    @partial-action="partialAction"
                    @adopt-trigger="adoptTrigger"
                    @suggest="suggest"
                    @rate-handoff="rateHandoff"
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
          <div v-if="humanActive" class="human-banner" role="status"><span><strong>{{humanSession?.status==='active'?'人工客服已接入':'正在等待人工接入'}}</strong> · {{humanSession?.status==='active'?'可在下方直接发送消息':'可以继续留言，客服接入后会查看历史记录'}}</span><button :disabled="handoffBusy" @click="endHuman">结束人工服务</button></div>
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
                  >{{humanActive?'人工客服':'有数助手'}}</span
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
              <div class="composer-actions">
              <VoiceInput :disabled="loading || busy || navigationBusy || !connected || !authReady" :context="conversationId" @text="insertVoice" @busy="voiceBusy = $event" />
              <button v-if="canStop" type="button" class="send-button" :disabled="stopping || stopAfterSubmit" :aria-label="stopping || stopAfterSubmit ? '正在停止任务' : '停止当前任务'" title="停止任务，未完成交易按失败处理" @click="stopCurrentTask()"><Square :size="16" fill="currentColor" /></button>
              <button v-else
                type="submit"
                class="send-button"
                :disabled="
                  !draft.trim() || voiceBusy || busy || navigationBusy || !connected
                "
                aria-label="发送消息"
              >
                <ArrowUp :size="20" />
              </button>
              </div>
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
        v-if="panelOpen || contactsOpen || investmentOpen"
        class="panel-backdrop"
        aria-label="关闭侧边面板"
        @click="panelOpen = false; contactsOpen = false; investmentOpen = false"
      ></button>
      <InvestmentPanel v-if="investmentOpen && bank" :bank="bank" :conversation-id="conversationId" :busy="navigationBusy || busy || pending" @close="investmentOpen = false" @changed="bootstrap(conversationId)" />
      <ContactsPanel v-if="contactsOpen" :busy="navigationBusy" :revision="contactsRevision" @close="contactsOpen = false" @select="startContactConversation" />
      <BankPanel
        v-if="panelOpen && bank"
        :bank="bank"
        :busy="busy"
        :resetting="resetting"
        :conversation-id="conversationId"
        :subscription-view="confirmedSubscriptions"
        @changed="bootstrap(conversationId)"


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
        <div class="dialog-heading settings-heading">
          <div>
            <span class="small-eyebrow">让助手准备就绪</span>
            <h2 id="settings-title">应用设置</h2>
          </div>
          <button
            class="icon-button"
            aria-label="关闭设置"
            @click="settingsOpen = false"
          >
            <X :size="20" />
          </button>
        </div>
        <div class="settings-scroll">
        <AppSecuritySettings v-if="auth" :state="auth" @changed="auth = $event" @safety-login="safetyLogin">
<details class="settings-group"><summary><span>飞书私聊</span><small>绑定账号 · 消息推送</small></summary><div class="settings-group-content">        <div class="setting-section">
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
          <label v-if="config?.feishu_enabled" class="feishu-notification-option"><input type="checkbox" :checked="feishuNotifications" @change="setFeishuNotifications" /> 推送日报、特别关心的生日提醒和定时交易结果</label>
          <div v-if="config?.feishu_enabled" class="feishu-binding-actions">
          <button
            class="outline-button"
            :disabled="bindingBusy"
            @click="generateBinding"
          >
            <Link2 :size="15" />{{
              bindingBusy ? "正在生成…" : "生成私聊绑定码"
            }}
          </button>
          <button v-if="config?.feishu_bound" class="quiet-button" :disabled="bindingBusy" @click="unbindFeishu">解除飞书关联</button>
          </div>
          <p v-else class="setting-note">
            当前未启用，Web 可以独立使用全部能力。
          </p>
          <div v-if="bindingCode" class="binding-code">
            {{ bindingCode }}<small>10 分钟内有效，仅可使用一次。</small>
          </div>
        </div>
</div></details>
<details class="settings-group"><summary><span>模型配置</span><small>查看当前模型与连接信息</small></summary><div class="settings-group-content">        <div class="setting-section">
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
</div></details>
        </AppSecuritySettings>
        <div class="dialog-footer">
          <ShieldCheck :size="15" /><span>操作凭证与确认均由后端校验</span
          ><button class="quiet-button" @click="settingsOpen = false">
            知道了
          </button>
        </div>
        <button class="settings-logout" @click="logout">退出登录</button>
        </div>
      </section>
    </div>
    <aside v-if="paymentSms && transactionPinOpen" class="demo-sms" role="status"><MessageCircle :size="20" /><div><strong>模拟交易短信 · {{ paymentSms.phone_masked }}</strong><p>本次交易验证码为 <b>{{ paymentSms.demo_code }}</b>，5 分钟内有效。不会发送真实短信。</p></div><button aria-label="关闭模拟交易短信" @click="paymentSms = null">×</button></aside>
    <div v-if="transactionPinOpen" class="investment-dialog-backdrop"><form class="investment-dialog auth-inline-form" role="dialog" aria-modal="true" aria-label="验证交易密码" @submit.prevent="resolvePin(transactionPin)"><h3>{{paymentTiming==='scheduled'?'验证定时授权':'输入交易密码'}}</h3><template v-if="paymentContext?.schedule_allowed"><label>执行方式<select aria-label="交易执行方式" v-model="paymentTiming" @change="paymentAccountChanged"><option value="now">立即执行</option><option value="scheduled">定时执行</option></select></label><label v-if="paymentTiming==='scheduled'">执行时间（北京时间）<input v-model="paymentExecution" type="datetime-local" required step="1" aria-label="交易执行时间" @change="paymentAccountChanged"/></label><p v-if="paymentTiming==='scheduled'">现在只验证授权，不扣款、不冻结余额。到期按原价格和内容复核；变化则停止并要求重新授权。后台离线超过5分钟不补扣。</p></template><p>{{ paymentContext?.title || '核对已选操作' }}</p><p v-if="paymentContext?.basis==='single_transaction'">单笔金额超过 ¥{{money(paymentContext.threshold_minor??100000)}} 时需{{paymentFaceEnabled?'人脸、密码和短信':'密码和短信'}}；部分操作始终需要加强验证。</p><p v-if="paymentContext?.scheduled_preverification || (paymentTiming==='scheduled' && ['transfer','transfer_own_funds','save_to_goal','schedule_transfer'].includes(paymentContext?.tool||''))">定时转账提前完成三重验证，到点按当前安全设置及交易条件复核。</p><strong v-if="typeof paymentContext?.amount_minor === 'number'">¥{{ money(paymentContext.amount_minor) }}</strong><dl v-if="paymentContext" class="confirmation-fields"><div v-for="field in paymentContext.fields.filter(f => f.label !== '付款账户' && (!paymentContext?.schedule_allowed || !['执行时间（北京时间）','定时规则','离线规则'].includes(f.label)))" :key="field.label"><dt>{{ field.label }}</dt><dd>{{ field.value }}</dd></div></dl><label v-if="paymentContext?.account_required">付款账户<select aria-label="付款账户" v-model="paymentAccount" required @change="paymentAccountChanged"><option value="" disabled>请选择付款账户</option><option v-for="account in paymentContext.accounts" :key="account.id" :value="account.id" :disabled="!account.eligible">{{ account.name }} · 余额 ¥{{ money(account.balance_minor) }}{{ account.reason ? ' · ' + account.reason : '' }}</option></select></label><p v-if="paymentNeedsSms" class="auth-error">{{paymentFaceEnabled?'红色操作：需要人脸、支付密码和短信三项验证。':'红色操作：需要支付密码和短信验证码。'}}</p><p v-else>黄色操作：{{paymentFaceEnabled?'先刷脸，通过即可执行；也可改用支付密码。':'使用支付密码验证。'}}</p><label v-if="paymentNeedsSms">短信验证码<div class="auth-code-row"><input v-model="paymentSmsCode" required pattern="[0-9]{6}" inputmode="numeric" maxlength="6" autocomplete="one-time-code" /><button type="button" :disabled="paymentSmsBusy || !timingReady() || (!!paymentContext?.account_required && !paymentAccount)" @click="sendPaymentSms">{{ paymentSmsBusy ? '发送中…' : '获取验证码' }}</button></div></label><p v-if="paymentSmsError" class="auth-error" role="alert">{{ paymentSmsError }}</p><label v-if="paymentNeedsSms || paymentUsePassword">四位交易密码<input v-model="transactionPin" type="password" required pattern="[0-9]{4}" inputmode="numeric" maxlength="4" autocomplete="off" /></label><template v-if="paymentFaceEnabled&&(paymentNeedsSms||!paymentUsePassword)"><button type="button" class="outline-button" :disabled="paymentFaceBusy||!timingReady()|| (!!paymentContext?.account_required&&!paymentAccount)||(paymentNeedsSms&&(transactionPin.length!==4||paymentSmsCode.length!==6))" @click="verifyPaymentFace">{{paymentFaceBusy?'正在核验…':paymentFaceToken?'人脸已通过，点击重新验证':paymentNeedsSms?'验证本人人脸':'刷脸并确认交易'}}</button></template><button v-if="!paymentNeedsSms&&!paymentUsePassword" type="button" class="auth-link" @click="paymentUsePassword=true">改用支付密码</button><div class="investment-buttons"><button type="button" @click="resolvePin(null)">取消验证</button><button v-if="canStop" type="button" :disabled="stopping" @click="stopCurrentTask()">停止交易</button><button v-if="paymentNeedsSms||paymentUsePassword" class="investment-primary" type="submit" :disabled="paymentFaceBusy || (paymentNeedsFace&&!paymentFaceToken) || !timingReady() || (!!paymentContext?.account_required && !paymentAccount)">{{paymentTiming==='scheduled'||paymentContext?.tool==='schedule_transfer'?'确认定时授权':paymentContext?.tool==='add_bank_card'?'确认添加':paymentContext?.tool==='cancel_travel_order'?'确认退订并退款':paymentContext?.tool==='change_travel_order'?(paymentContext.account_required?'确认补款并改签':'确认改签'):'确认支付'}}</button></div></form></div>
  </div>
</template>
<style scoped>.human-banner{display:flex;align-items:center;justify-content:space-between;gap:12px;background:#eef4e8;border:1px solid #dfe8d6;border-radius:11px;padding:11px 14px;margin-bottom:12px;color:#62795a;font-size:12px}.human-banner strong{color:#36583b}.human-banner button{border:0;background:transparent;white-space:nowrap;color:#477145;cursor:pointer;font:inherit}@media(max-width:600px){.human-banner{align-items:flex-start}.human-banner span{line-height:1.8}}</style>
