const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const {createInterface} = require('node:readline');
const {chromium} = require('playwright');

// 模型中心专项：移除连接确认（取消零请求）、provider_in_use 文案、连接步骤第 2 步状态。
async function main() {
  const server = spawn(process.env.PYTHON || 'python3', ['-m', 'tests.model_center_browser_fixture'], {
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
    const page = await browser.newPage({viewport: {width: 1380, height: 1000}});
    page.on('pageerror', error => errors.push(error.message));
    const removes = [];
    let posts = 0;
    page.on('request', request => {
      if (request.method() !== 'POST') return;
      posts++;
      if (request.url().endsWith('/api/model-center/remove')) removes.push(request.postDataJSON());
    });
    const step = id => page.locator('#' + id);
    const dialog = page.locator('.confirm-dialog');
    const pick = name => page.locator('#mc-provider-list .mc-provider').filter({hasText: name}).click();

    await page.goto(url);
    await page.locator('#project-status').filter({hasText: '已暂停'}).waitFor();
    await page.locator('#project-settings').click();
    await page.locator('[data-mc-tab="providers"]').click();

    // OAuth（Codex 登录）未连接时第 2 步保持 current。
    await pick('Codex 登录');
    assert.equal(await step('mc-step-account').getAttribute('data-state'), 'current');
    // 本地无需密钥连接：凭据行显示无需密钥，第 2 步不再是永远高亮的 current。
    await pick('本地目录服务');
    assert.equal(await page.locator('#mc-credential-status').innerText(), '无需密钥');
    assert.equal(await step('mc-step-account').getAttribute('data-state'), 'done');
    assert.equal(await step('mc-step-models').getAttribute('data-state'), 'done');
    await page.locator('#mc-model-list .model-more').first().locator('summary').click();
    await page.locator('#mc-model-list').getByRole('button', {name: '估算价格', exact: true}).first().click();
    await page.getByLabel('输入价格 / USD').fill('1.2');
    await page.getByLabel('输出价格 / USD').fill('3.4');
    const priced = page.waitForResponse(response => response.url().endsWith('/api/model-center/model') && response.request().method() === 'POST');
    await page.locator('#mc-price-form button[type=submit]').click();
    const priceResponse = await priced;
    assert.equal(priceResponse.status(), 200);
    const pricePayload = priceResponse.request().postDataJSON().model;
    assert.deepEqual(pricePayload.prices, {input:1.2,output:3.4}, 'only edited price fields are submitted');
    assert.equal(Object.hasOwn(pricePayload, 'unit_price'), false, 'an untouched blended price must not overwrite the server value');
    await page.locator('#mc-price-dialog').waitFor({state:'hidden'});
    // —— O04：两个窗口各自只提交自己改过的价格字段，先后保存不能覆盖对方未编辑的字段。
    const other = await browser.newPage({viewport: {width: 1380, height: 1000}});
    other.on('pageerror', error => errors.push(error.message));
    const selectDirectoryProvider = async target => {
      await target.goto(url);
      await target.locator('#project-status').filter({hasText: '已暂停'}).waitFor();
      await target.locator('#project-settings').click();
      await target.locator('[data-mc-tab="providers"]').click();
      await target.locator('#mc-provider-list .mc-provider').filter({hasText: '本地目录服务'}).click();
    };
    const openPriceDialog = async target => {
      const row = target.locator('#mc-model-list .mc-model-row').filter({hasText: 'fixture-pro'});
      await row.locator('summary').click();
      await row.getByRole('button', {name: '估算价格', exact: true}).click();
      await target.locator('#mc-price-dialog').waitFor();
    };
    await selectDirectoryProvider(other);
    await openPriceDialog(page);
    await openPriceDialog(other);
    await page.locator('#mc-price-form').getByLabel('输入价格 / USD').fill('0.7');
    const firstSave = page.waitForResponse(response => response.url().endsWith('/api/model-center/model') && response.request().method() === 'POST');
    await page.locator('#mc-price-form button[type=submit]').click();
    const firstPayload = (await firstSave).request().postDataJSON().model;
    assert.deepEqual(firstPayload.prices, {input:0.7}, 'the first window submits only its own edited field');
    await other.locator('#mc-price-form').getByLabel('输出价格 / USD').fill('1.9');
    const secondSave = other.waitForResponse(response => response.url().endsWith('/api/model-center/model') && response.request().method() === 'POST');
    await other.locator('#mc-price-form button[type=submit]').click();
    const secondPayload = (await secondSave).request().postDataJSON().model;
    assert.deepEqual(secondPayload.prices, {output:1.9}, 'the second window must not resend the other window\'s untouched input price');
    const project = await page.locator('#project-select').inputValue();
    const merged = await page.evaluate(async projectId => {
      const snapshot = await (await fetch('/api/model-center?project=' + encodeURIComponent(projectId))).json();
      const provider = snapshot.providers.find(item => item.name === '本地目录服务');
      return provider.catalog.models.find(model => model.id === 'fixture-pro').prices;
    }, project);
    assert.deepEqual(merged, {input:0.7,output:1.9}, 'both windows keep each other\'s edited price fields');
    await other.close();
    // 文件密钥连接已获取目录：第 2 步完成。
    await pick('引用中转');
    assert.equal(await page.locator('#mc-credential-status').innerText(), '密钥已保存到本机私有文件');
    assert.equal(await step('mc-step-account').getAttribute('data-state'), 'done');
    assert.equal(await step('mc-step-models').getAttribute('data-state'), 'done');
    // 凭据已配置但未获取目录：第 2 步完成，当前步骤推进到第 3 步。
    await pick('未获取中转');
    assert.equal(await step('mc-step-account').getAttribute('data-state'), 'done');
    assert.equal(await step('mc-step-models').getAttribute('data-state'), 'current');

    // OAuth 登录完成后第 2 步仍正确显示为完成。
    // CI 高负载下首次登录请求偶尔未生效，页面停留在登录表单；重试点击直到授权码出现。
    await pick('Codex 登录');
    const authCode = page.locator('.mc-auth-code').filter({hasText: 'PROBE-CODE-01'});
    for (let attempt = 0; attempt < 3; attempt++) {
      await page.getByRole('button', {name: '前往 Codex 官方登录', exact: true}).click();
      try {
        await authCode.waitFor({timeout: 15000});
        break;
      } catch (error) {
        if (attempt === 2) {
          const panel = await page.locator('#mc-auth').innerText().catch(() => '<unreadable>');
          throw Error('auth code missing after retries; panel=' + panel.slice(0, 400));
        }
        await page.waitForTimeout(1000);
      }
    }
    await page.waitForFunction(() => document.getElementById('mc-step-account').dataset.state === 'done', null, {timeout: 20000});

    // 移除连接必须先确认；取消时不发出任何请求。
    await pick('本地目录服务');
    const postsBefore = posts;
    await page.locator('#mc-remove-provider').click();
    await dialog.waitFor();
    assert.match(await dialog.innerText(), /不可恢复/);
    assert.match(await dialog.innerText(), /新连接/);
    assert.equal(removes.length, 0);
    await dialog.getByRole('button', {name: '取消', exact: true}).click();
    await dialog.waitFor({state: 'detached'});
    assert.equal(removes.length, 0);
    assert.equal(posts - postsBefore, 0);
    await page.locator('#mc-provider-list .mc-provider').filter({hasText: '本地目录服务'}).waitFor();

    // 确认文案跟随界面语言。
    await page.locator('#language-select').selectOption('en');
    await page.locator('#mc-remove-provider').click();
    await dialog.waitFor();
    assert.match(await dialog.innerText(), /cannot be undone/i);
    await dialog.getByRole('button', {name: 'Cancel', exact: true}).click();
    await dialog.waitFor({state: 'detached'});
    await page.locator('#language-select').selectOption('zh-CN');

    // 仍被角色引用时确认移除：请求发出、服务端拒绝，连接保留并解释原因。
    await pick('引用中转');
    await page.locator('#mc-remove-provider').click();
    await dialog.waitFor();
    await dialog.getByRole('button', {name: '确认', exact: true}).click();
    await page.waitForFunction(() => document.getElementById('toast').textContent.includes('此连接仍被角色或备用模型使用'));
    assert.equal(removes.length, 1);
    await page.locator('#mc-provider-list .mc-provider').filter({hasText: '引用中转'}).waitFor();

    // 确认后真正移除未引用的连接。
    await pick('会话中转');
    await page.locator('#mc-remove-provider').click();
    await dialog.waitFor();
    const removeResponse = page.waitForResponse(response => response.url().endsWith('/api/model-center/remove') && response.request().method() === 'POST');
    await dialog.getByRole('button', {name: '确认', exact: true}).click();
    assert.equal((await removeResponse).status(), 200);
    await page.waitForFunction(() => document.getElementById('mc-feedback').textContent.includes('连接已移除'));
    assert.equal(await page.locator('#mc-provider-list .mc-provider').filter({hasText: '会话中转'}).count(), 0);
    assert.equal(removes.length, 2);

    assert.deepEqual(errors, []);
    console.log('Model center browser checks passed: remove confirmation, zero-request cancel, provider_in_use copy, account step states.');
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
