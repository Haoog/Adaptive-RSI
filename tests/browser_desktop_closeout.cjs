'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const {spawn} = require('node:child_process');
const {fixtureReady, closeFixture, waitForPausedProject} = require('./fixture_browser.cjs');
const {chromium} = require('playwright');

async function checkMachineSwitch(browser,url){
 const page=await browser.newPage();
 page.setDefaultTimeout(8000);
 const errors=[];
 page.on('pageerror',error=>errors.push(error.message));
 const local={kind:'local',connected:true,name:'Local fixture',scope:'http://127.0.0.1:12345'};
 const remote={kind:'remote',connected:true,name:'AWS fixture',scope:'https://rsi.example.com'};
 const tunneled={kind:'remote',connected:true,name:'Tunnel fixture',scope:'ssh:instance:0123456789abcdef',origin:'http://127.0.0.1:6090'};
 await page.addInitScript(({local,remote,tunneled})=>{
  const fetchFixture=window.fetch.bind(window);
  const runtimes={remote,tunneled};
  const generation=()=>Number(sessionStorage.getItem('fixture-generation')||0);
  const target=()=>({...(runtimes[sessionStorage.getItem('fixture-runtime')]||local),connection_generation:generation()});
  window.bridgeCalls=[];
  window.__TAURI_INTERNALS__={invoke:async(command,args)=>{
   window.bridgeCalls.push({command,args});
   if(command==='runtime_info')return target();
   if(command==='native_update')return {state:'idle',current_version:'fixture'};
   if(command==='validate_connection_target'){
    const expected=args.sshTunnel?tunneled.origin:remote.scope;
    return [expected+'/?token=fixture',expected+'/?token=renewed'].includes(args.url);
   }
   if(command==='connect'){
    sessionStorage.setItem('fixture-connect-args',JSON.stringify(args));
    if(args.url==='')return new Promise(()=>{});
    if(!args.url){
     sessionStorage.removeItem('fixture-runtime');
     sessionStorage.setItem('fixture-generation',generation()+1);
     return target();
    }
    await new Promise((resolve,reject)=>{window.finishConnect=resolve;window.failConnect=reject;});
    sessionStorage.setItem('fixture-runtime',args.sshTunnel?'tunneled':'remote');
    sessionStorage.setItem('fixture-generation',generation()+1);
    sessionStorage.removeItem('fixture-session-expired');
    return target();
   }
   if(command==='request'){
    if(args.connectionGeneration!==generation())throw 'Runtime connection changed';
    if(args.path==='task?id=pending')return new Promise(resolve=>{window.finishTaskRequest=()=>resolve({status:200,body:'{}'});});
    if(args.path.startsWith('state?')){
     if(window.fixtureOffline)throw Error('Cannot connect to the selected runtime');
     if(sessionStorage.getItem('fixture-session-expired'))return {status:401,body:'{}'};
    }
    if(args.body!==null)return {status:200,body:JSON.stringify({runtime:target().name})};
    const response=await fetchFixture('/api/'+args.path);
    return {status:response.status,body:await response.text()};
   }
  }};
 },{local,remote,tunneled});
 try{
  await page.goto(url);
  await waitForPausedProject(page);
  await page.getByRole('button',{name:local.name}).click();
  const dialog=page.getByRole('dialog').filter({has:page.getByRole('heading',{name:'运行机器'})});
  const address=dialog.getByLabel('服务器认证地址');
  const connect=dialog.getByRole('button',{name:'连接服务器'});
  const mode=dialog.getByRole('combobox',{name:'服务器连接方式'});
  const connectCalls=()=>page.evaluate(()=>window.bridgeCalls.filter(call=>call.command==='connect').map(call=>call.args));
  const sshAddress=tunneled.origin+'/?token=fixture';
  assert.equal(await mode.inputValue(),'https');
  await page.evaluate(()=>{window.taskOutcome=fetch('/api/task?id=pending').then(reply=>reply.status);});
  await page.waitForFunction(()=>window.finishTaskRequest);
  await address.fill(sshAddress);
  await connect.click();
  await dialog.getByRole('alert').filter({hasText:'请输入 HTTPS 认证地址。'}).waitFor();
  assert.deepEqual(await connectCalls(),[]);
  await address.fill(remote.scope+'/?token=fixture');
  await connect.click();
  await page.waitForFunction(()=>window.failConnect);
  assert.equal(await page.evaluate(()=>{
   const request=window.bridgeCalls.find(call=>call.command==='request'&&call.args.path==='task?id=pending');
   return window.bridgeCalls.some(call=>call.command==='cancel_request'&&call.args.id===request.args.requestId);
  }),false);
  await page.evaluate(()=>window.finishTaskRequest());
  assert.equal(await page.evaluate(()=>window.taskOutcome),200);
  assert.deepEqual(await connectCalls(),[{url:remote.scope+'/?token=fixture'}]);
  await page.keyboard.press('Escape');
  const openAfterEscape=await dialog.isVisible();
  const blocked=await page.evaluate(async()=>{
   const count=path=>window.bridgeCalls.filter(call=>call.command==='request'&&call.args.path===path).length;
   const paths=['state?sections=core','model-center/routes'];
   const before=paths.map(count);
   const attempts=await Promise.allSettled([fetch('/api/'+paths[0]),fetch('/api/'+paths[1],{method:'POST',body:'{}'})]);
   return {outcomes:attempts.map(item=>item.status),forwarded:paths.map((path,index)=>count(path)-before[index])};
  });
  assert.deepEqual({openAfterEscape,...blocked},{openAfterEscape:true,outcomes:['rejected','rejected'],forwarded:[0,0]});
  assert.equal(await dialog.getByRole('button',{name:'关闭'}).isDisabled(),true);
  await page.evaluate(()=>window.failConnect('Missing runtime authentication'));
  await dialog.getByRole('alert').filter({hasText:'Missing runtime authentication'}).waitFor();
  assert.equal(await address.inputValue(),remote.scope+'/?token=fixture');
  assert.equal(await page.evaluate(async()=>(await fetch('/api/state?sections=core')).status),200);
  await connect.click();
  await page.waitForFunction(()=>window.bridgeCalls.filter(call=>call.command==='connect').length===2);
  await Promise.all([page.waitForEvent('load'),page.evaluate(()=>window.finishConnect())]);
  await page.getByRole('button',{name:remote.name}).waitFor();
  assert.equal(await page.evaluate(()=>window.__RSI_MACHINE_KEY__),remote.scope);
  await page.waitForFunction(()=>Boolean(state));
  await page.evaluate(()=>{window.fixtureOffline=true;scheduleRefreshPoll(0);});
  await page.locator('#connection').filter({hasText:'连接中断'}).waitFor();
  await page.evaluate(()=>{window.fixtureOffline=false;return refresh();});
  assert.equal(await page.locator('#connection').textContent(),'已连接');
  const saved=await page.evaluate(async()=>{
   const response=await fetch('/api/model-center/routes',{method:'POST',body:'{}'});
   return (await response.json()).runtime;
  });
  assert.equal(saved,remote.name);
  await page.evaluate(()=>{sessionStorage.setItem('fixture-session-expired','1');return refresh().catch(()=>{});});
  await page.locator('#reload-ui').click();
  await dialog.getByRole('alert').filter({hasText:'登录已失效'}).waitFor();
  await address.fill('   ');
  await connect.click();
  assert.equal(await page.evaluate(()=>window.bridgeCalls.filter(call=>call.command==='connect').length),0,'a blank address must not select the local runtime');
  await address.fill(remote.scope+'/?token=renewed');
  await connect.click();
  await page.waitForFunction(()=>window.finishConnect);
  await Promise.all([page.waitForEvent('load'),page.evaluate(()=>window.finishConnect())]);
  await page.waitForFunction(()=>Boolean(state));
  await page.getByRole('button',{name:remote.name}).click();
  await mode.selectOption({label:'SSH 隧道'});
  assert.equal(await address.getAttribute('placeholder'),'http://127.0.0.1:6090/?token=…');
  await mode.selectOption({label:'HTTPS'});
  assert.equal(await address.getAttribute('placeholder'),'https://rsi.example.com/?token=…');
  await mode.selectOption({label:'SSH 隧道'});
  const rejectedAddresses=[remote.scope,'http://localhost:6090','http://127.1:6090','http://2130706433:6090',
   'http://127.0.0.1.example.com:6090','http://127.0.0.1@rsi.example.com','http://127.0.0.1','http://127.0.0.1:0'];
  for(const invalid of rejectedAddresses){
   await address.fill(invalid+'/?token=fixture');
   await connect.click();
   await dialog.getByRole('alert').filter({hasText:'请输入使用 127.0.0.1 并带端口的 HTTP 认证地址。'}).waitFor();
  }
  assert.deepEqual(await connectCalls(),[]);
  await address.fill(sshAddress);
  await connect.click();
  await page.waitForFunction(()=>window.failConnect);
  assert.deepEqual(await connectCalls(),[{url:sshAddress,sshTunnel:true}]);
  assert.equal(await mode.isDisabled(),true);
  await page.evaluate(()=>window.failConnect('Server connection failed'));
  await dialog.getByRole('alert').filter({hasText:'Server connection failed'}).waitFor();
  assert.equal(await mode.inputValue(),'ssh');
  assert.equal(await mode.isEnabled(),true);
  assert.equal(await address.inputValue(),sshAddress);
  assert.equal(await page.evaluate(()=>window.__RSI_MACHINE_KEY__),remote.scope);
  await connect.click();
  await page.waitForFunction(()=>window.bridgeCalls.filter(call=>call.command==='connect').length===2);
  assert.deepEqual(await connectCalls(),[{url:sshAddress,sshTunnel:true},{url:sshAddress,sshTunnel:true}]);
  await Promise.all([page.waitForEvent('load'),page.evaluate(()=>window.finishConnect())]);
  await page.getByRole('button',{name:tunneled.name}).click();
  assert.equal(await page.evaluate(()=>window.__RSI_MACHINE_KEY__),tunneled.scope);
  assert.equal(await mode.inputValue(),'ssh');
  await Promise.all([page.waitForEvent('load'),dialog.getByRole('button',{name:'使用本机'}).click()]);
  await page.getByRole('button',{name:local.name}).waitFor();
  assert.equal(await page.evaluate(()=>window.__RSI_MACHINE_KEY__),local.scope);
  assert.deepEqual(await page.evaluate(()=>JSON.parse(sessionStorage.getItem('fixture-connect-args'))),{url:null});
  await page.waitForFunction(()=>Boolean(state));
  await page.locator('#language-select').selectOption('en');
  await page.getByRole('button',{name:local.name}).click();
  const englishDialog=page.getByRole('dialog').filter({has:page.getByRole('heading',{name:'Runtime machine'})});
  await englishDialog.getByRole('combobox',{name:'Server connection method'}).selectOption({label:'SSH tunnel'});
  await englishDialog.getByText(/Set up SSH forwarding in Terminal/).waitFor();
  await englishDialog.getByLabel('Server authentication URL').fill('http://localhost:6090/?token=fixture');
  await englishDialog.getByRole('button',{name:'Connect server'}).click();
  await englishDialog.getByRole('alert').filter({hasText:'Enter an HTTP authentication URL with 127.0.0.1 and a port.'}).waitFor();
  assert.deepEqual(await connectCalls(),[]);
  assert.deepEqual(errors,[]);
 }finally{await page.close();}
}

async function main(){
 const server=spawn(process.env.PYTHON||'python3',['-B','-m','tests.browser_fixture'],{stdio:['pipe','pipe','pipe']});
 server.stderr.on('data',chunk=>process.stderr.write(chunk));
 let browser, scenarioError;
 try{
  const url = await fixtureReady(server);
  browser=await chromium.launch(process.env.RSI_BROWSER_CHANNEL?{channel:process.env.RSI_BROWSER_CHANNEL}:{});
  const page=await browser.newPage();const errors=[];page.on('pageerror',error=>errors.push(error.message));
  await page.goto(url);await waitForPausedProject(page);

  const degraded=await page.evaluate(async()=>{
   const original=window.fetch;
   window.fetch=async(url,options)=>url==='/api/contract'?new Response(JSON.stringify({descriptor:{api_version:1,capabilities:[]}}),{status:200}):original(url,options);
   serviceCapabilities=null;serviceContractPromise=null;
   await negotiateCapabilities();
   const notice=$('capability-notice'),shown=!notice.hidden?notice.textContent:'';
   window.fetch=original;serviceCapabilities=null;serviceContractPromise=null;
   await negotiateCapabilities();
   return {shown,hiddenAfterRestore:$('capability-notice').hidden};
  });
  assert.match(degraded.shown,/部分功能不可用/);
  assert.equal(degraded.hiddenAfterRestore,true);
  const capabilityRoutes=await page.evaluate(()=>({
   preview:featureForPath('diagnostics/preview'),
   export:featureForPath('diagnostics/export'),
   shutdown:featureForPath('shutdown-status')
  }));
  assert.deepEqual(capabilityRoutes,{preview:'diagnostics-preview',export:'diagnostics-preview',shutdown:'shutdown-progress'});

  await page.click('#storage-tools');
  await page.locator('dialog[open] h2').filter({hasText:'数据与诊断'}).waitFor();
  const panel = page.locator('dialog[open]');
  const diagnostic = panel.locator('details').filter({hasText:'诊断导出预览'});
  const [previewResponse] = await Promise.all([
   page.waitForResponse(response => new URL(response.url()).pathname === '/api/diagnostics/preview'),
   diagnostic.locator('summary').click(),
  ]);
  assert.equal(previewResponse.ok(), true);
  const preview = await previewResponse.json();
  assert.ok(preview.snapshot);
  const consent = diagnostic.locator('#diagnostic-consent');
  const exportButton = diagnostic.getByRole('button', {name:'导出诊断包'});
  assert.equal(await exportButton.isDisabled(), true, 'diagnostic export must require explicit consent');
  await consent.check();
  assert.equal(await diagnostic.locator('pre').textContent(), preview.body);
  const [exportResponse, download] = await Promise.all([
   page.waitForResponse(response => new URL(response.url()).pathname === '/api/diagnostics/export'),
   page.waitForEvent('download'),
   exportButton.click(),
  ]);
  assert.equal(exportResponse.ok(), true);
  assert.deepEqual(exportResponse.request().postDataJSON(), {consent:true, snapshot:preview.snapshot});
  assert.equal(download.suggestedFilename(), 'adaptive-rsi-diagnostics.json');
  assert.equal(await fs.readFile(await download.path(), 'utf8'), preview.body);

  const scopeCases = [
    {kind: 'local', owned: true, expected: '更新本机客户端并先排空自有服务任务。'},
    {kind: 'local', owned: false, expected: '只更新本机客户端，不停止已有本机服务。'},
    {kind: 'remote', owned: false, expected: '只更新本机客户端，远端服务不受影响。'},
  ];
  for (const item of scopeCases) {
    const scopePage = await browser.newPage();
    scopePage.on('pageerror', error => errors.push(item.kind + '/' + item.owned + ': ' + (error.stack || error.message)));
    await scopePage.addInitScript(info => {
      window.RSIDesktop = {
        ready: Promise.resolve(info),
        invoke: async () => ({state: 'idle', current_version: '0.5.127'}),
      };
    }, {kind: item.kind, owned: item.owned, connected: true, name: 'fixture'});
    await scopePage.goto(url);
    await scopePage.locator('#native-update-panel').waitFor({state:'attached'});
    const text = await scopePage.locator('#native-update-panel').textContent();
    assert.ok(text.includes(item.expected),
      'update scope for ' + item.kind + '/' + item.owned);
    await scopePage.close();
  }

  const receiptPage = await browser.newPage();
  receiptPage.on('pageerror', error => errors.push(error.message));
  await receiptPage.addInitScript(() => {
    window.__receiptBlobs = [];
    window.__receiptCalls = [];
    const create = URL.createObjectURL.bind(URL);
    URL.createObjectURL = blob => { window.__receiptBlobs.push(blob); return create(blob); };
    window.RSIDesktop = {
      ready: Promise.resolve({kind: 'local', owned: true}),
      invoke: async (command, args) => {
        window.__receiptCalls.push(args || {});
        if (args && args.action === 'receipt') {
          return {available: true, receipt: {
            receipt_version: 1, operation_id: 'fixture-operation', from_version: '0.5.126',
            to_version: '0.5.127', artifact_sha256: 'a'.repeat(64), signature_sha256: 'b'.repeat(64),
            signature_verified: true, state: 'succeeded', error_code: null,
          }};
        }
        return {state: 'ready', current_version: '0.5.126', version: '0.5.127', authorization_revision: 1};
      },
    };
  });
  await receiptPage.goto(url);
  // The update panel lives in the collapsed advanced section of the settings view.
  await receiptPage.waitForFunction(() => Boolean(state));
  await receiptPage.evaluate(() => { showView('settings'); document.getElementById('advanced-settings').open = true; });
  await receiptPage.locator('#view-settings').waitFor({state: 'visible'});
  await receiptPage.locator('#native-update-panel').getByRole('button', {name: '导出升级回执'}).click();
  await receiptPage.waitForFunction(() => window.__receiptBlobs.length > 0);
  const upgradeReceipt = await receiptPage.evaluate(async () => {
    const text = await window.__receiptBlobs[0].text();
    return {receipt: JSON.parse(text), calls: window.__receiptCalls.filter(args => args.action === 'receipt')};
  });
  assert.equal(upgradeReceipt.calls.length, 1, 'the receipt button must read the persisted transaction');
  assert.equal(upgradeReceipt.receipt.receipt_version, 2);
  assert.equal(upgradeReceipt.receipt.transaction.operation_id, 'fixture-operation');
  assert.equal(upgradeReceipt.receipt.transaction.state, 'succeeded');
  assert.equal(upgradeReceipt.receipt.transaction_error, null);
  await receiptPage.close();

  await page.evaluate(() => {
   const fetchOriginal = window.fetch;
   window.shutdownHttpPolls = 0;
   window.fetch = (url, options) => {
    if (url === '/api/shutdown-status') window.shutdownHttpPolls++;
    return fetchOriginal(url, options);
   };
   window.RSIDesktop = {
    ready: Promise.resolve({kind: 'remote', connected: true}),
    invoke: async command => {
     if (command === 'quit_progress') return window.childStopped ? 'stopped' : 'stopping';
    },
   };
   window.dispatchEvent(new CustomEvent('rsi-app-shutdown'));
  });
  const shutdown = page.locator('.shutdown-overlay[role="status"]');
  await shutdown.getByText('正在关闭本机服务', {exact: true}).waitFor();
  await page.evaluate(() => { window.childStopped = true; });
  await shutdown.getByText('本机服务已停止，正在退出应用。', {exact: true}).waitFor();
  assert.equal(await page.evaluate(() => window.shutdownHttpPolls), 0);

  assert.deepEqual(errors,[]);
  await checkMachineSwitch(browser,url);
  console.log('browser_desktop_closeout: ok');
 } catch (error) {
  scenarioError = error;
  throw error;
 } finally {
  await closeFixture(server, browser, scenarioError);
 }
}
main().catch(error=>{console.error(error);process.exitCode=1;});
