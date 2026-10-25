const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const {createInterface} = require('node:readline');
const {chromium} = require('playwright');

const reply = (route, data, status = 200) => route.fulfill({status, contentType: 'application/json', body: JSON.stringify(data)});

async function checkReadiness(browser, url, staleFails, errors) {
  const page = await browser.newPage();
  page.on('pageerror', error => errors.push(error.message));
  let oldRequest, count = 0;
  await page.route('**/api/readiness?*', route => {
    if (++count === 1) { oldRequest = route; return; }
    return reply(route, {ready: true, items: []});
  });
  try {
    await page.goto(url);
    await page.locator('#project-status').filter({hasText: '已暂停'}).waitFor();
    await page.waitForFunction(() => document.getElementById('readiness-summary').textContent.includes('正在检查'));
    await page.locator('#project-settings').click();
    await page.locator('#settings-save').click();
    await page.waitForFunction(() => !document.getElementById('settings-save').disabled);
    await page.waitForFunction(() => document.getElementById('readiness-summary').textContent.includes('基础条件已就绪'));
    await reply(oldRequest, staleFails ? {error: 'outdated-readiness-error'} : {ready: false, items: []}, staleFails ? 500 : 200);
    await page.waitForTimeout(100);
    assert.match(await page.locator('#readiness-summary').textContent(), /基础条件已就绪/);
    assert.doesNotMatch(await page.locator('#toast').textContent(), /outdated-readiness-error/);
    await page.waitForTimeout(5200);
    assert.equal(count, 2, 'stale completion must not discard the successful readiness cache');
  } finally { await page.close(); }
}

async function checkModelForms(browser, url, errors) {
  const page = await browser.newPage({viewport: {width: 1100, height: 900}});
  page.on('pageerror', error => errors.push(error.message));
  const posts = [];
  let pending;
  await page.route('**/api/model-center/model', route => {
    posts.push(route.request().postDataJSON());
    if (!pending) { pending = route; return; }
    return route.continue();
  });
  const finish = async () => {
    const response = page.waitForResponse(response => response.url().endsWith('/api/model-center/model') && response.request().method() === 'POST');
    await pending.continue();
    assert.equal((await response).status(), 200);
  };
  const saveAgain = async button => {
    const response = page.waitForResponse(response => response.url().endsWith('/api/model-center/model') && response.request().method() === 'POST');
    await button.click();
    assert.equal((await response).status(), 200);
  };
  try {
    await page.goto(url);
    await page.locator('#project-status').filter({hasText: '已暂停'}).waitFor();
    await page.locator('#project-settings').click();
    await page.locator('[data-mc-tab="providers"]').click();
    await page.locator('#mc-provider-list .mc-provider').filter({hasText: '测试目录服务'}).click();
    await page.locator('#mc-add-model').click();
    const custom = page.locator('#mc-custom-form'), add = custom.locator('button[type="submit"]');
    await page.locator('#mc-custom-id').fill('hardening-fixture-model');
    await page.locator('#mc-custom-name').fill('Original fixture name');
    await add.dblclick();
    await page.locator('#mc-custom-name').fill('Latest fixture name');
    await custom.dispatchEvent('submit');
    await page.waitForTimeout(100);
    assert.equal(posts.length, 1);
    assert.equal(await add.isDisabled(), true);
    await finish();
    await page.waitForFunction(() => !document.querySelector('#mc-custom-form button[type="submit"]').disabled);
    assert.equal(await page.locator('#mc-custom-name').inputValue(), 'Latest fixture name');
    await saveAgain(add);
    await page.locator('#mc-custom-dialog').waitFor({state: 'hidden'});
    const row = page.locator('#mc-model-list .mc-model-row').filter({hasText: 'hardening-fixture-model'});
    await row.locator('strong').filter({hasText: 'Latest fixture name'}).waitFor();
    assert.deepEqual(posts.map(post => post.model.name), ['Original fixture name', 'Latest fixture name']);
    await row.locator('summary').click();
    await row.getByRole('button', {name: '估算价格', exact: true}).click();
    const price = page.locator('#mc-price-form'), save = price.locator('button[type="submit"]');
    await page.locator('#mc-price-input').fill('1');
    pending = null;
    await save.dblclick();
    await page.locator('#mc-price-input').fill('2');
    await price.dispatchEvent('submit');
    await page.waitForTimeout(100);
    assert.equal(posts.length, 3);
    assert.equal(await save.isDisabled(), true);
    await finish();
    await page.waitForFunction(() => !document.querySelector('#mc-price-form button[type="submit"]').disabled);
    assert.equal(await page.locator('#mc-price-input').inputValue(), '2');
    await saveAgain(save);
    await page.locator('#mc-price-dialog').waitFor({state: 'hidden'});
    await row.filter({hasText: '$2 / 1M tokens'}).waitFor();
    assert.deepEqual(posts.slice(2).map(post => post.model.unit_price), [1, 2]);
    await page.setViewportSize({width: 390, height: 844});
    await page.locator('#language-select').selectOption('en');
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), true);
  } finally { await page.close(); }
}

async function checkDraftRecovery(browser, url, errors) {
  const page=await browser.newPage();page.on('pageerror',error=>errors.push(error.message));
  const secret='BROWSER-SECRET-MUST-NOT-PERSIST';
  try{
    await page.goto(url);
    await page.locator('#project-status').filter({hasText:'已暂停'}).waitFor();
    await page.locator('#goal-text').fill('Unsubmitted browser goal draft');
    await page.reload();
    await page.locator('#project-status').filter({hasText:'已暂停'}).waitFor();
    assert.equal(await page.locator('#goal-text').inputValue(),'Unsubmitted browser goal draft');

    await page.locator('#project-settings').click();
    await page.locator('#schedule-mode').selectOption('custom');
    const interval=page.locator('[data-path="schedule.light_minutes"]');
    await interval.fill('125');
    await page.locator('#advanced-settings > summary').click();
    await page.locator('#work-start').fill('8');
    await page.locator('#work-end').fill('17');
    await page.locator('#issue-labels').fill('bug, urgent');
    const checks=page.locator('#checks-json'),serverChecks=await checks.inputValue();
    await checks.fill('["RAW-CHECKS-MUST-NOT-PERSIST"]');
    await page.waitForFunction(()=>sessionStorage.getItem('rsi-ui-drafts')?.includes('125'));
    assert.equal(await page.evaluate(()=>sessionStorage.getItem('rsi-ui-drafts')?.includes('bug, urgent')||false),true);
    assert.equal(await page.evaluate(()=>sessionStorage.getItem('rsi-ui-drafts')?.includes('RAW-CHECKS-MUST-NOT-PERSIST')||false),false);
    const secretField=page.locator('[data-mc-tab="providers"]');
    await secretField.click();
    await page.locator('#mc-add-provider').click();
    await page.locator('#mc-input-key').fill(secret);
    assert.equal(await page.evaluate(()=>sessionStorage.getItem('rsi-ui-drafts')?.includes('BROWSER-SECRET-MUST-NOT-PERSIST')||false),false);

    await page.reload();
    await page.locator('#project-status').filter({hasText:'已暂停'}).waitFor();
    assert.equal(await page.locator('#goal-text').inputValue(),'Unsubmitted browser goal draft');
    await page.locator('#project-settings').click();
    await page.locator('#schedule-mode').waitFor();
    assert.equal(await page.locator('#schedule-mode').inputValue(),'custom');
    assert.equal(await page.locator('[data-path="schedule.light_minutes"]').inputValue(),'125');
    await page.locator('#advanced-settings > summary').click();
    assert.equal(await page.locator('#work-start').inputValue(),'8');
    assert.equal(await page.locator('#work-end').inputValue(),'17');
    assert.equal(await page.locator('#issue-labels').inputValue(),'bug, urgent');
    assert.equal(await page.locator('#checks-json').inputValue(),serverChecks);
    assert.doesNotMatch(await page.evaluate(()=>sessionStorage.getItem('rsi-ui-drafts')||''),/RAW-CHECKS-MUST-NOT-PERSIST/);

    await page.locator('#back-workspace').click();
    const saved=page.waitForResponse(response=>response.url().endsWith('/api/goal')&&response.request().method()==='POST');
    await page.locator('#goal-form button[type="submit"]').click();
    assert.equal((await saved).status(),200);
    await page.waitForFunction(()=>document.getElementById('goal-text').value==='');
    await page.reload();
    await page.locator('#project-status').filter({hasText:'已暂停'}).waitFor();
    assert.equal(await page.locator('#goal-text').inputValue(),'');
  }finally{await page.close();}
}

async function checkInvalidConfigRepair(browser,url,errors) {
  const page=await browser.newPage();page.on('pageerror',error=>errors.push(error.message));
  let invalid=true;const repairs=[];
  await page.route('**/api/state*',async route=>{
    const response=await route.fetch(),data=await response.json();
    if(invalid){const project=data.projects.find(row=>row.id===data.selected_project)||data.projects[0];project.configuration_error='Malformed fixture override';project.paused=true;}
    await route.fulfill({response,body:JSON.stringify(data)});
  });
  await page.route('**/api/config/repair',async route=>{
    repairs.push(route.request().postDataJSON());invalid=false;
    await reply(route,{saved:true,repaired:true,paused:true});
  });
  try{
    await page.goto(url);
    await page.locator('#project-config-error').filter({hasText:'项目配置异常'}).waitFor();
    const button=page.locator('#project-config-error').getByRole('button',{name:'重置损坏设置'});
    await button.waitFor();
    const warning=await page.locator('#project-config-error').textContent();
    assert.match(warning,/本地审计备份/);assert.match(warning,/仍保持暂停/);
    await page.locator('#language-select').selectOption('en');
    await page.locator('#project-config-error').getByRole('button',{name:'Reset damaged settings'}).waitFor();
    assert.match(await page.locator('#project-config-error').textContent(),/local audit backup/);
    await page.locator('#language-select').selectOption('zh-CN');
    await button.click();
    const confirmBox=page.locator('.confirm-dialog');
    await confirmBox.waitFor();
    const confirmation=await confirmBox.locator('p').textContent();
    assert.match(confirmation,/原始配置仅保存在本地审计备份/);assert.match(confirmation,/需要你手动启动/);
    await confirmBox.getByRole('button',{name:'确认'}).click();
    await page.locator('#project-config-error').waitFor({state:'hidden'});
    assert.equal(repairs.length,1);
    assert.equal(repairs[0].project,await page.locator('#project-select').inputValue());
    assert.equal(repairs[0].confirmation,'reset-invalid-overrides-to-global-defaults');
    assert.equal(await page.locator('#pause-button').textContent(),'开始运行');
  }finally{await page.close();}
}

async function checkReadinessInstallAction(browser,url,errors) {
  const page=await browser.newPage();page.on('pageerror',error=>errors.push(error.message));
  await page.route('**/api/readiness?*',route=>reply(route,{ready:false,items:[{
    code:'tools',ready:false,action:'install',reason:'tools_missing',missing:['fixture-tool'],detail:'Missing fixture-tool'
  }]}));
  try{
    await page.goto(url);
    await page.locator('#project-status').filter({hasText:'已暂停'}).waitFor();
    const action=page.locator('#readiness-items button').filter({hasText:'检查运行环境'});
    await action.waitFor();await action.click();
    await page.locator('#view-settings').waitFor({state:'visible'});
    assert.equal(await page.locator('#advanced-settings').evaluate(node=>node.open),true);
    assert.equal(await page.locator('#capabilities').evaluate(node=>node.closest('details').open),true);
  }finally{await page.close();}
}

async function checkRunProgress(browser,url,errors) {
  const page=await browser.newPage();page.on('pageerror',error=>errors.push(error.message));
  await page.route('**/api/state*',async route=>{
    const response=await route.fetch(),data=await response.json();
    const project=data.projects.find(row=>row.id===data.selected_project)||data.projects[0];
    if(project){
      project.paused=false;
      const task=data.tasks[0]||{id:'fixture-live-task',title:'验证运行阶段'};
      data.runs=[{id:'fixture-live-run',project:project.id,task:task.id,state:'evaluating',
        started:Date.now()/1000-125,ended:null,last_stage:'review',last_stage_at:Date.now()/1000-5}];
    }
    await route.fulfill({response,body:JSON.stringify(data)});
  });
  try{
    await page.goto(url);
    await page.locator('#current-work-detail').filter({hasText:'正在评审改动'}).waitFor();
    const text=await page.locator('#current-work-detail').textContent();
    assert.match(text,/已运行/);assert.match(text,/最近进展/);
  }finally{await page.close();}
}

async function main() {
  const server = spawn(process.env.PYTHON || 'python3', ['-m', 'tests.browser_fixture'], {
    stdio: ['pipe', 'pipe', 'pipe'], env: {...process.env, PYTHONDONTWRITEBYTECODE: '1'}
  });
  server.stderr.on('data', chunk => process.stderr.write(chunk));
  let browser;
  const errors = [];
  try {
    const url = await new Promise((resolve, reject) => {
      const timeout = setTimeout(() => reject(Error('Console startup timed out')), 30000);
      createInterface({input: server.stdout}).once('line', line => {clearTimeout(timeout); resolve(line);});
      server.once('exit', code => {clearTimeout(timeout); reject(Error('Console exited: ' + code));});
    });
    browser = await chromium.launch(process.env.RSI_BROWSER_CHANNEL ? {channel: process.env.RSI_BROWSER_CHANNEL} : {});
    await checkReadiness(browser, url, false, errors);
    await checkReadiness(browser, url, true, errors);
    await checkReadinessInstallAction(browser,url,errors);
    await checkRunProgress(browser,url,errors);
    await checkModelForms(browser, url, errors);
    await checkDraftRecovery(browser,url,errors);
    await checkInvalidConfigRepair(browser,url,errors);
    assert.deepEqual(errors, []);
    console.log('Delayed readiness and model form checks passed.');
  } finally {
    if (browser) await browser.close();
    server.stdin.end('\n');
    await new Promise(resolve => server.once('exit', resolve));
  }
}

main().catch(error => {console.error(error); process.exitCode = 1;});
