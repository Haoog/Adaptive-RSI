const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
function load() {
 const context = {window:{},Map,Set,t:value=>value,document:{querySelector:()=>null}};
 vm.createContext(context);
 vm.runInContext(fs.readFileSync('adaptive_rsi/web/batch.js','utf8'),context);
 return context.window.RSIBatch;
}
function element(tag,text,cls){
 const node={tag,className:cls,textContent:text===undefined?'':text,children:[],dataset:{},listeners:{},disabled:false,open:false,id:'',
  append(...items){node.children.push(...items);},
  replaceChildren(...items){node.children=items;},
  setAttribute(){},remove(){},focus(){},showModal(){node.open=true;},close(){node.open=false;node.onclose?.();},
  addEventListener(type,fn){(node.listeners[type]??=[]).push(fn);},
  dispatch(type,extra){let result;for(const fn of node.listeners[type]||[])result=fn({type,target:node,...extra});return result;},
  click(){if(!node.disabled)return node.dispatch('click');}};
 return node;
}
// Drives the real toolbar entry point so the regression covers the user path, not a private helper.
function harness(api,{confirm=true,clickConfirm=false}={}){
 const drafts=new Map(),calls=[],toasts=[],confirms=[],refreshes=[],rows={};
 let panel=null,dialog=null,requestNumber=0;
 const host=element('div');host.after=node=>{panel=node;};
 const context={window:{},Map,Set,JSON,crypto:{randomUUID:()=>++requestNumber===1?'request-new':'request-new-'+requestNumber},console,projectId:'project',navigationSequence:0,t:value=>value,
  statusNames:{queued:'排队'},reviewNames:{},el:element,
  toast:(message,error=false)=>toasts.push({message,error}),
  uiConfirm:async message=>{confirms.push(message);return clickConfirm?confirmDialog(message):confirm;},
  refresh:async options=>{refreshes.push(options);},
  safe:async fn=>{try{await fn();}catch(error){context.toast(error.message,true);}},
  api:async(path,body)=>{calls.push({path,body});return api(path,body);},
  document:{querySelector:selector=>selector==='.task-toolbar'?host:null,getElementById:id=>id==='batch-actions'?panel:null,body:{append(node){dialog=node;}}}};
 const app=fs.readFileSync('adaptive_rsi/web/app.js','utf8');
 vm.createContext(context);
 const confirmDialog=vm.runInContext('('+app.slice(app.indexOf('function uiConfirm('),app.indexOf('function elapsedCopy('))+')',context);
 if(clickConfirm){
  context.structuredClone=structuredClone;
  context.sessionStorage={getItem:key=>drafts.get(key)??null,setItem:(key,value)=>drafts.set(key,value)};
  vm.runInContext(app.slice(app.indexOf('const draftStore='),app.indexOf('const configReasonLabels=')),context);
 }else{
  context.window.RSIDraftStore={get:(kind,scope)=>drafts.get(kind+':'+scope)??null,set:(kind,scope,value)=>drafts.set(kind+':'+scope,value),remove:(kind,scope)=>drafts.delete(kind+':'+scope)};
 }
 vm.runInContext(fs.readFileSync('adaptive_rsi/web/batch.js','utf8'),context);
 const checkbox=context.window.RSIBatch.row({id:'t1',title:'任务一',fingerprint:''});rows.t1=checkbox;
 const button=()=>panel.children.find(node=>node.textContent==='批量重试');
 const render=tasks=>{const list=tasks.map(task=>typeof task==='string'?{id:task}:task);context.window.RSIBatch.toolbar(list);for(const task of list)rows[task.id]=context.window.RSIBatch.row({id:task.id,title:'任务 '+task.id,fingerprint:''});};
 // Mirror a real checkbox click: the browser toggles checked before dispatching click.
 const check=(id,shiftKey=false)=>{const box=rows[id];box.checked=!box.checked;box.dispatch('click',{shiftKey});};
 return {calls,toasts,confirms,refreshes,drafts,store:context.window.RSIDraftStore,rows,panel,dialog:()=>dialog,
  project(value){context.projectId=value;context.navigationSequence++;},
  machine(value){context.window.__RSI_MACHINE_KEY__=value;},
  navigate(){context.navigationSequence++;},
  confirm(){dialog.children.at(-1).children[1].click();},
  batch:context.window.RSIBatch,button,render,check,count:()=>panel.children[0].textContent,
  buttonByText:text=>panel.children.find(node=>node.textContent===text),click:()=>button().click()};
}
function previewFor(ids){return {tasks:ids.map(id=>({id,title:'任务 '+id,state:'queued'})),conflicts:[],unknown_scope:[],originals:[],revision:'rev-1'};}
function draft(signature='["retry",["t1"]]'){return {signature,id:'request-1',tasks:['t1'],action:'retry'};}
const preview={tasks:[{id:'t1',title:'任务一',state:'queued'}],conflicts:[],unknown_scope:[],originals:[],revision:'rev-1'};
test('cost preview keeps unknown costs explicit without promising speedup',()=>{
 const batch=load();
 assert.match(batch.costText({}), /共享检查收益: 未知/);
 assert.match(batch.costText({separate_seconds:12}), /分别验证: 12.0 s/);
 assert.match(batch.costText({}), /不保证加速/);
});
test('evidence rendering is available to task detail',()=>{
 assert.equal(typeof load().evidence,'function');
});
test('a transient receipt lookup keeps the confirmed batch retry and reuses its request ID',async()=>{
 const writes=[];
 const fixture=harness(async(path,body)=>{
  if(body===undefined)throw new TypeError('Failed to fetch');
  if(body.preview)return preview;
  writes.push(body);
  return {complete:true,receipts:[{id:'t1',state:'accepted'}]};
 });
 fixture.drafts.set('batch:project',draft());
 fixture.check('t1');
 await fixture.click();
 assert.equal(fixture.confirms.length,1,'the confirmed operation continues after the receipt lookup fails');
 assert.match(fixture.confirms[0],/批次回执暂时无法核对/);
 assert.equal(writes.length,1);
 assert.equal(writes[0].request_id,'request-1','the stored request ID is reused so the server reconciles instead of repeating');
 assert.equal(fixture.drafts.size,0);
 assert.match(fixture.toasts.at(-1).message,/批量操作已完成/);
});
test('a declined confirmation after a failed receipt lookup keeps the draft for retry',async()=>{
 const writes=[];
 const fixture=harness(async(path,body)=>{
  if(body===undefined)throw new TypeError('Failed to fetch');
  if(body.preview)return preview;
  writes.push(body);
  return {complete:true,receipts:[{id:'t1',state:'accepted'}]};
 },{confirm:false});
 fixture.drafts.set('batch:project',draft());
 fixture.check('t1');
 await fixture.click();
 assert.equal(fixture.confirms.length,1);
 assert.equal(writes.length,0,'declining the confirmation sends no write');
 assert.equal(fixture.drafts.get('batch:project').id,'request-1','the request ID stays available for the next attempt');
 assert.equal(fixture.button().disabled,false,'pending state is released so the user can retry');
});
test('a transient batch write reports a retry hint instead of dropping the request ID',async()=>{
 const fixture=harness(async(path,body)=>{
  if(body===undefined)return {receipt:null};
  if(body.preview)return preview;
  throw new TypeError('Failed to fetch');
 });
 fixture.drafts.set('batch:project',draft());
 fixture.check('t1');
 await fixture.click();
 const toast=fixture.toasts.at(-1);
 assert.match(toast.message,/结果待核对/);
 assert.match(toast.message,/不会重复操作/);
 assert.equal(toast.error,true);
 assert.equal(fixture.drafts.get('batch:project').id,'request-1');
 assert.equal(fixture.button().disabled,false);
});
test('shift-click selects a contiguous page range and the request carries exactly those tasks',async()=>{
 const ids=['t1','t2','t3','t4','t5'];
 const fixture=harness(async(path,body)=>{
  if(body===undefined)throw new TypeError('Failed to fetch');
  if(body.preview)return previewFor(ids);
  return {complete:true,receipts:ids.map(id=>({id,state:'accepted'}))};
 });
 fixture.render(ids);
 fixture.check('t1');
 fixture.check('t5',true);
 assert.match(fixture.count(),/5 \/ 5/,'the range covers every row between the two clicks');
 await fixture.click();
 const write=fixture.calls.find(call=>call.body&&!call.body.preview);
 assert.deepEqual(Array.from(write.body.tasks),ids,'the request carries the visible selection only');
});
test('shift-click selects ranges in either direction and unchecks a checked range',()=>{
 const ids=['t1','t2','t3','t4','t5'];
 const fixture=harness(async()=>({complete:true,receipts:[]}));
 fixture.render(ids);
 fixture.check('t4');
 fixture.check('t2',true);
 assert.match(fixture.count(),/3 \/ 5/,'the range covers t2..t4 regardless of direction');
 fixture.check('t4');
 fixture.check('t2',true);
 assert.match(fixture.count(),/0 \/ 5/,'shift-clicking a checked row clears the same range');
});
test('select all covers the visible page and clear resets without a request',()=>{
 const fixture=harness(async()=>{throw Error('select all and clear must not call the API');});
 fixture.render(['t1','t2','t3']);
 fixture.buttonByText('全选当前页').click();
 assert.match(fixture.count(),/3 \/ 3/);
 assert.equal(fixture.buttonByText('全选当前页').disabled,true,'nothing is left to select on this page');
 fixture.buttonByText('清除选择').click();
 assert.match(fixture.count(),/0 \/ 3/);
 assert.equal(fixture.calls.length,0);
});
test('changing the filter or page drops selections that are no longer visible',async()=>{
 const fixture=harness(async(path,body)=>{
  if(body===undefined)throw new TypeError('Failed to fetch');
  if(body.preview)return previewFor(['t1']);
  return {complete:true,receipts:[{id:'t1',state:'accepted'}]};
 });
 fixture.render(['t1','t2','t3']);
 fixture.check('t1');fixture.check('t3');
 assert.match(fixture.count(),/2 \/ 3/);
 fixture.render(['t1','t2']);
 assert.match(fixture.count(),/1 \/ 2/,'t3 left the page and must not stay selected');
 await fixture.click();
 const write=fixture.calls.find(call=>call.body&&!call.body.preview);
 assert.deepEqual(Array.from(write.body.tasks),['t1']);
});

test('later batch operations retain separate unresolved receipt IDs', async () => {
 const fixture=harness(async (_path,body)=>{
  if(!body)throw new TypeError('Receipt unavailable');
  if(body.preview)return previewFor(body.tasks);
  if(body.tasks[0]==='t1')throw new TypeError('Reply interrupted');
  return {complete:body.tasks[0]==='t3',receipts:[]};
 });
 fixture.drafts.set('batch:project',draft());
 fixture.render(['t1','t2','t3']);
 fixture.check('t2');await fixture.click();
 fixture.check('t3');await fixture.click();
 assert.deepEqual(Array.from(fixture.drafts.get('batch:project'),entry=>entry.id),['request-1','request-new']);
 fixture.check('t1');await fixture.click();
 assert.equal(fixture.calls.filter(call=>call.body&&!call.body.preview).at(-1).body.request_id,'request-1');
});

function deferred(){
 let resolve,reject;
 const promise=new Promise((accept,decline)=>{resolve=accept;reject=decline;});
 return {promise,resolve,reject};
}
const completedBatch={complete:true,receipts:[{id:'t1',state:'accepted'}]};
const executionCount=fixture=>fixture.calls.filter(({body})=>body?.preview===false).length;

for(const [stage,switchTo] of [
 ['preview',fixture=>fixture.project('other')],
 ['preview error',fixture=>fixture.machine('other')],
 ['receipt lookup',fixture=>fixture.machine('other')],
 ['failed receipt lookup',fixture=>{fixture.project('other');fixture.project('project');}],
 ['project confirmation',fixture=>fixture.project('other')],
 ['runtime confirmation',fixture=>fixture.machine('other')],
 ['round trip confirmation',fixture=>{fixture.navigate();fixture.navigate();}],
]){
 test(`batch ownership blocks stale ${stage}`,async()=>{
  const response=deferred(),previewStarted=deferred();
  const lookup=stage.includes('lookup'),confirming=stage.includes('confirmation');
  const fixture=harness((_path,body)=>{
   if(!body)return response.promise;
   if(!body.preview)return completedBatch;
   previewStarted.resolve();
   return confirming||lookup?preview:response.promise;
  },{clickConfirm:true});
  if(lookup)fixture.store.set('batch','project',draft());
  fixture.check('t1');
  const pending=fixture.click();
  if(confirming){await previewStarted.promise;await new Promise(setImmediate);assert.equal(fixture.dialog()?.open,true);}
  switchTo(fixture);
  if(stage.includes('error')||stage.startsWith('failed'))response.reject(new TypeError('Old connection lost'));
  else response.resolve(lookup?{receipt:{...preview,complete:false}}:preview);
  if(!confirming)await new Promise(setImmediate);
  // Confirm a stale dialog if the old implementation opened one, so the test also counts writes.
  if(fixture.dialog()?.open)fixture.confirm();
  await pending;
  assert.deepEqual([fixture.confirms.length,executionCount(fixture),fixture.toasts.length,fixture.refreshes.length],
   [confirming?1:0,0,0,0],'counts of confirmations, execution POSTs, toasts and refreshes');
  if(lookup)assert.equal(fixture.calls.length,1,'an old receipt lookup never starts a new preview');
 });
}

for(const stage of ['complete receipt lookup','successful execution','failed execution']){
 test(`batch ownership keeps ${stage} in the original runtime`,async()=>{
  const response=deferred(),started=deferred(),lookup=stage.includes('lookup'),failed=stage.startsWith('failed');
  const fixture=harness((_path,body)=>{
   if(body?.preview)return preview;
   started.resolve();return response.promise;
  },{clickConfirm:true});
  fixture.machine('original');
  fixture.store.set('batch','project',draft());
  fixture.render(['t1']);
  fixture.check('t1');
  let pending;
  if(lookup){pending=fixture.click();}
  else{
   // Leave execution pending after a real confirmation click, using a fresh request ID.
   fixture.store.remove('batch','project');
   pending=fixture.click();
   await new Promise(setImmediate);
   fixture.confirm();
  }
  await started.promise;
  const requestId=lookup?'request-1':fixture.calls.at(-1).body.request_id;
  fixture.machine('other');
  const currentDraft={...draft(),id:requestId};
  fixture.store.set('batch','project',currentDraft);
  if(failed)response.reject(new TypeError('Old connection lost'));
  else response.resolve(lookup?{receipt:completedBatch}:completedBatch);
  await pending;
  assert.deepEqual({current:fixture.store.get('batch','project'),confirmations:fixture.confirms.length,
   posts:executionCount(fixture),toasts:fixture.toasts.length,refreshes:fixture.refreshes.length,selection:fixture.count()},
   {current:currentDraft,confirmations:lookup?0:1,posts:lookup?0:1,toasts:0,refreshes:0,selection:'当前页已选 · 1 / 1'});
  fixture.machine('original');
  const original=fixture.store.get('batch','project');
  assert.equal(original?.[0]?.id??null,failed?requestId:null);
 });
}

test('batch history ignores a response after a round trip through another project', async () => {
 let finish;
 const fixture=harness(()=>new Promise(resolve=>{finish=resolve;}));
 const pending=fixture.buttonByText('批次记录').onclick();
 fixture.project('other');fixture.project('project');
 finish({batches:[]});await pending;
 assert.equal(fixture.dialog(),null);
});

test('same-name checks at different positions are not attributed to grouped requirements', async () => {
 const check=(index,passed)=>({name:'shared name',run:'run',stage:'acceptance_candidate',index,passed});
 const fixture=harness(async()=>({group_lineage:{acceptance_evidence:{all_passed:true,requirements:[
  {task:'a',index:0,criterion:'Criterion A',state:'pass',checks:[check(0,true)]},
  {task:'b',index:0,criterion:'Criterion B',state:'pass',checks:[check(1,true)]},
 ]}}}));
 await fixture.batch.evidence({id:'group'});
 const [,,first,second]=fixture.dialog().children;
 assert.equal(fixture.dialog().children[1].textContent,'覆盖记录未知');
 assert.deepEqual(first.children.map(node=>node.textContent),['a · 1 · Criterion A','未知']);
 assert.deepEqual(second.children.map(node=>node.textContent),['b · 1 · Criterion B','未知']);
});
