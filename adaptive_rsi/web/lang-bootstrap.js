'use strict';
// 选择英文时先隐藏页面，等 i18n.js 的 translateStatic 完成再显示，避免中文先闪现。
// 内联脚本会被 CSP 拦截，因此这里是独立文件；JS 被禁用时不会隐藏任何内容。
try {
  if (localStorage.getItem('rsi-language') === 'en') {
    document.documentElement.style.visibility = 'hidden';
  }
} catch (error) { /* localStorage 不可用时按默认语言渲染。 */ }
