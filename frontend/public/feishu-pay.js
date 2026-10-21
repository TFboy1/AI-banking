/* No third-party scripts, credentials in query strings, or automatic retries. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const storageKey = 'youshu.feishu.payment';
  let token = location.hash.slice(1), context = null, faceToken = null, busy = false;
  try { if (token) sessionStorage.setItem(storageKey, token); else token = sessionStorage.getItem(storageKey) || ''; } catch (_) {}
  if (location.hash) history.replaceState(null, '', location.pathname);
  // Feishu may reuse the same webview for a second card. A fragment-only
  // navigation must not leave the previous transaction visible or selected.
  window.addEventListener('hashchange', () => { if (location.hash) location.reload(); });
  function panel(id) { ['loading','login','payment','result'].forEach(name => $(name).hidden = name !== id); }
  function error(message) { $('error').textContent = message; $('error').hidden = !message; }
  function locked(value) { busy = value; document.querySelectorAll('button,input,select').forEach(node => node.disabled = value); }
  async function api(path, body, scoped = false) {
    const headers = { 'Content-Type': 'application/json' };
    if (scoped) headers['X-Feishu-Payment'] = token;
    const response = await fetch('/api/' + path, { method: body === undefined ? 'GET' : 'POST', headers, body: body === undefined ? undefined : JSON.stringify(body), credentials: 'same-origin', cache: 'no-store' });
    const data = await response.json();
    if (!response.ok) { const exc = new Error(typeof data.detail === 'string' ? data.detail : '请求未通过校验，请核对信息。'); exc.status = response.status; throw exc; }
    return data;
  }
  const money = minor => '¥' + (minor / 100).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  function smsPopup(data) { $('demo-code').textContent = data.phone_masked + '，验证码：' + data.demo_code + '（5分钟内有效）'; $('demo-sms').hidden = false; }
  function finished(data) {
    panel('result'); $('refresh').hidden = true; $('switch').hidden = true;
    const scheduled = !!data.receipt?.scheduled;
    $('status-title').textContent = ({executed: scheduled ? '定时授权已保存' : '交易成功', cancelled: '交易已取消', failed: '交易未完成', expired: '交易已过期'})[data.status] || '请核对交易状态';
    $('status-icon').textContent = data.status === 'executed' ? '✓' : '—';
    $('status-detail').textContent = data.status === 'executed' ? (scheduled ? '当前未扣款，请关闭页面返回飞书查看预约。' : '请关闭页面返回飞书查看结果。') : '请返回飞书查看详情，或重新发起需求。';
    $('receipt').textContent = data.receipt?.action_id ? '回执编号：' + data.receipt.action_id : '';
    $('pin').value = ''; $('sms-code').value = ''; $('demo-sms').hidden = true;
  }
  async function load() {
    error(''); $('refresh').hidden = true;
    if (!token) { panel('loading'); $('loading').textContent = '请从飞书交易卡片打开此页面。'; return; }
    try {
      const state = await api('auth/status');
      if (!state.ready) { panel('login'); if(state.system_locked) throw new Error('系统已安全锁定，请回有数联系人工申请解锁。'); return; }
      $('switch').hidden = false;
      context = await api('feishu-pay/context', undefined, true);
      if(context.status !== 'pending') { finished(context); return; }
      panel('payment'); $('title').textContent = context.title;
      $('amount').textContent = typeof context.amount_minor === 'number' ? money(context.amount_minor) : '';
      $('fields').replaceChildren();
      context.fields.filter(f => f.label !== '付款账户').forEach(field => { const row = document.createElement('div'), dt = document.createElement('dt'), dd = document.createElement('dd'); dt.textContent = field.label; dd.textContent = field.value; row.append(dt,dd); $('fields').append(row); });
      $('account-label').hidden = !context.account_required; $('account').required = context.account_required;
      $('account').replaceChildren(new Option('请选择付款账户',''));
      context.accounts.forEach(account => { const option = new Option(account.name + ' · ' + money(account.balance_minor) + (account.reason ? ' · ' + account.reason : ''), account.id); option.disabled = !account.eligible; $('account').append(option); });
      $('account').value = context.account_id || '';
      $('sms-label').hidden = !context.sms_required; $('sms-code').required = context.sms_required;
      $('risk').textContent = context.sms_required ? '红色操作：人脸、支付密码和短信三重验证。' : '黄色操作：先刷脸，通过即可交易，也可改用密码。';
      faceToken=null;$('face-status').textContent='';$('pin').value='';$('password-fallback').hidden=context.sms_required;$('pin').closest('label').hidden=!context.sms_required;$('pin').required=context.sms_required;$('confirm').hidden=!context.sms_required;
      $('face-verify').hidden=context.face_enabled===false;
      if(context.face_enabled===false){passwordFallback();$('risk').textContent=context.sms_required?'红色操作：支付密码和短信验证。':'黄色操作：支付密码验证。';}
      if(context.basis === 'single_transaction') $('risk').textContent += ' 单笔金额门槛 ' + money(context.threshold_minor) + '。';
    } catch(exc) { panel('loading'); $('loading').textContent = '暂时无法打开交易'; error(exc.message); $('refresh').hidden = [403,404,409,410].includes(exc.status); $('switch').hidden = ![401,403,404].includes(exc.status); }
  }
  async function run(action) { if(busy) return; error(''); locked(true); try { await action(); } catch(exc) { error(exc.message || '连接中断，交易结果暂未确认，请核对状态后再操作。'); $('refresh').hidden = false; } finally { locked(false); } }
  $('login-sms').onclick = () => run(async () => { if(!$('phone').checkValidity()) { $('phone').reportValidity(); return; } smsPopup(await api('auth/sms', { phone: $('phone').value, purpose: 'login' })); });
  $('login-form').onsubmit = event => { event.preventDefault(); run(async () => { const state = await api('auth/login', {phone: $('phone').value, purpose:'login', code:$('login-code').value}); $('login-code').value = ''; $('demo-sms').hidden = true; if(!state.ready) throw new Error('请先在有数完成账号引导，再返回此页。'); await load(); }); };
  $('account').onchange = () => { faceToken=null;$('face-status').textContent='付款账户已变化，请重新验证人脸。'; $('sms-code').value = ''; $('demo-sms').hidden = true; error(''); };
  function passwordFallback(){faceToken=null;$('pin').closest('label').hidden=false;$('pin').required=true;$('confirm').hidden=false;$('pin').focus();}
  $('password-fallback').onclick=passwordFallback;
  $('face-verify').onclick=()=>run(async()=>{
    if(context.account_required&&!$('account').value)throw new Error('请先选择付款账户。');
    if(context.sms_required&&($('pin').value.length!==4||$('sms-code').value.length!==6))throw new Error('请先填写支付密码和短信验证码，再刷脸。');
    const module=await import('/face-camera.js?v=20261019-guide3');
    try{const result=await module.runFace(()=>api('feishu-pay/face',{purpose:'payment',account_id:$('account').value||null,...(context.sms_required?{pin:$('pin').value,sms_code:$('sms-code').value}:{})},true));faceToken=result.face_token;$('face-status').textContent='本人验证通过，90秒内有效。';}
    catch(exc){if(!context.sms_required)passwordFallback();throw exc;}
    if(!context.sms_required){finished(await api('feishu-pay/decision',{decision:'confirm',account_id:$('account').value||null,face_token:faceToken},true));}
  });
  $('pay-sms').onclick = () => run(async () => { if(context.account_required && !$('account').value) throw new Error('请先选择付款账户。'); smsPopup(await api('feishu-pay/sms', {account_id: $('account').value || null}, true)); });
  $('pay-form').onsubmit = event => { event.preventDefault(); run(async () => {
    if(context.sms_required&&context.face_enabled!==false&&!faceToken)throw new Error('请先验证本人面容。');
    const body = {face_token:faceToken, decision:'confirm', account_id:$('account').value || null, pin:$('pin').value, sms_code:context.sms_required ? $('sms-code').value : null};
    $('pin').value = '';
    try { finished(await api('feishu-pay/decision', body, true)); }
    catch(exc) {
      if(!exc.status) { await load(); throw new Error('连接中断，已重新查询交易状态；请以回执为准，勿重复提交。'); }
      throw exc;
    }
  }); };
  $('cancel').onclick = () => run(async () => finished(await api('feishu-pay/decision', {decision:'cancel'}, true)));
  $('refresh').onclick = () => run(load);
  $('switch').onclick = () => { panel('login'); error(''); $('switch').hidden = true; };
  $('hide-sms').onclick = () => $('demo-sms').hidden = true;
  $('close').onclick = () => { window.close(); $('status-detail').textContent = '请点击飞书左上角关闭按钮，返回聊天。'; };
  load();
})();
