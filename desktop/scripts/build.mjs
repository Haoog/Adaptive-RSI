import {spawnSync} from 'node:child_process';
import {readFileSync,writeFileSync,mkdirSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import path from 'node:path';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'../..');
const desktop=path.join(root,'desktop');
function run(command,args,cwd=desktop){const result=spawnSync(command,args,{cwd,stdio:'inherit',env:process.env});if(result.error)throw result.error;if(result.status!==0)process.exit(result.status||1);}
const version=readFileSync(path.join(root,'adaptive_rsi/__init__.py'),'utf8').match(/__version__ = "([^"]+)"/)[1];
mkdirSync(path.join(desktop,'.generated'),{recursive:true});
const signingIdentity=process.env.APPLE_SIGNING_IDENTITY;
const config={version};
const updaterPublicKey=process.env.RSI_UPDATER_PUBLIC_KEY;
if(updaterPublicKey){
 if(!process.env.TAURI_SIGNING_PRIVATE_KEY)throw new Error('Signed updater builds require TAURI_SIGNING_PRIVATE_KEY');
 config.bundle={createUpdaterArtifacts:true};
 config.plugins={updater:{pubkey:updaterPublicKey}};
}
if(signingIdentity&&signingIdentity!=='-'){
 if(process.platform!=='darwin')throw new Error('Apple signing requires macOS');
 const account=process.env.APPLE_ID&&process.env.APPLE_PASSWORD&&process.env.APPLE_TEAM_ID;
 const api=process.env.APPLE_API_KEY&&process.env.APPLE_API_ISSUER&&process.env.APPLE_API_KEY_PATH;
 if(!account&&!api)throw new Error('Developer ID builds require complete notarization credentials');
 config.bundle={...config.bundle,macOS:{signingIdentity,hardenedRuntime:true}};
}
writeFileSync(path.join(desktop,'.generated/version.json'),JSON.stringify(config));
run('python3',[path.join(desktop,'scripts/engine.py')],root);
run(path.join(desktop,'node_modules/.bin/tauri'),['icon','app-icon.svg']);
run(path.join(desktop,'node_modules/.bin/tauri'),[process.argv[2]==='dev'?'dev':'build','--config','.generated/version.json']);
