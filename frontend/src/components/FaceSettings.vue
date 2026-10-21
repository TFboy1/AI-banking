<script setup lang="ts">
import {onMounted,ref} from 'vue';
import {MessageCircle} from '@lucide/vue';
import {api} from '../types';
import {runFace,faceStatus} from '../face';
const bound=ref(false),ready=ref(false),enabled=ref(true),busy=ref(false),error=ref(''),notice=ref('');
const serverEnabled=ref(true);
const emit=defineEmits<{changed:[enabled:boolean];enrolled:[]}>();
const purpose=ref(''),pin=ref(''),sms=ref(''),demo=ref(''),consent=ref(false);
async function refresh(){const state=await faceStatus();bound.value=state.bound;ready.value=state.ready;enabled.value=state.enabled!==false;serverEnabled.value=state.server_enabled!==false;emit('changed',enabled.value&&bound.value);}
onMounted(()=>refresh().catch(e=>error.value=e.message));
function choose(value:string){purpose.value=value;pin.value='';sms.value='';demo.value='';error.value='';notice.value='';consent.value=false;}
async function send(){busy.value=true;error.value='';try{demo.value=(await api<{demo_code:string}>('/face/sms',{purpose:purpose.value,pin:pin.value})).demo_code;}catch(e){error.value=(e as Error).message;}finally{busy.value=false;}}
async function capture(){busy.value=true;error.value='';try{
 const result=await runFace(()=>api('/face/challenge',{purpose:purpose.value,pin:pin.value,sms_code:sms.value,consent:consent.value}));
 const action=purpose.value;bound.value=!!result.bound;choose('');await refresh();notice.value=action==='enable'?'已开启本账户的人脸验证。':action==='disable'?'已关闭本账户的人脸验证，人脸档案已保留。':result.bound?'人脸绑定成功。':'人脸已解绑。';
 if(result.bound&&action==='enroll')emit('enrolled');
}catch(e){error.value=(e as Error).message;}finally{busy.value=false;pin.value='';sms.value='';demo.value='';}}
</script>
<template><section class="setting-section"><div class="setting-title"><h3>本人人脸</h3><span class="connection-state" :class="{ready:bound}">{{bound?'已绑定':'未绑定'}}</span></div><p>绑定并开启后，黄色交易可刷脸或使用密码，红色交易增加人脸验证。未绑定或关闭时不要求刷脸。</p><p v-if="!serverEnabled">服务器总开关已关闭，人脸功能暂不可用。</p><p v-else-if="!enabled&&bound">本账户已关闭人脸验证，黄色使用密码，红色使用密码和短信。人脸档案已保留。</p><p v-else-if="!ready" class="auth-error">本地人脸模型暂不可用。</p><div v-if="serverEnabled" class="investment-buttons"><button v-if="!bound" :disabled="busy||!ready" @click="choose('enroll')">开启人脸</button><template v-else><button v-if="!enabled" :disabled="busy||!ready" @click="choose('enable')">开启人脸</button><button v-else :disabled="busy||!ready" @click="choose('disable')">关闭人脸</button><button :disabled="busy||!ready" @click="choose('replace')">重新录入</button><button :disabled="busy||!ready" @click="choose('delete')">解绑人脸</button></template></div>
<form v-if="purpose" class="auth-inline-form" @submit.prevent="capture"><p v-if="bound">需先验证原人脸；原人脸无法识别时请联系人工处理，不能直接换绑。</p><label>支付密码<input v-model="pin" type="password" pattern="[0-9]{4}" maxlength="4" inputmode="numeric" autocomplete="off" required :disabled="busy"/></label><label>短信验证码<div class="auth-code-row"><input v-model="sms" pattern="[0-9]{6}" maxlength="6" inputmode="numeric" autocomplete="one-time-code" required :disabled="busy"/><button type="button" :disabled="busy||pin.length!==4" @click="send">{{busy?'发送中…':'获取验证码'}}</button></div></label><label class="face-consent"><input v-model="consent" type="checkbox" required :disabled="busy"/>我同意采集人脸特征用于本账户核验，本机加密保存，不保存照片和视频。删除后可重新绑定。</label><div class="investment-buttons"><button type="button" :disabled="busy" @click="choose('')">取消</button><button class="investment-primary" :disabled="busy||!consent">{{busy?'正在验证…':purpose==='enable'?'刷脸确认开启':purpose==='disable'?'刷脸确认关闭':bound?'验证原人脸':'开始录入'}}</button></div></form><p v-if="error" class="auth-error" role="alert">{{error}}</p><p v-if="notice" role="status">{{notice}}</p><aside v-if="demo" class="demo-sms face-settings-sms" role="status"><MessageCircle :size="20"/><div><strong>模拟短信 · 人脸设置</strong><p>本次验证码为 <b>{{demo}}</b>，5 分钟内有效。</p></div><button type="button" aria-label="关闭人脸设置短信" @click="demo=''">×</button></aside></section></template>
<style scoped>.face-consent{display:flex!important;align-items:flex-start;gap:8px;font-size:12px;line-height:1.7}.face-consent input{width:18px!important;min-height:18px;flex-shrink:0}.face-settings-sms{z-index:200}.face-settings-sms strong{font-size:13px}.face-settings-sms p{font-size:12px;line-height:1.7}.face-settings-sms b{font-size:19px;letter-spacing:3px}</style>
