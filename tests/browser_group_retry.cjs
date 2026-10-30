const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const {createInterface} = require('node:readline');
const {chromium} = require('playwright');

(async () => {
  const server = spawn(process.env.PYTHON || 'python3', ['-m', 'tests.browser_fixture'],
    {stdio: ['pipe', 'pipe', 'pipe'], env: {...process.env, PYTHONDONTWRITEBYTECODE: '1', RSI_BROWSER_FAILED_GROUP:'1'}});
  let browser;
  try {
    const url = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(Error('Fixture startup timed out')), 30000);
      createInterface({input: server.stdout}).once('line', line => {clearTimeout(timer); resolve(line);});
      server.once('exit', code => {clearTimeout(timer); reject(Error('Fixture exited: ' + code));});
    });
    browser = await chromium.launch(process.env.RSI_BROWSER_CHANNEL ? {channel: process.env.RSI_BROWSER_CHANNEL} : {});
    const page = await browser.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(url);
    await page.locator('#project-status').filter({hasText: '已暂停'}).waitFor();
    await page.locator('[data-task-filter="attention"]').click();
    const evidence = page.getByRole('button', {name:'逐条验收证据', exact:true}).first();
    await evidence.waitFor();
    for (const width of [1440, 390]) {
      await page.setViewportSize({width, height:1000});
      const bounds = await evidence.evaluate(button => {
        const text = document.createRange();text.selectNodeContents(button);
        return {lines:text.getClientRects().length, height:button.getBoundingClientRect().height,
          fits:document.documentElement.scrollWidth <= innerWidth};
      });
      assert.equal(bounds.lines, 1, 'batch evidence action stays on one readable line');
      assert(bounds.height <= 44, 'batch evidence action stays compact');
      assert(bounds.fits, 'batch task fits desktop and mobile viewports');
    }
    await page.setViewportSize({width:1440, height:1000});
    await page.getByRole('checkbox', {name:'选择任务 · 拆分来源一'}).check();
    let previews = 0, writes = 0, receipts = 0;
    await page.route('**/api/tasks/batch*', async route => {
      if (route.request().method() === 'POST') {
        const body = route.request().postDataJSON();
        if (body.preview) {previews++;await new Promise(resolve => setTimeout(resolve, 100));return route.continue();}
        writes++;
        await route.fetch();
        return route.abort('failed');
      }
      receipts++;return route.continue();
    });
    const split = page.getByRole('button', {name:'拆分失败任务组并重做', exact:true});
    await split.evaluate(button => {button.click();button.click();});
    const dialog = page.locator('dialog.confirm-dialog[open]');
    await dialog.getByText('分组后修订的完整要求将另建任务重新验证，避免丢失或猜测归属。', {exact:false}).waitFor();
    await dialog.getByRole('button', {name:'确认', exact:true}).click();
    await split.waitFor();
    await page.waitForFunction(() => ![...document.querySelectorAll('button')].find(button => button.textContent === '拆分失败任务组并重做').disabled);
    await split.click();
    await page.locator('#toast').filter({hasText:'批量操作已完成'}).waitFor();
    assert.equal(previews, 1, 'double-click and response loss do not start another preview');
    assert.equal(writes, 1, 'lost response is reconciled before any repeat write');
    assert.equal(receipts, 1, 'retry queries the persisted receipt');
    await page.locator('[data-task-filter="active"]').click();
    const state = await page.evaluate(async () => (await fetch('/api/state?filter=all&sections=core')).json());
    const supplement = state.tasks.find(task => task.fingerprint?.startsWith('group-revision:'));
    assert.ok(supplement, 'shared revisions are preserved as a fresh task');
    assert.ok(supplement.body.includes('分组后新增的共同要求'));
    assert.equal(supplement.state, 'queued');
    assert.equal(state.tasks.filter(task => task.state === 'queued' && ['拆分来源一','拆分来源二'].includes(task.title)).length, 3);
    const source = state.tasks.find(task => task.title === '拆分来源二' && task.state === 'queued');
    await page.getByRole('button', {name:'拆分来源二', exact:true}).click();
    await page.getByText('任务组关联与成果', {exact:true}).click();
    await page.getByText('整组失败证据', {exact:true}).waitFor();
    const detail = await page.evaluate(async id => (await fetch('/api/task?id='+id+'&project='+projectId)).json(), source.id);
    assert.equal(detail.group_lineage.groups[0].evidence_scope, 'whole_group');
    await page.locator('#detail-dialog .close-dialog').click();
    await page.waitForFunction(() => !new URLSearchParams(location.hash.slice(1)).has('task'));
    await page.route('**/batch.js', async route => {
      await new Promise(resolve => setTimeout(resolve, 1000));
      await route.continue();
    });
    await page.reload();
    await page.locator('#project-status').waitFor();
    assert.equal(await page.locator('#detail-dialog[open]').count(), 0, 'closed detail must stay closed after reload');
    await page.getByRole('button', {name:'批次记录', exact:true}).click();
    const history = page.locator('dialog[open]').filter({hasText:'导出批次记录'});
    const reports = await page.evaluate(async () => (await fetch('/api/tasks/batches?project='+projectId)).json());
    const restored = reports.batches.find(report => report.action === 'split_retry');
    assert.ok(restored?.complete);
    await history.getByRole('searchbox', {name:'搜索批次记录'}).fill(restored.request_id);
    await history.getByText(restored.tasks[0].title, {exact:true}).waitFor();
    const downloadReady = page.waitForEvent('download');
    await history.getByRole('button', {name:'导出批次记录', exact:true}).click();
    const stream = await (await downloadReady).createReadStream();
    const chunks = [];for await (const chunk of stream) chunks.push(chunk);
    assert.deepEqual(JSON.parse(Buffer.concat(chunks).toString()), reports);
    assert.deepEqual(errors, []);
    console.log('Failed group split and revised requirement preservation browser checks passed without model calls.');
  } finally {
    if (browser) await browser.close();
    server.stdin.end('\n');
    await new Promise(resolve => server.once('exit', resolve));
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
