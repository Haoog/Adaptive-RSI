'use strict';
const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const vm = require('node:vm');
const {EventEmitter} = require('node:events');

function run(args, files) {
  const started = [], output = [];
  const context = {
    __dirname: require('node:path').resolve('scripts'),
    require(name) {
      if (name === 'node:fs') return {readdirSync: () => files, mkdirSync() {}, writeFileSync() {}};
      if (name === 'node:child_process') return {spawn(executable, argv, options) {
        started.push({argv, options});
        const child = new EventEmitter();
        child.pid = 123;
        return child;
      }};
      return require(name);
    },
    process: {argv: ['node', 'test_browser.cjs', ...args], env: {}, execPath: process.execPath, on() {}},
    console: {log: value => output.push(value), error() {}},
    setTimeout: () => ({unref() {}}), clearTimeout() {},
  };
  vm.runInNewContext(fs.readFileSync('scripts/test_browser.cjs', 'utf8'), context);
  return {started, output};
}

test('listing one browser scenario does not start unrelated browsers', () => {
  const result = run(['--list', 'browser_task_detail.cjs'], ['browser_smoke.cjs', 'browser_task_detail.cjs']);
  assert.deepEqual(result.started, []);
  assert.deepEqual(result.output, ['browser_task_detail.cjs']);
});

test('browser selectors select individual hardening groups and reject mistakes', () => {
  const files = ['browser_hardening.cjs', 'browser_smoke.cjs'];
  const selected = run(['browser_hardening.cjs:routes'], files);
  assert.equal(selected.started.length, 1);
  assert.equal(selected.started[0].options.env.RSI_BROWSER_HARDENING_GROUP, 'routes');
  assert.equal(run(['--list', 'browser_hardening.cjs'], files).output.length, 6);
  assert.throws(() => run(['missing.cjs'], files), /Unknown browser selector/);
  assert.throws(() => run(['browser_smoke.cjs', 'browser_smoke.cjs'], files), /must be unique/);
  assert.throws(() => run([], []), /No browser scenarios/);
});
