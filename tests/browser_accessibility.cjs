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
      await page.locator('#language-select').selectOption(locale);
      for (const width of [1440, 390]) {
        await page.setViewportSize({width, height: 900});
        await audit(page);
        await page.locator('#project-settings').click();
        await audit(page);
        await page.locator('#back-workspace').click();
      }
      await page.locator('#language-select').selectOption('zh-CN');
    }
    console.log('Accessible names, labels, ARIA references and page language passed at desktop/mobile sizes in both languages.');
  } finally {
    if (browser) await browser.close();
    server.stdin.end('\n');
    await new Promise(resolve => server.once('exit', resolve));
  }
}
main().catch(error => {console.error(error); process.exitCode = 1;});
