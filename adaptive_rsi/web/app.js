'use strict';
let state, projectId = localStorage.getItem('rsi-project') || '', view = 'overview', settingsBaseline, toastTimer;
const $ = id => document.getElementById(id);
const statusNames = {deferred:'已暂缓',rejected:'不采纳',queued:'排队中',planning:'规划中',executing:'实现中',evaluating:'评估中',awaiting_approval:'待交付',pr_open:'PR 已创建',completed:'已完成',obsolete:'已过时',blocked:'已阻塞',failed:'未通过',interrupted:'已中断',cancelled:'已取消',insufficient_evidence:'证据不足',stable:'稳定使用',superseded:'历史稳定版',ready:'等待晋升',pass:'通过',fail:'未通过',rolled_back:'已回退'};
const sourceNames = {goal:'用户目标',inspection:'自主发现',issue:'GitHub Issue',system_light:'轻量巡检',system_deep:'深度巡检',system_issues:'Issues 同步',system_evolve:'策略改进',system_publish:'批准交付'};
const reviewNames = {correctness:'正确性',security:'安全性',maintainability:'可维护性',architecture:'架构',tests:'测试',scope:'改动范围',critical:'致命',high:'高',medium:'中',low:'低'};
const roleNames = {analysis:'项目理解 / 巡检',planning:'规划',implementation:'代码实现 / 修复',review:'独立审查',evolution:'策略改进'};
function el(tag, text, cls){ const node=document.createElement(tag); if(text!==undefined)node.textContent=text; if(cls)node.className=cls; return node; }
function toast(message,error=false){ $('toast').textContent=message; $('toast').className=error?'error':''; $('toast').hidden=false; clearTimeout(toastTimer); toastTimer=setTimeout(()=>$('toast').hidden=true,6500); }
async function api(path,body){ const options=body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json','X-RSI-CSRF':state.csrf},body:JSON.stringify(body)}; const response=await fetch('/api/'+path,options); const data=await response.json(); if(!response.ok)throw Error(data.error||response.status); return data; }
function currentProject(){return state.projects.find(p=>p.id===projectId);}
function scoped(rows){return rows.filter(row=>row.project===projectId);}
function stamp(seconds){return new Date(seconds*1000).toLocaleString(language,{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'});}
function badge(status){return el('span',t(statusNames[status]||status),'badge '+(['stable','completed','pr_open','pass','awaiting_approval'].includes(status)?'good':['failed','fail','blocked','rolled_back'].includes(status)?'bad':['queued','planning','executing','evaluating'].includes(status)?'warn':'neutral'));}
function action(text,callback,cls='button'){const node=el('button',text,cls);node.type='button';node.addEventListener('click',()=>safe(callback));return node;}
async function safe(fn){try{await fn();}catch(error){toast(error.message,true);}}
async function refresh(){const next=await api('state');if(state&&state.version!==next.version){location.reload();return;}state=next; $('connection').textContent=t('已连接'); $('version').textContent='Adaptive RSI '+state.version;
 const select=$('project-select'); const old=projectId; if(!state.projects.some(p=>p.id===projectId))projectId=state.projects[0]?.id||'';
 if(JSON.stringify([...select.options].map(x=>[x.value,x.text]))!==JSON.stringify(state.projects.map(p=>[p.id,p.name]))){select.replaceChildren(...state.projects.map(p=>{const o=el('option',p.name);o.value=p.id;return o;})); if(!state.projects.length){const o=el('option',t('尚未接入项目'));o.value='';select.append(o);}}
 select.value=projectId; if(old!==projectId){settingsBaseline=null;localStorage.setItem('rsi-project',projectId);}
 render();}
function render(){const project=currentProject(), tasks=scoped(state.tasks), runs=scoped(state.runs); $('onboarding').hidden=!!project;
 $('pause-button').disabled=!project; $('pause-button').textContent=!project||project.paused?t('启用项目'):t('暂停项目'); $('project-status').textContent=!project?t('尚未接入'):project.paused?t('已暂停'):t('运行中');
 $('project-status').className='badge '+(project&&!project.paused?'good':'neutral');
 const queue=tasks.filter(t=>t.state==='queued').length; $('stat-queue').textContent=String(queue).padStart(2,'0'); $('queue-count').textContent=queue;
 $('stat-active').textContent=String(runs.filter(r=>!r.ended).length).padStart(2,'0'); $('stat-pass').textContent=String(tasks.filter(t=>['awaiting_approval','pr_open'].includes(t.state)).length).padStart(2,'0');
 const usage=state.usage.find(u=>u.project===projectId); $('stat-calls').textContent=String(usage?.calls||0).padStart(2,'0'); $('stat-tokens').textContent=(usage?.tokens||0).toLocaleString()+t(' tokens · 实际记录');
 const config=project?.config||state.config;
 for(const kind of ['light','issues','deep']){const mins=config.schedule[kind+'_minutes']; let number=mins,unit=t('分钟');if(mins%1440===0){number=mins/1440;unit=t('天');}else if(mins%60===0){number=mins/60;unit=t('小时');} if(language==='en'&&number===1)unit={minutes:'minute',hours:'hour',days:'day'}[unit]; const node=$(kind+'-interval');node.replaceChildren(document.createTextNode(number+' '),el('small',unit+t(' / 次')));
  const schedule=state.schedules.find(s=>s.project===projectId&&s.kind===kind); $(kind+'-due').textContent=!project?t('接入后开始'):project.paused?t('项目已暂停'):kind==='issues'&&!config.github.repository?t('未关联仓库'):schedule?t('下次 ')+stamp(schedule.due):t('等待首次检查');}
 document.querySelectorAll('[data-inspect]').forEach(b=>b.disabled=!project);
 const activity=$('activity');activity.replaceChildren();const events=state.events.filter(e=>!e.project||e.project===projectId).slice(0,9);if(!events.length)activity.append(el('p',t('运行记录将在这里出现。每次调用、检查与决策都可追溯。'),'empty-copy'));
 for(const e of events){const row=el('div',undefined,'activity-row');row.append(el('time',stamp(e.created)),el('span',e.kind,'event-kind'),el('span',e.message));activity.append(row);}
 renderUpdate();if(view==='tasks')renderTasks(); if(view==='evolution')renderEvolution(); if(view==='settings'&&!settingsBaseline)renderSettings();}
function showView(name){view=name;document.querySelectorAll('.view').forEach(node=>node.hidden=node.id!=='view-'+name);document.querySelectorAll('.nav').forEach(node=>node.classList.toggle('active',node.dataset.view===name));$('page-name').textContent={overview:t('运行总览'),tasks:t('任务与证据'),evolution:t('策略与记忆'),settings:t('运行设置')}[name];if(name==='settings'){settingsBaseline=null;renderSettings();}render();window.scrollTo(0,0);}
function renderTasks(){const list=$('tasks-list');list.replaceChildren();const filter=$('task-filter').value;
 const tasks=scoped(state.tasks).filter(t=>filter==='all'||(filter==='running'?['planning','executing','evaluating'].includes(t.state):filter==='blocked'?['blocked','failed','insufficient_evidence','interrupted'].includes(t.state):t.state===filter));
 if(!tasks.length)list.append(el('p',t('这里还没有任务。可以输入目标，或启用巡检与 Issues 同步。'),'empty-copy'));
 for(const task of tasks){const row=el('div',undefined,'task-row'),title=el('div');title.append(action(task.title,()=>showTask(task),'task-title'),el('span',stamp(task.updated)+' · '+task.id.slice(0,8),'task-meta'));const actions=el('div',undefined,'task-actions');
  if(['blocked','failed','interrupted','cancelled','insufficient_evidence','deferred','rejected'].includes(task.state))actions.append(action(t('重试'),async()=>{await api('retry',{task:task.id});toast(t('已重新排队'));await refresh();},''));
  if(['queued','planning','executing','evaluating'].includes(task.state))actions.append(action(t('取消'),async()=>{await api('cancel',{task:task.id});await refresh();},''));
  row.append(el('span',t(sourceNames[task.source]||task.source),'source-tag'),title,badge(task.state),actions);list.append(row);}}
let detailTask;
async function showTask(task){detailTask=task;$('detail-title').textContent=task.title;const body=$('detail-body');body.replaceChildren(badge(task.state));const description=el('p',task.body,'muted');body.append(description);
 const evidence=el('section',undefined,'detail-section');evidence.append(el('h3',t('来源与证据')),el('pre',JSON.stringify(JSON.parse(task.evidence),null,2)));body.append(evidence);
 for(const run of scoped(state.runs).filter(r=>r.task===task.id)){const data=await api('run?id='+run.id),section=el('section',undefined,'detail-section');section.append(el('h3',t('运行 ')+run.id+' · '+(t(statusNames[run.state]||run.state))),el('p',run.reason,'muted'));
  section.append(el('pre',JSON.stringify({models:data.config.models,calls:data.calls,tokens:data.tokens,base:data.base,result:data.result},null,2)));
  if(data.result.value){const report=el('div',undefined,'review-report');report.append(el('h3',t('任务价值评估')),el('pre',JSON.stringify(data.result.value,null,2)));section.append(report);}
  if(data.result.review){
   const review=data.result.review,report=el('div',undefined,'review-report');report.append(el('h3',t('代码评审')),badge(review.decision),el('p',review.reason));
   for(const assessment of review.assessments||[]){const row=el('p');row.append(badge(assessment.decision),el('strong',' '+t(reviewNames[assessment.area]||assessment.area)+' · '),document.createTextNode(assessment.reason));report.append(row);}
   for(const finding of review.findings||[]){const card=el('div',undefined,'provider-card');card.append(el('strong',t(reviewNames[finding.severity]||finding.severity)+' · '+finding.title),el('p',finding.path+':'+finding.line),el('p',finding.reason),el('p',t('修改建议')+'：'+finding.suggestion));report.append(card);}
   section.append(report);
  }
  if(data.result.files){const link=el('a',t('查看候选补丁 ↗'));link.href='/api/patch?id='+run.id;link.target='_blank';link.rel='noreferrer';section.append(link);}
  if(run.state==='awaiting_approval')section.append(action(t('批准推送并创建 PR'),async()=>{if(!window.confirm(t('这会把候选分支推送到关联 GitHub 仓库，并创建 PR。确认批准？')))return;await api('deliver',{run:run.id});toast(currentProject()?.paused?t('交付任务已加入；启用项目后执行'):t('交付任务已加入'));$('detail-dialog').close();await refresh();}));
  if(run.pr){const link=el('a',t('打开 GitHub PR ↗'));if(/^https:\/\/github\.com\//.test(run.pr)){link.href=run.pr;link.target='_blank';link.rel='noreferrer';section.append(link);}}
  if(run.state==='pr_open')section.append(action(t('生成这次改动的回退候选'),async()=>{await api('revert-code',{run:run.id});toast(t('回退任务已加入；通过检查后才可交付'));$('detail-dialog').close();await refresh();}));
  body.append(section);}
 if(!$('detail-dialog').open)$('detail-dialog').showModal();}
function renderEvolution(){const summary=$('suite-summary');summary.replaceChildren();for(const [split,title,description]of [['development',t('开发集'),t('用于提出和调整候选')],['validation',t('验证集'),t('配对比较 · 受限复用')],['holdout',t('最终评估集'),t('隐藏任务 · 限制曝光次数')]]){const count=scoped(state.cases).find(c=>c.split===split)?.count||0,card=el('article',undefined,'suite-card');card.append(el('strong',count),el('span',title),el('small',description));summary.append(card);}
 const list=$('candidates');list.replaceChildren();const candidates=scoped(state.candidates);if(!candidates.length)list.append(el('p',t('还没有策略候选。运行积累证据后会自动提出，也可以手动发起。'),'empty-copy'));
 for(const c of candidates){const card=el('article',undefined,'panel candidate-card'),top=el('div',undefined,'panel-title');top.append(el('h2',{strategy:t('执行策略'),meta_strategy:t('元策略'),adapter:t('适配指导')}[c.kind]||c.kind),badge(c.status));card.append(top,el('div',(c.parent?c.parent.slice(0,8):'BASELINE')+' → '+c.id.slice(0,8),'lineage'),el('p',c.content.guidance),el('p',c.reason,'muted'),el('small',c.trial_count+t(' 组配对任务 · ')+stamp(c.created),'muted'));
  if(c.status==='stable')card.append(action(t('回退至上一稳定版'),async()=>{await api('rollback',{candidate:c.id});toast(t('策略已回退'));await refresh();}));list.append(card);}}
function getPath(obj,path){return path.split('.').reduce((v,k)=>v?.[k],obj);}
function setPath(obj,path,value){const keys=path.split('.');let node=obj;keys.slice(0,-1).forEach(k=>node=node[k]??={});node[keys.at(-1)]=value;}
function deepMerge(a,b){for(const [key,value]of Object.entries(b)){if(value&&typeof value==='object'&&!Array.isArray(value))a[key]=deepMerge(a[key]||{},value);else a[key]=value;}return a;}
function readProviders(){return [...$('provider-fields').children].map(row=>Object.fromEntries([...row.querySelectorAll('[data-field]')].map(input=>[input.dataset.field,input.value])));}
function refreshProviderChoices(){
 const choices=[['openai','OpenAI / Codex'],['ollama','Ollama'],['lmstudio','LM Studio'],...readProviders().map(p=>[p.id,p.name||p.id])];
 document.querySelectorAll('[data-path$=".provider"]').forEach(select=>{
  const selected=select.value||getPath(settingsBaseline,select.dataset.path);
  select.replaceChildren();
  for(const [id,name]of choices){const option=el('option',name);option.value=id;select.append(option);}
  if(selected&&!choices.some(p=>p[0]===selected)){const option=el('option',selected+' · '+t('服务未配置'));option.value=selected;select.append(option);}
  select.value=selected;
 });
}
function renderProviders(providers){
 $('provider-fields').replaceChildren();
 for(const provider of providers){
  const row=el('div',undefined,'provider-card'),grid=el('div',undefined,'field-grid');
  for(const [key,title,placeholder]of [['id','服务标识','gateway'],['name','显示名称','My gateway'],['base_url','服务地址','https://example.com/v1'],['env_key','密钥环境变量名','MODEL_API_KEY']]){
   const label=el('label',t(title)),input=el('input');input.dataset.field=key;input.value=provider[key]||'';input.placeholder=placeholder;input.setAttribute('aria-label',t(title));
   input.addEventListener('input',refreshProviderChoices);input.addEventListener('change',refreshProviderChoices);label.append(input);grid.append(label);
  }
  const protocolLabel=el('label',t('接口协议')),protocol=el('select');protocol.dataset.field='protocol';protocol.setAttribute('aria-label',t('接口协议'));
  for(const [value,label]of [['codex','Codex / Responses'],['responses','Responses API'],['chat_completions','Chat Completions API'],['anthropic','Anthropic Messages'],['gemini','Gemini GenerateContent'],['ollama','Ollama API']]){const option=el('option',label);option.value=value;protocol.append(option);}
  protocol.value=provider.protocol||'codex';protocolLabel.append(protocol);grid.append(protocolLabel);
  row.append(grid,action(t('移除服务'),()=>{row.remove();refreshProviderChoices();}));$('provider-fields').append(row);
 }
 refreshProviderChoices();
}
function renderSettings(){settingsBaseline=structuredClone($('config-scope').value==='global'?state.config:(currentProject()?.config||state.config));const models=$('model-fields');models.replaceChildren();
 renderProviders(settingsBaseline.providers);
 for(const [role,title]of Object.entries(roleNames)){
  const row=el('div',undefined,'model-row'),input=el('input'),select=el('select'),provider=el('select');
  input.dataset.path='models.'+role+'.model';input.setAttribute('aria-label',t(title)+t(' 模型'));
  provider.dataset.path='models.'+role+'.provider';provider.setAttribute('aria-label',t(title)+' · '+t('模型服务'));
  select.dataset.path='models.'+role+'.effort';select.setAttribute('aria-label',t(title)+t(' 推理强度'));
  for(const effort of ['default','low','medium','high','xhigh','max','ultra']){const o=el('option',effort==='default'?t('服务默认'):effort);o.value=effort;select.append(o);}
  provider.addEventListener('change',()=>{if(provider.value!=='openai')select.value='default';});
  row.append(el('span',t(title)),provider,input,select);models.append(row);
 }
 refreshProviderChoices();
 document.querySelectorAll('[data-path]').forEach(node=>{const value=getPath(settingsBaseline,node.dataset.path);if(node.type==='checkbox')node.checked=value;else node.value=value;});
 document.querySelector('[data-path="limits.concurrency"]').disabled=$('config-scope').value!=='global';
 document.querySelectorAll('[data-path^="updates."]').forEach(node=>node.disabled=$('config-scope').value!=='global');
 renderUpdate();
 $('work-start').value=settingsBaseline.schedule.working_hours[0];$('work-end').value=settingsBaseline.schedule.working_hours[1];$('issue-labels').value=settingsBaseline.github.labels.join(', ');$('checks-json').value=JSON.stringify(settingsBaseline.checks,null,2);$('advanced-json').value='';
 safe(async()=>{const target=$('capabilities');target.replaceChildren();if(!projectId){target.append(el('p',t('接入项目后识别工具链。'),'muted'));return;}const capabilities=await api('capabilities?project='+projectId);if(!capabilities.modules.length)target.append(el('p',t('未识别到标准构建清单。可手动配置验收命令。'),'muted'));for(const module of capabilities.modules){const row=el('div',undefined,'capability-row');row.append(el('strong',module.language),el('code',module.manifest),el('span',{executable:t('检查可执行 · 已验证'),unverified:t('待隔离验证'),analysis_only:t('可分析 · 需配置检查'),environment_blocked:t('环境缺少工具')}[module.status],'badge '+(module.status==='environment_blocked'?'warn':module.status==='executable'?'good':'neutral')));target.append(row);}});}
document.querySelectorAll('.nav').forEach(node=>node.addEventListener('click',()=>showView(node.dataset.view)));
const providerPresets={
 custom:{id:'gateway',name:'Gateway',base_url:'https://example.com/v1',env_key:'MODEL_API_KEY',protocol:'chat_completions'},
 openai:{id:'openai_api',name:'OpenAI API',base_url:'https://api.openai.com/v1',env_key:'OPENAI_API_KEY',protocol:'responses'},
 anthropic:{id:'anthropic_api',name:'Anthropic',base_url:'https://api.anthropic.com/v1',env_key:'ANTHROPIC_API_KEY',protocol:'anthropic'},
 gemini:{id:'gemini_api',name:'Gemini',base_url:'https://generativelanguage.googleapis.com/v1beta',env_key:'GEMINI_API_KEY',protocol:'gemini'},
 deepseek:{id:'deepseek_api',name:'DeepSeek',base_url:'https://api.deepseek.com',env_key:'DEEPSEEK_API_KEY',protocol:'chat_completions'},
 ollama:{id:'ollama_api',name:'Ollama API',base_url:'http://127.0.0.1:11434',env_key:'',protocol:'ollama'},
 lmstudio:{id:'lmstudio_api',name:'LM Studio API',base_url:'http://127.0.0.1:1234/v1',env_key:'',protocol:'chat_completions'}
};
$('add-provider').addEventListener('click',()=>{const current=readProviders(),preset={...providerPresets[$('provider-preset').value]},base=preset.id;let i=2;while(current.some(p=>p.id===preset.id))preset.id=base+'_'+i++;renderProviders([...current,preset]);});
$('project-select').addEventListener('change',()=>{projectId=$('project-select').value;localStorage.setItem('rsi-project',projectId);settingsBaseline=null;render();});
for(const id of ['add-project','onboard-add'])$(id).addEventListener('click',()=>$('project-dialog').showModal());
document.querySelectorAll('.close-dialog').forEach(button=>button.addEventListener('click',()=>button.closest('dialog').close()));
$('project-source').addEventListener('change',()=>{const remote=$('project-source').value==='github';$('local-project-fields').hidden=remote;$('github-project-fields').hidden=!remote;$('project-path').required=!remote;$('project-urls').required=remote;});
$('project-form').addEventListener('submit',event=>{event.preventDefault();safe(async()=>{
 $('project-submit').disabled=true;
 try{
  if($('project-source').value==='github'){
   const urls=$('project-urls').value.split(/\s+/).filter(Boolean);if(!urls.length||urls.length>10)throw Error(t('一次请输入 1 至 10 个仓库链接'));
   const failed=[];
   for(let i=0;i<urls.length;i++){
    $('import-status').textContent=t('正在导入')+' '+(i+1)+' / '+urls.length;
    try{const added=await api('projects/import',{url:urls[i],directory:$('project-directory').value||null});projectId=added.id;}catch(error){failed.push(urls[i]+': '+error.message);}
   }
   settingsBaseline=null;await refresh();
   if(failed.length){$('import-status').textContent=failed.join('\n');return;}
  }else{const added=await api('projects',{path:$('project-path').value,name:$('project-name').value||null,enabled:$('project-enabled').checked});projectId=added.id;settingsBaseline=null;await refresh();}
  $('project-dialog').close();$('project-form').reset();$('project-source').dispatchEvent(new Event('change'));$('import-status').textContent='';toast(t('项目已接入'));
 }finally{$('project-submit').disabled=false;}
});});
$('pause-button').addEventListener('click',()=>safe(async()=>{const project=currentProject();await api('pause',{project:project.id,paused:!project.paused});await refresh();toast(project.paused?t('项目已启用'):t('已暂停，正在中断当前工作'));}));
$('goal-form').addEventListener('submit',event=>{event.preventDefault();safe(async()=>{if(!projectId)throw Error(t('请先接入项目'));await api('goal',{project:projectId,text:$('goal-text').value});$('goal-text').value='';await refresh();toast(currentProject().paused?t('目标已入队；启用项目后执行'):t('目标已加入任务池'));});});
document.querySelectorAll('[data-inspect]').forEach(button=>button.addEventListener('click',()=>safe(async()=>{await api('request',{project:projectId,kind:button.dataset.inspect});await refresh();toast(currentProject().paused?t('已加入队列；启用项目后执行'):t('已加入队列'));})));
$('task-filter').addEventListener('change',renderTasks);$('config-scope').addEventListener('change',renderSettings);
$('settings-form').addEventListener('submit',event=>{event.preventDefault();safe(async()=>{if($('config-scope').value==='project'&&!projectId)throw Error(t('请先接入项目，或切换到全局默认'));let patch={};document.querySelectorAll('[data-path]').forEach(node=>{if(node.disabled)return;const value=node.type==='checkbox'?node.checked:node.type==='number'?Number(node.value):node.value;if(JSON.stringify(value)!==JSON.stringify(getPath(settingsBaseline,node.dataset.path)))setPath(patch,node.dataset.path,value);});
 const hours=[Number($('work-start').value),Number($('work-end').value)];if(JSON.stringify(hours)!==JSON.stringify(settingsBaseline.schedule.working_hours))setPath(patch,'schedule.working_hours',hours);
 const labels=$('issue-labels').value.split(',').map(x=>x.trim()).filter(Boolean);if(JSON.stringify(labels)!==JSON.stringify(settingsBaseline.github.labels))setPath(patch,'github.labels',labels);
 const providers=readProviders();if(JSON.stringify(providers)!==JSON.stringify(settingsBaseline.providers))patch.providers=providers;
 const checks=JSON.parse($('checks-json').value);if(JSON.stringify(checks)!==JSON.stringify(settingsBaseline.checks))patch.checks=checks;
 patch=deepMerge(patch,JSON.parse($('advanced-json').value||'{}'));await api('config',{project:$('config-scope').value==='project'?projectId:null,values:patch});settingsBaseline=null;await refresh();toast(t('已保存，从下一轮生效'));});});
$('case-form').addEventListener('submit',event=>{event.preventDefault();safe(async()=>{if(!projectId)throw Error(t('请先接入项目'));await api('cases',{project:projectId,manifest:JSON.parse($('case-json').value)});$('case-form').reset();await refresh();toast(t('评估任务已导入'));});});
const updateNames={unchecked:'尚未检查更新',up_to_date:'已是最新稳定版',no_release:'尚无可用的正式发布包',available:'发现可用更新',waiting_idle:'等待任务结束后更新',downloading:'正在下载并校验',restarting:'正在重启服务',installed:'更新已完成',rolled_back:'更新失败，已恢复旧版',error:'更新检查或安装失败'};
function renderUpdate(){if(!state)return;const update=state.update||{state:'unchecked'};$('update-status').textContent=t(updateNames[update.state]||update.state)+(update.version?' · '+update.version:'')+(update.reason?' · '+update.reason:'');$('apply-update').disabled=!['available','waiting_idle'].includes(update.state);$('check-update').disabled=['downloading','restarting'].includes(update.state);}
$('check-update').addEventListener('click',()=>safe(async()=>{await api('updates/check',{});toast(t('已开始检查更新'));}));
$('apply-update').addEventListener('click',()=>safe(async()=>{if(!confirm(t('将安装正式发布包，并在任务空闲时重启服务。确认更新？')))return;await api('updates/apply',{confirm:true});toast(t('已申请更新，等待空闲和重启'));await refresh();}));
safe(refresh);setInterval(()=>refresh().catch(error=>{$('connection').textContent=t('连接中断');}),5000);
$('language-select').addEventListener('change',()=>safe(async()=>{
 language=$('language-select').value;
 localStorage.setItem('rsi-language',language);
 translateStatic();
 $('connection').textContent=t(['连接中断',english['连接中断']].includes($('connection').textContent)?'连接中断':'已连接');
 if(!projectId&&$('project-select').options.length)$('project-select').options[0].textContent=t('尚未接入项目');
 $('toast').hidden=true;
 if(!state)return;
 const form=$('settings-form');
 const values=[...form.querySelectorAll('input,textarea,select')].map(node=>({id:node.id,path:node.dataset.path,value:node.value,checked:node.checked}));
 const baseline=settingsBaseline;
 const providers=readProviders();
 showView(view);
 if(view==='settings'){
  settingsBaseline=baseline;
  renderProviders(providers);
  for(const saved of values){
   const node=saved.id?$(saved.id):saved.path?form.querySelector('[data-path="'+saved.path+'"]'):null;
   if(node){node.value=saved.value;node.checked=saved.checked;}
  }
 }
 if($('detail-dialog').open&&detailTask)await showTask(detailTask);
}));
