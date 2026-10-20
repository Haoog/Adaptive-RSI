const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../adaptive_rsi/web/app.js'), 'utf8');
const response = (data, status = 200) => ({ok: status < 400, status, json: async () => data});
const rejected = () => response({error_code: 'update_in_progress'}, 503);
const snapshot = (version = '0.2.0', csrf = 'original-csrf') => ({
  version, csrf, update: {busy: false},
  projects: [{id: 'a', name: 'A', paused: true}, {id: 'b', name: 'B', paused: true}]
});

function browser() {
  const nodes = new Map(), listeners = new Map(), timers = [];
  let created = 0;
  const node = id => {
    if (!nodes.has(id)) nodes.set(id, {
      id, value: '', dataset: {}, handlers: {}, options: [], children: [], disabled: false,
      addEventListener(type, handler) { this.handlers[type] = handler; },
      closest() { return this.id.endsWith('-form') ? this : this.form; },
      replaceChildren(...children) { this.options = children; this.children = children; },
      append(...children) { this.children.push(...children); },
      querySelectorAll() { return []; },
      showModal() { this.open = true; },
      close() { this.open = false; this.handlers.close?.(); }
    });
    return nodes.get(id);
  };
  const document = {
    getElementById: node,
    querySelectorAll: () => [],
    addEventListener(type, handler) { listeners.set(type, handler); },
    createElement: tag => Object.assign(node('element-' + ++created), {tagName: tag.toUpperCase()}),
    createTextNode: text => ({textContent: text})
  };
  const context = vm.createContext({
    document, window: {}, initialState: snapshot(), structuredClone,
    localStorage: {getItem: () => null}, t: value => value,
    fetch: () => new Promise(() => {}),
    setInterval() {}, clearTimeout() {},
    setTimeout(callback, delay) { if (delay === 1000) timers.push(callback); },
    location: {reload() { throw new Error('A version change must retain pending forms'); }},
    lastAction: null
  });
  vm.runInContext(source, context);
  vm.runInContext('state=initialState; projectId="a"; render=()=>{}; safe=fn=>{lastAction=fn();return lastAction;}', context);
  node('project-select').options = [{value: 'a', text: 'A'}, {value: 'b', text: 'B'}];
  return {
    context, node, document,
    input(id, form) { const target = node(id); target.form = node(form); listeners.get('input')({target}); },
    submit(form) { node(form).handlers.submit({target: node(form), preventDefault() {}}); return context.lastAction; },
    async resume() {
      await new Promise(setImmediate);
      assert.equal(timers.length, 1, 'one pending update wait');
      timers.shift()();
    }
  };
}

test('settings edited during the acknowledgement refresh keep the saved baseline and the draft', async () => {
  const page = browser(), field = page.node('call-limit');
  Object.assign(field, {dataset: {path: 'limits.daily_calls'}, type: 'number', value: '30'});
  page.document.querySelectorAll = selector => selector === '[data-path]' ? [field] : [];
  page.node('config-scope').value = 'project';
  page.node('work-start').value = '0';
  page.node('work-end').value = '24';
  page.node('checks-json').value = '[]';
  vm.runInContext('view="settings"; settingsBaseline={limits:{daily_calls:20},schedule:{working_hours:[0,24]},github:{labels:[]},checks:[]}; renderSettings=()=>{$("call-limit").value="30";}', page.context);
  let finishRefresh;
  page.context.fetch = async (url) => url.endsWith('/state')
    ? new Promise(resolve => { finishRefresh = () => resolve(response(snapshot())); })
    : response({saved: true, config: {limits: {daily_calls: 30}}});
  const pending = page.submit('settings-form');
  await new Promise(setImmediate);
  assert.equal(vm.runInContext('settingsBaseline?.limits.daily_calls', page.context), 30);
  field.value = '20';
  page.input('call-limit', 'settings-form');
  finishRefresh();
  await pending;
  assert.equal(field.value, '20');
  assert.equal(vm.runInContext('settingsBaseline.limits.daily_calls', page.context), 30);
});

test('an explicit update rejection retries the original payload with fresh CSRF', async () => {
  const page = browser(), posts = [];
  page.context.fetch = async (url, options) => {
    if (url.endsWith('/state')) return response(snapshot('0.2.1', 'fresh-csrf'));
    posts.push({body: JSON.parse(options.body), csrf: options.headers['X-RSI-CSRF']});
    return posts.length === 1 ? rejected() : response({saved: true});
  };
  const payload = {project: 'a', values: {schedule: {weight: 3}}};
  const pending = page.context.api('config', payload);
  payload.project = 'b';
  payload.values.schedule.weight = 9;
  await page.resume();
  await pending;
  assert.equal(posts.length, 2);
  assert.deepEqual(posts.map(row => row.body), [
    {project: 'a', values: {schedule: {weight: 3}}},
    {project: 'a', values: {schedule: {weight: 3}}}
  ]);
  assert.equal(posts[1].csrf, 'fresh-csrf');
});

test('an ambiguous connection failure after a write is never replayed', async () => {
  const page = browser();
  let calls = 0;
  page.context.fetch = async () => { calls++; throw new TypeError('connection lost'); };
  await assert.rejects(page.context.api('projects/import', {url: 'https://github.com/example/project'}), /connection lost/);
  assert.equal(calls, 1);
});

test('a periodic refresh seeing the new version retains a queued submission', async () => {
  const page = browser();
  let posts = 0;
  page.context.fetch = async (url) => {
    if (url.endsWith('/state')) return response(snapshot('0.2.1'));
    return ++posts === 1 ? rejected() : response({saved: true});
  };
  const pending = page.context.api('config', {project: 'a', values: {}});
  await new Promise(setImmediate);
  await page.context.refresh();
  await page.resume();
  await pending;
  assert.equal(posts, 2);
});

for (const switchProject of [false, true]) {
  test(`a delayed goal preserves a newer draft${switchProject ? ' in another project' : ' in the same form'}`, async () => {
    const page = browser(), posts = [];
    page.node('goal-text').value = 'Original task';
    page.context.fetch = async (url, options) => {
      if (url.endsWith('/state')) return response(snapshot());
      posts.push(JSON.parse(options.body));
      return posts.length === 1 ? rejected() : response({id: 'task'});
    };
    const pending = page.submit('goal-form');
    await new Promise(setImmediate);
    if (switchProject) vm.runInContext('projectId="b"', page.context);
    page.node('goal-text').value = 'New unsaved task';
    page.input('goal-text', 'goal-form');
    await page.resume();
    await pending;
    assert.equal(page.node('goal-text').value, 'New unsaved task');
    assert.deepEqual(posts[1], {project: 'a', text: 'Original task'});
  });
}

for (const nextValue of [40, 20]) {
test(`delayed settings retain draft ${nextValue} against the acknowledged saved baseline`, async () => {
  const page = browser(), posts = [];
  const field = page.node('call-limit');
  Object.assign(field, {dataset: {path: 'limits.daily_calls'}, type: 'number', value: '30'});
  page.document.querySelectorAll = selector => selector === '[data-path]' ? [field] : [];
  page.node('config-scope').value = 'project';
  page.node('work-start').value = '0';
  page.node('work-end').value = '24';
  page.node('checks-json').value = '[]';
  vm.runInContext('settingsBaseline={limits:{daily_calls:20},schedule:{working_hours:[0,24]},github:{labels:[]},checks:[]}', page.context);
  page.context.fetch = async (url, options) => {
    if (url.endsWith('/state')) return response(snapshot());
    posts.push(JSON.parse(options.body));
    return posts.length === 1 ? rejected() : response({saved: true, config: {
      limits: {daily_calls: posts.at(-1).values.limits.daily_calls},
      schedule: {working_hours: [0, 24]}, github: {labels: []}, checks: []
    }});
  };
  const pending = page.submit('settings-form');
  await new Promise(setImmediate);
  field.value = String(nextValue);
  page.input('call-limit', 'settings-form');
  await page.resume();
  await pending;
  assert.equal(field.value, String(nextValue));
  assert.equal(vm.runInContext('settingsBaseline.limits.daily_calls', page.context), 30);
  assert.deepEqual(posts[1], {project: 'a', values: {limits: {daily_calls: 30}}});
  await page.submit('settings-form');
  assert.deepEqual(posts[2], {project: 'a', values: {limits: {daily_calls: nextValue}}});
});
}

test('task groups keep failures visible and background jobs available on demand', () => {
  const page = browser();
  const rows = [
    {id: 'queued', source: 'goal', state: 'queued'},
    {id: 'review', source: 'issue', state: 'awaiting_approval'},
    {id: 'failure', source: 'goal', state: 'regressed'},
    {id: 'done', source: 'goal', state: 'verified'},
    {id: 'background', source: 'system_delivery', state: 'failed'}
  ];
  const ids = (group, system = false) => Array.from(page.context.visibleTasks(rows, group, system), row => row.id);
  assert.deepEqual(ids('active'), ['queued']);
  assert.deepEqual(ids('attention'), ['review', 'failure']);
  assert.deepEqual(ids('attention', true), ['review', 'failure', 'background']);
  assert.deepEqual(ids('history'), ['done']);
});

test('duration fields preserve exact minutes across hours and days', () => {
  const page = browser(), unit = page.node('duration-unit');
  page.document.querySelector = () => unit;
  const field = {type: 'number', value: '2', dataset: {duration: 'schedule.light_minutes'}};
  unit.value = '60';
  assert.equal(page.context.fieldValue(field), 120);
  unit.value = '1440';
  field.value = String(120 / 1440);
  assert.equal(page.context.fieldValue(field), 120);
  field.value = '3';
  assert.equal(page.context.fieldValue(field), 4320);
  unit.value = '60';
  field.value = '0.333';
  assert.throws(() => page.context.fieldValue(field));
});

test('delivery summary preserves existing custom settings and disables global authorization', () => {
  const page = browser(), mode = page.node('delivery-mode'), auto = page.node('delivery-auto');
  page.document.querySelector = selector => selector.includes('auto_merge') ? auto : mode;
  page.node('config-scope').value = 'project';
  mode.value = 'local'; auto.checked = true;
  page.context.syncDelivery();
  assert.equal(page.node('delivery-choice').value, 'custom');
  assert.equal(mode.value, 'local');
  assert.equal(auto.checked, true);
  mode.value = 'pr';
  page.context.syncDelivery();
  assert.equal(page.node('delivery-choice').value, 'auto');
  page.node('config-scope').value = 'global';
  page.context.syncDelivery();
  assert.equal(page.node('delivery-choice').disabled, true);
});

test('out-of-order task details cannot attach cancellation for the previous task', async () => {
  const page = browser(), cancellations = [];
  const task = id => ({id, project: 'a', title: 'Task ' + id, body: '', evidence: '[]', state: 'executing'});
  const a = task('a'), b = task('b');
  page.context.initialState.runs = [{id: 'ra', task: 'a', project: 'a'}, {id: 'rb', task: 'b', project: 'a'}];
  let finishA;
  const detail = {config: {models: {}}, result: {}};
  page.context.fetch = async (url, options) => {
    if (url.includes('run?id=ra')) return new Promise(resolve => { finishA = () => resolve(response(detail)); });
    if (url.includes('run?id=rb')) return response(detail);
    if (url.endsWith('/cancel')) { cancellations.push(JSON.parse(options.body)); return response({saved: true}); }
    return response(snapshot());
  };
  const pendingA = page.context.showTask(a);
  await page.context.showTask(b);
  finishA(); await pendingA;
  assert.equal(page.node('detail-title').textContent, 'Task b');
  const buttons = page.node('detail-body').children.filter(node => node.textContent === '取消任务');
  assert.equal(buttons.length, 1);
  await buttons[0].handlers.click();
  assert.deepEqual(cancellations, [{task: 'b'}]);
});

test('closing task details while a response waits keeps the dialog closed', async () => {
  const page = browser();
  page.context.initialState.runs = [{id: 'ra', task: 'a', project: 'a'}];
  let finish;
  page.context.fetch = () => new Promise(resolve => { finish = () => resolve(response({config: {models: {}}, result: {}})); });
  const pending = page.context.showTask({id: 'a', title: 'Task a', evidence: '[]', state: 'executing'});
  page.context.closeTaskDetail(); finish(); await pending;
  assert.notEqual(page.node('detail-dialog').open, true);
  assert.equal(page.node('detail-body').children.filter(node => node.textContent === '取消任务').length, 0);
});

test('settings drafts retain independent baselines and duration units across project scopes', () => {
  const page = browser(), field = page.node('limit'), unit = page.node('unit-light_minutes');
  field.dataset.path = 'limits.daily_calls'; field.value = '41'; unit.value = '60';
  page.node('settings-form').querySelectorAll = () => [field, unit];
  page.document.querySelectorAll = () => [];
  page.node('config-scope').value = 'project';
  vm.runInContext('settingsContext="a";settingsBaseline={limits:{daily_calls:40}}', page.context);
  page.context.stashSettings();
  vm.runInContext('projectId="b";settingsContext="b";settingsBaseline={limits:{daily_calls:30}}', page.context);
  field.value = '52'; unit.value = '1440'; page.context.stashSettings();
  vm.runInContext('projectId="a"', page.context);page.context.restoreSettingsDraft();
  assert.equal(field.value, '41'); assert.equal(unit.value, '60');
  assert.equal(vm.runInContext('settingsBaseline.limits.daily_calls', page.context), 40);
  vm.runInContext('projectId="b"', page.context);page.context.restoreSettingsDraft();
  assert.equal(field.value, '52'); assert.equal(unit.value, '1440');
  assert.equal(vm.runInContext('settingsBaseline.limits.daily_calls', page.context), 30);
});
