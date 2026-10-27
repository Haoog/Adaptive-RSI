const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const { createInterface } = require("node:readline");
const { chromium } = require("playwright");

// 更新器原因文案回归：真实 Chromium + 暂停的临时项目，零模型调用、零 GitHub 调用。
// 通过路由拦截 /api/state 注入 update 载荷，断言中文界面显示可操作的中文指引、
// 英文界面保留英文原文；覆盖检查失败、回滚、恢复模式（recovery_required）、
// 无发布包的动态 GitHub HTTP 码，以及未识别原因保留原文的前缀处理。

const cases = [
  { update: { state: "error", reason: "Unable to verify the official release feed" },
    zh: "无法访问 GitHub 官方发布源", en: "Unable to verify the official release feed" },
  { update: { state: "rolled_back", reason: "Previous code and database restored and health verified" },
    zh: "已恢复旧版本：代码和数据库均已还原", en: "Previous code and database restored and health verified" },
  { update: { state: "recovery_required", reason: "Recovery backup is missing or corrupt; refusing an unsafe database rollback" },
    zh: "已拒绝回滚数据库", en: "Recovery backup is missing or corrupt; refusing an unsafe database rollback" },
  { update: { state: "no_release", reason: "GitHub HTTP 404" },
    zh: "无法访问 GitHub 发布接口（HTTP 404）", en: "GitHub HTTP 404" },
  { update: { state: "error", reason: "<urlopen error request timed out>" },
    zh: "更新失败原因（原文）：", en: "<urlopen error request timed out>" },
];

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
    // 真实服务器仍提供认证、项目与状态骨架；只替换 update 字段。
    let payload = null;
    const injectUpdate = async (route) => {
      const response = await route.fetch();
      const data = await response.json();
      if (payload) data.update = payload;
      await route.fulfill({ json: data });
    };
    // 软件更新状态位于「通用设置 → 更多运行选项」面板内，先进入设置视图再断言。
    for (const [language, key] of [["zh-CN", "zh"], ["en", "en"]]) {
      const context = await browser.newContext();
      if (language === "en") await context.addInitScript(() => localStorage.setItem("rsi-language", "en"));
      await context.route("**/api/state*", injectUpdate);
      const page = await context.newPage();
      for (const item of cases) {
        payload = { ...item.update, busy: false };
        await page.goto(url, { waitUntil: "load" });
        await page.waitForFunction(() => Boolean(state), null, { timeout: 15000 });
        await page.evaluate(() => showView("settings"));
        await page.click("#advanced-settings > summary");
        await page.locator("#update-status").filter({ hasText: item[key] }).waitFor({ timeout: 8000 })
          .catch(async () => {
            throw new Error(language + " UI never showed " + JSON.stringify(item[key]) + "; got: " +
              JSON.stringify(await page.locator("#update-status").textContent()));
          });
        console.log("  ok: " + language + " " + item.update.state + " -> " + item[key]);
      }
      await context.close();
    }
    console.log("Updater reason browser regressions passed: real Chromium, paused projects, zero model calls.");
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
