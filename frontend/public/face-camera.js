/* Shared by the full app and Feishu payment page. No image/file upload fallback. */
export async function runFace(start) {
 if(!window.isSecureContext||!navigator.mediaDevices?.getUserMedia)throw new Error('当前浏览器不支持摄像头，请通过HTTPS使用手机浏览器打开。');
 const controller=new AbortController();let stream=null,challenge=null,cancelled=false;
 const overlay=document.createElement('div');overlay.className='youshu-face-overlay';overlay.setAttribute('role','dialog');overlay.setAttribute('aria-modal','true');overlay.setAttribute('aria-label','人脸验证');
 const card=document.createElement('section');card.className='youshu-face-card';
 const title=document.createElement('h3');title.textContent='本人刷脸验证';
 const video=document.createElement('video');video.autoplay=true;video.muted=true;video.playsInline=true;video.setAttribute('aria-label','摄像头预览');
 const preview=document.createElement('div');preview.className='youshu-face-preview';
 const guide=document.createElement('div');guide.className='youshu-face-guide';guide.setAttribute('aria-hidden','true');preview.append(video,guide);
 const hint=document.createElement('p');hint.setAttribute('role','status');hint.textContent='正在打开摄像头…';
 const progress=document.createElement('p');progress.className='youshu-face-progress';progress.textContent='正在准备验证';
 const note=document.createElement('small');note.textContent='请将整张脸放入框内，保持手机稳定。';
 const close=document.createElement('button');close.type='button';close.textContent='取消验证';
 card.append(title,preview,hint,progress,note,close);overlay.append(card);document.body.append(overlay);
 const previous=document.activeElement;close.focus();
 const cancel=()=>{cancelled=true;controller.abort();stream?.getTracks().forEach(t=>t.stop());};
 const hidden=()=>{if(document.hidden)cancel();};
 const keyboard=e=>{if(e.key==='Escape')cancel();if(e.key==='Tab'){e.preventDefault();close.focus();}};
 close.onclick=cancel;document.addEventListener('visibilitychange',hidden);overlay.addEventListener('keydown',keyboard);
 const wait=ms=>new Promise((resolve,reject)=>{if(controller.signal.aborted){reject(new Error('已取消人脸验证。'));return;}const abort=()=>{clearTimeout(timer);reject(new Error('已取消人脸验证。'));};const timer=setTimeout(()=>{controller.signal.removeEventListener('abort',abort);resolve();},ms);controller.signal.addEventListener('abort',abort,{once:true});});
 let timeout;
 try{
  const camera=navigator.mediaDevices.getUserMedia({video:{facingMode:{ideal:'user'},width:{ideal:640},height:{ideal:480}},audio:false}).catch(error=>{
   if(!cancelled&&['NotFoundError','OverconstrainedError'].includes(error.name))return navigator.mediaDevices.getUserMedia({video:true,audio:false});throw error;
  }).then(value=>{if(cancelled){value.getTracks().forEach(t=>t.stop());throw new Error('已取消人脸验证。');}stream=value;return value;});
  await Promise.race([camera,new Promise((_,reject)=>{timeout=setTimeout(()=>{cancel();reject(new Error('打开摄像头超时，请检查权限后重试。'));},15000);controller.signal.addEventListener('abort',()=>reject(new Error('已取消人脸验证。')),{once:true});})]);clearTimeout(timeout);
  video.srcObject=stream;await video.play();if(cancelled)throw new Error('已取消人脸验证。');
  hint.textContent='正在核对验证信息…';
  const issuance=Promise.resolve().then(start).then(value=>{
   if(cancelled){if(value?.challenge_id)void fetch('/api/face/challenges/'+encodeURIComponent(value.challenge_id),{method:'DELETE',credentials:'same-origin',keepalive:true}).catch(()=>{});throw new Error('已取消人脸验证。');}return value;
  });
  challenge=await Promise.race([issuance,new Promise((_,reject)=>{controller.signal.addEventListener('abort',()=>reject(new Error('已取消人脸验证。')),{once:true});})]);
  if(cancelled)throw new Error('已取消人脸验证。');
  const canvas=document.createElement('canvas');const context=canvas.getContext('2d');
  if(!context)throw new Error('当前浏览器无法采集画面。');
  const deadline=Date.now()+120000;
  while(Date.now()<deadline){
   const steps=challenge.steps||4;
   progress.textContent=steps===1?'正在核对本人面容，通过后自动继续':'步骤 '+Math.min((challenge.step||0)+1,steps)+' / '+steps+' · 完成后自动核验';
   note.textContent=steps===1?'请将整张脸放入框内，正对摄像头，保持手机稳定。':'请将整张脸放入框内，按提示轻轻转脸，保持手机稳定。';
   hint.textContent=challenge.prompt||'请正对镜头';await wait(750);
   if(!video.videoWidth)continue;
   // Match the displayed object-fit:contain frame, including any letterboxing.
   const view=preview.getBoundingClientRect();
   const scale=Math.min(640/view.width,480/view.height);
   canvas.width=Math.round(view.width*scale);canvas.height=Math.round(view.height*scale);
   context.fillStyle='#17291b';context.fillRect(0,0,canvas.width,canvas.height);
   const fit=Math.min(canvas.width/video.videoWidth,canvas.height/video.videoHeight);
   const drawWidth=video.videoWidth*fit,drawHeight=video.videoHeight*fit;
   context.drawImage(video,(canvas.width-drawWidth)/2,(canvas.height-drawHeight)/2,drawWidth,drawHeight);
   const blob=await new Promise(resolve=>canvas.toBlob(resolve,'image/jpeg',.82));if(!blob)throw new Error('摄像头画面读取失败。');
   const response=await fetch('/api/face/challenges/'+encodeURIComponent(challenge.challenge_id)+'/frame',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'image/jpeg'},body:blob,signal:controller.signal,cache:'no-store'});
   const result=await response.json();if(response.status===429){hint.textContent='正在等待人脸服务…';await wait(1000);continue;}
   if(!response.ok)throw new Error(typeof result.detail==='string'?result.detail:'人脸验证失败，请重试。');
   if(result.done){hint.textContent='验证通过';return result;}
   if(result.replacing){title.textContent='请录入新的本人面容';}
   challenge={...challenge,...result};
  }
  throw new Error('人脸验证超时，请重新开始。');
 }catch(error){
  if(error.name==='NotAllowedError')throw new Error('未获得摄像头权限，请在浏览器设置中允许。');
  if(error.name==='NotFoundError')throw new Error('没有找到摄像头，请使用带摄像头的设备。');
  if(error.name==='AbortError'||cancelled)throw new Error('已取消人脸验证。');throw error;
 }finally{
  clearTimeout(timeout);cancel();
  if(challenge?.challenge_id)void fetch('/api/face/challenges/'+encodeURIComponent(challenge.challenge_id),{method:'DELETE',credentials:'same-origin',keepalive:true}).catch(()=>{});
  video.srcObject=null;overlay.remove();document.removeEventListener('visibilitychange',hidden);previous?.focus?.();
 }
}
