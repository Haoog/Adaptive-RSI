'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');

const source=()=>fs.readFileSync('adaptive_rsi/web/native-update.js','utf8');

function fixture({runtime, snapshot={state:'idle'}, stored, receiptError, cancelError, authorizeError}={}){
 let panel;const saved=new Map(),calls=[],blobs=[],downloads=[];
 const el=(tag,text,className)=>({tag,className,textContent:text||'',children:[],append(...nodes){this.children.push(...nodes);},setAttribute(){},after(node){panel=node;}});
 const invoke=async(_,args)=>{
  calls.push(args);
  if(args.action==='authorize'){
   if(authorizeError)throw Error(authorizeError);
   return Object.assign({state:'idle'},snapshot,{authorization_revision:args.revision});
  }
  if(args.action==='receipt'){
   if(receiptError)throw Error(receiptError);
   return stored||{available:false,error:'update_receipt_missing'};
  }
  if(args.action==='cancel'&&cancelError)throw Error(cancelError);
  return snapshot;
 };
 const context={
  window:{RSIDesktop:{invoke,ready:runtime},navigator:{platform:'MacIntel'}},
  document:{activeElement:null,addEventListener(){},documentElement:{lang:'zh-CN'},
   createElement:()=>({click(){downloads.push(this.download);},href:'',download:''})},
  Blob:class{constructor(parts,options){blobs.push({parts,type:options&&options.type});}},
  URL:{createObjectURL:()=>'blob:receipt',revokeObjectURL(){}},
  el,$:()=>el('anchor'),t:x=>x,localRead:key=>saved.get(key),
  localStorage:{setItem(key,value){saved.set(key,value);}},
  safe:fn=>fn(),setTimeout(){},setInterval(){},Date,Math,Number,String,Error,JSON,
 };
 vm.runInNewContext(source(),context);
 const api=context.window.RSINativeUpdate;
 const all=node=>[node,...node.children.flatMap(all)];
 const text=()=>all(panel).map(node=>node.textContent).join(' ');
 return{api,calls,saved,blobs,downloads,nodes:()=>all(panel),text,panel:()=>panel,
  selects:()=>all(panel).filter(node=>node.tag==='select')};
}

test('exporting a receipt reads the persisted transaction instead of panel state',async()=>{
 const transaction={receipt_version:1,operation_id:'a1b2c3d4',from_version:'0.5.126',
  to_version:'0.5.127',artifact_sha256:'a'.repeat(64),signature_verified:true,
  state:'succeeded',error_code:null};
 const f=fixture({stored:{available:true,receipt:transaction},
  snapshot:{state:'ready',current_version:'0.5.126',version:'0.5.127',authorization_revision:4}});
 await f.api.run('status');
 f.api.render();
 const receipt=await f.api.exportReceipt();
 assert.deepEqual(f.calls.filter(args=>args.action==='receipt').length,1);
 assert.equal(receipt.receipt_version,2);
 assert.deepEqual(receipt.transaction,transaction);
 assert.equal(receipt.authorization_revision,4);
 assert.equal(receipt.transaction_error,null);
 assert.deepEqual(f.downloads,['adaptive-rsi-update-receipt.json']);
 assert.equal(f.blobs[0].type,'application/json');
 const text=f.blobs[0].parts[0];
 assert.ok(!/token|cookie|password|secret/i.test(text),text);
});

test('a missing or unreadable persisted transaction is reported, never invented',async()=>{
 const missing=fixture({stored:{available:false,error:'update_receipt_missing'}});
 const receipt=await missing.api.exportReceipt();
 assert.equal(receipt.transaction,null);
 assert.equal(receipt.transaction_error,'update_receipt_missing');
 const broken=fixture({receiptError:'update_receipt_invalid'});
 const failed=await broken.api.exportReceipt();
 assert.equal(failed.transaction,null);
 assert.equal(failed.transaction_error,'update_receipt_invalid');
});
