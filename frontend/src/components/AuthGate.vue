<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { ShieldCheck, MessageCircle, LockKeyhole } from "@lucide/vue";
import { api } from "../types";
import type { AuthState } from "../types";
import FaceSettings from './FaceSettings.vue';
const props = defineProps<{forceSms?: boolean}>();
const emit = defineEmits<{ ready: [state: AuthState] }>();
const state = ref<AuthState | null>(null);
const mode = ref("register");
const username = ref(""); const phone = ref(""); const code = ref("");
const pin = ref(""); const repeatPin = ref(""); const pinStep = ref(false);
const error = ref(""); const working = ref(false); const sms = ref<{ demo_code: string; phone_masked: string } | null>(null);
const contactName = ref(""); const contactPhone = ref(""); const contactBank = ref(""); const card = ref("");
const sameName = ref<{ id: string; name: string; bank: string }[]>([]);
const step = computed(() => !state.value ? "loading" : state.value.ready ? (state.value.face_onboarding_done===false&&state.value.face_enabled!==false?'face':'ready') : state.value.authenticated && !state.value.contacts_done ? "contacts" : state.value.authenticated && !state.value.has_pin ? "pin" : state.value.has_pin && state.value.lock_enabled && mode.value === "unlock" ? "unlock" : "login");
async function accept(next: AuthState) { state.value = next; pin.value = ""; repeatPin.value = ""; if (next.ready&&!(next.face_onboarding_done===false&&next.face_enabled!==false)) emit("ready", next); }
async function finishFace(){await run(async()=>accept(await api<AuthState>('/auth/face-onboarding',{})));}
async function run(fn: () => Promise<void>) { working.value = true; error.value = ""; try { await fn(); } catch (e) { error.value = (e as Error).message; } finally { working.value = false; } }
async function open() { await run(async () => { const next = await api<AuthState>("/auth/open", {}); mode.value = next.registered && !props.forceSms ? "unlock" : "login"; await accept(next); }); }
async function sendCode() { await run(async () => { sms.value = await api("/auth/sms", {phone: phone.value, purpose: mode.value === "register" ? "register" : "login"}); }); }
async function login() { await run(async () => { await accept(await api<AuthState>("/auth/login", {phone: phone.value, purpose: mode.value === "register" ? "register" : "login", username: username.value, code: code.value})); sms.value = null; }); }
async function savePin() { await run(async () => { await accept(await api<AuthState>("/auth/pin", {pin: pin.value, repeat_pin: repeatPin.value})); }); }
async function nextContact() { await accept(await api<AuthState>("/auth/contacts-done", {})); }
async function addContact(choice?: {same_person_id?: string; new_person?: boolean}) { await run(async () => {
  if (!choice) { const check = await api<{id:string;name:string;bank:string}[]>("/contacts/check-name", {name:contactName.value}); if (check.length) { sameName.value = check; return; } }
  await api("/contacts", {name:contactName.value,phone:contactPhone.value || null,bank:contactBank.value || null,card_number:card.value || null,...choice}); sameName.value = []; await nextContact();
}); }
onMounted(open);
</script>

<template>
  <main class="auth-screen"><section class="auth-card">
    <div class="auth-brand"><ShieldCheck :size="27" /><strong>有数</strong><span>你的个人银行助手</span></div>
    <p v-if="error" class="auth-error" role="alert">{{ error }}</p>
    <div v-if="step === 'loading'"><p>正在连接应用…</p><button :disabled="working" @click="open">重试</button></div>
    <form v-else-if="step === 'unlock'" @submit.prevent="run(async () => accept(await api<AuthState>('/auth/unlock', {pin})))">
      <LockKeyhole :size="30" /><h1>输入密码，解锁有数</h1><p>请输入你设置的四位应用与交易密码。</p><label>四位密码<input v-model="pin" type="password" inputmode="numeric" pattern="[0-9]{4}" maxlength="4" autocomplete="off" required /></label><button class="auth-primary" :disabled="working">解锁应用</button><button type="button" class="auth-link" @click="mode = 'register'; pin = ''; error = ''">没有账号？注册应用</button><button type="button" class="auth-link" @click="mode = 'login'; pin = ''; error = ''">使用手机号短信登录</button>
    </form>
    <form v-else-if="step === 'login'" @submit.prevent="login">
      <h1>{{ mode === 'register' ? '欢迎注册有数' : '手机号登录' }}</h1><p>{{ mode === 'register' ? '先创建你的模拟应用账号，再添加联系人和设置交易密码。' : '通过模拟短信验证，登录已有账号。' }}</p>
      <label v-if="mode === 'register'">用户名<input v-model="username" required maxlength="40" autocomplete="username" /></label>
      <label>手机号<input v-model="phone" required type="tel" pattern="1[0-9]{10}" maxlength="11" autocomplete="tel" /></label>
      <label>短信验证码<div class="auth-code-row"><input v-model="code" required inputmode="numeric" pattern="[0-9]{6}" maxlength="6" autocomplete="one-time-code" /><button type="button" :disabled="working || !/^1\d{10}$/.test(phone)" @click="sendCode">获取验证码</button></div></label>
      <button class="auth-primary" :disabled="working">{{ mode === 'register' ? '验证并注册' : '验证并登录' }}</button>
      <button type="button" class="auth-link" @click="mode = mode === 'register' ? 'login' : 'register'; sms = null; code = ''; error = ''">{{ mode === 'register' ? '已注册？手机号登录' : '没有账号？注册应用' }}</button>
    </form>
    <form v-else-if="step === 'contacts'" @submit.prevent="addContact()">
      <span class="auth-step">引导 1 / 3</span><h1>添加你的第一位联系人</h1><p>现有演示联系人会保留，你也可以稍后再添加。</p>
      <template v-if="sameName.length"><p>有同名联系人，是同一个人吗？</p><button v-for="person in sameName" :key="person.id" type="button" :disabled="working" @click="addContact({same_person_id:person.id})">合并到 {{ person.name }} · {{ person.bank }}</button><button type="button" :disabled="working" @click="addContact({new_person:true})">不是同一个人，新增联系人</button></template>
      <template v-else><label>姓名<input v-model="contactName" required maxlength="60" /></label><label>手机号<input v-model="contactPhone" type="tel" pattern="1[3-9][0-9]{9}" maxlength="11" /></label><label>开户银行<input v-model="contactBank" :required="!!card" maxlength="60" /></label><label>银行卡号<input v-model="card" :required="!!contactBank" inputmode="numeric" pattern="[0-9]{12,19}" maxlength="19" /></label><button class="auth-primary" :disabled="working">保存并继续</button></template>
      <button type="button" class="auth-link" :disabled="working" @click="run(nextContact)">稍后添加，继续设置密码</button>
    </form>
    <form v-else-if="step === 'pin'" @submit.prevent="pinStep ? savePin() : (pinStep = true)">
      <span class="auth-step">引导 2 / 3</span><h1>{{ pinStep ? '再次输入密码' : '设置四位交易密码' }}</h1><p>用于打开应用时解锁，以及资金交易的验证。应用密码默认开启。</p>
      <label v-if="!pinStep">设置四位密码<input v-model="pin" type="password" required pattern="[0-9]{4}" inputmode="numeric" maxlength="4" autocomplete="new-password" /></label>
      <label v-else>再次输入四位密码<input v-model="repeatPin" type="password" required pattern="[0-9]{4}" inputmode="numeric" maxlength="4" autocomplete="new-password" /></label>
      <button class="auth-primary" :disabled="working">{{ pinStep ? '确认密码并继续' : '下一步：确认密码' }}</button><button v-if="pinStep" type="button" class="auth-link" @click="pinStep = false; repeatPin = ''">重新设置</button>
    </form>
    <div v-else-if="step==='face'"><span class="auth-step">引导 3 / 3 · 可跳过</span><h1>录入你的本人面容</h1><p>刷脸可用于交易验证，也可以稍后到应用设置中录入。跳过后仍可使用密码和短信验证，之后也能在应用设置中录入。</p><FaceSettings @enrolled="finishFace"/><button type="button" class="auth-link" :disabled="working" @click="finishFace">暂时跳过，进入应用</button></div>
    <p class="auth-demo-note">模拟账户演示 · 验证码会显示在右下角，不会发送真实短信。</p>
  </section><aside v-if="sms" class="demo-sms" role="status"><MessageCircle :size="20" /><div><strong>模拟短信 · {{ sms.phone_masked }}</strong><p>你的验证码为 <b>{{ sms.demo_code }}</b>，5 分钟内有效。</p></div><button aria-label="关闭模拟短信" @click="sms = null">×</button></aside></main>
</template>
