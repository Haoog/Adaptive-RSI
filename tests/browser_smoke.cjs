const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const {createInterface} = require('node:readline');
const {chromium} = require('playwright');
const fs = require('node:fs');

async function checkCodexHTTPLogin(browser,url,errors){
 const page=await browser.newPage({viewport:{width:1100,height:900}});
 page.on('pageerror',error=>errors.push(error.message));
 try{
  await page.goto(url);await page.locator('#project-status').filter({hasText:'已暂停'}).waitFor();
  await page.locator('#project-settings').click();await page.locator('[data-mc-tab="providers"]').click();
  await page.locator('#mc-provider-list .mc-provider').filter({hasText:'Codex 登录'}).click();
  const start=async()=>{
   const response=page.waitForResponse(response=>response.url().endsWith('/api/auth/start')&&response.request().method()==='POST');
   await page.getByRole('button',{name:'前往 Codex 官方登录',exact:true}).click();
   const started=await response;assert.equal(started.status(),200,await started.text());
   assert.deepEqual(started.request().postDataJSON(),{backend:'codex',provider:null,method:0,inputs:{}});
   const session=await started.json();assert.equal(session.backend,'codex');assert.equal(session.provider,null);
   await page.locator('.mc-auth-code').filter({hasText:'HTTP-TEST-CODE'}).waitFor();return session.id;
  };
  const cancel=async(id)=>{
   const response=page.waitForResponse(response=>response.url().endsWith('/api/auth/cancel')&&response.request().method()==='POST');
   await page.getByRole('button',{name:'取消登录',exact:true}).click();const cancelled=await response;
   assert.equal(cancelled.status(),200,await cancelled.text());assert.equal((await cancelled.json()).id,id);
   await page.locator('#mc-auth').filter({hasText:'已取消登录'}).waitFor();
  };
  const first=await start();await cancel(first);const second=await start();assert.notEqual(second,first);await cancel(second);
 }finally{await page.close();}
}

async function checkReleaseRetryUI(browser,url,errors){
 const page=await browser.newPage({viewport:{width:1100,height:900}}),writes=[];
 let automation={};
 page.on('pageerror',error=>errors.push(error.message));
 page.on('request',request=>{if(request.method()==='POST')writes.push({path:new URL(request.url()).pathname,body:request.postDataJSON()});});
 const reply=(route,data,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(data)});
 await page.route('**/api/release-status?*',route=>reply(route,automation));
 await page.route('**/api/release-retry',route=>reply(route,{ok:true}));
 try{
  await page.goto(url);await page.locator('#project-status').filter({hasText:'已暂停'}).waitFor();
  await page.locator('[data-task-filter="attention"]').click();
  const project=await page.locator('#project-select').inputValue();
  for(const test of [
   {kind:'release',operation:{state:'rerun_unknown',workflow_run_id:17},label:'重新确认状态',explanation:true},
   {kind:'deploy',operation:{state:'rollback_failed',rollback:{state:'rerun_unknown',workflow_run_id:18}},label:'重新确认状态',explanation:true},
   {kind:'release',operation:{state:'failed',workflow_run_id:19},label:'重试 · 发布'},
   {kind:'deploy',operation:{state:'rollback_failed',rollback:{state:'failed',workflow_run_id:20}},label:'重试恢复旧版本'}
  ]){
   automation={[test.kind]:test.operation};
   await page.locator('#tasks-list .task-title').filter({hasText:'等待修复线上检查'}).click();
   const section=page.locator('#detail-dialog .release-outcome');await section.getByRole('button',{name:test.label,exact:true}).waitFor();
   if(test.explanation)assert.match(await section.innerText(),/重新确认只查询状态，不会再次启动/);
   const before=writes.length,response=page.waitForResponse(response=>response.url().endsWith('/api/release-retry')),refreshed=page.waitForResponse(response=>new URL(response.url()).pathname==='/api/release-status');
   await section.getByRole('button',{name:test.label,exact:true}).click();assert.equal((await response).status(),200);
   await refreshed;
   await section.getByRole('button',{name:test.label,exact:true}).waitFor();
   assert.deepEqual(writes.slice(before),[{path:'/api/release-retry',body:{project,run:'browser-ci',kind:test.kind}}]);
   await page.locator('#detail-dialog .close-dialog').click();
  }
 }finally{await page.close();}
}

async function checkAccountAndReleaseUI(browser,url,errors){
 const page=await browser.newPage({viewport:{width:1100,height:900}}),requests=[];
 page.on('pageerror',error=>errors.push(error.message));
 let signedIn=false,complete=false,authActive=false,releaseStarted,authStarts=0,pollTransientFailure=true,completionStatusFailures=0,catalogFailures=0;
 const session={id:'browser-auth-session',backend:'codex',provider:null,state:'awaiting_authorization',method:'code',verification_url:'https://auth.openai.com/device',user_code:'TEST-1234',expires_at:Math.floor(Date.now()/1000)+600};
 const reply=(route,data,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(data)});
 await page.route('**/api/auth/**',async route=>{
  const path=new URL(route.request().url()).pathname,body=route.request().postDataJSON();requests.push({path,body});
  if(path.endsWith('/status')){if(complete&&completionStatusFailures++===0)return reply(route,{error:'Temporary status failure'},503);return reply(route,{codex:{state:signedIn?'authenticated':'signed_out'},opencode:{state:'signed_out',authenticated_providers:[]},sessions:authActive?[session]:[]});}
  if(path.endsWith('/providers'))return reply(route,[{id:'openai',name:'OpenAI',methods:[{index:0,label:'Official login',type:'oauth',supported:true},{index:1,label:'API key',type:'api'}]},{id:'github-copilot',name:'GitHub Copilot',methods:[{index:0,label:'Device login',type:'oauth',supported:true,prompts:[{type:'select',key:'deployment',message:'Account type',options:[{label:'GitHub.com',value:'public'},{label:'Enterprise',value:'enterprise'}]},{type:'text',key:'domain',message:'Enterprise domain',when:{key:'deployment',op:'eq',value:'enterprise'}}]}]}]);
  if(path.endsWith('/start')){authStarts++;if(body.backend==='opencode'){await new Promise(resolve=>{releaseStarted=resolve;});return reply(route,{...session,id:'opencode-session',backend:'opencode',provider:body.provider});}complete=false;authActive=true;return reply(route,session);}
  if(path.endsWith('/session')){if(authActive&&pollTransientFailure){pollTransientFailure=false;return reply(route,{error_code:'temporary_network_error',error:'temporary network failure'},503);}if(complete){signedIn=true;authActive=false;}return reply(route,{...session,state:complete?'completed':'awaiting_authorization'});}
  if(path.endsWith('/complete')){complete=true;return reply(route,{...session,state:'completing'});}
  if(path.endsWith('/cancel')){authActive=false;return reply(route,{...session,state:'cancelled'});}
  if(path.endsWith('/logout')){signedIn=false;return reply(route,{ok:true});}
  return reply(route,{ok:true});
 });
 let catalogs=0,probes=0;
 await page.route('**/api/model-center/fetch',route=>{catalogs++;if(catalogFailures++===0)return reply(route,{error:'Temporary catalog failure'},503);return reply(route,{ok:true});});
 await page.route('**/api/model-center/probe',route=>{probes++;return reply(route,{ok:true});});
 await page.route('**/api/release-workflows?*',route=>reply(route,{workflows:[{workflow:'.github/workflows/release.yml',name:'Release',compatible:true}]}));
 await page.route('**/api/release-authorize',route=>{requests.push({path:'/api/release-authorize',body:route.request().postDataJSON()});return reply(route,{enabled:true,workflow:'.github/workflows/release.yml',ref:'rsi-workflow-test',sha:'a'.repeat(40)});});
 try{
  await page.goto(url);await page.locator('#project-status').filter({hasText:'已暂停'}).waitFor();await page.locator('#project-settings').click();await page.locator('[data-mc-tab="providers"]').click();await page.locator('#mc-provider-list .mc-provider').filter({hasText:'Codex 登录'}).click();
  await page.getByRole('button',{name:'前往 Codex 官方登录',exact:true}).click();
  await page.locator('.mc-auth-code').filter({hasText:'TEST-1234'}).waitFor();
  const code=page.locator('[data-auth-field="code:browser-auth-session"]');await code.fill('keep-my-code');await code.focus();await page.locator('#mc-auth').filter({hasText:'暂时无法连接，授权状态已保留'}).waitFor();
  await page.waitForTimeout(1650);await page.locator('.mc-auth-code').filter({hasText:'TEST-1234'}).waitFor();assert.equal(authStarts,1);
  assert.equal(await code.inputValue(),'keep-my-code');assert(await code.evaluate(node=>document.activeElement===node));
  if(process.env.RSI_SCREENSHOT_DIR)await page.locator('#mc-auth').screenshot({path:process.env.RSI_SCREENSHOT_DIR+'/account-sign-in.png'});
  await page.reload();await page.waitForFunction(()=>state&&currentProject()?.paused);await page.locator('.sidebar nav [data-view="overview"]').click();await page.locator('#project-status').filter({hasText:'已暂停'}).waitFor();await page.locator('#project-settings').click();await page.locator('[data-mc-tab="providers"]').click();await page.locator('.mc-auth-code').filter({hasText:'TEST-1234'}).waitFor();
  await page.getByRole('button',{name:'取消登录',exact:true}).click();await page.locator('#mc-auth').filter({hasText:'已取消登录'}).waitFor();
  await page.getByRole('button',{name:'前往 Codex 官方登录',exact:true}).click();await code.fill('official-code');await page.getByRole('button',{name:'完成授权',exact:true}).click();
  await page.locator('#mc-auth').filter({hasText:'账号已连接'}).waitFor();assert.equal(requests.find(r=>r.path.endsWith('/complete')).body.code,'official-code');
  await page.waitForFunction(()=>document.getElementById('mc-feedback').textContent.includes('模型列表已更新'));assert.equal(catalogs,2);assert(completionStatusFailures>=2);assert.equal(probes,0);assert.equal(authStarts,2);assert.equal(requests.filter(r=>r.path.endsWith('/complete')).length,1);
  await page.waitForTimeout(1650);assert.equal(catalogs,2,'successful completion refresh must stop retrying');
  await page.getByRole('button',{name:'退出账号',exact:true}).click();await page.locator('#mc-auth').filter({hasText:'尚未登录'}).waitFor();assert.equal(requests.find(r=>r.path.endsWith('/logout')).body.provider,null);
  await page.getByRole('button',{name:'OpenCode',exact:true}).click();await page.locator('#mc-auth-provider').selectOption('github-copilot');
  const domain=page.getByLabel('Enterprise domain');assert.equal(await domain.isVisible(),false);await page.getByLabel('Account type').selectOption('enterprise');await domain.fill('github.example.com');
  await page.getByRole('button',{name:'官方登录 · Device login',exact:true}).click();await page.locator('#mc-auth-provider').selectOption('openai');
  await page.waitForFunction(()=>document.querySelector('#mc-auth input[type="password"]'));
  releaseStarted();await page.waitForTimeout(150);
  assert.equal(await page.locator('.mc-auth-code').count(),0);
  const started=requests.find(r=>r.body?.backend==='opencode'&&r.path.endsWith('/start'));assert.deepEqual(started.body.inputs,{deployment:'enterprise',domain:'github.example.com'});
  await page.locator('#mc-auth input[type="password"]').fill('fake-test-key');await page.getByRole('button',{name:'保存供应商密钥',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('#mc-auth input[type="password"]').value==='');assert.equal(requests.find(r=>r.path.endsWith('/key')).body.provider,'openai');
  assert.equal(await page.locator('#deploy-health-settings').isVisible(),false);
  await page.locator('#release-workflow-list').selectOption('.github/workflows/release.yml');await page.locator('#release-authorize').click();await page.locator('#release-authorization-feedback').filter({hasText:'工作流已授权'}).waitFor();
  assert.equal(await page.locator('[data-path="delivery.release.enabled"]').isChecked(),true);assert.equal(await page.locator('[data-path="delivery.release.sha"]').inputValue(),'a'.repeat(40));assert.equal(requests.find(r=>r.path==='/api/release-authorize').body.kind,'release');
  await page.locator('#deploy-authorize').click();await page.locator('#deploy-health-settings').waitFor();await page.locator('[data-path="delivery.deploy.enabled"]').uncheck();assert.equal(await page.locator('#deploy-health-settings').isVisible(),false);
  if(process.env.RSI_SCREENSHOT_DIR)await page.locator('#release-settings').screenshot({path:process.env.RSI_SCREENSHOT_DIR+'/release-settings.png'});
  await page.setViewportSize({width:390,height:844});await page.locator('#language-select').selectOption('en');assert.equal(await page.getByRole('heading',{name:'Release and deployment after merge',exact:true}).count(),1);assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
 }finally{await page.close();}
}

async function main() {
  const server = spawn(process.env.PYTHON || 'python3', ['-m', 'tests.browser_fixture'], {
    stdio: ['pipe', 'pipe', 'pipe'], env: {...process.env, PYTHONDONTWRITEBYTECODE: '1'}
  });
  let browser;
  const errors = [];
  server.stderr.on('data', chunk => process.stderr.write(chunk));
  try {
    const url = await new Promise((resolve, reject) => {
      const timeout = setTimeout(() => reject(Error('Console startup timed out')), 30000);
      createInterface({input: server.stdout}).once('line', line => {clearTimeout(timeout); resolve(line);});
      server.once('exit', code => {clearTimeout(timeout); reject(Error('Console exited: ' + code));});
    });
    browser = await chromium.launch(process.env.RSI_BROWSER_CHANNEL ? {channel: process.env.RSI_BROWSER_CHANNEL} : {});
    const page = await browser.newPage({viewport: {width: 1380, height: 1000}});
    const saveSettings = async () => {
      const saved = page.waitForResponse(response=>response.url().endsWith('/api/config')&&response.request().method()==='POST');
      await page.locator('#settings-save').click();
      const response=await saved;assert.equal(response.status(),200,await response.text());
      await page.waitForFunction(()=>!document.getElementById('settings-save').disabled);
    };
    page.on('pageerror', error => errors.push(error.message));
    await checkCodexHTTPLogin(browser,url,errors);
    if(process.env.RSI_BROWSER_CODEX_HTTP_ONLY){assert.deepEqual(errors,[]);console.log('Real HTTP Codex button checks passed.');return;}
    await checkReleaseRetryUI(browser,url,errors);
    if(process.env.RSI_BROWSER_RELEASE_RETRY_ONLY){assert.deepEqual(errors,[]);console.log('Release confirmation and retry UI checks passed.');return;}
    if(process.env.RSI_BROWSER_AUTH_ONLY){await checkAccountAndReleaseUI(browser,url,errors);assert.deepEqual(errors,[]);console.log('Targeted account and release UI checks passed.');return;}
    await page.goto(url);
    await page.locator('#readiness-summary').filter({hasText: '基础条件已就绪'}).waitFor();
    assert.equal(await page.locator('#project-status').innerText(), '已暂停');
    const draftProject = await page.locator('#project-select').inputValue();
    const otherProject = await page.locator('#project-select option').evaluateAll(options => options.find(option => option.textContent.includes('另一个独立项目')).value);
    await page.locator('#goal-text').fill('只属于浏览器验证项目的草稿');
    await page.locator('#project-select').selectOption(otherProject);
    await page.waitForFunction(() => document.getElementById('goal-project-target').textContent === '另一个独立项目');
    assert.equal(await page.locator('#goal-text').inputValue(), '');
    await page.locator('#goal-text').fill('只属于另一个项目的草稿');
    await page.locator('#project-select').selectOption(draftProject);
    await page.waitForFunction(() => document.getElementById('goal-project-target').textContent === '浏览器验证项目');
    assert.equal(await page.locator('#goal-text').inputValue(), '只属于浏览器验证项目的草稿');
    await page.locator('#goal-text').fill('');
    await page.locator('#project-select').selectOption(otherProject);
    assert.equal(await page.locator('#goal-text').inputValue(), '只属于另一个项目的草稿');
    await page.locator('#goal-text').fill('');
    await page.locator('#project-select').selectOption(draftProject);
    assert.equal(await page.locator('#goal-project-target').innerText(), '浏览器验证项目');
    await page.locator('#activity').evaluate(node=>{node.closest('details').open=true;});
    await page.locator('#activity .activity-row').filter({hasText:'运行期间 Issue 已关闭或被排除'}).waitFor();
    await page.locator('#language-select').selectOption('en');
    await page.locator('#activity .activity-row').filter({hasText:'Issue closed or excluded during execution'}).waitFor();
    await page.locator('#language-select').selectOption('zh-CN');
    await page.locator('#goal-text').fill('验证保留用户输入');
    await page.locator('#readiness-summary').click();
    await page.locator('#readiness-check').click();
    await page.waitForTimeout(5500);
    assert.equal(await page.locator('#goal-text').inputValue(), '验证保留用户输入');
    await page.locator('#project-settings').click();
    await page.locator('#delivery-choice').selectOption('auto');
    await saveSettings();
    await page.locator('#toast').filter({hasText: '已保存'}).waitFor();
    assert.equal(await page.locator('#delivery-choice').inputValue(), 'auto');
    // 扫描设置必须热更新，并保留项目与通用设置之间的独立关系。
    await page.locator('#schedule-mode').selectOption('custom');
    await page.locator('#unit-light_minutes').selectOption('1440');
    await page.locator('[data-path="schedule.light_minutes"]').fill('2');
    await saveSettings();
    await page.locator('#toast').filter({hasText: '已保存'}).waitFor();
    const firstProject = await page.locator('#project-select').inputValue();
    await page.locator('#global-settings').click();
    assert.equal(await page.locator('[data-schedule-preset]').count(), 2);
    assert.equal(await page.locator('[data-path="limits.daily_calls"], [data-path="limits.run_calls"], [data-path="limits.run_seconds"]').count(), 0);
    await page.locator('[data-mc-tab="providers"]').click();
    await page.getByRole('button', {name:'OpenCode', exact:true}).click();
    await page.getByRole('heading', {name:'OpenCode', exact:true}).waitFor();
    await page.locator('[data-mc-tab="roles"]').click();
    await page.locator('[data-schedule-preset="light"]').selectOption('360');
    await page.locator('[data-schedule-preset="issues"]').selectOption('10080');
    await saveSettings();
    await page.locator('#toast').filter({hasText: '已保存'}).waitFor();
    let snapshot = await page.evaluate(async () => (await fetch('/api/state')).json());
    assert.equal(snapshot.config.schedule.light_minutes, 360);
    assert.equal(snapshot.projects.find(p=>p.id===firstProject).config.schedule.light_minutes, 2880);
    assert.equal(snapshot.projects.find(p=>p.id===firstProject).config.schedule.issues_minutes, 10080);
    const provider = snapshot.config.providers.find(p=>p.name==='测试目录服务');
    const selectRoute=async(role,provider,model)=>{
      await page.locator('#mc-role-model-'+role).click();
      await page.locator('#mc-picker-provider').selectOption(provider);
      await page.locator('#mc-picker-search').fill(model);
      await page.locator('#mc-picker-list .mc-picker-option').filter({hasText:model}).click();
    };
    const planning = page.locator('#mc-role-model-planning');
    const coding = page.locator('#mc-role-model-implementation');
    await selectRoute('planning',provider.id,'fixture-planner');
    await selectRoute('implementation',provider.id,'fixture-coder');
    await page.locator('#mc-role-list .mc-effort-wrap .mc-text-button').first().click();
    await page.locator('#mc-advanced-effort select').selectOption('high');
    await page.locator('#mc-advanced-form button[type="submit"]').click();
    await page.locator('#mc-save-routes').click();
    await page.locator('#mc-feedback').filter({hasText:'模型分工已保存，从下一轮任务生效'}).waitFor();
    snapshot = await page.evaluate(async () => (await fetch('/api/state')).json());
    assert.equal(snapshot.config.models.planning.effort, 'high');
    assert.equal(snapshot.config.models.implementation.effort, 'default');
    // 高级连接选项真实保存；隐藏的环境变量字段不能阻断其他认证方式。
    await page.locator('#mc-add-provider').click();
    await page.locator('#mc-input-name').fill('浏览器连接验证');
    await page.locator('#mc-input-url').fill(provider.base_url);
    await page.locator('#mc-connection-advanced summary').click();
    await page.locator('#mc-input-protocol').selectOption('chat_completions');
    await page.locator('#mc-input-auth').selectOption('env');
    await page.locator('#mc-input-env').fill('INVALID-NAME');
    await page.locator('#mc-provider-submit').click();
    assert.equal(await page.locator('#mc-input-env').evaluate(node=>node.validity.patternMismatch), true);
    await page.locator('#mc-input-auth').selectOption('none');
    await page.locator('#mc-input-models').fill('fixture-manual');
    await page.locator('#mc-provider-submit').click();
    await page.locator('#mc-provider-dialog').waitFor({state:'hidden'});
    await page.locator('#mc-model-list').filter({hasText:'fixture-manual'}).waitFor();
    snapshot = await page.evaluate(async () => (await fetch('/api/state')).json());
    const savedProvider = snapshot.config.providers.find(p=>p.name==='浏览器连接验证');
    assert.equal(savedProvider.protocol, 'chat_completions');
    assert.equal(savedProvider.credential, 'none');
    assert.equal(savedProvider.env_key, '');
    await page.locator('[data-mc-tab="roles"]').click();
    await page.locator('[data-settings-scope="project"]').click();
    await selectRoute('implementation',provider.id,'fixture-planner');
    await page.locator('#mc-save-routes').click();
    await page.locator('#mc-feedback').filter({hasText:'模型分工已保存，从下一轮任务生效'}).waitFor();
    snapshot = await page.evaluate(async () => (await fetch('/api/state')).json());
    assert.equal(snapshot.config.models.implementation.model, 'fixture-coder');
    assert.equal(snapshot.projects.find(p=>p.id===firstProject).config.models.implementation.model, 'fixture-planner');
    await selectRoute('review',provider.id,'fixture-planner');
    await page.locator('[data-settings-scope="global"]').click();
    await selectRoute('planning',provider.id,'fixture-coder');
    await page.locator('#mc-save-routes').click();
    await page.locator('#mc-feedback').filter({hasText:'模型分工已保存，从下一轮任务生效'}).waitFor();
    await page.locator('[data-settings-scope="project"]').click();
    await page.waitForFunction(id=>document.getElementById('mc-role-model-planning')?.value===JSON.stringify([id,'fixture-coder']),provider.id);
    assert.equal(await coding.getAttribute('value'),JSON.stringify([provider.id, 'fixture-planner']));
    assert.equal(await page.locator('#mc-role-model-review').getAttribute('value'),JSON.stringify([provider.id, 'fixture-planner']));
    assert.equal(await page.locator('#mc-save-routes').isEnabled(),true);
    await page.locator('#mc-undo').click();
    assert.equal(await page.locator('#mc-save-routes').isDisabled(),true);
    await page.locator('.brand').click();
    await page.locator('#goal-form button[type="submit"]').click();
    await page.locator('#toast').filter({hasText: '目标已入队'}).waitFor();
    await page.locator('[data-task-filter="active"]').click();
    await page.locator('#tasks-page').filter({hasText: '56'}).waitFor().catch(async error=>{console.error('Task page:',await page.locator('#tasks-page').innerText());console.error('Counts:',await page.evaluate(async()=>{const s=await(await fetch('/api/state')).json();return s.task_counts;}));throw error;});
    await page.locator('[data-task-filter="attention"]').click();
    await page.locator('#tasks-list .task-row').filter({hasText:'等待修复线上检查'}).filter({hasText:'线上检查未通过'}).waitFor();
    await page.locator('#language-select').selectOption('en');
    await page.locator('#tasks-list .task-row').filter({hasText:'等待修复线上检查'}).filter({hasText:'GitHub checks failed'}).waitFor();
    await page.locator('#language-select').selectOption('zh-CN');
    await page.locator('#tasks-list .task-title').filter({hasText: 'Fix addition'}).click();
    await page.locator('#detail-dialog[open] .stage-progress .done').first().waitFor();
    assert.equal(await page.locator('#detail-dialog .stage-progress .done').count(), 4);
    await page.locator('#detail-dialog summary').filter({hasText: '检查过程与运行记录'}).click();
    await page.locator('#detail-dialog summary').filter({hasText: '运行与效果检查'}).click();
    await page.locator('#detail-dialog .close-dialog').click();
    await page.locator('[data-task-filter="active"]').click();
    await page.locator('#tasks-page').filter({hasText: '56'}).waitFor();
    await page.locator('#tasks-next').click();
    await page.locator('#tasks-page').filter({hasText: '51'}).waitFor();
    assert.equal(await page.locator('#tasks-list .task-row').count(), 6);
    await page.locator('#project-select').selectOption(otherProject);
    await page.locator('#tasks-list').filter({hasText: '另一个项目专属任务'}).waitFor();
    assert.equal(await page.locator('#tasks-list .task-row').filter({hasText:'等待修复线上检查'}).count(),0);
    assert.equal(await page.locator('#tasks-list .task-row').count(), 1);
    await page.locator('#edit-schedule').click();
    assert.equal(await page.locator('#schedule-mode').inputValue(), 'inherit');
    await page.locator('#schedule-mode').selectOption('custom');
    await page.locator('[data-schedule-preset="light"]').selectOption('1440');
    await saveSettings();
    await page.locator('#toast').filter({hasText:'已保存'}).waitFor();
    // 恢复通用后应删除覆盖，并跟随之后的通用修改。
    await page.locator('#schedule-mode').selectOption('inherit');
    await saveSettings();
    await page.locator('#toast').filter({hasText:'已保存'}).waitFor();
    await page.locator('#global-settings').click();
    await page.locator('[data-schedule-preset="light"]').selectOption('720');
    await saveSettings();
    await page.locator('#toast').filter({hasText:'已保存'}).waitFor();
    snapshot = await page.evaluate(async () => (await fetch('/api/state')).json());
    assert.equal(snapshot.projects.find(p=>p.id!==firstProject).config.schedule.light_minutes, 720);
    assert.equal(snapshot.projects.find(p=>p.id===firstProject).config.schedule.light_minutes, 2880);
    assert.equal(snapshot.projects.find(p=>p.id!==firstProject).config.models.implementation.model, 'fixture-coder');
    await page.locator('.brand').click();
    await page.locator('#language-select').selectOption('en');
    await page.locator('[data-task-filter="active"]').filter({hasText: 'In progress'}).waitFor();
    await page.setViewportSize({width: 390, height: 844});
    await page.locator('#project-settings').click();
    await page.locator('#delivery-choice').selectOption('local');
    await saveSettings();
    await page.locator('#toast').filter({hasText: 'Saved'}).waitFor();
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
    await page.locator('.brand').click();
    await page.locator('#project-select').selectOption(firstProject);
    await page.locator('#language-select').selectOption('zh-CN');
    await page.locator('[data-task-filter="attention"]').click();
    await page.locator('#tasks-list .task-title').filter({hasText:'Fix addition'}).click();
    await page.locator('#detail-dialog .task-outcome').waitFor();
    const mobileDetail=await page.locator('#detail-dialog').boundingBox();
    assert.equal(mobileDetail.x,0);assert.equal(mobileDetail.width,390);
    assert.equal(await page.locator('#detail-dialog .task-outcome').filter({hasText:'代码评审'}).count(),1);
    if(process.env.RSI_SCREENSHOT_DIR){fs.mkdirSync(process.env.RSI_SCREENSHOT_DIR,{recursive:true});await page.screenshot({path:process.env.RSI_SCREENSHOT_DIR+'/mobile-task.png'});}
    await page.locator('#detail-dialog .close-dialog').click();
    await page.setViewportSize({width:320,height:740});
    await page.locator('#global-settings').click();
    await page.locator('#mc-add-provider').click();
    await page.locator('#mc-connection-advanced summary').click();
    await page.locator('#mc-input-auth').selectOption('env');
    assert(await page.locator('#mc-provider-dialog').evaluate(node=>node.scrollWidth<=node.clientWidth+1));
    if(process.env.RSI_SCREENSHOT_DIR)await page.screenshot({path:process.env.RSI_SCREENSHOT_DIR+'/mobile-advanced.png'});
    await page.locator('#mc-provider-dialog .close-dialog').click();
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
    if (process.env.RSI_SCREENSHOT_DIR) {
      await page.setViewportSize({width:390,height:844});
      await page.locator('#language-select').selectOption('en');
      fs.mkdirSync(process.env.RSI_SCREENSHOT_DIR, {recursive: true});
      await page.screenshot({path: process.env.RSI_SCREENSHOT_DIR + '/mobile-settings.png', fullPage: true});
      await page.setViewportSize({width: 1380, height: 1000});
      await page.locator('#language-select').selectOption('zh-CN');
      await page.locator('.brand').click();
      await page.screenshot({path: process.env.RSI_SCREENSHOT_DIR + '/desktop-workspace.png', fullPage: true});
    }
    await checkAccountAndReleaseUI(browser,url,errors);
    assert.deepEqual(errors, []);
    const state = await page.evaluate(async () => (await fetch('/api/state')).json());
    assert(state.projects.every(project => project.paused));
    assert.equal(state.usage.length, 0);
    console.log('Browser checks passed: forms, hot settings, pagination, project isolation, languages, mobile, account authorization, release authorization, zero paid calls.');
  } finally {
    if (browser) await browser.close();
    server.stdin.end('\n');
    const ended = new Promise(resolve => server.once('exit', resolve));
    const timeout = setTimeout(() => server.kill('SIGTERM'), 5000);
    if (server.exitCode === null && server.signalCode === null) await ended;
    clearTimeout(timeout);
  }
}
main().catch(error => {console.error(error); process.exitCode = 1;});
