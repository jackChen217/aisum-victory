"""Start here. Run:  .venv/bin/python -m uvicorn src.main:app --reload  → http://localhost:8000

HTTP routes. The browser talks only to this server — never to MCP or the gateway.
Two reasons: the team's API keys must not reach the browser, and the MCP server
sends no CORS headers, so a direct fetch from the page would be blocked anyway.

Every /api/* route except /api/firebase-config needs a signed-in user (src/auth.py).

  /api/search/text    search_products_by_text        — one call
  /api/search/image   search_products_by_image        — one call
  /api/scene/start +
  /api/scene/result   start_scene_match / get_scene_match_result — two steps,
                       the browser polls /api/scene/result itself (see static/app.js)
  /api/video/start +
  /api/video/result   start_video_match / get_video_match_result — the same two steps
  /api/llm/check      one LLM gateway call
  /api/tool-call      one tool call, end to end — the model asks, this app runs it on
                       MCP, the model answers

GhostCart (src/agent/, page at /ghostcart):
  /api/plan           photo + scenario + budget -> slots, optimized plan, explanation
  /api/replan         same session, new budget / owned -> re-optimized plan + changes
  /api/upload         JPEG/PNG bytes (max 8 MB) -> public Cloud Storage URL
  /api/lookboard-image  a session's photo or planned product image, proxied so the
                       page can export the Lookboard canvas as PNG
"""
import asyncio
import hashlib
import json
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

import httpx
from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator

from . import image_proxy, llm_client, uploads
from .agent import comparison, orchestrator, planner, ui_format
from .agent.orchestrator import SessionNotFound
from .agent.outfit import PlanTimeout
from .agent.planner import PlanError
from .auth import require_user
from .config import (FIREBASE_API_KEY, FIREBASE_APP_ID, FIREBASE_AUTH_DOMAIN,
                     FIREBASE_MESSAGING_SENDER_ID, FIREBASE_PROJECT_ID,
                     FIREBASE_STORAGE_BUCKET, ROOT)
from .llm_client import LlmError
from .mcp_client import McpClient, McpError

STATIC = ROOT / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.mcp = McpClient()
    planner.use_client(app.state.mcp)  # one MCP session for the whole app
    yield
    await app.state.mcp.aclose()


# Cloud Run's source build stamps every file 1980, so without this browsers treat
# pages and scripts as fresh for a very long time and keep old versions after a deploy.
REVALIDATE = {"Cache-Control": "no-cache"}


class RevalidatedStatic(StaticFiles):
    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers.update(REVALIDATE)
        return response


def asset_version(directory: Path) -> str:
    """Short hash of every script and stylesheet under `directory`: it changes exactly
    when one of them does, so each deploy gets new asset URLs without editing a number.
    (.git is not uploaded to Cloud Run, so there is no commit hash to use.)"""
    digest = hashlib.sha256()
    for path in sorted(directory.rglob("*")):
        if path.suffix in (".js", ".css") and path.is_file():
            digest.update(path.relative_to(directory).as_posix().encode() + b"\0")
            digest.update(path.read_bytes())
    return digest.hexdigest()[:10]


ASSET_VERSION = asset_version(STATIC)
# Our own scripts and stylesheets in a page: src="/static/x.js" or href="/static/x.css".
_ASSET_REF = re.compile(r'((?:src|href)="/static/[^"?#]+\.(?:js|css))(?:\?v=[^"]*)?"')


def page(name: str) -> HTMLResponse:
    """A page with ?v=<ASSET_VERSION> on every /static script and stylesheet."""
    html = (STATIC / name).read_text(encoding="utf-8")
    return HTMLResponse(_ASSET_REF.sub(rf'\1?v={ASSET_VERSION}"', html), headers=REVALIDATE)


app = FastAPI(title="AISUM Hackathon Starter Kit", lifespan=lifespan)
app.mount("/static", RevalidatedStatic(directory=STATIC), name="static")


def _mcp_error(e: McpError, **extra) -> JSONResponse:
    raw = e.raw if e.raw is None or isinstance(e.raw, str) else json.dumps(e.raw)
    return JSONResponse({"error": f"{e.code}: {e}", "raw": raw and raw[:500], **extra},
                        status_code=502)


# ── Page and Firebase config (no sign-in needed) ─────────────────────────

# HEAD too: uptime checks, link previews and `curl -I` send HEAD, and FastAPI's
# @app.get answers it with 405.
PAGE_METHODS = ["GET", "HEAD"]


# WeChat's domain appeal checks for this one file at the site root.
@app.api_route("/102905f7b945d56af40e18fb57d9d994.txt", methods=PAGE_METHODS, include_in_schema=False)
def wechat_verification() -> PlainTextResponse:
    return PlainTextResponse("49660d71b2a5f9874ff53248f7bb5402979df318")


@app.api_route("/", methods=PAGE_METHODS)
async def index():
    return page("index.html")


@app.api_route("/ghostcart", methods=PAGE_METHODS)
async def ghostcart():
    return page("ghostcart.html")


@app.api_route("/classic", methods=PAGE_METHODS)
async def classic():
    """The previous look of /ghostcart, same features and API."""
    return page("classic.html")


@app.api_route("/ghostcart/batch", methods=PAGE_METHODS)
async def ghostcart_batch():
    return page("ghostcart-batch.html")


@app.get("/api/firebase-config")
async def firebase_config():
    """The one place the browser learns which Firebase project it's talking to.

    static/auth.js fetches this before calling initializeApp — so switching project
    (personal account -> company project -> your team's project) is an env-var
    change on this server, never a code change in the browser bundle.
    """
    return {
        "apiKey": FIREBASE_API_KEY,
        "authDomain": FIREBASE_AUTH_DOMAIN,
        "projectId": FIREBASE_PROJECT_ID,
        "storageBucket": FIREBASE_STORAGE_BUCKET,
        "messagingSenderId": FIREBASE_MESSAGING_SENDER_ID,
        "appId": FIREBASE_APP_ID,
    }


# ── 1–2. One-call MCP tools ──────────────────────────────────────────────
# Field names match the tool's arguments, so the request body passes straight through.
# `uid` is the verified Firebase user — unused here, it's where per-user features hook in.

# The tool accepts more than this. Your team documentation lists the rest.
class TextSearch(BaseModel):
    query: str


@app.post("/api/search/text")
async def search_text(req: TextSearch, uid: str = Depends(require_user)):
    try:
        return await app.state.mcp.call("search_products_by_text",
                                         req.model_dump(exclude_none=True))
    except McpError as e:
        return _mcp_error(e)


# The tool accepts more than this. Your team documentation lists the rest.
class ImageSearch(BaseModel):
    image_url: str


@app.post("/api/search/image")
async def search_image(req: ImageSearch, uid: str = Depends(require_user)):
    try:
        return await app.state.mcp.call("search_products_by_image",
                                         req.model_dump(exclude_none=True))
    except McpError as e:
        return _mcp_error(e)


# ── 3–4. Two-step MCP tools: start once, then ask again with the same task_id ──

class SceneStart(BaseModel):
    title: str
    image_url: str


@app.post("/api/scene/start")
async def scene_start(req: SceneStart, uid: str = Depends(require_user)):
    try:
        # Scene requests can be rate limited. A short wait is waited out once; a long one is raised.
        return await app.state.mcp.call_waiting_once("start_scene_match", req.model_dump())
    except McpError as e:
        return _mcp_error(e)


@app.get("/api/scene/result/{task_id}")
async def scene_result(task_id: str, uid: str = Depends(require_user)):
    return await _poll("get_scene_match_result", task_id)


class VideoStart(BaseModel):
    video_url: str


@app.post("/api/video/start")
async def video_start(req: VideoStart, uid: str = Depends(require_user)):
    try:
        # A new video can be rate limited. Short wait: waited out once. Long wait: raised.
        return await app.state.mcp.call_waiting_once("start_video_match", req.model_dump())
    except McpError as e:
        if e.code == "RATE_LIMITED":
            return _mcp_error(e, hint="New videos are busy. Use a pre-analysed video "
                                      "from docs/videos.md — those work immediately.")
        return _mcp_error(e)


@app.get("/api/video/result/{task_id}")
async def video_result(task_id: str, uid: str = Depends(require_user)):
    return await _poll("get_video_match_result", task_id)


async def _poll(tool: str, task_id: str):
    """One ask. The browser calls this repeatedly with the SAME task_id — see
    static/app.js's polling loop. Nothing here ever starts the job again.
    """
    try:
        return await app.state.mcp.call(tool, {"task_id": task_id})
    except McpError as e:
        # A job that has not started yet can answer NOT_FOUND instead of pending.
        # Treat it as pending: obeying the literal message would mean starting the
        # job again, forever. No record of "tasks we started" is kept — on Cloud Run
        # the next ask may reach another instance, which would not have it.
        if e.code == "NOT_FOUND":
            return {"status": "pending", "task_id": task_id}
        return _mcp_error(e)


# ── 5. One LLM call ──────────────────────────────────────────────────────

class LlmCheck(BaseModel):
    prompt: str = "Say hello in one short sentence."


@app.post("/api/llm/check")
async def llm_check(req: LlmCheck, uid: str = Depends(require_user)):
    try:
        choice = await llm_client.chat([{"role": "user", "content": req.prompt}])
    except (LlmError, httpx.HTTPError) as e:
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=502)
    return {"text": choice["message"].get("content"),
            "finish_reason": choice["finish_reason"]}


# ── 6. One tool call, end to end ─────────────────────────────────────────

DEMO_TOOL = "search_products_by_text"


class ToolCallDemo(BaseModel):
    question: str


@app.post("/api/tool-call")
async def tool_call(req: ToolCallDemo, uid: str = Depends(require_user)):
    """One tool call, end to end — the four steps an agent repeats, done once.

    A connection check, not a pattern to copy: write your own loop or use any SDK or
    framework — the messages on the wire are the same, and this shows them. No loop,
    no memory, one tool. `report` is only for the page; the four numbered blocks are
    the wiring.
    """
    report: list[dict] = []
    try:
        tools = [await _tool_for_llm(DEMO_TOOL)]
        messages = [{"role": "user", "content": req.question}]

        # 1  model — in: the question + the tool's schema.
        #    out: an answer, or tool_calls = [{id, function: {name, arguments}}],
        #    where `arguments` is a JSON *string* the model wrote.
        t = time.perf_counter()
        reply = await llm_client.chat(messages, tools)
        calls = reply["message"].get("tool_calls") or []
        report.append(_step(1, t, finish_reason=reply["finish_reason"],
                            tool_calls=[{"id": c["id"], **c["function"]} for c in calls],
                            content=reply["message"].get("content")))
        if not calls:
            return {"steps": report, "note": "The model answered without calling the "
                    "tool, so there is nothing for steps 2-4 to do."}

        # 2  app — run the first requested call on MCP. A model may ask for several
        #    at once; running them all would already be a design decision.
        call = calls[0]
        name, args = call["function"]["name"], json.loads(call["function"]["arguments"])
        t = time.perf_counter()
        result = await app.state.mcp.call(name, args)
        report.append(_step(2, t, tool=name, arguments=args, result=result))

        # 3  app — send back the model's own message unchanged (it may carry
        #    reasoning fields), then the result as role "tool" with the SAME call id.
        messages += [reply["message"],
                     {"role": "tool", "tool_call_id": call["id"], "content": json.dumps(result)}]
        report.append(_step(3, messages_sent=[m["role"] for m in messages],
                            tool_content_chars=len(messages[-1]["content"])))

        # 4  model — in: everything so far. out: an answer in words.
        t = time.perf_counter()
        final = await llm_client.chat(messages, tools)
        report.append(_step(4, t, finish_reason=final["finish_reason"],
                            content=final["message"].get("content"),
                            # A loop would run this next call; this demo stops here.
                            asked_for_another_call=bool(final["message"].get("tool_calls"))))
        return {"steps": report}
    except McpError as e:
        return _mcp_error(e, steps=report)
    except json.JSONDecodeError as e:
        # The model's `arguments` is a string it wrote; nothing guarantees it parses.
        return JSONResponse({"steps": report, "error": f"The model's tool arguments are "
                                                       f"not valid JSON: {e}"},
                            status_code=502)
    except (LlmError, httpx.HTTPError) as e:
        return JSONResponse({"steps": report, "error": f"{type(e).__name__}: {e}"},
                            status_code=502)


async def _tool_for_llm(name: str) -> dict:
    """An MCP tool, in the shape a chat call's `tools` list expects:
    {"type": "function", "function": {name, description, parameters}}.

    MCP's `inputSchema` and the chat API's `parameters` are both JSON Schema, so the
    schema passes through unchanged.
    """
    available = await app.state.mcp.list_tools()
    spec = next((t for t in available if t["name"] == name), None)
    if spec is None:
        raise McpError(f"No tool named {name!r} on the server. Available: "
                       f"{', '.join(t['name'] for t in available)}", code="UNKNOWN_TOOL")
    return {"type": "function", "function": {
        "name": spec["name"], "description": spec.get("description", ""),
        "parameters": spec["inputSchema"]}}


def _step(n: int, started: float | None = None, **fields) -> dict:
    """One entry of the page's report: the step number, seconds since `started`."""
    if started is not None:
        fields["seconds"] = round(time.perf_counter() - started, 2)
    return {"step": n, **fields}


# ── GhostCart: plan and re-plan ──────────────────────────────────────────

class PlanRequest(BaseModel):
    image_url: str
    # The user always chooses; there is no auto-detect.
    scenario: Literal["outfit", "desk", "room"]
    budget: float = Field(gt=0, le=100_000)
    owned: list[str] = []
    notes: str = Field(default="", max_length=2000)
    # The user's own choice. Required for outfit (Women / Men / Unisex), unused otherwise.
    gender: Literal["F", "M", "UNISEX"] | None = None
    # Desk only: the user's desk width; product sizes are read from product names.
    desk_width: float | None = Field(default=None, gt=0, le=1000)
    desk_width_unit: Literal["in", "cm"] = "in"

    @model_validator(mode="after")
    def _outfit_needs_gender(self):
        if self.scenario == "outfit" and self.gender is None:
            raise ValueError("gender is required for an outfit: F, M or UNISEX")
        return self


def _shaped(data: dict, uid: str, fmt: str | None) -> dict:
    """`?format=ui`: the frontend's shape (src/agent/ui_format.py), else as is."""
    if fmt != "ui":
        return data
    return ui_format.to_ui(data, orchestrator.SESSIONS[(uid, data["session_id"])])


@app.post("/api/plan")
async def plan(req: PlanRequest, uid: str = Depends(require_user),
               format: Literal["ui"] | None = None):
    try:
        data = await orchestrator.plan(uid, req.image_url, req.scenario, req.budget,
                                       req.owned, req.notes, req.gender,
                                       req.desk_width, req.desk_width_unit)
        return _shaped(data, uid, format)
    except PlanTimeout as e:
        return JSONResponse({"error": str(e)}, status_code=504)
    except PlanError as e:
        # Nothing usable in the photo (or the model never produced slots): the user's
        # input, not our outage — say what happened.
        return JSONResponse({"error": str(e)}, status_code=422)
    except McpError as e:
        return _mcp_error(e)
    except (LlmError, httpx.HTTPError) as e:
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=502)


class ComparisonRequest(BaseModel):
    image_url: str
    budget: float = Field(gt=0, le=100_000)
    mode: Literal["raw", "budget", "enhanced", "default"]
    comparison_id: str | None = None
    gender: Literal["F", "M", "UNISEX"] | None = None


@app.post("/api/compare/outfit")
async def compare_outfit(req: ComparisonRequest, uid: str = Depends(require_user)):
    try:
        async with asyncio.timeout(comparison.ENHANCED_TIMEOUT_SECONDS):
            return await comparison.stage(uid, req.image_url, req.budget, req.mode,
                                          req.comparison_id, req.gender)
    except comparison.ComparisonNotFound:
        return JSONResponse({"error": "Comparison expired. Run this image again."}, status_code=404)
    except TimeoutError:
        return JSONResponse({"error": "This stage exceeded 120 seconds; earlier comparison results are kept."}, status_code=504)
    except PlanError as e:
        return JSONResponse({"error": str(e)}, status_code=422)
    except McpError as e:
        return _mcp_error(e)
    except (LlmError, httpx.HTTPError) as e:
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=502)


class ReplanRequest(BaseModel):
    session_id: str
    budget: float = Field(gt=0, le=100_000)
    owned: list[str] | None = None
    # {slot: p_key} that must be in the plan. Omit to keep the session's pins, {} to clear.
    pin: dict[str, str] | None = None


@app.post("/api/replan")
async def replan(req: ReplanRequest, uid: str = Depends(require_user),
                 format: Literal["ui"] | None = None):
    try:
        data = await orchestrator.replan(uid, req.session_id, req.budget, req.owned,
                                         req.pin)
        return _shaped(data, uid, format)
    except SessionNotFound:
        return JSONResponse({"error": "This plan has expired. Generate a new one."},
                            status_code=404)


# ── Image upload ─────────────────────────────────────────────────────────

@app.post("/api/upload")
async def upload(request: Request, uid: str = Depends(require_user)):
    """The raw image is the request body (Content-Type image/jpeg or image/png).

    Read in chunks and cut off past the limit, so an oversized upload is refused
    without holding all of it in memory.
    """
    if not uploads.bucket_name():
        return JSONResponse({"error": "Uploads are not configured (UPLOAD_BUCKET)."},
                            status_code=503)
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > uploads.MAX_BYTES:
        return JSONResponse({"error": "The image is larger than 8 MB."}, status_code=413)
    data = bytearray()
    async for chunk in request.stream():
        data += chunk
        if len(data) > uploads.MAX_BYTES:
            return JSONResponse({"error": "The image is larger than 8 MB."}, status_code=413)
    try:
        return await uploads.save(bytes(data))
    except uploads.UploadError as e:
        return JSONResponse({"error": str(e)}, status_code=415 if "JPEG" in str(e) else 400)
    except Exception as e:  # storage / credentials: say which, do not leak a trace
        return JSONResponse({"error": f"Upload failed: {type(e).__name__}: {e}"[:300]},
                            status_code=502)


# ── Lookboard image proxy ────────────────────────────────────────────────

@app.get("/api/comparison-image")
async def comparison_image(comparison_id: str, variant: Literal["raw", "budget", "enhanced", "default"],
                           slot: str, uid: str = Depends(require_user)):
    try:
        url = comparison.image_url_for(uid, comparison_id, variant, slot)
    except comparison.ComparisonNotFound:
        return JSONResponse({"error": "Comparison expired. Run this image again."}, status_code=404)
    except KeyError:
        return JSONResponse({"error": "No product image for this comparison slot."}, status_code=404)
    try:
        data, kind = await image_proxy.fetch(url)
    except (image_proxy.ImageFetchError, httpx.HTTPError) as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    return Response(data, media_type=kind, headers={"Cache-Control": "private, max-age=600"})

@app.get("/api/lookboard-image")
async def lookboard_image(session_id: str, slot: str, uid: str = Depends(require_user)):
    """Image bytes for the Lookboard PNG export. `slot` is a planned slot, or "_photo"
    for the original photo. The URL comes from the user's own session, never the query."""
    try:
        url = await uploads.fresh(orchestrator.image_url_for(uid, session_id, slot))
    except SessionNotFound:
        return JSONResponse({"error": "This plan has expired. Generate a new one."},
                            status_code=404)
    except KeyError:
        return JSONResponse({"error": f"No image for slot {slot!r} in the current plan."},
                            status_code=404)
    try:
        data, kind = await image_proxy.fetch(url)
    except image_proxy.ImageFetchError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    except httpx.HTTPError as e:
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=502)
    return Response(data, media_type=kind, headers={"Cache-Control": "private, max-age=600"})
