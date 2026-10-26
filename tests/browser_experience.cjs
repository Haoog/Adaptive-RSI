const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const { createInterface } = require("node:readline");
const { chromium } = require("playwright");
(async () => {
  const server = spawn(
    process.env.PYTHON || "python3",
    ["-m", "tests.browser_fixture"],
    { stdio: ["pipe", "pipe", "pipe"] },
  );
  server.stderr.on("data", (chunk) => process.stderr.write(chunk));
  let browser;
  try {
    const url = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(Error("Fixture timed out")), 30000);
      createInterface({ input: server.stdout }).once("line", (line) => {
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
    const ids = await page.evaluate(async () => {
      const project = projectId;
      const first = await api("goal", {
        project,
        text: "手机测试目标",
        request_id: "mobile-browser-request",
      });
      const second = await api("goal", {
        project,
        text: "手机测试目标",
        request_id: "mobile-browser-request",
      });
      return [first.id, second.id, project];
    });
    assert.equal(ids[0], ids[1]);
    await page.evaluate(() => refresh());
    await page
      .getByRole("button", { name: "手机测试目标", exact: true })
      .click();
    await page.locator(".task-controls textarea").fill("增加手机验收要求");
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
    await page.waitForFunction(() =>
      document.querySelector('[data-run="old-run-1"] pre'),
    );
    assert.equal(runRequests, 2);
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
    await page.locator('#project-status').filter({hasText:'已暂停'}).waitFor();
    await page.locator('#project-settings').click();
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
    assert.notEqual(await page.locator('.mc-route-picker').first().evaluate(node=>getComputedStyle(node).backgroundColor),'rgb(255, 255, 255)');
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
    assert.deepEqual(errors, []);
    console.log(
      "Mobile fresh navigation, install assets, idempotency, revisions, lazy history, evidence, diff and backup passed.",
    );
  } finally {
    if (browser) await browser.close();
    server.stdin.end("\n");
    await new Promise((resolve) => server.once("exit", resolve));
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
