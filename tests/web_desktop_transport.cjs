'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
const {webcrypto}=require('node:crypto');
const desktopSource=fs.readFileSync('adaptive_rsi/web/desktop.js','utf8');
const appSource=fs.readFileSync('adaptive_rsi/web/app.js','utf8');
const i18nSource=fs.readFileSync('adaptive_rsi/web/i18n.js','utf8');
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
 testConnectionRejection(`SSH connection rejection is safe and keeps the old page usable: ${rejection}`,{mode:'ssh',rejection,expected});
}

const httpsAuth=[
 'HTTPS 认证被拒绝。请生成新的认证地址后重试。',
 'HTTPS authentication was rejected. Generate a fresh authentication URL and try again.',
];
const httpsNetwork=[
 '无法连接 HTTPS 服务。请检查网络和服务器是否可达后重试。',
 'Could not reach the HTTPS service. Check your network and server reachability, then try again.',
];
const httpsUnknown=[
 '未能完成 HTTPS 连接。请核对完整的认证地址，并确认远端服务正在运行后重试。',
 'Could not complete the HTTPS connection. Verify the full authentication URL and make sure the remote service is running, then try again.',
];
for(const [name,rejection,[expected,english]] of [
 ['address preflight',null,['请输入 HTTPS 认证地址。','Enter an HTTPS authentication URL.']],
 ['health 401','Runtime health check failed (HTTP 401)',httpsAuth],
 ['health 403','Runtime health check failed (HTTP 403)',httpsAuth],
 ['state 401','Runtime state check failed (HTTP 401)',httpsAuth],
 ['state 403',new Error('Runtime state check failed (HTTP 403)'),httpsAuth],
 ['initial transport failure','Server connection failed',httpsNetwork],
 ['runtime transport failure','Cannot connect to the selected runtime',httpsNetwork],
 ['ambiguous server rejection','Server did not accept this connection',httpsUnknown],
 ['health 503','Runtime health check failed (HTTP 503)',httpsUnknown],
 ['missing CSRF','Missing runtime authentication',httpsUnknown],
 ['unknown error with credentials','Runtime state check failed (HTTP 401): https://user:fixture-password@rsi.example.test/?token=fixture-token',httpsUnknown],
 ['upgrade required','Update the remote runtime before connecting',[
  '远端服务版本不兼容。请升级远端服务后重试。',
  'The remote service version is incompatible. Upgrade the remote service and try again.',
 ]],
]){
 testConnectionRejection(`direct HTTPS connection rejection through the form: ${name}`,{mode:'https',rejection,expected,english});
}
testConnectionRejection('local connection rejection retains its existing error',{mode:'local',
 rejection:'Local runtime is unavailable',expected:'Local runtime is unavailable',english:'Local runtime is unavailable'});

function testConnectionRejection(name,{mode,rejection,expected,english}){
 test(name,async()=>{
  const handlers={},elements={},calls=[];
  let finishRequest,failConnect;
  const makeElement=()=>({textContent:'',append(...children){this.children=children;},prepend(){},addEventListener(){},
   setAttribute(name,value){if(name==='role'&&value==='alert')elements.alert=this;}});
  const oldReply={status:200,body:'{"runtime":"old"}'};
  const scope=mode==='ssh'?'local':'https://old.example.test';
  const window={fetch:async()=>{},__TAURI_INTERNALS__:{invoke:async(command,args)=>{
   calls.push({command,args});
   if(command==='runtime_info')return {kind:mode==='ssh'?'local':'remote',connected:true,scope,connection_generation:7};
   if(command==='request')return args.path==='state'?new Promise(resolve=>{finishRequest=resolve;}):oldReply;
   if(command==='validate_connection_target')return rejection!==null;
   if(command==='connect')return new Promise((_,reject)=>{failConnect=reject;});
  }}};
  const document={body:makeElement(),querySelector:makeElement,querySelectorAll:()=>[],getElementById:makeElement,
   documentElement:{style:{}},createTreeWalker:()=>({nextNode:()=>false}),
   createElement(tag){const element=makeElement();elements[tag]=element;return element;},
   addEventListener(name,handler){handlers[name]=handler;}};
  let reloads=0;
  const context=vm.createContext({window,document,NodeFilter:{SHOW_TEXT:4},crypto:webcrypto,Response,DOMException,console,
   location:{reload(){reloads++;}}});
  vm.runInContext(i18nSource,context);
  vm.runInContext(desktopSource,context);
  await handlers.DOMContentLoaded();
  const pending=window.fetch('/api/state');
  await new Promise(setImmediate);
  const sshTunnel=mode==='ssh'?true:undefined;
  const url=mode==='ssh'?'http://127.0.0.1:6090/?token=fixture':
   rejection===null?'https://rsi.example.test/':'https://rsi.example.test/?token=fixture-token';
  if(mode==='local'){
   elements.form.children.find(element=>element.textContent==='使用本机').onclick();
  }else{
   if(sshTunnel){elements.select.value='ssh';elements.select.onchange();}
   else assert.equal(elements.select.value,'https');
   elements.input.value=url;
   elements.form.onsubmit({preventDefault(){}});
  }
  await new Promise(setImmediate);
  const expectedCalls=mode==='local'?[['connect',null,undefined]]:[['validate_connection_target',url,sshTunnel]];
  if(mode!=='local'&&rejection!==null)expectedCalls.push(['connect',url,sshTunnel]);
  assert.deepEqual(calls.slice(2).map(({command,args})=>[command,args.url,args.sshTunnel]),expectedCalls);
  if(rejection!==null){failConnect(rejection);await new Promise(setImmediate);}
  assert.equal(elements.alert.hidden,false);
  assert.equal(elements.alert.textContent,expected);
  if(english){
   vm.runInContext('language="en";window.RSIDesktop.translate()',context);
   assert.equal(elements.alert.textContent,english);
  }
  assert.equal(elements.select.disabled,false);
  assert.equal(window.__RSI_MACHINE_KEY__,scope);
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
async function runtimePage({invoke,fetch=async()=>assert.fail('unexpected network request')}){
 const handlers={},nodes=new Map(),dialogs=[],storageReads=[],warnings=[],routes=[];
 const element=()=>({textContent:'',value:'',open:false,hidden:false,options:[],dataset:{},children:[],listeners:{},
  classList:{add(){},remove(){},toggle(){}},style:{},setAttribute(){},focus(){},querySelector(){return null},querySelectorAll(){return[]},remove(){},
  append(...children){this.children.push(...children);},prepend(child){this.children.unshift(child);},replaceChildren(...children){this.children=children;},
  addEventListener(name,handler,capture=false){(this.listeners[name]??=[]).push({handler,capture});},
  showModal(){this.open=true;},close(){this.open=false;for(const {handler} of this.listeners.close??[])handler();}});
 const node=id=>{if(!nodes.has(id))nodes.set(id,element());return nodes.get(id);};
 const body=element(),tools=element(),document={body,documentElement:{dataset:{build:'loaded'}},visibilityState:'visible',getElementById:node,
  querySelector:selector=>selector==='.project-tools'?tools:selector==='#language-select'?node('language-select'):null,
  querySelectorAll:()=>[],createTextNode:text=>({textContent:text}),
  createElement(tag){const child=element();if(tag==='dialog')dialogs.push(child);return child;},
  addEventListener(name,handler){(handlers[name]??=[]).push(handler);}};
 let reloads=0,confirmations=0,timer=0;
 const window={fetch,addEventListener(){}};
 if(invoke)window.__TAURI_INTERNALS__={invoke};
 const storage={getItem(key){storageReads.push(key);return null;},setItem(){},removeItem(){}};
 const context=vm.createContext({window,document,localStorage:storage,sessionStorage:storage,structuredClone,URLSearchParams,AbortController,DOMException,queueMicrotask,
  TextEncoder,TextDecoder,crypto:webcrypto,Response,console:{...console,warn(...args){warnings.push(args);}},
  setTimeout:()=>++timer,clearTimeout(){},setInterval(){},clearInterval(){},NodeFilter:{SHOW_TEXT:4},
  history:{replaceState(_state,_title,url){routes.push(url);}},location:{hash:'',pathname:'/index.html',search:'',reload(){reloads++;}},
  navigator:{clipboard:{}},matchMedia:()=>({matches:false,addEventListener(){}})});
 document.documentElement.style={};
 document.createTreeWalker=()=>({nextNode:()=>false});
 vm.runInContext(i18nSource,context);
 vm.runInContext(desktopSource,context);
 context.fetch=window.fetch;
 const reload=node('reload-ui');reload.textContent=refreshButton;reload.hidden=true;
 node('connection').textContent='已连接';
 if(invoke)window.RSINativeUpdate={tick:async()=>{}};
 vm.runInContext(appSource,context);
 context.render=()=>{};
 context.uiConfirm=async()=>{confirmations++;return true;};
 await Promise.all((handlers.DOMContentLoaded??[]).map(handler=>handler()));
 const tick=()=>new Promise(setImmediate);
 const dialog=dialogs[0],form=dialog?.children[0];
 return {window,context,dialog,form,machine:tools.children[0],node,tick,storageReads,warnings,routes,
  reload,reloads:()=>reloads,confirmations:()=>confirmations};
}
async function sessionPage(kind,scope){
 const pending=[],cancelled=[];
 const page=await runtimePage({invoke:async(command,args)=>{
  if(command==='runtime_info')return {kind,scope,connected:true,connection_generation:7};
  if(command==='request'){
   if(args.path==='sys-metrics')return {status:200,body:'{}'};
   return new Promise(resolve=>pending.push({args,resolve}));
  }
  if(command==='cancel_request'){cancelled.push(args.id);return;}
  if(command==='validate_connection_target')return false;
  if(command==='set_native_language')return;
  throw new Error(`Unexpected command: ${command}`);
 }});
 const {window,dialog,form,node,tick,reload}=page;
 const take=async()=>{await tick();const request=pending.shift();assert.ok(request,node('toast').textContent);assert.equal(request.args.connectionGeneration,7);return request;};
 const finish=async(promise,reply)=>{const request=await take();request.resolve(reply);return promise;};
 const initial=await take();assert.match(initial.args.path,/^state\?/);initial.resolve(stateReply());await tick();
 assert.equal(node('connection').textContent,'已连接');
 const input=form.children[5],alert=form.children[6];
 return {...page,alert,input,cancelled,take,finish,read:(path='/api/state?sections=core',options)=>window.fetch(path,options),
  openMachine:()=>page.machine.onclick(),
  async clickReload(){let stopped=false;for(const {handler} of [...reload.listeners.click].sort((a,b)=>Number(b.capture)-Number(a.capture))){handler({stopImmediatePropagation(){stopped=true;}});if(stopped)break;}await tick();}};
}
const startupError='无法读取当前运行机器。请连接服务器或使用本机。';
const startupErrorEnglish='Could not read the current runtime. Connect a server or use this computer.';
const localRecoveryError='未能连接本机运行服务，请稍后重试。';
const localRecoveryErrorEnglish='Could not connect to the local runtime. Try again.';
for(const recovery of ['local','server']){
 test(`desktop startup recovers from a rejected runtime snapshot through ${recovery}`,async()=>{
  const calls=[];
  const secret='https://user:fixture-password@rsi.example.test/?token=fixture-token';
  const scope=recovery==='local'?'local':'https://rsi.example.test';
  const url=recovery==='local'?null:'https://rsi.example.test/?token=fixture-token';
  let finishConnect,snapshot;
  const invoke=async(command,args)=>{
   calls.push({command,args});
   if(command==='runtime_info'){
    if(!snapshot)throw new Error(`Bridge error: ${secret}`);
    return snapshot;
   }
   if(command==='set_native_language'){
    if(!snapshot)throw new Error(`Bridge error: ${secret}`);
    return;
   }
   if(command==='validate_connection_target')return true;
   if(command==='connect')return new Promise((resolve,reject)=>{finishConnect={resolve,reject};});
   if(command==='request')return stateReply();
   assert.fail(`Unexpected command: ${command}`);
  };
  const {window,context,machine,dialog,form,node,tick,warnings,storageReads,routes,reloads}=await runtimePage({invoke});
  assert.equal(await window.RSIDesktop.ready,null);
  assert.equal(machine?.textContent,'连接运行机器');
  assert.equal(dialog.open,true);
  const [input,alert,connect,local,close]=form.children.slice(5);
  assert.equal(alert.textContent,startupError);
  assert.equal(alert.hidden,false);
  assert.equal(node('connection').textContent,'连接中断');
  assert.equal(node('toast').textContent,'');
  assert.equal(window.__RSI_MACHINE_KEY__,undefined);
  assert.deepEqual(storageReads.filter(key=>/^rsi-(project|navigation)/.test(key)),[]);
  assert.deepEqual(calls.map(({command})=>command),['runtime_info','set_native_language']);
  const setLanguage=async value=>{const select=node('language-select');select.value=value;await select.listeners.change[0].handler();};
  await setLanguage('en');
  assert.equal(machine.textContent,'Connect a runtime');
  assert.equal(alert.textContent,startupErrorEnglish);
  assert.equal(node('connection').textContent,'Disconnected');
  await setLanguage('zh-CN');
  const submit=()=>{
   if(url===null)return local.onclick();
   input.value=url;
   form.onsubmit({preventDefault(){}});
  };
  submit();await tick();
  assert.equal(connect.disabled,true);
  assert.equal(local.disabled,true);
  assert.equal(close.disabled,true);
  assert.equal(calls.at(-1).args.url,url);
  await assert.rejects(window.fetch('/api/config',{method:'POST',body:'{}'}),{name:'AbortError'});
  finishConnect.reject(`Native connect error: ${secret}`);await tick();
  const expectedError=url===null?localRecoveryError:'未能完成 HTTPS 连接。请核对完整的认证地址，并确认远端服务正在运行后重试。';
  assert.equal(alert.textContent,expectedError);
  assert.equal(connect.disabled,false);
  assert.equal(local.disabled,false);
  assert.equal(close.disabled,false);
  assert.equal(reloads(),0);
  close.onclick();assert.equal(dialog.open,false);assert.equal(input.value,'');
  machine.onclick();assert.equal(dialog.open,true);assert.equal(alert.textContent,expectedError);
  await setLanguage('en');
  assert.equal(alert.textContent,url===null?localRecoveryErrorEnglish:'Could not complete the HTTPS connection. Verify the full authentication URL and make sure the remote service is running, then try again.');
  submit();await tick();
  snapshot={kind:recovery==='local'?'local':'remote',name:'Recovered',scope,connected:true,connection_generation:8};
  finishConnect.resolve(snapshot);await tick();
  assert.equal(reloads(),1);
  assert.deepEqual(routes,['/index.html']);
  assert.equal(window.__RSI_MACHINE_KEY__,undefined);
  await assert.rejects(window.fetch('/api/state'),{name:'AbortError'});
  assert.deepEqual(calls.filter(({command})=>command==='request'),[]);
  assert.deepEqual(calls.filter(({command})=>command==='connect').map(({args})=>args.url),[url,url]);
  assert.deepEqual(calls.filter(({command})=>command==='validate_connection_target').map(({args})=>args.url),url===null?[]:[url,url]);
  assert.doesNotMatch(JSON.stringify(warnings),/fixture-password|fixture-token/);
  const reloaded=await runtimePage({invoke});await reloaded.tick();
  assert.equal(reloaded.dialog.open,false);
  assert.equal(reloaded.machine.textContent,'Recovered');
  assert.equal(reloaded.node('connection').textContent,'已连接');
  assert.equal(reloaded.window.__RSI_MACHINE_KEY__,scope);
  assert.deepEqual(reloaded.storageReads.filter(key=>/^rsi-(project|navigation)/.test(key)),[`rsi-project:${scope}`,`rsi-navigation:${scope}`]);
  const requests=calls.filter(({command})=>command==='request').map(({args})=>[args.path.split('?')[0],args.connectionGeneration]);
  assert.deepEqual(requests,[['sys-metrics',8],['state',8]]);
 });
}

test('desktop startup preserves the explicit disconnected snapshot and generation',async()=>{
 const requests=[];
 const snapshot={connected:false,connection_generation:7,error:'Local runtime exited'};
 const page=await runtimePage({invoke:async(command,args)=>{
  if(command==='runtime_info')return snapshot;
  if(command==='set_native_language')return;
  assert.equal(command,'request');requests.push(args);
  throw 'Runtime is unavailable';
 }});
 await page.tick();
 assert.equal(await page.window.RSIDesktop.ready,snapshot);
 assert.equal(page.window.__RSI_MACHINE_KEY__,'disconnected');
 assert.equal(page.dialog.open,true);
 assert.equal(page.form.children[6].textContent,snapshot.error);
 assert.equal(page.node('connection').textContent,'连接中断');
 assert.deepEqual(requests.map(args=>args.connectionGeneration),[7,7]);
});

test('desktop startup leaves browser startup on its original fetch',async()=>{
 const calls=[];
 const fetch=async path=>{calls.push(path);const reply=stateReply();return new Response(reply.body,{status:reply.status});};
 const page=await runtimePage({fetch});await page.tick();
 assert.equal(page.window.fetch,fetch);
 assert.equal(page.window.RSIDesktop,undefined);
 assert.equal(page.machine,undefined);
 assert.equal(page.dialog,undefined);
 assert.equal(page.node('connection').textContent,'已连接');
 assert.deepEqual(calls.map(path=>path.split('?')[0]),['/api/state','/api/sys-metrics']);
});

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
