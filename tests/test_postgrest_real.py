"""Real-PostgREST round trip: red until the deployed stack matches.

Exercises the PostgREST store end to end: minted JWT -> role -> schema
profile -> tables. It only ever touches negative tag ids, which the API
refuses, so it cannot collide with a printed tag. Cleanup is a filtered
delete of exactly those tag ids.
"""

import uuid

import pytest

from app import bootstrap
from app.stores.postgrest import PostgrestStore

TAG_A = -9001
TAG_B = -9002
TAG_C = -9003
TEST_TAGS = (TAG_A, TAG_B, TAG_C)

BOX_KEYS = {"id", "tag_id", "name", "location", "notes", "created_at", "updated_at", "updated_by", "items"}
ITEM_KEYS = {"id", "box_id", "name", "qty", "created_at", "updated_at"}


def _remove_test_boxes(store: PostgrestStore) -> None:
    for tag_id in TEST_TAGS:
        store.delete_box(tag_id)


@pytest.fixture(scope="module")
def live_store():
    ok, detail = bootstrap.db_reachable()
    assert ok, f"database unreachable, red, not skipped: {detail}"
    bootstrap.apply_schema()
    store = PostgrestStore()
    reachable, detail = store.health()
    assert reachable, f"postgrest unreachable, red, not skipped: {detail}"
    return store


@pytest.fixture()
def store(live_store):
    # leftovers from an interrupted run would break the assertions below
    _remove_test_boxes(live_store)
    try:
        yield live_store
    finally:
        _remove_test_boxes(live_store)
        for tag_id in TEST_TAGS:
            assert live_store.get_box(tag_id) is None, f"test box {tag_id} was left behind"


def test_box_round_trip(store):
    assert store.get_box(TAG_A) is None

    created = store.upsert_box(TAG_A, {"name": "Round trip", "location": "Nowhere"}, "tester")
    assert set(created) == BOX_KEYS
    assert uuid.UUID(created["id"])
    assert created["tag_id"] == TAG_A
    assert created["name"] == "Round trip"
    assert created["location"] == "Nowhere"
    assert created["notes"] == ""
    assert created["updated_by"] == "tester"
    assert created["items"] == []
    assert created["created_at"].endswith("+00:00")

    updated = store.upsert_box(TAG_A, {"notes": "still a test"}, "other")
    assert updated["id"] == created["id"]
    assert updated["name"] == "Round trip"
    assert updated["notes"] == "still a test"
    assert updated["updated_by"] == "other"
    assert updated["created_at"] == created["created_at"]
    assert updated["updated_at"] > created["updated_at"]

    assert store.get_box(TAG_A) == updated
    assert store.delete_box(TAG_A) is True
    assert store.delete_box(TAG_A) is False
    assert store.get_box(TAG_A) is None


def test_list_boxes_is_ordered_and_embeds_items(store):
    store.upsert_box(TAG_A, {"name": "A"}, "")
    store.upsert_box(TAG_B, {"name": "B"}, "")
    names = [f"item {n:02d}" for n in range(6)]
    for name in names:
        store.add_item(TAG_B, name, 1, "")

    boxes = store.list_boxes()

    tag_ids = [box["tag_id"] for box in boxes]
    assert tag_ids == sorted(tag_ids)
    ours = [box for box in boxes if box["tag_id"] in TEST_TAGS]
    # negative ids sort first, and -9002 sorts before -9001
    assert [box["tag_id"] for box in ours] == [TAG_B, TAG_A]
    assert [item["name"] for item in ours[0]["items"]] == names
    assert ours[1]["items"] == []
    assert ours[0] == store.get_box(TAG_B)


def test_item_round_trip(store):
    box = store.upsert_box(TAG_A, {"name": "Items"}, "alice")

    item = store.add_item(TAG_A, "Widget", 3, "bob")
    assert set(item) == ITEM_KEYS
    assert item["box_id"] == box["id"]
    assert item["name"] == "Widget"
    assert item["qty"] == 3
    assert item["created_at"] == item["updated_at"]

    touched = store.get_box(TAG_A)
    assert touched["items"] == [item]
    assert touched["updated_by"] == "bob"

    changed = store.update_item(item["id"], {"name": "Widget, large", "qty": 5}, "carol")
    assert changed["name"] == "Widget, large"
    assert changed["qty"] == 5
    assert changed["created_at"] == item["created_at"]
    assert changed["updated_at"] > item["updated_at"]
    assert store.update_item(item["id"], {}, "dave") == changed

    after = store.get_box(TAG_A)
    assert after["items"] == [changed]
    assert after["updated_by"] == "carol"

    assert store.delete_item(item["id"]) is True
    assert store.delete_item(item["id"]) is False
    assert store.get_box(TAG_A)["items"] == []


def test_add_item_creates_the_box(store):
    item = store.add_item(TAG_C, "Loose item", 1, "alice")
    box = store.get_box(TAG_C)

    assert box is not None
    assert box["id"] == item["box_id"]
    assert box["name"] == ""
    assert box["updated_by"] == "alice"
    assert box["items"] == [item]


def test_missing_items_are_none_not_errors(store):
    assert store.update_item(str(uuid.uuid4()), {"qty": 2}, "") is None
    assert store.update_item("not-a-uuid", {"qty": 2}, "") is None
    assert store.delete_item(str(uuid.uuid4())) is False
    assert store.delete_item("not-a-uuid") is False


def test_delete_box_cascades_to_items(store):
    item = store.add_item(TAG_A, "Goes with the box", 1, "")

    assert store.delete_box(TAG_A) is True
    assert store.update_item(item["id"], {"qty": 2}, "") is None


def test_meta_is_readable_and_missing_keys_are_none(store):
    # read only: writing a key here would leave a row this test has no way to remove
    assert store.get_meta(f"ztest_{uuid.uuid4().hex}") is None
