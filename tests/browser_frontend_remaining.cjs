const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const {createInterface} = require('node:readline');
const {chromium} = require('playwright');

async function main(){
 const server=spawn(process.env.PYTHON||'python3',['-B','-m','tests.browser_fixture'],{stdio:['pipe','pipe','pipe']});
 server.stderr.on('data',chunk=>process.stderr.write(chunk));
 let browser;
 try{
  const url=await new Promise((resolve,reject)=>{const timer=setTimeout(()=>reject(Error('Startup timeout')),30000);createInterface({input:server.stdout}).once('line',line=>{clearTimeout(timer);resolve(line);});server.once('exit',code=>{clearTimeout(timer);reject(Error('Fixture exit '+code));});});
  browser=await chromium.launch(process.env.RSI_BROWSER_CHANNEL?{channel:process.env.RSI_BROWSER_CHANNEL}:{});
  const page=await browser.newPage();const errors=[];page.on('pageerror',error=>errors.push(error.message));
  await page.goto(url);await page.locator('#project-status').filter({hasText:'已暂停'}).waitFor();
  assert.equal(await page.evaluate(()=>typeof featureForPath),'function','client negotiates capabilities by feature');
  const negotiation=await page.evaluate(async()=>{
   const original=window.fetch;let blockedPosts=0;
   window.fetch=async(url,options)=>{if(url==='/api/contract')return new Response(JSON.stringify({descriptor:{api_version:1,capabilities:[]}}),{status:200});if(options?.method==='POST')blockedPosts++;return original(url,options);};
   serviceContractPromise=null;serviceCapabilities=null;
   const basic=await api('state?limit=1');let error;
   try{await api('task-batch/preview',{project:projectId});}catch(e){error=e.message;}
   window.fetch=original;serviceContractPromise=null;serviceCapabilities=null;await negotiateCapabilities();
   $('saved-view-controls')?.remove();listSnapshots.delete($('tasks-list'));render();
   return {basic:!!basic.projects,error,blockedPosts};
  });
  assert.equal(negotiation.basic,true);assert.equal(negotiation.blockedPosts,0);assert.match(negotiation.error,/不支持/);
  const logs=await page.evaluate(async()=>{
   const text=Array.from({length:20000},(_,i)=>`2026-10-28T01:02:03Z [evaluation] line ${i} ${i===19000?'needle':''}`).join('\n');
   const box=lazyEvidence('大日志',()=>text);document.body.append(box);
   const collapsed=box.querySelector('pre')?.textContent||'';box.open=true;await new Promise(resolve=>setTimeout(resolve,50));
   const first=box.querySelector('pre').textContent;const search=box.querySelector('input[type=search]');search.value='needle';search.dispatchEvent(new Event('input'));await new Promise(resolve=>setTimeout(resolve,100));
   const found=box.querySelector('pre').textContent;const pages=box.querySelector('[data-log-status]').textContent;
   box.remove();return {collapsed,first,found,pages};
  });
  assert.equal(logs.collapsed,'');assert.ok(logs.first.length<40000);assert.match(logs.found,/line 19000 needle/);assert.match(logs.pages,/20000|行/);
  const preferences=await page.evaluate(()=>{
   window.__RSI_MACHINE_KEY__='https://one.example';localWrite('rsi-project','one');
   window.__RSI_MACHINE_KEY__='https://two.example';localWrite('rsi-project','two');const two=localRead('rsi-project');
   window.__RSI_MACHINE_KEY__='https://one.example';const one=localRead('rsi-project');delete window.__RSI_MACHINE_KEY__;return {one,two};
  });assert.deepEqual(preferences,{one:'one',two:'two'});
  const restoredRuntime=await page.evaluate(()=>{const project=projectId;window.__RSI_MACHINE_KEY__='runtime-a';localWrite('rsi-project',project);localWrite('rsi-navigation',JSON.stringify({view:'settings',filter:'history',system:true}));window.__RSI_MACHINE_KEY__='runtime-b';localWrite('rsi-project','unavailable');localWrite('rsi-navigation',JSON.stringify({view:'usage',filter:'active',system:false}));restoreRuntimeNavigation();const b=view;window.__RSI_MACHINE_KEY__='runtime-a';restoreRuntimeNavigation();const a={project:projectId,view,filter:$('task-filter').value,system:$('show-system-tasks').checked};delete window.__RSI_MACHINE_KEY__;projectId=project;view='overview';$('task-filter').value='active';$('show-system-tasks').checked=false;render();return {a,b,project};});assert.equal(restoredRuntime.a.project,restoredRuntime.project);assert.equal(restoredRuntime.a.view,'settings');assert.equal(restoredRuntime.a.filter,'history');assert.equal(restoredRuntime.b,'usage');
  const focus=await page.evaluate(()=>{
   const list=$('tasks-list'),button=list.querySelector('button');button.focus();const task=button.closest('.task-row')?.dataset.taskId;
   const original=scoped(state.tasks).find(row=>row.id===task);if(original)original.updated++;
   renderTasks();return {task,active:document.activeElement.closest('.task-row')?.dataset.taskId,live:list.getAttribute('aria-live')};
  });assert.ok(focus.task);assert.equal(focus.active,focus.task);assert.equal(focus.live,'off');
  const gone=await page.evaluate(()=>{const button=$('tasks-list').querySelector('button');button.focus();const id=button.closest('.task-row').dataset.taskId;state.tasks=state.tasks.filter(task=>task.id!==id);renderTasks();return document.activeElement.closest('.task-row')?.dataset.taskId||document.activeElement.id;});assert.ok(gone);
  await page.evaluate(()=>{const box=lazyEvidence('来源与证据',()=> 'original\\n'.repeat(30000));box.id='export-log-test';document.body.append(box);box.open=true;});
  const downloadPromise=page.waitForEvent('download');await page.locator('#export-log-test').getByRole('button',{name:'导出完整原文'}).click();const download=await downloadPromise;const stream=await download.createReadStream();const chunks=[];for await(const chunk of stream)chunks.push(chunk);assert.equal(Buffer.concat(chunks).toString(),'original\\n'.repeat(30000));await page.locator('#export-log-test').evaluate(node=>node.remove());
  const benchmark=await page.evaluate(async()=>{
   const text=('2026-10-28T00:00:00Z [evaluation] '+ 'x'.repeat(160)+'\n').repeat(100000);let serializations=0;
   const start=performance.now(),box=lazyEvidence('来源与证据',()=>{serializations++;return text;});document.body.append(box);const collapsedMs=performance.now()-start,collapsedChars=box.textContent.length,collapsedCalls=serializations;
   box.open=true;await new Promise(resolve=>setTimeout(resolve,50));const rendered=box.querySelector('pre').textContent.length;
   const input=box.querySelector('input');input.value='missing-token';const searchStart=performance.now();input.dispatchEvent(new Event('input'));
   while(!box.querySelector('[data-log-status]').textContent.includes('未找到'))await new Promise(resolve=>setTimeout(resolve,10));
   const searchMs=performance.now()-searchStart;box.remove();return {bytes:text.length,collapsedMs,collapsedChars,collapsedCalls,rendered,searchMs};
  });assert.equal(benchmark.collapsedCalls,0);assert.equal(benchmark.rendered,16000);assert.ok(benchmark.searchMs<5000);console.log('Evidence measurement '+JSON.stringify(benchmark));
  const transfer=await page.evaluate(()=>{const original=view;const text=exportSavedView();view='settings';importSavedView(text);const restored=view;let rejected=false;try{importSavedView(JSON.stringify({version:1,view:'overview',project:'unknown',filter:'all',system:false}));}catch{rejected=true;}return {original,restored,rejected,text};});
  assert.equal(transfer.original,transfer.restored);assert.equal(transfer.rejected,true);assert.ok(!transfer.text.includes('csrf'));
  const second=await browser.newPage();await second.goto(url);await second.locator('#project-status').filter({hasText:'已暂停'}).waitFor();
  assert.equal(await second.evaluate(text=>{importSavedView(text);return view;},transfer.text),transfer.original);await second.close();
  await page.locator('#language-select').selectOption('en');await page.locator('#saved-view-controls > summary').filter({hasText:'Restore a view in another browser'}).waitFor();assert.equal(await page.locator('#saved-view-controls input').getAttribute('aria-label'),'Import view file');
  await page.locator('#language-select').selectOption('zh-CN');
  assert.deepEqual(errors,[]);
  console.log('Feature negotiation and lazy paginated searchable evidence passed.');
 }finally{await browser?.close();server.stdin.end('\n');await new Promise(resolve=>server.once('exit',resolve));}
}
main().catch(error=>{console.error(error);process.exitCode=1;});
