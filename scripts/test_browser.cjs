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
let failed=false,stopping=false;
function terminate(child){
 try{process.kill(-child.pid,'SIGTERM');}catch{}
 const timer=setTimeout(()=>{try{process.kill(-child.pid,'SIGKILL');}catch{}},1500);timer.unref();
}
for(const signal of ['SIGINT','SIGTERM'])process.on(signal,()=>{stopping=true;failed=true;pending.length=0;for(const child of active)terminate(child);});
function next(){
 while(!stopping&&active.size<concurrency&&pending.length){
  const {file,group,label}=pending.shift(),began=Date.now();let timedOut=false;
  const child=spawn(process.execPath,[path.join(root,'tests',file)],{cwd:root,stdio:'inherit',detached:true,env:{...process.env,RSI_BROWSER_HARDENING_GROUP:group||''}});
  active.add(child);
  const limit=setTimeout(()=>{failed=true;timedOut=true;console.error('[browser] timeout: '+label);terminate(child);},180000);
  child.once('error',error=>{failed=true;console.error('[browser] '+label+': '+error.message);});
  child.once('close',code=>{clearTimeout(limit);active.delete(child);failed=failed||code!==0;timings[label]={seconds:(Date.now()-began)/1000,exit_code:code,timed_out:timedOut};console.log('[browser] '+label+' '+timings[label].seconds+'s exit='+code+' timed_out='+timedOut);next();});
 }
 if(!pending.length&&!active.size){mkdirSync(path.join(root,'.ci'),{recursive:true});writeFileSync(path.join(root,'.ci/browser-durations.json'),JSON.stringify(timings,null,2)+'\n');process.exitCode=failed?1:0;}
}
if(listing)for(const item of pending)console.log(item.label);else next();
