'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');

function fixture({runtime, snapshot={state:'idle'}}={}){
 let panel;const saved=new Map();
 const el=(tag,text,className)=>({tag,className,textContent:text||'',children:[],append(...nodes){this.children.push(...nodes);},setAttribute(){},after(node){panel=node;}});
 const context={window:{RSIDesktop:{invoke:async()=>snapshot,ready:runtime}},document:{activeElement:null,addEventListener(){}},el,$:()=>el('anchor'),t:x=>x,localRead:key=>saved.get(key),localStorage:{setItem(key,value){saved.set(key,value);}},safe:fn=>fn(),setTimeout(){},setInterval(){},Date,Math,Number,String,Error};
 vm.runInNewContext(fs.readFileSync('adaptive_rsi/web/native-update.js','utf8'),context);
 const api=context.window.RSINativeUpdate;api.render();
 const all=node=>[node,...node.children.flatMap(all)];
 return{api,run:async action=>{await api.run(action);return all(panel);},nodes:()=>all(panel)};
}

test('unknown or zero download total never shows a percentage or a guessed size',()=>{
 const {api}=fixture();
 for(const total of [undefined,null,0,'','x',-1]){
  const text=api.progressText({state:'downloading',bytes:5*1048576,total,speed:2*1048576});
  assert.equal(text.includes('%'),false,text);
  assert.equal(text.includes(' / '),false,text);
  assert.match(text,/^5\.0 MB · 2\.0 MB\/s$/);
 }
 assert.equal(api.progressText({state:'downloading',bytes:0,total:0}),'0 B');
});

test('known total adds percentage and the real total',()=>{
 const {api}=fixture();
 assert.equal(api.progressText({state:'downloading',bytes:1048576,total:4194304}),'25% · 1.0 MB / 4.0 MB');
});

test('speed is hidden when missing or non-positive and formatted when present',()=>{
 const {api}=fixture();
 for(const value of [undefined,null,0,-1,NaN,'unknown'])assert.equal(api.speedText(value),'',String(value));
 assert.equal(api.speedText(2500000),'2.4 MB/s');
 assert.equal(api.speedText(1048576),'1.0 MB/s');
 assert.equal(api.speedText(2048),'2 KB/s');
 assert.equal(api.progressText({state:'downloading',bytes:1048576,total:4194304}).includes('/s'),false);
 assert.equal(api.sampleSpeed(0,1048576,1000),1048576);
 assert.equal(api.sampleSpeed(0,1048576,0),0);
 assert.equal(api.sampleSpeed(10,5,1000),0);
});

test('scope wording distinguishes owned local, unowned local and remote runtimes',()=>{
 const {api}=fixture();
 assert.equal(api.scopeText({kind:'local',owned:true}),'更新本机客户端并先排空自有服务任务。');
 assert.equal(api.scopeText({kind:'local',owned:false}),'只更新本机客户端，不停止已有本机服务。');
 assert.equal(api.scopeText({kind:'remote',owned:true}),'只更新本机客户端，远端服务不受影响。');
 const neutral='更新本机客户端；当前无法判断服务影响范围。';
 for(const value of [undefined,null,{}, {kind:'unknown'}, 'remote'])assert.equal(api.scopeText(value),neutral,String(value));
});

test('incompatible platform error is explained and existing error codes are kept',()=>{
 const {api}=fixture();
 assert.equal(api.errorText('update_incompatible_platform'),'此更新包与当前系统或架构不兼容，当前版本继续可用。');
 assert.equal(api.errorText('update_disk_space'),'磁盘可用空间不足，请清理后重试。');
 assert.equal(api.errorText('update_wait_cancelled'),'已取消等待更新，当前任务继续运行。');
 assert.equal(api.errorText('update_install_failed'),'安装未完成，请重试或手动安装。');
 assert.equal(api.errorText('something_new'),'更新未完成，请重试或手动安装。');
});

test('verifying and replacing phases have their own wording and waiting shows the reason count',()=>{
 const {api}=fixture();
 assert.equal(api.phaseOf({state:'verifying',stage:'verifying',bytes:10,total:10}),'verifying');
 assert.equal(api.phaseOf({state:'downloading',bytes:10,total:10}),'verifying');
 assert.equal(api.phaseOf({state:'downloading',bytes:10,total:0}),'downloading');
 assert.equal(api.phaseOf({state:'downloading',bytes:10,total:20}),'downloading');
 assert.equal(api.phaseOf({state:'installing',stage:'installing'}),'replacing');
 assert.equal(api.statusText({state:'downloading',bytes:10,total:10}),'正在校验签名');
 assert.equal(api.statusText({state:'verifying',bytes:10,total:10}),'正在校验签名');
 assert.equal(api.statusText({state:'installing'}),'正在替换应用');
 assert.equal(api.progressText({state:'verifying',stage:'verifying',bytes:10,total:10}),'');
 assert.equal(api.statusText({state:'waiting_idle',reasons:['active_task','disk_busy','model_busy']}),'等待 3 项本机任务结束后更新');
 assert.equal(api.statusText({state:'waiting_idle',reasons:[]}),'等待当前任务结束后更新');
 assert.equal(api.statusText({state:'waiting_idle'}),'等待当前任务结束后更新');
 assert.equal(api.statusText({state:'latest'}),'已是最新稳定版');
});

test('rendered panel hides progress while verifying and follows runtime_info scope',async()=>{
 const local=fixture({runtime:Promise.resolve({kind:'local',owned:true}),snapshot:{state:'downloading',bytes:100,total:100}});
 const nodes=await local.run('status');
 const progress=nodes.find(node=>node.tag==='p'&&node.className===undefined&&node.hidden===true);
 assert.ok(progress,'progress is hidden while verifying');
 assert.equal(progress.textContent,'');
 assert.ok(nodes.some(node=>node.textContent==='正在校验签名'),'verifying label must be rendered');
 await Promise.resolve();await Promise.resolve();
 assert.ok(local.nodes().some(node=>node.textContent==='更新本机客户端并先排空自有服务任务。'));
 const remote=fixture({runtime:Promise.resolve({kind:'remote'}),snapshot:{state:'idle'}});
 await Promise.resolve();await Promise.resolve();
 assert.ok(remote.nodes().some(node=>node.textContent==='只更新本机客户端，远端服务不受影响。'));
 const missing=fixture({snapshot:{state:'idle'}});
 await Promise.resolve();await Promise.resolve();
 assert.ok(missing.nodes().some(node=>node.textContent==='更新本机客户端；当前无法判断服务影响范围。'));
});

test('rendered download progress contains bytes, total and rate only when known',async()=>{
 const known=fixture({snapshot:{state:'downloading',stage:'downloading',bytes:2097152,total:4194304}});
 let nodes=await known.run('status');
 let progress=nodes.find(node=>node.tag==='p'&&node.className===undefined&&node.hidden===false);
 assert.equal(progress.textContent,'50% · 2.0 MB / 4.0 MB');
 const unknown=fixture({snapshot:{state:'downloading',stage:'downloading',bytes:2097152,total:null,bytes_per_second:null}});
 nodes=await unknown.run('status');
 progress=nodes.find(node=>node.tag==='p'&&node.className===undefined&&node.hidden===false);
 assert.equal(progress.textContent,'2.0 MB');
 assert.equal(progress.textContent.includes('%'),false);
 assert.equal(progress.textContent.includes('/s'),false);
 const native=fixture({snapshot:{state:'downloading',stage:'downloading',bytes:1048576,total:2097152,bytes_per_second:1048576}});
 nodes=await native.run('status');
 progress=nodes.find(node=>node.tag==='p'&&node.className===undefined&&node.hidden===false);
 assert.equal(progress.textContent,'50% · 1.0 MB / 2.0 MB · 1.0 MB/s');
});
