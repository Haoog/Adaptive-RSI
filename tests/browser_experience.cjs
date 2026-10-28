const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const { createInterface } = require("node:readline");
const { once } = require("node:events");
const { chromium } = require("playwright");

async function checkGoalRecovery(page, evidence) {
  // The install-page check shares localStorage and previously selected English.
  await page.locator('#language-select').selectOption('zh-CN');
  const original = '手机测试目标\n  保留原文空格、换行与 <tag> & English details。  ',
    changed = '换文本后的失败目标',
    otherText = '另一个项目的待确认目标',
    texts = [original, changed, otherText],
    requests = [];
  let accepted;
  const intercept = async (route) => {
    requests.push(route.request().postDataJSON());
    if (requests.length === 1) {
      // Commit through the real server, then lose only the browser response.
      const response = await route.fetch();
      assert.equal(response.status(), 200, await response.text());
      accepted = await response.json();
      await route.abort('failed');
    } else if (requests.length <= 3) {
      // New text fails before reaching the server and must get its own record.
      await route.abort('failed');
    } else {
      await route.continue();
    }
  };
  await page.route('**/api/goal', intercept);
  try {
    const project = await page.locator('#project-select').inputValue(),
      other = await page.locator('#project-select option').evaluateAll(
        (nodes, current) => nodes.map(node => node.value).find(id => id && id !== current), project,
      ),
      input = page.locator('#goal-text'),
      submit = page.locator('#goal-form button[type="submit"]'),
      recovery = page.locator('[data-goal-recovery]');
    const failGoal = async (text) => {
      const count = requests.length;
      await input.fill(text);
      await submit.click();
      await page.locator('#goal-form [data-form-error]:visible').waitFor();
      assert.equal(await submit.isEnabled(), true);
      assert.equal(await input.inputValue(), text);
      assert.equal(requests.length, count + 1);
    };
    const options = (select) => select.locator('option').evaluateAll(
      nodes => nodes.filter(node => node.value).map(node => ({id: node.value, text: node.textContent})),
    );
    const noOverflow = async (language) => {
      const width = page.viewportSize().width;
      const size = await page.evaluate(() => ({
        width: innerWidth, scroll: document.documentElement.scrollWidth,
      }));
      assert.ok(size.width <= width + 1 && size.scroll <= width + 1,
        `${language} recovery overflows ${width}px viewport: ${JSON.stringify(size)}`);
    };
    await failGoal(original);
    assert.ok(accepted.id);
    await failGoal(changed);
    assert.notEqual(requests[0].request_id, requests[1].request_id);
    assert.deepEqual(requests.slice(0, 2).map(({project: id, text}) => [id, text]),
      [[project, original], [project, changed]]);
    await page.reload();
    await page.locator('#project-status').filter({hasText: '已暂停'}).waitFor();
    const chinese = page.getByRole('combobox', {name: '待确认的目标', exact: true});
    await chinese.waitFor();
    assert.equal(await input.inputValue(), changed, 'reload keeps the newer draft');
    assert.deepEqual(await options(chinese), [
      {id: requests[0].request_id, text: original},
      {id: requests[1].request_id, text: changed},
    ]);
    assert.equal(await chinese.locator('option').first().innerText(), '选择要恢复的目标');
    await noOverflow('Chinese');
    let stored = await evidence(texts);
    assert.deepEqual(stored.tasks, [{id: accepted.id, project, body: original, state: 'queued', attempts: 0}]);
    assert.deepEqual(stored.submissions, [{project, request: requests[0].request_id, task: accepted.id}]);

    await page.locator('#project-select').selectOption(other);
    await recovery.waitFor({state: 'hidden'});
    assert.equal(await input.inputValue(), '', 'another project must not inherit the draft');
    await failGoal(otherText);
    assert.deepEqual(await options(chinese), [{id: requests[2].request_id, text: otherText}]);
    assert.equal(requests[2].project, other);
    assert.ok(!requests.slice(0, 2).some(request => request.request_id === requests[2].request_id));
    await page.locator('#language-select').selectOption('en');
    const english = page.getByRole('combobox', {name: 'Unconfirmed goals', exact: true});
    await english.waitFor();
    assert.equal(await english.locator('option').first().innerText(), 'Select a goal to restore');
    await english.selectOption(requests[2].request_id);
    await page.getByRole('button', {name: 'Restore to input (replace current draft)', exact: true}).click();
    assert.equal(await input.inputValue(), otherText);
    await noOverflow('English, other project');

    await page.locator('#project-select').selectOption(project);
    await english.waitFor();
    assert.deepEqual(await options(english), [
      {id: requests[0].request_id, text: original},
      {id: requests[1].request_id, text: changed},
    ], 'each project exposes only its own pending goals');
    assert.equal(await input.inputValue(), changed);
    await english.selectOption(requests[0].request_id);
    await page.getByRole('button', {name: 'Restore to input (replace current draft)', exact: true}).click();
    assert.equal(await input.inputValue(), original);
    await noOverflow('English, original project');
    await page.locator('#language-select').selectOption('zh-CN');
    await chinese.waitFor();
    await input.fill('等待被恢复入口替换的草稿');
    await chinese.selectOption(requests[0].request_id);
    await page.getByRole('button', {name: '恢复到输入框（替换当前草稿）', exact: true}).click();
    assert.equal(await input.inputValue(), original, 'restore preserves exact multiline text');
    await noOverflow('Chinese, restored');
    assert.equal(requests.length, 3, 'reload, project/language switches and restoration must not submit');

    const response = page.waitForResponse(r => new URL(r.url()).pathname === '/api/goal' && r.request().method() === 'POST');
    await submit.click();
    const confirmed = await response;
    assert.equal(confirmed.status(), 200, await confirmed.text());
    const confirmedGoal = await confirmed.json();
    assert.equal(confirmedGoal.id, accepted.id);
    await page.locator('#toast').filter({hasText: '目标已入队'}).waitFor();
    assert.equal(requests.length, 4);
    assert.deepEqual(requests[3], requests[0], 'explicit retry must reuse the original request ID and payload');
    assert.equal(await input.inputValue(), '');
    assert.deepEqual(await options(chinese), [{id: requests[1].request_id, text: changed}],
      'acknowledgement removes only the recovered record');
    stored = await evidence(texts);
    assert.deepEqual(stored.tasks, [{id: accepted.id, project, body: original, state: 'queued', attempts: 0}],
      'real SQLite contains exactly one task after response loss and retry');
    assert.deepEqual(stored.submissions, [{project, request: requests[0].request_id, task: accepted.id}]);
    assert.ok(stored.projects.every(item => item.paused));
    assert.equal(stored.calls, 0);
    await page.locator('#project-select').selectOption(other);
    await chinese.waitFor();
    assert.deepEqual(await options(chinese), [{id: requests[2].request_id, text: otherText}],
      'confirming the original project leaves the other project record intact');
    await page.locator('#project-select').selectOption(project);
    await chinese.waitFor();
    console.log('Chromium goal recovery passed: response loss after server commit, changed text failure, reload, bilingual restoration, explicit original-ID retry, one SQLite task, project isolation, mobile overflow, zero model calls.');
    return [accepted.id, confirmedGoal.id, project];
  } finally {
    await page.unroute('**/api/goal', intercept);
  }
}
async function checkQueueEditor(page, evidence) {
  await page.locator('#language-select').selectOption('zh-CN');
  await page.getByRole('button', {name: '排队草稿验证', exact: true}).click();
  const box = page.getByRole('dialog', {name: '排队草稿验证', exact: true});
  const select = box.getByRole('combobox', {name: '前置任务（合并后再开始）', exact: true});
  await box.getByText('前置任务候选页 1', {exact: true}).waitFor();
  assert.equal(await select.getByRole('option', {name: '旧失败前置任务', exact: true}).count(), 0);
  await box.getByRole('button', {name: '下一页', exact: true}).click();
  await box.getByText('前置任务候选页 2', {exact: true}).waitFor();
  await select.selectOption({label: '旧失败前置任务'});
  const parent = await select.inputValue();
  await box.getByRole('checkbox', {name: '优先处理', exact: true}).uncheck();
  await box.getByRole('button', {name: '上一页', exact: true}).click();
  await box.getByText('前置任务候选页 1', {exact: true}).waitFor();
  assert.equal(await select.inputValue(), parent);
  assert.equal(await box.getByRole('checkbox', {name: '优先处理', exact: true}).isChecked(), false);
  await page.locator('#language-select').selectOption('en');
  const english = box.getByRole('combobox', {name: 'Predecessor (start after it merges)', exact: true});
  await box.getByText('Dependency candidate page 1', {exact: true}).waitFor();
  assert.equal(await english.inputValue(), parent);
  assert.equal(await box.getByRole('checkbox', {name: 'Urgent', exact: true}).isChecked(), false);
  await box.getByRole('textbox', {name: 'Supplement requirements (on this task)', exact: true}).fill('Queue supplement preserves original draft');
  await box.getByRole('button', {name: 'Save supplement and reassess', exact: true}).click();
  await box.getByText('Requirement revisions', {exact: true}).waitFor();
  assert.equal(await english.inputValue(), parent);
  assert.equal(await box.getByRole('checkbox', {name: 'Urgent', exact: true}).isChecked(), false);
  const saved = page.waitForResponse(r => new URL(r.url()).pathname === '/api/task/schedule' && r.request().method() === 'POST');
  await box.getByRole('button', {name: 'Save queue settings', exact: true}).click();
  const result = await saved;
  assert.equal(result.status(), 200, await result.text());
  assert.deepEqual(result.request().postDataJSON(), {task: new URL(page.url()).hash.match(/task=([^&]+)/)[1], dependency: parent, urgency: 0});
  const size = await page.evaluate(() => ({width: innerWidth, scroll: document.documentElement.scrollWidth}));
  assert.ok(size.scroll <= size.width, JSON.stringify(size));
  await page.reload();
  await box.getByText('Dependency candidate page 1', {exact: true}).waitFor();
  assert.equal(await english.inputValue(), parent, 'real server persists the off-page dependency');
  assert.equal(await box.getByRole('checkbox', {name: 'Urgent', exact: true}).isChecked(), false);
  for (const trigger of ['language', 'revision', 'periodic']) {
    const save = box.getByRole('button', {name: 'Save queue settings', exact: true});
    const urgent = box.getByRole('checkbox', {name: 'Urgent', exact: true});
    // Start each race at none/0, then let the real server commit parent/1 before holding its acknowledgement.
    await english.selectOption(''); await urgent.uncheck(); await save.click();
    await page.locator('#toast').filter({hasText: 'Queue settings saved'}).waitFor();
    await save.waitFor({state: 'visible'});
    await page.waitForFunction(() => !document.querySelector('.task-controls form button[type="submit"]:disabled'));
    await box.getByRole('button', {name: 'Next', exact: true}).click();
    await box.getByText('Dependency candidate page 2', {exact: true}).waitFor();
    await english.selectOption(parent); await urgent.check();
    let release, committed, writes = 0;
    const arrived = new Promise(resolve => {committed = resolve;});
    const gate = new Promise(resolve => {release = resolve;});
    const holdAck = async route => {
      writes++;
      const response = await route.fetch();
      assert.equal(response.status(), 200, await response.text());
      committed(); await gate; await route.fulfill({response});
    };
    await page.route('**/api/task/schedule', holdAck);
    const pollState = async route => {
      const response = await route.fetch();
      const data = await response.json();
      await route.fulfill({json: {...data, state: 'blocked'}});
    };
    try {
      await save.click(); await arrived;
      await english.selectOption(''); await urgent.uncheck();
      if (trigger === 'language') {
        await page.locator('#language-select').selectOption('zh-CN');
        await box.getByText('前置任务候选页 1', {exact: true}).waitFor();
        assert.equal(await select.inputValue(), '');
        assert.equal(await box.getByRole('button', {name: '保存排队设置', exact: true}).isDisabled(), true);
        await page.locator('#language-select').selectOption('en');
      } else if (trigger === 'revision') {
        await box.getByRole('textbox', {name: 'Supplement requirements (on this task)', exact: true}).fill('Save acknowledgement race supplement');
        await box.getByRole('button', {name: 'Save supplement and reassess', exact: true}).click();
        await box.getByText('Save acknowledgement race supplement', {exact: false}).waitFor({state: 'attached'});
      } else {
        await page.route('**/api/task?id=*', pollState);
        await box.getByText('Blocked', {exact: true}).waitFor();
      }
      await box.getByText('Dependency candidate page 1', {exact: true}).waitFor();
      assert.equal(await english.inputValue(), ''); assert.equal(await urgent.isChecked(), false);
      assert.equal(await save.isDisabled(), true);
      // A direct form submit also exercises the logical mutex beyond the disabled GUI button.
      await save.evaluate(node => node.form.requestSubmit());
      assert.equal(writes, 1);
      const acknowledged = page.waitForResponse(r => new URL(r.url()).pathname === '/api/task/schedule');
      release(); await acknowledged;
      await page.waitForFunction(() => ![...document.querySelectorAll('.task-controls button')].find(node => node.textContent === 'Save queue settings')?.disabled);
      await page.locator('#language-select').selectOption('zh-CN');
      await box.getByText('前置任务候选页 1', {exact: true}).waitFor();
      assert.equal(await select.inputValue(), '');
      assert.equal(await box.getByRole('checkbox', {name: '优先处理', exact: true}).isChecked(), false);
      await page.locator('#language-select').selectOption('en');
      await box.getByText('Dependency candidate page 1', {exact: true}).waitFor();
    } finally {
      release(); await page.unroute('**/api/task/schedule', holdAck);
      if (trigger === 'periodic') await page.unroute('**/api/task?id=*', pollState);
    }
    const undone = page.waitForResponse(r => new URL(r.url()).pathname === '/api/task/schedule' && r.request().method() === 'POST');
    await save.click(); const response = await undone;
    assert.equal(response.status(), 200);
    assert.equal(response.request().postDataJSON().dependency, null);
    assert.equal(response.request().postDataJSON().urgency, 0);
    await page.reload();
    await box.getByText('Dependency candidate page 1', {exact: true}).waitFor();
    assert.equal(await english.inputValue(), ''); assert.equal(await urgent.isChecked(), false);
    console.log('Queue save race passed: committed server values, held acknowledgement, ' + trigger + ' rebuild, duplicate submit blocked, undo preserved and persisted.');
  }
  const stored = await evidence([]);
  assert.ok(stored.projects.every(project => project.paused));
  assert.equal(stored.calls, 0);
  console.log('Queue editor browser passed: old failed parent, bounded paging, bilingual drafts, supplement, real schedule persistence, mobile layout, paused projects and zero model calls.');
}

(async () => {
  const server = spawn(
    process.env.PYTHON || "python3",
    ["-m", "tests.browser_fixture"],
    { stdio: ["pipe", "pipe", "pipe"] },
  );
  server.stderr.on("data", (chunk) => process.stderr.write(chunk));
  const output = createInterface({ input: server.stdout });
  const evidence = async (texts) => {
    const next = once(output, 'line', {signal: AbortSignal.timeout(10000)});
    server.stdin.write(JSON.stringify({goal_evidence: texts}) + '\n');
    return JSON.parse((await next)[0]);
  };
  let browser;
  try {
    const url = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(Error("Fixture timed out")), 30000);
      output.once("line", (line) => {
        clearTimeout(timer);
        resolve(line);
      });
      server.once("exit", (code) => reject(Error("Fixture exited " + code)));
    });
    browser = await chromium.launch(
      process.env.RSI_BROWSER_CHANNEL
        ? { channel: process.env.RSI_BROWSER_CHANNEL }
        : {},
    );
    const context = await browser.newContext({
      viewport: { width: 390, height: 844 },
      isMobile: true,
      hasTouch: true,
    });
    const page = await context.newPage(),
      errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.goto(url);
    await page
      .locator("#project-status")
      .filter({ hasText: "已暂停" })
      .waitFor();
    if (process.env.RSI_BROWSER_QUEUE_ONLY) {
      await checkQueueEditor(page, evidence);
      assert.deepEqual(errors, []);
      return;
    }
    // Disabled sources keep manual entry points; only a missing repository blocks Issue sync.
    assert.equal(await page.locator('[data-inspect="light"]').isDisabled(), false);
    assert.equal(await page.locator('[data-inspect="issues"]').isDisabled(), true);
    const issueRequestError = await page.evaluate(async () => {
      try {
        await api('request', {project: projectId, kind: 'issues'});
        return 'unexpected success';
      } catch (error) {
        return error.message;
      }
    });
    assert.equal(issueRequestError, '请先在项目设置中填写 GitHub 仓库，再安排 Issue 同步。');
    await page.locator('#language-select').selectOption('en');
    assert.equal(await page.evaluate(async () => {
      try {
        await api('request', {project: projectId, kind: 'issues'});
        return 'unexpected success';
      } catch (error) {
        return error.message;
      }
    }), 'Configure a GitHub repository in project settings before scheduling Issue sync.');
    await page.locator('#language-select').selectOption('zh-CN');
    // Incomplete scans are visible as unfinished work with a continuation, not a completed sync.
    const issueRoute = async route => {
      const response = await route.fetch(), data = await response.json();
      data.events = [{
        seq: 999999, project: data.projects[0]?.id, kind: 'issues', created: Date.now() / 1000,
        message: 'Scanned 3 Issues; 1 updates synchronized; sync continues next cycle',
        data: JSON.stringify({complete: false, scanned: 3, changed: 1, next_page: 2, pages_read: 2}),
      }, ...(data.events || [])];
      await route.fulfill({json: data});
    };
    await page.route('**/api/state*', issueRoute);
    try {
      await page.locator('.sidebar nav [data-view="overview"]').click();
      await page.getByText('最近活动', {exact: true}).click();
      await page.locator('#activity').getByText(
        '已检查 3 个 Issue，同步 1 条更新；本次同步未完成，下一轮继续', {exact: true},
      ).waitFor();
    } finally {
      await page.unroute('**/api/state*', issueRoute);
    }
    // A synced Issue revision keeps queued state but updates the open detail without losing a draft.
    const liveTask = page.locator('#tasks-list .task-title').first();
    const liveTitle = await liveTask.innerText();
    await liveTask.click();
    await page.locator('#detail-dialog[open] #detail-title').filter({hasText: liveTitle}).waitFor();
    const liveDraft = page.locator('#detail-dialog .task-controls textarea').first();
    await liveDraft.fill('本地未保存草稿');
    let syncedDetail = false;
    const syncDetail = async route => {
      const response = await route.fetch(), data = await response.json();
      if (syncedDetail) {
        data.title = '同步后的 Issue 标题';
        data.body = '同步后的 Issue 正文';
        data.evidence = JSON.stringify([{number: 7, updated: 'new', state: 'open'}]);
        data.updated = (data.updated || 0) + 1;
      }
      await route.fulfill({json: data});
    };
    await page.route('**/api/task?id=*', syncDetail);
    try {
      syncedDetail = true;
      await page.locator('#detail-dialog[open] #detail-title')
        .filter({hasText: '同步后的 Issue 标题'}).waitFor({timeout: 15000});
      await page.locator('#detail-dialog').getByText('来源与证据', {exact: true}).click();
      await page.locator('#detail-dialog #detail-body')
        .getByText('同步后的 Issue 正文', {exact: true}).waitFor();
      assert.equal(await page.locator('#detail-dialog .task-controls textarea').first().inputValue(),
        '本地未保存草稿');
    } finally {
      await page.unroute('**/api/task?id=*', syncDetail);
    }
    await page.evaluate(() => {
      RSIDraftStore.remove('task-supplement',
        new URLSearchParams(location.hash.slice(1)).get('task'));
    });
    await page.locator('#detail-dialog .close-dialog').click();
    for (const name of ["overview", "models", "usage"])
      assert.equal(
        await page.locator(`.sidebar nav [data-view="${name}"]`).isVisible(),
        true,
      );
    await page.locator('.sidebar nav [data-view="usage"]').click();
    await page.locator("#usage-totals strong").first().waitFor();
    await page.locator("#mobile-install").click();
    await page
      .locator(".experience-dialog a")
      .filter({ hasText: "/install" })
      .waitFor();
    const install = await page
      .locator(".experience-dialog a")
      .first()
      .getAttribute("href");
    assert.equal(new URL(install).search, "");
    await page
      .locator(".experience-dialog")
      .getByRole("button", { name: "关闭", exact: true })
      .click();
    const installPage = await context.newPage();
    await installPage.goto(install);
    await installPage
      .getByRole("heading", { name: "把工作台装进口袋" })
      .waitFor();
    await installPage.locator("#language-select").selectOption("en");
    await installPage
      .getByRole("heading", { name: "Take your workbench with you" })
      .waitFor();
    const manifest = await (
      await context.request.get(new URL("/manifest.webmanifest", url).href)
    ).json();
    assert.equal(manifest.start_url, "/");
    // any 两种尺寸 + 一份 maskable 图标，Android 自适应裁切使用。
    assert.equal(manifest.icons.length, 3);
    assert.ok(manifest.icons.some((icon) => icon.purpose === "maskable"));
    for (const icon of manifest.icons)
      assert.equal(
        (await context.request.get(new URL(icon.src, url).href)).status(),
        200,
      );
    await installPage.close();
    await page.locator('.sidebar nav [data-view="overview"]').click();
    const ids = await checkGoalRecovery(page, evidence);
    await page
      .getByRole("button", { name: "手机测试目标", exact: true })
      .click();
    await page.getByRole("textbox", {name:"补充需求（保留在当前任务）",exact:true}).fill("增加手机验收要求");
    await page
      .getByRole("button", { name: "保存补充并重新评估", exact: true })
      .click();
    await page
      .locator("#detail-body")
      .filter({ hasText: "增加手机验收要求" })
      .waitFor();
    assert.match(page.url(), /task=/);
    await page.locator("#detail-dialog .close-dialog").click();
    await page.locator('[data-task-filter="attention"]').click();
    await page
      .getByRole("button", { name: "Fix addition", exact: true })
      .click();
    await page.locator("#live-task-outcome").waitFor();
    assert.equal(await page.locator(".evidence-table").count(), 0);
    assert.ok(await page.locator('.check-result').count() > 0);
    await page.locator('.approval-summary').filter({hasText:'本次检查基线'}).waitFor();
    await page.getByRole('button',{name:'批准推送并创建 PR',exact:true}).click();
    await page.locator('.confirm-dialog .approval-summary').filter({hasText:'项目已暂停'}).waitFor();
    await page.locator('.confirm-dialog').getByRole('button',{name:'取消',exact:true}).click();
    await page.locator('.confirm-dialog').waitFor({state:'detached'});
    await page
      .getByRole("button", { name: "阅读代码差异", exact: true })
      .click();
    await page.locator(".readable-diff .diff-add").first().waitFor();
    const diffDialog=page.locator('.experience-dialog');
    await diffDialog.getByRole('searchbox',{name:'在差异中搜索',exact:true}).fill('return');
    await diffDialog.getByRole('button',{name:'下一个匹配',exact:true}).click();
    assert.ok(await diffDialog.locator('.diff-selected').count());
    await diffDialog.getByRole('button',{name:'下一个变更块',exact:true}).click();
    assert.match(await diffDialog.locator('.diff-selected').innerText(),/^@@/);
    await diffDialog.getByLabel('自动换行',{exact:true}).check();
    assert.equal(await diffDialog.locator('.readable-diff').evaluate(node=>node.classList.contains('diff-wrap')),true);
    const jump=diffDialog.getByLabel('跳转到文件',{exact:true});
    if(await jump.count()){await jump.selectOption({index:1});assert.match(await diffDialog.locator('.diff-selected').innerText(),/^diff --git /);}

    await page
      .locator(".experience-dialog")
      .getByRole("button", { name: "关闭", exact: true })
      .click();
    const run = await page
      .locator("[data-run]")
      .first()
      .getAttribute("data-run");
    let runRequests = 0, liveSummary = "";
    const phaseStart = Date.now()/1000-20;
    await page.route("**/api/task-runs?*", async (route) => {
      const response = await route.fetch(),
        rows = await response.json();
      await route.fulfill({
        json: [
          rows[0],
          { ...rows[0], id: "old-run-1" },
          { ...rows[0], id: "old-run-2" },
        ],
      });
    });
    await page.route("**/api/run?id=*", async (route) => {
      runRequests++;
      const parsed = new URL(route.request().url());
      parsed.searchParams.set("id", run);
      const response = await route.fetch({ url: parsed.href }), data = await response.json();
      if (liveSummary) data.result.summary = liveSummary;
      data.state = 'merged';
      data.phases = [{phase:'implementation',started:phaseStart,ended:null}];
      data.result.evaluation.checks.acceptance_baseline=[{name:'Browser acceptance evidence',passed:false,exit_code:1,stderr:'<img src=x onerror="window.__evidenceExecuted=true"> original baseline failure'}];
      data.result.evaluation.checks.acceptance_candidate=[{name:'Browser acceptance evidence',passed:true,exit_code:0,stdout:'fixture acceptance passed'}];
      await route.fulfill({ json: data });
    });
    await page.route('**/api/release-status?*', route => route.fulfill({json:{deploy:{state:'failed',workflow_run_id:42,error:'connection'}}}));
    await page.locator("#detail-dialog .close-dialog").click();
    runRequests = 0;
    await page
      .getByRole("button", { name: "Fix addition", exact: true })
      .click();
    await page.locator("#live-task-outcome").waitFor();
    assert.equal(runRequests, 1, "collapsed history must not fetch full runs");
    assert.equal(await page.locator('.evidence-table').count(),1);
    const acceptanceCard=page.locator('.check-result').filter({hasText:'original baseline failure'});
    await acceptanceCard.locator('summary').click();
    assert.match(await acceptanceCard.locator('pre').innerText(),/<img src=x/);
    assert.equal(await acceptanceCard.locator('img').count(),0);
    assert.equal(await page.evaluate(()=>window.__evidenceExecuted),undefined);
    await page.locator('[data-run="old-run-1"] > summary').click();
    const historicalRaw = page.locator('[data-run="old-run-1"] details').filter({
      has: page.getByText('原始运行记录', {exact: true}),
    });
    await historicalRaw.locator(':scope > summary').waitFor();
    assert.equal(runRequests, 2);
    assert.equal(await historicalRaw.locator('pre').count(), 0, 'collapsed raw evidence stays lazy');
    await historicalRaw.locator(':scope > summary').click();
    await historicalRaw.locator('pre').filter({hasText: 'original baseline failure'}).waitFor();
    assert.match(await historicalRaw.locator('pre').innerText(), /<img src=x/);
    assert.equal(await historicalRaw.locator('img').count(), 0);
    assert.equal(await page.evaluate(()=>window.__evidenceExecuted), undefined);
    assert.equal(runRequests, 2, 'opening loaded raw evidence must not fetch the run again');
    const timeline = page.locator('#live-task-outcome details').filter({has: page.getByText('阶段耗时', {exact: true})});
    await timeline.locator('summary').click();
    liveSummary = 'Live checkpoint received';
    await page.locator('#live-task-outcome').filter({hasText: liveSummary}).waitFor();
    assert.equal(await timeline.getAttribute('open'), '', 'same-state updates preserve expanded run details');
    assert.equal(await page.locator('[data-run="old-run-1"]').getAttribute('open'), '');
    await page.locator('#live-task-outcome .release-outcome').filter({hasText:'部署'}).waitFor();
    const phaseText = await page.locator('#live-task-outcome details').filter({has:page.getByText('阶段耗时',{exact:true})}).textContent();
    await page.waitForFunction(previous => [...document.querySelectorAll('#live-task-outcome details')].find(n=>n.querySelector('summary')?.textContent==='阶段耗时')?.textContent !== previous, phaseText);
    await page.locator('#live-task-outcome .release-outcome').filter({hasText:'部署'}).waitFor();
    const deepUrl = page.url();
    const deep = await context.newPage();
    await deep.goto(deepUrl);
    await deep.locator('#detail-dialog[open] #detail-title').filter({hasText:'Fix addition'}).waitFor();
    await deep.close();
    await page.locator("#detail-dialog .close-dialog").click();
    await page.locator("#storage-tools").click();
    await page
      .getByRole("button", { name: "创建一致性备份", exact: true })
      .click();
    await page.locator(".experience-dialog a[download]").first().waitFor();
    await page.getByText("诊断导出预览", { exact: true }).click();
    assert.ok(
      !(await page.locator(".experience-dialog").innerText()).includes(
        "DO_NOT_EXPORT",
      ),
    );
    await page
      .locator(".experience-dialog")
      .getByRole("button", { name: "关闭", exact: true })
      .click();
    let notificationState = 'merged', feedbackHead = 'head-1';
    await page.route('**/api/attention', async route => {
      const response = await route.fetch(), data = await response.json();
      data.notifications = [{id:'notification-test',project:ids[2],title:'Notification fixture',state:'merged',display_state:notificationState,run:'notification-run',result:JSON.stringify({feedback:{state:notificationState==='regressed'?'regressed':'verified',head:feedbackHead}})}];
      await route.fulfill({json:data});
    });
    await page.evaluate(() => {
      window.__notifications = [];
      window.Notification = class { static permission = 'granted'; constructor(title, options) { window.__notifications.push({title,...options}); } close() {} };
      localStorage.setItem('rsi-notifications:'+location.origin,'true');
      const now = Date.now.bind(Date); window.__timeOffset = 0; Date.now = () => now() + window.__timeOffset;
    });
    async function pollNotifications(expected) {
      const response = page.waitForResponse(r => new URL(r.url()).pathname === '/api/attention');
      await page.evaluate(() => {window.__timeOffset += 20000; RSIExperience.refreshed();});
      await response;
      await page.waitForFunction(n => window.__notifications.length === n, expected);
    }
    await pollNotifications(1);
    await pollNotifications(1);
    notificationState = 'regressed';
    await pollNotifications(2);
    await pollNotifications(2);
    notificationState = 'merged';feedbackHead = 'head-2';
    await pollNotifications(3);
    notificationState = 'regressed';
    await pollNotifications(4);
    // Structured checks save exact argv (including spaces), keep unrelated config,
    // survive a reload, and never start a model task.
    await page.locator('#project-settings').click();
    const beforeConfig=await page.evaluate(()=>structuredClone(currentProject().config));
    await page.locator('#checks-add').click();
    const editor=page.locator('#checks-editor fieldset').last();
    await editor.locator('[data-check-field="name"]').fill('UI argv regression');
    await editor.locator('[data-check-field="cwd"]').fill('tests');
    await editor.locator('[data-check-arg]').fill('python3');
    await editor.getByRole('button',{name:'添加参数',exact:true}).click();
    await editor.locator('[data-check-arg]').nth(1).fill('-c');
    await editor.getByRole('button',{name:'添加参数',exact:true}).click();
    await editor.locator('[data-check-arg]').nth(2).fill('print("argument with spaces")\nprint(2)');
    await editor.locator('[data-check-field="name"]').fill('UI argv regression renamed');
    await page.locator('#save-note').filter({hasText:'未保存'}).waitFor();
    const persisted=page.waitForResponse(response=>response.url().endsWith('/api/config')&&response.request().method()==='POST');
    await page.locator('#settings-save').click();
    assert.equal((await persisted).status(),200);
    await page.locator('#save-note[data-state="saved"]').filter({hasText:'设置已保存'}).waitFor();
    const savedConfig=await page.evaluate(()=>currentProject().config);
    assert.deepEqual(savedConfig.checks.at(-1),{name:'UI argv regression renamed',cwd:'tests',argv:['python3','-c','print("argument with spaces")\nprint(2)']});
    assert.deepEqual(savedConfig.limits,beforeConfig.limits);
    assert.deepEqual(savedConfig.evolution,beforeConfig.evolution);
    assert.equal(await page.locator('#advanced-json:visible, #checks-json:visible').count(),0);
    assert.equal(await page.evaluate(()=>currentProject().paused),1);
    await page.locator('#language-select').selectOption('zh-CN');
    await page.reload();
    await page.waitForFunction(()=>state&&currentProject()?.paused);
    await page.locator('#view-settings').waitFor();
    assert.equal(await page.locator('#checks-editor [data-check-field="name"]').last().inputValue(),'UI argv regression renamed');
    assert.deepEqual(await page.locator('#checks-editor fieldset').last().locator('[data-check-arg]').evaluateAll(nodes=>nodes.map(node=>node.value)),['python3','-c','print("argument with spaces")\nprint(2)']);
    // Acknowledging weight=2 must keep a newer weight=1 draft visibly dirty.
    await page.locator('#advanced-settings > summary').click();
    const weight=page.locator('[data-path="schedule.weight"]');
    await weight.fill('2');
    let releaseSave,saveArrived;
    const arrived=new Promise(resolve=>saveArrived=resolve),gate=new Promise(resolve=>releaseSave=resolve);
    const delayedSave=async route=>{const response=await route.fetch();saveArrived();await gate;await route.fulfill({response});};
    await page.route('**/api/config',delayedSave);
    await page.locator('#settings-save').click();await arrived;
    await weight.fill('1');releaseSave();
    await page.locator('#settings-save:not([disabled])').waitFor();
    await page.locator('#save-note[data-state="dirty"]').waitFor();
    assert.equal(await weight.inputValue(),'1');
    assert.equal(await page.evaluate(()=>settingsBaseline.schedule.weight),2);
    await page.unroute('**/api/config',delayedSave);
    await page.locator('#settings-save').click();
    await page.locator('#save-note[data-state="saved"]').waitFor();
    // A name edit never changes the multiline argument restored from the server.
    assert.deepEqual(await page.evaluate(()=>JSON.parse(document.getElementById('checks-json').value).at(-1).argv),['python3','-c','print("argument with spaces")\nprint(2)']);
    for(const saved of [false,true])for(const live of [false,true]){
      const copy=await page.evaluate(({saved,live})=>{const prior=currentProject().config.delivery.auto_merge;currentProject().config.delivery.auto_merge=live;const section=RSIProduct.approval({config:{...structuredClone(currentProject().config),delivery:{...structuredClone(currentProject().config.delivery),auto_merge:saved}}});currentProject().config.delivery.auto_merge=prior;return section.textContent;},{saved,live});
      assert.ok(copy.includes(saved&&live?'本次与当前均已授权':'本次与当前未同时授权'));
      assert.ok(!copy.includes('批准仅用于'));
    }
    await page.locator("#language-select").selectOption("en");
    await page.locator('#settings-effect').filter({hasText:'No restart required'}).waitFor();
    assert.equal(await page.locator('#checks-add').innerText(),'Add check');
    await page.locator('#checks-editor [data-check-field="name"]').last().fill('Project A unsaved');
    await page.locator('#save-note[data-state="dirty"]').waitFor();
    const projects=await page.locator('#project-select option').evaluateAll(nodes=>nodes.map(node=>node.value));
    await page.locator('#project-select').selectOption(projects.find(id=>id!==ids[2]));
    await page.locator('#save-note[data-state="saved"]').waitFor();
    assert.doesNotMatch(await page.locator('#save-note').innerText(),/Unsaved/);
    await page.locator('#project-select').selectOption(ids[2]);
    await page.locator('#save-note[data-state="dirty"]').waitFor();
    assert.equal(await page.locator('#checks-editor [data-check-field="name"]').last().inputValue(),'Project A unsaved');
    await page.emulateMedia({colorScheme:'dark'});
    await page.waitForFunction(()=>{
      const picker=document.querySelector('.mc-route-picker');
      const surface=getComputedStyle(document.documentElement).getPropertyValue('--surface').trim();
      return picker&&surface==='#1e2620'&&getComputedStyle(picker).backgroundColor==='rgb(30, 38, 32)';
    });
    await page.locator('#mc-add-provider').click();
    await page.locator('#mc-provider-preset').selectOption('opencode_go');
    assert.equal(await page.locator('#mc-input-name').inputValue(),'OpenCode Go');
    assert.equal(await page.locator('#mc-input-url').inputValue(),'');
    await page.locator('#mc-go-hint').filter({hasText:'official OpenCode console'}).waitFor();
    await page.locator('#mc-provider-dialog .close-dialog').click();
    assert.equal(
      await page.locator("#mobile-install").innerText(),
      "Install on phone",
    );
    assert.equal(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
      true,
    );
    await page.evaluate(([task, project]) => {
      window.RSIDesktop = {};
      window.dispatchEvent(new CustomEvent('rsi-notification-open',{detail:{task,project}}));
    }, [ids[0],ids[2]]);
    await page.locator('#detail-dialog[open] #detail-title').filter({hasText:'手机测试目标'}).waitFor();
    // 流程质量与耗时：按钮文案与阶段名跟随界面语言往返切换。
    const qualityRoute = async route => route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({
      tasks: 2, accepted: 1, delivered: 1, first_pass: 1, infrastructure_failures: 0, repeated_failures: 0,
      requirements_revised: 0, stage_reused: 0, interventions: 0, stale_rework: 0, queue_seconds: 10,
      phase_seconds: {preparation: 5, triage: 600, evolution: 300}, model_seconds: 30,
      remote_wait_seconds: {}, invalidations: {}, events: {}, elapsed_samples: 1, elapsed_seconds: 600,
      accepted_tokens: 100, accepted_unknown_calls: 0, delivered_tokens: 100, delivered_unknown_calls: 0,
      to_pr_samples: 1, to_pr_seconds: 60, to_merge_samples: 1, to_merge_seconds: 120})});
    await page.route('**/api/workflow-metrics*', qualityRoute);
    try {
      await page.locator('#detail-dialog .close-dialog').click();
      await page.locator('[data-view="usage"]').click();
      await page.locator('#usage-totals strong').first().waitFor();
      await page.locator('#language-select').selectOption('zh-CN');
      await page.getByRole('button', {name: '流程质量与耗时', exact: true}).waitFor();
      await page.getByRole('button', {name: '流程质量与耗时', exact: true}).click();
      const qualityDialog = page.locator('dialog[open].experience-dialog');
      await qualityDialog.getByText('价值初筛 · 600 秒').waitFor();
      await qualityDialog.getByText('策略改进 · 300 秒').waitFor();
      assert.equal(await qualityDialog.innerText().then(text => /triage ·|evolution ·/.test(text)), false,
        'phase names must not fall back to raw identifiers');
      await qualityDialog.getByRole('button', {name: '关闭', exact: true}).click();
      await page.locator('#language-select').selectOption('en');
      await page.getByRole('button', {name: 'Workflow quality and timing', exact: true}).waitFor();
      await page.getByRole('button', {name: 'Workflow quality and timing', exact: true}).click();
      const englishDialog = page.locator('dialog[open].experience-dialog');
      await englishDialog.getByText('Value screening · 600 seconds').waitFor();
      await englishDialog.getByText('Strategy improvement · 300 seconds').waitFor();
      assert.equal(await englishDialog.innerText().then(text => /triage ·|evolution ·/.test(text)), false,
        'phase names must not fall back to raw identifiers');
      await englishDialog.getByRole('button', {name: 'Close', exact: true}).click();
      await page.locator('#language-select').selectOption('zh-CN');
      await page.getByRole('button', {name: '流程质量与耗时', exact: true}).waitFor();
    } finally {
      await page.unroute('**/api/workflow-metrics*', qualityRoute);
    }
    assert.deepEqual(errors, []);
    console.log(
      "Mobile fresh navigation, install assets, idempotency, revisions, lazy history, evidence, diff and backup passed.",
    );
  } finally {
    if (browser) await browser.close();
    if (server.exitCode === null && server.signalCode === null) {
      const ended = once(server, 'exit');
      server.stdin.end("\n");
      const timeout = setTimeout(() => server.kill('SIGTERM'), 5000);
      await ended;
      clearTimeout(timeout);
    }
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
