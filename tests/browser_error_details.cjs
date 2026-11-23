const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const {fixtureReady, closeFixture, waitForPausedProject} = require('./fixture_browser.cjs');
const {chromium} = require('playwright');

(async () => {
  const server = spawn(process.env.PYTHON || 'python3', ['-m', 'tests.browser_fixture'],
    {stdio: ['pipe', 'pipe', 'pipe'], env: {...process.env, PYTHONDONTWRITEBYTECODE: '1'}});
  let browser, scenarioError;
  try {
    const url = await fixtureReady(server);
    browser = await chromium.launch(process.env.RSI_BROWSER_CHANNEL ? {channel: process.env.RSI_BROWSER_CHANNEL} : {});
    const context=await browser.newContext({permissions:['clipboard-read','clipboard-write']});
    const page = await context.newPage(), errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(url);
    await waitForPausedProject(page);
    await page.locator('#project-settings').click();
    const trigger=()=>page.evaluate(async()=>safe(()=>api('config',{values:{limits:{concurrency:5}}}),document.getElementById('settings-form')));
    await trigger();
    const feedback=page.locator('#settings-form [data-form-error]');
    await feedback.filter({hasText:'并行项目数须为 1–4。'}).waitFor();
    assert.match(await feedback.textContent(),/limits.concurrency/);
    await page.locator('#language-select').selectOption('en');
    await trigger();
    await feedback.filter({hasText:'Parallel projects must be 1–4.'}).waitFor();
    assert.match(await feedback.textContent(),/limits.concurrency/);
    const diagnostic='0123456789abcdef';
    await page.route('**/api/unknown-error-fixture',route=>route.fulfill({status:500,headers:{'X-RSI-Request-ID':diagnostic},contentType:'application/json',body:JSON.stringify({error:'Request could not be completed',error_code:'internal_error'})}));
    await page.evaluate(async()=>safe(()=>api('unknown-error-fixture')));
    await page.locator('#toast').filter({hasText:'Diagnostic ID'}).waitFor();
    await page.getByRole('button',{name:'Copy diagnostic ID',exact:true}).click();
    assert.equal(await page.evaluate(()=>navigator.clipboard.readText()),diagnostic);
    assert.deepEqual(errors, []);
    console.log('Structured configuration guidance, bilingual fields and diagnostic ID copy passed.');
  } catch (error) {
    scenarioError = error;
    throw error;
  } finally {
    await closeFixture(server, browser, scenarioError);
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
