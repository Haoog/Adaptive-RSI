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
  click(){return node.dispatch('click');}};
 return node;
}
// Drives the real toolbar entry point so the regression covers the user path, not a private helper.
function harness(api,{confirm=true}={}){
 const drafts=new Map(),calls=[],toasts=[],confirms=[],rows={};
 let panel=null;
 const host=element('div');host.after=node=>{panel=node;};
 const context={window:{},Map,Set,JSON,crypto:{randomUUID:()=>'request-new'},console,projectId:'project',t:value=>value,
  statusNames:{queued:'排队'},reviewNames:{},el:element,
  toast:(message,error=false)=>toasts.push({message,error}),
  uiConfirm:async message=>{confirms.push(message);return confirm;},
  refresh:async()=>{},
  safe:async fn=>{await fn();},
  api:async(path,body)=>{calls.push({path,body});return api(path,body);},
  document:{querySelector:selector=>selector==='.task-toolbar'?host:null,getElementById:id=>id==='batch-actions'?panel:null,body:{append(){}}}};
 context.window.RSIDraftStore={get:(kind,scope)=>drafts.get(kind+':'+scope)??null,set:(kind,scope,value)=>drafts.set(kind+':'+scope,value),remove:(kind,scope)=>drafts.delete(kind+':'+scope)};
 vm.createContext(context);
 vm.runInContext(fs.readFileSync('adaptive_rsi/web/batch.js','utf8'),context);
 const checkbox=context.window.RSIBatch.row({id:'t1',title:'任务一',fingerprint:''});rows.t1=checkbox;
 const button=()=>panel.children.find(node=>node.textContent==='批量重试');
 const render=tasks=>{const list=tasks.map(task=>typeof task==='string'?{id:task}:task);context.window.RSIBatch.toolbar(list);for(const task of list)rows[task.id]=context.window.RSIBatch.row({id:task.id,title:'任务 '+task.id,fingerprint:''});};
 // Mirror a real checkbox click: the browser toggles checked before dispatching click.
 const check=(id,shiftKey=false)=>{const box=rows[id];box.checked=!box.checked;box.dispatch('click',{shiftKey});};
 return {calls,toasts,confirms,drafts,rows,panel,batch:context.window.RSIBatch,button,render,check,count:()=>panel.children[0].textContent,
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
