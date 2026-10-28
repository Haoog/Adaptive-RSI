'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
test('tightening automatic to download cancels an automatic install waiting to replace',async()=>{
 let panel,finish;const calls=[];
 const el=tag=>({tag,children:[],append(...nodes){this.children.push(...nodes);},setAttribute(){},after(node){panel=node;}});
 const context={window:{RSIDesktop:{invoke:async(_,args)=>{calls.push(args);if(args.action==='install')return new Promise(resolve=>{finish=resolve;});return{state:'waiting_idle'};}}},document:{activeElement:null,addEventListener(){}},el,$:()=>el('anchor'),t:x=>x,localRead:key=>key==='rsi-native-update-policy'?'automatic':null,localStorage:{setItem(){}},safe:fn=>fn(),setTimeout(){},setInterval(){},Date,Math,Number,String,Error};
 vm.runInNewContext(fs.readFileSync('adaptive_rsi/web/native-update.js','utf8'),context);
 const api=context.window.RSINativeUpdate;api.render();
 const pending=api.run('install',true);await api.tick();
 const policy=panel.children.find(node=>node.tag==='label').children.find(node=>node.tag==='select');
 policy.value='download';policy.onchange();
 assert.ok(calls.some(args=>args.action==='cancel'));
 finish({state:'ready'});await pending;
});