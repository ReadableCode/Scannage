"""SQLite store against a real database file under tmp_path."""

import os
import sqlite3
import subprocess
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app import history, samples
from app.stores.base import StoreError
from app.stores.sqlite import SqliteStore

REPO_ROOT = Path(__file__).resolve().parent.parent

BOX_KEYS = {"id", "tag_id", "name", "location", "notes", "created_at", "updated_at", "updated_by", "items", "photos"}
ITEM_KEYS = {"id", "box_id", "name", "qty", "created_at", "updated_at", "photos"}
PHOTO_KEYS = {"id", "box_id", "item_id", "width", "height", "size", "created_at", "created_by"}
HISTORY_KEYS = {"id", "at", "actor", "action", "tag_id", "box_id", "box_name", "item_id", "item_name", "changes"}

SAMPLE_TAGS = [1, 2, 3, 4, 5, 6]
SAMPLE_NAMES = ["Camping", "Christmas", "Paint supplies", "Power tools", "Car care", "Cables"]

# The store keeps whatever bytes it is handed; processing is the API's job.
IMAGE = {"width": 4, "height": 3, "data": b"\xff\xd8 full \x00\xff", "thumb": b"\xff\xd8 small"}

# The tables exactly as version 1 created them, before history and photos.
VERSION_1_SCHEMA = """
CREATE TABLE IF NOT EXISTS boxes (
    id         TEXT PRIMARY KEY,
    tag_id     INTEGER UNIQUE NOT NULL,
    name       TEXT NOT NULL DEFAULT '',
    location   TEXT NOT NULL DEFAULT '',
    notes      TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS items (
    id         TEXT PRIMARY KEY,
    box_id     TEXT NOT NULL REFERENCES boxes (id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    qty        INTEGER NOT NULL DEFAULT 1 CHECK (qty >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS items_box_id_idx ON items (box_id);

CREATE TABLE IF NOT EXISTS app_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@pytest.fixture()
def store(tmp_path):
    store = SqliteStore(tmp_path / "nested" / "scannage.db")
    # a database that was initialised before: the flag is what keeps the samples out
    store.apply_schema()
    store.set_meta(samples.META_KEY, "set by the test")
    store.bootstrap()
    return store


@pytest.fixture()
def fresh(tmp_path):
    """A store on a database file that does not exist yet."""
    return SqliteStore(tmp_path / "fresh" / "scannage.db")


def _tables(store: SqliteStore) -> set[str]:
    with sqlite3.connect(store.path) as conn:
        return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _entry(tag_id: int, at: str, **fields) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "at": at,
        "actor": "",
        "action": "box_updated",
        "tag_id": tag_id,
        "box_id": str(uuid.uuid4()),
        "box_name": "Camping",
        "item_id": None,
        "item_name": "",
        "changes": {"name": ["Camp", "Camping"]},
        **fields,
    }


def _at(second: int) -> str:
    return f"2026-01-01T00:00:{second:02d}.000000+00:00"


def _is_utc_iso(value: str) -> bool:
    return datetime.fromisoformat(value).utcoffset() == timedelta(0)


def test_bootstrap_creates_parent_dir_and_is_idempotent(store):
    assert store.path.is_file()
    store.upsert_box(1, {"name": "Camping"}, "")
    store.add_item(1, "Tent", 1, "")
    store.set_meta("k", "v")

    store.bootstrap()
    store.bootstrap()

    box = store.get_box(1)
    assert box["name"] == "Camping"
    assert [item["name"] for item in box["items"]] == ["Tent"]
    assert store.get_meta("k") == "v"


def test_health(store, tmp_path):
    assert store.health() == (True, "ok")

    fresh = SqliteStore(tmp_path / "never_bootstrapped.db")
    ok, detail = fresh.health()
    assert not ok
    assert "boxes" in detail


def test_upsert_creates_box_with_defaults(store):
    box = store.upsert_box(7, {"name": "Camping"}, "alice")

    assert set(box) == BOX_KEYS
    assert uuid.UUID(box["id"])
    assert box["tag_id"] == 7
    assert box["name"] == "Camping"
    assert box["location"] == ""
    assert box["notes"] == ""
    assert box["updated_by"] == "alice"
    assert box["items"] == []
    assert box["photos"] == []
    assert box["created_at"] == box["updated_at"]
    assert _is_utc_iso(box["created_at"])


def test_upsert_updates_only_given_fields(store):
    first = store.upsert_box(7, {"name": "Camping", "location": "Shelf A", "notes": "heavy"}, "alice")
    second = store.upsert_box(7, {"location": "Shelf B"}, "bob")

    assert second["id"] == first["id"]
    assert second["name"] == "Camping"
    assert second["location"] == "Shelf B"
    assert second["notes"] == "heavy"
    assert second["updated_by"] == "bob"
    assert second["created_at"] == first["created_at"]
    assert second["updated_at"] > first["updated_at"]


def test_upsert_ignores_unknown_and_none_fields(store):
    store.upsert_box(7, {"name": "Camping"}, "")
    box = store.upsert_box(7, {"name": None, "tag_id": 99, "id": "x", "bogus": "y"}, "")

    assert box["tag_id"] == 7
    assert box["name"] == "Camping"
    assert store.get_box(99) is None


def test_upsert_can_clear_a_field(store):
    store.upsert_box(7, {"notes": "heavy"}, "")
    assert store.upsert_box(7, {"notes": ""}, "")["notes"] == ""


def test_get_box_missing_is_none(store):
    assert store.get_box(7) is None


def test_get_box_by_id(store):
    box = store.upsert_box(7, {"name": "Camping"}, "")
    store.add_item(7, "Tent", 1, "")

    assert store.get_box_by_id(box["id"]) == store.get_box(7)
    assert store.get_box_by_id(str(uuid.uuid4())) is None
    assert store.get_box_by_id("not-a-uuid") is None


def test_store_accepts_negative_tag_ids(store):
    assert store.upsert_box(-9001, {"name": "Test"}, "")["tag_id"] == -9001
    assert store.delete_box(-9001) is True


def test_list_boxes_ordered_by_tag_id_with_items(store):
    for tag_id in (5, 1, 3):
        store.upsert_box(tag_id, {"name": f"box {tag_id}"}, "")
    store.add_item(3, "Rope", 2, "")

    boxes = store.list_boxes()

    assert [box["tag_id"] for box in boxes] == [1, 3, 5]
    assert [[item["name"] for item in box["items"]] for box in boxes] == [[], ["Rope"], []]
    assert all(set(box) == BOX_KEYS for box in boxes)


def test_list_boxes_empty(store):
    assert store.list_boxes() == []


def test_delete_box(store):
    store.upsert_box(7, {"name": "Camping"}, "")

    assert store.delete_box(7) is True
    assert store.get_box(7) is None
    assert store.delete_box(7) is False


def test_delete_box_frees_the_tag(store):
    first = store.upsert_box(7, {"name": "Camping"}, "")
    store.delete_box(7)
    second = store.upsert_box(7, {"name": "Tools"}, "")

    assert second["id"] != first["id"]
    assert second["name"] == "Tools"


def test_delete_box_cascades_to_items(store):
    item = store.add_item(7, "Tent", 1, "")
    other = store.add_item(8, "Drill", 1, "")

    store.delete_box(7)

    with sqlite3.connect(store.path) as conn:
        remaining = [row[0] for row in conn.execute("SELECT id FROM items")]
    assert remaining == [other["id"]]
    assert store.update_item(item["id"], {"qty": 2}, "") is None


def test_add_item(store):
    box = store.upsert_box(7, {"name": "Camping"}, "alice")
    item = store.add_item(7, "Sleeping bag", 2, "bob")

    assert set(item) == ITEM_KEYS
    assert uuid.UUID(item["id"])
    assert item["box_id"] == box["id"]
    assert item["name"] == "Sleeping bag"
    assert item["qty"] == 2
    assert item["photos"] == []
    assert item["created_at"] == item["updated_at"]
    assert _is_utc_iso(item["created_at"])
    assert store.get_item(item["id"]) == item

    after = store.get_box(7)
    assert after["items"] == [item]
    assert after["updated_by"] == "bob"
    assert after["updated_at"] > box["updated_at"]


def test_add_item_creates_the_box(store):
    item = store.add_item(12, "Lantern", 1, "alice")
    box = store.get_box(12)

    assert box is not None
    assert box["id"] == item["box_id"]
    assert box["name"] == ""
    assert box["updated_by"] == "alice"
    assert box["items"] == [item]


def test_items_ordered_by_created_at(store):
    names = [f"item {n:02d}" for n in range(25)]
    for name in names:
        store.add_item(7, name, 1, "")

    assert [item["name"] for item in store.get_box(7)["items"]] == names
    assert [item["name"] for item in store.list_boxes()[0]["items"]] == names


def test_items_with_equal_created_at_ordered_by_id(store):
    ids = sorted(store.add_item(7, f"item {n}", 1, "")["id"] for n in range(5))
    with sqlite3.connect(store.path) as conn:
        conn.execute("UPDATE items SET created_at = ? WHERE box_id IS NOT NULL", ("2026-01-01T00:00:00.000000+00:00",))

    assert [item["id"] for item in store.get_box(7)["items"]] == ids


def test_update_item(store):
    item = store.add_item(7, "Lantern", 1, "alice")

    renamed = store.update_item(item["id"], {"name": "Lantern, red"}, "bob")
    assert renamed["name"] == "Lantern, red"
    assert renamed["qty"] == 1
    assert renamed["created_at"] == item["created_at"]
    assert renamed["updated_at"] > item["updated_at"]

    recounted = store.update_item(item["id"], {"qty": 4}, "carol")
    assert recounted["name"] == "Lantern, red"
    assert recounted["qty"] == 4

    box = store.get_box(7)
    assert box["items"] == [recounted]
    assert box["updated_by"] == "carol"
    assert box["updated_at"] == recounted["updated_at"]


def test_update_item_without_fields_changes_nothing(store):
    item = store.add_item(7, "Lantern", 1, "alice")

    assert store.update_item(item["id"], {}, "bob") == item
    assert store.get_box(7)["updated_by"] == "alice"


def test_update_item_missing_is_none(store):
    assert store.update_item(str(uuid.uuid4()), {"qty": 2}, "") is None
    assert store.update_item("not-a-uuid", {"qty": 2}, "") is None


def test_get_item_missing_is_none(store):
    assert store.get_item(str(uuid.uuid4())) is None
    assert store.get_item("not-a-uuid") is None


def test_delete_item(store):
    keep = store.add_item(7, "Lantern", 1, "")
    drop = store.add_item(7, "Tarp", 1, "")

    assert store.delete_item(drop["id"]) is True
    assert store.delete_item(drop["id"]) is False
    assert store.delete_item("not-a-uuid") is False
    assert store.get_box(7)["items"] == [keep]


def test_qty_below_one_is_refused_by_the_database(store):
    with pytest.raises(StoreError):
        store.add_item(7, "Lantern", 0, "")
    # the failed write rolled back, including the box it would have created
    assert store.get_box(7) is None


def test_meta_get_set(store):
    assert store.get_meta("a_flag") is None

    store.set_meta("a_flag", "first")
    assert store.get_meta("a_flag") == "first"

    store.set_meta("a_flag", "second")
    assert store.get_meta("a_flag") == "second"
    assert store.get_meta("other") is None


def test_unusable_path_raises_store_error(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("not a directory")

    with pytest.raises(StoreError):
        SqliteStore(blocker / "scannage.db").bootstrap()


# --- photos -------------------------------------------------------------------


def test_add_photo_to_a_box(store):
    box = store.upsert_box(7, {"name": "Camping"}, "alice")

    photo = store.add_photo(box["id"], None, IMAGE, "bob")

    assert set(photo) == PHOTO_KEYS
    assert uuid.UUID(photo["id"])
    assert photo["box_id"] == box["id"]
    assert photo["item_id"] is None
    assert (photo["width"], photo["height"]) == (4, 3)
    assert photo["size"] == len(IMAGE["data"])
    assert photo["created_by"] == "bob"
    assert _is_utc_iso(photo["created_at"])
    assert store.get_photo(photo["id"]) == photo

    after = store.get_box(7)
    assert after["photos"] == [photo]
    assert after["updated_by"] == "bob"
    assert after["updated_at"] == photo["created_at"]


def test_add_photo_to_an_item(store):
    item = store.add_item(7, "Tent", 1, "")
    other = store.add_item(7, "Tarp", 1, "")

    photo = store.add_photo(item["box_id"], item["id"], IMAGE, "")

    assert photo["item_id"] == item["id"]
    box = store.get_box(7)
    # a photo of an item is not a photo of its box
    assert box["photos"] == []
    assert [entry["photos"] for entry in box["items"]] == [[photo], []]
    assert store.get_item(item["id"])["photos"] == [photo]
    assert store.get_item(other["id"])["photos"] == []
    assert store.update_item(item["id"], {"qty": 2}, "")["photos"] == [photo]
    assert store.update_item(item["id"], {}, "")["photos"] == [photo]


def test_photo_bytes_come_back_as_they_went_in(store):
    box = store.upsert_box(7, {}, "")
    photo = store.add_photo(box["id"], None, IMAGE, "")

    assert store.get_photo_data(photo["id"]) == IMAGE["data"]
    assert store.get_photo_data(photo["id"], thumb=True) == IMAGE["thumb"]
    assert isinstance(store.get_photo_data(photo["id"]), bytes)


def test_photo_lists_never_hold_the_bytes(store):
    item = store.add_item(7, "Tent", 1, "")
    store.add_photo(item["box_id"], None, IMAGE, "")
    store.add_photo(item["box_id"], item["id"], IMAGE, "")

    for box in (store.get_box(7), store.list_boxes()[0]):
        assert set(box) == BOX_KEYS
        assert [set(photo) for photo in box["photos"]] == [PHOTO_KEYS]
        assert set(box["items"][0]) == ITEM_KEYS
        assert [set(photo) for photo in box["items"][0]["photos"]] == [PHOTO_KEYS]


def test_photos_ordered_by_created_at_then_id(store):
    box = store.upsert_box(7, {}, "")
    added = [store.add_photo(box["id"], None, IMAGE, "")["id"] for _ in range(5)]
    assert [photo["id"] for photo in store.get_box(7)["photos"]] == added

    with sqlite3.connect(store.path) as conn:
        conn.execute("UPDATE photos SET created_at = ? WHERE box_id = ?", (_at(0), box["id"]))
    assert [photo["id"] for photo in store.get_box(7)["photos"]] == sorted(added)
    assert [photo["id"] for photo in store.list_boxes()[0]["photos"]] == sorted(added)


def test_list_boxes_hangs_each_photo_where_it_belongs(store):
    first = store.add_item(3, "Rope", 1, "")
    second = store.add_item(5, "Drill", 1, "")
    on_box = store.add_photo(first["box_id"], None, IMAGE, "")
    on_item = store.add_photo(second["box_id"], second["id"], IMAGE, "")

    boxes = store.list_boxes()

    assert [box["tag_id"] for box in boxes] == [3, 5]
    assert boxes[0]["photos"] == [on_box]
    assert boxes[0]["items"][0]["photos"] == []
    assert boxes[1]["photos"] == []
    assert boxes[1]["items"][0]["photos"] == [on_item]
    assert boxes == [store.get_box(3), store.get_box(5)]


def test_add_photo_needs_a_box_that_exists(store):
    with pytest.raises(StoreError):
        store.add_photo(str(uuid.uuid4()), None, IMAGE, "")


def test_delete_photo(store):
    box = store.upsert_box(7, {}, "")
    keep = store.add_photo(box["id"], None, IMAGE, "")
    drop = store.add_photo(box["id"], None, IMAGE, "")

    assert store.delete_photo(drop["id"]) is True
    assert store.delete_photo(drop["id"]) is False
    assert store.delete_photo("not-a-uuid") is False
    assert store.get_photo(drop["id"]) is None
    assert store.get_photo_data(drop["id"]) is None
    assert store.get_box(7)["photos"] == [keep]


def test_missing_photos_are_none_not_errors(store):
    assert store.get_photo(str(uuid.uuid4())) is None
    assert store.get_photo("not-a-uuid") is None
    assert store.get_photo_data(str(uuid.uuid4())) is None
    assert store.get_photo_data("not-a-uuid", thumb=True) is None


def test_photos_go_with_their_box(store):
    item = store.add_item(7, "Tent", 1, "")
    on_box = store.add_photo(item["box_id"], None, IMAGE, "")
    on_item = store.add_photo(item["box_id"], item["id"], IMAGE, "")
    elsewhere = store.add_photo(store.upsert_box(8, {}, "")["id"], None, IMAGE, "")

    store.delete_box(7)

    assert store.get_photo(on_box["id"]) is None
    assert store.get_photo(on_item["id"]) is None
    with sqlite3.connect(store.path) as conn:
        assert [row[0] for row in conn.execute("SELECT id FROM photos")] == [elsewhere["id"]]


def test_photos_go_with_their_item(store):
    item = store.add_item(7, "Tent", 1, "")
    on_box = store.add_photo(item["box_id"], None, IMAGE, "")
    on_item = store.add_photo(item["box_id"], item["id"], IMAGE, "")

    store.delete_item(item["id"])

    assert store.get_photo(on_item["id"]) is None
    assert store.get_box(7)["photos"] == [on_box]


# --- history ------------------------------------------------------------------


def test_history_round_trip(store):
    item_id = str(uuid.uuid4())
    entry = _entry(7, _at(1), actor="alice", action="item_updated", item_id=item_id, item_name="Tent")
    entry["changes"] = {"qty": [1, 2], "name": ["Tent", "Tent, 4 person"]}

    assert store.add_history(entry) == entry

    listed = store.list_history(None, 50, None)
    assert listed == [entry]
    assert set(listed[0]) == HISTORY_KEYS
    assert listed[0]["changes"]["qty"] == [1, 2]


def test_history_keeps_nested_changes(store):
    changes = {"name": ["Camping", None], "items": [[{"name": "Tent", "qty": 1}], None], "photos": [2, None]}
    entry = _entry(7, _at(1), action="box_deleted", changes=changes)
    store.add_history(entry)

    assert store.list_history(7, 1, None)[0]["changes"] == changes


def test_history_is_newest_first_and_limited(store):
    entries = [store.add_history(_entry(7, _at(second))) for second in (3, 1, 2)]

    listed = store.list_history(None, 50, None)

    assert [entry["at"] for entry in listed] == [_at(3), _at(2), _at(1)]
    assert store.list_history(None, 2, None) == listed[:2]
    assert {entry["id"] for entry in listed} == {entry["id"] for entry in entries}


def test_history_before_pages_through(store):
    for second in range(1, 6):
        store.add_history(_entry(7, _at(second)))

    first = store.list_history(None, 2, None)
    second = store.list_history(None, 2, first[-1]["at"])
    third = store.list_history(None, 2, second[-1]["at"])

    assert [entry["at"] for entry in first] == [_at(5), _at(4)]
    assert [entry["at"] for entry in second] == [_at(3), _at(2)]
    assert [entry["at"] for entry in third] == [_at(1)]
    assert store.list_history(None, 2, third[-1]["at"]) == []


def test_history_filters_by_tag(store):
    store.add_history(_entry(7, _at(1)))
    store.add_history(_entry(8, _at(2)))
    store.add_history(_entry(7, _at(3)))

    assert [entry["at"] for entry in store.list_history(7, 50, None)] == [_at(3), _at(1)]
    assert [entry["at"] for entry in store.list_history(8, 50, None)] == [_at(2)]
    assert [entry["at"] for entry in store.list_history(7, 50, _at(3))] == [_at(1)]
    assert store.list_history(9, 50, None) == []


def test_history_of_negative_tags_is_hidden_unless_asked_for(store):
    real = store.add_history(_entry(7, _at(1)))
    test = store.add_history(_entry(-9001, _at(2)))

    assert store.list_history(None, 50, None) == [real]
    assert store.list_history(-9001, 50, None) == []
    assert store.list_history(None, 50, None, include_tests=True) == [test, real]
    assert store.list_history(-9001, 50, None, include_tests=True) == [test]


def test_update_history(store):
    entry = store.add_history(_entry(7, _at(1)))
    other = store.add_history(_entry(7, _at(2)))

    store.update_history(entry["id"], {"name": ["Camp", "Camping gear"]}, _at(9), "Camping gear", "")

    changed, untouched = store.list_history(7, 50, None)
    assert changed == {
        **entry,
        "at": _at(9),
        "box_name": "Camping gear",
        "changes": {"name": ["Camp", "Camping gear"]},
    }
    assert untouched == other


def test_delete_history(store):
    entry = store.add_history(_entry(7, _at(1)))
    other = store.add_history(_entry(7, _at(2)))

    assert store.delete_history(entry["id"]) is True
    assert store.delete_history(entry["id"]) is False
    assert store.list_history(7, 50, None) == [other]


def test_history_outlives_its_box(store):
    box = store.upsert_box(7, {"name": "Camping"}, "")
    entry = store.add_history(_entry(7, _at(1), box_id=box["id"]))

    store.delete_box(7)

    assert store.list_history(7, 50, None) == [entry]


# --- sample boxes -------------------------------------------------------------


def test_a_new_database_gets_the_samples(fresh):
    fresh.bootstrap()

    boxes = fresh.list_boxes()
    assert [box["tag_id"] for box in boxes] == SAMPLE_TAGS
    assert [box["name"] for box in boxes] == SAMPLE_NAMES
    assert [box["location"] for box in boxes] == ["Shelf A, top"] * 3 + ["Shelf A, bottom"] * 3
    assert [(item["name"], item["qty"]) for item in boxes[0]["items"]] == [
        ("Tent, 4 person", 1),
        ("Sleeping bag", 2),
        ("Camp stove", 1),
        ("Lantern", 2),
        ("Tarp", 1),
        ("Tent stakes", 12),
    ]
    assert [len(box["items"]) for box in boxes] == [6, 4, 5, 4, 3, 5]
    assert fresh.get_meta(samples.META_KEY) is not None


def test_samples_are_recorded_as_samples_not_as_a_person(fresh):
    fresh.bootstrap()

    entries = fresh.list_history(None, 200, None)

    assert len(entries) == 6 + 27
    assert {entry["actor"] for entry in entries} == {"samples"}
    assert {entry["action"] for entry in entries} == {"box_created", "item_added"}
    assert {box["updated_by"] for box in fresh.list_boxes()} == {"samples"}


def test_samples_do_not_come_back_once_deleted(fresh):
    fresh.bootstrap()
    fresh.bootstrap()
    assert [box["tag_id"] for box in fresh.list_boxes()] == SAMPLE_TAGS

    for tag_id in SAMPLE_TAGS:
        assert fresh.delete_box(tag_id) is True
    fresh.bootstrap()

    assert fresh.list_boxes() == []


def test_samples_are_never_added_to_an_inventory_in_use(fresh):
    fresh.apply_schema()
    fresh.upsert_box(40, {"name": "Mine"}, "")

    fresh.bootstrap()

    assert [box["tag_id"] for box in fresh.list_boxes()] == [40]
    assert fresh.get_meta(samples.META_KEY) is not None
    fresh.delete_box(40)
    fresh.bootstrap()
    assert fresh.list_boxes() == []


def test_apply_schema_alone_adds_no_samples(fresh):
    assert fresh.apply_schema() is True

    assert fresh.list_boxes() == []
    assert fresh.get_meta(samples.META_KEY) is None


class FailsOnTheThirdBox(SqliteStore):
    def upsert_box(self, tag_id, fields, actor):
        if tag_id == 3:
            raise StoreError(500, "disk on fire")
        return super().upsert_box(tag_id, fields, actor)


def test_failed_seeding_does_not_stop_the_boot_and_leaves_the_flag_unset(tmp_path):
    store = FailsOnTheThirdBox(tmp_path / "scannage.db")

    store.bootstrap()

    assert store.get_meta(samples.META_KEY) is None
    assert store.health() == (True, "ok")


def test_samples_add_fills_only_unclaimed_tags(store):
    mine = store.upsert_box(2, {"name": "Mine", "notes": "keep"}, "alice")
    store.add_item(2, "My thing", 3, "alice")
    mine = store.get_box(2)
    also_mine = store.upsert_box(5, {}, "alice")

    added = samples.add(store)

    assert added == [1, 3, 4, 6]
    boxes = {box["tag_id"]: box for box in store.list_boxes()}
    assert sorted(boxes) == SAMPLE_TAGS
    assert boxes[2] == mine
    assert boxes[5] == also_mine
    assert [boxes[tag_id]["name"] for tag_id in added] == ["Camping", "Paint supplies", "Power tools", "Cables"]
    assert {entry["tag_id"] for entry in store.list_history(None, 200, None)} == set(added)

    # asked for again with every tag taken, it adds nothing and changes nothing
    before = store.list_boxes()
    assert samples.add(store) == []
    assert store.list_boxes() == before


def test_samples_add_brings_them_back_on_purpose(fresh):
    fresh.bootstrap()
    for tag_id in SAMPLE_TAGS:
        fresh.delete_box(tag_id)

    assert samples.add(fresh) == SAMPLE_TAGS
    assert [box["name"] for box in fresh.list_boxes()] == SAMPLE_NAMES


def _init_db(path, *flags: str) -> str:
    env = {**os.environ, "SCANNAGE_STORE": "sqlite", "SCANNAGE_SQLITE_PATH": str(path)}
    script = REPO_ROOT / "scripts" / "init_db.py"
    done = subprocess.run([sys.executable, str(script), *flags], env=env, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return done.stdout


def test_init_db_without_a_flag_only_applies_the_schema(tmp_path):
    store = SqliteStore(tmp_path / "scannage.db")

    assert "applied" in _init_db(store.path)
    _init_db(store.path, "--force")

    assert {"boxes", "items", "app_meta", "history", "photos"} <= _tables(store)
    assert store.list_boxes() == []
    assert store.get_meta(samples.META_KEY) is None


def test_init_db_samples_skips_claimed_tags(tmp_path):
    store = SqliteStore(tmp_path / "scannage.db")
    store.apply_schema()
    mine = store.upsert_box(4, {"name": "Mine"}, "alice")

    out = _init_db(store.path, "--samples")

    assert "sample boxes added on tags: 1, 2, 3, 5, 6" in out
    assert "left alone: 4" in out
    boxes = store.list_boxes()
    assert [box["tag_id"] for box in boxes] == SAMPLE_TAGS
    assert boxes[3] == mine
    assert "sample boxes added on tags: none" in _init_db(store.path, "--samples")
    assert store.list_boxes() == boxes


# --- upgrade in place ---------------------------------------------------------


def test_a_version_1_database_is_upgraded_in_place(tmp_path):
    path = tmp_path / "scannage.db"
    stamp = "2026-05-01T10:00:00.000000+00:00"
    box_id, first_id, second_id = (str(uuid.uuid4()) for _ in range(3))
    with sqlite3.connect(path) as conn:
        conn.executescript(VERSION_1_SCHEMA)
        conn.execute(
            "INSERT INTO boxes (id, tag_id, name, location, notes, created_at, updated_at, updated_by) "
            "VALUES (?, 7, 'Camping', 'Shelf A, top', 'heavy', ?, ?, 'alice')",
            (box_id, stamp, stamp),
        )
        conn.execute(
            "INSERT INTO items (id, box_id, name, qty, created_at, updated_at) VALUES (?, ?, 'Tent', 1, ?, ?)",
            (first_id, box_id, stamp, stamp),
        )
        conn.execute(
            "INSERT INTO items (id, box_id, name, qty, created_at, updated_at) VALUES (?, ?, 'Tarp', 2, ?, ?)",
            (second_id, box_id, "2026-05-01T10:00:01.000000+00:00", stamp),
        )
    store = SqliteStore(path)
    assert _tables(store) == {"boxes", "items", "app_meta"}

    store.bootstrap()
    store.bootstrap()

    assert {"boxes", "items", "app_meta", "history", "photos"} <= _tables(store)
    assert store.list_boxes() == [
        {
            "id": box_id,
            "tag_id": 7,
            "name": "Camping",
            "location": "Shelf A, top",
            "notes": "heavy",
            "created_at": stamp,
            "updated_at": stamp,
            "updated_by": "alice",
            "items": [
                {
                    "id": first_id,
                    "box_id": box_id,
                    "name": "Tent",
                    "qty": 1,
                    "created_at": stamp,
                    "updated_at": stamp,
                    "photos": [],
                },
                {
                    "id": second_id,
                    "box_id": box_id,
                    "name": "Tarp",
                    "qty": 2,
                    "created_at": "2026-05-01T10:00:01.000000+00:00",
                    "updated_at": stamp,
                    "photos": [],
                },
            ],
            "photos": [],
        }
    ]
    # boxes exist, so it is an inventory in use: flagged, and never topped up
    assert store.get_meta(samples.META_KEY) is not None
    assert store.list_history(None, 50, None) == []

    # and the upgraded database takes the new kinds of write
    photo = store.add_photo(box_id, first_id, IMAGE, "bob")
    history.update_item(store, first_id, {"qty": 3}, "bob")
    assert store.get_box(7)["items"][0]["photos"] == [photo]
    assert [entry["changes"] for entry in store.list_history(7, 50, None)] == [{"qty": [1, 3]}]


def test_a_version_1_database_that_was_already_seeded_stays_as_it_is(tmp_path):
    path = tmp_path / "scannage.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(VERSION_1_SCHEMA)
        conn.execute("INSERT INTO app_meta (key, value) VALUES ('samples_seeded', 'long ago')")
    store = SqliteStore(path)

    store.bootstrap()

    assert store.list_boxes() == []
    assert store.get_meta(samples.META_KEY) == "long ago"
