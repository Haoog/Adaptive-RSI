const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const {createInterface} = require('node:readline');
const {chromium} = require('playwright');

async function audit(page) {
  const problems = await page.evaluate(() => {
    const visible = node => node.getClientRects().length && !node.closest('[hidden], [inert]');
    const named = node => Boolean(node.getAttribute('aria-label')?.trim() ||
      node.getAttribute('aria-labelledby')?.split(/\s+/).some(id => document.getElementById(id)?.textContent.trim()) ||
      node.labels?.length || node.textContent.trim() || node.getAttribute('title')?.trim() ||
      (node.tagName === 'INPUT' && ['submit', 'button'].includes(node.type) && node.value));
    const errors = [];
    const ids = new Set();
    for (const node of document.querySelectorAll('[id]')) {
      if (ids.has(node.id)) errors.push('Duplicate id: ' + node.id);
      ids.add(node.id);
    }
    for (const node of document.querySelectorAll('button,a[href],input:not([type=hidden]),select,textarea')) {
      if (visible(node) && !named(node)) errors.push('Unnamed control: ' + node.tagName + '#' + node.id);
    }
    for (const node of document.querySelectorAll('[aria-labelledby],[aria-describedby]')) {
      for (const attr of ['aria-labelledby', 'aria-describedby']) {
        for (const id of (node.getAttribute(attr) || '').split(/\s+/).filter(Boolean)) {
          if (!document.getElementById(id)) errors.push('Missing ARIA target: ' + id);
        }
      }
    }
    if (!document.documentElement.lang) errors.push('Missing page language');
    return errors;
  });
  assert.deepEqual(problems, []);
}

async function main() {
  const server = spawn(process.env.PYTHON || 'python3', ['-m', 'tests.browser_fixture'], {
    stdio: ['pipe', 'pipe', 'pipe'], env: {...process.env, PYTHONDONTWRITEBYTECODE: '1'}
  });
  server.stderr.on('data', chunk => process.stderr.write(chunk));
  let browser;
  try {
    const url = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(Error('Console startup timed out')), 30000);
      createInterface({input: server.stdout}).once('line', line => {clearTimeout(timer); resolve(line);});
      server.once('exit', code => {clearTimeout(timer); reject(Error('Console exited: ' + code));});
    });
    browser = await chromium.launch(process.env.RSI_BROWSER_CHANNEL ? {channel: process.env.RSI_BROWSER_CHANNEL} : {});
    const page = await browser.newPage();
    for (const locale of ['zh-CN', 'en']) {
      await page.goto(url);
      await page.locator('#project-status').filter({hasText: '已暂停'}).waitFor();
      const style = await page.request.get(new URL('/workspace.css', url).href);
      assert.equal(style.status(), 200, 'authenticated redesign stylesheet is served');
      assert.match(style.headers()['content-type'], /text\/css/);
      await page.waitForFunction(() => / · \d+$/.test(document.getElementById('attention-inbox')?.textContent || ''));
      const inboxCount = (await page.locator('#attention-inbox').textContent()).match(/ · \d+$/)[0];
      await page.locator('#language-select').selectOption(locale);
      if (locale === 'en') {
        assert.equal(await page.locator('#archive-project').textContent(), 'Archive current project');
        assert.equal(await page.locator('#attention-inbox').textContent(), 'Attention inbox' + inboxCount);
      }
      for (const width of [1440, 390]) {
        await page.setViewportSize({width, height: 900});
        await audit(page);
        await page.locator('#project-settings').click();
        await audit(page);
        await page.locator('#back-workspace').click();
        for (const colorScheme of ['light', 'dark']) {
          await page.emulateMedia({colorScheme});
          assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1),
            'workspace fits the viewport in both themes and languages');
        }
      }
      await page.locator('#language-select').selectOption('zh-CN');
    }
    await page.setViewportSize({width: 1440, height: 1000});
    await page.locator('[data-task-filter="active"]').click();
    const layout = await page.evaluate(() => {
      const row = document.querySelector('.task-row');
      const title = row.querySelector('.task-title').getBoundingClientRect();
      const checkbox = row.querySelector('input[type=checkbox]').getBoundingClientRect();
      const actions = row.querySelector('.task-actions').getBoundingClientRect();
      const sidebar = document.querySelector('.sidebar').getBoundingClientRect();
      const picker = document.querySelector('#project-select').getBoundingClientRect();
      return {titleX:title.x, checkX:checkbox.x, actionsY:actions.y, rowY:row.getBoundingClientRect().y,
        titleWidth:title.width, rowWidth:row.getBoundingClientRect().width, pickerRight:picker.right,
        sidebarRight:sidebar.right, unselectedActions:[...document.querySelectorAll('#batch-actions button')]
          .filter(button=>button.disabled && button.getClientRects().length).length};
    });
    assert(layout.titleX > layout.checkX, 'task selection precedes the title');
    assert(layout.titleWidth < layout.rowWidth * 0.8, 'task title has a bounded reading column');
    assert(layout.actionsY - layout.rowY < 40, 'task actions align with the title row');
    assert(layout.pickerRight < layout.sidebarRight, 'long project paths stay inside the sidebar');
    assert.equal(layout.unselectedActions, 0, 'unused bulk actions stay out of the default view');
    assert.equal(await page.locator('#batch-actions .muted').isVisible(), false,
      'an empty selection does not show a zero-count toolbar');
    await page.locator('.task-row input[type=checkbox]').first().check();
    assert(await page.getByRole('button', {name:'合并任务', exact:true}).isVisible(),
      'selecting a task reveals its batch actions');
    await page.setViewportSize({width:390, height:1000});
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
      'selected batch actions fit the mobile viewport');
    await page.locator('.task-row .task-title').first().click();
    const dependencies = page.locator('#detail-dialog select[multiple]');
    await dependencies.waitFor({state:'attached'});
    assert.equal(await dependencies.isVisible(),false,'dependency editing stays out of the initial task reading flow');
    await page.locator('.task-queue-editor > summary').click();
    for (const width of [1440, 390]) {
      await page.setViewportSize({width, height:1000});
      const options = await dependencies.locator('option').evaluateAll(nodes => nodes.slice(0, 2).map(node => {
        const box = node.getBoundingClientRect();return {top:box.top,bottom:box.bottom,width:box.width};
      }));
      assert.equal(options.length, 2);
      assert(options[1].top >= options[0].bottom, 'dependency choices form a vertical list without overlap');
      assert(options.every(option => option.width > 100), 'dependency labels have readable width');
    }
    console.log('Accessible names, labels, ARIA references and page language passed at desktop/mobile sizes in both languages.');
  } finally {
    if (browser) await browser.close();
    server.stdin.end('\n');
    await new Promise(resolve => server.once('exit', resolve));
  }
}
main().catch(error => {console.error(error); process.exitCode = 1;});
