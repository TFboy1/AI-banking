<script setup lang="ts">
import {computed,onMounted,onBeforeUnmount,ref} from 'vue';
import {ShieldCheck,RefreshCw,X} from '@lucide/vue';
import {api} from '../types';
interface Scope {scope:string;label:string;blocked_until:number;requires_reauth:number;reason:string;probe_until:number}
interface Status {guard:{credential_trips:number;suspicious_trips:number};server_now:number;scopes:Scope[];can_recover:boolean;events:{scope:string;kind:string;detail:string;created_at:string}[]}
const emit=defineEmits<{login:[]}>();
const data=ref<Status|null>(null),error=ref(''),working=ref(false),code=ref(''),demoCode=ref('');
const clock=ref(Date.now()/1000);let offset=0;let timer:number|undefined;
const user=computed(()=>data.value?.scopes.find(s=>s.scope==='transactions'));
const remaining=(scope:Scope)=>Math.max(0,Math.ceil(scope.blocked_until-clock.value));
const paused=(scope:Scope)=>!!scope.requires_reauth||remaining(scope)>0;
async function load(){try{data.value=await api<Status>('/safety/status');offset=data.value.server_now-Date.now()/1000;clock.value=Date.now()/1000+offset;}catch(e){error.value=(e as Error).message;}}
async function send(){working.value=true;error.value='';try{demoCode.value=(await api<{demo_code:string}>('/safety/recovery-sms',{})).demo_code;}catch(e){error.value=(e as Error).message;}finally{working.value=false;}}
async function recover(){working.value=true;error.value='';try{data.value=await api<Status>('/safety/recover',{code:code.value});code.value='';demoCode.value='';}catch(e){error.value=(e as Error).message;}finally{working.value=false;}}
onMounted(()=>{void load();timer=window.setInterval(()=>{clock.value=Date.now()/1000+offset;},1000);});
onBeforeUnmount(()=>{if(timer)clearInterval(timer);});
</script>
<template>
  <section class="setting-section safety-panel" aria-label="安全状态">
    <div class="setting-title"><h3><ShieldCheck :size="16"/>安全状态</h3><button class="icon-button" aria-label="刷新安全状态" @click="error='';load()"><RefreshCw :size="15"/></button></div>
    <template v-if="data && user">
      <p>密码／验证码保护累计 {{data.guard.credential_trips}} / 3 次，可疑请求保护累计 {{data.guard.suspicious_trips}} / 3 次。任一类累计3次后，仅可申请人工解锁。</p>
      <strong :class="{'safety-warning':paused(user)}">{{paused(user)?'交易已暂停':'交易保护正常'}}</strong>
      <p v-if="user.requires_reauth">{{user.reason}} 查询和取消操作仍可使用。</p>
      <p v-else-if="remaining(user)">{{user.reason}} 剩余 {{remaining(user)}} 秒，你仍可查看账户与流水。</p>
      <p v-else>连续安全异常会暂停交易；普通业务失败和主动取消不会计入。冷却结束后可重试，后台会重新检查。</p>
      <template v-if="user.requires_reauth">
        <p v-if="!data.can_recover">先重新使用手机号短信登录，再回到这里验证恢复。</p>
        <button v-if="!data.can_recover" class="outline-button" @click="emit('login')">重新短信登录</button>
        <form v-else class="auth-inline-form" @submit.prevent="recover"><label>交易恢复验证码<input v-model="code" pattern="[0-9]{6}" inputmode="numeric" maxlength="6" required :disabled="working"/></label><div class="investment-buttons"><button type="button" :disabled="working" @click="send">获取恢复验证码</button><button type="submit" class="investment-primary" :disabled="working">验证并恢复交易</button></div></form>
      </template>
      <div v-for="scope in data.scopes.filter(s=>s.scope!=='transactions')" :key="scope.scope" class="platform-safety"><span>{{scope.label}}</span><span>{{remaining(scope)?`服务暂停 ${remaining(scope)} 秒`:scope.blocked_until?'等待一次恢复探测':'正常'}}</span></div>
      <details class="safety-records"><summary>查看安全记录（{{data.events.length}}）</summary><p v-if="!data.events.length">暂无安全异常记录。</p><div v-for="(item,i) in data.events" :key="i"><time>{{new Date(item.created_at).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false})}}</time><p>{{item.detail}}</p></div></details>
    </template>
    <p v-if="error" class="auth-error" role="alert">{{error}}</p>
    <aside v-if="demoCode" class="safety-sms" role="status"><button class="icon-button" aria-label="关闭演示恢复短信" @click="demoCode=''"><X :size="14"/></button><strong>演示短信</strong><p>交易恢复验证码：<b>{{demoCode}}</b></p><small>5分钟有效，仅用于本次恢复验证。</small></aside>
  </section>
</template>
<style scoped>
.setting-title h3{display:flex;align-items:center;gap:7px}.safety-warning{color:#ae6035}.platform-safety{display:flex;justify-content:space-between;gap:12px;font-size:12px;padding:10px 0;border-bottom:1px solid #edf0e9}.safety-records{margin-top:14px;font-size:12px}.safety-records summary{cursor:pointer}.safety-records>div{padding:10px 0;border-bottom:1px solid #edf0e9}.safety-records time{font-size:10px;color:#879282}.safety-records p{overflow-wrap:anywhere}.safety-sms{position:fixed;bottom:22px;right:22px;z-index:150;background:white;border:1px solid #dce5da;border-radius:14px;padding:18px;box-shadow:0 12px 36px #263c2522;max-width:calc(100vw - 44px)}.safety-sms button{float:right}.safety-sms small{color:#7e8b7c}
</style>
