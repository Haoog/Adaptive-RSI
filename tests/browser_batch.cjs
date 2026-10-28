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
    const page = await browser.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(url);
    await page.locator('#project-status').filter({hasText: '已暂停'}).waitFor();
    for (const title of ['批量目标一', '批量目标二']) {
      await page.locator('#goal-text').fill(title);
      await page.locator('#goal-form button[type=submit]').click();
      await page.getByRole('button', {name:title, exact:true}).waitFor();
    }
    await page.getByRole('checkbox', {name:'选择任务 · 批量目标一'}).check();
    await page.getByRole('checkbox', {name:'选择任务 · 批量目标二'}).check();
    await page.getByRole('button', {name:'合并任务'}).click();
    const dialog = page.locator('dialog.confirm-dialog[open]');
    await dialog.getByRole('button', {name:'确认', exact:true}).click();
    await page.locator('#toast').filter({hasText:'批量操作已完成'}).waitFor();
    const state = await page.evaluate(async () => (await fetch('/api/state?filter=all&sections=core')).json());
    const grouped = state.tasks.find(task => task.fingerprint?.startsWith('batch:') && task.body?.includes('批量目标一') && task.body?.includes('批量目标二'));
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
  } finally {
    if (browser) await browser.close();
    server.stdin.end('\n');
    await new Promise(resolve => server.once('exit', resolve));
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
