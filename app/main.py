"""Scannage: FastAPI app.

Browser -> this app -> the configured store (a SQLite file, or PostgREST in
front of Postgres). There is no login here. The app expects a reverse proxy
to handle sign-in and to pass the user along in the Remote-User header.
"""

from __future__ import annotations

import hashlib
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path as FilePath
from typing import Annotated

from fastapi import FastAPI, HTTPException, Path, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, StringConstraints
from starlette.concurrency import run_in_threadpool

from . import config, history, photos, qr
from .stores import StoreError, get_store
from .stores.base import normalize_timestamp

logging.basicConfig(level=logging.INFO, format=config.LOG_FORMAT)
log = logging.getLogger("scannage")

STATIC_DIR = FilePath(__file__).resolve().parent / "static"
# StaticFiles refuses to mount a missing directory; the frontend fills it in.
STATIC_DIR.mkdir(exist_ok=True)

ASSET_PLACEHOLDER = "__ASSET_VERSION__"
MAX_ACTOR_LENGTH = 120
# A photo never changes once stored, so its address can be cached for good.
PHOTO_CACHE = "private, max-age=31536000, immutable"

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
    # the schema, and on a new database the sample boxes
    await run_in_threadpool(store.bootstrap)
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


class PrintedBody(BaseModel):
    tag_ids: Annotated[
        list[Annotated[int, Field(ge=0, lt=config.TAG_COUNT)]], Field(min_length=1, max_length=config.TAG_COUNT)
    ]


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
    return await run_in_threadpool(history.put_box, get_store(), tag_id, fields, _actor(request))


@app.delete("/api/boxes/{tag_id}", status_code=204)
async def delete_box(tag_id: TagId, request: Request):
    _require_same_origin(request)
    if not await run_in_threadpool(history.delete_box, get_store(), tag_id, _actor(request)):
        raise HTTPException(status_code=404, detail="no box on this tag")
    return Response(status_code=204)


# --- items --------------------------------------------------------------------


@app.post("/api/boxes/{tag_id}/items", status_code=201)
async def add_item(tag_id: TagId, body: ItemBody, request: Request):
    _require_same_origin(request)
    return await run_in_threadpool(history.add_item, get_store(), tag_id, body.name, body.qty, _actor(request))


@app.patch("/api/items/{item_id}")
async def patch_item(item_id: str, body: ItemPatchBody, request: Request):
    _require_same_origin(request)
    fields = body.model_dump(exclude_none=True)
    item = await run_in_threadpool(history.update_item, get_store(), item_id, fields, _actor(request))
    if item is None:
        raise HTTPException(status_code=404, detail="no such item")
    return item


@app.delete("/api/items/{item_id}", status_code=204)
async def delete_item(item_id: str, request: Request):
    _require_same_origin(request)
    if not await run_in_threadpool(history.delete_item, get_store(), item_id, _actor(request)):
        raise HTTPException(status_code=404, detail="no such item")
    return Response(status_code=204)


# --- photos -------------------------------------------------------------------


async def _read_upload(request: Request) -> bytes:
    """The request body, given up on as soon as it passes the limit."""
    too_large = HTTPException(status_code=413, detail=f"a photo can be at most {photos.MAX_UPLOAD_BYTES} bytes")
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > photos.MAX_UPLOAD_BYTES:
        raise too_large
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > photos.MAX_UPLOAD_BYTES:
            raise too_large
    return bytes(body)


def _process_upload(raw: bytes) -> dict:
    try:
        return photos.process(raw)
    except photos.PhotoError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _require_room(owner: dict, what: str) -> None:
    if len(owner["photos"]) >= photos.MAX_PER_OWNER:
        raise HTTPException(status_code=409, detail=f"{what} already holds {photos.MAX_PER_OWNER} photos")


def _add_box_photo(tag_id: int, raw: bytes, actor: str) -> dict:
    store = get_store()
    image = _process_upload(raw)
    box = store.get_box(tag_id)
    if box is None:
        box = history.put_box(store, tag_id, {}, actor)
    _require_room(box, "this box")
    return history.add_photo(store, box["id"], None, image, actor)


def _add_item_photo(item_id: str, raw: bytes, actor: str) -> dict:
    store = get_store()
    image = _process_upload(raw)
    item = store.get_item(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="no such item")
    _require_room(item, "this item")
    return history.add_photo(store, item["box_id"], item["id"], image, actor)


@app.post("/api/boxes/{tag_id}/photos", status_code=201)
async def add_box_photo(tag_id: TagId, request: Request):
    _require_same_origin(request)
    raw = await _read_upload(request)
    return await run_in_threadpool(_add_box_photo, tag_id, raw, _actor(request))


@app.post("/api/items/{item_id}/photos", status_code=201)
async def add_item_photo(item_id: str, request: Request):
    _require_same_origin(request)
    if await run_in_threadpool(get_store().get_item, item_id) is None:
        raise HTTPException(status_code=404, detail="no such item")
    raw = await _read_upload(request)
    return await run_in_threadpool(_add_item_photo, item_id, raw, _actor(request))


async def _photo_response(photo_id: str, thumb: bool) -> Response:
    data = await run_in_threadpool(get_store().get_photo_data, photo_id, thumb)
    if data is None:
        raise HTTPException(status_code=404, detail="no such photo")
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": PHOTO_CACHE})


@app.get("/api/photos/{photo_id}")
async def get_photo(photo_id: str):
    return await _photo_response(photo_id, thumb=False)


@app.get("/api/photos/{photo_id}/thumb")
async def get_photo_thumb(photo_id: str):
    return await _photo_response(photo_id, thumb=True)


def _remove_photo(photo_id: str, actor: str) -> None:
    if not history.delete_photo(get_store(), photo_id, actor):
        raise HTTPException(status_code=404, detail="no such photo on a box or item")


def _erase_photo(photo_id: str, actor: str) -> None:
    store = get_store()
    if store.get_photo(photo_id) is not None:
        raise HTTPException(status_code=409, detail="this photo is still on a box or item, remove it first")
    if not history.erase_photo(store, photo_id, actor):
        raise HTTPException(status_code=404, detail="no such photo")


@app.delete("/api/photos/{photo_id}", status_code=204)
async def delete_photo(photo_id: str, request: Request, erase: bool = False):
    _require_same_origin(request)
    # without erase the photo is kept, and erase is the only way it leaves the database
    await run_in_threadpool(_erase_photo if erase else _remove_photo, photo_id, _actor(request))
    return Response(status_code=204)


# --- history ------------------------------------------------------------------


def _before(value: str | None) -> str | None:
    """The page marker in the app's own timestamp format, which is what the stores compare against."""
    if value is None or not value.strip():
        return None
    value = value.strip()
    # a "+" sent without encoding arrives as a space
    for candidate in (value, value.replace(" ", "+")):
        try:
            stamp = datetime.fromisoformat(candidate)
        except ValueError:
            continue
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return normalize_timestamp(stamp.isoformat())
    raise HTTPException(status_code=422, detail="before: not a timestamp")


@app.get("/api/history")
async def list_history(
    tag_id: Annotated[int | None, Query(ge=0, lt=config.TAG_COUNT)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    before: str | None = None,
):
    return await run_in_threadpool(history.list_entries, get_store(), tag_id, limit, _before(before))


# --- labels -------------------------------------------------------------------


@app.get("/api/qr/{tag_id}.svg")
async def qr_svg(tag_id: TagId, request: Request):
    svg = await run_in_threadpool(qr.svg, f"{_base_url(request)}/b/{tag_id}")
    return Response(svg, media_type="image/svg+xml", headers={"Cache-Control": "no-store"})


def _labels() -> dict:
    store = get_store()
    # negative tag ids belong to tests against a live store
    printed = [row for row in store.list_printed() if row["tag_id"] >= 0]
    in_use = [tag_id for tag_id in store.list_tag_ids() if tag_id >= 0]
    taken = {row["tag_id"] for row in printed} | set(in_use)
    # labels are handed out from 1 upward, and tag 0 goes last
    order = [*range(1, config.TAG_COUNT), 0]
    return {
        "tag_count": config.TAG_COUNT,
        "printed": printed,
        "in_use": in_use,
        "next": [tag_id for tag_id in order if tag_id not in taken],
    }


def _record_printed(tag_ids: list[int], actor: str) -> dict:
    get_store().record_printed(tag_ids, actor)
    return _labels()


@app.get("/api/labels")
async def get_labels():
    return await run_in_threadpool(_labels)


@app.post("/api/labels/printed")
async def record_printed(body: PrintedBody, request: Request):
    _require_same_origin(request)
    # printing is not part of a box's history: a label can be printed long before a box exists
    return await run_in_threadpool(_record_printed, body.tag_ids, _actor(request))


@app.delete("/api/labels/printed/{tag_id}", status_code=204)
async def forget_printed(tag_id: TagId, request: Request):
    _require_same_origin(request)
    if not await run_in_threadpool(get_store().forget_printed, tag_id):
        raise HTTPException(status_code=404, detail="no label was printed for this tag")
    return Response(status_code=204)


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
