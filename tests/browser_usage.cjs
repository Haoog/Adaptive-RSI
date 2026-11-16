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
  // —— B10：刷新失败保留旧报表时，必须持久标出旧数据范围、最后成功时间与可重试入口，成功/重试后清除。
  const allTotals=await page.locator('#usage-totals').innerText();
  await page.route('**/api/usage?*',route=>route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({error:'Isolated usage outage'})}));
  const failedRefresh=page.waitForResponse(response=>new URL(response.url()).pathname==='/api/usage'&&response.status()===503);
  await page.locator('#usage-period').selectOption('7');
  await failedRefresh;
  await page.waitForFunction(()=>!document.getElementById('usage-refresh').disabled);
  assert.equal(await page.locator('#usage-totals').innerText(),allTotals,'failed refresh must keep the last successful report');
  const stale=page.locator('#usage-stale');
  await stale.waitFor();
  const staleCopy=await stale.innerText();
  assert.match(staleCopy,/全部时间/,'stale notice must name the range that produced the visible data');
  assert.match(staleCopy,/最后成功更新/,'stale notice must include the last successful refresh time');
  assert.equal(await stale.getByRole('button',{name:'重试',exact:true}).count(),1,'stale notice must offer a retry entry');
  assert.equal(await page.locator('#usage-period').inputValue(),'7','the newly chosen filter stays visible beside the stale marker');
  await page.unroute('**/api/usage?*');

  await page.locator('#language-select').selectOption('en');
  await stale.filter({hasText:'All time'}).waitFor();
  assert.doesNotMatch(await stale.innerText(),/全部时间/,'retained report scope must translate with the page');
  await page.locator('#language-select').selectOption('zh-CN');
  const recovered=page.waitForResponse(response=>new URL(response.url()).pathname==='/api/usage'&&response.ok());
  await stale.getByRole('button',{name:'重试',exact:true}).click();
  await recovered;
  await page.waitForFunction(()=>!document.getElementById('usage-refresh').disabled);
  await stale.waitFor({state:'hidden'});
  // 首次加载就失败、还没有旧报表时，也要有可重试的持久提示，而不是只有 toast。
  const emptyPage=await browser.newPage();emptyPage.on('pageerror',error=>errors.push(error.message));
  await emptyPage.route('**/api/usage?*',route=>route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({error:'Isolated usage outage'})}));
  await emptyPage.goto(url);await emptyPage.locator('#project-status').filter({hasText:'已暂停'}).waitFor();
  await emptyPage.locator('[data-view="usage"]').click();
  const emptyStale=emptyPage.locator('#usage-stale');await emptyStale.waitFor();
  assert.match(await emptyStale.innerText(),/用量加载失败/,'first failure without data must explain the failure inline');
  assert.equal(await emptyStale.getByRole('button',{name:'重试',exact:true}).count(),1);
  await emptyPage.unroute('**/api/usage?*');
  const emptyRecovered=emptyPage.waitForResponse(response=>new URL(response.url()).pathname==='/api/usage'&&response.ok());
  await emptyStale.getByRole('button',{name:'重试',exact:true}).click();
  await emptyRecovered;await emptyStale.waitFor({state:'hidden'});
  await emptyPage.close();
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
   if(detail)Object.assign(data,{references:{groups:[{role:'planning',model:'fixture',history:{status:'ok',samples:8,denominator:8,tokens:{p25:100,p75:400},seconds:{p25:1,p75:4}}}],min_samples:8},runs:[{id:'fixture-run',tokens:0,unknown_calls:1,model_seconds:1,retry_calls:1,recovered_stages:1}],call_total:1,calls:[{role:'planning',model:'fixture',provider:'fixture',effort:'high',state:'failed',created:100,usage_known:false,tokens:0,input_tokens:0,output_tokens:0,cache_read_tokens:0,cache_write_tokens:0,seconds:1,unit:'unknown'}]});
   await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(data)});
  });
  await page.locator('#usage-refresh').click();await page.getByRole('button',{name:'查看用量明细',exact:true}).click();
  assert.equal(await page.locator('#usage-totals strong').first().textContent(),'用量未知');
  assert.equal(await page.locator('#usage-detail-content h2').textContent(),'未知用量验证');
  await page.locator('#usage-detail-content .usage-reference').filter({hasText:'100–400 tokens'}).waitFor();
  await page.locator('#usage-detail-content .usage-run').filter({hasText:'fixture-run'}).waitFor();
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
  // 明细翻页在途时关闭：响应不能重开弹窗，也不能抢走恢复后的焦点。
  let holdPage=false,releasePage,pageStarted,pageFailure=null;
  const paginationRoute=async route=>{
   const params=new URL(route.request().url()).searchParams;
   if(!params.has('task'))return route.fallback();
   const offset=Number(params.get('offset'));
   if(holdPage&&offset===25){const failure=pageFailure;pageStarted();await new Promise(resolve=>releasePage=resolve);if(failure==='503')return route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({error:'Delayed usage failure'})});if(failure==='json')return route.fulfill({status:200,contentType:'application/json',body:'{invalid'});if(failure==='network')return route.abort('failed');}
   const call={role:'planning',model:'page-'+offset,provider:'fixture',effort:'high',state:'done',created:100,usage_known:true,tokens:1,input_tokens:1,output_tokens:0,cache_read_tokens:0,cache_write_tokens:0,seconds:1,unit:'http_request'};
   await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({roles:[],calls:[call],call_total:30})});
  };
  await page.route('**/api/usage?*',paginationRoute);
  const openDetail=page.getByRole('button',{name:'查看用量明细',exact:true}),dialog=page.locator('#usage-detail');
  const nextPage=dialog.getByRole('button',{name:'下一页',exact:true});
  for(const close of ['button','escape','backdrop']){
   holdPage=false;await openDetail.click();await dialog.locator('.usage-call').filter({hasText:'page-0'}).waitFor();
   if(close==='button'){
    await nextPage.click();await dialog.locator('.usage-call').filter({hasText:'page-25'}).waitFor();
    await dialog.getByRole('button',{name:'上一页',exact:true}).click();await dialog.locator('.usage-call').filter({hasText:'page-0'}).waitFor();
   }
   holdPage=true;const started=new Promise(resolve=>pageStarted=resolve);await nextPage.click();await started;
   if(close==='button')await dialog.locator('.close-dialog').click();
   else if(close==='escape')await page.keyboard.press('Escape');
   else {const box=await dialog.boundingBox();await page.mouse.click(box.x-5,box.y-5);}
   await dialog.waitFor({state:'hidden'});
   await page.waitForFunction(()=>document.activeElement===document.querySelector('#usage-tasks .text-button'));
   const response=page.waitForResponse(response=>new URL(response.url()).searchParams.get('task')&&new URL(response.url()).searchParams.get('offset')==='25');
   releasePage();await response;await page.waitForTimeout(100);
   assert.equal(await dialog.isVisible(),false,close+' must stay closed after pagination');
   assert.equal(await openDetail.evaluate(node=>document.activeElement===node),true,close+' must retain restored focus');
  }
  holdPage=false;await openDetail.click();await dialog.locator('.usage-call').filter({hasText:'page-0'}).waitFor();
  holdPage=true;const startedAgain=new Promise(resolve=>pageStarted=resolve);await nextPage.click();await startedAgain;
  await dialog.locator('.close-dialog').click();await dialog.waitFor({state:'hidden'});
  await openDetail.click();await dialog.locator('.usage-call').filter({hasText:'page-0'}).waitFor();
  const lateResponse=page.waitForResponse(response=>new URL(response.url()).searchParams.get('task')&&new URL(response.url()).searchParams.get('offset')==='25');
  releasePage();await lateResponse;await page.waitForTimeout(100);
  assert.equal(await dialog.locator('.usage-call').filter({hasText:'page-25'}).count(),0,'old pagination must not overwrite a reopened dialog');
  assert.equal(await dialog.isVisible(),true);
  await dialog.locator('.close-dialog').click();
  // 延迟失败也必须遵守相同的生命周期；有效请求的错误仍交给全局提示。
  for(const [failure,reopen] of [['503',false],['503',true],['json',true],['network',true]]){
   await page.evaluate(()=>document.getElementById('toast').hidden=true);
   holdPage=false;await openDetail.click();await dialog.locator('.usage-call').filter({hasText:'page-0'}).waitFor();
   pageFailure=failure;holdPage=true;const started=new Promise(resolve=>pageStarted=resolve);await nextPage.click();await started;
   await dialog.locator('.close-dialog').click();await dialog.waitFor({state:'hidden'});
   if(reopen){await openDetail.click();await dialog.locator('.usage-call').filter({hasText:'page-0'}).waitFor();}
   const finished=failure==='network'?page.waitForEvent('requestfailed',request=>new URL(request.url()).searchParams.get('offset')==='25'):page.waitForResponse(response=>new URL(response.url()).searchParams.get('task')&&new URL(response.url()).searchParams.get('offset')==='25');
   releasePage();await finished;await page.waitForTimeout(150);
   assert.equal(await page.locator('#toast').isHidden(),true,`${failure} from a stale page must not show a toast (reopen=${reopen})`);
   assert.equal(await dialog.isVisible(),reopen);
   if(reopen){assert.equal(await dialog.locator('.usage-call').filter({hasText:'page-0'}).count(),1);await dialog.locator('.close-dialog').click();}
  }
  holdPage=false;await openDetail.click();await dialog.locator('.usage-call').filter({hasText:'page-0'}).waitFor();
  pageFailure='503';holdPage=true;const currentStarted=new Promise(resolve=>pageStarted=resolve);await nextPage.click();await currentStarted;
  releasePage();await page.locator('#toast').filter({hasText:'操作未完成'}).waitFor();
  assert.equal(await dialog.isVisible(),true,'current failure must leave the dialog open');
  await dialog.locator('.close-dialog').click();await page.locator('#toast .toast-close').click();await page.unroute('**/api/usage?*',paginationRoute);
  assert.deepEqual(errors,[]);
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
  // —— 角色汇总：未知用量必须与顶部 summary 同口径标出，全部已知时文本与改动前一致。
  const roleBox=page.locator('#usage-role-summary'),roleValues=async()=>Object.fromEntries(await roleBox.locator('span').evaluateAll(nodes=>nodes.map(node=>[node.querySelector('small').textContent,node.querySelector('strong').textContent])));
  let roleRows=[{role:'planning',tokens:150,unknown_calls:1},{role:'acceptance',tokens:12,unknown_calls:0},{role:'triage',tokens:0,unknown_calls:1}];
  const roleRoute=async route=>route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({summary:{tokens:162,unknown_calls:2,tasks:1,model_seconds:1,cache_read_tokens:0,estimated_usd:null,unpriced_calls:1},daily:[],roles:roleRows,tasks:[],total:0})});
  await page.route('**/api/usage?*',roleRoute);
  try{
   await page.locator('#usage-refresh').click();
   await page.waitForFunction(()=>document.querySelector('#usage-role-summary').textContent.includes('部分未知'));
   assert.deepEqual(await roleValues(),{'规划':'150 + 部分未知','验收设计':'12 tokens','价值初筛':'用量未知'});
   assert.equal(await page.locator('#usage-totals strong').first().textContent(),'162 + 部分未知');
   roleRows=[{role:'planning',tokens:150,unknown_calls:0},{role:'acceptance',tokens:12,unknown_calls:0}];
   await page.locator('#usage-refresh').click();
   await page.waitForFunction(()=>!document.querySelector('#usage-role-summary').textContent.includes('部分未知'));
   assert.deepEqual(await roleValues(),{'规划':'150 tokens','验收设计':'12 tokens'});
  }finally{await page.unroute('**/api/usage?*',roleRoute);}
  console.log('Usage page: real API, empty/filter state, English, mobile and no JS errors passed.');
 }finally{if(browser)await browser.close();server.stdin.end('\n');await new Promise(resolve=>server.once('exit',resolve));}
})().catch(e=>{console.error(e);process.exitCode=1;});
