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

from .. import bootstrap, config, samples
from .base import (
    BOX_FIELDS,
    HISTORY_KEYS,
    ITEM_FIELDS,
    KEPT_PHOTO_KEYS,
    PHOTO_KEYS,
    PRINTED_KEYS,
    StoreError,
    attach_photos,
    chunks,
    clean_uuid,
    clean_uuids,
    new_id,
    normalize_timestamp,
    once_each,
    pick,
    utc_now,
)

BOX_KEYS = ("id", "tag_id", "name", "location", "notes", "created_at", "updated_at", "updated_by")
ITEM_KEYS = ("id", "box_id", "name", "qty", "created_at", "updated_at")
STAMP_KEYS = ("created_at", "updated_at")

# One request returns boxes with their items, both already in contract order.
# The foreign key is named because photos points at both tables, which can
# read as a second path between them.
BOX_SELECT = {
    "select": "*,items!items_box_id_fkey(*)",
    "order": "tag_id.asc",
    "items.order": "created_at.asc,id.asc",
}
# Never data or thumb: photos are listed without their bytes and merged in here.
PHOTO_SELECT = {"select": ",".join(PHOTO_KEYS), "order": "created_at.asc,id.asc"}
KEPT_PHOTO_SELECT = {"select": ",".join(KEPT_PHOTO_KEYS)}
KEPT_PHOTOS = "kept_photos"
# Ids per request when filtering by a set of them, which keeps the address under about 4000 characters.
ID_CHUNK = 80
HISTORY_SELECT = {"select": ",".join(HISTORY_KEYS), "order": "at.desc,id.desc"}
PRINTED_TAGS = "printed_tags"
PRINTED_SELECT = {"select": ",".join(PRINTED_KEYS), "order": "tag_id.asc"}
PRINTED_STAMPS = ("first_printed_at", "last_printed_at")

# Re-mint this long before expiry so a token never lapses mid-request.
JWT_REFRESH_MARGIN_SECONDS = 60

# bytea travels through JSON as text in the Postgres hex format
BYTEA_PREFIX = "\\x"


def _shape(row: dict, keys: tuple[str, ...], stamps: tuple[str, ...] = STAMP_KEYS) -> dict:
    shaped = {key: row[key] for key in keys}
    for key in stamps:
        shaped[key] = normalize_timestamp(shaped[key])
    return shaped


def _shape_box(row: dict) -> dict:
    return {**_shape(row, BOX_KEYS), "items": [_shape(item, ITEM_KEYS) for item in row.get("items") or []]}


def _shape_photo(row: dict) -> dict:
    return _shape(row, PHOTO_KEYS, ("created_at",))


def _shape_kept_photo(row: dict) -> dict:
    return _shape(row, KEPT_PHOTO_KEYS, ("created_at", "removed_at"))


def _shape_entry(row: dict) -> dict:
    return _shape(row, HISTORY_KEYS, ("at",))


def _shape_printed(row: dict) -> dict:
    return _shape(row, PRINTED_KEYS, PRINTED_STAMPS)


def printed_rows(tag_ids: list[int], known: dict[int, dict], now: str, actor: str) -> list[dict]:
    """What one print writes: a row per tag, every row with the same keys, as a bulk insert needs.

    A tag printed before keeps its first time and counts one more.
    """
    rows = []
    for tag_id in tag_ids:
        before = known.get(tag_id)
        rows.append(
            {
                "tag_id": tag_id,
                "first_printed_at": before["first_printed_at"] if before else now,
                "last_printed_at": now,
                "times": before["times"] + 1 if before else 1,
                "printed_by": actor,
            }
        )
    return rows


def to_bytea(value: bytes) -> str:
    return BYTEA_PREFIX + value.hex()


def id_filters(ids: list[str]) -> list[str]:
    """The filter values that ask for a set of ids, split so that no request grows too long."""
    return [f"in.({','.join(chunk)})" for chunk in chunks(ids, ID_CHUNK)]


def from_bytea(value: str) -> bytes:
    if not value.startswith(BYTEA_PREFIX):
        raise StoreError(502, "postgrest sent a photo that is not in the hex format")
    try:
        return bytes.fromhex(value.removeprefix(BYTEA_PREFIX))
    except ValueError as exc:
        raise StoreError(502, "postgrest sent a photo that is not in the hex format") from exc


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
        self._keeps_photos = False

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
        body: dict | list[dict] | None = None,
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

    def _require_keeping(self) -> None:
        """Nothing is deleted until the table that keeps photos is served.

        The schema bootstrap is best effort, so this app can find itself in
        front of an older schema, where a delete would take the photos with it.
        """
        if self._keeps_photos:
            return
        self._call("GET", KEPT_PHOTOS, {"select": "id", "limit": "1"})
        self._keeps_photos = True

    # --- lifecycle ------------------------------------------------------------

    def bootstrap(self) -> None:
        bootstrap.bootstrap_best_effort()
        samples.seed_best_effort(self)

    def apply_schema(self, force: bool = False) -> bool:
        return bootstrap.apply_schema(force=force)

    def health(self) -> tuple[bool, str]:
        try:
            self._call("GET", "boxes", {"select": "id", "limit": "1"})
        except StoreError as exc:
            return False, exc.detail
        return True, "ok"

    # --- reads ----------------------------------------------------------------

    def _photos(self, column: str = "", value: str = "") -> list[dict]:
        where = {column: f"eq.{value}"} if column else {}
        return [_shape_photo(row) for row in self._call("GET", "photos", {**PHOTO_SELECT, **where})]

    def _box(self, column: str, value: int | str) -> dict | None:
        rows = self._call("GET", "boxes", {**BOX_SELECT, column: f"eq.{value}"})
        if not rows:
            return None
        box = _shape_box(rows[0])
        return attach_photos([box], self._photos("box_id", box["id"]))[0]

    def list_boxes(self) -> list[dict]:
        boxes = [_shape_box(row) for row in self._call("GET", "boxes", BOX_SELECT)]
        # one more request for every photo's metadata, however many boxes there are
        return attach_photos(boxes, self._photos()) if boxes else boxes

    def list_tag_ids(self) -> list[int]:
        # which tags have a box, without reading what is in them
        rows = self._call("GET", "boxes", {"select": "tag_id", "order": "tag_id.asc"})
        return [row["tag_id"] for row in rows]

    def get_box(self, tag_id: int) -> dict | None:
        return self._box("tag_id", tag_id)

    def get_box_by_id(self, box_id: str) -> dict | None:
        clean_id = clean_uuid(box_id)
        if clean_id is None:
            return None
        return self._box("id", clean_id)

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
        # items and photos go with it through ON DELETE CASCADE, and the trigger keeps each photo
        self._require_keeping()
        rows = self._call(
            "DELETE", "boxes", {"tag_id": f"eq.{tag_id}", "select": "id"}, prefer="return=representation"
        )
        return bool(rows)

    # --- items ----------------------------------------------------------------

    def _shape_item(self, row: dict) -> dict:
        return {**_shape(row, ITEM_KEYS), "photos": self._photos("item_id", row["id"])}

    def get_item(self, item_id: str) -> dict | None:
        clean_id = clean_uuid(item_id)
        if clean_id is None:
            return None
        rows = self._call("GET", "items", {"id": f"eq.{clean_id}"})
        return self._shape_item(rows[0]) if rows else None

    def add_item(self, tag_id: int, name: str, qty: int, actor: str) -> dict:
        now = utc_now()
        box_id = self._ensure_box(tag_id, actor, now)
        row = {"id": new_id(), "box_id": box_id, "name": name, "qty": qty, "created_at": now, "updated_at": now}
        rows = self._call("POST", "items", None, row, prefer="return=representation")
        if not rows:
            raise StoreError(502, "postgrest: the new item was not returned")
        self._touch_box(box_id, actor, now)
        return {**_shape(rows[0], ITEM_KEYS), "photos": []}

    def update_item(self, item_id: str, fields: dict, actor: str) -> dict | None:
        clean_id = clean_uuid(item_id)
        if clean_id is None:
            return None
        values = pick(fields, ITEM_FIELDS)
        if not values:
            return self.get_item(clean_id)
        now = utc_now()
        rows = self._call(
            "PATCH", "items", {"id": f"eq.{clean_id}"}, {**values, "updated_at": now}, prefer="return=representation"
        )
        if not rows:
            return None
        self._touch_box(rows[0]["box_id"], actor, now)
        return self._shape_item(rows[0])

    def delete_item(self, item_id: str) -> bool:
        clean_id = clean_uuid(item_id)
        if clean_id is None:
            return False
        # its photos go with it through ON DELETE CASCADE, and the trigger keeps each one
        self._require_keeping()
        rows = self._call(
            "DELETE", "items", {"id": f"eq.{clean_id}", "select": "id"}, prefer="return=representation"
        )
        return bool(rows)

    # --- photos ---------------------------------------------------------------

    def add_photo(self, box_id: str, item_id: str | None, image: dict, actor: str) -> dict:
        now = utc_now()
        row = {
            "id": new_id(),
            "box_id": box_id,
            "item_id": item_id,
            "width": image["width"],
            "height": image["height"],
            "size": len(image["data"]),
            "created_at": now,
            "created_by": actor,
            "data": to_bytea(image["data"]),
            "thumb": to_bytea(image["thumb"]),
        }
        # select keeps the bytes out of the answer
        rows = self._call(
            "POST", "photos", {"select": PHOTO_SELECT["select"]}, row, prefer="return=representation"
        )
        if not rows:
            raise StoreError(502, "postgrest: the new photo was not returned")
        self._touch_box(box_id, actor, now)
        return _shape_photo(rows[0])

    def get_photo(self, photo_id: str) -> dict | None:
        clean_id = clean_uuid(photo_id)
        if clean_id is None:
            return None
        photos = self._photos("id", clean_id)
        return photos[0] if photos else None

    def get_photo_data(self, photo_id: str, thumb: bool = False) -> bytes | None:
        clean_id = clean_uuid(photo_id)
        if clean_id is None:
            return None
        column = "thumb" if thumb else "data"
        # a kept photo is served from the same address as before
        for table in ("photos", KEPT_PHOTOS):
            rows = self._call("GET", table, {"select": column, "id": f"eq.{clean_id}"})
            if rows:
                return from_bytea(rows[0][column])
        return None

    def delete_photo(self, photo_id: str) -> bool:
        clean_id = clean_uuid(photo_id)
        if clean_id is None:
            return False
        # the trigger keeps it
        self._require_keeping()
        rows = self._call(
            "DELETE", "photos", {"id": f"eq.{clean_id}", "select": "id"}, prefer="return=representation"
        )
        return bool(rows)

    def get_kept_photo(self, photo_id: str) -> dict | None:
        clean_id = clean_uuid(photo_id)
        if clean_id is None:
            return None
        rows = self._call("GET", KEPT_PHOTOS, {**KEPT_PHOTO_SELECT, "id": f"eq.{clean_id}"})
        return _shape_kept_photo(rows[0]) if rows else None

    def list_photos_by_ids(self, photo_ids: list[str]) -> list[dict]:
        wanted = clean_uuids(photo_ids)
        found: list[dict] = []
        for table in ("photos", KEPT_PHOTOS):
            # one request per table, more only when the ids do not fit in one address
            for id_filter in id_filters(wanted):
                rows = self._call("GET", table, {**PHOTO_SELECT, "id": id_filter})
                found.extend({**_shape_photo(row), "kept": table == KEPT_PHOTOS} for row in rows)
            # what is still on a box or item is not asked for again
            here = {photo["id"] for photo in found}
            wanted = [photo_id for photo_id in wanted if photo_id not in here]
        return found

    def erase_photo(self, photo_id: str) -> bool:
        clean_id = clean_uuid(photo_id)
        if clean_id is None:
            return False
        # the one place image bytes are deleted, and only ever those of a kept photo
        rows = self._call(
            "DELETE", KEPT_PHOTOS, {"id": f"eq.{clean_id}", "select": "id"}, prefer="return=representation"
        )
        return bool(rows)

    # --- history --------------------------------------------------------------

    def add_history(self, entry: dict) -> dict:
        values = {key: entry[key] for key in HISTORY_KEYS}
        self._call("POST", "history", None, values, prefer="return=minimal")
        return values

    def update_history(self, entry_id: str, changes: dict, at: str, box_name: str, item_name: str) -> None:
        values = {"changes": changes, "at": at, "box_name": box_name, "item_name": item_name}
        self._call("PATCH", "history", {"id": f"eq.{entry_id}"}, values, prefer="return=minimal")

    def delete_history(self, entry_id: str) -> bool:
        rows = self._call(
            "DELETE", "history", {"id": f"eq.{entry_id}", "select": "id"}, prefer="return=representation"
        )
        return bool(rows)

    def list_history(
        self, tag_id: int | None, limit: int, before: str | None, include_tests: bool = False
    ) -> list[dict]:
        params = {**HISTORY_SELECT, "limit": str(limit)}
        if tag_id is not None:
            if tag_id < 0 and not include_tests:
                return []
            params["tag_id"] = f"eq.{tag_id}"
        elif not include_tests:
            params["tag_id"] = "gte.0"
        if before is not None:
            params["at"] = f"lt.{before}"
        return [_shape_entry(row) for row in self._call("GET", "history", params)]

    # --- printed labels -------------------------------------------------------

    def list_printed(self) -> list[dict]:
        return [_shape_printed(row) for row in self._call("GET", PRINTED_TAGS, PRINTED_SELECT)]

    def record_printed(self, tag_ids: list[int], actor: str) -> None:
        wanted = once_each(tag_ids)
        if not wanted:
            return
        now = utc_now()
        # one request for what is there, one for the whole print. The API sends at most
        # 250 ids, which fit in one address.
        where = {"tag_id": f"in.({','.join(str(tag_id) for tag_id in wanted)})"}
        rows = self._call("GET", PRINTED_TAGS, {**PRINTED_SELECT, **where})
        known = {row["tag_id"]: _shape_printed(row) for row in rows}
        self._call(
            "POST",
            PRINTED_TAGS,
            {"on_conflict": "tag_id"},
            printed_rows(wanted, known, now, actor),
            prefer="resolution=merge-duplicates,return=minimal",
        )

    def forget_printed(self, tag_id: int) -> bool:
        rows = self._call(
            "DELETE", PRINTED_TAGS, {"tag_id": f"eq.{tag_id}", "select": "tag_id"}, prefer="return=representation"
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
