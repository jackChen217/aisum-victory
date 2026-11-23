// GhostCart page: /api/upload, /api/plan?format=ui, /api/replan?format=ui.
// Prices, names and links are shown as the API returns them.
import { cartMarks, spreadDots } from '/static/resultmarks.js';
import { driver } from '/static/vendor/driver-1.14.0.mjs';
import { markSent, rememberSource, wasSent } from '/static/source.js';

const $ = (id) => document.getElementById(id);

function el(tag, props = {}, ...kids) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (v === undefined || v === null) continue;
    if (k === 'class') node.className = v;
    else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
    else if (k in node && k !== 'list') node[k] = v;
    else node.setAttribute(k, v);
  }
  node.append(...kids.filter((x) => x !== null && x !== undefined));
  return node;
}

const money = (n) => '$' + Number(n).toLocaleString('en-US',
  { minimumFractionDigits: Number(n) % 1 ? 2 : 0, maximumFractionDigits: 2 });
const safeLink = (u) => (/^https?:\/\//.test(u || '') ? u : null);

// Gallery photos are public Unsplash images; the looks are listed in static/gallery.json.
// A look's photo is on Unsplash (default) or Pexels (look.source === 'pexels').
const photo = (look, w) => (look.source === 'pexels'
  ? `https://images.pexels.com/photos/${look.photo}/pexels-photo-${look.photo}.jpeg?auto=compress&cs=tinysrgb&w=${w}`
  : `https://images.unsplash.com/${look.photo}?w=${w}&q=85&fm=jpg`);
const SITE_NAME = (look) => (look.source === 'pexels' ? 'Pexels' : 'Unsplash');

const GENDER_LABEL = { F: 'Women', M: 'Men', UNISEX: 'Unisex' };
const STATUS_TEXT = { over_budget: 'over budget', owned: 'you already own this',
                      no_candidates: 'no products found', not_picked: 'not picked' };

const state = {
  category: null,
  imageUrl: null,        // what goes to /api/plan
  uploading: false,
  data: null,            // latest ?format=ui response
  firstBudget: 0,
  gender: null,
  pins: {},
  requestId: 0,
  marks: [],             // cart items in photo order, with their boxes and dots
  selected: null,        // slot shown on the photo
  sticky: false,         // selected by a click, not just a hover
  showBoxes: false,
  source: null,          // ?src= this visitor came with (kept in localStorage)
  finchCode: null,       // Finch completion code, once the server has one
};

const announce = (msg) => { $('live').textContent = msg; };

function formError(msg) {
  $('form-msg').hidden = !msg;
  $('form-msg').textContent = msg || '';
}

async function api(path, options) {
  const token = await window.__auth?.token();
  if (!token) throw new Error('Please sign in first.');
  const r = await fetch(path, { ...options,
    headers: { Authorization: `Bearer ${token}`, ...(options.headers || {}) } });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.error || `The request failed (HTTP ${r.status}).`);
  return body;
}
const postJSON = (path, body) => api(path, { method: 'POST',
  headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });

// ── Where the visitor came from (?src=finch) ─────────────────────────
// Read before sign-in and kept in localStorage, so the popup sign-in, the history
// entries this page pushes (they drop the query) and a reload don't lose it. Once
// signed in it is sent to the server once per account (src/finch_task.py).
const storage = (() => { try { return window.localStorage; } catch { return null; } })();
state.source = rememberSource(location.search, storage);
let sourceSending = null;

function ensureSource() {
  const user = window.__auth?.user;
  if (!state.source || !user || wasSent(storage, user.uid)) return Promise.resolve();
  if (!sourceSending) {
    sourceSending = postJSON('/api/me/source', { source: state.source })
      .then(() => markSent(storage, user.uid))
      .catch((err) => console.info('[source]', err.message))
      .finally(() => { sourceSending = null; });
  }
  return sourceSending;
}

// The Finch completion code: only for src=finch accounts, and the server answers with
// a code only once that account has built an outfit plan.
async function fetchFinchCode() {
  try {
    const r = await api('/api/me/finch-code', { method: 'GET' });
    if (r.source && !state.source) {
      // Recorded on another device or browser: remember it here too.
      state.source = r.source;
      try { storage.setItem('gc_src', r.source); } catch { /* memory only */ }
    }
    if (r.code) state.finchCode = r.code;
  } catch (err) {
    console.info('[finch-code]', err.message);
  }
  showFinchCode();
}

// After sign-in: record this browser's source first, then ask the server (it may know
// the source and the code from another device even when this browser doesn't).
async function checkFinchOnSignIn() {
  await ensureSource();
  await fetchFinchCode();
}

// After a new plan: only accounts known to come from Finch ask again.
async function refreshFinchCode() {
  if (!state.source && !state.finchCode) return;
  if (!state.finchCode) await fetchFinchCode();
  else showFinchCode();
}

function showFinchCode() {
  $('finch-code').hidden = !state.finchCode;
  $('finch-code-value').textContent = state.finchCode || '';
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    const range = document.createRange();   // fallback: select it for ⌘C / long-press
    range.selectNodeContents($('finch-code-value'));
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
    return false;
  }
}
$('finch-code-copy').addEventListener('click', async () => {
  if (!state.finchCode) return;
  const ok = await copyText(state.finchCode);
  const btn = $('finch-code-copy');
  btn.textContent = ok ? 'Copied' : 'Selected: press Copy';
  announce(ok ? 'Code copied' : 'Code selected');
  setTimeout(() => { btn.textContent = 'Copy code'; }, 2000);
});

// ── Category and gender ──────────────────────────────────────────────
function setCategory(key) {
  state.category = key;
  document.querySelectorAll('.scene-chip').forEach((c) => {
    const on = c.dataset.category === key;
    c.classList.toggle('is-active', on);
    c.setAttribute('aria-pressed', String(on));
  });
  $('gender-row').hidden = key !== 'outfit';
  formError('');
  renderGallery(key);
}
$('scene-chips').addEventListener('click', (e) => {
  const chip = e.target.closest('.scene-chip');
  if (!chip || chip.dataset.category === state.category) return;
  setCategory(chip.dataset.category);
  nav.push({ view: 'search', category: chip.dataset.category });
});

// ── Upload: click, drag and drop, paste ──────────────────────────────
const zone = $('photo-zone');
let previewUrl = null;

function setPreview(src, name, status, kind) {
  $('photo-img').src = src;
  $('photo-empty').hidden = true;
  $('photo-preview').hidden = false;
  $('photo-name').textContent = name;
  $('photo-status').textContent = status;
  $('photo-status').dataset.state = kind;
}

function clearPhoto() {
  if (previewUrl) URL.revokeObjectURL(previewUrl);
  previewUrl = null;
  state.imageUrl = null;
  $('photo-img').removeAttribute('src');
  $('photo-preview').hidden = true;
  $('photo-empty').hidden = false;
}

async function uploadFile(file) {
  if (!file) return;
  if (!['image/jpeg', 'image/png'].includes(file.type)) {
    formError('Please use a JPG or PNG image.');
    return;
  }
  formError('');
  if (previewUrl) URL.revokeObjectURL(previewUrl);
  previewUrl = URL.createObjectURL(file);
  state.imageUrl = null;
  state.uploading = true;
  setPreview(previewUrl, file.name || 'Pasted image', 'Uploading', 'running');
  try {
    const up = await api('/api/upload', { method: 'POST', body: file,
                                          headers: { 'Content-Type': file.type } });
    state.imageUrl = up.url;
    setPreview(previewUrl, file.name || 'Pasted image', 'Ready', 'ok');
    announce('Photo uploaded');
  } catch (err) {
    clearPhoto();
    formError(err.message);
  } finally {
    state.uploading = false;
  }
}

zone.addEventListener('click', (e) => {
  if (e.target === $('photo-input') || e.target.closest('#photo-remove')) return;
  $('photo-input').click();
});
zone.addEventListener('keydown', (e) => {
  if (e.target === zone && (e.key === 'Enter' || e.key === ' ')) {
    e.preventDefault();
    $('photo-input').click();
  }
});
$('photo-input').addEventListener('change', () => {
  uploadFile($('photo-input').files[0]);
  $('photo-input').value = '';
});
for (const name of ['dragenter', 'dragover']) {
  zone.addEventListener(name, (e) => { e.preventDefault(); zone.classList.add('is-drag'); });
}
for (const name of ['dragleave', 'drop']) {
  zone.addEventListener(name, (e) => { e.preventDefault(); zone.classList.remove('is-drag'); });
}
zone.addEventListener('drop', (e) => uploadFile(e.dataTransfer.files[0]));
document.addEventListener('paste', (e) => {
  if ($('search-view').hidden) return;
  const file = [...(e.clipboardData?.files || [])].find((f) => f.type.startsWith('image/'));
  if (file) { e.preventDefault(); uploadFile(file); }
});
$('photo-remove').addEventListener('click', (e) => { e.stopPropagation(); clearPhoto(); });

// ── Generate ─────────────────────────────────────────────────────────
function readBudget() {
  const n = Number($('budget-input').value);
  return n > 0 && n <= 100000 ? n : null;
}

async function generate() {
  if (state.uploading) return formError('The photo is still uploading.');
  if (!state.imageUrl) return formError('Add a photo first, or tap a look in the gallery.');
  if (!state.category) return formError('Choose a category: Outfit, Desk or Room.');
  const gender = $('gender').value;
  if (state.category === 'outfit' && !gender) return formError('Choose who you are shopping for.');
  const budget = readBudget();
  if (!budget) return formError('Enter a budget between $1 and $100,000.');

  formError('');
  const go = $('generate');
  go.disabled = true;
  $('generate-text').textContent = state.category === 'outfit'
    ? 'Finding each item… up to 12 s' : 'Reading the photo… up to a minute';
  announce('Building your plan');
  try {
    await ensureSource();        // so a src=finch account's plan counts for its code
    const data = await postJSON('/api/plan?format=ui', {
      image_url: state.imageUrl, scenario: state.category, budget,
      ...(state.category === 'outfit' ? { gender } : {}) });
    state.firstBudget = budget;
    state.gender = state.category === 'outfit' ? gender : null;
    state.pins = {};
    renderResult(data, null);
    setupSlider(budget);
    nav.pushResult(null);        // before showResult scrolls to the top
    showResult();
    refreshFinchCode();
  } catch (err) {
    formError(err.message);
  } finally {
    go.disabled = false;
    $('generate-text').textContent = 'Generate Plan';
  }
}
$('generate').addEventListener('click', generate);
$('budget-input').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') { e.preventDefault(); generate(); }
});

// ── Gallery: one grid that follows the category (Outfit on the home page) ──
// Plans come from the server's cache (src/gallery.py); the looks from static/gallery.json.
const GALLERY_TEXT = {
  outfit: { title: 'Tap a look to build a plan at your budget', dot: 'dot-fashion' },
  desk: { title: 'Desk setups: tap one to open its plan', dot: 'dot-gaming', beta: true },
  room: { title: 'Rooms: tap one to open its plan', dot: 'dot-room', beta: true },
};
const GALLERY_MAX = 12;
let opening = false;
let galleryShown = null;

// No gender label on the tile; the look's gender is still used for its search.
function galleryTile(look) {
  const tile = el('button', { class: 'gallery-tile', type: 'button',
      'aria-label': `Open the plan for ${look.title}`,
      onclick: () => openLook(look, tile) },
    el('img', { src: photo(look, 700), alt: look.alt, loading: 'lazy' }),
    el('span', { class: 'tile-shade' }),
    el('span', { class: 'tile-caption' }, el('strong', {}, look.title)),
    el('span', { class: 'tile-busy', 'aria-hidden': 'true' }, 'Opening…'));
  const c = look.credit || {};
  const credit = c.page
    ? el('p', { class: 'tile-credit' }, 'Photo: ',
        el('a', { href: c.url, target: '_blank', rel: 'noopener' }, c.name), ' · ',
        el('a', { href: c.page, target: '_blank', rel: 'noopener' }, SITE_NAME(look)))
    : el('p', { class: 'tile-credit' }, 'Photo: ',
        el('a', { href: c.url || 'https://unsplash.com', target: '_blank', rel: 'noopener' }, c.name || 'Unsplash'));
  return el('div', { class: 'gallery-item' }, tile, credit);
}

// Desk / Room looks show only once that category is picked; otherwise outfits.
function renderGallery(category) {
  const which = category === 'desk' || category === 'room' ? category : 'outfit';
  if (!galleryLooks.size || which === galleryShown) return;
  galleryShown = which;
  const text = GALLERY_TEXT[which];
  const looks = [...galleryLooks.values()].filter((l) => l.category === which).slice(0, GALLERY_MAX);
  $('gallery-rows').replaceChildren(el('div', { class: 'gallery-row', 'data-category': which },
    el('div', { class: 'gallery-row-head' },
      el('h3', {}, el('span', { class: `dot ${text.dot}`, 'aria-hidden': 'true' }), text.title,
         text.beta ? el('span', { class: 'beta' }, 'Beta') : null)),
    el('div', { class: 'gallery-grid' }, ...looks.map(galleryTile))));
}

const galleryLooks = new Map();            // look id -> look, for Back/Forward

async function loadGallery() {
  try {
    const doc = await (await fetch('/static/gallery.json')).json();
    for (const l of doc.looks) galleryLooks.set(l.id, l);
    renderGallery(state.category);
  } catch (err) {
    console.error('[gallery]', err);
  }
}
const galleryReady = loadGallery();

// record: add a history entry (false when Back/Forward brings a look back).
// refresh: let the server refresh the look in the background (false when only
// restoring it, so returning to a page never starts an AISUM search by itself).
async function openLook(look, tile, { record = true, refresh = true } = {}) {
  if (opening) return;
  opening = true;
  tile?.classList.add('is-busy');
  if (tile) tile.querySelector('.tile-busy').textContent = 'Opening…';
  formError('');
  announce(`Opening ${look.title}`);
  // A look with no saved plan yet is planned live; say so if it takes a while.
  const slow = setTimeout(() => {
    if (tile) tile.querySelector('.tile-busy').textContent = look.category === 'outfit'
      ? 'Building… up to 12 s' : 'Building… up to a minute';
  }, 1500);
  try {
    await ensureSource();
    const data = await postJSON(`/api/gallery/${encodeURIComponent(look.id)}/open?format=ui`
                                + (refresh ? '' : '&refresh=0'), {});
    clearPhoto();
    state.imageUrl = photo(look, 1080);
    setPreview(photo(look, 700), look.title, 'Ready', 'ok');
    setCategory(look.category);
    if (look.gender) $('gender').value = look.gender;
    $('budget-input').value = data.budget;
    state.firstBudget = data.budget;
    state.gender = look.category === 'outfit' ? look.gender : null;
    state.pins = {};
    renderResult(data, null);
    setupSlider(data.budget);
    if (record) nav.pushResult(look.id);  // before showResult scrolls to the top
    else nav.rememberResult(look.id);
    showResult();
    refreshFinchCode();
  } catch (err) {
    formError(err.message);
    tile?.scrollIntoView({ behavior: 'smooth', block: 'center' });
  } finally {
    clearTimeout(slow);
    tile?.classList.remove('is-busy');
    opening = false;
  }
}

// ── First-visit tour: once per account (stored by the server), Driver.js ──
// Three steps on a computer, two on a phone. Left out on purpose: "Pick a category"
// (the chips say it themselves, and Generate asks if none is picked) and a separate
// "Set your budget" step (budget and Generate sit side by side, so one step covers both).
const DESKTOP_STEPS = [
  { element: '#photo-zone', popover: { title: 'Add a photo you love', description: 'Upload, drag in or paste (⌘V) any picture of the look.', side: 'left', align: 'start' } },
  { element: '.search-actions', popover: { title: 'Set a budget, then build', description: 'We find real products that match, and the whole set fits your budget.', side: 'bottom', align: 'end' } },
  { element: '.gallery', popover: { title: 'Or start from a look', description: 'Tap any look below to open its plan right away. Pick Desk or Room to see those instead.', side: 'top', align: 'start' } },
];
const PHONE_STEPS = [
  { element: '#photo-zone', popover: { title: 'Add a photo you love', description: 'Or tap a look in the gallery further down.', side: 'bottom' } },
  { element: '.search-actions', popover: { title: 'Set a budget, then build', description: 'Real products that match, all within budget.', side: 'top' } },
];
let tourChecked = false;

async function maybeTour() {
  if (tourChecked || !window.__auth?.user || $('search-view').hidden) return;
  tourChecked = true;
  try {
    const params = new URLSearchParams(location.search);
    if (params.has('reset-tour')) {
      // Local testing only: the server answers 404 anywhere but this machine.
      await api('/api/me/onboarding', { method: 'DELETE' }).catch((e) => console.info('[tour] reset:', e.message));
      params.delete('reset-tour');
      history.replaceState(history.state, '', location.pathname + (params.size ? `?${params}` : '') + location.hash);
    }
    const { seen } = await api('/api/me/onboarding', { method: 'GET' });
    if (seen) return;
    const steps = (narrow() ? PHONE_STEPS : DESKTOP_STEPS).filter((s) => document.querySelector(s.element));
    const tour = driver({
      steps, showProgress: true, allowClose: true, overlayOpacity: 0.55, stagePadding: 6,
      stageRadius: 12, popoverClass: 'gc-tour', nextBtnText: 'Next', prevBtnText: 'Back',
      doneBtnText: 'Done', progressText: '{{current}} of {{total}}',
      // Finished or skipped: either way, don't show it again on any device.
      onDestroyed: () => { postJSON('/api/me/onboarding', {}).catch(() => {}); },
    });
    tour.drive();
  } catch (err) {
    console.info('[tour]', err.message);
  }
}
window.addEventListener('authchange', (e) => {
  if (!e.detail.user) {
    state.finchCode = null;   // signed out: the next account has its own code
    showFinchCode();
    return;
  }
  checkFinchOnSignIn();
  setTimeout(maybeTour, 400);
  galleryReady.then(() => nav.resume());
});
if (window.__auth?.user) setTimeout(maybeTour, 400);

// ── Result ───────────────────────────────────────────────────────────
function productImage(p) {
  const img = el('img', { alt: p.name || '', loading: 'lazy' });
  if (p.image_url) img.src = p.image_url;
  img.addEventListener('error', () => {
    img.closest('.product-media')?.classList.add('media-fallback');
    img.remove();
  });
  return img;
}

function altsRow(slot, alts, title) {
  if (!alts?.length) return null;
  return el('div', { class: 'alts' }, el('span', { class: 'alts-title' }, title),
    ...alts.map((a) => el('button', { type: 'button', class: 'alt',
        title: `${a.name} · ${money(a.price)}: use this one`,
        'aria-label': `Use ${a.name}, ${money(a.price)}`,
        onclick: () => swap(slot, a) },
      el('img', { src: a.image_url || undefined, alt: '', loading: 'lazy' }),
      el('span', {}, money(a.price)))));
}

function sizeNote(p) {
  if (!p.size) return null;
  const text = p.size.text ? `Listed size: ${p.size.text}` : 'Size not listed';
  return el('p', { class: 'product-size' }, text);
}

function productCard(p, changed) {
  const link = safeLink(p.product_url);
  const initial = (p.name || '?').charAt(0);
  return el('article', { class: `product-card is-in-plan${changed ? ' is-changed' : ''}`, id: `card-${p.slot}` },
    el('div', { class: 'product-media', 'data-initial': initial },
      productImage(p),
      p.rank === 1 ? el('span', { class: 'match-chip' }, 'Best match') : null,
      el('span', { class: 'plan-chip' }, changed || 'In plan')),
    el('div', { class: 'product-body' },
      el('p', { class: 'product-cat' }, p.item_label),
      el('h3', { title: p.name }, p.name),
      p.brand ? el('p', { class: 'product-brand' }, p.brand) : null,
      sizeNote(p),
      el('div', { class: 'product-foot' },
        el('span', { class: 'price' }, money(p.price)),
        link ? el('a', { class: 'btn btn-secondary btn-sm', href: link, target: '_blank', rel: 'noopener' }, 'View') : null),
      altsRow(p.slot, p.alternatives, 'Or:')));
}

// ── Photo marks and cart: one number per cart item ───────────────────
const DOT = 22, DOT_GAP = 26;
const canHover = window.matchMedia('(hover: hover)').matches;
const narrow = () => window.matchMedia('(max-width: 760px)').matches;
const frac = (v) => `${(v * 100).toFixed(3)}%`;

// "Tap an item to shop" shows until the first product is opened. Storage can be off
// (private mode, blocked site data); then the hint just shows each time.
const HINT_KEY = 'ghostcart.shopHintSeen';
function hintSeen() { try { return localStorage.getItem(HINT_KEY) === '1'; } catch { return false; } }
function markHintSeen() {
  try { localStorage.setItem(HINT_KEY, '1'); } catch { /* not stored: shows again next time */ }
  $('cart-hint').hidden = true;
}

function renderMarks() {
  $('mark-boxes').replaceChildren(...state.marks.flatMap((m) => m.boxes.map((b) =>
    el('span', { class: 'mark-box', 'data-slot': m.product.slot,
                 style: `left:${frac(b.x1)};top:${frac(b.y1)};width:${frac(b.x2 - b.x1)};height:${frac(b.y2 - b.y1)}` }))));
  $('mark-dots').replaceChildren(...state.marks.flatMap((m) => m.dots.map((d) =>
    el('button', { type: 'button', class: 'mark-dot', 'data-slot': m.product.slot,
                   'data-x': d.x, 'data-y': d.y,
                   'aria-label': `${m.n}. ${m.product.item_label}: show in cart`,
                   onclick: (e) => { e.stopPropagation(); pickFromPhoto(m.product.slot); } },
      String(m.n)))));
  placeDots();
}

// Dots sit at the top centre of their box; two that would touch are moved apart.
function placeDots() {
  const frame = $('photo-boxes');
  const W = frame.clientWidth, H = frame.clientHeight;
  if (!W || !H) return;
  const dots = [...$('mark-dots').children];
  const spread = spreadDots(dots.map((d) => ({ x: d.dataset.x * W, y: d.dataset.y * H + 3 })),
                            DOT_GAP, H - DOT - 3);
  dots.forEach((d, i) => {
    const x = Math.min(W - DOT / 2 - 3, Math.max(DOT / 2 + 3, spread[i].x));
    d.style.left = `${x}px`;
    d.style.top = `${Math.max(3, spread[i].y)}px`;
  });
}
new ResizeObserver(placeDots).observe($('photo-boxes'));
$('result-photo').addEventListener('load', placeDots);

function applySelection() {
  const sel = state.selected;
  const frame = $('photo-boxes');
  frame.classList.toggle('has-focus', Boolean(sel));
  frame.classList.toggle('show-all', state.showBoxes);
  frame.querySelectorAll('.mark-box').forEach((b) => b.classList.toggle('is-on', b.dataset.slot === sel));
  frame.querySelectorAll('.mark-dot').forEach((d) => {
    d.classList.toggle('is-on', d.dataset.slot === sel);
    d.setAttribute('aria-pressed', String(d.dataset.slot === sel));
  });
  const mark = state.marks.find((m) => m.product.slot === sel);
  const holes = (mark?.boxes || []).map((b) => `M${b.x1} ${b.y1}H${b.x2}V${b.y2}H${b.x1}Z`).join('');
  $('photo-mask').querySelector('path').setAttribute('d', holes ? `M0 0H1V1H0Z${holes}` : '');
  document.querySelectorAll('.cart-item').forEach((c) => {
    c.classList.toggle('is-active', c.dataset.slot === sel);
    c.querySelector('.cart-num')?.setAttribute('aria-pressed', String(c.dataset.slot === sel));
  });
}

function select(slot, sticky) {
  state.selected = slot;
  state.sticky = Boolean(slot) && sticky;
  applySelection();
}

function pickFromPhoto(slot) {
  if (state.sticky && state.selected === slot) return select(null, false);
  select(slot, true);
  const card = $(`cart-${slot}`);
  if (!card) return;
  card.classList.remove('is-flash');
  void card.offsetWidth;            // restart the highlight animation
  card.classList.add('is-flash');
  if (narrow()) card.scrollIntoView({ behavior: 'smooth', block: 'center' });
}

// Tapping the photo outside a dot clears the selection.
$('photo-boxes').addEventListener('click', () => { if (state.selected) select(null, false); });

$('show-boxes').addEventListener('click', () => {
  state.showBoxes = !state.showBoxes;
  $('show-boxes').setAttribute('aria-checked', String(state.showBoxes));
  applySelection();
});

const ARROW = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M7 17 17 7M8 7h9v9"/></svg>';

function cartItem(m, located) {
  const p = m.product;
  const link = safeLink(p.product_url);
  const label = located ? `${m.n}. ${p.item_label}` : p.item_label;
  const card = el('div', { class: 'cart-item', id: `cart-${p.slot}`, 'data-slot': p.slot },
    el('div', { class: 'cart-media product-media', 'data-initial': (p.name || '?').charAt(0) }, productImage(p)),
    // click_url exactly as AISUM gave it: it carries the store's tracking parameters.
    link ? el('a', { class: 'cart-link', href: link, target: '_blank', rel: 'noopener',
                     title: p.name || p.item_label,
                     'aria-label': `Shop ${label}: ${p.name || ''}, ${money(p.price)} (opens in a new tab)`,
                     onclick: () => { markHintSeen(); if (located) select(p.slot, true); } }) : null,
    !located ? null : el('button', { type: 'button', class: 'cart-num', 'aria-pressed': 'false',
                   'aria-label': `Show ${m.n}. ${p.item_label} on the photo`,
                   onclick: () => {
                     if (state.sticky && state.selected === p.slot) return select(null, false);
                     select(p.slot, true);
                     if (narrow()) $('photo-boxes').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
                   } }, String(m.n)),
    link ? el('span', { class: 'cart-go' }) : null,
    el('span', { class: 'cart-price' }, money(p.price)),
    el('span', { class: 'cart-shop' }, link ? 'Shop' : p.item_label));
  card.querySelector('.cart-go')?.insertAdjacentHTML('afterbegin', ARROW);
  if (!located) return card;          // nothing on the photo to point at
  if (canHover) {
    card.addEventListener('mouseenter', () => { if (!state.sticky) select(p.slot, false); });
    card.addEventListener('mouseleave', () => { if (!state.sticky) select(null, false); });
  }
  card.addEventListener('focusin', () => { if (!state.sticky) select(p.slot, false); });
  card.addEventListener('focusout', (e) => {
    if (!state.sticky && !card.contains(e.relatedTarget)) select(null, false);
  });
  return card;
}

function renderCart(data) {
  // Only outfit items have a place on the photo (AISUM image-search boxes). Desk and
  // Room items come from a text list, so no boxes, dots or numbers that would suggest one.
  const located = data.category === 'outfit';
  state.marks = located ? cartMarks(data)
    : data.matched_products.map((product, i) => ({ n: i + 1, product, boxes: [], dots: [] }));
  if (!located) { state.showBoxes = false; $('show-boxes').setAttribute('aria-checked', 'false'); }
  $('show-boxes').hidden = !located;
  if (!state.marks.some((m) => m.product.slot === state.selected)) select(null, false);
  renderMarks();
  $('cart-board').replaceChildren(...state.marks.map((m) => cartItem(m, located)));
  $('cart-hint').hidden = !state.marks.length || hintSeen();
  $('cart-total').textContent = `Total ${money(data.total_price)} · Budget ${money(data.budget)}`;
  applySelection();
}

function renderResult(data, previous) {
  state.data = data;
  const before = previous
    ? Object.fromEntries(previous.matched_products.map((p) => [p.slot, p.id])) : null;
  const changedTag = (p) => (!before ? null
    : !(p.slot in before) ? 'Added' : before[p.slot] !== p.id ? 'Swapped' : null);

  const beta = data.category !== 'outfit';
  $('result-eyebrow').textContent = `Plan ready · ${data.category_label}${beta ? ' (Beta)' : ''}`;
  $('result-title').textContent = `Your ${data.category_label.toLowerCase()} plan.`;
  $('result-sub').textContent = data.explanation || '';
  const chips = [['Category', data.category_label], ['Budget', money(data.budget)],
                 ['Items', String(data.matched_products.length)]];
  if (state.gender) chips.push(['For', GENDER_LABEL[state.gender]]);
  $('result-chips').replaceChildren(...chips.map(([k, v]) =>
    el('span', { class: 'result-chip' }, el('strong', {}, `${k}: `), v)));

  const pct = data.budget ? Math.round((data.total_price / data.budget) * 100) : 0;
  const n = data.matched_products.length;
  $('replan-title').textContent = n
    ? `${n} item${n === 1 ? '' : 's'} for ${money(data.total_price)}`
    : 'Nothing fits this budget yet';
  $('replan-body').textContent = data.budget > state.firstBudget
    ? `Products were searched up to ${money(state.firstBudget)}. Generate a new plan to look at pricier items.`
    : 'Move the budget or swap a product and the whole set is rebuilt to stay within it.';
  $('stat-total').textContent = money(data.total_price);
  $('stat-usage').textContent = `${pct}%`;
  $('stat-left').textContent = money(Math.max(0, data.budget - data.total_price));
  $('budget-meter-fill').style.width = `${Math.min(100, pct)}%`;

  if (data.original_image && $('result-photo').src !== data.original_image) {
    $('result-photo').src = data.original_image;
  }
  $('download').hidden = data.category !== 'outfit' || !n;

  renderCart(data);
  $('product-grid').replaceChildren(...data.matched_products.map((p) => productCard(p, changedTag(p))));

  $('unmatched-section').hidden = !data.unmatched_items.length;
  $('unmatched-list').replaceChildren(...data.unmatched_items.map((u) =>
    el('article', { class: 'unmatched-card' },
      el('p', { class: 'product-cat' }, u.item_label),
      el('p', { class: 'unmatched-text' }, u.description ? `${u.description}. ${u.message}` : u.message),
      altsRow(u.slot, u.alternatives, 'Closest:'))));

  const seen = new Set();
  const leftOut = data.detected_items.filter((d) => {
    if (!STATUS_TEXT[d.status] || seen.has(d.slot)) return false;
    seen.add(d.slot);
    return true;
  });
  $('left-out-section').hidden = !leftOut.length;
  $('left-out').replaceChildren(...leftOut.map((d) =>
    el('li', {}, el('strong', {}, d.item_label), `: ${STATUS_TEXT[d.status]}`)));
}

function showResult() {
  $('search-view').hidden = true;
  $('result-view').hidden = false;
  window.scrollTo({ top: 0 });
  $('result-title').focus({ preventScroll: true });
}
function showSearch() {
  $('result-view').hidden = true;
  $('search-view').hidden = false;
  window.scrollTo({ top: 0 });
}
function backToSearch() {
  if (history.state?.gc && history.state.view === 'result' && history.state.depth > 0) {
    history.back();                       // the same as the browser's Back button
  } else {
    nav.push({ view: 'search', category: state.category });
    showSearch();
  }
}
$('back-to-search').addEventListener('click', backToSearch);
$('new-search').addEventListener('click', backToSearch);

// ── Browser Back / Forward (and the phone's back gesture) ────────────
// Each step is a history entry: a category picked, a result opened, back to the
// gallery. Entries remember their scroll position. Results are kept in memory for
// Back/Forward; after a reload, a gallery look is reopened from the server's cache
// (no refresh, so nothing searches by itself) and an own-photo result, which can't be
// rebuilt, falls back to the search page.
const nav = (() => {
  const results = new Map();               // key -> what renderResult needs
  let seq = 0;
  if ('scrollRestoration' in history) history.scrollRestoration = 'manual';

  const url = (entry) => {
    const q = new URLSearchParams(location.search);
    for (const k of ['category', 'look', 'view']) q.delete(k);
    if (entry.view === 'result') {
      if (entry.look) q.set('look', entry.look); else q.set('view', 'result');
    } else if (entry.category) {
      q.set('category', entry.category);
    }
    return location.pathname + (q.size ? `?${q}` : '') + location.hash;
  };
  const here = () => (history.state?.gc ? history.state : null);
  // Keep the current entry's scroll position up to date.
  const saveScroll = () => {
    if (here()) history.replaceState({ ...history.state, scroll: window.scrollY }, '');
  };
  let t = 0;
  window.addEventListener('scroll', () => { clearTimeout(t); t = setTimeout(saveScroll, 120); },
                          { passive: true });

  function push(entry) {
    clearTimeout(t);
    saveScroll();
    const depth = (here()?.depth ?? 0) + 1;
    history.pushState({ gc: 1, depth, scroll: 0, ...entry }, '', url(entry));
  }
  function snapshot(look) {
    return { data: state.data, look, firstBudget: state.firstBudget, gender: state.gender,
             pins: state.pins, category: state.category, slider: Number($('slider').value) };
  }
  function pushResult(look) {
    const key = `r${++seq}`;
    results.set(key, snapshot(look));
    push({ view: 'result', key, look, category: state.category });
  }
  function rememberResult(look) {           // the entry exists; just keep its result
    const key = here()?.key || `r${++seq}`;
    results.set(key, snapshot(look));
    history.replaceState({ ...history.state, key }, '');
  }
  // Slider moves and swaps change the result shown in this entry.
  function updateResult() {
    const key = here()?.view === 'result' ? here().key : null;
    if (key && results.has(key)) results.set(key, snapshot(results.get(key).look));
  }
  const restoreScroll = (y) => requestAnimationFrame(() => window.scrollTo(0, y || 0));

  async function show(entry) {
    if (entry.view === 'result') {
      const r = results.get(entry.key);
      if (r) {
        state.firstBudget = r.firstBudget; state.gender = r.gender; state.pins = r.pins;
        setCategory(r.category);
        renderResult(r.data, null);
        setupSlider(r.firstBudget);
        $('slider').value = r.slider; $('slider-val').textContent = money(r.slider);
        $('search-view').hidden = true; $('result-view').hidden = false;
        restoreScroll(entry.scroll);
        return;
      }
      const look = entry.look && galleryLooks.get(entry.look);
      if (look && window.__auth?.user) {
        await openLook(look, null, { record: false, refresh: false });
        restoreScroll(entry.scroll);
        return;
      }
      // An own-photo result after a reload: nothing to show, go to the search page.
      history.replaceState({ gc: 1, depth: entry.depth, view: 'search', category: entry.category, scroll: 0 },
                           '', url({ view: 'search', category: entry.category }));
      entry = history.state;
    }
    setCategory(entry.category || null);
    $('result-view').hidden = true; $('search-view').hidden = false;
    restoreScroll(entry.scroll);
  }
  window.addEventListener('popstate', (e) => {
    clearTimeout(t);
    if (e.state?.gc) show(e.state);
  });

  // First entry: take the page's address as it is (?category=, ?look=).
  function start() {
    const q = new URLSearchParams(location.search);
    const look = q.get('look');
    const category = q.get('category');
    const entry = look ? { view: 'result', look, key: null, category: null }
                       : { view: 'search', category: ['outfit', 'desk', 'room'].includes(category) ? category : null };
    history.replaceState({ gc: 1, depth: 0, scroll: 0, ...entry }, '', url(entry));
    if (entry.view === 'search' && entry.category) setCategory(entry.category);
  }
  // After sign-in (or straight away if already signed in), bring back a ?look= entry.
  function resume() {
    const entry = here();
    if (entry?.view === 'result' && !state.data) show(entry);
  }
  return { push, pushResult, rememberResult, updateResult, start, resume };
})();

// ── Replan: budget slider and swaps ──────────────────────────────────
function setupSlider(budget) {
  const s = $('slider');
  s.min = Math.max(5, Math.round((budget * 0.3) / 5) * 5);
  s.max = Math.round((budget * 1.5) / 5) * 5;
  s.step = 5;
  s.value = budget;
  $('slider-val').textContent = money(budget);
}

async function replan(budget, pins, message) {
  const previous = state.data;
  const id = ++state.requestId;
  $('replan-msg').textContent = 'Updating…';
  try {
    const data = await postJSON('/api/replan?format=ui',
      { session_id: previous.session_id, budget, pin: pins });
    if (id !== state.requestId) return;   // a newer change is on its way
    state.pins = pins;
    renderResult(data, previous);
    nav.updateResult();
    $('replan-msg').textContent = message;
    announce(message);
  } catch (err) {
    if (id !== state.requestId) return;
    $('slider').value = previous.budget;
    $('slider-val').textContent = money(previous.budget);
    $('replan-msg').textContent = /session|expired|HTTP 404/i.test(err.message)
      ? 'This plan has expired. Please generate it again.' : err.message;
  }
}

function swap(slot, alt) {
  replan(state.data.budget, { ...state.pins, [slot]: alt.id },
         `Swapped in ${alt.item_label}: ${(alt.name || '').slice(0, 50)}`);
}
$('slider').addEventListener('input', () => {
  $('slider-val').textContent = money($('slider').value);
});
$('slider').addEventListener('change', () => {
  const budget = Number($('slider').value);
  if (state.data && budget !== state.data.budget) {
    replan(budget, state.pins, `Budget changed to ${money(budget)}.`);
  }
});

// ── Download: photo + cart as a PNG, images via the session proxy ────
const ZONES = [
  ['side', /bag|tote|purse|backpack|clutch|wallet/],
  ['feet', /shoe|sneaker|boot|heel|sandal|loafer|flat|slipper|mule|clog|sock/],
  ['bottom', /pant|jean|trouser|skirt|short|legging|jogger/],
  ['top', /coat|jacket|blazer|cardigan|sweater|hoodie|shirt|top|blouse|tee|vest|dress|jumpsuit|parka|outer/],
  ['head', /hat|cap|beanie|beret|fedora|glass|earring|necklace|scarf|tie|jewel|watch|bracelet|belt/],
];
const ROWS = ['head', 'top', 'bottom', 'feet'];
const zoneOf = (slot) => (ZONES.find(([, re]) => re.test(slot)) || ['head'])[0];

async function boardImage(slot) {
  const token = await window.__auth?.token();
  const r = await fetch(`/api/lookboard-image?session_id=${encodeURIComponent(state.data.session_id)}`
                        + `&slot=${encodeURIComponent(slot)}`,
                        { headers: { Authorization: `Bearer ${token}` } });
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return createImageBitmap(await r.blob());
}

function drawContain(ctx, img, x, y, w, h) {
  const k = Math.min(w / img.width, h / img.height);
  const dw = img.width * k, dh = img.height * k;
  ctx.drawImage(img, x + (w - dw) / 2, y + (h - dh) / 2, dw, dh);
}

// The footer's QR code and address. static/ghostcart-qr.svg holds exactly this URL;
// scripts/make_qr.py regenerates it, tests/test_lookboard_qr.py checks the two agree.
const SITE_URL = 'https://app-103901851610.us-central1.run.app/ghostcart';
const QR_MODULES = 45;   // version 5 with its 4-module border
const QR_PX = 5;         // per module: 225 px, sharp edges, 202 px if shrunk to 1080 wide

function loadImage(src) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => resolve(img);
    img.onerror = () => reject(new Error('QR code image did not load'));
    img.src = src;
  });
}

async function downloadBoard() {
  const data = state.data;
  if (!data) return;
  const btn = $('download');
  btn.disabled = true;
  $('replan-msg').textContent = 'Building the image…';
  try {
    const W = 1200, BOARD_H = 900, PAD = 40, TOP = 90, BOTTOM = 90;
    const QR = QR_MODULES * QR_PX, FOOT_PAD = 30, H = BOARD_H + QR + FOOT_PAD * 2;
    const zones = { head: [], top: [], bottom: [], feet: [], side: [] };
    for (const p of data.matched_products) zones[zoneOf(p.slot)].push(p);
    const rows = ROWS.filter((z) => zones[z].length);
    const hasSide = zones.side.length > 0;
    const photoW = 520, itemsX = PAD + photoW + 30;
    const itemsW = (hasSide ? W - 200 - PAD : W - PAD) - itemsX;
    const slots = ['_photo', ...data.matched_products.map((p) => p.slot)];
    const [imgs, qr] = await Promise.all([
      Promise.all(slots.map(async (s) => {
        try { return [s, await boardImage(s)]; } catch { return [s, null]; }
      })).then(Object.fromEntries),
      loadImage("/static/ghostcart-qr.svg"),
    ]);

    const c = el('canvas', { width: W, height: H });
    const ctx = c.getContext('2d');
    ctx.fillStyle = '#F8FAFC'; ctx.fillRect(0, 0, W, H);
    ctx.fillStyle = '#0F172A'; ctx.font = '700 34px system-ui, sans-serif';
    ctx.fillText('GhostCart Lookboard', PAD, 58);
    const areaH = BOARD_H - TOP - BOTTOM;
    ctx.fillStyle = '#F1F5F9'; ctx.fillRect(PAD, TOP, photoW, areaH);
    if (imgs._photo) drawContain(ctx, imgs._photo, PAD, TOP, photoW, areaH);

    const tile = (p, x, y, w, h) => {
      const ih = h - 40;
      ctx.fillStyle = '#FFFFFF'; ctx.fillRect(x, y, w, ih);
      ctx.strokeStyle = '#E2E8F0'; ctx.lineWidth = 2; ctx.strokeRect(x, y, w, ih);
      if (imgs[p.slot]) drawContain(ctx, imgs[p.slot], x + 6, y + 6, w - 12, ih - 12);
      ctx.textAlign = 'center';
      ctx.fillStyle = '#475569'; ctx.font = '15px system-ui, sans-serif';
      ctx.fillText(p.item_label, x + w / 2, y + ih + 17);
      ctx.fillStyle = '#0F172A'; ctx.font = '700 16px system-ui, sans-serif';
      ctx.fillText(money(p.price), x + w / 2, y + ih + 36);
      ctx.textAlign = 'left';
    };
    const rowH = rows.length ? areaH / rows.length : 0;
    rows.forEach((z, r) => {
      const n = zones[z].length, gap = 14;
      const w = Math.min((itemsW - gap * (n - 1)) / n, rowH - 50);
      const start = itemsX + (itemsW - (w * n + gap * (n - 1))) / 2;
      zones[z].forEach((p, i) => tile(p, start + i * (w + gap), TOP + r * rowH + 5, w, rowH - 10));
    });
    if (hasSide) {
      const n = zones.side.length, h = Math.min(areaH / n, 220);
      zones.side.forEach((p, i) => tile(p, W - PAD - 170, TOP + i * h, 170, h - 10));
    }
    ctx.fillStyle = '#0F172A'; ctx.font = '700 32px system-ui, sans-serif';
    const total = money(data.total_price);
    ctx.fillText(total, PAD, BOARD_H - 36);
    const tw = ctx.measureText(total).width;
    ctx.fillStyle = '#475569'; ctx.font = '20px system-ui, sans-serif';
    ctx.fillText(`of ${money(data.budget)} budget`, PAD + tw + 12, BOARD_H - 36);
    ctx.fillStyle = '#475569'; ctx.font = '16px system-ui, sans-serif';
    ctx.textAlign = 'right';
    ctx.fillText('Product results powered by AISUM', W - PAD, BOARD_H - 36);
    ctx.textAlign = 'left';

    // Footer: name, line and address on the left; QR code on the right.
    const footY = BOARD_H;
    ctx.fillStyle = '#FFFFFF'; ctx.fillRect(0, footY, W, H - footY);
    ctx.fillStyle = '#E2E8F0'; ctx.fillRect(0, footY, W, 2);
    const mid = footY + (H - footY) / 2;
    ctx.fillStyle = '#0F172A'; ctx.font = '700 44px system-ui, sans-serif';
    ctx.fillText('GhostCart', PAD, mid - 22);
    ctx.fillStyle = '#334155'; ctx.font = '24px system-ui, sans-serif';
    ctx.fillText('Upload a look. Get it on budget.', PAD, mid + 18);
    ctx.fillStyle = '#64748B'; ctx.font = '16px system-ui, sans-serif';
    ctx.fillText(SITE_URL, PAD, mid + 54);
    const qrX = W - PAD - QR, qrY = footY + FOOT_PAD;
    ctx.imageSmoothingEnabled = false;
    ctx.drawImage(qr, qrX, qrY, QR, QR);
    ctx.imageSmoothingEnabled = true;
    ctx.textAlign = 'right';
    ctx.fillStyle = '#475569'; ctx.font = '600 17px system-ui, sans-serif';
    ctx.fillText('Scan with your camera · 用相机扫码打开', qrX - 18, mid + 6);
    ctx.textAlign = 'left';

    const blob = await new Promise((res) => c.toBlob(res, 'image/png'));
    const a = el('a', { href: URL.createObjectURL(blob), download: 'ghostcart-lookboard.png' });
    document.body.append(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 5000);
    const missing = slots.filter((s) => !imgs[s]).length;
    $('replan-msg').textContent = missing
      ? `Saved; ${missing} image(s) could not be loaded and are blank.`
      : 'Saved as ghostcart-lookboard.png.';
  } catch (err) {
    $('replan-msg').textContent = `Could not build the image: ${err.message}`;
  } finally {
    btn.disabled = false;
  }
}
$('download').addEventListener('click', downloadBoard);

nav.start();
