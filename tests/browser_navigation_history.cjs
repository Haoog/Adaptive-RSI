const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const {createInterface} = require('node:readline');
const {chromium} = require('playwright');

(async () => {
  const server = spawn(process.env.PYTHON || 'python3', ['-m', 'tests.browser_fixture'],
    {stdio: ['pipe', 'pipe', 'pipe'], env: {...process.env, PYTHONDONTWRITEBYTECODE: '1'}});
  let browser;
  try {
    const url = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(Error('Fixture startup timed out')), 30000);
      createInterface({input: server.stdout}).once('line', line => {clearTimeout(timer); resolve(line);});
      server.once('exit', code => {clearTimeout(timer); reject(Error('Fixture exited: ' + code));});
    });
    browser = await chromium.launch(process.env.RSI_BROWSER_CHANNEL ? {channel: process.env.RSI_BROWSER_CHANNEL} : {});
    const page = await browser.newPage(), errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(url);
    await page.locator('#project-status').filter({hasText: '已暂停'}).waitFor();
    const firstStep=await browser.newPage();await firstStep.goto(url);
    await firstStep.locator('#project-status').filter({hasText:'已暂停'}).waitFor();
    const home=await firstStep.locator('#project-select').inputValue(),away=await firstStep.locator('#project-select option').evaluateAll((options,home)=>options.find(option=>option.value!==home).value,home);
    await firstStep.locator('#project-select').selectOption(away);await firstStep.waitForFunction(project=>projectId===project,away);
    await firstStep.reload();await firstStep.waitForFunction(project=>projectId===project,away);
    await firstStep.goBack();await firstStep.waitForFunction(project=>projectId===project&&document.getElementById('project-select').value===project,home,{timeout:60000});
    await firstStep.close();
    const first=await page.locator('#project-select').inputValue();
    const second=await page.locator('#project-select option').evaluateAll((options,first)=>options.find(option=>option.value!==first).value,first);
    let writes=0;page.on('request',request=>{if(request.method()==='POST')writes++;});
    await page.locator('#goal-text').fill('Navigation must retain my unsent draft');
    const visible=async(name,project)=>{
      await page.waitForFunction(({name,project})=>view===name&&projectId===project&&!document.getElementById('view-'+name).hidden,{name,project});
      assert.equal(await page.locator('#project-select').inputValue(),project);
    };
    await page.locator('.sidebar nav [data-view="usage"]').click();
    await visible('usage',first);
    await page.locator('#project-select').selectOption(second);
    await visible('usage',second);
    await page.locator('.sidebar [data-view="evolution"]').click();
    await visible('evolution',second);
    await page.goBack();await visible('usage',second);
    await page.goBack();await visible('usage',first);
    await page.goBack();await visible('overview',first);
    assert.equal(await page.locator('#goal-text').inputValue(),'Navigation must retain my unsent draft');
    await page.goForward();await visible('usage',first);
    await page.goForward();await visible('usage',second);
    await page.goForward();await visible('evolution',second);
    await page.reload();await visible('evolution',second);
    const task=await page.evaluate(async project=>{
      const data=await api('state?sections=core&project='+project+'&limit=200');return data.tasks.find(task=>task.title==='Fix addition');
    },first);
    await page.evaluate(({task,project})=>{window.RSIDesktop={invoke:async()=>({})};window.dispatchEvent(new CustomEvent('rsi-notification-open',{detail:{task,project}}));},{task:task.id,project:first});
    await page.locator('#detail-dialog[open]').waitFor();await visible('overview',first);
    await page.waitForFunction(task=>new URLSearchParams(location.hash.slice(1)).get('task')===task,task.id);
    await page.goBack();await visible('evolution',second);
    await page.locator('#detail-dialog[open]').waitFor({state:'detached'});
    await page.goForward();await visible('overview',first);
    await page.locator('#detail-dialog[open]').waitFor();
    assert.equal(await page.locator('#detail-title').textContent(),'Fix addition');
    const replacement=await page.evaluate(async first=>{const data=await api('state?sections=core&project='+first+'&limit=200');return data.tasks.find(task=>task.title.startsWith('历史任务'));},first);
    await page.route('**/api/state?*',async route=>{await new Promise(resolve=>setTimeout(resolve,200));await route.continue();});
    await page.evaluate(({task,project})=>window.dispatchEvent(new CustomEvent('rsi-notification-open',{detail:{task,project}})),{task:replacement.id,project:first});
    await page.locator('#detail-title').filter({hasText:replacement.title}).waitFor();
    await page.locator('#detail-dialog[open]').waitFor();
    await page.evaluate(({task,project})=>{
      document.getElementById('detail-dialog').dispatchEvent(new MouseEvent('click',{bubbles:true}));
      window.dispatchEvent(new CustomEvent('rsi-notification-open',{detail:{task,project}}));
    },{task:task.id,project:first});
    await page.locator('#detail-title').filter({hasText:'Fix addition'}).waitFor();
    await page.locator('#detail-dialog[open]').waitFor();
    await page.unroute('**/api/state?*');

    await page.locator('#detail-dialog .close-dialog').click();
    await page.waitForFunction(()=>!new URLSearchParams(location.hash.slice(1)).has('task'));
    const route=await page.evaluate(()=>Object.fromEntries(new URLSearchParams(location.hash.slice(1))));
    assert.equal(route.project,first);assert.equal(route.view,'overview');
    await page.reload();await visible('overview',first);
    assert.equal(await page.locator('#detail-dialog[open]').count(),0);
    assert.equal(writes,0,'navigation and notification inspection must not mutate tasks');
    assert.deepEqual(errors, []);
    console.log('Cross-project back/forward, initial-route restoration, notification details and draft retention passed.');
  } finally {
    if (browser) await browser.close();
    server.stdin.end();
    await new Promise(resolve => {server.once('exit', resolve); setTimeout(()=>{server.kill('SIGKILL');resolve();},5000).unref();});
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
