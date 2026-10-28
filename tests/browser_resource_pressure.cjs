const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const {createInterface} = require('node:readline');
const {chromium} = require('playwright');

(async () => {
  const server = spawn(process.env.PYTHON || 'python3', ['-m', 'tests.browser_fixture'],
    {stdio: ['pipe', 'pipe', 'pipe'], env: {...process.env, PYTHONDONTWRITEBYTECODE: '1', RSI_BROWSER_RESOURCE_PRESSURE:'1'}});
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
    await page.getByRole('button',{name:'等待资源恢复的任务',exact:true}).waitFor();
    const row = page.locator('.task-row').filter({has:page.getByRole('button',{name:'等待资源恢复的任务',exact:true})});
    await row.getByText('等待磁盘或内存压力解除',{exact:false}).waitFor();
    const before = await page.evaluate(async()=>api('state?sections=core'));
    assert.equal(before.resource_admission.admitted,false);
    assert.equal(before.tasks.find(task=>task.title==='等待资源恢复的任务').state,'queued');
    await page.locator('#project-settings').click();
    await page.locator('#advanced-settings > summary').click();
    const disk = page.locator('[data-path="limits.min_free_disk_mb"]');
    assert.equal(await disk.inputValue(),'1048576');
    await disk.fill('0');
    const saved = page.waitForResponse(response=>new URL(response.url()).pathname==='/api/config'&&response.request().method()==='POST');
    await page.locator('#settings-save').click();
    assert.equal((await saved).status(),200);
    await page.waitForFunction(()=>!document.getElementById('settings-save').disabled);
    const after = await page.evaluate(async()=>api('state?sections=core'));
    assert.notEqual(after.tasks.find(task=>task.title==='等待资源恢复的任务').wait_reason,'resource_pressure');
    await page.locator('#language-select').selectOption('en');
    await page.getByText('Minimum free disk space / MiB',{exact:true}).waitFor();
    assert.equal(await disk.inputValue(),'0');
    assert.deepEqual(errors, []);
    console.log('Resource pressure queue explanation, threshold save and bilingual controls passed.');
  } finally {
    if (browser) await browser.close();
    server.stdin.end();
    await new Promise(resolve => {server.once('exit', resolve); setTimeout(()=>{server.kill('SIGKILL');resolve();},5000).unref();});
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
