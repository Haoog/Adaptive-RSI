const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const { createInterface } = require("node:readline");
const { chromium } = require("playwright");

async function checkEvolution(browser, url, errors) {
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  page.on('pageerror', error => errors.push(error.message));
  const posts = [], stateSections = [];
  page.on('request', request => {
    const parsed = new URL(request.url());
    if (request.method() === 'POST' && parsed.pathname.startsWith('/api/')) posts.push(parsed.pathname);
    if (parsed.pathname === '/api/state') stateSections.push(parsed.searchParams.get('sections'));
  });
  try {
    await page.goto(url);
    await page.locator('#project-status').filter({hasText: '已暂停'}).waitFor({timeout: 15000});
    await page.locator('[data-view="evolution"]').first().click();
    await page.locator('#view-evolution').waitFor({state: 'visible'});
    await page.locator('#view-evolution details').first().locator('summary').click();
    await page.locator('#suite-summary .suite-card').first().waitFor({state: 'visible'});

    // ---------- 1) 套件卡显示已用/上限，而不是只有样本数 ----------
    const suites = page.locator('#suite-summary .suite-card');
    assert.equal(await suites.count(), 3);
    assert.match(await suites.nth(0).innerText(), /^4\s*开发集/m);
    assert.match(await suites.nth(1).innerText(), /已用 6\/9/);
    assert.match(await suites.nth(2).innerText(), /已用 9\/9/);

    // ---------- 2) 只给 ready 候选提供启用按钮，重复运行不冒充独立样本 ----------
    const readyCard = page.locator('#candidates .candidate-card').filter({
      has: page.locator('.badge', {hasText: '等待启用'}),
    });
    assert.equal(await readyCard.count(), 1);
    assert.match(await readyCard.innerText(), /3 个独立用例 · 共 12 次配对运行/);
    assert.equal(await readyCard.getByRole('button', {name: '启用这个策略', exact: true}).count(), 1);
    assert.equal(await page.locator('#candidates .candidate-card').filter({
      has: page.locator('.badge', {hasText: '稳定使用'}),
    }).getByRole('button', {name: '启用这个策略', exact: true}).count(), 0);

    // ---------- 3) 曝光耗尽原因给出可操作中文说明，不再直出英文 ----------
    const exhaustedCard = page.locator('#candidates .candidate-card').filter({
      has: page.locator('.badge', {hasText: '证据不足'}),
    });
    const exhaustedText = await exhaustedCard.innerText();
    assert.match(exhaustedText, /曝光次数已达上限/);
    assert.match(exhaustedText, /evolution\.max_holdout_uses/);
    assert.doesNotMatch(exhaustedText, /Validation or holdout exposure limit reached/);

    // ---------- 4) 英文界面对应文案：副标题、曝光提示、样本与启用按钮 ----------
    await page.locator('#language-select').selectOption('en');
    await page.locator('#page-name').filter({hasText: 'Learning history'}).waitFor();
    assert.equal(await page.locator('#view-evolution .page-heading p').innerText(),
      'The new approach is compared with the old one first and adopted only after it proves better.');
    assert.match(await page.locator('#suite-summary .suite-card').nth(1).innerText(), /Used 6\/9/);
    assert.match(await page.locator('#candidates .candidate-card').filter({
      has: page.locator('.badge', {hasText: 'Insufficient evidence'}),
    }).innerText(), /exposure is at its limit/);
    assert.equal(await page.getByRole('button', {name: 'Enable this strategy', exact: true}).count(), 1);
    await page.locator('#language-select').selectOption('zh-CN');
    await page.getByRole('button', {name: '启用这个策略', exact: true}).waitFor();

    // ---------- 5) 确认后调用受控晋升，并绕过 TTL 立即刷新候选 ----------
    const fullBefore = stateSections.length;
    await page.getByRole('button', {name: '启用这个策略', exact: true}).click();
    const dialog = page.locator('dialog.confirm-dialog');
    await dialog.waitFor({timeout: 5000});
    const consequence = await dialog.innerText();
    assert.match(consequence, /替换当前稳定策略/);
    assert.match(consequence, /有限监控/);
    assert.match(consequence, /回滚/);
    await dialog.getByRole('button', {name: '确认', exact: true}).click();
    await page.waitForFunction(() => ![...document.querySelectorAll('#candidates .candidate-card')]
      .some(card => card.textContent.includes('等待启用')), null, {timeout: 15000});
    assert.ok(posts.includes('/api/evolution/promote'), 'manual promotion uses the controlled API');
    assert.ok(stateSections.slice(fullBefore).includes('core,candidates,analytics'),
      'promotion bypasses the candidates/analytics TTL');
    assert.equal(await page.locator('#candidates .badge').filter({hasText: '稳定使用'}).count(), 1);
    assert.equal(await page.locator('#candidates .badge').filter({hasText: '历史稳定版'}).count(), 1);
    await page.getByText('配对质量有所提升且未观察到退步；用量单独记录。').waitFor();
    assert.ok(![...posts].some(path => /model-center|probe|request$/.test(path)), 'no model calls were started');
    assert.deepEqual(errors, []);
    console.log('Evolution browser passed: exposure/limit counts, ready-only manual promotion with confirmation, immediate forced refresh, bilingual reasons and corrected subtitle.');
  } finally {
    await page.close();
  }
}

(async () => {
  const server = spawn(process.env.PYTHON || 'python3', ['-m', 'tests.browser_fixture'], {
    stdio: ['pipe', 'pipe', 'pipe'],
    env: {...process.env, PYTHONDONTWRITEBYTECODE: '1', RSI_BROWSER_EVOLUTION: '1'},
  });
  server.stderr.on('data', chunk => process.stderr.write(chunk));
  let browser;
  const errors = [];
  try {
    const url = await new Promise((resolve, reject) => {
      const timeout = setTimeout(() => reject(Error('Fixture timed out')), 30000);
      createInterface({input: server.stdout}).once('line', line => { clearTimeout(timeout); resolve(line); });
      server.once('exit', code => { clearTimeout(timeout); reject(Error('Console exited: ' + code)); });
    });
    browser = await chromium.launch(process.env.RSI_BROWSER_CHANNEL ? {channel: process.env.RSI_BROWSER_CHANNEL} : {});
    await checkEvolution(browser, url, errors);
  } finally {
    if (browser) await browser.close();
    server.kill();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
