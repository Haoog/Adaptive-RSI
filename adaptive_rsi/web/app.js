'use strict';
let state, projectId = localStorage.getItem('rsi-project') || '', view = 'overview', settingsBaseline, toastTimer;
const $ = id => document.getElementById(id);
const evaluationNames={baseline:'原始基线',candidate:'冻结回归',candidate_full:'候选完整测试',acceptance_baseline:'基线独立验收',acceptance_candidate:'候选独立验收'};
const deliveryNames={waiting:'等待可合并状态',waiting_review:'等待必需评审',waiting_ci:'等待 CI',ci_failed:'CI 失败',stale:'基线或候选已变化',merged:'已合并',merge_submitted:'已提交合并',closed:'已关闭',unverified_merge:'外部合并包含未经验证的改动',ready:'可合并'};
const statusNames = {unverified_merge:'未经验证的外部合并',reverted:'已回退',merged:'已合并',pr_closed:'PR 已关闭',verified:'合并后验证通过',regressed:'合并后出现回归',deferred:'已暂缓',rejected:'不采纳',queued:'排队中',planning:'规划中',executing:'实现中',evaluating:'评估中',awaiting_approval:'待交付',pr_open:'PR 已创建',completed:'已完成',obsolete:'已过时',blocked:'已阻塞',failed:'未通过',interrupted:'已中断',cancelled:'已取消',insufficient_evidence:'证据不足',stable:'稳定使用',superseded:'历史稳定版',ready:'等待晋升',pass:'通过',fail:'未通过',rolled_back:'已回退'};
const sourceNames = {goal:'用户目标',inspection:'自主发现',issue:'GitHub Issue',system_light:'轻量巡检',system_deep:'深度巡检',system_issues:'Issues 同步',system_evolve:'策略改进',system_delivery:'交付跟踪与复查',system_reassess:'暂缓任务复核',system_publish:'批准交付'};
const reviewNames = {correctness:'正确性',security:'安全性',maintainability:'可维护性',architecture:'架构',tests:'测试',scope:'改动范围',critical:'致命',high:'高',medium:'中',low:'低'};
const roleNames = {analysis:'项目理解 / 巡检',planning:'规划',implementation:'代码实现 / 修复',review:'独立审查',evolution:'策略改进'};
const formRevisions=new WeakMap(), settingsDrafts=new Map();
let settingsContext='';
function reviseForm(form){if(form)formRevisions.set(form,(formRevisions.get(form)||0)+1);}
for(const type of ['input','change','reset'])document.addEventListener(type,event=>reviseForm(event.target.closest('form')),true);
document.addEventListener('close',event=>{if(event.target.tagName==='DIALOG')reviseForm(event.target.querySelector('form'));},true);
function unchangedForm(form){const revision=formRevisions.get(form)||0,project=projectId;return ()=>(formRevisions.get(form)||0)===revision&&projectId===project;}
function el(tag, text, cls){ const node=document.createElement(tag); if(text!==undefined)node.textContent=text; if(cls)node.className=cls; return node; }
function toast(message,error=false){ $('toast').textContent=message; $('toast').className=error?'error':''; $('toast').hidden=false; clearTimeout(toastTimer); toastTimer=setTimeout(()=>$('toast').hidden=true,6500); }
async function sendApi(path,body){
 const options=body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json','X-RSI-CSRF':state.csrf},body:JSON.stringify(body)};
 const response=await fetch('/api/'+path,options),data=await response.json();
 if(!response.ok){const error=Error(data.error||response.status);error.code=data.error_code||data.catalog_error;throw error;}
 return data;
}
async function api(path,body){
 const payload=body===undefined?undefined:structuredClone(body);
 try{return await sendApi(path,payload);}catch(error){if(payload===undefined||error.code!=='update_in_progress')throw error;}
 toast(t('服务正在完成更新，表单已保留，恢复后自动提交。'));
 const deadline=Date.now()+120000;
 while(Date.now()<deadline){
  await new Promise(resolve=>setTimeout(resolve,1000));
  let next;
  try{next=await sendApi('state');}catch(error){if(error instanceof TypeError)continue;throw error;}
  if(next.update?.busy??['downloading','restarting'].includes(next.update?.state))continue;
  state=next;
  // 只重试服务明确拒绝执行的请求；提交后断线时不重复操作。
  try{return await sendApi(path,payload);}catch(error){if(error.code!=='update_in_progress')throw error;}
 }
 throw Error(t('更新尚未完成，已保留输入；请稍后再提交。'));
}
function currentProject(){return state.projects.find(p=>p.id===projectId);}
function scoped(rows){return rows.filter(row=>row.project===projectId);}
function stamp(seconds){return new Date(seconds*1000).toLocaleString(language,{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'});}
function badge(status){return el('span',t(statusNames[status]||status),'badge '+(['stable','completed','pr_open','merged','verified','pass','awaiting_approval'].includes(status)?'good':['failed','fail','blocked','rolled_back'].includes(status)?'bad':['queued','planning','executing','evaluating'].includes(status)?'warn':'neutral'));}
function action(text,callback,cls='button'){const node=el('button',text,cls);node.type='button';node.addEventListener('click',()=>safe(callback));return node;}
async function safe(fn){try{await fn();}catch(error){toast(error.message,true);}}
async function refresh(){const next=await api('state');if(state&&state.version!==next.version)toast(t('后台已更新，当前输入已保留。刷新页面可加载新版界面。'));state=next; $('connection').textContent=t('已连接'); $('version').textContent='Adaptive RSI '+state.version;
 const select=$('project-select'); const old=projectId; if(!state.projects.some(p=>p.id===projectId))projectId=state.projects[0]?.id||'';
 if(JSON.stringify([...select.options].map(x=>[x.value,x.text]))!==JSON.stringify(state.projects.map(p=>[p.id,p.name]))){select.replaceChildren(...state.projects.map(p=>{const o=el('option',p.name);o.value=p.id;return o;})); if(!state.projects.length){const o=el('option',t('尚未接入项目'));o.value='';select.append(o);}}
 select.value=projectId; if(old!==projectId){stashSettings();closeTaskDetail();settingsBaseline=null;localStorage.setItem('rsi-project',projectId);}
 render();}
function parentView(name){return name==='models'?'settings':name==='evolution'?'tasks':name;}
function renderNavigation(){
 const parent=parentView(view),configScope=$('config-scope').value;
 document.querySelectorAll('.nav').forEach(node=>node.classList.toggle('active',node.dataset.view===parent));
 document.querySelectorAll('.section-tabs [data-view]').forEach(node=>{node.classList.toggle('active',node.dataset.view===view);node.setAttribute('aria-current',node.dataset.view===view?'page':'false');});
 $('settings-tabs').hidden=parent!=='settings';$('tasks-tabs').hidden=parent!=='tasks';$('settings-context').hidden=parent!=='settings';
 $('page-name').textContent=t({overview:'工作台',tasks:'任务',settings:'设置'}[parent]);
 $('settings-context-title').textContent=configScope==='global'?t('通用默认设置'):(currentProject()?.name||t('当前项目'))+' · '+t('项目设置');
 $('settings-context-note').textContent=t(configScope==='global'?'未单独设置的项目会沿用这里的配置。':'仅修改当前项目，其他项目不受影响。');
 document.querySelectorAll('[data-settings-scope]').forEach(node=>{node.classList.toggle('active',node.dataset.settingsScope===configScope);node.setAttribute('aria-pressed',String(node.dataset.settingsScope===configScope));node.disabled=node.dataset.settingsScope==='project'&&!projectId;});
}
const activeStates=['queued','planning','executing','evaluating','pr_open'];
const attentionStates=['awaiting_approval','blocked','failed','insufficient_evidence','interrupted','regressed','unverified_merge'];
function visibleTasks(rows,filter,includeSystem=false){return rows.filter(task=>(includeSystem||!task.source.startsWith('system_'))&&(filter==='all'||(filter==='active'?activeStates.includes(task.state):filter==='attention'?attentionStates.includes(task.state):!activeStates.includes(task.state)&&!attentionStates.includes(task.state))));}
function eventCopy(event){
 const known={'Project registered (paused)':'项目已接入，等待启动','Project registered and enabled':'项目已接入并启动','Settings saved; active runs retain their snapshot':'设置已保存','Model routes updated':'模型分工已保存','Project paused':'项目已暂停','Project enabled':'项目已启动','Cancellation requested':'已申请取消任务','Dependency snapshot frozen':'已准备独立工作环境','Committed snapshot unchanged; no model call':'项目没有新改动，本次检查未调用模型','Fresh run scheduled with bounded backoff':'已安排重试','Provider resumed by user':'模型连接已恢复'};
 if(known[event.message])return t(known[event.message]);
 const issues=event.message.match(/^(\d+) Issue updates synchronized$/);if(issues)return t('已同步 Issue 更新')+' · '+issues[1];
 return event.message;
}
function render(){
 const project=currentProject(),tasks=scoped(state.tasks),runs=scoped(state.runs),config=project?.config||state.config;
 if(!project&&$('config-scope').value==='project'){$('config-scope').value='global';settingsBaseline=null;}
 renderNavigation();$('onboarding').hidden=!!project;$('project-workspace').hidden=!project;$('pause-button').hidden=!project;
 $('pause-button').disabled=!project;$('pause-button').textContent=t(!project||project.paused?'开始运行':'暂停运行');
 $('workspace-title').textContent=project?.name||t('从一个项目开始');
 $('workspace-description').textContent=t(project?'自主发现，也随时接受你的目标。':'连接项目与模型，让它持续发现值得做的改进。');
 $('project-status').textContent=t(!project||project.paused?'已暂停':'运行中');$('project-status').className='badge '+(project&&!project.paused?'good':'neutral');
 const running=runs.find(run=>!run.ended),queue=tasks.filter(task=>task.state==='queued').length;
 $('queue-count').textContent=queue;$('queue-count').hidden=!queue;
 $('current-work').textContent=running?(tasks.find(task=>task.id===running.task)?.title||t(statusNames[running.state]||'执行中')):t(project?.paused?'由你启动后开始工作':queue?'准备处理任务':'等待下一次检查');
 const usage=state.usage.find(u=>u.project===(projectId||'model-center'));$('usage-brief').textContent=(usage?.calls||0)+' '+t('次调用');$('runtime-evidence').hidden=!project&&!usage;
 const next=state.schedules.filter(row=>row.project===projectId&&((row.kind==='issues'&&config.sources.issues&&config.github.repository)||(['light','deep'].includes(row.kind)&&config.sources.inspection))).sort((a,b)=>a.due-b.due)[0];
 $('next-check').textContent=t(project?.paused?'启动后开始':!next?'未安排自动巡检':'下次 ')+(project&&!project.paused&&next?stamp(next.due):'');
 const attention=visibleTasks(tasks,'attention',true);$('attention-banner').hidden=!attention.length;$('attention-text').textContent=t('需要处理的任务')+' · '+attention.length;
 const recent=$('recent-tasks');recent.replaceChildren();const latest=visibleTasks(tasks,'all').slice(0,3);
 if(!latest.length)recent.append(el('p',t('还没有任务。可以输入目标，或启动自动巡检。'),'empty-copy'));
 for(const task of latest){const row=el('div',undefined,'recent-task');row.append(action(task.title,()=>showTask(task),'task-title'),badge(task.state));recent.append(row);}
 for(const kind of ['light','issues','deep']){
  const mins=config.schedule[kind+'_minutes'],factor=mins%1440===0?1440:mins%60===0?60:1;
  $(kind+'-interval').textContent=(mins/factor)+' '+t(factor===1440?'天':factor===60?'小时':'分钟');
  const enabled=kind==='issues'?config.sources.issues&&config.github.repository:config.sources.inspection;
  const schedule=state.schedules.find(row=>row.project===projectId&&row.kind===kind);
  $(kind+'-due').textContent=t(!enabled?'未启用':project?.paused?'项目已暂停':schedule?'下次 ':'等待首次检查')+(enabled&&!project?.paused&&schedule?stamp(schedule.due):'');
 }
 document.querySelectorAll('[data-inspect]').forEach(button=>button.disabled=!project);
 const activity=$('activity');activity.replaceChildren();const events=state.events.filter(e=>!e.project||e.project===projectId).slice(0,6);
 if(!events.length)activity.append(el('p',t('运行记录将在这里出现。'),'empty-copy'));
 for(const event of events){const row=el('div',undefined,'activity-row');row.append(el('time',stamp(event.created)),el('span',eventCopy(event)));if(language!=='en'&&eventCopy(event)!==event.message){const raw=el('details');raw.append(el('summary',t('原文')),el('p',event.message));row.append(raw);}activity.append(row);}
 $('delivery-summary').textContent=t(config.delivery.mode==='local'?'当前交付：保存在本地，确认后创建 PR。':config.delivery.auto_merge?'当前交付：检查通过后自动创建 PR 并合并。':'当前交付：检查通过后自动创建 PR。');
 renderTelemetry();renderUpdate();if(view==='tasks')renderTasks();if(view==='evolution')renderEvolution();if(view==='settings'&&!settingsBaseline)renderSettings();
}
function renderTelemetry(){
 const summary=$('quality-summary');summary.replaceChildren();const quality=state.quality?.find(q=>q.project===projectId);
 const rate=(n,d)=>d?Math.round(n/d*100)+'% ('+n+'/'+d+')':t('暂无样本');
 for(const [label,value]of [['验收与评审通过率',rate(quality?.passed||0,quality?.evaluated||0)],['评审阻断率',rate(quality?.review_blocked||0,quality?.reviewed||0)],['重复失败任务',quality?.repeat_failures||0],['合并后验证通过',quality?.postmerge_verified||0],['合并后出现回归',quality?.postmerge_regressed||0]]){const card=el('div');card.append(el('small',t(label)),el('strong',String(value)));summary.append(card);}
 const usage=$('usage-summary');usage.replaceChildren();const usageScope=projectId||'model-center',rows=(state.model_usage||[]).filter(u=>u.project===usageScope);
 const total=(state.usage||[]).find(u=>u.project===usageScope);usage.append(el('p',t('近 24 小时')+' · '+(total?.calls||0)+' '+t('次调用')+' · '+Math.round(total?.seconds||0)+'s · '+(total?.tokens||0)+' tokens'));
 const estimate=total?.estimate==null?t('未知'):('$'+total.estimate.toFixed(4));usage.append(el('p',t('估算费用')+' · '+estimate+' USD · '+(total?.unpriced||0)+' '+t('次调用缺少价格'),'muted'));
 usage.append(el('p',t('费用由用户单价估算，不代表账单或金额硬上限。调用按 Codex 会话或原生 HTTP 请求统计。'),'muted'));
 if(rows.length){const table=el('table'),head=el('tr');for(const key of ['供应商 / 模型','调用','失败','耗时','估算费用'])head.append(el('th',t(key)));const thead=el('thead');thead.append(head);table.append(thead);const body=el('tbody');for(const row of rows){const tr=el('tr');for(const value of [row.provider+' / '+row.model,row.calls,row.failed,Math.round(row.seconds)+'s',row.estimate==null?t('未知'):'$'+row.estimate.toFixed(4)+(row.unpriced?' *':'')])tr.append(el('td',String(value)));body.append(tr);}table.append(body);const wrap=el('div',undefined,'usage-table');wrap.append(table);usage.append(wrap);}
}
function valueReport(value,reason,scope){
 const report=el('div',undefined,'value-report');if(reason)report.append(el('p',reason));
 if(value){for(const [key,label]of [['benefit','预期收益'],['cost','预计成本'],['inaction','不做的影响']])if(value[key]){const row=el('p');row.append(el('strong',t(label)+' · '),document.createTextNode(value[key]));report.append(row);}
 const levels=el('p',t('影响')+' '+t(reviewNames[value.impact]||value.impact)+' · '+t('工作量')+' '+t(reviewNames[value.effort]||value.effort)+' · '+t('信心')+' '+t(reviewNames[value.confidence]||value.confidence),'muted');report.append(levels);
 if(value.evidence?.length){const details=el('details'),list=el('ul');details.append(el('summary',t('判断证据')));for(const item of value.evidence)list.append(el('li',item));details.append(list);report.append(details);}}
 if(scope?.length)report.append(el('p',t('计划范围')+' · '+scope.join(', '),'muted'));return report;
}
function showView(name){
 if(!['overview','tasks','evolution','settings','models'].includes(name))return;
 view=name;document.querySelectorAll('.view').forEach(node=>node.hidden=node.id!=='view-'+name);
 if(name==='models')window.RSIModelCenter?.show($('config-scope').value==='project'?projectId:null);
 render();window.scrollTo(0,0);
}
function renderTasks(){
 const list=$('tasks-list'),filter=$('task-filter').value;list.replaceChildren();
 document.querySelectorAll('[data-task-filter]').forEach(button=>{button.classList.toggle('active',button.dataset.taskFilter===filter);button.setAttribute('aria-pressed',String(button.dataset.taskFilter===filter));});
 const tasks=visibleTasks(scoped(state.tasks),filter,$('show-system-tasks').checked);
 if(!tasks.length)list.append(el('p',t('这里暂无任务。其他分组中可能有历史记录。'),'empty-copy'));
 for(const task of tasks){
  const row=el('div',undefined,'task-row'),title=el('div'),actions=el('div',undefined,'task-actions');
  title.append(action(task.title,()=>showTask(task),'task-title'),el('span',t(sourceNames[task.source]||task.source)+' · '+stamp(task.updated),'task-meta'));
  const latest=scoped(state.runs).find(run=>run.task===task.id),reason=latest?.plan_reason||latest?.reason;
  if(reason)title.append(el('p',reason,'task-reason'));
  if(['blocked','failed','interrupted','cancelled','insufficient_evidence','deferred','rejected'].includes(task.state))actions.append(action(t('重试'),async()=>{await api('retry',{task:task.id});await refresh();},'text-button'));
  actions.append(action(t('查看详情'),()=>showTask(task),'text-button'));
  row.append(title,badge(task.state),actions);list.append(row);
 }
}
let detailTask,detailSequence=0;
function closeTaskDetail(){detailSequence++;detailTask=null;if($('detail-dialog').open)$('detail-dialog').close();}
async function showTask(task){const ticket=++detailSequence,project=projectId;detailTask=task;$('detail-title').textContent=task.title;const body=$('detail-body');body.replaceChildren(badge(task.state));const description=el('p',task.body,'muted');body.append(description);
 const evidence=el('details',undefined,'detail-section');evidence.append(el('summary',t('来源与证据')),el('pre',JSON.stringify(JSON.parse(task.evidence),null,2)));body.append(evidence);
 for(const run of scoped(state.runs).filter(r=>r.task===task.id)){const data=await api('run?id='+run.id);if(ticket!==detailSequence||projectId!==project)return;const section=el('section',undefined,'detail-section');section.append(el('h3',t('运行 ')+run.id+' · '+(t(statusNames[run.state]||run.state))),el('p',run.reason,'muted'));
  const raw=el('details');raw.append(el('summary',t('原始运行记录')),el('pre',JSON.stringify({models:data.config.models,calls:data.calls,tokens:data.tokens,base:data.base,result:data.result},null,2)));section.append(raw);
  if(data.result.value){const report=el('div',undefined,'review-report');report.append(el('h3',t('任务价值评估')),valueReport(data.result.value,data.result.plan_reason,data.result.scope));section.append(report);}
  if(data.result.review){
   const review=data.result.review,report=el('div',undefined,'review-report');report.append(el('h3',t('代码评审')),badge(review.decision),el('p',review.reason));
   for(const assessment of review.assessments||[]){const row=el('p');row.append(badge(assessment.decision),el('strong',' '+t(reviewNames[assessment.area]||assessment.area)+' · '),document.createTextNode(assessment.reason));report.append(row);}
   for(const finding of review.findings||[]){const card=el('div',undefined,'provider-card');card.append(el('strong',t(reviewNames[finding.severity]||finding.severity)+' · '+finding.title),el('p',finding.path+':'+finding.line),el('p',finding.reason),el('p',t('修改建议')+'：'+finding.suggestion));report.append(card);}
   section.append(report);
  }
  if(data.result.evaluation){const report=el('div',undefined,'review-report');report.append(el('h3',t('独立验收')),badge(data.result.evaluation.decision));for(const [label,rows] of Object.entries(data.result.evaluation.checks||{})){if(!Array.isArray(rows))continue;const detail=el('details');detail.append(el('summary',t(evaluationNames[label]||label)+' · '+rows.filter(r=>r.passed).length+'/'+rows.length),el('pre',JSON.stringify(rows,null,2)));report.append(detail);}section.append(report);}
  if(data.result.delivery_status)section.append(el('p',t('交付状态')+' · '+t(deliveryNames[data.result.delivery_status]||data.result.delivery_status)));
  if(data.result.feedback){section.append(el('h3',t('合并后收益反馈')),badge(data.result.feedback.state),el('p',stamp(data.result.feedback.checked_at)+' · '+data.result.feedback.head));}
  if(data.result.files){const link=el('a',t('查看候选补丁 ↗'));link.href='/api/patch?id='+run.id;link.target='_blank';link.rel='noreferrer';section.append(link);}
  if(run.state==='awaiting_approval')section.append(action(t('批准推送并创建 PR'),async()=>{if(!window.confirm(t('这会把候选分支推送到关联 GitHub 仓库，并创建 PR。确认批准？')))return;await api('deliver',{run:run.id});toast(currentProject()?.paused?t('交付任务已加入；启用项目后执行'):t('交付任务已加入'));$('detail-dialog').close();await refresh();}));
  if(run.pr){const link=el('a',t('打开 GitHub PR ↗'));if(/^https:\/\/github\.com\//.test(run.pr)){link.href=run.pr;link.target='_blank';link.rel='noreferrer';section.append(link);}}
  if(['pr_open','merged'].includes(run.state))section.append(action(t('生成这次改动的回退候选'),async()=>{await api('revert-code',{run:run.id});toast(t('回退任务已加入；通过检查后才可交付'));$('detail-dialog').close();await refresh();}));
  body.append(section);}
 if(ticket!==detailSequence||projectId!==project)return;
 if(['queued','planning','executing','evaluating'].includes(task.state))body.append(action(t('取消任务'),async()=>{if(ticket!==detailSequence||projectId!==project)return;await api('cancel',{task:task.id});$('detail-dialog').close();await refresh();}));
 if(!$('detail-dialog').open)$('detail-dialog').showModal();}
function renderEvolution(){const summary=$('suite-summary');summary.replaceChildren();for(const [split,title,description]of [['development',t('开发集'),t('用于提出和调整候选')],['validation',t('验证集'),t('配对比较 · 受限复用')],['holdout',t('最终评估集'),t('隐藏任务 · 限制曝光次数')]]){const count=scoped(state.cases).find(c=>c.split===split)?.count||0,card=el('article',undefined,'suite-card');card.append(el('strong',count),el('span',title),el('small',description));summary.append(card);}
 const list=$('candidates');list.replaceChildren();const candidates=scoped(state.candidates);if(!candidates.length)list.append(el('p',t('还没有策略候选。运行积累证据后会自动提出，也可以手动发起。'),'empty-copy'));
 for(const c of candidates){const card=el('article',undefined,'panel candidate-card'),top=el('div',undefined,'panel-title');top.append(el('h2',{strategy:t('执行策略'),meta_strategy:t('元策略'),adapter:t('适配指导')}[c.kind]||c.kind),badge(c.status));card.append(top,el('div',(c.parent?c.parent.slice(0,8):'BASELINE')+' → '+c.id.slice(0,8),'lineage'),el('p',c.content.guidance),el('p',c.reason,'muted'),el('small',c.trial_count+t(' 组配对任务 · ')+stamp(c.created),'muted'));
  if(c.status==='stable')card.append(action(t('回退至上一稳定版'),async()=>{await api('rollback',{candidate:c.id});toast(t('策略已回退'));await refresh();}));list.append(card);}}
function getPath(obj,path){return path.split('.').reduce((v,k)=>v?.[k],obj);}
function setPath(obj,path,value){const keys=path.split('.');let node=obj;keys.slice(0,-1).forEach(k=>node=node[k]??={});node[keys.at(-1)]=value;}
function deepMerge(a,b){for(const [key,value]of Object.entries(b)){if(value&&typeof value==='object'&&!Array.isArray(value))a[key]=deepMerge(a[key]||{},value);else a[key]=value;}return a;}
function settingsKey(){return $('config-scope').value==='global'?'global':projectId;}
function stashSettings(){
 if(!settingsBaseline||!settingsContext)return;
 const values=[...$('settings-form').querySelectorAll('input,textarea,select')].map(node=>({id:node.id,path:node.dataset.path,value:node.value,checked:node.checked}));
 settingsDrafts.set(settingsContext,{baseline:settingsBaseline,values});
}
function restoreSettingsDraft(){
 const draft=settingsDrafts.get(settingsKey());if(!draft)return;
 settingsBaseline=draft.baseline;
 for(const saved of draft.values){const node=saved.id?$(saved.id):$('settings-form').querySelector('[data-path="'+saved.path+'"]');if(node){node.value=saved.value;node.checked=saved.checked;}}
 document.querySelectorAll('[data-unit-for]').forEach(unit=>{unit.dataset.previous=unit.value;const node=document.querySelector('[data-path="'+unit.dataset.unitFor+'"]');node.min=5/Number(unit.value);node.max=525600/Number(unit.value);});
}
function durationUnit(node){return node.dataset.duration?Number(document.querySelector('[data-unit-for="'+node.dataset.duration+'"]').value):1;}
function fieldValue(node){
 let value=node.type==='checkbox'?node.checked:node.type==='number'?Number(node.value)*durationUnit(node):node.value;
 if(node.dataset.duration&&Math.abs(value-Math.round(value))<1e-7)value=Math.round(value);
 if(node.dataset.duration&&(!Number.isInteger(value)||value<5||value>525600))throw Error(t('巡检间隔须为 5 分钟至 365 天，且精确到整分钟。'));
 return value;
}
function syncDelivery(){
 const mode=document.querySelector('[data-path="delivery.mode"]').value,auto=document.querySelector('[data-path="delivery.auto_merge"]').checked;
 $('delivery-choice').value=mode==='pr'?(auto?'auto':'pr'):auto?'custom':'local';
 $('delivery-choice').disabled=$('config-scope').value!=='project'||!projectId;
 $('delivery-note').textContent=t($('delivery-choice').disabled?'自动交付需在具体项目中设置。':'创建 PR 和合并前，都会先通过价值判断、测试及独立评审。');
}
function changeSettingsScope(scope){
 stashSettings();$('config-scope').value=scope;settingsBaseline=null;
 if(view==='models')window.RSIModelCenter?.show(scope==='project'?projectId:null);
 else renderSettings();renderNavigation();
}
function renderSettings(){settingsBaseline=structuredClone($('config-scope').value==='global'?state.config:(currentProject()?.config||state.config));
 document.querySelectorAll('[data-path]').forEach(node=>{const value=getPath(settingsBaseline,node.dataset.path);if(node.type==='checkbox')node.checked=value;else node.value=value;if(node.dataset.duration){const factor=value%1440===0?1440:value%60===0?60:1;document.querySelector('[data-unit-for="'+node.dataset.duration+'"]').value=String(factor);document.querySelector('[data-unit-for="'+node.dataset.duration+'"]').dataset.previous=String(factor);node.value=value/factor;node.min=5/factor;node.max=525600/factor;}});
 document.querySelector('[data-path="limits.concurrency"]').disabled=$('config-scope').value!=='global';
 document.querySelectorAll('[data-path^="delivery."]').forEach(node=>node.disabled=$('config-scope').value!=='project'||!projectId);
 document.querySelectorAll('[data-path^="updates."]').forEach(node=>node.disabled=$('config-scope').value!=='global');
 renderUpdate();
 $('work-start').value=settingsBaseline.schedule.working_hours[0];$('work-end').value=settingsBaseline.schedule.working_hours[1];$('issue-labels').value=settingsBaseline.github.labels.join(', ');$('checks-json').value=JSON.stringify(settingsBaseline.checks,null,2);$('advanced-json').value='';settingsContext=settingsKey();restoreSettingsDraft();syncDelivery();
 safe(async()=>{const target=$('capabilities');target.replaceChildren();if(!projectId){target.append(el('p',t('接入项目后识别工具链。'),'muted'));return;}const capabilities=await api('capabilities?project='+projectId);if(!capabilities.modules.length)target.append(el('p',t('未识别到标准构建清单。可手动配置验收命令。'),'muted'));for(const module of capabilities.modules){const row=el('div',undefined,'capability-row');row.append(el('strong',module.language),el('code',module.manifest),el('span',{executable:t('检查可执行 · 已验证'),unverified:t('待隔离验证'),analysis_only:t('可分析 · 需配置检查'),environment_blocked:t('环境缺少工具')}[module.status],'badge '+(module.status==='environment_blocked'?'warn':module.status==='executable'?'good':'neutral')));target.append(row);}});}
document.querySelectorAll('[data-view]').forEach(node=>node.addEventListener('click',()=>showView(node.dataset.view)));
$('detail-dialog').addEventListener('close',()=>{detailSequence++;detailTask=null;});
document.querySelectorAll('[data-settings-scope]').forEach(button=>button.addEventListener('click',()=>changeSettingsScope(button.dataset.settingsScope)));
document.querySelectorAll('[data-task-filter]').forEach(button=>button.addEventListener('click',()=>{$('task-filter').value=button.dataset.taskFilter;renderTasks();}));
$('show-system-tasks').addEventListener('change',renderTasks);
$('attention-open').addEventListener('click',()=>{$('show-system-tasks').checked=true;$('task-filter').value='attention';showView('tasks');});
document.querySelectorAll('[data-unit-for]').forEach(unit=>unit.addEventListener('change',()=>{const node=document.querySelector('[data-path="'+unit.dataset.unitFor+'"]');const previous=Number(unit.dataset.previous||durationUnit(node));node.value=Number(node.value)*previous/Number(unit.value);unit.dataset.previous=unit.value;node.min=5/Number(unit.value);node.max=525600/Number(unit.value);}));
$('delivery-choice').addEventListener('change',()=>{if($('delivery-choice').value==='custom')return;document.querySelector('[data-path="delivery.mode"]').value=$('delivery-choice').value==='local'?'local':'pr';document.querySelector('[data-path="delivery.auto_merge"]').checked=$('delivery-choice').value==='auto';});
$('project-select').addEventListener('change',()=>{stashSettings();closeTaskDetail();projectId=$('project-select').value;localStorage.setItem('rsi-project',projectId);settingsBaseline=null;if(view==='models')window.RSIModelCenter?.show($('config-scope').value==='project'?projectId:null);render();});
for(const id of ['add-project','onboard-add'])$(id).addEventListener('click',()=>$('project-dialog').showModal());
document.querySelectorAll('.close-dialog').forEach(button=>button.addEventListener('click',()=>button.closest('dialog').close()));
$('project-source').addEventListener('change',()=>{const remote=$('project-source').value==='github';$('local-project-fields').hidden=remote;$('github-project-fields').hidden=!remote;$('project-path').required=!remote;$('project-urls').required=remote;});
$('project-form').addEventListener('submit',event=>{event.preventDefault();safe(async()=>{
 const unchanged=unchangedForm(event.target);
 $('project-submit').disabled=true;
 try{
  let addedProject;
  if($('project-source').value==='github'){
   const urls=$('project-urls').value.split(/\s+/).filter(Boolean),directory=$('project-directory').value||null;if(!urls.length||urls.length>10)throw Error(t('一次请输入 1 至 10 个仓库链接'));
   const failed=[];
   for(let i=0;i<urls.length;i++){
    $('import-status').textContent=t('正在导入')+' '+(i+1)+' / '+urls.length;
    try{const added=await api('projects/import',{url:urls[i],directory});addedProject=added.id;}catch(error){failed.push(urls[i]+': '+error.message);}
   }
   if(failed.length){if(unchanged())$('import-status').textContent=failed.join('\n');await refresh();return;}
  }else{const added=await api('projects',{path:$('project-path').value,name:$('project-name').value||null,enabled:$('project-enabled').checked});addedProject=added.id;}
  if(unchanged()){stashSettings();closeTaskDetail();projectId=addedProject;settingsBaseline=null;$('project-dialog').close();$('project-form').reset();$('project-source').dispatchEvent(new Event('change'));$('import-status').textContent='';}
  await refresh();toast(t('项目已接入'));
 }finally{$('project-submit').disabled=false;}
});});
$('pause-button').addEventListener('click',()=>safe(async()=>{const project=currentProject();await api('pause',{project:project.id,paused:!project.paused});await refresh();toast(project.paused?t('项目已启用'):t('已暂停，正在中断当前工作'));}));
$('goal-form').addEventListener('submit',event=>{event.preventDefault();safe(async()=>{if(!projectId)throw Error(t('请先接入项目'));const unchanged=unchangedForm(event.target),project=projectId;await api('goal',{project,text:$('goal-text').value});if(unchanged())$('goal-text').value='';await refresh();toast(state.projects.find(p=>p.id===project)?.paused?t('目标已入队；启用项目后执行'):t('目标已加入任务池'));});});
document.querySelectorAll('[data-inspect]').forEach(button=>button.addEventListener('click',()=>safe(async()=>{await api('request',{project:projectId,kind:button.dataset.inspect});await refresh();toast(currentProject().paused?t('已加入队列；启用项目后执行'):t('已加入队列'));})));
$('task-filter').addEventListener('change',renderTasks);$('config-scope').addEventListener('change',()=>changeSettingsScope($('config-scope').value));
$('settings-form').addEventListener('invalid',event=>{let detail=event.target.closest('details');while(detail){detail.open=true;detail=detail.parentElement.closest('details');}},true);
$('settings-form').addEventListener('submit',event=>{event.preventDefault();safe(async()=>{const button=$('settings-save');if(button.disabled)return;button.disabled=true;try{if($('config-scope').value==='project'&&!projectId)throw Error(t('请先接入项目，或切换到全局默认'));const unchanged=unchangedForm(event.target),baseline=settingsBaseline,scope=$('config-scope').value,project=projectId,key=settingsKey();let patch={};document.querySelectorAll('[data-path]').forEach(node=>{if(node.disabled)return;const value=fieldValue(node);if(JSON.stringify(value)!==JSON.stringify(getPath(settingsBaseline,node.dataset.path)))setPath(patch,node.dataset.path,value);});
 const hours=[Number($('work-start').value),Number($('work-end').value)];if(JSON.stringify(hours)!==JSON.stringify(settingsBaseline.schedule.working_hours))setPath(patch,'schedule.working_hours',hours);
 const labels=$('issue-labels').value.split(',').map(x=>x.trim()).filter(Boolean);if(JSON.stringify(labels)!==JSON.stringify(settingsBaseline.github.labels))setPath(patch,'github.labels',labels);
 const checks=JSON.parse($('checks-json').value);if(JSON.stringify(checks)!==JSON.stringify(settingsBaseline.checks))patch.checks=checks;
 patch=deepMerge(patch,JSON.parse($('advanced-json').value||'{}'));const saved=await api('config',{project:scope==='project'?project:null,values:patch}),acknowledged=saved.config||deepMerge(structuredClone(baseline),patch);
 const draft=settingsDrafts.get(key);if(draft?.baseline===baseline)draft.baseline=acknowledged;
 if(settingsBaseline===baseline&&projectId===project&&$('config-scope').value===scope)settingsBaseline=acknowledged;
 await refresh();if(settingsBaseline===acknowledged&&unchanged()&&$('config-scope').value===scope&&view==='settings'){settingsDrafts.delete(key);renderSettings();}
 toast(t(unchanged()?'已保存，无需重启；新任务使用新配置':'已保存提交的配置，后续修改仍未保存。'));}finally{button.disabled=false;}});});
$('case-form').addEventListener('submit',event=>{event.preventDefault();safe(async()=>{if(!projectId)throw Error(t('请先接入项目'));const unchanged=unchangedForm(event.target);await api('cases',{project:projectId,manifest:JSON.parse($('case-json').value)});if(unchanged())$('case-form').reset();await refresh();toast(t('评估任务已导入'));});});
const updateNames={unchecked:'尚未检查更新',up_to_date:'已是最新稳定版',no_release:'尚无可用的正式发布包',available:'发现可用更新',waiting_idle:'等待任务结束后更新',downloading:'正在下载并校验',restarting:'正在重启服务',installed:'更新已完成',rolled_back:'更新失败，已恢复旧版',error:'更新检查或安装失败'};
function renderUpdate(){if(!state)return;const update=state.update||{state:'unchecked'};$('update-status').textContent=t(updateNames[update.state]||update.state)+(update.version?' · '+update.version:'')+(update.reason?' · '+update.reason:'');$('apply-update').disabled=!['available','waiting_idle'].includes(update.state);$('check-update').disabled=update.busy??['downloading','restarting'].includes(update.state);}
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
 render();if(settingsBaseline)syncDelivery();
 window.RSIModelCenter?.translate();
 if($('detail-dialog').open&&detailTask)await showTask(detailTask);
}));
