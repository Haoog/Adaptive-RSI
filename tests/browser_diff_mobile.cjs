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
    const patch = 'diff --git a/first.py b/first.py\n--- a/first.py\n+++ b/first.py\n@@ -1 +1 @@\n-old\n+new\ndiff --git a/second.py b/second.py\n--- a/second.py\n+++ b/second.py\n@@ -1 +1 @@\n-other\n+UNIQUE-SECOND-MATCH';
    await page.route('**/api/patch?*', route=>route.fulfill({status:200,contentType:'text/plain',body:patch}));
    await page.route('**/api/diff-files?*', route=>route.fulfill({status:200,contentType:'application/json',body:JSON.stringify([{path:'first.py',line:1,additions:1,deletions:1},{path:'second.py',line:7,additions:1,deletions:1}])}));
    await page.setViewportSize({width:390,height:844});
    await page.evaluate(()=>RSIExperience.showDiff('fixture'));
    const box=page.locator('dialog.experience-dialog[open]'),files=box.locator('.diff-file');
    assert.equal(await files.count(),2);
    assert.equal(await files.nth(1).locator('pre').textContent(),'','collapsed files defer line rendering');
    assert.equal(await box.getByLabel('自动换行',{exact:true}).isChecked(),true);
    await box.getByRole('searchbox',{name:'在差异中搜索',exact:true}).fill('UNIQUE-SECOND-MATCH');
    await box.getByRole('button',{name:'下一个匹配',exact:true}).click();
    assert.equal(await files.nth(1).evaluate(node=>node.open),true);
    assert.match(await box.locator('.diff-selected').innerText(),/UNIQUE-SECOND-MATCH/);
    await box.getByRole('button',{name:'折叠全部文件',exact:true}).click();
    assert.equal(await files.evaluateAll(nodes=>nodes.every(node=>!node.open)),true);
    await box.getByRole('button',{name:'展开全部文件',exact:true}).click();
    assert.equal(await files.evaluateAll(nodes=>nodes.every(node=>node.open)),true);
    await box.getByRole('button',{name:'关闭',exact:true}).click();
    const longLines=Array.from({length:2300},(_,i)=>'+'+i+' '+('long text '.repeat(15)));
    longLines[2100]='+LARGE-TARGET '+('wrapped '.repeat(30));
    await page.route('**/api/patch?*',route=>route.fulfill({status:200,contentType:'text/plain',body:patch+'\n'+longLines.join('\n')}));
    await page.evaluate(()=>RSIExperience.showDiff('large-fixture'));
    await box.getByRole('searchbox',{name:'在差异中搜索',exact:true}).fill('LARGE-TARGET');
    await box.getByRole('button',{name:'下一个匹配',exact:true}).click();
    const position=await box.locator('.readable-diff').evaluate(root=>{
      const file=[...root.querySelectorAll('.diff-file')].at(-1),text=file.querySelector('pre').firstChild,offset=text.textContent.indexOf('+LARGE-TARGET');
      const range=document.createRange();range.setStart(text,offset);range.setEnd(text,offset+13);
      const bounds=range.getBoundingClientRect(),frame=root.getBoundingClientRect();return {top:bounds.top,bottom:bounds.bottom,frameTop:frame.top,frameBottom:frame.bottom,overflow:document.documentElement.scrollWidth>window.innerWidth};
    });
    assert.ok(position.top>=position.frameTop-3&&position.bottom<=position.frameBottom+3,JSON.stringify(position));
    assert.equal(position.overflow,false,'mobile document must not overflow horizontally');
    await box.getByRole('button',{name:'关闭',exact:true}).click();
    await page.route('**/api/patch?*',route=>route.fulfill({status:200,contentType:'text/plain',body:patch.replace('diff --git a/second.py',longLines.slice(0,90).join('\n')+'\ndiff --git a/second.py')}));
    await page.evaluate(()=>RSIExperience.showDiff('layout-fixture'));
    await box.getByRole('button',{name:'展开全部文件',exact:true}).click();
    const layout=await box.locator('.diff-file').evaluateAll(files=>({
      firstBottom:files[0].querySelector('pre').getBoundingClientRect().bottom,
      secondTop:files[1].getBoundingClientRect().top,
      maxHeight:getComputedStyle(files[0].querySelector('pre')).maxHeight
    }));
    assert.equal(layout.maxHeight,'none');
    assert.ok(layout.firstBottom<=layout.secondTop,JSON.stringify(layout));

    assert.deepEqual(errors, []);
    console.log('Mobile diff folding, lazy files and wrapped large-text location passed.');
  } finally {
    if (browser) await browser.close();
    server.stdin.end();
    await new Promise(resolve => {server.once('exit', resolve); setTimeout(()=>{server.kill('SIGKILL');resolve();},5000).unref();});
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
