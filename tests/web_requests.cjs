const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../adaptive_rsi/web/app.js'), 'utf8');
const authSource = fs.readFileSync(path.join(__dirname, '../adaptive_rsi/web/auth.js'), 'utf8');
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

test('the live run stage marks an in-progress review before its stage row exists', () => {
  const page = browser();
  assert.equal(vm.runInContext('runningStage({state:"evaluating",stage:"review"})', page.context), '正在评审改动');
  assert.equal(vm.runInContext('runningStage({state:"evaluating",last_stage:"review"})', page.context), '正在评审改动');
});
