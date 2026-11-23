const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const {fixtureReady, closeFixture, waitForPausedProject} = require('./fixture_browser.cjs');
const {chromium} = require('playwright');

(async () => {
  const server = spawn(process.env.PYTHON || 'python3', ['-m', 'tests.browser_fixture'], {
    stdio:['pipe','pipe','pipe'],
    env:{...process.env, PYTHONDONTWRITEBYTECODE:'1', RSI_BROWSER_REVISION_FLOW:'1', RSI_BROWSER_TASK_DETAIL:'1'}
  });
  server.stderr.on('data', chunk => process.stderr.write(chunk));
  let browser, scenarioError;
  try {
    const url = await fixtureReady(server);
    browser = await chromium.launch(process.env.RSI_BROWSER_CHANNEL ? {channel: process.env.RSI_BROWSER_CHANNEL} : {});
    // Route-controlled outages must not depend on service-worker activation timing.
    const page = await browser.newPage({serviceWorkers:'block'}), errors=[];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(url);
    await page.waitForLoadState('networkidle');
    await waitForPausedProject(page);
    await page.locator('[data-task-filter="active"]').click();
    let mode='fail', failures=0, held=0, heldTask=null, releaseRetry;
    const waitRetry = new Promise(resolve => {releaseRetry=resolve;});
    await page.route(url => url.pathname === '/api/task-runs', async route => {
      if (mode==='fail') {failures++;return route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({error:'temporary outage'})});}
      if (mode==='hold') {
        const task=new URL(route.request().url()).searchParams.get('task');
        const retryPending=task===heldTask&&await page.evaluate(() => [...document.querySelectorAll('#detail-body .detail-section button')].some(button => button.textContent==='重试' && button.disabled));
        if(!retryPending)return route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({error:'temporary outage'})});
        mode='pass';held++;await waitRetry;
      }
      await route.continue();
    });
    // A background history request can race the selected task's first request.
    // Both must see the simulated outage until the user chooses to retry.
    assert.equal(await page.evaluate(() => fetch('/api/task-runs?task=background-probe').then(response => response.status)), 503);
    const firstOutageBaseline=failures;
    const row=page.locator('.task-row').filter({hasText:'普通排队任务'}).first();
    await row.getByRole('button',{name:'查看详情',exact:true}).click();
    await page.locator('#detail-dialog[open]').waitFor();
    const failed=page.locator('#detail-body .detail-section').filter({hasText:'运行记录加载失败。'});
    await failed.waitFor();
    assert.ok(failures>firstOutageBaseline, 'the selected task also encountered the outage');
    await page.locator('#detail-body .task-controls').waitFor();
    await page.locator('.task-supplement-editor > summary').click();
    const reason=page.getByLabel('干预原因（可选，保留原文）',{exact:true});
    await reason.fill('保留重试前的干预原因');
    await page.locator('.task-queue-editor > summary').click();
    const urgent=page.locator('.task-queue-editor input[type=checkbox]');
    await urgent.check();
    const retry=failed.getByRole('button',{name:'重试',exact:true});
    heldTask=await page.evaluate(() => detailTask.id);mode='hold';
    assert.equal(await page.evaluate(() => fetch('/api/task-runs?task=background-probe').then(response => response.status)),503);
    assert.equal(held,0,'an unrelated request cannot consume the held retry');
    await retry.click();
    await page.waitForFunction(() => [...document.querySelectorAll('#detail-body .detail-section button')].some(x => x.textContent === '重试' && x.disabled), null, {timeout:5000});
    await reason.fill('重试期间继续输入的干预原因');
    releaseRetry();
    await failed.waitFor({state:'detached'});
    assert.equal(await reason.inputValue(), '重试期间继续输入的干预原因');
    assert.equal(await urgent.isChecked(), true);
    assert.equal(held,1);
    // A live rebuild can carry a snapshot from before this history error. New
    // disclosure edits made while recovery is visible must not be reverted by it.
    await page.locator('.task-supplement-editor').evaluate(node => {node.open=false;});
    await page.locator('.task-queue-editor').evaluate(node => {node.open=false;});
    const nextOutageBaseline=failures;mode='fail';
    await page.evaluate(() => showTask(detailTask, window.RSIExperience.detailsSnapshot(document.getElementById('detail-body'))));
    const nextFailure=page.locator('#detail-body .detail-section').filter({hasText:'运行记录加载失败。'});
    await nextFailure.waitFor();
    assert.ok(failures>nextOutageBaseline, 'the second outage was observed');
    await page.locator('#detail-body .task-controls').waitFor();
    if (!await page.locator('.task-supplement-editor').evaluate(node => node.open)) await page.locator('.task-supplement-editor > summary').click();
    if (!await page.locator('.task-queue-editor').evaluate(node => node.open)) await page.locator('.task-queue-editor > summary').click();
    await page.getByLabel('干预原因（可选，保留原文）',{exact:true}).fill('保持展开并保留原因');
    mode='pass';await nextFailure.getByRole('button',{name:'重试',exact:true}).click();
    await nextFailure.waitFor({state:'detached'});
    assert.equal(await page.locator('.task-supplement-editor').evaluate(node => node.open),true);
    assert.equal(await page.locator('.task-queue-editor').evaluate(node => node.open),true);
    assert.equal(await page.getByLabel('干预原因（可选，保留原文）',{exact:true}).inputValue(),'保持展开并保留原因');
    assert.ok(failures>=2, 'both history outages were observed');
    // Exercise the continuation with a nonempty first page. The latest run is
    // held while the user edits, so the assertions run after the footer appears.
    await page.evaluate(() => document.getElementById('detail-dialog').close());
    await page.locator('[data-task-filter="attention"]').click();
    const historyRow=page.locator('.task-row').filter({hasText:'详情历史任务'}).first();
    await historyRow.getByRole('button',{name:'查看详情',exact:true}).click();
    await page.locator('#live-task-outcome').waitFor();
    await page.locator('#detail-body .task-controls').waitFor();
    const historyOutageBaseline=failures;mode='fail';
    await page.evaluate(() => showTask(detailTask, window.RSIExperience.detailsSnapshot(document.getElementById('detail-body'))));
    const historyFailure=page.locator('#detail-body .detail-section').filter({hasText:'运行记录加载失败。'});
    await historyFailure.waitFor();
    assert.ok(failures>historyOutageBaseline, 'the history outage was observed');
    await page.locator('#detail-body .task-controls').waitFor();
    if (!await page.locator('.task-supplement-editor').evaluate(node => node.open)) await page.locator('.task-supplement-editor > summary').click();
    let releaseRun, runStarted;
    const waitRun=new Promise(resolve => {releaseRun=resolve;});
    const heldRun=new Promise(resolve => {runStarted=resolve;});
    await page.route('**/api/run?id=detail-h3', async route => {runStarted();await waitRun;await route.continue();});
    mode='pass';await historyFailure.getByRole('button',{name:'重试',exact:true}).click();
    await heldRun;
    await page.getByLabel('干预原因（可选，保留原文）',{exact:true}).fill('运行详情加载期间的输入');
    releaseRun();
    await page.locator('#detail-body').getByText('已显示最近 3 次运行',{exact:true}).waitFor();
    assert.equal(await page.locator('.task-supplement-editor').evaluate(node => node.open),true);
    assert.equal(await page.getByLabel('干预原因（可选，保留原文）',{exact:true}).inputValue(),'运行详情加载期间的输入');
    assert.equal(await page.locator('#detail-body > details.detail-section[data-run]').count(),3);
    assert.ok(failures>=3, 'all history outages were observed');
    assert.deepEqual(errors,[]);
    console.log('Browser history retry preserved inputs and disclosure state.');
  } catch (error) {
    scenarioError = error;
    throw error;
  } finally {
    await closeFixture(server, browser, scenarioError);
  }
})().catch(error => {console.error(error); process.exitCode=1;});
