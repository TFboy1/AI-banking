<script setup lang="ts">
import {computed,ref,watch} from 'vue';
const props=defineProps<{request:(path:string,body?:object)=>Promise<any>;users:{id:string;username:string}[];userId:string}>();
const entries=ref<{id:string;username:string;result:any}[]>([]),busy=ref(false),error=ref('');
const targets=computed(()=>[...props.users.filter(u=>!props.userId||u.id===props.userId),...(!props.userId?[{id:'__admin__',username:'管理员操作记录'}]:[])]);
const labels:Record<string,string>={verified:'已签名 · 校验通过',pending:'新增记录待签名',unanchored:'尚未建立独立检查点',alert:'发现异常 · 停止签名',unavailable:'凭据不可用'};
let version=0;
async function check(sealId?:string){const requestVersion=++version;busy.value=true;error.value='';try{
 const results=await Promise.all(targets.value.map(async u=>({ ...u,result:await props.request(`/audit/users/${encodeURIComponent(u.id)}/integrity`+(sealId===u.id?'/checkpoint':''),sealId===u.id?{}:undefined)})));
 if(requestVersion===version)entries.value=results;
}catch(e){if(requestVersion===version)error.value=(e as Error).message;}finally{if(requestVersion===version)busy.value=false;}}
async function download(id:string){try{const response=await fetch(`/api/admin/audit/users/${encodeURIComponent(id)}/integrity/export`,{credentials:'same-origin'});if(!response.ok)throw new Error('校验凭据导出失败，请检查管理员登录。');const href=URL.createObjectURL(await response.blob()),link=document.createElement('a');link.href=href;link.download='有数审计校验凭据.json';link.click();setTimeout(()=>URL.revokeObjectURL(href),1000);}catch(e){error.value=(e as Error).message;}}
const at=(value:string)=>value?new Date(value).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false}):'—';
watch(()=>targets.value.map(u=>u.id).join(','),()=>check(),{immediate:true});
</script>
<template><section class="integrity-view" aria-label="审计防篡改校验">
 <div class="integrity-heading"><div><h2>审计防篡改校验</h2><p>核对记录链、当前数据及独立进程保存的签名凭据。自动签名开启且管理员服务运行时，约每10秒检查并签名新增记录。</p></div><button :disabled="busy" @click="check()">{{busy?'正在校验…':'重新校验'}}</button></div>
 <p v-if="error" role="alert" class="integrity-error">{{error}}</p>
 <article v-for="entry in entries" :key="entry.id" :class="['integrity-card',entry.result.status]">
  <header><h3>{{entry.username}}</h3><strong>{{labels[entry.result.status]||'待核对'}}</strong></header>
  <p>{{entry.result.message}}</p>
  <dl><div><dt>当前记录数</dt><dd>{{entry.result.count??'—'}}</dd></div><div><dt>已签名记录数</dt><dd>{{entry.result.anchored_count??'—'}}</dd></div><div><dt>最近签名时间</dt><dd>{{at(entry.result.checkpoint_at)}}</dd></div><div><dt>基线建立时间</dt><dd>{{at(entry.result.baseline_at)}}</dd></div></dl>
  <p class="integrity-note">{{entry.result.baseline_note}}</p><p class="integrity-note">{{entry.result.protection_note}}</p>
  <details v-if="entry.result.head_hash"><summary>查看校验依据</summary><p>当前链头 {{entry.result.head_hash}}</p><p>签名公钥标识 {{entry.result.key_id}} · Ed25519</p><p>范围：{{entry.result.scope?.join('、')}}</p><p>检查时间 {{at(entry.result.checked_at)}}</p></details>
  <footer><button :disabled="busy||['alert','unavailable'].includes(entry.result.status)" @click="check(entry.id)">签名新增记录</button><button :disabled="busy" @click="download(entry.id)">导出校验凭据</button></footer>
 </article><p v-if="!targets.length">暂无可校验用户。</p>
</section></template>
<style scoped>
.integrity-view{padding:26px 30px;overflow:auto;flex:1;color:#405e43}.integrity-heading{display:flex;justify-content:space-between;gap:20px;align-items:center}.integrity-heading h2{font-size:18px;margin:0 0 10px}.integrity-heading p,.integrity-card p{font-size:12px;line-height:1.9}.integrity-heading p{color:#8b9b7f}.integrity-view button{font:inherit;font-size:12px;min-height:40px;padding:9px 14px;background:#f7f9f1;border:1px solid #dbe5d2;color:#466947;border-radius:8px;cursor:pointer;white-space:nowrap}.integrity-view button:disabled{opacity:.5;cursor:default}.integrity-card{background:white;border:1px solid #dce6d3;border-radius:12px;padding:22px;margin:20px 0}.integrity-card.alert,.integrity-card.unavailable{border-color:#dca28b}.integrity-card header{display:flex;justify-content:space-between;gap:20px;align-items:center}.integrity-card h3{font-size:15px;margin:0}.integrity-card header strong{font-size:12px;color:#4c8057}.integrity-card.alert header strong,.integrity-error{color:#af6550}.integrity-card.pending header strong,.integrity-card.unanchored header strong{color:#a38143}.integrity-card dl{display:flex;flex-wrap:wrap;gap:24px;margin:22px 0}.integrity-card dt{font-size:11px;color:#8b9d80}.integrity-card dd{font-size:14px;margin:8px 0 0}.integrity-note{color:#91a086}.integrity-card details{font-size:11px;color:#7c9273;overflow-wrap:anywhere}.integrity-card summary{cursor:pointer;min-height:32px}.integrity-card footer{display:flex;gap:10px;margin-top:18px;flex-wrap:wrap}
</style>
