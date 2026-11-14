'use strict';
// 「发布与上线」回执重试回归：派发前的永久失败会以 state=failed + dispatch_receipt=preflight_error
// 终态化，桌面界面必须像 rejected 一样给出重试入口，否则用户只能卡在失败状态。
const test = require('node:test'), assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm'), path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../adaptive_rsi/web/app.js'), 'utf8');
const start = source.indexOf('async function renderReleaseOutcome(');
const end = source.indexOf("$('release-workflows-refresh')", start);
if (start < 0 || end < 0) throw new Error('renderReleaseOutcome is missing from app.js');
const renderSource = source.slice(start, end);

function node(tag, text, cls) {
  return {tag, textContent: text === undefined ? '' : text, className: cls || '', children: [], dataset: {}, isConnected: true,
    append(...items) { this.children.push(...items); },
    addEventListener(type, handler) { (this.handlers = this.handlers || []).push([type, handler]); },
    setAttribute() {}, focus() {}, scrollIntoView() {},
    click() {
      return Promise.all((this.handlers || []).filter(([type]) => type === 'click').map(([, handler]) => handler()));
    }};
}

function harness(deploy) {
  const calls = [];
  const context = {
    api: async (target, body) => {
      // vm 上下文里的对象原型不同，断言前转成本域普通对象。
      calls.push([target, body === undefined ? undefined : JSON.parse(JSON.stringify(body))]);
      return String(target).startsWith('release-status') ? {deploy} : {};
    },
    t: text => text,
    el: node,
    action: (text, callback, cls) => {
      const button = node('button', text, cls);
      button.addEventListener('click', () => callback());
      return button;
    },
    releaseStates: {failed: '失败'},
    apiErrors: {},
    projectId: 'p',
    detailTask: null,
    refresh: async () => { calls.push(['refresh']); },
    showTask: async () => {},
    releaseCandidatesCache: new Map(),
    renderReleaseReconciliation: () => {}
  };
  vm.runInNewContext(renderSource + '\n;globalThis.renderReleaseOutcome=renderReleaseOutcome;', context);
  return {context, calls};
}

function findButton(root, label) {
  if (root.tag === 'button' && root.textContent === label) return root;
  for (const child of root.children || []) {
    const found = findButton(child, label);
    if (found) return found;
  }
  return null;
}

function texts(root) {
  return [root.textContent, ...(root.children || []).flatMap(texts)].filter(Boolean);
}

test('preflight_error receipt renders the retry entry and calls release-retry', async () => {
  const {context, calls} = harness({state: 'failed', dispatch_receipt: 'preflight_error', error: 'Resource not accessible by integration'});
  const root = node('div');
  await context.renderReleaseOutcome(root, {id: 'run', state: 'merged'}, 'p');
  assert.ok(texts(root).includes('提交未被接受。修复授权或工作流配置后，可以安全重试。'),
    'preflight_error reuses the rejected explanation');
  const button = findButton(root, '修复权限后重试');
  assert.ok(button, 'a failed preflight_error receipt must stay retryable');
  await button.click();
  assert.deepEqual(calls.filter(([target]) => target === 'release-retry'),
    [['release-retry', {project: 'p', run: 'run', kind: 'deploy'}]]);
  assert.ok(calls.some(([target]) => target === 'refresh'));
});

test('rejected receipt keeps its retry entry', async () => {
  const {context, calls} = harness({state: 'failed', dispatch_receipt: 'rejected'});
  const root = node('div');
  await context.renderReleaseOutcome(root, {id: 'run', state: 'merged'}, 'p');
  const button = findButton(root, '修复权限后重试');
  assert.ok(button);
  await button.click();
  assert.deepEqual(calls.filter(([target]) => target === 'release-retry'),
    [['release-retry', {project: 'p', run: 'run', kind: 'deploy'}]]);
});

test('failed receipt that reached GitHub keeps the workflow retry entry', async () => {
  const {context} = harness({state: 'failed', dispatch_receipt: 'accepted', workflow_run_id: 17});
  const root = node('div');
  await context.renderReleaseOutcome(root, {id: 'run', state: 'merged'}, 'p');
  assert.ok(findButton(root, '重试 · 部署'));
});
