'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');

function fixture({cancelError,authorizeError,appliedOverride,readyState,wait=false,deferRevision=0,deferredResult,storePreference=()=>{},nativeRevision=0}={}){
 let panel,finish,release;const calls=[];let stored=nativeRevision;
 const el=(tag,text,className)=>({tag,className,textContent:text||'',children:[],append(...nodes){this.children.push(...nodes);},setAttribute(){},after(node){panel=node;}});
 const invoke=async(_,args)=>{
  calls.push(args);
  if(args.action==='authorize'){
   const error=typeof authorizeError==='function'?authorizeError(args):authorizeError;
   if(error)throw Error(error);
   // The native process keeps its counter across a webview reload and refuses a
   // revision that is not strictly newer, replying with the one it still holds.
   if(args.revision<=stored)return{state:'waiting_idle',authorization_revision:stored};
   stored=args.revision;
   const reply={state:'waiting_idle',authorization_revision:args.revision,
    authorization:{policy:args.policy,scope:args.versionScope,window:args.installationWindow,...appliedOverride}};
   if(args.revision!==deferRevision)return reply;
   // Simulate an out-of-order reply from an older revision.
   return new Promise((resolve,reject)=>{release=()=>deferredResult instanceof Error?reject(deferredResult):resolve(deferredResult||reply);});
  }
  if(args.action==='cancel'){
   if(cancelError)throw Error(cancelError);
   return{state:'waiting_idle'};
  }
  if(args.action==='install'){
   // Native require_authorization: only the stored revision may install.
   if(args.revision!==stored)return{state:'error',error_code:'update_authorization_changed'};
   if(!wait)return{state:'ready'};
   return new Promise(resolve=>{finish=resolve;});
  }
  return{state:'waiting_idle',authorization_revision:stored};
 };
 const context={window:{RSIDesktop:{invoke,ready:Promise.resolve({kind:'local',owned:true})}},
  document:{activeElement:null,readyState,addEventListener(){},documentElement:{lang:'zh-CN'}},
  el,$:()=>el('anchor'),t:x=>x,localRead:key=>key==='rsi-native-update-policy'?'automatic':null,
  localStorage:{setItem:storePreference},safe:fn=>fn(),setTimeout(){},setInterval(){},Date,Math,Number,String,Error,JSON};
 vm.runInNewContext(fs.readFileSync('adaptive_rsi/web/native-update.js','utf8'),context);
 const api=context.window.RSINativeUpdate;api.render();
 const all=node=>[node,...node.children.flatMap(all)];
 const policy=()=>all(panel).find(node=>node.tag==='label').children.find(node=>node.tag==='select');
 const text=()=>all(panel).map(node=>node.textContent).join(' ');
 return{api,calls,policy,text,storageWarning:()=>all(panel).find(node=>node.textContent.includes('偏好未保存')),selects:()=>all(panel).filter(node=>node.tag==='select'),
  nativeRevision:()=>stored,advanceNativeRevision:value=>{stored=value;},resolve:value=>finish(value),release:()=>release()};
}

test('tightening automatic to download cancels an automatic install waiting to replace',async()=>{
 const f=fixture({wait:true});
 const pending=f.api.run('install',true);await f.api.tick();
 const policy=f.policy();policy.value='download';policy.onchange();
 // Wait for the asynchronous authorize -> cancel sequence.
 for(let i=0;i<20&&!f.calls.some(args=>args.action==='cancel');i++)await new Promise(resolve=>setImmediate(resolve));
 const actions=f.calls.map(args=>args.action);
 assert.ok(actions.includes('authorize'),actions.join(','));
 assert.ok(actions.includes('cancel'),actions.join(','));
 assert.ok(actions.indexOf('authorize')<actions.indexOf('cancel'),'the new revision is stored before the old install is revoked');
 assert.equal(f.calls.find(args=>args.action==='authorize'&&args.revision>1).policy,'download');
 f.resolve({state:'ready'});await pending;
});

test('a failed cancel is surfaced instead of pretending the replacement stopped',async()=>{
 const f=fixture({cancelError:'update_cancel_failed',wait:true});
 const pending=f.api.run('install',true);await f.api.tick();
 const policy=f.policy();policy.value='download';policy.onchange();
 for(let i=0;i<20&&f.api.errorCode()!=='update_cancel_failed';i++)await new Promise(resolve=>setImmediate(resolve));
 assert.equal(f.api.errorCode(),'update_cancel_failed');
 assert.match(f.text(),/未能确认本机已撤销安装/);
 f.resolve({state:'error',error_code:'update_wait_cancelled'});
 await pending;
 // The failed install result still drives the panel; the cancel error is kept
 // visible instead of being replaced by a success state.
 assert.equal(f.api.errorCode(),'update_cancel_failed');
});

test('a rejected authorization still revokes the waiting install and is reported',async()=>{
 const f=fixture({authorizeError:args=>args.revision>1?'update_authorization_failed':null,wait:true});
 const pending=f.api.run('install',true);await f.api.tick();
 const policy=f.policy();policy.value='download';policy.onchange();
 for(let i=0;i<20&&!f.calls.some(args=>args.action==='cancel');i++)await new Promise(resolve=>setImmediate(resolve));
 assert.ok(f.calls.some(args=>args.action==='cancel'),'an install approved under stale consent must not continue');
 assert.equal(f.api.errorCode(),'update_authorization_failed');
 assert.match(f.text(),/更新设置未能同步到本机/);
 f.resolve({state:'error',error_code:'update_authorization_changed'});
 await pending;
});

test('manual installs carry the authorization revision the panel holds',async()=>{
 const f=fixture();
 await f.api.run('install');
 const install=f.calls.find(args=>args.action==='install');
 assert.equal(typeof install.revision,'number');
 assert.equal(install.installationWindow,'any');
 await f.api.syncAuthorization();
 const authorize=f.calls.filter(args=>args.action==='authorize').pop();
 assert.ok(authorize.revision>install.revision,'settings changes advance the revision');
});

test('an out-of-order reply from an older revision is not reported as a failure',async()=>{
 const f=fixture({deferRevision:1});
 f.api.render();
 const [policy, scope]=f.selects();
 policy.value='download';policy.onchange();
 // The second change supersedes revision 1 while its reply is still in flight.
 scope.value='minor';scope.onchange();
 for(let i=0;i<20&&f.calls.filter(args=>args.action==='authorize').length<2;i++)await new Promise(resolve=>setImmediate(resolve));
 f.release();
 for(let i=0;i<20;i++)await new Promise(resolve=>setImmediate(resolve));
 assert.deepEqual(f.calls.filter(args=>args.action==='authorize').map(args=>[args.revision,args.versionScope]),[[1,'patch'],[2,'minor']]);
 assert.equal(f.api.errorCode(),'');
 assert.equal(f.api.revision(),2);
});

// A reloaded webview starts its counter at 0 while the native process keeps the
// stored authorization past 0. Every later change must still be stored, and the
// install must bind to the revision the native side actually holds.
test('a reloaded panel continues the stored revision instead of replaying 1',async()=>{
 const f=fixture({nativeRevision:3});
 await f.api.tick();
 const policy=f.policy();policy.value='download';policy.onchange();
 for(let i=0;i<20&&!f.calls.some(args=>args.action==='authorize');i++)await new Promise(resolve=>setImmediate(resolve));
 const authorize=f.calls.filter(args=>args.action==='authorize').pop();
 assert.equal(authorize.revision,4);
 assert.equal(f.nativeRevision(),4);
 assert.equal(f.api.errorCode(),'');
 assert.equal(f.api.revision(),4);
});

test('a refused authorization is re-issued under the stored revision',async()=>{
 // The panel changes a setting before any status reply seeded it, so its first
 // revision is stale. The refusal must not leave the panel unable to authorize.
 const f=fixture({nativeRevision:3});
 await f.api.syncAuthorization();
 assert.deepEqual(f.calls.filter(args=>args.action==='authorize').map(args=>args.revision),[1,4]);
 assert.equal(f.nativeRevision(),4);
 assert.equal(f.api.errorCode(),'');
});

test('an install after a reload carries the revision the native side still holds',async()=>{
 const f=fixture({nativeRevision:3});
 await f.api.tick();
 await f.api.run('install');
 const install=f.calls.find(args=>args.action==='install');
 assert.equal(install.revision,4);
 assert.equal(f.api.errorCode(),'');
});

for(const failure of [false,true])test(`an older ${failure?'rejected':'successful'} authorization leaves the latest result visible`,async()=>{
 const f=fixture({deferRevision:1,deferredResult:failure?Error('stale authorization'):undefined,
  authorizeError:args=>!failure&&args.revision===2?'update_authorization_invalid':null});
 const older=f.api.syncAuthorization();
 await f.api.syncAuthorization();
 f.release();await older;
 assert.equal(f.api.errorCode(),failure?'':'update_authorization_invalid');
});

test('a successful preference save retries a previously failed write and clears the warning', async () => {
 const saved=new Map();let writable=false;
 const f=fixture({storePreference(key,value){if(!writable)throw Error('temporary storage failure');saved.set(key,value);}});
 const [,scope,window]=f.selects();
 scope.value='minor';scope.onchange();
 await new Promise(setImmediate);
 assert.equal(f.storageWarning().hidden,false);
 writable=true;window.value='night';window.onchange();
 await new Promise(setImmediate);
 assert.equal(f.storageWarning().hidden,true);
 assert.equal(saved.get('rsi-native-update-scope'),'minor');
 assert.equal(saved.get('rsi-native-update-window'),'night');
});

for(const appliedOverride of [{policy:'download'},{scope:'stable'},{window:'night'}]){
 test(`matching revision with different ${Object.keys(appliedOverride)[0]} cannot start an install`,async()=>{
  const f=fixture({appliedOverride,readyState:'complete'});
  await f.api.run('install');
  assert.equal(f.api.errorCode(),'update_authorization_failed');
  assert.equal(f.calls.some(args=>args.action==='install'),false);
 });
}

test('install reauthorizes instead of borrowing a revision observed only by polling',async()=>{
 const f=fixture();
 await f.api.run('install');
 f.advanceNativeRevision(4);
 await f.api.tick();
 await f.api.run('install');
 assert.deepEqual(f.calls.filter(args=>args.action==='authorize').map(args=>args.revision),[1,5]);
 assert.deepEqual(f.calls.filter(args=>args.action==='install').map(args=>args.revision),[1,5]);
});
