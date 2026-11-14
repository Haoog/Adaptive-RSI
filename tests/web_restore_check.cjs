const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function fixture(api) {
  const source = fs.readFileSync('adaptive_rsi/web/experience.js', 'utf8');
  const start = source.indexOf('      let checking = false, status, timer, deadlineTimer;');
  const end = source.indexOf('      box.append(checkButton);', start);
  assert.ok(start >= 0 && end > start);
  let button, status, deadline;
  const toasts = [];
  const context = {
    api, AbortController, Date, saved: {id:'backup'}, restoreCheckTimeout: 600000,
    t: text => text,
    el: () => (status = {setAttribute() {}}),
    action: (_, handler) => (button = {handler, after() {}, disabled:false}),
    toast: (...args) => toasts.push(args),
    setTimeout: (fn, ms) => { if (ms === 300) queueMicrotask(fn); else deadline = fn; return 1; },
    clearTimeout() {}, queueMicrotask,
  };
  vm.runInNewContext(source.slice(start, end), context);
  return {button, toasts, get status() { return status; }, expire: () => deadline()};
}

test('restore verification shows status while creating and blocks repeated clicks', async () => {
  let reject, calls = 0;
  const f = fixture(() => { calls++; return new Promise((_, fail) => { reject = fail; }); });
  const first = f.button.handler();
  assert.equal(f.button.disabled, true);
  assert.equal(f.status.textContent, '正在验证恢复点…');
  await f.button.handler();
  assert.equal(calls, 1);
  reject(Error('offline'));
  await assert.rejects(first, /offline/);
  assert.equal(f.status.textContent, '验证未完成，请重试。');
  assert.equal(f.button.disabled, false);
});

test('restore verification aborts a stuck poll at its deadline', async () => {
  let polled;
  const started = new Promise(resolve => { polled = resolve; });
  const f = fixture((path, body, options) => {
    if (path === 'operations') return Promise.resolve({id:'op'});
    return new Promise((resolve, reject) => {
      options.signal.addEventListener('abort', () => reject(Error('aborted')));
      polled();
    });
  });
  const first = f.button.handler();
  await started;
  f.expire();
  await first;
  assert.equal(f.status.textContent, '验证未完成，请重试。');
  assert.equal(f.button.disabled, false);
  assert.equal(f.toasts.length, 1);
});
