const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const { createInterface } = require("node:readline");
const { chromium } = require("playwright");

async function openDetail(page, title, filter) {
  await page.locator('[data-task-filter="' + filter + '"]').click();
  const row = page.locator('.task-row').filter({ hasText: title });
  await row.first().waitFor({ timeout: 10000 });
  await row.first().getByRole('button', { name: '查看详情', exact: true }).click();
  await page.locator('#detail-dialog[open]').waitFor({ timeout: 5000 });
  await page.locator('#detail-title').filter({ hasText: title }).waitFor({ timeout: 5000 });
  await page.locator('#detail-body .task-controls').waitFor({ timeout: 8000 });
}

async function closeDetail(page) {
  await page.evaluate(() => document.getElementById('detail-dialog').close());
  await page.locator('#detail-dialog').waitFor({ state: 'hidden', timeout: 3000 }).catch(() => {});
}

async function checkOverflow(page, viewport, errors) {
  await openDetail(page, '修订长文本任务', 'attention');
  const revisions = page.locator('#detail-body details').filter({ hasText: '需求修订记录' });
  await revisions.locator(':scope > summary').click();
  await page.waitForTimeout(300);
  const metrics = await page.evaluate(() => {
    const dialog = document.querySelector('#detail-dialog');
    const body = document.querySelector('#detail-body');
    const longParagraphs = [...document.querySelectorAll('#detail-body p,#detail-body .task-controls summary')]
      .filter(node => /X{100}|Z{100}|https:\/\/example\.test\//.test(node.textContent || ''));
    return {
      viewport: innerWidth,
      documentScroll: document.documentElement.scrollWidth,
      dialog: {client: dialog.clientWidth, scroll: dialog.scrollWidth},
      body: {client: body.clientWidth, scroll: body.scrollWidth},
      paragraphs: longParagraphs.map(node => ({
        client: node.clientWidth,
        scroll: node.scrollWidth,
        overflowWrap: getComputedStyle(node).overflowWrap,
        wordBreak: getComputedStyle(node).wordBreak,
        text: (node.textContent || '').slice(0, 40),
      })),
    };
  });
  assert.ok(metrics.paragraphs.length >= 2, `${viewport}: long body and revision paragraphs are present`);
  assert.ok(metrics.documentScroll <= metrics.viewport + 1,
    `${viewport}: document must not scroll horizontally (${metrics.documentScroll} > ${metrics.viewport})`);
  assert.ok(metrics.dialog.scroll <= metrics.dialog.client + 1,
    `${viewport}: dialog must not overflow (${metrics.dialog.scroll} > ${metrics.dialog.client})`);
  assert.ok(metrics.body.scroll <= metrics.body.client + 1,
    `${viewport}: detail body must not overflow (${metrics.body.scroll} > ${metrics.body.client})`);
  for (const paragraph of metrics.paragraphs) {
    assert.ok(paragraph.scroll <= paragraph.client + 1,
      `${viewport}: "${paragraph.text}" overflows (${paragraph.scroll} > ${paragraph.client})`);
    assert.equal(paragraph.overflowWrap, 'anywhere', `${viewport}: long text wraps inside the paragraph`);
    assert.equal(paragraph.wordBreak, 'break-word', `${viewport}: long text breaks inside the paragraph`);
  }
  await closeDetail(page);
  assert.deepEqual(errors, [], `${viewport}: no page errors while opening long detail`);
}

async function checkBackoffRetry(page, url, errors) {
  await page.locator('[data-task-filter="active"]').click();
  const backoffRow = page.locator('.task-row').filter({ hasText: '自动退避中的任务' });
  const plainRow = page.locator('.task-row').filter({ hasText: '普通排队任务' });
  await backoffRow.waitFor({ timeout: 10000 });
  await plainRow.waitFor({ timeout: 10000 });
  assert.equal(await backoffRow.getByRole('button', { name: '立即重试', exact: true }).count(), 1,
    'backoff row offers an immediate retry');
  assert.equal(await plainRow.getByRole('button', { name: '立即重试', exact: true }).count(), 0,
    'ordinary queued row must not offer an immediate retry');

  await backoffRow.getByRole('button', { name: '查看详情', exact: true }).click();
  await page.locator('#detail-dialog[open]').waitFor({ timeout: 5000 });
  await page.locator('#detail-body .task-controls').waitFor({ timeout: 8000 });
  const detailRetry = page.locator('#detail-body .task-controls button', { hasText: '立即重试' });
  assert.equal(await detailRetry.count(), 1, 'backoff detail offers an immediate retry');
  const retried = page.waitForResponse(
    response => new URL(response.url()).pathname === '/api/retry' && response.request().method() === 'POST');
  await detailRetry.click();
  assert.equal((await retried).status(), 200);
  const project = await page.locator('#project-select').inputValue();
  const task = page.locator('#detail-body details').filter({ hasText: '来源与证据' });
  await task.waitFor();
  const taskId = await page.evaluate(() => {
    const match = new URL(location.href).hash.match(/task=([^&]+)/);
    return match ? decodeURIComponent(match[1]) : '';
  });
  assert.ok(taskId, 'detail URL keeps the selected task');
  const detail = await (await page.request.get(new URL('/api/task?id=' + taskId + '&project=' + project, url).href)).json();
  assert.equal(detail.available, 0, 'manual retry clears the backoff availability');
  assert.equal(detail.infra_retries, 0, 'manual retry clears infrastructure retries');
  await page.locator('#detail-body .badge').filter({ hasText: '排队中' }).waitFor({ timeout: 10000 });
  assert.equal(await page.locator('#detail-body .task-controls button', { hasText: '立即重试' }).count(), 0,
    'immediate retry disappears after the wait is cleared');
  await closeDetail(page);

  // 普通排队任务详情同样不出现立即重试，也不出现终态重试。
  await page.locator('[data-task-filter="active"]').click();
  await plainRow.getByRole('button', { name: '查看详情', exact: true }).click();
  await page.locator('#detail-dialog[open]').waitFor({ timeout: 5000 });
  await page.locator('#detail-body .task-controls').waitFor({ timeout: 8000 });
  assert.equal(await page.locator('#detail-body .task-controls button', { hasText: '立即重试' }).count(), 0,
    'ordinary queued detail must not offer an immediate retry');
  assert.equal(await page.locator('#detail-body .task-controls button', { hasText: '基于当前输入重新检查' }).count(), 0,
    'ordinary queued detail must not offer a terminal retry');
  await closeDetail(page);
  assert.deepEqual(errors, [], 'no page errors in the retry flow');
}

async function checkRevisionFlow(browser, url, errors) {
  for (const [name, viewport] of [['mobile', { width: 390, height: 844 }], ['desktop', { width: 1280, height: 900 }]]) {
    const page = await browser.newPage({ viewport, isMobile: name === 'mobile' });
    page.on('pageerror', error => errors.push(name + ': ' + error.message));
    try {
      await page.goto(url);
      await page.locator('#project-status').filter({ hasText: '已暂停' }).waitFor({ timeout: 15000 });
      await page.locator('.task-row').first().waitFor({ timeout: 10000 });
      await checkOverflow(page, name, errors);
      if (name === 'mobile') await checkBackoffRetry(page, url, errors);
      if (name === 'desktop') {
        await openDetail(page, '修订长文本任务', 'attention');
        const supplement = page.locator('.task-supplement-editor');
        assert.equal(await supplement.count(), 1, 'supplement has its own disclosure');
        assert.equal(await supplement.evaluate(node => node.open), false, 'empty supplement starts collapsed');
        assert.equal(await supplement.locator('textarea').first().isVisible(), false);
        await page.locator('.task-supplement-editor > summary').click();
        await page.getByLabel('补充需求（保留在当前任务）', {exact: true}).fill('验证精确修订快照');
        await closeDetail(page);
        await openDetail(page, '修订长文本任务', 'attention');
        assert.equal(await supplement.evaluate(node => node.open), true, 'unsaved supplement reopens automatically');
        assert.equal(await page.getByLabel('补充需求（保留在当前任务）', {exact: true}).inputValue(), '验证精确修订快照');
        await page.getByLabel('干预原因（可选，保留原文）', {exact: true}).fill('补充边界条件');
        const saved = page.waitForResponse(response => new URL(response.url()).pathname === '/api/task/revise' && response.request().method() === 'POST');
        await page.getByRole('button', {name: '保存补充并重新评估', exact: true}).click();
        assert.equal((await saved).status(), 200);
        const history = page.locator('#detail-body .task-controls details').filter({has: page.locator('summary', {hasText: '需求修订记录'})}).first();
        await history.locator(':scope > summary').click();
        const revision = history.locator('details').filter({has: page.locator('summary', {hasText: '验证精确修订快照'})}).first();
        await revision.locator(':scope > summary').click();
        await revision.getByText('旧候选已失效，后续运行将重新验证。', {exact: true}).waitFor();
        assert.ok((await revision.locator('pre').first().textContent()).includes('+验证精确修订快照'));
        await revision.getByText('修改后需求', {exact: true}).click();
        assert.ok((await revision.locator('details').filter({hasText: '修改后需求'}).locator('pre').textContent()).includes('来源正文'));
        const interventions = page.locator('#detail-body .task-controls details').filter({has: page.locator(':scope > summary', {hasText: '人工干预记录'})}).first();
        await interventions.locator(':scope > summary').click();
        const event = interventions.locator('details').filter({hasText: '补充边界条件'}).first();
        await event.locator(':scope > summary').click();
        await event.getByText('补充边界条件', {exact: true}).waitFor();
        const downloaded = page.waitForEvent('download');
        await interventions.getByRole('button', {name: '导出已加载的干预记录', exact: true}).click();
        const receipt = JSON.parse(await require('node:fs/promises').readFile(await (await downloaded).path(), 'utf8'));
        assert.equal(receipt.complete, true);
        assert.ok(receipt.entries.some(entry => entry.data.reason === '补充边界条件' && entry.data.before.body_hash !== entry.data.after.body_hash));
      }
    } finally {
      await page.close();
    }
  }
  console.log('Revision flow browser passed: 390px and 1280px long text stay inside the dialog; backoff retry appears in list and detail and clears on retry; ordinary queued tasks have no retry.');
}

(async () => {
  const server = spawn(process.env.PYTHON || 'python3', ['-m', 'tests.browser_fixture'], {
    stdio: ['pipe', 'pipe', 'pipe'],
    env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1', RSI_BROWSER_REVISION_FLOW: '1' },
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
    await checkRevisionFlow(browser, url, errors);
  } finally {
    if (browser) await browser.close();
    server.kill();
  }
})();
