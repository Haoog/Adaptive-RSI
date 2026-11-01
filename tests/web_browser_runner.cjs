'use strict';
const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const vm = require('node:vm');
const {EventEmitter} = require('node:events');

function run(args, files) {
  const signals = [];
  const started = [], output = [], errors = [], timers = [], kills = [];
  const context = {
    __dirname: require('node:path').resolve('scripts'),
    require(name) {
      if (name === 'node:fs') return {readdirSync: () => files, mkdirSync() {}, writeFileSync() {}};
      if (name === 'node:child_process') return {spawn(executable, argv, spawnOptions) {
        const child = new EventEmitter();
        child.pid = 4000 + started.length;
        child.groupAlive = true;
        child.close = code => child.emit('close', code);
        started.push({argv, options: spawnOptions, child});
        return child;
      }};
      return require(name);
    },
    process: {argv: ['node', 'test_browser.cjs', ...args], env: {}, execPath: process.execPath,
      on(signal, handler) {signals.push({signal, handler});},
      kill(pid, signal) {
        const child = started.find(item => item.child && -item.child.pid === pid);
        if (!child) throw Object.assign(new Error('kill ESRCH'), {code: 'ESRCH'});
        if (signal === 0) {
          if (!child.child.groupAlive) throw Object.assign(new Error('kill ESRCH'), {code: 'ESRCH'});
          return;
        }
        kills.push({pid, signal});
      }},
    console: {log: value => output.push(value), error: value => errors.push(value)},
    setTimeout(handler, delay) {
      const timer = {delay, handler, unrefd: false, cleared: false, fired: false,
        unref() {timer.unrefd = true;},
        fire() {if (!timer.cleared && !timer.fired) {timer.fired = true;handler();}}};
      timers.push(timer);
      return timer;
    },
    clearTimeout(timer) {if (timer) timer.cleared = true;},
    setInterval() {
      const interval = {interval: true, unrefd: false, cleared: false, unref() {interval.unrefd = true;}};
      timers.push(interval);
      return interval;
    },
    clearInterval(interval) {if (interval) interval.cleared = true;},
  };
  vm.runInNewContext(fs.readFileSync('scripts/test_browser.cjs', 'utf8'), context);
  return {started, output, errors, signals, timers, kills, context,
    isCleanupPending: () => context.isCleanupPending(),
    exitCode: () => context.process.exitCode};
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

test('per-scenario timeout escalates to SIGKILL and finalizes only after close', () => {
  const files = ['browser_smoke.cjs', 'browser_task_detail.cjs'];
  const result = run(['browser_smoke.cjs', 'browser_task_detail.cjs'], files);
  result.started[0].child.groupAlive = false;
  result.started[0].child.close(0);
  const limit = result.timers.find(timer => timer.delay === 180000 && !timer.cleared);
  assert.ok(limit, 'a per-scenario timeout must be scheduled');
  limit.fire();
  assert.deepEqual(result.errors, ['[browser] timeout: browser_task_detail.cjs']);
  assert.deepEqual(result.kills, [{pid: -result.started[1].child.pid, signal: 'SIGTERM'}],
    'only the timed-out child gets the group SIGTERM');
  const escalate = result.timers.find(timer => timer.delay === 1500 && !timer.cleared && !timer.fired);
  assert.ok(escalate, 'a SIGKILL escalation must be scheduled');
  assert.equal(escalate.unrefd, true, 'escalation timer should be unrefd');
  assert.equal(result.isCleanupPending(), true, 'cleanup-pending while the descendant may still be alive');
  assert.equal(result.exitCode(), undefined, 'runner must not finalize while cleanup is pending');
  escalate.fire();
  assert.deepEqual(result.kills[1], {pid: -result.started[1].child.pid, signal: 'SIGKILL'},
    'cleanup path escalates to a real group SIGKILL');
  assert.equal(result.isCleanupPending(), true, 'still pending until the child closes');
  result.started[1].child.close(null);
  assert.equal(result.isCleanupPending(), false);
  assert.equal(result.exitCode(), 1, 'timed-out scenario must fail the run');
});

test('spawn error fails the run; a live group defers finalization to the escalation', () => {
  const files = ['browser_smoke.cjs'];
  const dead = run(['browser_smoke.cjs'], files);
  dead.started[0].child.groupAlive = false;
  dead.started[0].child.emit('error', new Error('spawn boom'));
  assert.deepEqual(dead.errors, ['[browser] browser_smoke.cjs: spawn boom']);
  assert.equal(dead.exitCode(), undefined, 'nothing finalizes before the child closes');
  assert.equal(dead.isCleanupPending(), false);
  dead.started[0].child.close(null);
  assert.equal(dead.exitCode(), 1, 'spawn errors must fail the run');
  assert.equal(dead.isCleanupPending(), false);

  const alive = run(['browser_smoke.cjs'], files);
  const child = alive.started[0].child;
  child.emit('error', new Error('spawn boom'));
  child.close(null);
  assert.equal(alive.isCleanupPending(), true, 'a live group keeps cleanup pending after the error');
  assert.equal(alive.exitCode(), undefined);
  alive.timers.find(timer => timer.delay === 1500).fire();
  assert.equal(alive.isCleanupPending(), false);
  assert.equal(alive.exitCode(), 1);
});

test('successful scenario never reports cleanup-pending and exits zero', () => {
  const files = ['browser_smoke.cjs'];
  const result = run(['browser_smoke.cjs'], files);
  result.started[0].child.groupAlive = false;
  result.started[0].child.close(0);
  assert.equal(result.isCleanupPending(), false);
  assert.equal(result.timers.some(timer => timer.delay === 1500), false, 'no cleanup for a fully reaped group');
  assert.equal(result.timers.some(timer => timer.interval), false, 'no keep-alive needed on the happy path');
  assert.equal(result.exitCode(), 0);
});

// 信号路径：中断后停止派发、对所有活动子进程补发组信号，结算仍要等升级落地。
test('SIGINT stops dispatching and finalizes only after the group escalations settle', () => {
  const files = ['browser_smoke.cjs', 'browser_task_detail.cjs'];
  const result = run(['browser_smoke.cjs', 'browser_task_detail.cjs'], files);
  const first = result.started[0].child, second = result.started[1].child;
  result.signals.find(item => item.signal === 'SIGINT').handler();
  assert.equal(result.started.length, 2, 'no new scenario starts after the signal');
  assert.deepEqual(result.kills, [
    {pid: -first.pid, signal: 'SIGTERM'}, {pid: -second.pid, signal: 'SIGTERM'},
  ], 'every active child gets a group SIGTERM');
  assert.equal(result.isCleanupPending(), true);
  assert.equal(result.exitCode(), undefined, 'finalization waits for the escalations');
  const escalations = result.timers.filter(timer => timer.delay === 1500);
  assert.equal(escalations.length, 2, 'one SIGKILL escalation per active child');
  for (const escalate of escalations) {
    assert.equal(escalate.unrefd, true);
    escalate.fire();
  }
  first.groupAlive = false;first.close(null);
  second.groupAlive = false;second.close(null);
  assert.equal(result.isCleanupPending(), false);
  assert.equal(result.exitCode(), 1, 'an interrupted run must fail');
});

// close 之后场景超时定时器已 clear，剩下的唯一定时器就是 unref 的升级定时器。
// 旧实现里主流程此时已写 exitCode、事件循环一空 runner 就退出，unref 定时器没机会触发；
// 修复后 runner 推迟结算，升级 SIGKILL 真实发出后才落 exitCode。
test('stub descendant ignoring SIGTERM is killed by the escalated SIGKILL before the run finalizes', () => {
  const files = ['browser_smoke.cjs'];
  const result = run(['browser_smoke.cjs'], files);
  const child = result.started[0].child;
  child.close(0);
  assert.equal(result.exitCode(), undefined, 'no finalize while the stubborn descendant may still be alive');
  assert.deepEqual(result.kills, [{pid: -child.pid, signal: 'SIGTERM'}], 'close probes the group and re-signals it');
  const escalation = result.timers.find(timer => timer.delay === 1500 && !timer.cleared && !timer.fired);
  assert.ok(escalation, 'the SIGKILL escalation must still be armed after close');
  assert.equal(escalation.unrefd, true, 'escalation timer should be unrefd');
  const keepAlive = result.timers.find(timer => timer.interval);
  assert.ok(keepAlive, 'a refd keep-alive must hold the loop open during cleanup');
  assert.equal(keepAlive.unrefd, false, 'keep-alive must be refd or the runner exits early');
  escalation.fire();
  assert.deepEqual(result.kills, [{pid: -child.pid, signal: 'SIGTERM'}, {pid: -child.pid, signal: 'SIGKILL'}],
    'cleanup path must escalate to a real group SIGKILL');
  assert.equal(result.isCleanupPending(), false);
  assert.equal(result.exitCode(), 0, 'finalization happens only after the kill landed');
});
