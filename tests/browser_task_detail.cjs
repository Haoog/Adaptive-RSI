const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const { createInterface } = require("node:readline");
const { chromium } = require("playwright");

async function openDetail(page, title, filter) {
  await closeDetail(page);
  await page.locator('[data-task-filter="' + filter + '"]').click();
  const row = page.locator('.task-row').filter({ hasText: title });
  await row.first().waitFor({ timeout: 10000 });
  await row.first().getByRole('button', { name: '查看详情', exact: true }).click();
  await page.locator('#detail-dialog[open]').waitFor({ timeout: 5000 });
  await page.locator('#detail-title').filter({ hasText: title }).waitFor({ timeout: 5000 });
}

async function closeDetail(page) {
  if (await page.locator('#detail-dialog').evaluate(dialog => dialog.open)) {
    await page.locator('#detail-dialog .close-dialog').click();
  }
  await page.locator('#detail-dialog').waitFor({ state: 'hidden', timeout: 10000 });
}

async function checkTaskDetail(browser, url, errors) {
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  page.on('pageerror', error => errors.push(error.message));
  let paid = 0;
  page.on('request', request => {
    if (/\/api\/(?:model-center\/probe|pause|request)$/.test(new URL(request.url()).pathname) && request.method() === 'POST') paid++;
  });
  try {
    await page.goto(url);
    await page.locator('#project-status').filter({ hasText: '已暂停' }).waitFor({ timeout: 15000 });
    await page.locator('.task-row').first().waitFor({ timeout: 10000 });
    const project = await page.locator('#project-select').inputValue();

    // ---------- 1) 运行历史懒加载失败：区块内错误项 + 重试成功 ----------
    await openDetail(page, '详情历史任务', 'attention');
    await page.locator('#live-task-outcome').waitFor({ timeout: 5000 });
    const valueBox = page.locator('#live-task-outcome .value-feedback');
    await valueBox.getByLabel('价值评价').selectOption('helpful');
    await valueBox.getByLabel('评价说明（可选）').fill('<script>original feedback</script>');
    const feedbackSaved = page.waitForResponse(response =>
      new URL(response.url()).pathname === '/api/task/value-feedback' && response.request().method() === 'POST');
    await valueBox.getByRole('button', {name: '保存评价', exact: true}).click();
    assert.equal((await feedbackSaved).status(), 200);
    await valueBox.getByRole('status').filter({hasText: '评价已保存'}).waitFor();
    const persisted = await page.evaluate(async () => (await fetch('/api/run?id=detail-h3')).json());
    assert.equal(persisted.evidence_summary.value_feedback.rating, 'helpful');
    assert.equal(persisted.evidence_summary.value_feedback.note, '<script>original feedback</script>');
    assert.equal(await valueBox.locator('script').count(), 0);
    await closeDetail(page);
    await openDetail(page, '详情历史任务', 'attention');
    await page.locator('#live-task-outcome .value-feedback').waitFor();
    assert.equal(await page.locator('#live-task-outcome .value-feedback select').inputValue(), 'helpful');
    const sections = page.locator('#detail-body > details.detail-section[data-run]');
    assert.equal(await sections.count(), 3, 'three run history sections');
    // 首个运行已随详情加载；中间与更早的运行懒加载。
    await sections.nth(0).locator('> summary').click();
    await sections.nth(0).locator('details').filter({ hasText: '原始运行记录' }).waitFor({ timeout: 5000 });

    let aborted = 0;
    await page.route('**/api/run?id=detail-h2', async route => {
      aborted++;
      if (aborted === 1) return route.abort('failed');
      return route.continue();
    });
    await sections.nth(1).locator('> summary').click();
    const failedSection = sections.nth(1);
    await failedSection.locator('.form-error').filter({ hasText: '运行记录加载失败' }).waitFor({ timeout: 5000 });
    assert.equal(await failedSection.locator('details').filter({ hasText: '原始运行记录' }).count(), 0,
      'failed section must not render run content');
    const retryButton = failedSection.getByRole('button', { name: '重试', exact: true });
    assert.equal(await retryButton.count(), 1, 'retry entry inside the failed section');
    await page.unroute('**/api/run?id=detail-h2');
    await retryButton.click();
    await failedSection.locator('details').filter({ hasText: '原始运行记录' }).waitFor({ timeout: 5000 });
    assert.equal(await failedSection.locator('.form-error').count(), 0, 'error item cleared after successful retry');

    // 收起再展开同样会重新请求（失败后 loaded 已复位）。
    let abortedOnce = false;
    await page.route('**/api/run?id=detail-h1', async route => {
      if (!abortedOnce) { abortedOnce = true; return route.abort('failed'); }
      return route.continue();
    });
    await sections.nth(2).locator('> summary').click();
    await sections.nth(2).locator('.form-error').filter({ hasText: '运行记录加载失败' }).waitFor({ timeout: 5000 });
    await page.unroute('**/api/run?id=detail-h1');
    await sections.nth(2).locator('> summary').click(); // 收起
    await sections.nth(2).locator('> summary').click(); // 再次展开 → 重新加载
    await sections.nth(2).locator('details').filter({ hasText: '原始运行记录' }).waitFor({ timeout: 5000 });
    await closeDetail(page);

    // ---------- 2) 待交付任务详情可取消：确认文案说明后果，取消后状态与列表更新 ----------
    await openDetail(page, '待交付详情任务', 'attention');
    await page.locator('#live-task-outcome').waitFor({ timeout: 5000 });
    const cancelButton = page.locator('#detail-body button', { hasText: '取消任务' });
    assert.equal(await cancelButton.count(), 1, 'awaiting_approval detail must offer 取消任务');
    let cancelPosts = 0;
    page.on('request', request => {
      if (new URL(request.url()).pathname === '/api/cancel' && request.method() === 'POST') cancelPosts++;
    });
    const cancelRequest = page.waitForResponse(
      response => new URL(response.url()).pathname === '/api/cancel' && response.request().method() === 'POST');
    await cancelButton.click();
    const confirmBox = page.locator('dialog.confirm-dialog[open]');
    await confirmBox.waitFor({ timeout: 5000 });
    const confirmText = await confirmBox.textContent();
    assert.ok(confirmText.includes('撤销'), 'confirm copy mentions candidate revocation');
    assert.ok(confirmText.includes('重新验证'), 'confirm copy mentions fresh verification');
    assert.ok(confirmText.includes('PR'), 'confirm copy mentions existing PRs unaffected');
    assert.ok(confirmText.includes('等待待交付任务的后继'), 'preview lists the affected downstream task');
    assert.equal(await confirmBox.getByLabel('同时取消可取消的后继任务', {exact: true}).isChecked(), false);
    // 先拒绝一次：不应触发取消请求。
    await confirmBox.locator('button', { hasText: '取消' }).first().click();
    await page.waitForTimeout(400);
    assert.equal(cancelPosts, 0, 'dismissing the confirm dialog must not cancel');
    await cancelButton.click();
    await page.locator('dialog.confirm-dialog[open]').waitFor({ timeout: 5000 });
    await page.locator('dialog.confirm-dialog').getByLabel('同时取消可取消的后继任务', {exact: true}).check();
    await page.locator('dialog.confirm-dialog').getByLabel('干预原因（可选，保留原文）', {exact: true}).fill('取消整个相关范围');
    await page.locator('dialog.confirm-dialog button', { hasText: '确认' }).click();
    const cancelled = await cancelRequest;
    assert.equal(cancelled.status(), 200, await cancelled.text());
    const cancellation = await cancelled.json();
    assert.equal(cancellation.cascade, true);
    assert.equal(cancellation.receipts.filter(item => item.state === 'cancelled').length, 2);
    const cancelTaskId = cancelled.request().postDataJSON().task;
    await page.locator('#detail-dialog').waitFor({ state: 'hidden', timeout: 5000 });
    await page.locator('[data-task-filter="history"]').click();
    const cancelledRow = page.locator('.task-row').filter({ hasText: '待交付详情任务' });
    await cancelledRow.locator('.badge').filter({ hasText: '已取消' }).waitFor({ timeout: 10000 });
    assert.equal(
      (await (await page.request.get(new URL('/api/task?id=' + cancelTaskId + '&project=' + project, url).href)).json()).state,
      'cancelled');
    // 待处理分组不再展示该任务。
    await page.locator('[data-task-filter="attention"]').click();
    await page.locator('.task-row').filter({ hasText: '待交付详情任务' }).waitFor({ state: 'detached', timeout: 10000 })
      .catch(async () => {
        // 等待刷新完成后重新检查一次，避免把尚未完成的轮询当成回归。
        await page.waitForTimeout(500);
        assert.equal(await page.locator('.task-row').filter({ hasText: '待交付详情任务' }).count(), 0,
          'cancelled task must leave the attention group');
      });

    // ---------- 3) 等待额度任务详情内可重试 ----------
    await openDetail(page, '额度等待详情任务', 'attention');
    await page.locator('#detail-body .task-controls').waitFor({ timeout: 8000 });
    const budgetRetry = page.locator('#detail-body button', { hasText: '基于当前输入重新检查' });
    assert.equal(await budgetRetry.count(), 1, 'waiting_budget detail must offer retry');
    const budgetResponse = page.waitForResponse(
      response => new URL(response.url()).pathname === '/api/retry' && response.request().method() === 'POST');
    await budgetRetry.click();
    assert.equal((await budgetResponse).status(), 200);
    await page.locator('#detail-body .badge').filter({ hasText: '排队中' }).waitFor({ timeout: 10000 });
    await closeDetail(page);

    // ---------- 4) 过时任务详情内可重试 ----------
    await openDetail(page, '过时详情任务', 'history');
    await page.locator('#detail-body .task-controls').waitFor({ timeout: 8000 });
    const obsoleteRetry = page.locator('#detail-body button', { hasText: '基于当前输入重新检查' });
    assert.equal(await obsoleteRetry.count(), 1, 'obsolete detail must offer retry');
    const obsoleteResponse = page.waitForResponse(
      response => new URL(response.url()).pathname === '/api/retry' && response.request().method() === 'POST');
    await obsoleteRetry.click();
    assert.equal((await obsoleteResponse).status(), 200);
    await page.locator('#detail-body .badge').filter({ hasText: '排队中' }).waitFor({ timeout: 10000 });
    await closeDetail(page);

    assert.equal(paid, 0, 'task detail controls must not start model calls');
    assert.deepEqual(errors, []);
    console.log('Task detail browser passed: lazy-load failure shows a retryable error, awaiting_approval cancel copy and state updates, waiting_budget and obsolete retry from the detail, project stays paused.');
  } finally {
    await page.close();
  }
}

(async () => {
  const server = spawn(process.env.PYTHON || 'python3', ['-m', 'tests.browser_fixture'], {
    stdio: ['pipe', 'pipe', 'pipe'],
    env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1', RSI_BROWSER_TASK_DETAIL: '1' },
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
    await checkTaskDetail(browser, url, errors);
  } finally {
    if (browser) await browser.close();
    server.kill();
  }
})();
