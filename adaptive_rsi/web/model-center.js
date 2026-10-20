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
    missing_key:'请填写密钥；环境变量不可用时，可改用钥匙串或会话密钥。', auth:'认证失败，请检查密钥及账号权限。',
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
    provider_paused:'供应商连续失败已暂停，请检查配置后恢复请求。',model_missing:'模型不存在或当前账号不可用，请重新获取目录。',tool_format:'模型未返回符合要求的工具结果，请检查协议与模型能力。',probe_failed:'工具格式测试未通过，请检查账号权限、推理参数和模型工具能力。',
    budget_exhausted:'每日模型调用上限已用完，请等待下次额度周期。',default_effort_required:'此接口要求使用服务默认推理强度。',
    unsupported_model_kind:'此模型类型不能用于当前代码任务。',invalid_model:'请填写有效的模型 ID 和名称。',invalid_routes:'角色配置不完整，请重新选择。'
  };
  const purpose = {analysis:'查找有依据的改进',planning:'判断是否值得执行',implementation:'实现并补充验证',review:'检查正确性与可维护性',evolution:'分析证据与提出候选'};
  const drafts = new Map(), jobs = new Set();
  let data, active='openai', scope=null, tab='roles', sequence=0, editing=null, picking=null, busy=false, loading=false;
  let feedbackKey='',feedbackError=false,modelPage=0,pickerPage=0,priceTarget=null;
  const pageSize=20;
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
      for(const id of ['mc-provider-list','mc-model-list','mc-role-list','mc-extra-role-list'])$(id).replaceChildren();
      $('mc-provider-name').textContent=t('正在读取配置…');$('mc-inheritance').textContent='';
      $('mc-provider-address').textContent='';$('mc-credential-status').textContent='';$('mc-catalog-status').textContent='';
      $('mc-scope-note').textContent=t('正在读取所选范围的配置…');
      for(const id of ['mc-save-routes','mc-add-provider','mc-fetch','mc-edit-provider','mc-remove-provider','mc-add-model','mc-reset-inheritance','mc-undo','mc-resume'])$(id).disabled=true;
    }
    let snapshot;
    try{snapshot=await api('model-center'+(scope?'?project='+encodeURIComponent(scope):''));}
    catch(error){if(ticket===sequence){loading=false;setStatus(message(error),true);}throw error;}
    if(ticket!==sequence||requested!==scope)return;
    data=snapshot;loading=false;
    if((typeof reset==='function'?reset():reset)||!drafts.has(scopeKey()))drafts.set(scopeKey(),structuredClone(data.models));
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
      const button=action('',()=>{active=provider.id;modelPage=0;$('mc-search').value='';setStatus('');render();},'mc-provider');
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
    $('mc-health').hidden=!provider.health.paused&&!provider.health.failures;
    $('mc-health-status').textContent=provider.health.paused?t('连续失败已暂停')+' · '+t(errors[provider.health.code]||''):t('连续故障次数')+' '+provider.health.failures+' / 3';
    $('mc-resume').hidden=!provider.health.paused;$('mc-resume').disabled=false;
    renderModels();
  }
  function renderModels(){
    const provider=selected(),query=$('mc-search').value.toLowerCase(),list=$('mc-model-list');list.replaceChildren();
    const models=provider.catalog.models;
    $('mc-model-count').textContent=models.length;
    $('mc-enabled-count').textContent=models.filter(m=>m.enabled).length+' '+t('个已启用');
    const filter=$('mc-model-filter').value;
    const matches=models.filter(m=>(m.name+' '+m.id).toLowerCase().includes(query)&&(filter==='all'||filter==='enabled'&&m.enabled||filter==='favorite'&&m.favorite));
    const pages=Math.max(1,Math.ceil(matches.length/pageSize));modelPage=Math.min(modelPage,pages-1);
    $('mc-page').textContent=(modelPage+1)+' / '+pages+' · '+matches.length+' '+t('个模型');$('mc-prev').disabled=modelPage===0;$('mc-next').disabled=modelPage===pages-1;
    for(const model of matches.slice(modelPage*pageSize,(modelPage+1)*pageSize)){
      const row=el('div',undefined,'mc-model-row'),copy=el('div',undefined,'mc-model-copy');
      copy.append(el('strong',model.name));if(model.name!==model.id)copy.append(el('code',model.id));
      const status=[];
      if(model.last_attempt)status.push(t('最近测试未执行')+' · '+model.last_attempt.effort+' · '+t(errors[model.last_attempt.code]||''));
      if(model.missing)status.push(t('目录中已失效'));
      else if(model.manual)status.push(t('自定义模型'));
      else if(model.configured&&!provider.catalog.fetched)status.push(t('当前配置 · 尚未获取目录'));
      if(model.kind)status.push(t('非代码任务模型')+' · '+model.kind);
      const latest=Object.values(model.probes||{}).sort((a,b)=>b.at-a.at)[0];
      if(latest?.state==='fail')status.push(t('最近测试失败')+' · '+latest.effort+' · '+t(errors[latest.code]||''));
      if(model.verified)status.push(t('工具格式测试通过')+' · '+stamp(model.verified.at)+' · '+model.verified.effort);

      if(!model.kind&&!model.verified&&!latest)status.push(t('尚未进行模型测试'));
      if(status.length)copy.append(el('small',status.join(' · ')));
      if(model.probes)for(const proof of Object.values(model.probes)){if(proof.state==='pass'&&proof.effort!==model.verified?.effort)copy.append(el('small',t('工具格式测试通过')+' · '+proof.effort+' · '+stamp(proof.at)));}
      if(model.unit_price!=null)copy.append(el('small',t('估算单价')+' · $'+model.unit_price+' / 1M tokens'));
      const controls=el('div',undefined,'mc-model-actions'),label=el('label',undefined,'mc-enable'),checkbox=el('input');
      checkbox.type='checkbox';checkbox.checked=!!model.enabled;checkbox.disabled=!!model.kind;checkbox.setAttribute('aria-label',t('启用模型')+' '+model.name);
      checkbox.addEventListener('change',()=>safe(async()=>{const original=model.enabled;model.enabled=checkbox.checked;checkbox.disabled=true;try{await request('model',{provider:provider.id,model:{id:model.id,enabled:checkbox.checked}});setStatus(t('模型选择范围已保存。'));await load();}catch(error){model.enabled=original;checkbox.checked=original;}finally{checkbox.disabled=false;}}));
      label.append(checkbox,document.createTextNode(t('启用')));
      const test=action(t('测试模型'),()=>openProbe(provider,model),'mc-text-button');test.disabled=!!model.kind;test.setAttribute('aria-label',t('测试模型')+' '+model.name);
      const favorite=action(t(model.favorite?'取消收藏':'收藏'),async()=>{await request('model',{provider:provider.id,model:{id:model.id,favorite:!model.favorite}});await load();},'mc-text-button');
      const price=action(t('估算价格'),()=>{priceTarget={project:scope,provider:provider.id,model:model.id};$('mc-price-target').textContent=provider.name+' / '+model.id;$('mc-price-input').value=model.unit_price??'';$('mc-price-error').textContent='';$('mc-price-dialog').showModal();},'mc-text-button');
      const more=el('details',undefined,'model-more'),menu=el('div',undefined,'model-more-actions');more.append(el('summary',t('更多')));menu.append(favorite,test,price);more.append(menu);controls.append(label,more);row.append(copy,controls);list.append(row);
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
    const container=$('mc-role-list'),extra=$('mc-extra-role-list');container.replaceChildren();extra.replaceChildren();
    for(const [role,title]of Object.entries(roleNames)){
      const route=routes()[role],row=el('div',undefined,'mc-role-row'),label=el('div',undefined,'mc-role-title');
      const simpleTitle={planning:'思考与规划',implementation:'代码实现',review:'代码评审'}[role]||title;
      label.append(el('strong',t(simpleTitle)),el('small',t(purpose[role])));
      if(scope)label.append(el('small',t(JSON.stringify(route)===JSON.stringify(data.global_models[role])?'继承全局配置':'当前项目覆盖')));
      const button=action('',()=>openPicker(role),'mc-role-model');button.setAttribute('aria-label',t(simpleTitle)+' · '+t('选择模型'));
      const provider=data.providers.find(p=>p.id===route.provider),copy=el('span');
      const name=findModel(route)?.name||route.model;copy.append(el('strong',name),el('small',(provider?.name||route.provider)+(name!==route.model?' · '+route.model:'')));
      const proof=findModel(route)?.probes?.[route.effort];copy.append(info(t(proof?.state==='pass'?'当前强度工具测试通过':proof?.state==='fail'?'当前强度测试失败':'当前强度尚未测试'),proof?.state==='fail'?'mc-warning':'mc-secondary'));
      if(route.fallback)copy.append(info(t('备用')+' · '+(data.providers.find(p=>p.id===route.fallback.provider)?.name||route.fallback.provider)+' / '+route.fallback.model+' · '+route.fallback.effort));
      if(findModel(route)?.missing)copy.append(info(t('目录中已失效'),'mc-warning'));
      button.append(copy,info('⌄'));
      const control=el('div',undefined,'mc-effort-wrap');control.append(effortControl(route,t(simpleTitle),value=>{route.effort=value;updateDirty();}));
      const advanced=action(t('高级参数'),()=>openAdvanced(role),'mc-text-button');control.append(advanced);
      row.append(label,button,control);(['analysis','evolution'].includes(role)?extra:container).append(row);
    }
    $('mc-inheritance').textContent=scope?(Object.keys(data.overrides).length?t('当前项目有独立角色配置。'):t('当前项目继承全局模型配置。')):t('未覆盖的项目跟随全局配置。');
    $('mc-reset-inheritance').hidden=!scope;
    updateDirty();
  }
  function updateDirty(){$('mc-dirty').textContent=modified()?t('有未保存的角色分配'):t('角色分配已保存');$('mc-save-routes').disabled=!modified();$('mc-undo').disabled=!modified();}
  function openPicker(role){
    picking=role;pickerPage=0;renderPickerProviders();$('mc-picker-title').textContent=t(roleNames[role])+' · '+t('选择模型');$('mc-picker-search').value='';renderPicker();$('mc-picker-dialog').showModal();$('mc-picker-search').focus();
  }
  function renderPickerProviders(){
    const select=$('mc-picker-provider'),before=select.value;select.replaceChildren();const all=el('option',t('所有供应商'));all.value='';select.append(all);
    for(const provider of data.providers){const option=el('option',provider.name);option.value=provider.id;select.append(option);}select.value=before;
  }
  function renderPicker(){
    const list=$('mc-picker-list'),query=$('mc-picker-search').value.toLowerCase();list.replaceChildren();
    const providerFilter=$('mc-picker-provider').value;
    const matches=data.providers.flatMap(p=>p.catalog.models.filter(m=>m.enabled&&!m.missing&&!m.kind&&(!providerFilter||p.id===providerFilter)&&(m.id+' '+m.name+' '+p.name).toLowerCase().includes(query)).map(m=>({provider:p,model:m})));
    const pages=Math.max(1,Math.ceil(matches.length/pageSize));pickerPage=Math.min(pickerPage,pages-1);$('mc-picker-page').textContent=(pickerPage+1)+' / '+pages;$('mc-picker-prev').disabled=pickerPage===0;$('mc-picker-next').disabled=pickerPage===pages-1;
    const visible=matches.slice(pickerPage*pageSize,(pickerPage+1)*pageSize);
    for(const provider of data.providers){
      const models=visible.filter(item=>item.provider.id===provider.id).map(item=>item.model);
      if(!models.length)continue;
      list.append(el('h3',provider.name));
      for(const model of models){const button=action('',()=>{const old=routes()[picking];routes()[picking]={...old,provider:provider.id,model:model.id,effort:old.provider===provider.id&&old.model===model.id?old.effort:'default'};$('mc-picker-dialog').close();renderRoles();},'mc-picker-option');
        const copy=el('span');copy.append(el('strong',model.name));if(model.name!==model.id)copy.append(el('small',model.id));button.append(copy,info(model.verified||Object.values(model.probes||{}).some(p=>p.state==='pass')?t('已测试'):t('待验证')));list.append(button);}
    }
    if(!list.children.length)list.append(el('p',t('没有已启用的匹配模型，请先到供应商页面获取或添加。'),'mc-empty'));
  }
  function openAdvanced(role){
    const route=routes()[role];$('mc-advanced-title').textContent=t(roleNames[role])+' · '+t('高级参数');
    $('mc-advanced-effort').replaceChildren(effortControl(route,t(roleNames[role]),()=>{},true));
    renderFallback(route.fallback);
    $('mc-advanced-dialog').dataset.role=role;$('mc-advanced-error').textContent='';$('mc-advanced-dialog').showModal();
  }
  function renderFallback(fallback=null){
    const select=$('mc-fallback-provider');select.replaceChildren();
    const none=el('option',t('不启用备用'));none.value='';select.append(none);
    for(const provider of data.providers){const option=el('option',provider.name);option.value=provider.id;select.append(option);}
    select.value=fallback?.provider||'';renderFallbackModels(fallback);
  }
  function renderFallbackModels(fallback=null){
    const provider=data.providers.find(p=>p.id===$('mc-fallback-provider').value),select=$('mc-fallback-model');select.replaceChildren();
    const models=(provider?.catalog.models||[]).filter(m=>m.enabled&&!m.missing&&!m.kind);
    if(fallback&&fallback.provider===provider?.id&&!models.some(m=>m.id===fallback.model))models.push({id:fallback.model,name:fallback.model});
    for(const model of models){const option=el('option',model.name);option.value=model.id;select.append(option);}
    if(fallback)select.value=fallback.model;select.disabled=!provider;
    renderFallbackEffort(fallback?.effort||'default');
  }
  function renderFallbackEffort(effort='default'){
    const route={provider:$('mc-fallback-provider').value,model:$('mc-fallback-model').value,effort};
    $('mc-fallback-effort').replaceChildren(effortControl(route,t('备用模型'),()=>{},true));
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
    $('mc-search').addEventListener('input',()=>{modelPage=0;renderModels();});
    $('mc-model-filter').addEventListener('change',()=>{modelPage=0;renderModels();});
    $('mc-prev').addEventListener('click',()=>{modelPage--;renderModels();});$('mc-next').addEventListener('click',()=>{modelPage++;renderModels();});
    $('mc-undo').addEventListener('click',()=>{drafts.set(scopeKey(),structuredClone(data.models));renderRoles();});
    $('mc-resume').addEventListener('click',()=>safe(async()=>{await request('resume',{provider:active});await load();setStatus(t('已恢复请求，下次调用将重新检查。'));}));
    $('mc-fallback-provider').addEventListener('change',()=>renderFallbackModels());$('mc-fallback-model').addEventListener('change',()=>renderFallbackEffort());
    $('mc-price-form').addEventListener('submit',event=>{event.preventDefault();safe(async()=>{const unchanged=unchangedForm(event.target);try{await api('model-center/model',{project:priceTarget.project,provider:priceTarget.provider,model:{id:priceTarget.model,unit_price:$('mc-price-input').value===''?null:Number($('mc-price-input').value)}});if(unchanged())$('mc-price-dialog').close();await load();}catch(error){$('mc-price-error').textContent=message(error);}});});$('mc-picker-search').addEventListener('input',()=>{pickerPage=0;renderPicker();});
    $('mc-picker-provider').addEventListener('change',()=>{pickerPage=0;renderPicker();});
    $('mc-picker-prev').addEventListener('click',()=>{pickerPage--;renderPicker();});$('mc-picker-next').addEventListener('click',()=>{pickerPage++;renderPicker();});
    $('mc-fetch').addEventListener('click',()=>safe(()=>fetchProvider()));
    $('mc-provider-dialog').addEventListener('close',()=>{$('mc-input-key').value='';});
    $('mc-provider-form').addEventListener('submit',event=>{event.preventDefault();safe(async()=>{
      const button=$('mc-provider-submit'),project=scope,unchanged=unchangedForm(event.target);button.disabled=true;$('mc-provider-error').textContent='';
      try{const provider={name:$('mc-input-name').value.trim(),base_url:$('mc-input-url').value.trim(),protocol:$('mc-input-protocol').value,env_key:$('mc-input-env').value.trim()};if(editing)provider.id=editing.id;
        const authentication={mode:$('mc-input-auth').value,value:$('mc-input-key').value};$('mc-input-key').value='';
        const result=await api('model-center/provider',{project,provider,authentication});
        if(scope===project){await refresh();await load();if(unchanged()&&scope===project){active=result.id;tab='providers';render();$('mc-provider-dialog').close();}setStatus(t('连接配置已保存。'));}
        await fetchProvider(result.id,project);
      }catch(error){$('mc-provider-error').textContent=message(error);}finally{button.disabled=false;}
    });});
    $('mc-remove-provider').addEventListener('click',()=>safe(async()=>{await request('remove',{provider:active});await load();await refresh();setStatus(t('连接已移除。'));}));
    $('mc-add-model').addEventListener('click',()=>{$('mc-custom-form').reset();$('mc-custom-error').textContent='';$('mc-custom-dialog').showModal();});
    $('mc-custom-form').addEventListener('submit',event=>{event.preventDefault();safe(async()=>{const unchanged=unchangedForm(event.target);try{const id=$('mc-custom-id').value.trim();await request('model',{provider:active,model:{id,name:$('mc-custom-name').value.trim()||id,manual:true}});if(unchanged())$('mc-custom-dialog').close();await load();}catch(error){$('mc-custom-error').textContent=message(error);}});});
    $('mc-save-routes').addEventListener('click',()=>safe(async()=>{const project=scope,models=structuredClone(routes()),unchanged=()=>JSON.stringify(routes())===JSON.stringify(models);$('mc-save-routes').disabled=true;try{await api('model-center/routes',{project,models});if(scope===project){await load({reset:unchanged});await refresh();setStatus(t(unchanged()?'角色分配已保存，从下一轮任务生效。':'已保存提交的配置，后续修改仍未保存。'));}}catch(error){setStatus(message(error),true);updateDirty();}}));
    $('mc-reset-inheritance').addEventListener('click',()=>{drafts.set(scopeKey(),structuredClone(data.global_models));renderRoles();setStatus(t('已选择继承全局配置，保存后生效。'));});
    $('mc-advanced-form').addEventListener('submit',event=>{event.preventDefault();try{const provider=$('mc-fallback-provider').value,model=$('mc-fallback-model').value;const fallback=provider?{provider,model,effort:$('mc-fallback-effort').querySelector('select').value}:null,route=routes()[$('mc-advanced-dialog').dataset.role];if(provider&&!model)throw Error(t('请先获取并启用备用模型。'));if(fallback&&provider===route.provider&&model===route.model)throw Error(t('备用模型应与主模型不同。'));route.fallback=fallback;route.effort=$('mc-advanced-effort').querySelector('select').value;$('mc-advanced-dialog').close();renderRoles();}catch(error){$('mc-advanced-error').textContent=error.message;}});
    $('mc-probe-start').addEventListener('click',()=>safe(async()=>{
      const button=$('mc-probe-start'),project=scope,dialog=$('mc-probe-dialog');button.disabled=true;busy=true;$('mc-probe-result').textContent=t('正在执行工具格式测试…');
      try{const result=await api('model-center/probe',{project,provider:dialog.dataset.provider,model:dialog.dataset.model,effort:$('mc-probe-effort').querySelector('select').value});
        $('mc-probe-result').textContent=t('工具格式测试通过')+' · '+result.calls.length+' '+t('次调用')+' · '+result.calls.reduce((n,c)=>n+c.tokens,0)+' tokens';if(project===scope)await load();
      }catch(error){$('mc-probe-result').textContent=message(error);if(project===scope)await load();}finally{busy=false;button.disabled=false;}
    }));
    document.querySelectorAll('[data-open-model-center]').forEach(button=>button.addEventListener('click',()=>showView('models')));
    window.addEventListener('beforeunload',event=>{if(modified()){event.preventDefault();event.returnValue='';}});
  }
  init();
  function translate(){
    if(!data)return;render();setStatus(feedbackKey,feedbackError);
    if($('mc-picker-dialog').open){renderPickerProviders();$('mc-picker-title').textContent=t(roleNames[picking])+' · '+t('选择模型');renderPicker();}
    if($('mc-provider-dialog').open){$('mc-provider-dialog-title').textContent=t(editing?'连接设置':'添加供应商');$('mc-input-key').placeholder=t(editing&&['stored','configured'].includes(editing.credential_status)?'留空保留当前密钥':'填写 API Key');}
    if($('mc-advanced-dialog').open){const provider=$('mc-fallback-provider').value,model=$('mc-fallback-model').value,effort=$('mc-fallback-effort').querySelector('select').value;renderFallback(provider?{provider,model,effort}:null);const role=$('mc-advanced-dialog').dataset.role;$('mc-advanced-title').textContent=t(roleNames[role])+' · '+t('高级参数');}
    if($('mc-probe-dialog').open){const dialog=$('mc-probe-dialog'),model=data.providers.find(p=>p.id===dialog.dataset.provider)?.catalog.models.find(m=>m.id===dialog.dataset.model);$('mc-probe-title').textContent=t('测试模型')+' · '+(model?.name||dialog.dataset.model);}
  }
  return {show:(requested=null)=>{
    const next=requested||null,changed=scope!==next;scope=next;
    if(!data||changed||data.project!==scope)safe(()=>load());else render();
  },translate,load};
})();
