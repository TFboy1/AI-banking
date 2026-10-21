<script setup lang="ts">
import {onMounted,onBeforeUnmount,ref} from 'vue';
import {ShieldAlert,Phone} from '@lucide/vue';
import {api} from '../types';
interface Guard{system_locked:boolean;reason:string;request:{id:string;status:string;review_note?:string}|null}
const status=ref<Guard|null>(null),note=ref(''),error=ref(''),working=ref(false);
const messages=ref<{id:string;role:string;text:string;data:{source?:string}}[]>([]),draft=ref(''),service=ref<{status:string}|null>(null);
let timer:ReturnType<typeof setInterval>|undefined,polling=false;
async function chat(){try{const result=await api<{session:typeof service.value;messages:typeof messages.value}>('/handoff');service.value=result.session;messages.value=result.messages.filter(m=>m.data.source==='human'||m.role==='user');}catch(e){error.value=(e as Error).message;}}
async function send(){if(!draft.value.trim()||working.value)return;working.value=true;try{await api('/handoff/messages',{text:draft.value.trim()});draft.value='';await chat();}catch(e){error.value=(e as Error).message;}finally{working.value=false;}}
async function refresh(){error.value='';try{status.value=await api<Guard>('/safety/guard');if(!status.value.system_locked)window.location.reload();}catch(e){error.value=(e as Error).message;}}
async function submit(){working.value=true;error.value='';try{status.value=await api<Guard>('/safety/unlock-request',{note:note.value});await chat();}catch(e){error.value=(e as Error).message;}finally{working.value=false;}}
onMounted(async()=>{await refresh();await chat();timer=setInterval(async()=>{if(polling)return;polling=true;try{await refresh();if(status.value?.request)await chat();}finally{polling=false;}},2500);});
onBeforeUnmount(()=>clearInterval(timer));
</script>
<template>
  <main class="manual-lock"><section class="lock-card" aria-label="系统安全锁定">
    <ShieldAlert :size="36"/><h1>系统已安全锁定</h1>
    <p>{{status?.reason||'安全保护多次触发，当前仅保留人工申请入口。'}}</p>
    <p>重新登录、短信验证或等待冷却都不能解除。已完成交易保留，未完成的操作已停止。</p>
    <h2><Phone :size="18"/>申请人工解锁</h2>
    <p>申请将提交到人工服务工作台，管理员审核后会在这里显示结果，也可以直接与客服沟通。</p>
    <div v-if="status?.request" class="review-state" role="status"><strong>{{status.request.status==='pending'?'申请已提交，等待人工审核':status.request.status==='rejected'?'申请未通过，系统仍保持锁定':'人工审核已通过'}}</strong><p>申请编号：{{status.request.id}}</p><p v-if="status.request.review_note">审核说明：{{status.request.review_note}}</p></div>
    <form v-if="status && status.request?.status!=='pending'" @submit.prevent="submit"><label>申请说明<textarea v-model="note" maxlength="500" required rows="4" placeholder="请简要说明需要解锁的情况"/></label><button type="submit" :disabled="working||!note.trim()">{{working?'正在提交…':'提交人工解锁申请'}}</button></form>
    <button type="button" class="refresh-review" :disabled="working" @click="refresh">刷新审核状态</button>
    <section v-if="service && service.status!=='closed'" class="support-chat"><h2>人工沟通 · {{service.status==='active'?'客服已接入':'等待接入'}}</h2><div class="support-messages" role="log"><p v-for="m in messages" :key="m.id"><strong>{{m.role==='user'?'你':'人工客服'}}：</strong>{{m.text}}</p></div><form @submit.prevent="send"><label>给客服留言<textarea v-model="draft" maxlength="4000" rows="2" placeholder="描述你需要的帮助"/></label><button :disabled="working||!draft.trim()">发送消息</button></form></section>
    <p v-if="error" class="auth-error" role="alert">{{error}}</p>
  </section></main>
</template>
<style scoped>
.manual-lock{min-height:100vh;display:grid;place-items:center;padding:24px;background:#f4f7f1;color:#2d483a}.lock-card{width:min(100%,540px);background:white;border:1px solid #dce4d8;border-radius:22px;padding:32px;box-shadow:0 15px 55px #24442e0d}.lock-card>svg{color:#a76e39}.lock-card h1{font-size:24px;margin:18px 0}.lock-card h2{display:flex;align-items:center;gap:9px;font-size:17px;margin:26px 0 12px}.lock-card p{font-size:13px;line-height:1.8;color:#778473;overflow-wrap:anywhere}.lock-card label{display:grid;gap:10px;font-size:13px}.lock-card textarea{padding:12px;border:1px solid #d9e1d5;border-radius:10px;resize:vertical;font:inherit;color:inherit;background:#fafcf8}.lock-card button{margin-top:16px;border:0;border-radius:10px;padding:12px 18px;background:#285946;color:white;cursor:pointer}.lock-card button:disabled{opacity:.5;cursor:default}.lock-card .refresh-review{background:#edf2e9;color:#42604b}.review-state{background:#f4f7ef;border-radius:12px;padding:14px;font-size:13px}@media(max-width:500px){.manual-lock{padding:14px}.lock-card{padding:24px}}
</style>
<style scoped>.support-chat{border-top:1px solid #e1e7dc;margin-top:24px}.support-messages{max-height:220px;overflow:auto;background:#f6f8f2;padding:10px;border-radius:10px}.support-messages p{white-space:pre-wrap}.support-chat textarea{width:100%}</style>
