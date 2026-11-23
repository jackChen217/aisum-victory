/* In-app browsers (WeChat, Instagram, ...): Google refuses to sign in inside them
 * (disallowed_useragent). Tell people to open the page in their real browser.
 * Loaded on every page. In a normal browser it does nothing. */
import { linkWithSource } from './source.js';

const APPS = [
  [/MicroMessenger/i, '微信', 'zh'],
  [/xhsdiscover/i, '小红书', 'zh'],
  [/weibo/i, '微博', 'zh'],
  // The QQ app says "QQ/8.9.0". The standalone QQ Browser ("MQQBrowser/", "QQBrowser/")
  // is a real browser and must not match.
  [/\bQQ\/\d/, 'QQ', 'zh'],
  [/Instagram/i, 'Instagram', 'en'],
  [/FBAN|FBAV|FB_IAB/, 'Facebook', 'en'],
  [/musical_ly|BytedanceWebview|TikTok/i, 'TikTok', 'en'],
  [/Snapchat/i, 'Snapchat', 'en'],
  [/LinkedIn/i, 'LinkedIn', 'en'],
  [/\bLine\/\d/, 'Line', 'en'],  // "Line/13.0", never the "line" inside other words
];

/** The in-app browser this User-Agent belongs to: {name, lang}, or null for a normal browser. */
export function detectInApp(ua) {
  for (const [pattern, name, lang] of APPS) {
    if (pattern.test(ua || '')) return { name, lang };
  }
  return null;
}

const TEXT = {
  zh: {
    message: (app) => `Google 登录在${app}里用不了，请点右上角 ··· →「在浏览器打开」`,
    copy: '复制链接',
    copied: '已复制，去浏览器粘贴打开',
    failed: '复制失败，请长按下面的网址复制',
    close: '知道了',
  },
  en: {
    message: (app) => `Google sign-in doesn't work inside ${app}. Tap ··· → Open in external browser.`,
    copy: 'Copy link',
    copied: 'Copied. Paste it in your browser.',
    failed: "Couldn't copy. Press and hold the address below.",
    close: 'Got it',
  },
};

const CSS = `
.inapp-banner { position: sticky; top: 0; z-index: 1000; display: flex; flex-wrap: wrap;
  align-items: center; gap: 8px 12px; padding: 10px 16px; background: #FEF3C7; color: #78350F;
  border-bottom: 1px solid #FDE68A; font: 500 14px/1.5 system-ui, sans-serif; }
.inapp-banner p, .inapp-dialog p { margin: 0; }
.inapp-banner .inapp-message { flex: 1 1 220px; }
.inapp-copy { flex: none; padding: 6px 12px; border: 1px solid #92400E; border-radius: 8px;
  background: #FFFFFF; color: #78350F; font: 600 14px/1.4 system-ui, sans-serif; cursor: pointer; }
.inapp-status { flex: 1 0 100%; font-size: 13px; overflow-wrap: anywhere; }
.inapp-overlay { position: fixed; inset: 0; z-index: 1001; display: flex; align-items: center;
  justify-content: center; padding: 16px; background: rgba(15, 23, 42, .5); }
.inapp-dialog { display: flex; flex-direction: column; gap: 14px; width: 100%; max-width: 360px;
  padding: 24px; border-radius: 16px; background: #FFFFFF; color: #0F172A;
  font: 500 16px/1.5 system-ui, sans-serif; }
.inapp-actions { display: flex; gap: 8px; justify-content: flex-end; }
.inapp-close { padding: 6px 12px; border: 0; background: transparent; color: #475569;
  font: 600 14px/1.4 system-ui, sans-serif; cursor: pointer; }
`;

// Clipboard access is often refused inside these apps, so fall back to the old way.
async function copy(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    const area = document.createElement('textarea');
    area.value = text;
    area.setAttribute('readonly', '');
    area.style.cssText = 'position:fixed;top:0;left:-9999px';
    document.body.append(area);
    area.select();
    area.setSelectionRange(0, text.length);
    let ok = false;
    try { ok = document.execCommand('copy'); } catch { ok = false; }
    area.remove();
    return ok;
  }
}

function make(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text) node.textContent = text;
  return node;
}

function copyControls(words) {
  const button = make('button', 'inapp-copy', words.copy);
  button.type = 'button';
  const status = make('p', 'inapp-status');
  status.setAttribute('role', 'status');
  status.hidden = true;
  button.addEventListener('click', async () => {
    // Keep ?src=finch in the copied link, so the real browser records the source too.
    let storage = null;
    try { storage = window.localStorage; } catch { /* blocked: copy the page as is */ }
    const link = linkWithSource(location.href, storage);
    const ok = await copy(link);
    status.textContent = ok ? words.copied : `${words.failed} ${link}`;
    status.hidden = false;
  });
  return { button, status };
}

function showGuide(app, words) {
  if (document.querySelector('.inapp-overlay')) return;
  const overlay = make('div', 'inapp-overlay');
  const dialog = make('div', 'inapp-dialog');
  dialog.setAttribute('role', 'dialog');
  dialog.setAttribute('aria-modal', 'true');
  const message = make('p', '', words.message(app.name));
  dialog.setAttribute('aria-label', message.textContent);
  const { button, status } = copyControls(words);
  const close = make('button', 'inapp-close', words.close);
  close.type = 'button';
  const actions = make('div', 'inapp-actions');
  actions.append(close, button);
  dialog.append(message, status, actions);
  overlay.append(dialog);
  document.body.append(overlay);
  const opener = document.getElementById('gate-signin');
  const dismiss = () => { overlay.remove(); opener?.focus(); };
  close.addEventListener('click', dismiss);
  overlay.addEventListener('click', (e) => { if (e.target === overlay) dismiss(); });
  overlay.addEventListener('keydown', (e) => { if (e.key === 'Escape') dismiss(); });
  button.focus();
}

function start(app) {
  const words = TEXT[app.lang];
  document.head.append(make('style', '', CSS));
  const banner = make('div', 'inapp-banner');
  banner.append(make('p', 'inapp-message', words.message(app.name)));
  const { button, status } = copyControls(words);
  banner.append(button, status);
  document.body.prepend(banner);

  // Capture on document so auth.js's own listener on the button never runs.
  document.addEventListener('click', (e) => {
    if (!(e.target instanceof Element) || !e.target.closest('#gate-signin')) return;
    e.preventDefault();
    e.stopPropagation();
    showGuide(app, words);
  }, true);
}

if (typeof document !== 'undefined') {
  const app = detectInApp(navigator.userAgent);
  if (app) start(app);
}
