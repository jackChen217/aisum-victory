# GhostCart architecture

How one plan is made, and what a shopper can do with it. Code: `src/main.py` (routes),
`src/agent/orchestrator.py` (the flow), `src/agent/planner.py` (slots and search),
`src/agent/optimizer.py` (budget knapsack), `src/agent/comparison.py` (A/B/C/D test
groups), `src/agent/matching.py` (colour and text matching, used only by the
experimental pipeline).

## `POST /api/plan` (default path)

The user always picks `scenario` (`outfit`, `desk` or `room`); nothing is
auto-detected. An outfit also needs the user's `gender` (`F`, `M` or `UNISEX`).

The outfit path (`src/agent/outfit.py`) makes **no model calls** and has a 12-second
deadline: one image search, a crop re-search for weak items, unmatched items set aside,
then the knapsack. The explanation is a factual template (`GENERATE_EXPLANATIONS =
False` in `orchestrator.py`; set it to `True` for the old LLM explanation).

```mermaid
sequenceDiagram
    autonumber
    actor U as Shopper (browser)
    participant API as main.py /api/plan
    participant O as orchestrator
    participant P as planner
    participant A as AISUM MCP
    participant D as DeepSeek (LLM gateway)

    U->>API: image_url, budget, scenario, gender (outfit), desk_width? (Firebase ID token)
    API->>API: require_user → uid, validate scenario + gender
    API->>O: plan(uid, ...)

    alt scenario = outfit (12 s deadline, no LLM)
        O->>P: outfit.candidates
        P->>A: search_products_by_image(image_url, gender, max_price=budget)
        A-->>P: detections[] (item, item_detail, box, products) or note
        P->>P: one slot per detection, same item + item_detail merged (left/right shoe)
        P->>P: candidates = AISUM order, dedupe by p_key, repair mojibake
        opt slots whose best score < 0.5 (CROP_CUTOFF)
            par at most 3 at once, cancelled near the deadline
                P->>P: crop the best-scoring detection's box grown 10 % a side, upload
                P->>A: search_products_by_image(crop_url, category=item, gender, max_price)
                A-->>P: detections[], keep the better result list
            end
        end
        P->>P: still < 0.45 (UNMATCHED_CUTOFF) → unmatched (not planned, 3 alternatives)
        P->>P: quality = 1/(1+rank)
    else scenario = desk / room
        P->>D: photo → slots JSON (slot, required, weight, query, domain)
        D-->>P: slots (one retry if it does not parse)
        par one text search per slot
            P->>A: search_products_by_text(query, domain, max_price)
            A-->>P: results[]
        end
        par category check, one call per slot (max 4 at once, first 10 candidates)
            P->>D: which candidates are the wrong product type?
            D-->>P: {"reject": [ids]}
        end
        P->>P: score: rank + BM25/attributes
        P->>P: sizes from product names (regex), compared with desk_width
    end

    P-->>O: slots, candidates (best first), quality scores
    O->>O: optimizer: multiple-choice knapsack in cents, whole plan ≤ budget
    O->>O: template explanation (no LLM by default)
    O->>O: save session (uid, session_id): slots, candidates, scores, plan
    O-->>API: plan, match, alternatives, dropped, budget, total, slots with boxes, report, timings_ms
    API-->>U: JSON (or ?format=ui: the frontend's shape) → product cards, Lookboard, alternatives
```

`/api/replan` reuses the saved candidates: no search, only the knapsack again.
`pin: {slot: p_key}` forces that product into its slot and the optimizer spends the
rest of the budget on the other slots.

## Comparison groups (`/ghostcart/batch`, `POST /api/compare/outfit`)

Outfit only. One request per group (`mode`), so a slow or failed group never erases
the earlier ones. The page runs A → B → D → C (C is slowest and can time out) and
shows A/B/C/D.

| Group | `mode` | Image search | Picks | Whole-outfit budget | Left/right shoe | LLM |
| --- | --- | --- | --- | --- | --- | --- |
| A | `raw` | no filters | literal `products[0]` per detection | not enforced | kept apart | none |
| B | `budget` | `max_price=budget` (per item) | literal `products[0]` per detection | not enforced | kept apart | none |
| C | `enhanced` | reuses B's response | old experimental pipeline (below) | knapsack | merged | 1 review call |
| D | `default` | reuses B's response | AISUM order (the default path without crop re-search / unmatched) | knapsack | merged | none |

B's `max_price` caps each item, not the outfit; D is B plus the whole-outfit
knapsack. It equals `/api/plan` with the score cutoff set to 0.
C and D search again (with `gender`) only when the user chose a gender; A and B never
send it. Their `timings_ms.including_reused_search` adds B's search time.

```mermaid
sequenceDiagram
    autonumber
    actor U as Batch page
    participant API as /api/compare/outfit
    participant C as comparison
    participant P as planner + optimizer
    participant A as AISUM MCP

    U->>API: mode=raw
    API->>C: stage(raw)
    C->>A: search_products_by_image(image_url)
    A-->>C: detections[]
    C-->>U: A: products[0] per detection, raw_response
    U->>API: mode=budget, comparison_id
    C->>A: search_products_by_image(image_url, max_price)
    A-->>C: detections[]
    C-->>U: B: products[0] per detection, raw_response
    U->>API: mode=default, comparison_id
    C->>P: B's response → merged slots → AISUM order → knapsack
    P-->>C: plan ≤ budget
    C-->>U: D
    U->>API: mode=enhanced, comparison_id
    C->>P: B's response → merged slots → enhanced pipeline → knapsack
    P-->>C: plan ≤ budget (or 504 after 120 s, A/B/D kept)
    C-->>U: C
```

### Experimental pipeline (group C only)

`planner.outfit_candidates(..., enhanced=True)`. Not the default.

```mermaid
sequenceDiagram
    participant P as planner + matching
    participant A as AISUM MCP
    participant D as DeepSeek (LLM gateway)

    par review and work that does not depend on it
        P->>D: review_outfit: photo + detections → keep / correct / drop / missing
        D-->>P: JSON (on failure: keep detections as they are)
    and early text search per detection
        P->>A: search_products_by_text(item_detail, domain=fashion, gender, max_price)
        A-->>P: results[]
    and photo + product images
        P->>P: download photo, product images via image proxy (128px, cached) → Lab palettes
    end
    opt review corrected or added items
        par one per corrected / added slot
            P->>A: search_products_by_text(precise description, fashion, gender, max_price)
            A-->>P: results[] (corrected: ranked before image results)
        end
    end
    P->>P: drop background items, then keyword category rules
    P->>P: score: 0.40 rank + 0.35 colour (box crop vs product) + 0.25 BM25/attributes
```

## Use cases

Mermaid has no use-case diagram type; this flowchart draws one: actors on the sides,
use cases in the middle, `include` where one use case always runs another.

```mermaid
flowchart LR
    shopper(["🧑 Shopper"])
    tester(["🧪 Team tester"])
    aisum(["AISUM product search (MCP)"])
    deepseek(["DeepSeek (LLM gateway)"])

    subgraph GhostCart
        direction TB
        upload((Upload image))
        plan((Generate plan))
        budget((Change budget))
        swap((Swap in an alternative))
        board((Download Lookboard))
        compare((Compare A/B/C/D in batch))
        search((Search products))
        desk((Plan desk slots))
    end

    shopper --- upload
    shopper --- plan
    shopper --- budget
    shopper --- swap
    shopper --- board
    tester --- compare

    plan -. include .-> search
    plan -. include .-> desk
    compare -. include .-> upload
    compare -. include .-> search

    search --- aisum
    desk --- deepseek
    compare --- deepseek
```

| Use case | Route | External calls |
| --- | --- | --- |
| Upload image | `POST /api/upload` | Cloud Storage (private, signed URL 60 min, deleted after 1 day) |
| Generate plan | `POST /api/plan` | outfit: AISUM image search (+ crop searches), Cloud Storage for crops; desk / room: DeepSeek (slots, category checks) and AISUM text search |
| Change budget | `POST /api/replan` `{budget}` | none by default |
| Swap in an alternative | `POST /api/replan` `{pin: {slot: p_key}}` | none by default |
| Download Lookboard | `GET /api/lookboard-image` per image, canvas → PNG in the browser | the product image hosts, through the server |
| Compare A/B/C/D | `POST /api/compare/outfit` per group, `GET /api/comparison-image` for the PNG | AISUM image search (A, B; C/D only with gender); C also AISUM text search and one DeepSeek review |
