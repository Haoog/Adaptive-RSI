const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const {createInterface} = require('node:readline');
const {chromium} = require('playwright');

(async () => {
  const server = spawn(process.env.PYTHON || 'python3', ['-m', 'tests.browser_fixture'],
    {stdio: ['pipe', 'pipe', 'pipe'], env: {...process.env, PYTHONDONTWRITEBYTECODE: '1', RSI_BROWSER_DAG:'1'}});
  let browser;
  try {
    const url = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(Error('Fixture startup timed out')), 30000);
      createInterface({input: server.stdout}).once('line', line => {clearTimeout(timer); resolve(line);});
      server.once('exit', code => {clearTimeout(timer); reject(Error('Fixture exited: ' + code));});
    });
    browser = await chromium.launch(process.env.RSI_BROWSER_CHANNEL ? {channel: process.env.RSI_BROWSER_CHANNEL} : {});
    const page = await browser.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(url);
    await page.locator('#project-status').filter({hasText: '已暂停'}).waitFor();
    await page.getByRole('button', {name:'等待两个前置任务',exact:true}).click();
    const select = page.locator('#detail-body select[multiple]');
    await select.waitFor({state: 'attached'});
    const queue = page.locator('.task-queue-editor');
    assert.equal(await queue.count(), 1, 'queue controls have a separate disclosure');
    assert.equal(await queue.evaluate(node => node.open), false, 'saved queue settings start collapsed');
    assert.equal(await select.isVisible(), false, 'dependencies do not occupy the initial detail');
    const supplement = page.locator('.task-supplement-editor');
    assert.equal(await supplement.evaluate(node => node.open), false, 'empty supplement starts collapsed');
    assert.equal(await supplement.locator('textarea').first().isVisible(), false);
    const blockers = page.locator('#detail-body .task-controls > details').filter({has: page.locator(':scope > summary', {hasText: '为什么还没开始'})});
    await blockers.locator(':scope > summary').click();
    await blockers.getByText('项目已暂停', {exact: true}).waitFor();
    assert.equal(await blockers.evaluate(node => node.open), true, 'blocking causes remain clickable while paused');
    await page.locator('.task-queue-editor > summary').click();
    assert.equal(await select.isVisible(), true);
    const parents = await select.locator('option').evaluateAll(options=>options.filter(option=>['前置甲','前置乙'].includes(option.textContent)).map(option=>option.value));
    assert.equal(parents.length,2);
    await select.selectOption(parents);
    await page.locator('#language-select').selectOption('en');
    await page.waitForFunction(() => document.querySelector('#detail-body select[multiple]')?.selectedOptions.length === 2);
    assert.equal(await queue.evaluate(node => node.open), true, 'unsaved queue edits reopen after language rebuild');
    const saved = page.waitForResponse(response=>new URL(response.url()).pathname==='/api/task/schedule'&&response.request().method()==='POST');
    await page.getByRole('button',{name:'Save queue settings',exact:true}).click();
    assert.equal((await saved).status(),200);
    await page.locator('#language-select').selectOption('zh-CN');
    await page.waitForFunction(() => document.querySelector('#detail-body select[multiple]')?.selectedOptions.length === 2);
    const persisted = await page.evaluate(async()=> (await api('task?id='+detailTask.id+'&project='+projectId)).dependencies.map(row=>row.dependency).sort());
    assert.deepEqual(persisted,parents.sort());
    await page.reload();
    await page.waitForFunction(() => document.querySelector('#detail-body select[multiple]')?.selectedOptions.length === 2);
    assert.deepEqual(errors, []);
    console.log('Multiple predecessor selection, bilingual drafts, persistence and reload browser checks passed without model calls.');
  } finally {
    if (browser) await browser.close();
    server.stdin.end('\n');
    await new Promise(resolve => server.once('exit', resolve));
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
