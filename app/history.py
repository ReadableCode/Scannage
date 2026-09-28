"""History: every write goes through here, so it is recorded the same way on either store.

Each function reads the state before, does the write through the store, works
out what changed and records it. Recording is best effort: when it fails the
write still stands and the failure is logged.

Reading goes through here too: list_entries hangs on each entry the photos it
refers to.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable

from .stores.base import BOX_FIELDS, ITEM_FIELDS, Store, new_id, utc_now

log = logging.getLogger("scannage.history")

MERGE_SECONDS = 120
MERGE_KEYS = ("action", "box_id", "item_id", "actor")
# How far back on a tag the names of a box and item that are gone are looked for.
NAME_LOOKBACK = 200

# Where entries get their time. Tests replace it to move time without sleeping.
clock: Callable[[], str] = utc_now

_FAILED = object()


def _safely(what: str, fn: Callable, *args):
    try:
        return fn(*args)
    except Exception:
        log.exception("history: %s failed, the write itself is not affected", what)
        return _FAILED


# --- changes ------------------------------------------------------------------


def _created(thing: dict, fields: tuple[str, ...]) -> dict:
    return {field: [None, thing[field]] for field in fields if thing[field] != ""}


def _removed(thing: dict, fields: tuple[str, ...]) -> dict:
    return {field: [thing[field], None] for field in fields if thing[field] != ""}


def _changed(before: dict, after: dict, fields: tuple[str, ...]) -> dict:
    return {field: [before[field], after[field]] for field in fields if before[field] != after[field]}


def _ids(photos: list[dict]) -> list[str]:
    return [photo["id"] for photo in photos]


def _box_photo_ids(box: dict) -> list[str]:
    """Its own photos, then those of its items."""
    return _ids(box["photos"]) + [photo_id for item in box["items"] for photo_id in _ids(item["photos"])]


def _with_photos(changes: dict, photo_ids: list[str]) -> dict:
    return {**changes, "photos": [photo_ids, None]} if photo_ids else changes


# --- recording ----------------------------------------------------------------


def _seconds_between(earlier: str, later: str) -> float:
    return (datetime.fromisoformat(later) - datetime.fromisoformat(earlier)).total_seconds()


def _merge(store: Store, entry: dict) -> bool:
    """Fold an update into the newest entry of its box when the contract allows it. True when it was."""
    newest = store.list_history(entry["tag_id"], 1, None, include_tests=True)
    if not newest:
        return False
    last = newest[0]
    if any(last[key] != entry[key] for key in MERGE_KEYS):
        return False
    if not 0 <= _seconds_between(last["at"], entry["at"]) < MERGE_SECONDS:
        return False
    changes = dict(last["changes"])
    for field, (before, after) in entry["changes"].items():
        first = changes[field][0] if field in changes else before
        if first == after:
            changes.pop(field, None)
        else:
            changes[field] = [first, after]
    if changes:
        store.update_history(last["id"], changes, entry["at"], entry["box_name"], entry["item_name"])
    else:
        store.delete_history(last["id"])
    return True


def _record(store: Store, action: str, box: dict, item: dict | None, changes: dict, actor: str) -> None:
    entry = {
        "id": new_id(),
        "at": clock(),
        "actor": actor,
        "action": action,
        "tag_id": box["tag_id"],
        "box_id": box["id"],
        "box_name": box["name"],
        "item_id": item["id"] if item else None,
        "item_name": item["name"] if item else "",
        "changes": changes,
    }
    if action.endswith("_updated"):
        if not changes or _merge(store, entry):
            return
    store.add_history(entry)


# --- boxes --------------------------------------------------------------------


def _box_written(store: Store, before: dict | None, box: dict, actor: str) -> None:
    if before is None:
        _record(store, "box_created", box, None, _created(box, BOX_FIELDS), actor)
    else:
        _record(store, "box_updated", box, None, _changed(before, box, BOX_FIELDS), actor)


def put_box(store: Store, tag_id: int, fields: dict, actor: str) -> dict:
    before = _safely("reading the box", store.get_box, tag_id)
    box = store.upsert_box(tag_id, fields, actor)
    if before is not _FAILED:
        _safely("recording a box", _box_written, store, before, box, actor)
    return box


def _box_deleted(store: Store, before: dict, actor: str) -> None:
    items = [{"name": item["name"], "qty": item["qty"]} for item in before["items"]]
    changes = {**_removed(before, BOX_FIELDS), "items": [items, None]}
    _record(store, "box_deleted", before, None, _with_photos(changes, _box_photo_ids(before)), actor)


def delete_box(store: Store, tag_id: int, actor: str) -> bool:
    before = _safely("reading the box", store.get_box, tag_id)
    deleted = store.delete_box(tag_id)
    if deleted and before is not _FAILED and before is not None:
        _safely("recording a deleted box", _box_deleted, store, before, actor)
    return deleted


# --- items --------------------------------------------------------------------


def _item_added(store: Store, before: dict | None, item: dict, actor: str) -> None:
    box = before
    if box is None:
        box = store.get_box_by_id(item["box_id"])
        if box is None:
            return
        # the tag was unclaimed, so the item brought its box with it
        _record(store, "box_created", box, None, _created(box, BOX_FIELDS), actor)
    _record(store, "item_added", box, item, _created(item, ITEM_FIELDS), actor)


def add_item(store: Store, tag_id: int, name: str, qty: int, actor: str) -> dict:
    before = _safely("reading the box", store.get_box, tag_id)
    item = store.add_item(tag_id, name, qty, actor)
    if before is not _FAILED:
        _safely("recording an item", _item_added, store, before, item, actor)
    return item


def _item_written(store: Store, action: str, item: dict, changes: dict, actor: str) -> None:
    box = store.get_box_by_id(item["box_id"])
    if box is not None:
        _record(store, action, box, item, changes, actor)


def update_item(store: Store, item_id: str, fields: dict, actor: str) -> dict | None:
    before = _safely("reading the item", store.get_item, item_id)
    item = store.update_item(item_id, fields, actor)
    if item is None or before is _FAILED or before is None:
        return item
    changes = _changed(before, item, ITEM_FIELDS)
    if changes:
        _safely("recording an item", _item_written, store, "item_updated", item, changes, actor)
    return item


def delete_item(store: Store, item_id: str, actor: str) -> bool:
    before = _safely("reading the item", store.get_item, item_id)
    deleted = store.delete_item(item_id)
    if deleted and before is not _FAILED and before is not None:
        changes = _with_photos(_removed(before, ITEM_FIELDS), _ids(before["photos"]))
        _safely("recording a removed item", _item_written, store, "item_removed", before, changes, actor)
    return deleted


# --- photos -------------------------------------------------------------------


def _photo_written(store: Store, action: str, photo: dict, changes: dict, actor: str) -> None:
    box = store.get_box_by_id(photo["box_id"])
    if box is None:
        return
    item = None
    if photo["item_id"] is not None:
        item = next((item for item in box["items"] if item["id"] == photo["item_id"]), None)
    _record(store, action, box, item, changes, actor)


def add_photo(store: Store, box_id: str, item_id: str | None, image: dict, actor: str) -> dict:
    photo = store.add_photo(box_id, item_id, image, actor)
    _safely("recording a photo", _photo_written, store, "photo_added", photo, {"photo": [None, photo["id"]]}, actor)
    return photo


def delete_photo(store: Store, photo_id: str, actor: str) -> bool:
    before = _safely("reading the photo", store.get_photo, photo_id)
    deleted = store.delete_photo(photo_id)
    if deleted and before is not _FAILED and before is not None:
        changes = {"photo": [before["id"], None]}
        _safely("recording a removed photo", _photo_written, store, "photo_removed", before, changes, actor)
    return deleted


def _last_name(entries: list[dict], key: str, value: str | None, name: str) -> str:
    if value is None:
        return ""
    return next((entry[name] for entry in entries if entry[key] == value), "")


def _photo_erased(store: Store, kept: dict, actor: str) -> None:
    if kept["tag_id"] is None:
        log.warning("history: photo %s was kept without its tag, so erasing it is not recorded", kept["id"])
        return
    # its box or item may be long gone, and their newest entries still say what they were called
    entries = store.list_history(kept["tag_id"], NAME_LOOKBACK, None, include_tests=True)
    box = {
        "id": kept["box_id"],
        "tag_id": kept["tag_id"],
        "name": _last_name(entries, "box_id", kept["box_id"], "box_name"),
    }
    item = None
    if kept["item_id"] is not None:
        item = {"id": kept["item_id"], "name": _last_name(entries, "item_id", kept["item_id"], "item_name")}
    _record(store, "photo_erased", box, item, {"photo": [kept["id"], None]}, actor)


def erase_photo(store: Store, photo_id: str, actor: str) -> bool:
    """Erases a kept photo for good. The caller has made sure it is not on a box or item."""
    before = _safely("reading the kept photo", store.get_kept_photo, photo_id)
    erased = store.erase_photo(photo_id)
    if erased and before is not _FAILED and before is not None:
        _safely("recording an erased photo", _photo_erased, store, before, actor)
    return erased


# --- reading ------------------------------------------------------------------


def _photo_ids(entry: dict) -> list[str]:
    """The photos an entry refers to. One written before photos were kept holds a count, and refers to none."""
    changes = entry["changes"]
    if entry["action"] in ("photo_added", "photo_removed"):
        listed = changes.get("photo")
    elif entry["action"] in ("box_deleted", "item_removed"):
        pair = changes.get("photos")
        listed = pair[0] if isinstance(pair, list) and pair else None
    else:
        return []
    return [value for value in listed if isinstance(value, str)] if isinstance(listed, list) else []


def list_entries(
    store: Store, tag_id: int | None, limit: int, before: str | None, include_tests: bool = False
) -> list[dict]:
    """A page of entries, each with the photos it refers to that still exist."""
    entries = store.list_history(tag_id, limit, before, include_tests)
    referred = [_photo_ids(entry) for entry in entries]
    wanted = [photo_id for photo_ids in referred for photo_id in photo_ids]
    # every photo of the page in one go, never entry by entry
    found = {photo["id"]: photo for photo in store.list_photos_by_ids(wanted)} if wanted else {}
    return [
        {**entry, "photos": [found[photo_id] for photo_id in photo_ids if photo_id in found]}
        for entry, photo_ids in zip(entries, referred)
    ]
