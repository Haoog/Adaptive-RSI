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
    assert.equal(manifest.icons.length, 2);
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
    await page.locator("#language-select").selectOption("en");
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
