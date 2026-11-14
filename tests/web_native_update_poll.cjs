'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');

// Deterministic timers: the panel schedules itself with setTimeout, so every read
// and every pending timer can be asserted instead of waited for.
function fakeClock(start=1700000000000){
 let now=start,nextId=1;const timers=new Map();
 const flush=async()=>{for(let i=0;i<20;i++)await new Promise(resolve=>setImmediate(resolve));};
 const clock={
  now:()=>now,
  setTimeout(fn,ms){const id=nextId++;timers.set(id,{at:now+Math.max(0,Number(ms)||0),fn});return id;},
  clearTimeout(id){timers.delete(id);},
  setInterval(){throw new Error('the update panel must not create an interval');},
  pending:()=>timers.size,
  flush,
  async advance(ms){
   const target=now+ms;let guard=0;
   for(;;){
    let pick=null;
    for(const [id,timer] of timers)if(timer.at<=target&&(!pick||timer.at<pick[1].at))pick=[id,timer];
    if(!pick)break;
    now=pick[1].at;timers.delete(pick[0]);pick[1].fn();await flush();
    if(++guard>2000)throw new Error('runaway timer');
   }
   now=target;await flush();
  },
 };
 return clock;
}

function fixture({visibility='visible',policy,respond}={}){
 let panel;const saved=new Map(),calls=[],listeners={},writes={total:0,option:0};
 const clock=fakeClock();
 let reply=respond||(()=>({state:'latest'}));
 const counted=(node,kind)=>{
  let text='';
  Object.defineProperty(node,'textContent',{get(){return text;},set(value){text=String(value);writes.total++;if(kind)writes[kind]++;}});
  return node;
 };
 const el=(tag,text,className)=>{
  const node=counted({tag,className,children:[],append(...nodes){this.children.push(...nodes);},setAttribute(){},after(node){panel=node;}},tag==='option'?'option':null);
  if(text)node.textContent=text;
  return node;
 };
 const document={activeElement:null,visibilityState:visibility,documentElement:{lang:'zh-CN'},
  addEventListener(type,handler){(listeners[type]||(listeners[type]=[])).push(handler);},
  createElement:()=>el('a')};
 const invoke=async(_,args)=>{calls.push({action:args.action,at:clock.now()});return reply(args);};
 const context={
  window:{RSIDesktop:{invoke,ready:Promise.resolve({kind:'local',owned:true})}},
  document,el,$:()=>el('anchor'),t:x=>x,
  localRead:key=>key==='rsi-native-update-checked'?String(clock.now()):policy,
  localStorage:{setItem(key,value){saved.set(key,value);}},safe:fn=>fn(),
  setTimeout:clock.setTimeout,clearTimeout:clock.clearTimeout,setInterval:clock.setInterval,
  Date:{now:()=>clock.now()},Math,Number,String,Error,JSON,
 };
 vm.runInNewContext(fs.readFileSync('adaptive_rsi/web/native-update.js','utf8'),context);
 const api=context.window.RSINativeUpdate;
 const all=node=>[node,...node.children.flatMap(all)];
 return{api,clock,calls,writes,saved,
  boot(){for(const handler of listeners.DOMContentLoaded||[])handler();},
  flush:clock.flush,
  advance:clock.advance,
  pendingTimers:clock.pending,
  statusCalls:()=>calls.filter(call=>call.action==='status'),
  statusCount:()=>calls.filter(call=>call.action==='status').length,
  setVisibility(state){document.visibilityState=state;for(const handler of listeners.visibilitychange||[])handler();},
  button(label){return all(panel).find(node=>node.tag==='button'&&node.textContent===label);},
  resetWrites(){writes.total=0;writes.option=0;},
  text:()=>all(panel).map(node=>node.textContent).join(' '),
 };
}

test('a pending initial authorization does not block status polling or start automatic actions',async()=>{
 let authorize;
 const gate=new Promise(resolve=>{authorize=resolve;});
 let bytes=0;
 const f=fixture({respond:args=>{
  if(args.action==='authorize')return gate;
  if(args.action==='status')return {state:'downloading',bytes:bytes+=1000,total:100000};
  return {state:'latest'};
 }});
 f.boot();
 await f.advance(5000);
 assert.equal(f.statusCount(),1,'the first status read does not wait for authorization');
 await f.advance(3000);
 assert.equal(f.statusCount(),4,'status reads continue while authorization remains pending');
 assert.match(f.text(),/4%/);
 assert.equal(f.button('取消').hidden,false);
 assert.equal(f.pendingTimers(),1);
 assert.equal(f.calls.filter(call=>['check','download','install'].includes(call.action)).length,0);
 authorize({authorization_revision:1});
 await f.flush();
 assert.equal(f.pendingTimers(),1,'authorization completion does not stack timers');
});

test('automatic actions wait for the initial authorization and resume after it completes',async()=>{
 let authorize;
 const gate=new Promise(resolve=>{authorize=resolve;});
 const f=fixture({respond:args=>{
  if(args.action==='authorize')return gate;
  if(args.action==='status')return {state:'available',version:'1.2.4',current_version:'1.2.3'};
  return {state:'ready'};
 }});
 f.boot();
 await f.advance(15000);
 assert.equal(f.calls.filter(call=>call.action==='download').length,0);
 authorize({authorization_revision:1});
 await f.flush();
 assert.equal(f.calls.filter(call=>call.action==='download').length,1,'the pending action resumes without another timer');
});

test('a settled visible client reads status on a five-minute cadence, not every second',async()=>{
 const f=fixture();
 f.boot();
 await f.advance(5000);
 assert.equal(f.statusCount(),1,'the boot read still happens');
 await f.advance(600000);
 const calls=f.statusCalls();
 assert.ok(calls.length<=3,'ten minutes hold the boot read plus two five-minute reads');
 assert.ok(calls.length>=2,'status polling must not stop entirely');
 for(let i=1;i<calls.length;i++)assert.ok(calls[i].at-calls[i-1].at>=300000,`gap ${calls[i].at-calls[i-1].at}ms`);
});

test('a hidden window is capped at one status read per minute',async()=>{
 const f=fixture({visibility:'hidden'});
 f.boot();
 await f.advance(5000);
 const before=f.statusCount();
 await f.advance(600000);
 const hidden=f.statusCalls().slice(before);
 assert.ok(hidden.length<=10,`${hidden.length} reads in ten hidden minutes`);
 assert.ok(hidden.length>=9,'a hidden window still notices downloads finishing');
 for(let i=1;i<hidden.length;i++)assert.ok(hidden[i].at-hidden[i-1].at>=60000,`gap ${hidden[i].at-hidden[i-1].at}ms`);
});

test('an active download keeps one-second progress updates while visible',async()=>{
 let downloaded=0;
 const f=fixture({respond:()=>({state:'downloading',bytes:downloaded+=1000,total:100000})});
 f.boot();
 await f.advance(5000);
 const before=f.statusCount();
 await f.advance(10000);
 const calls=f.statusCalls().slice(before);
 assert.equal(calls.length,10);
 for(let i=1;i<calls.length;i++)assert.equal(calls[i].at-calls[i-1].at,1000);
 assert.match(f.text(),/\d+%/,'progress keeps rendering during a download');
 assert.equal(f.api.pollDelay(),1000);
});

test('poll cadence follows native state and visibility',async()=>{
 const settled=fixture();
 await settled.api.tick();
 assert.equal(settled.api.pollDelay(),300000);
 const downloading=fixture({respond:()=>({state:'downloading',bytes:1,total:2})});
 await downloading.api.tick();
 assert.equal(downloading.api.pollDelay(),1000);
 const available=fixture({respond:()=>({state:'available',version:'1.2.4'})});
 await available.api.tick();
 assert.equal(available.api.pollDelay(),5000);
 const hidden=fixture({visibility:'hidden',respond:()=>({state:'downloading',bytes:1,total:2})});
 await hidden.api.tick();
 assert.equal(hidden.api.pollDelay(),60000);
});

test('returning to the foreground refreshes once and never stacks timers',async()=>{
 const f=fixture();
 f.boot();
 await f.advance(5000);
 const booted=f.statusCount();
 f.setVisibility('hidden');
 await f.flush();
 assert.equal(f.statusCount(),booted,'hiding does not read status again');
 assert.equal(f.pendingTimers(),1);
 await f.advance(59999);
 assert.equal(f.statusCount(),booted,'a hidden window waits the full minute');
 await f.advance(1);
 assert.equal(f.statusCount(),booted+1);
 const before=f.statusCount();
 f.setVisibility('visible');
 await f.flush();
 assert.equal(f.statusCount(),before+1,'the foreground catch-up read happens exactly once');
 assert.equal(f.pendingTimers(),1,'the catch-up replaces the timer instead of stacking one');
 f.setVisibility('visible');
 await f.flush();
 f.setVisibility('visible');
 await f.flush();
 assert.equal(f.pendingTimers(),1,'repeated visibility events keep a single timer');
});

test('an unchanged snapshot does not rewrite option or status text',async()=>{
 const f=fixture({policy:'manual',respond:()=>({state:'latest',version:'1.2.3'})});
 f.api.render();
 await f.api.tick();
 await f.flush();
 f.resetWrites();
 for(let i=0;i<5;i++)await f.api.tick();
 assert.equal(f.writes.total,0,'identical snapshots must not touch the panel');
 assert.equal(f.writes.option,0,'option labels must not be rewritten every tick');
 f.api.render();
 await f.flush();
 assert.equal(f.writes.total,0,'a language-neutral re-render of the same data is also free');
});

test('a long automatic download keeps status reads, progress and cancel live',async()=>{
 // The native download call stays pending for the whole transfer; status must
 // still be read while it runs, exactly like the real IPC channel.
 const gate=new Promise(()=>{});
 let started=false,bytes=0;
 const f=fixture({respond:args=>{
  if(args.action==='download'){started=true;return gate;}
  return started?{state:'downloading',bytes:bytes+=1000,total:100000}:{state:'available',version:'1.2.4',current_version:'1.2.3'};
 }});
 f.boot();
 await f.advance(5000);
 assert.ok(started,'the available package starts downloading automatically');
 const before=f.statusCount();
 await f.advance(15000);
 const calls=f.statusCalls().slice(before);
 assert.ok(calls.length>=10,`${calls.length} status reads while the download invoke is pending`);
 for(let i=1;i<calls.length;i++)assert.ok(calls[i].at-calls[i-1].at<=1000,`gap ${calls[i].at-calls[i-1].at}ms during the download`);
 assert.match(f.text(),/\d+%/,'progress keeps rendering while the download runs');
 assert.equal(f.button('取消').hidden,false,'the cancel button appears during the download');
 assert.equal(f.pendingTimers(),1,'a single poll timer stays armed');
});

test('a user-started download replaces the pending idle timer',async()=>{
 // A manual download leaves its IPC call pending, like the automatic one, so the
 // timer must move to the active cadence while that call is still running.
 const gate=new Promise(()=>{});
 let started=false;
 const f=fixture({respond:args=>{
  if(args.action==='download'){started=true;return gate;}
  return started?{state:'downloading',bytes:25000,total:100000}:{state:'latest',version:'1.2.3'};
 }});
 f.boot();
 await f.advance(5000);
 assert.equal(f.api.pollDelay(),300000,'a settled client waits five minutes');
 const before=f.statusCount();
 f.api.run('download');
 await f.flush();
 assert.equal(f.api.pollDelay(),1000,'the pending action switches the cadence to active');
 await f.advance(3000);
 assert.ok(f.statusCount()-before>=3,`${f.statusCount()-before} progress reads in three seconds`);
 assert.match(f.text(),/25%/,'progress replaces the stale settled view');
 assert.equal(f.button('取消').hidden,false,'the download is cancellable right away');
});
