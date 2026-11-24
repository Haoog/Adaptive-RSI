export function buildConfig(versionSource, env, platform) {
  const version = versionSource.match(/__version__ = "([^"]+)"/)[1];
  const config = {version};
  const updaterPublicKey = env.RSI_UPDATER_PUBLIC_KEY;
  if (updaterPublicKey) {
    if (!env.TAURI_SIGNING_PRIVATE_KEY) throw new Error('Signed updater builds require TAURI_SIGNING_PRIVATE_KEY');
    config.bundle = {createUpdaterArtifacts: true};
    config.plugins = {updater: {pubkey: updaterPublicKey}};
  }

  const signingIdentity = env.APPLE_SIGNING_IDENTITY;
  const developerId = signingIdentity && signingIdentity !== '-';
  if (developerId) {
    if (platform !== 'darwin') throw new Error('Apple signing requires macOS');
    const account = env.APPLE_ID && env.APPLE_PASSWORD && env.APPLE_TEAM_ID;
    const api = env.APPLE_API_KEY && env.APPLE_API_ISSUER && env.APPLE_API_KEY_PATH;
    if (!account && !api) throw new Error('Developer ID builds require complete notarization credentials');
    config.bundle = {...config.bundle, macOS: {signingIdentity, hardenedRuntime: true}};
  }
  return {config, updaterPublicKey, developerId};
}
