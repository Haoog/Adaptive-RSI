const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const {createInterface} = require('node:readline');
const {chromium} = require('playwright');

// 「发布与上线」面板回归：服务端 error 字符串有中文映射；等待中状态用中性样式，
// 真正失败（blocked_verification/superseded/health_failed）保持红色；英文界面同步翻译。
const VERIFICATION_ERROR = 'Post-merge verification must pass before release/deploy';
const STALE_TARGET_ERROR = 'Deployment target already has this or a newer commit; refusing a stale or diverged deployment';
const OWNERSHIP_ERROR = 'Deployment ownership changed during validation';
const DISCARDED_HEALTH_ERROR = 'A newer deployment owns this target; old health evidence was discarded';
const HEALTH_ERROR = 'Health response must confirm the exact deployment SHA and healthy status';
const IDENTITY_ERROR = 'Workflow run identity differs from the authorized operation';
const RED = 'rgb(157, 48, 41)';

const VERIFICATION_ZH = '合并后检查尚未通过，发布与部署会在检查通过后自动继续。';
const VERIFICATION_EN = 'Post-merge verification has not passed yet; release and deploy will continue automatically once it passes.';
const STALE_TARGET_ZH = '部署目标已包含本次或更新的提交，继续部署会回退服务，已拒绝这次过期部署。';
const STALE_TARGET_EN = 'The deployment target already contains this or a newer commit. The stale deployment was refused instead of rolling the service back.';
const OWNERSHIP_ZH = '校验期间同一服务的部署归属发生了变化，本次部署会等最新部署结束后继续。';
const DISCARDED_HEALTH_ZH = '已有更新版本接管此服务，本次部署的健康检查结果已作废。';
const HEALTH_ZH = '健康检查未确认本次部署版本正常，请检查服务返回的版本和状态。';
const IDENTITY_ZH = '工作流执行记录与授权的部署操作不一致，请到 GitHub 核对本次部署对应的工作流记录。';

// 场景 → [行标题, 状态文案, 消息, 期望样式]；样式 muted=进行中等待，form-error=真实失败。
const CASES = [
  ['waiting_verification', ['发布', '等待合并后检查', VERIFICATION_ZH, 'muted']],
  ['blocked_verification', ['发布', '合并后检查未通过，暂不发布', VERIFICATION_ZH, 'form-error']],
  ['waiting_release', ['部署', '等待发布成功', VERIFICATION_ZH, 'muted']],
  ['waiting_target', ['部署', '等待同一服务的上次部署结束', OWNERSHIP_ZH, 'muted']],
  ['superseded_stale', ['部署', '已有更新版本接管部署', STALE_TARGET_ZH, 'form-error']],
  ['superseded_health', ['部署', '已有更新版本接管部署', DISCARDED_HEALTH_ZH, 'form-error']],
  ['health_failed', ['部署', '上线后健康检查失败', HEALTH_ZH, 'form-error']],
  ['identity_mismatch', ['部署', '排队中', IDENTITY_ZH, 'form-error']],
];

const reply = (route, data, status = 200) => route.fulfill({status, contentType: 'application/json', body: JSON.stringify(data)});
const operation = scenario => {
  const state = scenario.startsWith('superseded') ? 'superseded' : scenario === 'identity_mismatch' ? 'queued' : scenario;
  const kind = ['waiting_verification', 'blocked_verification'].includes(state) ? 'release' : 'deploy';
  return {[kind]: {state, error: {waiting_verification: VERIFICATION_ERROR, blocked_verification: VERIFICATION_ERROR,
    waiting_release: VERIFICATION_ERROR, waiting_target: OWNERSHIP_ERROR, superseded_stale: STALE_TARGET_ERROR,
    superseded_health: DISCARDED_HEALTH_ERROR, health_failed: HEALTH_ERROR, identity_mismatch: IDENTITY_ERROR}[scenario]}};
};

async function openDetail(page, buttonName) {
  await page.locator('[data-task-filter="attention"]').click();
  const row = page.locator('.task-row').filter({hasText: '详情历史任务'});
  await row.first().waitFor({timeout: 10000});
  await row.first().getByRole('button', {name: buttonName, exact: true}).click();
  await page.locator('#detail-dialog[open]').waitFor({timeout: 5000});
}

async function assertRow(page, [name, [kind, stateLabel, message, cls]]) {
  const row = page.locator('#live-task-outcome .release-status-row').filter({hasText: kind});
  await row.filter({hasText: stateLabel}).waitFor({timeout: 10000});
  const shown = row.locator('p.' + cls).filter({hasText: message});
  await shown.waitFor({timeout: 10000});
  assert.equal(await shown.count(), 1, name + ' must show the mapped message once');
  const wrong = cls === 'muted' ? 'form-error' : 'muted';
  assert.equal(await row.locator('p.' + wrong).count(), 0, name + ' must not mix ' + wrong + ' into a ' + cls + ' row');
  const color = await shown.evaluate(node => getComputedStyle(node).color);
  if (cls === 'form-error') assert.equal(color, RED, name + ' failure message must render in the error red');
  else assert.notEqual(color, RED, name + ' waiting notice must not render in the error red');
}

async function main() {
  const server = spawn(process.env.PYTHON || 'python3', ['-m', 'tests.browser_fixture'], {
    stdio: ['pipe', 'pipe', 'pipe'],
    env: {...process.env, PYTHONDONTWRITEBYTECODE: '1', RSI_BROWSER_TASK_DETAIL: '1'},
  });
  let browser;
  const errors = [];
  try {
    const url = await new Promise((resolve, reject) => {
      const timeout = setTimeout(() => reject(Error('Console startup timed out')), 30000);
      createInterface({input: server.stdout}).once('line', line => {clearTimeout(timeout); resolve(line);});
      server.once('exit', code => {clearTimeout(timeout); reject(Error('Console exited: ' + code));});
    });
    browser = await chromium.launch(process.env.RSI_BROWSER_CHANNEL ? {channel: process.env.RSI_BROWSER_CHANNEL} : {});
    const page = await browser.newPage({viewport: {width: 1380, height: 1000}});
    page.on('pageerror', error => errors.push(error.message));
    let scenario = 'waiting_verification';
    await page.route('**/api/release-status?*', route => reply(route, operation(scenario)));
    // 「发布与上线」只渲染在最新一次运行上；把 fixture 中已合并的 detail-h1 排到首位。
    await page.route('**/api/task-runs?*', async route => {
      const response = await route.fetch();
      const rows = await response.json();
      await route.fulfill({response, contentType: 'application/json', body: JSON.stringify([...rows].reverse())});
    });
    await page.goto(url);
    await page.locator('#project-status').filter({hasText: '已暂停'}).waitFor({timeout: 15000});

    // 中文界面：逐一注入状态，断言映射文案与红/中性样式。
    await openDetail(page, '查看详情');
    await page.locator('#live-task-outcome h4').filter({hasText: '发布与上线'}).waitFor({timeout: 10000});
    for (const [name, expectation] of CASES) {
      scenario = name;
      await page.evaluate(() => document.getElementById('detail-dialog').close());
      await openDetail(page, '查看详情');
      await assertRow(page, [name, expectation]);
    }
    console.log('ok: all injected states show mapped Chinese copy with the right style');

    // 英文界面：等待态与终止失败态的映射文案同步翻译，样式不变。
    scenario = 'waiting_verification';
    await page.locator('#language-select').selectOption('en');
    await page.locator('#live-task-outcome .release-status-row p.muted').filter({hasText: VERIFICATION_EN}).waitFor({timeout: 10000});
    scenario = 'superseded_stale';
    await page.evaluate(() => document.getElementById('detail-dialog').close());
    await openDetail(page, 'Details');
    await assertRow(page, ['superseded_stale_en', ['Deploy', 'A newer version has taken over deployment', STALE_TARGET_EN, 'form-error']]);
    console.log('ok: English interface shows translated waiting and failure copy');
    await page.close();

    assert.deepEqual(errors, [], 'no page errors: ' + JSON.stringify(errors));
    console.log('Delivery status browser checks passed.');
  } finally {
    if (browser) await browser.close();
    server.stdin.end('\n');
    const ended = new Promise(resolve => server.once('exit', resolve));
    const timeout = setTimeout(() => server.kill('SIGTERM'), 5000);
    if (server.exitCode === null && server.signalCode === null) await ended;
    clearTimeout(timeout);
  }
}
main().catch(error => {console.error(error); process.exitCode = 1;});
