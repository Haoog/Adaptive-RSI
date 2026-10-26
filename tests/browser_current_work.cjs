const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const { createInterface } = require("node:readline");
const { chromium } = require("playwright");

// 当前任务标题回归：运行中任务不在任务列表当前页（深队列第 2 页）或被分组
// 筛选排除时，工作台「当前任务」必须显示任务标题而不是状态词；运行结束后
// 回退等待文案，项目切换不串显示其他项目的运行标题。
async function checkCurrentWork(browser, url, errors, server) {
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  page.on('pageerror', error => errors.push(error.message));
  const title = '修复登录页空指针';
  const pausedCopy = '暂停期间不会调用模型或修改代码';
  try {
    await page.goto(url);
    await page.locator('#project-status').filter({ hasText: '已暂停' }).waitFor({ timeout: 15000 });
    await page.locator('.task-row').first().waitFor({ timeout: 10000 });

    // 深队列：运行中任务在第 2 页，第一页 50 行里没有它。
    assert.equal(await page.locator('.task-row').count(), 50, 'page 1 is full of queued rows');
    assert.equal(await page.locator('.task-row').filter({ hasText: title }).count(), 0,
      'running task must be off page 1');
    await page.locator('#current-work').filter({ hasText: title }).waitFor({ timeout: 10000 });

    // 运行行携带任务标题（分页无法藏住当前任务）。
    const project = await page.locator('#project-select').inputValue();
    const state = await (await page.context().request.get(
      new URL('/api/state?limit=50&offset=0&filter=active&system=true', url).href)).json();
    assert.equal(state.runs.find(run => !run.ended)?.task_title, title,
      'open run row must carry the task title');

    // 分组筛选把运行中任务从列表排除后，标题仍然显示。
    for (const filter of ['attention', 'history', 'active']) {
      await page.locator('[data-task-filter="' + filter + '"]').click();
      await page.locator('#current-work').filter({ hasText: title }).waitFor({ timeout: 10000 });
    }

    // 项目切换：另一个项目不显示本项目的运行标题，切回后恢复。
    const options = page.locator('#project-select option');
    const other = await options.filter({ hasText: '另一个独立项目' }).getAttribute('value');
    await page.locator('#project-select').selectOption(other);
    await page.locator('#project-status').filter({ hasText: '已暂停' }).waitFor({ timeout: 15000 });
    await page.locator('#current-work').filter({ hasText: pausedCopy }).waitFor({ timeout: 10000 });
    assert.ok(!(await page.locator('#current-work').textContent()).includes(title), 'no cross-project leak');
    await page.locator('#project-select').selectOption(project);
    await page.locator('.task-row').first().waitFor({ timeout: 10000 });
    await page.locator('#current-work').filter({ hasText: title }).waitFor({ timeout: 10000 });

    // 运行结束：卡片回退到等待文案，不再显示标题或状态词。
    server.stdin.write('end-current-work-run\n');
    await page.locator('#current-work').filter({ hasText: pausedCopy }).waitFor({ timeout: 20000 });
    assert.equal(await page.locator('#current-work').textContent(), pausedCopy);
    console.log('Current work browser passed: running task title survives deep queues, group filters and project switches; the card falls back once the run ends.');
  } finally {
    await page.close();
  }
}

(async () => {
  const server = spawn(process.env.PYTHON || 'python3', ['-m', 'tests.browser_fixture'], {
    stdio: ['pipe', 'pipe', 'pipe'],
    env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1', RSI_BROWSER_CURRENT_WORK: '1' },
  });
  server.stderr.on('data', chunk => process.stderr.write(chunk));
  let browser;
  const errors = [];
  try {
    const url = await new Promise((resolve, reject) => {
      const timeout = setTimeout(() => reject(Error('Console startup timed out')), 30000);
      createInterface({ input: server.stdout }).once('line', line => { clearTimeout(timeout); resolve(line); });
      server.once('exit', code => { clearTimeout(timeout); reject(Error('Console exited: ' + code)); });
    });
    browser = await chromium.launch(process.env.RSI_BROWSER_CHANNEL ? { channel: process.env.RSI_BROWSER_CHANNEL } : {});
    await checkCurrentWork(browser, url, errors, server);
  } finally {
    if (browser) await browser.close();
    server.kill();
  }
  assert.deepEqual(errors, []);
})();
