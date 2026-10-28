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
    await page.evaluate(async () => {
      await api('config', {project: projectId, values: {github:{repository:'example/project'}, delivery:{mode:'pr',deploy:{enabled:true,workflow:'.github/workflows/deploy.yml',ref:'trusted-v1',sha:'a'.repeat(40),health_url:'https://app.example/health',health_urls:['https://eu.example/health'],health_attempts:4,health_consecutive_successes:2}}}});
      await refresh({force:true});
    });
    await page.locator('#project-settings').click();
    const additional = page.locator('[data-path="delivery.deploy.health_urls"]');
    assert.equal(await additional.inputValue(), 'https://eu.example/health');
    await additional.fill('https://eu.example/health\nhttps://us.example/health');
    const streak = page.locator('[data-path="delivery.deploy.health_consecutive_successes"]');
    await streak.fill('3');
    await page.locator('[data-path="delivery.deploy.rollback_data_policy"]').selectOption('backward_compatible');
    await page.locator('[data-path="delivery.deploy.rollback_recovery_notes"]').fill('Inspect schema backup; restore a compatible version.');
    const saved = page.waitForResponse(response=>new URL(response.url()).pathname==='/api/config'&&response.request().method()==='POST');
    await page.locator('#settings-save').click();
    const response = await saved;
    assert.equal(response.status(),200,await response.text());
    assert.deepEqual(response.request().postDataJSON().values.delivery.deploy.health_urls,['https://eu.example/health','https://us.example/health']);
    await page.waitForFunction(() => !document.getElementById('settings-save').disabled);
    await page.locator('#language-select').selectOption('en');
    assert.equal(await additional.inputValue(), 'https://eu.example/health\nhttps://us.example/health');
    assert.equal(await streak.inputValue(), '3');
    assert.equal(await page.locator('[data-path="delivery.deploy.rollback_data_policy"]').inputValue(), 'backward_compatible');
    assert.equal(await page.locator('[data-path="delivery.deploy.rollback_recovery_notes"]').inputValue(), 'Inspect schema backup; restore a compatible version.');
    await page.getByText('Required consecutive successful rounds',{exact:true}).waitFor();
    await page.reload();await page.waitForFunction(()=>state&&currentProject()?.paused);await page.locator('.sidebar nav [data-view="overview"]').click();
    await page.locator('#project-status').waitFor();
    await page.locator('#project-settings').click();
    assert.equal(await additional.inputValue(), 'https://eu.example/health\nhttps://us.example/health');
    await page.locator('[data-path="delivery.deploy.enabled"]').uncheck();
    assert.equal(await additional.isDisabled(),true);
    assert.equal(await streak.isDisabled(),true);
    assert.deepEqual(errors, []);
    console.log('Deployment health endpoints, consecutive gate, persistence and bilingual controls passed.');
  } finally {
    if (browser) await browser.close();
    server.stdin.end();
    await new Promise(resolve => {server.once('exit', resolve); setTimeout(()=>{server.kill('SIGKILL');resolve();},5000).unref();});
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
