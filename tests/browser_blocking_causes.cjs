const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const {createInterface} = require('node:readline');
const {chromium} = require('playwright');

(async () => {
  const server = spawn(process.env.PYTHON || 'python3', ['-m', 'tests.browser_fixture'],
    {stdio: ['pipe', 'pipe', 'pipe'], env: {...process.env, PYTHONDONTWRITEBYTECODE: '1', RSI_BROWSER_BLOCKERS:'1'}});
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
    await page.evaluate(async()=>{const data=await api('state?sections=core&limit=200');await showTask(data.tasks.find(task=>task.title==='等待原因链任务'));});
    const section=page.locator('#detail-body details').filter({has:page.locator('summary').filter({hasText:'为什么还没开始'})});
    await section.locator('summary').click();
    await section.getByText('项目已暂停',{exact:true}).waitFor();
    await section.getByText('等待原因链任务 → 原因链前置甲',{exact:true}).waitFor();
    await section.getByText('原因链前置甲 → 原因链前置乙',{exact:true}).waitFor();
    assert.match(await section.innerText(),/等待连接重试时间/);
    await page.evaluate(async()=>api('pause',{project:projectId,paused:false}));
    await section.getByRole('button',{name:'刷新阻塞原因',exact:true}).click();
    await section.getByText('项目已暂停',{exact:true}).waitFor({state:'detached'});
    assert.match(await section.innerText(),/等待连接重试时间/);
    assert.match(await section.innerText(),/等待前置任务合并/);
    await page.locator('#language-select').selectOption('en');
    const english=page.locator('#detail-body details').filter({has:page.locator('summary').filter({hasText:'Why has this not started?'})});
    if(!await english.evaluate(node=>node.open))await english.locator('summary').click();
    await english.getByText('等待原因链任务 → 原因链前置甲',{exact:true}).waitFor();
    assert.match(await english.innerText(),/Waiting for connection retry/);
    assert.deepEqual(errors, []);
    console.log('Simultaneous blocking causes, dependency chain, refresh and bilingual controls passed.');
  } finally {
    if (browser) await browser.close();
    server.stdin.end();
    await new Promise(resolve => {server.once('exit', resolve); setTimeout(()=>{server.kill('SIGKILL');resolve();},5000).unref();});
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
