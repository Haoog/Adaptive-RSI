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
  await page.getByText('开始日期',{exact:true}).waitFor();
  await page.getByText('结束日期',{exact:true}).waitFor();
  await page.locator('#language-select').selectOption('en');
  await page.locator('#page-name').filter({hasText:'Records and usage'}).waitFor();
  assert.equal(await page.locator('#usage-refresh').textContent(),'Refresh');
  assert.equal(await page.locator('#usage-search').locator('xpath=..').textContent(),'Search');
  await page.getByText('Start date',{exact:true}).waitFor();
  await page.getByText('End date',{exact:true}).waitFor();
  await page.setViewportSize({width:390,height:844});
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'Mobile overflow');
  await page.screenshot({path:'/tmp/rsi-usage-mobile.png',fullPage:true});
  await page.locator('#language-select').selectOption('zh-CN');
  const totals={tokens:0,unknown_calls:1,tasks:1,model_seconds:1,cache_read_tokens:0,estimated_usd:null,unpriced_calls:1};
  let background=false;
  let holdDetail=false,resolveDetail,startedDetail;let detailStarted=new Promise(resolve=>startedDetail=resolve);
  await page.route('**/api/usage?*',async route=>{
   const detail=new URL(route.request().url()).searchParams.has('task');
   if(detail&&holdDetail){startedDetail();await new Promise(resolve=>{resolveDetail=resolve;});}
   const data={summary:totals,daily:[],roles:[],tasks:[{task:'unknown',title:background?'System: light':'未知用量验证',background,tokens:totals.tokens,unknown_calls:totals.unknown_calls,model_seconds:1,attempts:1,state:'failed'}],total:1};
   if(detail)Object.assign(data,{call_total:1,calls:[{role:'planning',model:'fixture',provider:'fixture',effort:'high',state:'failed',created:100,usage_known:false,tokens:0,input_tokens:0,output_tokens:0,cache_read_tokens:0,cache_write_tokens:0,seconds:1,unit:'unknown'}]});
   await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(data)});
  });
  await page.locator('#usage-refresh').click();await page.getByRole('button',{name:'查看用量明细',exact:true}).click();
  assert.equal(await page.locator('#usage-totals strong').first().textContent(),'用量未知');
  assert.equal(await page.locator('#usage-detail-content h2').textContent(),'未知用量验证');
  await page.locator('#usage-detail-content p').filter({hasText:'输入 未知 / 输出 未知 / 缓存读取 未知 / 缓存写入 未知'}).waitFor();
  await page.locator('#usage-detail .close-dialog').click();background=true;
  await page.locator('#usage-refresh').click();
  await page.waitForFunction(()=>document.querySelector('#usage-tasks strong').textContent==='后台检查与连接测试');
  await page.getByRole('button',{name:'查看用量明细',exact:true}).click();
  await page.locator('#usage-detail-content h2').filter({hasText:'后台检查与连接测试'}).waitFor();
  assert.equal(await page.locator('#usage-detail-content').textContent().then(text=>text.includes('System: light')),false);
  await page.locator('#usage-detail .close-dialog').click();
  await page.locator('#language-select').selectOption('en');
  await page.getByRole('button',{name:'View usage details',exact:true}).click();
  await page.locator('#usage-detail-content h2').filter({hasText:'Background checks and connection tests'}).waitFor();
  assert.equal(await page.locator('#usage-totals strong').first().textContent(),'Unknown usage');
  await page.locator('#usage-detail .close-dialog').click();
  await page.locator('#language-select').selectOption('zh-CN');totals.tokens=12;
  await page.locator('#usage-refresh').click();await page.waitForFunction(()=>document.querySelector('#usage-totals strong').textContent==='12 + 部分未知');
  totals.tokens=0;totals.unknown_calls=0;await page.locator('#usage-refresh').click();
  await page.waitForFunction(()=>document.querySelector('#usage-totals strong').textContent==='0');
  totals.unknown_calls=1;await page.locator('#usage-refresh').click();
  await page.getByRole('button',{name:'查看用量明细',exact:true}).click();
  assert.deepEqual(errors,[]);console.log('Unknown usage details remain unknown, never fabricated zeroes.');
  await page.locator('#usage-detail .close-dialog').click();holdDetail=true;detailStarted=new Promise(resolve=>startedDetail=resolve);
  await page.getByRole('button',{name:'查看用量明细',exact:true}).click();await detailStarted;
  await page.locator('#usage-search').fill('changed while detail loads');await page.waitForTimeout(50);resolveDetail();await page.waitForTimeout(100);
  assert.equal(await page.locator('#usage-detail').isVisible(),false,'a delayed detail must not reopen after the search changes');
  await page.waitForTimeout(400);detailStarted=new Promise(resolve=>startedDetail=resolve);
  await page.getByRole('button',{name:'查看用量明细',exact:true}).click();await detailStarted;
  const otherProject=await page.locator('#project-select option').evaluateAll(options=>options.find(option=>option.textContent.includes('另一个独立项目')).value);
  await page.locator('#project-select').selectOption(otherProject);await page.waitForFunction(()=>!document.getElementById('usage-refresh').disabled);assert.equal(await page.locator('#usage-detail').isVisible(),false);
  resolveDetail();await page.waitForTimeout(100);assert.equal(await page.locator('#usage-detail').isVisible(),false,'a delayed detail must not reopen after the project changes');
  const dstContext=await browser.newContext({timezoneId:'America/New_York'}),dstPage=await dstContext.newPage();const requests=[];
  await dstPage.route('**/api/usage?*',async route=>{
   requests.push(Object.fromEntries(new URL(route.request().url()).searchParams));
   await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({summary:{tokens:12,unknown_calls:0,tasks:1,model_seconds:1,cache_read_tokens:0,estimated_usd:null,unpriced_calls:1},daily:[],roles:[{role:'acceptance',tokens:12}],tasks:[],total:0})});
  });
  try{
   assert.equal(await dstPage.evaluate(()=>Intl.DateTimeFormat().resolvedOptions().timeZone),'America/New_York');
   await dstPage.goto(url);await dstPage.locator('#project-status').filter({hasText:'已暂停'}).waitFor();await dstPage.locator('[data-view="usage"]').click();
   await dstPage.locator('#usage-role-summary').filter({hasText:'验收设计'}).waitFor();
   await dstPage.locator('#usage-period').selectOption('custom');
   await dstPage.locator('#usage-start').fill('2026-11-01');
   await dstPage.locator('#usage-end').fill('2026-11-01');
   await dstPage.waitForTimeout(150);
   const expectedStart=await dstPage.evaluate(()=>new Date(2026,10,1).getTime()/1000);
   const dst=requests.find(query=>Number(query.start)===expectedStart);
   assert.ok(dst,'custom usage request was not sent');
   assert.equal(dst.timezone,'America/New_York');
   assert.equal(Number(dst.end)-Number(dst.start),25*3600,'fall-back day must end at the next local midnight');
  }finally{await dstPage.close();await dstContext.close();}
  // —— 分页：在途禁用、越界回夹、边界状态；明细角色回退（价值初筛/验收设计）中英文一致。
  let shrinkTotal=0;
  const usageRows=(offset,total)=>Array.from({length:Math.max(0,Math.min(25,total-offset))},(_,i)=>({
   task:'page-'+(offset+i),title:'分页任务 '+(offset+i+1),project:'fixture',tokens:10,unknown_calls:0,
   model_seconds:1,attempts:1,state:'completed'}));
  const usageRoute=async route=>{
   const params=new URL(route.request().url()).searchParams;
   if(params.has('task')){
    const roles=[['triage',6],['acceptance',4],['evolution',2]].map(([role,tokens])=>({role,provider:'fixture',model:'fixture',effort:'high',tokens}));
    const calls=roles.map((item,index)=>({role:item.role,model:'fixture',provider:'fixture',effort:'high',state:'done',created:100+index,usage_known:true,tokens:3,input_tokens:1,output_tokens:1,cache_read_tokens:0,cache_write_tokens:0,seconds:1,unit:'http_request'}));
    return route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({summary:{tokens:12,unknown_calls:0,tasks:1,model_seconds:3,cache_read_tokens:0,estimated_usd:null,unpriced_calls:0},daily:[],roles,tasks:[],total:0,call_total:calls.length,calls})});
   }
   await new Promise(resolve=>setTimeout(resolve,200));
   const offset=Number(params.get('offset')||0),total=shrinkTotal||60;
   await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({summary:{tokens:600,unknown_calls:0,tasks:1,model_seconds:6,cache_read_tokens:0,estimated_usd:null,unpriced_calls:0},daily:[],roles:[],tasks:usageRows(offset,total),total})});
  };
  await page.route('**/api/usage?*',usageRoute);
  try{
   await page.locator('#usage-refresh').click();
   await page.waitForFunction(()=>!document.getElementById('usage-refresh').disabled);
   assert.equal(await page.locator('#usage-page').textContent(),'60 条记录 · 1–25');
   assert.equal(await page.locator('#usage-prev').isDisabled(),true);
   assert.equal(await page.locator('#usage-next').isDisabled(),false);
   assert.equal(await page.locator('#usage-tasks article').count(),25);
   await page.locator('#usage-next').click();
   assert.equal(await page.locator('#usage-next').isDisabled(),true,'next must be disabled while a page loads');
   assert.equal(await page.locator('#usage-prev').isDisabled(),true,'prev must be disabled while a page loads');
   await page.waitForFunction(()=>!document.getElementById('usage-next').disabled);
   assert.equal(await page.locator('#usage-page').textContent(),'60 条记录 · 26–50');
   await page.locator('#usage-next').click();
   await page.waitForFunction(()=>document.getElementById('usage-page').textContent==='60 条记录 · 51–60');
   assert.equal(await page.locator('#usage-next').isDisabled(),true,'next must be disabled on the last page');
   assert.equal(await page.locator('#usage-prev').isDisabled(),false);
   // 记录总数回落到当前页之前时，offset 必须回夹并重取当前页，而不是显示“51–36”和空列表。
   shrinkTotal=36;
   await page.locator('#usage-refresh').click();
   await page.waitForFunction(()=>document.getElementById('usage-page').textContent==='36 条记录 · 26–36');
   assert.equal(await page.locator('#usage-tasks article').count(),11);
   assert.equal(await page.locator('#usage-next').isDisabled(),true);
   assert.equal(await page.locator('#usage-prev').isDisabled(),false);
   // 搜索仍然把分页重置回第一页。
   await page.locator('#usage-search').fill('分页任务');
   await page.waitForFunction(()=>document.getElementById('usage-page').textContent==='36 条记录 · 1–25');
   assert.equal(await page.locator('#usage-prev').isDisabled(),true);
   // 明细角色：triage/acceptance/evolution 显示“价值初筛/验收设计/策略改进”。
   const detailContent=page.locator('#usage-detail-content');
   await page.getByRole('button',{name:'查看用量明细',exact:true}).first().click();
   await detailContent.locator('span').filter({hasText:'价值初筛'}).first().waitFor();
   assert.ok(await detailContent.locator('span').filter({hasText:'验收设计'}).first().isVisible());
   assert.ok(await detailContent.locator('strong').filter({hasText:'策略改进'}).first().isVisible());
   assert.equal(await detailContent.innerText().then(text=>/triage|acceptance|evolution/.test(text)),false);
   await page.locator('#usage-detail .close-dialog').click();
   await page.locator('#language-select').selectOption('en');
   await page.getByRole('button',{name:'View usage details',exact:true}).first().click();
   await detailContent.locator('span').filter({hasText:'Value screening'}).first().waitFor();
   assert.ok(await detailContent.locator('span').filter({hasText:'Acceptance design'}).first().isVisible());
   assert.ok(await detailContent.locator('strong').filter({hasText:'Strategy improvement'}).first().isVisible());
   assert.equal(await detailContent.innerText().then(text=>/triage|acceptance|evolution/.test(text)),false);
   await page.locator('#usage-detail .close-dialog').click();
   await page.locator('#language-select').selectOption('zh-CN');
   await page.getByRole('button',{name:'查看用量明细',exact:true}).first().click();
   await detailContent.locator('span').filter({hasText:'价值初筛'}).first().waitFor();
   await page.locator('#usage-detail .close-dialog').click();
  }finally{await page.unroute('**/api/usage?*',usageRoute);}
  console.log('Usage page: real API, empty/filter state, English, mobile and no JS errors passed.');
 }finally{if(browser)await browser.close();server.stdin.end('\n');await new Promise(resolve=>server.once('exit',resolve));}
})().catch(e=>{console.error(e);process.exitCode=1;});
