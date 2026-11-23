const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const {fixtureReady, closeFixture, waitForPausedProject} = require('./fixture_browser.cjs');
const {chromium} = require('playwright');

(async () => {
  const server = spawn(process.env.PYTHON || 'python3', ['-m', 'tests.browser_fixture'],
    {stdio: ['pipe', 'pipe', 'pipe'], env: {...process.env, PYTHONDONTWRITEBYTECODE: '1', RSI_BROWSER_BLOCKERS:'1'}});
  let browser, scenarioError;
  try {
    const url = await fixtureReady(server);
    browser = await chromium.launch(process.env.RSI_BROWSER_CHANNEL ? {channel: process.env.RSI_BROWSER_CHANNEL} : {});
    const page = await browser.newPage(), errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(url);
    await waitForPausedProject(page);
    await page.evaluate(async()=>{const data=await api('state?sections=core&limit=200');await showTask(data.tasks.find(task=>task.title==='等待原因链任务'));});
    const section=page.locator('#detail-body details').filter({has:page.locator('summary').filter({hasText:'为什么还没开始'})});
    await section.locator('summary').click();
    await section.getByText('项目已暂停',{exact:true}).waitFor();
    await section.getByText('等待原因链任务 → 原因链前置甲',{exact:true}).waitFor();
    await section.getByText('原因链前置甲 → 原因链前置乙',{exact:true}).waitFor();
    assert.match(await section.innerText(),/等待连接重试时间/);
    await page.evaluate(async()=>api('pause',{project:projectId,paused:false}));
    await section.getByRole('button',{name:'刷新阻塞原因',exact:true}).click();
    await section.getByText('项目已暂停',{exact:true}).waitFor({state:'detached'});
    assert.match(await section.innerText(),/等待连接重试时间/);
    assert.match(await section.innerText(),/等待前置任务合并/);
    await page.locator('#language-select').selectOption('en');
    const english=page.locator('#detail-body details').filter({has:page.locator('summary').filter({hasText:'Why has this not started?'})});
    if(!await english.evaluate(node=>node.open))await english.locator('summary').click();
    await english.getByText('等待原因链任务 → 原因链前置甲',{exact:true}).waitFor();
    assert.match(await english.innerText(),/Waiting for connection retry/);
    assert.deepEqual(errors, []);
    console.log('Simultaneous blocking causes, dependency chain, refresh and bilingual controls passed.');
  } catch (error) {
    scenarioError = error;
    throw error;
  } finally {
    await closeFixture(server, browser, scenarioError);
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
