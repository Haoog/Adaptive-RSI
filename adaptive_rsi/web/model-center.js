'use strict';
window.RSIModelCenter = (() => {
  const presets = {
    custom: {name:'我的中转站',protocol:'chat_completions',base_url:'',env_key:'MODEL_API_KEY'},
    opencode_go: {name:'OpenCode Go',protocol:'chat_completions',base_url:'',env_key:'OPENCODE_API_KEY'},
    openai: {name:'OpenAI API',protocol:'responses',base_url:'https://api.openai.com/v1',env_key:'OPENAI_API_KEY'},
    anthropic: {name:'Anthropic',protocol:'anthropic',base_url:'https://api.anthropic.com/v1',env_key:'ANTHROPIC_API_KEY'},
    gemini: {name:'Gemini',protocol:'gemini',base_url:'https://generativelanguage.googleapis.com/v1beta',env_key:'GEMINI_API_KEY'},
    deepseek: {name:'DeepSeek',protocol:'chat_completions',base_url:'https://api.deepseek.com',env_key:'DEEPSEEK_API_KEY'},
    ollama: {name:'Ollama API',protocol:'ollama',base_url:'http://127.0.0.1:11434',env_key:''},
    lmstudio: {name:'LM Studio API',protocol:'chat_completions',base_url:'http://127.0.0.1:1234/v1',env_key:''}
  };
  const errors = {
    catalog_busy:'此连接正在刷新模型目录，请稍候。',probe_busy:'同时测试的模型已达上限，请稍候重试。',
    invalid_credential_registry:'凭据清理登记损坏，请保留状态并检查诊断。',credential_registry_full:'待清理凭据过多，请先完成凭据清理。',
    missing_key:'请填写密钥；环境变量不可用时，可改用钥匙串或会话密钥。', auth:'认证失败，请检查密钥及账号权限。',
    unsupported:'服务未提供标准模型目录，可添加自定义模型。',rate_limit:'请求被限流，请稍后重试。',
    connection:'无法连接服务，请检查地址及本地服务状态。',timeout:'请求超时，请重试。',
    http_error:'模型服务暂时不可用，请稍后重试。',invalid_response:'模型目录格式不兼容，可添加自定义模型。',
    too_large:'模型目录过大，可添加自定义模型。',invalid_provider:'请检查模型服务名称、地址及接口协议。',
    keychain_unavailable:'此系统不支持 macOS 钥匙串，请选择会话密钥或环境变量。',
    keychain_locked:'钥匙串已锁定或访问被取消，请解锁后重试。',keychain_failed:'无法访问系统钥匙串，请重试或改用会话密钥。',
    invalid_key:'密钥格式不正确，请重新填写。',provider_in_use:'此连接仍被角色或备用模型使用，请先更换相关配置。',
    builtin_provider:'内置连接不能移除。',unknown_provider:'此连接已不存在，请刷新列表。',
    connection_changed:'连接配置已经改变，请重新获取或测试。',update_in_progress:'服务正在更新，请稍后重试。',
    codex_missing:'未找到 Codex CLI，请安装并登录。',opencode_missing:'未找到 OpenCode，请安装后重新获取模型。',opencode_error:'无法读取 OpenCode 模型，请检查连接配置和 CLI 版本。',opencode_version_unknown:'无法确认 OpenCode 版本，请升级 Adaptive RSI，或临时使用 OpenCode 1.19.3。',codex_error:'无法读取 Codex 模型目录，请检查 CLI 版本和登录状态。',
    provider_paused:'模型服务连续失败已暂停，请检查配置后恢复请求。',model_missing:'模型不存在或当前账号不可用，请重新获取目录。',tool_format:'模型未返回符合要求的工具结果，请检查协议与模型能力。',probe_failed:'基础能力测试未通过，请检查账号权限、推理参数和模型工具能力。',
    budget_exhausted:'模型服务暂未提供调用，请检查服务账号用量或稍后重试。',default_effort_required:'此接口要求使用服务默认思考深度。',
    unsupported_model_kind:'此模型类型不能用于当前代码任务。',invalid_model:'请填写有效的模型 ID 和名称。',invalid_routes:'角色配置不完整，请重新选择。'
  };
  const purpose = {analysis:'查找有依据的改进',planning:'判断是否值得执行',implementation:'实现并补充验证',review:'检查正确性与可维护性',evolution:'分析证据与提出待验证改动'};
  const drafts = new Map(), baselines = new Map(), jobs = new Set(), savingRoutes = new Set(), inheritanceResets = new Map();
  let data, active='openai', scope=null, tab='roles', sequence=0, editing=null, editorSession=null, probeSession=null, picking=null, busy=false, loading=false;
  let feedbackKey='',feedbackError=false,modelPage=0,pickerPage=0,priceTarget=null;
  const pageSize=20;
  const scopeKey = () => scope || 'global';
  const safeRoute = route => ({provider:route.provider,model:route.model,effort:route.effort||'default',fallback:route.fallback?{provider:route.fallback.provider,model:route.fallback.model,effort:route.fallback.effort||'default'}:null});
  const safeRoutes = routes => Object.fromEntries(Object.entries(routes||{}).map(([role,route])=>[role,safeRoute(route)]));
  function routeDraft(baseline,routes,key){
    return {baseline:safeRoutes(baseline),routes:safeRoutes(routes),resetInheritance:inheritanceResets.has(key),resetModels:inheritanceResets.get(key)?.models};
  }
  function persistRouteDraft(){
    if(!data||!routes())return;const key=scopeKey(),draft=routeDraft(data.models,routes(),key);
    if(!draft.resetInheritance&&JSON.stringify(draft.baseline)===JSON.stringify(draft.routes))window.RSIDraftStore?.remove('routes',key);
    else window.RSIDraftStore?.set('routes',key,draft);
  }
  function inheritedRoles(models,reset){
    return reset?.models?Object.keys(models).filter(role=>JSON.stringify(safeRoute(models[role]))===JSON.stringify(reset.models[role])):[];
  }
  function rebaseReset(models,reset){
    for(const role of inheritedRoles(models,reset)){
      models[role]=structuredClone(data.global_models[role]);
      reset.models[role]=safeRoute(data.global_models[role]);
    }
  }
  const selected = () => data?.providers.find(p=>p.id===active);
  const routes = () => drafts.get(scopeKey());
  const modified = () => data?.project===scope && routes() && (inheritanceResets.has(scopeKey())||JSON.stringify(routes())!==JSON.stringify(data.models));
  const resetConflict = key => inheritanceResets.has(key)&&!inheritanceResets.get(key).models;
  const resetConflictCopy = '重置草稿缺少继承基线，已保留修改。请重新点击「跟随通用设置」后再保存，或撤销修改。';
  const jobKey = (provider,project=scope) => (project||'global')+':'+provider;
  function errorCopy(code,fallback='模型服务暂时不可用，请稍后重试。'){
    const match=String(code||'').match(/^opencode_incompatible_(\d+\.\d+\.\d+)$/);
    if(match)return t('已检测到 OpenCode ')+match[1]+t('，当前命令参数不兼容。请升级 Adaptive RSI，或临时使用 OpenCode 1.19.3。');
    return t(errors[code]||fallback);
  }
  const message = error => errorCopy(error.code,error.message);
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
    const key=scopeKey(),previous=baselines.get(key),draft=drafts.get(key);let conflicts=false;
    if((typeof reset==='function'?reset():reset)||!draft){
      const saved=window.RSIDraftStore?.get('routes',key),restored=structuredClone(data.models);
      if(saved?.baseline&&saved?.routes){
        if(scope&&saved.resetInheritance){
          const reset={models:saved.resetModels};inheritanceResets.set(key,reset);
          for(const role of Object.keys(data.models))if(saved.routes[role])restored[role]={...data.models[role],...saved.routes[role]};
          rebaseReset(restored,reset);
        }else for(const role of Object.keys(data.models)){const old=saved.baseline[role],incoming=safeRoute(data.models[role]),candidate=saved.routes[role];if(candidate&&JSON.stringify(old)===JSON.stringify(incoming))restored[role]={...data.models[role],...candidate};else if(candidate&&JSON.stringify(candidate)!==JSON.stringify(incoming))conflicts=true;}
        drafts.set(key,restored);
      }else drafts.set(key,restored);
    }else if(inheritanceResets.has(key)){
      if(!savingRoutes.has(key))rebaseReset(draft,inheritanceResets.get(key));
    }else if(previous)for(const role of Object.keys(data.models)){
      if(savingRoutes.has(key))continue;
      // 仅在服务端基线未变化时保留本地分工草稿。
      const unchanged=JSON.stringify(draft[role])===JSON.stringify(previous[role]);
      if(unchanged)draft[role]=structuredClone(data.models[role]);
      else if(JSON.stringify(safeRoute(previous[role]))!==JSON.stringify(safeRoute(data.models[role]))){draft[role]=structuredClone(data.models[role]);conflicts=true;}
    }
    baselines.set(key,structuredClone(data.models));
    if(!selected())active=data.providers[0].id;
    render();if(conflicts)setStatus(t('服务器模型设置已变化，冲突分工未从旧草稿恢复。'),true);
  }
  function render(){
    if(!data||loading||data.project!==scope)return;
    $('view-models').dataset.scope=scopeKey();
    for(const id of ['mc-add-provider','mc-edit-provider','mc-remove-provider','mc-add-model','mc-reset-inheritance'])$(id).disabled=false;
    $('mc-scope').value=scope?'project':'global';
    $('mc-scope').querySelector('[value="project"]').disabled=!projectId;
    $('mc-scope-note').textContent=scope?t('当前项目的模型服务与角色覆盖独立保存。'):t('模型服务与角色默认供各项目继承使用。');
    $('mc-scope-note').hidden=tab!=='providers';
    document.querySelectorAll('[data-mc-tab]').forEach(button=>{const on=button.dataset.mcTab===tab;button.setAttribute('aria-selected',String(on));button.tabIndex=on?0:-1;});
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
      copy.append(el('small',['openai','opencode'].includes(provider.id)?t('本机账户'):provider.credential_status==='missing'?t('待配置凭据'):provider.builtin?t('本地服务'):protocolName(provider.protocol)));
      button.append(symbol,copy);list.append(button);
    }
  }
  function protocolName(protocol){return {codex:'Codex / Responses',responses:'Responses API',chat_completions:'Chat Completions',anthropic:'Anthropic Messages',gemini:'Gemini',ollama:'Ollama API',codex_login:t('本机账户')}[protocol]||protocol;}
  function renderProvider(){
    const provider=selected(),catalog=provider.catalog;
    $('mc-provider-name').textContent=provider.id==='openai'?t('Codex 登录'):provider.name;
    $('mc-provider-address').textContent=provider.base_url||t(provider.id==='opencode'?'沿用此运行机器的 OpenCode 连接':'沿用本机 Codex 账户');
    const credential={login:'使用 Codex 登录',opencode:'使用 OpenCode 连接',configured:'凭据已配置',stored:'密钥已存入钥匙串',file:'密钥已保存到本机私有文件',missing:'待配置凭据',not_required:'无需密钥'};
    $('mc-credential-status').textContent=t(credential[provider.credential_status]);
    $('mc-catalog-status').textContent=catalog.fetched?t('已获取模型列表'):t('尚未获取模型列表');
    $('mc-catalog-status').className='badge '+(catalog.fetched?'good':'neutral');
    $('mc-refreshed').textContent=catalog.fetched?t('更新于 ')+stamp(catalog.fetched):'';
    $('mc-edit-provider').hidden=provider.builtin;$('mc-remove-provider').hidden=provider.builtin;
    $('mc-fetch').disabled=jobs.has(jobKey(provider.id));$('mc-fetch').textContent=t(jobs.has(jobKey(provider.id))?'获取中…':'获取模型');
    $('mc-directory-error').hidden=!catalog.error;
    $('mc-directory-error').textContent=catalog.error?errorCopy(catalog.error):'';
    const oauthAccount=['openai','opencode'].includes(provider.id);
    const connected=oauthAccount&&!!window.RSIAuth?.isConnected?.(provider.id);
    // OAuth 连接以本机登录状态为准；密钥/本地连接凭据由「连接设置」管理，无需密钥或已配置即完成，待配置时保持当前步骤。
    const accountReady=connected||(!oauthAccount&&provider.credential_status!=='missing');
    $('mc-step-provider').dataset.state='done';
    $('mc-step-account').dataset.state=accountReady?'done':'current';
    $('mc-step-models').dataset.state=catalog.fetched?'done':accountReady?'current':'pending';
    $('mc-truncated').hidden=!catalog.truncated;
    $('mc-health').hidden=!provider.health.paused&&!provider.health.failures;
    $('mc-health-status').textContent=provider.health.paused?t('连续失败已暂停')+' · '+t(errors[provider.health.code]||''):t('连续故障次数')+' '+provider.health.failures+' / 3';
    $('mc-resume').hidden=!provider.health.paused;$('mc-resume').disabled=false;
    let diagnostics=$('mc-diagnostics');
    if(!diagnostics){diagnostics=el('button',t('只读诊断'),'mc-text-button');diagnostics.id='mc-diagnostics';diagnostics.type='button';$('mc-health').after(diagnostics);}
    diagnostics.onclick=()=>safe(async()=>{
      const model=provider.catalog.models.find(item=>item.enabled)?.id||provider.catalog.models[0]?.id;
      if(!model)return;
      const result=await api('model-center/diagnose',{provider:provider.id,model,project:scope==='project'?scope:null});
      const box=dialog(t('模型路由诊断')),body=el('pre',JSON.stringify(result,null,2));
      box.append(el('p',t('本次只读取本地状态，不发送模型请求，也不产生费用。'),'muted'),body);
    });
    const authProvider=provider.id,authScope=scope;window.RSIAuth?.show(authProvider,()=>fetchProvider(authProvider,authScope,true));
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
      else if(model.configured&&!provider.catalog.fetched)status.push(t('当前配置 · 尚未获取模型列表'));
      if(model.kind)status.push(t('不适合代码任务的模型')+' · '+model.kind);
      const latest=Object.values(model.probes||{}).sort((a,b)=>b.at-a.at)[0];
      if(latest?.state==='fail')status.push(t('最近测试失败')+' · '+latest.effort+' · '+t(errors[latest.code]||''));
      if(model.verified)status.push(t('基础能力测试通过')+' · '+stamp(model.verified.at)+' · '+model.verified.effort+(model.verified.version?' · v'+model.verified.version:'')+' · '+t('仅适用于当时的连接与运行环境'));

      if(!model.kind&&!model.verified&&!latest)status.push(t('尚未进行模型测试'));
      if(status.length)copy.append(el('small',status.join(' · ')));
      if(model.probes)for(const proof of Object.values(model.probes)){if(proof.state==='pass'&&proof.effort!==model.verified?.effort)copy.append(el('small',t('基础能力测试通过')+' · '+proof.effort+' · '+stamp(proof.at)));}
      if(model.unit_price!=null)copy.append(el('small',t('估算价格')+' · $'+model.unit_price+' / 1M tokens'));
      const controls=el('div',undefined,'mc-model-actions'),label=el('label',undefined,'mc-enable'),checkbox=el('input');
      checkbox.type='checkbox';checkbox.checked=!!model.enabled;checkbox.disabled=!!model.kind;checkbox.setAttribute('aria-label',t('启用模型')+' '+model.name);
      label.title=t('此开关只控制模型选择列表；已有角色配置和正在运行的任务继续使用原来的模型。');
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
    const select=el('select');select.className='mc-effort';select.setAttribute('aria-label',title+' · '+t('思考深度'));
    for(const value of effortOptions(route,advanced)){const option=el('option',t({default:'服务默认',low:'快速 (low)',medium:'均衡 (medium)',high:'深入 (high)',xhigh:'更深入 (xhigh)',max:'充分思考 (max)',ultra:'最高强度 (ultra)'}[value]||value));option.value=value;select.append(option);}
    select.value=route.effort;select.addEventListener('change',()=>onChange(select.value));return select;
  }
  function renderRoles(){
    const container=$('mc-role-list'),extra=$('mc-extra-role-list');container.replaceChildren();extra.replaceChildren();
    for(const [role,title]of Object.entries(roleNames)){
      const route=routes()[role],row=el('div',undefined,'mc-role-row'),label=el('div',undefined,'mc-role-title');
      const simpleTitle={planning:'思考与规划',implementation:'代码实现',review:'代码评审'}[role]||title;
      label.append(el('strong',t(simpleTitle)),el('small',t(purpose[role])));
      if(scope)label.append(el('small',t(JSON.stringify(route)===JSON.stringify(data.global_models[role])?'跟随通用设置':'此项目单独设置')));
      const modelField=el('div',undefined,'mc-model-field'),select=action('',()=>openPicker(role),'mc-role-select mc-route-picker');
      select.id='mc-role-model-'+role;select.value=JSON.stringify([route.provider,route.model]);
      select.setAttribute('aria-label',t(simpleTitle)+' · '+t('选择模型'));select.setAttribute('aria-haspopup','dialog');
      select.append(el('strong',findModel(route)?.name||route.model),el('small',data.providers.find(p=>p.id===route.provider)?.name||route.provider),el('span','⌄','mc-picker-arrow'));
      const provider=data.providers.find(p=>p.id===route.provider),copy=el('span');
      const name=findModel(route)?.name||route.model;copy.append(el('small',(provider?.name||route.provider)+(name!==route.model?' · '+route.model:'')));
      const proof=findModel(route)?.probes?.[route.effort];copy.append(info(t(proof?.state==='pass'?'当前强度工具测试通过':proof?.state==='fail'?'当前强度测试失败':'当前强度尚未测试'),proof?.state==='fail'?'mc-warning':'mc-secondary'));
      if(scope&&JSON.stringify(route)===JSON.stringify(data.models[role]))copy.append(info(t(data.task_proofs?.[role]?'已通过真实任务验证':'尚未通过真实任务验证'),'mc-secondary'));
      if(route.fallback)copy.append(info(t('备用')+' · '+(data.providers.find(p=>p.id===route.fallback.provider)?.name||route.fallback.provider)+' / '+route.fallback.model+' · '+route.fallback.effort));
      if(findModel(route)?.missing)copy.append(info(t('目录中已失效'),'mc-warning'));
      const proofDetails=el('details',undefined,'mc-route-proof');proofDetails.append(el('summary',t('验证与备用模型')),copy);
      modelField.append(select,proofDetails);
      if(findModel(route)?.missing||proof?.state==='fail'||provider?.health.paused)modelField.append(el('p',t(findModel(route)?.missing?'目录中已失效':provider?.health.paused?'连续失败已暂停':'当前强度测试失败'),'mc-warning'));
      const control=el('div',undefined,'mc-effort-wrap'),effort=effortControl(route,t(simpleTitle),value=>{route.effort=value;renderRoles();$('mc-role-effort-'+role).focus();});effort.id='mc-role-effort-'+role;control.append(el('span',t('思考深度'),'mc-secondary'),effort);
      const advanced=action(t('高级参数'),()=>openAdvanced(role),'mc-text-button');control.append(advanced);
      row.append(label,modelField,control);(['analysis','evolution'].includes(role)?extra:container).append(row);
    }
    $('mc-inheritance').textContent=scope?(Object.keys(data.overrides).length?t('这个项目已单独选择模型。'):t('这个项目跟随通用模型设置。')):t('没有单独选模型的项目，会使用通用设置。');
    $('mc-reset-inheritance').hidden=!scope;
    updateDirty();
    persistRouteDraft();
  }
  function updateDirty(){const saving=savingRoutes.has(scopeKey());$('mc-dirty').textContent=t(saving?'正在保存模型分工…':modified()?'模型分工有未保存的修改':'模型分工已保存');$('mc-save-routes').disabled=saving||resetConflict(scopeKey())||!modified();$('mc-undo').disabled=!modified();if(resetConflict(scopeKey()))setStatus(t(resetConflictCopy),true);}
  function openPicker(role){
    picking=role;pickerPage=0;renderPickerProviders();$('mc-picker-title').textContent=t(roleNames[role])+' · '+t('选择模型');$('mc-picker-search').value='';renderPicker();$('mc-picker-dialog').showModal();$('mc-picker-search').focus();
  }
  function renderPickerProviders(){
    const select=$('mc-picker-provider'),before=select.value;select.replaceChildren();const all=el('option',t('所有模型服务'));all.value='';select.append(all);
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
      for(const model of models){const button=action('',()=>{const old=routes()[picking];routes()[picking]={...old,provider:provider.id,model:model.id,effort:old.provider===provider.id&&old.model===model.id?old.effort:'default'};$('mc-picker-dialog').close();renderRoles();$('mc-role-model-'+picking)?.focus();},'mc-picker-option');
        const copy=el('span');copy.append(el('strong',model.name));if(model.name!==model.id)copy.append(el('small',model.id));button.append(copy,info(model.verified||Object.values(model.probes||{}).some(p=>p.state==='pass')?t('已测试'):t('待验证')));list.append(button);}
    }
    if(!list.children.length)list.append(el('p',t('没有已启用的匹配模型，请先到模型服务页面获取或添加。'),'mc-empty'));
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
    $('mc-go-hint').hidden=$('mc-provider-preset').value!=='opencode_go';
    $('mc-input-name').value=t(preset.name);$('mc-input-url').value=preset.base_url;$('mc-input-protocol').value=preset.protocol;$('mc-input-env').value=preset.env_key;
    $('mc-input-auth').value=preset.env_key?(data.keychain?'keychain':'file'):'none';syncAuth();
  }
  function syncAuth(){const mode=$('mc-input-auth').value;$('mc-key-field').hidden=!['keychain','session','file'].includes(mode);$('mc-input-key').disabled=!['keychain','session','file'].includes(mode);$('mc-env-field').hidden=mode!=='env';$('mc-input-env').disabled=mode!=='env';$('mc-input-env').required=mode==='env';$('mc-input-env').pattern='[A-Za-z_][A-Za-z0-9_]*';$('mc-session-hint').hidden=mode!=='session';}
  function openEditor(provider=null){
    editorSession={project:scope};editing=provider;$('mc-provider-form').reset();$('mc-provider-error').textContent='';$('mc-provider-dialog-title').textContent=t(provider?'连接设置':'添加模型服务');
    $('mc-connection-advanced').open=false;$('mc-go-hint').hidden=true;
    $('mc-provider-preset-field').hidden=!!provider;
    $('mc-input-auth').querySelector('[value="keychain"]').disabled=!data.keychain;
    if(provider){$('mc-input-name').value=provider.name;$('mc-input-url').value=provider.base_url;$('mc-input-protocol').value=provider.protocol;$('mc-input-env').value=provider.env_key;$('mc-input-auth').value=provider.credential||'env';syncAuth();}
    else applyPreset();
    $('mc-input-key').placeholder=t(provider&&['stored','configured'].includes(provider.credential_status)?'留空保留当前密钥':'填写 API Key');
    $('mc-provider-dialog').showModal();
  }
  async function fetchProvider(providerId=active,requested=scope,propagateError=false){
    const key=jobKey(providerId,requested);if(jobs.has(key)){if(propagateError)throw new Error('catalog_refresh_pending');return;}
    jobs.add(key);if(scope===requested)renderProvider();
    try{await api('model-center/fetch',{provider:providerId,project:requested});if(scope===requested){await load();if(scope===requested&&active===providerId)setStatus(t('模型列表已更新，原来选择的模型没有改变。'));}}
    catch(error){if(scope===requested){await load();if(scope===requested&&active===providerId)setStatus(message(error),true);}if(propagateError)throw error;}
    finally{jobs.delete(key);if(scope===requested&&data)renderProvider();}
  }
  function invalidateProbe(){
    probeSession=null;
    const dialog=$('mc-probe-dialog');if(dialog.open)dialog.close();
    $('mc-probe-result').textContent='';$('mc-probe-start').disabled=true;
  }
  function changeScope(next){if(scope!==next)invalidateProbe();scope=next;}
  function openProbe(provider,model){
    if(busy){setStatus(t('正在执行基础能力测试…'));return;}
    probeSession={project:scope,provider:provider.id,model:model.id};
    $('mc-probe-title').textContent=t('测试模型')+' · '+model.name;
    $('mc-probe-target').textContent=provider.name+' / '+model.id;
    $('mc-probe-dialog').dataset.provider=provider.id;$('mc-probe-dialog').dataset.model=model.id;
    const route=Object.values(routes()).find(r=>r.provider===provider.id&&r.model===model.id)||{provider:provider.id,model:model.id,effort:'default'};
    $('mc-probe-effort').replaceChildren(effortControl({...route},t('测试模型'),()=>{},true));$('mc-probe-result').textContent='';$('mc-probe-start').disabled=false;$('mc-probe-dialog').showModal();
  }
  function init(){
    const tabs=[...document.querySelectorAll('[data-mc-tab]')];
    tabs.forEach(button=>{button.tabIndex=button.dataset.mcTab===tab?0:-1;button.addEventListener('click',()=>{tab=button.dataset.mcTab;render();});});
    tabs[0].parentElement.addEventListener('keydown',event=>{
      if(event.key!=='ArrowLeft'&&event.key!=='ArrowRight')return;
      const current=tabs.indexOf(document.activeElement);
      if(current<0)return;
      event.preventDefault();
      const next=tabs[(current+(event.key==='ArrowRight'?1:tabs.length-1))%tabs.length];
      next.focus();next.click();
    });
    $('mc-scope').addEventListener('change',()=>safe(async()=>{changeScope($('mc-scope').value==='project'?projectId:null);setStatus('');await load();}));
    $('mc-add-provider').addEventListener('click',()=>{if(data)openEditor();});$('mc-edit-provider').addEventListener('click',()=>openEditor(selected()));
    $('mc-provider-preset').addEventListener('change',applyPreset);$('mc-input-auth').addEventListener('change',syncAuth);
    $('mc-search').addEventListener('input',()=>{modelPage=0;renderModels();});
    $('mc-model-filter').addEventListener('change',()=>{modelPage=0;renderModels();});
    $('mc-prev').addEventListener('click',()=>{modelPage--;renderModels();});$('mc-next').addEventListener('click',()=>{modelPage++;renderModels();});
    $('mc-undo').addEventListener('click',()=>{if(resetConflict(scopeKey()))setStatus('');inheritanceResets.delete(scopeKey());drafts.set(scopeKey(),structuredClone(data.models));renderRoles();});
    $('mc-resume').addEventListener('click',()=>safe(async()=>{await request('resume',{provider:active});await load();setStatus(t('已恢复请求，下次调用将重新检查。'));}));
    $('mc-fallback-provider').addEventListener('change',()=>renderFallbackModels());$('mc-fallback-model').addEventListener('change',()=>renderFallbackEffort());
    $('mc-price-form').addEventListener('submit',event=>{event.preventDefault();safe(async()=>{
      const unchanged=unchangedForm(event.target),project=scope,target={...priceTarget},button=event.target.querySelector('button[type="submit"]');button.disabled=true;$('mc-price-error').textContent='';
      try{await api('model-center/model',{project:target.project,provider:target.provider,model:{id:target.model,unit_price:$('mc-price-input').value===''?null:Number($('mc-price-input').value)}});if(unchanged()&&scope===project)$('mc-price-dialog').close();if(scope===project)await load();}
      catch(error){if(unchanged()&&scope===project)$('mc-price-error').textContent=message(error);else toast(t('先前的提交未成功。当前输入已保留，请回到原来的项目或设置范围检查。'),true);}
      finally{button.disabled=false;}
    },event.target);});$('mc-picker-search').addEventListener('input',()=>{pickerPage=0;renderPicker();});
    $('mc-picker-provider').addEventListener('change',()=>{pickerPage=0;renderPicker();});
    $('mc-picker-prev').addEventListener('click',()=>{pickerPage--;renderPicker();});$('mc-picker-next').addEventListener('click',()=>{pickerPage++;renderPicker();});
    $('mc-fetch').addEventListener('click',()=>safe(()=>fetchProvider()));
    $('mc-provider-dialog').addEventListener('close',()=>{editorSession=null;$('mc-input-key').value='';});
    $('mc-provider-form').addEventListener('invalid',event=>{const detail=event.target.closest('details');if(detail)detail.open=true;},true);
    $('mc-provider-form').addEventListener('submit',event=>{event.preventDefault();safe(async()=>{
      const button=$('mc-provider-submit'),session=editorSession;if(button.disabled||!session)return;const project=session.project,unchanged=unchangedForm(event.target);button.disabled=true;$('mc-provider-error').textContent='';
      let connectionSaved=false,remaining=[];
      try{const provider={name:$('mc-input-name').value.trim(),base_url:$('mc-input-url').value.trim(),protocol:$('mc-input-protocol').value,env_key:$('mc-input-env').value.trim()};if(editing)provider.id=editing.id;
        const models=[...new Set($('mc-input-models').value.split(/\r?\n/).map(id=>id.trim()).filter(Boolean))];
        if(models.some(id=>id.length>256)||models.length>50)throw Error(t('最多添加 50 个模型，每个模型 ID 不超过 256 个字符。'));
        if(models.some(id=>!(/^[a-zA-Z0-9._/:-]+$/).test(id)))throw Error(t('模型 ID 只能包含字母、数字、点、下划线、斜线、冒号和连字符。'));
        const authentication={mode:$('mc-input-auth').value,value:$('mc-input-key').value};$('mc-input-key').value='';
        const result=await api('model-center/provider',{project,provider,authentication});
        connectionSaved=true;remaining=[...models];
        if(editorSession===session)editing={...provider,id:result.id};
        for(const id of models){await api('model-center/model',{project,provider:result.id,model:{id,manual:true}});remaining.shift();}
        if(scope===project){await refresh();await load();if(unchanged()&&scope===project){active=result.id;tab='providers';render();$('mc-provider-dialog').close();}setStatus(t('连接配置已保存。'));}
        await fetchProvider(result.id,project);
      }catch(error){if(editorSession===session){$('mc-provider-error').textContent=(connectionSaved?t('连接已保存；部分后续操作未完成。尚未添加的模型：')+' '+remaining.join(', ')+' · ':'')+message(error);if(connectionSaved&&unchanged())$('mc-input-models').value=remaining.join('\n');}}finally{button.disabled=false;}
    });});
    $('mc-remove-provider').addEventListener('click',()=>safe(async()=>{
      // 移除立即退役连接并删除本机凭据，重新添加生成新 id；必须先确认，取消路径不发出任何请求。
      if(!(await uiConfirm(t('此连接与本机保存的凭据（钥匙串或私有文件）会一并删除且不可恢复；重新添加将生成新连接，启用、收藏、验证与价格记录不会恢复。若仍被角色分工或备用模型引用，服务端会拒绝移除。'))))return;
      await request('remove',{provider:active});await load();await refresh();setStatus(t('连接已移除。'));
    }));
    $('mc-add-model').addEventListener('click',()=>{$('mc-custom-form').reset();$('mc-custom-error').textContent='';$('mc-custom-dialog').showModal();});
    $('mc-custom-form').addEventListener('submit',event=>{event.preventDefault();safe(async()=>{
      const unchanged=unchangedForm(event.target),project=scope,provider=active,button=event.target.querySelector('button[type="submit"]');button.disabled=true;$('mc-custom-error').textContent='';
      try{const id=$('mc-custom-id').value.trim();await api('model-center/model',{project,provider,model:{id,name:$('mc-custom-name').value.trim(),manual:true}});if(unchanged()&&scope===project)$('mc-custom-dialog').close();if(scope===project)await load();}
      catch(error){if(unchanged()&&scope===project)$('mc-custom-error').textContent=message(error);else toast(t('先前的提交未成功。当前输入已保留，请回到原来的项目或设置范围检查。'),true);}
      finally{button.disabled=false;}
    },event.target);});
    $('mc-save-routes').addEventListener('click',()=>safe(async()=>{
      const project=scope,key=scopeKey();if(savingRoutes.has(key))return;
      if(resetConflict(key)){setStatus(t(resetConflictCopy),true);return;}
      const models=structuredClone(routes()),submittedReset=inheritanceResets.get(key);savingRoutes.add(key);updateDirty();setStatus(t('正在保存模型分工…'));
      try{
        const inherit=inheritedRoles(models,submittedReset);
        const result=await api('model-center/routes',{project,models,revision:project?state?.config_revision:state?.global_config_revision,...(submittedReset?{inherit}:{})}),saved=result.models||models,current=drafts.get(key);
        const sameReset=inheritanceResets.get(key)===submittedReset;
        if(current&&sameReset)for(const role of Object.keys(saved))if(JSON.stringify(safeRoute(current[role]))===JSON.stringify(safeRoute(models[role])))current[role]=structuredClone(saved[role]);
        if(sameReset)inheritanceResets.delete(key);
        baselines.set(key,structuredClone(saved));
        if(data?.project===project)data.models=structuredClone(saved);
        if(current){const draft=routeDraft(saved,current,key);if(!draft.resetInheritance&&JSON.stringify(draft.baseline)===JSON.stringify(draft.routes))window.RSIDraftStore?.remove('routes',key);else window.RSIDraftStore?.set('routes',key,draft);}
        if(scope===project){await load();await refresh();if(scope===project)setStatus(t(modified()?'已保存提交的配置，后续修改仍未保存。':'模型分工已保存，从下一轮任务生效。'));}
      }catch(error){if(scope===project)setStatus(message(error),true);else toast(t('先前的提交未成功。当前输入已保留，请回到原来的项目或设置范围检查。'),true);}
      finally{savingRoutes.delete(key);if(scope===project)updateDirty();}
    }));
    $('mc-reset-inheritance').addEventListener('click',()=>{if(!scope)return;inheritanceResets.set(scopeKey(),{models:safeRoutes(data.global_models)});drafts.set(scopeKey(),structuredClone(data.global_models));renderRoles();setStatus(t('已选择跟随通用设置，保存后生效。'));});
    $('mc-advanced-form').addEventListener('submit',event=>{event.preventDefault();try{const provider=$('mc-fallback-provider').value,model=$('mc-fallback-model').value;const fallback=provider?{provider,model,effort:$('mc-fallback-effort').querySelector('select').value}:null,route=routes()[$('mc-advanced-dialog').dataset.role];if(provider&&!model)throw Error(t('请先获取并启用备用模型。'));if(fallback&&provider===route.provider&&model===route.model)throw Error(t('备用模型应与主模型不同。'));route.fallback=fallback;route.effort=$('mc-advanced-effort').querySelector('select').value;$('mc-advanced-dialog').close();renderRoles();}catch(error){$('mc-advanced-error').textContent=error.message;}});
    $('mc-probe-dialog').addEventListener('close',()=>{if(!$('mc-probe-dialog').open)probeSession=null;});
    $('mc-probe-start').addEventListener('click',()=>safe(async()=>{
      const button=$('mc-probe-start'),session=probeSession;
      if(!session||session.project!==scope||!$('mc-probe-dialog').open||busy)return;
      const current=()=>probeSession===session&&scope===session.project;
      button.disabled=true;busy=true;$('mc-probe-result').textContent=t('正在执行基础能力测试…');
      try{const result=await api('model-center/probe',{...session,effort:$('mc-probe-effort').querySelector('select').value});
        if(current()){$('mc-probe-result').textContent=t('基础能力测试通过')+' · '+result.calls.length+' '+t('次调用')+' · '+result.calls.reduce((n,c)=>n+c.tokens,0)+' tokens';await load();}
      }catch(error){if(current()){$('mc-probe-result').textContent=message(error);await load();}}finally{busy=false;if(current())button.disabled=false;}
    }));
    document.querySelectorAll('[data-open-model-center]').forEach(button=>button.addEventListener('click',()=>showView('models')));
    window.addEventListener('beforeunload',event=>{if(modified()){event.preventDefault();event.returnValue='';}});
  }
  init();
  function translate(){
    if(!data)return;render();window.RSIAuth?.translate();setStatus(feedbackKey,feedbackError);
    if($('mc-picker-dialog').open){renderPickerProviders();$('mc-picker-title').textContent=t(roleNames[picking])+' · '+t('选择模型');renderPicker();}
    if($('mc-provider-dialog').open){$('mc-provider-dialog-title').textContent=t(editing?'连接设置':'添加模型服务');$('mc-input-key').placeholder=t(editing&&['stored','configured'].includes(editing.credential_status)?'留空保留当前密钥':'填写 API Key');}
    if($('mc-advanced-dialog').open){const provider=$('mc-fallback-provider').value,model=$('mc-fallback-model').value,effort=$('mc-fallback-effort').querySelector('select').value;renderFallback(provider?{provider,model,effort}:null);const role=$('mc-advanced-dialog').dataset.role;$('mc-advanced-title').textContent=t(roleNames[role])+' · '+t('高级参数');}
    if($('mc-probe-dialog').open){const dialog=$('mc-probe-dialog'),model=data.providers.find(p=>p.id===dialog.dataset.provider)?.catalog.models.find(m=>m.id===dialog.dataset.model);$('mc-probe-title').textContent=t('测试模型')+' · '+(model?.name||dialog.dataset.model);}
  }
  return {show:(requested=null,reload=false)=>{
    const next=requested||null,changed=scope!==next;changeScope(next);
    if(loading&&!changed)return;
    if(!data||changed||data.project!==scope||reload)safe(()=>load());
  },connections:()=>{tab='providers';if(data)render();},translate,load};
})();
