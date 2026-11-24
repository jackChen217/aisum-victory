"""Direct API for the Finch marketplace: one outfit photo + budget -> shoppable items.

    GET  /api/finch/health   anonymous, {"status": "ok", ...}
    POST /api/finch/plan     Authorization: Bearer <FINCH_API_TOKEN>
                             {"image_url", "budget_usd"?, "gender"?} -> items in budget

The plan is the website's outfit path, unchanged (orchestrator.plan -> outfit.candidates:
one AISUM image search, left/right shoe merged, crop re-search for weak items, unmatched
items set aside, knapsack within budget). What differs:

- No Google sign-in: one shared bearer token from the env var FINCH_API_TOKEN, compared
  in constant time. Rotate it by changing the env var. Unset -> every request is 503.
- AISUM searches run inside limits.finch(): they wait for a slot, but a website user who
  is waiting is always served first (src/agent/limits.py).
- Because that wait counts against the outfit deadline, Finch gets OUTFIT_DEADLINE_S
  instead of the website's 12 s; the whole request is cut at TOTAL_TIMEOUT_S (504).
- Sessions are made under the service uid FINCH_UID and removed right after the
  response is built, so Finch never grows the session store.
- At most MAX_CONCURRENT Finch plans at once; more get 429.
- One `ghostcart_finch` log line per call (src/usage.py), not counted by /admin/stats.

Schemas: docs/finch/plan.request.schema.json, plan.response.schema.json.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import time
from typing import Annotated, Literal
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import usage
from .agent import limits, orchestrator
from .agent.outfit import PlanTimeout
from .agent.planner import PlanError
from .llm_client import LlmError
from .mcp_client import McpError

log = logging.getLogger(__name__)

TOKEN_ENV = "FINCH_API_TOKEN"
FINCH_UID = "_finch"
OPEN_URL = "https://app-103901851610.us-central1.run.app/ghostcart?src=finch"
TOTAL_TIMEOUT_S = 110.0     # under Finch's 120 s, with room to build and send the reply
OUTFIT_DEADLINE_S = 60.0    # first image search incl. waiting behind website users
MAX_CONCURRENT = 4
MIN_BUDGET, MAX_BUDGET = 10, 2000
DEFAULT_BUDGET = 200.0      # when budget_usd is left out
API_VERSION = "1"

router = APIRouter(prefix="/api/finch")
_running = 0


class FinchPlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    image_url: str = Field(max_length=2048)
    # Optional (default DEFAULT_BUDGET). strict: a number, not "100", true or null.
    budget_usd: Annotated[float, Field(strict=True, ge=MIN_BUDGET, le=MAX_BUDGET)] = DEFAULT_BUDGET
    # Omitted = no gender filter. null is refused, like the schema says.
    gender: Literal["F", "M"] = None  # type: ignore[assignment]

    @field_validator("image_url")
    @classmethod
    def _https_url(cls, v: str) -> str:
        parts = urlsplit(v)
        if parts.scheme != "https" or not parts.hostname or any(c.isspace() for c in v):
            raise ValueError("image_url must be a public https URL of a JPEG or PNG photo")
        return v


def _error(status: int, message: str, code: str, headers: dict | None = None) -> JSONResponse:
    return JSONResponse({"error": message, "code": code}, status_code=status, headers=headers)


def _authorized(authorization: str) -> JSONResponse | None:
    """None if the bearer token matches FINCH_API_TOKEN, else the error response."""
    expected = os.environ.get(TOKEN_ENV, "").strip()
    if not expected:
        return _error(503, "The Finch API is not configured.", "NOT_CONFIGURED")
    scheme, _, given = authorization.partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(
            given.strip().encode(), expected.encode()):
        return _error(401, "Missing or invalid bearer token.", "UNAUTHORIZED",
                      {"WWW-Authenticate": "Bearer"})
    return None


def to_finch(data: dict, budget: float) -> dict:
    """orchestrator.plan output -> the Finch response (plan.response.schema.json).
    Names, prices, images and click_url are AISUM's, untouched."""
    slots = {s["slot"]: s for s in data.get("slots") or []}
    items = []
    for slot, p in data["plan"].items():
        s = slots.get(slot, {})
        items.append({
            "category": s.get("item") or slot,
            "description": s.get("query"),
            "product_name": p.get("product_name") or "",
            "brand": p.get("brand") or None,
            "price_usd": p["product_price"],
            "image_url": p.get("img_url"),
            "click_url": p.get("click_url"),
            "similarity_score": p.get("score"),
        })
    unmatched = []
    for d in data.get("dropped") or []:
        if d.get("reason") != "unmatched":
            continue
        s = slots.get(d["slot"], {})
        unmatched.append({"category": s.get("item") or d["slot"],
                          "description": s.get("query"),
                          "message": d.get("message") or "No similar product found."})
    return {"api_version": API_VERSION, "budget_usd": budget,
            "total_usd": round(data["total"], 2), "currency": "USD",
            "items": items, "unmatched": unmatched, "open_url": OPEN_URL}


async def _plan(req: FinchPlanRequest) -> dict:
    data = await orchestrator.plan(FINCH_UID, req.image_url, "outfit", req.budget_usd,
                                   gender=req.gender, outfit_deadline_s=OUTFIT_DEADLINE_S,
                                   gender_required=False)
    # Finch never re-plans: drop the session now so Finch can't grow the store.
    orchestrator.SESSIONS.pop((FINCH_UID, data["session_id"]), None)
    return to_finch(data, req.budget_usd)


@router.get("/health")
async def health():
    return {"status": "ok", "service": "ghostcart-finch", "api_version": API_VERSION,
            "configured": bool(os.environ.get(TOKEN_ENV, "").strip())}


@router.post("/plan")
async def plan(request: Request, authorization: str = Header(default="")):
    """Auth runs before the body is validated, so a caller without the token learns
    nothing about the input format (401/503 before 422)."""
    global _running
    started = time.perf_counter()
    outcome, items, unmatched, gender = "error", 0, 0, None

    def done(resp: JSONResponse | dict):
        code = resp.status_code if isinstance(resp, JSONResponse) else 200
        usage.log_finch(code, outcome, (time.perf_counter() - started) * 1000,
                        items, unmatched, gender)
        return resp

    if (refused := _authorized(authorization)) is not None:
        outcome = "unauthorized" if refused.status_code == 401 else "not_configured"
        return done(refused)
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        outcome = "invalid"
        return done(_error(422, "The body must be JSON.", "INVALID_INPUT"))
    try:
        req = FinchPlanRequest.model_validate(body)
    except ValueError as e:      # pydantic.ValidationError is a ValueError
        outcome = "invalid"
        detail = getattr(e, "errors", lambda: [])()
        return done(JSONResponse(
            {"error": "Invalid input.", "code": "INVALID_INPUT",
             "detail": [{"loc": list(x.get("loc", ())), "msg": x.get("msg")} for x in detail]},
            status_code=422))
    gender = req.gender
    if _running >= MAX_CONCURRENT:
        outcome = "busy"
        return done(_error(429, "Too many Finch requests at once. Retry shortly.",
                           "BUSY", {"Retry-After": "10"}))
    _running += 1
    try:
        with limits.finch():
            result = await asyncio.wait_for(_plan(req), TOTAL_TIMEOUT_S)
        outcome, items, unmatched = "ok", len(result["items"]), len(result["unmatched"])
        return done(result)
    except (PlanTimeout, TimeoutError):
        outcome = "timeout"
        return done(_error(504, f"The plan did not finish within {TOTAL_TIMEOUT_S:g} "
                                "seconds. Please retry.", "TIMEOUT"))
    except PlanError as e:
        outcome = "no_items"
        return done(_error(422, str(e), "NO_ITEMS"))
    except McpError as e:
        outcome = "upstream_error"
        if e.code == "RATE_LIMITED":
            return done(_error(503, "Product search is busy. Retry shortly.", e.code,
                               {"Retry-After": str(e.retry_after_s or 10)}))
        return done(_error(502, f"Product search failed: {e}", e.code))
    except (LlmError, httpx.HTTPError) as e:
        outcome = "upstream_error"
        return done(_error(502, f"{type(e).__name__}: {e}", "UPSTREAM_ERROR"))
    except Exception:
        log.exception("finch plan failed")
        return done(_error(500, "Internal error.", "INTERNAL"))
    finally:
        _running -= 1
