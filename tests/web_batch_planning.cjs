const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
function load() {
 const context = {window:{},Map,Set,t:value=>value,document:{querySelector:()=>null}};
 vm.createContext(context);
 vm.runInContext(fs.readFileSync('adaptive_rsi/web/batch.js','utf8'),context);
 return context.window.RSIBatch;
}
test('cost preview keeps unknown costs explicit without promising speedup',()=>{
 const batch=load();
 assert.match(batch.costText({}), /共享检查收益: 未知/);
 assert.match(batch.costText({separate_seconds:12}), /分别验证: 12.0 s/);
 assert.match(batch.costText({}), /不保证加速/);
});
test('evidence rendering is available to task detail',()=>{
 assert.equal(typeof load().evidence,'function');
});
