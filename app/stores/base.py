"""The interface both stores implement, and the helpers they share.

Ids and timestamps are minted here, in the app, so a box, item, photo or
history entry looks the same whichever store wrote it.
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timedelta, timezone
from itertools import batched
from typing import Protocol

BOX_FIELDS = ("name", "location", "notes")
ITEM_FIELDS = ("name", "qty")
# Everything about a photo except the image bytes, which never ride along in a list.
PHOTO_KEYS = ("id", "box_id", "item_id", "width", "height", "size", "created_at", "created_by")
# A kept photo also says which tag it was on and when it left the inventory.
KEPT_PHOTO_KEYS = (*PHOTO_KEYS, "tag_id", "removed_at")
HISTORY_KEYS = ("id", "at", "actor", "action", "tag_id", "box_id", "box_name", "item_id", "item_name", "changes")
PRINTED_KEYS = ("tag_id", "first_printed_at", "last_printed_at", "times", "printed_by")


class StoreError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class Store(Protocol):
    name: str  # "sqlite" or "postgrest"

    def bootstrap(self) -> None: ...
    def apply_schema(self, force: bool = False) -> bool: ...
    def health(self) -> tuple[bool, str]: ...
    def list_boxes(self) -> list[dict]: ...
    def list_tag_ids(self) -> list[int]: ...
    def get_box(self, tag_id: int) -> dict | None: ...
    def get_box_by_id(self, box_id: str) -> dict | None: ...
    def upsert_box(self, tag_id: int, fields: dict, actor: str) -> dict: ...
    def delete_box(self, tag_id: int) -> bool: ...
    def get_item(self, item_id: str) -> dict | None: ...
    def add_item(self, tag_id: int, name: str, qty: int, actor: str) -> dict: ...
    def update_item(self, item_id: str, fields: dict, actor: str) -> dict | None: ...
    def delete_item(self, item_id: str) -> bool: ...
    def add_photo(self, box_id: str, item_id: str | None, image: dict, actor: str) -> dict: ...
    def get_photo(self, photo_id: str) -> dict | None: ...
    def get_photo_data(self, photo_id: str, thumb: bool = False) -> bytes | None: ...
    def delete_photo(self, photo_id: str) -> bool: ...
    def get_kept_photo(self, photo_id: str) -> dict | None: ...
    def list_photos_by_ids(self, photo_ids: list[str]) -> list[dict]: ...
    def erase_photo(self, photo_id: str) -> bool: ...
    def add_history(self, entry: dict) -> dict: ...
    def update_history(self, entry_id: str, changes: dict, at: str, box_name: str, item_name: str) -> None: ...
    def delete_history(self, entry_id: str) -> bool: ...

    def list_history(
        self, tag_id: int | None, limit: int, before: str | None, include_tests: bool = False
    ) -> list[dict]: ...

    def list_printed(self) -> list[dict]: ...
    def record_printed(self, tag_ids: list[int], actor: str) -> None: ...
    def forget_printed(self, tag_id: int) -> bool: ...
    def get_meta(self, key: str) -> str | None: ...
    def set_meta(self, key: str, value: str) -> None: ...


_clock_lock = threading.Lock()
_last_stamp = datetime.min.replace(tzinfo=timezone.utc)


def utc_now() -> str:
    """UTC ISO 8601 with fixed microsecond width, strictly increasing in this process.

    Items are ordered by created_at, so two written back to back must not tie.
    """
    global _last_stamp
    with _clock_lock:
        stamp = datetime.now(timezone.utc)
        if stamp <= _last_stamp:
            stamp = _last_stamp + timedelta(microseconds=1)
        _last_stamp = stamp
    return stamp.isoformat(timespec="microseconds")


def normalize_timestamp(value: str) -> str:
    """Whatever offset and precision a database hands back, return the app's own format."""
    return datetime.fromisoformat(value).astimezone(timezone.utc).isoformat(timespec="microseconds")


def new_id() -> str:
    return str(uuid.uuid4())


def clean_uuid(value: str) -> str | None:
    """Canonical form of an id, or None when it cannot be one (so the caller answers 404)."""
    try:
        return str(uuid.UUID(str(value)))
    except ValueError:
        return None


def clean_uuids(values: list) -> list[str]:
    """Canonical ids, each one once, in the order given. Whatever cannot be an id is left out."""
    cleaned = (clean_uuid(value) for value in values)
    return list(dict.fromkeys(value for value in cleaned if value is not None))


def once_each(tag_ids: list[int]) -> list[int]:
    """Each tag id once, in the order given, so a tag repeated in one print counts once."""
    return list(dict.fromkeys(tag_ids))


def chunks(values: list[str], size: int) -> list[list[str]]:
    return [list(batch) for batch in batched(values, size)]


def pick(fields: dict, allowed: tuple[str, ...]) -> dict:
    """Only the known columns that were actually supplied."""
    return {key: fields[key] for key in allowed if fields.get(key) is not None}


def attach_photos(boxes: list[dict], photos: list[dict]) -> list[dict]:
    """Hang each photo on its item, or on its box when it has no item. Photos arrive in contract order."""
    by_box: dict[str, list[dict]] = {}
    by_item: dict[str, list[dict]] = {}
    for photo in photos:
        if photo["item_id"] is None:
            by_box.setdefault(photo["box_id"], []).append(photo)
        else:
            by_item.setdefault(photo["item_id"], []).append(photo)
    for box in boxes:
        box["photos"] = by_box.get(box["id"], [])
        for item in box["items"]:
            item["photos"] = by_item.get(item["id"], [])
    return boxes
