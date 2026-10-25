'use strict';
// 选择英文时先隐藏页面，等 i18n.js 的 translateStatic 完成再显示，避免中文先闪现。
// 内联脚本会被 CSP 拦截，因此这里是独立文件；JS 被禁用时不会隐藏任何内容。
try {
  if (localStorage.getItem('rsi-language') === 'en') {
    document.documentElement.style.visibility = 'hidden';
    // 兜底：i18n.js 加载失败时 3 秒后强制恢复显示，页面绝不因翻译脚本报错而永久空白。
    setTimeout(() => { document.documentElement.style.visibility = ''; }, 3000);
  }
} catch (error) { /* localStorage 不可用时按默认语言渲染。 */ }
