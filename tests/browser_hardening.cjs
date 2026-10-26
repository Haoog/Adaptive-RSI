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

async function checkProviderSaveRace(browser, url, errors, reopen = false) {
  const page = await browser.newPage();
  page.on('pageerror', error => errors.push(error.message));
  const posts = [];
  let release, arrived;
  const gate = new Promise(resolve => release = resolve), received = new Promise(resolve => arrived = resolve);
  let created;
  await page.route('**/api/model-center/provider', async route => {
    posts.push(route.request().postDataJSON());
    const response = await route.fetch();
    if (posts.length === 1) { created = await response.json(); arrived(); await gate; }
    await route.fulfill({response});
  });
  try {
    await page.goto(url);
    await page.locator('#project-status').filter({hasText: '已暂停'}).waitFor();
    await page.locator('#project-settings').click();
    await page.locator('[data-settings-scope="project"]').click();
    await page.locator('[data-mc-tab="providers"]').click();
    await page.locator('#view-models[data-scope]:not([data-scope="global"])').waitFor();
    const project = await page.locator('#project-select').inputValue();
    const snapshot = await (await page.request.get(new URL('/api/model-center?project=' + project, url).href)).json();
    const endpoint = snapshot.providers.find(provider => provider.name === '测试目录服务').base_url;
    const original = reopen ? 'Previous scope provider' : 'Pending provider creation';
    const revised = reopen ? 'New scope provider' : 'Edited provider creation';
    await page.locator('#mc-add-provider').click();
    await page.locator('#mc-input-name').fill(original);
    await page.locator('#mc-input-url').fill(endpoint);
    await page.locator('#mc-connection-advanced summary').click();
    await page.locator('#mc-input-protocol').selectOption('responses');
    await page.locator('#mc-input-auth').selectOption('none');
    await page.locator('#mc-provider-submit').click();
    await received;
    if (reopen) {
      await page.locator('#mc-provider-dialog .close-dialog').click();
      await page.locator('[data-settings-scope="global"]').click();
      await page.locator('#view-models[data-scope="global"]').waitFor();
      await page.locator('#mc-add-provider').click();
      await page.locator('#mc-input-url').fill(endpoint);
      await page.locator('#mc-connection-advanced summary').click();
      await page.locator('#mc-input-protocol').selectOption('responses');
      await page.locator('#mc-input-auth').selectOption('none');
    }
    await page.locator('#mc-input-name').fill(revised);
    release();
    await page.locator('#mc-provider-submit:not([disabled])').waitFor();
    assert.equal(await page.locator('#mc-provider-dialog').isVisible(), true);
    assert.equal(await page.locator('#mc-input-name').inputValue(), revised);
    assert.equal(await page.locator('#mc-input-url').inputValue(), endpoint);
    assert.equal(await page.locator('#mc-input-auth').inputValue(), 'none');
    assert.equal(await page.locator('#mc-input-key').inputValue(), '');
    await page.locator('#mc-provider-submit').click();
    await page.locator('#mc-provider-dialog').waitFor({state: 'hidden'});
    assert.equal(posts.length, 2);
    assert.equal(posts[0].provider.id, undefined);
    assert.equal(posts[1].provider.id, reopen ? undefined : created.id, 'the acknowledgement must belong only to its originating editor');
    assert.deepEqual(posts.map(post => post.project), [project, reopen ? null : project]);
    const saved = await (await page.request.get(new URL('/api/model-center?project=' + project, url).href)).json();
    const connections = saved.providers.filter(provider => [original, revised].includes(provider.name));
    assert.deepEqual(connections.map(provider => ({id: provider.id, name: provider.name})), [{id: created.id, name: reopen ? original : revised}]);
    const global = await (await page.request.get(new URL('/api/model-center', url).href)).json();
    assert.equal(global.providers.some(provider => provider.id === created.id), false);
    const newer = global.providers.filter(provider => provider.name === revised);
    assert.equal(newer.length, reopen ? 1 : 0);
    if (reopen) assert.notEqual(newer[0].id, created.id);
  } finally { release(); await page.close(); }
}

async function checkRouteSaveRace(browser,url,errors) {
  const page=await browser.newPage();page.on('pageerror',error=>errors.push(error.message));
  let release,arrived;const gate=new Promise(resolve=>release=resolve),received=new Promise(resolve=>arrived=resolve);
  let posts=0;
  await page.route('**/api/model-center/routes',async route=>{posts++;const response=await route.fetch();arrived();await gate;await route.fulfill({response});});
  try{
    await page.goto(url);await page.locator('#project-status').filter({hasText:'已暂停'}).waitFor();
    await page.locator('#project-settings').click();
    const effort=page.locator('#mc-role-effort-implementation');
    const original=await effort.inputValue();
    await effort.selectOption('default');await page.locator('#mc-save-routes').click();await received;
    await effort.locator('xpath=ancestor::*[contains(@class,"mc-role-row")]').getByRole('button',{name:'高级参数',exact:true}).click();
    await page.locator('#mc-advanced-effort select').selectOption(original);
    await page.locator('#mc-advanced-form button[type="submit"]').click();
    assert.equal(await page.locator('#mc-save-routes').isDisabled(),true,'editing during save must not allow overlapping submissions');
    release();
    await page.locator('#mc-save-routes:not([disabled])').waitFor();
    assert.equal(await effort.inputValue(),original,'acknowledgment must preserve a newer same-role draft');
    await page.locator('#mc-dirty').filter({hasText:'未保存'}).waitFor();
    assert.equal(posts,1);
    await page.unroute('**/api/model-center/routes');
    await page.locator('#mc-save-routes').click();
    await page.locator('#mc-dirty').filter({hasText:'模型分工已保存'}).waitFor();
    await page.reload();await page.locator('#project-status').filter({hasText:'已暂停'}).waitFor();await page.locator('#project-settings').click();
    assert.equal(await effort.inputValue(),original);
  }finally{release();await page.close();}
}

async function checkUpdatePending(browser,url,errors) {
  const page=await browser.newPage();page.on('pageerror',error=>errors.push(error.message));
  let checked=false;
  await page.route('**/api/state*',async route=>{
    const response=await route.fetch(),data=await response.json();
    data.update={state:checked?'checking':'error',busy:false,channel:'managed',...(checked?{}:{reason:'previous-check-error'})};
    await route.fulfill({response,body:JSON.stringify(data)});
  });
  await page.route('**/api/updates/check',route=>{checked=true;return reply(route,{started:true,update:{state:'checking',busy:false,channel:'managed'}});});
  try{
    await page.goto(url);await page.locator('#project-status').filter({hasText:'已暂停'}).waitFor();
    await page.locator('#project-settings').click();
    await page.locator('#advanced-settings > summary').click();
    await page.locator('#update-status').filter({hasText:'previous-check-error'}).waitFor();
    await page.locator('#check-update').click();
    await page.locator('#update-status').filter({hasText:'正在检查更新'}).waitFor();
    assert.doesNotMatch(await page.locator('#update-status').textContent(),/previous-check-error/);
    assert.equal(await page.locator('#check-update').isDisabled(),true);
    assert.equal(await page.locator('#apply-update').isDisabled(),true);
    await page.locator('#language-select').selectOption('en');
    await page.locator('#update-status').filter({hasText:'Checking for updates'}).waitFor();
    assert.equal(await page.locator('#settings-save').isDisabled(),false);
  }finally{await page.close();}
}

async function checkUpdateRateLimit(browser,url,errors) {
  const page=await browser.newPage({timezoneId:'Asia/Shanghai'});
  page.on('pageerror',error=>errors.push(error.message));
  const reset=1792998274,target='99.0.0',requests=[];
  // Match UpdateManager.rate_limit_failure and snapshot; the cooldown is an error,
  // not a separate state, and retains the approved installation target.
  const update={state:'error',current:'0.5.110',version:target,requested:true,failures:0,
    checked:reset-1800,reason:'GitHub HTTP 403',reason_code:'github_primary_rate_limit',
    rate_limit_until:reset,busy:false,channel:'managed',desktop_managed:false};
  await page.clock.setFixedTime(new Date((reset-1)*1000));
  await page.route('**/api/state*',async route=>{
    const response=await route.fetch(),data=await response.json();
    data.update=update;
    await route.fulfill({response,body:JSON.stringify(data)});
  });
  await page.route('**/api/updates/*',route=>{
    requests.push(new URL(route.request().url()).pathname);
    assert.equal(route.request().method(),'POST');
    assert.equal(requests.at(-1),'/api/updates/check','cooldown must never submit an installation');
    return reply(route,{started:true,update:{state:'checking',busy:false,channel:'managed'}});
  });
  const screenshot=async name=>{
    if(process.env.RSI_SCREENSHOT_DIR)await page.locator('#update-status').locator('..').screenshot({path:process.env.RSI_SCREENSHOT_DIR+'/'+name+'.png'});
  };
  const verify=async(locale,expired)=>{
    const text=await page.locator('#update-status').textContent();
    const retryTime=new Date(reset*1000).toLocaleString(locale,{timeZone:'Asia/Shanghai'});
    assert.ok(text.includes(retryTime),'retry deadline must be displayed in the selected language and local timezone');
    assert.ok(text.includes(target),'the approved target must remain visible');
    assert.match(text,locale==='en'?/GitHub anonymous API quota is exhausted/:/GitHub 匿名 API 额度已用尽/);
    assert.match(text,expired?(locale==='en'?/ready to retry/:/已到，可重试/):(locale==='en'?/paused until then/:/此时间前暂停更新请求/));
    assert.equal(await page.locator('#check-update').isDisabled(),!expired);
    assert.equal(await page.locator('#apply-update').isDisabled(),true);
    assert.equal(await page.locator('#settings-save').isDisabled(),false,'cooldown must not block ordinary settings');
    assert.deepEqual(requests,[],'rendering, language changes and expiry must not issue update requests');
    await screenshot('update-cooldown-'+locale+(expired?'-expired':''));
  };
  try{
    await page.goto(url);await page.locator('#project-status').filter({hasText:'已暂停'}).waitFor();
    await page.locator('#project-settings').click();
    await page.locator('#advanced-settings > summary').click();
    await page.locator('#update-status').filter({hasText:'GitHub 匿名 API 额度已用尽'}).waitFor();
    await verify('zh-CN',false);
    await page.locator('#language-select').selectOption('en');
    await page.locator('#update-status').filter({hasText:'GitHub anonymous API quota is exhausted'}).waitFor();
    await verify('en',false);
    // Jump to the exact deadline without a real wait or invoking application functions.
    // A normal language change rerenders the unchanged intercepted cooldown snapshot.
    await page.clock.setFixedTime(new Date(reset*1000));
    await page.locator('#language-select').selectOption('zh-CN');
    await page.locator('#update-status').filter({hasText:'已到，可重试'}).waitFor();
    await verify('zh-CN',true);
    await page.locator('#language-select').selectOption('en');
    await page.locator('#update-status').filter({hasText:'ready to retry'}).waitFor();
    await verify('en',true);
    await page.locator('#check-update').click();
    await page.locator('#update-status').filter({hasText:'Checking for updates'}).waitFor();
    assert.deepEqual(requests,['/api/updates/check']);
    assert.equal(await page.locator('#check-update').isDisabled(),true);
    assert.equal(await page.locator('#apply-update').isDisabled(),true);
  }finally{await page.close();}
}

async function checkPendingSettingsDraft(browser, url, errors) {
  for (const scenario of ['project-away', 'project-round-trip', 'scope-away', 'failure-away', 'project-committed', 'global-committed']) {
    const page = await browser.newPage();
    page.on('pageerror', error => errors.push(error.message));
    let release, arrived;
    const gate = new Promise(resolve => release = resolve), received = new Promise(resolve => arrived = resolve);
    const posts = [], committed = scenario.endsWith('-committed'), global = scenario === 'global-committed';
    await page.route('**/api/config', async route => {
      posts.push(route.request().postDataJSON());
      // Cover both orders: navigation before commit, and navigation GET after commit
      // while the POST acknowledgement is still held.
      const earlyResponse = committed ? await route.fetch() : null;
      if (earlyResponse) assert.equal(earlyResponse.status(), 200, await earlyResponse.text());
      if (posts.length === 1) {
        arrived(); await gate;
        if (scenario === 'failure-away') return reply(route, {error: 'Project not found'}, 404);
      }
      const response = earlyResponse || await route.fetch(); assert.equal(response.status(), 200, await response.text()); await route.fulfill({response});
    });
    try {
      await page.goto(url);
      await page.locator('#project-status').filter({hasText: '已暂停'}).waitFor();
      await page.locator('#project-settings').click();
      if (global) await page.locator('[data-settings-scope="global"]').click();
      await page.locator('#advanced-settings > summary').click();
      const weight = page.locator('[data-path="schedule.weight"]');
      const project = await page.locator('#project-select').inputValue(), key = global ? 'global' : project;
      const other = await page.locator('#project-select option').evaluateAll((options, id) => options.find(option => option.value !== id).value, project);
      const original = await weight.inputValue(), submitted = original === '2' ? '3' : '2';
      await weight.fill(submitted); await page.locator('#settings-save').click(); await received;
      await weight.fill(original);
      const navigate = async returning => {
        if (scenario === 'scope-away' || global) await page.locator('[data-settings-scope="' + (returning !== global ? 'project' : 'global') + '"]').click();
        else await page.locator('#project-select').selectOption(returning ? project : other);
        if (global) await page.evaluate(() => refresh());
        // Settle the navigation GET before releasing the save, so this test controls
        // the save acknowledgement rather than racing an unrelated state response.
        await page.waitForFunction(() => !refreshPromise && state.selected_project === document.getElementById('project-select').value);
      };
      const leave = () => navigate(false), back = () => navigate(true);
      await leave(); await weight.fill('4');
      if (scenario === 'project-round-trip' || committed) await back();
      const beforeAck = committed ? {baseline: await page.evaluate(() => settingsBaseline.schedule.weight), value: await weight.inputValue()} : null;
      release(); await page.locator('#settings-save:not([disabled])').waitFor();
      if (committed) {
        assert.equal(beforeAck.baseline, Number(submitted), 'navigation GET must see the committed save before its POST acknowledgement');
        assert.equal(beforeAck.value, original, 'the pending save must not conflict with its own newer draft');
      }
      if (scenario !== 'project-round-trip' && !committed) {
        assert.equal(await weight.inputValue(), '4', 'acknowledgement must preserve the other scope');
        await back();
      }
      assert.equal(await weight.inputValue(), original, 'post-submit return to the old baseline must survive navigation');
      const draft = await page.evaluate(id => window.RSIDraftStore.get('settings', id), key);
      if (scenario === 'failure-away') assert.equal(draft, null, 'failure cleans the draft against the old baseline');
      else {
        assert.ok(draft, JSON.stringify({scenario, original, submitted, posts, baseline: await weight.inputValue(), storage: await page.evaluate(() => sessionStorage.getItem('rsi-ui-drafts'))}));
        const saved = draft.values.find(entry => entry.path === 'schedule.weight');
        assert.equal(saved.value, original); assert.equal(saved.baselineValue, Number(submitted));
        await page.reload(); await page.locator('#project-status').filter({hasText: '已暂停'}).waitFor();
        await page.locator('#project-settings').click();
        if (global) await page.locator('[data-settings-scope="global"]').click();
        await page.locator('#advanced-settings > summary').click();
        assert.equal(await weight.inputValue(), original, 'the rebased safe draft survives reload');
        await page.locator('#save-note').filter({hasText: '未保存'}).waitFor();
      }
      await page.locator('#settings-save').click(); await page.locator('#settings-save:not([disabled])').waitFor();
      assert.equal(posts.length, 2);
      assert.equal(posts[1].project, global ? null : project);
      assert.equal(posts[1].values.schedule?.weight, scenario === 'failure-away' ? undefined : Number(original));
      assert.equal(await page.evaluate(id => window.RSIDraftStore.get('settings', id), key), null);
      await leave(); assert.equal(await weight.inputValue(), '4', 'the other scope keeps its independent draft');
      console.log('Pending settings draft browser check passed: ' + scenario);
    } finally { release(); await page.close(); }
  }
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
    await page.locator('#checks-editor textarea').first().fill('RAW-CHECKS-MUST-NOT-PERSIST');
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

async function checkObsoleteActions(browser,url,errors) {
  const page=await browser.newPage();page.on('pageerror',error=>errors.push(error.message));
  let paid=0;page.on('request',request=>{if(/\/api\/(?:model-center\/probe|pause|request)$/.test(new URL(request.url()).pathname)&&request.method()==='POST')paid++;});
  try {
    await page.goto(url);
    await page.locator('#project-status').filter({hasText:'已暂停'}).waitFor();
    await page.locator('[data-task-filter="history"]').click();
    const retryRow=page.locator('.task-row').filter({has:page.getByRole('button',{name:'过时任务重试入口',exact:true})});
    await retryRow.filter({hasText:'已过时'}).waitFor();
    const retryResponse=page.waitForResponse(response=>new URL(response.url()).pathname==='/api/retry'&&response.request().method()==='POST');
    await retryRow.getByRole('button',{name:'重试',exact:true}).click();
    const retried=await retryResponse;assert.equal(retried.status(),200);
    const task=JSON.parse(retried.request().postData()).task;
    const project=await page.locator('#project-select').inputValue();
    assert.equal((await (await page.request.get(new URL('/api/task?id='+task+'&project='+project,url).href)).json()).state,'queued');
    await page.getByRole('button',{name:'过时任务补充入口',exact:true}).click();
    await page.locator('.task-controls textarea').fill('补充过时任务的新验收要求');
    const revised=page.waitForResponse(response=>new URL(response.url()).pathname==='/api/task/revise'&&response.request().method()==='POST');
    await page.getByRole('button',{name:'保存补充并重新评估',exact:true}).click();
    assert.equal((await revised).status(),200);
    await page.locator('#detail-body').filter({hasText:'补充过时任务的新验收要求'}).waitFor();
    assert.equal(await page.locator('#detail-body .badge').first().innerText(),'排队中');
    assert.equal(paid,0,'obsolete actions must not enable projects or start model tests');
    assert.deepEqual(errors,[]);
    console.log('Obsolete history retry and detail revision passed against real HTTP; project remains paused.');
  } finally {await page.close();}
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
    if(process.env.RSI_BROWSER_SETTINGS_ONLY){await checkPendingSettingsDraft(browser,url,errors);assert.deepEqual(errors,[]);return;}
    if(process.env.RSI_BROWSER_OBSOLETE_ONLY){await checkObsoleteActions(browser,url,errors);return;}
    if(process.env.RSI_BROWSER_PROVIDERS_ONLY){await checkProviderSaveRace(browser,url,errors);await checkProviderSaveRace(browser,url,errors,true);assert.deepEqual(errors,[]);console.log('Provider creation acknowledgement checks passed.');return;}
    if(process.env.RSI_BROWSER_ROUTES_ONLY){await checkRouteSaveRace(browser,url,errors);return;}
    if(process.env.RSI_BROWSER_UPDATES_ONLY){await checkUpdatePending(browser,url,errors);await checkUpdateRateLimit(browser,url,errors);assert.deepEqual(errors,[]);console.log('Update pending and primary rate-limit cooldown browser checks passed.');return;}
    await checkReadiness(browser, url, false, errors);
    await checkReadiness(browser, url, true, errors);
    await checkReadinessInstallAction(browser,url,errors);
    await checkRunProgress(browser,url,errors);
    await checkModelForms(browser, url, errors);
    await checkProviderSaveRace(browser, url, errors);
    await checkProviderSaveRace(browser, url, errors, true);
    await checkRouteSaveRace(browser,url,errors);
    await checkUpdatePending(browser,url,errors);
    await checkUpdateRateLimit(browser,url,errors);
    await checkPendingSettingsDraft(browser,url,errors);
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
