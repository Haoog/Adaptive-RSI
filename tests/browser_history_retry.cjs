const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const {createInterface} = require('node:readline');
const {chromium} = require('playwright');

(async () => {
  const server = spawn(process.env.PYTHON || 'python3', ['-m', 'tests.browser_fixture'], {
    stdio:['pipe','pipe','pipe'],
    env:{...process.env, PYTHONDONTWRITEBYTECODE:'1', RSI_BROWSER_REVISION_FLOW:'1', RSI_BROWSER_TASK_DETAIL:'1'}
  });
  server.stderr.on('data', chunk => process.stderr.write(chunk));
  let browser;
  try {
    const url = await new Promise((resolve, reject) => {
      const timeout=setTimeout(() => reject(Error('Fixture startup timed out')), 30000);
      createInterface({input:server.stdout}).once('line', line => {clearTimeout(timeout); resolve(line);});
      server.once('exit', code => {clearTimeout(timeout); reject(Error('Fixture exited: '+code));});
    });
    browser = await chromium.launch();
    const page = await browser.newPage(), errors=[];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(url);
    await page.waitForLoadState('networkidle');
    await page.locator('#project-status').filter({hasText:'已暂停'}).waitFor({timeout:15000});
    await page.locator('[data-task-filter="active"]').click();
    let mode='fail', failures=0, held=0, releaseRetry;
    const waitRetry = new Promise(resolve => {releaseRetry=resolve;});
    await page.route('**/api/task-runs?*', async route => {
      if (mode==='fail') {mode='pass';failures++;return route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({error:'temporary outage'})});}
      if (mode==='hold') {mode='pass';held++;await waitRetry;}
      await route.continue();
    });
    const row=page.locator('.task-row').filter({hasText:'普通排队任务'}).first();
    await row.getByRole('button',{name:'查看详情',exact:true}).click();
    await page.locator('#detail-dialog[open]').waitFor();
    const failed=page.locator('#detail-body .detail-section').filter({hasText:'运行记录加载失败。'});
    await failed.waitFor();
    await page.locator('#detail-body .task-controls').waitFor();
    await page.locator('.task-supplement-editor > summary').click();
    const reason=page.getByLabel('干预原因（可选，保留原文）',{exact:true});
    await reason.fill('保留重试前的干预原因');
    await page.locator('.task-queue-editor > summary').click();
    const urgent=page.locator('.task-queue-editor input[type=checkbox]');
    await urgent.check();
    const retry=failed.getByRole('button',{name:'重试',exact:true});
    mode='hold';await retry.click();
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
    mode='fail';
    await page.evaluate(() => showTask(detailTask, window.RSIExperience.detailsSnapshot(document.getElementById('detail-body'))));
    const nextFailure=page.locator('#detail-body .detail-section').filter({hasText:'运行记录加载失败。'});
    await nextFailure.waitFor();
    await page.locator('#detail-body .task-controls').waitFor();
    if (!await page.locator('.task-supplement-editor').evaluate(node => node.open)) await page.locator('.task-supplement-editor > summary').click();
    if (!await page.locator('.task-queue-editor').evaluate(node => node.open)) await page.locator('.task-queue-editor > summary').click();
    await page.getByLabel('干预原因（可选，保留原文）',{exact:true}).fill('保持展开并保留原因');
    await nextFailure.getByRole('button',{name:'重试',exact:true}).click();
    await nextFailure.waitFor({state:'detached'});
    assert.equal(await page.locator('.task-supplement-editor').evaluate(node => node.open),true);
    assert.equal(await page.locator('.task-queue-editor').evaluate(node => node.open),true);
    assert.equal(await page.getByLabel('干预原因（可选，保留原文）',{exact:true}).inputValue(),'保持展开并保留原因');
    assert.equal(failures,2);
    // Exercise the continuation with a nonempty first page. The latest run is
    // held while the user edits, so the assertions run after the footer appears.
    await page.evaluate(() => document.getElementById('detail-dialog').close());
    await page.locator('[data-task-filter="attention"]').click();
    const historyRow=page.locator('.task-row').filter({hasText:'详情历史任务'}).first();
    await historyRow.getByRole('button',{name:'查看详情',exact:true}).click();
    await page.locator('#live-task-outcome').waitFor();
    await page.locator('#detail-body .task-controls').waitFor();
    mode='fail';
    await page.evaluate(() => showTask(detailTask, window.RSIExperience.detailsSnapshot(document.getElementById('detail-body'))));
    const historyFailure=page.locator('#detail-body .detail-section').filter({hasText:'运行记录加载失败。'});
    await historyFailure.waitFor();
    await page.locator('#detail-body .task-controls').waitFor();
    if (!await page.locator('.task-supplement-editor').evaluate(node => node.open)) await page.locator('.task-supplement-editor > summary').click();
    let releaseRun, runStarted;
    const waitRun=new Promise(resolve => {releaseRun=resolve;});
    const heldRun=new Promise(resolve => {runStarted=resolve;});
    await page.route('**/api/run?id=detail-h3', async route => {runStarted();await waitRun;await route.continue();});
    await historyFailure.getByRole('button',{name:'重试',exact:true}).click();
    await heldRun;
    await page.getByLabel('干预原因（可选，保留原文）',{exact:true}).fill('运行详情加载期间的输入');
    releaseRun();
    await page.locator('#detail-body').getByText('已显示最近 3 次运行',{exact:true}).waitFor();
    assert.equal(await page.locator('.task-supplement-editor').evaluate(node => node.open),true);
    assert.equal(await page.getByLabel('干预原因（可选，保留原文）',{exact:true}).inputValue(),'运行详情加载期间的输入');
    assert.equal(await page.locator('#detail-body > details.detail-section[data-run]').count(),3);
    assert.equal(failures,3);
    assert.deepEqual(errors,[]);
    console.log('Browser history retry preserved inputs and disclosure state.');
  } finally {
    if(browser) await browser.close();
    server.kill();
  }
})().catch(error => {console.error(error); process.exitCode=1;});
