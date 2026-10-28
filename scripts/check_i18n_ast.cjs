'use strict';
const fs = require('node:fs');
const path = require('node:path');
const acorn = require('acorn');

function children(node) {
  return Object.values(node).flatMap(value => Array.isArray(value) ? value.filter(item=>item?.type) : value?.type ? [value] : []);
}

function checkSource(source, translations, filename='source.js') {
  let tree;
  try { tree=acorn.parse(source,{ecmaVersion:'latest',sourceType:'script',locations:true}); }
  catch(error) { return [`${filename}:${error.loc?.line||1}: JavaScript syntax error: ${error.message}`]; }
  const scopes=new WeakMap(),calls=[],errors=new Set();
  const bindPattern=(pattern,scope,init=null)=>{
    if(!pattern)return;
    if(pattern.type==='Identifier')scope.bindings.set(pattern.name,init);
    else if(pattern.type==='AssignmentPattern')bindPattern(pattern.left,scope);
    else if(pattern.type==='RestElement')bindPattern(pattern.argument,scope);
    else if(pattern.type==='ObjectPattern')for(const property of pattern.properties)bindPattern(property.value||property.argument,scope);
    else if(pattern.type==='ArrayPattern')for(const element of pattern.elements)bindPattern(element,scope);
  };
  function index(node,scope) {
    if(node.type==='FunctionDeclaration'&&node.id)bindPattern(node.id,scope);
    const functionScope=/^(?:FunctionDeclaration|FunctionExpression|ArrowFunctionExpression)$/.test(node.type);
    if(functionScope||['BlockStatement','Program','CatchClause','ForStatement','ForInStatement','ForOfStatement','SwitchStatement'].includes(node.type)) {
      scope={parent:scope,bindings:new Map(),functionScope:functionScope||node.type==='Program'};
      if(functionScope){for(const parameter of node.params)bindPattern(parameter,scope);if(node.id)bindPattern(node.id,scope);}
      if(node.type==='CatchClause')bindPattern(node.param,scope);
    }
    scopes.set(node,scope);
    if(node.type==='VariableDeclaration')for(const declaration of node.declarations){
      let target=scope;if(node.kind==='var')while(target&&!target.functionScope)target=target.parent;
      bindPattern(declaration.id,target,declaration.init);
    }
    if(node.type==='CallExpression'&&node.callee.type==='Identifier'&&node.callee.name==='t'&&node.arguments[0])calls.push(node.arguments[0]);
    for(const child of children(node))index(child,scope);
  }
  index(tree,null);
  const message=(text,node)=>{
    if(/[\u3400-\u9fff]/u.test(text)&&!Object.hasOwn(translations,text))errors.add(`${filename}:${node.loc.start.line}: ${text}`);
  };
  function inspect(node,seen=new Set()) {
    if(!node||seen.has(node))return;
    seen.add(node);
    switch(node.type){
      case 'Literal':if(typeof node.value==='string')message(node.value,node);break;
      case 'TemplateLiteral':
        for(const part of node.quasis)message(part.value.cooked??part.value.raw,part);
        for(const expression of node.expressions)inspect(expression,seen);
        break;
      case 'Identifier':{
        let scope=scopes.get(node);
        while(scope&&!scope.bindings.has(node.name))scope=scope.parent;
        if(scope)inspect(scope.bindings.get(node.name),seen);
        break;
      }
      case 'MemberExpression':inspect(node.object,seen);break;
      case 'ChainExpression':inspect(node.expression,seen);break;
      case 'ConditionalExpression':inspect(node.consequent,seen);inspect(node.alternate,seen);break;
      case 'LogicalExpression':case 'BinaryExpression':inspect(node.left,seen);inspect(node.right,seen);break;
      case 'ObjectExpression':for(const property of node.properties)inspect(property.type==='SpreadElement'?property.argument:property.value,seen);break;
      case 'ArrayExpression':for(const element of node.elements)inspect(element,seen);break;
      case 'SequenceExpression':inspect(node.expressions.at(-1),seen);break;
    }
  }
  for(const call of calls)inspect(call);
  return [...errors];
}

function check(root=path.resolve(__dirname,'..')) {
  const directory=path.join(root,'adaptive_rsi/web'),source=fs.readFileSync(path.join(directory,'i18n.js'),'utf8');
  const translations=JSON.parse(source.slice(source.indexOf('{'),source.indexOf('\n};')+2));
  return fs.readdirSync(directory).filter(name=>name.endsWith('.js')&&name!=='i18n.js').flatMap(name=>
    checkSource(fs.readFileSync(path.join(directory,name),'utf8'),translations,name));
}

if(require.main===module){
  const errors=check();
  if(errors.length){console.error(errors.join('\n'));process.exitCode=1;}
  else console.log('AST localization literals, aliases, dictionaries and syntax passed');
}
module.exports={checkSource,check};
