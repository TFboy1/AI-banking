<script setup lang="ts">
import {onMounted,ref} from 'vue';
import {api} from '../types';
const enabled=ref(true),time=ref('09:00'),busy=ref(false),ready=ref(false),error=ref(''),saved=ref('');
let savedTime='09:00';
interface Preference{enabled:boolean;time:string;next_run?:string|null}
async function load(){try{const p=await api<Preference>('/daily-report/settings');enabled.value=p.enabled;time.value=savedTime=p.time;ready.value=true;}catch(e){error.value=(e as Error).message;}}
async function save(){if(!ready.value)return;busy.value=true;error.value='';saved.value='';try{const p=await api<Preference>('/daily-report/settings',{enabled:enabled.value,time:enabled.value?time.value:savedTime},'PATCH');enabled.value=p.enabled;time.value=savedTime=p.time;saved.value=p.enabled?'日报设置已保存':'自动日报已关闭';}catch(e){error.value=(e as Error).message;}finally{busy.value=false;}}
onMounted(load);
</script>
<template>
<section class="setting-section"><div class="setting-title"><h3>每日早报</h3></div><form class="auth-inline-form" @submit.prevent="save"><label class="daily-toggle"><input v-model="enabled" type="checkbox" :disabled="busy||!ready"/>开启自动日报</label><label>生成时间（北京时间）<input v-model="time" type="time" min="00:00" max="12:00" step="60" required :disabled="busy||!enabled||!ready" aria-label="日报生成时间"/></label><p>每日独立会话，数据截至当天00:00，统计完整昨天；临近生日从当天计算。</p><p>时间可设为00:00–12:00。后台需要运行；不打断当前对话。</p><button class="auth-primary" :disabled="busy||!ready">{{busy?'正在保存…':'保存日报设置'}}</button></form><p v-if="error" class="auth-error" role="alert">{{error}}</p><p v-if="saved" role="status">{{saved}}</p></section>
</template>
<style scoped>
.daily-toggle{display:flex!important;flex-direction:row!important;align-items:center;gap:10px}
.daily-toggle input{width:16px!important;min-height:16px!important;accent-color:#38684e}
@media(max-width:600px){
  .settings-scroll .auth-inline-form input[type=time]{width:148px;max-width:100%;height:40px;min-height:40px;padding:8px 10px;margin-top:6px;font-size:16px;line-height:normal}
}
</style>
