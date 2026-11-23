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
 const node={tag,className:cls,textContent:text===undefined?'':text,children:[],dataset:{},listeners:{},disabled:false,open:false,isConnected:true,id:'',
  append(...items){node.children.push(...items);},
  replaceChildren(...items){for(const child of node.children)child.isConnected=false;node.children=items;},
  setAttribute(){},remove(){},focus(){},showModal(){node.open=true;},close(){node.open=false;node.onclose?.();},
  addEventListener(type,fn){(node.listeners[type]??=[]).push(fn);},
  dispatch(type,extra){let result;for(const fn of node.listeners[type]||[])result=fn({type,target:node,...extra});return result;},
  click(){if(!node.disabled)return node.onclick?.()??node.dispatch('click');}};
 return node;
}
// Drives the real toolbar entry point so the regression covers the user path, not a private helper.
function harness(api,{confirm=true,clickConfirm=false,capabilities}={}){
 const drafts=new Map(),calls=[],toasts=[],confirms=[],refreshes=[],rows={};
 let panel=null,dialog=null,requestNumber=0,exportBlob;
 const downloads=[];
 const host=element('div');host.after=node=>{panel=node;};
 const context={window:{},Map,Set,JSON,AbortController,DOMException,crypto:{randomUUID:()=>++requestNumber===1?'request-new':'request-new-'+requestNumber},console,projectId:'project',navigationSequence:0,t:value=>value,
  statusNames:{queued:'排队'},reviewNames:{},Blob,setTimeout:()=>0,
  URL:{createObjectURL(blob){exportBlob=blob;return 'blob:batch';},revokeObjectURL(){}},
  el(tag,...args){
   const node=element(tag,...args);
   node.focus=()=>{context.document.activeElement=node;};
   if(tag==='input')node.value='';
   if(tag==='a')node.onclick=()=>downloads.push({name:node.download,blob:exportBlob});
   return node;
  },
  toast:(message,error=false)=>toasts.push({message,error}),
  uiConfirm:async (message,...options)=>{confirms.push(message);return clickConfirm?confirmDialog(message,...options):confirm;},
  refresh:async options=>{refreshes.push(options);},
  safe:async fn=>{try{await fn();}catch(error){context.toast(error.message,true);}},
  api:async(path,body)=>recordRequest(path,body),
  document:{querySelector:selector=>selector==='.task-toolbar'?host:null,getElementById:id=>id==='batch-actions'?panel:panel?.children.find(node=>node.id===id),body:{append(node){dialog=node;}}}};
 function recordRequest(path,body){
  const view={machine:context.window.__RSI_MACHINE_KEY__??'',project:context.projectId,navigation:context.navigationSequence};
  calls.push({path,body,view});return api(path,body,view);
 }
 const app=fs.readFileSync('adaptive_rsi/web/app.js','utf8');
 vm.createContext(context);
 const confirmDialog=vm.runInContext('('+app.slice(app.indexOf('function uiConfirm('),app.indexOf('function elapsedCopy('))+')',context);
 if(capabilities){
  Object.assign(context,{structuredClone,setTimeout,clearTimeout,state:{csrf:'test'},featureForPath:()=> 'task-batch',
   configReasonLabels:{},apiErrors:{},negotiateCapabilities:capabilities,
   fetch:async(url,options)=>{
    const path=url.slice('/api/'.length),body=options.body===undefined?undefined:JSON.parse(options.body);
    const data=await recordRequest(path,body),status=data.error_code?409:200;
    return {ok:status===200,status,json:async()=>data};
   }});
  vm.runInContext(app.slice(app.indexOf('async function sendApi('),app.indexOf('const goalSubmissionLimit=')),context);
 }
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
 return {calls,toasts,confirms,refreshes,drafts,downloads,store:context.window.RSIDraftStore,rows,panel,dialog:()=>dialog,
  focused:()=>context.document.activeElement,
  project(value){context.projectId=value;context.navigationSequence++;context.window.RSIBatch.viewChanged?.();},
  machine(value){context.window.__RSI_MACHINE_KEY__=value;context.window.RSIBatch.viewChanged?.();},
  navigate(){context.navigationSequence++;context.window.RSIBatch.viewChanged?.();},
  confirm(){dialog.children.at(-1).children[1].click();},
  decline(){dialog.children.at(-1).children[0].click();},
  recommend:()=>panel.children.find(node=>node.textContent==='推荐任务簇').click(),
  notify:message=>context.toast(message),
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

const recommendedClusters=[['t1','t2'],['t3','t4']];
const recommendation={...previewFor(recommendedClusters.flat()),recommendations:recommendedClusters.map((tasks,index)=>({tasks,revision:'cluster-'+index}))};
const groupDraft={signature:'["group",["t1","t2"]]',id:'group-1',tasks:recommendedClusters[0],action:'group'};
const nextTurn=()=>new Promise(setImmediate);
function selectRecommendations(fixture){
 fixture.render(recommendation.tasks.map(task=>task.id));
 fixture.buttonByText('全选当前页').click();
}
async function confirmRecommendations(fixture,pending){
 await nextTurn();
 while(fixture.dialog()?.open){fixture.confirm();await nextTurn();}
 await pending;
}
function recommendationCounts(fixture){
 return {previews:fixture.calls.filter(({body})=>body?.action==='recommend').length,
  lookups:fixture.calls.filter(({body})=>!body).length,confirmations:fixture.confirms.length,
  posts:executionCount(fixture),refreshes:fixture.refreshes.length};
}

for(const [stage,switchView] of [
 ['preview after project switch',fixture=>fixture.project('other')],
 ['empty preview after navigation round trip',fixture=>{fixture.navigate();fixture.navigate();}],
 ['failed preview after runtime switch',fixture=>fixture.machine('other')],
 ['open confirmation after project round trip',fixture=>{fixture.project('other');fixture.project('project');}],
 ['open confirmation after runtime switch',fixture=>fixture.machine('other')],
]){
 test(`recommendation ownership ignores ${stage}`,async()=>{
  const reply=deferred(),confirming=stage.startsWith('open');
  const fixture=harness((_path,body)=>body.preview?(confirming?recommendation:reply.promise):completedBatch,{clickConfirm:true});
  selectRecommendations(fixture);
  const pending=fixture.recommend();
  if(confirming){await nextTurn();assert.equal(fixture.dialog()?.open,true);}
  switchView(fixture);
  const dialogLeftOpen=fixture.dialog()?.open===true;
  fixture.notify('新视图提示');
  if(stage.startsWith('failed'))reply.reject(new TypeError('旧预览连接失败'));
  else reply.resolve(stage.startsWith('empty')?{recommendations:[]}:recommendation);
  await confirmRecommendations(fixture,pending);
  assert.deepEqual({...recommendationCounts(fixture),dialogLeftOpen,notice:fixture.toasts.at(-1)?.message},
   {previews:1,lookups:0,confirmations:confirming?1:0,posts:0,refreshes:0,dialogLeftOpen:false,notice:'新视图提示'});
 });
}

for(const [stage,outcome] of [['lookup','complete'],['lookup','failed'],['execution','complete'],['execution','partial'],['execution','failed']]){
 test(`recommendation ownership isolates ${outcome} ${stage} in the original runtime`,async()=>{
  const lookup=stage==='lookup',reply=deferred(),started=deferred();
  const fixture=harness((_path,body)=>{
   if(body?.preview)return recommendation;
   if(!body||(!lookup&&body.tasks[0]==='t1')){started.resolve();return reply.promise;}
   return completedBatch;
  },{clickConfirm:true});
  fixture.machine('original');
  if(lookup)fixture.store.set('batch','project',groupDraft);
  selectRecommendations(fixture);
  const pending=fixture.recommend();
  await nextTurn();fixture.confirm();await started.promise;
  const requestId=lookup?groupDraft.id:fixture.calls.at(-1).body.request_id;
  fixture.machine('other');
  const currentDraft={...groupDraft,id:requestId};
  fixture.store.set('batch','project',currentDraft);
  fixture.notify('新运行环境提示');
  if(outcome==='failed')reply.reject(new TypeError('原运行环境应答失败'));
  else if(lookup)reply.resolve({receipt:completedBatch});
  else reply.resolve(outcome==='complete'?completedBatch:{complete:false,receipts:[]});
  await confirmRecommendations(fixture,pending);
  const current=fixture.store.get('batch','project');
  fixture.machine('original');
  const saved=fixture.store.get('batch','project');
  assert.deepEqual({...recommendationCounts(fixture),current,originalId:saved?.[0]?.id??saved?.id??null,
   selection:fixture.count(),notice:fixture.toasts.at(-1)?.message},
   {previews:1,lookups:lookup?1:0,confirmations:1,posts:lookup?0:1,refreshes:0,current:currentDraft,
    originalId:outcome==='complete'?null:requestId,selection:'当前页已选 · 4 / 4',notice:'新运行环境提示'});
 });
}

test('recommendation ownership blocks a confirmed POST still waiting on capability negotiation',async()=>{
 const negotiation=deferred(),started=deferred();
 let checks=0;
 const fixture=harness((_path,body)=>body.preview?recommendation:completedBatch,{clickConfirm:true,capabilities:()=>{
  if(++checks!==2)return new Set(['task-batch']);
  started.resolve();return negotiation.promise;
 }});
 selectRecommendations(fixture);
 const pending=fixture.recommend();
 await nextTurn();fixture.confirm();await started.promise;
 fixture.navigate();fixture.navigate();
 negotiation.resolve(new Set(['task-batch']));
 await confirmRecommendations(fixture,pending);
 assert.deepEqual(recommendationCounts(fixture),{previews:1,lookups:0,confirmations:1,posts:0,refreshes:0});
});

test('recommendation ownership stops the update retry after leaving the view',async()=>{
 const status=deferred(),pollStarted=deferred();
 let attempted=false;
 const fixture=harness((path,body)=>{
  if(path==='state'){pollStarted.resolve();return status.promise;}
  if(body.preview)return recommendation;
  if(!attempted){attempted=true;return {error_code:'update_in_progress'};}
  return completedBatch;
 },{clickConfirm:true,capabilities:()=>new Set(['task-batch'])});
 selectRecommendations(fixture);
 const pending=fixture.recommend();
 await nextTurn();fixture.confirm();await pollStarted.promise;
 fixture.navigate();fixture.navigate();fixture.notify('新视图提示');
 status.resolve({update:{busy:false}});
 await confirmRecommendations(fixture,pending);
 assert.deepEqual({posts:executionCount(fixture),confirmations:fixture.confirms.length,refreshes:fixture.refreshes.length,
  notice:fixture.toasts.at(-1)?.message},
  {posts:1,confirmations:1,refreshes:0,notice:'新视图提示'});
});

test('recommendation ownership preserves a newer checkbox selection during execution',async()=>{
 const reply=deferred();
 const fixture=harness((_path,body)=>body.preview?{...recommendation,recommendations:recommendation.recommendations.slice(0,1)}:reply.promise,{clickConfirm:true});
 fixture.render(['t1','t2','t3']);
 fixture.check('t1');fixture.check('t2');
 const pending=fixture.recommend();
 await nextTurn();fixture.confirm();await nextTurn();
 fixture.check('t1');fixture.check('t1');fixture.check('t3');
 reply.resolve(completedBatch);await pending;
 assert.deepEqual({...recommendationCounts(fixture),selection:['t1','t2','t3'].map(id=>fixture.rows[id].checked)},
  {previews:1,lookups:0,confirmations:1,posts:1,refreshes:1,selection:[true,false,true]});
});

test('recommendation ownership skips a declined cluster and reconciles a partial cluster by request ID',async()=>{
 const fixture=harness((path,body)=>{
  if(body?.preview)return recommendation;
  if(!body){assert.match(path,/id=request-new$/);return {receipt:completedBatch};}
  return {complete:false,receipts:[{id:'t3',state:'grouped'},{id:'t4',state:'unknown'}]};
 },{clickConfirm:true});
 selectRecommendations(fixture);
 let pending=fixture.recommend();await nextTurn();
 assert.match(fixture.confirms.at(-1),/任务 t1/);
 fixture.decline();await nextTurn();
 assert.match(fixture.confirms.at(-1),/任务 t3/);
 fixture.confirm();await pending;
 assert.equal(executionCount(fixture),1);
 const write=fixture.calls.at(-1).body;
 assert.deepEqual(Array.from(write.tasks),recommendedClusters[1]);
 assert.equal(write.revision,'cluster-1');
 assert.equal(fixture.store.get('batch','project')[0].id,write.request_id);
 assert.match(fixture.toasts.at(-1).message,/部分结果待核对/);
 assert.equal(fixture.count(),'当前页已选 · 4 / 4');
 pending=fixture.recommend();await nextTurn();fixture.decline();await nextTurn();fixture.confirm();await pending;
 assert.deepEqual(recommendationCounts(fixture),{previews:2,lookups:1,confirmations:4,posts:1,refreshes:2});
 assert.equal(fixture.store.get('batch','project'),null);
 assert.equal(fixture.count(),'当前页已选 · 2 / 4');
});

test('recommendation ownership reuses the confirmed request ID when lookup fails',async()=>{
 const fixture=harness((_path,body)=>{
  if(!body)throw new TypeError('Receipt unavailable');
  if(body.preview)return {...recommendation,recommendations:recommendation.recommendations.slice(0,1)};
  return completedBatch;
 },{clickConfirm:true});
 fixture.store.set('batch','project',groupDraft);
 selectRecommendations(fixture);
 await confirmRecommendations(fixture,fixture.recommend());
 assert.deepEqual(recommendationCounts(fixture),{previews:1,lookups:1,confirmations:1,posts:1,refreshes:1});
 assert.equal(fixture.calls.at(-1).body.request_id,groupDraft.id);
 assert.equal(fixture.store.get('batch','project'),null);
});

test('batch history ignores a response after a round trip through another project', async () => {
 let finish;
 const fixture=harness(()=>new Promise(resolve=>{finish=resolve;}));
 const pending=fixture.buttonByText('批次记录').onclick();
 fixture.project('other');fixture.project('project');
 finish({batches:[]});await pending;
 assert.equal(fixture.dialog(),null);
});

function historyReport(title='原运行环境任务'){
 return {batches:[{action:'retry',request_id:'request-1',revision:'rev-1',complete:false,
  tasks:[{id:'t1',title}],receipts:[{id:'t1',state:'unknown'}]}]};
}
async function openHistory(fixture){
 const button=fixture.buttonByText('批次记录');button.focus();await button.click();return fixture.dialog();
}
const historyRows=box=>box.children[2];
const historyRetry=box=>historyRows(box).children[0]?.children[2];
const historyTitle=box=>historyRows(box).children[0]?.children[0]?.textContent;
function searchHistory(box,value){const filter=box.children[1];filter.value=value;filter.oninput();}
const historyScopes=[
 ['project switch',fixture=>fixture.project('other')],
 ['navigation round trip',fixture=>{fixture.navigate();fixture.navigate();}],
 ['runtime switch',fixture=>fixture.machine('other')],
];

for(const [scope,leave] of [['same view',null],...historyScopes]){
 for(const stage of ['preview','receipt lookup','confirmed execution']){
  test(`toolbar ${stage} capability wait respects ${scope}`,async()=>{
   const negotiation=deferred(),started=deferred(),supported=new Set(['task-batch']);
   const lookup=stage==='receipt lookup',execution=stage==='confirmed execution';
   let checks=0;
   const fixture=harness((_path,body)=>{
    if(!body)return {receipt:{...preview,complete:false}};
    return body.preview?preview:completedBatch;
   },{clickConfirm:true,capabilities:()=>{
    if(++checks!==(execution?2:1))return supported;
    started.resolve();return negotiation.promise;
   }});
   if(lookup)fixture.store.set('batch','project',draft());
   fixture.check('t1');
   const pending=fixture.click();
   if(execution){await nextTurn();assert.equal(fixture.dialog()?.open,true);fixture.confirm();}
   await started.promise;
   assert.equal(fixture.button().disabled,true);
   await fixture.click();
   leave?.(fixture);
   negotiation.resolve(supported);
   await nextTurn();
   if(fixture.dialog()?.open)fixture.confirm();
   await pending;
   assert.deepEqual({previews:fixture.calls.filter(call=>call.body?.preview).length,
    lookups:fixture.calls.filter(call=>!call.body).map(call=>call.path),posts:executionCount(fixture),
    confirmations:fixture.confirms.length,refreshes:fixture.refreshes.length,toasts:fixture.toasts.length},
   {previews:execution||(!leave&&!lookup)?1:0,lookups:!leave&&lookup?['tasks/batch?project=project&id=request-1']:[],
    posts:leave?0:1,confirmations:execution||!leave?1:0,refreshes:leave?0:1,toasts:leave?0:1});
   if(!leave&&lookup)assert.equal(fixture.calls.at(-1).body.request_id,'request-1');
  });
 }

 test(`toolbar execution update wait respects ${scope}`,async()=>{
  const status=deferred(),pollStarted=deferred();
  const fixture=harness((path,body)=>{
   if(path==='state'){pollStarted.resolve();return status.promise;}
   if(!body)return {receipt:{...preview,complete:false}};
   if(body.preview)return preview;
   return executionCount(fixture)===1?{error_code:'update_in_progress'}:completedBatch;
  },{clickConfirm:true,capabilities:()=>new Set(['task-batch'])});
  fixture.machine('original');
  fixture.check('t1');
  const pending=fixture.click();
  await nextTurn();assert.equal(fixture.dialog()?.open,true);fixture.confirm();
  await pollStarted.promise;
  assert.equal(fixture.button().disabled,true);
  await fixture.click();
  const original=fixture.calls.find(call=>call.body?.preview===false);
  leave?.(fixture);
  status.resolve({update:{busy:false}});
  await pending;
  const posts=()=>fixture.calls.filter(call=>call.body?.preview===false);
  assert.deepEqual(posts().map(call=>call.body.request_id),leave?[original.body.request_id]:[original.body.request_id,original.body.request_id]);
  assert.deepEqual([fixture.confirms.length,fixture.refreshes.length,fixture.toasts.length],leave?[1,0,1]:[1,1,2]);
  if(!leave)return;
  // Returning to the original view can reconcile the preserved ID in a new action.
  fixture.project('project');fixture.machine('original');fixture.render(['t1']);
  assert.equal(fixture.store.get('batch','project')?.[0]?.id,original.body.request_id);
  const recovery=fixture.click();
  await nextTurn();assert.equal(fixture.dialog()?.open,true);fixture.confirm();
  await recovery;
  assert.equal(fixture.calls.at(-2).path,'tasks/batch?project=project&id='+original.body.request_id);
  assert.deepEqual(posts().map(call=>call.body),[original.body,original.body]);
  assert.equal(fixture.store.get('batch','project'),null);
 });
}

for(const [scope,leave] of historyScopes){
 test(`batch history blocks an old dialog click after ${scope}`,async()=>{
  const fixture=harness((_path,body)=>body?completedBatch:historyReport());
  fixture.machine('original');
  const old=await openHistory(fixture),button=historyRetry(old);
  leave(fixture);
  await button.click();
  assert.deepEqual({open:old.open,sent:fixture.calls.filter(call=>call.body).map(call=>call.view)},
   {open:false,sent:[]});
 });

 test(`batch history blocks a POST waiting for capabilities after ${scope}`,async()=>{
  const negotiation=deferred(),started=deferred(),supported=new Set(['task-batch']);
  let checks=0;
  const fixture=harness((_path,body)=>body?completedBatch:historyReport(),{capabilities:()=>{
   if(++checks!==2)return supported;
   started.resolve();return negotiation.promise;
  }});
  fixture.machine('original');
  const box=await openHistory(fixture),pending=historyRetry(box).click();
  await started.promise;
  leave(fixture);
  negotiation.resolve(supported);await pending;
  assert.deepEqual({sent:fixture.calls.filter(call=>call.body).map(call=>call.view),toasts:fixture.toasts.length},
   {sent:[],toasts:0});
 });
}

test('batch history ignores a list response from a previous runtime',async()=>{
 const reply=deferred(),fixture=harness(()=>reply.promise);
 fixture.machine('original');
 const pending=fixture.buttonByText('批次记录').click();
 fixture.machine('other');reply.resolve(historyReport());await pending;
 assert.equal(fixture.dialog()===null,true);
});

test('batch history keeps feedback in the current dialog when reopened in the same view',async()=>{
 const reply=deferred();
 const fixture=harness((_path,body)=>body?reply.promise:historyReport());
 const old=await openHistory(fixture),pending=historyRetry(old).click();
 old.children.at(-1).click();
 const fresh=await openHistory(fixture);
 fixture.notify('新弹窗提示');
 reply.reject(new TypeError('旧弹窗请求失败'));await pending;
 assert.deepEqual({posts:executionCount(fixture),open:fresh.open,focus:fixture.focused()===fresh.children[1],
  notices:fixture.toasts.map(toast=>toast.message)},
  {posts:1,open:true,focus:true,notices:['新弹窗提示']});
});

test('batch history suppresses automatic update retry when the rejected POST is no longer current',async()=>{
 const reply=deferred(),sent=deferred();
 const fixture=harness((path,body)=>{
  if(path==='state')return {update:{busy:false}};
  if(!body)return historyReport();
  if(executionCount(fixture)>1)return completedBatch;
  sent.resolve();return reply.promise;
 },{capabilities:()=>new Set(['task-batch'])});
 const box=await openHistory(fixture),pending=historyRetry(box).click();
 await sent.promise;
 fixture.navigate();fixture.navigate();
 reply.resolve({error_code:'update_in_progress'});await pending;
 assert.deepEqual({paths:fixture.calls.map(call=>call.path),toasts:fixture.toasts.length},
  {paths:['tasks/batches?project=project','tasks/batch'],toasts:0});
});

for(const outcome of ['complete','partial','failure']){
 test(`batch history isolates a late ${outcome} response from a replaced dialog and runtime`,async()=>{
  const reply=deferred(),started=deferred();
  const fixture=harness((_path,body,view)=>{
   if(!body)return historyReport(view.machine==='other'?'新运行环境任务':undefined);
   started.resolve();return reply.promise;
  },{clickConfirm:true,capabilities:()=>new Set(['task-batch'])});
  fixture.machine('original');fixture.store.set('batch','project',draft());
  const old=await openHistory(fixture),pending=historyRetry(old).click();
  await started.promise;
  fixture.machine('other');fixture.store.set('batch','project',draft());
  // Reopen through the toolbar even when running against the original behavior.
  if(old.open)old.children.at(-1).click();
  const fresh=await openHistory(fixture);
  fixture.notify('新弹窗提示');
  if(outcome==='failure')reply.reject(new TypeError('旧连接失败'));
  else reply.resolve(outcome==='complete'?completedBatch:{complete:false,receipts:[{id:'t1',state:'failed'}]});
  await pending;
  const currentId=fixture.store.get('batch','project')?.id??null;
  const actual={sent:fixture.calls.filter(call=>call.body).map(call=>call.view.machine),currentId,
   title:historyTitle(fresh),oldTitle:historyTitle(old),freshOpen:fresh.open,focus:fixture.focused()===fresh.children[1],
   notices:fixture.toasts.map(toast=>toast.error?'旧错误':toast.message)};
  fixture.machine('original');
  assert.deepEqual({...actual,originalId:fixture.store.get('batch','project')?.id??null},
   {sent:['original'],currentId:'request-1',title:'重试 · request-1 · 结果待核对',oldTitle:'重试 · request-1 · 结果待核对',
    freshOpen:true,focus:true,notices:['新弹窗提示'],originalId:outcome==='complete'?null:'request-1'});
 });
}

test('batch history search keeps one pending request and exports partial results before retrying the same ID',async()=>{
 const reply=deferred();
 const fixture=harness((_path,body)=>!body?historyReport('Task Alpha'):executionCount(fixture)===1?reply.promise:completedBatch);
 const box=await openHistory(fixture),pending=historyRetry(box).click();
 searchHistory(box,'no match');const hidden=historyRows(box).children.length;
 searchHistory(box,'ALPHA');
 const redrawn=historyRetry(box),disabled=redrawn.disabled;
 await redrawn.click();
 reply.resolve({complete:false,receipts:[{id:'t1',state:'failed'}]});await pending;
 const afterPartial={title:historyTitle(box),enabled:historyRetry(box)?.disabled===false,posts:executionCount(fixture)};
 box.children.at(-2).click();
 const [{name,blob}]=fixture.downloads;
 const exported=JSON.parse(await blob.text());
 await historyRetry(box).click();
 const posts=fixture.calls.filter(call=>call.body).map(call=>structuredClone(call.body));
 assert.deepEqual({hidden,disabled,afterPartial,name,exported,posts,
  finalTitle:historyTitle(box),finalRetry:!!historyRetry(box)},
  {hidden:0,disabled:true,afterPartial:{title:'重试 · request-1 · 操作失败',enabled:true,posts:1},
   name:'adaptive-rsi-batches-project.json',exported:{batches:[{...historyReport('Task Alpha').batches[0],receipts:[{id:'t1',state:'failed'}]}]},
   posts:Array(2).fill({project:'project',tasks:['t1'],action:'retry',request_id:'request-1',revision:'rev-1',preview:false}),
   finalTitle:'重试 · request-1 · 已受理',finalRetry:false});
});

test('batch history keeps a current error retryable and returns focus after toolbar redraw',async()=>{
 const fixture=harness((_path,body)=>{
  if(body)throw new TypeError('当前连接失败');
  return historyReport();
 });
 const button=fixture.buttonByText('批次记录'),box=await openHistory(fixture);
 assert.equal(fixture.focused(),box.children[1]);
 await historyRetry(box).click();
 assert.equal(historyRetry(box).disabled,false);
 assert.deepEqual(fixture.toasts,[{message:'当前连接失败',error:true}]);
 box.children.at(-1).click();assert.equal(fixture.focused(),button);
 const reopened=await openHistory(fixture);
 fixture.render(['t1']);
 reopened.children.at(-1).click();
 assert.equal(fixture.focused()===fixture.buttonByText('批次记录'),true);
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
