'use strict';
function localRead(key){try{return localStorage.getItem(key);}catch{return null;}}
function localWrite(key,value){try{localStorage.setItem(key,value);}catch{}}
function localRemove(key){try{localStorage.removeItem(key);}catch{}}
let state, projectId = localRead('rsi-project') || '', view = 'overview', settingsBaseline, toastTimer;
const $ = id => document.getElementById(id);
const evaluationNames={baseline:'修改前的版本',candidate:'原有功能检查',candidate_full:'修改后的完整测试',acceptance_baseline:'修改前的独立检查',acceptance_candidate:'修改后的独立检查'};
const deliveryNames={waiting:'等待可合并状态',waiting_review:'等待必需评审',waiting_ci:'等待 CI',ci_failed:'线上检查未通过',stale:'基线或待验证改动已变化',issue_changed:'Issue 已变化，等待同步后重评',merged:'已合并',merge_submitted:'已提交合并',closed:'已关闭',unverified_merge:'外部合并包含未经验证的改动',ready:'可合并'};
const statusNames = {expired:'候选已过期',releasing:'正在发布',deploying:'正在部署',release_failed:'发布未完成',deploy_failed:'部署未完成',ci_failed:'线上检查未通过',waiting_budget:'等待模型服务恢复用量',assessed:'价值已评估',unverified_merge:'未经验证的外部合并',reverted:'已恢复旧版本',merged:'已合并',pr_closed:'PR 已关闭',verified:'合并后验证通过',regressed:'合并后出现回归',deferred:'已暂缓',rejected:'不采纳',queued:'排队中',planning:'规划中',executing:'实现中',evaluating:'评估中',awaiting_approval:'待交付',pr_open:'PR 已创建',completed:'已完成',obsolete:'已过时',blocked:'已阻塞',failed:'未通过',interrupted:'已中断',cancelled:'已取消',insufficient_evidence:'证据不足',stable:'稳定使用',superseded:'已被替代',ready:'等待启用',pass:'通过',fail:'未通过',rolled_back:'已恢复旧版本'};
const sourceNames = {goal:'用户目标',inspection:'自主发现',issue:'GitHub Issue',system_light:'轻量检查',system_deep:'深度检查',system_issues:'GitHub 问题同步',system_evolve:'策略改进',system_delivery:'交付跟踪与复查',system_reassess:'暂缓任务复核',system_publish:'批准交付'};
const reviewNames = {correctness:'正确性',security:'安全性',maintainability:'可维护性',architecture:'架构',tests:'测试',scope:'改动范围',critical:'致命',high:'高',medium:'中',low:'低'};
const roleNames = {analysis:'项目理解 / 检查',planning:'规划',implementation:'代码实现 / 修复',review:'独立审查',evolution:'策略改进'};
const formRevisions=new WeakMap(), settingsDrafts=new Map(), settingsClean=new Map(), settingsModes=new WeakMap(), settingsFieldRevisions=new Map(), goalDrafts=new Map(), goalDraftRevisions=new Map();
let navigationSequence=0;
let settingsContext='',settingsGeneration=0,goalDraftRestoredProject=null;
let configurationRepairBusy=false;
let refreshSequence=0,refreshPromise=null,refreshController=null,refreshProject=null,refreshKey=null,refreshQueued=false,refreshForce=false,refreshWaiters=[],refreshPollTimer=null,lastUpdateToast='';
let capabilitySequence=0,scopeChosen=false,scopeInitialized=false,taskOffset=0;
const readinessCache=new Map(),bootBuild=document.documentElement?.dataset?.build||'';
let readinessSequence=0;
const loadedStateSections={analyticsProject:null,analyticsAt:0,candidatesProject:null,candidatesAt:0};
const pendingForms=new WeakSet(),pendingSettingsSaves=new Map(),listSnapshots=new WeakMap();
function changedList(node,rows){const signature=JSON.stringify([projectId,language,rows]);if(listSnapshots.get(node)===signature)return false;listSnapshots.set(node,signature);return true;}
const draftStore=(()=>{
 const memory=new Map();let warned=false;
 function failed(){if(!warned){warned=true;Promise.resolve().then(()=>toast(t('浏览器未能保存草稿；当前页面仍保留输入，关闭页面前请另行保存。'),true));}}
 const storageKey=()=> 'rsi-ui-drafts'+(window.__RSI_MACHINE_KEY__?':'+window.__RSI_MACHINE_KEY__:''),version=1,ttl=7*24*60*60*1000;
 function read(){if(memory.has(storageKey()))return structuredClone(memory.get(storageKey()));try{const saved=JSON.parse(sessionStorage.getItem(storageKey())||'null');if(saved?.version===version&&saved.entries)return saved;}catch{}return{version,entries:{}};}
 function get(kind,scope){const data=read(),entry=data.entries[kind]?.[scope];if(!entry)return null;if(entry.version!==version||entry.expiresAt<Date.now()){delete data.entries[kind][scope];try{sessionStorage.setItem(storageKey(),JSON.stringify(data));}catch{}return null;}return structuredClone(entry.value);}
 function set(kind,scope,value){const data=read();data.entries[kind]??={};data.entries[kind][scope]={version,savedAt:Date.now(),expiresAt:Date.now()+ttl,value:structuredClone(value)};memory.set(storageKey(),structuredClone(data));try{sessionStorage.setItem(storageKey(),JSON.stringify(data));memory.delete(storageKey());}catch{failed();}}
 function remove(kind,scope){const data=read();if(!data.entries[kind])return;delete data.entries[kind][scope];if(!Object.keys(data.entries[kind]).length)delete data.entries[kind];memory.set(storageKey(),structuredClone(data));try{sessionStorage.setItem(storageKey(),JSON.stringify(data));memory.delete(storageKey());}catch{failed();}}
 return{get,set,remove};
})();
window.RSIDraftStore=draftStore;
const apiErrors={
 "Grouped requirements changed; reconcile revisions before splitting":"任务组的需求已修订，请先核对新增要求的归属，暂不自动拆分。",
 "Wait for the grouped run to stop before splitting":"任务组仍有运行，请先停止并等待运行结束，再拆分重做。",
 "Split retry preview changed; inspect it again":"任务组或原任务已变化，请重新查看拆分预览。",
 "Dependencies require up to 100 unique task IDs":"前置任务最多选择 100 个，不能重复。",
 "Dependency graph exceeds 1000 ancestors":"前置任务链超过 1000 个节点，请拆分安排。",
 "Only a stopped failed or cancelled group can be split for retry":"请选择一个已停止的失败或已取消任务组。",
 "Select one failed group to split for retry":"每次请选择一个失败任务组来拆分重做。",
 "Cancellation impact exceeds 1000 tasks; split the operation":"取消影响超过 1000 个任务，请分批处理。",
 "Preview dependent cancellations before confirming":"请先查看后继任务的取消预览，再确认操作。",
 "Cancellation preview changed; review it again":"任务或依赖已变化，请重新查看取消预览。",
 "Intervention reason must be at most 2000 characters":"干预原因最多填写 2000 个字符。",
 "Wait for the active run to finish stopping before retrying or supplementing this task":"任务仍在停止中，请等当前运行结束后再重试或补充需求。",
 "Restart plan is missing; inspect installations before admitting work":"缺少重启计划，请检查安装记录，确认当前版本后重新启动服务。",
 "Update check interrupted; retry is safe":"上次检查更新被中断，未更改任何内容，可直接重新检查。",
 "Installation interrupted before restart; retry is safe":"上次安装在重启前被中断，未更改现有版本，可直接重新安装。",
 "Unable to verify the official release feed":"无法访问 GitHub 官方发布源，未能确认是否有新版本。请检查网络后重试。",
 "Release changed; check again before installing":"发布内容已变化，为避免装错版本，请重新检查更新后再安装。",
 "Automatic update authorization changed":"自动更新授权已变化，本次不再自动安装；如需更新请手动确认。",
 "Managed installation directory cannot be a symlink":"内置自更新目录不能是符号链接，已停止安装。请检查 installations 安装记录。",
 "Previously staged installation differs from the verified bundle":"已暂存的安装目录与本次校验通过的安装包不一致，已停止安装。请重新检查更新后重试。",
 "Updates must come from official GitHub release URLs":"更新包只允许来自官方 GitHub 发布地址，已拒绝本次下载。",
 "Untrusted update redirect":"下载被重定向到非官方地址，已拒绝继续下载。",
 "Release download exceeded the total time budget":"下载新版本超时，已停止下载。请检查网络后重试。",
 "Release asset exceeds size limit":"更新包超过大小限制，已停止下载。",
 "Only stable major.minor.patch releases are supported":"仅支持 x.y.z 形式的正式稳定版本，无法识别该发布的版本号。",
 "Release is missing its plugin ZIP or SHA-256 file":"该发布缺少插件 ZIP 或 SHA-256 校验文件，无法安装。",
 "Release assets belong to another repository":"发布附件不属于官方仓库，已拒绝安装。",
 "Release SHA-256 verification failed":"更新包 SHA-256 校验未通过，已停止安装。",
 "Unsafe release archive path":"更新包内包含不安全的文件路径，已停止安装。",
 "Unpacked release exceeds size limit":"更新包解压后超过大小限制，已停止安装。",
 "Unsupported update manifest or state migration":"更新包的清单或状态迁移格式不受支持，已停止安装。",
 "Plugin manifest does not match release version":"插件清单中的版本与发布版本不一致，已停止安装。",
 "Incomplete release bundle":"更新包内容不完整，缺少必要文件，已停止安装。",
 "Restart plan disappeared before completion; preserve current state and inspect installations":"重启计划在完成前丢失。当前状态已保留，请检查 installations 安装记录后重新启动服务。",
 "Legacy supervisor did not confirm installation within 90 seconds; preserve current database and inspect installations":"旧版监督进程 90 秒内未确认安装。数据库已保留，请检查 installations 安装记录后重新启动服务。",
 "Unsupported state migration; refusing automatic rollback":"状态迁移格式不受支持，已拒绝自动回滚。请检查 installations 安装记录后重新启动服务。",
 "Restored service did not acknowledge the health-verified rollback":"恢复后的服务未确认回滚完成。请检查服务日志和 installations 安装记录后重新启动服务。",
 "Rolled-back service cannot be verified; preserve the database and restart plan":"无法确认回滚后的服务正常。数据库和重启计划已保留，请检查后重新启动服务。",
 "Committed candidate cannot be verified; preserve current database and inspect the retained restart plan":"已提交的新版本未能通过健康检查。当前数据库已保留，请检查保留的重启计划后重新启动服务。",
 "An unverified candidate is still running; preserve database and inspect the retained restart plan":"未通过验证的新版本仍在运行。数据库已保留，请检查保留的重启计划后重新启动服务。",
 "Unknown restart phase; refusing to overwrite state":"重启计划包含未知阶段，已拒绝覆盖当前状态。请检查 installations 安装记录后重新启动服务。",
 "A service still owns runtime.lock; inspect the retained restart plan before recovery":"仍有服务占用 runtime.lock。重启计划已保留，请处理后重新启动服务。",
 "Recovery backup is missing or corrupt; refusing an unsafe database rollback":"恢复备份缺失或损坏，为保护数据已拒绝回滚数据库。请检查 installations 安装记录后重新启动服务。",
 "Installation commit was interrupted; preserve current database and restart plan":"安装提交被中断。当前数据库和重启计划已保留，请检查后重新启动服务。",
 "Another runtime owns the database; recovery plan and database were preserved":"数据库正被其他运行实例占用。恢复计划和数据库均已保留，请关闭多余实例后重新启动服务。",
 "Recovery backup is corrupt; current database was preserved":"恢复备份损坏，当前数据库已原样保留。请检查 installations 安装记录后重新启动服务。",
 "Rolled-back service failed its startup health check; database was restored, inspect server.log and restart manually":"回滚后的服务未通过启动健康检查。数据库已恢复，请查看 server.log 并手动重启服务。",
 "Previous code and database restored and health verified":"已恢复旧版本：代码和数据库均已还原，健康检查通过。",
 "Verifying restored service":"正在验证恢复后的服务",
 "A newer deployment owns this target; no stale rollback will be issued":"已有更新版本接管此服务，本次不会恢复旧版本。",
 "A newer deployment owns this target; rollback was cancelled":"已有更新版本接管此服务，本次恢复已取消。",
 "A newer deployment owns this target; old health evidence was discarded":"已有更新版本接管此服务，本次部署的健康检查结果已作废。",
 "Another deployment is still active on this target":"同一服务的上次部署还未结束，将在结束后继续。",
 "Post-merge verification must pass before release/deploy":"合并后检查尚未通过，发布与部署会在检查通过后自动继续。",
 "Deployment target already has this or a newer commit; refusing a stale or diverged deployment":"部署目标已包含本次或更新的提交，继续部署会回退服务，已拒绝这次过期部署。",
 "Deployment ownership changed during validation":"校验期间同一服务的部署归属发生了变化，本次部署会等最新部署结束后继续。",
 "Workflow run identity differs from the authorized operation":"工作流执行记录与授权的部署操作不一致，请到 GitHub 核对本次部署对应的工作流记录。",
 "Select a compatible existing workflow from this project": "请先为项目添加支持自动交付的工作流，再刷新列表。",
 "Workflow authorization changed during inspection; refresh before authorizing": "检查期间工作流授权已变更，请刷新后重新授权。",
 "Trusted workflow tag points to a different commit": "工作流标签已指向其他版本，请重新授权。",
 "Trusted workflow tag changed; authorize the workflow again": "工作流标签已改变，请重新授权。",
 "Release/deploy authorization revoked or target changed": "发布授权已关闭或目标已改变，请检查项目设置。",
 "Pinned workflow does not implement the RSI dispatch contract": "此工作流缺少自动交付输入，请先更新工作流。",
 "Multiple workflow runs match one operation; inspect GitHub before retrying": "有多个工作流执行记录，请先到 GitHub 核对结果。",
 "Project paused or operation cancelled": "项目已暂停或操作已取消，发布暂时停止。",
 "Health response must confirm the exact deployment SHA and healthy status": "健康检查未确认本次部署版本正常，请检查服务返回的版本和状态。",
 "Restore the original authorization and let tracking retry the pending operation": "请恢复原工作流授权，系统会继续跟踪待执行操作。",
 "Only a confirmed failed workflow run can be retried; uncertain dispatches require reconciliation": "请先到 GitHub 确认工作流结果。只有已确认失败的执行才能重试。",
 "Candidate was cleaned up after the retention window; regenerate it before publishing": "候选已超过保留期被清理，请重新生成后再发布。",
 "Automatic merge requires PR delivery": "自动合并只在交付方式为 PR 时可用：请把「完成后如何交付」改为「自动提交修改，等待合并」或「检查通过后自动合并」，或关闭自动合并。",
 "Automation workflow must be a fixed .github/workflows YAML file": "工作流必须是 .github/workflows/ 目录下的固定 .yml 或 .yaml 文件（例如 .github/workflows/deploy.yml），请修正工作流路径后重新保存。",
 "Automation requires a fixed trusted tag": "工作流标签必须是固定名称：只能包含字母、数字、点、下划线和斜杠，不能包含 .. 或 //，也不能以 / 结尾。请填写仓库中真实存在的标签或分支。",
 "Automation requires the trusted workflow tag's full commit SHA": "工作流 SHA 必须是该标签指向的完整 40 位提交号，且只能使用小写字母 a-f 和数字：请在 GitHub 打开对应提交，复制完整 SHA 后重新粘贴。",
 "Release/deploy requires an explicit project and a pinned workflow tag": "启用发布或部署需要同时具备：GitHub 仓库、工作流文件、固定标签和完整提交 SHA。请在项目设置中补全这些字段后重新保存。",
 "Deployment health URL requires HTTPS without credentials, query or fragment": "健康检查地址必须以 https:// 开头，且不能包含账号密码、? 查询参数、# 片段或空格：请改用形如 https://example.com/health 的地址。",
 "Automatic deployment rollback requires an explicit health URL": "自动回滚需要先填写 HTTPS 健康检查地址：请补全「上线后检查地址」，或关闭「检查失败时恢复上一正常版本」。",
 "Workflow repository must match the selected project's origin": "无法读取工作流：项目设置中的 GitHub 仓库与项目本地 origin 不一致。请把 GitHub 仓库改成与 origin 相同的 owner/name（或更新项目 origin）后点击「刷新工作流」。",
 "Workflow must be an existing repository file": "无法读取工作流：仓库中不存在该工作流文件。请确认 .github/workflows 下的工作流路径后点击「刷新工作流」。",

 "Select the root of an existing Git repository": "请选择已有 Git 仓库的根目录。",
 "Expected a GitHub repository URL": "请填写 GitHub 仓库链接，例如 https://github.com/owner/repository。",
 "Use a GitHub repository URL such as https://github.com/owner/repository": "请填写 GitHub 仓库链接，例如 https://github.com/owner/repository。",
 "Import directory already exists; register it as a local project instead": "保存目录已存在，请选择本地项目接入，或换一个空目录。",
 "Project not found": "项目已不存在，请重新选择项目。",
 "Project and controller state must be in separate directory trees": "项目目录不能与控制台的数据目录相互包含，请换一个位置。",
 "Projects must not contain each other's directories": "各项目目录不能相互包含，请选择独立目录。",
 "working_hours requires different start/end hours in 0..24; start must be below 24": "工作时段的开始和结束需不同；开始为 0 至 23 时，结束为 0 至 24 时，可跨午夜。",
 "Repository must be owner/name": "仓库请填写 owner/name，不是完整链接。",
 "Goal must contain 1..30000 characters": "目标不能为空，且不能超过 30000 个字符。",
 "Wait until active runs finish before importing projects": "还有任务正在运行，请等任务结束后再接入项目。",
 "Wait until active runs finish before registering projects so every worker uses the same isolation boundaries": "还有任务正在运行，请等任务结束后再接入项目。",
 "Only project scan settings can inherit global defaults": "只能让项目扫描设置跟随通用设置。",
 "An inherited setting cannot also have an override": "跟随通用的扫描项不能同时填写 JSON 覆盖，请先选择单独设置。",
 "Invalid origin or CSRF token": "页面安全校验已过期。输入已保留，请重新打开控制台后重试。",
 "No GitHub repository configured": "请先在项目设置中填写 GitHub 仓库，再安排 Issue 同步。",
 "Candidate evidence is incompatible with current configuration": "候选的评估证据与当前配置不兼容，请重新评估后再启用；当前稳定策略已保留。",
 "Supplement must contain 1..10000 characters": "补充需求不能为空，且不能超过 10000 个字符。请缩短后重试。",
 "Pause or finish active work before supplementing this task": "该任务正在运行或属于系统自动任务，暂不能补充需求。请等待当前运行结束后再试。",
 "Combined requirements exceed 30000 characters": "合并后的需求超过 30000 个字符。请缩短补充内容；原始需求不会被截断。",
 "Task dependency cycle": "任务依赖形成环路，无法设置该前置依赖。",
 "Dependency must be another task in the same project": "前置依赖必须是同一项目下的其他任务。",
 "Urgency must be 0 or 1": "紧急程度只能为 0 或 1。",
 "Only waiting tasks can be scheduled": "只有处于排队或暂缓的任务才能设置前置依赖。",
 "Automatic delivery must be explicitly enabled per project": "自动交付必须在具体项目设置中显式启用，不能在全局开启。",
 "Parallel project limit is a global setting; select global defaults": "并发项目限制是全局设置，请在全局通用设置中调整。",
 "Software updates are a global setting": "软件更新是全局通用设置，不能在项目级别单独覆盖。",
 "Only a verified candidate awaiting approval can be delivered": "只有通过验证且待批准的候选才能交付。",
 "Independent gates have not passed": "独立验证与审查门禁未全部通过，无法交付。",
 "No verified code candidate to revert": "没有可回滚的代码候选。",
 "Explicit confirmation is required to reset invalid project settings": "需要明确确认才能重置无效的项目配置。",
 "Project configuration is valid; reset is unavailable": "当前项目配置有效，无需重置。",
 "Project configuration must be a JSON object": "项目配置必须是有效的 JSON 对象。",
 "Configuration must be an object": "配置必须是对象格式。",
 "Invalid event pagination": "事件分页参数无效。"
};
function reviseForm(form){if(form)formRevisions.set(form,(formRevisions.get(form)||0)+1);}
for(const type of ['input','change','reset'])document.addEventListener(type,event=>{
 const form=event.target.closest('form');reviseForm(form);
 if(form?.id==='settings-form'&&event.target.dataset.path){const key=settingsKey(),revisions=settingsFieldRevisions.get(key)||new Map();revisions.set(event.target.dataset.path,(revisions.get(event.target.dataset.path)||0)+1);settingsFieldRevisions.set(key,revisions);}
},true);
document.addEventListener('close',event=>{if(event.target.tagName==='DIALOG')reviseForm(event.target.querySelector('form'));},true);
function unchangedForm(form,{projectSensitive=true}={}){const revision=formRevisions.get(form)||0,project=projectId;return ()=>(formRevisions.get(form)||0)===revision&&(!projectSensitive||projectId===project);}
function el(tag, text, cls){ const node=document.createElement(tag); if(text!==undefined)node.textContent=text; if(cls)node.className=cls; return node; }
function toast(message,error=false){ $('toast').textContent=message; $('toast').className=error?'error':''; $('toast').hidden=false; clearTimeout(toastTimer); if(!error)toastTimer=setTimeout(()=>$('toast').hidden=true,6500);else{const close=el('button','×','toast-close');close.type='button';close.setAttribute('aria-label',t('关闭'));close.addEventListener('click',()=>$('toast').hidden=true);$('toast').append(close);} }
async function sendApi(path,body,signal,{text=false}={}){
 const bootstrapRepair=body!==undefined&&!state&&path==='config/repair-global';
 if(body!==undefined&&!state&&!bootstrapRepair)throw Error(t('正在连接本地服务，请稍候再提交。'));
 const controller=new AbortController();
 // Refresh already owns a shared deadline and cancellation across pagination.
 const timeout=signal?null:setTimeout(()=>controller.abort(),path==='model-center/probe'?960000:path==='projects/import'?210000:60000);
 try{
 const options=body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json','X-RSI-CSRF':state?.csrf||''},body:JSON.stringify(body)};options.signal=signal||controller.signal;
 if(bootstrapRepair){
  try{
   const contract=await fetch('/api/contract');
   if(!contract.ok)throw new Error('contract unavailable');
   options.headers['X-RSI-CSRF']=(await contract.json()).csrf;
  }catch{throw new TypeError(t('正在连接本地服务，请稍候再提交。'));}
 }
 let response;
 try{response=await fetch('/api/'+path,options);}catch(error){throw new TypeError(t('无法连接本地服务。输入已保留，请确认服务仍在运行后重试。'));}
 if(response.status===401){const error=Error(t('登录已失效，输入已保留。请从启动入口重新打开控制台，无需重启。'));error.code='session_required';error.status=401;throw error;}
 if(text&&response.ok)return await response.text();
 let data;
 try{data=await response.json();}catch(error){const invalid=Error(t('服务返回了无法识别的结果。输入已保留，请稍后重试。'));invalid.status=response.status;throw invalid;}
 if(!response.ok){
  // 已知错误给中文指引；未知技术错误兜底为通用文案，原文留在控制台便于诊断。
  const known=apiErrors[data.error];
  const message=known?t(known):(data.error?t('操作未完成，请稍后重试。')+(data.error_code?'（'+data.error_code+'）':''):String(response.status));
  if(!known&&data.error)console.warn('[adaptive-rsi]',data.error);
  if(data.error_code==='invalid_global_configuration')queueMicrotask(()=>offerGlobalConfigRepair(data.raw));
  const error=Error(message);error.code=data.error_code||data.catalog_error;error.status=response.status;error.evidence={status:response.status,code:error.code,error:data.error};throw error;
 }
 if(body!==undefined&&(path==='config'||path==='config/repair'||path.startsWith('model-center/')))readinessCache.clear();
 return data;
 }finally{if(timeout!==null)clearTimeout(timeout);}
}
async function api(path,body,{signal}={}){
 const payload=body===undefined?undefined:structuredClone(body);
 try{return await sendApi(path,payload,signal);}catch(error){if(payload===undefined||error.code!=='update_in_progress')throw error;}
 toast(t('服务正在完成更新，表单已保留，恢复后自动提交。'));
 const deadline=Date.now()+120000;
 while(Date.now()<deadline){
  await new Promise(resolve=>setTimeout(resolve,1000));
  let next;
  try{next=await sendApi('state',undefined,signal);}catch(error){if(error instanceof TypeError)continue;throw error;}
  if(next.update?.busy??['downloading','restarting'].includes(next.update?.state))continue;
  state=next;
  // 只重试服务明确拒绝执行的请求；提交后断线时不重复操作。
  try{return await sendApi(path,payload,signal);}catch(error){if(error.code!=='update_in_progress')throw error;}
 }
 throw Error(t('更新尚未完成，已保留输入；请稍后再提交。'));
}
const goalSubmissionLimit=32;
function goalSubmissionKey(project){return 'rsi-submission:'+(window.__RSI_MACHINE_KEY__?window.__RSI_MACHINE_KEY__+':':'')+project;}
let goalRecoveryPanel,goalRecoveryKey,goalRecoverySequence=0;
function goalRecordError(){return Error(t('无法读取目标提交记录。输入已保留，请检查浏览器会话存储后重试。'));}
async function goalContentDigest(text){return Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(JSON.stringify(text)))),byte=>byte.toString(16).padStart(2,'0')).join('');}
async function goalSubmissions(project){
 let raw;try{raw=sessionStorage.getItem(goalSubmissionKey(project));}catch{throw goalRecordError();}if(raw===null)return [];
 let saved;try{saved=JSON.parse(raw);}catch{throw goalRecordError();}
 if(!saved||typeof saved!=='object'||Array.isArray(saved))throw goalRecordError();
 const validText=text=>typeof text==='string'&&!!text.trim()&&text.length<=30000;
 let entries;
 if(!Object.hasOwn(saved,'version')&&validText(saved.text))entries=[{digest:await goalContentDigest(saved.text),id:saved.id,text:saved.text}];
 else if(saved.version===2&&Array.isArray(saved.entries)&&saved.entries.length<=goalSubmissionLimit)entries=saved.entries;
 else throw goalRecordError();
 const ids=new Set(),digests=new Set(),validated=[];
 for(const entry of entries){
  if(!entry||typeof entry.digest!=='string'||!/^[a-f0-9]{64}$/i.test(entry.digest)||typeof entry.id!=='string'||!entry.id.length||Array.from(entry.id).length>100)throw goalRecordError();
  const digest=entry.digest.toLowerCase();
  if(ids.has(entry.id)||digests.has(digest))throw goalRecordError();
  if(Object.hasOwn(entry,'text')&&(!validText(entry.text)||await goalContentDigest(entry.text)!==digest))throw goalRecordError();
  ids.add(entry.id);digests.add(digest);validated.push({digest,id:entry.id,...(Object.hasOwn(entry,'text')?{text:entry.text}:{})});
 }
 return validated;
}
function saveGoalSubmissions(project,entries){const key=goalSubmissionKey(project);if(entries.length)sessionStorage.setItem(key,JSON.stringify({version:2,entries}));else sessionStorage.removeItem(key);}
async function renderGoalRecovery(){
 const project=projectId;let raw;try{raw=sessionStorage.getItem(goalSubmissionKey(project));}catch{return;}
 const key=JSON.stringify([project,typeof language==='undefined'?'':language,raw]);
 if(goalRecoveryKey===key)return;
 goalRecoveryKey=key;const ticket=++goalRecoverySequence;
 if(!goalRecoveryPanel){goalRecoveryPanel=el('div');goalRecoveryPanel.dataset.goalRecovery='';$('goal-form').append(goalRecoveryPanel);}
 const panel=goalRecoveryPanel;panel.replaceChildren();panel.hidden=true;
 if(!project)return;
 let entries;try{entries=await goalSubmissions(project);}catch(error){if(ticket===goalRecoverySequence&&project===projectId){panel.append(el('p',error.message,'form-error'));panel.hidden=false;}return;}
 if(ticket!==goalRecoverySequence||project!==projectId||!entries.length)return;
 panel.hidden=false;
 panel.append(el('p',t('待确认目标的原文保留在当前浏览器会话中。恢复后点击添加，将沿用原提交记录确认结果。'),'muted'));
 const recoverable=entries.filter(entry=>entry.text!==undefined);
 if(recoverable.length){
  const label=el('label',t('待确认的目标')),select=el('select'),placeholder=el('option',t('选择要恢复的目标'));placeholder.value='';select.append(placeholder);
  for(const entry of recoverable){const option=el('option',entry.text.slice(0,100));option.value=entry.id;select.append(option);}
  label.append(select);
  const restore=action(t('恢复到输入框（替换当前草稿）'),()=>{
   if(ticket!==goalRecoverySequence||project!==projectId)return;
   const entry=recoverable.find(item=>item.id===select.value);if(!entry)return;
   $('goal-text').value=entry.text;goalDraftRevisions.set(project,(goalDraftRevisions.get(project)||0)+1);reviseForm($('goal-form'));stashGoalDraft();$('goal-text').focus();
  });
  panel.append(label,restore);
 }
 if(recoverable.length!==entries.length)panel.append(el('p',t('部分旧提交记录没有保留原文。请输入当时的完整目标后重试，系统会沿用原提交记录；这些记录不会自动删除。'),'muted'));
}
async function pendingGoalSubmission(project,text){
 const digest=await goalContentDigest(text),entries=await goalSubmissions(project);
 let submission=entries.find(entry=>entry.digest===digest);
 if(!submission){
  if(entries.length>=goalSubmissionLimit)throw Error(t('待确认的目标提交过多。请从下方恢复一个目标并重试，确认成功后再提交新目标。'));
  submission={digest,id:crypto.randomUUID(),text};entries.push(submission);
 }else submission.text=text;
 // Persist before sending; never evict an ID whose server outcome is still unknown.
 saveGoalSubmissions(project,entries);await renderGoalRecovery();return submission;
}
function currentProject(){return state.projects.find(p=>p.id===projectId);}
function projectPath(project){return typeof project?.path==='string'?project.path:'';}
function projectName(project){const name=typeof project?.name==='string'?project.name.trim():'';return name||projectPath(project).split(/[\\/]/).filter(Boolean).pop()||t('未命名项目');}
function projectLabel(project){const path=projectPath(project);return projectName(project)+(path?' — '+path:'');}
function stashGoalDraft(){if(!projectId)return;const value=$('goal-text').value;if(value){goalDrafts.set(projectId,value);draftStore.set('goal',projectId,value);}else{goalDrafts.delete(projectId);draftStore.remove('goal',projectId);}}
function restoreGoalDraft(){if(!projectId){$('goal-text').value='';return;}const value=goalDrafts.has(projectId)?goalDrafts.get(projectId):draftStore.get('goal',projectId)||'';if(value)goalDrafts.set(projectId,value);$('goal-text').value=value;}
function scoped(rows){return rows.filter(row=>row.project===projectId);}
let displayTz=null;
function stamp(seconds){try{return new Date(seconds*1000).toLocaleString(language,{year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',timeZone:displayTz||undefined});}catch{return new Date(seconds*1000).toLocaleString(language,{year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'});}}
function scheduleStamp(seconds,timezone){try{return new Date(seconds*1000).toLocaleString(language,{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',timeZone:timezone});}catch{return stamp(seconds);}}
async function showActivityHistory(){
 const project=projectId,box=el('dialog',undefined,'experience-dialog'),list=el('div'),more=action(t('加载更早活动'),load),close=action(t('关闭'),()=>box.close());
 box.append(el('h2',t('活动历史')),el('p',t('显示时区')+' · '+(displayTz||Intl.DateTimeFormat().resolvedOptions().timeZone)),list,more,close);
 box.addEventListener('close',()=>box.remove());document.body.append(box);box.showModal();let before='';
 async function load(){more.disabled=true;try{const data=await api('events?'+new URLSearchParams({project,limit:100,...(before?{before}:{})}));if(!box.open)return;for(const event of data.events){list.append(el('p',stamp(event.created)+' · '+eventCopy(event)));if(event.kind==='native_tool'){const detail=el('details'),raw=el('pre');detail.append(el('summary',t('工具轨迹元数据')),raw);detail.addEventListener('toggle',()=>{if(detail.open&&!raw.textContent)raw.textContent=typeof event.data==='string'?event.data:JSON.stringify(event.data,null,2);});list.append(detail);}}before=data.next_before;more.hidden=!data.has_more;}finally{more.disabled=false;}}
 await load();
}
function badge(status){return el('span',t(statusNames[status]||status),'badge '+(['stable','completed','pr_open','merged','verified','pass','awaiting_approval'].includes(status)?'good':['failed','fail','blocked','rolled_back','ci_failed','regressed'].includes(status)?'bad':['queued','planning','executing','evaluating'].includes(status)?'warn':'neutral'));}
function action(text,callback,cls='button'){const node=el('button',text,cls);node.type='button';node.addEventListener('click',()=>safe(callback));return node;}
function uiConfirm(message,content){
 // 原生 confirm 无法跟随界面语言且风格割裂；用统一的对话框获取确认结果。
 return new Promise(resolve=>{
  const box=el('dialog',undefined,'confirm-dialog'),text=el('p',message),row=el('div',undefined,'confirm-actions');
  const no=el('button',t('取消'),'button');no.type='button';
  const yes=el('button',t('确认'),'button primary');yes.type='button';
  row.append(no,yes);box.append(text);if(content)box.append(content);box.append(row);document.body.append(box);
  const previousFocus=document.activeElement;
  let settled=false;
  const finish=value=>{if(settled)return;settled=true;resolve(value);box.close();box.remove();previousFocus?.focus?.();};
  no.addEventListener('click',()=>finish(false));
  yes.addEventListener('click',()=>finish(true));
  box.addEventListener('cancel',event=>{event.preventDefault();finish(false);});
  box.showModal();
  no.focus();
 });
}
function elapsedCopy(seconds){const value=Math.max(0,Math.round(seconds||0));return value<60?value+' '+t('秒'):value<3600?Math.floor(value/60)+' '+t('分钟'):Math.floor(value/3600)+' '+t('小时')+' '+Math.floor(value%3600/60)+' '+t('分钟');}
function runningStage(run){if(run.last_stage==='review'||run.stage==='review')return t('正在评审改动');return t({planning:'正在判断是否值得做',executing:'正在修改代码',evaluating:'正在运行检查'}[run.state]||'正在处理任务');}
async function safe(fn,form){
 if(form&&pendingForms.has(form))return;
 if(form)pendingForms.add(form);
 const generation=settingsGeneration,scope=form?.id==='settings-form'?settingsKey():projectId;
 const unchanged=form?unchangedForm(form,{projectSensitive:form.id!=='project-form'}):()=>true;
 const feedbackCurrent=()=>unchanged()&&(form?.id!=='settings-form'||(generation===settingsGeneration&&scope===settingsKey()));
 const submit=form?.id==='goal-form'?form.querySelector?.('button[type="submit"]'):null,label=submit?.textContent;
 if(submit){submit.disabled=true;submit.textContent=t('正在添加…');}
 let feedback=form?.querySelector?.('[data-form-error]');
 if(feedback)feedback.hidden=true;
 try{await fn();}catch(error){
  const message=error instanceof SyntaxError?t('JSON 格式有误，请检查逗号、引号和括号；输入已保留。'):error.message;
  if(form&&!feedbackCurrent()){toast(t('先前的提交未成功。当前输入已保留，请回到原来的项目或设置范围检查。'),true);return;}
  if(form){if(!feedback){feedback=el('p',undefined,'form-error');feedback.dataset.formError='';feedback.setAttribute('role','alert');form.append(feedback);}feedback.textContent=message;feedback.hidden=false;}
  toast(message,true);if(error.evidence&&window.RSIProduct){const evidence=window.RSIProduct.raw(error.evidence,'查看原始诊断');$('toast').append(evidence);}
 }finally{if(form)pendingForms.delete(form);if(submit){submit.disabled=false;submit.textContent=label;}}
}
function refreshDelay(){if(document.visibilityState==='hidden')return 60000;return state?.runs?.some(run=>!run.ended)?5000:15000;}
function scheduleRefreshPoll(delay=refreshDelay()){clearTimeout(refreshPollTimer);refreshPollTimer=setTimeout(()=>{refreshPollTimer=null;refresh().catch(error=>{$('connection').textContent=t(error.code==='session_required'?'需要重新登录':'连接中断');});},delay);}
function requestedStateSections(force=false){
 const sections=['core'],project=projectId||null,stale=Date.now()-30000;
 // 用户刚完成回滚、导入或晋升时，旧数组仍在 TTL 内也不能沿用；其他轮询继续使用 TTL。
 if(force||view==='evolution'&&(loadedStateSections.candidatesProject!==project||loadedStateSections.candidatesAt<stale))sections.push('candidates');
 if(force||['overview','evolution'].includes(view)&&(loadedStateSections.analyticsProject!==project||loadedStateSections.analyticsAt<stale))sections.push('analytics');
 return sections;
}
async function refresh(options={}){
 const force=!!options.force,requestedProject=projectId,requestedView=view,sections=requestedStateSections(force),taskView=['overview','tasks'].includes(view),filter=taskView?$('task-filter').value:'all',system=taskView?String($('show-system-tasks').checked):'true',offset=taskView?taskOffset:0;
 const key=JSON.stringify([requestedProject,view,offset,filter,system,sections]);
 if(refreshPromise){if(refreshKey===key)return refreshPromise;refreshQueued=true;refreshForce=refreshForce||force;refreshSequence++;refreshController?.abort();return new Promise((resolve,reject)=>refreshWaiters.push({resolve,reject}));}
 const ticket=++refreshSequence;clearTimeout(refreshPollTimer);
 const controller=new AbortController(),query=new URLSearchParams({project:requestedProject,limit:'50',offset:String(offset),filter,system,sections:sections.join(',')});
 refreshController=controller;refreshProject=requestedProject;refreshKey=key;let timedOut=false;const timeout=setTimeout(()=>{timedOut=true;controller.abort();},15000);
 const operation=(async()=>{
  try{
   const current=()=>!controller.signal.aborted&&ticket===refreshSequence&&requestedProject===projectId&&requestedView===view&&(!taskView||(taskOffset===offset&&$('task-filter').value===filter&&String($('show-system-tasks').checked)===system));
   let next=await api('state?'+query,undefined,{signal:controller.signal});if(!current())return;
   // A shrinking group can strand the last page. Retry its first page once, under the same cancellation guard.
   const correctPage=taskView&&next.selected_project===requestedProject&&offset>0&&next.pagination?.offset===offset&&offset>=next.pagination.total;
   if(correctPage){
    query.set('offset','0');
    next=await api('state?'+query,undefined,{signal:controller.signal});if(!current())return;
    if(next.selected_project!==requestedProject||next.pagination?.offset!==0)return;
    taskOffset=0;
   }
   if(bootBuild&&next.build&&bootBuild!==next.build)$('reload-ui').hidden=false;if(state&&state.version!==next.version&&next.version!==lastUpdateToast){lastUpdateToast=next.version;toast(t('后台已更新，当前输入已保留。刷新页面可加载新版界面。'));}
   const previousProject=state?.selected_project,returned=new Set(Array.isArray(next.sections)?next.sections:[]);
   if(!next.sections){if(Object.hasOwn(next,'candidates'))returned.add('candidates');if(['cases','quality','usage','model_usage'].every(key=>Object.hasOwn(next,key)))returned.add('analytics');}
   if(previousProject!==undefined&&previousProject!==next.selected_project){const merged={...(state||{}),...next};if(!returned.has('candidates'))delete merged.candidates;if(!returned.has('analytics'))for(const key of ['cases','quality','usage','model_usage'])delete merged[key];state=merged;}
   else state={...(state||{}),...next};
   if(returned.has('analytics')){loadedStateSections.analyticsProject=next.selected_project??null;loadedStateSections.analyticsAt=Date.now();}
   if(returned.has('candidates')){loadedStateSections.candidatesProject=next.selected_project??null;loadedStateSections.candidatesAt=Date.now();}
   $('connection').textContent=t('已连接');$('version').textContent='Adaptive RSI '+state.version;
   const select=$('project-select'),old=projectId;if(!state.projects.some(p=>p.id===projectId)){stashGoalDraft();projectId=state.projects[0]?.id||'';restoreGoalDraft();goalDraftRestoredProject=projectId;}
   if(JSON.stringify([...select.options].map(x=>[x.value,x.text]))!==JSON.stringify(state.projects.map(p=>[p.id,projectLabel(p)]))){select.replaceChildren(...state.projects.map(p=>{const o=el('option',projectLabel(p));o.value=p.id;o.title=projectPath(p);return o;}));if(!state.projects.length){const o=el('option',t('尚未接入项目'));o.value='';select.append(o);}}
   if(projectId&&!scopeChosen&&!scopeInitialized){stashSettings();$('config-scope').value='project';settingsBaseline=null;scopeInitialized=true;}
   select.value=projectId;if(old!==projectId){stashSettings();closeTaskDetail();settingsBaseline=null;localWrite('rsi-project',projectId);goalDraftRestoredProject=projectId;}if(goalDraftRestoredProject!==projectId){restoreGoalDraft();goalDraftRestoredProject=projectId;}
   render();window.RSIExperience?.refreshed();
  }catch(error){if(controller.signal.aborted){if(timedOut&&requestedProject===projectId)$('connection').textContent=t('连接超时');return;}throw error;}
  finally{clearTimeout(timeout);}
 })();refreshPromise=operation;
 try{return await operation;}finally{if(refreshPromise===operation){refreshPromise=null;refreshController=null;refreshProject=null;refreshKey=null;}if(refreshQueued){refreshQueued=false;const waiters=refreshWaiters.splice(0),forceNext=refreshForce;refreshForce=false;let next;try{next=refresh({force:forceNext});}catch(error){for(const waiter of waiters)waiter.reject(error);scheduleRefreshPoll();}if(next)Promise.resolve(next).then(value=>waiters.forEach(waiter=>waiter.resolve(value)),error=>waiters.forEach(waiter=>waiter.reject(error)));}else scheduleRefreshPoll();}
}
function parentView(name){return name==='models'?'settings':name==='tasks'?'overview':name;}
function renderNavigation(){
 const parent=parentView(view),configScope=$('config-scope').value,project=currentProject();
 const path=projectPath(project);document.title=(project?projectName(project)+(path?' · '+path:'')+' · ':'')+t('Adaptive RSI · 控制台');
 document.querySelectorAll('.nav').forEach(node=>node.classList.toggle('active',node.dataset.view===parent));
 document.querySelectorAll('.nav').forEach(node=>{const selected=node.dataset.view===parent;if(selected)node.setAttribute('aria-current','page');else node.removeAttribute('aria-current');});
 document.querySelector('#tasks-list')?.setAttribute('aria-live','polite');
 $('settings-tabs').hidden=parent!=='settings';$('settings-context').hidden=parent!=='settings';
 $('page-name').textContent=t(parent==='settings'?(configScope==='global'?'通用设置':'项目设置'):{overview:'工作台',evolution:'自我改进记录',usage:'记录与用量'}[parent]);
 $('project-settings').hidden=!projectId;$('back-workspace').hidden=parent==='overview';
 $('global-settings').classList.toggle('active',parent==='settings'&&configScope==='global');
 $('settings-context-title').textContent=configScope==='global'?t('通用默认设置'):(project?projectName(project):t('当前项目'))+' · '+t('项目设置');
 $('settings-context-note').textContent=t(configScope==='global'?'未单独设置的项目会沿用这里的配置。':'仅修改当前项目，其他项目不受影响。');
 document.querySelectorAll('[data-settings-scope]').forEach(node=>{node.classList.toggle('active',node.dataset.settingsScope===configScope);node.setAttribute('aria-pressed',String(node.dataset.settingsScope===configScope));node.disabled=node.dataset.settingsScope==='project'&&!projectId;});
}
const activeStates=['releasing','deploying','queued','planning','executing','evaluating','pr_open'];
const attentionStates=['release_failed','deploy_failed','waiting_budget','awaiting_approval','blocked','failed','insufficient_evidence','interrupted','regressed','unverified_merge','ci_failed','expired'];
function visibleTasks(rows,filter,includeSystem=false){return rows.filter(task=>(includeSystem||!task.source.startsWith('system_'))&&(filter==='all'||(filter==='active'?activeStates.includes(task.display_state||task.state):filter==='attention'?attentionStates.includes(task.display_state||task.state):!activeStates.includes(task.display_state||task.state)&&!attentionStates.includes(task.display_state||task.state))));}
function issueSyncCopy(scanned,changed,complete){
 const key=complete?(changed?'已检查 {scanned} 个 Issue，同步 {changed} 条更新':'已检查 {scanned} 个 Issue，没有需要同步的变化'):(changed?'已检查 {scanned} 个 Issue，同步 {changed} 条更新；本次同步未完成，下一轮继续':'已检查 {scanned} 个 Issue，暂未发现变化；本次同步未完成，下一轮继续');
 return t(key).replace('{scanned}',scanned).replace('{changed}',changed);
}
function eventCopy(event){
const known={'Project registered (paused)':'项目已接入，等待启动','Project registered and enabled':'项目已接入并启动','Settings saved; active runs retain their snapshot':'设置已保存','Model routes updated':'模型分工已保存','Project paused':'项目已暂停','Project enabled':'项目已启动','Cancellation requested':'已申请取消任务','Dependency snapshot frozen':'已准备独立工作环境','Committed snapshot unchanged; no model call':'项目没有新改动，本次检查未调用模型','Fresh run scheduled with bounded backoff':'已安排重试','Provider resumed by user':'模型连接已恢复','Issue closed or excluded during execution; recheck before delivery':'运行期间 Issue 已关闭或被排除，将在交付前复核','Earlier attempt ended without a fix; rediscovered finding queued for fresh verification':'此前尝试未产出修复；重新发现的问题已入队等待全新验证','Dependency will not merge; waiting released and the task needs manual review':'前置任务无法合并；等待已解除，任务需要人工复核后重试'};
 if(event.message==='Dependency will not merge; task needs manual review')return t('前置任务无法合并；请复核其他依赖并手动重试。');
 if(event.message==='Desktop update admission recovered')return t('桌面更新屏障已恢复');
 if(known[event.message])return t(known[event.message]);
 let data=event.data;if(typeof data==='string'){try{data=JSON.parse(data);}catch{}}
 if(event.kind==='native_tool'&&data){const states={started:t('开始'),completed:t('已完成'),failed:t('未通过'),cancelled:t('已取消')};return t('原生工具轨迹')+' · '+data.tool+' · '+(states[data.state]||t('未知'))+(data.partial_results?.length?' · '+t('已保留命中位置')+' '+data.partial_results.length:'');}
 if(event.kind==='issues'&&data&&Number.isInteger(data.scanned)&&Number.isInteger(data.changed))return issueSyncCopy(data.scanned,data.changed,data.complete);
 const issues=event.message.match(/^(\d+) Issue updates synchronized$/);
 if(issues){const copy=t('已同步 Issue 更新')+' · '+issues[1];return data?.complete===false?copy+t('；')+t('本次同步未完成，下一轮继续'):copy;}
 return event.message;
}
function render(){
 void renderGoalRecovery();
 const project=currentProject(),tasks=scoped(state.tasks),runs=scoped(state.runs),config=project?.config||state.config;
 // 统一时间显示约定：活动与任务时间使用项目时区（与「下次检查」一致），未接入项目时用浏览器时区。
 displayTz=config?.schedule?.timezone||null;
 if(!project&&$('config-scope').value==='project'){$('config-scope').value='global';settingsBaseline=null;}
 renderNavigation();$('onboarding').hidden=!!project;$('project-workspace').hidden=!project;$('pause-button').hidden=!project;
 const configError=project?.configuration_error,configErrorText=typeof configError==='string'?configError:configError?.message||configError?.error||'';
 $('pause-button').disabled=!project||!!(configError&&project.paused);$('pause-button').textContent=t(!project||project.paused?'开始运行':'暂停运行');
 $('project-config-error').hidden=!configError;$('project-config-error').textContent=configError?t('项目配置异常，已暂停运行。请修复后再启动。')+(configErrorText?' '+configErrorText:'')+' '+t('重置会丢弃项目单独覆盖并改为跟随通用默认；原始配置仅保存在本地审计备份中。修复后仍保持暂停，需要手动启动。'):'';
 if(configError){const repair=el('button',t('重置损坏设置'),'text-button');repair.type='button';repair.disabled=configurationRepairBusy;repair.addEventListener('click',()=>safe(async()=>{
  if(configurationRepairBusy)return;const project=projectId;if(!project||!(await uiConfirm(t('确认重置损坏设置？这会删除当前项目单独覆盖并改为跟随通用默认；原始配置仅保存在本地审计备份中。修复后仍保持暂停，需要你手动启动。'))))return;
  configurationRepairBusy=true;repair.disabled=true;let result;try{result=await api('config/repair',{project,confirmation:'reset-invalid-overrides-to-global-defaults'});}finally{configurationRepairBusy=false;repair.disabled=false;}
  if(result.repaired){await refresh();toast(t('项目设置已修复为通用默认，项目仍保持暂停。'));}
 }));$('project-config-error').append(repair);}
 $('workspace-title').textContent=project?projectName(project):t('从一个项目开始');
 $('workspace-path').textContent=project?projectPath(project):'';
 let archive=$('archive-project');
 if(!archive){archive=el('button',t('归档当前项目'),'text-button');archive.id='archive-project';archive.type='button';$('workspace-path').after(archive);}
 archive.hidden=!project;
 $('workspace-path').hidden=!project;
 $('workspace-path').title=project?projectPath(project):'';
 $('goal-project-target').textContent=project?projectName(project):t('尚未接入项目');
 $('workspace-description').hidden=!!project;
 $('workspace-description').textContent=t(project?'自主发现，也随时接受你的目标。':'连接项目与模型，让它持续发现值得做的改进。');
 $('project-status').textContent=t(configError?'设置异常':!project||project.paused?'已暂停':'运行中');$('project-status').className='badge '+(configError?'bad':project&&!project.paused?'good':'neutral');
 const counts=state.selected_project&&state.selected_project!==projectId?null:state.task_counts;
 const running=runs.find(run=>!run.ended),queue=counts?.find(r=>r.state==='queued')?.count??tasks.filter(task=>task.state==='queued').length;
 $('queue-count').textContent=queue;$('queue-count').hidden=!queue;
 $('current-work').textContent=running?(running.task_title||tasks.find(task=>task.id===running.task)?.title||t(statusNames[running.state]||'执行中')):t(project?.paused?'暂停期间不会调用模型或修改代码':queue?'准备处理任务':'等待下一次检查');
 $('current-work-detail').textContent=running?runningStage(running)+' · '+t('已运行')+' '+elapsedCopy(Date.now()/1000-running.started)+(running.last_stage_at?' · '+t('最近进展')+' '+stamp(running.last_stage_at):''):project?.paused?t('启动后会按计划检查项目，也可以随时添加目标。'):queue?t('已有')+' '+queue+' '+t('个任务等待，当前任务结束后按价值顺序开始。'):t('已到下一次检查时间时会自动开始，无需保持页面前台运行。');
 const usage=(state.usage||[]).find(u=>u.project===(projectId||'model-center'));$('usage-brief').textContent=loadedStateSections.analyticsProject===(projectId||null)?(usage?.tokens||0)+' tokens':t('统计加载中');$('runtime-evidence').hidden=!project&&!usage;
 const scheduleTime=row=>row.next_effective??row.due;
 const next=state.schedules.filter(row=>row.project===projectId&&((row.kind==='issues'&&config.sources.issues&&config.github.repository)||(row.kind==='light'&&config.sources.inspection))).sort((a,b)=>scheduleTime(a)-scheduleTime(b))[0];
 const scheduleTimezone=config.schedule?.timezone||'UTC';
 $('next-check').textContent=t(project?.paused?'启动后开始':!next?'未安排自动检查':'下次 ')+(project&&!project.paused&&next?scheduleStamp(scheduleTime(next),scheduleTimezone)+' · '+t('项目时区')+' '+scheduleTimezone:'');
 const attention=counts?counts.filter(r=>attentionStates.includes(r.state)).reduce((n,r)=>n+r.count,0):visibleTasks(tasks,'attention',true).length;$('attention-banner').hidden=!attention;$('attention-text').textContent=t('需要处理的任务')+' · '+attention;
 for(const kind of ['light','issues']){
  const mins=config.schedule[kind+'_minutes'],factor=mins%1440===0?1440:mins%60===0?60:1;
  $(kind+'-interval').textContent=(mins/factor)+' '+t(factor===1440?'天':factor===60?'小时':'分钟');
  const enabled=kind==='issues'?config.sources.issues&&config.github.repository:config.sources.inspection;
  const schedule=state.schedules.find(row=>row.project===projectId&&row.kind===kind);
  $(kind+'-due').textContent=t(!enabled?'未启用':project?.paused?'项目已暂停':schedule?'下次 ':'等待首次检查')+(enabled&&!project?.paused&&schedule?scheduleStamp(scheduleTime(schedule),scheduleTimezone)+' · '+scheduleTimezone:'');
 }
 $('scan-summary').textContent=['light','issues'].map(kind=>t({light:'项目扫描',issues:'GitHub 问题同步',deep:'深度检查'}[kind])+' '+($(kind+'-due').textContent===t('未启用')?t('未启用'):t('每')+' '+$(kind+'-interval').textContent)).join(' · ')+' · '+t('项目时区')+' '+scheduleTimezone;
 document.querySelectorAll('[data-inspect]').forEach(button=>{const kind=button.dataset.inspect;button.disabled=!project||!!configError||(kind==='issues'&&!config.github.repository);});
 const activity=$('activity'),events=state.events.filter(e=>!e.project||e.project===projectId).slice(0,6);
 if(changedList(activity,events)){activity.replaceChildren();
 if(!events.length)activity.append(el('p',t('运行记录将在这里出现。'),'empty-copy'));
 for(const event of events){const copy=eventCopy(event),row=el('div',undefined,'activity-row');row.append(el('time',stamp(event.created)),el('span',copy));if(language!=='en'&&copy!==event.message){const raw=el('details');raw.append(el('summary',t('原文')),el('p',event.message));row.append(raw);}activity.append(row);}activity.append(action(t('查看活动历史'),showActivityHistory,'text-button'));}
 $('delivery-summary').textContent=t(config.delivery.mode==='local'?'当前交付：保存在本地，确认后创建 PR。':config.delivery.auto_merge?'当前交付：检查和评审通过后自动提交并合并。':'当前交付：检查通过后自动提交修改，等待合并。');
 if(project&&!readinessCache.has(project.id))safe(()=>checkReadiness());else renderReadiness();
 renderTelemetry();renderUpdate();if(['overview','tasks'].includes(view))renderTasks();if(view==='evolution')renderEvolution();if(view==='settings'){if(!settingsBaseline)renderSettings();window.RSIModelCenter?.show($('config-scope').value==='project'?projectId:null);}
}
function renderTelemetry(){
 const summary=$('quality-summary'),usage=$('usage-summary');summary.replaceChildren();usage.replaceChildren();if(loadedStateSections.analyticsProject!==(projectId||null)){summary.append(el('p',t('打开工作台查看运行统计。'),'muted'));usage.append(el('p',t('打开工作台查看用量统计。'),'muted'));return;}const quality=state.quality?.find(q=>q.project===projectId);
 const rate=(n,d)=>d?Math.round(n/d*100)+'% ('+n+'/'+d+')':t('暂无样本');
 for(const [label,value]of [['验收与评审通过率',rate(quality?.passed||0,quality?.evaluated||0)],['评审阻断率',rate(quality?.review_blocked||0,quality?.reviewed||0)],['重复失败任务',quality?.repeat_failures||0],['合并后验证通过',quality?.postmerge_verified||0],['合并后出现回归',quality?.postmerge_regressed||0]]){const card=el('div');card.append(el('small',t(label)),el('strong',String(value)));summary.append(card);}
 const usageScope=projectId||'model-center',rows=(state.model_usage||[]).filter(u=>u.project===usageScope);
 const total=(state.usage||[]).find(u=>u.project===usageScope);usage.append(el('p',t('近 24 小时')+' · '+(total?.calls||0)+' '+t('次调用')+' · '+Math.round(total?.seconds||0)+'s · '+(total?.tokens||0)+' tokens'));
 const estimate=total?.estimate==null?t('未知'):('$'+total.estimate.toFixed(4));usage.append(el('p',t('估算费用')+' · '+estimate+' USD · '+(total?.unpriced||0)+' '+t('次调用缺少价格'),'muted'));
 usage.append(el('p',t('新输入')+' '+(total?.input_tokens||0)+' · '+t('输出')+' '+(total?.output_tokens||0)+' · '+t('缓存读取')+' '+(total?.cache_read_tokens||0)+' · '+t('缓存写入')+' '+(total?.cache_write_tokens||0)+' tokens'));
 usage.append(el('p',t('用量未知的调用')+' '+(total?.unknown_usage||0)+' · '+t('用量只记录，不设额度；费用为估算。'),'muted'));
 if(rows.length){const table=el('table'),head=el('tr');for(const key of ['模型服务 / 模型','调用','tokens','失败','耗时','估算费用'])head.append(el('th',t(key)));const thead=el('thead');thead.append(head);table.append(thead);const body=el('tbody');for(const row of rows){const tr=el('tr');for(const value of [row.provider+' / '+row.model+' · '+t({opencode_session:'OpenCode 会话',codex_session:'Codex 会话',http_request:'HTTP 请求',unknown:'历史统计单位未知'}[row.unit]||'历史统计单位未知'),row.calls,row.tokens,row.failed,Math.round(row.seconds)+'s',row.estimate==null?t('未知'):'$'+row.estimate.toFixed(4)+(row.unpriced?' *':'')])tr.append(el('td',String(value)));body.append(tr);}table.append(body);const wrap=el('div',undefined,'usage-table');wrap.append(table);usage.append(wrap);}
}
function valueReport(value,reason,scope){
 const report=el('div',undefined,'value-report');if(reason)report.append(el('p',reason));
 if(value){for(const [key,label]of [['benefit','预期收益'],['cost','预计成本'],['inaction','不做的影响']])if(value[key]){const row=el('p');row.append(el('strong',t(label)+' · '),document.createTextNode(value[key]));report.append(row);}
 const levels=el('p',t('影响')+' '+t(reviewNames[value.impact]||value.impact)+' · '+t('工作量')+' '+t(reviewNames[value.effort]||value.effort)+' · '+t('信心')+' '+t(reviewNames[value.confidence]||value.confidence),'muted');report.append(levels);
 if(value.evidence?.length){const details=el('details'),list=el('ul');details.append(el('summary',t('判断证据')));for(const item of value.evidence)list.append(el('li',item));details.append(list);report.append(details);}}
 if(scope?.length)report.append(el('p',t('计划范围')+' · '+scope.join(', '),'muted'));return report;
}
async function confirmCandidateApproval(data){
 const content=el('div'),reason=el('textarea'),label=el('label',t('干预原因（可选，保留原文）'));
 reason.maxLength=2000;reason.rows=2;label.append(reason);
 const summary=window.RSIProduct?.approval(data);if(summary)content.append(summary);content.append(label);
 return await uiConfirm(t('这会把待验证改动分支推送到关联 GitHub 仓库，并创建 PR。确认批准？'),content)?{reason:reason.value}:null;
}
function writeNavigation(name){
 if(location.hash!==undefined&&location.pathname!==undefined){const navigation=new URLSearchParams(String(location.hash||'').replace(/^#/,''));navigation.set('view',name);if(projectId)navigation.set('project',projectId);else navigation.delete('project');const target=location.pathname+location.search+'#'+navigation;if(target!==location.pathname+location.search+location.hash)history.pushState(null,'',target);}
}
function showView(name){
 if(!state){toast(t('正在连接本地服务，请稍候再提交。'));return;}
 if(!['overview','tasks','evolution','settings','models','usage'].includes(name))return;
 const models=name==='models';name=name==='tasks'?'overview':models?'settings':name;
 navigationSequence++;view=name;if(models)window.RSIModelCenter?.connections();document.querySelectorAll('.view').forEach(node=>node.hidden=node.id!=='view-'+name);
 writeNavigation(models?'models':name);
 render();if(['overview','evolution'].includes(name))safe(refresh);if(name==='settings')window.RSIModelCenter?.show($('config-scope').value==='project'?projectId:null,true);window.scrollTo(0,0);
 if(name==='usage')window.RSIUsage?.show();
 if(models)$('view-models').scrollIntoView({block:'start'});
}
(function initialNavigation(){
 const route=new URLSearchParams(location.href?String(location.hash||'').replace(/^#/,''):''),requestedView=route.get('view'),requestedProject=route.get('project');
 if(requestedProject){projectId=requestedProject;}
 if(requestedView&&['overview','tasks','evolution','settings','models','usage'].includes(requestedView)){
  view=requestedView==='tasks'?'overview':requestedView==='models'?'settings':requestedView;
 }
})();
typeof window.addEventListener==='function'&&window.addEventListener('hashchange',()=>{
 if(!state)return;const route=new URLSearchParams(String(location.hash||'').replace(/^#/,'')),requested=route.get('view'),project=route.get('project');
 if(project&&state.projects.some(item=>item.id===project)&&project!==projectId){stashSettings();stashGoalDraft();closeTaskDetail();projectId=project;taskOffset=0;settingsBaseline=null;restoreGoalDraft();localWrite('rsi-project',projectId);}
 if(requested&&['overview','tasks','evolution','settings','models','usage'].includes(requested))showView(requested);
});
function renderTasks(){
 const list=$('tasks-list'),filter=$('task-filter').value;
 const waitingRetries=scoped(state.tasks).filter(task=>task.state==='queued'&&task.wait_reason!=='working_hours'&&task.available>Date.now()/1000).map(task=>task.id);
 if(!changedList(list,[filter,$('show-system-tasks').checked,taskOffset,state.selected_project,state.pagination,scoped(state.tasks),scoped(state.runs),waitingRetries]))return;
 list.replaceChildren();
 if(state.tasks.length>120){
  const pages=Math.ceil(state.pagination?.total/50);
  list.append(el('p',t('本次只渲染当前页，长历史通过分页加载。')+' · '+(Math.floor(taskOffset/50)+1)+'/'+Math.max(1,pages),'muted'));
 }
 document.querySelectorAll('[data-task-filter]').forEach(button=>{button.classList.toggle('active',button.dataset.taskFilter===filter);button.setAttribute('aria-pressed',String(button.dataset.taskFilter===filter));});
 const tasks=visibleTasks(scoped(state.tasks),filter,$('show-system-tasks').checked);
 window.RSIBatch?.toolbar();
 const candidate=state.selected_project&&state.selected_project!==projectId?null:state.pagination;
 const page=candidate&&candidate.offset===taskOffset&&(candidate.total===0?candidate.offset===0:candidate.offset<candidate.total)?candidate:null;
 $('tasks-prev').disabled=!taskOffset;$('tasks-next').disabled=!page?.has_more;$('tasks-page').textContent=page?(tasks.length?page.offset+1:0)+'–'+(tasks.length?Math.min(page.offset+tasks.length,page.total):0)+' / '+page.total:'';
 if(!tasks.length)list.append(el('p',t('这里暂无任务。其他分组中可能有历史记录。'),'empty-copy'));
 for(const task of tasks){
  const row=el('div',undefined,'task-row'),title=el('div'),actions=el('div',undefined,'task-actions');
  title.append(action(task.title,()=>showTask(task),'task-title'),el('span',t(sourceNames[task.source]||task.source)+' · '+stamp(task.updated),'task-meta'));
  if(task.wait_reason)title.append(el('p',window.RSIExperience?.queueCopy(task)||'','task-meta'));
  // 暂缓/不采纳的任务可能没有运行记录：理由回退到 task_decisions 的决定记录（批量初筛或规划）。
  const latest=scoped(state.runs).find(run=>run.task===task.id),
        decisionReason=['deferred','rejected'].includes(task.state)&&task.decision_reason?task.decision_reason:'',
        reason=latest?.reason||latest?.plan_reason||decisionReason;
  if(reason)title.append(el('p',latest?.failure?failureCopy(latest.failure):reason,'task-reason'));
  if(latest?.summary&&latest.summary!==reason)title.append(el('p',latest.summary,'task-result-summary'));
  const proof=[];if(latest?.evaluation_decision)proof.push(t('运行检查')+' '+t(statusNames[latest.evaluation_decision]||latest.evaluation_decision));if(latest?.review_decision)proof.push(t('代码评审')+' '+t(statusNames[latest.review_decision]||latest.review_decision));
  for(const [kind,label]of [['release','发布'],['deploy','部署']]){const operation=latest?.automation?.[kind];if(operation)proof.push(t(label)+' '+t(releaseStates[operation.state]||operation.state));}if(proof.length)title.append(el('p',proof.join(' · '),'task-meta'));

  if(['waiting_budget','blocked','failed','interrupted','cancelled','insufficient_evidence','deferred','rejected','expired','obsolete'].includes(task.state))actions.append(action(t('重试'),async()=>{await api('retry',{task:task.id});await refresh();},'text-button'));
  // 仅自动退避（有未来 available）的排队任务可提前重试；普通排队任务没有可用操作。
  else if(task.state==='queued'&&task.wait_reason!=='working_hours'&&task.available>Date.now()/1000)actions.append(action(t('立即重试'),async()=>{await api('retry',{task:task.id});await refresh();},'text-button'));
  actions.append(action(t('查看详情'),()=>showTask(task),'text-button'));
  if(!task.source.startsWith('system_')&&window.RSIBatch)row.append(window.RSIBatch.row(task));
  row.append(title,badge(task.display_state||task.state),actions);list.append(row);
 }
}
let detailTask,detailSequence=0;
function closeTaskDetail(){detailSequence++;detailTask=null;if($('detail-dialog').open)$('detail-dialog').close();}
function taskOutcome(data){
 const result=data.result,outcome=el('section',undefined,'task-outcome');outcome.append(el('h3',t('这次的结果')));
 outcome.append(el('h4',t('改了什么')),el('p',result.summary||result.reason||t('还没有完成结果。')));
 if(result.value){const reason=el('details',undefined,'outcome-value');reason.open=true;reason.append(el('summary',t('为什么值得做')),valueReport(result.value,result.plan_reason,result.scope));outcome.append(reason);}
 if(result.failure){outcome.append(el('p',failureCopy(result.failure),'form-error'));if(window.RSIProduct)outcome.append(window.RSIProduct.raw({failure:result.failure,reason:result.reason},'查看原始诊断'));}
 if(result.files?.length)outcome.append(el('p',t('修改文件')+' · '+result.files.join(', '),'muted'));
 for(const [key,title]of [['evaluation','运行检查'],['review','代码评审']]){const row=el('p');row.append(el('strong',t(title)+' · '),result[key]?badge(result[key].decision):document.createTextNode(t('尚未完成')));outcome.append(row);}
 const next=result.delivery_status?t(deliveryNames[result.delivery_status]||result.delivery_status):t(statusNames[data.state]||data.state);
 outcome.append(el('p',t('交付状态')+' · '+next));
 const guidance={awaiting_approval:'修改保存在本机；确认后可推送并创建 PR。',pr_open:'可以打开 GitHub 查看合并进度。',merged:'修改已合并；合并后的运行情况会继续复查。',failed:'查看检查结果，解决问题后可以重试。',blocked:'按错误提示调整设置后重试。',insufficient_evidence:'需要补充能证明问题已解决的检查。'}[data.state];
 if(guidance)outcome.append(el('p',t(guidance),'muted'));
 if(result.failure){const target=['credentials','provider'].includes(result.failure.code)?'models':'settings';outcome.append(el('p',t(failureNext[result.failure.code]||failureNext.execution),'muted'));outcome.append(action(t(target==='models'?'修复模型连接':'检查项目设置'),()=>{$('detail-dialog').close();showView(target);},'text-button'));}
 if(window.RSIProduct){outcome.append(window.RSIProduct.checkList(result.evaluation?.checks));if(data.state==='awaiting_approval')outcome.append(window.RSIProduct.approval(data));}outcome.append(stageProgress(data));return outcome;
}
async function showTask(task,restoreView,queueDraft){if(queueDraft===undefined&&$('detail-dialog').open&&detailTask?.id===task.id&&detailTask?.project===task.project&&projectId===task.project)queueDraft=window.RSIExperience?.queueDraft();const ticket=++detailSequence,project=projectId;detailTask=task;$('detail-title').textContent=task.title;const body=$('detail-body');body.replaceChildren(badge(task.state));
 // A request can finish after the user closed the previous detail view.
 // Only show the dialog after checking that this exact request is still current.
 const stillCurrent=()=>ticket===detailSequence&&projectId===project&&detailTask?.id===task.id;
 if(!$('detail-dialog').open)$('detail-dialog').showModal();
 window.RSIExperience?.taskOpened(task,queueDraft);
 const evidence=el('details',undefined,'detail-section');let evidenceText=task.evidence||'';try{evidenceText=JSON.stringify(JSON.parse(task.evidence),null,2);}catch{}evidence.append(el('summary',t('来源与证据')),el('p',task.body,'muted'),el('pre',evidenceText));body.append(evidence);
 const history=await api('task-runs?task='+encodeURIComponent(task.id)+'&project='+encodeURIComponent(project)+'&limit=50');if(ticket!==detailSequence||projectId!==project)return;
 async function loadRun(run,index,section){const data=await api('run?id='+run.id);if(ticket!==detailSequence||projectId!==project)return;
  section.replaceChildren();
  if(index===0){const outcome=taskOutcome(data);outcome.id='live-task-outcome';body.insertBefore(outcome,evidence);window.RSIExperience?.enhanceOutcome(outcome,data);await renderReleaseOutcome(outcome,run,project);if(ticket!==detailSequence||projectId!==project)return;}
  section.append(el('summary',t(index===0?'检查过程与运行记录':'以前的运行')+' · '+run.id),el('p',data.result.failure?failureCopy(data.result.failure):data.result.reason,'muted'));
  if(index>0)section.append(stageProgress(data));
  const raw=el('details');raw.append(el('summary',t('原始运行记录')),el('pre',JSON.stringify({models:data.config.models,calls:data.calls,tokens:data.tokens,base:data.base,result:data.result},null,2)));section.append(raw);
  if(data.result.value){const report=el('details',undefined,'review-report');report.append(el('summary',t('为什么值得做')),valueReport(data.result.value,data.result.plan_reason,data.result.scope));section.append(report);}
  if(data.result.review){
   const review=data.result.review,report=el('details',undefined,'review-report');report.append(el('summary',t('代码评审')),badge(review.decision),el('p',review.reason));
   for(const assessment of review.assessments||[]){const row=el('p');row.append(badge(assessment.decision),el('strong',' '+t(reviewNames[assessment.area]||assessment.area)+' · '),document.createTextNode(assessment.reason));report.append(row);}
   for(const finding of review.findings||[]){const card=el('div',undefined,'provider-card');card.append(el('strong',t(reviewNames[finding.severity]||finding.severity)+' · '+finding.title),el('p',finding.path+':'+finding.line),el('p',finding.reason),el('p',t('修改建议')+t('：')+finding.suggestion));report.append(card);}
   section.append(report);
  }
  if(data.result.evaluation){const report=el('details',undefined,'review-report');report.append(el('summary',t('运行与效果检查')),badge(data.result.evaluation.decision));for(const [label,rows] of Object.entries(data.result.evaluation.checks||{})){if(!Array.isArray(rows))continue;const detail=el('details');detail.append(el('summary',t(evaluationNames[label]||label)+' · '+rows.filter(r=>r.passed).length+'/'+rows.length),el('pre'));detail.addEventListener('toggle',()=>{if(detail.open&&!detail.dataset.loaded){detail.dataset.loaded='true';detail.querySelector('pre').textContent=JSON.stringify(rows,null,2);}});report.append(detail);}section.append(report);}
  if(data.result.delivery_status)section.append(el('p',t('交付状态')+' · '+t(deliveryNames[data.result.delivery_status]||data.result.delivery_status)));
  if(data.result.feedback){section.append(el('h3',t('合并后收益反馈')),badge(data.result.feedback.state),el('p',stamp(data.result.feedback.checked_at)+' · '+data.result.feedback.head));}
  const delivery=el('div',undefined,'task-actions');
  if(data.result.files){delivery.append(action(t('阅读代码差异'),()=>window.RSIExperience?.showDiff(run.id),'text-button'));if(!window.RSIDesktop){const link=el('a',t('查看具体代码修改 ↗'));link.href='/api/patch?id='+run.id;link.target='_blank';link.rel='noreferrer';delivery.append(link);}}
  let delivering=false;if(run.state==='awaiting_approval')delivery.append(action(t('批准推送并创建 PR'),async(evt)=>{if(delivering)return;const approval=await confirmCandidateApproval(data);if(!approval)return;delivering=true;if(evt?.target)evt.target.disabled=true;try{await api('deliver',{run:run.id,...approval});toast(currentProject()?.paused?t('交付任务已加入；启用项目后执行'):t('交付任务已加入'));$('detail-dialog').close();await refresh();}finally{delivering=false;if(evt?.target)evt.target.disabled=false;}}));
  if(data.result.pr){run.pr=data.result.pr;const link=el('a',t('打开 GitHub PR ↗'));if(/^https:\/\/github\.com\//.test(run.pr)){link.href=run.pr;link.target='_blank';link.rel='noreferrer';delivery.append(link);}}
  if(['pr_open','merged'].includes(run.state))delivery.append(action(t('撤销这次修改（先检查再提交）'),async()=>{await api('revert-code',{run:run.id});toast(t('恢复旧版本任务已加入；通过检查后才可交付'));$('detail-dialog').close();await refresh();}));
  if(index===0)body.insertBefore(delivery,evidence);else section.append(delivery);
  restoreView?.(body);
  }
 for(const [index,run] of history.entries()){const section=el('details',undefined,'detail-section');section.dataset.run=run.id;section.append(el('summary',t(index===0?'检查过程与运行记录':'以前的运行')+' · '+run.id));body.append(section);
  // 加载失败不能让区块永久空白：失败后在区块内给出错误项与重试入口；进行中的请求不重复发起。
  let loaded=false,loading=false;
  const load=async()=>{if(loading||loaded)return;loading=true;
   try{await loadRun(run,index,section);loaded=true;}
   catch(error){
    if(ticket!==detailSequence||projectId!==project)return;
    section.replaceChildren(section.firstChild,el('p',t('运行记录加载失败。'),'form-error'),action(t('重试'),load,'text-button'));
   }finally{loading=false;}};
  section.addEventListener('toggle',()=>{if(section.open&&!loaded)load();});
  if(index===0)await load();}

 if(ticket!==detailSequence||projectId!==project)return;
 // 与 runtime.cancel_task 的 cancellable 状态保持一致；待批准的候选取消前需确认后果。
 if(['queued','planning','executing','evaluating','blocked','waiting_budget','deferred','insufficient_evidence','failed','interrupted','awaiting_approval'].includes(task.state))body.append(action(t('取消任务'),async()=>{
  if(ticket!==detailSequence||projectId!==project)return;
  const plan=await api('task-cancel-preview?id='+encodeURIComponent(task.id)+'&project='+encodeURIComponent(project));
  if(ticket!==detailSequence||projectId!==project)return;
  const preview=el('div'),cascade=el('input'),reason=el('textarea');cascade.type='checkbox';reason.maxLength=2000;reason.rows=2;
  preview.append(el('p',t('取消后待批准的候选将被撤销，需要重新验证才能再次交付；已创建的 PR 不受影响。确认取消？')));
  if(plan.affected.length){preview.append(el('p',t('以下后继任务依赖本任务；默认只取消当前任务。')));for(const item of plan.affected)preview.append(el('p',item.title+' · '+t(statusNames[item.state]||item.state)));if(!plan.truncated){const label=el('label',t('同时取消可取消的后继任务'));label.prepend(cascade);preview.append(label);}}
  if(plan.truncated)preview.append(el('p',t('后继过多，预览仅展示前 999 项；本次只能取消当前任务。')));
  const reasonLabel=el('label',t('干预原因（可选，保留原文）'));reasonLabel.append(reason);preview.append(reasonLabel);
  if(!(await uiConfirm(t('确认取消任务及所选后继？'),preview)))return;
  await api('cancel',{task:task.id,cascade:cascade.checked,revision:plan.revision,reason:reason.value});$('detail-dialog').close();await refresh();}));
 restoreView?.(body);
 if(stillCurrent()&&!$('detail-dialog').open)$('detail-dialog').showModal();}
const evolutionReasons={
 "Need independent validation/holdout cases before proposing":"需要先导入独立的验证集和最终评估集任务，才能提出改进方案。",
 "Validation or holdout exposure limit reached; import new cases or raise evolution.max_holdout_uses":"验证集或最终评估集的曝光次数已达上限；请导入新的评估任务，或在项目配置中调高 evolution.max_holdout_uses。",
 "Too few independent, repeated improvements for promotion":"独立用例的重复改进证据不足，暂不能晋升。",
 "Incomplete paired trials":"配对试验不完整。",
 "No demonstrated improvement on each evaluated split":"每个评估集合都没有显示出改进。",
 "Both validation and holdout are required":"需要同时覆盖验证集和最终评估集。",
 "Candidate regressed on previously passing cases":"候选在原本通过的用例上出现退步。",
 "Repeated paired improvement on both splits without observed regressions":"验证集和最终评估集均重复显示改进，且未观察到退步。",
 "Paired quality improved without observed regressions; usage recorded separately":"配对质量有所提升且未观察到退步；用量单独记录。"
};
function evolutionReason(reason){return evolutionReasons[reason]?t(evolutionReasons[reason]):reason;}
function experimentEvidence(card,c){
 const comparison=c.comparison||{}, ratio=value=>typeof value==='number'&&Number.isFinite(value)?value.toFixed(2)+'×':t('不可比较');
 card.append(el('p',t('Token 用量比')+' · '+ratio(comparison.token_ratio)+' / '+t('延迟比')+' · '+ratio(comparison.latency_ratio),'muted'));
 if(!comparison.comparable)card.append(el('p',t('缺失用量或零基线时不可比较成本；质量通过不代表更便宜。'),'muted'));
 const detail=el('details');detail.append(el('summary',t('查看实验依据与分组结果')));let loading=false,loaded=false;
 detail.addEventListener('toggle',()=>{if(!detail.open||loading||loaded)return;safe(async()=>{
  loading=true;
  try{
   const data=await api('candidate?id='+encodeURIComponent(c.id)+'&project='+encodeURIComponent(c.project));
   if(!detail.isConnected)return;
   const report=data.comparison, cohorts=el('div');
   for(const row of report.cohorts){const source={real:t('真实任务'),synthetic:t('合成任务')}[row.source]||t('未知'),split={validation:t('验证集'),holdout:t('最终评估集'),development:t('开发集')}[row.split]||t('未知'),size={small:t('短任务'),medium:t('中等任务'),large:t('长任务')}[row.size]||t('未知');cohorts.append(el('p',source+' · '+split+' · '+size+' / '+t('独立用例')+': '+row.families+' / '+t('胜 / 负 / 平')+': '+row.wins+' / '+row.losses+' / '+row.ties));}
   cohorts.append(el('p',t('分组结果仅描述已观察样本，不代表对其他任务的收益。')));
   detail.append(cohorts);
   const evidence=el('details'),raw=el('pre');evidence.append(el('summary',t('封存计划与输入清单')),raw);
   evidence.addEventListener('toggle',()=>{if(evidence.open&&!raw.textContent)raw.textContent=JSON.stringify({preregistered_plan:data.evidence.preregistered_plan||null,dataset:data.evidence.dataset||null,config_hash:data.evidence.config_hash||null},null,2);});
   const download=el('button',t('导出实验依据'),'text-button');download.type='button';download.onclick=()=>{const url=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:'application/json'})),link=el('a');link.href=url;link.download='adaptive-rsi-experiment-'+c.id+'.json';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);};
   detail.append(evidence,download);loaded=true;
  }finally{loading=false;}
 });});card.append(detail);
}
function renderEvolution(){const summary=$('suite-summary');summary.replaceChildren();if(loadedStateSections.analyticsProject===(projectId||null)){const limit=Number(currentProject()?.config?.evolution?.max_holdout_uses??state.config?.evolution?.max_holdout_uses);for(const [split,title,description]of [['development',t('开发集'),t('用于提出和调整待验证改动')],['validation',t('验证集'),t('配对比较 · 受限复用')],['holdout',t('最终评估集'),t('隐藏任务 · 限制曝光次数')]]){const row=(state.cases||[]).find(c=>c.project===projectId&&c.split===split),count=row?.count||0,uses=row?.uses||0,usage=split!=='development'&&count>0&&Number.isFinite(limit)&&limit>0?t('已用 ')+uses+'/'+(count*limit)+' · ':'',card=el('article',undefined,'suite-card');card.append(el('strong',count),el('span',title),el('small',usage+description));summary.append(card);}}else summary.append(el('p',t('验证统计加载中。'),'muted'));
 $('experiment-readiness').textContent=t('改进方案需通过独立样本的前后对照验证；调用与 tokens 只记录用量。');
 const list=$('candidates');list.replaceChildren();if(loadedStateSections.candidatesProject!==(projectId||null)){list.append(el('p',t('正在加载改进方案…'),'empty-copy'));return;}const candidates=scoped(state.candidates||[]);if(!candidates.length)list.append(el('p',t('还没有改进方案。积累运行记录后会自动提出，也可以手动发起。'),'empty-copy'));
 for(const c of candidates){const card=el('article',undefined,'panel candidate-card'),top=el('div',undefined,'panel-title'),independent=c.independent_cases??c.trial_count??0,trials=c.trial_count??independent;top.append(el('h2',{strategy:t('执行策略'),meta_strategy:t('规划方法'),adapter:t('项目执行说明')}[c.kind]||c.kind),badge(c.status));card.append(top,el('div',(c.parent?c.parent.slice(0,8):'BASELINE')+' → '+c.id.slice(0,8),'lineage'),el('p',c.content.guidance),el('p',evolutionReason(c.reason),'muted'),el('small',independent+t(' 个独立用例 · 共 ')+trials+t(' 次配对运行 · ')+stamp(c.created),'muted'));
  experimentEvidence(card,c);
  if(c.status==='stable')card.append(action(t('恢复到上一个已验证版本'),async()=>{await api('rollback',{candidate:c.id});toast(t('策略已恢复旧版本'));await refresh({force:true});}));
  if(c.status==='ready')card.append(action(t('启用这个策略'),async()=>{if(!(await uiConfirm(t('启用后会替换当前稳定策略，并开始有限监控；出现回归时可以回滚。确认启用？'))))return;await api('evolution/promote',{candidate:c.id});toast(t('策略已启用'));await refresh({force:true});}));
  list.append(card);}}
function getPath(obj,path){return path.split('.').reduce((v,k)=>v?.[k],obj);}
function setPath(obj,path,value){const keys=path.split('.');let node=obj;keys.slice(0,-1).forEach(k=>node=node[k]??={});node[keys.at(-1)]=value;}
function deepMerge(a,b){for(const [key,value]of Object.entries(b)){if(value&&typeof value==='object'&&!Array.isArray(value))a[key]=deepMerge(a[key]||{},value);else a[key]=value;}return a;}
function settingsKey(){return $('config-scope').value==='global'?'global':projectId;}
function sensitiveField(node){return node.type==='password'||/(?:api.?key|oauth|secret|password|credential|authorization|token)/i.test([node.id,node.name,node.dataset.path,node.autocomplete].join(' '));}
function settingsValues(){return [...$('settings-form').querySelectorAll('input,textarea,select')].filter(node=>!sensitiveField(node)&&!node.hasAttribute('data-unit-for')&&!node.hasAttribute('data-check-field')&&!node.hasAttribute('data-check-arg')).map(node=>({id:node.id,path:node.dataset.path,value:node.value,checked:node.checked,unit:node.dataset.duration?document.querySelector('[data-unit-for="'+node.dataset.duration+'"]').value:undefined}));}
function draftSettingPath(path){return !!path&&!sensitiveField({id:'',name:'',dataset:{path},autocomplete:''})&&path!=='delivery.deploy.health_url';}
function persistentSettingsValues(){const values=[...$('settings-form').querySelectorAll('[data-path]')].filter(node=>draftSettingPath(node.dataset.path)).map(node=>({id:node.id,path:node.dataset.path,value:node.value,checked:node.checked,unit:node.dataset.duration?document.querySelector('[data-unit-for="'+node.dataset.duration+'"]').value:undefined}));for(const id of ['work-start','work-end','issue-labels']){const node=$(id);values.push({id,value:node.value,checked:node.checked});}values.push({id:'schedule-mode',value:$('schedule-mode').value});return values;}
function persistableDraftEntry(entry){return !!(entry.path&&draftSettingPath(entry.path)||['schedule-mode','work-start','work-end','issue-labels'].includes(entry.id));}
function settingBaselineValue(entry,baseline,mode){
 if(entry.path)return getPath(baseline,entry.path);
 if(entry.id==='work-start')return baseline.schedule.working_hours[0];
 if(entry.id==='work-end')return baseline.schedule.working_hours[1];
 if(entry.id==='issue-labels')return baseline.github.labels;
 if(entry.id==='checks-json')return baseline.checks;
 if(entry.id==='schedule-mode')return settingsModes.get(baseline)??mode;
 if(entry.id==='delivery-choice')return [baseline.delivery.mode,baseline.delivery.auto_merge];
 if(entry.id==='advanced-json')return baseline;
 return undefined;
}
function sameValue(a,b){return JSON.stringify(a)===JSON.stringify(b);}
function acknowledgedSettingsValues(values,config,mode){
 return values.map(entry=>{const saved={...entry},value=settingBaselineValue(entry,config,mode);
  if(entry.path){if(typeof value==='boolean')saved.checked=value;else if(value!==undefined)saved.value=String(entry.unit?value/Number(entry.unit):value);}
  else if(['work-start','work-end'].includes(entry.id))saved.value=String(value);
  else if(entry.id==='issue-labels'){if(!sameValue(entry.value.split(',').map(x=>x.trim()).filter(Boolean),value))saved.value=value.join(', ');}
  else if(entry.id==='checks-json'){try{if(!sameValue(JSON.parse(entry.value),value))saved.value=JSON.stringify(value,null,2);}catch{saved.value=JSON.stringify(value,null,2);}}
  else if(entry.id==='schedule-mode')saved.value=mode;
  else if(entry.id==='delivery-choice')saved.value=config.delivery.mode==='pr'?(config.delivery.auto_merge?'auto':'pr'):config.delivery.auto_merge?'custom':'local';
  else if(entry.id==='advanced-json')saved.value='';
  return saved;
 });
}
function writePersistentSettingsDraft(scope,values,baseline,mode){
 const clean=JSON.parse(settingsClean.get(scope)||'[]'),cleanByPath=new Map(clean.map(v=>[v.path||v.id,v]));
 const saved=values.filter(v=>persistableDraftEntry(v)&&!sameValue(v,cleanByPath.get(v.path||v.id))).map(v=>({...v,baselineValue:settingBaselineValue(v,baseline,mode)}));
 if(saved.length)draftStore.set('settings',scope,{values:saved,mode});else draftStore.remove('settings',scope);
}
function finishPendingSettingsSave(key){
 if(!pendingSettingsSaves.delete(key))return;
 const draft=settingsDrafts.get(key);
 if(draft&&JSON.stringify(draft.values)===settingsClean.get(key)){settingsDrafts.delete(key);draftStore.remove('settings',key);}
}
function stashSettings(){
 if(!settingsBaseline||!settingsContext)return;
 const values=settingsValues();
 // The old clean value can become an unsaved edit when an in-flight save is acknowledged.
 if(!pendingSettingsSaves.has(settingsContext)&&JSON.stringify(values)===settingsClean.get(settingsContext)){settingsDrafts.delete(settingsContext);draftStore.remove('settings',settingsContext);return;}
 const mode=$('schedule-mode').value,baselineValues=Object.fromEntries(values.map(entry=>[entry.path||entry.id,settingBaselineValue(entry,settingsBaseline,mode)]));
 settingsDrafts.set(settingsContext,{baseline:structuredClone(settingsBaseline),baselineValues,mode,values});
 writePersistentSettingsDraft(settingsContext,persistentSettingsValues(),settingsBaseline,mode);
}
function restoreSettingsDraft(){
 const key=settingsKey(),memory=settingsDrafts.get(key),persisted=memory?null:draftStore.get('settings',key),draft=memory||persisted;if(!draft){renderSettingsConflicts(key);return;}
 const currentMode=settingsModes.get(settingsBaseline),restored=[],conflicting=[];let conflicts=0;
 for(const saved of draft.values||[]){const node=saved.id?$(saved.id):$('settings-form').querySelector('[data-path="'+saved.path+'"]');if(!node)continue;
  const oldBase=memory?draft.baselineValues?.[saved.path||saved.id]:saved.baselineValue,currentBase=settingBaselineValue(saved,settingsBaseline,currentMode);
  // A navigation GET can observe this save before its POST acknowledgement.
  // Accept only the submitted baseline for this field; unrelated changes still conflict.
  const pending=memory&&pendingSettingsSaves.get(key);
  if(!sameValue(oldBase,currentBase)&&!(pending&&sameValue(currentBase,settingBaselineValue(saved,pending.config,pending.mode)))){conflicts++;if(persistableDraftEntry(saved))conflicting.push({local:saved,server:currentBase});continue;}
  node.value=saved.value;node.checked=saved.checked;if(saved.unit&&saved.path){const unit=document.querySelector('[data-unit-for="'+saved.path+'"]');if(unit){unit.value=saved.unit;unit.dataset.previous=saved.unit;}}restored.push(saved);
 }
 document.querySelectorAll('[data-unit-for]').forEach(unit=>{unit.dataset.previous=unit.value;const node=document.querySelector('[data-path="'+unit.dataset.unitFor+'"]');node.min=5/Number(unit.value);node.max=525600/Number(unit.value);});
 if(restored.length){const values=settingsValues(),mode=$('schedule-mode').value,baselineValues=Object.fromEntries(values.map(entry=>[entry.path||entry.id,settingBaselineValue(entry,settingsBaseline,mode)]));settingsDrafts.set(key,{baseline:structuredClone(settingsBaseline),baselineValues,mode,values});writePersistentSettingsDraft(key,persistentSettingsValues(),settingsBaseline,mode);}
 else if(persisted)draftStore.remove('settings',key);
 if(conflicting.length)draftStore.set('settings-conflicts',key,conflicting);
 renderSettingsConflicts(key);
 if(conflicts)toast(t('服务器设置已变化，冲突字段未从旧草稿恢复。'),true);
}
function renderSettingsConflicts(key){
 $('settings-conflicts')?.remove();
 const entries=draftStore.get('settings-conflicts',key)||[];
 if(!Array.isArray(entries)||!entries.length)return;
 const panel=el('details',undefined,'review-report');panel.id='settings-conflicts';panel.open=true;
 panel.append(el('summary',t('设置冲突：保留的本地修改')));
 const resolve=(entry,local)=>{if(settingsKey()!==key)return;
  {const saved=local?entry.local:acknowledgedSettingsValues([entry.local],settingsBaseline,settingsModes.get(settingsBaseline))[0],node=saved.id?$(saved.id):$('settings-form').querySelector('[data-path="'+saved.path+'"]');if(!node)return;node.value=saved.value;node.checked=saved.checked;if(saved.unit&&saved.path){const unit=document.querySelector('[data-unit-for="'+saved.path+'"]');if(unit){unit.value=saved.unit;unit.dataset.previous=saved.unit;}}stashSettings();}
  const remaining=entries.filter(item=>item!==entry);if(remaining.length)draftStore.set('settings-conflicts',key,remaining);else draftStore.remove('settings-conflicts',key);renderSettingsConflicts(key);
 };
 for(const entry of entries){const row=el('div',undefined,'provider-card');row.append(el('strong',entry.local.path||entry.local.id),el('p',t('服务端值')+t('：')+JSON.stringify(settingBaselineValue(entry.local,settingsBaseline,settingsModes.get(settingsBaseline)))),el('pre',JSON.stringify(entry.local,null,2)),action(t('使用本地修改'),()=>resolve(entry,true),'text-button'),action(t('采用服务端值'),()=>resolve(entry,false),'text-button'));panel.append(row);}
 $('settings-form').prepend(panel);
}
function durationUnit(node){return node.dataset.duration?Number(document.querySelector('[data-unit-for="'+node.dataset.duration+'"]').value):1;}
function fieldValue(node){
 let value=node.type==='checkbox'?node.checked:node.type==='number'?Number(node.value)*durationUnit(node):node.dataset.lines?node.value.split('\n').map(line=>line.trim()).filter(Boolean):node.value;
 if(node.dataset.duration&&Math.abs(value-Math.round(value))<1e-7)value=Math.round(value);
 if(node.dataset.duration&&(!Number.isInteger(value)||value<5||value>525600))throw Error(t('检查间隔须为 5 分钟至 365 天，且精确到整分钟。'));
 return value;
}
function syncDelivery(){
 const deploying=document.querySelector('[data-path="delivery.deploy.enabled"]')?.checked;
 $('deploy-health-settings').hidden=!deploying;
 for(const path of ['health_url','health_urls','health_attempts','health_consecutive_successes','health_interval_seconds','health_warmup_seconds','automatic_rollback']){const field=document.querySelector('[data-path="delivery.deploy.'+path+'"]');if(field)field.disabled=!deploying||$('config-scope').value!=='project'||!projectId;}
 const mode=document.querySelector('[data-path="delivery.mode"]').value,auto=document.querySelector('[data-path="delivery.auto_merge"]').checked;
 $('delivery-choice').value=mode==='pr'?(auto?'auto':'pr'):auto?'custom':'local';
 $('delivery-choice').disabled=$('config-scope').value!=='project'||!projectId;
 $('delivery-field').hidden=$('delivery-choice').disabled;
 $('delivery-project-settings').hidden=!$('delivery-choice').disabled||!projectId;
 $('release-project-note').hidden=!$('delivery-choice').disabled;$('release-project-fields').hidden=$('delivery-choice').disabled;
 $('delivery-note').textContent=t($('delivery-choice').disabled?'通用设置不能替所有项目授权提交代码，请切换到具体项目设置。':{local:'修改完成后只保存在本机，不会上传到 GitHub。你确认后再提交。',pr:'检查通过后会在 GitHub 创建修改申请（PR），由你决定何时合并。',auto:'检查通过后会提交并自动合并；GitHub 要求的检查和审核仍必须通过。',custom:'保留原来的特殊设置。选择其他选项后，才会改变提交方式。'}[$('delivery-choice').value]);
}
function changeSettingsScope(scope){
 if(!state)return;
 scopeChosen=true;
 stashSettings();$('config-scope').value=scope;settingsBaseline=null;
 if(view==='models')renderNavigation();
 else renderSettings();renderNavigation();window.RSIModelCenter?.show(scope==='project'?projectId:null);
}
const schedulePaths=['schedule.light_minutes','schedule.issues_minutes','sources.inspection','sources.issues'];
function syncScheduleMode(){
 const global=$('config-scope').value==='global',inherit=!global&&$('schedule-mode').value==='inherit';
 $('schedule-mode-field').hidden=global;
 for(const path of schedulePaths){const node=document.querySelector('[data-path="'+path+'"]');node.disabled=inherit;
  if(inherit){const value=getPath(state.config,path);if(node.type==='checkbox')node.checked=value;else{const unit=document.querySelector('[data-unit-for="'+path+'"]'),factor=value%1440===0?1440:value%60===0?60:1;unit.value=String(factor);unit.dataset.previous=String(factor);node.value=value/factor;node.min=5/factor;node.max=525600/factor;}}
 }
 document.querySelectorAll('[data-unit-for], [data-schedule-preset]').forEach(node=>node.disabled=inherit);
 syncSchedulePresets();
}
function syncSchedulePresets(){
 document.querySelectorAll('[data-schedule-preset]').forEach(select=>{const input=document.querySelector('[data-path="schedule.'+select.dataset.schedulePreset+'_minutes"]'),minutes=Number(input.value)*durationUnit(input);select.value=[...select.options].some(o=>o.value===String(minutes))?String(minutes):'custom';});
}
function renderSettings(){settingsGeneration++;const feedback=$('settings-form').querySelector('[data-form-error]');if(feedback)feedback.hidden=true;settingsBaseline=structuredClone($('config-scope').value==='global'?state.config:(currentProject()?.config||state.config));
 document.querySelectorAll('[data-path]').forEach(node=>{const value=getPath(settingsBaseline,node.dataset.path);if(node.type==='checkbox')node.checked=value;else node.value=node.dataset.lines&&Array.isArray(value)?value.join('\n'):value??'';if(node.dataset.duration){const factor=value%1440===0?1440:value%60===0?60:1;document.querySelector('[data-unit-for="'+node.dataset.duration+'"]').value=String(factor);document.querySelector('[data-unit-for="'+node.dataset.duration+'"]').dataset.previous=String(factor);node.value=value/factor;node.min=5/factor;node.max=525600/factor;}});
 document.querySelector('[data-path="limits.concurrency"]').disabled=$('config-scope').value!=='global';
 document.querySelectorAll('[data-path^="delivery."]').forEach(node=>node.disabled=$('config-scope').value!=='project'||!projectId);
 document.querySelectorAll('[data-path^="updates."]').forEach(node=>node.disabled=$('config-scope').value!=='global');
 renderUpdate();
 $('schedule-mode').value=schedulePaths.some(path=>getPath(currentProject()?.overrides,path)!==undefined)?'custom':'inherit';
 settingsModes.set(settingsBaseline,$('schedule-mode').value);
 $('work-start').value=settingsBaseline.schedule.working_hours[0];$('work-end').value=settingsBaseline.schedule.working_hours[1];$('issue-labels').value=settingsBaseline.github.labels.join(', ');$('checks-json').value=JSON.stringify(settingsBaseline.checks,null,2);$('advanced-json').value='';settingsContext=settingsKey();syncDelivery();syncScheduleMode();settingsClean.set(settingsContext,JSON.stringify(settingsValues()));restoreSettingsDraft();syncDelivery();syncScheduleMode();window.RSIProduct?.renderEditor();window.RSIProduct?.dirtyStatus();
 safe(renderCapabilities);safe(loadReleaseWorkflows);}
async function renderCapabilities(){
 const ticket=++capabilitySequence,project=projectId,target=$('capabilities');target.replaceChildren();if(!project){target.append(el('p',t('接入项目后识别运行环境。'),'muted'));return;}const capabilities=await api('capabilities?project='+encodeURIComponent(project));if(ticket!==capabilitySequence||project!==projectId)return;if(!capabilities.modules.length)target.append(el('p',t('未识别到标准构建清单。可手动配置验收命令。'),'muted'));for(const module of capabilities.modules){const row=el('div',undefined,'capability-row');row.append(el('strong',module.language),el('code',module.manifest),el('span',{executable:t('检查可执行 · 已验证'),unverified:t('待隔离验证'),analysis_only:t('可分析 · 需配置检查'),environment_blocked:t('环境缺少工具')}[module.status],'badge '+(module.status==='environment_blocked'?'warn':module.status==='executable'?'good':'neutral')));target.append(row);}
}

const readinessNames={checks:'项目检查命令',worker:'模型执行环境',sandbox:'独立运行环境',dependencies:'项目依赖',tools:'执行工具',model:'模型连接配置',budget:'模型服务用量'};
const failureMessages={checks:'代码检查没有通过，请查看运行与效果检查。',review:'代码评审发现了需要修改的问题，请查看评审建议。',improvement:'修改后的检查通过了，但还缺少解决问题的前后对照证据。',budget:'模型服务暂未提供本次运行所需的用量，请检查连接后重试。',credentials:'模型认证失效，请检查模型连接设置。',provider:'模型服务暂不可用，请稍后重试。',environment:'运行条件不完整，请查看运行准备检查。',baseline:'项目内容已更新，请基于最新版本重新执行。',execution:'这次执行没有完成，请展开运行详情查看原因。'};
const failureNext={checks:'先查看失败的检查项，修正代码或补充验收条件后重试。',review:'先处理评审指出的问题，再让系统重新检查和评审。',improvement:'补充能实际运行、能证明问题已解决的验收检查。',budget:'检查模型连接和用量设置，恢复后再重试。',credentials:'检查模型连接或刷新登录凭据。',provider:'稍后重试；如果是持续故障，请切换备用模型。',environment:'检查项目依赖、执行工具和独立运行环境。',baseline:'基于最新主线重新执行，不要继续使用旧候选。',execution:'展开原始运行记录，按失败步骤修正后重试。'};
const stopMessages={output_limit:'模型输出达到上限，请检查输出限制后重试。',safety:'模型服务的安全策略中止了响应，请查看原始诊断并核对请求。',refusal:'模型拒绝了本次请求，请查看诊断并核对模型能力与服务策略。',paused:'模型暂停了本次响应，请查看运行诊断。',stop_sequence:'响应被停止序列提前截断，请检查模型设置。',text_only:'模型只返回了文本，未返回所需工具调用，请检查模型的工具能力。'};
function failureCopy(problem){return t(stopMessages[problem.stop_reason]||failureMessages[problem.code]||failureMessages.execution);}
function readinessDetail(item){
 const missing=Array.isArray(item.missing)?item.missing:[],model=item.provider&&item.model?item.provider+' / '+item.model+' · '+(item.effort||'default')+t('：'):'';
 const compatibility=String(item.reason||'').match(/^opencode_incompatible_(\d+\.\d+\.\d+)$/);
 if(compatibility)return t('已检测到 OpenCode ')+compatibility[1]+t('，当前命令参数不兼容。请升级 Adaptive RSI，或临时使用 OpenCode 1.19.3。');
 const reasons={
  checks_missing:'尚未配置项目检查命令，请先在设置中添加。',
  sandbox_unavailable:'没有可用的独立运行环境，请检查运行环境设置。',
  tools_missing:'缺少执行工具：',
  dependencies_missing:'缺少项目依赖路径：',
  provider_unknown:'模型服务或所选模型不存在。',
  provider_runtime_missing:'模型服务运行工具未安装。',
  credentials_missing:'模型服务缺少可用凭据。',
  provider_paused:'模型服务多次失败后已暂停。',
  worker_missing:'缺少模型执行工具或运行镜像。',
  catalog_missing:'最新模型目录中未找到所选模型。',
  model_probe_failed:'当前思考强度的工具测试未通过。',
  model_unverified:'当前思考强度尚未通过工具测试；本次检查没有调用模型。'
 };
 const reason=item.reason||'',base=t(reasons[reason]||'运行条件需要检查。');
 if(['tools_missing','dependencies_missing'].includes(reason))return base+missing.join(t('、'));
 if(reason.startsWith('provider_')||reason.startsWith('credentials_')||reason.startsWith('worker_')||reason.startsWith('model_'))return model+base;
 return base;
}
async function checkReadiness(force=false){
 const project=projectId;if(!project)return;const existing=readinessCache.get(project);if(existing?.loading&&!force)return;
 const ticket=++readinessSequence,pending={loading:true};readinessCache.set(project,pending);renderReadiness();
 const current=()=>ticket===readinessSequence&&project===projectId&&readinessCache.get(project)===pending;
 const discard=()=>{if(readinessCache.get(project)===pending)readinessCache.delete(project);};
 try{const result=await api('readiness?project='+encodeURIComponent(project));if(!current()){discard();return;}readinessCache.set(project,result);renderReadiness();}
 catch(error){if(!current()){discard();return;}discard();$('readiness-summary').textContent=error.message;$('readiness-check').disabled=false;throw error;}
}
function renderReadiness(){
 window.RSIProduct?.health();const result=readinessCache.get(projectId);if(!result)return;$('readiness-check').disabled=!!result.loading;
 $('readiness-panel').classList.toggle('needs-attention',!result.loading&&!result.ready);
 if(!result.loading&&!result.ready)$('readiness-panel').open=true;
 $('readiness-summary').textContent=t(result.loading?'正在检查运行条件…':result.ready?'基础条件已就绪，实际效果将在任务中验证。':'还有运行条件需要处理，请按提示设置。');
 const list=$('readiness-items');list.replaceChildren();if(result.loading)return;
 for(const item of result.items){if(item.ready)continue;const row=el('div',undefined,'readiness-row');row.append(el('span',t(readinessNames[item.code]||item.code)+(item.role?' · '+t(roleNames[item.role]):'')));
  const detail=readinessDetail(item);if(detail)row.append(el('small',detail,'muted'));if(item.detail){const raw=el('details',undefined,'readiness-detail-raw');raw.append(el('summary',t('查看原始诊断')),el('p',item.detail,'muted'));row.append(raw);}
  const kind=item.action||({model:'models',checks:'settings',dependencies:'settings',tools:'install',sandbox:'install',budget:'models'}[item.code]||'settings'),actions={models:['检查模型连接','models'],login:['连接账号','models'],settings:['检查项目设置','settings'],install:['检查运行环境','install'],budget:['查看模型用量','overview']},[label,target]=actions[kind]||actions.settings;
  row.append(action(t(label),()=>{if(['install','settings','models'].includes(target))changeSettingsScope('project');showView(target==='install'?'settings':target);if(target==='install'){$('advanced-settings').open=true;$('capabilities').closest('details').open=true;$('capabilities').scrollIntoView({block:'nearest'});}},'text-button'));list.append(row);}
 if(result.model_evidence?.length){const evidence=el('div',undefined,'readiness-evidence');evidence.append(el('span',t('模型验证状态'),'readiness-evidence-label'));for(const item of result.model_evidence){const state=item.task_verified?'真实任务已验证':item.tool_tested?'基础工具测试通过':item.configured?'连接已配置':'需要检查连接';evidence.append(el('span',t(roleNames[item.role]||item.role)+' · '+t(state),'readiness-evidence-item '+(item.task_verified?'good':item.tool_tested?'tested':item.configured?'configured':'bad')));}list.append(evidence);}
 if(result.ready)list.append(el('small',t(result.execution_verified?'当前版本已有任务运行验证。':'尚未进行真实任务验证；模型连接检查不产生推理费用。'),'muted'));
 if(result.merge_protection)list.append(el('small',t(result.merge_protection.strict_checks?'已观察到严格状态检查；GitHub 分支保护仍决定是否允许合并。':'请在 GitHub 确认严格状态检查或合并队列；当前权限可能无法读取全部保护规则。'),'muted'));
}
function stageProgress(data){
 const node=el('ol',undefined,'stage-progress'),done=new Map((data.stages||[]).map(s=>[s.stage,s]));
 for(const [key,title]of [['planning','判断价值'],['acceptance','确定完成标准'],['implementation','修改代码'],['evaluation','运行检查'],['review','代码评审']]){const row=el('li',t(title));row.className=done.has(key)?(['fail','insufficient_evidence'].includes(done.get(key).decision)?'stage-failed':'done'):'';node.append(row);}return node;
}
$('readiness-check').addEventListener('click',()=>safe(()=>checkReadiness(true)));
$('tasks-prev').addEventListener('click',()=>{taskOffset=Math.max(0,taskOffset-50);safe(refresh);});
$('tasks-next').addEventListener('click',()=>{taskOffset+=50;safe(refresh);});
$('reload-ui').addEventListener('click',()=>safe(async()=>{if(await uiConfirm(t('刷新会清除当前未提交的输入，请确认已经保存。')))location.reload();}));

document.querySelector('.brand')?.addEventListener('click',event=>{event.preventDefault();showView('overview');});
document.querySelectorAll('[data-view]').forEach(node=>node.addEventListener('click',()=>showView(node.dataset.view)));
$('project-settings').addEventListener('click',()=>{changeSettingsScope('project');showView('settings');});
$('global-settings').addEventListener('click',()=>{changeSettingsScope('global');showView('settings');});
$('edit-schedule').addEventListener('click',()=>{changeSettingsScope('project');showView('settings');$('schedule-settings').scrollIntoView({block:'start'});$('schedule-mode').focus({preventScroll:true});});
document.querySelectorAll('[data-settings-anchor]').forEach(button=>button.addEventListener('click',()=>$(button.dataset.settingsAnchor).scrollIntoView({block:'start'})));
$('schedule-mode').addEventListener('change',syncScheduleMode);
document.querySelectorAll('[data-schedule-preset]').forEach(select=>select.addEventListener('change',()=>{if(select.value==='custom')return;const node=document.querySelector('[data-path="schedule.'+select.dataset.schedulePreset+'_minutes"]'),unit=document.querySelector('[data-unit-for="'+node.dataset.duration+'"]'),value=Number(select.value),factor=value%1440===0?1440:60;unit.value=String(factor);unit.dataset.previous=String(factor);node.value=value/factor;node.min=5/factor;node.max=525600/factor;}));
document.querySelectorAll('[data-duration]').forEach(node=>node.addEventListener('input',syncSchedulePresets));
$('delivery-project-settings').addEventListener('click',()=>changeSettingsScope('project'));
$('detail-dialog').addEventListener('close',()=>{detailSequence++;detailTask=null;window.RSIExperience?.taskClosed();});
document.querySelectorAll('[data-settings-scope]').forEach(button=>button.addEventListener('click',()=>changeSettingsScope(button.dataset.settingsScope)));
document.querySelectorAll('[data-task-filter]').forEach(button=>button.addEventListener('click',()=>{$('task-filter').value=button.dataset.taskFilter;taskOffset=0;safe(refresh);}));
$('show-system-tasks').addEventListener('change',()=>{taskOffset=0;safe(refresh);});
$('attention-open').addEventListener('click',()=>{$('show-system-tasks').checked=true;$('task-filter').value='attention';taskOffset=0;showView('tasks');safe(refresh);});
document.querySelectorAll('[data-unit-for]').forEach(unit=>unit.addEventListener('change',()=>{const node=document.querySelector('[data-path="'+unit.dataset.unitFor+'"]');const previous=Number(unit.dataset.previous||durationUnit(node));node.value=Number(node.value)*previous/Number(unit.value);unit.dataset.previous=unit.value;node.min=5/Number(unit.value);node.max=525600/Number(unit.value);}));
$('delivery-choice').addEventListener('change',()=>{if($('delivery-choice').value==='custom')return;document.querySelector('[data-path="delivery.mode"]').value=$('delivery-choice').value==='local'?'local':'pr';document.querySelector('[data-path="delivery.auto_merge"]').checked=$('delivery-choice').value==='auto';syncDelivery();});
$('goal-text').addEventListener('input',()=>{goalDraftRevisions.set(projectId,(goalDraftRevisions.get(projectId)||0)+1);stashGoalDraft();});
$('project-select').addEventListener('change',()=>{stashSettings();stashGoalDraft();closeTaskDetail();projectId=$('project-select').value;restoreGoalDraft();goalDraftRestoredProject=projectId;localWrite('rsi-project',projectId);settingsBaseline=null;if(view==='models')window.RSIModelCenter?.show($('config-scope').value==='project'?projectId:null);taskOffset=0;writeNavigation(view);render();safe(refresh);});
var offerGlobalConfigRepair=function(raw){
 const dialog=el('dialog','','confirm-dialog');dialog.setAttribute('aria-labelledby','global-config-repair-title');
 const title=el('h2',t('通用配置损坏'));title.id='global-config-repair-title';
 const text=el('p',t('通用设置无法解析。重置会清除全局覆盖并暂停所有项目；原始内容保存在本地审计记录中。')+' '+JSON.stringify(raw||{}).slice(0,1000));
 const actions=el('div','','task-actions'),cancel=el('button',t('稍后处理'),'text-button'),repair=el('button',t('重置通用配置'),'button');
 cancel.type=repair.type='button';actions.append(cancel,repair);dialog.append(title,text,actions);document.body.append(dialog);dialog.showModal();
 cancel.onclick=()=>dialog.close();repair.onclick=()=>safe(async()=>{repair.disabled=true;try{await api('config/repair-global',{confirmation:'reset-invalid-global-config'});dialog.close();await refresh({force:true});toast(t('通用配置已重置，所有项目保持暂停。'));}finally{repair.disabled=false;}});
};
for(const id of ['add-project','onboard-add'])$(id).addEventListener('click',()=>$('project-dialog').showModal());
document.querySelectorAll('.close-dialog').forEach(button=>button.addEventListener('click',()=>button.closest('dialog').close()));
document.querySelectorAll('dialog').forEach(d=>d.addEventListener('click',e=>{if(e.target===d)d.close();}));
$('project-source').addEventListener('change',()=>{const remote=$('project-source').value==='github';$('local-project-fields').hidden=remote;$('github-project-fields').hidden=!remote;$('project-path').required=!remote;$('project-urls').required=remote;});
$('project-form').addEventListener('submit',event=>{event.preventDefault();safe(async()=>{
 const unchanged=unchangedForm(event.target,{projectSensitive:false});
 $('project-submit').disabled=true;
 try{
  let addedProject,existingProject=null;
  if($('project-source').value==='github'){
   const urls=$('project-urls').value.split(/\s+/).filter(Boolean),directory=$('project-directory').value||null;if(!urls.length||urls.length>10)throw Error(t('一次请输入 1 至 10 个仓库链接'));
   const failed=[],failedUrls=[];
   for(let i=0;i<urls.length;i++){
    $('import-status').textContent=t('正在导入')+' '+(i+1)+' / '+urls.length;
    try{const added=await api('projects/import',{url:urls[i],directory});addedProject=added.id;}catch(error){failed.push(urls[i]+': '+error.message);failedUrls.push(urls[i]);}
   }
   if(failed.length){if(unchanged()){$('project-urls').value=failedUrls.join('\n');$('import-status').textContent=t('未成功的链接已保留，已接入的项目不会重复导入。')+'\n'+failed.join('\n');}await refresh();return;}
  }else{
   const added=await api('projects',{path:$('project-path').value,name:$('project-name').value||null,enabled:$('project-enabled').checked});addedProject=added.id;
   if(added.existing)existingProject=added;
  }
  if(existingProject?.archived){
   if(!(await uiConfirm(t('该项目已归档。恢复后保持暂停，不会重新执行已取消的任务。确认恢复？'))))return;
   await api('project/restore',{project:existingProject.id,confirmation:'restore-project'});
   existingProject.archived=false;existingProject.paused=true;
  }
  if(unchanged()){stashSettings();closeTaskDetail();projectId=addedProject;localWrite('rsi-project',projectId);writeNavigation(view);settingsBaseline=null;$('project-dialog').close();$('project-form').reset();$('project-source').dispatchEvent(new Event('change'));$('import-status').textContent='';}
  await refresh();
  if(existingProject)toast(t('该路径已接入')+t('：')+(existingProject.name||t('未命名项目'))+' · '+t(existingProject.paused?'已暂停':'运行中')+' · '+t('未修改名称或启用状态'));
  else toast(t('项目已接入'));
 }finally{$('project-submit').disabled=false;}
},event.target);});
$('pause-button').addEventListener('click',()=>safe(async()=>{const project=currentProject();await api('pause',{project:project.id,paused:!project.paused});await refresh();toast(project.paused?t('项目已启用'):t('已暂停，正在中断当前工作'));}));
document.querySelector('#project-workspace')?.addEventListener('click',event=>{if(event.target.id!=='archive-project')return;safe(async()=>{const project=currentProject();if(!project)return;
 if(!(await uiConfirm(t('归档会停止调度并隐藏此项目，但保留任务、运行记录和审计历史。确认归档？'))))return;
 await api('project/archive',{project:project.id,confirmation:'archive-project'});
 projectId='';localWrite('rsi-project','');settingsBaseline=null;closeTaskDetail();await refresh({force:true});toast(t('项目已归档；历史记录仍保留。'));
})});
$('goal-form').addEventListener('submit',event=>{event.preventDefault();safe(async()=>{if(!projectId)throw Error(t('请先接入项目'));const project=projectId,text=$('goal-text').value,draftRevision=goalDraftRevisions.get(project)||0;if(!text.trim()||text.length>30000)throw Error(t('目标不能为空，且不能超过 30000 个字符。'));const submission=await pendingGoalSubmission(project,text);await api('goal',{project,text,request_id:submission.id});saveGoalSubmissions(project,(await goalSubmissions(project)).filter(entry=>entry.id!==submission.id));await renderGoalRecovery();if((goalDraftRevisions.get(project)||0)===draftRevision){if(goalDrafts.get(project)===text)goalDrafts.delete(project);if(draftStore.get('goal',project)===text)draftStore.remove('goal',project);if(projectId===project&&$('goal-text').value===text)$('goal-text').value='';}await refresh();toast(state.projects.find(p=>p.id===project)?.paused?t('目标已入队；启用项目后执行'):t('目标已加入任务列表'));},event.target);});
document.querySelectorAll('[data-inspect]').forEach(button=>button.addEventListener('click',()=>safe(async()=>{await api('request',{project:projectId,kind:button.dataset.inspect});await refresh();toast(currentProject().paused?t('已加入队列；启用项目后执行'):t('已加入队列'));})));
$('task-filter').addEventListener('change',()=>{taskOffset=0;safe(refresh);});$('config-scope').addEventListener('change',()=>changeSettingsScope($('config-scope').value));
let configHistoryDialog=null;
async function showConfigHistory(){
 if(configHistoryDialog?.open)return;
 const scope=settingsKey(),project=scope==='global'?null:projectId,box=el('dialog',undefined,'experience-dialog'),rows=el('div'),more=el('button',t('加载更多'),'text-button'),close=el('button',t('关闭'),'text-button');
 if(scope!=='global'&&!project)return;
 box.append(el('h2',t('设置变更记录')),el('p',t('仅最新且未被覆盖的设置可以撤销；活动中的任务继续使用原快照。')),rows,more,close);
 more.type=close.type='button';close.onclick=()=>box.close();box.addEventListener('close',()=>{box.remove();if(configHistoryDialog===box)configHistoryDialog=null;});document.body.append(box);box.showModal();configHistoryDialog=box;let before=null,loading=false,undoing=false;
 async function load(){if(loading)return;loading=true;more.disabled=true;
  try{const params=new URLSearchParams();if(project)params.set('project',project);if(before)params.set('before',before);const data=await api('config-history?'+params);if(!box.open)return;
   for(const entry of data.entries){const card=el('details');if(entry.error){card.append(el('summary',t('配置记录损坏，无法安全撤销。')));rows.append(card);continue;}
    const sources={console:t('界面保存'),runtime:t('运行器保存'),undo:t('撤销操作')};card.append(el('summary',stamp(entry.created)+' · '+(sources[entry.source]||t('未知'))+' · '+entry.revision));
    for(const change of entry.changes){const value=item=>item==='[redacted]'?t('已隐藏'):JSON.stringify(item);card.append(el('p',change.path+' : '+value(change.before)+' → '+value(change.after)));}
    if(entry.truncated)card.append(el('p',t('差异仅显示前 200 项；撤销将恢复完整设置快照。')));
    if(entry.can_undo){const undo=el('button',t('撤销这次设置修改'),'button');undo.type='button';undo.onclick=()=>safe(async()=>{if(undoing)return;undoing=true;undo.disabled=true;try{if(!(await uiConfirm(t('确定恢复这次修改之前的设置？这将影响之后的新任务。'))))return;await api('config/undo',{project,revision:entry.revision,expected:data.revision});box.close();await refresh({force:true});toast(t('设置已撤销'));}finally{undoing=false;undo.disabled=false;}});card.append(undo);}
    rows.append(card);
   }before=data.next_before;more.hidden=!data.has_more;
  }finally{loading=false;more.disabled=false;}
 }more.onclick=()=>safe(load);await load();
}
$('settings-history').addEventListener('click',()=>safe(showConfigHistory));
$('settings-form').addEventListener('invalid',event=>{let detail=event.target.closest('details');while(detail){detail.open=true;detail=detail.parentElement.closest('details');}},true);
$('settings-form').addEventListener('input',stashSettings);$('settings-form').addEventListener('change',stashSettings);
$('settings-form').addEventListener('submit',event=>{event.preventDefault();safe(async()=>{const button=$('settings-save'),saveKey=settingsKey();if(button.disabled)return;button.disabled=true;try{if($('config-scope').value==='project'&&!projectId)throw Error(t('请先接入项目，或切换到全局默认'));const unchanged=unchangedForm(event.target),baseline=settingsBaseline,scope=$('config-scope').value,project=projectId,key=settingsKey();let patch={};document.querySelectorAll('[data-path]').forEach(node=>{if(node.disabled)return;const value=fieldValue(node);if(JSON.stringify(value)!==JSON.stringify(getPath(settingsBaseline,node.dataset.path)))setPath(patch,node.dataset.path,value);});
 const hours=[Number($('work-start').value),Number($('work-end').value)];if(JSON.stringify(hours)!==JSON.stringify(settingsBaseline.schedule.working_hours))setPath(patch,'schedule.working_hours',hours);
 const labels=$('issue-labels').value.split(',').map(x=>x.trim()).filter(Boolean);if(JSON.stringify(labels)!==JSON.stringify(settingsBaseline.github.labels))setPath(patch,'github.labels',labels);
 const checks=JSON.parse($('checks-json').value);window.RSIProduct?.validateChecks(checks);if(JSON.stringify(checks)!==JSON.stringify(settingsBaseline.checks))patch.checks=checks;
 const inherit=scope==='project'&&$('schedule-mode').value==='inherit'?schedulePaths:[];
 const scheduleMode=$('schedule-mode').value;
 if(scope==='project'&&scheduleMode==='custom'&&settingsModes.get(baseline)==='inherit')for(const path of schedulePaths){const node=document.querySelector('[data-path="'+path+'"]');setPath(patch,path,fieldValue(node));}
 patch=deepMerge(patch,JSON.parse($('advanced-json').value||'{}'));const submittedValues=settingsValues(),pendingConfig=deepMerge(structuredClone(baseline),structuredClone(patch));for(const path of inherit)setPath(pendingConfig,path,getPath(state.config,path));settingsModes.set(pendingConfig,scheduleMode);pendingSettingsSaves.set(key,{config:pendingConfig,mode:scheduleMode});window.RSIProduct?.savingStatus(true,false,key);const saved=await api('config',{project:scope==='project'?project:null,values:patch,revision:scope==='global'?state.global_config_revision:state.config_revision,...(inherit.length?{inherit}:{})}),acknowledged=deepMerge(structuredClone(baseline),saved.config||patch);
 settingsModes.set(acknowledged,scheduleMode);
 settingsClean.set(key,JSON.stringify(acknowledgedSettingsValues(submittedValues,acknowledged,scheduleMode)));
 const draft=settingsDrafts.get(key);if(draft){draft.baseline=structuredClone(acknowledged);draft.baselineValues=Object.fromEntries(draft.values.map(entry=>[entry.path||entry.id,settingBaselineValue(entry,acknowledged,scheduleMode)]));writePersistentSettingsDraft(key,draft.values.filter(persistableDraftEntry),acknowledged,scheduleMode);}
 finishPendingSettingsSave(key);
 if(settingsBaseline&&(settingsBaseline===baseline||settingsContext===key)&&settingsKey()===key){settingsBaseline=acknowledged;stashSettings();}
 readinessCache.clear();await refresh();if(settingsBaseline===acknowledged&&unchanged()&&$('config-scope').value===scope&&view==='settings'&&sameValue(settingsValues(),submittedValues)){settingsDrafts.delete(key);renderSettings();}
 window.RSIProduct?.savingStatus(false,true,saveKey);toast(t(unchanged()?'已保存，无需重启；新任务使用新配置':'已保存提交的配置，后续修改仍未保存。'));}finally{finishPendingSettingsSave(saveKey);button.disabled=false;window.RSIProduct?.savingStatus(false,false,saveKey);}},event.target);});
$('case-form').addEventListener('submit',event=>{event.preventDefault();safe(async()=>{if(!projectId)throw Error(t('请先接入项目'));const unchanged=unchangedForm(event.target);await api('cases',{project:projectId,manifest:JSON.parse($('case-json').value)});if(unchanged())$('case-form').reset();await refresh({force:true});toast(t('评估任务已导入'));},event.target);});
const updateNames={checking:'正在检查更新',recovery_required:'更新恢复需要处理，请检查安装记录后重新启动服务',unchecked:'尚未检查更新',up_to_date:'已是最新稳定版',no_release:'尚无可用的正式发布包',available:'发现可用更新',waiting_idle:'等待任务结束后更新',downloading:'正在下载并校验',restarting:'正在重启服务',installed:'更新已完成',rolled_back:'更新失败，已恢复旧版',error:'更新检查或安装失败'};
const releaseStates={waiting_verification:'等待合并后检查',blocked_verification:'合并后检查未通过，暂不发布',waiting_target:'等待同一服务的上次部署结束',superseded:'已有更新版本接管部署',healthy:'健康检查通过',health_failed:'上线后健康检查失败',rolling_back:'正在恢复上一正常版本',rolled_back:'已恢复上一正常版本',rollback_failed:'恢复上一正常版本失败',pending:'等待执行',submitted:'工作流已提交',dispatch_unknown:'是否启动仍待确认',running:'工作流执行中',rerunning:'工作流重试中',rerun_unknown:'重试是否启动仍待确认',waiting_release:'等待发布成功',not_authorized:'尚未授权',waiting:'等待前置步骤',dispatching:'正在启动工作流',dispatched:'工作流已启动',queued:'排队中',in_progress:'工作流执行中',workflow_succeeded:'工作流执行成功',workflow_failed:'工作流执行失败',failed:'操作失败',blocked:'发布被阻止',uncertain:'执行结果待确认',cancelled:'工作流已取消',disabled:'已关闭'};
let releaseWorkflowSequence=0;
async function loadReleaseWorkflows(){
 const project=projectId,ticket=++releaseWorkflowSequence;if(!project||$('config-scope').value!=='project')return;
 const list=$('release-workflow-list');list.replaceChildren();const placeholder=el('option',t('正在读取工作流…'));placeholder.value='';list.append(placeholder);
 try{const result=await api('release-workflows?project='+encodeURIComponent(project));if(ticket!==releaseWorkflowSequence||project!==projectId||$('config-scope').value!=='project')return;
  list.replaceChildren();const first=el('option',t('选择发布或部署工作流'));first.value='';list.append(first);
  for(const workflow of result.workflows||[]){const option=el('option',workflow.name+' · '+workflow.workflow+(workflow.compatible?'':' · '+t('需要补充自动交付输入')));option.value=workflow.workflow;option.disabled=!workflow.compatible;list.append(option);}
  if(!result.workflows?.length)$('release-authorization-feedback').textContent=t('此仓库还没有可用工作流。可在高级设置手动指定，或先为项目添加工作流。');else $('release-authorization-feedback').textContent='';
 }catch(error){if(ticket!==releaseWorkflowSequence)return;list.replaceChildren();const failed=el('option',t('读取失败：')+error.message);failed.value='';list.append(failed);$('release-authorization-feedback').textContent=error.message;}
}
async function authorizeRelease(kind){
 const project=projectId,scope=$('config-scope').value,workflow=$('release-workflow-list').value;if(!project||scope!=='project')return;
 if(!workflow)throw Error(t('请先选择可用工作流。'));
 const button=$(kind==='release'?'release-authorize':'deploy-authorize');if(button.disabled)return;button.disabled=true;
 const baseline=structuredClone(settingsBaseline),submitted=settingsValues(),submittedMode=$('schedule-mode').value,revisions=new Map(settingsFieldRevisions.get(project)),prefix='delivery.'+kind+'.';
 try{const result=await api('release-authorize',{project,kind,workflow}),reply=result.authorization||result,authorization=Object.fromEntries(['enabled','workflow','ref','sha'].map(name=>[name,reply[name]]));
  const current=project===projectId&&$('config-scope').value===scope&&settingsContext===project&&!!settingsBaseline;
  if(current)stashSettings();
  const draft=settingsDrafts.get(project),mode=current?$('schedule-mode').value:draft?.mode||submittedMode,acknowledged=structuredClone(current?settingsBaseline:draft?.baseline||state.projects.find(p=>p.id===project)?.config||baseline);
  setPath(acknowledged,'delivery.'+kind,{...getPath(acknowledged,'delivery.'+kind),...authorization});settingsModes.set(acknowledged,mode);
  const affected=entry=>entry.path?.startsWith(prefix)&&Object.hasOwn(authorization,entry.path.slice(prefix.length));
  const clean=JSON.parse(settingsClean.get(project)||'[]');
  settingsClean.set(project,JSON.stringify(clean.map(entry=>affected(entry)?acknowledgedSettingsValues([entry],acknowledged,mode)[0]:entry)));
  const values=(current?settingsValues():draft?.values||clean).map(entry=>{
   if(!affected(entry))return entry;
   const before=submitted.find(value=>value.path===entry.path),unchanged=(settingsFieldRevisions.get(project)?.get(entry.path)||0)===(revisions.get(entry.path)||0)&&sameValue(entry,before);
   return unchanged?acknowledgedSettingsValues([entry],acknowledged,mode)[0]:entry;
  });
  const target=state.projects.find(p=>p.id===project);if(target?.config)setPath(target.config,'delivery.'+kind,{...getPath(target.config,'delivery.'+kind),...authorization});
  if(current){settingsBaseline=acknowledged;for(const entry of values.filter(affected)){const node=document.querySelector('[data-path="'+entry.path+'"]');node.value=entry.value;node.checked=entry.checked;}stashSettings();syncDelivery();$('release-authorization-feedback').textContent=t('工作流已授权。只影响当前项目，其他尚未保存的设置仍可继续修改。');window.RSIProduct?.dirtyStatus();}
  else if(draft||values.some(entry=>affected(entry)&&(settingsFieldRevisions.get(project)?.get(entry.path)||0)!==(revisions.get(entry.path)||0))){
   // A newer edit can return to the clean value and remove the old draft before navigation.
   const rebased=draft||{mode,baselineValues:Object.fromEntries(values.map(entry=>[entry.path||entry.id,settingBaselineValue(entry,acknowledged,mode)]))};
   rebased.baseline=acknowledged;rebased.values=values;for(const entry of values.filter(affected))rebased.baselineValues[entry.path]=settingBaselineValue(entry,acknowledged,mode);settingsDrafts.set(project,rebased);writePersistentSettingsDraft(project,values.filter(persistableDraftEntry),acknowledged,mode);
  }
  readinessCache.clear();await refresh();
 }finally{button.disabled=false;}
}
async function renderReleaseOutcome(node,run,project){
 if(!['merged','pr_open'].includes(run.state))return;
 let automation;try{automation=await api('release-status?project='+encodeURIComponent(project)+'&run='+encodeURIComponent(run.id));}catch{return;}
 if(project!==projectId||!node.isConnected)return;
 const section=el('section',undefined,'release-outcome');section.append(el('h4',t('发布与上线')));
 for(const [kind,title]of [['release','发布'],['deploy','部署']]){const operation=automation[kind];if(!operation)continue;const row=el('div',undefined,'release-status-row');row.append(el('strong',t(title)+' · '),el('span',t(releaseStates[operation.state]||operation.state)));
  const rerunUnknown=operation.state==='rerun_unknown'||operation.rollback?.state==='rerun_unknown',dispatchRejected=operation.state==='failed'&&operation.dispatch_receipt==='rejected'&&!operation.workflow_run_id;
  // 等待中的状态（合并后检查、同服务前次部署、等待发布成功）属进行中，提示用中性样式；其余错误保持红色。
  const waitingNotice=['waiting_verification','waiting_target','waiting_release'].includes(operation.state);
  if(operation.error&&!rerunUnknown)row.append(el('p',t(apiErrors[operation.error]||operation.error),waitingNotice?'muted':'form-error'));
  if(dispatchRejected)row.append(el('p',t('提交未被接受。修复授权或工作流配置后，可以安全重试。'),'muted'));
  if(kind==='deploy'&&operation.state==='workflow_succeeded')row.append(el('p',t('部署工作流已完成，服务健康状态尚未验证。'),'muted'));
  if(operation.rollback_unavailable)row.append(el('p',t('没有已验证的历史版本可恢复，请人工检查服务。'),'form-error'));
  if(operation.rollback){const rollback=operation.rollback;row.append(el('p',t('恢复旧版本')+' · '+t(releaseStates[rollback.state]||rollback.state)));}
  if(operation.url&&/^https:\/\/github\.com\//.test(operation.url)){const link=el('a',t('查看 GitHub 工作流'));link.href=operation.url;link.target='_blank';link.rel='noreferrer';row.append(link);}
  if(rerunUnknown)row.append(el('p',t('GitHub 暂未确认重试，请查看工作流。重新确认只查询状态，不会再次启动。'),'muted'));
  if(rerunUnknown||dispatchRejected||operation.state==='failed'&&operation.workflow_run_id||operation.state==='rollback_failed')row.append(action(rerunUnknown?t('重新确认状态'):operation.state==='rollback_failed'?t('重试恢复旧版本'):dispatchRejected?t('修复权限后重试'):t('重试')+' · '+t(title),async()=>{await api('release-retry',{project,run:run.id,kind});if(projectId===project){await refresh();if(detailTask)await showTask(detailTask);}},'text-button'));
  if(['blocked','dispatch_unknown','health_failed','rollback_failed'].includes(operation.state))row.append(action(t('检查发布设置'),()=>{$('detail-dialog').close();changeSettingsScope('project');$('release-settings').scrollIntoView({block:'start'});},'text-button'));
  section.append(row);
 }node.append(section);
}
$('release-workflows-refresh').addEventListener('click',()=>safe(loadReleaseWorkflows));
$('release-authorize').addEventListener('click',()=>safe(()=>authorizeRelease('release')));
$('deploy-authorize').addEventListener('click',()=>safe(()=>authorizeRelease('deploy')));
for(const kind of ['release','deploy'])document.querySelector('[data-path="delivery.'+kind+'.enabled"]')?.addEventListener('change',event=>{
 if(event.target.checked&&!document.querySelector('[data-path="delivery.'+kind+'.sha"]').value){event.target.checked=false;$('release-authorization-feedback').textContent=t('请先选择工作流并点击授权。');$('release-workflow-list').focus();}syncDelivery();
});

const updateChannels={desktop:'桌面应用',pip:'Python 包（建议用 pip 升级）',managed:'内置自更新'};
let desktopUpdate=null,desktopUpdateTimer=null,desktopCheckError=false;
function stopDesktopUpdatePolling(){clearInterval(desktopUpdateTimer);desktopUpdateTimer=null;}
async function checkDesktopUpdate(force=false){
 if(window.RSINativeUpdate){await window.RSINativeUpdate.run('check');desktopCheckError=false;return;}
 if(!window.RSIDesktop?.invoke)return;
 try{const info=await window.RSIDesktop.invoke('check_desktop_update',{force});desktopUpdate=info&&info.version?info:null;desktopCheckError=!!(info&&info.check_error);}catch{desktopUpdate=null;desktopCheckError=true;}
 // 下载可能先于本次页面会话开始（刷新界面或切换运行机器后加载）：恢复轮询直到完成或失败。
 if(desktopUpdate?.downloading&&!desktopUpdateTimer)pollDesktopUpdate();
 renderUpdate();
}
function pollDesktopUpdate(){
 if(desktopUpdateTimer||!window.RSIDesktop?.invoke)return;
 desktopUpdateTimer=setInterval(async()=>{
   try{const info=await window.RSIDesktop.invoke('check_desktop_update');
     if(!info||!info.version){desktopUpdate=null;stopDesktopUpdatePolling();}
     else if(info.error){desktopUpdate=null;stopDesktopUpdatePolling();toast(t('新版本下载失败：')+(info.error_code==='checksum'?t('校验未通过，已删除下载文件。'):info.error),true);}
     else{desktopUpdate=info;if(!info.downloading&&info.percent>=100){stopDesktopUpdatePolling();toast(t('下载已完成，安装镜像已打开；请拖拽安装后重启应用。'));}}
   }catch{stopDesktopUpdatePolling();}
   renderUpdate();
 },1000);
}
async function startDesktopUpdate(){
 if(!desktopUpdate||desktopUpdateTimer)return;
 if(desktopUpdate.opened&&!desktopUpdate.downloading)return;
 if(!(await uiConfirm(t('将下载新版桌面安装镜像并自动打开，下载完成后请拖拽安装。确认继续？'))))return;
 desktopUpdate.downloading=true;desktopUpdate.percent=0;renderUpdate();
 try{await window.RSIDesktop.invoke('download_desktop_update');}catch(error){desktopUpdate=null;toast(t('新版本下载失败：')+error,true);renderUpdate();return;}
 pollDesktopUpdate();
}
function renderUpdateExtras(update){
 if(window.RSINativeUpdate&&window.RSIDesktop){window.RSINativeUpdate.render();return;}
 let notes=$('update-notes');
 if(update.notes&&update.version){if(!notes){notes=el('details');notes.id='update-notes';$('update-status').after(notes);}notes.replaceChildren(el('summary',t('这个版本更新了什么')+' · '+update.version),el('pre',update.notes));}
 else if(notes)notes.remove();
 let button=$('desktop-update');
 const candidate=window.RSIDesktop&&desktopUpdate;
 if(candidate){if(!button){button=el('button',undefined,'button');button.id='desktop-update';button.type='button';button.addEventListener('click',()=>safe(startDesktopUpdate));$('apply-update').after(button);}
  const done=!candidate.downloading&&candidate.opened;
  button.replaceChildren((candidate.downloading?t('正在下载新版本…'):done?t('安装镜像已打开，请拖拽安装'):t('下载并安装新版本'))+' · '+candidate.version+(candidate.downloading&&Number.isFinite(candidate.percent)?' · '+Math.max(0,Math.min(100,Math.round(candidate.percent)))+'%':''));button.disabled=!!candidate.downloading||done;}
 else if(button)button.remove();
}
function renderUpdate(){if(!state)return;const update=state.update||{state:'unchecked'};
 const progress=update.state==='downloading'&&Number.isFinite(update.progress)?' · '+Math.max(0,Math.min(100,Math.round(update.progress)))+'%':'';
 const channel=updateChannels[update.channel]?' · '+t('安装渠道')+t('：')+t(updateChannels[update.channel]):'';
 const limited=!update.desktop_managed&&update.reason_code==='github_primary_rate_limit'&&Number.isFinite(update.rate_limit_until);
 const cooling=limited&&Date.now()<update.rate_limit_until*1000;
 const retryTime=limited?new Date(update.rate_limit_until*1000).toLocaleString(typeof language==='undefined'?'zh-CN':language):'';
 // 已知原因给中文指引；GitHub HTTP 码含动态内容走结构化说明；
 // 未识别的原因保留原文（原始运行证据），加中文前缀说明来源。
 const githubHttp=update.reason&&/^GitHub HTTP (\d+)$/.exec(update.reason);
 const reason=limited?t('GitHub 匿名 API 额度已用尽。')+' '+t('可重试时间：')+retryTime+(cooling?' '+t('此时间前暂停更新请求。'):t('（已到，可重试）')):
  githubHttp?t('无法访问 GitHub 发布接口（HTTP {code}），请稍后重试。').replace('{code}',githubHttp[1]):
  update.reason?(apiErrors[update.reason]?t(apiErrors[update.reason]):t('更新失败原因（原文）：')+update.reason):'';
 let status=t(updateNames[update.state]||update.state)+(update.version?' · '+update.version:'')+(reason?' · '+reason:'')+progress;
 if(update.desktop_managed)status=t('桌面应用')+channel;
 else status=t('后台服务')+' · '+status+channel;
 $('update-status').textContent=status;
 $('apply-update').disabled=!!update.desktop_managed||cooling||!['available','waiting_idle'].includes(update.state);
 $('apply-update').hidden=!!update.desktop_managed&&!!window.RSINativeUpdate;
 $('check-update').disabled=cooling||update.state==='checking'||(update.busy??['downloading','restarting'].includes(update.state));
 renderUpdateExtras(update);}
$('check-update').addEventListener('click',()=>safe(async()=>{if(window.RSIDesktop&&window.RSINativeUpdate){await window.RSINativeUpdate.run('check');return;}if(window.RSIDesktop){await checkDesktopUpdate(true);if(desktopCheckError){toast(t('检查更新失败，请稍后重试。'),true);return;}if(!desktopUpdate)toast(t('已是最新稳定版'));return;}const result=await api('updates/check',{});if(result.update){state.update=result.update;renderUpdate();}else await refresh();if(result.started)toast(t('已开始检查更新'));}));
$('apply-update').addEventListener('click',()=>safe(async()=>{if(!(await uiConfirm(t('将安装正式发布包，并在任务空闲时重启服务。确认更新？'))))return;await api('updates/apply',{confirm:true});toast(t('已申请更新，等待空闲和重启'));await refresh();}));
safe(refresh);
if(window.RSIDesktop)safe(checkDesktopUpdate);
$('language-select').addEventListener('change',()=>safe(async()=>{
 language=$('language-select').value;
 localWrite('rsi-language',language);
 translateStatic();
 const connectionStates=['已连接','连接中断','连接超时','需要重新登录'];
 $('connection').textContent=t(connectionStates.find(name=>[name,english[name]].includes($('connection').textContent))||'已连接');
 if(!projectId&&$('project-select').options.length)$('project-select').options[0].textContent=t('尚未接入项目');
 $('toast').hidden=true;
 if(!state)return;
 render();if(settingsBaseline){syncDelivery();window.RSIProduct?.renderEditor();window.RSIProduct?.dirtyStatus();}
 window.RSIModelCenter?.translate();
 window.RSIUsage?.translate();
 window.RSIExperience?.translate?.();
 window.RSIDesktop?.translate?.();
 window.RSINativeUpdate?.render?.();
 if($('detail-dialog').open&&detailTask)await showTask(detailTask);
}));
typeof document.addEventListener==='function'&&document.addEventListener('visibilitychange',()=>{
 if(document.visibilityState==='visible'&&state)safe(refresh);
 else scheduleRefreshPoll();
});
typeof window.addEventListener==='function'&&window.addEventListener('focus',()=>{if(state)safe(refresh);});
