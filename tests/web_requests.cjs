const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../adaptive_rsi/web/app.js'), 'utf8');
const authSource = fs.readFileSync(path.join(__dirname, '../adaptive_rsi/web/auth.js'), 'utf8');
const experienceSource = fs.readFileSync(path.join(__dirname, '../adaptive_rsi/web/experience.js'), 'utf8');
const usageSource = fs.readFileSync(path.join(__dirname, '../adaptive_rsi/web/usage.js'), 'utf8');
const response = (data, status = 200) => ({ok: status < 400, status, json: async () => data});
const rejected = () => response({error_code: 'update_in_progress'}, 503);
async function waitFor(predicate) {
  const deadline = Date.now() + 5000;
  while (!predicate()) {
    assert.ok(Date.now() < deadline, 'asynchronous request reached its expected checkpoint');
    await new Promise(setImmediate);
  }
}
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
    AbortController, DOMException, queueMicrotask, TextEncoder, crypto: require('node:crypto').webcrypto,
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
  vm.runInContext('originalSafe=safe; originalRender=render; state=initialState; scopeInitialized=true; projectId="a"; render=()=>{}; safe=fn=>{lastAction=fn();return lastAction;}', context);
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

function settingsSavePage(scope = 'project') {
  const page = browser(), field = page.node('weight'), posts = [];
  Object.assign(field, {type: 'number', dataset: {path: 'schedule.weight'}, checked: false});
  const fields = [field, ...['schedule-mode', 'work-start', 'work-end', 'issue-labels', 'checks-json', 'advanced-json'].map(page.node)];
  page.document.querySelectorAll = selector => selector === '[data-path]' ? [field] : [];
  page.node('settings-form').querySelectorAll = selector => selector === 'input,textarea,select' ? fields : selector === '[data-path]' ? [field] : [];
  page.node('config-scope').value = scope;
  const config = {schedule: {weight: 1, working_hours: [0, 24]}, github: {labels: []}, checks: []};
  page.context.initialState.config = structuredClone(config);
  page.context.initialState.projects.forEach(project => { project.config = structuredClone(config); });
  // Only rendering and unrelated refreshes are isolated; app.js owns submit, input,
  // scope/project navigation, baseline acknowledgement and draft persistence/restoration.
  vm.runInContext(`view='settings';renderNavigation=()=>{};renderSettings=()=>{
    settingsBaseline=structuredClone(settingsKey()==='global'?state.config:currentProject().config);
    settingsContext=settingsKey();settingsModes.set(settingsBaseline,'custom');
    $('weight').value=String(settingsBaseline.schedule.weight);$('schedule-mode').value='custom';
    $('work-start').value='0';$('work-end').value='24';$('checks-json').value='[]';
    settingsClean.set(settingsContext,JSON.stringify(settingsValues()));restoreSettingsDraft();
  };render=()=>{if(!settingsBaseline)renderSettings();};render();`, page.context);
  page.context.refresh = async () => {};
  let resolveSave, rejectSave;
  page.context.fetch = async (_url, options) => {
    const post = JSON.parse(options.body); posts.push(post);
    return new Promise((resolve, reject) => { resolveSave = resolve; rejectSave = reject; });
  };
  return Object.assign(page, {
    posts, field,
    edit(value) { field.value = String(value); page.input('weight', 'settings-form'); page.node('settings-form').handlers.input(); },
    project(id) { page.node('project-select').value = id; page.node('project-select').handlers.change(); },
    scope(value) { page.context.changeSettingsScope(value); },
    finish(outcome = 'success') {
      if (outcome === 'network') { rejectSave(new TypeError('connection lost')); return; }
      if (outcome === 'failure') { resolveSave(response({error: 'Project not found'}, 404)); return; }
      if (outcome === 'timeout') { resolveSave(rejected()); return; }
      const post = posts.at(-1), target = post.project === null ? page.context.initialState : page.context.initialState.projects.find(project => project.id === post.project);
      target.config.schedule.weight = post.values.schedule?.weight ?? target.config.schedule.weight;
      resolveSave(response({saved: true, config: structuredClone(target.config)}));
    },
    storedWeight(key) { return vm.runInContext(`draftStore.get('settings',${JSON.stringify(key)})?.values.find(entry=>entry.path==='schedule.weight')`, page.context); }
  });
}

function productionSettingsPage(scope) {
  const page = browser(), fields = [];
  const html = fs.readFileSync(path.join(__dirname, '../adaptive_rsi/web/index.html'), 'utf8').split('<form id="settings-form">')[1].split('</form>')[0];
  for (const match of html.matchAll(/<(input|select|textarea)\b([^>]*)>/g)) {
    const attrs = Object.fromEntries([...match[2].matchAll(/([\w-]+)="([^"]*)"/g)].map(item => [item[1], item[2]]));
    const node = page.node(attrs.id || 'control-' + fields.length);
    Object.assign(node, {id: attrs.id || '', type: attrs.type || match[1], tagName: match[1].toUpperCase(), checked: false, form: page.node('settings-form')});
    for (const [key, value] of Object.entries(attrs)) if (key.startsWith('data-')) node.dataset[key.slice(5).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase())] = value;
    node.hasAttribute = key => Object.hasOwn(attrs, key);
    let value = ''; Object.defineProperty(node, 'value', {get: () => value, set: next => { value = String(next); }});
    const content = html.slice(match.index + match[0].length).split('</' + match[1] + '>')[0];
    node.options = match[1] === 'select' ? [...content.matchAll(/<option value="([^"]*)"/g)].map(item => ({value: item[1]})) : [];
    fields.push(node);
  }
  const select = selector => {
    if (selector === 'input,textarea,select') return fields;
    if (selector === '[data-path]') return fields.filter(node => node.dataset.path);
    if (selector === '[data-unit-for]') return fields.filter(node => node.dataset.unitFor);
    if (selector === '[data-schedule-preset]') return fields.filter(node => node.dataset.schedulePreset);
    if (selector === '[data-unit-for], [data-schedule-preset]') return fields.filter(node => node.dataset.unitFor || node.dataset.schedulePreset);
    const exact = selector.match(/^\[data-(path|unit-for)="([^"]+)"\]$/);
    if (exact) return fields.filter(node => node.dataset[exact[1] === 'path' ? 'path' : 'unitFor'] === exact[2]);
    const prefix = selector.match(/^\[data-path\^="([^"]+)"\]$/);
    return prefix ? fields.filter(node => node.dataset.path?.startsWith(prefix[1])) : [];
  };
  page.document.querySelectorAll = select; page.document.querySelector = selector => select(selector)[0] || null;
  page.node('settings-form').querySelectorAll = select;
  const prior = page.node('settings-form').querySelector.bind(page.node('settings-form'));
  page.node('settings-form').querySelector = selector => select(selector)[0] || prior(selector);
  const config = JSON.parse(require('node:child_process').execFileSync(process.env.PYTHON || 'python3', ['-B', '-c', 'import json; from adaptive_rsi.config import DEFAULTS; print(json.dumps(DEFAULTS))'], {cwd: path.join(__dirname, '..'), encoding: 'utf8'}));
  page.context.initialState.config = structuredClone(config);
  page.context.initialState.projects.forEach(project => { project.config = structuredClone(config); project.overrides = {}; });
  Object.assign(page.context, {URL, navigator: {}, history: {replaceState() {}}, language: 'zh-CN'});
  page.context.window.addEventListener = () => {}; page.context.window.scrollTo = () => {};
  vm.runInContext(experienceSource, page.context);
  // Keep production renderSettings, refresh, draft restoration and dirty status.
  // Isolate only unrelated panels and their background requests.
  vm.runInContext(`view='settings';renderCapabilities=async()=>{};loadReleaseWorkflows=async()=>{};
    renderUpdateExtras=()=>{};renderNavigation=()=>{};window.RSIExperience.refreshed=()=>{};
    render=()=>{if(!settingsBaseline)renderSettings();};`, page.context);
  page.node('config-scope').value = scope; page.context.renderSettings();
  const server = structuredClone(page.context.initialState), posts = [];
  let acknowledge;
  page.context.fetch = async (url, options) => {
    if (url.includes('/state?')) return response({...structuredClone(server), selected_project: new URLSearchParams(url.split('?')[1]).get('project')});
    const post = JSON.parse(options.body); posts.push(post);
    const target = post.project === null ? server : server.projects.find(project => project.id === post.project);
    page.context.deepMerge(target.config, post.values);
    if (post.project !== null) page.context.deepMerge(target.overrides, post.values);
    const saved = structuredClone(target.config);
    return new Promise(resolve => { acknowledge = () => resolve(response({saved: true, config: saved})); });
  };
  const weight = select('[data-path="schedule.weight"]')[0];
  return Object.assign(page, {server, posts, weight,
    edit(selector, value) {
      const node = select(selector)[0]; node.value = value;
      page.input(node.id || 'control-' + fields.indexOf(node), 'settings-form');
      page.node('settings-form').handlers.input(); page.context.window.RSIProduct.dirtyStatus();
    },
    async navigate(back) {
      if (scope === 'project') { page.node('project-select').value = back ? 'a' : 'b'; page.node('project-select').handlers.change(); }
      else page.context.changeSettingsScope(back ? 'global' : 'project');
      await page.context.refresh();
    },
    finish() { acknowledge(); }
  });
}

for (const scope of ['project', 'global']) {
  for (const external of ['none', 'unrelated-field', 'same-field']) {
    test(`production settings restore after GET sees pending ${scope} save with ${external} conflict`, async () => {
      const page = productionSettingsPage(scope), key = scope === 'project' ? 'a' : 'global';
      const target = scope === 'project' ? page.server.projects[0] : page.server;
      page.edit('[data-path="schedule.weight"]', '2'); const pending = page.submit('settings-form');
      page.edit('[data-path="schedule.weight"]', '1');
      if (external === 'unrelated-field') page.edit('[data-path="limits.retries"]', '7');
      if (external === 'same-field') target.config.schedule.weight = 6;
      if (external === 'unrelated-field') target.config.limits.retries = 8;
      assert.equal(target.config.schedule.weight, external === 'same-field' ? 6 : 2, 'server commits before POST acknowledgement');
      await page.navigate(false); await page.navigate(true);
      assert.equal(vm.runInContext('settingsBaseline.schedule.weight', page.context), external === 'same-field' ? 6 : 2, 'navigation GET observes the committed baseline');
      assert.equal(page.weight.value, external === 'same-field' ? '6' : '1', 'only a baseline change belonging to this save preserves the newer draft');
      if (external === 'unrelated-field') assert.equal(page.document.querySelector('[data-path="limits.retries"]').value, '8', 'unrelated external conflicts retain server values');
      if (external !== 'none') assert.match(page.node('toast').textContent, /冲突字段/);
      page.finish(); await pending;
      if (external === 'same-field') return;
      assert.equal(page.weight.value, '1');
      assert.equal(page.node('save-note').dataset.state, 'dirty');
      const stored = vm.runInContext(`draftStore.get('settings',${JSON.stringify(key)})`, page.context);
      assert.equal(stored.values.find(entry => entry.path === 'schedule.weight').baselineValue, 2);
      assert.equal(stored.values.find(entry => entry.path === 'schedule.weight').value, '1');
    });
  }
}

for (const scope of ['project', 'global']) {
  for (const navigation of ['same', 'project-away', 'project-round-trip', 'scope-away', 'scope-round-trip']) {
    for (const newer of [1, 2, 3]) {
      test(`pending settings save preserves ${scope} weight ${newer} after ${navigation}`, async () => {
        const page = settingsSavePage(scope), key = scope === 'project' ? 'a' : 'global';
        page.edit(2); const pending = page.submit('settings-form');
        page.edit(newer);
        const leave = () => navigation.startsWith('project-') ? page.project('b') : page.scope(scope === 'project' ? 'global' : 'project');
        const back = () => navigation.startsWith('project-') ? page.project('a') : page.scope(scope);
        if (navigation !== 'same') {
          leave();
          // Project navigation shares the global scope; all other destinations own a separate draft.
          if (scope !== 'global' || !navigation.startsWith('project-')) page.edit(4);
          if (navigation.endsWith('round-trip')) back();
        }
        const visibleBefore = page.field.value;
        page.finish(); await pending;
        if (navigation.endsWith('away')) {
          assert.equal(page.field.value, visibleBefore, 'an acknowledgement cannot overwrite the visible scope'); back();
        }
        assert.equal(page.field.value, String(newer));
        assert.equal(vm.runInContext('settingsBaseline.schedule.weight', page.context), 2);
        assert.equal(vm.runInContext('pendingSettingsSaves.size', page.context), 0);
        if (newer === 2) {
          assert.equal(vm.runInContext(`settingsDrafts.has(${JSON.stringify(key)})`, page.context), false);
          assert.equal(page.storedWeight(key), undefined, 'equal acknowledged input is no longer dirty');
        } else {
          assert.equal(page.storedWeight(key).value, String(newer));
          assert.equal(page.storedWeight(key).baselineValue, 2);
        }
        const savedAgain = page.submit('settings-form'); page.finish(); await savedAgain;
        assert.deepEqual(page.posts[1], {project: scope === 'project' ? 'a' : null, values: newer === 2 ? {} : {schedule: {weight: newer}}});
        assert.equal(page.storedWeight(key), undefined, 'a subsequent save clears the draft');
        // Undoing to the new baseline must also clear a fresh draft once pending ownership is gone.
        page.edit(5); page.edit(newer);
        assert.equal(vm.runInContext(`settingsDrafts.has(${JSON.stringify(key)})`, page.context), false);
        assert.equal(page.storedWeight(key), undefined);
        if (navigation !== 'same' && (scope !== 'global' || !navigation.startsWith('project-'))) {
          leave(); assert.equal(page.field.value, '4', 'the other scope retains its independent draft');
        }
      });
    }
  }
}

for (const outcome of ['failure', 'network', 'timeout']) {
  for (const navigation of ['same', 'project-away', 'project-round-trip', 'scope-away']) {
    for (const newer of [1, 3]) {
      test(`pending settings save ${outcome} cleans against the old baseline after ${navigation} with weight ${newer}`, async () => {
        const page = settingsSavePage(); page.edit(2); const pending = page.submit('settings-form');
        // Attach rejection handling before resolving the mock transport.
        const failed = assert.rejects(pending, outcome === 'failure' ? /项目已不存在/ : outcome === 'network' ? /无法连接本地服务/ : /更新尚未完成/);
        page.edit(newer);
        if (navigation.startsWith('project-')) page.project('b');
        if (navigation === 'scope-away') page.scope('global');
        if (navigation === 'project-round-trip') page.project('a');
        const visibleBefore = page.field.value;
        page.finish(outcome);
        if (outcome === 'timeout') {
          // Exercise api()'s real update timeout without waiting two minutes.
          await new Promise(setImmediate);
          vm.runInContext('Date.now=()=>Number.MAX_SAFE_INTEGER', page.context);
          page.context.fetch = async () => response({...snapshot(), update: {busy: true}});
          await page.resume();
        }
        await failed;
        if (outcome === 'timeout') vm.runInContext('Date.now=()=>'+Date.now(), page.context);
        assert.equal(page.field.value, visibleBefore);
        assert.equal(vm.runInContext('pendingSettingsSaves.size', page.context), 0);
        if (navigation === 'scope-away') page.scope('project'); else if (navigation === 'project-away') page.project('a');
        assert.equal(page.field.value, String(newer));
        assert.equal(vm.runInContext('settingsBaseline.schedule.weight', page.context), 1);
        assert.equal(vm.runInContext('settingsDrafts.has("a")', page.context), newer !== 1);
        assert.equal(page.storedWeight('a')?.baselineValue, newer === 1 ? undefined : 1);
        // The failed request must release ownership so retry and ordinary undo work.
        page.context.fetch = async (_url, options) => { page.posts.push(JSON.parse(options.body)); page.context.initialState.projects[0].config.schedule.weight = newer; return response({saved: true, config: {schedule: {weight: newer}}}); };
        await page.submit('settings-form');
        assert.equal(page.posts.length, 2);
        assert.deepEqual(page.posts[1].values, newer === 1 ? {} : {schedule: {weight: newer}});
        page.edit(5); page.edit(newer); assert.equal(page.storedWeight('a'), undefined);
      });
    }
  }
}

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

test('primary rate limit shows retry time in both languages and enables checking at reset', () => {
  const reset=1792998274;
  for(const locale of ['zh-CN','en']){
    const page=browser();
    const i18n=fs.readFileSync(path.join(__dirname,'../adaptive_rsi/web/i18n.js'),'utf8');
    // Load the real dictionary and translator without static DOM traversal.
    vm.runInContext(i18n.slice(0,i18n.indexOf('const staticText =')),page.context);
    vm.runInContext(`language=${JSON.stringify(locale)};Date.now=()=>${(reset-1)*1000};state.update={state:'error',reason:'GitHub HTTP 403',reason_code:'github_primary_rate_limit',rate_limit_until:${reset},busy:false};renderUpdateExtras=()=>{};renderUpdate();`,page.context);
    const expectedTime=vm.runInContext(`new Date(${reset}*1000).toLocaleString(language)`,page.context);
    assert.ok(page.node('update-status').textContent.includes(expectedTime));
    assert.match(page.node('update-status').textContent,locale==='en'?/anonymous API quota is exhausted/:/匿名 API 额度已用尽/);
    assert.match(page.node('update-status').textContent,locale==='en'?/paused until then/:/暂停更新请求/);
    assert.equal(page.node('check-update').disabled,true);
    assert.equal(page.node('apply-update').disabled,true);
    vm.runInContext(`Date.now=()=>${reset*1000};renderUpdate();`,page.context);
    assert.equal(page.node('check-update').disabled,false);
    assert.match(page.node('update-status').textContent,locale==='en'?/ready to retry/:/已到，可重试/);
    vm.runInContext('state.update={state:"error",reason:"GitHub HTTP 403",busy:false};renderUpdate();',page.context);
    // 无额度标记的 GitHub HTTP 码走结构化说明分支，两种语言都不再直出原文片段。
    assert.match(page.node('update-status').textContent,
      locale==='en'?/GitHub HTTP 403: could not reach the official release feed/:/无法访问 GitHub 发布接口（HTTP 403）/);
    assert.equal(page.node('check-update').disabled,false);
  }
});

test('every fixed updater reason emitted by updates.py is localized for both console languages', () => {
  const py = fs.readFileSync(path.join(__dirname, '../adaptive_rsi/updates.py'), 'utf8');
  const i18n = fs.readFileSync(path.join(__dirname, '../adaptive_rsi/web/i18n.js'), 'utf8');
  const apiBlock = source.slice(source.indexOf('const apiErrors={'), source.indexOf('function reviseForm'));
  const apiErrors = JSON.parse('{' + apiBlock.slice(apiBlock.indexOf('{') + 1, apiBlock.lastIndexOf('}')) + '}');
  const englishBlock = i18n.slice(i18n.indexOf('const english = {'), i18n.indexOf('let language ='));
  const english = JSON.parse(englishBlock.slice(englishBlock.indexOf('{'), englishBlock.lastIndexOf('}') + 1));
  const emitted = new Set();
  // 原因只从这些写入点进入 update-status：恢复模式、检查失败、内联 reason 字面量。
  for (const pattern of [/recovery_required\([^,\n]+,\s*"((?:[^"\\]|\\.)*)"\)/g,
                         /check_failure\([^,\n]+,\s*"((?:[^"\\]|\\.)*)"\)/g,
                         /"reason":\s*"((?:[^"\\]|\\.)*)"/g])
    for (const match of py.matchAll(pattern)) emitted.add(match[1]);
  // apply 的 except 会把 download/release_info/extract_bundle/version/apply 内
  // 抛出的 ValueError 文本原样写入 reason，这些固定文案同样必须可映射。
  const span = (signature, terminator) => {
    const start = py.indexOf(signature);
    assert.ok(start >= 0, 'updates.py contains ' + signature);
    const end = py.slice(start + 1).search(terminator);
    return py.slice(start, end < 0 ? undefined : start + 1 + end);
  };
  for (const name of ['def version(', 'def download(', 'def release_info(', 'def extract_bundle('])
    for (const match of span(name, /\ndef /).matchAll(/ValueError\(\s*"((?:[^"\\]|\\.)*)"/g)) emitted.add(match[1]);
  for (const match of span('    def apply(self, status):', /\n    def |\ndef /).matchAll(/ValueError\(\s*"((?:[^"\\]|\\.)*)"/g)) emitted.add(match[1]);
  assert.ok(emitted.size >= 36, 'reason extraction should cover every updater literal, found ' + emitted.size);
  // "GitHub HTTP <码>" 含动态内容，由 renderUpdate 的结构化分支渲染，不进映射键。
  const structured = reason => /^GitHub HTTP \d+$/.test(reason);
  const unmapped = [...emitted].filter(reason => !structured(reason) && !Object.hasOwn(apiErrors, reason));
  assert.deepEqual(unmapped, [], 'updates.py emits update reasons the console cannot localize');
  // 既有条目的英文值沿用改写风格；其余更新器原因英文界面必须显示原文。
  const restated = new Set(['Restart plan is missing; inspect installations before admitting work']);
  for (const [reason, localized] of Object.entries(apiErrors)) {
    if (!emitted.has(reason)) continue;
    assert.ok(Object.hasOwn(english, localized), 'english dictionary misses the updater reason: ' + reason);
    if (!restated.has(reason)) assert.equal(english[localized], reason, 'English UI must show the original updater reason: ' + reason);
  }
  assert.ok(Object.hasOwn(english, '无法访问 GitHub 发布接口（HTTP {code}），请稍后重试。'),
    'english dictionary misses the structured GitHub HTTP template');
  assert.ok(Object.hasOwn(english, '更新失败原因（原文）：'),
    'english dictionary misses the verbatim-reason prefix');
});

test('update reasons show Chinese guidance in Chinese and the original text in English', () => {
  const i18n = fs.readFileSync(path.join(__dirname, '../adaptive_rsi/web/i18n.js'), 'utf8');
  const cases = [
    [{state: 'error', reason: 'Unable to verify the official release feed'}, '无法访问 GitHub 官方发布源'],
    [{state: 'rolled_back', reason: 'Previous code and database restored and health verified'}, '已恢复旧版本：代码和数据库均已还原'],
    [{state: 'recovery_required', reason: 'Recovery backup is missing or corrupt; refusing an unsafe database rollback'}, '已拒绝回滚数据库'],
    [{state: 'no_release', reason: 'GitHub HTTP 404'}, '无法访问 GitHub 发布接口（HTTP 404）'],
    [{state: 'restarting', reason: 'Verifying restored service'}, '正在验证恢复后的服务'],
    [{state: 'error', reason: '<urlopen error request timed out>'}, '更新失败原因（原文）：'],
  ];
  for (const locale of ['zh-CN', 'en']) for (const [update, zhHint] of cases) {
    const page = browser();
    vm.runInContext(i18n.slice(0, i18n.indexOf('const staticText =')), page.context);
    vm.runInContext(`language=${JSON.stringify(locale)};state.update=${JSON.stringify({...update, busy: false})};renderUpdateExtras=()=>{};renderUpdate();`, page.context);
    const text = page.node('update-status').textContent;
    if (locale === 'zh-CN') {
      assert.ok(text.includes(zhHint), 'Chinese UI should guide on: ' + update.reason + ' — got: ' + text);
      if (update.reason !== '<urlopen error request timed out>' && !/^GitHub HTTP/.test(update.reason))
        assert.ok(!text.includes(update.reason), 'Chinese UI should not show the raw reason: ' + update.reason);
    } else {
      assert.ok(text.includes(update.reason), 'English UI should keep the original reason: ' + update.reason);
    }
  }
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
    await waitFor(() => posts.length === 1);
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

test('issue sync activity separates scans, real changes and unfinished continuation', () => {
  const page = browser();
  const copy = event => vm.runInContext(`eventCopy(${JSON.stringify(event)})`, page.context);
  const data = values => JSON.stringify(values);
  assert.equal(copy({kind:'issues',message:'Scanned 7 Issues; 2 updates synchronized',
    data:data({complete:true,scanned:7,changed:2})}), '已检查 7 个 Issue，同步 2 条更新');
  assert.equal(copy({kind:'issues',message:'Scanned 7 Issues; 0 updates synchronized',
    data:data({complete:true,scanned:7,changed:0})}), '已检查 7 个 Issue，没有需要同步的变化');
  assert.equal(copy({kind:'issues',message:'Scanned 7 Issues; 2 updates synchronized; sync continues next cycle',
    data:data({complete:false,scanned:7,changed:2})}),
  '已检查 7 个 Issue，同步 2 条更新；本次同步未完成，下一轮继续');
  assert.equal(copy({kind:'issues',message:'Scanned 7 Issues; 0 updates synchronized; sync continues next cycle',
    data:data({complete:false,scanned:7,changed:0})}),
  '已检查 7 个 Issue，暂未发现变化；本次同步未完成，下一轮继续');
  // Historical events keep their old message but no longer imply a finished sync.
  assert.equal(copy({kind:'issues',message:'3 Issue updates synchronized',
    data:data({complete:false,next_page:2,pages_read:1})}),
  '已同步 Issue 更新 · 3；本次同步未完成，下一轮继续');
});

test('inspection entry points follow project, configuration and repository availability', () => {
  const page = browser();
  const buttons = ['light', 'issues'].map(kind => {
    const button = page.node('inspect-' + kind); button.dataset.inspect = kind; return button;
  });
  const config = {
    sources: {inspection: false, issues: false},
    github: {repository: '', labels: []},
    schedule: {light_minutes: 60, issues_minutes: 60, timezone: 'UTC'},
    delivery: {mode: 'local', auto_merge: false},
  };
  Object.assign(page.context.initialState, {
    projects: [{id: 'a', name: 'A', paused: true, config}],
    config, tasks: [], runs: [], task_counts: [], schedules: [], events: [],
    quality: [], usage: [], model_usage: [], candidates: [], update: {},
  });
  page.document.querySelectorAll = selector => selector === '[data-inspect]' ? buttons : [];
  page.node('config-scope').value = 'project';
  vm.runInContext('language="zh-CN";view="settings";settingsBaseline={};readinessCache.set("a",{ready:true,items:[]});renderUpdateExtras=()=>{};originalRender()', page.context);
  assert.equal(buttons[0].disabled, false, 'manual check stays available when its source is disabled');
  assert.equal(buttons[1].disabled, true, 'missing repository cannot schedule Issue sync');
  page.context.initialState.projects[0].config.sources = {inspection: true, issues: true};
  page.context.initialState.projects[0].config.github.repository = 'owner/project';
  vm.runInContext('originalRender()', page.context);
  assert.equal(buttons[0].disabled, false);
  assert.equal(buttons[1].disabled, false);
  page.context.initialState.projects[0].config.github.repository = '';
  vm.runInContext('originalRender()', page.context);
  assert.equal(buttons[0].disabled, false, 'disabling Issue sync never hides manual checks');
  assert.equal(buttons[1].disabled, true, 'Issue sync stays disabled without a repository');
  page.context.initialState.projects[0].configuration_error = 'broken configuration';
  vm.runInContext('originalRender()', page.context);
  assert.equal(buttons[0].disabled, true, 'configuration errors disable manual checks');
  assert.equal(buttons[1].disabled, true);
});

test('no-repository and historical Issue copy use English punctuation and messages', () => {
  const page = browser();
  const i18n = fs.readFileSync(path.join(__dirname, '../adaptive_rsi/web/i18n.js'), 'utf8');
  const english = vm.runInNewContext(i18n.slice(0, i18n.indexOf('let language')) + ';english');
  page.context.t = value => english[value] ?? value;
  assert.equal(page.context.t('请先在项目设置中填写 GitHub 仓库，再安排 Issue 同步。'),
    'Configure a GitHub repository in project settings before scheduling Issue sync.');
  assert.equal(vm.runInContext('t(apiErrors["No GitHub repository configured"])', page.context),
    'Configure a GitHub repository in project settings before scheduling Issue sync.');
  const event = {kind: 'issues', message: '3 Issue updates synchronized',
    data: JSON.stringify({complete: false, next_page: 2, pages_read: 1})};
  assert.equal(vm.runInContext(`eventCopy(${JSON.stringify(event)})`, page.context),
    'Issue updates synchronized · 3; This sync did not finish and will continue next cycle');
});

test('live task details rerender when synced input changes but keep unchanged polls and drafts', async () => {
  const page = experiencePage(), task = {
    id: 'issue-task', project: 'a', title: '旧标题', source: 'issue', state: 'queued',
    body: '旧正文', evidence: '[{"number":7,"updated":"old"}]', updated: 1,
  };
  let metadata = {...task, dependency: null, revisions: []}, polls = 0, renders = 0;
  const originalShow = page.context.showTask;
  page.context.showTask = (...args) => { renders++; return originalShow(...args); };
  page.context.fetch = async url => {
    if (url.includes('task-runs?')) return response([]);
    if (url.includes('/task?')) { polls++; return response(metadata); }
    if (url.includes('/state?')) return response({tasks: []});
    return response({saved: true});
  };
  vm.runInContext("draftStore.set('task-supplement','issue-task','未保存补充草稿')", page.context);
  await page.context.showTask(task);
  await new Promise(setImmediate);
  assert.equal(renders, 1);
  const draft = node => node.tagName === 'TEXTAREA' ? node :
    (node.children || []).map(draft).find(Boolean);
  assert.equal(draft(page.node('detail-body')).value, '未保存补充草稿');
  // A synced Issue revision keeps state=queued but changes the visible input.
  metadata = {...metadata, title: '同步后的标题', body: '同步后的正文',
    evidence: '[{"number":7,"updated":"new"}]', updated: 2};
  const before = polls;
  page.fireTimer(5000);
  await waitFor(() => renders === 2 && polls > before);
  assert.equal(page.node('detail-title').textContent, '同步后的标题');
  assert.match(draft(page.node('detail-body')).value, /未保存补充草稿/);
  assert.ok(page.node('detail-body').children.some(node =>
    node.className === 'detail-section' && node.children.some(child => child.textContent === '同步后的正文')));
  // Unchanged polls must not rebuild the dialog.
  const settled = polls;
  await waitFor(() => page.timers.some(timer => timer.active && timer.delay === 5000));
  page.fireTimer(5000);
  await waitFor(() => polls > settled);
  await new Promise(setImmediate);
  assert.equal(renders, 2, 'unchanged live polls reuse the rendered detail');
});

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
  await waitFor(()=>calls===1);assert.equal(calls,1);release();await first;
  page.node('goal-text').value='next goal';
  const next=page.submit('goal-form');await waitFor(()=>calls===2);assert.equal(calls,2);release();await next;
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

test('ambiguous goals retain separate IDs through failed edits, reload, and project switches', async () => {
  const posts = [], tasks = new Map(); let page = browser(), failure = 'after-commit';
  const connect = () => {
    page.context.refresh = async () => {};
    vm.runInContext('safe=(fn,form)=>{lastAction=originalSafe(fn,form);return lastAction;}', page.context);
    page.context.fetch = async (url, options) => {
      assert.equal(url, '/api/goal');
      const body = JSON.parse(options.body); posts.push(body);
      if (failure === 'before-commit') throw Error('offline');
      tasks.set(body.project + ':' + body.request_id, body.text);
      if (failure === 'after-commit') throw Error('lost acknowledgement');
      return response({id: body.request_id});
    };
  };
  const submit = async text => {page.node('goal-text').value = text; page.input('goal-text', 'goal-form'); await page.submit('goal-form');};
  const switchTo = project => {page.node('project-select').value = project; page.node('project-select').handlers.change();};
  connect();
  await submit('Goal A');
  failure = 'before-commit'; await submit('Goal B');
  const pending = JSON.parse(page.sessionData.get('rsi-submission:a'));
  page = browser(page.sessionData); connect();
  switchTo('b'); failure = ''; await submit('Goal A');
  assert.notEqual(posts[2].request_id, posts[0].request_id, 'the same text in another project has its own ID');
  assert.equal(page.sessionData.has('rsi-submission:b'), false);
  switchTo('a'); await submit('Goal A');
  assert.equal(posts[3].request_id, posts[0].request_id, 'A retry reuses its ID after failed B and reload');
  assert.equal(pending.entries.length, 2);
  assert.ok(pending.entries.every(entry => /^[a-f0-9]{64}$/.test(entry.digest)));
  assert.deepEqual(pending.entries.map(entry => entry.text), ['Goal A', 'Goal B'], 'pending text remains available for visible recovery');
  assert.equal([...tasks.keys()].filter(key => key.startsWith('a:')).length, 1, 'A is created exactly once');
  assert.deepEqual(JSON.parse(page.sessionData.get('rsi-submission:a')).entries.map(entry => entry.id), [posts[1].request_id], 'A success removes only A');
  await submit('Goal B');
  assert.equal(posts[4].request_id, posts[1].request_id);
  assert.equal(page.sessionData.has('rsi-submission:a'), false);
  await submit('Goal A');
  assert.notEqual(posts[5].request_id, posts[0].request_id, 'a new submission after acknowledgement gets a fresh ID');
});

test('legacy pending goals migrate without losing the old ID or recovery text', async () => {
  const session = new Map([['rsi-submission:a', JSON.stringify({text: 'Legacy goal', id: 'legacy-id'})]]);
  const page = browser(session), posts = [];
  page.context.refresh = async () => {};
  page.context.fetch = async (_url, options) => {posts.push(JSON.parse(options.body)); throw Error('offline');};
  page.node('goal-text').value = 'Another goal';
  await assert.rejects(page.submit('goal-form'));
  const saved = JSON.parse(session.get('rsi-submission:a'));
  assert.equal(saved.version, 2); assert.equal(saved.entries.length, 2);
  assert.equal(saved.entries[0].id, 'legacy-id');
  assert.deepEqual(saved.entries.map(entry => entry.text), ['Legacy goal', 'Another goal']);
  page.context.fetch = async (_url, options) => {posts.push(JSON.parse(options.body)); return response({id: 'task'});};
  page.node('goal-text').value = 'Legacy goal'; await page.submit('goal-form');
  assert.equal(posts[1].request_id, 'legacy-id');
  assert.deepEqual(JSON.parse(session.get('rsi-submission:a')).entries.map(entry => entry.id), [posts[0].request_id]);
});

test('32 offline goals remain visibly recoverable after draft replacement and reload, freeing capacity on success', async () => {
  let page = browser(); const posts = [];
  page.context.refresh = async () => {};
  page.context.fetch = async (_url, options) => {posts.push(JSON.parse(options.body)); throw Error('offline');};
  for (let index = 0; index < 32; index++) {
    page.node('goal-text').value = 'Pending ' + index;
    await assert.rejects(page.submit('goal-form'));
  }
  const saved = page.sessionData.get('rsi-submission:a');
  page.node('goal-text').value = 'Replacement draft'; page.input('goal-text', 'goal-form');
  page = browser(page.sessionData); page.context.restoreGoalDraft();
  assert.equal(page.node('goal-text').value, 'Replacement draft');
  await page.context.renderGoalRecovery();
  const panel = page.node('goal-form').children.find(node => node.dataset.goalRecovery !== undefined);
  const select = panel.children[1].children[0], restore = panel.children[2];
  assert.equal(select.children.length, 33, 'all 32 old texts are selectable without remembering them');
  assert.equal(select.children[1].textContent, 'Pending 0');
  page.context.refresh = async () => {};
  page.context.fetch = async (_url, options) => {posts.push(JSON.parse(options.body)); return response({id: 'task'});};
  await assert.rejects(page.submit('goal-form'), /待确认的目标提交过多/);
  assert.equal(posts.length, 32); assert.equal(page.sessionData.get('rsi-submission:a'), saved);
  select.value = select.children[1].value;
  restore.handlers.click(); await page.context.lastAction;
  assert.equal(posts.length, 32, 'restoring a goal requires an explicit submit to send it');
  assert.equal(page.node('goal-text').value, 'Pending 0');
  assert.equal(vm.runInContext('draftStore.get("goal","a")', page.context), 'Pending 0');
  await page.submit('goal-form');
  assert.equal(posts[32].request_id, posts[0].request_id, 'visible recovery uses the original ID at capacity');
  const remaining = JSON.parse(page.sessionData.get('rsi-submission:a')).entries;
  assert.deepEqual(remaining.map(entry => entry.id), posts.slice(1, 32).map(post => post.request_id), 'success removes only the recovered entry');
  page.node('goal-text').value = 'New goal'; await page.submit('goal-form');
  assert.equal(posts.length, 34, 'acknowledgement frees capacity for new content');
});

test('digest-only legacy records explain exact-text recovery and retain their IDs', async () => {
  const digest = require('node:crypto').createHash('sha256').update(JSON.stringify('Original legacy goal')).digest('hex');
  const session = new Map([['rsi-submission:a', JSON.stringify({version: 2, entries: [{digest, id: 'old-digest-id'}]})]]);
  const page = browser(session), posts = [];
  await page.context.renderGoalRecovery();
  const panel = page.node('goal-form').children.find(node => node.dataset.goalRecovery !== undefined);
  assert.ok(panel.children.some(node => node.textContent.includes('没有保留原文')));
  assert.equal(JSON.parse(session.get('rsi-submission:a')).entries[0].id, 'old-digest-id');
  page.context.refresh = async () => {};
  page.context.fetch = async (_url, options) => {posts.push(JSON.parse(options.body)); throw Error('offline');};
  page.node('goal-text').value = 'Original legacy goal';
  await assert.rejects(page.submit('goal-form'));
  assert.equal(posts[0].request_id, 'old-digest-id');
  assert.equal(JSON.parse(session.get('rsi-submission:a')).entries[0].text, 'Original legacy goal');
  const updated = page.node('goal-form').children.find(node => node.dataset.goalRecovery !== undefined);
  assert.equal(updated.children[1].children[0].children[1].textContent, 'Original legacy goal');
});

test('pending goal restoration stays with its project and invalidates stale recovery controls', async () => {
  const page = browser();
  page.context.refresh = async () => {};
  page.context.fetch = async () => {throw Error('offline');};
  page.node('goal-text').value = 'A pending'; await assert.rejects(page.submit('goal-form'));
  const panel = page.node('goal-form').children.find(node => node.dataset.goalRecovery !== undefined);
  const select = panel.children[1].children[0], restore = panel.children[2];
  select.value = select.children[1].value;
  vm.runInContext('projectId="b"', page.context); page.node('goal-text').value = 'B draft';
  await page.context.renderGoalRecovery();
  assert.equal(panel.hidden, true);
  restore.handlers.click(); await page.context.lastAction;
  assert.equal(page.node('goal-text').value, 'B draft');
  vm.runInContext('projectId="a"', page.context); await page.context.renderGoalRecovery();
  assert.equal(panel.hidden, false);
  assert.equal(panel.children[1].children[0].children[1].textContent, 'A pending');
});

const validPendingEntry = {digest: 'a'.repeat(64), id: 'valid-id'};
for (const [name, saved] of [
  ['invalid JSON', '{'], ['null record', 'null'], ['array record', '[]'],
  ['invalid digest', JSON.stringify({version: 2, entries: [{...validPendingEntry, digest: 'bad'}]})],
  ['empty ID', JSON.stringify({version: 2, entries: [{...validPendingEntry, id: ''}]})],
  ['oversized ID', JSON.stringify({version: 2, entries: [{...validPendingEntry, id: 'x'.repeat(101)}]})],
  ['nonstring ID', JSON.stringify({version: 2, entries: [{...validPendingEntry, id: 1}]})],
  ['duplicate ID', JSON.stringify({version: 2, entries: [validPendingEntry, {...validPendingEntry, digest: 'b'.repeat(64)}]})],
  ['duplicate digest', JSON.stringify({version: 2, entries: [validPendingEntry, {...validPendingEntry, digest: 'A'.repeat(64), id: 'other'}]})],
  ['text does not match digest', JSON.stringify({version: 2, entries: [{...validPendingEntry, text: 'Altered text'}]})],
  ['oversized ledger', JSON.stringify({version: 2, entries: Array.from({length: 33}, (_, index) => ({digest: index.toString(16).padStart(64, '0'), id: 'id-' + index}))})],
  ['invalid legacy ID', JSON.stringify({text: 'Original', id: ''})],
]) {
  test(`invalid pending goal ledger (${name}) preserves records and reports a local recovery error`, async () => {
    const session = new Map([['rsi-submission:a', saved]]), page = browser(session); let posts = 0;
    page.context.fetch = async () => {posts++; return response({id: 'unexpected'});};
    vm.runInContext('safe=(fn,form)=>{lastAction=originalSafe(fn,form);return lastAction;}', page.context);
    page.node('goal-text').value = 'Keep draft';
    await page.submit('goal-form');
    assert.equal(posts, 0); assert.equal(session.get('rsi-submission:a'), saved);
    assert.equal(page.node('goal-text').value, 'Keep draft');
    assert.match(page.node('toast').textContent, /无法读取目标提交记录/);
    assert.doesNotMatch(page.node('toast').textContent, /JSON 格式有误/);
    await page.context.renderGoalRecovery();
    const panel = page.node('goal-form').children.find(node => node.dataset.goalRecovery !== undefined);
    assert.match(panel.children[0].textContent, /无法读取目标提交记录/);
  });
}

test('goals are never sent when their recovery IDs cannot be persisted', async () => {
  const page = browser(); let posts = 0;
  page.context.fetch = async () => {posts++; return response({id: 'task'});};
  page.context.sessionStorage.setItem = () => {throw Error('quota exceeded');};
  page.node('goal-text').value = 'Keep input';
  await assert.rejects(page.submit('goal-form'), /quota exceeded/);
  assert.equal(posts, 0); assert.equal(page.node('goal-text').value, 'Keep input');
});

test('a successful goal save clears only its submitted draft and preserves newer input', async () => {
  const page=browser();let finish;
  page.context.refresh=()=>Promise.resolve();
  page.context.fetch=()=>new Promise(resolve=>{finish=()=>resolve(response({id:'created'}));});
  page.node('goal-text').value='Submitted goal';page.input('goal-text','goal-form');
  const pending=page.submit('goal-form');await waitFor(()=>!!finish);
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
    await waitFor(() => !!finish);
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

function queueForm(page) {
  return page.node('detail-body').children.find(node => node.className === 'task-controls')
    .children.filter(node => node.tagName === 'FORM').at(-1);
}
function changeDetailLanguage(page, language = 'en') {
  Object.assign(page.context, {language: 'zh-CN', english: {}, translateStatic() {}});
  page.node('language-select').value = language;
  page.node('language-select').handlers.change();
  return page.context.lastAction;
}

for (const trigger of ['language', 'supplement']) {
  for (const remove of [false, true]) {
    test(`${trigger} detail rebuild preserves queue ${remove ? 'removal' : 'new selection'} and posts the draft`, async () => {
      const page = experiencePage(), task = fixtureTask('child', 'queued'), posts = [];
      let metadata = {...task, dependency: remove ? {dependency: 'parent'} : null, urgency: remove ? 1 : 0};
      page.context.fetch = async (url, options) => {
        if (options?.method === 'POST') {
          posts.push({url, body: JSON.parse(options.body)});
          if (url.endsWith('/task/revise')) metadata = {...metadata, state: 'deferred'};
          return response({saved: true});
        }
        if (url.includes('/task-runs?')) return response([]);
        if (url.includes('/state?')) return response({tasks: [fixtureTask('parent')]});
        return response(url.includes('id=parent&') ? fixtureTask('parent') : metadata);
      };
      const controls = await openExperienceTask(page, task);
      let form = queueForm(page);
      form.children[0].children[0].value = remove ? '' : 'parent';
      form.children[1].children[0].checked = !remove;
      if (trigger === 'language') await changeDetailLanguage(page);
      else {
        const supplement = controls.children.find(node => node.tagName === 'FORM');
        supplement.children[0].children[0].value = 'Additional requirement';
        supplement.onsubmit({preventDefault() {}}); await page.context.lastAction;
      }
      await new Promise(setImmediate);
      form = queueForm(page);
      assert.equal(form.children[0].children[0].value, remove ? '' : 'parent');
      assert.equal(form.children[1].children[0].checked, !remove);
      assert.equal(posts.filter(post => post.url.endsWith('/task/schedule')).length, 0);
      form.onsubmit({preventDefault() {}}); await page.context.lastAction;
      assert.deepEqual(posts.at(-1).body, {task: task.id, dependency: remove ? null : 'parent', urgency: remove ? 0 : 1});
    });
  }
}

for (const trigger of ['language', 'revision', 'periodic']) {
  for (const serverVisible of [false, true]) {
    test(`queue save rebases post-submit undo across ${trigger} rebuild (server visible: ${serverVisible})`, async () => {
      const page = experiencePage(), task = fixtureTask('child', 'queued'), posts = [];
      let metadata = {...task, dependency: null, urgency: 0}, acknowledge;
      vm.runInContext('safe=(fn,form)=>{lastAction=originalSafe(fn,form);return lastAction;}', page.context);
      page.context.fetch = async (url, options) => {
        if (url.endsWith('/task/schedule')) {
          const submitted = JSON.parse(options.body); posts.push(submitted);
          const commit = () => {metadata = {...metadata, dependency: submitted.dependency ? {dependency: submitted.dependency} : null, urgency: submitted.urgency};};
          if (posts.length > 1) {commit(); return response({saved: true});}
          if (serverVisible) commit();
          return new Promise(resolve => {acknowledge = () => {commit(); resolve(response({saved: true}));};});
        }
        if (url.endsWith('/task/revise')) {metadata = {...metadata, state: 'deferred'}; return response({saved: true});}
        if (url.includes('/task-runs?')) return response([]);
        if (url.includes('/state?')) return response({tasks: [fixtureTask('parent')]});
        return response(url.includes('id=parent&') ? fixtureTask('parent') : metadata);
      };
      await openExperienceTask(page, task);
      let form = queueForm(page);
      form.children[0].children[0].value = 'parent'; form.children[1].children[0].checked = true;
      form.onsubmit({preventDefault() {}}); const pending = page.context.lastAction;
      form.children[0].children[0].value = ''; form.children[1].children[0].checked = false;
      if (trigger === 'language') await changeDetailLanguage(page);
      if (trigger === 'revision') {
        const supplement = page.node('detail-body').children.find(node => node.className === 'task-controls').children.find(node => node.tagName === 'FORM');
        supplement.children[0].children[0].value = 'Rebuild during schedule save';
        supplement.onsubmit({preventDefault() {}}); await page.context.lastAction;
      }
      if (trigger === 'periodic') {
        metadata = {...metadata, state: 'deferred'};
        page.fireTimer(5000); await new Promise(setImmediate); await page.context.lastAction;
      }
      await new Promise(setImmediate);
      form = queueForm(page);
      assert.equal(form.children[0].children[0].value, ''); assert.equal(form.children[1].children[0].checked, false);
      assert.equal(form.children[2].disabled, true, 'logical task save remains pending in the rebuilt form');
      form.onsubmit({preventDefault() {}}); await page.context.lastAction;
      assert.equal(posts.length, 1, 'rebuilt form cannot issue a concurrent POST, even by programmatic submit');
      acknowledge(); await pending;
      assert.equal(form.children[2].disabled, false);
      await changeDetailLanguage(page, 'zh-CN'); await new Promise(setImmediate);
      form = queueForm(page);
      assert.equal(form.children[0].children[0].value, '', 'undo remains dirty against acknowledged parent');
      assert.equal(form.children[1].children[0].checked, false);
      form.onsubmit({preventDefault() {}}); await page.context.lastAction;
      assert.deepEqual(posts, [{task: 'child', dependency: 'parent', urgency: 1}, {task: 'child', dependency: null, urgency: 0}]);
      assert.equal(metadata.dependency, null); assert.equal(metadata.urgency, 0);
    });
  }
}

for (const navigation of ['close-reopen', 'other-task-return', 'project-return']) {
  test(`pending task save blocks duplicate POST but ends draft on ${navigation}`, async () => {
    const page = experiencePage(), task = fixtureTask('child', 'queued');
    let metadata = {...task, dependency: null, urgency: 0}, acknowledge, writes = 0;
    page.context.fetch = async (url, options) => {
      if (url.endsWith('/task/schedule')) {
        writes++;
        return new Promise(resolve => {acknowledge = () => {metadata = {...metadata, dependency: {dependency: 'parent'}, urgency: 1}; resolve(response({saved: true}));};});
      }
      if (url.includes('/task-runs?')) return response([]);
      if (url.includes('/state?')) return response({tasks: [fixtureTask('parent')]});
      return response(url.includes('id=other&') ? fixtureTask('other', 'queued') : metadata);
    };
    await openExperienceTask(page, task);
    let form = queueForm(page);
    form.children[0].children[0].value = 'parent'; form.children[1].children[0].checked = true;
    form.onsubmit({preventDefault() {}}); const pending = page.context.lastAction;
    if (navigation === 'other-task-return') await openExperienceTask(page, fixtureTask('other', 'queued'));
    else page.context.closeTaskDetail();
    if (navigation === 'project-return') vm.runInContext('projectId="b"', page.context);
    vm.runInContext('projectId="a"', page.context);
    await openExperienceTask(page, task); form = queueForm(page);
    assert.equal(form.children[0].children[0].value, '', 'new opening does not inherit an unsaved draft');
    assert.equal(form.children[1].children[0].checked, false);
    assert.equal(form.children[2].disabled, true);
    form.onsubmit({preventDefault() {}}); await page.context.lastAction;
    assert.equal(writes, 1);
    acknowledge(); await pending;
    assert.equal(form.children[2].disabled, false);
    assert.equal(form.children[0].children[0].value, '', 'old session acknowledgement does not restore its draft');
  });
}

test('a failed task save releases rebuilt controls and retains post-submit undo', async () => {
  const page = experiencePage(), task = fixtureTask('child', 'queued');
  let rejectSave, writes = 0;
  page.context.fetch = async (url, options) => {
    if (url.endsWith('/task/schedule')) {writes++; return new Promise((_resolve, reject) => {rejectSave = reject;});}
    if (url.includes('/task-runs?')) return response([]);
    if (url.includes('/state?')) return response({tasks: [fixtureTask('parent')]});
    return response(task);
  };
  await openExperienceTask(page, task);
  let form = queueForm(page);
  form.children[0].children[0].value = 'parent'; form.children[1].children[0].checked = true;
  form.onsubmit({preventDefault() {}}); const pending = page.context.lastAction;
  form.children[0].children[0].value = ''; form.children[1].children[0].checked = false;
  await changeDetailLanguage(page); await new Promise(setImmediate);
  rejectSave(Error('offline')); await assert.rejects(pending);
  form = queueForm(page); assert.equal(form.children[2].disabled, false);
  await changeDetailLanguage(page, 'zh-CN'); await new Promise(setImmediate);
  assert.equal(queueForm(page).children[0].children[0].value, '');
  assert.equal(queueForm(page).children[1].children[0].checked, false); assert.equal(writes, 1);
});

test('consecutive language rebuilds retain the draft while controls are still loading', async () => {
  const page = experiencePage(), task = fixtureTask('child', 'queued');
  let hold = false, release;
  page.context.fetch = async url => {
    if (url.includes('/task-runs?')) return response([]);
    if (url.includes('/state?')) return response({tasks: [fixtureTask('parent')]});
    if (hold && url.includes('id=child&')) { hold = false; return new Promise(resolve => {release = () => resolve(response(task));}); }
    return response(url.includes('id=parent&') ? fixtureTask('parent') : task);
  };
  await openExperienceTask(page, task);
  queueForm(page).children[0].children[0].value = 'parent';
  queueForm(page).children[1].children[0].checked = true;
  hold = true;
  await changeDetailLanguage(page);
  assert.equal(typeof release, 'function');
  await changeDetailLanguage(page, 'zh-CN'); await new Promise(setImmediate);
  release(); await new Promise(setImmediate);
  assert.equal(queueForm(page).children[0].children[0].value, 'parent');
  assert.equal(queueForm(page).children[1].children[0].checked, true);
});

test('dependency pages reach the 202nd old failed parent with bounded reads and retain edits across paging and rebuild', async () => {
  const page = experiencePage(), task = fixtureTask('child', 'queued'), requests = [], posts = [];
  const tasks = [task, ...Array.from({length: 200}, (_, i) => fixtureTask('visible-' + i, 'queued')), fixtureTask('old-failed-parent')];
  page.context.fetch = async (url, options) => {
    if (options?.method === 'POST') {posts.push(JSON.parse(options.body)); return response({saved: true});}
    if (url.includes('/task-runs?')) return response([]);
    const params = new URL(url, 'http://fixture.test').searchParams;
    if (url.includes('/state?')) {
      requests.push(url);
      assert.equal(params.get('project'), 'a'); assert.equal(params.get('limit'), '200');
      assert.equal(params.get('filter'), 'all'); assert.equal(params.get('system'), 'false'); assert.equal(params.get('sections'), 'core');
      const offset = Number(params.get('offset'));
      return response({tasks: tasks.slice(offset, offset + 200), pagination: {has_more: offset + 200 < tasks.length, total: tasks.length}});
    }
    return response(fixtureTask(params.get('id'), params.get('id') === 'child' ? 'queued' : 'failed'));
  };
  await openExperienceTask(page, task);
  let form = queueForm(page), select = form.children[0].children[0], pager = form.children[3];
  assert.equal(requests.length, 1, 'opening must not download every page');
  assert.equal(pager.children[0].disabled, true); assert.equal(pager.children[2].disabled, false);
  assert.ok(!select.children.some(option => option.value === 'child'));
  assert.ok(!select.children.some(option => option.value === 'old-failed-parent'));
  pager.children[2].handlers.click(); await page.context.lastAction;
  assert.equal(requests.length, 2); assert.equal(pager.children[2].disabled, true);
  select.value = 'old-failed-parent'; form.children[1].children[0].checked = true;
  pager.children[0].handlers.click(); await page.context.lastAction;
  assert.equal(select.value, 'old-failed-parent'); assert.equal(form.children[1].children[0].checked, true);
  assert.ok(select.children.length <= 202, 'pages replace candidates instead of accumulating the entire queue');
  await page.context.refresh();
  assert.equal(select.value, 'old-failed-parent');
  await changeDetailLanguage(page); await new Promise(setImmediate);
  form = queueForm(page); select = form.children[0].children[0];
  assert.equal(select.value, 'old-failed-parent', 'off-page draft is restored after translation');
  assert.equal(select.children.find(option => option.value === 'old-failed-parent').textContent, 'Task old-failed-parent');
  form.onsubmit({preventDefault() {}}); await page.context.lastAction;
  assert.deepEqual(posts, [{task: 'child', dependency: 'old-failed-parent', urgency: 1}]);
});

for (const navigation of ['stay', 'close', 'project', 'other-task']) {
  test(`pending dependency page preserves ${navigation} scope and edits; failed page can retry`, async () => {
    const page = experiencePage(), task = fixtureTask('child', 'queued');
    let release, fail = true, delayed = false, reads = 0;
    page.context.fetch = async url => {
      if (url.includes('/task-runs?')) return response([]);
      if (url.includes('/state?')) {
        reads++;
        if (url.includes('offset=200')) {
          if (fail) {fail = false; return response({error: 'offline'}, 503);}
          delayed = true; return new Promise(resolve => {release = () => resolve(response({tasks: [fixtureTask('late-parent')], pagination: {has_more: false}}));});
        }
        return response({tasks: [fixtureTask('parent'), fixtureTask('new-parent')], pagination: {has_more: true}});
      }
      return response(url.includes('id=other&') ? fixtureTask('other', 'queued') : task);
    };
    await openExperienceTask(page, task);
    const form = queueForm(page), select = form.children[0].children[0], pager = form.children[3];
    select.value = 'parent';
    pager.children[2].handlers.click(); await assert.rejects(page.context.lastAction);
    assert.equal(pager.children[2].disabled, false); assert.equal(select.value, 'parent');
    pager.children[2].handlers.click(); const pending = page.context.lastAction;
    await new Promise(setImmediate); assert.equal(delayed, true);
    pager.children[2].handlers.click(); await page.context.lastAction;
    assert.equal(reads, 3, 'double clicks do not issue overlapping page reads');
    select.value = 'new-parent'; form.children[1].children[0].checked = true;
    if (navigation === 'close') page.context.closeTaskDetail();
    if (navigation === 'project') {page.context.closeTaskDetail(); vm.runInContext('projectId="b"', page.context);}
    if (navigation === 'other-task') await openExperienceTask(page, fixtureTask('other', 'queued'));
    const before = page.node('detail-body').children.slice();
    release(); await pending;
    if (navigation === 'stay') {
      assert.equal(select.value, 'new-parent'); assert.equal(form.children[1].children[0].checked, true);
      assert.ok(select.children.some(option => option.value === 'late-parent'));
    } else {
      assert.deepEqual(page.node('detail-body').children, before);
      assert.ok(!select.children.some(option => option.value === 'late-parent'), 'late page cannot mutate obsolete controls');
      if (navigation === 'other-task') {
        assert.equal(queueForm(page).children[0].children[0].value, '');
        assert.equal(queueForm(page).children[1].children[0].checked, false);
      }
    }
  });
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

for (const failFirst of [false, true]) {
  test(`queue saves block overlapping submits and preserve later edits after ${failFirst ? 'failure' : 'success'}`, async () => {
    const page = experiencePage(), task = fixtureTask('child', 'queued'), posts = [];
    let acknowledge, metadata = {...task, urgency: 0};
    vm.runInContext('safe=(fn,form)=>{lastAction=originalSafe(fn,form);return lastAction;}', page.context);
    page.context.fetch = async (url, options) => {
      if (url.endsWith('/task/schedule')) {
        const body = JSON.parse(options.body); posts.push(body);
        return new Promise((resolve, reject) => {acknowledge = () => {
          if (failFirst && posts.length === 1) {reject(Error('offline')); return;}
          metadata = {...metadata, dependency: body.dependency ? {dependency: body.dependency} : null, urgency: body.urgency};
          resolve(response({saved: true}));
        };});
      }
      if (url.includes('/task-runs?')) return response([]);
      if (url.includes('/state?')) return response({tasks: [fixtureTask('parent')]});
      return response(metadata);
    };
    const controls = await openExperienceTask(page, task);
    const form = controls.children.filter(node => node.tagName === 'FORM').at(-1);
    const dependency = form.children[0].children[0], urgency = form.children[1].children[0], submit = form.children[2];
    dependency.value = 'parent'; urgency.checked = true;
    form.onsubmit({preventDefault() {}}); const first = page.context.lastAction;
    assert.equal(submit.disabled, true);
    dependency.value = ''; urgency.checked = false;
    form.onsubmit({preventDefault() {}}); await page.context.lastAction;
    assert.equal(posts.length, 1, 'even a programmatic submit cannot overlap');
    acknowledge(); await first;
    assert.equal(submit.disabled, false, 'finally releases the button after success or failure');
    assert.equal(dependency.value, ''); assert.equal(urgency.checked, false);
    form.onsubmit({preventDefault() {}}); const later = page.context.lastAction;
    assert.deepEqual(posts[1], {task: 'child', dependency: null, urgency: 0}, 'later manual save uses the newer draft');
    acknowledge(); await later;
    assert.equal(metadata.dependency, null); assert.equal(metadata.urgency, 0);
    assert.equal(submit.disabled, false);
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

function modelScopePage(savedDrafts = new Map()) {
  const page=browser(),posts=[],globals={planning:{provider:'ollama',model:'scope-coder',effort:'medium',fallback:null}},overrides=new Map([['a',structuredClone(globals)],['b',{}]]);
  let release,held=false,holdNext=false,probeFailure=false;
  const effective=project=>({...structuredClone(globals),...structuredClone(overrides.get(project)||{})});
  const modelSnapshot=project=>({project,models:effective(project),global_models:structuredClone(globals),overrides:structuredClone(overrides.get(project)||{}),providers:[{id:'ollama',name:'Ollama',protocol:'ollama',catalog:{models:[{id:'scope-coder',name:'Scope coder'}]}}]});
  page.context.english={};page.context.window.addEventListener=()=>{};
  const tab=page.node('model-tab');tab.dataset.mcTab='roles';tab.parentElement=page.node('model-tabs');
  page.document.querySelectorAll=selector=>selector==='[data-mc-tab]'?[tab]:[];
  for(const id of ['view-models','mc-provider-list','mc-model-list','mc-role-list','mc-extra-role-list'])page.node(id).removeAttribute=()=>{};
  page.node('mc-probe-effort').querySelector=()=>page.node('mc-probe-effort').children[0];
  page.context.window.RSIDraftStore={get:(_type,key)=>structuredClone(savedDrafts.get(key)),set:(_type,key,value)=>savedDrafts.set(key,structuredClone(value)),remove:(_type,key)=>savedDrafts.delete(key)};
  page.context.api=async(path,body)=>{
    if(body){posts.push({path,body:structuredClone(body)});if(holdNext){holdNext=false;held=true;await new Promise(resolve=>{release=resolve;});held=false;}}
    if(path==='model-center/routes'){
      overrides.set(body.project,Object.fromEntries(Object.entries(body.models).filter(([role,route])=>!body.inherit?.includes(role)&&JSON.stringify(route)!==JSON.stringify(globals[role]))));
      return {saved:true,models:effective(body.project)};
    }
    if(path==='model-center/probe'){if(probeFailure)throw Object.assign(new Error('Probe failed'),{code:'probe_failed'});return {calls:[{tokens:1}]};}
    if(path.startsWith('model-center'))return modelSnapshot(path.includes('?')?decodeURIComponent(path.split('=')[1]):null);
    throw new Error('Unexpected model request '+path);
  };
  page.context.refresh=async()=>{};
  const modelSource=fs.readFileSync(path.join(__dirname,'../adaptive_rsi/web/model-center.js'),'utf8');
  // Keep production loading, saving, draft persistence and event handlers; omit unrelated catalog rendering.
  const instrumented=modelSource.replace('  init();',`  render=renderRoles=()=>{updateDirty();persistRouteDraft();};
  window.modelScopeTest={openProbe:()=>openProbe(data.providers[0],data.providers[0].catalog.models[0]),edit:effort=>{routes().planning.effort=effort;renderRoles();},draft:()=>structuredClone(routes())};
  init();`);
  vm.runInContext(instrumented,page.context);
  return {...page,posts,globals,overrides,savedDrafts,
    async show(project){page.context.window.RSIModelCenter.show(project,true);await page.context.lastAction;},
    click(id){page.node(id).handlers.click();return page.context.lastAction;},
    edit(effort){page.context.window.modelScopeTest.edit(effort);},
    draft(){return page.context.window.modelScopeTest.draft();},
    openProbe(){page.context.window.modelScopeTest.openProbe();},
    hold(){holdNext=true;},failProbe(){probeFailure=true;},release(){assert(held);release();}
  };
}

for(const length of [160,161,256])for(const name of ['', '短显示名'])test(`custom model form preserves ${length}-character ID with ${name?'explicit':'blank'} name`,async()=>{
  const page=modelScopePage();await page.show('a');
  page.node('mc-custom-form').querySelector=()=>page.node('custom-submit');
  page.node('mc-custom-dialog').showModal();
  const id='x'.repeat(length);
  page.node('mc-custom-id').value=id;page.node('mc-custom-name').value=name;
  await page.submit('mc-custom-form');
  assert.deepEqual(page.posts[0],{path:'model-center/model',body:{project:'a',provider:'ollama',model:{id,name,manual:true}}});
  assert.equal(page.node('mc-custom-dialog').open,false);
});

test('provider form sends full custom IDs without inventing truncated display names',async()=>{
  const page=modelScopePage();await page.show('a');
  page.node('mc-provider-form').reset=()=>{page.node('mc-provider-preset').value='custom';};
  page.node('mc-input-auth').querySelector=()=>page.node('keychain-option');
  page.node('mc-add-provider').handlers.click();
  page.node('mc-input-name').value='Fixture';page.node('mc-input-url').value='http://fixture.invalid';
  page.node('mc-input-protocol').value='chat_completions';page.node('mc-input-auth').value='none';
  const id='x'.repeat(256);page.node('mc-input-models').value=id;
  const api=page.context.api;
  page.context.api=async(path,body)=>path==='model-center/provider'?{id:'ollama'}:api(path,body);
  await page.submit('mc-provider-form');
  assert.deepEqual(page.posts.find(post=>post.path==='model-center/model').body.model,{id,manual:true});
});

test('equal-valued project overrides can be reset, persisted across reload, and follow later defaults',async()=>{
  const page=modelScopePage();await page.show('a');
  assert.equal(page.node('mc-save-routes').disabled,true);
  page.click('mc-reset-inheritance');
  assert.equal(page.node('mc-save-routes').disabled,false);
  assert.equal(page.savedDrafts.get('a').resetInheritance,true);
  const restored=modelScopePage(page.savedDrafts);await restored.show('a');
  assert.equal(restored.node('mc-save-routes').disabled,false);
  await restored.click('mc-save-routes');
  assert.equal(restored.overrides.get('a').planning,undefined);
  assert.equal(restored.node('mc-save-routes').disabled,true);
  assert.equal(restored.savedDrafts.has('a'),false);
  restored.globals.planning.effort='low';await restored.show('a');
  assert.equal(restored.draft().planning.effort,'low');
});

for(const mode of ['reload','scope-round-trip','pending-save'])test(`reset follows defaults changed before acknowledgement after ${mode}`,async()=>{
  const original=modelScopePage();await original.show('a');original.click('mc-reset-inheritance');
  const page=mode==='reload'?modelScopePage(original.savedDrafts):original;
  if(mode==='pending-save'){
    page.hold();const saving=page.click('mc-save-routes');await new Promise(setImmediate);
    page.globals.planning.effort='low';page.release();await saving;
  }else{
    page.globals.planning.effort='low';if(mode==='scope-round-trip')await page.show('b');
    await page.show('a');assert.equal(page.draft().planning.effort,'low');
    assert.equal(page.node('mc-save-routes').disabled,false);await page.click('mc-save-routes');
  }
  assert.deepEqual(page.posts.find(post=>post.path==='model-center/routes').body.inherit,['planning']);
  assert.equal(page.overrides.get('a').planning,undefined);
  assert.equal(page.draft().planning.effort,'low');assert.equal(page.node('mc-save-routes').disabled,true);
  page.globals.planning.effort='high';await page.show('a');assert.equal(page.draft().planning.effort,'high');
});

for(const effort of ['high','low'])test(`reset preserves later ${effort} edit across changed defaults and persisted reload`,async()=>{
  const original=modelScopePage();await original.show('a');original.click('mc-reset-inheritance');original.edit(effort);
  const page=modelScopePage(original.savedDrafts);page.globals.planning.effort='low';await page.show('a');
  assert.equal(page.draft().planning.effort,effort);
  await page.show('b');await page.show('a');assert.equal(page.draft().planning.effort,effort);
  await page.click('mc-save-routes');
  assert.deepEqual(page.posts[0].body.inherit,[]);assert.equal(page.posts[0].body.models.planning.effort,effort);
  assert.equal(page.node('mc-save-routes').disabled,true);
  assert.equal(page.overrides.get('a').planning?.effort,effort==='low'?undefined:effort);
});

for(const later of ['edit','reset','reset-then-edit'])test(`route acknowledgement retains later ${later} through a scope round trip`,async()=>{
  const page=modelScopePage();await page.show('a');page.click('mc-reset-inheritance');
  page.hold();const saving=page.click('mc-save-routes');await new Promise(setImmediate);
  if(later.includes('reset'))page.click('mc-reset-inheritance');
  if(later.includes('edit'))page.edit('high');
  await page.show('b');await page.show('a');
  page.release();await saving;
  assert.equal(page.draft().planning.effort,later.includes('edit')?'high':'medium');
  assert.equal(page.node('mc-save-routes').disabled,false);
  assert.equal(page.savedDrafts.get('a').resetInheritance,later.includes('reset'));
  await page.click('mc-save-routes');
  assert.equal(page.node('mc-save-routes').disabled,true);
  assert.equal(page.savedDrafts.has('a'),false);
});

test('a reset during an ordinary pending route save survives its acknowledgement',async()=>{
  const page=modelScopePage();await page.show('a');page.edit('high');page.hold();
  const saving=page.click('mc-save-routes');await new Promise(setImmediate);
  page.click('mc-reset-inheritance');await page.show('b');page.release();await saving;await page.show('a');
  assert.equal(page.draft().planning.effort,'medium');
  assert.equal(page.node('mc-save-routes').disabled,false);
  assert.equal(page.savedDrafts.get('a').resetInheritance,true);
  await page.click('mc-save-routes');assert.equal(page.overrides.get('a').planning,undefined);
});

test('a failed route save preserves inheritance reset intent for retry',async()=>{
  const page=modelScopePage();await page.show('a');page.click('mc-reset-inheritance');
  const api=page.context.api;page.context.api=async(path,body)=>{if(path==='model-center/routes')throw new Error('Save failed');return api(path,body);};
  await page.click('mc-save-routes');
  assert.equal(page.node('mc-save-routes').disabled,false);assert.equal(page.savedDrafts.get('a').resetInheritance,true);
  page.globals.planning.effort='low';await page.show('b');await page.show('a');
  page.context.api=api;page.hold();const retry=page.click('mc-save-routes');await new Promise(setImmediate);
  page.globals.planning.effort='high';page.release();await retry;
  assert.equal(page.draft().planning.effort,'high');
  assert.equal(page.node('mc-save-routes').disabled,true);assert.equal(page.overrides.get('a').planning,undefined);
});

test('reset edits survive failed save, reload, and a changed default on retry',async()=>{
  const original=modelScopePage();await original.show('a');original.click('mc-reset-inheritance');original.edit('high');
  const api=original.context.api;original.context.api=async(path,body)=>{if(path==='model-center/routes')throw new Error('Save failed');return api(path,body);};
  await original.click('mc-save-routes');assert.equal(original.savedDrafts.get('a').resetInheritance,true);
  const page=modelScopePage(original.savedDrafts);page.globals.planning.effort='low';await page.show('a');
  assert.equal(page.draft().planning.effort,'high');await page.click('mc-save-routes');
  assert.deepEqual(page.posts[0].body.inherit,[]);assert.equal(page.overrides.get('a').planning.effort,'high');
  assert.equal(page.node('mc-save-routes').disabled,true);assert.equal(page.savedDrafts.has('a'),false);
});

for(const projectEffort of ['medium','high'])for(const draftEffort of ['medium','xhigh'])test(`parent reset draft with project ${projectEffort} and draft ${draftEffort} requires an explicit reset`,async()=>{
  // 642 stores the project baseline and reset routes, but no resetModels.
  const route=effort=>({planning:{provider:'ollama',model:'scope-coder',effort,fallback:null}});
  const savedDrafts=new Map([['a',{baseline:route(projectEffort),routes:route(draftEffort),resetInheritance:true}]]);
  const page=modelScopePage(savedDrafts);page.overrides.set('a',route(projectEffort));page.globals.planning.effort='low';await page.show('a');
  assert.equal(page.draft().planning.effort,draftEffort);
  assert.equal(page.node('mc-save-routes').disabled,true);assert.equal(page.node('mc-undo').disabled,false);
  assert.match(page.node('mc-dirty').textContent,/未保存/);
  assert.match(page.node('mc-feedback').textContent,/缺少继承基线.*重新点击/);
  await page.click('mc-save-routes');assert.equal(page.posts.length,0);
  await page.show('b');await page.show('a');assert.equal(page.draft().planning.effort,draftEffort);
  assert.equal(page.node('mc-save-routes').disabled,true);assert.match(page.node('mc-feedback').textContent,/缺少继承基线/);
  page.edit('xhigh');await page.click('mc-save-routes');assert.equal(page.posts.length,0);
  const reopened=modelScopePage(savedDrafts);reopened.globals.planning.effort='low';await reopened.show('a');
  assert.equal(reopened.draft().planning.effort,'xhigh');assert.equal(reopened.node('mc-save-routes').disabled,true);
  assert.equal(savedDrafts.get('a').resetInheritance,true);assert.equal(savedDrafts.get('a').resetModels,undefined);
  reopened.click('mc-reset-inheritance');assert.equal(reopened.node('mc-save-routes').disabled,false);
  assert.equal(reopened.draft().planning.effort,'low');await reopened.click('mc-save-routes');
  assert.deepEqual(reopened.posts[0].body.inherit,['planning']);assert.equal(reopened.overrides.get('a').planning,undefined);
  assert.equal(reopened.node('mc-save-routes').disabled,true);assert.equal(savedDrafts.has('a'),false);
});

test('undo discards an ambiguous parent reset draft and allows ordinary editing',async()=>{
  const page=modelScopePage();await page.show('a');page.click('mc-reset-inheritance');
  delete page.savedDrafts.get('a').resetModels;
  const restored=modelScopePage(page.savedDrafts);await restored.show('a');
  assert.equal(restored.node('mc-save-routes').disabled,true);restored.click('mc-undo');
  assert.equal(restored.savedDrafts.has('a'),false);assert.equal(restored.node('mc-undo').disabled,true);
  restored.edit('high');assert.equal(restored.node('mc-save-routes').disabled,false);
  await restored.click('mc-save-routes');assert.equal(restored.overrides.get('a').planning.effort,'high');
});

test('undo clears equal-valued inheritance reset intent',async()=>{
  const page=modelScopePage();await page.show('a');page.click('mc-reset-inheritance');page.click('mc-undo');
  assert.equal(page.node('mc-save-routes').disabled,true);assert.equal(page.savedDrafts.has('a'),false);
});

for(const switchWith of ['show','scope-control'])test(`probe dialog invalidates on ${switchWith} scope change before start`,async()=>{
  const page=modelScopePage();await page.show('a');page.openProbe();
  assert.equal(page.node('mc-probe-dialog').open,true);
  if(switchWith==='show')await page.show('b');
  else{page.node('mc-scope').value='global';page.node('mc-scope').handlers.change();await page.context.lastAction;}
  assert.equal(page.node('mc-probe-dialog').open,false);
  await page.click('mc-probe-start');
  assert.equal(page.posts.filter(p=>p.path==='model-center/probe').length,0);
});

test('a current probe renders its result and retains the opening project',async()=>{
  const page=modelScopePage();await page.show('a');page.openProbe();await page.click('mc-probe-start');
  assert.equal(page.posts[0].body.project,'a');assert.match(page.node('mc-probe-result').textContent,/基础能力测试通过/);
  assert.equal(page.node('mc-probe-start').disabled,false);
});

for(const failure of [false,true])test(`delayed probe ${failure?'failure':'success'} cannot paint another project or a replacement dialog`,async()=>{
  const page=modelScopePage();await page.show('a');page.openProbe();page.hold();if(failure)page.failProbe();
  const probing=page.click('mc-probe-start');await new Promise(setImmediate);
  assert.equal(page.posts[0].body.project,'a');
  await page.show('b');assert.equal(page.node('mc-probe-result').textContent,'');
  page.release();await probing;
  assert.equal(page.node('mc-probe-result').textContent,'');assert.equal(page.node('mc-probe-dialog').open,false);
  await page.show('a');page.openProbe();assert.equal(page.node('mc-probe-result').textContent,'');
  page.node('mc-probe-dialog').close();
  await page.click('mc-probe-start');assert.equal(page.posts.length,1);
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
    if(failure==='catalog'&&calls.catalog===1)throw Object.assign(new Error('Catalog unavailable'),{status:503});
    if(failure==='catalog503')throw Object.assign(new Error('Catalog unavailable'),{status:503});
    if(failure==='catalog400')throw Object.assign(new Error('Catalog rejected'),{status:400});
  });
  await tick();await tick();
  return {page,calls,tick,walk,release:()=>release(),text:()=>walk(page.node('mc-auth')).map(node=>node.textContent||'').join(' '),
    activeTimers:()=>page.timers.filter(timer=>timer.active&&timer.delay===1500).length,
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

test('completed auth stops catalog retries on a non-retryable rejection',async()=>{
  const run=await authCompletionPage({failure:'catalog400'});await run.finish();
  assert.equal(run.calls.catalog,1);
  assert.match(run.text(),/模型目录刷新失败/);
  assert.equal(run.activeTimers(),0,'a 4xx catalog rejection must not keep retrying');
  assert.equal(run.calls.catalog,1,'no further catalog attempts after a permanent rejection');
});

test('completed auth bounds catalog retries for a persistent transient failure',async()=>{
  const run=await authCompletionPage({failure:'catalog503'});await run.finish();
  for(let attempt=0;attempt<8;attempt++){if(!run.activeTimers())break;run.page.fireTimer(1500);await run.tick();await run.tick();}
  assert.equal(run.calls.catalog,5,'catalog retries stop after four automatic retries');
  assert.match(run.text(),/模型目录刷新失败/);
  assert.equal(run.activeTimers(),0);
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

function usagePage() {
  const page = browser(), requests = [];
  let ready, hiddenChanged;
  page.context.language = 'zh-CN';
  page.document.visibilityState = 'visible';
  page.document.addEventListener = (type, callback) => { if (type === 'DOMContentLoaded') ready = callback; };
  page.context.MutationObserver = class {
    constructor(callback) { hiddenChanged = callback; }
    observe(node, options) {
      assert.equal(node, page.node('view-usage'));
      assert.deepEqual(Array.from(options.attributeFilter), ['hidden']);
    }
  };
  vm.runInContext(usageSource, page.context);
  ready();
  page.node('view-usage').hidden = false;
  page.node('usage-period').value = 'all';
  page.node('usage-scope').value = 'current';
  page.node('usage-sort').value = 'recent';
  vm.runInContext('view="usage"', page.context);
  page.context.fetch = url => new Promise(resolve => {
    requests.push({params: new URLSearchParams(url.split('?')[1]), finish(title, status = 200) {
      resolve(response({summary: {tokens: 100, tasks: 1}, roles: [], daily: [],
        tasks: [{task: title, title, project: 'a', state: 'failed', tokens: 100, attempts: 1}], total: 60}, status));
    }});
  });
  return Object.assign(page, {
    requests,
    show() { page.context.window.RSIUsage.show(); return page.context.lastAction; },
    search(query) { page.node('usage-search').value = query; page.node('usage-search').handlers.input(); },
    hide() { vm.runInContext('view="overview"', page.context); page.node('view-usage').hidden = true; hiddenChanged(); },
    active(delay) { return page.timers.filter(timer => timer.active && timer.delay === delay).length; },
    title() { return page.node('usage-tasks').children[0]?.children[0]?.children[0]?.textContent; }
  });
}

for (const timing of ['before-input', 'during-debounce', 'after-search']) {
  for (const status of timing === 'before-input' ? [200] : [200, 404, 500]) {
    test(`usage search survives an old ${status} response ${timing}`, async () => {
      const page = usagePage(), old = page.show();
      if (timing === 'before-input') { page.requests[0].finish('Old result'); await old; }
      page.search('latest query');
      assert.equal(page.active(350), 1);
      assert.equal(page.active(30000), 0);
      assert.equal(page.node('usage-refresh').disabled, false);
      if (timing === 'during-debounce') {
        page.requests[0].finish('Old result', status); await old;
        assert.equal(page.active(350), 1, 'old finalization must preserve the search');
        assert.equal(page.title(), undefined, 'old data must not render after input');
        assert.equal(page.active(30000), 0);
      }
      page.fireTimer(350);
      const search = page.context.lastAction;
      assert.equal(page.requests.length, 2);
      assert.equal(page.requests[1].params.get('q'), 'latest query');
      assert.equal(page.requests[1].params.get('offset'), '0');
      page.requests[1].finish('Latest result'); await search;
      if (timing === 'after-search') { page.requests[0].finish('Old result', status); await old; }
      assert.equal(page.title(), 'Latest result');
      assert.equal(page.node('usage-search').value, 'latest query');
      assert.equal(page.active(350), 0);
      assert.equal(page.active(30000), 1);
    });
  }
}

test('usage coalesces repeated input, resets pagination and refreshes the latest query', async () => {
  const page = usagePage(), initial = page.show();
  page.requests[0].finish('Initial'); await initial;
  page.node('usage-next').onclick(); const next = page.context.lastAction;
  assert.equal(page.requests[1].params.get('offset'), '25');
  page.requests[1].finish('Page two'); await next;
  page.search('first'); page.search('latest');
  assert.equal(page.active(350), 1);
  assert.equal(page.active(30000), 0);
  page.fireTimer(350); const search = page.context.lastAction;
  assert.equal(page.requests.length, 3);
  assert.equal(page.requests[2].params.get('q'), 'latest');
  assert.equal(page.requests[2].params.get('offset'), '0');
  page.document.visibilityState = 'hidden';
  page.requests[2].finish('Latest'); await search;
  assert.equal(page.active(60000), 1);
  page.document.visibilityState = 'visible';
  page.fireTimer(60000); const refresh = page.context.lastAction;
  assert.equal(page.requests[3].params.get('q'), 'latest');
  page.requests[3].finish('Refreshed'); await refresh;
  assert.equal(page.active(30000), 1);
});

test('usage pagination stays disabled during a load and re-enables at the boundaries', async () => {
  const page = usagePage(), initial = page.show();
  assert.equal(page.node('usage-prev').disabled, true, 'prev must be disabled while a page loads');
  assert.equal(page.node('usage-next').disabled, true, 'next must be disabled while a page loads');
  page.requests[0].finish('Initial'); await initial;
  assert.equal(page.node('usage-page').textContent, '60 条记录 · 1–25');
  assert.equal(page.node('usage-prev').disabled, true, 'prev must stay disabled on the first page');
  assert.equal(page.node('usage-next').disabled, false);
  page.node('usage-next').onclick(); const second = page.context.lastAction;
  assert.equal(page.node('usage-prev').disabled, true, 'prev must be disabled while a page loads');
  assert.equal(page.node('usage-next').disabled, true, 'next must be disabled while a page loads');
  page.requests[1].finish('Page two'); await second;
  assert.equal(page.node('usage-page').textContent, '60 条记录 · 26–50');
  page.node('usage-next').onclick(); const third = page.context.lastAction;
  page.requests[2].finish('Page three'); await third;
  assert.equal(page.node('usage-page').textContent, '60 条记录 · 51–60');
  assert.equal(page.node('usage-next').disabled, true, 'next must be disabled on the last page');
  assert.equal(page.node('usage-prev').disabled, false);
  page.node('usage-prev').onclick(); const previous = page.context.lastAction;
  page.requests[3].finish('Page two again'); await previous;
  assert.equal(page.node('usage-page').textContent, '60 条记录 · 26–50');
});

test('usage pagination restores the rendered page when a request fails', async () => {
  const page = usagePage(), initial = page.show();
  page.requests[0].finish('Initial'); await initial;
  page.node('usage-next').onclick(); const second = page.context.lastAction;
  page.requests[1].finish('Page two', 500); await second.catch(() => {});
  assert.equal(page.title(), 'Initial', 'a failed page load must keep the previous rows');
  assert.equal(page.node('usage-page').textContent, '60 条记录 · 1–25', 'the page label must match the rendered rows after a failure');
  assert.equal(page.node('usage-refresh').disabled, false);
  page.node('usage-next').onclick(); const retry = page.context.lastAction;
  assert.equal(page.requests[2].params.get('offset'), '25', 'the retry must request the failed page again instead of skipping it');
  page.requests[2].finish('Page two retried'); await retry;
  assert.equal(page.title(), 'Page two retried');
  assert.equal(page.node('usage-page').textContent, '60 条记录 · 26–50');
});

test('usage clamps an offset beyond the total and reloads the clamped page', async () => {
  const page = usagePage(), initial = page.show();
  page.requests[0].finish('Initial'); await initial;
  page.node('usage-next').onclick(); const second = page.context.lastAction;
  page.requests[1].finish('Page two'); await second;
  page.node('usage-next').onclick(); const third = page.context.lastAction;
  page.requests[2].finish('Page three'); await third;
  const shrunk = [];
  const textOf = node => [node.textContent, ...(node.children || []).map(textOf)].filter(Boolean).join(' ');
  page.context.fetch = url => new Promise(resolve => {
    const params = new URLSearchParams(url.split('?')[1]);
    shrunk.push({params, finish(total) {
      resolve(response({summary: {tokens: 100, tasks: 1}, roles: [], daily: [],
        tasks: [{task: 'Clamped', title: 'Clamped', project: 'a', state: 'failed', tokens: 100, attempts: 1}],
        total}));
    }});
  });
  page.node('usage-refresh').onclick();
  await waitFor(() => shrunk.length >= 1);
  assert.equal(shrunk[0].params.get('offset'), '50');
  shrunk[0].finish(36);
  await waitFor(() => shrunk.length >= 2 && shrunk[1].params.get('offset') === '25');
  shrunk[1].finish(36);
  await new Promise(setImmediate);
  assert.equal(page.node('usage-page').textContent, '36 条记录 · 26–36');
  assert.equal(textOf(page.node('usage-tasks')).includes('Clamped'), true, 'the clamped page must render its own rows');
  assert.equal(page.node('usage-next').disabled, true, 'next must be disabled after clamping to the last page');
  assert.equal(page.node('usage-prev').disabled, false);
});

test('usage detail roles fall back to the usage role names for triage and acceptance', async () => {
  const page = usagePage(), initial = page.show();
  page.requests[0].finish('Initial'); await initial;
  const textOf = node => [node.textContent, ...(node.children || []).map(textOf)].filter(Boolean).join(' ');
  const details = [];
  page.context.fetch = url => new Promise(resolve => {
    details.push({finish: data => resolve(response(data))});
  });
  page.node('usage-tasks').children[0].children[2].handlers.click();
  const detail = page.context.lastAction;
  await waitFor(() => details.length >= 1);
  details[0].finish({summary: {tokens: 3, tasks: 1}, daily: [], roles: [
    {role: 'triage', provider: 'x', model: 'm', effort: 'high', tokens: 2},
    {role: 'acceptance', provider: 'x', model: 'm', effort: 'high', tokens: 1}],
    calls: [], call_total: 0, total: 0});
  await detail;
  const text = textOf(page.node('usage-detail-content'));
  assert.ok(text.includes('价值初筛'), 'triage must use the usage role name');
  assert.ok(text.includes('验收设计'), 'acceptance must use the usage role name');
});

for (const trigger of ['project', 'period', 'refresh', 'hide']) {
  test(`usage ${trigger} cancels pending search and discards late responses`, async () => {
    const page = usagePage();
    if (trigger === 'refresh') {
      const initial = page.show(); page.requests[0].finish('Initial'); await initial;
      page.requests.length = 0;
      page.node('usage-next').onclick();
    } else page.show();
    const old = page.context.lastAction;
    page.search('new query');
    if (trigger === 'project') {
      vm.runInContext('projectId="b"', page.context);
      page.node('project-select').handlers.change();
    } else if (trigger === 'period') {
      page.node('usage-period').value = '7'; page.node('usage-period').handlers.change();
    } else if (trigger === 'refresh') page.node('usage-refresh').onclick();
    else page.hide();
    const current = page.context.lastAction;
    assert.equal(page.active(350), 0);
    page.requests[0].finish('Stale'); await old;
    assert.equal(page.title(), trigger === 'refresh' ? 'Initial' : undefined);
    if (trigger === 'hide') {
      assert.equal(page.requests.length, 1);
      assert.equal(page.active(30000), 0);
      assert.equal(page.node('usage-refresh').disabled, false);
      vm.runInContext('view="usage"', page.context); page.node('view-usage').hidden = false;
      page.show();
    } else {
      assert.equal(page.node('usage-refresh').disabled, true, 'old finalization cannot enable the current request');
      assert.equal(page.active(30000), 0);
    }
    const latest = trigger === 'hide' ? page.context.lastAction : current;
    assert.equal(page.requests.length, 2);
    assert.equal(page.requests[1].params.get('q'), 'new query');
    assert.equal(page.requests[1].params.get('offset'), '0');
    assert.equal(page.requests[1].params.get('project'), trigger === 'project' ? 'b' : 'a');
    page.requests[1].finish('Current'); await latest;
    assert.equal(page.title(), 'Current');
    assert.equal(page.active(30000), 1);
  });
}

test('leaving usage clears auto refresh and pending detail without reopening the dialog', async () => {
  const page = usagePage(), initial = page.show();
  page.requests[0].finish('Initial'); await initial;
  page.node('usage-tasks').children[0].children[2].handlers.click();
  const detail = page.context.lastAction;
  assert.equal(page.requests[1].params.get('task'), 'Initial');
  page.hide();
  assert.equal(page.active(30000), 0);
  page.requests[1].finish('Old detail'); await detail;
  assert.notEqual(page.node('usage-detail').open, true);
});

test('a current unsupported usage response stops auto refresh', async () => {
  const page = usagePage(), pending = page.show();
  page.requests[0].finish('Unsupported', 404); await pending;
  assert.equal(page.node('usage-refresh').disabled, false);
  assert.equal(page.active(30000), 0);
  assert.match(page.node('usage-tasks').children[0].textContent, /尚不支持用量页面/);
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

function desktopPage() {
  const page = browser();
  const desktop = {checks: 0, downloads: [], states: [], invoke: async (command, args) => {
    if (command === 'check_desktop_update') { desktop.checks++; return desktop.states[Math.min(desktop.checks - 1, desktop.states.length - 1)]; }
    if (command === 'download_desktop_update') { desktop.downloads.push(args || null); return; }
    throw new Error('unexpected desktop command ' + command);
  }};
  let poll = null, intervalId = 0;
  page.context.setInterval = (callback, delay) => { poll = callback; return ++intervalId; };
  page.context.clearInterval = id => { if (id === intervalId) poll = null; };
  page.context.uiConfirm = async () => true;
  page.context.window.RSIDesktop = desktop;
  for (const id of ['update-status', 'update-notes', 'apply-update', 'desktop-update'])
    Object.assign(page.node(id), {after(...children) { this.children.push(...children); }, remove() { this.removed = true; }});
  page.desktop = desktop;
  page.tick = async () => {
    assert.ok(poll, 'desktop update poller is active');
    const callback = poll;
    await callback();
    await new Promise(setImmediate);
  };
  return page;
}

test('reloading during a desktop download resumes polling until the completion toast', async () => {
  const page = desktopPage();
  page.desktop.states = [
    {version: '9.9.9', notes: 'n', downloading: true, percent: 30, opened: false, error: '', error_code: ''},
    {version: '9.9.9', notes: 'n', downloading: true, percent: 80, opened: false, error: '', error_code: ''},
    {version: '9.9.9', notes: 'n', downloading: false, percent: 100, opened: true, error: '', error_code: ''}
  ];
  await vm.runInContext('checkDesktopUpdate()', page.context);
  assert.ok(vm.runInContext('desktopUpdateTimer', page.context), 'a page reload must resume polling for the in-flight download');
  assert.equal(page.node('desktop-update').disabled, true);
  assert.match(String(page.node('desktop-update').children[0]), /正在下载新版本/);
  assert.match(String(page.node('desktop-update').children[0]), /30%/);
  await page.tick();
  assert.match(String(page.node('desktop-update').children[0]), /80%/);
  await page.tick();
  assert.equal(vm.runInContext('desktopUpdateTimer', page.context), null, 'polling stops once the download completed');
  assert.match(page.node('toast').textContent, /下载已完成/);
  assert.equal(page.node('desktop-update').disabled, true, 'the completed download cannot be started again');
  assert.match(String(page.node('desktop-update').children[0]), /安装镜像已打开/);
  await vm.runInContext('startDesktopUpdate()', page.context);
  assert.equal(page.desktop.downloads.length, 0, 'the completed state never re-downloads the image');
});

test('repeated update checks during a download keep one poller and the reported progress', async () => {
  const page = desktopPage();
  page.desktop.states = [{version: '9.9.9', notes: 'n', downloading: true, percent: 40, opened: false, error: '', error_code: ''}];
  await vm.runInContext('checkDesktopUpdate()', page.context);
  const first = vm.runInContext('desktopUpdateTimer', page.context);
  assert.ok(first, 'polling resumes for an in-flight download');
  await vm.runInContext('checkDesktopUpdate(true)', page.context);
  assert.equal(vm.runInContext('desktopUpdateTimer', page.context), first, 'a repeated check never starts a second poller');
  assert.equal(vm.runInContext('desktopUpdate.percent', page.context), 40, 'a repeated check keeps the reported progress');
  assert.equal(page.desktop.checks, 2);
  assert.match(String(page.node('desktop-update').children[0]), /40%/);
});

test('an available desktop update starts polling only after its download begins', async () => {
  const page = desktopPage();
  page.desktop.states = [{version: '9.9.9', notes: 'n', downloading: false, percent: 0, opened: false, error: '', error_code: ''}];
  await vm.runInContext('checkDesktopUpdate()', page.context);
  assert.equal(vm.runInContext('desktopUpdateTimer', page.context), null, 'an available update does not poll');
  assert.equal(page.node('desktop-update').disabled, false);
  await vm.runInContext('startDesktopUpdate()', page.context);
  assert.equal(page.desktop.downloads.length, 1, 'the download is requested once');
  assert.ok(vm.runInContext('desktopUpdateTimer', page.context), 'polling starts once the download begins');
  assert.equal(page.node('desktop-update').disabled, true);
});
