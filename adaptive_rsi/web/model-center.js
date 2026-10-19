'use strict';
window.RSIModelCenter = (() => {
  const presets = {
    custom: {name:'我的中转站',protocol:'chat_completions',base_url:'',env_key:'MODEL_API_KEY'},
    openai: {name:'OpenAI API',protocol:'responses',base_url:'https://api.openai.com/v1',env_key:'OPENAI_API_KEY'},
    anthropic: {name:'Anthropic',protocol:'anthropic',base_url:'https://api.anthropic.com/v1',env_key:'ANTHROPIC_API_KEY'},
    gemini: {name:'Gemini',protocol:'gemini',base_url:'https://generativelanguage.googleapis.com/v1beta',env_key:'GEMINI_API_KEY'},
    deepseek: {name:'DeepSeek',protocol:'chat_completions',base_url:'https://api.deepseek.com',env_key:'DEEPSEEK_API_KEY'},
    ollama: {name:'Ollama API',protocol:'ollama',base_url:'http://127.0.0.1:11434',env_key:''},
    lmstudio: {name:'LM Studio API',protocol:'chat_completions',base_url:'http://127.0.0.1:1234/v1',env_key:''}
  };
  const errors = {
    missing_key:'请填写密钥，或检查服务进程的环境变量。', auth:'认证失败，请检查密钥及账号权限。',
    unsupported:'服务未提供标准模型目录，可添加自定义模型。',rate_limit:'请求被限流，请稍后重试。',
    connection:'无法连接服务，请检查地址及本地服务状态。',timeout:'请求超时，请重试。',
    http_error:'供应商暂时不可用，请稍后重试。',invalid_response:'模型目录格式不兼容，可添加自定义模型。',
    too_large:'模型目录过大，可添加自定义模型。',invalid_provider:'请检查供应商名称、地址及接口协议。',
    keychain_unavailable:'此系统不支持 macOS 钥匙串，请选择会话密钥或环境变量。',
    keychain_locked:'钥匙串已锁定或访问被取消，请解锁后重试。',keychain_failed:'无法访问系统钥匙串，请重试或改用会话密钥。',
    invalid_key:'密钥格式不正确，请重新填写。',provider_in_use:'此连接仍被角色或备用模型使用，请先更换相关配置。',
    builtin_provider:'内置连接不能移除。',unknown_provider:'此连接已不存在，请刷新列表。',
    connection_changed:'连接配置已经改变，请重新获取或测试。',update_in_progress:'服务正在更新，请稍后重试。',
    codex_missing:'未找到 Codex CLI，请安装并登录。',codex_error:'无法读取 Codex 模型目录，请检查 CLI 版本和登录状态。',
    probe_failed:'工具格式测试未通过，请检查账号权限、推理参数和模型工具能力。',
    budget_exhausted:'每日模型调用上限已用完，请等待下次额度周期。',default_effort_required:'此接口要求使用服务默认推理强度。',
    unsupported_model_kind:'此模型类型不能用于当前代码任务。',invalid_model:'请填写有效的模型 ID 和名称。',invalid_routes:'角色配置不完整，请重新选择。'
  };
  const purpose = {analysis:'查找有依据的改进',planning:'判断是否值得执行',implementation:'实现并补充验证',review:'检查正确性与可维护性',evolution:'分析证据与提出候选'};
  const drafts = new Map(), jobs = new Set();
  let data, active='openai', scope=null, tab='providers', sequence=0, editing=null, picking=null, busy=false, loading=false;
  let previousProject='',feedbackKey='',feedbackError=false;
  const scopeKey = () => scope || 'global';
  const selected = () => data?.providers.find(p=>p.id===active);
  const routes = () => drafts.get(scopeKey());
  const modified = () => data?.project===scope && routes() && JSON.stringify(routes())!==JSON.stringify(data.models);
  const jobKey = (provider,project=scope) => (project||'global')+':'+provider;
  const message = error => t(errors[error.code] || error.message);
  const setStatus = (text,error=false) => { feedbackKey=Object.hasOwn(english,text)?text:Object.entries(english).find(([,value])=>value===text)?.[0]||text;feedbackError=error;$('mc-feedback').textContent=t(feedbackKey);$('mc-feedback').classList.toggle('mc-error',error); };
  function info(text,cls='mc-secondary'){return el('span',text,cls);}
  function findModel(route){return data?.providers.find(p=>p.id===route.provider)?.catalog.models.find(m=>m.id===route.model);}
  async function request(path,body){try{return await api('model-center/'+path,{...body,project:scope});}catch(error){error.message=message(error);setStatus(error.message,true);throw error;}}
  async function load({reset=false}={}){
    if(!state)return;
    const ticket=++sequence,requested=scope;
    loading=true;
    if(!data||data.project!==scope){
      $('view-models').removeAttribute('data-scope');
      for(const id of ['mc-provider-list','mc-model-list','mc-role-list'])$(id).replaceChildren();
      $('mc-provider-name').textContent=t('正在读取配置…');$('mc-inheritance').textContent='';
      $('mc-provider-address').textContent='';$('mc-credential-status').textContent='';$('mc-catalog-status').textContent='';
      $('mc-scope-note').textContent=t('正在读取所选范围的配置…');
      for(const id of ['mc-save-routes','mc-add-provider','mc-fetch','mc-edit-provider','mc-remove-provider','mc-add-model','mc-reset-inheritance'])$(id).disabled=true;
    }
    let snapshot;
    try{snapshot=await api('model-center'+(scope?'?project='+encodeURIComponent(scope):''));}
    catch(error){if(ticket===sequence){loading=false;setStatus(message(error),true);}throw error;}
    if(ticket!==sequence||requested!==scope)return;
    data=snapshot;loading=false;
    if(reset||!drafts.has(scopeKey()))drafts.set(scopeKey(),structuredClone(data.models));
    if(!selected())active=data.providers[0].id;
    render();
  }
  function render(){
    if(!data||loading||data.project!==scope)return;
    $('view-models').dataset.scope=scopeKey();
    for(const id of ['mc-add-provider','mc-edit-provider','mc-remove-provider','mc-add-model','mc-reset-inheritance'])$(id).disabled=false;
    $('mc-scope').value=scope?'project':'global';
    $('mc-scope').querySelector('[value="project"]').disabled=!projectId;
    $('mc-scope-note').textContent=scope?t('当前项目的供应商与角色覆盖独立保存。'):t('供应商与角色默认供各项目继承使用。');
    $('mc-scope-note').hidden=tab!=='providers';
    document.querySelectorAll('[data-mc-tab]').forEach(button=>{const on=button.dataset.mcTab===tab;button.setAttribute('aria-selected',String(on));});
    $('mc-providers-panel').hidden=tab!=='providers';$('mc-roles-panel').hidden=tab!=='roles';
    renderProviders();renderProvider();renderRoles();
  }
  function renderProviders(){
    const list=$('mc-provider-list');list.replaceChildren();
    for(const provider of data.providers){
      const button=action('',()=>{active=provider.id;$('mc-search').value='';setStatus('');render();},'mc-provider');
      button.setAttribute('aria-pressed',String(provider.id===active));button.setAttribute('aria-label',provider.name);
      const symbol=el('span',provider.name.slice(0,1).toUpperCase(),'mc-provider-symbol'),copy=el('span',undefined,'mc-provider-copy');
      copy.append(el('strong',provider.id==='openai'?t('Codex 登录'):provider.name));
      copy.append(el('small',provider.id==='openai'?t('本机账户'):provider.credential_status==='missing'?t('待配置凭据'):provider.builtin?t('本地服务'):protocolName(provider.protocol)));
      button.append(symbol,copy);list.append(button);
    }
  }
  function protocolName(protocol){return {codex:'Codex / Responses',responses:'Responses API',chat_completions:'Chat Completions',anthropic:'Anthropic Messages',gemini:'Gemini',ollama:'Ollama API',codex_login:t('本机账户')}[protocol]||protocol;}
  function renderProvider(){
    const provider=selected(),catalog=provider.catalog;
    $('mc-provider-name').textContent=provider.id==='openai'?t('Codex 登录'):provider.name;
    $('mc-provider-address').textContent=provider.base_url||t('沿用本机 Codex 账户');
    const credential={login:'使用 Codex 登录',configured:'凭据已配置',stored:'密钥已存入钥匙串',missing:'待配置凭据',not_required:'无需密钥'};
    $('mc-credential-status').textContent=t(credential[provider.credential_status]);
    $('mc-catalog-status').textContent=catalog.fetched?t('目录已获取'):t('尚未获取目录');
    $('mc-catalog-status').className='badge '+(catalog.fetched?'good':'neutral');
    $('mc-refreshed').textContent=catalog.fetched?t('更新于 ')+stamp(catalog.fetched):'';
    $('mc-edit-provider').hidden=provider.builtin;$('mc-remove-provider').hidden=provider.builtin;
    $('mc-fetch').disabled=jobs.has(jobKey(provider.id));$('mc-fetch').textContent=t(jobs.has(jobKey(provider.id))?'获取中…':'获取模型');
    $('mc-directory-error').hidden=!catalog.error;
    $('mc-directory-error').textContent=catalog.error?t(errors[catalog.error]||'供应商暂时不可用，请稍后重试。'):'';
    $('mc-truncated').hidden=!catalog.truncated;
    renderModels();
  }
  function renderModels(){
    const provider=selected(),query=$('mc-search').value.toLowerCase(),list=$('mc-model-list');list.replaceChildren();
    const models=provider.catalog.models;
    $('mc-model-count').textContent=models.length;
    $('mc-enabled-count').textContent=models.filter(m=>m.enabled).length+' '+t('个已启用');
    const matches=models.filter(m=>(m.name+' '+m.id).toLowerCase().includes(query));
    for(const model of matches){
      const row=el('div',undefined,'mc-model-row'),copy=el('div',undefined,'mc-model-copy');
      copy.append(el('strong',model.name));if(model.name!==model.id)copy.append(el('code',model.id));
      const status=[];
      if(model.missing)status.push(t('目录中已失效'));
      else if(model.manual)status.push(t('自定义模型'));
      else if(model.configured&&!provider.catalog.fetched)status.push(t('当前配置 · 尚未获取目录'));
      if(model.kind)status.push(t('非代码任务模型')+' · '+model.kind);
      if(model.verified)status.push(t('工具格式测试通过')+' · '+stamp(model.verified.at)+' · '+model.verified.effort);
      else status.push(t('工具调用待验证'));
      copy.append(el('small',status.join(' · ')));
      const controls=el('div',undefined,'mc-model-actions'),label=el('label',undefined,'mc-enable'),checkbox=el('input');
      checkbox.type='checkbox';checkbox.checked=!!model.enabled;checkbox.disabled=!!model.kind;checkbox.setAttribute('aria-label',t('启用模型')+' '+model.name);
      checkbox.addEventListener('change',()=>safe(async()=>{const original=model.enabled;model.enabled=checkbox.checked;checkbox.disabled=true;try{await request('model',{provider:provider.id,model:{id:model.id,enabled:checkbox.checked}});setStatus(t('模型选择范围已保存。'));await load();}catch(error){model.enabled=original;checkbox.checked=original;}finally{checkbox.disabled=false;}}));
      label.append(checkbox,document.createTextNode(t('启用')));
      const test=action(t('测试模型'),()=>openProbe(provider,model),'mc-text-button');test.disabled=!!model.kind;test.setAttribute('aria-label',t('测试模型')+' '+model.name);
      controls.append(label,test);row.append(copy,controls);list.append(row);
    }
    if(!matches.length)list.append(el('p',t(models.length?'没有匹配的模型。':'连接后点击「获取模型」，也可以添加自定义模型。'),'mc-empty'));
  }
  function effortOptions(route,advanced=false){
    const provider=data.providers.find(p=>p.id===route.provider),model=findModel(route);
    let options=['default'];
    if(model?.efforts?.length)options.push(...model.efforts);
    if(advanced&&['responses','chat_completions','codex','codex_login'].includes(provider?.protocol))options=['default','low','medium','high','xhigh','max','ultra'];
    if(!options.includes(route.effort))options.push(route.effort);
    if(route.model.includes('luna'))options=options.filter(value=>value!=='ultra');
    return [...new Set(options)];
  }
  function effortControl(route,title,onChange,advanced=false){
    const select=el('select');select.className='mc-effort';select.setAttribute('aria-label',title+' · '+t('推理强度'));
    for(const value of effortOptions(route,advanced)){const option=el('option',value==='default'?t('服务默认'):value);option.value=value;select.append(option);}
    select.value=route.effort;select.addEventListener('change',()=>onChange(select.value));return select;
  }
  function renderRoles(){
    const container=$('mc-role-list');container.replaceChildren();
    for(const [role,title]of Object.entries(roleNames)){
      const route=routes()[role],row=el('div',undefined,'mc-role-row'),label=el('div',undefined,'mc-role-title');
      label.append(el('strong',t(title)),el('small',t(purpose[role])));
      if(scope)label.append(el('small',t(data.overrides[role]?'当前项目覆盖':'继承全局配置')));
      const button=action('',()=>openPicker(role),'mc-role-model');button.setAttribute('aria-label',t(title)+' · '+t('选择模型'));
      const provider=data.providers.find(p=>p.id===route.provider),copy=el('span');
      const name=findModel(route)?.name||route.model;copy.append(el('strong',name),el('small',(provider?.name||route.provider)+(name!==route.model?' · '+route.model:'')));
      if(findModel(route)?.missing)copy.append(info(t('目录中已失效'),'mc-warning'));
      button.append(copy,info('⌄'));
      const control=el('div',undefined,'mc-effort-wrap');control.append(effortControl(route,t(title),value=>{route.effort=value;updateDirty();}));
      const advanced=action(t('高级参数'),()=>openAdvanced(role),'mc-text-button');control.append(advanced);
      row.append(label,button,control);container.append(row);
    }
    $('mc-inheritance').textContent=scope?(Object.keys(data.overrides).length?t('当前项目有独立角色配置。'):t('当前项目继承全局模型配置。')):t('未覆盖的项目跟随全局配置。');
    $('mc-reset-inheritance').hidden=!scope;
    updateDirty();
  }
  function updateDirty(){$('mc-dirty').textContent=modified()?t('有未保存的角色分配'):t('角色分配已保存');$('mc-save-routes').disabled=!modified();}
  function openPicker(role){
    picking=role;$('mc-picker-title').textContent=t(roleNames[role])+' · '+t('选择模型');$('mc-picker-search').value='';renderPicker();$('mc-picker-dialog').showModal();$('mc-picker-search').focus();
  }
  function renderPicker(){
    const list=$('mc-picker-list'),query=$('mc-picker-search').value.toLowerCase();list.replaceChildren();
    for(const provider of data.providers){
      const models=provider.catalog.models.filter(m=>m.enabled&&!m.missing&&!m.kind&&(m.id+' '+m.name+' '+provider.name).toLowerCase().includes(query));
      if(!models.length)continue;
      list.append(el('h3',provider.name));
      for(const model of models){const button=action('',()=>{const old=routes()[picking];routes()[picking]={...old,provider:provider.id,model:model.id,effort:old.provider===provider.id&&old.model===model.id?old.effort:'default'};$('mc-picker-dialog').close();renderRoles();},'mc-picker-option');
        const copy=el('span');copy.append(el('strong',model.name));if(model.name!==model.id)copy.append(el('small',model.id));button.append(copy,info(model.verified?t('已测试'):t('待验证')));list.append(button);}
    }
    if(!list.children.length)list.append(el('p',t('没有已启用的匹配模型，请先到供应商页面获取或添加。'),'mc-empty'));
  }
  function openAdvanced(role){
    const route=routes()[role];$('mc-advanced-title').textContent=t(roleNames[role])+' · '+t('高级参数');
    $('mc-advanced-effort').replaceChildren(effortControl(route,t(roleNames[role]),()=>{},true));
    $('mc-fallback-json').value=JSON.stringify(route.fallback||null,null,2);
    $('mc-advanced-dialog').dataset.role=role;$('mc-advanced-error').textContent='';$('mc-advanced-dialog').showModal();
  }
  function applyPreset(){
    const preset=presets[$('mc-provider-preset').value];
    $('mc-input-name').value=t(preset.name);$('mc-input-url').value=preset.base_url;$('mc-input-protocol').value=preset.protocol;$('mc-input-env').value=preset.env_key;
    $('mc-input-auth').value=preset.env_key?(data.keychain?'keychain':'session'):'none';syncAuth();
  }
  function syncAuth(){const mode=$('mc-input-auth').value;$('mc-key-field').hidden=!['keychain','session'].includes(mode);$('mc-env-field').hidden=mode!=='env';$('mc-session-hint').hidden=mode!=='session';}
  function openEditor(provider=null){
    editing=provider;$('mc-provider-form').reset();$('mc-provider-error').textContent='';$('mc-provider-dialog-title').textContent=t(provider?'连接设置':'添加供应商');
    $('mc-provider-preset-field').hidden=!!provider;
    $('mc-input-auth').querySelector('[value="keychain"]').disabled=!data.keychain;
    if(provider){$('mc-input-name').value=provider.name;$('mc-input-url').value=provider.base_url;$('mc-input-protocol').value=provider.protocol;$('mc-input-env').value=provider.env_key;$('mc-input-auth').value=provider.credential||'env';syncAuth();}
    else applyPreset();
    $('mc-input-key').placeholder=t(provider&&['stored','configured'].includes(provider.credential_status)?'留空保留当前密钥':'填写 API Key');
    $('mc-provider-dialog').showModal();
  }
  async function fetchProvider(providerId=active,requested=scope){
    const key=jobKey(providerId,requested);if(jobs.has(key))return;
    jobs.add(key);if(scope===requested)renderProvider();
    try{await api('model-center/fetch',{provider:providerId,project:requested});if(scope===requested){await load();setStatus(t('模型目录已更新，已有选择已保留。'));}}
    catch(error){if(scope===requested){await load();setStatus(message(error),true);}}
    finally{jobs.delete(key);if(scope===requested&&data)renderProvider();}
  }
  function openProbe(provider,model){
    if(busy){setStatus(t('正在执行工具格式测试…'));return;}
    $('mc-probe-title').textContent=t('测试模型')+' · '+model.name;
    $('mc-probe-target').textContent=provider.name+' / '+model.id;
    $('mc-probe-dialog').dataset.provider=provider.id;$('mc-probe-dialog').dataset.model=model.id;
    const route=Object.values(routes()).find(r=>r.provider===provider.id&&r.model===model.id)||{provider:provider.id,model:model.id,effort:'default'};
    $('mc-probe-effort').replaceChildren(effortControl({...route},t('测试模型'),()=>{},true));$('mc-probe-result').textContent='';$('mc-probe-start').disabled=false;$('mc-probe-dialog').showModal();
  }
  function init(){
    document.querySelectorAll('[data-mc-tab]').forEach(button=>button.addEventListener('click',()=>{tab=button.dataset.mcTab;render();}));
    $('mc-scope').addEventListener('change',()=>safe(async()=>{scope=$('mc-scope').value==='project'?projectId:null;setStatus('');await load();}));
    $('mc-add-provider').addEventListener('click',()=>{if(data)openEditor();});$('mc-edit-provider').addEventListener('click',()=>openEditor(selected()));
    $('mc-provider-preset').addEventListener('change',applyPreset);$('mc-input-auth').addEventListener('change',syncAuth);
    $('mc-search').addEventListener('input',renderModels);$('mc-picker-search').addEventListener('input',renderPicker);
    $('mc-fetch').addEventListener('click',()=>safe(()=>fetchProvider()));
    $('mc-provider-dialog').addEventListener('close',()=>{$('mc-input-key').value='';});
    $('mc-provider-form').addEventListener('submit',event=>{event.preventDefault();safe(async()=>{
      const button=$('mc-provider-submit'),project=scope;button.disabled=true;$('mc-provider-error').textContent='';
      try{const provider={name:$('mc-input-name').value.trim(),base_url:$('mc-input-url').value.trim(),protocol:$('mc-input-protocol').value,env_key:$('mc-input-env').value.trim()};if(editing)provider.id=editing.id;
        const authentication={mode:$('mc-input-auth').value,value:$('mc-input-key').value};$('mc-input-key').value='';
        const result=await api('model-center/provider',{project,provider,authentication});
        if(scope===project){active=result.id;settingsBaseline=null;await refresh();await load();$('mc-provider-dialog').close();setStatus(t('连接配置已保存。'));}
        await fetchProvider(result.id,project);
      }catch(error){$('mc-provider-error').textContent=message(error);}finally{button.disabled=false;}
    });});
    $('mc-remove-provider').addEventListener('click',()=>safe(async()=>{await request('remove',{provider:active});await load();settingsBaseline=null;await refresh();setStatus(t('连接已移除。'));}));
    $('mc-add-model').addEventListener('click',()=>{$('mc-custom-form').reset();$('mc-custom-error').textContent='';$('mc-custom-dialog').showModal();});
    $('mc-custom-form').addEventListener('submit',event=>{event.preventDefault();safe(async()=>{try{const id=$('mc-custom-id').value.trim();await request('model',{provider:active,model:{id,name:$('mc-custom-name').value.trim()||id,manual:true}});$('mc-custom-dialog').close();await load();}catch(error){$('mc-custom-error').textContent=message(error);}});});
    $('mc-save-routes').addEventListener('click',()=>safe(async()=>{const project=scope,models=structuredClone(routes());$('mc-save-routes').disabled=true;try{await api('model-center/routes',{project,models});if(scope===project){await load({reset:true});settingsBaseline=null;await refresh();setStatus(t('角色分配已保存，从下一轮任务生效。'));}}catch(error){setStatus(message(error),true);updateDirty();}}));
    $('mc-reset-inheritance').addEventListener('click',()=>{drafts.set(scopeKey(),structuredClone(data.global_models));renderRoles();setStatus(t('已选择继承全局配置，保存后生效。'));});
    $('mc-advanced-form').addEventListener('submit',event=>{event.preventDefault();try{const fallback=JSON.parse($('mc-fallback-json').value),route=routes()[$('mc-advanced-dialog').dataset.role];route.fallback=fallback;route.effort=$('mc-advanced-effort').querySelector('select').value;$('mc-advanced-dialog').close();renderRoles();}catch(error){$('mc-advanced-error').textContent=t('请填写有效的 JSON。');}});
    $('mc-probe-start').addEventListener('click',()=>safe(async()=>{
      const button=$('mc-probe-start'),project=scope,dialog=$('mc-probe-dialog');button.disabled=true;busy=true;$('mc-probe-result').textContent=t('正在执行工具格式测试…');
      try{const result=await api('model-center/probe',{project,provider:dialog.dataset.provider,model:dialog.dataset.model,effort:$('mc-probe-effort').querySelector('select').value});
        $('mc-probe-result').textContent=t('工具格式测试通过')+' · '+result.calls.length+' '+t('次调用')+' · '+result.calls.reduce((n,c)=>n+c.tokens,0)+' tokens';if(project===scope)await load();
      }catch(error){$('mc-probe-result').textContent=message(error);}finally{busy=false;button.disabled=false;}
    }));
    document.querySelectorAll('[data-open-model-center]').forEach(button=>button.addEventListener('click',()=>showView('models')));
    window.addEventListener('beforeunload',event=>{if(modified()){event.preventDefault();event.returnValue='';}});
  }
  init();
  function translate(){
    if(!data)return;render();setStatus(feedbackKey,feedbackError);
    if($('mc-picker-dialog').open){$('mc-picker-title').textContent=t(roleNames[picking])+' · '+t('选择模型');renderPicker();}
    if($('mc-provider-dialog').open){$('mc-provider-dialog-title').textContent=t(editing?'连接设置':'添加供应商');$('mc-input-key').placeholder=t(editing&&['stored','configured'].includes(editing.credential_status)?'留空保留当前密钥':'填写 API Key');}
    if($('mc-advanced-dialog').open){const role=$('mc-advanced-dialog').dataset.role;$('mc-advanced-title').textContent=t(roleNames[role])+' · '+t('高级参数');}
    if($('mc-probe-dialog').open){const dialog=$('mc-probe-dialog'),model=data.providers.find(p=>p.id===dialog.dataset.provider)?.catalog.models.find(m=>m.id===dialog.dataset.model);$('mc-probe-title').textContent=t('测试模型')+' · '+(model?.name||dialog.dataset.model);}
  }
  return {show:()=>{if(!data||data.project!==scope||previousProject!==projectId){previousProject=projectId;if(scope)scope=projectId||null;safe(()=>load());}else render();},translate,projectChanged:()=>{if(scope){scope=projectId||null;safe(()=>load());}},load};
})();
