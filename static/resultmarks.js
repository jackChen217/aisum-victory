/* Which boxes and numbered dots the result photo shows, one number per cart item.
 * Pure functions, no DOM, so Node can test them (tests/test_result_marks.py).
 *
 * A cart item (slot) can have several detections:
 *  - a left and a right shoe: two boxes of about the same size, side by side, both kept;
 *  - the same item found twice (pants inside a full-body box), or a stray box elsewhere
 *    (a "handbag" on the next person): only the box AISUM scored best is kept.
 */

const area = (b) => Math.max(0, b.x2 - b.x1) * Math.max(0, b.y2 - b.y1);

/** Share of the smaller box covered by the other: 1 when one sits inside the other. */
export function overlap(a, b) {
  const w = Math.min(a.x2, b.x2) - Math.max(a.x1, b.x1);
  const h = Math.min(a.y2, b.y2) - Math.max(a.y1, b.y1);
  if (w <= 0 || h <= 0) return 0;
  return (w * h) / Math.min(area(a), area(b));
}

// A second box is the other half of a pair (left and right shoe), not a stray, when it
// is about as big, roughly level with the first, and AISUM found products almost as
// good for it. Feet can be far apart, so distance across is not a test.
const PAIR_AREA_RATIO = 1.8;
const PAIR_LEVEL = 0.6;       // centre heights differ by at most this share of the taller box
const PAIR_SCORE = 0.7;       // of the best box's score
const SAME_THING = 0.5;       // overlap() above this: the same thing found twice

function pairsWith(box, other, best) {
  const ratio = area(box) / area(other);
  if (ratio > PAIR_AREA_RATIO || ratio < 1 / PAIR_AREA_RATIO) return false;
  const dy = Math.abs((box.y1 + box.y2) / 2 - (other.y1 + other.y2) / 2);
  const tall = Math.max(box.y2 - box.y1, other.y2 - other.y1);
  return dy <= PAIR_LEVEL * tall && box.score >= PAIR_SCORE * best;
}

/** detections of one slot -> the boxes to show, best first. */
export function keptBoxes(detections) {
  const boxes = detections
    .filter((d) => d.bounding_box)
    .map((d) => ({ ...d.bounding_box, score: d.score ?? 0 }))
    .sort((a, b) => b.score - a.score || area(b) - area(a));
  if (!boxes.length) return [];
  const kept = [boxes[0]];
  for (const box of boxes.slice(1)) {
    if (kept.some((k) => overlap(box, k) > SAME_THING)) continue;
    if (kept.some((k) => pairsWith(box, k, boxes[0].score))) kept.push(box);
  }
  return kept.map(({ score, ...b }) => b);
}

const union = (boxes) => ({
  x1: Math.min(...boxes.map((b) => b.x1)), y1: Math.min(...boxes.map((b) => b.y1)),
  x2: Math.max(...boxes.map((b) => b.x2)), y2: Math.max(...boxes.map((b) => b.y2)),
});

/** ui-format result -> the cart in photo order, top to bottom (hat, top, pants, shoes):
 *  [{ n, product, boxes, dots: [{x, y}] }], each dot at the top centre of a box. Two
 *  shoes close together share one dot over both; feet far apart get one dot each, so
 *  no dot lands on the floor between them. */
export function cartMarks(data) {
  const bySlot = {};
  for (const d of data.detected_items || []) (bySlot[d.slot] ||= []).push(d);
  const top = (b) => ({ x: (b.x1 + b.x2) / 2, y: b.y1 });
  const items = (data.matched_products || []).map((product, i) => {
    const boxes = keptBoxes(bySlot[product.slot] || []);
    let dots = boxes.map(top);
    if (boxes.length > 1) {
      const all = union(boxes);
      const widths = boxes.reduce((sum, b) => sum + (b.x2 - b.x1), 0);
      if (all.x2 - all.x1 <= widths * 1.5) dots = [top(all)];
    }
    return { product, boxes, dots, order: i,
             y: boxes.length ? Math.min(...boxes.map((b) => b.y1)) : 2 };
  });
  // Items with no box go last, in the order the plan listed them.
  items.sort((a, b) => a.y - b.y || a.order - b.order);
  return items.map(({ order, y, ...item }, i) => ({ n: i + 1, ...item }));
}

/** Dots in pixels -> the same dots moved down until none is closer than `gap` to one
 *  placed before it, so two items with boxes starting at the same height stay readable.
 *  The y is clamped to `maxY` (the photo's height less a dot). */
export function spreadDots(points, gap, maxY = Infinity) {
  const placed = [];
  for (const p of points) {
    const q = { ...p };
    for (let tries = 0; tries < 12; tries += 1) {
      const hit = placed.find((o) => Math.hypot(o.x - q.x, o.y - q.y) < gap);
      if (!hit) break;
      q.y = Math.min(maxY, hit.y + gap);
      if (q.y === maxY) { q.x = hit.x + (q.x >= hit.x ? gap : -gap); }
    }
    placed.push(q);
  }
  return placed;
}
