<script setup lang="ts">
import { onMounted, ref } from "vue";
import { api } from "../types";
import type { AuthState } from "../types";
import SafetyPanel from './SafetyPanel.vue';
import DailyReportSettings from './DailyReportSettings.vue';
import FaceSettings from './FaceSettings.vue';
const props = defineProps<{ state: AuthState }>();
const faceEnabled=ref(true);
onMounted(async()=>{try{const state=(await api<{enabled:boolean;bound:boolean}>('/face/status'));faceEnabled.value=state.enabled!==false&&state.bound;}catch{/* Server decides final verification requirements. */}});
const emit = defineEmits<{ changed: [state: AuthState]; safetyLogin: [] }>();
const syncCode = ref(""); const syncExpires = ref(""); const syncError = ref(""); const syncBusy = ref(false);
async function generateSyncCode() {
  syncBusy.value = true; syncError.value = ""; syncCode.value = "";
  try { const result = await api<{code:string; expires:number}>("/connections/pairing-code", {}); syncCode.value = result.code; syncExpires.value = new Date(result.expires * 1000).toLocaleTimeString("zh-CN", {hour12:false}); }
  catch(e) { syncError.value = (e as Error).message; } finally { syncBusy.value = false; }
}
const editing = ref(false); const newPin = ref(""); const repeatPin = ref(""); const currentPin = ref("");
const pinSms=ref(""),pinDemo=ref(""),pinFace=ref('');
async function verifyPinFace(){working.value=true;error.value='';try{const {runFace}=await import('../face');pinFace.value=(await runFace(()=>api('/face/challenge',{purpose:'pin_change',pin:currentPin.value,sms_code:pinSms.value}))).face_token||'';}catch(e){error.value=(e as Error).message;}finally{working.value=false;}}
async function sendPinSms(){working.value=true;error.value="";try{pinDemo.value=(await api<{demo_code:string}>("/auth/pin/sms",{pin:newPin.value,repeat_pin:repeatPin.value,current_pin:currentPin.value})).demo_code;}catch(e){error.value=(e as Error).message;}finally{working.value=false;}}
const setting = ref(false); const error = ref(""); const working = ref(false);
const renaming = ref(false); const username = ref(""); const profileError = ref(""); const profileStatus = ref("");
const threshold = ref("1000"), savedThreshold = ref("1000"), policyPin = ref("");
const policyError = ref(""), policySaved = ref(""), policyBusy = ref(false), policyReady = ref(false);
onMounted(async () => { try { const result = await api<{threshold_minor:number}>("/payment-policy"); threshold.value = savedThreshold.value = String(result.threshold_minor / 100); policyReady.value = true; } catch (e) { policyError.value = (e as Error).message; } });
async function savePolicy() {
 policyBusy.value=true; policyError.value=""; policySaved.value="";
 try { const result=await api<{threshold_minor:number}>("/payment-policy",{threshold_yuan:String(threshold.value),pin:policyPin.value},"PATCH");
  threshold.value=savedThreshold.value=String(result.threshold_minor/100);policyPin.value="";policySaved.value="单笔交易门槛已保存。";
 } catch(e){policyError.value=(e as Error).message;}finally{policyBusy.value=false;}
}
async function rename() {
  profileError.value = ""; profileStatus.value = "";
  if (!username.value.trim()) { profileError.value = "用户名不能为空。"; return; }
  working.value = true;
  try {
    const next = await api<AuthState>("/auth/profile", {username:username.value.trim()}, "PATCH");
    emit("changed", next); renaming.value = false; profileStatus.value = "用户名已更新。";
  } catch (e) { profileError.value = (e as Error).message; } finally { working.value = false; }
}
async function save(change: boolean) {
  error.value = ""; working.value = true;
  try {
    const next = await api<AuthState>(change ? "/auth/pin" : "/auth/lock-setting", change ? {pin:newPin.value,repeat_pin:repeatPin.value,current_pin:currentPin.value,sms_code:pinSms.value,face_token:pinFace.value} : {enabled:!props.state.lock_enabled,pin:currentPin.value});
    emit("changed", next); editing.value = false; setting.value = false; currentPin.value = ""; newPin.value = ""; repeatPin.value = ""; pinSms.value="";pinDemo.value="";
  } catch (e) { error.value = (e as Error).message; } finally { working.value = false; }
}
</script>
<template>
  <section class="setting-section"><div class="setting-title"><h3>个人信息</h3></div><p>{{ state.username }} · {{ state.phone_masked }}</p><button class="outline-button" :disabled="working" @click="renaming = !renaming; username = state.username; profileError = ''; profileStatus = ''">修改用户名</button><form v-if="renaming" class="auth-inline-form" @submit.prevent="rename"><label>新用户名<input v-model="username" required maxlength="40" autocomplete="nickname" :disabled="working" /></label><div class="investment-buttons"><button type="button" :disabled="working" @click="renaming = false; profileError = ''">取消</button><button type="submit" class="investment-primary" :disabled="working">保存用户名</button></div></form><p v-if="profileError" class="auth-error" role="alert">{{ profileError }}</p><p v-if="profileStatus" role="status">{{ profileStatus }}</p></section>
<details class="settings-group"><summary><span>登录与密码</span><small>应用锁 · 修改密码</small></summary><div class="settings-group-content">  <section class="setting-section security-settings"><div class="setting-title"><h3>应用与交易密码</h3><span class="connection-state" :class="{ready:state.lock_enabled}">{{ state.lock_enabled ? '已开启' : '已关闭' }}</span></div><p>{{ state.username }} · {{ state.phone_masked }}</p><p>开启后，每次打开应用需输入四位密码。资金交易始终需要密码验证。</p><div class="investment-buttons"><button :disabled="working" @click="setting = !setting; editing = false">{{ state.lock_enabled ? '关闭应用密码' : '开启应用密码' }}</button><button :disabled="working" @click="editing = !editing; setting = false">修改四位密码</button></div>
    <form v-if="setting || editing" class="auth-inline-form" @submit.prevent="save(editing)"><label>当前四位密码<input v-model="currentPin" type="password" pattern="[0-9]{4}" maxlength="4" inputmode="numeric" required autocomplete="off" /></label><template v-if="editing"><label>新四位密码<input v-model="newPin" type="password" pattern="[0-9]{4}" maxlength="4" inputmode="numeric" required autocomplete="new-password" /></label><label>再次输入新密码<input v-model="repeatPin" type="password" pattern="[0-9]{4}" maxlength="4" inputmode="numeric" required autocomplete="new-password" /></label><p>修改密码属于红色操作，需要{{faceEnabled?'原密码、短信和已绑定人脸':'原密码和短信'}}。</p><label>修改密码验证码<input v-model="pinSms" pattern="[0-9]{6}" maxlength="6" inputmode="numeric" required autocomplete="one-time-code" /></label><button type="button" :disabled="working || !currentPin || newPin.length!==4 || newPin!==repeatPin" @click="sendPinSms">获取修改密码验证码</button><button v-if="faceEnabled" type="button" :disabled="working || pinSms.length!==6 || !currentPin" @click="verifyPinFace">{{pinFace?'人脸已验证，点击重新验证':'验证本人面容'}}</button></template><button class="auth-primary" :disabled="working || (editing&&faceEnabled&&!pinFace)">确认{{ editing ? '修改密码' : state.lock_enabled ? '关闭' : '开启' }}</button></form><p v-if="error" class="auth-error" role="alert">{{ error }}</p>
    <aside v-if="pinDemo" class="safety-sms" role="status"><button type="button" class="auth-link" @click="pinDemo=''">关闭</button><strong>演示短信</strong><p>修改密码验证码：<b>{{pinDemo}}</b></p><small>5分钟有效，仅用于当前新密码。</small></aside>
  </section></div></details>

<details class="settings-group"><summary><span>安全状态与恢复</span><small>安全记录 · 解除交易限制</small></summary><div class="settings-group-content"><SafetyPanel @login="emit('safetyLogin')"/></div></details>
<details class="settings-group"><summary><span>人脸验证</span><small>绑定本人 · 重新录入</small></summary><div class="settings-group-content"><FaceSettings @changed="faceEnabled=$event"/></div></details>
<details class="settings-group"><summary><span>每日早报</span><small>开启或关闭 · 调整时间</small></summary><div class="settings-group-content"><DailyReportSettings/></div></details>
<details class="settings-group"><summary><span>连接其他平台</span><small>生成一个通用同步码</small></summary><div class="settings-group-content">  <section class="setting-section"><div class="setting-title"><h3>平台通用同步码</h3></div><p>证券、外卖和旅行平台都输入这同一个码，关联当前有数账号。5分钟有效，每个平台可使用一次；重新生成后旧码对所有平台失效。</p><button class="outline-button" :disabled="syncBusy" @click="generateSyncCode">{{ syncBusy ? "正在生成…" : syncCode ? "重新生成同步码" : "生成同步码" }}</button><div v-if="syncCode" class="sync-code-display"><strong aria-label="通用同步码">{{ syncCode }}</strong><p>有效至 {{ syncExpires }} · 仅在你自己的证券、外卖或旅行页面输入</p></div><p v-if="syncError" class="auth-error" role="alert">{{ syncError }}</p></section></div></details>
<slot />
<details class="settings-group"><summary><span>交易验证规则</span><small>设置单笔金额门槛与验证要求</small></summary><div class="settings-group-content"><section class="setting-section"><div class="setting-title"><h3>单笔交易安全门槛</h3><span class="connection-state ready">可自定义</span></div>
<p>单笔不超过 ¥{{ Number(savedThreshold).toFixed(2) }} 为黄色，{{faceEnabled?'刷脸或支付密码':'支付密码'}}验证；超过为红色，需要{{faceEnabled?'人脸、密码和短信':'密码和短信'}}。不累计当日其他交易，默认门槛1000元。</p>
<form class="auth-inline-form" @submit.prevent="savePolicy"><label>单笔金额门槛（元）<input v-model="threshold" type="number" min="0.01" max="999999999.99" step="0.01" inputmode="decimal" required :disabled="policyBusy||!policyReady" /></label><label>支付密码<input v-model="policyPin" type="password" pattern="[0-9]{4}" inputmode="numeric" maxlength="4" autocomplete="off" required :disabled="policyBusy||!policyReady" /></label><button class="auth-primary" :disabled="policyBusy||!policyReady">{{policyBusy?'正在保存…':'保存金额门槛'}}</button></form>
<p>添加银行卡、理财申购、股票买卖和定时转账始终需要{{faceEnabled?'人脸、密码与短信':'密码与短信'}}验证，不受金额门槛影响。</p><p v-if="policyError" class="auth-error" role="alert">{{policyError}}</p><p v-if="policySaved" role="status">{{policySaved}}</p></section></div></details>
</template>

<style scoped>
.sync-code-display{padding:16px 0 0}.sync-code-display strong{font-size:28px;font-variant-numeric:tabular-nums;letter-spacing:5px;color:#38684e}.sync-code-display p{font-size:12px}
.safety-sms{position:fixed;right:18px;bottom:18px;z-index:200;width:290px;max-width:calc(100vw - 36px);box-sizing:border-box;background:#fffdf4;border:1px solid #d9dfc9;border-radius:14px;padding:18px;color:#3b6045;box-shadow:0 12px 45px #203e3329}.safety-sms button{float:right}.safety-sms b{font-size:22px;letter-spacing:3px}.safety-sms small{font-size:11px;color:#77866d}
</style>
