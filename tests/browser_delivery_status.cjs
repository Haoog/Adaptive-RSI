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
const MULTI_REASON = 'Multiple workflow runs match this operation ID. Compare repository, workflow, ref, exact target SHA and operation ID before selecting one.';
const EMPTY_REASON = 'GitHub has not published a matching workflow run yet. Wait and query again; manual run IDs are not accepted.';
const RERUN_REASON = 'GitHub now reports a new run attempt. Selecting it only reads its status and never repeats the rerun request.';
const candidate = (id, overrides = {}) => ({id, url: 'https://github.com/example/project/actions/runs/' + id,
  status: 'queued', conclusion: null, attempt: 1, created_at: '2026-10-30T00:00:00Z',
  updated_at: '2026-10-30T00:05:00Z', ...overrides});
const manualOperation = (state, candidates, overrides = {}) => ({
  release: {state, repository: 'example/project', workflow: '.github/workflows/publish.yml', ref: 'trusted-v1',
    workflow_sha: 'a'.repeat(40), operation_id: 'rsi-operation', sha: 'b'.repeat(40),
    error: state === 'rerun_unknown' ? 'GitHub has not confirmed a new run attempt' : 'GitHub did not confirm the dispatch',
    workflow_run_id: state === 'rerun_unknown' ? 17 : undefined, previous_attempt: 1, attempt: 1,
    checked_at: Math.floor(Date.now() / 1000),
    reconciliation: {state: candidates.length > 1 ? 'manual_inspection_required' : candidates.length ? 'matched' : 'awaiting_remote_visibility',
      candidate_run_ids: candidates.map(item => item.id), candidates,
      checked_at: Math.floor(Date.now() / 1000), redispatch_allowed: false,
      reason: state === 'rerun_unknown' ? RERUN_REASON : candidates.length > 1 ? MULTI_REASON : candidates.length ? 'One matching workflow run is visible. Reading its status never starts another run.' : EMPTY_REASON},
    ...overrides},
});

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
    let scenario = 'waiting_verification', statusOverride = null, candidatesReply = null, resolveReply = null;
    const candidateCalls = [], resolvePosts = [];
    await page.route('**/api/release-status?*', route => reply(route, statusOverride || operation(scenario)));
    await page.route('**/api/release-candidates?*', route => {
      candidateCalls.push(route.request().url());
      return reply(route, candidatesReply || {state: 'dispatch_unknown', candidates: [], reason: EMPTY_REASON});
    });
    await page.route('**/api/release-resolve', route => {
      resolvePosts.push(route.request().postDataJSON());
      return reply(route, resolveReply?.data ?? {ok: true}, resolveReply?.status ?? 200);
    });
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

    // 人工核对：无候选说明、只读重查、多候选选择、失败提示、重试确认和双语。
    await page.evaluate(() => document.getElementById('detail-dialog').close());
    await page.locator('#language-select').selectOption('zh-CN');
    await page.waitForFunction(() => document.documentElement.lang === 'zh-CN');
    statusOverride = manualOperation('dispatch_unknown', []);
    candidatesReply = {state: 'dispatch_unknown', candidates: [], reason: EMPTY_REASON};
    await openDetail(page, '查看详情');
    let panel = page.locator('#live-task-outcome .release-reconciliation').first();
    await panel.waitFor({timeout: 10000});
    assert.match(await panel.innerText(), /是否启动仍待确认/);
    assert.match(await panel.innerText(), /不接受手动填写运行编号/);
    assert.equal(await panel.locator('select').count(), 0, 'no candidates must not render a selector');
    assert.equal(await panel.locator('input').count(), 0, 'no free-form run id input');
    assert.equal(await panel.getByRole('button', {name: '关联所选记录', exact: true}).count(), 0);
    await panel.getByRole('button', {name: '只读重新查询', exact: true}).waitFor();
    console.log('ok: no candidates explain the state without a free-form run id');

    // 只读重查：服务端返回候选后，用固定仓库链接和候选选择器展示。
    const one = candidate(17);
    statusOverride = manualOperation('dispatch_unknown', [one]);
    candidatesReply = {state: 'dispatch_unknown', candidates: [one],
      reason: 'One matching workflow run is visible. Reading its status never starts another run.'};
    const rechecked = page.waitForResponse(response => response.url().includes('/api/release-candidates?') && response.status() === 200);
    await panel.getByRole('button', {name: '只读重新查询', exact: true}).click();
    await rechecked;
    panel = page.locator('#live-task-outcome .release-reconciliation').first();
    await panel.locator('a[href="https://github.com/example/project/actions/runs/17"]').waitFor({timeout: 10000});
    assert.equal(await panel.locator('select option').count(), 2, 'placeholder plus one server candidate');
    assert.equal(resolvePosts.length, 0, 're-query must not submit a selection');
    assert.match(candidateCalls.at(-1), /project=[^&]+&run=[^&]+&kind=release&target=operation/);
    console.log('ok: read-only re-query lists the fixed repository link');

    // 多候选：只允许选择服务端候选；失败提示后仍可重试，成功后按 GitHub 状态显示。
    const two = [candidate(101), candidate(102, {status: 'completed', conclusion: 'failure'})];
    statusOverride = manualOperation('dispatch_unknown', two);
    await page.evaluate(() => document.getElementById('detail-dialog').close());
    await openDetail(page, '查看详情');
    panel = page.locator('#live-task-outcome .release-reconciliation').first();
    await panel.locator('a[href="https://github.com/example/project/actions/runs/101"]').waitFor({timeout: 10000});
    assert.equal(await panel.locator('a[href="https://github.com/example/project/actions/runs/102"]').count(), 1);
    const select = panel.locator('select.release-candidate-select');
    assert.equal(await select.count(), 1);
    assert.equal(await select.locator('option').count(), 3, 'placeholder plus two server candidates');
    const bind = panel.getByRole('button', {name: '关联所选记录', exact: true});
    assert.equal(await bind.isDisabled(), true, 'selection is explicit');
    await select.selectOption('101');
    assert.equal(await bind.isDisabled(), false);
    const project = await page.locator('#project-select').inputValue();
    resolveReply = {status: 400, data: {error: "The selected workflow run does not match this operation's repository, workflow, ref, exact target SHA or operation ID"}};
    await bind.click();
    await page.locator('#toast').filter({hasText: '所选工作流记录与本次操作的仓库、工作流、引用、精确目标提交或操作编号不一致'}).waitFor({timeout: 10000});
    assert.equal(resolvePosts.length, 1);
    assert.deepEqual(resolvePosts[0], {project, run: 'detail-h1', kind: 'release', target: 'operation', workflow_run_id: 101});
    assert.equal(await panel.count(), 1, 'a rejected selection keeps the candidates visible');

    const resolved = manualOperation('queued', []);
    resolved.release.workflow_run_id = 101;
    statusOverride = resolved;
    resolveReply = {status: 200, data: {ok: true}};
    const resolvedResponse = page.waitForResponse(response => response.url().includes('/api/release-status?') && response.status() === 200);
    await bind.click();
    await resolvedResponse;
    await page.locator('#live-task-outcome .release-status-row').filter({hasText: '排队中'}).waitFor({timeout: 10000});
    assert.equal(await page.locator('#live-task-outcome .release-reconciliation').count(), 0, 'resolved state leaves the manual panel');
    assert.deepEqual(resolvePosts[1], {project, run: 'detail-h1', kind: 'release', target: 'operation', workflow_run_id: 101});
    console.log('ok: multiple candidates require an explicit server-backed selection and show API failures');

    // 重试待确认：列出原始执行记录，英文界面使用已翻译的状态标题和服务端英文原因。
    await page.evaluate(() => document.getElementById('detail-dialog').close());
    statusOverride = manualOperation('rerun_unknown', [candidate(17, {status: 'in_progress', attempt: 2})]);
    await openDetail(page, '查看详情');
    panel = page.locator('#live-task-outcome .release-reconciliation').first();
    await panel.waitFor({timeout: 10000});
    assert.match(await panel.innerText(), /重试是否启动仍待确认/);
    await panel.locator('a[href="https://github.com/example/project/actions/runs/17"]').waitFor();
    assert.match(await panel.innerText(), /核对时间/);
    await page.locator('#language-select').selectOption('en');
    await page.waitForFunction(() => document.documentElement.lang === 'en');
    panel = page.locator('#live-task-outcome .release-reconciliation').first();
    await panel.locator('h5').filter({hasText: 'Retry startup is still unconfirmed'}).waitFor({timeout: 10000});
    assert.match(await panel.innerText(), /GitHub now reports a new run attempt/);
    assert.match(await panel.innerText(), /#17/);
    console.log('ok: bilingual rerun reconciliation keeps the server evidence');
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
