'use strict';
const {spawn}=require('node:child_process');
const {readdirSync,mkdirSync,writeFileSync}=require('node:fs');
const path=require('node:path');
const root=path.resolve(__dirname,'..');
const files=readdirSync(path.join(root,'tests')).filter(name=>/^browser_.*\.cjs$/.test(name)).sort();
const concurrency=Number(process.env.RSI_BROWSER_WORKERS||2);
if(!Number.isInteger(concurrency)||concurrency<1||concurrency>4)throw Error('RSI_BROWSER_WORKERS must be 1..4');
const available=files.flatMap(file=>file==='browser_hardening.cjs'?['core','providers','routes','updates','settings','recovery'].map(group=>({file,group,label:file+':'+group})):[{file,label:file}]);
const args=process.argv.slice(2),listing=args.includes('--list'),selectors=args.filter(arg=>arg!=='--list');
if(new Set(selectors).size!==selectors.length)throw Error('Browser selectors must be unique');
for(const selector of selectors)if(!available.some(item=>item.file===selector||item.label===selector))throw Error('Unknown browser selector: '+selector);
const pending=available.filter(item=>!selectors.length||selectors.includes(item.file)||selectors.includes(item.label)),active=new Set(),timings={};
if(!pending.length)throw Error('No browser scenarios found');
let failed=false,stopping=false,finished=false;
const upgrades=new Map(),finalizers=[];
let keepAlive=null;
function groupAlive(pid){
 if(typeof pid!=='number')return false;
 try{process.kill(-pid,0);return true;}catch(error){return error.code!=='ESRCH';}
}
function isCleanupPending(){return upgrades.size>0;}
function settle(child){
 const entry=upgrades.get(child);
 if(!entry||!entry.closed)return;
 if(!entry.fired&&groupAlive(child.pid))return;
 upgrades.delete(child);
 if(!upgrades.size&&keepAlive){clearInterval(keepAlive);keepAlive=null;for(const finish of finalizers.splice(0))finish();}
}
function armUpgrade(child,closed){
 const entry={fired:false,closed,timer:setTimeout(()=>{entry.fired=true;try{process.kill(-child.pid,'SIGKILL');}catch{}settle(child);},1500)};
 entry.timer.unref();
 upgrades.set(child,entry);
 if(!keepAlive)keepAlive=setInterval(()=>{},1000);
}
function terminate(child){
 try{process.kill(-child.pid,'SIGTERM');}catch{}
 if(child.pid===undefined||upgrades.has(child))return;
 armUpgrade(child,false);
}
for(const signal of ['SIGINT','SIGTERM'])process.on(signal,()=>{stopping=true;failed=true;pending.length=0;for(const child of active)terminate(child);});
function next(){
 while(!stopping&&active.size<concurrency&&pending.length){
  const {file,group,label}=pending.shift(),began=Date.now();let timedOut=false;
  const child=spawn(process.execPath,[path.join(root,'tests',file)],{cwd:root,stdio:'inherit',detached:true,env:{...process.env,RSI_BROWSER_HARDENING_GROUP:group||''}});
  active.add(child);
  const limit=setTimeout(()=>{failed=true;timedOut=true;console.error('[browser] timeout: '+label);terminate(child);},180000);
  child.once('error',error=>{failed=true;console.error('[browser] '+label+': '+error.message);});
  child.once('close',code=>{
   clearTimeout(limit);active.delete(child);failed=failed||code!==0;
   timings[label]={seconds:(Date.now()-began)/1000,exit_code:code,timed_out:timedOut};
   console.log('[browser] '+label+' '+timings[label].seconds+'s exit='+code+' timed_out='+timedOut);
   if(upgrades.has(child))upgrades.get(child).closed=true;
   else if(groupAlive(child.pid)){try{process.kill(-child.pid,'SIGTERM');}catch{}armUpgrade(child,true);}
   settle(child);next();
  });
 }
 if(!pending.length&&!active.size){
  const finish=()=>{if(finished)return;finished=true;mkdirSync(path.join(root,'.ci'),{recursive:true});writeFileSync(path.join(root,'.ci/browser-durations.json'),JSON.stringify(timings,null,2)+'\n');process.exitCode=failed?1:0;};
  if(isCleanupPending())finalizers.push(finish);else finish();
 }
}
globalThis.isCleanupPending=isCleanupPending;
if(listing)for(const item of pending)console.log(item.label);else next();
