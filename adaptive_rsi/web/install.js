"use strict";
let installPrompt;
const installButton = document.getElementById("install-app"),
  installStatus = document.getElementById("install-status");
function installCopy() {
  document.getElementById("origin-note").textContent =
    location.protocol === "https:"
      ? t("当前安装地址：") + location.origin + "/install"
      : t(
          "当前是本机地址。请先配置 HTTPS，再把 HTTPS 安装地址发到自己的手机。",
        );
}
document.getElementById("language-select").onchange = (event) => {
  language = event.target.value;
  try { localStorage.setItem("rsi-language", language); } catch {}
  translateStatic();
  installCopy();
};
window.addEventListener("beforeinstallprompt", (event) => {
  event.preventDefault();
  installPrompt = event;
  installButton.hidden = false;
});
installButton.onclick = async () => {
  if (!installPrompt) return;
  await installPrompt.prompt();
  await installPrompt.userChoice;
  installPrompt = null;
  installButton.hidden = true;
};
window.addEventListener("appinstalled", () => {
  installStatus.textContent = t("已安装，可以从主屏幕打开。");
  installButton.hidden = true;
});
if ("serviceWorker" in navigator && window.isSecureContext)
  navigator.serviceWorker.register("/sw.js").catch(() => {
    installStatus.textContent = t("请使用浏览器菜单添加到主屏幕。");
  });
installCopy();
