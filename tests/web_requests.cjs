const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../adaptive_rsi/web/app.js'), 'utf8');
const authSource = fs.readFileSync(path.join(__dirname, '../adaptive_rsi/web/auth.js'), 'utf8');
const experienceSource = fs.readFileSync(path.join(__dirname, '../adaptive_rsi/web/experience.js'), 'utf8');
const response = (data, status = 200) => ({ok: status < 400, status, json: async () => data});
const rejected = () => response({error_code: 'update_in_progress'}, 503);
const snapshot = (version = '0.2.0', csrf = 'original-csrf') => ({
  version, csrf, update: {busy: false},
  projects: [{id: 'a', name: 'A', paused: true}, {id: 'b', name: 'B', paused: true}]
});

function browser(savedSession = new Map()) {
  const nodes = new Map(), listeners = new Map(), timers = [], local = new Map(), session = savedSession;
  let created = 0, nextTimer = 0;
  const node = id => {
    if (!nodes.has(id)) nodes.set(id, {
      id, value: '', dataset: {}, handlers: {}, options: [], children: [], disabled: false,
      attributes: {}, classList: {toggle() {}, add() {}, remove() {}}, isConnected: true,
      addEventListener(type, handler) { this.handlers[type] = handler; },
      closest() { return this.id.endsWith('-form') ? this : this.form; },
      replaceChildren(...children) { this.options = children; this.children = children; },
      append(...children) { this.children.push(...children); },
      prepend(...children) { this.children.unshift(...children); },
      insertBefore(child, sibling) { this.children.splice(this.children.indexOf(sibling), 0, child); },
      querySelectorAll() { return []; },
      querySelector(selector) { return selector==='[data-form-error]'?this.children.find(child=>child.dataset?.formError!==undefined):null; },
      setAttribute(name,value) { this.attributes[name]=value; this[name]=value; },
      hasAttribute(name) { return Object.hasOwn(this.attributes, name) || name==='data-unit-for' && !!this.dataset.unitFor; },
      focus() {}, scrollIntoView() {},
      showModal() { this.open = true; },
      close() { this.open = false; this.handlers.close?.(); }
    });
    return nodes.get(id);
  };
  const document = {
    getElementById: node,
    querySelectorAll: () => [],
    querySelector: () => null,
    activeElement: null,
    addEventListener(type, handler) { listeners.set(type, handler); },
    createElement: tag => Object.assign(node('element-' + ++created), {tagName: tag.toUpperCase()}),
    createTextNode: text => ({textContent: text})
  };
  const storage = values => ({
    getItem(key) { return values.has(key) ? values.get(key) : null; },
    setItem(key, value) { values.set(key, String(value)); },
    removeItem(key) { values.delete(key); },
    clear() { values.clear(); }
  });
  const defaultFetch = (_url, {signal} = {}) => new Promise((_resolve, reject) => {
    if (!signal) return;
    if (signal.aborted) reject(new DOMException('Aborted', 'AbortError'));
    else signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')), {once: true});
  });
  const context = vm.createContext({
    document, window: {}, initialState: snapshot(), structuredClone, URLSearchParams,
    localStorage: storage(local), sessionStorage: storage(session), t: value => value,
    AbortController, DOMException, queueMicrotask, crypto: require('node:crypto').webcrypto,
    fetch: defaultFetch,
    setInterval() {}, clearInterval() {},
    setTimeout(callback, delay) { const timer = {id: ++nextTimer, callback, delay, active: true}; timers.push(timer); return timer.id; },
    clearTimeout(id) { const timer = timers.find(value => value.id === id); if (timer) timer.active = false; },
    location: {reload() { throw new Error('A version change must retain pending forms'); }},
    lastAction: null
  });
  vm.runInContext(source, context);
  // The app starts a boot refresh; abort its isolated request so each test owns its fetches.
  vm.runInContext('refreshController?.abort();refreshSequence++;refreshPromise=null;refreshController=null;refreshProject=null;refreshKey=null;refreshQueued=false;refreshWaiters.splice(0);clearTimeout(refreshPollTimer);', context);
  vm.runInContext('originalSafe=safe; state=initialState; scopeInitialized=true; projectId="a"; render=()=>{}; safe=fn=>{lastAction=fn();return lastAction;}', context);
  node('project-select').options = [{value: 'a', text: 'A'}, {value: 'b', text: 'B'}];
  return {
    context, node, document, timers, sessionData: session,
    input(id, form) { const target = node(id); target.form = node(form); listeners.get('input')({target}); target.handlers.input?.({target}); },
    submit(form) { node(form).handlers.submit({target: node(form), preventDefault() {}}); return context.lastAction; },
    async resume() {
      await new Promise(setImmediate);
      const waits = timers.filter(timer => timer.active && timer.delay === 1000);
      assert.equal(waits.length, 1, 'one pending update wait');
      waits[0].active = false; waits[0].callback();
    },
    fireTimer(delay) {
      const timer = timers.find(value => value.active && value.delay === delay);
      assert.ok(timer, `pending ${delay}ms timer`); timer.active = false; timer.callback();
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
  page.context.fetch = async (url) => url.split('?')[0].endsWith('/state')
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

test('update check acknowledgment replaces stale failure and disables only update controls', async () => {
  const page=browser();
  vm.runInContext('state.update={state:"error",reason:"old-check-error",busy:false};renderUpdateExtras=()=>{};renderUpdate()',page.context);
  assert.match(page.node('update-status').textContent,/old-check-error/);
  page.context.fetch=async()=>response({started:true,update:{state:'checking',busy:false}});
  page.node('check-update').handlers.click();await page.context.lastAction;
  assert.match(page.node('update-status').textContent,/正在检查更新/);
  assert.doesNotMatch(page.node('update-status').textContent,/old-check-error/);
  assert.equal(page.node('check-update').disabled,true);
  assert.equal(page.node('apply-update').disabled,true);
  assert.equal(vm.runInContext('state.update.busy',page.context),false);
  vm.runInContext('state.update={state:"available",version:"9.0.0",busy:false};renderUpdate()',page.context);
  assert.equal(page.node('check-update').disabled,false);
  assert.equal(page.node('apply-update').disabled,false);
});

test('update check acknowledgment preserves a fast completed result', async () => {
  const page=browser();vm.runInContext('renderUpdateExtras=()=>{}',page.context);
  page.context.fetch=async()=>response({started:true,update:{state:'up_to_date',busy:false}});
  page.node('check-update').handlers.click();await page.context.lastAction;
  assert.match(page.node('update-status').textContent,/已是最新稳定版/);
  assert.equal(page.node('check-update').disabled,false);
  assert.equal(page.node('apply-update').disabled,true);
});

test('an explicit update rejection retries the original payload with fresh CSRF', async () => {
  const page = browser(), posts = [];
  page.context.fetch = async (url, options) => {
    if (url.split('?')[0].endsWith('/state')) return response(snapshot('0.2.1', 'fresh-csrf'));
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
  await assert.rejects(page.context.api('projects/import', {url: 'https://github.com/example/project'}), /无法连接本地服务/);
  assert.equal(calls, 1);
});

test('a periodic refresh seeing the new version retains a queued submission', async () => {
  const page = browser();
  let posts = 0;
  page.context.fetch = async (url) => {
    if (url.split('?')[0].endsWith('/state')) return response(snapshot('0.2.1'));
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
      if (url.split('?')[0].endsWith('/state')) return response(snapshot());
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
    assert.deepEqual(posts[1], {project: 'a', text: 'Original task', request_id: posts[0].request_id});
    assert.match(posts[1].request_id, /^[0-9a-f-]{36}$/);
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
    if (url.split('?')[0].endsWith('/state')) return response(snapshot());
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

for (const kind of ['release', 'deploy']) {
  for (const navigation of ['unchanged', 'same', 'project-away', 'project-round-trip', 'scope-away', 'scope-round-trip', 'clean-project-away', 'clean-scope-away']) {
    test(`${kind} authorization acknowledges only its fields and preserves newer edits after ${navigation}`, async () => {
      const page = browser(), fields = new Map(), posts = [];
      const policy = {enabled: false, workflow: '.github/workflows/old.yml', ref: 'old', sha: 'b'.repeat(40)};
      const config = {schedule: {weight: 1, working_hours: [0, 24]}, github: {labels: []}, checks: [],
        delivery: {mode: 'pr', auto_merge: false, release: {...policy}, deploy: {...policy, health_url: 'https://old.test/health', automatic_rollback: false}}};
      for (const path of ['schedule.weight', ...['release', 'deploy'].flatMap(type => Object.keys(config.delivery[type]).map(name => `delivery.${type}.${name}`))]) {
        const value = path.split('.').reduce((node, key) => node[key], config);
        fields.set(path, Object.assign(page.node(path), {type: typeof value === 'boolean' ? 'checkbox' : typeof value === 'number' ? 'number' : 'text', dataset: {path}}));
      }
      const all = [...fields.values()];
      page.document.querySelectorAll = selector => selector === '[data-path]' ? all : [];
      page.document.querySelector = selector => fields.get(selector.match(/data-path="([^"]+)"/)?.[1]) || null;
      page.node('settings-form').querySelectorAll = selector => ['input,textarea,select', '[data-path]'].includes(selector) ? all : [];
      page.node('settings-form').querySelector = page.document.querySelector;
      page.node('config-scope').value = 'project'; page.node('schedule-mode').value = 'custom';
      page.node('work-start').value = '0'; page.node('work-end').value = '24'; page.node('checks-json').value = '[]';
      page.context.initialState.config = structuredClone(config);
      page.context.initialState.projects.forEach(project => { project.config = structuredClone(config); });
      // Exercise production draft restoration and navigation without unrelated rendering or requests.
      vm.runInContext(`view="settings";syncDelivery=()=>{};renderNavigation=()=>{};
        renderSettings=()=>{settingsBaseline=structuredClone(settingsKey()==="global"?state.config:currentProject().config);settingsContext=settingsKey();settingsModes.set(settingsBaseline,"custom");
          for(const node of document.querySelectorAll('[data-path]')){const value=getPath(settingsBaseline,node.dataset.path);node.value=node.type==="checkbox"?"on":String(value);node.checked=node.type==="checkbox"?value:false;}
          settingsClean.set(settingsContext,JSON.stringify(settingsValues()));restoreSettingsDraft();};
        render=()=>{if(!settingsBaseline)renderSettings();};render();`, page.context);
      page.context.refresh = async () => {};
      const edit = (path, value) => { const node = fields.get(path); if (node.type === 'checkbox') node.checked = value; else node.value = value; page.input(path, 'settings-form'); page.context.stashSettings(); };
      const switchProject = project => { page.node('project-select').value = project; page.node('project-select').handlers.change(); };
      const switchScope = scope => page.context.changeSettingsScope(scope);
      let finish;
      const authorization = {enabled: true, workflow: '.github/workflows/chosen.yml', ref: 'trusted', sha: 'a'.repeat(40)};
      page.context.fetch = async (_url, options) => {
        posts.push(JSON.parse(options.body));
        if (posts.length === 1) return new Promise(resolve => { finish = () => resolve(response({...authorization, health_url: 'https://old.test/health', automatic_rollback: false})); });
        return response({saved: true});
      };
      page.node('release-workflow-list').value = authorization.workflow;
      const pending = page.context.authorizeRelease(kind);
      if (navigation !== 'unchanged') {
        // Returning to the original false value is still a newer explicit disable choice.
        edit(`delivery.${kind}.enabled`, true); edit(`delivery.${kind}.enabled`, false);
        if (!navigation.startsWith('clean-')) edit(`delivery.${kind}.workflow`, '.github/workflows/newer.yml');
      }
      const other = kind === 'release' ? 'deploy' : 'release', cleanReturn = navigation.startsWith('clean-');
      if (!cleanReturn) {
        edit(`delivery.${other}.ref`, 'other-kind-draft');
        edit('delivery.deploy.health_url', 'https://newer.test/health');
        edit('delivery.deploy.automatic_rollback', true);
        edit('schedule.weight', '2');
      } else {
        assert.equal(vm.runInContext('settingsDrafts.has("a")', page.context), false, 'returning to clean removes the draft');
      }
      if (navigation.includes('project-')) switchProject('b');
      if (navigation.includes('scope-')) switchScope('global');
      if (navigation.endsWith('round-trip')) {
        edit('schedule.weight', '4');
        if (navigation.startsWith('project-')) switchProject('a'); else switchScope('project');
      }
      const visibleBefore = JSON.stringify(page.context.settingsValues());
      finish(); await pending;
      if (navigation.endsWith('away')) {
        assert.equal(JSON.stringify(page.context.settingsValues()), visibleBefore, 'acknowledgement must not touch another scope');
        if (navigation.includes('project-')) switchProject('a'); else switchScope('project');
      }
      assert.equal(fields.get(`delivery.${kind}.enabled`).checked, navigation === 'unchanged');
      assert.equal(fields.get(`delivery.${kind}.workflow`).value, navigation === 'unchanged' || cleanReturn ? authorization.workflow : '.github/workflows/newer.yml');
      assert.equal(fields.get(`delivery.${kind}.ref`).value, 'trusted', 'untouched field accepts the acknowledgement');
      assert.equal(fields.get(`delivery.${kind}.sha`).value, authorization.sha);
      assert.equal(fields.get(`delivery.${other}.ref`).value, cleanReturn ? 'old' : 'other-kind-draft');
      assert.equal(fields.get('delivery.deploy.health_url').value, cleanReturn ? 'https://old.test/health' : 'https://newer.test/health');
      assert.equal(fields.get('delivery.deploy.automatic_rollback').checked, !cleanReturn);
      assert.equal(fields.get('schedule.weight').value, cleanReturn ? '1' : '2');
      assert.deepEqual(JSON.parse(vm.runInContext(`JSON.stringify(settingsBaseline.delivery.${kind})`, page.context)), {...config.delivery[kind], ...authorization});
      if (navigation !== 'unchanged') assert.equal(vm.runInContext(`draftStore.get("settings","a").values.find(entry=>entry.path==="delivery.${kind}.enabled").baselineValue`, page.context), true);
      await page.submit('settings-form');
      assert.equal(posts[1].project, 'a');
      const expectedKindPatch = {...(navigation === 'unchanged' ? {} : cleanReturn ? {enabled: false} : {enabled: false, workflow: '.github/workflows/newer.yml'}), ...(kind === 'deploy' && !cleanReturn ? {health_url: 'https://newer.test/health', automatic_rollback: true} : {})};
      assert.deepEqual(posts[1].values.delivery[kind] || {}, expectedKindPatch);
      if (cleanReturn) assert.deepEqual(posts[1].values, {delivery: {[kind]: {enabled: false}}});
      else {assert.equal(posts[1].values.delivery[other].ref, 'other-kind-draft');assert.equal(posts[1].values.schedule.weight, 2);}
      if (navigation.endsWith('round-trip')) {
        if (navigation.startsWith('project-')) switchProject('b'); else switchScope('global');
        assert.equal(fields.get('schedule.weight').value, '4', 'the other scope retains its draft');
      }
    });
  }
}

test('out-of-order task details cannot attach cancellation for the previous task', async () => {
  const page = browser(), cancellations = [];
  const task = id => ({id, project: 'a', title: 'Task ' + id, body: '', evidence: '[]', state: 'executing'});
  const a = task('a'), b = task('b');
  page.context.initialState.runs = [{id: 'ra', task: 'a', project: 'a'}, {id: 'rb', task: 'b', project: 'a'}];
  let finishA;
  const detail = {config: {models: {}}, result: {}};
  page.context.fetch = async (url, options) => {
    if (url.includes('task-runs?task=a')) return response([{id:'ra',task:'a',project:'a'}]);
    if (url.includes('task-runs?task=b')) return response([{id:'rb',task:'b',project:'a'}]);
    if (url.includes('run?id=ra')) return new Promise(resolve => { finishA = () => resolve(response(detail)); });
    if (url.includes('run?id=rb')) return response(detail);
    if (url.endsWith('/cancel')) { cancellations.push(JSON.parse(options.body)); return response({saved: true}); }
    return response(snapshot());
  };
  const pendingA = page.context.showTask(a);
  await new Promise(setImmediate);
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
  vm.runInContext('settingsContext="a";settingsBaseline={limits:{daily_calls:40},schedule:{working_hours:[0,24]},github:{labels:[]}};settingsModes.set(settingsBaseline,"inherit")', page.context);
  page.context.stashSettings();
  vm.runInContext('projectId="b";settingsContext="b";settingsBaseline={limits:{daily_calls:30},schedule:{working_hours:[0,24]},github:{labels:[]}};settingsModes.set(settingsBaseline,"inherit")', page.context);
  field.value = '52'; unit.value = '1440'; page.context.stashSettings();
  vm.runInContext('projectId="a";settingsBaseline={limits:{daily_calls:40},schedule:{working_hours:[0,24]},github:{labels:[]}};settingsModes.set(settingsBaseline,"inherit")', page.context);page.context.restoreSettingsDraft();
  assert.equal(field.value, '41'); assert.equal(unit.value, '60');
  assert.equal(vm.runInContext('settingsBaseline.limits.daily_calls', page.context), 40);
  vm.runInContext('projectId="b";settingsBaseline={limits:{daily_calls:30},schedule:{working_hours:[0,24]},github:{labels:[]}};settingsModes.set(settingsBaseline,"inherit")', page.context);page.context.restoreSettingsDraft();
  assert.equal(field.value, '52'); assert.equal(unit.value, '1440');
  assert.equal(vm.runInContext('settingsBaseline.limits.daily_calls', page.context), 30);
});

test('safe settings and schedule mode survive reload without persisting secrets', () => {
  const storage = new Map(), first = browser(storage);
  const baseline = {schedule:{light_minutes:60,issues_minutes:120,timezone:'UTC',working_hours:[0,24],weight:1},sources:{inspection:true,issues:true},github:{labels:[]},checks:[],delivery:{mode:'local',auto_merge:false}};
  const configure = page => {
    const interval=page.node('light-interval'),mode=page.node('schedule-mode'),key=page.node('api-key'),code=page.node('oauth-code');
    const start=page.node('work-start'),end=page.node('work-end'),labels=page.node('issue-labels'),checks=page.node('checks-json');
    Object.assign(interval,{type:'number',value:'120',dataset:{path:'schedule.light_minutes'}});
    Object.assign(mode,{value:'custom'});
    Object.assign(key,{type:'password',value:'NEVER-PERSIST-THIS-API-KEY',dataset:{path:'auth.api_key'}});
    Object.assign(code,{type:'text',value:'NEVER-PERSIST-THIS-OAUTH-CODE'});
    Object.assign(start,{type:'number',value:'8'});Object.assign(end,{type:'number',value:'17'});
    Object.assign(labels,{type:'text',value:'bug, urgent'});Object.assign(checks,{type:'textarea',value:'["RAW-CHECKS-MUST-NOT-PERSIST"]'});
    const form=page.node('settings-form');
    form.querySelectorAll=selector=>selector==='[data-path]'?[interval,key]:selector==='input,textarea,select'?[interval,key,code,mode,start,end,labels,checks]:[];
    page.node('config-scope').value='project';
    vm.runInContext(`settingsContext="a";settingsBaseline=${JSON.stringify(baseline)};settingsModes.set(settingsBaseline,"inherit");settingsClean.set("a","[]")`,page.context);
    return {interval,mode,key,code,start,end,labels,checks};
  };
  const firstFields=configure(first);first.context.stashSettings();
  const stored=JSON.stringify([...storage.values()]);
  assert.doesNotMatch(stored,/NEVER-PERSIST-THIS-(?:API-KEY|OAUTH-CODE)|RAW-CHECKS-MUST-NOT-PERSIST/);
  const entries=JSON.parse(storage.get('rsi-ui-drafts')).entries.settings.a.value.values;
  assert.deepEqual(entries.map(entry=>entry.id).sort(),['issue-labels','light-interval','schedule-mode','work-end','work-start']);
  assert.equal(entries.find(entry=>entry.id==='schedule-mode').baselineValue,'inherit');

  const second=browser(storage),restored=configure(second);
  restored.interval.value='60';restored.mode.value='inherit';restored.start.value='0';restored.end.value='24';restored.labels.value='';restored.checks.value='';
  vm.runInContext('restoreSettingsDraft()',second.context);
  assert.equal(restored.interval.value,'120');
  assert.equal(restored.mode.value,'custom');
  assert.equal(restored.start.value,'8');assert.equal(restored.end.value,'17');assert.equal(restored.labels.value,'bug, urgent');
  assert.equal(restored.checks.value,'');
  assert.equal(vm.runInContext('settingsBaseline.schedule.light_minutes',second.context),60);
});

test('scheduled check times use and label the project timezone', () => {
  const page=browser();page.context.language='en';
  const instant=Date.parse('2026-10-22T00:00:00Z')/1000;
  const utc=vm.runInContext(`scheduleStamp(${instant},"UTC")`,page.context);
  const losAngeles=vm.runInContext(`scheduleStamp(${instant},"America/Los_Angeles")`,page.context);
  assert.notEqual(utc,losAngeles);
  const i18n=fs.readFileSync(path.join(__dirname,'../adaptive_rsi/web/i18n.js'),'utf8');
  const english=vm.runInNewContext(i18n.slice(0,i18n.indexOf('let language'))+';english');
  assert.equal(english['项目时区'],'Project timezone');
});

function paginationPage(offset = 50) {
  const page = browser();
  page.context.language = 'zh-CN';
  page.node('task-filter').value = 'active';
  page.node('show-system-tasks').checked = false;
  vm.runInContext(`taskOffset=${offset};render=()=>renderTasks()`, page.context);
  return page;
}

function taskPage(total, offset, {project = 'a', filter = 'active', system = false} = {}) {
  const count = Math.max(0, Math.min(50, total - offset));
  return {...snapshot(), selected_project: project, sections: ['core'],
    tasks: Array.from({length: count}, (_, i) => ({
      ...fixtureTask(String(offset + i), filter === 'attention' ? 'failed' : 'queued'),
      project, source: system ? 'system_light' : 'goal', updated: 1
    })), runs: [], pagination: {total, offset, limit: 50, has_more: offset + 50 < total}};
}

for (const total of [50, 0]) {
  test(`a shrinking task group refetches page zero before rendering (${total} remaining)`, async () => {
    const page = paginationPage(), requests = [], rendered = [];
    page.context.recordPage = () => rendered.push(page.node('tasks-page').textContent);
    vm.runInContext('render=()=>{renderTasks();recordPage()}', page.context);
    page.context.fetch = async (url, {signal}) => {
      const params = new URLSearchParams(url.split('?')[1]);
      requests.push({params, signal});
      return response(taskPage(total, Number(params.get('offset'))));
    };
    await page.context.refresh();
    assert.deepEqual(requests.map(r => r.params.get('offset')), ['50', '0']);
    assert.equal(requests[0].signal, requests[1].signal, 'correction shares cancellation and timeout');
    for (const {params} of requests) {
      assert.equal(params.get('project'), 'a');
      assert.equal(params.get('filter'), 'active');
      assert.equal(params.get('system'), 'false');
    }
    assert.deepEqual(rendered, [total ? '1–50 / 50' : '0–0 / 0']);
    assert.equal(vm.runInContext('taskOffset', page.context), 0);
    assert.equal(page.node('tasks-prev').disabled, true);
    assert.equal(page.node('tasks-next').disabled, true);
  });
}

test('a valid final page is kept and renders its returned range', async () => {
  const page = paginationPage(); let calls = 0;
  page.context.fetch = async () => { calls++; return response(taskPage(51, 50)); };
  await page.context.refresh();
  assert.equal(calls, 1);
  assert.equal(page.node('tasks-page').textContent, '51–51 / 51');
  assert.equal(vm.runInContext('taskOffset', page.context), 50);
  vm.runInContext('taskOffset=100;renderTasks()', page.context);
  assert.equal(page.node('tasks-page').textContent, '', 'old page must not be relabeled with the requested offset');
  vm.runInContext('state={...state,tasks:[],pagination:{total:50,offset:100,limit:50,has_more:false}};renderTasks()', page.context);
  assert.equal(page.node('tasks-page').textContent, '', 'invalid ranges are never clamped into a fictitious loaded page');
});

for (const navigation of ['project', 'group', 'system']) {
  for (const stage of ['initial', 'correction']) {
    test(`pagination correction respects ${navigation} navigation during ${stage}`, async () => {
      const page = paginationPage(), requests = []; let release, oldSignal;
      page.context.fetch = async (url, {signal}) => {
        const params = new URLSearchParams(url.split('?')[1]); requests.push(params);
        if (requests.length === (stage === 'initial' ? 1 : 2)) {
          oldSignal = signal;
          // A response may complete even after its transport was aborted.
          return new Promise(resolve => { release = () => resolve(response(taskPage(50, Number(params.get('offset'))))); });
        }
        return response(taskPage(50, Number(params.get('offset')), {
          project: params.get('project'), filter: params.get('filter'), system: params.get('system') === 'true'
        }));
      };
      const pending = page.context.refresh(); await new Promise(setImmediate);
      assert.equal(typeof release, 'function');
      if (navigation === 'project') vm.runInContext('projectId="b";taskOffset=0', page.context);
      if (navigation === 'group') vm.runInContext('$("task-filter").value="attention";taskOffset=0', page.context);
      if (navigation === 'system') vm.runInContext('$("show-system-tasks").checked=true;taskOffset=0', page.context);
      const navigated = page.context.refresh();
      assert.equal(oldSignal.aborted, true);
      release(); await Promise.all([pending, navigated]);
      assert.equal(requests.length, stage === 'initial' ? 2 : 3);
      const last = requests.at(-1);
      assert.equal(last.get('project'), navigation === 'project' ? 'b' : 'a');
      assert.equal(last.get('filter'), navigation === 'group' ? 'attention' : 'active');
      assert.equal(last.get('system'), String(navigation === 'system'));
      assert.equal(last.get('offset'), '0');
      assert.equal(page.node('tasks-page').textContent, '1–50 / 50');
      assert.equal(vm.runInContext('state.selected_project', page.context), last.get('project'));
    });
  }
}

test('same-scope refreshes share the corrective GET and an aborted correction releases queued navigation', async () => {
  const page = paginationPage(), requests = []; let correctingSignal;
  page.context.fetch = async (url, {signal}) => {
    const params = new URLSearchParams(url.split('?')[1]); requests.push(params);
    if (requests.length === 1) return response(taskPage(50, 50));
    if (requests.length === 2) {
      correctingSignal = signal;
      return new Promise((_resolve, reject) => signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')), {once: true}));
    }
    return response(taskPage(1, 0, {project: 'b'}));
  };
  const pending = page.context.refresh(); await new Promise(setImmediate);
  const duplicate = page.context.refresh(); await new Promise(setImmediate);
  assert.equal(requests.length, 2, 'same scope must not start another correction');
  vm.runInContext('projectId="b";taskOffset=0', page.context);
  const navigated = page.context.refresh();
  await Promise.all([pending, duplicate, navigated]);
  assert.equal(correctingSignal.aborted, true);
  assert.equal(requests.length, 3);
  assert.equal(page.node('tasks-page').textContent, '1–1 / 1');
  assert.equal(vm.runInContext('state.selected_project', page.context), 'b');
});

test('a changed group cannot trigger a correction even before a replacement refresh starts', async () => {
  const page = paginationPage(); let release, calls = 0;
  page.context.fetch = () => { calls++; return new Promise(resolve => { release = () => resolve(response(taskPage(50, 50))); }); };
  const pending = page.context.refresh(); await new Promise(setImmediate);
  page.node('task-filter').value = 'attention';
  release(); await pending;
  assert.equal(calls, 1);
  assert.equal(vm.runInContext('taskOffset', page.context), 50);
  assert.equal(vm.runInContext('state.pagination', page.context), undefined);
});

test('a corrective response with the old offset is discarded without looping or relabeling data', async () => {
  const page = paginationPage(); let calls = 0;
  page.context.fetch = async () => { calls++; return response(taskPage(50, 50)); };
  await page.context.refresh();
  assert.equal(calls, 2);
  assert.equal(vm.runInContext('taskOffset', page.context), 50);
  assert.equal(vm.runInContext('state.pagination', page.context), undefined);
});

test('a timed-out pagination correction cannot apply a late success or start another correction', async () => {
  const page = paginationPage(); let release, calls = 0, signal;
  page.context.fetch = async (_url, options) => {
    calls++; signal = options.signal;
    if (calls === 1) return response(taskPage(50, 50));
    return new Promise(resolve => { release = () => resolve(response(taskPage(50, 0))); });
  };
  const pending = page.context.refresh(); await new Promise(setImmediate);
  page.fireTimer(15000); assert.equal(signal.aborted, true);
  release(); await pending;
  assert.equal(calls, 2);
  assert.equal(vm.runInContext('taskOffset', page.context), 50);
  assert.equal(vm.runInContext('state.pagination', page.context), undefined);
});

test('a failed corrective GET leaves the loaded state intact and releases refresh ownership', async () => {
  const page = paginationPage(); let calls = 0;
  page.context.fetch = async () => {
    if (++calls === 1) return response(taskPage(50, 50));
    throw new Error('offline');
  };
  await assert.rejects(page.context.refresh(), /无法连接本地服务/);
  assert.equal(calls, 2);
  assert.equal(vm.runInContext('taskOffset', page.context), 50);
  assert.equal(vm.runInContext('state.pagination', page.context), undefined);
  assert.equal(vm.runInContext('refreshPromise', page.context), null);
});

test('a slow refresh merges same-scope manual and periodic requests into one GET', async () => {
  const page=browser();let finish,calls=0;
  page.context.fetch=(url,{signal}={})=>{
    if(!url.split('?')[0].endsWith('/state'))return response({});
    calls++;
    return new Promise((resolve,reject)=>{
      finish=()=>resolve(response({...snapshot('new'),selected_project:'a',sections:['core']}));
      signal?.addEventListener('abort',()=>reject(new DOMException('Aborted','AbortError')),{once:true});
    });
  };
  vm.runInContext('view="settings";state={...initialState,selected_project:"a",version:"old"}',page.context);
  const first=vm.runInContext('refresh()',page.context);
  await new Promise(setImmediate);
  const sameScope=vm.runInContext('refresh()',page.context);
  vm.runInContext('scheduleRefreshPoll(5000)',page.context);
  page.fireTimer(5000);
  await new Promise(setImmediate);
  assert.equal(calls,1,'a slow GET must not be invalidated or duplicated');
  finish();await Promise.all([first,sameScope]);
  assert.equal(vm.runInContext('state.version',page.context),'new');
});

test('switching projects aborts the old GET and waits for the new project state', async () => {
  const page=browser(),requests=[];let oldSignal;
  page.context.fetch=(url,{signal}={})=>{
    const project=new URLSearchParams(url.split('?')[1]).get('project');requests.push(project);
    if(project==='a'){
      oldSignal=signal;
      return new Promise((_resolve,reject)=>signal.addEventListener('abort',()=>reject(new DOMException('Aborted','AbortError')),{once:true}));
    }
    return Promise.resolve(response({...snapshot('b'),selected_project:'b',sections:['core']}));
  };
  vm.runInContext('view="settings";state={...initialState,selected_project:"a",version:"old"}',page.context);
  const old=vm.runInContext('refresh()',page.context);await new Promise(setImmediate);
  vm.runInContext('projectId="b"',page.context);
  await vm.runInContext('refresh()',page.context);
  await old;
  assert.equal(oldSignal.aborted,true);
  assert.deepEqual(requests,['a','b']);
  assert.equal(vm.runInContext('state.version',page.context),'b');
  assert.equal(vm.runInContext('projectId',page.context),'b');
});

test('core-only state keeps loaded analytics, then evolution requests missing sections', async () => {
  const page=browser(),urls=[];
  page.context.fetch=async url=>{
    urls.push(url);
    if(url.includes('sections=core%2Ccandidates%2Canalytics'))return response({
      ...snapshot('full'),selected_project:'a',sections:['core','candidates','analytics'],
      candidates:[{id:'c1'}],cases:[{project:'a',split:'development',count:4}],
      quality:[{project:'a'}],usage:[{project:'a',tokens:10}],model_usage:[]
    });
    return response({...snapshot('core'),selected_project:'a',sections:['core']});
  };
  vm.runInContext('view="settings";state={...initialState,selected_project:"a",usage:[{project:"a",tokens:7}],cases:[{project:"a",split:"development",count:3}],candidates:[{id:"old"}]};loadedStateSections.analyticsProject="a";loadedStateSections.analyticsAt=Date.now();loadedStateSections.candidatesProject="a";loadedStateSections.candidatesAt=Date.now()',page.context);
  await vm.runInContext('refresh()',page.context);
  assert.equal(vm.runInContext('state.usage[0].tokens',page.context),7);
  assert.equal(vm.runInContext('state.cases[0].count',page.context),3);
  assert.equal(vm.runInContext('state.candidates[0].id',page.context),'old');
  vm.runInContext('view="evolution";loadedStateSections.analyticsProject=null;loadedStateSections.candidatesProject=null',page.context);
  await vm.runInContext('refresh()',page.context);
  assert.match(urls[1],/sections=core%2Ccandidates%2Canalytics/);
  assert.equal(vm.runInContext('state.cases[0].count',page.context),4);
  assert.equal(vm.runInContext('state.candidates[0].id',page.context),'c1');
});

for(const staleFails of [false,true]){
test(`an outdated readiness ${staleFails?'failure':'success'} cannot erase the newer result`,async()=>{
  const page=browser(),pending=[];
  page.context.renderReadiness=()=>{};
  page.context.fetch=()=>new Promise(resolve=>pending.push(resolve));
  const older=page.context.checkReadiness(),newer=page.context.checkReadiness(true);
  pending[1](response({ready:true,items:[]}));await newer;
  pending[0](response(staleFails?{error:'outdated readiness error'}:{ready:false,items:[]},staleFails?500:200));await older;
  assert.equal(vm.runInContext('readinessCache.get("a").ready',page.context),true);
  assert.notEqual(page.node('readiness-summary').textContent,'outdated readiness error');
});
}

test('a late readiness error in another project leaves the current check pending',async()=>{
  const page=browser(),pending=[];
  page.context.renderReadiness=()=>{};
  page.context.fetch=()=>new Promise(resolve=>pending.push(resolve));
  const older=page.context.checkReadiness();
  vm.runInContext('projectId="b"',page.context);
  const current=page.context.checkReadiness();
  pending[0](response({error:'outdated project error'},500));await older;
  assert.equal(vm.runInContext('readinessCache.has("a")',page.context),false);
  assert.equal(vm.runInContext('readinessCache.get("b").loading',page.context),true);
  pending[1](response({ready:true,items:[]}));await current;
  assert.equal(vm.runInContext('readinessCache.get("b").ready',page.context),true);
});

test('expired authentication is actionable and never retries a mutation', async () => {
  const page=browser();let calls=0;
  page.context.fetch=async()=>{calls++;return response({error:'internal auth message'},401);};
  await assert.rejects(vm.runInContext('api("goal",{project:"a",text:"keep me"})',page.context),error=>error.code==='session_required'&&error.message.includes('输入已保留'));
  assert.equal(calls,1);
});

test('temporary auth polling failures preserve authorization; explicit expiry stops polling', async () => {
  const visibleText=node=>[node.textContent||'',...(node.children||[]).map(visibleText)].join(' ');
  async function authPage(sessionReply){
    const page=browser();let sessionCalls=0;
    page.context.safe=page.context.originalSafe;
    page.context.fetch=async url=>{
      if(url.endsWith('/api/auth/status'))return response({sessions:[{backend:'codex',provider:null,id:'session-1',state:'awaiting_authorization'}],codex:{available:true,state:'signed_out'}});
      if(url.includes('/api/auth/session?id=session-1')){
        sessionCalls++;
        return sessionCalls===1?response({id:'session-1',backend:'codex',state:'awaiting_authorization',method:'auto'}):sessionReply;
      }
      return response({});
    };
    vm.runInContext(authSource,page.context);
    page.context.window.RSIAuth.show('openai',()=>Promise.resolve());
    await new Promise(setImmediate);await new Promise(setImmediate);
    return {page,sessionCalls:()=>sessionCalls,visibleText};
  }
  const temporary=await authPage(response({error:'gateway unavailable'},503));
  temporary.page.fireTimer(1500);await new Promise(setImmediate);await new Promise(setImmediate);
  const temporaryText=temporary.visibleText(temporary.page.node('mc-auth'));
  assert.match(temporaryText,/授权状态已保留/);
  assert.doesNotMatch(temporaryText,/登录失败/);
  assert.equal(temporary.sessionCalls(),2);

  const terminal=await authPage(response({error_code:'unknown_session',error:'Session no longer exists'},404));
  terminal.page.fireTimer(1500);await new Promise(setImmediate);await new Promise(setImmediate);
  const terminalText=terminal.visibleText(terminal.page.node('mc-auth'));
  assert.match(terminalText,/登录已中断/);
  assert.equal(terminal.page.timers.filter(timer=>timer.active&&timer.delay===1500).length,0);
});

test('non JSON errors give recovery instructions rather than a parser exception', async () => {
  const page=browser();
  page.context.fetch=async()=>({ok:false,status:502,json:async()=>{throw new SyntaxError('HTML error page');}});
  await assert.rejects(vm.runInContext('api("state")',page.context),error=>error.status===502&&/无法识别的结果/.test(error.message));
});

test('one form cannot submit twice while its first write is pending', async () => {
  const page=browser();let release,calls=0;
  page.context.fetch=async url=>{
    if(url.split('?')[0].endsWith('/state'))return response(snapshot());
    calls++;return new Promise(resolve=>release=()=>resolve(response({added:true})));
  };
  vm.runInContext('safe=fn=>{lastAction=originalSafe(fn,$("goal-form"));return lastAction;}',page.context);
  page.node('goal-text').value='one goal';
  const first=page.submit('goal-form');await page.submit('goal-form');
  assert.equal(calls,1);release();await first;
  page.node('goal-text').value='next goal';
  const next=page.submit('goal-form');assert.equal(calls,2);release();await next;
});

test('form errors stay next to preserved input and clear on retry', async () => {
  const page=browser();
  page.node('goal-text').value='preserve input';
  await vm.runInContext('originalSafe(async()=>{throw new SyntaxError("bad JSON");},$("goal-form"))',page.context);
  const feedback=page.node('goal-form').children[0];
  assert.equal(feedback.role,'alert');assert.match(feedback.textContent,/JSON 格式有误/);
  assert.equal(page.node('goal-text').value,'preserve input');
  assert.equal(page.node('toast').hidden,false);
  await vm.runInContext('originalSafe(async()=>{},$("goal-form"))',page.context);
  assert.equal(feedback.hidden,true);
});

test('toolchain results from the previous project cannot appear in current settings', async () => {
  const page=browser();let finishA,finishB;
  page.context.fetch=url=>new Promise(resolve=>{if(url.endsWith('project=a'))finishA=resolve;else finishB=resolve;});
  const first=vm.runInContext('renderCapabilities()',page.context);
  const second=vm.runInContext('projectId="b";renderCapabilities()',page.context);
  finishB(response({modules:[{language:'B language',manifest:'b.json',status:'executable'}]}));await second;
  finishA(response({modules:[{language:'A language',manifest:'a.json',status:'executable'}]}));await first;
  const rows=page.node('capabilities').children;
  assert.equal(rows.length,1);assert.equal(rows[0].children[0].textContent,'B language');
});

test('unchanged polling retains DOM state, but project or language changes invalidate it', () => {
  const page=browser();
  page.context.language='zh-CN';
  assert.equal(vm.runInContext('changedList($("activity"),[{id:1}])',page.context),true);
  assert.equal(vm.runInContext('changedList($("activity"),[{id:1}])',page.context),false);
  assert.equal(vm.runInContext('projectId="b";changedList($("activity"),[{id:1}])',page.context),true);
  assert.equal(vm.runInContext('language="en";changedList($("activity"),[{id:1}])',page.context),true);
});

test('project settings are the initial default without overriding an explicit global choice', async () => {
  const page=browser();page.context.fetch=async()=>response(snapshot());
  page.node('config-scope').value='global';vm.runInContext('scopeInitialized=false',page.context);
  await vm.runInContext('refresh()',page.context);
  assert.equal(page.node('config-scope').value,'project');
  page.node('config-scope').value='global';vm.runInContext('scopeChosen=true',page.context);
  await vm.runInContext('refresh()',page.context);
  assert.equal(page.node('config-scope').value,'global');
});

test('an import form remains unchanged when polling auto-selects a newly imported project', () => {
  const page=browser();
  vm.runInContext('projectId="";importInputUnchanged=unchangedForm($("project-form"),{projectSensitive:false});projectId="new-project";',page.context);
  assert.equal(vm.runInContext('importInputUnchanged()',page.context),true);
  page.input('project-urls','project-form');
  assert.equal(vm.runInContext('importInputUnchanged()',page.context),false);
});

test('goal drafts stay with their project and submit only to that project', async () => {
  const page=browser(),posts=[];
  page.context.refresh=()=>Promise.resolve();
  page.context.fetch=async(url,options)=>{posts.push(JSON.parse(options.body));return response({id:'task'});};
  page.node('goal-text').value='Only project A: update the guide';page.input('goal-text','goal-form');
  page.node('project-select').value='b';page.node('project-select').handlers.change({target:page.node('project-select')});
  assert.equal(page.node('goal-text').value,'');
  page.node('goal-text').value='Only project B: fix a typo';page.input('goal-text','goal-form');
  page.node('project-select').value='a';page.node('project-select').handlers.change({target:page.node('project-select')});
  assert.equal(page.node('goal-text').value,'Only project A: update the guide');
  await page.submit('goal-form');
  assert.deepEqual(posts[0],{project:'a',text:'Only project A: update the guide',request_id:posts[0].request_id});
  assert.match(posts[0].request_id,/^[0-9a-f-]{36}$/);
  assert.equal(page.node('goal-text').value,'');
  assert.equal(vm.runInContext('window.RSIDraftStore.get("goal","a")',page.context),null);
  page.node('project-select').value='b';page.node('project-select').handlers.change({target:page.node('project-select')});
  assert.equal(page.node('goal-text').value,'Only project B: fix a typo');
});

test('a successful goal save clears only its submitted draft and preserves newer input', async () => {
  const page=browser();let finish;
  page.context.refresh=()=>Promise.resolve();
  page.context.fetch=()=>new Promise(resolve=>{finish=()=>resolve(response({id:'created'}));});
  page.node('goal-text').value='Submitted goal';page.input('goal-text','goal-form');
  const pending=page.submit('goal-form');await new Promise(setImmediate);
  page.node('goal-text').value='New goal typed while saving';page.input('goal-text','goal-form');
  finish();await pending;
  assert.equal(page.node('goal-text').value,'New goal typed while saving');
  assert.equal(vm.runInContext('window.RSIDraftStore.get("goal","a")',page.context),'New goal typed while saving');
});

for (const newerDraft of [null, 'New A draft', 'Submitted goal']) {
  test(`goal acknowledgement away from its project clears only the submitted draft (${newerDraft === null ? 'unchanged' : newerDraft})`, async () => {
    const page = browser(); let finish;
    page.context.refresh = async () => {};
    page.context.fetch = () => new Promise(resolve => { finish = () => resolve(response({id: 'created'})); });
    const switchTo = project => { page.node('project-select').value = project; page.node('project-select').handlers.change(); };
    const input = value => { page.node('goal-text').value = value; page.input('goal-text', 'goal-form'); };
    input('Submitted goal');
    const pending = page.submit('goal-form');
    switchTo('b'); input('B draft');
    if (newerDraft !== null) {
      switchTo('a'); input('Intermediate A edit'); input(newerDraft); switchTo('b');
    }
    finish(); await pending;
    assert.equal(page.node('goal-text').value, 'B draft');
    assert.equal(vm.runInContext('draftStore.get("goal","b")', page.context), 'B draft');
    assert.equal(vm.runInContext('goalDrafts.get("a")', page.context), newerDraft ?? undefined);
    assert.equal(vm.runInContext('draftStore.get("goal","a")', page.context), newerDraft);
    switchTo('a'); assert.equal(page.node('goal-text').value, newerDraft ?? '');
    const reloaded = browser(page.sessionData);
    reloaded.context.restoreGoalDraft();
    assert.equal(reloaded.node('goal-text').value, newerDraft ?? '');
  });
}

test('expired candidates returned by attention pagination remain visible with a retry action', () => {
  const page = browser();
  page.context.language = 'zh-CN';
  page.node('task-filter').value = 'attention';
  page.node('show-system-tasks').checked = false;
  vm.runInContext('state={...initialState,selected_project:"a",tasks:[{id:"expired",project:"a",source:"goal",title:"Expired candidate",state:"expired",display_state:"expired",updated:1}],runs:[],pagination:{total:1,limit:50,offset:0,has_more:false}};renderTasks()', page.context);
  const row = page.node('tasks-list').children[0];
  assert.equal(row.children[0].children[0].textContent, 'Expired candidate');
  assert.equal(row.children[2].children[0].textContent, '重试');
  assert.equal(vm.runInContext('visibleTasks(state.tasks,"history").length', page.context), 0);
  assert.equal(vm.runInContext('visibleTasks(state.tasks,"active").length', page.context), 0);
  assert.equal(page.node('tasks-page').textContent, '1–1 / 1');
});

test('a late settings error cannot mark a different project or a rerendered scope as invalid', async () => {
  for(const change of ['projectId="b"','settingsGeneration++','$("config-scope").value="global"']){
    const page=browser();page.node('config-scope').value='project';
    let fail;page.context.rejectLater=()=>new Promise((_,reject)=>fail=()=>reject(new Error('old scope failure')));
    const pending=vm.runInContext('originalSafe(rejectLater,$("settings-form"))',page.context);
    vm.runInContext(change,page.context);fail();await pending;
    assert.equal(page.node('settings-form').children.length,0);
    assert.match(page.node('toast').textContent,/先前的提交未成功/);
  }
});

test('English UI copy has no accidentally translated Chinese fragments and preserves scheduled-check meaning', () => {
  const i18n=fs.readFileSync(path.join(__dirname,'../adaptive_rsi/web/i18n.js'),'utf8');
  const english=vm.runInNewContext(i18n.slice(0,i18n.indexOf('let language'))+';english');
  for(const [key,value] of Object.entries(english))assert.doesNotMatch(value,/[\u4e00-\u9fff]/,key);
  assert.equal(english['安排检查'],'Schedule check');
  assert.equal(english['Issue 已变化，等待同步后重评'],'Issue changed; waiting for sync and reassessment');
  assert.equal(english['连接超时'],'Connection timed out');
  assert.equal(english['通用'],'General');
  const definitions=[...i18n.matchAll(/^  "((?:[^"\\]|\\.)*)":/gm)].map(match=>match[1]);
  assert.equal(definitions.length,new Set(definitions).size,'english 对象存在重复定义的 key');
});

function experiencePage({navigation = false} = {}) {
  const page = browser(), ready = [], windowEvents = new Map();
  Object.assign(page.context, {
    URL, navigator: {}, history: {replaceState() {}},
    location: {origin: 'http://fixture.test', href: 'http://fixture.test/', pathname: '/', search: ''}
  });
  page.context.window.addEventListener = (type, handler) => windowEvents.set(type, handler);
  page.context.window.scrollTo = () => {};
  const listen = page.document.addEventListener;
  page.document.addEventListener = (type, handler) => {
    if (type === 'DOMContentLoaded') ready.push(handler);
    else listen(type, handler);
  };
  // Load the real task-open/action wiring; all requests and timers stay in memory.
  vm.runInContext(experienceSource, page.context);
  if (navigation) ready[0]();
  page.notify = task => windowEvents.get('rsi-notification-open')({detail: task});
  const create = page.document.createElement;
  page.document.createElement = tag => {
    const node = create(tag);
    if (tag === 'select') {
      let value = '';
      Object.defineProperty(node, 'value', {
        get: () => value,
        set: next => { value = node.children.some(option => option.value === next) ? next : ''; }
      });
    }
    return node;
  };
  page.context.refresh = async () => {};
  return page;
}

const fixtureTask = (id = 'child', state = 'failed') => ({
  id, project: 'a', title: 'Task ' + id, source: 'goal', state, body: '', evidence: '[]'
});

async function openExperienceTask(page, task) {
  await page.context.showTask(task);
  await page.context.lastAction;
  return page.node('detail-body').children.find(node => node.className === 'task-controls');
}

test('runless detail polling updates cancellation controls and preserves supplement drafts', async () => {
  const page = experiencePage(), task = fixtureTask('runless', 'queued');
  let metadata = task, reads = 0;
  page.context.fetch = async url => {
    if (url.includes('/task-runs?')) return response([]);
    if (url.includes('/state?')) return response({tasks: []});
    reads++; return response(metadata);
  };
  const controls = await openExperienceTask(page, task);
  const supplement = controls.children.find(node => node.tagName === 'FORM').children[0].children[0];
  supplement.value = 'Keep this unfinished supplement'; supplement.handlers.input();
  const before = page.node('detail-body').children.slice();
  page.fireTimer(5000); await new Promise(setImmediate);
  assert.deepEqual(page.node('detail-body').children, before, 'unchanged state retains the form nodes');
  assert.equal(supplement.value, 'Keep this unfinished supplement');
  const text = node => [node.textContent, ...(node.children || []).flatMap(text)];
  assert.ok(text(page.node('detail-body')).includes('取消任务'));
  metadata = {...task, state: 'cancelled'};
  page.node('detail-dialog').scrollTop = 42;
  page.fireTimer(5000); await new Promise(setImmediate);
  assert.equal(vm.runInContext('detailTask.state', page.context), 'cancelled');
  assert.ok(text(page.node('detail-body')).includes('基于当前输入重新检查'));
  assert.equal(text(page.node('detail-body')).includes('取消任务'), false);
  const updatedControls = page.node('detail-body').children.find(node => node.className === 'task-controls');
  assert.equal(updatedControls.children.find(node => node.tagName === 'FORM').children[0].children[0].value, supplement.value);
  assert.equal(page.node('detail-dialog').scrollTop, 42);
  assert.equal(page.timers.filter(timer => timer.active && timer.delay === 5000).length, 1, 'one live poll after rebuilding details');
  assert.ok(reads >= 4);
});

for (const dependency of ['draft-parent', '']) {
  test(`runless deferred-to-queued polling preserves queue edits (${dependency || 'remove dependency'})`, async () => {
    const page = experiencePage(), task = fixtureTask('runless', 'deferred'), posts = [], parentReads = [];
    let metadata = {...task, dependency: {dependency: 'saved-parent'}, urgency: dependency ? 0 : 1};
    page.context.fetch = async (url, options) => {
      if (url.endsWith('/task/schedule')) { posts.push(JSON.parse(options.body)); return response({saved: true}); }
      if (url.includes('/task-runs?')) return response([]);
      if (url.includes('/state?')) return response({tasks: metadata.state === 'deferred' ? [fixtureTask('draft-parent')] : []});
      const id = new URL(url, 'http://fixture.test').searchParams.get('id');
      if (id !== task.id) { parentReads.push(id); return response(fixtureTask(id)); }
      return response(metadata);
    };
    const controls = await openExperienceTask(page, task);
    const original = controls.children.filter(node => node.tagName === 'FORM').at(-1);
    original.children[0].children[0].value = dependency;
    original.children[1].children[0].checked = !!dependency;
    metadata = {...metadata, state: 'queued', dependency: {dependency: 'new-server-parent'}};
    page.fireTimer(5000); await new Promise(setImmediate);
    await page.context.lastAction;
    const updated = page.node('detail-body').children.find(node => node.className === 'task-controls');
    const form = updated.children.filter(node => node.tagName === 'FORM').at(-1);
    assert.notEqual(form, original, 'metadata transition rebuilds controls');
    assert.equal(form.children[0].children[0].value, dependency);
    assert.equal(form.children[1].children[0].checked, !!dependency);
    assert.ok(form.children[0].children[0].children.some(option => option.value === 'new-server-parent'), 'current server dependency remains available off-page');
    if (dependency) assert.ok(parentReads.includes(dependency), 'off-page draft dependency is restored as an option');
    form.onsubmit({preventDefault() {}}); await page.context.lastAction;
    assert.deepEqual(posts, [{task: task.id, dependency: dependency || null, urgency: dependency ? 1 : 0}]);
    assert.equal(page.timers.filter(timer => timer.active && timer.delay === 5000).length, 1);
  });
}

for (const editTiming of ['after acknowledgement', 'during save']) {
  test(`queue save rebases drafts while preserving edits ${editTiming}`, async () => {
    const page = experiencePage(), task = fixtureTask('runless', 'deferred'), posts = [];
    let metadata = task, acknowledge;
    page.context.fetch = async (url, options) => {
      if (url.endsWith('/task/schedule')) {
        const submitted = JSON.parse(options.body); posts.push(submitted);
        return new Promise(resolve => { acknowledge = () => {
          metadata = {...metadata, dependency: {dependency: submitted.dependency}, urgency: submitted.urgency};
          resolve(response({saved: true}));
        }; });
      }
      if (url.includes('/task-runs?')) return response([]);
      if (url.includes('/state?')) return response({tasks: [fixtureTask('parent')]});
      return response(metadata);
    };
    const controls = await openExperienceTask(page, task);
    const form = controls.children.filter(node => node.tagName === 'FORM').at(-1);
    const dependency = form.children[0].children[0], urgency = form.children[1].children[0];
    dependency.value = 'parent'; urgency.checked = true;
    form.onsubmit({preventDefault() {}});
    const pending = page.context.lastAction;
    await new Promise(setImmediate);
    if (editTiming === 'during save') { dependency.value = ''; urgency.checked = false; }
    acknowledge(); await pending;
    if (editTiming === 'after acknowledgement') { dependency.value = ''; urgency.checked = false; }
    assert.deepEqual(posts, [{task: task.id, dependency: 'parent', urgency: 1}]);
    assert.equal(dependency.value, '', 'acknowledgement keeps the newer selection');
    assert.equal(urgency.checked, false, 'acknowledgement keeps the newer urgency');
    metadata = {...metadata, state: 'queued'};
    page.fireTimer(5000); await new Promise(setImmediate);
    await page.context.lastAction;
    const updated = page.node('detail-body').children.find(node => node.className === 'task-controls');
    const rebuilt = updated.children.filter(node => node.tagName === 'FORM').at(-1);
    assert.equal(rebuilt.children[0].children[0].value, '', 'returning to the opened value remains an unsaved edit after saving');
    assert.equal(rebuilt.children[1].children[0].checked, false);
  });
}

for (const navigation of ['close', 'project', 'other-task']) {
  test(`a queue save acknowledgement cannot affect ${navigation} navigation`, async () => {
    const page = experiencePage(), task = fixtureTask('runless', 'deferred');
    let acknowledge, refreshes = 0;
    page.context.refresh = async () => { refreshes++; };
    page.context.fetch = async (url, options) => {
      if (url.endsWith('/task/schedule')) return new Promise(resolve => { acknowledge = () => resolve(response({saved: true})); });
      if (url.includes('/task-runs?')) return response([]);
      if (url.includes('/state?')) return response({tasks: [fixtureTask('parent')]});
      return response(url.includes('id=other&') ? fixtureTask('other', 'queued') : task);
    };
    const controls = await openExperienceTask(page, task);
    const form = controls.children.filter(node => node.tagName === 'FORM').at(-1);
    form.children[0].children[0].value = 'parent';
    form.children[1].children[0].checked = true;
    form.onsubmit({preventDefault() {}});
    const pending = page.context.lastAction;
    await new Promise(setImmediate);
    if (navigation === 'close') page.context.closeTaskDetail();
    if (navigation === 'project') { page.context.closeTaskDetail(); vm.runInContext('projectId="b"', page.context); }
    if (navigation === 'other-task') await openExperienceTask(page, fixtureTask('other', 'queued'));
    const before = page.node('detail-body').children.slice(), toast = page.node('toast').textContent;
    acknowledge(); await pending;
    assert.deepEqual(page.node('detail-body').children, before);
    assert.equal(refreshes, 0, 'obsolete save does not refresh the new context');
    assert.equal(page.node('toast').textContent, toast, 'obsolete save does not announce success in the new context');
    if (navigation === 'other-task') {
      const other = page.node('detail-body').children.find(node => node.className === 'task-controls').children.filter(node => node.tagName === 'FORM').at(-1);
      assert.equal(other.children[0].children[0].value, '');
      assert.equal(other.children[1].children[0].checked, false);
    }
  });
}

test('runless metadata rebuilding accepts server queue changes for untouched fields', async () => {
  const page = experiencePage(), task = fixtureTask('runless', 'deferred');
  let metadata = task;
  page.context.fetch = async url => {
    if (url.includes('/task-runs?')) return response([]);
    if (url.includes('/state?')) return response({tasks: []});
    return response(url.includes('id=server-parent&') ? fixtureTask('server-parent') : metadata);
  };
  await openExperienceTask(page, task);
  metadata = {...task, state: 'queued', dependency: {dependency: 'server-parent'}, urgency: 1};
  page.fireTimer(5000); await new Promise(setImmediate);
  await page.context.lastAction;
  const controls = page.node('detail-body').children.find(node => node.className === 'task-controls');
  const form = controls.children.filter(node => node.tagName === 'FORM').at(-1);
  assert.equal(form.children[0].children[0].value, 'server-parent');
  assert.equal(form.children[1].children[0].checked, true);
});

test('runless terminal transition does not restore inapplicable queue controls', async () => {
  const page = experiencePage(), task = fixtureTask('runless', 'deferred');
  let metadata = task, candidateReads = 0;
  page.context.fetch = async url => {
    if (url.includes('/task-runs?')) return response([]);
    if (url.includes('/state?')) { candidateReads++; return response({tasks: [fixtureTask('draft-parent')]}); }
    return response(metadata);
  };
  const controls = await openExperienceTask(page, task);
  const form = controls.children.filter(node => node.tagName === 'FORM').at(-1);
  form.children[0].children[0].value = 'draft-parent';
  form.children[1].children[0].checked = true;
  metadata = {...task, state: 'cancelled'};
  page.fireTimer(5000); await new Promise(setImmediate);
  await page.context.lastAction;
  const updated = page.node('detail-body').children.find(node => node.className === 'task-controls');
  const text = node => [node.textContent, ...(node.children || []).flatMap(text)];
  assert.equal(vm.runInContext('detailTask.state', page.context), 'cancelled');
  assert.equal(text(updated).includes('保存排队设置'), false);
  assert.ok(text(updated).includes('基于当前输入重新检查'));
  assert.equal(candidateReads, 1, 'terminal controls do not load queue candidates');
});

for (const navigation of ['close', 'project', 'other-task']) {
  test(`a runless detail poll cannot overwrite ${navigation} navigation`, async () => {
    const page = experiencePage(), task = fixtureTask('runless', 'queued');
    let polling = false, release;
    page.context.fetch = async url => {
      if (url.includes('/task-runs?')) return response([]);
      if (url.includes('/state?')) return response({tasks: []});
      if (polling && url.includes('id=runless&')) return new Promise(resolve => { release = () => resolve(response({...task, state: 'cancelled'})); });
      return response(url.includes('id=other&') ? fixtureTask('other') : task);
    };
    await openExperienceTask(page, task);
    polling = true; page.fireTimer(5000); await new Promise(setImmediate);
    if (navigation === 'close') page.node('detail-dialog').close();
    if (navigation === 'project') vm.runInContext('projectId="b"', page.context);
    if (navigation === 'other-task') await openExperienceTask(page, fixtureTask('other'));
    const before = page.node('detail-body').children.slice();
    release(); await new Promise(setImmediate);
    assert.deepEqual(page.node('detail-body').children, before);
    assert.equal(page.node('detail-dialog').open, navigation !== 'close');
    assert.equal(vm.runInContext('detailTask?.id', page.context), navigation === 'close' ? undefined : navigation === 'other-task' ? 'other' : 'runless');
  });
}

for (const entry of ['notification', 'deep-link']) {
  for (const stage of ['refresh', 'metadata']) {
    for (const navigation of ['project', 'round-trip', 'other-task', 'view', 'new-notification', 'unchanged']) {
      test(`${entry} loading during ${stage} preserves ${navigation} navigation`, async () => {
        const page = experiencePage({navigation: true}), task = fixtureTask('old');
        page.context.window.RSIDesktop = {};
        let release, refreshCalls = 0, metadataCalls = 0;
        const refreshGate = stage === 'refresh' ? new Promise(resolve => { release = resolve; }) : Promise.resolve();
        page.context.refresh = () => ++refreshCalls <= 2 ? refreshGate : Promise.resolve();
        page.context.fetch = async url => {
          if (url.includes('/task-runs?')) return response([]);
          if (url.includes('/task?id=old&') && ++metadataCalls === 1 && stage === 'metadata') {
            return new Promise(resolve => { release = () => resolve(response(task)); });
          }
          return response(url.includes('id=other&') ? fixtureTask('other') : task);
        };
        if (entry === 'notification') page.notify({id: task.id, task: task.id, project: task.project});
        else {
          page.context.location.hash = '#project=a&task=old';
          page.context.window.RSIExperience.refreshed();
        }
        const pending = page.context.lastAction;
        await new Promise(setImmediate);
        assert.equal(typeof release, 'function');
        const switchTo = project => { page.node('project-select').value = project; page.node('project-select').handlers.change(); };
        if (navigation === 'project' || navigation === 'round-trip') {
          switchTo('b');
          if (navigation === 'round-trip') switchTo('a');
        } else if (navigation === 'other-task') await openExperienceTask(page, fixtureTask('other'));
        else if (navigation === 'view') page.context.showView('settings');
        else if (navigation === 'new-notification') {
          page.notify({task: 'other', project: 'a'});
          await page.context.lastAction;
        }
        release(); await pending; await new Promise(setImmediate);
        const hasTask = ['other-task', 'new-notification', 'unchanged'].includes(navigation);
        assert.equal(page.node('detail-dialog').open || false, hasTask);
        if (hasTask) assert.equal(page.node('detail-title').textContent, navigation === 'unchanged' ? 'Task old' : 'Task other');
        assert.equal(vm.runInContext('projectId', page.context), navigation === 'project' ? 'b' : 'a');
        if (stage === 'refresh' && navigation !== 'unchanged') assert.equal(metadataCalls, 0, 'obsolete navigation must stop before requesting old metadata');
      });
    }
  }
}

test('an urgency-only save preserves an off-page dependency; explicit removal still clears it', async () => {
  const page = experiencePage(), task = fixtureTask('child', 'queued'), posts = [];
  page.context.fetch = async (url, options) => {
    if (url.endsWith('/task/schedule')) {
      posts.push(JSON.parse(options.body));
      return response({saved: true});
    }
    if (url.includes('/task-runs?')) return response([]);
    if (url.includes('/state?')) return response({
      tasks: Array.from({length: 200}, (_, i) => fixtureTask('visible-' + i, 'queued')),
      pagination: {has_more: true}
    });
    if (url.includes('id=outside-page&')) return response(fixtureTask('outside-page'));
    return response({...task, dependency: {dependency: 'outside-page'}, urgency: 0});
  };
  const controls = await openExperienceTask(page, task);
  const form = controls.children.filter(node => node.tagName === 'FORM').at(-1);
  const select = form.children[0].children[0], urgency = form.children[1].children[0];
  assert.equal(select.value, 'outside-page');
  assert.equal(select.children.find(option => option.value === 'outside-page').textContent, 'Task outside-page');
  urgency.checked = true;
  form.onsubmit({preventDefault() {}}); await page.context.lastAction;
  assert.deepEqual(posts[0], {task: 'child', dependency: 'outside-page', urgency: 1});
  select.value = '';
  form.onsubmit({preventDefault() {}}); await page.context.lastAction;
  assert.deepEqual(posts[1], {task: 'child', dependency: null, urgency: 1});
});

for (const actionKind of ['retry', 'supplement']) {
  for (const navigation of ['close', 'project', 'other-task', 'round-trip']) {
    for (const delayedStage of ['mutation', 'metadata']) {
      test(`${actionKind} completion cannot reopen old details after ${navigation} during ${delayedStage}`, async () => {
        const page = experiencePage(), task = fixtureTask(), posts = [];
        let release, actionStarted = false;
        page.context.fetch = async (url, options) => {
          if (options?.method === 'POST') {
            actionStarted = true; posts.push(JSON.parse(options.body));
            if (delayedStage === 'mutation') return new Promise(resolve => { release = () => resolve(response({saved: true})); });
            return response({saved: true});
          }
          if (url.includes('/task-runs?')) return response([]);
          if (actionStarted && url.includes('id=child&') && delayedStage === 'metadata') {
            return new Promise(resolve => { release = () => resolve(response(task)); });
          }
          return response(url.includes('id=other&') ? fixtureTask('other') : task);
        };
        const controls = await openExperienceTask(page, task);
        if (actionKind === 'retry') controls.children.find(node => node.textContent === '基于当前输入重新检查').handlers.click();
        else {
          const form = controls.children.find(node => node.tagName === 'FORM');
          form.children[0].children[0].value = 'More requirements';
          form.onsubmit({preventDefault() {}});
        }
        const pending = page.context.lastAction;
        await new Promise(setImmediate);
        assert.equal(typeof release, 'function');
        if (navigation === 'other-task') await openExperienceTask(page, fixtureTask('other'));
        else {
          page.context.closeTaskDetail();
          if (navigation !== 'close') vm.runInContext('projectId="b"', page.context);
          if (navigation === 'round-trip') vm.runInContext('projectId="a"', page.context);
        }
        release(); await pending;
        assert.equal(posts.length, 1);
        assert.equal(page.node('detail-dialog').open, navigation === 'other-task');
        if (navigation === 'other-task') assert.equal(page.node('detail-title').textContent, 'Task other');
      });
    }
  }
  test(`${actionKind} completion refreshes details when the original task is still open`, async () => {
    const page = experiencePage(), task = fixtureTask();
    let revised = false;
    page.context.fetch = async (url, options) => {
      if (options?.method === 'POST') { revised = true; return response({saved: true}); }
      if (url.includes('/task-runs?')) return response([]);
      return response({...task, title: revised ? 'Updated task' : task.title});
    };
    const controls = await openExperienceTask(page, task);
    if (actionKind === 'retry') controls.children.find(node => node.textContent === '基于当前输入重新检查').handlers.click();
    else {
      const form = controls.children.find(node => node.tagName === 'FORM');
      form.children[0].children[0].value = 'More requirements';
      form.onsubmit({preventDefault() {}});
    }
    await page.context.lastAction;
    assert.equal(page.node('detail-dialog').open, true);
    assert.equal(page.node('detail-title').textContent, 'Updated task');
  });
}

for (const draftWeight of [1, 3]) {
  test(`settings acknowledgement rebases after A-B-A and preserves newer weight ${draftWeight}`, async () => {
    const page = browser(), field = page.node('weight'), posts = [];
    Object.assign(field, {type: 'number', dataset: {path: 'schedule.weight'}});
    page.document.querySelectorAll = selector => selector === '[data-path]' ? [field] : [];
    page.node('settings-form').querySelectorAll = selector => ['input,textarea,select', '[data-path]'].includes(selector) ? [field] : [];
    page.node('config-scope').value = 'project'; page.node('schedule-mode').value = 'inherit';
    page.node('work-start').value = '0'; page.node('work-end').value = '24'; page.node('checks-json').value = '[]';
    const config = {schedule: {weight: 1, working_hours: [0, 24]}, github: {labels: []}, checks: []};
    page.context.initialState.projects.forEach(project => { project.config = structuredClone(config); });
    // Minimal renderer retains the production draft restore and project-switch handlers.
    vm.runInContext(`view="settings";render=()=>{if(!settingsBaseline){
      settingsBaseline=structuredClone(currentProject().config);settingsContext=settingsKey();
      settingsModes.set(settingsBaseline,"inherit");$("weight").value=String(settingsBaseline.schedule.weight);
      settingsClean.set(settingsContext,JSON.stringify(settingsValues()));restoreSettingsDraft();
    }};renderSettings=()=>{settingsBaseline=null;render();};render();`, page.context);
    let release;
    page.context.refresh = async () => {};
    page.context.fetch = async (_url, options) => {
      const post = JSON.parse(options.body); posts.push(post);
      if (posts.length === 1) return new Promise(resolve => { release = () => resolve(response({config: {...config, schedule: {...config.schedule, weight: 2}}})); });
      return response({config: {...config, schedule: {...config.schedule, weight: draftWeight}}});
    };
    field.value = '2'; page.input('weight', 'settings-form');
    const pending = page.submit('settings-form');
    page.node('project-select').value = 'b'; page.node('project-select').handlers.change();
    field.value = '4'; page.input('weight', 'settings-form');
    page.node('project-select').value = 'a'; page.node('project-select').handlers.change();
    field.value = String(draftWeight); page.input('weight', 'settings-form');
    release(); await pending;
    assert.equal(field.value, String(draftWeight));
    assert.equal(vm.runInContext('settingsBaseline.schedule.weight', page.context), 2);
    assert.equal(vm.runInContext('draftStore.get("settings","a").values.find(v=>v.path==="schedule.weight").baselineValue', page.context), 2);
    assert.equal(vm.runInContext('settingsDrafts.get("b").values[0].value', page.context), '4');
    await page.submit('settings-form');
    assert.deepEqual(posts[1], {project: 'a', values: {schedule: {weight: draftWeight}}, inherit: ['schedule.light_minutes','schedule.issues_minutes','sources.inspection','sources.issues']});
  });
}

test('an ordinary settings save accepts canonical response values and finishes visibly saved', async () => {
  const page = experiencePage(), field = page.node('weight');
  Object.assign(field, {type: 'number', value: '2.0', dataset: {path: 'schedule.weight'}});
  page.context.language = 'zh-CN';
  page.node('config-scope').value = 'project'; page.node('schedule-mode').value = 'inherit';
  page.node('work-start').value = '0'; page.node('work-end').value = '24'; page.node('checks-json').value = '[]';
  page.document.querySelectorAll = selector => selector === '[data-path]' ? [field] : [];
  page.node('settings-form').querySelectorAll = selector => ['input,textarea,select', '[data-path]'].includes(selector) ? [field] : [];
  vm.runInContext('view="settings";settingsContext="a";settingsBaseline={schedule:{weight:1,working_hours:[0,24]},github:{labels:[]},checks:[]}', page.context);
  let rerenders = 0;
  page.context.renderSettings = () => {
    rerenders++;
    field.value = '2';
    vm.runInContext('settingsClean.set("a",JSON.stringify(settingsValues()))', page.context);
  };
  page.context.fetch = async () => response({saved: true, config: {schedule: {weight: 2}}});
  await page.submit('settings-form');
  assert.equal(rerenders, 1, 'unchanged submitted input must accept the canonical response');
  assert.equal(page.node('save-note').dataset.state, 'saved');
  assert.match(page.node('save-note').textContent, /设置已保存/);
});

test('an active run stopping error explains when retry and supplement become available', async () => {
  const page = browser();
  page.context.fetch = async () => response({error: 'Wait for the active run to finish stopping before retrying or supplementing this task'}, 400);
  await assert.rejects(page.context.api('retry', {task: 'child'}), /任务仍在停止中，请等当前运行结束后再重试或补充需求。/);
});

for(const fails of [false,true])test(`catalog callback ${fails?'failure':'success'} preserves feedback after provider switch`,async()=>{
  const page=browser();let release,callback;
  const modelSource=fs.readFileSync(path.join(__dirname,'../adaptive_rsi/web/model-center.js'),'utf8');
  // Expose the production closure with only rendering/load stubbed for this focused ownership test.
  const instrumented=modelSource.replace('  init();','').replace('  return {show:(requested=null,reload=false)=>{',
    '  return {testSetup(){renderProvider=()=>{};load=async()=>{};active="openai";scope="a";return ()=>fetchProvider("openai","a",true);},testSwitch(){active="opencode";setStatus("Provider B feedback");},show:(requested=null,reload=false)=>{');
  page.context.english={};vm.runInContext(instrumented,page.context);
  page.context.fetch=async()=>new Promise(resolve=>{release=()=>resolve(response({},fails?503:200));});
  callback=page.context.window.RSIModelCenter.testSetup();
  const result=callback();page.context.window.RSIModelCenter.testSwitch();release();
  if(fails)await assert.rejects(result);else await result;
  assert.equal(page.node('mc-feedback').textContent,'Provider B feedback');
});

async function authCompletionPage({failure='',direct=false,hold=''}={}) {
  const page=browser(),calls={status:0,session:0,catalog:0,start:0,complete:0},walk=node=>[node,...(node.children||[]).flatMap(walk)];
  const tick=()=>new Promise(setImmediate),session={id:'completion-A',backend:'codex',provider:null,state:'awaiting_authorization',method:direct?'code':'auto'};
  let release,held=false;
  const waitHeld=async stage=>{if(hold===stage&&!held){held=true;await new Promise(resolve=>{release=resolve;});}};
  page.context.fetch=async(url,options={})=>{
    if(url.endsWith('/api/auth/status')){
      calls.status++;if(calls.status>1){await waitHeld('status');if(calls.status===2&&failure==='status')return response({},503);if(failure==='session_required')return response({},401);}
      return response({sessions:calls.status===1?[session]:[],codex:{available:true,state:calls.status===1?'signed_out':'authenticated'}});
    }
    if(url.includes('/api/auth/session?id=')){calls.session++;if(url.includes('completion-B'))return response({...session,id:'completion-B',state:'starting'});return response({...session,state:calls.session===1?'awaiting_authorization':'completed'});}
    if(url.endsWith('/api/auth/complete')){calls.complete++;return response({...session,state:'completed'});}
    if(url.endsWith('/api/auth/cancel'))return response({...session,state:'cancelled'});
    if(url.endsWith('/api/auth/start')){calls.start++;return response({...session,id:'completion-B',state:'starting'});}
    if(url.endsWith('/api/auth/logout'))return response({ok:true});
    throw new Error('Unexpected request '+url);
  };
  vm.runInContext(authSource,page.context);
  page.context.window.RSIAuth.show('openai',async()=>{
    calls.catalog++;await waitHeld('catalog');
    if(calls.catalog===1&&failure==='catalog')throw Object.assign(new Error('Catalog unavailable'),{status:503});
  });
  await tick();await tick();
  return {page,calls,tick,walk,release:()=>release(),text:()=>walk(page.node('mc-auth')).map(node=>node.textContent||'').join(' '),
    async finish(){
      if(direct){const form=walk(page.node('mc-auth')).find(node=>node.tagName==='FORM');form.children[0].children[0].value='TEST-CODE';form.handlers.submit({preventDefault(){}});}
      else page.fireTimer(1500);
      await tick();await tick();
    }};
}

for(const direct of [false,true])for(const failure of ['status','catalog']){
  test(`completed auth retries ${failure} refresh after ${direct?'code submission':'poll'} without repeating OAuth`,async()=>{
    const run=await authCompletionPage({failure,direct});await run.finish();
    assert.match(run.text(),/更新失败，正在重试/);
    assert.equal(run.calls.catalog,failure==='status'?0:1);
    run.page.fireTimer(1500);await run.tick();await run.tick();
    assert.doesNotMatch(run.text(),/更新失败/);
    assert.equal(run.calls.status,failure==='status'?3:2);
    assert.equal(run.calls.catalog,failure==='status'?1:2);
    assert.equal(run.calls.session,direct?1:2);
    assert.equal(run.calls.complete,direct?1:0);
    assert.equal(run.calls.start,0);
    assert.equal(run.page.timers.filter(timer=>timer.active&&timer.delay===1500).length,0);
  });
}

test('completed auth console expiry stays actionable and stops refresh retries',async()=>{
  const run=await authCompletionPage({failure:'session_required'});await run.finish();
  assert.match(run.text(),/控制台登录已失效/);assert.equal(run.calls.catalog,0);
  assert.equal(run.page.timers.filter(timer=>timer.active&&timer.delay===1500).length,0);
});

for(const hold of ['status','catalog'])for(const restart of [false,true]){
  test(`completion ${hold} pending cannot revive auth after ${restart?'restart':'cancel or logout'}`,async()=>{
    const run=await authCompletionPage({hold,failure:hold==='status'?'session_required':'catalog'});await run.finish();
    const button=run.walk(run.page.node('mc-auth')).find(node=>node.textContent===(hold==='status'?'取消登录':'退出账号'));
    assert.ok(button);button.handlers.click();await run.page.context.lastAction;
    if(restart){
      run.walk(run.page.node('mc-auth')).find(node=>node.tagName==='FORM').handlers.submit({preventDefault(){}});await run.page.context.lastAction;
    }
    run.release();await run.tick();await run.tick();
    assert.equal(run.calls.catalog,hold==='status'?0:1);
    assert.doesNotMatch(run.text(),/更新失败/);
    if(restart)assert.match(run.text(),/正在准备官方登录/);
    else assert.equal(run.page.timers.filter(timer=>timer.active&&timer.delay===1500).length,0);
  });
}

test('pending completion catalog refresh cannot invoke duplicate callbacks',async()=>{
  const run=await authCompletionPage({hold:'catalog'});await run.finish();
  run.page.fireTimer(1500);await run.tick();assert.equal(run.calls.catalog,1);
  run.release();await run.tick();await run.tick();
  assert.equal(run.calls.catalog,1);
  assert.equal(run.page.timers.filter(timer=>timer.active&&timer.delay===1500).length,0);
});

test('the live run stage marks an in-progress review before its stage row exists', () => {
  const page = browser();
  assert.equal(vm.runInContext('runningStage({state:"evaluating",stage:"review"})', page.context), '正在评审改动');
  assert.equal(vm.runInContext('runningStage({state:"evaluating",last_stage:"review"})', page.context), '正在评审改动');
});

for (const restart of [false, true]) {
  for (const reply of ['awaiting_authorization', 'completed', 'unknown_session', 'temporary_failure', 'session_required']) {
    test(`stale auth poll ${reply} cannot replace ${restart ? 'a restarted' : 'a cancelled'} login`, async () => {
      const page = browser(), requests = [];
      const walk = node => [node, ...(node.children || []).flatMap(walk)];
      const visibleText = () => walk(page.node('mc-auth')).map(node => node.textContent || '').join(' ');
      const tick = () => new Promise(setImmediate);
      const old = {id: 'old-session', backend: 'codex', provider: null, state: 'awaiting_authorization', method: 'auto', user_code: 'OLD-TEST-CODE'};
      const replacement = {...old, id: 'replacement-session', state: 'starting', user_code: undefined};
      let oldReads = 0, releaseOld, refreshes = 0;
      page.context.fetch = async (url, options = {}) => {
        requests.push({url, body: options.body ? JSON.parse(options.body) : null});
        if (url.endsWith('/api/auth/status')) return response({sessions: [old], codex: {available: true, state: 'signed_out'}});
        if (url.includes('/api/auth/session?id=old-session')) {
          if (++oldReads === 1) return response(old);
          return new Promise(resolve => {
            releaseOld = () => resolve(
              reply === 'unknown_session' ? response({error_code: reply}, 404) :
              reply === 'temporary_failure' ? response({error: 'temporary'}, 503) :
              reply === 'session_required' ? response({}, 401) : response({...old, state: reply}));
          });
        }
        if (url.includes('/api/auth/session?id=replacement-session')) return response({...replacement, state: 'awaiting_authorization', user_code: 'NEW-TEST-CODE'});
        if (url.endsWith('/api/auth/cancel')) return response({...old, state: 'cancelled', user_code: undefined});
        if (url.endsWith('/api/auth/start')) return response(replacement);
        throw new Error('Unexpected request ' + url);
      };
      vm.runInContext(authSource, page.context);
      page.context.window.RSIAuth.show('openai', async () => { refreshes++; });
      await tick(); await tick();
      page.fireTimer(1500); await tick();
      assert.equal(typeof releaseOld, 'function', 'the old session poll must be pending');
      walk(page.node('mc-auth')).find(node => node.textContent === '取消登录').handlers.click();
      await page.context.lastAction;
      assert.match(visibleText(), /已取消登录/);
      if (restart) {
        walk(page.node('mc-auth')).find(node => node.tagName === 'FORM').handlers.submit({preventDefault() {}});
        await page.context.lastAction;
        assert.equal(requests.filter(request => request.url.endsWith('/api/auth/start')).length, 1);
      }
      releaseOld(); await tick(); await tick();
      assert.doesNotMatch(visibleText(), /OLD-TEST-CODE|控制台登录已失效|登录已中断|授权状态已保留/);
      assert.equal(refreshes, 0, 'discarded completion must not refresh the model catalog');
      if (restart) {
        assert.match(visibleText(), /正在准备官方登录/);
        page.fireTimer(1500); await tick(); await tick();
        assert.match(visibleText(), /NEW-TEST-CODE/);
        assert.equal(requests.filter(request => request.url.includes('session?id=replacement-session')).length, 1);
      } else {
        assert.match(visibleText(), /已取消登录/);
        assert.equal(page.timers.filter(timer => timer.active && timer.delay === 1500).length, 0);
      }
    });
  }
}
