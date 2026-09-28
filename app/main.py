"""Scannage: FastAPI app.

Browser -> this app -> the configured store (a SQLite file, or PostgREST in
front of Postgres). There is no login here. The app expects a reverse proxy
to handle sign-in and to pass the user along in the Remote-User header.
"""

from __future__ import annotations

import hashlib
import logging
from contextlib import asynccontextmanager
from pathlib import Path as FilePath
from typing import Annotated

from fastapi import FastAPI, HTTPException, Path, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, StringConstraints
from starlette.concurrency import run_in_threadpool

from . import config, qr, samples
from .stores import StoreError, get_store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("scannage")

STATIC_DIR = FilePath(__file__).resolve().parent / "static"
# StaticFiles refuses to mount a missing directory; the frontend fills it in.
STATIC_DIR.mkdir(exist_ok=True)

ASSET_PLACEHOLDER = "__ASSET_VERSION__"
MAX_ACTOR_LENGTH = 120

_asset_version = ""


def _hash_static() -> str:
    """Short hash over every static file, so a changed asset gets a new URL."""
    digest = hashlib.sha256()
    for path in sorted(p for p in STATIC_DIR.rglob("*") if p.is_file()):
        digest.update(path.relative_to(STATIC_DIR).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()[:12]


def asset_version() -> str:
    global _asset_version
    if not _asset_version:
        _asset_version = _hash_static()
    return _asset_version


@asynccontextmanager
async def _lifespan(app: FastAPI):
    global _asset_version
    store = get_store()
    log.info("store: %s", store.name)
    await run_in_threadpool(store.bootstrap)
    await run_in_threadpool(samples.seed_best_effort, store)
    _asset_version = await run_in_threadpool(_hash_static)
    log.info("asset version: %s", _asset_version)
    yield


app = FastAPI(title="Scannage", lifespan=_lifespan)


# --- request plumbing ---------------------------------------------------------


def _require_same_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if not origin:
        return
    host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
    if origin.split("://", 1)[-1].split("/", 1)[0] != host:
        raise HTTPException(status_code=403, detail="cross-origin request rejected")


def _actor(request: Request) -> str:
    return request.headers.get("remote-user", "").strip()[:MAX_ACTOR_LENGTH]


def _first(value: str | None) -> str:
    # a chain of proxies sends a comma separated list, nearest the client first
    return (value or "").split(",", 1)[0].strip()


def _base_url(request: Request) -> str:
    if config.BASE_URL:
        return config.BASE_URL
    proto = _first(request.headers.get("x-forwarded-proto")) or request.url.scheme
    host = _first(request.headers.get("x-forwarded-host")) or request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}"


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    return response


@app.exception_handler(StoreError)
async def store_error(request: Request, exc: StoreError):
    log.error("%s store error: %s", config.STORE, exc.detail)
    return JSONResponse({"detail": "store unavailable"}, status_code=502)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    # one plain string, the same shape as every other error
    parts = []
    for error in exc.errors():
        where = ".".join(str(loc) for loc in error.get("loc", ())[1:])
        parts.append(f"{where}: {error.get('msg', 'invalid')}" if where else error.get("msg", "invalid"))
    return JSONResponse({"detail": "; ".join(parts) or "invalid request"}, status_code=422)


# --- request bodies -----------------------------------------------------------

TagId = Annotated[int, Path(ge=0, lt=config.TAG_COUNT)]

ShortText = Annotated[str, StringConstraints(strip_whitespace=True, max_length=120)]
LongText = Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)]
ItemName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
Qty = Annotated[int, Field(ge=1, le=9999)]


class BoxBody(BaseModel):
    name: ShortText | None = None
    location: ShortText | None = None
    notes: LongText | None = None


class ItemBody(BaseModel):
    name: ItemName
    qty: Qty = 1


class ItemPatchBody(BaseModel):
    name: ItemName | None = None
    qty: Qty | None = None


# --- health and config --------------------------------------------------------


@app.get("/api/health")
async def health():
    store = get_store()
    ok, detail = await run_in_threadpool(store.health)
    body = {"status": "ok" if ok else "degraded", "store": store.name, "detail": detail}
    return JSONResponse(body, status_code=200 if ok else 503)


@app.get("/api/config")
async def get_config(request: Request):
    return {
        "dictionary": config.DICTIONARY,
        "tag_count": config.TAG_COUNT,
        "base_url": _base_url(request),
        "user": _actor(request),
        "store": get_store().name,
    }


# --- boxes --------------------------------------------------------------------


@app.get("/api/boxes")
async def list_boxes():
    return await run_in_threadpool(get_store().list_boxes)


@app.get("/api/boxes/{tag_id}")
async def get_box(tag_id: TagId):
    box = await run_in_threadpool(get_store().get_box, tag_id)
    if box is None:
        raise HTTPException(status_code=404, detail="no box on this tag")
    return box


@app.put("/api/boxes/{tag_id}")
async def put_box(tag_id: TagId, request: Request, body: BoxBody | None = None):
    _require_same_origin(request)
    fields = body.model_dump(exclude_none=True) if body else {}
    return await run_in_threadpool(get_store().upsert_box, tag_id, fields, _actor(request))


@app.delete("/api/boxes/{tag_id}", status_code=204)
async def delete_box(tag_id: TagId, request: Request):
    _require_same_origin(request)
    if not await run_in_threadpool(get_store().delete_box, tag_id):
        raise HTTPException(status_code=404, detail="no box on this tag")
    return Response(status_code=204)


# --- items --------------------------------------------------------------------


@app.post("/api/boxes/{tag_id}/items", status_code=201)
async def add_item(tag_id: TagId, body: ItemBody, request: Request):
    _require_same_origin(request)
    return await run_in_threadpool(get_store().add_item, tag_id, body.name, body.qty, _actor(request))


@app.patch("/api/items/{item_id}")
async def patch_item(item_id: str, body: ItemPatchBody, request: Request):
    _require_same_origin(request)
    fields = body.model_dump(exclude_none=True)
    item = await run_in_threadpool(get_store().update_item, item_id, fields, _actor(request))
    if item is None:
        raise HTTPException(status_code=404, detail="no such item")
    return item


@app.delete("/api/items/{item_id}", status_code=204)
async def delete_item(item_id: str, request: Request):
    _require_same_origin(request)
    if not await run_in_threadpool(get_store().delete_item, item_id):
        raise HTTPException(status_code=404, detail="no such item")
    return Response(status_code=204)


# --- labels -------------------------------------------------------------------


@app.get("/api/qr/{tag_id}.svg")
async def qr_svg(tag_id: TagId, request: Request):
    svg = await run_in_threadpool(qr.svg, f"{_base_url(request)}/b/{tag_id}")
    return Response(svg, media_type="image/svg+xml", headers={"Cache-Control": "no-store"})


# --- static frontend ----------------------------------------------------------


def _page(name: str) -> Response:
    path = STATIC_DIR / name
    if not path.is_file():
        return PlainTextResponse(f"{name} is not available yet", status_code=503)
    html = path.read_text(encoding="utf-8").replace(ASSET_PLACEHOLDER, asset_version())
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@app.get("/")
def index():
    return _page("index.html")


@app.get("/b/{tag_id}")
def box_page(tag_id: int):
    return _page("index.html")


@app.get("/labels")
def labels():
    return _page("labels.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
