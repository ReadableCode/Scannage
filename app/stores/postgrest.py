"""PostgREST store: every read and write goes through PostgREST over HTTP.

The schema is pinned per request with Accept-Profile / Content-Profile. The
app mints its own short-lived JWT for the schema's role, so no token is ever
stored or handed to a browser.
"""

from __future__ import annotations

import threading
import time

import httpx
import jwt

from .. import bootstrap, config
from .base import (
    BOX_FIELDS,
    ITEM_FIELDS,
    StoreError,
    clean_uuid,
    new_id,
    normalize_timestamp,
    pick,
    utc_now,
)

BOX_KEYS = ("id", "tag_id", "name", "location", "notes", "created_at", "updated_at", "updated_by")
ITEM_KEYS = ("id", "box_id", "name", "qty", "created_at", "updated_at")
STAMP_KEYS = ("created_at", "updated_at")

# One request returns boxes with their items, both already in contract order.
BOX_SELECT = {"select": "*,items(*)", "order": "tag_id.asc", "items.order": "created_at.asc,id.asc"}

# Re-mint this long before expiry so a token never lapses mid-request.
JWT_REFRESH_MARGIN_SECONDS = 60


def _shape(row: dict, keys: tuple[str, ...]) -> dict:
    shaped = {key: row[key] for key in keys}
    for key in STAMP_KEYS:
        shaped[key] = normalize_timestamp(shaped[key])
    return shaped


def _shape_box(row: dict) -> dict:
    return {**_shape(row, BOX_KEYS), "items": [_shape(item, ITEM_KEYS) for item in row.get("items") or []]}


class PostgrestStore:
    name = "postgrest"

    def __init__(self, url: str | None = None, secret: str | None = None, schema: str | None = None):
        self.url = (config.POSTGREST_URL if url is None else url).rstrip("/")
        self.secret = config.JWT_SECRET if secret is None else secret
        self.schema = config.APP_SCHEMA if schema is None else schema
        missing = [
            name for name, value in (("POSTGREST_URL", self.url), ("POSTGREST_JWT_SECRET", self.secret)) if not value
        ]
        if missing:
            raise RuntimeError(f"SCANNAGE_STORE=postgrest needs {' and '.join(missing)} to be set")
        self._client = httpx.Client(timeout=config.HTTP_TIMEOUT)
        self._token = ""
        self._token_expires = 0.0
        self._token_lock = threading.Lock()

    # --- plumbing -------------------------------------------------------------

    def _bearer(self) -> str:
        with self._token_lock:
            now = time.time()
            if now >= self._token_expires - JWT_REFRESH_MARGIN_SECONDS:
                expires = now + config.JWT_TTL_SECONDS
                claims = {"role": f"{self.schema}_user", "exp": int(expires)}
                self._token = jwt.encode(claims, self.secret, algorithm="HS256")
                self._token_expires = expires
            return self._token

    def _headers(self, write: bool = False, prefer: str = "") -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._bearer()}",
            "Accept-Profile": self.schema,
        }
        if write:
            headers["Content-Profile"] = self.schema
            headers["Content-Type"] = "application/json"
        if prefer:
            headers["Prefer"] = prefer
        return headers

    def _call(
        self,
        method: str,
        table: str,
        params: dict | None = None,
        body: dict | None = None,
        prefer: str = "",
    ) -> list[dict]:
        write = method != "GET"
        try:
            resp = self._client.request(
                method,
                f"{self.url}/{table}",
                params=params,
                json=body,
                headers=self._headers(write=write, prefer=prefer),
            )
        except httpx.HTTPError as exc:
            raise StoreError(502, f"postgrest unreachable: {exc}") from exc
        if resp.status_code >= 400:
            raise StoreError(resp.status_code, f"postgrest {resp.status_code}: {resp.text[:300]}")
        if not resp.content:
            return []
        try:
            return resp.json()
        except ValueError as exc:
            raise StoreError(502, f"postgrest sent a body that is not JSON: {resp.text[:300]}") from exc

    # --- lifecycle ------------------------------------------------------------

    def bootstrap(self) -> None:
        bootstrap.bootstrap_best_effort()

    def health(self) -> tuple[bool, str]:
        try:
            self._call("GET", "boxes", {"select": "id", "limit": "1"})
        except StoreError as exc:
            return False, exc.detail
        return True, "ok"

    # --- reads ----------------------------------------------------------------

    def list_boxes(self) -> list[dict]:
        return [_shape_box(row) for row in self._call("GET", "boxes", BOX_SELECT)]

    def get_box(self, tag_id: int) -> dict | None:
        rows = self._call("GET", "boxes", {**BOX_SELECT, "tag_id": f"eq.{tag_id}"})
        return _shape_box(rows[0]) if rows else None

    # --- boxes ----------------------------------------------------------------

    def _ensure_box(self, tag_id: int, actor: str, now: str) -> str:
        """Id of the box on this tag, claiming the tag with an empty box when it is free."""
        rows = self._call("GET", "boxes", {"select": "id", "tag_id": f"eq.{tag_id}"})
        if rows:
            return rows[0]["id"]
        row = {"id": new_id(), "tag_id": tag_id, "created_at": now, "updated_at": now, "updated_by": actor}
        # ignore-duplicates: when another request claimed the tag first, theirs stands
        created = self._call(
            "POST",
            "boxes",
            {"on_conflict": "tag_id", "select": "id"},
            row,
            prefer="resolution=ignore-duplicates,return=representation",
        )
        if created:
            return created[0]["id"]
        rows = self._call("GET", "boxes", {"select": "id", "tag_id": f"eq.{tag_id}"})
        if not rows:
            raise StoreError(502, f"postgrest: box for tag {tag_id} could not be created")
        return rows[0]["id"]

    def _touch_box(self, box_id: str, actor: str, now: str, values: dict | None = None) -> None:
        self._call(
            "PATCH",
            "boxes",
            {"id": f"eq.{box_id}"},
            {**(values or {}), "updated_at": now, "updated_by": actor},
            prefer="return=minimal",
        )

    def upsert_box(self, tag_id: int, fields: dict, actor: str) -> dict:
        now = utc_now()
        box_id = self._ensure_box(tag_id, actor, now)
        self._touch_box(box_id, actor, now, pick(fields, BOX_FIELDS))
        box = self.get_box(tag_id)
        if box is None:
            raise StoreError(502, f"postgrest: box for tag {tag_id} vanished while it was being saved")
        return box

    def delete_box(self, tag_id: int) -> bool:
        # items go with it through ON DELETE CASCADE
        rows = self._call(
            "DELETE", "boxes", {"tag_id": f"eq.{tag_id}", "select": "id"}, prefer="return=representation"
        )
        return bool(rows)

    # --- items ----------------------------------------------------------------

    def add_item(self, tag_id: int, name: str, qty: int, actor: str) -> dict:
        now = utc_now()
        box_id = self._ensure_box(tag_id, actor, now)
        row = {"id": new_id(), "box_id": box_id, "name": name, "qty": qty, "created_at": now, "updated_at": now}
        rows = self._call("POST", "items", None, row, prefer="return=representation")
        if not rows:
            raise StoreError(502, "postgrest: the new item was not returned")
        self._touch_box(box_id, actor, now)
        return _shape(rows[0], ITEM_KEYS)

    def update_item(self, item_id: str, fields: dict, actor: str) -> dict | None:
        clean_id = clean_uuid(item_id)
        if clean_id is None:
            return None
        values = pick(fields, ITEM_FIELDS)
        if not values:
            rows = self._call("GET", "items", {"id": f"eq.{clean_id}"})
            return _shape(rows[0], ITEM_KEYS) if rows else None
        now = utc_now()
        rows = self._call(
            "PATCH", "items", {"id": f"eq.{clean_id}"}, {**values, "updated_at": now}, prefer="return=representation"
        )
        if not rows:
            return None
        self._touch_box(rows[0]["box_id"], actor, now)
        return _shape(rows[0], ITEM_KEYS)

    def delete_item(self, item_id: str) -> bool:
        clean_id = clean_uuid(item_id)
        if clean_id is None:
            return False
        rows = self._call(
            "DELETE", "items", {"id": f"eq.{clean_id}", "select": "id"}, prefer="return=representation"
        )
        return bool(rows)

    # --- meta -----------------------------------------------------------------

    def get_meta(self, key: str) -> str | None:
        rows = self._call("GET", "app_meta", {"select": "value", "key": f"eq.{key}"})
        return rows[0]["value"] if rows else None

    def set_meta(self, key: str, value: str) -> None:
        self._call(
            "POST",
            "app_meta",
            {"on_conflict": "key"},
            {"key": key, "value": value},
            prefer="resolution=merge-duplicates,return=minimal",
        )
