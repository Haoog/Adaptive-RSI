const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const {createInterface} = require('node:readline');
const {chromium} = require('playwright');
const fs = require('node:fs');

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
    const page = await browser.newPage({viewport: {width: 1380, height: 1000}});
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(url);
    await page.locator('#readiness-summary').filter({hasText: '基础条件已就绪'}).waitFor();
    assert.equal(await page.locator('#project-status').innerText(), '已暂停');
    await page.locator('#goal-text').fill('验证保留用户输入');
    await page.locator('#readiness-check').click();
    await page.waitForTimeout(5500);
    assert.equal(await page.locator('#goal-text').inputValue(), '验证保留用户输入');
    await page.locator('[data-view="settings"]').first().click();
    await page.locator('#delivery-choice').selectOption('auto');
    await page.locator('#settings-save').click();
    await page.locator('#toast').filter({hasText: '已保存'}).waitFor();
    assert.equal(await page.locator('#delivery-choice').inputValue(), 'auto');
    await page.locator('[data-view="overview"]').first().click();
    await page.locator('#goal-form button[type="submit"]').click();
    await page.locator('#toast').filter({hasText: '目标已入队'}).waitFor();
    await page.locator('[data-view="tasks"]').first().click();
    await page.locator('#tasks-page').filter({hasText: '56'}).waitFor().catch(async error=>{console.error('Task page:',await page.locator('#tasks-page').innerText());console.error('Counts:',await page.evaluate(async()=>{const s=await(await fetch('/api/state')).json();return s.task_counts;}));throw error;});
    await page.locator('[data-task-filter="attention"]').click();
    await page.locator('#tasks-list .task-title').filter({hasText: 'Fix addition'}).click();
    await page.locator('#detail-dialog[open] .stage-progress .done').first().waitFor();
    assert.equal(await page.locator('#detail-dialog .stage-progress .done').count(), 5);
    await page.locator('#detail-dialog summary').filter({hasText: '运行与效果检查'}).click();
    await page.locator('#detail-dialog .close-dialog').click();
    await page.locator('[data-task-filter="active"]').click();
    await page.locator('#tasks-page').filter({hasText: '56'}).waitFor();
    await page.locator('#tasks-next').click();
    await page.locator('#tasks-page').filter({hasText: '51'}).waitFor();
    assert.equal(await page.locator('#tasks-list .task-row').count(), 6);
    await page.locator('#project-select').selectOption({label: '另一个独立项目'});
    await page.locator('#tasks-list').filter({hasText: '另一个项目专属任务'}).waitFor();
    assert.equal(await page.locator('#tasks-list .task-row').count(), 1);
    await page.locator('#language-select').selectOption('en');
    await page.locator('#view-tasks h1').filter({hasText: 'Tasks'}).waitFor();
    await page.setViewportSize({width: 390, height: 844});
    await page.locator('[data-view="settings"]').first().click();
    await page.locator('#delivery-choice').selectOption('local');
    await page.locator('#settings-save').click();
    await page.locator('#toast').filter({hasText: 'Saved'}).waitFor();
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
    if (process.env.RSI_SCREENSHOT_DIR) {
      fs.mkdirSync(process.env.RSI_SCREENSHOT_DIR, {recursive: true});
      await page.screenshot({path: process.env.RSI_SCREENSHOT_DIR + '/mobile-settings.png', fullPage: true});
      await page.setViewportSize({width: 1380, height: 1000});
      await page.locator('#language-select').selectOption('zh-CN');
      await page.locator('[data-view="overview"]').first().click();
      await page.screenshot({path: process.env.RSI_SCREENSHOT_DIR + '/desktop-workspace.png', fullPage: true});
    }
    assert.deepEqual(errors, []);
    const state = await page.evaluate(async () => (await fetch('/api/state')).json());
    assert(state.projects.every(project => project.paused));
    assert.equal(state.usage.length, 0);
    console.log('Browser checks passed: forms, hot settings, pagination, project isolation, languages, mobile, zero paid calls.');
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
