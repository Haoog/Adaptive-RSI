'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
function worker({open, fetch} = {}) {
 const handlers = {}, removed = [];
 const context = { URL, Set, Promise, fetch, self: {location: {origin:'https://rsi.test'},addEventListener:(name,fn)=>handlers[name]=fn,skipWaiting(){},clients:{claim(){}}},caches:{open, keys:async()=>['other-app','rsi-shell-v1','rsi-shell-icons-v2','rsi-shell-icons-v3'],delete:async name=>removed.push(name)}};
 vm.runInNewContext(fs.readFileSync('adaptive_rsi/web/sw.js','utf8'),context);
 return {handlers,removed};
}
test('service worker excludes credentials, navigation and executable assets',()=>{
 const {handlers}=worker();
 for(const path of ['/?token=private','/app.js','/i18n.js','/style.css','/workspace.css','/','/icon-192.png?token=private','/api/projects']){
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
 assert.deepEqual(removed,['rsi-shell-v1','rsi-shell-icons-v2']);
});

test('public icons are cached on a miss and served offline on a hit', async () => {
 const request = {method: 'GET', url: 'https://rsi.test/icon-192.png'};
 const copy = {body: 'copied icon'};
 const network = {ok: true, redirected: false, clone: () => copy};
 const writes = [];
 let cached, offline = false, requests = 0;
 const {handlers} = worker({
  async open(name) {
   assert.equal(name, 'rsi-shell-icons-v3');
   return {
    async match(key) {assert.equal(key, request); return cached;},
    async put(key, value) {writes.push([key, value]); cached = value;},
   };
  },
  async fetch(key) {
   assert.equal(key, request);
   requests++;
   if (offline) throw new Error('offline');
   return network;
  },
 });
 async function loadIcon() {
  let response;
  const pending = [];
  handlers.fetch({request, respondWith(value) {response = value;}, waitUntil(value) {pending.push(value);}});
  assert.ok(response, 'the worker handles cacheable icon requests');
  const result = await response;
  await Promise.all(pending);
  return result;
 }
 assert.equal(await loadIcon(), network);
 assert.deepEqual(writes, [[request, copy]]);
 offline = true;
 assert.equal(await loadIcon(), copy);
 assert.equal(requests, 2);
 assert.equal(writes.length, 1, 'a failed refresh preserves the cached icon');
});

test('a cached icon responds immediately and extends the fetch event until the cache write finishes', async () => {
 const oldIcon = {}, freshIcon = {ok:true, redirected:false, clone:()=>({new:true})};
 let deliver, finishWrite, lifetime, response, dispatching = true;
 const {handlers} = worker({
  fetch: () => new Promise(resolve => { deliver = resolve; }),
  open: async () => ({match: async () => oldIcon, put: async (_request, icon) => {
   assert.deepEqual(icon, {new:true});
   await new Promise(resolve => { finishWrite = resolve; });
  }}),
 });
 handlers.fetch({request:{method:'GET',url:'https://rsi.test/icon-192.png'},
  respondWith(promise){response=promise;},
  waitUntil(promise){assert.equal(dispatching,true);lifetime=promise;}});
 dispatching=false;
 assert.equal(await response,oldIcon);
 assert.ok(lifetime);
 let settled=false;lifetime.then(()=>{settled=true;});
 deliver(freshIcon);
 await new Promise(setImmediate);
 assert.equal(settled,false);
 finishWrite();await lifetime;
 assert.equal(settled,true);
});
