const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const { createInterface } = require("node:readline");
const { chromium } = require("playwright");

// 系统通知回归：真实 Chromium + 暂停的临时项目，零模型调用。
// 覆盖：S9 多标签页同一状态变化只弹一次；S11 批量中单条构造失败不吞掉同批
// 其他提醒、seen 只记成功、恢复后不重弹；S12 超过 5000 条裁剪后零变化不重弹；
// iOS 型构造器恒抛错时一次性提示且不风暴、恢复后继续工作。

const MOCK_INIT = `
window.__notifications = [];
class FakeNotification {
  static permission = 'granted';
  static async requestPermission() { return 'granted'; }
  constructor(title, options = {}) {
    this.title = title; this.body = options.body; this.tag = options.tag;
    window.__notifications.push({title, body: options.body, tag: options.tag});
  }
  close() {}
}
window.Notification = FakeNotification;
const now = Date.now.bind(Date); window.__timeOffset = 0; Date.now = () => now() + window.__timeOffset;
`;

const PARTIAL_INIT = `
window.__notifications = [];
window.__throwTag = null;
class PartialNotification {
  static permission = 'granted';
  static async requestPermission() { return 'granted'; }
  constructor(title, options = {}) {
    if (window.__throwTag && options.tag === window.__throwTag) throw new TypeError('simulated single-item display failure');
    window.__notifications.push({title, body: options.body, tag: options.tag});
  }
  close() {}
}
window.Notification = PartialNotification;
const now = Date.now.bind(Date); window.__timeOffset = 0; Date.now = () => now() + window.__timeOffset;
`;

const ALWAYS_THROW_INIT = `
window.__notifications = [];
class BrokenNotification {
  static permission = 'granted';
  static async requestPermission() { return 'granted'; }
  constructor() { throw new TypeError('Notification constructor unavailable in iOS PWA standalone mode'); }
}
window.Notification = BrokenNotification;
const now = Date.now.bind(Date); window.__timeOffset = 0; Date.now = () => now() + window.__timeOffset;
`;

function notificationRow(id, project, run, result, state = "failed") {
  return {
    id, project, title: "通知回归任务 " + id, state,
    display_state: state, updated: 1, run, result: JSON.stringify(result),
  };
}

// 与 experience.js pollInbox 的 signature 完全一致，供 S12 预置 seen 使用。
function signatureOf(run, state, result) {
  return JSON.stringify([
    run, state,
    result.failure?.code ?? null, result.failure?.detail ?? null,
    result.reason ?? null, result.feedback?.state ?? null,
    result.feedback?.head ?? null, result.automation?.release?.state ?? null,
    result.automation?.deploy?.state ?? null,
  ]);
}

async function waitPaused(page) {
  await page.locator("#project-status").filter({ hasText: "已暂停" }).waitFor();
  // 首次加载会完成一次 attention 轮询（此时通知开关未开启，只做 seen 预置）。
  await page.waitForTimeout(800);
}

async function forcePoll(page, label) {
  const response = page.waitForResponse(
    (route) => new URL(route.url()).pathname === "/api/attention",
  );
  await page.evaluate(() => { window.__timeOffset += 20000; window.RSIExperience.refreshed(); });
  await response;
  if (label) console.log("  polled: " + label);
}

async function expectNotificationCount(page, count, label) {
  await page
    .waitForFunction((n) => window.__notifications.length === n, count, { timeout: 8000 })
    .catch(async () => {
      throw new Error(label + ": expected " + count + " notification(s), got " +
        JSON.stringify(await page.evaluate(() => window.__notifications)) );
    });
  console.log("  ok: " + label + " -> " + count + " notification(s)");
}

async function spyToast(page) {
  await page.evaluate(() => {
    const original = window.toast;
    window.__toastLog = [];
    window.toast = (...args) => { window.__toastLog.push(String(args[0])); return original(...args); };
  });
}

// forcePoll 只等到响应头；seen 的落盘在同一次同步续体里完成，
// 这里用 waitForFunction 确保落盘可见后再做跨页面断言。
async function waitForSeen(page, match, label) {
  await page.waitForFunction((probe) => {
    const key = Object.keys(localStorage).find((item) =>
      item.startsWith("rsi-notifications:" + location.origin + ":seen:"));
    return !!key && localStorage.getItem(key).includes(probe);
  }, match, { timeout: 8000 }).catch(async () => {
    throw new Error(label + ": seen never contained " + match +
      "; keys=" + JSON.stringify(await page.evaluate(() => Object.keys(localStorage))));
  });
  console.log("  ok: seen contains " + label);
}

function seenPayload(page) {
  // seen 键带有 instance 后缀：rsi-notifications:<origin>:seen:<instance>
  return page.evaluate(() => {
    const key = Object.keys(localStorage).find((item) =>
      item.startsWith("rsi-notifications:" + location.origin + ":seen:"));
    const stored = key && localStorage.getItem(key);
    return stored ? JSON.parse(stored) : null;
  });
}

function seenKeyPrefix(page) {
  return page.evaluate(() =>
    Object.keys(localStorage).find((key) =>
      key.startsWith("rsi-notifications:" + location.origin + ":seen:")));
}

async function checkMultiTabDuplication(newScenario, url, projectId, payload) {
  payload.notifications = [];
  const context = await newScenario(MOCK_INIT);
  const page1 = await context.newPage();
  const page2 = await context.newPage();
  page1.__errors = []; page2.__errors = [];
  for (const page of [page1, page2]) page.on("pageerror", (error) => page.__errors.push(error.message));
  await page1.goto(url);
  await waitPaused(page1);
  await page2.goto(url);
  await waitPaused(page2);
  const origin = new URL(url).origin;
  await page1.evaluate((key) => localStorage.setItem(key, "true"), "rsi-notifications:" + origin);
  // 项目甲的失败原因变化：page1 先弹，page2 紧随轮询到同一状态变化。
  payload.notifications = [notificationRow("notif-tab", projectId, "run-tab",
    { failure: { code: "checks", detail: "multi-tab v2" }, reason: "多标签页回归" })];
  await forcePoll(page1);
  await expectNotificationCount(page1, 1, "page1 notifies once for the state change");
  await waitForSeen(page1, "multi-tab v2", "page1 persisted the new signature");
  await forcePoll(page2);
  await expectNotificationCount(page2, 0, "page2 must stay silent for the same state change");
  const persisted = await seenPayload(page2);
  assert.equal(persisted["notif-tab"], signatureOf("run-tab", "failed",
    { failure: { code: "checks", detail: "multi-tab v2" }, reason: "多标签页回归" }),
    "shared seen keeps the signature page1 recorded");
  assert.deepEqual(page1.__errors, []);
  assert.deepEqual(page2.__errors, []);
  await page1.close();
  await page2.close();
  console.log("S9 multi-tab dedup passed: one tab alerts, sibling tab sharing the profile stays silent.");
}

async function checkBatchErrorIsolation(newScenario, url, projectId, payload) {
  // 基线批次在页面加载时静默写入 seen；随后三条同时变化，第一条的构造器抛错。
  payload.notifications = [
    notificationRow("notif-a", projectId, "run-a",
      { failure: { code: "checks", detail: "batch v1" }, reason: "批量基线" }),
    notificationRow("notif-b", projectId, "run-b", { reason: "裁剪基线乙" }, "expired"),
    notificationRow("notif-c", projectId, "run-c", { reason: "批量基线丙" }, "awaiting_approval"),
  ];
  const context = await newScenario(PARTIAL_INIT);
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto(url);
  await waitPaused(page);
  const origin = new URL(url).origin;
  await page.evaluate((key) => localStorage.setItem(key, "true"), "rsi-notifications:" + origin);
  await spyToast(page);
  // 三条同时变化，第一条的构造器抛错：其余两条必须照常弹出。
  payload.notifications = [
    notificationRow("notif-a", projectId, "run-a",
      { failure: { code: "checks", detail: "batch v2" }, reason: "批量回归甲" }),
    notificationRow("notif-b", projectId, "run-b",
      { reason: "批量回归乙" }, "expired"),
    notificationRow("notif-c", projectId, "run-c",
      { reason: "批量回归丙" }, "awaiting_approval"),
  ];
  await page.evaluate((tag) => { window.__throwTag = tag; }, "notif-a");
  await forcePoll(page, "batch with first item throwing");
  await expectNotificationCount(page, 2, "remaining two items still notify when one constructor throws");
  const tagsAfterThrow = await page.evaluate(() => window.__notifications.map((n) => n.tag));
  assert.deepEqual(tagsAfterThrow, ["notif-b", "notif-c"]);
  const seenAfterThrow = await seenPayload(page);
  assert.equal(seenAfterThrow["notif-b"], signatureOf("run-b", "expired", { reason: "批量回归乙" }),
    "seen records only the displayed item");
  assert.equal(seenAfterThrow["notif-c"], signatureOf("run-c", "awaiting_approval", { reason: "批量回归丙" }),
    "seen records only the displayed item");
  assert.equal(seenAfterThrow["notif-a"], signatureOf("run-a", "failed",
    { failure: { code: "checks", detail: "batch v1" }, reason: "批量基线" }),
    "failed item keeps its previous signature so the next round retries");
  const toasts = await page.evaluate(() => window.__toastLog);
  assert.equal(toasts.length, 1, "exactly one hint toast for the failure");
  assert.match(toasts[0], /系统通知暂时无法显示/);
  // 构造器恢复后：只补发失败的那一条，已成功的两条不重弹。
  await page.evaluate(() => { window.__throwTag = null; });
  await forcePoll(page, "constructor recovered");
  await expectNotificationCount(page, 3, "only the previously failed item re-notifies after recovery");
  assert.equal((await page.evaluate(() => window.__notifications.at(-1))).tag, "notif-a");
  assert.equal((await page.evaluate(() => window.__toastLog)).length, 1, "no extra toast after recovery");
  await forcePoll(page, "steady state");
  await expectNotificationCount(page, 3, "successful items are not re-alerted on the next round");
  assert.deepEqual(errors, []);
  await page.close();
  console.log("S11 per-item isolation passed: one throwing item skips itself, the batch continues, seen records successes only, recovery notifies once.");
}

function seenKeyPrefix(page) {
  return page.evaluate(() =>
    Object.keys(localStorage).find((key) =>
      key.startsWith("rsi-notifications:" + location.origin + ":seen:")));
}

// seen 键携带服务器 instance 后缀；先在一次性上下文里触发一次轮询取得它，
// 才能在应用脚本运行前预置 5000 条记录复现裁剪场景。
async function discoverInstance(newScenario, url) {
  const context = await newScenario(null);
  const page = await context.newPage();
  await page.goto(url);
  await waitPaused(page);
  const prefix = await seenKeyPrefix(page);
  assert.ok(prefix, "instance-scoped seen key exists after the first poll");
  await context.close();
  return prefix.slice("rsi-notifications:".length + new URL(url).origin.length + ":seen:".length);
}

async function checkEvictionTrim(newScenario, url, projectId, payload) {
  const ids = ["notif-1", "notif-2", "notif-3", "notif-4", "notif-5", "notif-6"];
  const rows = ids.map((id, index) => notificationRow(id, projectId, "run-" + id,
    { reason: "裁剪基线 " + index }, index % 2 ? "expired" : "failed"));
  payload.notifications = rows;
  const instance = await discoverInstance(newScenario, url);
  const sigMap = Object.fromEntries(rows.map((row) => [row.id,
    signatureOf(row.run, row.display_state, JSON.parse(row.result))]));
  const context = await newScenario(MOCK_INIT);
  // 在应用脚本运行前写入 seen：真实条目在最前（最旧插入序），5000 条填充记录随后，
  // 通知开关已开启。首次加载即为"服务器零变化"的裁剪轮询。
  await context.addInitScript(([key, seeded, flagKey]) => {
    localStorage.setItem(key, JSON.stringify(seeded));
    localStorage.setItem(flagKey, "true");
  }, ["rsi-notifications:" + new URL(url).origin + ":seen:" + instance,
      { ...sigMap, ...Object.fromEntries(Array.from({ length: 5000 }, (_, i) => ["fake" + i, "[]"])) },
      "rsi-notifications:" + new URL(url).origin]);
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto(url);
  await waitPaused(page);
  await forcePoll(page, "verification poll (server unchanged)");
  await expectNotificationCount(page, 0, "zero-change state must not resurrect after the 5000-entry trim");
  const seenAfterTrim = await seenPayload(page);
  for (const id of ids)
    assert.ok(seenAfterTrim[id], id + " survives the trim while active");
  assert.equal(Object.keys(seenAfterTrim).length, 5000, "seen stays bounded at 5000 entries");
  assert.deepEqual(errors, []);
  await page.close();
  await context.close();
  console.log("S12 trim passed: active attention entries survive the 5000-entry bound without re-alerting.");
}

async function checkConstructorUnavailable(newScenario, url, projectId, payload) {
  // 基线在页面加载时静默写入 seen，随后同一条目变化而构造器恒抛错。
  payload.notifications = [notificationRow("notif-ios", projectId, "run-ios",
    { failure: { code: "checks", detail: "ios v1" }, reason: "iOS 基线" })];
  const context = await newScenario(ALWAYS_THROW_INIT);
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto(url);
  await waitPaused(page);
  const origin = new URL(url).origin;
  await page.evaluate((key) => localStorage.setItem(key, "true"), "rsi-notifications:" + origin);
  await spyToast(page);
  payload.notifications = [notificationRow("notif-ios", projectId, "run-ios",
    { failure: { code: "checks", detail: "ios v2" }, reason: "iOS 回归" })];
  await forcePoll(page, "constructor always throws");
  await expectNotificationCount(page, 0, "constructor failure shows no notification");
  const toasts = await page.evaluate(() => window.__toastLog);
  assert.equal(toasts.length, 1, "exactly one hint instead of silent failure");
  assert.match(toasts[0], /系统通知暂时无法显示/);
  const seenAfterFailure = await seenPayload(page);
  assert.notEqual(seenAfterFailure["notif-ios"], signatureOf("run-ios", "failed",
    { failure: { code: "checks", detail: "ios v2" }, reason: "iOS 回归" }),
    "failed display must not be recorded as seen");
  // 构造器恢复（例如用户从主屏幕重新打开）：同一提醒补发，不永久静默。
  await page.evaluate(() => {
    window.__notifications = [];
    window.Notification = class {
      static permission = "granted";
      constructor(title, options = {}) { window.__notifications.push({ title, body: options.body, tag: options.tag }); }
      close() {}
    };
  });
  await forcePoll(page, "constructor recovered");
  await expectNotificationCount(page, 1, "recovered constructor delivers the pending reminder");
  await forcePoll(page, "steady state after recovery");
  await expectNotificationCount(page, 1, "no repeat after the recovered delivery");
  assert.equal((await page.evaluate(() => window.__toastLog)).length, 1, "still exactly one hint");
  assert.deepEqual(errors, []);
  await page.close();
  console.log("iOS-style constructor passed: one-shot hint without storm, recovery is not permanently silenced.");
}

(async () => {
  const server = spawn(process.env.PYTHON || "python3", ["-m", "tests.browser_fixture"],
    { stdio: ["pipe", "pipe", "pipe"] });
  server.stderr.on("data", (chunk) => process.stderr.write(chunk));
  const output = createInterface({ input: server.stdout });
  let browser;
  try {
    const url = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(Error("Fixture timed out")), 60000);
      output.once("line", (line) => { clearTimeout(timer); resolve(line); });
      server.once("exit", (code) => reject(Error("Fixture exited " + code)));
    });
    browser = await chromium.launch(
      process.env.RSI_BROWSER_CHANNEL ? { channel: process.env.RSI_BROWSER_CHANNEL } : {},
    );
    // 通过路由注入稳定的 attention 载荷；真实服务器仍提供 instance、认证与暂停项目。
    const payload = { notifications: [] };
    const injectAttention = async (route) => {
      const response = await route.fetch();
      const data = await response.json();
      data.notifications = payload.notifications;
      await route.fulfill({ json: data });
    };
    const newScenario = async (init) => {
      const context = await browser.newContext();
      if (init) await context.addInitScript(init);
      await context.route("**/api/attention", injectAttention);
      return context;
    };
    const probeContext = await browser.newContext();
    const probePage = await probeContext.newPage();
    await probePage.goto(url);
    await probePage.locator("#project-status").filter({ hasText: "已暂停" }).waitFor();
    const projectId = await probePage.evaluate(() => projectId);
    await probeContext.close();
    // RSI_NOTIFICATION_ONLY=<名称> 可单独运行一个场景，便于排查。
    const only = process.env.RSI_NOTIFICATION_ONLY;
    const scenarios = [
      ["multi-tab", checkMultiTabDuplication],
      ["batch-isolation", checkBatchErrorIsolation],
      ["trim", checkEvictionTrim],
      ["constructor-unavailable", checkConstructorUnavailable],
    ];
    for (const [name, check] of scenarios)
      if (!only || only === name) await check(newScenario, url, projectId, payload);
    console.log("Notification browser regressions passed: real Chromium, paused projects, zero model calls.");
  } finally {
    if (browser) await browser.close();
    if (server.exitCode === null && server.signalCode === null) {
      const ended = new Promise((resolve) => server.once("exit", resolve));
      server.stdin.end("\n");
      const timeout = setTimeout(() => server.kill("SIGTERM"), 5000);
      await ended;
      clearTimeout(timeout);
    }
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
