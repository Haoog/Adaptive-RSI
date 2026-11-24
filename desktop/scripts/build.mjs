import {spawnSync} from 'node:child_process';
import {mkdirSync, readFileSync, writeFileSync} from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {buildConfig} from './build-config.mjs';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const desktop = path.join(root, 'desktop');
const tauri = path.join(desktop, 'node_modules', '@tauri-apps', 'cli');
const tauriRunner = `
const [modulePath, ...args] = process.argv.slice(1);
require(modulePath).run(args, 'tauri').catch(error => {
  console.error(error.message);
  process.exitCode = 1;
});`;
const pythonVersion = '3.12.13';
const env = process.env;
const unsignedEnv = Object.fromEntries(Object.entries(env).filter(([key]) => {
  const name = key.toUpperCase();
  return !name.startsWith('TAURI_SIGNING_') && !name.startsWith('APPLE_') &&
    name !== 'RSI_UPDATER_PUBLIC_KEY';
}));

function run(command, args, cwd, childEnv, argv0 = command) {
  const result = spawnSync(command, args, {argv0, cwd, stdio: 'inherit', env: childEnv});
  if (result.error) throw result.error;
  if (result.status !== 0) process.exit(result.status || 1);
}

function runTauri(args, childEnv) {
  run(process.execPath, ['-e', tauriRunner, '--', tauri, ...args], desktop, childEnv, 'node');
}

const {config, updaterPublicKey, developerId} = buildConfig(
  readFileSync(path.join(root, 'adaptive_rsi', '__init__.py'), 'utf8'), env, process.platform);

const generated = path.join(desktop, '.generated');
mkdirSync(generated, {recursive: true});
writeFileSync(path.join(generated, 'version.json'), JSON.stringify(config));
run('uv', ['run', '--no-project', '--managed-python', '--python', pythonVersion,
  'python', path.join(desktop, 'scripts', 'engine.py')], root, unsignedEnv);
runTauri(['icon', 'app-icon.svg'], unsignedEnv);
const mode = process.argv[2] === 'dev' ? 'dev' : 'build';
const bundleEnv = {...unsignedEnv};
// The Rust updater embeds the public key at compile time.
if (updaterPublicKey) bundleEnv.RSI_UPDATER_PUBLIC_KEY = updaterPublicKey;
if (mode === 'build') {
  for (const [key, value] of Object.entries(env)) {
    const name = key.toUpperCase();
    if ((updaterPublicKey && name.startsWith('TAURI_SIGNING_')) ||
        (developerId && name.startsWith('APPLE_')) || name === 'APPLE_SIGNING_IDENTITY') {
      bundleEnv[key] = value;
    }
  }
}
runTauri([mode, '--config', '.generated/version.json'], bundleEnv);
