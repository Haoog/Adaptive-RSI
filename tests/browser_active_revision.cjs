const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const {createInterface} = require('node:readline');
const {chromium} = require('playwright');

(async () => {
  const server = spawn(process.env.PYTHON || 'python3', ['-m', 'tests.browser_fixture'], {
    stdio: ['pipe', 'pipe', 'pipe'],
    env: {...process.env, PYTHONDONTWRITEBYTECODE: '1', RSI_BROWSER_ACTIVE_REVISION: '1'},
  });
  server.stderr.on('data', chunk => process.stderr.write(chunk));
  let browser;
  try {
    const url = await new Promise((resolve, reject) => {
      const timeout = setTimeout(() => reject(Error('Console startup timed out')), 30000);
      createInterface({input: server.stdout}).once('line', line => {clearTimeout(timeout); resolve(line);});
      server.once('exit', code => {clearTimeout(timeout); reject(Error('Console exited: ' + code));});
    });
    browser = await chromium.launch(process.env.RSI_BROWSER_CHANNEL ? {channel: process.env.RSI_BROWSER_CHANNEL} : {});
    const page = await browser.newPage({viewport: {width: 390, height: 844}, isMobile: true});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(url);
    await page.locator('[data-task-filter="active"]').click();
    const row = page.locator('.task-row').filter({hasText: '运行中补充需求任务'});
    await row.waitFor({timeout: 15000});
    await row.getByRole('button', {name: '查看详情', exact: true}).click();
    const editor = page.locator('#detail-body .task-supplement-editor');
    await editor.locator('summary').click();
    await page.getByLabel('补充需求（保留在当前任务）', {exact: true}).fill('补充负数边界');
    const saved = page.waitForResponse(response => new URL(response.url()).pathname === '/api/task/revise' && response.request().method() === 'POST');
    await editor.getByRole('button', {name: '保存补充并重新评估', exact: true}).click();
    const result = await saved;
    assert.equal(result.status(), 200);
    assert.deepEqual(await result.json(), {saved: true, pending: true});
    await page.locator('#detail-body').getByText('补充已收到，将在当前阶段结束后带入下一轮。', {exact: true}).waitFor();
    await page.reload();
    await page.locator('#detail-body').getByText('补充已收到，将在当前阶段结束后带入下一轮。', {exact: true}).waitFor();
    const task = await page.evaluate(async () => {
      const id = decodeURIComponent(new URL(location.href).hash.match(/task=([^&]+)/)[1]);
      const project = document.getElementById('project-select').value;
      return await (await fetch('/api/task?id=' + encodeURIComponent(id) + '&project=' + encodeURIComponent(project))).json();
    });
    assert.equal(task.state, 'executing');
    assert.equal(task.body, '最初要求\n\n补充负数边界');
    assert.deepEqual(errors, []);
    await page.close();
    console.log('Active revision browser passed: saved and acknowledged without a model call.');
  } finally {
    if (browser) await browser.close();
    server.kill();
  }
})();
