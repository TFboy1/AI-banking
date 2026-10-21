<script setup lang="ts">
import {onBeforeUnmount,onMounted,ref,watch} from 'vue';
import {Mic,Square,X,LoaderCircle} from '@lucide/vue';
const props=defineProps<{disabled:boolean;context:string}>();
const emit=defineEmits<{text:[text:string];busy:[value:boolean]}>();
const recording=ref(false),processing=ref(false),starting=ref(false),seconds=ref(0),notice=ref('');
const pointerQuery=window.matchMedia('(hover: none) and (pointer: coarse)');
const touchMode=ref(pointerQuery.matches);
let held=false;
let stream:MediaStream|null=null,audio:AudioContext|null=null,source:MediaStreamAudioSourceNode|null=null;
let processor:ScriptProcessorNode|null=null,gain:GainNode|null=null;
let chunks:Float32Array[]=[],sampleRate=16000,timer:ReturnType<typeof setInterval>|null=null;
let generation=0,abort:AbortController|null=null;
// Prepare the audio graph without asking permission or opening the microphone.
function prepare(){
 if(audio&&audio.state!=='closed')return audio;
 const prepared=new AudioContext();
 try{
  const node=prepared.createScriptProcessor(4096,1,1),silent=prepared.createGain();
  silent.gain.value=0;
  node.onaudioprocess=e=>{if(recording.value)chunks.push(new Float32Array(e.inputBuffer.getChannelData(0)));};
  node.connect(silent);silent.connect(prepared.destination);
  audio=prepared;processor=node;gain=silent;
  if(prepared.state==='running')void prepared.suspend().catch(()=>{});
  return prepared;
 }catch(e){void prepared.close().catch(()=>{});throw e;}
}
function disposeAudio(){
 if(processor){processor.onaudioprocess=null;processor.disconnect();processor=null;}
 gain?.disconnect();gain=null;
 const previous=audio;audio=null;if(previous)void previous.close().catch(()=>{});
}
function release(){
 if(timer){clearInterval(timer);timer=null;}
 source?.disconnect();source=null;
 stream?.getTracks().forEach(track=>track.stop());stream=null;
 recording.value=false;
 if(audio?.state==='running')void audio.suspend().catch(()=>{});
}
function cancel(){const wasStarting=starting.value;held=false;generation++;abort?.abort();abort=null;release();if(wasStarting)disposeAudio();chunks=[];processing.value=false;starting.value=false;notice.value='';emit('busy',false);}
function press(event:PointerEvent){
 if(event.pointerType==='touch')return;
 if(event.button!==0||props.disabled||processing.value||starting.value||recording.value)return;
 held=true;try{(event.currentTarget as HTMLElement).setPointerCapture(event.pointerId);}catch{/* Older browsers still deliver release while the finger stays on the button. */}
 void start();
}
function touchPress(){
 if(props.disabled||processing.value||starting.value||recording.value)return;
 held=true;void start();
}
function pointerRelease(event:PointerEvent){if(event.pointerType!=='touch')releasePress();}
function pointerCancelled(event:PointerEvent){if(event.pointerType!=='touch')cancelPress();}
function releasePress(){
 held=false;
 if(recording.value)void finish();
 else if(starting.value){cancel();notice.value='录音尚未准备完成，请允许麦克风后重新长按。';}
}
function cancelPress(){if(recording.value||starting.value)cancel();}
function pointerChanged(){cancel();touchMode.value=pointerQuery.matches;}
function pageHidden(){if(document.hidden){if(recording.value||starting.value)cancel();disposeAudio();}else{try{prepare();}catch{/* Retry on the next user gesture. */}}}
function wav(parts:Float32Array[],rate:number){
 const size=parts.reduce((n,p)=>n+p.length,0),input=new Float32Array(size);
 let offset=0;for(const part of parts){input.set(part,offset);offset+=part.length;}
 const count=Math.floor(size*16000/rate),buffer=new ArrayBuffer(44+count*2),view=new DataView(buffer);
 const word=(at:number,s:string)=>{for(let i=0;i<s.length;i++)view.setUint8(at+i,s.charCodeAt(i));};
 word(0,'RIFF');view.setUint32(4,36+count*2,true);word(8,'WAVE');word(12,'fmt ');
 view.setUint32(16,16,true);view.setUint16(20,1,true);view.setUint16(22,1,true);
 view.setUint32(24,16000,true);view.setUint32(28,32000,true);view.setUint16(32,2,true);view.setUint16(34,16,true);
 word(36,'data');view.setUint32(40,count*2,true);
 for(let i=0;i<count;i++){
  const start=Math.floor(i*rate/16000),end=Math.min(size,Math.max(start+1,Math.floor((i+1)*rate/16000)));
  let sum=0;for(let j=start;j<end;j++)sum+=input[j];
  const value=Math.max(-1,Math.min(1,sum/(end-start)));view.setInt16(44+i*2,value<0?value*32768:value*32767,true);
 }
 return new Blob([buffer],{type:'audio/wav'});
}
async function start(){
 if(props.disabled||starting.value||processing.value)return;
 if(!window.isSecureContext||!navigator.mediaDevices?.getUserMedia){notice.value='当前浏览器无法录音，请通过HTTPS在手机浏览器中打开。';return;}
 const current=++generation;notice.value='';starting.value=true;emit('busy',true);
 let captured:MediaStream|null=null,accepting=true;
 let startupTimeout:ReturnType<typeof setTimeout>|undefined;
 try{
  const prepared=prepare();
  // Both calls run inside the gesture. A suspended AudioContext must not delay
  // the permission prompt. Late permission grants must never leave a mic open.
  const microphone=navigator.mediaDevices.getUserMedia({audio:{channelCount:1,echoCancellation:true,noiseSuppression:true},video:false}).then(value=>{
   if(!accepting||current!==generation){value.getTracks().forEach(track=>track.stop());throw new DOMException('Cancelled','AbortError');}
   captured=value;stream=value;return value;
  });
  await Promise.race([
   Promise.all([prepared.resume(),microphone]),
   new Promise<never>((_,reject)=>{startupTimeout=setTimeout(()=>reject(new DOMException('Microphone startup timed out','TimeoutError')),15000);})
  ]);
  if(current!==generation||props.disabled||(touchMode.value&&!held))return;
  stream=captured;
  sampleRate=prepared.sampleRate;chunks=[];seconds.value=0;source=prepared.createMediaStreamSource(stream!);
  source.connect(processor!);
  recording.value=true;starting.value=false;
  timer=setInterval(()=>{seconds.value++;if(seconds.value>=59)void finish();},1000);
 }catch(e){if(current!==generation)return;cancel();const name=(e as DOMException).name;
  notice.value=name==='NotAllowedError'?'没有获得麦克风权限，请在浏览器设置中允许录音。':name==='NotFoundError'?'没有找到可用的麦克风。':name==='TimeoutError'?'打开麦克风超时，请确认浏览器权限后重新长按。':'暂时无法录音，请重试或使用文字输入。';
 }finally{accepting=false;clearTimeout(startupTimeout);if(captured&&captured!==stream)(captured as MediaStream).getTracks().forEach(track=>track.stop());if(current===generation){starting.value=false;if(!recording.value)emit('busy',false);}}
}
async function finish(){
 if(!recording.value)return;
 const current=generation,context=props.context,parts=chunks,rate=sampleRate;
 release();chunks=[];processing.value=true;notice.value='';
 const controller=new AbortController();abort=controller;
 const timeout=setTimeout(()=>controller.abort(),90000);
 try{
  const blob=wav(parts,rate);if(blob.size<6444)throw new Error('录音太短，请说完后再结束录音。');
  const response=await fetch('/api/speech/transcribe',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'audio/wav'},body:blob,signal:controller.signal});
  const result=await response.json();if(!response.ok)throw new Error(result.detail||'语音识别未完成，请重试。');
  if(current!==generation||context!==props.context||props.disabled)return;
  emit('text',result.text);notice.value='已转为文字，请核对姓名和金额后发送。';
 }catch(e){if(current===generation)notice.value=(e as Error).name==='AbortError'?'已停止识别，请重试或使用文字输入。':(e as Error).message;}
 finally{clearTimeout(timeout);if(current===generation){processing.value=false;abort=null;emit('busy',false);}}
}
watch(()=>[props.context,props.disabled],(next,previous)=>{if(next[0]!==previous[0]||props.disabled||recording.value||processing.value||starting.value)cancel();});
onMounted(()=>{try{prepare();}catch{/* Unsupported/limited browsers retry on user gesture. */}pointerQuery.addEventListener('change',pointerChanged);document.addEventListener('visibilitychange',pageHidden);});
onBeforeUnmount(()=>{cancel();disposeAudio();pointerQuery.removeEventListener('change',pointerChanged);document.removeEventListener('visibilitychange',pageHidden);});
</script>
<template>
 <div class="voice-input">
  <button v-if="touchMode" type="button" class="voice-button voice-hold" :class="{'is-recording':recording}" :disabled="disabled||processing" :aria-label="recording?'松手结束录音':'长按语音输入'" title="长按录音，松手结束" @pointerdown.prevent="press" @pointerup.prevent="pointerRelease" @pointercancel="pointerCancelled" @touchstart.prevent="touchPress" @touchend.prevent="releasePress" @touchcancel.prevent="cancelPress" @contextmenu.prevent @click.prevent><LoaderCircle v-if="starting||processing" class="voice-spinner" :size="17"/><template v-else-if="recording"><Square :size="14" fill="currentColor"/><span>{{seconds}}秒</span></template><Mic v-else :size="18"/></button>
  <template v-else><button v-if="recording" type="button" class="voice-button is-recording" aria-label="结束录音并转文字" @click="finish"><Square :size="14" fill="currentColor"/><span>{{seconds}}秒</span></button>
  <button v-else type="button" class="voice-button" :disabled="disabled||starting||processing" aria-label="开始语音输入" title="语音转文字" @click="start"><LoaderCircle v-if="starting||processing" class="voice-spinner" :size="17"/><Mic v-else :size="18"/></button></template>
  <button v-if="processing||(!touchMode&&(recording||starting))" type="button" class="voice-cancel" aria-label="取消语音输入" @click="cancel"><X :size="15"/></button>
  <span v-if="recording||processing||starting||notice" class="voice-status" role="status">{{recording?(touchMode?'正在录音，松手结束':'正在录音，点击方块结束'):processing?'正在转为文字…':starting?'正在打开麦克风…':notice}}</span>
 </div>
</template>
<style scoped>
.voice-hold *{pointer-events:none}.voice-hold{touch-action:none;user-select:none;-webkit-user-select:none;-webkit-touch-callout:none}.voice-input{display:flex;align-items:center;gap:4px}.voice-button,.voice-cancel{border:0;background:#eef3e8;color:#63804e;border-radius:10px;min-width:36px;min-height:38px;display:flex;align-items:center;justify-content:center;gap:5px;padding:7px;cursor:pointer}.voice-button:disabled{opacity:.5;cursor:wait}.voice-button.is-recording{background:#fff0e8;color:#ae6049;font-size:11px}.voice-cancel{background:transparent;color:#8b9681;min-width:28px}.voice-status{position:absolute;bottom:100%;left:12px;right:12px;margin-bottom:7px;border:1px solid #e2e9db;background:#fcfdf8;padding:8px 11px;border-radius:9px;font-size:11px;line-height:1.6;color:#6b805c;overflow-wrap:anywhere;pointer-events:none}.voice-spinner{animation:voice-turn 1s linear infinite}@keyframes voice-turn{to{transform:rotate(360deg)}}
</style>
