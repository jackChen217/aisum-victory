/* /admin/stats — team-only usage dashboard.
 *
 * The page is served as a 404 and stays a plain "Not Found" unless /api/admin/stats
 * answers, which it does only for a whitelisted teammate signed in on this site.
 * There is no sign-in button here on purpose: sign in on /ghostcart, then open this.
 * Everything drawn is an aggregate; the server never sends an email or a name.
 */
import { initializeApp } from 'https://www.gstatic.com/firebasejs/12.4.0/firebase-app.js';
import { getAuth, onAuthStateChanged } from 'https://www.gstatic.com/firebasejs/12.4.0/firebase-auth.js';

const $ = (id) => document.getElementById(id);
const NS = 'http://www.w3.org/2000/svg';

const SERIES = [
  { key: 'outfit', label: 'Outfit', color: 'var(--series-1)' },
  { key: 'desk', label: 'Desk', color: 'var(--series-2)' },
  { key: 'room', label: 'Room', color: 'var(--series-3)' },
  { key: 'request_log', label: 'All types (request log)', color: 'var(--series-history)' },
];

let data = null;

// ── Fetch ────────────────────────────────────────────────────────────────

async function load(user) {
  const token = await user.getIdToken();
  const r = await fetch('/api/admin/stats', { headers: { Authorization: `Bearer ${token}` } });
  if (!r.ok) return;  // not a teammate: leave the 404 as it is
  render(await r.json());
}

async function main() {
  let config;
  try {
    config = await (await fetch('/api/firebase-config')).json();
  } catch {
    return;
  }
  const auth = getAuth(initializeApp(config));
  onAuthStateChanged(auth, (user) => { if (user) load(user); });
  $('refresh').addEventListener('click', () => auth.currentUser && load(auth.currentUser));
}

// ── Formatting ───────────────────────────────────────────────────────────

const fmtDay = (iso) => {
  const [, m, d] = iso.split('-');
  return `${Number(m)}/${Number(d)}`;
};
const pct = (x) => (x == null ? '–' : `${Math.round(x * 100)}%`);
const el = (tag, attrs = {}, text) => {
  const node = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  if (text != null) node.textContent = text;
  return node;
};

// Four whole-number grid steps with room above the largest value (counts, never 2.5 users).
function niceMax(v) {
  const want = (v + 1) / 4;
  const p = 10 ** Math.floor(Math.log10(Math.max(1, want)));
  const step = [1, 2, 5, 10].map((m) => m * p).find((s) => s >= want);
  return step * 4;
}

// ── Page ─────────────────────────────────────────────────────────────────

function render(d) {
  data = d;
  document.title = 'GhostCart usage';
  $('notfound').hidden = true;
  $('dash').hidden = false;

  const generated = new Date(d.generated_at);
  $('meta').textContent = `${d.days.length} days from ${fmtDay(d.days[0])} · days in ${d.tz} · `
    + `computed ${generated.toLocaleString()}`;

  const u = d.users;
  const s = d.searches;
  const totalSearches = SERIES.reduce((n, ser) => n + s[ser.key].reduce((a, b) => a + b, 0), 0);
  const last = d.days.length - 1;
  const approx = u.approximate ? '<span class="badge">approx.</span>' : '';
  $('tiles').innerHTML = [
    tile('Valid users', u.total + approx, `+${u.new[last]} today`),
    tile('Searches since 11/8', totalSearches,
      `${SERIES.reduce((n, ser) => n + s[ser.key][last], 0)} today`),
    tile('Returning users', d.returning.count,
      `${pct(d.returning.share)} of valid users · ${d.returning.active} searched at least once`),
  ].join('');

  $('users-hint').textContent = u.approximate
    ? 'Approximate: counted from each user’s first plan, not from sign-ups.'
    : 'From the Firebase Auth user list.';

  $('legend-searches').innerHTML = SERIES
    .filter((ser) => ser.key !== 'request_log' || s.request_log.some(Boolean))
    .map((ser) => `<span style="--c:${ser.color}">${ser.label}</span>`).join('');

  const notes = [...d.notes];
  if (s.structured_since) {
    notes.push(`Per-user, per-type logging started ${new Date(s.structured_since).toLocaleString()}. `
      + 'Returning users only count plans since then.');
  } else {
    notes.push('No per-user plan logs yet (they start with the deploy that adds them), so '
      + 'returning users and the Outfit / Desk / Room split are empty until then.');
  }
  for (const ev of d.events) {
    if (ev.placeholder) notes.push(`“${ev.label}” is drawn on ${fmtDay(ev.date)}, a placeholder date.`);
  }
  const errors = Object.entries(d.errors).map(([k, v]) => `Couldn’t read ${k}: ${v}`);
  $('notes').innerHTML = '';
  for (const text of errors) $('notes').append(Object.assign(document.createElement('li'), { textContent: text, className: 'err' }));
  for (const text of notes) $('notes').append(Object.assign(document.createElement('li'), { textContent: text }));
  $('notes-card').hidden = !notes.length && !errors.length;

  requestAnimationFrame(drawAll);  // after layout, so the charts get the real width
  renderTable(d);
}

function tile(label, value, detail) {
  return `<div class="tile"><div class="label">${label}</div><div class="value">${value}</div>`
    + `<div class="detail">${detail}</div></div>`;
}

function renderTable(d) {
  const head = ['Day', 'New users', 'Cumulative', ...SERIES.map((s) => s.label)];
  const rows = d.days.map((day, i) => [fmtDay(day), d.users.new[i], d.users.cumulative[i],
    ...SERIES.map((s) => d.searches[s.key][i])]);
  $('table').innerHTML = `<thead><tr>${head.map((h) => `<th>${h}</th>`).join('')}</tr></thead>`
    + `<tbody>${rows.map((r) => `<tr>${r.map((c) => `<td>${c}</td>`).join('')}</tr>`).join('')}</tbody>`;
}

// ── Charts ───────────────────────────────────────────────────────────────

function drawAll() {
  if (!data) return;
  const d = data;
  chart($('chart-cumulative'), d, [{ label: 'Valid users', color: 'var(--series-1)', values: d.users.cumulative }], 'line');
  chart($('chart-new'), d, [{ label: 'New valid users', color: 'var(--series-1)', values: d.users.new }], 'bars');
  chart($('chart-searches'), d, SERIES.map((s) => ({ label: s.label, color: s.color, values: d.searches[s.key] }))
    .filter((s) => s.values.some(Boolean) || s.label !== SERIES[3].label), 'stack');
}

function chart(host, d, series, kind) {
  host.innerHTML = '';
  const W = Math.max(300, host.clientWidth);
  const H = W < 520 ? 200 : 240;
  const m = { t: 26, r: 12, b: 26, l: 34 };
  const iw = W - m.l - m.r;
  const ih = H - m.t - m.b;
  const n = d.days.length;
  const totals = d.days.map((_, i) => (kind === 'stack'
    ? series.reduce((a, s) => a + s.values[i], 0)
    : Math.max(...series.map((s) => s.values[i]))));
  const max = niceMax(Math.max(1, ...totals));
  const band = iw / n;
  const x = (i) => m.l + band * (i + 0.5);
  const y = (v) => m.t + ih - (v / max) * ih;

  const svg = el('svg', { viewBox: `0 0 ${W} ${H}`, role: 'img', 'aria-label': series.map((s) => s.label).join(', ') });

  // Grid and y labels: four steps.
  for (let k = 0; k <= 4; k++) {
    const v = (max / 4) * k;
    svg.append(el('line', { class: k ? 'grid' : 'axis', x1: m.l, x2: W - m.r, y1: y(v), y2: y(v) }));
    svg.append(el('text', { x: m.l - 6, y: y(v) + 4, 'text-anchor': 'end' }, Number.isInteger(v) ? v : v.toFixed(1)));
  }
  // X labels: thin out so they never collide.
  const every = Math.ceil(n / Math.max(1, Math.floor(iw / 44)));
  const lastShown = Math.floor((n - 1) / every) * every;
  d.days.forEach((day, i) => {
    if (i % every === 0 || (i === n - 1 && (i - lastShown) * band >= 40)) svg.append(el('text', { x: x(i), y: H - 8, 'text-anchor': 'middle' }, fmtDay(day)));
  });

  // Event annotations.
  for (const ev of d.events) {
    const i = d.days.indexOf(ev.date);
    if (i < 0) continue;
    svg.append(el('line', { class: 'event-line', x1: x(i), x2: x(i), y1: m.t - 4, y2: m.t + ih }));
    const right = x(i) > W * 0.6;
    svg.append(el('text', { class: 'event-label', x: x(i) + (right ? -5 : 5), y: m.t - 10, 'text-anchor': right ? 'end' : 'start' },
      W < 520 ? (ev.short || ev.label) : ev.label + (ev.placeholder ? ' (placeholder date)' : '')));
  }

  const bw = Math.max(2, Math.min(28, band * 0.62));
  if (kind === 'line') {
    const s = series[0];
    const pts = s.values.map((v, i) => `${x(i)},${y(v)}`).join(' ');
    svg.append(el('polyline', { points: pts, fill: 'none', stroke: s.color, 'stroke-width': 2, 'stroke-linejoin': 'round' }));
    s.values.forEach((v, i) => svg.append(el('circle', { cx: x(i), cy: y(v), r: n > 20 ? 2.5 : 4, fill: s.color, stroke: 'var(--surface-1)', 'stroke-width': 2 })));
    const lastI = n - 1;
    svg.append(el('text', { x: x(lastI), y: y(s.values[lastI]) - 9, 'text-anchor': 'end', style: 'fill:var(--text-primary);font-weight:600' }, s.values[lastI]));
  } else {
    d.days.forEach((_, i) => {
      let base = 0;
      const stack = kind === 'stack' ? series : series.slice(0, 1);
      const live = stack.filter((s) => s.values[i] > 0);
      live.forEach((s, j) => {
        const v = s.values[i];
        const top = y(base + v);
        const h = y(base) - top - (j < live.length - 1 ? 0 : 0);
        const gap = j > 0 ? 2 : 0;  // surface gap between stacked segments
        svg.append(roundedTop(x(i) - bw / 2, top, bw, Math.max(0, h - gap), j === live.length - 1 ? Math.min(4, bw / 2) : 0, s.color));
        base += v;
      });
    });
  }

  // Hover: a hit band per day, crosshair and a tooltip with every series' value.
  const cross = el('line', { class: 'cross', y1: m.t, y2: m.t + ih, visibility: 'hidden' });
  svg.append(cross);
  const tip = $('tip');
  d.days.forEach((day, i) => {
    const hit = el('rect', { x: m.l + band * i, y: m.t, width: band, height: ih, fill: 'transparent' });
    hit.addEventListener('pointerenter', () => {
      cross.setAttribute('x1', x(i)); cross.setAttribute('x2', x(i)); cross.setAttribute('visibility', 'visible');
      const ev = d.events.filter((e) => e.date === day);
      tip.innerHTML = `<div class="d">${fmtDay(day)}</div>`
        + series.map((s) => `<div class="r" style="--c:${s.color}"><span><i></i>${s.label}</span><b>${s.values[i]}</b></div>`).join('')
        + (kind === 'stack' ? `<div class="r"><span>Total</span><b>${totals[i]}</b></div>` : '')
        + ev.map((e) => `<div class="ev">${e.label}${e.placeholder ? ' (placeholder)' : ''}</div>`).join('');
      tip.hidden = false;
    });
    hit.addEventListener('pointermove', (e) => {
      const left = Math.min(e.clientX + 14, window.innerWidth - tip.offsetWidth - 8);
      tip.style.left = `${left}px`;
      tip.style.top = `${e.clientY + 14}px`;
    });
    hit.addEventListener('pointerleave', () => { tip.hidden = true; cross.setAttribute('visibility', 'hidden'); });
    svg.append(hit);
  });

  host.append(svg);
}

function roundedTop(x, y, w, h, r, fill) {
  r = Math.min(r, h);
  const p = `M${x},${y + h} V${y + r} Q${x},${y} ${x + r},${y} H${x + w - r} Q${x + w},${y} ${x + w},${y + r} V${y + h} Z`;
  return el('path', { d: p, fill });
}

let resizeTimer;
window.addEventListener('resize', () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(drawAll, 120); });

// For local previews without a Google sign-in: render a given payload.
window.__renderStats = render;

main();
