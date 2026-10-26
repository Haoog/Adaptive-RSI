const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const {createInterface} = require('node:readline');
const {chromium} = require('playwright');

// 交付授权错误反馈回归：服务端校验错误给出可操作中文提示，工作流列表失败后可恢复。
const UPPER_SHA = 'A'.repeat(40), LOWER_SHA = 'a'.repeat(40);
const ORIGIN_ERROR = "Workflow repository must match the selected project's origin";
const reply = (route, data, status = 200) => route.fulfill({status, contentType: 'application/json', body: JSON.stringify(data)});

async function openSettings(page) {
  await page.locator('#project-settings').click();
  await page.locator('#release-settings').scrollIntoViewIfNeeded();
  await page.locator('#release-project-fields').waitFor({state: 'visible'});
}

async function main() {
  const server = spawn(process.env.PYTHON || 'python3', ['-m', 'tests.browser_fixture'], {
    stdio: ['pipe', 'pipe', 'pipe'], env: {...process.env, PYTHONDONTWRITEBYTECODE: '1'}
  });
  let browser;
  const errors = [];
  server.stderr.on('data', chunk => process.stderr.write(chunk));
  try {
    const url = await new Promise((resolve, reject) => {
      const timeout = setTimeout(() => reject(Error('Console startup timed out')), 30000);
      createInterface({input: server.stdout}).once('line', line => {clearTimeout(timeout); resolve(line);});
      server.once('exit', code => {clearTimeout(timeout); reject(Error('Console exited: ' + code));});
    });
    browser = await chromium.launch(process.env.RSI_BROWSER_CHANNEL ? {channel: process.env.RSI_BROWSER_CHANNEL} : {});

    // S1 P2-2b：真实端点在本 fixture 上必然 origin 不一致，应显示可操作的中文提示，
    // 下拉框用可重载的错误项替换加载占位，并可通过现有刷新按钮重试。
    {
      const page = await browser.newPage({viewport: {width: 1380, height: 1000}});
      page.on('pageerror', error => errors.push(error.message));
      await page.goto(url);
      await page.locator('#project-status').filter({hasText: '已暂停'}).waitFor();
      await openSettings(page);
      await page.locator('#release-authorization-feedback').filter({hasText: 'origin 不一致'}).waitFor({timeout: 15000});
      const options = await page.locator('#release-workflow-list option').allInnerTexts();
      assert.ok(options.some(text => text.includes('读取失败')), JSON.stringify(options));
      assert.ok(!options.some(text => text.includes('正在读取工作流')), JSON.stringify(options));
      await page.locator('#release-workflows-refresh').click();
      await page.locator('#release-workflow-list option').filter({hasText: '读取失败'}).first().waitFor({state: 'attached'});
      await page.locator('#release-authorization-feedback').filter({hasText: 'origin 不一致'}).waitFor();
      await page.close();
    }
    console.log('ok: real origin mismatch shows actionable feedback and a reloadable error item');

    // S2 P2-2a：列表请求失败后不得残留「正在读取工作流…」；重试成功后恢复正常列表。
    {
      const page = await browser.newPage({viewport: {width: 1380, height: 1000}});
      page.on('pageerror', error => errors.push(error.message));
      let failing = true, calls = 0;
      await page.route('**/api/release-workflows?*', route => {
        calls++;
        if (failing) return reply(route, {error: ORIGIN_ERROR, error_code: 'validation_error'}, 400);
        return reply(route, {repository: 'example-owner/example-repo', sha: LOWER_SHA,
          workflows: [{workflow: '.github/workflows/release.yml', name: 'Release', compatible: true}]});
      });
      await page.goto(url);
      await page.locator('#project-status').filter({hasText: '已暂停'}).waitFor();
      await openSettings(page);
      await page.locator('#release-authorization-feedback').filter({hasText: 'origin 不一致'}).waitFor({timeout: 15000});
      let options = await page.locator('#release-workflow-list option').allInnerTexts();
      assert.ok(options.some(text => text.includes('读取失败')), JSON.stringify(options));
      assert.ok(!options.some(text => text.includes('正在读取工作流')), JSON.stringify(options));
      failing = false;
      await page.locator('#release-workflows-refresh').click();
      await page.waitForFunction(() => [...document.querySelectorAll('#release-workflow-list option')].some(option => option.textContent.includes('Release')), null, {timeout: 10000});
      assert.ok(calls >= 2, 'refresh must re-request the endpoint');
      assert.equal(await page.locator('#release-authorization-feedback').filter({hasText: 'origin 不一致'}).count(), 0,
        'stale failure feedback must clear after a successful reload');
      await page.close();
    }
    console.log('ok: failed listing shows error item and refresh recovers');

    // S3 P2-1a：三类服务端校验错误都必须显示可操作的中文指引（经高级 JSON 直达服务端）。
    {
      const page = await browser.newPage({viewport: {width: 1380, height: 1000}});
      page.on('pageerror', error => errors.push(error.message));
      await page.goto(url);
      await page.locator('#project-status').filter({hasText: '已暂停'}).waitFor();
      await openSettings(page);
      const feedback = page.locator('#settings-form [data-form-error]');
      await page.locator('#advanced-settings > summary').click();
      const save = async json => {
        // #advanced-json 是隐藏 textarea；直接赋值并派发 input，走与其他字段一致的草稿记录路径。
        await page.locator('#advanced-json').evaluate((node, text) => {
          node.value = text;
          node.dispatchEvent(new Event('input', {bubbles: true}));
        }, json);
        await page.locator('#settings-save').click();
      };
      await save('{"delivery":{"deploy":{"automatic_rollback":true}}}');
      await feedback.filter({hasText: '自动回滚需要'}).waitFor({timeout: 10000});
      await save('{"delivery":{"deploy":{"automatic_rollback":false,"health_url":"http://example.com/health"}}}');
      await feedback.filter({hasText: 'https:// 开头'}).waitFor({timeout: 10000});
      await save('{"delivery":{"deploy":{"health_url":""},"release":{"sha":"' + UPPER_SHA + '"}}}');
      await feedback.filter({hasText: '小写'}).waitFor({timeout: 10000});
      await page.close();
    }
    console.log('ok: delivery validation errors show actionable Chinese guidance');

    // S4 P2-1b：大写 SHA 与 http:// 健康地址被前端拦截，输入不被静默改写；小写 SHA 正常保存。
    {
      const page = await browser.newPage({viewport: {width: 1380, height: 1000}});
      page.on('pageerror', error => errors.push(error.message));
      const configPosts = [];
      page.on('request', request => {if (request.url().endsWith('/api/config') && request.method() === 'POST') configPosts.push(request.postDataJSON());});
      await page.goto(url);
      await page.locator('#project-status').filter({hasText: '已暂停'}).waitFor();
      await openSettings(page);
      await page.locator('#release-workflow-settings summary').click();
      const sha = page.locator('[data-path="delivery.release.sha"]');
      await sha.fill(UPPER_SHA);
      await page.locator('#settings-save').click();
      await page.waitForTimeout(800);
      assert.deepEqual(configPosts, [], 'uppercase SHA must not reach the server');
      assert.equal(await sha.inputValue(), UPPER_SHA, 'user input must not be silently rewritten');
      assert.ok(await sha.evaluate(node => node.validity.patternMismatch), 'client validation must reject uppercase');
      assert.ok((await sha.getAttribute('title') || '').includes('小写'), 'hint must require lowercase');
      await sha.fill(LOWER_SHA);
      await page.locator('#settings-save').click();
      await page.locator('#toast').filter({hasText: '已保存'}).waitFor();
      assert.equal(configPosts.length, 1, 'lowercase SHA saves normally');
      await page.locator('[data-path="delivery.deploy.sha"]').fill(LOWER_SHA);
      await page.locator('[data-path="delivery.deploy.enabled"]').click();
      const health = page.locator('[data-path="delivery.deploy.health_url"]');
      await health.waitFor({state: 'visible'});
      await health.fill('http://example.com/health');
      assert.ok(await health.evaluate(node => node.validity.patternMismatch), 'plain HTTP health URL must be blocked client-side');
      await health.fill('https://example.com/health');
      assert.ok(await health.evaluate(node => node.checkValidity()), 'HTTPS health URL must pass client validation');
      await page.locator('[data-path="delivery.deploy.enabled"]').click();
      // 英文界面下新的 title 提示也必须翻译。
      await page.locator('#language-select').selectOption('en');
      assert.equal(await sha.getAttribute('title'), 'Full 40-character commit SHA, lowercase a-f and digits only');
      assert.equal(await health.getAttribute('title'), 'Must start with https:// and contain no credentials, ? query or # fragment');
      await page.close();
    }
    console.log('ok: uppercase SHA and plain-HTTP health URL are blocked client-side without silent edits');

    assert.deepEqual(errors, [], 'no page errors: ' + JSON.stringify(errors));
    console.log('Delivery feedback browser checks passed.');
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
