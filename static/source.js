// Where a visitor came from (?src=finch), kept across the Google sign-in popup and any
// in-page navigation that drops the query. Pure functions so Node can test them
// (tests/test_finch_task.py); the page passes window.localStorage and location.search.
// Storage can be missing or throw (private mode, blocked site data): every access is
// wrapped, and the source found in the URL is still returned for this page load.

export const SOURCES = ['finch'];
export const KEY = 'gc_src';
export const sentKey = (uid) => `gc_src_sent:${uid}`;

export function sourceFromUrl(search) {
  const s = new URLSearchParams(search || '').get('src');
  return SOURCES.includes(s) ? s : null;
}

// The URL's source wins (and is stored); otherwise the one stored earlier, if any.
export function rememberSource(search, storage) {
  const fromUrl = sourceFromUrl(search);
  try {
    if (fromUrl) storage.setItem(KEY, fromUrl);
    const stored = fromUrl || storage.getItem(KEY);
    return SOURCES.includes(stored) ? stored : null;
  } catch {
    return fromUrl;
  }
}

export function wasSent(storage, uid) {
  try { return storage.getItem(sentKey(uid)) === '1'; } catch { return false; }
}

export function markSent(storage, uid) {
  try { storage.setItem(sentKey(uid), '1'); } catch { /* sent again next load: harmless */ }
}

// The page's address with ?src= added when this browser remembers a source and the
// address doesn't carry one (inapp.js copies it, so the real browser gets it too).
export function linkWithSource(href, storage) {
  const url = new URL(href);
  if (sourceFromUrl(url.search)) return url.href;
  let stored = null;
  try { stored = storage.getItem(KEY); } catch { /* no storage: copy the page as is */ }
  if (SOURCES.includes(stored)) url.searchParams.set('src', stored);
  return url.href;
}
