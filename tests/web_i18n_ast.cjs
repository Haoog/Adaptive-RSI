'use strict';
const assert=require('node:assert/strict');
const test=require('node:test');
const {checkSource}=require('../scripts/check_i18n_ast.cjs');

test('dynamic lookup and aliases cannot hide missing translations',()=>{
  const source="const novel={ready:'已翻译',failed:'未翻译动态状态'}; const alias=novel; t(alias[current]);";
  const errors=checkSource(source,{'已翻译':'Translated'},'fixture.js');
  assert.equal(errors.length,1);assert.match(errors[0],/未翻译动态状态/);
});
test('ternaries, nested dictionaries and escaped Unicode use decoded text',()=>{
  const source=String.raw`const messages={nested:{ok:'\u4e2d\u6587'}}; t(flag ? messages.nested[key] : '另一个');`;
  assert.deepEqual(checkSource(source,{'中文':'Chinese','另一个':'Another'}),[]);
  assert.equal(checkSource(source,{}).length,2);
});
test('lexical shadowing does not confuse arbitrary user content with messages',()=>{
  const source="const value='不应提取的原文'; function render(value){t(value)}; function other(){const value='固定文案';t(value)}";
  const errors=checkSource(source,{});assert.equal(errors.length,1);assert.match(errors[0],/固定文案/);
});
test('comments and regex braces cannot corrupt dictionary parsing',()=>{
  assert.deepEqual(checkSource("const d={ /* } */ ok:'已翻译', pattern: /[{}]/}; t(d[state]);",{'已翻译':'Translated'}),[]);
});
test('cycles terminate and syntax errors fail visibly',()=>{
  assert.deepEqual(checkSource('const a=b,b=a; t(a)',{}),[]);
  assert.match(checkSource('const broken={',{})[0],/syntax error/);
});
test('missing template fragments and spread dictionaries are inspected',()=>{
  const errors=checkSource("const common={x:'动态文案'}; const combined={...common};t(combined[x]);t(`缺失模板`);",{});
  assert.equal(errors.length,2);
});
test('template substitutions preserve static conditional coverage',()=>{
  const errors=checkSource('t(`${ready ? "新动态缺译" : "完成"}`);',{'完成':'Done'});
  assert.equal(errors.length,1);assert.match(errors[0],/新动态缺译/);
});
test('loop and switch bindings cannot hide outer translated expressions',()=>{
  for(const loop of ["for(let label of ['known']){}","for(let label in {}){}","for(let label=0;label<1;label++){}","switch(state){case 1:let label='known';break;}"]){
    const errors=checkSource("const label='遗漏翻译';"+loop+'t(label);',{});
    assert.equal(errors.length,1,loop);assert.match(errors[0],/遗漏翻译/);
  }
});
