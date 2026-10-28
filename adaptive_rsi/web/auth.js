'use strict';
window.RSIAuth = (() => {
  let snapshot=null,providers=null,context=null,providerQuery='',loading=null,pending=false,polling=false,pollingStopped=false,timer=null;
  const sessions=new Map(),selections=new Map();
  // 记住每个后端上次的供应商选择，重开页面后不必重新查找。
  try{for(const [backend,provider] of Object.entries(JSON.parse(localStorage.getItem('rsi-auth-selection')||'{}')))selections.set(backend,provider);}catch{}
  function rememberSelection(backend,provider){
    selections.set(backend,provider);
    try{localStorage.setItem('rsi-auth-selection',JSON.stringify(Object.fromEntries(selections)));}catch{}
  }
  const authErrors={invalid_response:'登录服务返回格式不兼容，请更新运行机器上的工具后重试。',auth_credentials_busy:'模型正在使用此账号，请暂停项目并等待当前调用结束后重试。',auth_container_unavailable:'服务器的容器服务不可用，请启动 Docker 后重试。',auth_worker_image_missing:'服务器尚未准备认证运行镜像，请先完成运行环境安装。',codex_missing:'请先在运行机器安装 Codex，再返回页面登录。',opencode_missing:'请先在运行机器安装 OpenCode，再返回页面登录。',opencode_version_unknown:'无法确认 OpenCode 版本，请升级 Adaptive RSI，或临时使用 OpenCode 1.19.3。',invalid_provider:'请重新选择供应商。',invalid_auth_method:'请刷新供应商列表后重新选择登录方式。',invalid_auth_inputs:'请填写官方登录需要的信息。',invalid_auth_code:'请重新粘贴官方页面的授权码。',invalid_api_key:'请输入有效的供应商密钥。',unsupported_auth_method:'此登录方式暂不支持远程页面，请使用供应商密钥。',auth_busy:'已有登录正在进行，请先完成或取消。',device_auth_unavailable:'设备登录暂不可用，请在账号设置中启用设备授权后重试。',authorization_failed:'官方授权未完成，请重新登录。',auth_protocol_error:'登录响应无法识别，请更新运行机器上的工具后重试。',auth_timeout:'等待登录超时，请重新登录。',auth_expired:'登录已过期，请重新登录。',process_interrupted:'登录已中断，请重新登录。',invalid_credentials:'账号凭据失效，请重新登录。',unknown_session:'登录会话已结束，请重新登录。',service_closed:'服务正在关闭，请刷新页面后重试。',invalid_auth_state:'此登录步骤已结束，请重新登录。'};
  const terminal=new Set(['completed','cancelled','expired','failed','interrupted']);
  const labels={authenticated:'账号已连接',signed_out:'尚未登录',unavailable:'运行机器尚未安装此工具',starting:'正在准备官方登录…',awaiting_authorization:'等待你在官方页面完成授权',completing:'正在确认授权…',completed:'登录成功，正在更新模型目录。',cancelled:'已取消登录',expired:'登录已过期，请重新登录。',failed:'登录失败，请查看提示后重试。',interrupted:'登录已中断，请重新登录。'};
  function authMessage(code,fallback){
    const match=String(code||'').match(/^opencode_incompatible_(\d+\.\d+\.\d+)$/);
    if(match)return t('已检测到 OpenCode ')+match[1]+t('，当前命令参数不兼容。请升级 Adaptive RSI，或临时使用 OpenCode 1.19.3。');
    return t(authErrors[code]||fallback||'登录服务暂时不可用，请稍后重试。');
  }
  const key=()=>context.backend+':'+(context.provider||'');
  const text=(tag,value,cls)=>el(tag,t(value),cls);
  function external(url,label){
    try{const parsed=new URL(url);if(parsed.protocol!=='https:')return null;const link=text('a',label,'button');link.href=parsed.href;link.target='_blank';link.rel='noopener noreferrer';return link;}catch{return null;}
  }
  function selectPreferredProvider(){
    if(!context||context.backend!=='opencode'||context.provider||!providers)return false;
    const authenticated=snapshot?.opencode?.authenticated_providers||[];
    const preferred=providers.find(provider=>provider.connected)||providers.find(provider=>authenticated.includes(provider.id));
    if(!preferred)return false;
    context.provider=preferred.id;selections.set(context.backend,preferred.id);return true;
  }
  async function status(force=false){
    if(loading)return loading;if(snapshot&&!force)return;
    loading=(async()=>{snapshot=await api('auth/status');
      for(const metadata of snapshot.sessions||[]){const id=metadata.backend+':'+(metadata.provider||'');if(terminal.has(metadata.state)||sessions.get(id)?.id===metadata.id)continue;
        const recovered=await api('auth/session?id='+encodeURIComponent(metadata.id)),refresh=context?.backend===metadata.backend?context.refresh:()=>api('model-center/fetch',{provider:metadata.backend==='codex'?'openai':'opencode',project:null});sessions.set(id,{...recovered,refresh});
        if(context?.backend===metadata.backend&&!context.provider&&metadata.provider){context.provider=metadata.provider;selections.set(metadata.backend,metadata.provider);}
      }selectPreferredProvider();render();schedule();
    })().finally(()=>loading=null);return loading;
  }
  async function getProviders(){if(providers)return;const result=await api('auth/providers?backend=opencode');providers=Array.isArray(result)?result:result.providers;
    selectPreferredProvider();render();}
  async function mutate(path,body){pending=true;render();try{return await api('auth/'+path,body);}catch(error){error.message=authMessage(error.code,error.message);throw error;}finally{pending=false;render();}}
  function schedule(){clearTimeout(timer);if(!pollingStopped&&[...sessions.values()].some(s=>!terminal.has(s.state)||s.refreshStage)){const delay=document.visibilityState==='hidden'?30000:1500;timer=setTimeout(()=>safe(poll),delay);}}
  async function refreshCompleted(id,session){
    if(session.refreshing||sessions.get(id)!==session)return;session.refreshing=true;
    try{
      if(session.refreshStage==='status'){
        readinessCache.clear();await status(true);if(sessions.get(id)!==session)return;
        session.refreshStage='catalog';
      }
      await session.refresh();if(sessions.get(id)!==session)return;
      session.refreshStage='';session.refreshAttempts=0;session.pollMessage='';
    }catch(error){
      if(sessions.get(id)!==session)return;
      if(error.code==='session_required'){pollingStopped=true;session.pollMessage='控制台登录已失效，请重新打开控制台后继续授权。';}
      else{
        // 固定输入类错误重试不会成功；瞬时故障最多自动重试 4 次，避免无限轮询。
        const transient=error instanceof TypeError||error.name==='AbortError'||error.status>=500;
        session.refreshAttempts=(session.refreshAttempts||0)+1;
        if(transient&&session.refreshAttempts<5)session.pollMessage='登录已完成，账号状态或模型目录更新失败，正在重试。';
        else{
          session.refreshStage='';session.refreshAttempts=0;
          session.pollMessage='登录已完成，但模型目录刷新失败。可稍后在模型列表手动刷新目录，或重新登录。';
        }
      }
    }finally{session.refreshing=false;}
  }
  async function poll(){
    if(polling||pollingStopped)return;polling=true;
    try{for(const [id,session] of sessions){
      if(session.refreshStage){await refreshCompleted(id,session);continue;}
      if(terminal.has(session.state))continue;
      try{const next=await api('auth/session?id='+encodeURIComponent(session.id));if(sessions.get(id)!==session)continue;
        const updated={...next,refresh:session.refresh,pollMessage:'',refreshStage:next.state==='completed'?'status':''};sessions.set(id,updated);
        if(updated.refreshStage)await refreshCompleted(id,updated);
      }catch(error){
        if(sessions.get(id)!==session)continue;
        if(error.code==='session_required'){pollingStopped=true;sessions.set(id,{...session,pollMessage:'控制台登录已失效，请重新打开控制台后继续授权。'});continue;}
        const terminalState={auth_expired:'expired',auth_timeout:'expired',process_interrupted:'interrupted',unknown_session:'interrupted',invalid_auth_state:'interrupted',authorization_failed:'failed'}[error.code];
        if(terminalState){sessions.set(id,{...session,state:terminalState,error:error.code,pollMessage:''});continue;}
        if(error instanceof TypeError||error.name==='AbortError'||error.status>=500){sessions.set(id,{...session,pollMessage:'暂时无法连接，授权状态已保留，正在重试。'});continue;}
        sessions.set(id,{...session,state:'failed',error:error.code||'authorization_failed',pollMessage:''});
      }
    }}finally{polling=false;render();schedule();}
  }
  async function completeSession(id,session,code){
    const next=await mutate('complete',{id:session.id,code});if(sessions.get(id)!==session)return;
    const updated={...next,refresh:session.refresh,refreshStage:next.state==='completed'?'status':''};sessions.set(id,updated);
    if(updated.refreshStage)await refreshCompleted(id,updated);render();schedule();
  }
  async function start(method,inputs={}){
    if(pending||!context)return;const current={...context},existing=sessions.get(current.backend+':'+(current.provider||''));if(existing&&!terminal.has(existing.state))return;
    const session=await mutate('start',{backend:current.backend,provider:current.backend==='codex'?null:current.provider,method:method.index,inputs});
    sessions.set(current.backend+':'+(current.provider||''),{...session,refresh:current.refresh});render();schedule();
  }
  function render(){
    const box=$('mc-auth'),same=box.dataset.authContext===(context?key():''),values=new Map();let focus=null;
    if(same)for(const node of box.querySelectorAll('[data-auth-field]')){values.set(node.dataset.authField,node.value);if(document.activeElement===node)focus={key:node.dataset.authField,start:node.selectionStart,end:node.selectionEnd};}
    renderBody();box.dataset.authContext=context?key():'';
    if(same)for(const node of box.querySelectorAll('[data-auth-field]')){if(values.has(node.dataset.authField))node.value=values.get(node.dataset.authField);if(focus?.key===node.dataset.authField){node.focus({preventScroll:true});if(typeof focus.start==='number')node.setSelectionRange(focus.start,focus.end);}}
    if(same)box.querySelectorAll('[data-auth-prompt]').forEach(node=>node.dispatchEvent(new Event('change')));
  }
  function renderBody(){
    const box=$('mc-auth');if(!context){box.hidden=true;return;}box.hidden=false;box.replaceChildren();
    const sessionKey=key(),backend=context.backend,account=snapshot?.[backend],session=sessions.get(sessionKey),connected=backend==='opencode'?account?.authenticated_providers?.includes(context.provider):account?.state==='authenticated';
    if($('mc-step-account'))$('mc-step-account').dataset.state=connected?'done':'current';
    if(connected&&$('mc-step-models')?.dataset.state==='pending')$('mc-step-models').dataset.state='current';
    box.append(text('h3','账号连接'),text('p',snapshot?(connected?'账号已连接':account?.available===false?'运行机器尚未安装此工具':'尚未登录'):'正在读取账号状态…','mc-secondary'));
    if(account?.error)box.append(el('p',authMessage(account.error,account.error),'mc-error'));
    if(backend==='opencode'){
      box.append(text('p','OpenCode Go 使用 API Key。请在供应商列表选择对应的 OpenCode 服务并保存密钥；ChatGPT 登录不提供 Go 凭据。','mc-secondary'));
      const guide=external('https://opencode.ai/v2/docs/console/go/','OpenCode Go 官方说明');if(guide)box.append(guide);
      const label=text('label','先选择 OpenCode 供应商'),select=el('select');select.id='mc-auth-provider';select.setAttribute('aria-label',t('OpenCode 供应商'));select.dataset.authField='provider';
      const search=el('input');search.type='search';search.id='mc-auth-provider-search';search.value=providerQuery;search.dataset.authField='provider-query';
      search.placeholder=t('搜索供应商');search.setAttribute('aria-label',t('搜索供应商'));
      const fill=()=>{const query=providerQuery.trim().toLocaleLowerCase();const first=text('option','选择供应商');first.value='';select.replaceChildren(first);
        for(const provider of (providers||[]).filter(item=>!query||item.name.toLocaleLowerCase().includes(query)||item.id.toLocaleLowerCase().includes(query))){
          const option=el('option',provider.name+(provider.connected?' · '+t('已连接'):''));option.value=provider.id;select.append(option);
        }
        select.value=context.provider||'';};
      fill();search.addEventListener('input',()=>{providerQuery=search.value;fill();});
      select.addEventListener('change',()=>{context.provider=select.value;rememberSelection(backend,select.value);render();});
      box.append(search);label.append(select);box.append(label);
      if(!providers)box.append(action(t('读取可连接的供应商'),()=>getProviders(),'text-button'));
      if(!context.provider)return;
    }
    const running=session&&!terminal.has(session.state);
    if(session){
      const feedback=text('p',labels[session.state]||session.state,session.state==='failed'?'mc-error':'mc-secondary');feedback.setAttribute('role','status');box.append(feedback);
      if(session.pollMessage)box.append(el('p',t(session.pollMessage),'mc-secondary'));
      if(running){
        const link=external(session.verification_url,'打开官方授权页面');if(link)box.append(link);
        if(session.user_code){const code=el('p',undefined,'mc-auth-code');code.append(text('span','设备验证码'),el('strong',session.user_code));box.append(code);}
        if(session.instructions)box.append(el('p',session.instructions,'mc-secondary'));
        if(session.expires_at)box.append(el('p',t('有效期至 ')+stamp(session.expires_at),'mc-secondary'));
        if(session.method==='code'){
          const form=el('form'),label=text('label','粘贴官方页面返回的授权码'),input=el('input');input.autocomplete='off';input.required=true;input.dataset.authField='code:'+session.id;label.append(input);const submit=text('button','完成授权','button primary');submit.type='submit';submit.disabled=pending;form.append(label,submit);
          form.addEventListener('submit',event=>{event.preventDefault();safe(async()=>{const value=input.value;input.value='';await completeSession(sessionKey,session,value);});});box.append(form);
        }
        const cancel=action(t('取消登录'),async()=>{const next=await mutate('cancel',{id:session.id});sessions.set(sessionKey,{...next,refresh:session.refresh});render();},'text-button');cancel.disabled=pending;box.append(cancel);
      }
      if(session.error)box.append(el('p',authMessage(session.error,session.error),'mc-error'));
    }
    if(running)return;
    if(connected){const logout=action(t('退出账号'),async()=>{const current={...context};await mutate('logout',{backend:current.backend,provider:current.backend==='codex'?null:current.provider});sessions.delete(current.backend+':'+(current.provider||''));await status(true);readinessCache.clear();},'text-button');logout.disabled=pending;box.append(logout);}
    const methods=backend==='codex'?[{index:0,type:'oauth',supported:true,label:t('前往 Codex 官方登录')}]:providers?.find(p=>p.id===context.provider)?.methods||[];
    for(const method of methods){
      if(method.type==='api'){
        if(method.supported===false){box.append(el('p',authMessage(method.unsupported_reason,'此登录方式暂不支持远程页面，请使用供应商密钥。'),'mc-secondary'));continue;}
        const form=el('form'),label=text('label','API 密钥'),input=el('input');input.type='password';input.dataset.authField='key:'+context.provider;input.autocomplete='new-password';input.required=true;input.maxLength=8192;label.append(input);const submit=text('button','保存供应商密钥','button');submit.type='submit';submit.disabled=pending;form.append(label,submit);
        form.addEventListener('submit',event=>{event.preventDefault();const current={...context};safe(async()=>{const secret=input.value;input.value='';await mutate('key',{backend:'opencode',provider:current.provider,key:secret});await status(true);readinessCache.clear();await current.refresh();});});box.append(form);continue;
      }
      const form=el('form'),fields=new Map();
      const updatePrompts=()=>{for(const prompt of method.prompts||[]){const field=fields.get(prompt.key),condition=prompt.when,show=!condition||(condition.op==='neq'?fields.get(condition.key)?.input.value!==condition.value:fields.get(condition.key)?.input.value===condition.value);field.label.hidden=!show;field.input.disabled=!show;field.input.required=show;}};
      for(const prompt of method.prompts||[]){const label=el('label',prompt.message),input=el(prompt.type==='select'?'select':'input');input.dataset.authField='prompt:'+method.index+':'+prompt.key;input.dataset.authPrompt='true';input.setAttribute('aria-label',prompt.message);if(prompt.placeholder)input.placeholder=prompt.placeholder;if(prompt.type==='select')for(const option of prompt.options||[]){const node=el('option',option.label);node.value=option.value;input.append(node);}label.append(input);fields.set(prompt.key,{input,label});input.addEventListener('change',updatePrompts);form.append(label);}
      updatePrompts();const button=text('button',connected?'重新登录':backend==='codex'?'前往 Codex 官方登录':'官方登录','button primary');if(backend!=='codex')button.append(document.createTextNode(' · '+method.label));button.type='submit';button.disabled=pending||method.supported===false;form.append(button);form.addEventListener('submit',event=>{event.preventDefault();safe(()=>start(method,Object.fromEntries([...fields].filter(([,field])=>!field.input.disabled).map(([name,field])=>[name,field.input.value]))));});box.append(form);
      if(method.supported===false)box.append(el('p',t(authErrors[method.unsupported_reason]||method.unsupported_reason||'此供应商暂不支持页面登录，请使用密钥连接。'),'mc-secondary'));
    }
    box.append(text('p','账号保存在运行机器上；授权成功后只刷新模型目录，不自动测试模型。','mc-secondary'));
  }
  return {show(provider,refresh){
    if(!['openai','opencode'].includes(provider)){context=null;render();return;}
    const backend=provider==='openai'?'codex':'opencode';if(provider!=='opencode')providerQuery='';context={backend,provider:selections.get(backend)||'',refresh};render();
    safe(()=>status());if(backend==='opencode'&&!providers)safe(getProviders);
  },isConnected:provider=>provider==='openai'?snapshot?.codex?.state==='authenticated':!!snapshot?.opencode?.authenticated_providers?.includes(provider),translate:render};
})();
