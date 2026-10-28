const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const {createInterface} = require('node:readline');
const {chromium} = require('playwright');

(async () => {
  const server = spawn(process.env.PYTHON || 'python3', ['-m', 'tests.browser_fixture'],
    {stdio: ['pipe', 'pipe', 'pipe'], env: {...process.env, PYTHONDONTWRITEBYTECODE: '1'}});
  let browser;
  try {
    const url = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(Error('Fixture startup timed out')), 30000);
      createInterface({input: server.stdout}).once('line', line => {clearTimeout(timer); resolve(line);});
      server.once('exit', code => {clearTimeout(timer); reject(Error('Fixture exited: ' + code));});
    });
    browser = await chromium.launch(process.env.RSI_BROWSER_CHANNEL ? {channel: process.env.RSI_BROWSER_CHANNEL} : {});
    const page = await browser.newPage(), errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(url);
    await page.locator('#project-status').filter({hasText: '已暂停'}).waitFor();
    const saved = await page.evaluate(async () => {
      const before = await api('config-history?project='+projectId);
      await api('config', {project: projectId, values: {schedule: {weight: 2}}, revision: before.revision});
      return (await api('config-history?project='+projectId)).revision;
    });
    await page.locator('#project-settings').click();
    await page.locator('#advanced-settings > summary').click();
    const weight = page.locator('[data-path="schedule.weight"]');
    await weight.fill('3');
    const history = page.locator('dialog[open]').filter({has: page.getByRole('heading', {name:'设置变更记录',exact:true})});
    await page.locator('#settings-history').evaluate(button => {button.click();button.click();});
    await history.locator('details').first().locator('summary').click();
    await history.getByRole('button', {name:'撤销这次设置修改',exact:true}).waitFor();
    assert.equal(await history.count(), 1, 'repeated history clicks open one dialog');
    const undo = history.getByRole('button', {name:'撤销这次设置修改',exact:true});
    await undo.evaluate(button => {button.click();button.click();});
    const confirmation = page.locator('dialog.confirm-dialog[open]');
    assert.equal(await confirmation.count(), 1, 'repeated undo clicks open one confirmation');
    await confirmation.getByRole('button', {name:'取消',exact:true}).click();
    assert.equal(await weight.inputValue(), '3', 'dismissed undo preserves an unsaved draft');
    await undo.click();
    const posted = page.waitForResponse(response=>new URL(response.url()).pathname==='/api/config/undo');
    await confirmation.getByRole('button', {name:'确认',exact:true}).click();
    assert.equal((await posted).status(), 200);
    await page.locator('#toast').filter({hasText:'设置已撤销'}).waitFor();
    const after = await page.evaluate(async () => api('config-history?project='+projectId));
    assert.equal(after.entries[0].source, 'undo');
    assert.equal(after.entries[0].undo_of, saved);
    assert.equal(await weight.inputValue(), '3', 'completed undo preserves a newer unsaved draft');
    await page.locator('#language-select').selectOption('en');
    await page.locator('#settings-history').click();
    await page.getByRole('heading', {name:'Settings change history',exact:true}).waitFor();
    await page.locator('dialog[open] details').first().locator('summary').click();
    await page.getByRole('button', {name:'Undo this settings change',exact:true}).waitFor();
    assert.deepEqual(errors, []);
    console.log('Configuration history, revision undo, drafts and bilingual controls passed.');
  } finally {
    if (browser) await browser.close();
    server.stdin.end();
    await new Promise(resolve => {server.once('exit', resolve); setTimeout(()=>{server.kill('SIGKILL');resolve();},5000).unref();});
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
