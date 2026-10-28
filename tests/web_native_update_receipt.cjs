'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function load() {
  const source = fs.readFileSync('adaptive_rsi/web/native-update.js', 'utf8');
  const helper = source.match(/ function exportReceipt\(\)\{[\s\S]*?\n \}/);
  assert.ok(helper, 'exportReceipt helper must exist');
  const downloads = [];
  const blobs = [];
  const context = {
    snapshot: { current_version: '0.5.126', version: '0.5.127', state: 'available', error_code: '' },
    policy: 'download', versionScope: 'patch', installWindow: 'any',
    window: { navigator: { platform: 'MacIntel' } },
    navigator: { platform: 'MacIntel' },
    document: { documentElement: { lang: 'zh-CN' }, createElement: () => ({ click() { downloads.push(this.download); } }) },
    Blob: class { constructor(parts, options) { blobs.push({ parts, type: options && options.type }); } },
    URL: { createObjectURL: () => 'blob:receipt', revokeObjectURL() {} },
    Date, JSON,
  };
  vm.runInNewContext(helper[0] + '\nresult = exportReceipt();', context);
  return { receipt: JSON.parse(JSON.stringify(context.result)), downloads, blobs };
}

test('update receipt is exportable and contains only non-secret diagnostics', () => {
  const { receipt, downloads, blobs } = load();
  assert.equal(receipt.receipt_version, 1);
  assert.equal(receipt.current_version, '0.5.126');
  assert.equal(receipt.target_version, '0.5.127');
  assert.equal(receipt.state, 'available');
  assert.equal(receipt.policy, 'download');
  assert.equal(receipt.version_scope, 'patch');
  assert.equal(receipt.install_window, 'any');
  assert.equal(receipt.platform, 'MacIntel');
  assert.equal(receipt.locale, 'zh-CN');
  assert.ok(receipt.generated_at);
  assert.deepEqual(downloads, ['adaptive-rsi-update-receipt.json']);
  assert.equal(blobs[0].type, 'application/json');
  const text = blobs[0].parts[0];
  assert.ok(!/token|cookie|password|secret/i.test(text), text);
});
