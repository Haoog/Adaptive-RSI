const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const {createInterface} = require('node:readline');
const {chromium} = require('playwright');

(async () => {
  const server = spawn(process.env.PYTHON || 'python3', ['-B', '-m', 'tests.browser_fixture'], {
    stdio:['pipe','pipe','pipe'], env:{...process.env,PYTHONDONTWRITEBYTECODE:'1'}
  });
  server.stderr.on('data', chunk => process.stderr.write(chunk));
  let browser;
  try {
    const url = await new Promise((resolve,reject) => {
      const timer = setTimeout(() => reject(Error('Fixture startup timed out')),30000);
      createInterface({input:server.stdout}).once('line',line => {clearTimeout(timer);resolve(line);});
      server.once('exit',code => {clearTimeout(timer);reject(Error('Fixture exited: '+code));});
    });
    browser = await chromium.launch(process.env.RSI_BROWSER_CHANNEL ? {channel:process.env.RSI_BROWSER_CHANNEL} : {});
    const page = await browser.newPage({viewport:{width:1440,height:1000}});
    const errors = [];page.on('pageerror',error => errors.push(error.message));
    await page.goto(url);
    await page.locator('#project-status').filter({hasText:'已暂停'}).waitFor();
    await page.locator('.sidebar [data-view=models]').click();
    assert.equal(await page.locator('#page-name').textContent(),'模型服务');
    assert.equal(await page.locator('.sidebar [data-view=models]').getAttribute('aria-current'),'page');
    assert.equal(await page.locator('#settings-form').isVisible(),false,'model page has no unrelated runtime settings');
    // 面板在模型快照返回后才显示；等首屏渲染完成再断言，避免把加载中的空帧当成导航失败。
    await page.locator('#mc-providers-panel').waitFor();
    assert.equal(await page.locator('#mc-providers-panel').isVisible(),true);
    assert.equal(await page.evaluate(() => scrollY),0,'model page opens at its heading');
    await page.reload();
    await page.locator('#view-models').waitFor();
    assert.equal(await page.locator('#page-name').textContent(),'模型服务','reload preserves the selected page');
    await page.locator('[data-settings-scope=global]').click();
    await page.locator('[data-mc-tab=roles]').click();
    await page.reload();
    await page.locator('#view-models[data-scope=global]').waitFor({timeout:10000});
    assert.equal(await page.locator('[data-settings-scope=global]').getAttribute('aria-pressed'),'true');
    assert.equal(await page.locator('#mc-roles-panel').isVisible(),true,'reload preserves model scope and subpage');
    await page.locator('#project-settings').click();
    assert.equal(await page.locator('#settings-form').isVisible(),true);
    assert.equal(await page.locator('#view-models').isVisible(),false,'runtime settings are a separate page');
    await page.locator('.sidebar [data-view=overview]').click();
    await page.setViewportSize({width:390,height:844});
    const mobile = await page.evaluate(() => ({
      navigation:document.querySelector('.sidebar').getBoundingClientRect().height,
      firstTask:document.querySelector('.task-row').getBoundingClientRect().top,
      overflow:document.documentElement.scrollWidth>innerWidth
    }));
    assert(mobile.navigation < 205,'mobile navigation leaves room for the workspace');
    assert(mobile.firstTask < 844,'a task is reachable in the first mobile screen');
    assert.equal(mobile.overflow,false);
    assert.equal(await page.locator('[data-task-filter=active]').textContent(),'未完成','queued work has an accurate group label');
    assert.match(await page.locator('#queue-count').getAttribute('aria-label'),/排队中/,'the sidebar count explains what it measures');
    for(const selector of ['#focus-goal','#pause-button','#project-settings','#project-select','#workspace-status-details > summary']){
      assert((await page.locator(selector).boundingBox()).height>=44,selector+' has a mobile touch target');
    }
    await page.locator('#sidebar-toggle').click();
    assert.equal(await page.locator('#sidebar-toggle').getAttribute('aria-expanded'),'true');
    await page.locator('#global-settings').click();
    assert.equal(await page.locator('#sidebar-toggle').getAttribute('aria-expanded'),'false','navigation closes the mobile utility menu');
    await page.locator('.sidebar [data-view=overview]').click();
    await page.locator('[data-task-filter=history]').click();
    await page.locator('#focus-goal').click();
    await page.locator('#goal-text').fill('第二轮交互回执验证');
    const created = page.waitForResponse(response => new URL(response.url()).pathname==='/api/goal'&&response.request().method()==='POST');
    await page.locator('#goal-form button[type=submit]').click();
    const task = await (await created).json();
    const receipt = page.locator('#goal-receipt');
    await receipt.filter({hasText:'第二轮交互回执验证'}).waitFor({timeout:10000});
    assert.equal(await page.locator('#task-filter').inputValue(),'active','new goals return to unfinished work');
    await receipt.getByRole('button',{name:'查看任务',exact:true}).click();
    await page.locator('#detail-dialog[open]').waitFor();
    assert.equal(await page.locator('#task-requirements').textContent(),'第二轮交互回执验证');
    assert.equal(await page.evaluate(()=>detailTask.id),task.id);
    await page.locator('#detail-body .task-wait-context').filter({hasText:'项目已暂停'}).waitFor({timeout:10000});
    const readingOrder=await page.locator('#detail-body').evaluate(body=>{
      const nodes=[...body.children];return {requirements:nodes.indexOf(body.querySelector('.task-requirements')),controls:nodes.indexOf(body.querySelector('.task-controls')),evidence:nodes.indexOf(body.querySelector('.detail-section'))};
    });
    assert(readingOrder.requirements<readingOrder.controls&&readingOrder.controls<readingOrder.evidence,'task context precedes the supporting evidence');
    await page.locator('#detail-dialog .close-dialog').click();
    let releaseDetail,detailArrived;
    const arrived=new Promise(resolve=>detailArrived=resolve),gate=new Promise(resolve=>releaseDetail=resolve);
    const holdDetail=async route=>{await gate;await route.continue();};
    await page.route('**/api/task?id=*',async route=>{detailArrived();await holdDetail(route);},{times:1});
    await receipt.getByRole('button',{name:'查看任务',exact:true}).click();
    await arrived;
    await page.locator('.sidebar [data-view=models]').click();
    const response=page.waitForResponse(reply=>new URL(reply.url()).pathname==='/api/task');
    releaseDetail();await response;
    await page.waitForTimeout(100);
    assert.equal(await page.locator('#detail-dialog').evaluate(dialog=>dialog.open),false,'a receipt response cannot reopen details after page navigation');
    await page.locator('.sidebar [data-view=overview]').click();
    await page.locator('#language-select').selectOption('en');
    assert.equal(await page.locator('#goal-form button[type=submit]').textContent(),'Add to current project','a completed submission keeps its label localizable');
    await page.locator('#language-select').selectOption('zh-CN');
    assert.equal(await page.locator('#goal-form button[type=submit]').textContent(),'添加到当前项目');
    assert.deepEqual(errors,[]);
    console.log('Page identity, model/settings separation, navigation persistence and mobile task access passed.');
  } finally {
    if(browser)await browser.close();
    server.stdin.end('\n');
    await new Promise(resolve=>server.once('exit',resolve));
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
