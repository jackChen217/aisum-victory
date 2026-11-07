# GhostCart architecture

How one plan is made, and what a shopper can do with it. Code: `src/main.py` (routes),
`src/agent/orchestrator.py` (the flow), `src/agent/planner.py` (slots and search),
`src/agent/matching.py` (colour and text matching, no LLM), `src/agent/optimizer.py`
(budget knapsack).

## `POST /api/plan`

`scenario` defaults to `auto`: image search runs first and decides. Detections mean an
outfit; none (the tool returns a `note`) means a desk, planned by DeepSeek.

```mermaid
sequenceDiagram
    autonumber
    actor U as Shopper (browser)
    participant API as main.py /api/plan
    participant O as orchestrator
    participant P as planner + matching
    participant A as AISUM MCP
    participant D as DeepSeek (LLM gateway)

    U->>API: image_url, budget, gender?, scenario=auto (Firebase ID token)
    API->>API: require_user → uid
    API->>O: plan(uid, ...)
    O->>P: plan_slots(auto)
    P->>A: search_products_by_image(image_url, max_price, gender)
    A-->>P: detections[] (item, item_detail, box, products) or note

    alt detections → outfit
        P-->>O: one slot per detection (left/right shoe merged)
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
        P->>P: drop background items, then keyword category rules (no LLM)
        P->>P: score: 0.40 rank + 0.35 colour (box crop vs product) + 0.25 BM25/attributes
    else no detections → desk
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
        P->>P: score: rank + BM25/attributes (no colour)
    end

    P-->>O: slots, candidates (best first), quality scores
    O->>O: optimizer: multiple-choice knapsack in cents, total ≤ budget
    O->>D: explain the plan (only words from product names)
    D-->>O: 2–4 sentences (retried / replaced if it invents colours or materials)
    O->>O: save session (uid, session_id): slots, candidates, scores, plan
    O-->>API: plan, match, alternatives, dropped, total, explanation, report
    API-->>U: JSON → product cards, Lookboard (outfit), alternatives
```

`/api/replan` reuses the saved candidates: no search, no review, one explanation call.
`pin: {slot: p_key}` forces that product into its slot and the optimizer spends the
rest of the budget on the other slots.

## Use cases

Mermaid has no use-case diagram type; this flowchart draws one: actors on the sides,
use cases in the middle, `include` where one use case always runs another.

```mermaid
flowchart LR
    shopper(["🧑 Shopper"])
    aisum(["AISUM product search (MCP)"])
    deepseek(["DeepSeek (LLM gateway)"])

    subgraph GhostCart
        direction TB
        upload((Upload image))
        plan((Generate plan))
        budget((Change budget))
        swap((Swap in an alternative))
        board((Download Lookboard))
        search((Search products))
        review((Review outfit / plan desk))
        explain((Explain the plan))
    end

    shopper --- upload
    shopper --- plan
    shopper --- budget
    shopper --- swap
    shopper --- board

    plan -. include .-> search
    plan -. include .-> review
    plan -. include .-> explain
    budget -. include .-> explain
    swap -. include .-> explain

    search --- aisum
    review --- aisum
    review --- deepseek
    explain --- deepseek
```

| Use case | Route | External calls |
| --- | --- | --- |
| Upload image | `POST /api/upload` | Cloud Storage (public for 1 day) |
| Generate plan | `POST /api/plan` | AISUM image + text search; DeepSeek: outfit review or desk slots, desk category checks, explanation |
| Change budget | `POST /api/replan` `{budget}` | DeepSeek: explanation |
| Swap in an alternative | `POST /api/replan` `{pin: {slot: p_key}}` | DeepSeek: explanation |
| Download Lookboard | `GET /api/lookboard-image` per image, canvas → PNG in the browser | the product image hosts, through the server |
