"""Real-PostgREST round trip: red until the deployed stack matches.

Exercises the PostgREST store end to end: minted JWT -> role -> schema
profile -> tables. It only ever touches negative tag ids, which the API
refuses, so it cannot collide with a printed tag. Cleanup is a filtered
delete of exactly those tag ids, and their items and photos go with them.

History is kept for good, so the entries these tests write stay behind.
They carry the same negative tag ids, which the API never returns. Every
assertion here looks only at the entries of the box its own test made.
"""

import io
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from PIL import Image

from app import bootstrap, history, photos
from app.stores.base import StoreError
from app.stores.postgrest import PostgrestStore

TAG_A = -9001
TAG_B = -9002
TAG_C = -9003
TEST_TAGS = (TAG_A, TAG_B, TAG_C)

BOX_KEYS = {"id", "tag_id", "name", "location", "notes", "created_at", "updated_at", "updated_by", "items", "photos"}
ITEM_KEYS = {"id", "box_id", "name", "qty", "created_at", "updated_at", "photos"}
PHOTO_KEYS = {"id", "box_id", "item_id", "width", "height", "size", "created_at", "created_by"}
HISTORY_KEYS = {"id", "at", "actor", "action", "tag_id", "box_id", "box_name", "item_id", "item_name", "changes"}

# PostgREST reloads its schema cache a moment after a new table is created.
RELOAD_SECONDS = 20


def _remove_test_boxes(store: PostgrestStore) -> None:
    for tag_id in TEST_TAGS:
        store.delete_box(tag_id)


def _wait_until_served(store: PostgrestStore, table: str) -> None:
    deadline = time.monotonic() + RELOAD_SECONDS
    while True:
        try:
            store._call("GET", table, {"select": "id", "limit": "1"})
            return
        except StoreError as exc:
            assert time.monotonic() < deadline, f"postgrest does not serve {table}, red, not skipped: {exc.detail}"
            time.sleep(0.5)


def _image(size: tuple[int, int] = (64, 48)) -> dict:
    out = io.BytesIO()
    Image.new("RGB", size, (220, 30, 30)).save(out, format="JPEG")
    return photos.process(out.getvalue())


def _entries(store: PostgrestStore, box: dict) -> list[dict]:
    """History of one box, newest first. Entries left by earlier runs belong to other boxes."""
    listed = store.list_history(box["tag_id"], 200, None, include_tests=True)
    return [entry for entry in listed if entry["box_id"] == box["id"]]


class Clock:
    """Stands in for the history clock, starting now, so a test can let two minutes pass without waiting."""

    def __init__(self):
        self.now = datetime.now(timezone.utc)

    def __call__(self) -> str:
        self.now += timedelta(seconds=1)
        return self.now.isoformat(timespec="microseconds")

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture()
def clock(live_store, monkeypatch):
    clock = Clock()
    # an earlier run let its clock run ahead, and its entries are still there
    for tag_id in TEST_TAGS:
        for entry in live_store.list_history(tag_id, 1, None, include_tests=True):
            clock.now = max(clock.now, datetime.fromisoformat(entry["at"]))
    monkeypatch.setattr(history, "clock", clock)
    return clock


@pytest.fixture(scope="module")
def live_store():
    ok, detail = bootstrap.db_reachable()
    assert ok, f"database unreachable, red, not skipped: {detail}"
    bootstrap.apply_schema()
    store = PostgrestStore()
    reachable, detail = store.health()
    assert reachable, f"postgrest unreachable, red, not skipped: {detail}"
    for table in ("history", "photos"):
        _wait_until_served(store, table)
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
    assert created["photos"] == []
    assert created["created_at"].endswith("+00:00")
    assert store.get_box_by_id(created["id"]) == created
    assert store.get_box_by_id(str(uuid.uuid4())) is None
    assert store.get_box_by_id("not-a-uuid") is None

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
    assert item["photos"] == []
    assert item["created_at"] == item["updated_at"]
    assert store.get_item(item["id"]) == item

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
    assert store.get_item(str(uuid.uuid4())) is None
    assert store.get_item("not-a-uuid") is None
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


# --- photos -------------------------------------------------------------------


def test_photo_round_trip(store):
    box = store.upsert_box(TAG_A, {"name": "Photos"}, "alice")
    image = _image((64, 48))

    photo = store.add_photo(box["id"], None, image, "bob")

    assert set(photo) == PHOTO_KEYS
    assert uuid.UUID(photo["id"])
    assert (photo["box_id"], photo["item_id"]) == (box["id"], None)
    assert (photo["width"], photo["height"]) == (64, 48)
    assert photo["size"] == len(image["data"])
    assert photo["created_by"] == "bob"
    assert photo["created_at"].endswith("+00:00")
    assert store.get_photo(photo["id"]) == photo

    touched = store.get_box(TAG_A)
    assert touched["photos"] == [photo]
    assert touched["updated_by"] == "bob"
    assert touched["updated_at"] == photo["created_at"]

    assert store.delete_photo(photo["id"]) is True
    assert store.delete_photo(photo["id"]) is False
    assert store.get_photo(photo["id"]) is None
    assert store.get_box(TAG_A)["photos"] == []


def test_photo_bytes_survive_the_trip_through_json(store):
    box = store.upsert_box(TAG_A, {}, "")
    image = _image((640, 480))

    photo = store.add_photo(box["id"], None, image, "")

    full = store.get_photo_data(photo["id"])
    thumb = store.get_photo_data(photo["id"], thumb=True)
    assert full == image["data"]
    assert thumb == image["thumb"]
    assert Image.open(io.BytesIO(full)).size == (640, 480)
    assert Image.open(io.BytesIO(thumb)).size == (320, 240)


def test_photos_hang_on_their_box_or_their_item(store):
    store.upsert_box(TAG_A, {"name": "A"}, "")
    item = store.add_item(TAG_B, "With a photo", 1, "")
    bare = store.add_item(TAG_B, "Without", 1, "")
    on_box = [store.add_photo(item["box_id"], None, _image(), "") for _ in range(2)]
    on_item = store.add_photo(item["box_id"], item["id"], _image(), "")

    box = store.get_box(TAG_B)

    assert set(box) == BOX_KEYS
    assert box["photos"] == on_box
    assert [set(entry) for entry in box["items"]] == [ITEM_KEYS, ITEM_KEYS]
    assert [entry["photos"] for entry in box["items"]] == [[on_item], []]
    assert store.get_item(item["id"])["photos"] == [on_item]
    assert store.get_item(bare["id"])["photos"] == []
    assert store.update_item(item["id"], {"qty": 2}, "")["photos"] == [on_item]

    ours = {listed["tag_id"]: listed for listed in store.list_boxes() if listed["tag_id"] in TEST_TAGS}
    assert ours[TAG_B] == store.get_box(TAG_B)
    assert ours[TAG_A]["photos"] == []
    # the list carries what a photo is, never the image itself
    for listed in ours.values():
        for photo in listed["photos"] + [photo for entry in listed["items"] for photo in entry["photos"]]:
            assert set(photo) == PHOTO_KEYS


def test_items_still_embed_now_that_photos_points_at_both_tables(store):
    names = [f"item {n:02d}" for n in range(4)]
    for name in names:
        store.add_item(TAG_C, name, 1, "")
    first = store.get_box(TAG_C)["items"][0]
    store.add_photo(first["box_id"], first["id"], _image(), "")

    assert [item["name"] for item in store.get_box(TAG_C)["items"]] == names
    ours = [box for box in store.list_boxes() if box["tag_id"] == TAG_C]
    assert [item["name"] for item in ours[0]["items"]] == names


def test_missing_photos_are_none_not_errors(store):
    assert store.get_photo(str(uuid.uuid4())) is None
    assert store.get_photo("not-a-uuid") is None
    assert store.get_photo_data(str(uuid.uuid4())) is None
    assert store.get_photo_data("not-a-uuid", thumb=True) is None
    assert store.delete_photo(str(uuid.uuid4())) is False
    assert store.delete_photo("not-a-uuid") is False


def test_photos_go_with_their_box(store):
    item = store.add_item(TAG_A, "Goes with the box", 1, "")
    on_box = store.add_photo(item["box_id"], None, _image(), "")
    on_item = store.add_photo(item["box_id"], item["id"], _image(), "")

    assert store.delete_box(TAG_A) is True

    assert store.get_photo(on_box["id"]) is None
    assert store.get_photo(on_item["id"]) is None
    assert store.get_photo_data(on_item["id"]) is None


def test_photos_go_with_their_item(store):
    item = store.add_item(TAG_A, "Goes", 1, "")
    on_box = store.add_photo(item["box_id"], None, _image(), "")
    on_item = store.add_photo(item["box_id"], item["id"], _image(), "")

    assert store.delete_item(item["id"]) is True

    assert store.get_photo(on_item["id"]) is None
    assert store.get_box(TAG_A)["photos"] == [on_box]


# --- history ------------------------------------------------------------------


def test_history_round_trip(store, clock):
    box = history.put_box(store, TAG_A, {"name": "History", "location": "Nowhere"}, "tester")

    entries = _entries(store, box)

    assert len(entries) == 1
    assert set(entries[0]) == HISTORY_KEYS
    assert uuid.UUID(entries[0]["id"])
    assert entries[0]["at"].endswith("+00:00")
    assert entries[0] == {
        "id": entries[0]["id"],
        "at": entries[0]["at"],
        "actor": "tester",
        "action": "box_created",
        "tag_id": TAG_A,
        "box_id": box["id"],
        "box_name": "History",
        "item_id": None,
        "item_name": "",
        "changes": {"name": [None, "History"], "location": [None, "Nowhere"]},
    }


def test_history_of_test_tags_is_hidden_unless_asked_for(store, clock):
    box = history.put_box(store, TAG_A, {"name": "Hidden"}, "tester")

    assert _entries(store, box)
    assert store.list_history(TAG_A, 50, None) == []
    assert all(entry["tag_id"] >= 0 for entry in store.list_history(None, 200, None))


def test_history_records_every_kind_of_write(store, clock):
    box = history.put_box(store, TAG_A, {"name": "Every kind"}, "tester")
    item = history.add_item(store, TAG_A, "Widget", 1, "tester")
    history.update_item(store, item["id"], {"qty": 4}, "tester")
    on_box = history.add_photo(store, box["id"], None, _image(), "tester")
    on_item = history.add_photo(store, box["id"], item["id"], _image(), "tester")
    history.delete_photo(store, on_box["id"], "tester")
    history.put_box(store, TAG_A, {"notes": "changed"}, "tester")
    other = history.add_item(store, TAG_A, "Gadget", 2, "tester")
    history.delete_item(store, item["id"], "tester")
    history.delete_box(store, TAG_A, "tester")

    entries = _entries(store, box)

    assert [(entry["action"], entry["item_id"], entry["changes"]) for entry in entries] == [
        (
            "box_deleted",
            None,
            {"name": ["Every kind", None], "notes": ["changed", None], "items": [[{"name": "Gadget", "qty": 2}], None]},
        ),
        ("item_removed", item["id"], {"name": ["Widget", None], "qty": [4, None], "photos": [1, None]}),
        ("item_added", other["id"], {"name": [None, "Gadget"], "qty": [None, 2]}),
        ("box_updated", None, {"notes": ["", "changed"]}),
        ("photo_removed", None, {"photo": [on_box["id"], None]}),
        ("photo_added", item["id"], {"photo": [None, on_item["id"]]}),
        ("photo_added", None, {"photo": [None, on_box["id"]]}),
        ("item_updated", item["id"], {"qty": [1, 4]}),
        ("item_added", item["id"], {"name": [None, "Widget"], "qty": [None, 1]}),
        ("box_created", None, {"name": [None, "Every kind"]}),
    ]
    assert {entry["actor"] for entry in entries} == {"tester"}
    assert [entry["at"] for entry in entries] == sorted((entry["at"] for entry in entries), reverse=True)
    # the box is gone and what happened to it is still there
    assert store.get_box(TAG_A) is None


def test_history_merges_quick_updates_and_drops_what_was_undone(store, clock):
    box = history.put_box(store, TAG_B, {"name": "Merge", "notes": "first"}, "tester")
    history.put_box(store, TAG_B, {"name": "Merge, renamed"}, "tester")
    history.put_box(store, TAG_B, {"name": "Merge, renamed again", "notes": "second"}, "tester")

    merged = _entries(store, box)
    assert [entry["action"] for entry in merged] == ["box_updated", "box_created"]
    assert merged[0]["changes"] == {"name": ["Merge", "Merge, renamed again"], "notes": ["first", "second"]}
    assert merged[0]["box_name"] == "Merge, renamed again"
    assert merged[0]["at"] > merged[1]["at"]

    history.put_box(store, TAG_B, {"name": "Merge"}, "tester")
    assert _entries(store, box)[0]["changes"] == {"notes": ["first", "second"]}

    history.put_box(store, TAG_B, {"notes": "first"}, "tester")
    assert [entry["action"] for entry in _entries(store, box)] == ["box_created"]

    # nothing changed, so nothing is written
    history.put_box(store, TAG_B, {"name": "Merge", "notes": "first"}, "tester")
    assert [entry["action"] for entry in _entries(store, box)] == ["box_created"]


def test_history_stops_merging_after_120_seconds_or_for_someone_else(store, clock):
    box = history.put_box(store, TAG_C, {"name": "Window"}, "tester")
    history.put_box(store, TAG_C, {"name": "Window, one"}, "tester")
    clock.advance(119)
    history.put_box(store, TAG_C, {"name": "Window, two"}, "tester")
    history.put_box(store, TAG_C, {"name": "Window, three"}, "someone else")

    assert [(entry["actor"], entry["changes"]) for entry in _entries(store, box)[:3]] == [
        ("someone else", {"name": ["Window, two", "Window, three"]}),
        ("tester", {"name": ["Window, one", "Window, two"]}),
        ("tester", {"name": ["Window", "Window, one"]}),
    ]


def test_history_pages_with_before(store, clock):
    box = history.put_box(store, TAG_A, {"name": "Pages"}, "tester")
    for number in range(4):
        history.add_item(store, TAG_A, f"item {number}", 1, "tester")
    everything = _entries(store, box)
    assert len(everything) == 5

    first = store.list_history(TAG_A, 2, None, include_tests=True)
    second = store.list_history(TAG_A, 2, first[-1]["at"], include_tests=True)

    assert first == everything[:2]
    assert second == everything[2:4]
    assert all(entry["at"] < first[-1]["at"] for entry in second)
