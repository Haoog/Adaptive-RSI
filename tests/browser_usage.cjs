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
  await page.locator('#usage-start').evaluate(el=>el.value='2026-10-20');
  await page.locator('#usage-end').evaluate(el=>el.value='2026-10-22');
  await page.locator('#usage-period').selectOption('custom');
  await page.getByText('开始日期（UTC）',{exact:true}).waitFor();
  await page.getByText('结束日期（UTC）',{exact:true}).waitFor();
  await page.locator('#language-select').selectOption('en');
  await page.locator('#page-name').filter({hasText:'Records and usage'}).waitFor();
  assert.equal(await page.locator('#usage-refresh').textContent(),'Refresh');
  assert.equal(await page.locator('#usage-search').locator('xpath=..').textContent(),'Search');
  await page.getByText('Start date (UTC)',{exact:true}).waitFor();
  await page.getByText('End date (UTC)',{exact:true}).waitFor();
  await page.setViewportSize({width:390,height:844});
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'Mobile overflow');
  await page.screenshot({path:'/tmp/rsi-usage-mobile.png',fullPage:true});
  await page.locator('#language-select').selectOption('zh-CN');
  const totals={tokens:0,unknown_calls:1,tasks:1,model_seconds:1,cache_read_tokens:0,estimated_usd:null,unpriced_calls:1};
  await page.route('**/api/usage?*',route=>{
   const detail=new URL(route.request().url()).searchParams.has('task');
   const data={summary:totals,daily:[],roles:[],tasks:[{task:'unknown',title:'未知用量验证',tokens:0,unknown_calls:1,model_seconds:1,attempts:1,state:'failed'}],total:1};
   if(detail)Object.assign(data,{call_total:1,calls:[{role:'planning',model:'fixture',provider:'fixture',effort:'high',state:'failed',created:100,usage_known:false,tokens:0,input_tokens:0,output_tokens:0,cache_read_tokens:0,cache_write_tokens:0,seconds:1,unit:'unknown'}]});
   return route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(data)});
  });
  await page.locator('#usage-refresh').click();await page.getByRole('button',{name:'查看用量明细',exact:true}).click();
  await page.locator('#usage-detail-content p').filter({hasText:'输入 未知 / 输出 未知 / 缓存读取 未知 / 缓存写入 未知'}).waitFor();
  assert.deepEqual(errors,[]);console.log('Unknown usage details remain unknown, never fabricated zeroes.');
  console.log('Usage page: real API, empty/filter state, English, mobile and no JS errors passed.');
 }finally{if(browser)await browser.close();server.stdin.end('\n');await new Promise(resolve=>server.once('exit',resolve));}
})().catch(e=>{console.error(e);process.exitCode=1;});
