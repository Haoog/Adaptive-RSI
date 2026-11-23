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
 let form,input,finishRequest,finishReload;
 const makeElement=()=>({append(){},prepend(){},setAttribute(){},addEventListener(){}});
 const window={fetch:async()=>{},__TAURI_INTERNALS__:{invoke:async(command,args)=>{
  calls.push({command,args});
  if(command==='runtime_info')return {kind:'local',connected:true,scope:'local',connection_generation:7};
  if(command==='request')return new Promise(resolve=>{finishRequest=resolve;});
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
 const pending=window.fetch('/api/state');
 await new Promise(setImmediate);
 assert.equal(calls.find(call=>call.command==='request').args.connectionGeneration,7);
 input.value='https://rsi.example.test/?token=fixture';
 form.onsubmit({preventDefault(){}});
 await reloaded;
 finishRequest({status:200,body:'{}'});
 await assert.rejects(pending,{name:'AbortError'});
 await assert.rejects(window.fetch('/api/config',{method:'POST',body:'{}'}),{name:'AbortError'});
 assert.deepEqual(calls.map(call=>call.command),['runtime_info','request','validate_connection_target','connect']);
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
