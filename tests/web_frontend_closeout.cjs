'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=name=>fs.readFileSync('adaptive_rsi/web/'+name+'.js','utf8');
function node(tag,text){return {tag,textContent:text,children:[],dataset:{},append(...items){this.children.push(...items);},setAttribute(){},addEventListener(){},remove(){},focus(){},showModal(){this.open=true;},close(){this.open=false;this.onclose?.();}};}
function diagnostics(scope){
 const text=source('model-center'),start=text.indexOf('    diagnostics.onclick='),end=text.indexOf('    const authProvider=',start),calls=[];
 const provider={id:'local',catalog:{models:[{id:'model',enabled:true}]}},view={hidden:false};
 // The extracted handler runs without the loaded model page's closure.
 const context={diagnostics:{},scope,provider,data:{project:scope},active:provider.id,tab:'providers',diagnosisSequence:0,
  $:id=>{assert.equal(id,'view-models');return view;},
  api:async(path,body)=>{calls.push({path,body:{...body}});return{scope:body.project,detail:'<strong>diagnostic result</strong>'};},
  safe:fn=>fn(),el:node,t:x=>x,document:{body:node('body')},JSON};
 // Include the private helper, when present, and execute the actual assigned handler.
 const helper=text.match(/  function diagnosticDialog\([\s\S]*?\n  }/);
 vm.runInNewContext((helper?.[0]||'')+'\n'+text.slice(start,end),context);
 return {context,calls,run:()=>context.diagnostics.onclick()};
}
test('R4-015 successful diagnostics presents a visible, safely rendered result',async()=>{
 const fixture=diagnostics(null);
 await fixture.run();
 assert.deepEqual(fixture.calls,[{path:'model-center/diagnose',body:{provider:'local',model:'model',project:null}}]);
 const box=fixture.context.document.body.children[0];
 assert.equal(box.open,true);
 const result=box.children.find(item=>item.tag==='pre');
 assert.ok(result);
 assert.deepEqual(JSON.parse(result.textContent),{scope:null,detail:'<strong>diagnostic result</strong>'});
});

function shutdownFixture(reply) {
 const events = {}, ready = [], calls = [], apiCalls = [], timers = [];
 const body = node('body');
 const window = {
  __RSI_MACHINE_KEY__: 'https://remote.example.test',
  RSIDesktop: {async invoke(command) {
   calls.push(command);
   return reply();
  }},
  addEventListener(type, handler) { events[type] = handler; },
 };
 const document = {body, addEventListener(type, handler) {
  if (type === 'DOMContentLoaded') ready.push(handler);
 }};
 vm.runInNewContext(source('experience'), {
  window, document, location: {origin: 'tauri://localhost'},
  $: () => ({prepend() {}, addEventListener() {}}),
  el: node, action: () => node('button'), t: text => text, safe: fn => fn(),
  MutationObserver: class { observe() {} },
  async api(path) { apiCalls.push(path); throw Error('remote runtime unavailable'); },
  setTimeout(fn, delay) { timers.push({fn, delay}); },
 });
 for (const handler of ready) handler();
 return {
  calls, apiCalls, timers, body,
  start: () => events['rsi-app-shutdown'](),
  status: () => body.children[0].children[1].textContent,
  async tick() {
   assert.equal(timers.length, 1);
   const {fn, delay} = timers.shift();
   assert.equal(delay, 350);
   fn();
   await new Promise(resolve => setImmediate(resolve));
  },
 };
}

test('desktop quit polls the local child with a remote target selected and retries a lost reply', async () => {
 const replies = ['stopping', Error('IPC interrupted'), 'stopped'];
 const fixture = shutdownFixture(() => {
  const reply = replies.shift();
  if (reply instanceof Error) throw reply;
  return reply;
 });
 const finished = fixture.start();
 await new Promise(resolve => setImmediate(resolve));
 assert.deepEqual(fixture.apiCalls, []);
 assert.equal(fixture.status(), '正在关闭本机服务');
 await fixture.start();
 assert.equal(fixture.body.children.length, 1);
 await fixture.tick();
 assert.equal(fixture.status(), '正在关闭本机服务');
 await fixture.tick();
 await finished;
 assert.equal(fixture.status(), '本机服务已停止，正在退出应用。');
 assert.deepEqual(fixture.calls, Array(3).fill('quit_progress'));
 assert.deepEqual(fixture.apiCalls, []);
 assert.equal(fixture.timers.length, 0);
});

test('desktop quit renders native failure without reporting that the child stopped', async () => {
 const fixture = shutdownFixture(() => 'failed');
 await fixture.start();
 assert.deepEqual(fixture.calls, ['quit_progress']);
 assert.deepEqual(fixture.apiCalls, []);
 assert.equal(fixture.status(), '关闭过程中出现问题');
 assert.equal(fixture.timers.length, 0);
});

test('install status is retranslated after a language change', async () => {
 const elements=new Map(),events={};
 const element=id=>{
  if(!elements.has(id))elements.set(id,node('div'));
  return elements.get(id);
 };
 const context=vm.createContext({language:'zh-CN',document:{getElementById:element},
  window:{addEventListener(name,fn){events[name]=fn;},isSecureContext:true},
  navigator:{serviceWorker:{register:async()=>{throw Error('unavailable');}}},
  location:{protocol:'https:',origin:'https://rsi.example.test'},localStorage:{setItem(){}},translateStatic(){}});
 context.t=key=>context.language+' '+key;
 vm.runInContext(source('install'),context);
 await new Promise(setImmediate);
 assert.equal(element('install-status').textContent,'zh-CN 请使用浏览器菜单添加到主屏幕。');
 element('language-select').onchange({target:{value:'en'}});
 assert.equal(element('install-status').textContent,'en 请使用浏览器菜单添加到主屏幕。');
 events.appinstalled();
 assert.equal(element('install-status').textContent,'en 已安装，可以从主屏幕打开。');
 element('language-select').onchange({target:{value:'zh-CN'}});
 assert.equal(element('install-status').textContent,'zh-CN 已安装，可以从主屏幕打开。');
});
