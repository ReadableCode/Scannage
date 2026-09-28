"""SQLite store against a real database file under tmp_path."""

import sqlite3
import uuid
from datetime import datetime, timedelta

import pytest

from app.stores.base import StoreError
from app.stores.sqlite import SqliteStore

BOX_KEYS = {"id", "tag_id", "name", "location", "notes", "created_at", "updated_at", "updated_by", "items"}
ITEM_KEYS = {"id", "box_id", "name", "qty", "created_at", "updated_at"}


@pytest.fixture()
def store(tmp_path):
    store = SqliteStore(tmp_path / "nested" / "scannage.db")
    store.bootstrap()
    return store


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
    assert item["created_at"] == item["updated_at"]
    assert _is_utc_iso(item["created_at"])

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
    assert store.get_meta("samples_seeded") is None

    store.set_meta("samples_seeded", "first")
    assert store.get_meta("samples_seeded") == "first"

    store.set_meta("samples_seeded", "second")
    assert store.get_meta("samples_seeded") == "second"
    assert store.get_meta("other") is None


def test_unusable_path_raises_store_error(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("not a directory")

    with pytest.raises(StoreError):
        SqliteStore(blocker / "scannage.db").bootstrap()
