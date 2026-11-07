const $ = (id) => document.getElementById(id);
const BUILD = 'outfit-three-way-v2';
const MODES = [['raw', 'A · AISUM 原始第一名'], ['budget', 'B · 只加价格上限第一名'], ['enhanced', 'C · 实验增强＋整套预算']];
const hasResult = (e) => Boolean(e.data || Object.values(e.variants || {}).some((v) => v.plan));
const price = (n) => n == null ? '价格未知' : money(n);
const elapsed = (v) => ((v.timings_ms?.including_reused_search ?? v.timings_ms?.total ?? 0) / 1000).toFixed(1);
const MAX_FILES = 10, MAX_BYTES = 8 * 1024 * 1024;
const money = (n) => `$${Number(n).toFixed(2)}`;
const label = (s) => s.replace(/_/g, ' ');
let files = [], run = null, busy = false, stop = false, owner = null, exporting = false;

function status(text) { $('status').textContent = text; }
function button(text, action) { const b = document.createElement('button'); b.textContent = text; b.onclick = action; return b; }
function text(tag, content, className) { const e = document.createElement(tag); e.textContent = content; if (className) e.className = className; return e; }
function controls() {
  $('start').disabled = busy || !files.length || !owner;
  $('stop').disabled = !busy; $('pick').disabled = busy;
  for (const id of ['budget', 'scenario', 'gender', 'test-mode', 'clear-history']) $(id).disabled = busy;
  $('scenario').disabled = busy || $('test-mode').value === 'triple';
  const ready = Boolean(run?.entries.some(hasResult));
  $('download-all').disabled = busy || exporting || !ready;
  $('export').disabled = busy || !run;
}
async function token(expectedOwner = owner) {
  const t = await window.__auth?.token();
  if (!t || owner !== expectedOwner || window.__auth?.user?.uid !== expectedOwner) throw new Error('请先登录。');
  return t;
}
async function request(path, body, type = 'application/json', expectedOwner = owner) {
  const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 140000);
  try {
    const r = await fetch(path, { method: 'POST', body: type === 'application/json' ? JSON.stringify(body) : body,
      signal: controller.signal, headers: { Authorization: `Bearer ${await token(expectedOwner)}`, 'Content-Type': type } });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || data.detail?.[0]?.msg || `请求失败：HTTP ${r.status}`);
    return data;
  } finally { clearTimeout(timer); }
}

// Only this browser stores these previews/results; each signed-in user has a separate history.
const database = new Promise((resolve, reject) => {
  const r = indexedDB.open('ghostcart-batch-v1', 1);
  r.onupgradeneeded = () => r.result.createObjectStore('runs', { keyPath: 'id' }).createIndex('uid', 'uid');
  r.onsuccess = () => resolve(r.result); r.onerror = () => reject(r.error);
});
database.catch(() => {});
async function records(uid) {
  const db = await database;
  return new Promise((resolve, reject) => {
    const r = db.transaction('runs').objectStore('runs').index('uid').getAll(uid);
    r.onsuccess = () => resolve(r.result.sort((a, b) => b.created - a.created)); r.onerror = () => reject(r.error);
  });
}
async function writeRecord(value) {
  const db = await database;
  await new Promise((resolve, reject) => {
    const tx = db.transaction('runs', 'readwrite'); tx.objectStore('runs').put(value);
    tx.oncomplete = resolve; tx.onerror = () => reject(tx.error);
  });
  const old = (await records(value.uid)).slice(5);
  if (old.length) await removeRecords(old);
}
async function removeRecords(values) {
  const db = await database;
  return new Promise((resolve, reject) => {
    const tx = db.transaction('runs', 'readwrite');
    for (const value of values) tx.objectStore('runs').delete(value.id);
    tx.oncomplete = resolve; tx.onerror = () => reject(tx.error);
  });
}
async function persist() {
  if (!run || run.uid !== owner) return;
  try { await writeRecord(run); } catch { status('本浏览器无法保存记录，请下载 JSON 和对比图留存。'); }
}
async function history() {
  $('history').replaceChildren();
  if (!owner) return;
  const uid = owner;
  try {
    const values = await records(uid);
    if (uid !== owner) return;
    for (const saved of values) $('history').append(button(
      `${new Date(saved.created).toLocaleString()} · ${saved.entries.filter(hasResult).length}/${saved.entries.length} 张`,
      () => { if (busy) return; run = saved; files = []; render(); controls(); status('已打开本地记录。要重新测试，请重新选择原图。'); }));
    if (!values.length) $('history').append(text('span', '还没有已保存的测试。', 'meta'));
  } catch { $('history').append(text('span', '浏览器未开放本地记录存储，请下载结果留存。', 'meta')); }
}

async function preview(file) {
  const image = await createImageBitmap(file);
  try {
    const scale = Math.min(1, 1000 / Math.max(image.width, image.height));
    const c = document.createElement('canvas'); c.width = Math.max(1, Math.round(image.width * scale)); c.height = Math.max(1, Math.round(image.height * scale));
    const ctx = c.getContext('2d'); ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, c.width, c.height); ctx.drawImage(image, 0, 0, c.width, c.height);
    return c.toDataURL('image/jpeg', .85);
  } finally { image.close(); }
}
function select(chosen) {
  if (busy) return;
  const items = [...chosen];
  if (!items.length) return;
  if (items.length > MAX_FILES) { status('一次最多 10 张，请减少图片后重选。'); return; }
  const invalid = items.find((f) => !['image/jpeg', 'image/png'].includes(f.type) || f.size > MAX_BYTES);
  if (invalid) { status(`${invalid.name} 不是 JPG/PNG，或超过 8 MB。请检查后重选。`); return; }
  files = items; run = null; $('results').replaceChildren(); $('queue').replaceChildren();
  for (const f of files) $('queue').append(text('span', f.name));
  status(`已选择 ${files.length} 张。每张独立使用上面的预算。`); controls();
}
$('test-mode').onchange = controls;
$('pick').onclick = () => $('files').click();
$('files').onchange = (e) => { select(e.target.files); e.target.value = ''; };
for (const ev of ['dragenter', 'dragover']) $('drop').addEventListener(ev, (e) => { e.preventDefault(); $('drop').classList.add('over'); });
for (const ev of ['dragleave', 'drop']) $('drop').addEventListener(ev, (e) => { e.preventDefault(); $('drop').classList.remove('over'); });
$('drop').addEventListener('drop', (e) => select(e.dataTransfer.files));
$('stop').onclick = () => { stop = true; status('会完成当前这张，然后停止；已完成结果会保留。'); };

function productGrid(plan, provenance = {}) {
  const products = document.createElement('div'); products.className = 'products';
  for (const [slot, p] of Object.entries(plan)) {
    const a = document.createElement('a'); a.className = 'product';
    if (/^https?:\/\//.test(p.click_url || '')) { a.href = p.click_url; a.target = '_blank'; a.rel = 'noopener'; }
    const img = document.createElement('img'); img.alt = label(slot); img.loading = 'lazy'; if (/^https?:\/\//.test(p.img_url || '')) img.src = p.img_url;
    const source = provenance[slot];
    const origin = source?.native_rank ? `原生第 ${source.native_rank} 名` : source?.image_matches?.length ? `图片候选第 ${source.image_matches[0].native_rank} 名` : source?.source === 'extra_search' ? '补充搜索商品' : '';
    a.append(img, text('small', label(slot)), text('div', p.product_name || '', 'name'), text('strong', price(p.product_price)), text('small', origin)); products.append(a);
  }
  return products;
}
function renderEntry(entry, index) {
  const card = document.createElement('section'); card.className = 'card result'; card.id = `result-${index}`;
  const head = document.createElement('div'); head.className = 'result-head'; head.append(text('h2', `${index + 1}. ${entry.name}`));
  if (hasResult(entry)) head.append(button('下载这张对比图', async (event) => {
    const b = event.currentTarget; b.disabled = true;
    try { const c = await board(entry); await downloadCanvas(c, `ghostcart-${index + 1}.png`); }
    catch (err) { status(`下载失败：${err.message}`); } finally { b.disabled = false; }
  }));
  card.append(head, text('p', entry.message, entry.error ? 'meta error' : 'meta'));
  const comparison = document.createElement('div'); comparison.className = entry.variants ? 'comparison triple' : 'comparison';
  const source = document.createElement('div'); source.className = 'source-column';
  const original = document.createElement('img'); original.className = 'original'; original.alt = `${entry.name} 原图`; if (entry.preview) original.src = entry.preview;
  source.append(text('h3', '输入原图'), original); comparison.append(source);
  if (entry.variants) {
    for (const [mode, title] of MODES) {
      const panel = document.createElement('div'); panel.className = 'variant'; panel.append(text('h3', title));
      const v = entry.variants[mode];
      panel.append(text('p', mode === 'raw' ? '不加价格或性别条件；每个检测项取第 1 个。' : mode === 'budget' ? `只加每件 ≤ ${money(run.settings.budget)}；整套可能超预算。` : '审核补漏、文字补搜、颜色/文字重排，再约束整套预算。', 'meta'));
      if (v?.plan) {
        panel.append(text('p', `${v.unknown_prices ? '已知价格合计 ' : '合计 '}${money(v.total)} · ${elapsed(v)} 秒`, 'variant-total'));
        if (v.unknown_prices) panel.append(text('p', `${v.unknown_prices} 件价格未知，不能判断完整总价。`, 'error'));
        if (v.over_budget) panel.append(text('p', '已知总价超过整套预算', 'error'));
        if (v.infeasible?.length) panel.append(text('p', `无候选／未配齐：${v.infeasible.map((x) => label(x.slot)).join('、')}`, 'error'));
        if (!Object.keys(v.plan).length) panel.append(text('p', v.report?.note || '没有找到商品。', 'error'));
        for (const warning of v.warnings || []) panel.append(text('p', `部分增强未完成：${warning}`, 'error'));
        panel.append(productGrid(v.plan, v.provenance));
        const detail = document.createElement('details'); detail.append(text('summary', '参数、检测项、排名与耗时'), text('pre', JSON.stringify({ request_args: v.request_args, slots: v.slots, provenance: v.provenance, timings_ms: v.timings_ms, report: v.report }, null, 2))); panel.append(detail);
      } else panel.append(text('p', v?.error || '等待处理', v?.error ? 'error' : 'meta'));
      comparison.append(panel);
    }
  } else if (entry.data) {
    comparison.append(productGrid(entry.data.plan)); card.append(text('p', `旧版记录 · ${money(entry.data.total)} / ${money(run.settings.budget)} · ${entry.seconds.toFixed(1)} 秒。此记录没有 A/B/C 原始响应，不能补算成三组结果。`, 'meta'));
    if (entry.data.infeasible?.length) card.append(text('p', `未配齐：${entry.data.infeasible.map((x) => label(x.slot)).join('、')}`, 'error'));
  } else comparison.append(text('p', entry.error || entry.message, entry.error ? 'error' : 'meta'));
  card.append(comparison); return card;
}
function render() {
  $('results').replaceChildren(); $('queue').replaceChildren();
  for (const [i, e] of (run?.entries || []).entries()) {
    const a = document.createElement('a'); a.href = `#result-${i}`; a.textContent = `${i + 1} · ${e.message}`; $('queue').append(a);
    $('results').append(renderEntry(e, i));
  }
}
$('start').onclick = async () => {
  const budget = Number($('budget').value);
  if (!Number.isFinite(budget) || budget <= 0 || budget > 100000) { status('预算需大于 0，且不超过 $100,000。'); return; }
  if (busy || !files.length || !owner) return;
  const selected = [...files], uid = owner;
  busy = true; stop = false;
  run = { id: crypto.randomUUID(), uid, created: Date.now(), version: BUILD,
    settings: { budget, scenario: $('scenario').value, gender: $('gender').value || null, test_mode: $('test-mode').value },
    entries: selected.map((f) => ({ name: f.name, message: '等待处理', preview: null, ...($('test-mode').value === 'triple' ? { variants: {} } : {}) })) };
  controls(); render();
  for (let i = 0; i < selected.length && !stop && owner === uid; i++) {
    const entry = run.entries[i], started = performance.now();
    try {
      entry.preview = await preview(selected[i]);
      if (owner !== uid) break;
      entry.message = '上传中'; render();
      const upload = await request('/api/upload', selected[i], selected[i].type, uid);
      if (owner !== uid) break;
      entry.url = upload.url; entry.message = '识别并搜索商品中'; render(); status(`处理中：${i + 1}/${selected.length} · ${entry.name}`);
      if (entry.variants) {
        for (const [mode, title] of MODES) {
          if (owner !== uid) break;
          entry.message = `正在运行 ${title}`; render(); status(`${i + 1}/${selected.length} · ${entry.name} · ${title}`);
          try {
            const data = await request('/api/compare/outfit', { image_url: upload.url, budget,
              mode, comparison_id: entry.comparison_id || null,
              gender: mode === 'enhanced' ? run.settings.gender : null }, 'application/json', uid);
            if (owner !== uid) break;
            entry.comparison_id = data.comparison_id; entry.variants[mode] = data;
          } catch (err) {
            if (owner !== uid) break;
            entry.variants[mode] = { error: err.message };
          }
          render(); await persist();
        }
        if (owner !== uid) break;
        const succeeded = Object.values(entry.variants).filter((v) => v.plan).length;
        entry.message = succeeded === 3 ? '三组已完成' : `部分完成（${succeeded}/3 组）`;
        if (!succeeded) entry.error = '三组均失败；请查看各组错误。';
      } else {
        const data = await request('/api/plan', { image_url: upload.url, scenario: run.settings.scenario,
          budget, gender: run.settings.gender }, 'application/json', uid);
        if (owner !== uid) break;
        entry.data = data; entry.message = '已完成';
      }
      entry.seconds = (performance.now() - started) / 1000;
    } catch (err) {
      if (owner !== uid) break;
      entry.error = err.message; entry.message = '处理失败';
    }
    render(); await persist();
  }
  busy = false;
  if (owner === uid && run) {
    for (const e of run.entries) if (e.message === '等待处理') e.message = '未处理（已停止）';
    render(); await persist(); await history();
    const ok = run.entries.filter(hasResult).length, failed = run.entries.filter((e) => e.error).length;
    status(`${stop ? '已停止' : '本批完成'}：成功 ${ok} 张，失败 ${failed} 张，未处理 ${selected.length - ok - failed} 张。`);
  }
  controls();
};

function download(blob, name) {
  const url = URL.createObjectURL(blob), a = document.createElement('a'); a.href = url; a.download = name; a.click(); setTimeout(() => URL.revokeObjectURL(url), 10000);
}
function downloadCanvas(c, name) { return new Promise((resolve, reject) => c.toBlob((blob) => { if (!blob) { reject(new Error('无法生成图片')); return; } download(blob, name); resolve(); }, 'image/png')); }
$('export').onclick = () => {
  if (!run) return;
  const { uid, ...value } = run;
  download(new Blob([JSON.stringify(value, null, 2)], { type: 'application/json' }), `ghostcart-batch-${run.id}.json`);
};
async function bitmap(entry, slot) {
  if (slot === '_photo') return createImageBitmap(await (await fetch(entry.preview)).blob());
  const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 10000);
  try {
    const r = await fetch(`/api/lookboard-image?session_id=${encodeURIComponent(entry.data.session_id)}&slot=${encodeURIComponent(slot)}`,
      { headers: { Authorization: `Bearer ${await token()}` }, signal: controller.signal });
    if (!r.ok) return null;
    return await createImageBitmap(await r.blob());
  } catch { return null; } finally { clearTimeout(timer); }
}
function contain(ctx, img, x, y, w, h) {
  if (!img) { ctx.fillStyle = '#78716c'; ctx.font = '15px sans-serif'; ctx.fillText('Image unavailable', x + 10, y + h / 2); return; }
  const scale = Math.min(w / img.width, h / img.height); ctx.drawImage(img, x + (w - img.width * scale) / 2, y + (h - img.height * scale) / 2, img.width * scale, img.height * scale);
}
async function legacyBoard(entry, budget = run?.settings.budget) {
  if (!entry.data) throw new Error('这张图片尚未生成方案');
  const pairs = Object.entries(entry.data.plan), keys = ['_photo', ...pairs.map(([s]) => s)];
  const images = await Promise.all(keys.map((s) => bitmap(entry, s)));
  try {
    const c = document.createElement('canvas'); c.width = 1200; c.height = Math.max(800, 170 + Math.ceil(pairs.length / 3) * 210);
    const ctx = c.getContext('2d'); ctx.fillStyle = '#fafaf9'; ctx.fillRect(0, 0, c.width, c.height);
    ctx.fillStyle = '#1c1917'; ctx.font = 'bold 28px sans-serif'; ctx.fillText(`GhostCart · ${entry.name.slice(0, 48)}`, 30, 45);
    contain(ctx, images[0], 30, 85, 450, c.height - 165);
    pairs.forEach(([slot, p], i) => {
      const x = 510 + (i % 3) * 220, y = 85 + Math.floor(i / 3) * 210;
      ctx.fillStyle = '#fff'; ctx.fillRect(x, y, 200, 160); contain(ctx, images[i + 1], x + 5, y + 5, 190, 150);
      ctx.fillStyle = '#57534e'; ctx.font = '14px sans-serif'; ctx.fillText(label(slot).slice(0, 23), x, y + 180);
      ctx.fillStyle = '#1c1917'; ctx.font = 'bold 17px sans-serif'; ctx.fillText(money(p.product_price), x, y + 201);
    });
    ctx.fillStyle = '#1c1917'; ctx.font = 'bold 25px sans-serif'; ctx.fillText(`${money(entry.data.total)} / ${money(budget)}   ${entry.seconds.toFixed(1)}s`, 30, c.height - 30);
    const missing = images.filter((x) => !x).length;
    if (missing) status(`${entry.name}：${missing} 张商品图未加载，导出图保留对应名称和价格。`);
    return c;
  } finally { for (const image of images) image?.close(); }
}
async function variantBitmap(entry, mode, slot) {
  const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 10000);
  try {
    const params = new URLSearchParams({ comparison_id: entry.variants[mode].comparison_id, variant: mode, slot });
    const r = await fetch(`/api/comparison-image?${params}`, { headers: { Authorization: `Bearer ${await token()}` }, signal: controller.signal });
    return r.ok ? await createImageBitmap(await r.blob()) : null;
  } catch { return null; } finally { clearTimeout(timer); }
}
function boardHeight(entry) {
  if (!entry.variants) return Math.max(800, 170 + Math.ceil(Object.keys(entry.data.plan).length / 3) * 210);
  const count = Math.max(0, ...MODES.map(([m]) => Object.keys(entry.variants[m]?.plan || {}).length));
  return Math.max(900, 270 + Math.ceil(count / 2) * 225);
}
async function board(entry, budget = run?.settings.budget) {
  if (!entry.variants) return legacyBoard(entry, budget);
  const c = document.createElement('canvas'); c.width = 2100; c.height = boardHeight(entry);
  const ctx = c.getContext('2d'); ctx.fillStyle = '#fafaf9'; ctx.fillRect(0, 0, c.width, c.height);
  ctx.fillStyle = '#1c1917'; ctx.font = 'bold 28px sans-serif'; ctx.fillText(`GhostCart A/B/C · ${entry.name.slice(0, 70)} · Budget ${money(budget)}`, 25, 40);
  const original = await bitmap(entry, '_photo');
  try { contain(ctx, original, 25, 105, 370, c.height - 180); } finally { original?.close(); }
  let unavailable = 0;
  for (let col = 0; col < MODES.length; col++) {
    const [mode, title] = MODES[col], v = entry.variants[mode], x = 420 + col * 555;
    ctx.fillStyle = '#1c1917'; ctx.font = 'bold 23px sans-serif'; ctx.fillText(title, x, 85);
    ctx.font = '16px sans-serif';
    ctx.fillText(mode === 'raw' ? 'No filters; top 1 per detection' : mode === 'budget' ? 'Per-item price cap; top 1 per detection' : 'Experimental review + reranking + total budget', x, 114);
    if (!v?.plan) { ctx.fillStyle = '#b91c1c'; ctx.fillText((v?.error || 'Not completed').slice(0, 60), x, 160); continue; }
    ctx.fillText(`${money(v.total)}${v.unknown_prices ? ' + unknown prices' : ''} | ${elapsed(v)}s`, x, 145);
    ctx.fillStyle = '#b91c1c';
    ctx.fillText(v.over_budget ? 'OVER TOTAL BUDGET' : v.infeasible?.length ? `${v.infeasible.length} unfilled item(s)` : '', x, 169);
    if (v.warnings?.length) ctx.fillText('Some enhancement steps failed; see JSON', x, 190);
    const pairs = Object.entries(v.plan);
    // Limit simultaneous image downloads; comparison stages themselves are sequential.
    for (let offset = 0; offset < pairs.length; offset += 4) {
      const chunk = pairs.slice(offset, offset + 4);
      const images = await Promise.all(chunk.map(([slot]) => variantBitmap(entry, mode, slot)));
      try {
        chunk.forEach(([slot, p], j) => {
          const i = offset + j, px = x + (i % 2) * 270, y = 215 + Math.floor(i / 2) * 225;
          ctx.fillStyle = '#fff'; ctx.fillRect(px, y, 250, 160); contain(ctx, images[j], px + 5, y + 5, 240, 150);
          if (!images[j]) unavailable++;
          ctx.fillStyle = '#57534e'; ctx.font = '14px sans-serif'; ctx.fillText(label(slot).slice(0, 30), px, y + 180);
          ctx.fillStyle = '#1c1917'; ctx.font = 'bold 17px sans-serif'; ctx.fillText(price(p.product_price), px, y + 202);
        });
      } finally { images.forEach((img) => img?.close()); }
    }
  }
  ctx.fillStyle = '#57534e'; ctx.font = '16px sans-serif'; ctx.fillText('A/B keep all detections (including paired shoes). C merges paired items; its time includes the reused B search.', 25, c.height - 25);
  if (unavailable) status(`${entry.name}：${unavailable} 张商品图未加载；导出保留品类和价格。`);
  return c;
}
$('download-all').onclick = async () => {
  if (!run || exporting) return;
  exporting = true; controls();
  try {
    const entries = run.entries.filter(hasResult), budget = run.settings.budget, c = document.createElement('canvas'); c.width = entries.some((e) => e.variants) ? 1500 : 1000;
    const heights = entries.map((e) => Math.round(boardHeight(e) * c.width / (e.variants ? 2100 : 1200)));
    c.height = heights.reduce((a, b) => a + b, 0); const ctx = c.getContext('2d'); let y = 0;
    for (let i = 0; i < entries.length; i++) {
      status(`正在导出对比图：${i + 1}/${entries.length}`);
      const single = await board(entries[i], budget); ctx.drawImage(single, 0, y, c.width, heights[i]); y += heights[i];
    }
    await downloadCanvas(c, 'ghostcart-batch-comparisons.png'); status(`已下载 ${entries.length} 张成功结果的对比长图。`);
  } catch (err) { status(`导出失败：${err.message}`); }
  finally { exporting = false; controls(); }
};
$('clear-history').onclick = async () => { if (busy || !owner) return; try { await removeRecords(await records(owner)); await history(); status('已删除当前账号的本地测试记录。'); } catch (err) { status(err.message); } };
window.addEventListener('authchange', (e) => {
  const next = e.detail.user?.uid || null;
  if (next !== owner) { stop = true; owner = next; files = []; run = null; render(); controls(); history(); }
});
owner = window.__auth?.user?.uid || null; controls(); history();
