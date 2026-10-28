const assert=require('node:assert/strict');
const {spawn}=require('node:child_process');
const {createInterface}=require('node:readline');
const {chromium}=require('playwright');
(async()=>{
 const server=spawn(process.env.PYTHON||'python3',['-m','tests.browser_fixture'],{stdio:['pipe','pipe','pipe'],env:{...process.env,PYTHONDONTWRITEBYTECODE:'1'}});
 let browser;
 try{
  const url=await new Promise((resolve,reject)=>{const timer=setTimeout(()=>reject(Error('startup timeout')),30000);createInterface({input:server.stdout}).once('line',line=>{clearTimeout(timer);resolve(line);});});
  browser=await chromium.launch(process.env.RSI_BROWSER_CHANNEL?{channel:process.env.RSI_BROWSER_CHANNEL}:{});
  const page=await browser.newPage();await page.goto(url);
  await page.locator('#project-status').filter({hasText:'已暂停'}).waitFor();
  for(const title of ['规划一','规划二']){await page.locator('#goal-text').fill(title);await page.locator('#goal-form button[type=submit]').click();await page.getByRole('checkbox',{name:'选择任务 · '+title}).check();}
  await page.getByRole('button',{name:'推荐任务簇',exact:true}).click();
  await page.locator('#toast').filter({hasText:'没有可推荐的任务簇'}).waitFor();
  await page.getByRole('button',{name:'合并任务',exact:true}).click();
  const confirm=page.locator('dialog.confirm-dialog[open]');
  assert.match(await confirm.innerText(),/共享检查收益: 未知/);
  await confirm.getByRole('button',{name:'取消',exact:true}).click();
  const state=await page.evaluate(async()=> (await fetch('/api/state?filter=all&sections=core')).json());
  assert.equal(state.tasks.filter(task=>['规划一','规划二'].includes(task.title)&&task.state==='queued').length,2);
  await page.getByRole('button',{name:'合并任务',exact:true}).click();await confirm.getByRole('button',{name:'确认',exact:true}).click();
  await page.getByRole('button',{name:'逐条验收证据',exact:true}).click();
  const evidence=page.locator('dialog.experience-dialog[open]');
  await evidence.getByText('实际完整检查耗时 · 未知',{exact:true}).waitFor();
  console.log('Batch planning cancellation, unknown costs and evidence browser checks passed; no model calls.');
 }finally{if(browser)await browser.close();server.stdin.end('\n');await new Promise(resolve=>server.once('exit',resolve));}
})().catch(error=>{console.error(error);process.exitCode=1;});
