'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const test=require('node:test');

// translateStatic() only translates a static node when its whole trimmed text
// (or attribute value) is a key in the english dictionary, so anything else is
// left in Chinese for English users. These pages rely on that dictionary.
const WEB=path.join(__dirname,'../adaptive_rsi/web');
const PAGES=['index.html','install.html'];
const CJK=/[\u4e00-\u9fff]/;
// The language names stay in their own language; the label is intentionally bilingual.
// login.html is intentionally excluded: it is a standalone page with an English <details> block.
const INTENTIONALLY_UNTRANSLATED=new Set(['中文','Language / 语言']);

function englishKeys(){
  const source=fs.readFileSync(path.join(WEB,'i18n.js'),'utf8');
  const marker=/const\s+english\s*=\s*\{/.exec(source);
  assert.ok(marker,'i18n.js: english dictionary not found');
  const start=marker.index+marker[0].length-1;
  const end=source.indexOf('\n};',start);
  assert.ok(end>start,'i18n.js: english dictionary is not closed');
  return new Set(Object.keys(JSON.parse(source.slice(start,end+2))));
}

function decode(value){
  const named={amp:'&',lt:'<',gt:'>',quot:'"',apos:"'",nbsp:'\u00a0'};
  return value.replace(/&(#[xX][0-9a-fA-F]+|#\d+|[a-zA-Z]+);/g,(match,body)=>{
    if(body[0]==='#'){
      const code=body[1]==='x'||body[1]==='X'?parseInt(body.slice(2),16):parseInt(body.slice(1),10);
      return Number.isInteger(code)&&code>=0&&code<=0x10ffff?String.fromCodePoint(code):match;
    }
    return Object.hasOwn(named,body.toLowerCase())?named[body.toLowerCase()]:match;
  });
}

// Mirrors what the DOM exposes to translateStatic(): text nodes (including
// <title>) trimmed, plus the three translated attributes.
function staticStrings(html){
  const stripped=html.replace(/<!--[\s\S]*?-->/g,'').replace(/<(script|style)\b[\s\S]*?<\/\1\s*>/gi,'');
  const values=[];
  for(const match of stripped.matchAll(/>([^<>]+)</g)){
    const text=decode(match[1]).trim();
    if(text)values.push(text);
  }
  for(const match of stripped.matchAll(/\s(?:aria-label|placeholder|title)\s*=\s*(?:"([^"]*)"|'([^']*)')/gi)){
    const value=decode(match[1]??match[2]).trim();
    if(value)values.push(value);
  }
  return values;
}

function missingStaticTranslations(html,keys){
  return [...new Set(staticStrings(html).filter(value=>CJK.test(value)&&!keys.has(value)&&!INTENTIONALLY_UNTRANSLATED.has(value)))];
}

for(const page of PAGES){
  test(`static Chinese text in ${page} has English translations`,()=>{
    const html=fs.readFileSync(path.join(WEB,page),'utf8');
    const values=staticStrings(html);
    assert.ok(values.some(value=>CJK.test(value)),`${page}: no Chinese static text found; the scanner may be broken`);
    const missing=missingStaticTranslations(html,englishKeys());
    assert.equal(missing.length,0,`${page} 缺少 i18n 键：\n${missing.map(value=>'  - '+value).join('\n')}`);
  });
}

test('scanner reports untranslated text nodes and translated attributes',()=>{
  const html='<p>已有翻译</p><p>新的文本漏译</p><button aria-label="也漏译">中文</button><input placeholder="占位漏译" title=\'标题漏译\'><script>const sample="脚本内的中文";</script>';
  assert.deepEqual(missingStaticTranslations(html,new Set(['已有翻译'])),['新的文本漏译','也漏译','占位漏译','标题漏译']);
});
