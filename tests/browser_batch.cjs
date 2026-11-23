const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const {fixtureReady, closeFixture, waitForPausedProject} = require('./fixture_browser.cjs');
const {chromium} = require('playwright');

(async () => {
  const server = spawn(process.env.PYTHON || 'python3', ['-m', 'tests.browser_fixture'],
    {stdio: ['pipe', 'pipe', 'pipe'], env: {...process.env, PYTHONDONTWRITEBYTECODE: '1'}});
  let browser, scenarioError;
  try {
    const url = await fixtureReady(server);
    browser = await chromium.launch(process.env.RSI_BROWSER_CHANNEL ? {channel: process.env.RSI_BROWSER_CHANNEL} : {});
    const page = await browser.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(url);
    await waitForPausedProject(page);
    for (const title of ['批量目标一', '批量目标二']) {
      await page.locator('#goal-text').fill(title);
      await page.locator('#goal-form button[type=submit]').click();
      await page.getByRole('button', {name:title, exact:true}).waitFor();
    }
    const pageCount = await page.getByRole('checkbox', {name: /^选择任务 · /}).count();
    const toolbarCount = () => page.locator('#batch-actions span').first().textContent();
    // 空选择时全选按钮仍须可见，否则用户必须先逐行勾选才能批量选择整页。
    const selectAll = page.getByRole('button', {name:'全选当前页'});
    assert.equal(await selectAll.isVisible(), true, 'select all stays visible before any task is selected');
    await selectAll.click();
    assert.match(await toolbarCount(), new RegExp('· ' + pageCount + ' / ' + pageCount), 'select all covers the visible page');
    await page.getByRole('button', {name:'清除选择'}).click();
    assert.match(await toolbarCount(), /· 0 \//, 'clearing after select all resets the count');
    await page.getByRole('checkbox', {name:'选择任务 · 批量目标一'}).click();
    await page.getByRole('checkbox', {name:'选择任务 · 批量目标二'}).click({modifiers:['Shift']});
    assert.match(await toolbarCount(), new RegExp('· 2 / ' + pageCount), 'shift-click selects the whole visible range');
    assert.equal(await page.getByRole('checkbox', {name:'选择任务 · 批量目标一'}).isChecked(), true);
    assert.equal(await page.getByRole('checkbox', {name:'选择任务 · 批量目标二'}).isChecked(), true);
    await page.getByRole('button', {name:'清除选择'}).click();
    assert.match(await toolbarCount(), /· 0 \//, 'clearing resets the count');
    assert.equal(await page.getByRole('checkbox', {name:'选择任务 · 批量目标一'}).isChecked(), false);
    assert.equal(await page.locator('dialog.confirm-dialog[open]').count(), 0, 'clearing the selection sends no request');
    await page.getByRole('checkbox', {name:'选择任务 · 批量目标一'}).check();
    await page.locator('[data-task-filter="history"]').click();
    // 筛选切换会先异步刷新列表；等渲染完成后再核对计数。
    await page.waitForFunction(()=>/· 0 \/ 0/.test(document.querySelector('#batch-actions span')?.textContent||''));
    assert.match(await toolbarCount(), /· 0 \/ 0/, 'switching the filter drops selections that left the page');
    await page.locator('[data-task-filter="active"]').click();
    await page.getByRole('checkbox', {name:'选择任务 · 批量目标一'}).check();
    await page.getByRole('checkbox', {name:'选择任务 · 批量目标二'}).check();
    await page.getByRole('button', {name:'合并任务'}).click();
    const dialog = page.locator('dialog.confirm-dialog[open]');
    await dialog.getByRole('button', {name:'确认', exact:true}).click();
    await page.locator('#toast').filter({hasText:'批量操作已完成'}).waitFor();
    const state = await page.evaluate(async () => (await fetch('/api/state?filter=all&sections=core')).json());
    // The list payload omits body/evidence; read the grouped task through the detail endpoint.
    const grouped = await page.evaluate(async tasks => {
      for (const task of tasks.filter(task => task.fingerprint?.startsWith('batch:'))) {
        const detail = await (await fetch('/api/task?id=' + encodeURIComponent(task.id) + '&project=' + encodeURIComponent(projectId))).json();
        if (detail.body?.includes('批量目标一') && detail.body?.includes('批量目标二')) return detail;
      }
      return null;
    }, state.tasks);
    assert.ok(grouped, 'grouped task keeps both requirements regardless of ID order');
    await page.getByRole('checkbox', {name:'选择任务 · '+grouped.title}).check();
    await page.getByRole('button', {name:'拆分未开始的任务'}).click();
    await dialog.getByRole('button', {name:'确认', exact:true}).click();
    await page.getByRole('button', {name:'批量目标二', exact:true}).waitFor();
    const restored = await page.evaluate(async () => (await fetch('/api/state?filter=all&sections=core')).json());
    assert.ok(!restored.tasks.some(task => task.id === grouped.id && task.state === 'queued'));
    assert.equal(restored.tasks.filter(task => ['批量目标一', '批量目标二'].includes(task.title) && task.state === 'queued').length, 2);
    assert.deepEqual(errors, []);
    console.log('Batch grouping and restore browser checks passed with paused projects and no model calls.');
  } catch (error) {
    scenarioError = error;
    throw error;
  } finally {
    await closeFixture(server, browser, scenarioError);
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
