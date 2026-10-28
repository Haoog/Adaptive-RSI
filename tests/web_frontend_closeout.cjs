'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=name=>fs.readFileSync('adaptive_rsi/web/'+name+'.js','utf8');
function node(tag,text){return {tag,textContent:text,children:[],dataset:{},append(...items){this.children.push(...items);},setAttribute(){},addEventListener(){},remove(){},focus(){},showModal(){this.open=true;},close(){this.open=false;this.onclose?.();}};}
function diagnostics(scope){
 const text=source('model-center'),start=text.indexOf('    diagnostics.onclick='),end=text.indexOf('    const authProvider=',start),calls=[];
 const context={diagnostics:{},scope,provider:{id:'local',catalog:{models:[{id:'model',enabled:true}]}},api:async(path,body)=>{calls.push(body);return{scope:body.project};},safe:fn=>fn(),el:node,t:x=>x,document:{body:node('body')},JSON};
 // Include the private helper, when present, and execute the actual assigned handler.
 const helper=text.match(/  function diagnosticDialog\([\s\S]*?\n  }/);
 vm.runInNewContext((helper?.[0]||'')+'\n'+text.slice(start,end),context);
 return {context,calls,run:()=>context.diagnostics.onclick()};
}
test('R4-015 successful diagnostics presents a visible, safely rendered result',async()=>{
 const fixture=diagnostics(null);await fixture.run();const box=fixture.context.document.body.children[0];assert.equal(box.open,true);assert.ok(box.children.some(item=>item.tag==='pre'&&item.textContent.includes('scope')));
});
