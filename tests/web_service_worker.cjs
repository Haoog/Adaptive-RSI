'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
function worker() {
 const handlers = {}, removed = [];
 const context = { URL, Set, Promise, self: {location: {origin:'https://rsi.test'},addEventListener:(name,fn)=>handlers[name]=fn,skipWaiting(){},clients:{claim(){}}},caches:{keys:async()=>['other-app','rsi-shell-v1','rsi-shell-icons-v2'],delete:async name=>removed.push(name)}};
 vm.runInNewContext(fs.readFileSync('adaptive_rsi/web/sw.js','utf8'),context);
 return {handlers,removed};
}
test('service worker excludes credentials, navigation and executable assets',()=>{
 const {handlers}=worker();
 for(const path of ['/?token=private','/app.js','/i18n.js','/style.css','/','/icon-192.png?token=private','/api/projects']){
  let intercepted=false;
  handlers.fetch({request:{method:'GET',url:'https://rsi.test'+path},respondWith(){intercepted=true;}});
  assert.equal(intercepted,false,path);
 }
});
test('activation deletes only this application shell caches',async()=>{
 const {handlers,removed}=worker();
 let finished;
 handlers.activate({waitUntil(promise){finished=promise;}});
 await finished;
 assert.deepEqual(removed,['rsi-shell-v1']);
});
