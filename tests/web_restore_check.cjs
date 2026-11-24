const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function fixture(api, fetch) {
  const source = fs.readFileSync('adaptive_rsi/web/experience.js', 'utf8');
  const state = source.indexOf('  const restoreChecks = new Map();');
  const start = source.lastIndexOf('      let status;', source.indexOf('      const checkButton = action(t("验证恢复点")'));
  const end = source.indexOf('        summary.append(checkButton);', start);
  assert.ok(state >= 0 && start > state && end > start);
  let button, status, deadline;
  const toasts = [];
  const context = vm.createContext({
    api, AbortController, Date, controller: new AbortController(), machine:'fixture', session:'fixture',
    visible: () => true, loadOperations() {}, saved: {id:'backup'}, restoreCheckTimeout: 600000,
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
    visible: () => true, loadOperations() {}, loadSummary() {},
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
  assert.equal(f.elements[0].textContent, '若未收到回执，请先查看近期操作。');
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
  const originalButton = f.button, originalStatus = f.status;
  await f.reopen().handler();
  assert.equal(calls, 1);
  reject(Error('offline'));
  await assert.rejects(first, /offline/);
  assert.equal(originalStatus.textContent, '若未收到回执，请先查看近期操作。');
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
      assert.deepEqual(JSON.parse(options.body), {kind:'restore-check', parameters:{id:'backup'}, reuse_active:true});
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

function storageFixture(api, fetch) {
  const source = fs.readFileSync('adaptive_rsi/web/experience.js', 'utf8');
  const start = source.indexOf('  const restoreChecks = new Map();');
  const end = source.indexOf('  function desktopDownload', start);
  const dialogs = [], downloads = [], toasts = [];
  function el(tag, textContent) {
    const listeners = {};
    return {
      tag, textContent, open:false, checked:false, disabled:false, children:[],
      classList:{add() {}}, setAttribute() {},
      append(...nodes) { this.children.push(...nodes); },
      replaceChildren() { this.children.length = 0; },
      after(node) { this.status = node; },
      remove() {},
      addEventListener(event, handler) { (listeners[event] ||= []).push(handler); },
      emit(event) { for (const handler of listeners[event] || []) handler(); },
      click() {
        if (tag === 'a') downloads.push(this);
        if (tag === 'button' && !this.disabled) return this.handler();
      },
      showModal() { this.open = true; },
      close() { this.open = false; this.emit('close'); },
    };
  }
  const context = vm.createContext({
    api, el, AbortController, Blob, state:{csrf:'current'}, window:{__RSI_MACHINE_KEY__:'first'},
    document:{createTextNode: text => text, body:{append: box => dialogs.push(box)}},
    t: text => text, stamp: at => `stamp-${at}`,
    URL:{createObjectURL: blob => blob, revokeObjectURL() {}}, toast: (...args) => toasts.push(args), desktopDownload() {},
    setTimeout(fn, ms) { if (ms === 300) queueMicrotask(fn); return 1; }, clearTimeout() {},
    action: (text, handler) => Object.assign(el('button', text), {handler}),
  });
  if (fetch) {
    const app = fs.readFileSync('adaptive_rsi/web/app.js', 'utf8');
    Object.assign(context, {fetch, structuredClone, $: () => null});
    vm.runInContext(app.slice(app.indexOf('let serviceCapabilities='), app.indexOf('const goalSubmissionLimit')), context);
  }
  vm.runInContext(source.slice(source.indexOf('  function dialog('), source.indexOf('  async function openTask(')), context);
  vm.runInContext(source.slice(start, end), context);
  const find = (node, predicate) => predicate(node) ? node : node.children?.map(child =>
    typeof child === 'object' ? find(child, predicate) : null).find(Boolean);
  return {open: () => vm.runInContext('storage()', context), context, dialogs, downloads, find, toasts};
}

test('data panel opens before usage, restores recent operations, and ignores late results after navigation', async () => {
  const requests = [];
  const f = storageFixture((path, body, {signal}) => {
    const response = deferred();
    requests.push({path, signal, ...response});
    return response.promise;
  });
  await f.open();
  const first = f.dialogs[0];
  const findText = (box, text) => f.find(box, node => node.textContent?.includes(text));
  assert.ok(findText(first, '创建一致性备份'));
  assert.ok(findText(first, '恢复命令'));
  assert.deepEqual(requests.map(row => row.path), ['storage?summary=true', 'operations', 'storage?usage=true']);
  requests[0].resolve({backups:[], expiring_count:2});
  requests[1].resolve({operations:[
    {kind:'restore-check', state:'completed', id:'lost-receipt', created:2, ended:3},
    {kind:'restore-check', state:'completed', id:'new-receipt', backup_id:'known-backup', created:2, ended:3},
  ]});
  await new Promise(setImmediate);
  assert.ok(findText(first, '备份标识未知 · lost-receipt'));
  assert.ok(findText(first, 'known-backup · new-receipt'));
  assert.ok(findText(first, '正在统计存储占用'));
  requests[2].resolve({scanned_at:10, bytes:{runs:1048576}});
  await new Promise(setImmediate);
  assert.ok(findText(first, 'stamp-10'));
  first.close();
  await f.open();
  const reopened = f.dialogs[1];
  assert.ok(findText(reopened, 'stamp-10'));
  requests[5].reject(Error('offline'));
  await new Promise(setImmediate);
  assert.ok(findText(reopened, '统计未完成'));
  assert.ok(findText(reopened, 'stamp-10'));
  reopened.close();
  assert.equal(requests[4].signal.aborted, true);
  f.context.window.__RSI_MACHINE_KEY__ = 'second';
  await f.open();
  const second = f.dialogs[2];
  requests[4].resolve({operations:[{kind:'backup', state:'completed', id:'old-machine', created:4, ended:5}]});
  await new Promise(setImmediate);
  assert.equal(findText(second, 'stamp-10'), undefined);
  assert.equal(findText(reopened, 'old-machine'), undefined);
  second.close();
});

function restorePanelFixture(request) {
  const f = storageFixture(async (path, body, options) => {
    if (path === 'storage?summary=true') return {backups:[{id:'backup', created:1}]};
    if (path === 'storage?usage=true') return {bytes:{}, scanned_at:1};
    if (path === 'operations' && !body) return {operations:[
      {kind:'restore-check', state:'running', id:'op/1', backup_id:'backup', created:2},
    ]};
    return request(path, body, options);
  });
  return {...f, async openPanel() {
    await f.open();
    await new Promise(setImmediate);
    const box = f.dialogs.at(-1);
    return {
      check: f.find(box, node => node.textContent === '验证恢复点'),
      close: () => f.find(box, node => node.textContent === '关闭').click(),
    };
  }};
}

test('closing the data panel cancels restore polling and reopening resumes the same ID', async () => {
  const staleMissing = deferred(), staleResult = deferred(), current = deferred();
  const requests = [], polls = [staleMissing, staleResult, current];
  const f = restorePanelFixture((path, body, {signal} = {}) => {
    requests.push({path, body, signal});
    if (body) return {id:'op/1'};
    return polls.shift().promise;
  });
  const original = await f.openPanel();
  const originalPoll = original.check.click();
  await new Promise(setImmediate);
  const oldSignal = requests[1].signal;
  original.close();
  assert.equal(oldSignal.aborted, true);

  const intermediate = await f.openPanel();
  const intermediatePoll = intermediate.check.click();
  await new Promise(setImmediate);
  assert.equal(requests[2].signal.aborted, false);
  intermediate.close();
  assert.equal(requests[2].signal.aborted, true);

  const reopened = await f.openPanel();
  const currentPoll = reopened.check.click();
  await new Promise(setImmediate);
  assert.deepEqual(requests.map(({path, body}) => [path, body?.kind]), [
    ['operations', 'restore-check'], ['operation?id=op%2F1', undefined],
    ['operation?id=op%2F1', undefined], ['operation?id=op%2F1', undefined],
  ]);
  assert.notEqual(requests[3].signal, oldSignal);
  assert.equal(requests[3].signal.aborted, false);

  // The old transport may settle after abort while the reopened query still owns the guard.
  staleMissing.reject(Object.assign(Error('late missing operation'), {status:404, code:'operation_not_found'}));
  await originalPoll;
  await reopened.check.handler();
  assert.equal(requests.length, 4);
  assert.equal(reopened.check.status.textContent, '正在验证恢复点…');
  assert.equal(reopened.check.disabled, true);

  current.resolve({ended:1, state:'completed'});
  await currentPoll;
  staleResult.resolve({ended:1, state:'failed'});
  await intermediatePoll;
  assert.equal(reopened.check.status.textContent, '恢复点验证通过。');
  assert.equal(reopened.check.disabled, false);
  assert.deepEqual(f.toasts, [['恢复点验证通过。', false]]);
  reopened.close();
});

test('closing before a restore receipt keeps the POST guard and isolates machines', async () => {
  const post = deferred(), requests = [];
  const f = restorePanelFixture((path, body) => {
    requests.push([path, body?.kind]);
    if (!body) return {ended:1, state:'completed'};
    return requests.length === 1 ? post.promise : {id:'other-machine'};
  });
  const original = await f.openPanel();
  const submission = original.check.click();
  original.close();
  const reopened = await f.openPanel();
  await reopened.check.click();
  assert.deepEqual(requests, [['operations', 'restore-check']]);
  const history = f.find(f.dialogs.at(-1), node => node.textContent?.includes('backup · op/1'));
  assert.ok(history);
  reopened.close();

  f.context.window.__RSI_MACHINE_KEY__ = 'second';
  const other = await f.openPanel();
  await other.check.click();
  assert.deepEqual(requests.slice(1), [['operations', 'restore-check'], ['operation?id=other-machine', undefined]]);
  post.resolve({id:'op/1'});
  await submission;
  assert.equal(requests.length, 3);
  other.close();

  f.context.window.__RSI_MACHINE_KEY__ = 'first';
  const returned = await f.openPanel();
  await returned.check.click();
  assert.deepEqual(requests.slice(3), [['operation?id=op%2F1', undefined]]);
  assert.equal(returned.check.status.textContent, '恢复点验证通过。');
  returned.close();
});

test('an old service can create, poll and cancel operations without supporting the history list', async () => {
  const calls = [];
  const f = storageFixture(undefined, async (path, options) => {
    const method = options.method || 'GET';
    calls.push([method, path]);
    const respond = data => new Response(JSON.stringify(data));
    if (path === '/api/contract') return respond({descriptor:{capabilities:['operations']}});
    if (path === '/api/storage?summary=true') return respond({backups:[]});
    if (path === '/api/storage?usage=true') return respond({bytes:{}, scanned_at:1});
    if (method === 'POST' && path === '/api/operations') return respond({id:'legacy-op'});
    if (path === '/api/operation?id=legacy-op') return respond({id:'legacy-op', state:'running'});
    if (path === '/api/operations/cancel') return respond({id:'legacy-op', state:'cancelled'});
    throw Error(path);
  });
  await f.open();
  await new Promise(setImmediate);
  const box = f.dialogs[0];
  assert.ok(f.find(box, node => node.textContent === '当前服务版本不支持近期操作列表。'));
  assert.equal(f.find(box, node => node.textContent === '刷新近期操作').hidden, true);
  assert.ok(f.find(box, node => node.textContent === '创建一致性备份'));
  const operation = await vm.runInContext('api("operations", {kind:"backup", parameters:{}})', f.context);
  assert.equal(operation.id, 'legacy-op');
  const running = await vm.runInContext('api("operation?id=legacy-op")', f.context);
  assert.equal(running.state, 'running');
  const cancelled = await vm.runInContext('api("operations/cancel", {id:"legacy-op"})', f.context);
  assert.equal(cancelled.state, 'cancelled');
  assert.ok(!calls.some(([method, path]) => method === 'GET' && path === '/api/operations'));
  box.close();
});

test('data panel downloads exactly the consented preview and clears consent when regenerated', async () => {
  const calls = [], snapshot = {snapshot:'id', body:'{\n  "format": 1\n}'};
  const f = storageFixture(async (path, body) => {
    calls.push([path, body]);
    if (path === 'storage?summary=true') return {backups:[]};
    if (path === 'storage?usage=true') return {bytes:{}, scanned_at:1};
    if (path === 'operations') return {operations:[]};
    if (path.startsWith('diagnostics/preview')) return snapshot;
    if (path === 'diagnostics/export') return {body:snapshot.body};
    throw Error(path);
  });
  await f.open();
  const box = f.dialogs[0];
  const preview = f.find(box, node => node.tag === 'details');
  const consent = f.find(preview, node => node.tag === 'input');
  const download = f.find(preview, node => node.textContent === '导出诊断包');
  preview.open = true;
  preview.emit('toggle');
  await new Promise(setImmediate);
  assert.equal(f.find(preview, node => node.tag === 'pre').textContent, snapshot.body);
  await download.handler();
  assert.equal(f.downloads.length, 0);
  consent.checked = true;
  consent.onchange();
  await download.handler();
  const exportCall = calls.at(-1);
  assert.equal(exportCall[0], 'diagnostics/export');
  assert.equal(JSON.stringify(exportCall[1]), '{"consent":true,"snapshot":"id"}');
  assert.equal(await f.downloads[0].href.text(), snapshot.body);
  await f.find(preview, node => node.textContent === '重新生成诊断预览').handler();
  assert.equal(consent.checked, false);
  assert.equal(download.disabled, true);
  box.close();
});
