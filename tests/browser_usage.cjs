const assert=require('node:assert/strict');
const {spawn}=require('node:child_process');
const {createInterface}=require('node:readline');
const {chromium}=require('playwright');
(async()=>{
 const server=spawn(process.env.PYTHON||'python3',['-m','tests.browser_fixture'],{stdio:['pipe','pipe','pipe']});server.stderr.on('data',chunk=>process.stderr.write(chunk));
 let browser;
 try{
  const url=await new Promise((resolve,reject)=>{const timeout=setTimeout(()=>reject(Error('Fixture timed out')),30000);createInterface({input:server.stdout}).once('line',line=>{clearTimeout(timeout);resolve(line);});});
  browser=await chromium.launch(process.env.RSI_BROWSER_CHANNEL?{channel:process.env.RSI_BROWSER_CHANNEL}:{});
  const page=await browser.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto(url);await page.locator('#project-status').filter({hasText:'已暂停'}).waitFor();
  await page.locator('[data-view="usage"]').click();await page.locator('#usage-totals strong').first().waitFor();
  await page.locator('#usage-period').selectOption('all');await page.locator('#usage-refresh').filter({hasNot:page.locator('[disabled]')});
  await page.waitForFunction(()=>!document.getElementById('usage-refresh').disabled);
  assert.equal(await page.locator('#view-usage').isVisible(),true);
  await page.locator('#usage-search').fill('no-matching-task-xyz');await page.waitForTimeout(700);
  await page.locator('#usage-tasks').filter({hasText:'所选时间没有用量记录。'}).waitFor();
  assert.match(await page.locator('#usage-page').textContent(),/0–0/);
  await page.locator('#usage-search').fill('');await page.waitForTimeout(700);
  await page.locator('#language-select').selectOption('en');
  await page.locator('#page-name').filter({hasText:'Records and usage'}).waitFor();
  await page.setViewportSize({width:390,height:844});
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'Mobile overflow');
  await page.screenshot({path:'/tmp/rsi-usage-mobile.png',fullPage:true});
  assert.deepEqual(errors,[]);console.log('Usage page: real API, empty/filter state, English, mobile and no JS errors passed.');
 }finally{if(browser)await browser.close();server.stdin.end('\n');await new Promise(resolve=>server.once('exit',resolve));}
})().catch(e=>{console.error(e);process.exitCode=1;});
