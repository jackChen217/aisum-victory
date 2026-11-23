# Gallery v2 — research and plan (stage 1, no code yet)

Branch `gallery-v2` (worktree `../aisum-gallery-v2`), cut from `main` at `3437d42`
(which already has the result-page dots, lookboard QR and `/admin/stats`). Checked 2026-11-22.

## A. What exists today

| Question | Answer | Where |
| --- | --- | --- |
| Does Desk / Room have a Gallery? | **No.** `/ghostcart` has one gallery row, Outfit only. (`/classic` has three text "Try:" buttons, one of them a gaming desk; not a gallery.) | `static/ghostcart.html` 136–147, `static/classic.html` 183–186 |
| How many looks? | **8**, all Outfit: 4 Women, 4 Men, public Unsplash photos. | `GALLERY` in `static/ghostcart.js` |
| Live AISUM call on click? | **Yes, every time.** `useLook()` sets the photo and calls `generate()`, which runs a full `POST /api/plan` (image search + up to 3 crop re-searches + knapsack). Nothing is cached; nothing runs on page load. | `static/ghostcart.js` `useLook`, `generate` |
| Is a gallery click logged differently? | No. It is an ordinary `/api/plan`, logged by `usage.log_plan` like a user's own photo. | `src/main.py` |

## B. Off-the-shelf components

All facts below were checked on 2026-11-22: GitHub REST API (licence, last commit on
the default branch, issues search), npm registry (version, licence, publish date), the
README, and the published `.d.ts` files on unpkg for the options named.

### B1. Spotlight / product tour

| | **Driver.js** `nilbuild/driver.js` (npm `driver.js` 1.14.0) | **Shepherd.js** `shipshapecode/shepherd` (npm `shepherd.js` 16.0.0) | **Intro.js** `usablica/intro.js` (npm `intro.js` 8.7.0) |
| --- | --- | --- | --- |
| What the README says it does | Tours, highlights that "dim the page around any element", hints, checklists; "No dependencies… works with any framework"; keyboard control, focus management, screen-reader announcements; themeable with CSS classes | Custom tours, minimal styles; wrappers for React/Ember/Angular/Vue/ES modules | Step-by-step tours; "No runtime dependencies" |
| Licence | **MIT**: "free for personal and commercial use"; keep the copyright notice in the vendored file | **AGPL-3.0 or paid commercial licence**: README says "Commercial license required for commercial products" | **AGPL-3.0 or paid commercial licence**: README says buy one "to use Intro.js in a commercial website… without releasing your source code" |
| Last commit / release | 2026-11-14 / 1.14.0 published 2026-11-13 | 2026-11-22 / 2026-11-11 | 2026-11-20 / 2026-11-20 |
| Popularity / open items | 27.2k stars, 1 open issue+PR | 13.8k stars, 15 open | 23.5k stars, 43 open |
| Known issues (searched "mobile", "scroll") | All closed: close button clicking through on mobile (#247), horizontal scrollbar on mobile (#286), element hidden behind fixed header after scroll (#580), driver inside a scrollable div (#577). Fixed but areas to test on our sticky header. | not checked further (licence rules it out) | not checked further (licence rules it out) |
| Matches our needs | Spotlight + popover ✔, steps ✔, `onDestroyed` / `onDestroyStarted` hooks to record "seen" ✔, `allowClose` (skip) ✔, `side` per step (popover placement) ✔, `showProgress` ✔, `overlayOpacity`, `stagePadding`, `popoverClass` for our styling ✔ | similar | similar |
| Missing (we write it) | "Seen once per account" storage, choosing desktop vs phone steps, starting only after sign-in | — | — |
| Integration cost | Low: one ES module + one CSS file vendored into `static/vendor/` (no bundler in this repo, no npm runtime dep); ~1 small module of ours for steps | — | — |
| Risk | Our sticky header / gallery carousel positions: test on phone. | Licence | Licence |

**Recommendation: use Driver.js directly** (vendored, pinned to 1.14.0). It is MIT,
dependency-free and actively maintained, and it already has the spotlight, steps, skip
and the hooks we need. Shepherd.js and Intro.js are excluded on licence: AGPL plus a paid
commercial licence is a bad fit for a hosted product. I found no fourth plain-JS, MIT
candidate worth listing. Reactour (MIT, 4.1k stars, last commit 2026-05-19, 81 open
items) is React-only, and this page has no React.

### B2. Gallery carousel (arrows, wrap-around, swipe)

| | **Embla Carousel** `davidjerleke/embla-carousel` (npm `embla-carousel` 8.6.0) | **Swiper** `nolimits4web/swiper` (npm `swiper` 14.4.0) | **Glide.js** `glidejs/glide` (npm `@glidejs/glide` 3.7.1) |
| --- | --- | --- | --- |
| README | "lightweight, dependency-free… precision swiping… framework-agnostic"; MIT | "mobile touch slider with hardware accelerated transitions"; 1:1 touch, rich API incl. navigation buttons, tree-shakeable, RTL | "dependency-free… ~23kb (~7kb gzipped)" |
| Options/API checked in `.d.ts` | `loop`, `startIndex`, `duration`, `slidesToScroll`, `watchDrag`; `scrollNext()`, `scrollPrev()`, `scrollTo()`, `selectedScrollSnap()`, `slidesInView()`, `on(...)` | (not checked further) | (not checked further) |
| Licence | MIT | MIT | MIT |
| Last commit / release | 2026-10-12 / 8.6.0 published 2026-10-08 | 2026-10-30 / 2026-10-30 | 2026-03-21 / 3.7.1 published 2024-11-22 |
| Open items / known issues | 34 open. Open bugs mention AutoScroll and the Accessibility *plugins* (#1422, #1389, #1263), which we would not use. Searching "loop" and "safari" found no core bug. | 239 open. Open loop issues incl. "Auto slides in conjunction with loop causes all sorts of issues" (#7787) and nav buttons disabled on first load (#7969) | 245 open, an open issue titled "is glide Dead?" (#720); no release since 2024-11 |
| Size (npm unpacked) | 0.49 MB package (core is small; most is builds/types) | 3.8 MB package | — |
| Matches our needs | Arrows via `scrollPrev/Next` ✔, wrap-around via `loop` ✔, swipe/drag on touch ✔, "current page + neighbours" via `slidesInView` / `select` event ✔ | All ✔ but heavy for 3 pages | ✔ but stale |
| Missing (we write it) | Arrow buttons and page dots (Embla ships none, by design), lazy thumbnail loading, keyboard arrows | — | — |
| Integration cost | Low: one ESM file vendored, ~60 lines of ours | Medium: large CSS/JS, more to override | Low, but maintenance risk |

**Recommendation: use Embla Carousel core directly** (vendored, pinned to 8.6.0, no
plugins). It is MIT, small and maintained, and has exactly the loop and swipe engine we
need; we add our own arrows and lazy loading. Swiper does everything, but it is far
larger than 3 pages need and has open loop bugs. Glide.js looks unmaintained.

## C. Overall plan

### C1. Gallery click: cached result first, real refresh behind it

1. Click a look → `GET /api/gallery/{look_id}`: the server returns the stored result in
   the same `?format=ui` shape, plus a new `session_id`. The result page shows at once,
   and the budget slider / swaps / Download image all work (see C2).
2. In the same request the server starts **one background refresh** for that look: a
   real `/api/plan`-equivalent run against AISUM. It does not change what this user
   sees. It replaces the stored result for the next viewer, and it is the real usage
   record.
3. **Upload wins:** when the same user uploads a photo or presses Generate, the server
   cancels that user's pending or running refresh first (tasks are keyed by uid), then
   handles the upload. The browser also never waits on the refresh.
4. **Nothing runs on page load.** Thumbnails are static images; no search starts until
   a click.
5. If no stored result exists yet (a new look), fall back to today's behaviour: a live
   plan with the spinner.

### C2. Cache

- **Stored server-side in Firestore** (the project already has Firestore, server-only).
  Cloud Run memory is lost on restart and not shared between instances. One document
  per look, holding the full session (slots, candidates, scores), not just the
  rendered result, so the slider can re-run the knapsack with no new search. Size per
  look is roughly 15–40 KB, well under Firestore's 1 MB document limit.
- **Older than 24 hours is regenerated.** The background refresh on each click keeps
  popular looks fresh. A precompute script (`scripts/precompute_gallery.py`) rebuilds
  every look that is missing or older than 24 h. Run it by hand after a deploy, or from
  Cloud Scheduler daily if you want it automatic (an extra GCP piece; your call).
- **Budget slider:** `/api/replan` already re-optimises from saved candidates with no
  new search; a gallery session is just a session seeded from the stored document.
  The cache is built at a fixed budget (proposed $150, the current default). Moving the
  slider up only reaches products that were found under that cap; the page already
  says this for normal plans.
- New runtime dependency: `google-cloud-firestore` (server only). It is the one new
  runtime package; Driver.js and Embla are static files, not installs.

### C3. A gallery for each of Outfit / Desk / Room

- Each category gets its own gallery row, up to 3 pages (proposed 4 looks per page on
  desktop, so up to 12 per category). Outfit keeps the 8 we have (2 pages).
- Desk and Room need new photos: public Unsplash images, as now. I'll propose 8 for
  each for you to pick from.
- **Desk / Room must be precomputed:** a live run is 30–40 s and uses DeepSeek (slot
  split, per-slot category check). Their background refresh reuses the stored slot
  list and category answers and redoes only the text searches, so a refresh makes no
  LLM call and fits under 10 s. Full regeneration, including the LLM, happens only in
  the 24 h precompute.

### C4. Carousel

> **Changed 2026-11-22 after trying it:** no carousel. The home page keeps the old grid with
> Outfit only (12 looks); Desk / Room looks replace it once that category is picked. Embla
> was removed. The tour is 3 steps on a computer and 2 on a phone.

- Left/right arrows, up to 3 pages. Right arrow on the last page goes back to page 1,
  and left on page 1 goes to the last page (Embla `loop: true`). Arrows hide when a
  category has only one page.
- The current page's thumbnails load immediately; the neighbouring pages' thumbnails
  are preloaded in the background (`loading="lazy"` swapped for eager on the select
  event); further pages load when reached. With 3 pages, both other pages are
  neighbours.
- Phone: swipe left/right (Embla drag); arrows stay as well. Keyboard ←/→ when the
  carousel has focus.

### C5. First-visit tour

- **Shown once per account:** after sign-in, the page asks `GET /api/me/onboarding`.
  Finishing or skipping the tour calls `POST /api/me/onboarding` (Driver.js
  `onDestroyed`), stored in Firestore under the uid only (no email). That covers a
  changed device. `localStorage` is only a fast path to avoid a flash.
- **Desktop steps (5):** category chips → photo upload → budget → gallery arrows →
  Generate.
- **Phone steps (3):** photo / gallery → budget → Generate.
- **Optional:** a 2-step mini tour on the first result page (numbered dots → cart tile
  opens the store).

### C6. Logging

`usage.log_plan` gains a `trigger` field:

| `trigger` | When |
| --- | --- |
| `user` | the user's own photo or Generate |
| `gallery_view` | a click served from cache (no AISUM call) |
| `gallery_refresh` | the background refresh (AISUM called) |
| `precompute` | the script or scheduler |

`/admin/stats` keeps counting `user` searches as today. Gallery views and refreshes get
their own line, so the search numbers don't jump just because the gallery got more
traffic.

### C7. AISUM limits — will we hit them, and how to avoid it

Our team's live limits, read from AISUM `/status` on 2026-11-22. They are per team and
"may change during the event":

| Tool | Limit |
| --- | --- |
| `search_products_by_image` | **3 at once** |
| `search_products_by_text` | **8 at once** |
| `start_scene_match` | 3 now, +1 every 10 s |
| `get_scene_match_result` | 8 at once |
| `start_video_match` | 5 now, +1 every 30 s |
| `get_video_match_result` | 8 at once |

- **We can hit it already today.** One Outfit plan is 1 image search, then up to 3 crop
  re-searches running together. Two users generating at the same moment can need 6
  image searches against a limit of 3, and AISUM answers `RATE_LIMITED`. Gallery
  refreshes on every click would make this much more likely.
- **Fix, in the app:**
  - A process-wide limiter in `planner.search_image` / `search_text`: 3 image and 8
    text slots.
  - Two priorities: user requests wait for a slot. Background refreshes only
    *try* to take one: if none is free they are skipped and logged as
    `gallery_refresh_skipped`, never queued in front of a user.
  - At most 1 refresh at a time per instance, and at most 1 per look every 10 minutes:
    repeated clicks on the same look do not stack.
  - Precompute runs one look at a time.
- **Caveat:** the limiter is per Cloud Run instance; the AISUM limit is per team. With
  several instances, the per-instance limit can't stop them together from going over.
  Options: cap the service at 1 instance (simplest; this traffic fits), or accept the
  rare `RATE_LIMITED`, which is already waited out and retried once by `McpClient.call_waiting_once`.
  Your call.

### Decisions needed before stage 2

1. Show the tour to **existing** accounts once too, or **only accounts created after
   this ships**? The second needs the account creation time, which the ID token does
   not carry: either the Admin SDK user lookup (the stats session couldn't confirm
   that permission) or "first time this uid is seen by our server".
2. Daily automatic regeneration with **Cloud Scheduler**, or refresh-on-click plus the
   hand-run script?
3. Cap Cloud Run at **1 instance** (keeps the limiter exact), or accept occasional
   `RATE_LIMITED` across instances?
4. Cache budget: **$150** for all three categories, or per category (e.g. Desk/Room
   $350)?
5. Desk / Room photos: I'll propose candidates; do you want to pick them?
