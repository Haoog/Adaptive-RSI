const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const path = require('node:path');
const {chromium} = require('playwright');
const {fixtureReady, closeFixture, waitForPausedProject} = require('./fixture_browser.cjs');

function startServer() {
  const server = spawn(process.env.PYTHON || 'python3', ['-m', 'tests.browser_fixture'], {
    stdio: ['pipe', 'pipe', 'pipe'],
    env: {...process.env, PYTHONDONTWRITEBYTECODE: '1', RSI_BROWSER_PROJECT_IDENTITY: '1'},
  });
  server.stderr.on('data', chunk => process.stderr.write(chunk));
  return server;
}

async function addLocalProject(page, repoPath, name, enabled) {
  const response = page.waitForResponse(item =>
    new URL(item.url()).pathname === '/api/projects' && item.request().method() === 'POST');
  await page.locator('#add-project').click();
  await page.locator('#project-source').selectOption('local');
  await page.locator('#project-path').fill(repoPath);
  await page.locator('#project-name').fill(name);
  if (enabled) await page.locator('#project-enabled').check();
  await page.locator('#project-submit').click();
  return (await response).json();
}

(async () => {
  const server = startServer();
  let browser, scenarioError;
  try {
    const url = await fixtureReady(server);
    browser = await chromium.launch(process.env.RSI_BROWSER_CHANNEL
      ? {channel: process.env.RSI_BROWSER_CHANNEL} : {});
    const page = await browser.newPage({viewport: {width: 1380, height: 1000}});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(url);
    await waitForPausedProject(page);

    const state = await page.evaluate(async () => (await fetch('/api/state')).json());
    const sameName = state.projects.filter(project => project.name === '同名仓库');
    assert.equal(sameName.length, 2, 'fixture exposes two same-name repositories');
    const options = await page.locator('#project-select option').evaluateAll(nodes =>
      nodes.filter(node => node.value).map(node => ({value: node.value, text: node.textContent, title: node.title})));
    for (const project of sameName) {
      const option = options.find(item => item.value === project.id);
      assert.match(option.text, /^同名仓库 — /, 'name stays readable');
      assert.ok(option.text.includes(project.path), 'dropdown shows the full path to distinguish repositories');
      assert.equal(option.title, project.path);
    }
    assert.notEqual(options.find(item => item.value === sameName[0].id).text,
      options.find(item => item.value === sameName[1].id).text);

    await page.locator('#project-select').selectOption(sameName[1].id);
    await page.waitForFunction(id => document.getElementById('project-select').value === id, sameName[1].id);
    assert.equal(await page.locator('#workspace-title').innerText(), '同名仓库');
    assert.equal(await page.locator('#workspace-path').innerText(), sameName[1].path);
    assert.equal(await page.locator('#workspace-path').isVisible(), true);

    // 重复接入同一路径：返回真实状态，不静默改名或启用。
    const duplicate = await addLocalProject(page, sameName[0].path, '试图改名', true);
    assert.equal(duplicate.existing, true);
    assert.equal(duplicate.id, sameName[0].id);
    assert.equal(duplicate.name, '同名仓库');
    await page.locator('#toast').filter({hasText: '该路径已接入'}).waitFor();
    const duplicateMessage = await page.locator('#toast').innerText();
    assert.match(duplicateMessage, /该路径已接入：/);
    assert.match(duplicateMessage, /未修改名称或启用状态/);
    assert.equal(await page.locator('#project-select').inputValue(), sameName[0].id);
    assert.equal(await page.evaluate(() => localStorage.getItem('rsi-project')), sameName[0].id);
    const unchanged = (await page.evaluate(async () => (await fetch('/api/state')).json()))
      .projects.find(project => project.id === sameName[0].id);
    assert.equal(unchanged.name, '同名仓库');
    assert.equal(unchanged.paused, 1);

    // 新增成功：纯空格名称回退到目录名，选择立即持久化并在刷新后保留。
    const newPath = path.join(path.dirname(sameName[0].path), 'identity-new');
    const added = await addLocalProject(page, newPath, '   ', false);
    assert.equal(added.existing, false);
    assert.equal(added.name, 'identity-new');
    assert.equal(added.paused, true);
    await page.locator('#toast').filter({hasText: '项目已接入'}).waitFor();
    await page.waitForFunction(id => document.getElementById('project-select').value === id, added.id);
    assert.equal(await page.evaluate(() => localStorage.getItem('rsi-project')), added.id);
    await page.reload();
    await waitForPausedProject(page);
    assert.equal(await page.locator('#project-select').inputValue(), added.id, 'reload keeps the newly added project');
    assert.equal(await page.evaluate(() => localStorage.getItem('rsi-project')), added.id);
    assert.equal(await page.locator('#workspace-title').innerText(), 'identity-new');
    assert.equal(await page.locator('#workspace-path').innerText(), newPath);

    // 英文提示同样准确，不能复用“项目已接入”。
    await page.locator('#language-select').selectOption('en');
    const duplicateEnglish = await addLocalProject(page, sameName[0].path, 'Renamed attempt', true);
    assert.equal(duplicateEnglish.existing, true);
    await page.locator('#toast').filter({hasText: 'This path is already registered'}).waitFor();
    assert.match(await page.locator('#toast').innerText(), /already registered: /);
    assert.match(await page.locator('#toast').innerText(), /Name and enabled state were not changed/);

    assert.deepEqual(errors, []);
    console.log('Project identity browser passed: same-name paths distinguished, blank names normalized, duplicate registration reported accurately, new selection persisted across reload, bilingual copy, zero model calls.');
  } catch (error) {
    scenarioError = error;
    throw error;
  } finally {
    await closeFixture(server, browser, scenarioError);
  }
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
