'use strict';
const assert=require('node:assert/strict');
const {spawn}=require('node:child_process');
const {createInterface}=require('node:readline');
const {chromium}=require('playwright');
(async()=>{
 const server=spawn(process.env.PYTHON||'python3',['-B','-m','tests.browser_fixture'],{stdio:['pipe','pipe','pipe']});
 server.stderr.on('data',chunk=>process.stderr.write(chunk));let browser;
 try{
  const url=await new Promise((resolve,reject)=>{const timer=setTimeout(()=>reject(Error('Fixture startup timeout')),30000);createInterface({input:server.stdout}).once('line',line=>{clearTimeout(timer);resolve(line);});server.once('exit',code=>{clearTimeout(timer);reject(Error('Fixture exited '+code));});});
  browser=await chromium.launch(process.env.RSI_BROWSER_CHANNEL?{channel:process.env.RSI_BROWSER_CHANNEL}:{});
  for(const choice of ['使用本地修改','采用服务端值']){
   const page=await browser.newPage(),errors=[];page.on('pageerror',error=>errors.push(error.message));
   await page.goto(url);await page.locator('#project-status').filter({hasText:'已暂停'}).waitFor();
   await page.evaluate(async()=>{await api('config',{project:projectId,revision:state.config_revision,values:{schedule:{weight:1}}});});
   await page.reload();await page.locator('#project-status').filter({hasText:'已暂停'}).waitFor();await page.locator('#project-settings').click();await page.locator('#advanced-settings > summary').click();
   const weight=page.locator('[data-path="schedule.weight"]');await weight.fill('3');await weight.blur();
   await page.evaluate(async()=>{await api('config',{project:projectId,revision:state.config_revision,values:{schedule:{weight:2}}});});
   await page.reload();await page.locator('#project-status').filter({hasText:'已暂停'}).waitFor();await page.locator('#project-settings').click();
   const panel=page.locator('#settings-conflicts');await panel.waitFor();assert.match(await panel.innerText(),/schedule.weight/);assert.match(await panel.innerText(),/服务端值：2/);
   // Conflict copies must survive another reload even when no ordinary draft remains.
   await page.reload();await page.locator('#project-status').filter({hasText:'已暂停'}).waitFor();await page.locator('#project-settings').click();await panel.waitFor();
   await page.locator('#advanced-settings > summary').click();await weight.fill('4');
   await panel.getByRole('button',{name:choice,exact:true}).click();
   assert.equal(await weight.inputValue(),choice==='使用本地修改'?'3':'2');
   await page.locator('#settings-save').click();await page.locator('#settings-save:not([disabled])').waitFor();
   assert.equal(await page.evaluate(()=>currentProject().config.schedule.weight),choice==='使用本地修改'?3:2);
   assert.equal(await page.evaluate(()=>window.RSIDraftStore.get('settings-conflicts',settingsKey())),null);
   assert.deepEqual(errors,[]);await page.close();
  }
  console.log('Settings conflict browser passed: both versions survive reload, per-field local/server resolution, ordinary edits and revision-checked saves.');
 }finally{
  if(browser)await browser.close();
  if(server.exitCode===null){const ended=new Promise(resolve=>server.once('exit',resolve));server.stdin.end('\n');const timer=setTimeout(()=>server.kill('SIGTERM'),5000);await ended;clearTimeout(timer);}
 }
})().catch(error=>{console.error(error);process.exitCode=1;});
