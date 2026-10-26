const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const { createInterface } = require("node:readline");
const { chromium } = require("playwright");

async function openDetail(page, title) {
  await page.locator('[data-task-filter="history"]').click();
  const row = page.locator('.task-row').filter({ hasText: title });
  await row.first().waitFor({ timeout: 10000 });
  // 按钮文案跟随界面语言（查看详情 / Details）。
  await row.first().getByRole('button', { name: /^(查看详情|Details)$/, exact: true }).click();
  await page.locator('#detail-dialog[open]').waitFor({ timeout: 5000 });
  await page.locator('#detail-title').filter({ hasText: title }).waitFor({ timeout: 5000 });
  await page.locator('#detail-body .task-controls').waitFor({ timeout: 8000 });
}

async function closeDetail(page) {
  await page.evaluate(() => document.getElementById('detail-dialog').close());
  await page.locator('#detail-dialog').waitFor({ state: 'hidden', timeout: 3000 }).catch(() => {});
}

const blockBySummary = (page, summary) => page.locator('#detail-body .task-controls > details').filter({
  has: page.locator('summary', { hasText: summary }),
});

async function checkTriageVisibility(browser, url, errors) {
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  page.on('pageerror', error => errors.push(error.message));
  let paid = 0;
  page.on('request', request => {
    if (/\/api\/(?:model-center\/probe|pause|request)$/.test(new URL(request.url()).pathname) && request.method() === 'POST') paid++;
  });
  try {
    await page.goto(url);
    await page.locator('#project-status').filter({ hasText: '已暂停' }).waitFor({ timeout: 15000 });
    await page.locator('[data-task-filter="history"]').click();

    // ---------- 1) 列表：不采纳/暂缓理由以固定样式显示，不再只靠徽标 ----------
    const reasons = {
      '初筛不采纳任务': 'Cosmetic churn: comment style change with disproportionate cost.',
      '初筛暂缓任务': 'Evidence is weak: the quoted line does not fully match the report.',
      '存量暂缓任务': 'Weak evidence: overlaps an existing queued finding.',
    };
    for (const [title, reason] of Object.entries(reasons)) {
      const row = page.locator('.task-row').filter({ hasText: title }).first();
      await row.waitFor({ timeout: 10000 });
      const shown = await row.locator('.task-reason').textContent();
      assert.equal(shown, reason, title + ' list reason');
    }

    // ---------- 2) 详情：不采纳理由结构化展示（默认展开，含基线短 SHA，无唤醒条件） ----------
    await openDetail(page, '初筛不采纳任务');
    const rejectedBlock = blockBySummary(page, '不采纳理由');
    await rejectedBlock.waitFor({ timeout: 8000 });
    assert.equal(await rejectedBlock.getAttribute('open'), '', 'rejection block opens by default');
    assert.equal(await rejectedBlock.locator(':scope > p').first().textContent(),
      reasons['初筛不采纳任务'], 'rejection reason paragraph');
    const rejectedBaseline = await rejectedBlock.locator('p', { hasText: '基线' }).textContent();
    assert.match(rejectedBaseline, /^基线 · [0-9a-f]{7}$/, 'short baseline SHA');
    assert.equal(await rejectedBlock.locator('p', { hasText: '唤醒条件' }).count(), 0,
      'rejected work promises no automatic wake-up');
    assert.ok((await rejectedBlock.locator('p', { hasText: '基线' }).getAttribute('title')).length === 40,
      'full SHA kept as tooltip');
    const rejectedRaw = rejectedBlock.locator('> details').filter({ hasText: '原始记录' });
    await rejectedRaw.locator('summary').click();
    assert.ok((await rejectedRaw.locator('pre').textContent()).includes('"decision": "rejected"'),
      'raw decision record stays expandable');
    await closeDetail(page);

    // ---------- 3) 详情：暂缓决定与唤醒条件（中文对照机器枚举） ----------
    for (const title of ['初筛暂缓任务', '存量暂缓任务']) {
      await openDetail(page, title);
      const deferredBlock = blockBySummary(page, '暂缓决定与唤醒条件');
      await deferredBlock.waitFor({ timeout: 8000 });
      assert.equal(await deferredBlock.getAttribute('open'), '', title + ' deferral block opens by default');
      assert.equal(await deferredBlock.locator(':scope > p').first().textContent(), reasons[title],
        title + ' reason paragraph');
      const wake = await deferredBlock.locator('p', { hasText: '唤醒条件' }).textContent();
      assert.equal(wake, '唤醒条件 · 输入、策略或相关代码变化后', title + ' wake condition in Chinese');
      const raw = deferredBlock.locator('> details').filter({ hasText: '原始记录' });
      await raw.locator('summary').click();
      assert.ok((await raw.locator('pre').textContent()).includes('inputs_policy_or_relevant_baseline_change'),
        title + ' raw record keeps the machine enum');
      await closeDetail(page);
    }

    // ---------- 4) 英文界面：唤醒条件等界面文案同步翻译，理由保留模型原文 ----------
    await page.locator('#language-select').selectOption('en');
    await page.locator('[data-task-filter="history"]').click();
    const englishRow = page.locator('.task-row').filter({ hasText: '初筛暂缓任务' }).first();
    await englishRow.waitFor({ timeout: 10000 });
    assert.equal(await englishRow.locator('.task-reason').textContent(), reasons['初筛暂缓任务'],
      'model-authored reason is not rewritten');
    await openDetail(page, '初筛暂缓任务');
    const englishBlock = blockBySummary(page, 'Deferral decision and wake conditions');
    await englishBlock.waitFor({ timeout: 8000 });
    assert.equal(await englishBlock.locator('p', { hasText: 'Wake condition' }).textContent(),
      'Wake condition · When inputs, policy or relevant code change', 'wake condition translated');
    assert.match(await englishBlock.locator('p', { hasText: 'Baseline' }).textContent(), /^Baseline · [0-9a-f]{7}$/,
      'baseline label translated');
    await closeDetail(page);
    await openDetail(page, '初筛不采纳任务');
    const englishRejected = blockBySummary(page, 'Rejection reason');
    await englishRejected.waitFor({ timeout: 8000 });
    assert.equal(await englishRejected.locator('p', { hasText: 'manual reconsideration' }).count(), 1,
      'rejected guidance translated');

    assert.equal(paid, 0, 'triage visibility checks must not start model calls');
    assert.deepEqual(errors, []);
    console.log('Triage visibility browser passed: batch triage rejection and deferral reasons show in list and detail, legacy triage-state migrations render, wake conditions and baseline are bilingual, raw records stay expandable, project stays paused.');
  } finally {
    await page.close();
  }
}

(async () => {
  const server = spawn(process.env.PYTHON || 'python3', ['-m', 'tests.browser_fixture'], {
    stdio: ['pipe', 'pipe', 'pipe'],
    env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1', RSI_BROWSER_TRIAGE_VISIBILITY: '1' },
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
    await checkTriageVisibility(browser, url, errors);
  } finally {
    if (browser) await browser.close();
    server.kill();
  }
})();
