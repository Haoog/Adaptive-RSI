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

async function checkDetailReadingOrder(page) {
  const metadataPattern = /\/api\/task\?id=/;
  const runPattern = /\/api\/run\?id=detail-h3$/;
  for (const first of ['controls', 'outcome']) {
    let release;
    const gate = new Promise(resolve => release = resolve);
    const metadataRoute = async route => {
      if (first === 'outcome') await gate;
      await route.continue();
    };
    const runRoute = async route => {
      if (first === 'controls') await gate;
      await route.continue();
    };
    await page.route(metadataPattern, metadataRoute);
    await page.route(runPattern, runRoute);
    try {
      await openDetail(page, '详情历史任务', 'attention');
      const controls = page.locator('#detail-body > .task-controls');
      const outcome = page.locator('#detail-body > #live-task-outcome');
      await (first === 'controls' ? controls : outcome).waitFor({timeout: 5000});
      assert.equal(await (first === 'controls' ? outcome : controls).count(), 0,
        'the slower response remains gated while ' + first + ' renders');
      release();
      await controls.waitFor({timeout: 5000});
      await outcome.waitFor({timeout: 5000});
      const order = await page.locator('#detail-body').evaluate(body => {
        const nodes = [...body.children];
        return ['.task-requirements', '.task-controls', '#live-task-outcome', '.detail-section']
          .map(selector => nodes.indexOf(body.querySelector(selector)));
      });
      assert(order.every(index => index >= 0) && order.every((index, i) => i === 0 || order[i - 1] < index),
        'requirements and next steps precede results and evidence when ' + first + ' responds first: ' + order.join(','));
    } finally {
      release();
      await closeDetail(page);
      await page.unroute(metadataPattern, metadataRoute);
      await page.unroute(runPattern, runRoute);
    }
  }
}

async function checkTaskDetail(browser, url, errors) {
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  page.on('pageerror', error => errors.push(error.message));
  let paid = 0;
  page.on('request', request => {
    if (/\/api\/(?:model-center\/probe|pause|request)$/.test(new URL(request.url()).pathname) && request.method() === 'POST') paid++;
  });
  try {
    // —— B08：工作台把原合并与后续主线回归分开显示，后续主线使用服务端 mainline_regressed。
    await page.route('**/api/state?*', async route => {
      const response = await route.fetch(), data = await response.json();
      if (Array.isArray(data.quality)) data.quality = data.quality.map(row => ({...row, postmerge_verified: 1, postmerge_regressed: 0, mainline_regressed: 1}));
      await route.fulfill({response, json: data});
    });
    await page.goto(url);
    await page.locator('#project-status').filter({ hasText: '已暂停' }).waitFor({ timeout: 15000 });
    await page.locator('.task-row').first().waitFor({ timeout: 10000 });
    const qualityCard = label => page.locator('#quality-summary > div').filter({hasText: label});
    await page.locator('#runtime-evidence').evaluate(node => node.open = true);
    await qualityCard('原合并版本验证通过').waitFor({timeout: 10000});
    assert.equal(await qualityCard('原合并版本验证通过').locator('strong').textContent(), '1');
    assert.equal(await qualityCard('原合并版本出现回归').locator('strong').textContent(), '0');
    assert.equal(await qualityCard('后续主线出现回归').locator('strong').textContent(), '1');
    await page.unroute('**/api/state?*');
    const project = await page.locator('#project-select').inputValue();
    await checkDetailReadingOrder(page);

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
    // —— B11：持久评价变化能触发实时刷新；状态重建保留未提交草稿；刷新失败显示中断/上次成功/重试。
    let liveSummary = null, liveBody = null;
    const runRoute = async route => {
      const response = await route.fetch(), data = await response.json();
      data.evidence_summary = {...data.evidence_summary, value_feedback: {rating: 'unnecessary', note: '另一个窗口保存的评价'}};
      if (liveSummary) data.result.summary = liveSummary;
      await route.fulfill({response, json: data});
    };
    const taskRoute = async route => {
      const response = await route.fetch(), data = await response.json();
      if (liveBody) data.body = liveBody;
      await route.fulfill({response, json: data});
    };
    await page.route(/\/api\/run\?id=detail-h3$/, runRoute);
    await page.waitForFunction(() => document.querySelector('#live-task-outcome .value-feedback select')?.value === 'unnecessary', null, {timeout: 15000});
    assert.equal(await page.locator('#live-task-outcome .value-feedback textarea').inputValue(), '另一个窗口保存的评价',
      'persisted feedback changes must refresh the live outcome');
    await page.locator('#live-task-outcome .value-feedback select').selectOption('helpful');
    await page.locator('#live-task-outcome .value-feedback textarea').fill('尚未保存的草稿');
    await page.route(/\/api\/task\?/, taskRoute);
    liveSummary = '实时刷新后的结果'; liveBody = '实时刷新后的来源正文';
    await page.locator('#detail-body').filter({hasText: '实时刷新后的来源正文'}).waitFor({timeout: 15000});
    await page.locator('#live-task-outcome').filter({hasText: '实时刷新后的结果'}).waitFor({timeout: 5000});
    assert.equal(await page.locator('#live-task-outcome .value-feedback select').inputValue(), 'helpful',
      'rebuilding the detail must keep the unsaved rating draft');
    assert.equal(await page.locator('#live-task-outcome .value-feedback textarea').inputValue(), '尚未保存的草稿',
      'rebuilding the detail must keep the unsaved note draft');
    await page.unroute(/\/api\/task\?/, taskRoute);
    const failing = route => route.fulfill({status: 503, contentType: 'application/json', body: JSON.stringify({error: 'Isolated polling outage'})});
    await page.route(/\/api\/task\?/, failing);
    const liveStatus = page.locator('#detail-live-status');
    await liveStatus.waitFor({timeout: 15000});
    const liveStatusCopy = await liveStatus.innerText();
    assert.match(liveStatusCopy, /实时刷新中断/, 'a failed live poll must leave a visible interruption marker');
    assert.match(liveStatusCopy, /上次成功更新/, 'the interruption marker must show the last successful update');
    assert.equal(await liveStatus.getByRole('button', {name: '重试', exact: true}).count(), 1);
    assert.equal(await page.locator('#live-task-outcome .value-feedback select').inputValue(), 'helpful');
    await page.unroute(/\/api\/task\?/, failing);
    await liveStatus.getByRole('button', {name: '重试', exact: true}).click();
    await liveStatus.waitFor({state: 'hidden', timeout: 15000});
    assert.equal(await page.locator('#live-task-outcome .value-feedback textarea').inputValue(), '尚未保存的草稿');
    // 清空已保存评价同样是未提交草稿，不能被另一个窗口的评价或轮询重建覆盖。
    // Capture both edits on the same form before a live poll can replace it.
    await page.locator('#live-task-outcome .value-feedback').evaluate(form => {
      const rating = form.querySelector('select'), note = form.querySelector('textarea');
      rating.value = '';rating.dispatchEvent(new Event('change', {bubbles:true}));
      note.value = '';note.dispatchEvent(new Event('input', {bubbles:true}));
    });
    liveSummary = '清空评价后的实时结果';
    await page.locator('#live-task-outcome').filter({hasText: liveSummary}).waitFor({timeout: 15000});
    assert.equal(await page.locator('#live-task-outcome .value-feedback select').inputValue(), '',
      'rebuilding the detail must preserve an explicitly cleared rating');
    assert.equal(await page.locator('#live-task-outcome .value-feedback textarea').inputValue(), '',
      'rebuilding the detail must preserve an explicitly cleared note');
    await page.unroute(/\/api\/run\?id=detail-h3$/, runRoute);
    const clearedFeedback = page.waitForResponse(response =>
      new URL(response.url()).pathname === '/api/task/value-feedback' && response.request().method() === 'POST');
    await page.locator('#live-task-outcome .value-feedback').getByRole('button', {name: '保存评价', exact: true}).click();
    const clearResponse = await clearedFeedback;
    assert.equal(clearResponse.status(), 200);
    assert.equal(clearResponse.request().postDataJSON().rating, null);
    assert.equal(clearResponse.request().postDataJSON().note, '');
    const clearedRun = await page.evaluate(async () => (await fetch('/api/run?id=detail-h3')).json());
    assert.equal(clearedRun.evidence_summary.value_feedback, null, 'saving the empty draft removes persisted feedback');
    // 注入的元数据/运行响应已经撤销；重开详情，让后续运行历史用例从服务端真实状态开始。
    await closeDetail(page);
    await openDetail(page, '详情历史任务', 'attention');
    await page.locator('#live-task-outcome').waitFor();
    assert.equal(await page.locator('#live-task-outcome .value-feedback select').inputValue(), '');
    assert.equal(await page.locator('#live-task-outcome .value-feedback textarea').inputValue(), '');
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
    assert.equal(await cancelButton.isVisible(), false, 'cancellation is secondary to reading the task');
    await page.locator('.task-secondary-actions > summary').click();
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
