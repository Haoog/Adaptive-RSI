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

function deferred() {
  let resolve, reject;
  const promise = new Promise((done, fail) => { resolve = done; reject = fail; });
  return {promise, resolve, reject};
}

function backupFixture(api) {
  const source = fs.readFileSync('adaptive_rsi/web/experience.js', 'utf8');
  const start = source.indexOf('    const backupButton = action(');
  const end = source.indexOf('    box.append(backupButton);', start);
  assert.ok(start >= 0 && end > start);
  const buttons = [], elements = [], clearedTimers = [];
  const context = {
    api,
    box: {append(...nodes) { elements.push(...nodes); }},
    t: text => text,
    el: (tag, textContent) => ({tag, textContent, setAttribute() {}}),
    action: (label, handler) => {
      const button = {label, handler, disabled:false, remove() { this.removed = true; }};
      buttons.push(button);
      return button;
    },
    setTimeout(fn) { queueMicrotask(fn); return 1; },
    clearTimeout(timer) { clearedTimers.push(timer); },
    desktopDownload() {},
  };
  vm.runInNewContext(source.slice(start, end), context);
  return {buttons, elements, clearedTimers};
}

test('backup reports lost tracking after progress when the status request fails', async () => {
  const calls = [];
  let notifyPoll, rejectPoll;
  const pollStarted = new Promise(resolve => { notifyPoll = resolve; });
  const poll = new Promise((_, reject) => { rejectPoll = reject; });
  const f = backupFixture(async path => {
    calls.push(path);
    if (calls.length === 1) return {id:'op/1'};
    if (calls.length === 2) return {ended:null, result:{pages_total:8, pages_done:2}};
    notifyPoll();
    return poll;
  });
  const [start] = f.buttons;
  // Capture any rejection immediately so the old handler can be inspected after cleanup.
  const pending = start.handler().catch(error => error);
  await pollStarted;
  const [status, cancel] = f.elements;
  assert.equal(status.textContent, '正在创建备份 · 25%');
  assert.equal(start.disabled, true);
  assert.equal(cancel.removed, undefined);
  rejectPoll(Error('raw status API error'));
  const error = await pending;
  assert.equal(status.textContent, '无法继续获取备份状态。服务可能仍在创建备份，请稍后查看备份列表。');
  assert.equal(error, undefined);
  assert.equal(cancel.removed, true);
  assert.equal(start.disabled, false);
  assert.deepEqual(f.clearedTimers, [1]);
  assert.deepEqual(calls, ['operations', 'operation?id=op%2F1', 'operation?id=op%2F1']);
  assert.equal(f.elements.some(node => node.tag === 'a'), false);
});

test('backup submission errors still reach the shared action handler', async () => {
  const error = Error('submission rejected');
  const f = backupFixture(async () => { throw error; });
  const [start] = f.buttons;
  await assert.rejects(start.handler(), failure => failure === error);
  assert.equal(start.disabled, false);
  assert.deepEqual(f.elements, []);
});

test('backup cancellation shows sending and waiting despite progress and sends only one request', async () => {
  const polls = Array.from({length:3}, () => ({started:deferred(), response:deferred()}));
  const post = deferred();
  const cancellationIds = [];
  let pollCount = 0;
  const f = backupFixture(async (path, body) => {
    if (path === 'operations') return {id:'op'};
    if (path === 'operations/cancel') {
      cancellationIds.push(body.id);
      return post.promise;
    }
    const poll = polls[pollCount++];
    poll.started.resolve();
    return poll.response.promise;
  });
  const [start] = f.buttons;
  const pending = start.handler();
  await polls[0].started.promise;
  const [status, cancel] = f.elements;
  const cancelling = cancel.handler();
  assert.equal(status.textContent, '正在发送备份取消请求…');
  assert.equal(cancel.disabled, true);
  await cancel.handler();
  polls[0].response.resolve({ended:null, result:{pages_total:8, pages_done:2}});
  await polls[1].started.promise;
  assert.equal(status.textContent, '正在发送备份取消请求…');
  post.resolve({ended:null, state:'running'});
  await cancelling;
  assert.equal(status.textContent, '已请求取消备份，等待服务确认…');
  assert.equal(cancel.disabled, true);
  await cancel.handler();
  assert.deepEqual(cancellationIds, ['op']);
  polls[1].response.resolve({ended:null, result:{pages_total:8, pages_done:4}});
  await polls[2].started.promise;
  assert.equal(status.textContent, '已请求取消备份，等待服务确认…');
  polls[2].response.resolve({ended:'now', state:'cancelled'});
  await pending;
  assert.equal(status.textContent, '备份已取消');
  assert.equal(cancel.removed, true);
  assert.equal(start.disabled, false);
  assert.equal(f.elements.some(node => node.tag === 'a'), false);
});

test('backup cancellation failure allows retry while polling continues', async () => {
  const firstPoll = deferred();
  const nextPoll = deferred();
  const firstPollStarted = deferred();
  const nextPollStarted = deferred();
  let postCount = 0, pollCount = 0;
  const f = backupFixture(async path => {
    if (path === 'operations') return {id:'op'};
    if (path === 'operations/cancel') {
      if (++postCount === 1) throw Error('raw cancellation request error');
      return {ended:null, state:'running'};
    }
    if (++pollCount === 1) {
      firstPollStarted.resolve();
      return firstPoll.promise;
    }
    nextPollStarted.resolve();
    return nextPoll.promise;
  });
  const pending = f.buttons[0].handler();
  await firstPollStarted.promise;
  const [status, cancel] = f.elements;
  await cancel.handler();
  assert.equal(status.textContent, '发送备份取消请求失败，可重试取消。');
  assert.equal(cancel.disabled, false);
  firstPoll.resolve({ended:null, result:{pages_total:8, pages_done:2}});
  await nextPollStarted.promise;
  assert.equal(status.textContent, '发送备份取消请求失败，可重试取消。');
  await cancel.handler();
  assert.equal(postCount, 2);
  assert.equal(status.textContent, '已请求取消备份，等待服务确认…');
  nextPoll.resolve({ended:'now', state:'cancelled'});
  await pending;
  assert.equal(status.textContent, '备份已取消');
});

test('backup cancellation uses terminal POST results despite a still-pending GET', async () => {
  for (const [state, expected, result] of [
    ['completed', '备份已完成', {id:'saved'}],
    ['cancelled', '备份已取消', {}],
    ['failed', '备份未完成，请重试。', {error_code:'raw operation failure'}],
    ['interrupted', '备份未完成，请重试。', {}],
  ]) {
    const pollStarted = deferred();
    const poll = deferred();
    const f = backupFixture(async path => {
      if (path === 'operations') return {id:'op'};
      if (path === 'operations/cancel') return {ended:'now', state, result};
      pollStarted.resolve();
      return poll.promise;
    });
    const [start] = f.buttons;
    const pending = start.handler();
    await pollStarted.promise;
    const [status, cancel] = f.elements;
    await cancel.handler();
    assert.equal(status.textContent, expected);
    await pending;
    assert.equal(cancel.removed, true);
    assert.equal(start.disabled, false);
    if (state === 'failed') poll.reject(Error('late status API error'));
    else poll.resolve({ended:'earlier', state:'completed', result:{id:'stale'}});
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(status.textContent, expected);
    assert.deepEqual(f.elements.filter(node => node.tag === 'a').map(link => link.href),
      state === 'completed' ? ['/api/backup?id=saved'] : []);
  }
});

test('backup ignores a late cancellation response after terminal GET or lost tracking', async () => {
  for (const terminal of [true, false]) {
    const pollStarted = deferred();
    const poll = deferred();
    const post = deferred();
    const f = backupFixture(async path => {
      if (path === 'operations') return {id:'op'};
      if (path === 'operations/cancel') return post.promise;
      pollStarted.resolve();
      return poll.promise;
    });
    const [start] = f.buttons;
    const pending = start.handler();
    await pollStarted.promise;
    const [status, cancel] = f.elements;
    const cancelling = cancel.handler();
    if (terminal) poll.resolve({ended:'now', state:'cancelled'});
    else poll.reject(Error('raw status API error'));
    await pending;
    const expected = terminal ? '备份已取消' :
      '无法继续获取备份状态。服务可能仍在创建备份，请稍后查看备份列表。';
    assert.equal(status.textContent, expected);
    post.resolve({ended:'now', state:'completed', result:{id:'stale'}});
    await cancelling;
    assert.equal(status.textContent, expected);
    assert.equal(cancel.removed, true);
    assert.equal(start.disabled, false);
    assert.equal(f.elements.some(node => node.tag === 'a'), false);
  }
});

test('backup preserves confirmed failure feedback and successful downloads', async () => {
  for (const [state, expected, result] of [
    ['failed', '备份未完成，请重试。', {error_code:'raw operation failure'}],
    ['interrupted', '备份未完成，请重试。', {}],
    ['completed', '备份已完成', {id:'saved'}],
  ]) {
    const f = backupFixture(async path => path === 'operations' ? {id:'op'} : {ended:'now', state, result});
    const [start] = f.buttons;
    await start.handler();
    const [status, cancel, link] = f.elements;
    assert.equal(status.textContent, expected);
    assert.equal(cancel.removed, true);
    assert.equal(start.disabled, false);
    if (state === 'completed') {
      assert.equal(link.href, '/api/backup?id=saved');
      assert.equal(link.download, 'adaptive-rsi-saved.sqlite3');
    } else {
      assert.equal(link, undefined);
    }
  }
});

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
