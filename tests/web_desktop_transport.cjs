'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
const {webcrypto}=require('node:crypto');
const desktopSource=fs.readFileSync('adaptive_rsi/web/desktop.js','utf8');
const appSource=fs.readFileSync('adaptive_rsi/web/app.js','utf8');
// Loads the desktop transport with a stubbed Tauri bridge; app.js is loaded by tests that exercise api().
function desktop(reply){
 const element=()=>({textContent:'',dataset:{},children:[],classList:{add(){},remove(){},toggle(){}},addEventListener(){},append(){},prepend(){},setAttribute(){},querySelector(){return null},querySelectorAll(){return[]},remove(){},focus(){},showModal(){},close(){},style:{}});
 const document={documentElement:{dataset:{}},getElementById:element,querySelector:()=>null,querySelectorAll:()=>[],addEventListener(){},createElement:element,createTextNode:text=>({textContent:text}),body:element(),visibilityState:'visible',activeElement:null};
 const storage={getItem(){return null},setItem(){},removeItem(){}};
 const window={fetch:async()=>{},addEventListener(){},__TAURI_INTERNALS__:{invoke:async command=>command==='runtime_info'?{kind:'local'}:reply}};
 const context=vm.createContext({window,document,localStorage:storage,sessionStorage:storage,structuredClone,URLSearchParams,AbortController,DOMException,queueMicrotask,TextEncoder,TextDecoder,crypto:webcrypto,Response,console,setInterval(){},clearInterval(){},setTimeout(){},clearTimeout(){},location:{hash:'',reload(){}},navigator:{clipboard:{}},matchMedia:()=>({matches:false,addEventListener(){}}),t:value=>value,fetch:window.fetch});
 vm.runInContext(desktopSource,context);
 context.fetch=window.fetch;
 return {window,context};
}
const errorReply=requestId=>({status:500,body:'{"error":"Internal","error_code":"internal_error"}',requestId});
test('desktop abort forwards the same request ID and releases the caller',async()=>{
 const calls=[];let release;
 const window={fetch:async()=>{},__TAURI_INTERNALS__:{invoke:async(command,args)=>{
  calls.push({command,args});
  if(command==='runtime_info')return {kind:'local'};
  if(command==='request')return new Promise(resolve=>{release=resolve;});
 }}};
 vm.runInNewContext(desktopSource,{window,document:{addEventListener(){}},crypto:webcrypto,Response,DOMException,console});
 await window.RSIDesktop.ready;
 const controller=new AbortController(),request=window.fetch('/api/state',{signal:controller.signal});
 await new Promise(setImmediate);
 const active=calls.find(call=>call.command==='request');assert.ok(active.args.requestId);
 controller.abort();await assert.rejects(request,{name:'AbortError'});
 assert.equal(calls.find(call=>call.command==='cancel_request').args.id,active.args.requestId);
 release({body:'{}',status:200});await new Promise(setImmediate);
});
test('desktop replies expose the server request ID to API errors',async()=>{
 const {window,context}=desktop(errorReply('0123456789abcdef'));
 const response=await window.fetch('/api/state');
 assert.equal(response.headers.get('X-RSI-Request-ID'),'0123456789abcdef');
 vm.runInContext(appSource,context);vm.runInContext('state={csrf:"csrf"}',context);
 await assert.rejects(context.api('retry',{task:'task'}),error=>
  error.requestId==='0123456789abcdef'&&error.evidence.request_id==='0123456789abcdef'&&error.message.includes('诊断标识')&&error.message.includes('0123456789abcdef'));
});
test('desktop replies drop malformed request IDs',async()=>{
 for(const requestId of ['<script>alert(1)</script>','0123456789ABCDEF','0123456789abcde',undefined]){
  const {window,context}=desktop(errorReply(requestId));
  const response=await window.fetch('/api/state');
  assert.equal(response.headers.get('X-RSI-Request-ID'),null);
  vm.runInContext(appSource,context);vm.runInContext('state={csrf:"csrf"}',context);
  await assert.rejects(context.api('retry',{task:'task'}),error=>error.requestId===null&&!error.message.includes('script'));
 }
});

test('desktop keeps the old page fenced after accepting a new connection',async()=>{
 const handlers={},calls=[];
 let form,input,finishReload;
 const replies=[];
 const makeElement=()=>({append(){},prepend(){},setAttribute(){},addEventListener(){}});
 const window={fetch:async()=>{},__TAURI_INTERNALS__:{invoke:async(command,args)=>{
  calls.push({command,args});
  if(command==='runtime_info')return {kind:'remote',connected:true,scope:'https://old.example.test',connection_generation:7};
  if(command==='request')return new Promise(resolve=>{replies.push(resolve);});
  if(command==='validate_connection_target')return true;
  if(command==='connect')return {scope:'remote',connection_generation:8};
 }}};
 const document={body:makeElement(),querySelector:makeElement,getElementById:makeElement,
  createElement(tag){const element=makeElement();if(tag==='form')form=element;if(tag==='input')input=element;return element;},
  addEventListener(name,handler){handlers[name]=handler;}};
 const reloaded=new Promise(resolve=>{finishReload=resolve;});
 vm.runInNewContext(desktopSource,{window,document,t:value=>value,crypto:webcrypto,Response,DOMException,console,
  history:{replaceState(){}},location:{reload:finishReload}});
 await handlers.DOMContentLoaded();
 const oldSuccess=assert.rejects(window.fetch('/api/state'),{name:'AbortError'});
 const oldFailure=assert.rejects(window.fetch('/api/state'),{name:'AbortError'});
 await new Promise(setImmediate);
 assert.deepEqual(calls.filter(call=>call.command==='request').map(call=>call.args.connectionGeneration),[7,7]);
 input.value='https://rsi.example.test';
 form.onsubmit({preventDefault(){}});
 await reloaded;
 replies[0]({status:200,body:'{"projects":[]}'});
 replies[1]({status:401,body:'{}'});
 await oldSuccess;await oldFailure;
 await assert.rejects(window.fetch('/api/config',{method:'POST',body:'{}'}),{name:'AbortError'});
 assert.deepEqual(calls.map(call=>call.command),['runtime_info','request','request','validate_connection_target','connect']);
});

const sshAuthError='SSH 隧道可达，但认证失败。请生成新的认证地址后重试。';
const sshNetworkError='未能通过 SSH 隧道连接。请检查终端中的转发仍在运行，并核对本机端口后重试。';
for(const [rejection,expected] of [
 ['Runtime health check failed (HTTP 401)',sshAuthError],
 ['Runtime health check failed (HTTP 403)',sshAuthError],
 ['Runtime state check failed (HTTP 401)',sshAuthError],
 ['Runtime state check failed (HTTP 403)',sshAuthError],
 ['SSH tunnel authentication was rejected',sshAuthError],
 ['Missing runtime authentication',sshAuthError],
 ['Server connection failed',`${sshNetworkError} (Server connection failed)`],
 [new Error('Server connection failed'),`${sshNetworkError} (Server connection failed)`],
 ['Cannot connect to the selected runtime',sshNetworkError],
 ['SSH tunnel handshake timed out',sshNetworkError],
 ['Runtime health check failed (HTTP 503)',sshNetworkError],
 ['Runtime state check failed (HTTP 401): session at http://127.0.0.1:6090/?token=fixture',sshNetworkError],
]){
 test(`SSH connection rejection is safe and keeps the old page usable: ${rejection}`,async()=>{
  const handlers={},elements={},calls=[];
  let finishRequest,failConnect;
  const makeElement=()=>({textContent:'',append(){},prepend(){},addEventListener(){},
   setAttribute(name,value){if(name==='role'&&value==='alert')elements.alert=this;}});
  const oldReply={status:200,body:'{"runtime":"old"}'};
  const window={fetch:async()=>{},__TAURI_INTERNALS__:{invoke:async(command,args)=>{
   calls.push({command,args});
   if(command==='runtime_info')return {kind:'local',connected:true,scope:'local',connection_generation:7};
   if(command==='request')return args.path==='state'?new Promise(resolve=>{finishRequest=resolve;}):oldReply;
   if(command==='validate_connection_target')return true;
   if(command==='connect')return new Promise((_,reject)=>{failConnect=reject;});
  }}};
  const document={body:makeElement(),querySelector:makeElement,getElementById:makeElement,
   createElement(tag){const element=makeElement();elements[tag]=element;return element;},
   addEventListener(name,handler){handlers[name]=handler;}};
  let reloads=0;
  vm.runInNewContext(desktopSource,{window,document,t:value=>value,crypto:webcrypto,Response,DOMException,console,
   location:{reload(){reloads++;}}});
  await handlers.DOMContentLoaded();
  const pending=window.fetch('/api/state');
  await new Promise(setImmediate);
  elements.select.value='ssh';
  elements.select.onchange();
  const url='http://127.0.0.1:6090/?token=fixture';
  elements.input.value=url;
  elements.form.onsubmit({preventDefault(){}});
  await new Promise(setImmediate);
  assert.deepEqual(calls.slice(2).map(({command,args})=>[command,args.url,args.sshTunnel]),[
   ['validate_connection_target',url,true],['connect',url,true],
  ]);
  failConnect(rejection);
  await new Promise(setImmediate);
  assert.equal(elements.alert.hidden,false);
  assert.equal(elements.alert.textContent,expected);
  assert.equal(elements.select.disabled,false);
  assert.equal(window.__RSI_MACHINE_KEY__,'local');
  assert.equal(reloads,0);
  finishRequest(oldReply);
  assert.deepEqual(await (await pending).json(),{runtime:'old'});
  assert.deepEqual(await (await window.fetch('/api/config')).json(),{runtime:'old'});
  assert.deepEqual(calls.filter(({command})=>command==='request').map(({args})=>[args.path,args.connectionGeneration]),[
   ['state',7],['config',7],
  ]);
 });
}

test('a stale page waits for native recovery then reloads without replaying its request',async()=>{
 const calls=[];
 let current={scope:'local',connected:true,connection_generation:1},reloads=0;
 const window={fetch:async()=>{},__TAURI_INTERNALS__:{invoke:async(command,args)=>{
  if(command==='runtime_info')return current;
  calls.push(args);
  throw 'Runtime connection changed';
 }}};
 vm.runInNewContext(desktopSource,{window,document:{addEventListener(){}},crypto:webcrypto,Response,DOMException,console,
  location:{reload(){reloads++;}}});
 await window.RSIDesktop.ready;
 current={connected:false,connection_generation:2};
 await assert.rejects(window.fetch('/api/state'),{name:'AbortError'});
 assert.equal(reloads,0);
 current={scope:'local',connected:true,connection_generation:3};
 await assert.rejects(window.fetch('/api/config',{method:'POST',body:'{}'}),{name:'AbortError'});
 assert.equal(reloads,1);
 await assert.rejects(window.fetch('/api/state'),{name:'AbortError'});
 assert.deepEqual(calls.map(call=>[call.path,call.connectionGeneration]),[['state',1],['config',1]]);
});

const sessionError='远端登录已失效。请生成新的认证地址并重新连接。';
const sessionButton='登录已失效，重新打开控制台';
const refreshButton='界面有更新，保存输入后刷新';
const stateReply=(build='running')=>({status:200,body:JSON.stringify({projects:[],build,version:'test'})});
async function sessionPage(kind,scope){
 const handlers={},nodes=new Map(),dialogs=[],pending=[],cancelled=[];
 const element=()=>({textContent:'',value:'',open:false,hidden:false,options:[],dataset:{},children:[],listeners:{},
  classList:{add(){},remove(){},toggle(){}},style:{},setAttribute(){},focus(){},querySelector(){return null},querySelectorAll(){return[]},remove(){},
  append(...children){this.children.push(...children);},prepend(child){this.children.unshift(child);},replaceChildren(...children){this.children=children;},
  addEventListener(name,handler,capture=false){(this.listeners[name]??=[]).push({handler,capture});},
  showModal(){this.open=true;},close(){this.open=false;for(const {handler} of this.listeners.close??[])handler();}});
 const node=id=>{if(!nodes.has(id))nodes.set(id,element());return nodes.get(id);};
 const body=element(),tools=element(),document={body,documentElement:{dataset:{build:'loaded'}},visibilityState:'visible',getElementById:node,
  querySelector:selector=>selector==='.project-tools'?tools:null,querySelectorAll:()=>[],createTextNode:text=>({textContent:text}),
  createElement(tag){const child=element();if(tag==='dialog')dialogs.push(child);return child;},
  addEventListener(name,handler){(handlers[name]??=[]).push(handler);}};
 let reloads=0,confirmations=0,timer=0;
 const window={fetch:async()=>{assert.fail('unexpected network request');},addEventListener(){},__TAURI_INTERNALS__:{invoke:async(command,args)=>{
  if(command==='runtime_info')return {kind,scope,connected:true,connection_generation:7};
  if(command==='request')return new Promise(resolve=>pending.push({args,resolve}));
  if(command==='cancel_request'){cancelled.push(args.id);return;}
  if(command==='validate_connection_target')return false;
  if(command==='set_native_language')return;
  throw new Error(`Unexpected command: ${command}`);
 }}};
 const storage={getItem(){return null;},setItem(){},removeItem(){}};
 const context=vm.createContext({window,document,localStorage:storage,sessionStorage:storage,structuredClone,URLSearchParams,AbortController,DOMException,queueMicrotask,
  TextEncoder,TextDecoder,crypto:webcrypto,Response,console,setTimeout:()=>++timer,clearTimeout(){},setInterval(){},clearInterval(){},
  location:{hash:'',reload(){reloads++;}},navigator:{clipboard:{}},matchMedia:()=>({matches:false,addEventListener(){}}),language:'zh-CN',t:value=>value});
 vm.runInContext(desktopSource,context);
 context.fetch=window.fetch;
 const reload=node('reload-ui');reload.textContent=refreshButton;reload.hidden=true;
 vm.runInContext(appSource,context);
 context.render=()=>{};
 context.uiConfirm=async()=>{confirmations++;return true;};
 await handlers.DOMContentLoaded[0]();
 const tick=()=>new Promise(setImmediate);
 const take=async()=>{await tick();const request=pending.shift();assert.ok(request,node('toast').textContent);assert.equal(request.args.connectionGeneration,7);return request;};
 const finish=async(promise,reply)=>{const request=await take();request.resolve(reply);return promise;};
 const initial=await take();assert.match(initial.args.path,/^state\?/);initial.resolve(stateReply());await tick();
 assert.equal(node('connection').textContent,'已连接');
 const dialog=dialogs[0],form=dialog.children[0],input=form.children[5],alert=form.children[6];
 return {context,dialog,alert,form,input,reload,cancelled,node,tick,take,finish,read:(path='/api/state?sections=core',options)=>window.fetch(path,options),
  openMachine:()=>tools.children[0].onclick(),reloads:()=>reloads,confirmations:()=>confirmations,
  async clickReload(){let stopped=false;for(const {handler} of [...reload.listeners.click].sort((a,b)=>Number(b.capture)-Number(a.capture))){handler({stopImmediatePropagation(){stopped=true;}});if(stopped)break;}await tick();}};
}
const unauthorized={status:401,body:'{}'};
for(const [name,kind,scope] of [
 ['local','local','local'],['SSH tunnel','remote','http://127.0.0.1:43217'],['direct HTTPS','remote','https://rsi.example.test'],
]){
 test(`desktop session refresh recovery through ${name}`,async()=>{
  const page=await sessionPage(kind,scope);
  assert.equal(page.reload.textContent,refreshButton);
  const failure=assert.rejects(page.context.refresh(),{code:'session_required'});
  await page.finish(failure,unauthorized);
  assert.equal(page.reload.textContent,sessionButton);
  assert.equal(vm.runInContext('refreshPollTimer',page.context),null);
  await page.clickReload();
  assert.equal(page.dialog.open,kind==='remote');
  assert.equal(page.confirmations(),kind==='remote'?0:1);
  if(kind==='remote')assert.equal(page.alert.textContent,sessionError);
  const response=await page.finish(page.read(),stateReply());
  assert.equal(response.status,200);
  assert.deepEqual(await response.json(),{projects:[],build:'running',version:'test'});
  if(kind==='local'){
   assert.equal(page.reload.textContent,sessionButton);
   assert.equal(vm.runInContext('refreshPollTimer',page.context),null);
   return;
  }
  assert.equal(page.dialog.open,true);
  assert.equal(page.alert.hidden,true);
  page.dialog.close();
  await page.clickReload();
  assert.equal(page.dialog.open,false);
  assert.equal(page.confirmations(),1);
  assert.equal(page.reloads(),1);
  assert.equal(page.reload.textContent,refreshButton);
  assert.equal(page.reload.hidden,false);
  assert.equal(page.node('connection').textContent,'已连接');
  assert.ok(vm.runInContext('refreshPollTimer',page.context));
  await page.finish(page.context.refresh(),stateReply());
 });
}

test('desktop session recovery hides the expired refresh button when the interface is current',async()=>{
 const page=await sessionPage('remote','https://rsi.example.test');
 const refresh=()=>page.context.safe(()=>page.context.refresh());
 await page.finish(refresh(),unauthorized);
 assert.equal(page.node('toast').hidden,false);
 assert.equal(page.reload.textContent,sessionButton);
 await page.finish(page.read(),stateReply('loaded'));
 assert.equal(page.node('toast').hidden,true);
 assert.equal(page.reload.textContent,refreshButton);
 assert.equal(page.reload.hidden,true);
});

test('desktop session recovery ignores a late refresh 401 after a newer valid state read',async()=>{
 const page=await sessionPage('remote','https://rsi.example.test');
 const refresh=page.context.safe(()=>page.context.refresh()),older=await page.take();
 await page.finish(page.read(),stateReply());
 older.resolve(unauthorized);
 await refresh;
 assert.equal(page.reload.textContent,refreshButton);
 assert.equal(page.node('connection').textContent,'已连接');
 assert.equal(page.node('toast').textContent,'');
 page.openMachine();assert.equal(page.alert.hidden,true);
});

test('desktop session recovery preserves the current connection validation error',async()=>{
 const page=await sessionPage('remote','https://rsi.example.test');
 await page.finish(page.read(),unauthorized);
 await page.clickReload();
 assert.equal(page.alert.textContent,sessionError);
 page.input.value='invalid.example.test';
 page.form.onsubmit({preventDefault(){}});
 await page.tick();
 const validationError='请输入 HTTPS 认证地址。';
 assert.equal(page.alert.textContent,validationError);
 page.dialog.close();
 await page.clickReload();
 assert.equal(page.alert.textContent,validationError);
 await page.finish(page.read(),stateReply());
 assert.equal(page.alert.textContent,validationError);
 page.dialog.close();page.openMachine();
 assert.equal(page.alert.textContent,validationError);
});

test('desktop session recovery orders valid state reads and ignores cancelled or unrelated replies',async()=>{
 const page=await sessionPage('remote','https://rsi.example.test');
 const first=page.read(),oldSuccess=await page.take();
 await page.finish(page.read(),unauthorized);
 oldSuccess.resolve(stateReply());await first;
 const controller=new AbortController(),aborted=assert.rejects(page.read(undefined,{signal:controller.signal}),{name:'AbortError'}),cancelled=await page.take();
 controller.abort();await aborted;
 assert.deepEqual(page.cancelled,[cancelled.args.requestId]);
 cancelled.resolve(stateReply());
 for(const [path,options,reply] of [
  ['/api/health',{},stateReply()],['/api/state',{method:'POST',body:'{}'},stateReply()],['/api/state',{method:'POST'},stateReply()],
  ['/api/state',{method:'GET',body:'{}'},stateReply()],['/api/state',{}, {status:200,body:'{}'}],
  ['/api/state',{}, {status:200,body:'unavailable'}],['/api/state',{}, {status:503,body:'{}'}],
 ])await page.finish(page.read(path,options),reply);
 await page.clickReload();assert.equal(page.alert.textContent,sessionError);
 page.dialog.close();
 const later=page.read(),oldFailure=await page.take();
 const newerController=new AbortController(),newer=assert.rejects(page.read(undefined,{signal:newerController.signal}),{name:'AbortError'}),newerRequest=await page.take();
 newerController.abort();await newer;newerRequest.resolve(unauthorized);
 await page.finish(page.read(),stateReply('loaded'));
 oldFailure.resolve(unauthorized);await later;
 const lateController=new AbortController(),late=assert.rejects(page.read(undefined,{signal:lateController.signal}),{name:'AbortError'}),lateRequest=await page.take();
 lateController.abort();await late;lateRequest.resolve(unauthorized);
 await page.clickReload();
 assert.equal(page.dialog.open,false);
 assert.equal(page.reloads(),1);
 page.openMachine();assert.equal(page.alert.hidden,true);
});

test('desktop download links are handled as downloads while ordinary external links use the native bridge', async () => {
 const handlers={},calls=[];
 const element=()=>({append(){},prepend(){},setAttribute(){},addEventListener(){}});
 const window={fetch:async()=>{},__TAURI_INTERNALS__:{invoke:async(command,args)=>{
  if(command==='runtime_info')return {kind:'remote',connected:true,scope:'https://rsi.example.test'};
  calls.push({command,args});
 }}};
 const document={body:element(),createElement:element,querySelector:element,getElementById:element,
  addEventListener(name,handler){handlers[name]=handler;}};
 vm.runInNewContext(desktopSource,{window,document,t:value=>value,console});
 await handlers.DOMContentLoaded();
 const click=(href,download)=>{
  let cancelled=false;
  const link={href,hasAttribute:name=>name==='download'&&download};
  handlers.click({target:{closest:()=>link},preventDefault(){cancelled=true;}});
  return cancelled;
 };
 assert.equal(click('https://rsi.example.test/api/backup?id=fixture',true),false);
 assert.deepEqual(calls,[]);
 assert.equal(click('https://docs.example.test/',false),true);
 assert.equal(calls[0].command,'open_url');
 assert.equal(calls[0].args.url,'https://docs.example.test/');
});
