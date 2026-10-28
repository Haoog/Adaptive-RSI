'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
function fixture(failStorage=false){
 let panel,now=100000000;
 const calls=[],saved=new Map();
 const el=(tag,text)=>({tag,textContent:text||'',children:[],append(...nodes){this.children.push(...nodes)},setAttribute(){},after(node){panel=node;}});
 const context={window:{RSIDesktop:{invoke:async(_,{action})=>{calls.push(action);if(action==='check')throw Error('offline');return{state:'idle'};}}},document:{activeElement:null,addEventListener(){}},el,$:()=>el('anchor'),t:x=>x,localRead:key=>saved.get(key),localStorage:{setItem(key,value){if(failStorage)throw Error('disabled');saved.set(key,value);}},safe:fn=>fn(),setTimeout(){},setInterval(){},Date:{now:()=>now},Math,Number,String,Error};
 vm.runInNewContext(fs.readFileSync('adaptive_rsi/web/native-update.js','utf8'),context);
 const api=context.window.RSINativeUpdate;
 const descendants=node=>[node,...node.children.flatMap(descendants)];
 return{api,calls,saved,context,advance(ms){now+=ms;},select(){return descendants(panel).find(node=>node.tag==='select');},selects(){return descendants(panel).filter(node=>node.tag==='select');}};
}
test('polling preserves focused policy and notes nodes',()=>{
 const f=fixture();f.api.render();const select=f.select();f.context.document.activeElement=select;f.api.render();assert.equal(f.select(),select);
});
test('failed checks retry after short backoff without recording success',async()=>{
 const f=fixture();await f.api.tick();assert.equal(f.saved.has('rsi-native-update-checked'),false);f.advance(10000);await f.api.tick();assert.equal(f.calls.filter(action=>action==='check').length,2);
});
test('manual preference survives disabled browser storage for this session',async()=>{
 const f=fixture(true);f.api.render();f.select().value='manual';f.select().onchange();await f.api.tick();assert.deepEqual(f.calls,['status']);assert.equal(f.select().value,'manual');
});

test('automatic eligibility respects exact patch/minor/stable boundaries',()=>{
 const {api}=fixture();
 assert.equal(api.eligible('1.2.3','1.2.4','patch'),true);
 assert.equal(api.eligible('1.2.3','1.3.0','patch'),false);
 assert.equal(api.eligible('1.2.3','1.3.0','minor'),true);
 assert.equal(api.eligible('1.2.3','2.0.0','minor'),false);
 assert.equal(api.eligible('1.2.3','2.0.0','stable'),true);
 for(const value of ['1.2.3','1.2.2','bad','1.2.4-beta'])assert.equal(api.eligible('1.2.3',value,'stable'),false);
});
test('automatic install windows cover midnight and weekends in local time',()=>{
 const f=fixture();f.api.render();const select=f.selects()[2];
 select.value='night';select.onchange();
 for(const [hour,wanted] of [[21,false],[22,true],[0,true],[5,true],[6,false]])assert.equal(f.api.withinWindow({getHours:()=>hour}),wanted);
 select.value='weekend';select.onchange();
 for(const day of [0,1,5,6])assert.equal(f.api.withinWindow({getDay:()=>day}),day===0||day===6);
});
