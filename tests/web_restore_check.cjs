const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function fixture(api, fetch) {
  const source = fs.readFileSync('adaptive_rsi/web/experience.js', 'utf8');
  const state = source.indexOf('  const restoreChecks = new Map();');
  const start = source.lastIndexOf('      let status;', source.indexOf('      const checkButton = action(t("验证恢复点")'));
  const end = source.indexOf('      box.append(checkButton);', start);
  assert.ok(state >= 0 && start > state && end > start);
  let button, status, deadline;
  const toasts = [];
  const context = vm.createContext({
    api, AbortController, Date, saved: {id:'backup'}, restoreCheckTimeout: 600000,
    t: text => text,
    el: () => (status = {setAttribute() {}}),
    action: (_, handler) => (button = {handler, after() {}, disabled:false}),
    toast: (...args) => toasts.push(args),
    setTimeout: (fn, ms) => { if (ms === 300) queueMicrotask(fn); else deadline = fn; return 1; },
    clearTimeout() {}, queueMicrotask,
  });
  if (fetch) {
    const app = fs.readFileSync('adaptive_rsi/web/app.js', 'utf8');
    const apiStart = app.indexOf('async function sendApi(');
    const apiEnd = app.indexOf('const goalSubmissionLimit', apiStart);
    assert.ok(apiStart >= 0 && apiEnd > apiStart);
    Object.assign(context, {
      fetch, structuredClone, state: {csrf:'fixture'}, featureForPath: () => null,
      apiErrors: {}, configReasonLabels: {}, runtimeConnectionError: () => 'offline',
    });
    vm.runInContext(app.slice(apiStart, apiEnd), context);
  }
  vm.runInContext(source.slice(state, source.indexOf('\n', state)), context);
  function reopen() {
    status = undefined;
    vm.runInContext(`{${source.slice(start, end)}}`, context);
    return button;
  }
  reopen();
  return {get button() { return button; }, toasts, get status() { return status; }, expire: () => deadline(), reopen};
}

test('restore verification shows status while creating and blocks repeated clicks', async () => {
  let reject, calls = 0;
  const f = fixture(() => { calls++; return new Promise((_, fail) => { reject = fail; }); });
  const first = f.button.handler();
  assert.equal(f.button.disabled, true);
  assert.equal(f.status.textContent, '正在验证恢复点…');
  await f.button.handler();
  const originalButton = f.button, originalStatus = f.status;
  await f.reopen().handler();
  assert.equal(calls, 1);
  reject(Error('offline'));
  await assert.rejects(first, /offline/);
  assert.equal(originalStatus.textContent, '验证未完成，请重试。');
  assert.equal(originalButton.disabled, false);
});

test('restore verification aborts a stuck poll and resumes with a fresh signal', async () => {
  let polled, firstSignal;
  const calls = [];
  const started = new Promise(resolve => { polled = resolve; });
  const f = fixture((path, body, options) => {
    calls.push([path, body?.kind]);
    if (body) return {id:'op'};
    if (firstSignal) {
      assert.notEqual(options.signal, firstSignal);
      assert.equal(options.signal.aborted, false);
      return {ended:1, state:'failed'};
    }
    firstSignal = options.signal;
    return new Promise((resolve, reject) => {
      firstSignal.addEventListener('abort', () => reject(Error('aborted')));
      polled();
    });
  });
  const first = f.button.handler();
  await started;
  f.expire();
  await first;
  assert.equal(firstSignal.aborted, true);
  assert.equal(f.status.textContent, '无法获取验证状态。再次点击可继续查看原验证。');
  assert.equal(f.button.disabled, false);
  assert.equal(f.toasts.length, 1);
  await f.button.handler();
  assert.equal(f.status.textContent, '恢复点验证未通过。');
  await f.button.handler();
  assert.deepEqual(calls, [
    ['operations', 'restore-check'], ['operation?id=op', undefined], ['operation?id=op', undefined],
    ['operations', 'restore-check'], ['operation?id=op', undefined],
  ]);
});

test('restore verification retries the same operation after a lost status response', async () => {
  const calls = [];
  const f = fixture(async (path, body) => {
    calls.push([path, body?.kind]);
    if (body) return {id:'op/1'};
    if (calls.length === 2) throw new TypeError('offline');
    return {ended:1, state:'completed'};
  });
  await assert.rejects(f.button.handler(), /offline/);
  const interruptedStatus = f.status.textContent;
  assert.equal(f.button.disabled, false);
  await f.reopen().handler();
  assert.deepEqual(calls, [
    ['operations', 'restore-check'],
    ['operation?id=op%2F1', undefined],
    ['operation?id=op%2F1', undefined],
  ]);
  assert.equal(interruptedStatus, '无法获取验证状态。再次点击可继续查看原验证。');
  assert.equal(f.status.textContent, '恢复点验证通过。');
  await f.button.handler();
  assert.deepEqual(calls.slice(3), [['operations', 'restore-check'], ['operation?id=op%2F1', undefined]]);
});

test('only the explicit missing-operation response allows a fresh restore-check POST', async () => {
  const calls = [];
  const responses = [
    [200, {id:'old/op'}],
    [404, {error:'Not found'}],
    [400, {error:'Invalid request', error_code:'invalid_request'}],
    [404, {error:'Operation not found', error_code:'operation_not_found'}],
    [200, {id:'new-op'}],
    [200, {ended:1, state:'completed'}],
  ];
  const f = fixture(undefined, async (path, options) => {
    calls.push([options.method ?? 'GET', path]);
    if (options.method === 'POST') {
      assert.deepEqual(JSON.parse(options.body), {kind:'restore-check', parameters:{id:'backup'}});
    }
    const [status, payload] = responses.shift();
    return {ok:status === 200, status, json:async () => payload};
  });
  for (const status of [404, 400]) {
    await assert.rejects(f.button.handler(), {status});
    assert.equal(f.status.textContent, '无法获取验证状态。再次点击可继续查看原验证。');
  }
  await f.button.handler();
  assert.equal(f.status.textContent, '原验证已不存在。再次点击可开始新的验证。');
  await f.button.handler();
  assert.equal(f.status.textContent, '恢复点验证通过。');
  assert.deepEqual(calls, [
    ['POST', '/api/operations'], ['GET', '/api/operation?id=old%2Fop'],
    ['GET', '/api/operation?id=old%2Fop'], ['GET', '/api/operation?id=old%2Fop'],
    ['POST', '/api/operations'], ['GET', '/api/operation?id=new-op'],
  ]);
});
