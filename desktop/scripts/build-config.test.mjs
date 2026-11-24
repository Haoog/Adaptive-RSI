import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import {buildConfig} from './build-config.mjs';

const source = '# fixture\n__version__ = "1.2.3"\n';
const generate = (env = {}, platform = 'darwin') => buildConfig(source, env, platform).config;
const publicKey = 'fixture-public-key';
const signing = {RSI_UPDATER_PUBLIC_KEY: publicKey, TAURI_SIGNING_PRIVATE_KEY: 'fixture-private-key'};
const identity = 'Developer ID Application: Fixture';
const developer = {...signing, APPLE_SIGNING_IDENTITY: identity};
const credentials = [
  {APPLE_ID: 'fixture-id', APPLE_PASSWORD: 'fixture-pass', APPLE_TEAM_ID: 'fixture-team'},
  {APPLE_API_KEY: 'fixture-key', APPLE_API_ISSUER: 'fixture-issuer', APPLE_API_KEY_PATH: 'fixture-path'},
];

test('unsigned builds override only the version', () => {
  assert.deepEqual(generate(), {version: '1.2.3'});
});

test('a public key requires a private key', () => {
  assert.throws(() => generate({RSI_UPDATER_PUBLIC_KEY: publicKey}), /TAURI_SIGNING_PRIVATE_KEY/);
});

test('signed builds generate update artifacts and embed the public key', () => {
  assert.deepEqual(generate(signing), {
    version: '1.2.3', bundle: {createUpdaterArtifacts: true}, plugins: {updater: {pubkey: publicKey}},
  });
});

test('Developer ID requires macOS and complete notarization credentials', () => {
  assert.throws(() => generate(developer), /complete notarization credentials/);
  assert.throws(() => generate({...developer, APPLE_ID: 'incomplete'}), /complete notarization credentials/);
  assert.throws(() => generate({...developer, ...credentials[0]}, 'linux'), /requires macOS/);
});

// Tauri applies the --config overlay using JSON Merge Patch (RFC 7396).
function mergePatch(base, patch) {
  const isObject = value => value !== null && typeof value === 'object' && !Array.isArray(value);
  if (!isObject(patch)) return patch;
  const result = isObject(base) ? {...base} : {};
  for (const [key, value] of Object.entries(patch)) {
    if (value === null) delete result[key];
    else result[key] = mergePatch(result[key], value);
  }
  return result;
}

for (const credential of credentials) {
  test(`Developer ID accepts ${Object.keys(credential)[0]} credentials and preserves base config`, () => {
    const base = JSON.parse(readFileSync(new URL('../src-tauri/tauri.conf.json', import.meta.url), 'utf8'));
    const patch = JSON.parse(JSON.stringify(generate({...developer, ...credential})));
    const effective = mergePatch(base, patch);
    assert.equal(effective.version, '1.2.3');
    assert.deepEqual(effective.bundle, {
      ...base.bundle, createUpdaterArtifacts: true,
      macOS: {...base.bundle.macOS, signingIdentity: identity, hardenedRuntime: true},
    });
    assert.deepEqual(effective.plugins, {
      ...base.plugins, updater: {...base.plugins.updater, pubkey: publicKey},
    });
    assert.equal(effective.plugins.updater.requireSignedVersion, true);
  });
}
