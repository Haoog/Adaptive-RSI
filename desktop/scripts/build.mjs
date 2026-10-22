import {spawnSync} from 'node:child_process';
import {readFileSync,writeFileSync,mkdirSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import path from 'node:path';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'../..');
const desktop=path.join(root,'desktop');
function run(command,args,cwd=desktop){const result=spawnSync(command,args,{cwd,stdio:'inherit',env:process.env});if(result.error)throw result.error;if(result.status!==0)process.exit(result.status||1);}
const version=readFileSync(path.join(root,'adaptive_rsi/__init__.py'),'utf8').match(/__version__ = "([^"]+)"/)[1];
mkdirSync(path.join(desktop,'.generated'),{recursive:true});
writeFileSync(path.join(desktop,'.generated/version.json'),JSON.stringify({version}));
run('python3',[path.join(desktop,'scripts/engine.py')],root);
run(path.join(desktop,'node_modules/.bin/tauri'),['icon','app-icon.svg']);
run(path.join(desktop,'node_modules/.bin/tauri'),[process.argv[2]==='dev'?'dev':'build','--config','.generated/version.json']);
