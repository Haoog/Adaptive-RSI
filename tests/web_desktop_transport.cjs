'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
const {webcrypto}=require('node:crypto');
test('desktop abort forwards the same request ID and releases the caller',async()=>{
 const calls=[];let release;
 const window={fetch:async()=>{},__TAURI_INTERNALS__:{invoke:async(command,args)=>{
  calls.push({command,args});
  if(command==='runtime_info')return {kind:'local'};
  if(command==='request')return new Promise(resolve=>{release=resolve;});
 }}};
 vm.runInNewContext(fs.readFileSync('adaptive_rsi/web/desktop.js','utf8'),{window,document:{addEventListener(){}},crypto:webcrypto,Response,DOMException,console});
 await window.RSIDesktop.ready;
 const controller=new AbortController(),request=window.fetch('/api/state',{signal:controller.signal});
 await new Promise(setImmediate);
 const active=calls.find(call=>call.command==='request');assert.ok(active.args.requestId);
 controller.abort();await assert.rejects(request,{name:'AbortError'});
 assert.equal(calls.find(call=>call.command==='cancel_request').args.id,active.args.requestId);
 release({body:'{}',status:200});await new Promise(setImmediate);
});
