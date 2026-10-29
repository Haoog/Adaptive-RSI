'use strict';
const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const {createInterface} = require('node:readline');
const {chromium} = require('playwright');

async function main(){
 const server=spawn(process.env.PYTHON||'python3',['-B','-m','tests.browser_fixture'],{stdio:['pipe','pipe','pipe']});
 server.stderr.on('data',chunk=>process.stderr.write(chunk));
 let browser;
 try{
  const url=await new Promise((resolve,reject)=>{const timer=setTimeout(()=>reject(Error('Startup timeout')),30000);createInterface({input:server.stdout}).once('line',line=>{clearTimeout(timer);resolve(line);});server.once('exit',code=>reject(Error('Fixture exit '+code)));});
  browser=await chromium.launch(process.env.RSI_BROWSER_CHANNEL?{channel:process.env.RSI_BROWSER_CHANNEL}:{});
  const page=await browser.newPage();const errors=[];page.on('pageerror',error=>errors.push(error.message));
  await page.goto(url);await page.locator('#project-status').filter({hasText:'已暂停'}).waitFor();

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
  const consent=page.locator('#diagnostic-consent');
  await consent.waitFor();
  const exportButton=page.getByRole('button',{name:'导出诊断包'});
  assert.equal(await exportButton.isDisabled(),true,'diagnostic export must require explicit consent');
  await consent.check();
  assert.equal(await exportButton.isDisabled(),false);
  const exported=await page.evaluate(async()=>{
   const original=window.fetch;let body=null;
   window.fetch=async(url,options)=>{if(url==='/api/diagnostics/export'){body=JSON.parse(options.body);return new Response(JSON.stringify({filename:'adaptive-rsi-diagnostics.json',body:'{"format":1}',bytes:10}),{status:200,headers:{'Content-Type':'application/json'}});}return original(url,options);};
   const click=HTMLAnchorElement.prototype.click;HTMLAnchorElement.prototype.click=function(){window.__diagnosticDownload=this.download;};
   document.querySelector('dialog[open]').querySelectorAll('button').forEach(button=>{if(button.textContent==='导出诊断包')button.click();});
   for(let i=0;i<50&&!window.__diagnosticDownload;i++)await new Promise(resolve=>setTimeout(resolve,20));
   HTMLAnchorElement.prototype.click=click;window.fetch=original;
   return {body,download:window.__diagnosticDownload};
  });
  assert.deepEqual(exported.body,{consent:true});
  assert.equal(exported.download,'adaptive-rsi-diagnostics.json');

  const scopeCases = [
    {kind: 'local', owned: true, expected: '更新本机客户端并先排空自有服务任务。'},
    {kind: 'local', owned: false, expected: '只更新本机客户端，不停止已有本机服务。'},
    {kind: 'remote', owned: false, expected: '只更新本机客户端，远端服务不受影响。'},
  ];
  for (const item of scopeCases) {
    const scopePage = await browser.newPage();
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

  const shutdown=await page.evaluate(async()=>{
   const original=window.fetch;let polls=0;
   window.fetch=async(url,options)=>{if(url==='/api/shutdown-status'){polls++;return new Response(JSON.stringify(polls===1?{phase:'workers',remaining_workers:2,remaining_operations:1}:{phase:'completed',remaining_workers:0,remaining_operations:0}),{status:200});}return original(url,options);};
   window.dispatchEvent(new CustomEvent('rsi-app-shutdown'));
   for(let i=0;i<60&&!document.querySelector('.shutdown-overlay');i++)await new Promise(resolve=>setTimeout(resolve,20));
   const overlay=document.querySelector('.shutdown-overlay');
   for(let i=0;i<60&&!overlay?.textContent.includes('剩余任务');i++)await new Promise(resolve=>setTimeout(resolve,30));
   const first=overlay?.textContent||'';
   for(let i=0;i<60&&!overlay?.textContent.includes('已安全停止');i++)await new Promise(resolve=>setTimeout(resolve,30));
   const final=overlay?.textContent||'';
   window.fetch=original;overlay?.remove();
   return {first,final};
  });
  assert.match(shutdown.first,/等待运行中的任务结束/);
  assert.match(shutdown.first,/剩余任务 2/);
  assert.match(shutdown.final,/已安全停止/);

  assert.deepEqual(errors,[]);
  console.log('browser_desktop_closeout: ok');
 } finally {
  if(browser)await browser.close();
  server.kill('SIGTERM');
 }
}
main().catch(error=>{console.error(error);process.exitCode=1;});
