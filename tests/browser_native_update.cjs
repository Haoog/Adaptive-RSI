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
  const page=await browser.newPage(),errors=[];page.on('pageerror',error=>errors.push(error.message));
  await page.addInitScript(()=>{
   localStorage.setItem('rsi-native-update-policy','manual');
   window.RSIDesktop={invoke:async(command,args)=>command==='native_update'?{state:'ready',version:'0.5.125',current_version:'0.5.124',notes:'Fixture release notes'}:{}};
  });
  await page.goto(url);await page.locator('#project-status').filter({hasText:'已暂停'}).waitFor();
  await page.locator('#project-settings').click();await page.locator('#advanced-settings > summary').click();
  const policy=page.getByRole('combobox',{name:'桌面更新策略',exact:true});await policy.waitFor();
  await policy.selectOption('manual');await policy.focus();
  await policy.evaluate(node=>{node.fixtureIdentity='preserved';});
  await page.locator('#native-update-panel details summary').click();
  await policy.focus();await page.evaluate(()=>window.RSINativeUpdate.tick());
  assert.equal(await policy.evaluate(node=>node.fixtureIdentity),'preserved');
  assert.equal(await policy.evaluate(node=>document.activeElement===node),true);
  assert.equal(await page.locator('#native-update-panel details').evaluate(node=>node.open),true);
  await page.getByRole('combobox',{name:'自动更新版本范围',exact:true}).selectOption('minor');
  await page.getByRole('combobox',{name:'自动安装时间',exact:true}).selectOption('night');
  assert.equal(await page.evaluate(()=>localStorage.getItem('rsi-native-update-scope')),'minor');
  assert.equal(await page.evaluate(()=>localStorage.getItem('rsi-native-update-window')),'night');
  await page.evaluate(()=>{const original=Storage.prototype.setItem;Storage.prototype.setItem=function(key,value){if(key.startsWith('rsi-native-update-'))throw Error('Fixture storage unavailable');return original.call(this,key,value);};});
  await policy.selectOption('download');await page.locator('#native-update-panel').getByText('偏好未保存到磁盘，本次会话已生效。',{exact:true}).waitFor();
  assert.equal(await policy.inputValue(),'download');
  await page.locator('#language-select').selectOption('en');
  await page.getByRole('combobox',{name:'Automatic update version range',exact:true}).waitFor();
  assert.deepEqual(errors,[]);
  console.log('Native update browser passed: persistent DOM/focus/notes, version scope, install window, storage failure and bilingual controls; native IPC is a fixture, no real update installed.');
 }finally{
  if(browser)await browser.close();
  if(server.exitCode===null){const ended=new Promise(resolve=>server.once('exit',resolve));server.stdin.end('\n');const timer=setTimeout(()=>server.kill('SIGTERM'),5000);await ended;clearTimeout(timer);}
 }
})().catch(error=>{console.error(error);process.exitCode=1;});
