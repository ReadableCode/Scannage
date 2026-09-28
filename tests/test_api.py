"""The HTTP API against a real SQLite store under tmp_path."""

import io
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from PIL.PngImagePlugin import PngInfo

from app import config, history, main, photos, qr, samples, stores
from app.stores.base import StoreError
from app.stores.sqlite import SqliteStore

SVG_NS = "{http://www.w3.org/2000/svg}"

PHOTO_KEYS = {"id", "box_id", "item_id", "width", "height", "size", "created_at", "created_by"}
HISTORY_KEYS = {
    "id",
    "at",
    "actor",
    "action",
    "tag_id",
    "box_id",
    "box_name",
    "item_id",
    "item_name",
    "changes",
    "photos",
}
PRINTED_KEYS = {"tag_id", "first_printed_at", "last_printed_at", "times", "printed_by"}
PHOTO_CACHE = "private, max-age=31536000, immutable"

RED = (220, 30, 30)
BLUE = (30, 30, 220)


@pytest.fixture()
def store(tmp_path):
    store = SqliteStore(tmp_path / "scannage.db")
    # a database that was initialised before, so the lifespan adds no sample boxes
    store.apply_schema()
    store.set_meta(samples.META_KEY, "set by the test")
    stores.set_store(store)
    yield store
    stores.set_store(None)


@pytest.fixture()
def fresh_store(tmp_path):
    """The app pointed at a database file that does not exist yet."""
    store = SqliteStore(tmp_path / "fresh.db")
    stores.set_store(store)
    yield store
    stores.set_store(None)


class Clock:
    """Stands in for the history clock: one second passes per entry, more when a test says so."""

    def __init__(self):
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def __call__(self) -> str:
        self.now += timedelta(seconds=1)
        return self.now.isoformat(timespec="microseconds")

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture()
def clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(history, "clock", clock)
    return clock


def _image(fmt: str = "JPEG", size: tuple[int, int] = (64, 48), mode: str = "RGB", **save) -> bytes:
    out = io.BytesIO()
    Image.new(mode, size, RED if mode == "RGB" else 0).save(out, format=fmt, **save)
    return out.getvalue()


def _upload(client, path: str, body: bytes | None = None, content_type: str = "image/jpeg", **headers):
    body = _image() if body is None else body
    return client.post(path, content=body, headers={"Content-Type": content_type, **headers})


def _opened(body: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(body))
    image.load()
    return image


def _is_close(pixel: tuple[int, ...], colour: tuple[int, int, int]) -> bool:
    # JPEG does not keep colours exactly
    return all(abs(got - want) < 40 for got, want in zip(pixel, colour))


def _kept(photo: dict) -> dict:
    return {**photo, "kept": True}


def _live(photo: dict) -> dict:
    return {**photo, "kept": False}


def _bytes(client, photo: dict) -> tuple[bytes, bytes]:
    """The image and its thumbnail, as served."""
    served = []
    for path in (f"/api/photos/{photo['id']}", f"/api/photos/{photo['id']}/thumb"):
        resp = client.get(path)
        assert resp.status_code == 200, path
        assert resp.headers["content-type"] == "image/jpeg"
        assert resp.headers["cache-control"] == PHOTO_CACHE
        served.append(resp.content)
    return served[0], served[1]


def _history(client, **params) -> list[dict]:
    resp = client.get("/api/history", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.fixture()
def client(store):
    # the context manager runs the lifespan, which bootstraps the store
    with TestClient(main.app) as client:
        yield client


# --- health and config --------------------------------------------------------


def test_health_ok(client):
    resp = client.get("/api/health")

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "store": "sqlite", "detail": "ok"}


def test_health_degraded_is_503(tmp_path):
    stores.set_store(SqliteStore(tmp_path / "never_bootstrapped.db"))
    try:
        # no context manager: the lifespan does not run, so the tables do not exist
        resp = TestClient(main.app).get("/api/health")
    finally:
        stores.set_store(None)

    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["store"] == "sqlite"
    assert body["detail"]


def test_config(client):
    resp = client.get("/api/config")

    assert resp.status_code == 200
    assert resp.json() == {
        "dictionary": "ARUCO_MIP_36h12",
        "tag_count": 250,
        "base_url": "http://testserver",
        "user": "",
        "store": "sqlite",
    }


def test_config_user_from_remote_user(client):
    assert client.get("/api/config", headers={"Remote-User": "alice"}).json()["user"] == "alice"


def test_base_url_from_forwarded_headers(client):
    headers = {"X-Forwarded-Proto": "https", "X-Forwarded-Host": "boxes.example.com"}
    assert client.get("/api/config", headers=headers).json()["base_url"] == "https://boxes.example.com"


def test_base_url_takes_first_of_a_proxy_chain(client):
    headers = {"X-Forwarded-Proto": "https, http", "X-Forwarded-Host": "boxes.example.com, inner:8791"}
    assert client.get("/api/config", headers=headers).json()["base_url"] == "https://boxes.example.com"


def test_base_url_falls_back_to_host_header(client):
    assert client.get("/api/config", headers={"Host": "garage.local:8791"}).json()["base_url"] == (
        "http://garage.local:8791"
    )


def test_base_url_setting_wins(client, monkeypatch):
    monkeypatch.setattr(config, "BASE_URL", "https://fixed.example.com")
    headers = {"X-Forwarded-Proto": "http", "X-Forwarded-Host": "other.example.com"}
    assert client.get("/api/config", headers=headers).json()["base_url"] == "https://fixed.example.com"


def test_security_headers(client):
    resp = client.get("/api/config")

    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["Referrer-Policy"] == "same-origin"


# --- boxes --------------------------------------------------------------------


def test_list_boxes_empty(client):
    resp = client.get("/api/boxes")

    assert resp.status_code == 200
    assert resp.json() == []


def test_put_creates_then_updates_box(client):
    created = client.put("/api/boxes/7", json={"name": "  Camping  ", "location": "Shelf A, top"})
    assert created.status_code == 200
    box = created.json()
    assert box["tag_id"] == 7
    assert box["name"] == "Camping"
    assert box["location"] == "Shelf A, top"
    assert box["notes"] == ""
    assert box["updated_by"] == ""
    assert box["items"] == []
    assert box["photos"] == []

    updated = client.put("/api/boxes/7", json={"notes": "heavy"}).json()
    assert updated["id"] == box["id"]
    assert updated["name"] == "Camping"
    assert updated["notes"] == "heavy"

    assert client.get("/api/boxes/7").json() == updated
    assert client.get("/api/boxes").json() == [updated]


def test_put_with_empty_body_claims_the_tag(client):
    assert client.put("/api/boxes/0", json={}).json()["tag_id"] == 0
    assert client.put("/api/boxes/249").json()["tag_id"] == 249
    assert [box["tag_id"] for box in client.get("/api/boxes").json()] == [0, 249]


def test_get_box_404(client):
    resp = client.get("/api/boxes/7")

    assert resp.status_code == 404
    assert isinstance(resp.json()["detail"], str)


def test_delete_box(client):
    client.put("/api/boxes/7", json={"name": "Camping"})
    item = client.post("/api/boxes/7/items", json={"name": "Tent"}).json()

    resp = client.delete("/api/boxes/7")
    assert resp.status_code == 204
    assert resp.content == b""

    assert client.get("/api/boxes/7").status_code == 404
    assert client.delete("/api/boxes/7").status_code == 404
    # the items went with the box
    assert client.patch(f"/api/items/{item['id']}", json={"qty": 2}).status_code == 404


def test_remote_user_flows_into_updated_by(client):
    box = client.put("/api/boxes/7", json={"name": "Camping"}, headers={"Remote-User": "alice"}).json()
    assert box["updated_by"] == "alice"

    client.post("/api/boxes/7/items", json={"name": "Tent"}, headers={"Remote-User": "bob"})
    assert client.get("/api/boxes/7").json()["updated_by"] == "bob"

    client.put("/api/boxes/7", json={"notes": "x"})
    assert client.get("/api/boxes/7").json()["updated_by"] == ""


@pytest.mark.parametrize("tag_id", ["-1", "250", "9999", "abc", "1.5"])
def test_tag_id_out_of_range_is_422(client, tag_id):
    responses = [
        client.get(f"/api/boxes/{tag_id}"),
        client.put(f"/api/boxes/{tag_id}", json={"name": "x"}),
        client.delete(f"/api/boxes/{tag_id}"),
        client.post(f"/api/boxes/{tag_id}/items", json={"name": "x"}),
        _upload(client, f"/api/boxes/{tag_id}/photos"),
        client.get("/api/history", params={"tag_id": tag_id}),
        client.get(f"/api/qr/{tag_id}.svg"),
        client.delete(f"/api/labels/printed/{tag_id}"),
    ]

    for resp in responses:
        assert resp.status_code == 422, resp.request.url
        assert isinstance(resp.json()["detail"], str)
    assert client.get("/api/boxes").json() == []
    assert _history(client) == []


@pytest.mark.parametrize(
    "body",
    [
        {"name": "x" * 121},
        {"location": "x" * 121},
        {"notes": "x" * 2001},
        {"name": 5},
    ],
)
def test_box_field_limits(client, body):
    resp = client.put("/api/boxes/7", json=body)

    assert resp.status_code == 422
    assert isinstance(resp.json()["detail"], str)
    assert client.get("/api/boxes/7").status_code == 404


def test_box_fields_at_the_limit_are_accepted(client):
    body = {"name": "n" * 120, "location": "l" * 120, "notes": "t" * 2000}
    box = client.put("/api/boxes/7", json=body).json()

    assert {key: box[key] for key in body} == body


# --- items --------------------------------------------------------------------


def test_add_item(client):
    box = client.put("/api/boxes/7", json={"name": "Camping"}).json()

    resp = client.post("/api/boxes/7/items", json={"name": "  Sleeping bag ", "qty": 2})

    assert resp.status_code == 201
    item = resp.json()
    assert uuid.UUID(item["id"])
    assert item["box_id"] == box["id"]
    assert item["name"] == "Sleeping bag"
    assert item["qty"] == 2
    assert item["photos"] == []
    assert client.get("/api/boxes/7").json()["items"] == [item]


def test_add_item_qty_defaults_to_one(client):
    assert client.post("/api/boxes/7/items", json={"name": "Tarp"}).json()["qty"] == 1


def test_add_item_creates_the_box(client):
    item = client.post("/api/boxes/12/items", json={"name": "Lantern"}).json()
    box = client.get("/api/boxes/12").json()

    assert box["id"] == item["box_id"]
    assert box["name"] == ""
    assert box["items"] == [item]


def test_items_keep_the_order_they_were_added(client):
    names = [f"item {n:02d}" for n in range(12)]
    for name in names:
        client.post("/api/boxes/7/items", json={"name": name})

    assert [item["name"] for item in client.get("/api/boxes/7").json()["items"]] == names


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"qty": 2},
        {"name": ""},
        {"name": "   "},
        {"name": "x" * 121},
        {"name": "Tarp", "qty": 0},
        {"name": "Tarp", "qty": -1},
        {"name": "Tarp", "qty": 10000},
        {"name": "Tarp", "qty": "many"},
        {"name": "Tarp", "qty": 1.5},
    ],
)
def test_add_item_validation(client, body):
    resp = client.post("/api/boxes/7/items", json=body)

    assert resp.status_code == 422
    assert isinstance(resp.json()["detail"], str)
    # a refused item does not claim the tag
    assert client.get("/api/boxes/7").status_code == 404


def test_item_qty_bounds_are_inclusive(client):
    assert client.post("/api/boxes/7/items", json={"name": "a", "qty": 1}).status_code == 201
    assert client.post("/api/boxes/7/items", json={"name": "b", "qty": 9999}).status_code == 201


def test_patch_item(client):
    item = client.post("/api/boxes/7/items", json={"name": "Lantern", "qty": 1}).json()

    renamed = client.patch(f"/api/items/{item['id']}", json={"name": " Lantern, red "})
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "Lantern, red"
    assert renamed.json()["qty"] == 1

    recounted = client.patch(f"/api/items/{item['id']}", json={"qty": 4}).json()
    assert recounted["name"] == "Lantern, red"
    assert recounted["qty"] == 4
    assert client.get("/api/boxes/7").json()["items"] == [recounted]


def test_patch_item_with_empty_body_returns_it_unchanged(client):
    item = client.post("/api/boxes/7/items", json={"name": "Lantern"}).json()

    resp = client.patch(f"/api/items/{item['id']}", json={})

    assert resp.status_code == 200
    assert resp.json() == item


@pytest.mark.parametrize("body", [{"name": ""}, {"name": "  "}, {"name": "x" * 121}, {"qty": 0}, {"qty": 10000}])
def test_patch_item_validation(client, body):
    item = client.post("/api/boxes/7/items", json={"name": "Lantern", "qty": 3}).json()

    resp = client.patch(f"/api/items/{item['id']}", json=body)

    assert resp.status_code == 422
    assert client.get("/api/boxes/7").json()["items"] == [item]


def test_patch_item_404(client):
    assert client.patch(f"/api/items/{uuid.uuid4()}", json={"qty": 2}).status_code == 404
    assert client.patch("/api/items/not-a-uuid", json={"qty": 2}).status_code == 404


def test_delete_item(client):
    keep = client.post("/api/boxes/7/items", json={"name": "Lantern"}).json()
    drop = client.post("/api/boxes/7/items", json={"name": "Tarp"}).json()

    resp = client.delete(f"/api/items/{drop['id']}")
    assert resp.status_code == 204
    assert resp.content == b""

    assert client.delete(f"/api/items/{drop['id']}").status_code == 404
    assert client.delete("/api/items/not-a-uuid").status_code == 404
    assert client.get("/api/boxes/7").json()["items"] == [keep]


# --- same-origin check --------------------------------------------------------


def test_cross_origin_writes_are_403(client):
    item = client.post("/api/boxes/7/items", json={"name": "Lantern"}).json()
    photo = _upload(client, "/api/boxes/7/photos").json()
    before = _history(client)
    evil = {"Origin": "https://evil.example.com"}

    responses = [
        client.put("/api/boxes/7", json={"name": "taken"}, headers=evil),
        client.delete("/api/boxes/7", headers=evil),
        client.post("/api/boxes/7/items", json={"name": "planted"}, headers=evil),
        client.patch(f"/api/items/{item['id']}", json={"qty": 9}, headers=evil),
        client.delete(f"/api/items/{item['id']}", headers=evil),
        _upload(client, "/api/boxes/7/photos", **evil),
        _upload(client, f"/api/items/{item['id']}/photos", **evil),
        client.delete(f"/api/photos/{photo['id']}", headers=evil),
        client.delete(f"/api/photos/{photo['id']}", params={"erase": 1}, headers=evil),
    ]

    for resp in responses:
        assert resp.status_code == 403, resp.request.method
        assert resp.json() == {"detail": "cross-origin request rejected"}
    box = client.get("/api/boxes/7").json()
    assert box["name"] == ""
    assert box["items"] == [item]
    assert box["photos"] == [photo]
    assert _history(client) == before


def test_same_origin_writes_are_allowed(client):
    resp = client.put("/api/boxes/7", json={"name": "Camping"}, headers={"Origin": "http://testserver"})
    assert resp.status_code == 200


def test_origin_is_checked_against_the_forwarded_host(client):
    proxied = {"X-Forwarded-Host": "boxes.example.com", "X-Forwarded-Proto": "https"}

    ok = client.put("/api/boxes/7", json={}, headers={**proxied, "Origin": "https://boxes.example.com"})
    assert ok.status_code == 200

    refused = client.put("/api/boxes/7", json={}, headers={**proxied, "Origin": "http://testserver"})
    assert refused.status_code == 403


def test_cross_origin_reads_are_not_blocked(client):
    assert client.get("/api/boxes", headers={"Origin": "https://evil.example.com"}).status_code == 200


# --- store failures -----------------------------------------------------------


class BrokenStore(SqliteStore):
    def list_boxes(self):
        raise StoreError(500, "disk on fire")


def test_store_failure_is_502(tmp_path):
    stores.set_store(BrokenStore(tmp_path / "scannage.db"))
    try:
        with TestClient(main.app) as client:
            resp = client.get("/api/boxes")
    finally:
        stores.set_store(None)

    assert resp.status_code == 502
    assert resp.json() == {"detail": "store unavailable"}


# --- qr -----------------------------------------------------------------------


def test_qr_svg(client):
    resp = client.get("/api/qr/3.svg")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/svg+xml")
    assert resp.headers["cache-control"] == "no-store"

    root = ET.fromstring(resp.content)
    assert root.tag == f"{SVG_NS}svg"
    assert root.get("viewBox")
    assert root.findall(f"{SVG_NS}path") or root.findall(f"{SVG_NS}rect")


def test_qr_quiet_zone_is_four_modules(client):
    root = ET.fromstring(client.get("/api/qr/3.svg").content)
    size = int(root.get("viewBox").split()[2])

    # a QR symbol is 17 + 4 * version modules wide, plus the quiet zone on both sides
    assert (size - 2 * 4 - 17) % 4 == 0


def test_qr_holds_the_box_url(client):
    assert client.get("/api/qr/3.svg").content == qr.svg("http://testserver/b/3")
    assert client.get("/api/qr/3.svg").content != qr.svg("http://testserver/b/4")

    proxied = {"X-Forwarded-Proto": "https", "X-Forwarded-Host": "boxes.example.com"}
    assert client.get("/api/qr/249.svg", headers=proxied).content == qr.svg("https://boxes.example.com/b/249")


# --- printed labels -----------------------------------------------------------

# the order labels are handed out in: from 1 upward, and tag 0 last
HANDED_OUT = [*range(1, 250), 0]


def _labels(client) -> dict:
    resp = client.get("/api/labels")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _print(client, tag_ids: list, **headers) -> dict:
    resp = client.post("/api/labels/printed", json={"tag_ids": tag_ids}, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_labels_with_nothing_printed_and_no_boxes(client):
    assert _labels(client) == {"tag_count": 250, "printed": [], "in_use": [], "next": HANDED_OUT}


def test_labels_on_a_new_database_start_after_the_samples(fresh_store):
    with TestClient(main.app) as client:
        labels = _labels(client)

    assert labels["printed"] == []
    assert labels["in_use"] == [1, 2, 3, 4, 5, 6]
    assert labels["next"] == [*range(7, 250), 0]


def test_record_a_print(client):
    recorded = _print(client, [5, 3, 4])

    assert set(recorded) == {"tag_count", "printed", "in_use", "next"}
    assert [row["tag_id"] for row in recorded["printed"]] == [3, 4, 5]
    assert all(set(row) == PRINTED_KEYS for row in recorded["printed"])
    for row in recorded["printed"]:
        assert row["times"] == 1
        assert row["printed_by"] == ""
        assert row["first_printed_at"] == row["last_printed_at"]
        assert datetime.fromisoformat(row["first_printed_at"]).utcoffset() == timedelta(0)
    assert recorded["in_use"] == []
    assert recorded["next"] == [1, 2, *range(6, 250), 0]
    # what the write answered is what a read answers
    assert _labels(client) == recorded


def test_a_reprint_counts_one_more_and_keeps_the_first_time(client):
    first = {row["tag_id"]: row for row in _print(client, [3, 4])["printed"]}

    again = {row["tag_id"]: row for row in _print(client, [4, 5])["printed"]}

    assert again[3] == first[3]
    assert again[4]["times"] == 2
    assert again[4]["first_printed_at"] == first[4]["first_printed_at"]
    assert again[4]["last_printed_at"] > first[4]["last_printed_at"]
    assert again[5]["times"] == 1
    assert again[5]["first_printed_at"] == again[5]["last_printed_at"] == again[4]["last_printed_at"]


def test_a_tag_repeated_in_one_print_counts_once(client):
    printed = _print(client, [7, 7, 8, 7])["printed"]
    assert [(row["tag_id"], row["times"]) for row in printed] == [(7, 1), (8, 1)]

    printed = _print(client, [8, 8])["printed"]
    assert [(row["tag_id"], row["times"]) for row in printed] == [(7, 1), (8, 2)]


def test_who_printed_is_the_remote_user_of_the_latest_print(client):
    printed = _print(client, [3, 4], **{"Remote-User": " alice "})["printed"]
    assert [row["printed_by"] for row in printed] == ["alice", "alice"]

    printed = _print(client, [4], **{"Remote-User": "bob"})["printed"]
    assert [row["printed_by"] for row in printed] == ["alice", "bob"]

    printed = _print(client, [3])["printed"]
    assert [row["printed_by"] for row in printed] == ["", "bob"]


def test_forget_a_print(client):
    stays = _print(client, [3, 4])["printed"][0]

    resp = client.delete("/api/labels/printed/4")
    assert resp.status_code == 204
    assert resp.content == b""

    labels = _labels(client)
    assert labels["printed"] == [stays]
    assert labels["next"] == [1, 2, *range(4, 250), 0]

    again = client.delete("/api/labels/printed/4")
    assert again.status_code == 404
    assert isinstance(again.json()["detail"], str)
    assert _labels(client) == labels


def test_forget_a_print_that_never_was_is_404(client):
    client.put("/api/boxes/7", json={"name": "Camping"})

    # in use is not printed
    for tag_id in (0, 7, 249):
        assert client.delete(f"/api/labels/printed/{tag_id}").status_code == 404


def test_a_forgotten_tag_is_printed_again_as_new(client):
    _print(client, [4])
    _print(client, [4])
    client.delete("/api/labels/printed/4")

    (row,) = _print(client, [4], **{"Remote-User": "alice"})["printed"]

    assert (row["times"], row["printed_by"]) == (1, "alice")
    assert row["first_printed_at"] == row["last_printed_at"]


def test_next_leaves_out_what_is_printed_and_what_is_in_use(client):
    for tag_id in (2, 9, 1):
        client.put(f"/api/boxes/{tag_id}", json={})
    client.post("/api/boxes/4/items", json={"name": "Tent"})
    _print(client, [3, 9, 11])

    labels = _labels(client)

    assert labels["in_use"] == [1, 2, 4, 9]
    # printed and in use at once is both
    assert [row["tag_id"] for row in labels["printed"]] == [3, 9, 11]
    assert labels["next"] == [5, 6, 7, 8, 10, *range(12, 250), 0]


def test_next_goes_from_1_upward_with_tag_0_last(client):
    assert _labels(client)["next"][:3] == [1, 2, 3]
    assert _labels(client)["next"][-2:] == [249, 0]

    _print(client, [1, 249])
    assert _labels(client)["next"] == [*range(2, 249), 0]

    _print(client, [0])
    assert _labels(client)["next"] == [*range(2, 249)]

    client.delete("/api/labels/printed/0")
    client.put("/api/boxes/0", json={})
    labels = _labels(client)
    assert labels["in_use"] == [0]
    assert labels["next"] == [*range(2, 249)]


def test_every_label_can_be_printed_in_one_go(client):
    labels = _print(client, list(range(250)))

    assert [row["tag_id"] for row in labels["printed"]] == list(range(250))
    assert labels["next"] == []


def test_a_deleted_box_frees_its_tag_but_a_printed_label_stays_printed(client):
    client.put("/api/boxes/3", json={})
    client.put("/api/boxes/4", json={})
    printed = _print(client, [4])["printed"]

    client.delete("/api/boxes/3")
    client.delete("/api/boxes/4")

    labels = _labels(client)
    assert labels["in_use"] == []
    assert labels["printed"] == printed
    assert labels["next"] == [1, 2, 3, *range(5, 250), 0]


@pytest.mark.parametrize(
    "body",
    [
        None,
        {},
        {"tag_ids": None},
        {"tag_ids": []},
        {"tag_ids": 3},
        {"tag_ids": "3"},
        {"tag_ids": {"3": 1}},
        {"tag_ids": list(range(250)) + [0]},
        {"tag_ids": [3] * 251},
        {"tag_ids": [250]},
        {"tag_ids": [3, 9999]},
        {"tag_ids": [-1]},
        {"tag_ids": [3, -9001]},
        {"tag_ids": ["abc"]},
        {"tag_ids": [3, 1.5]},
        {"tag_ids": [3, None]},
        {"tag_ids": [[3]]},
        {"tag_ids": [{"tag_id": 3}]},
    ],
)
def test_record_a_print_validation(client, body):
    resp = client.post("/api/labels/printed", json=body)

    assert resp.status_code == 422
    assert isinstance(resp.json()["detail"], str)
    # one bad id and none of them is recorded
    assert _labels(client)["printed"] == []


def test_negative_tags_are_never_returned(client, store):
    store.record_printed([-9001, 5, -9002], "tester")
    store.upsert_box(-9002, {"name": "Test"}, "tester")
    store.upsert_box(-9003, {"name": "Test"}, "tester")
    client.put("/api/boxes/6", json={})

    labels = _labels(client)

    assert [row["tag_id"] for row in labels["printed"]] == [5]
    assert labels["in_use"] == [6]
    assert labels["next"] == [1, 2, 3, 4, *range(7, 250), 0]
    # nor by the write, which answers with the same shape
    assert _print(client, [4])["printed"] == _labels(client)["printed"]
    assert [row["tag_id"] for row in _labels(client)["printed"]] == [4, 5]
    assert client.post("/api/labels/printed", json={"tag_ids": [-9001]}).status_code == 422
    assert client.delete("/api/labels/printed/-9001").status_code == 422
    # they are still there, for the test that wrote them to remove
    assert [(row["tag_id"], row["times"]) for row in store.list_printed()] == [(-9002, 1), (-9001, 1), (4, 1), (5, 1)]
    assert store.list_tag_ids() == [-9003, -9002, 6]


def test_cross_origin_label_writes_are_403(client):
    client.put("/api/boxes/7", json={})
    before = _print(client, [3])
    evil = {"Origin": "https://evil.example.com"}

    responses = [
        client.post("/api/labels/printed", json={"tag_ids": [3, 4]}, headers=evil),
        client.delete("/api/labels/printed/3", headers=evil),
    ]

    for resp in responses:
        assert resp.status_code == 403, resp.request.method
        assert resp.json() == {"detail": "cross-origin request rejected"}
    assert _labels(client) == before
    assert client.get("/api/labels", headers=evil).status_code == 200


def test_same_origin_label_writes_are_allowed(client):
    same = {"Origin": "http://testserver"}
    proxied = {"X-Forwarded-Host": "boxes.example.com", "Origin": "https://boxes.example.com"}

    assert client.post("/api/labels/printed", json={"tag_ids": [3]}, headers=same).status_code == 200
    assert client.post("/api/labels/printed", json={"tag_ids": [3]}, headers=proxied).status_code == 200
    assert client.delete("/api/labels/printed/3", headers=same).status_code == 204
    assert _labels(client)["printed"] == []


def test_printing_is_not_part_of_history_and_touches_no_box(client):
    box = client.put("/api/boxes/7", json={"name": "Camping"}, headers={"Remote-User": "alice"}).json()
    before = _history(client)

    _print(client, [7, 8], **{"Remote-User": "bob"})
    client.delete("/api/labels/printed/7")

    assert _history(client) == before
    assert client.get("/api/boxes/7").json() == box
    assert client.get("/api/boxes/8").status_code == 404


class ReadsNoBoxes(SqliteStore):
    """Fails on every read that loads what is in a box."""

    def list_boxes(self):
        raise StoreError(500, "the labels read every box")

    def get_box(self, tag_id):
        raise StoreError(500, "the labels read a box")


def test_labels_learn_which_tags_are_in_use_without_reading_the_boxes(tmp_path):
    store = ReadsNoBoxes(tmp_path / "scannage.db")
    store.apply_schema()
    store.set_meta(samples.META_KEY, "set by the test")
    store.upsert_box(7, {"name": "Camping"}, "")
    store.add_photo(store.add_item(7, "Tent", 1, "")["box_id"], None, photos.process(_image()), "")
    stores.set_store(store)
    try:
        with TestClient(main.app) as client:
            labels = _print(client, [8])
            assert client.delete("/api/labels/printed/8").status_code == 204
            assert _labels(client)["in_use"] == [7]
    finally:
        stores.set_store(None)

    assert labels["in_use"] == [7]
    assert [row["tag_id"] for row in labels["printed"]] == [8]
    assert labels["next"] == [1, 2, 3, 4, 5, 6, *range(9, 250), 0]


class PrintedFails(SqliteStore):
    def list_printed(self):
        raise StoreError(500, "disk on fire")


def test_a_store_failure_on_the_labels_is_502(tmp_path):
    store = PrintedFails(tmp_path / "scannage.db")
    store.apply_schema()
    store.set_meta(samples.META_KEY, "set by the test")
    stores.set_store(store)
    try:
        with TestClient(main.app) as client:
            resp = client.get("/api/labels")
    finally:
        stores.set_store(None)

    assert resp.status_code == 502
    assert resp.json() == {"detail": "store unavailable"}


# --- pages --------------------------------------------------------------------


@pytest.fixture()
def static_dir(tmp_path, monkeypatch):
    """A throwaway static directory, so the tests never depend on the real frontend."""
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text('<script src="/static/app.js?v=__ASSET_VERSION__"></script> __ASSET_VERSION__')
    (static / "app.js").write_text("// app")
    monkeypatch.setattr(main, "STATIC_DIR", static)
    monkeypatch.setattr(main, "_asset_version", "")
    return static


@pytest.mark.parametrize("path", ["/", "/b/7"])
def test_index_pages_get_the_asset_version(client, static_dir, path):
    resp = client.get(path)

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert resp.headers["cache-control"] == "no-store"
    version = main.asset_version()
    assert len(version) == 12
    assert resp.text == f'<script src="/static/app.js?v={version}"></script> {version}'


def test_asset_version_follows_the_static_files(static_dir):
    before = main._hash_static()
    assert main._hash_static() == before

    (static_dir / "app.js").write_text("// app, changed")
    assert main._hash_static() != before


def test_labels_page(client, static_dir):
    (static_dir / "labels.html").write_text("<p>labels __ASSET_VERSION__</p>")

    resp = client.get("/labels")

    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"
    assert resp.text == f"<p>labels {main.asset_version()}</p>"


def test_missing_page_is_a_plain_503(client, static_dir):
    resp = client.get("/labels")

    assert resp.status_code == 503
    assert resp.headers["content-type"].startswith("text/plain")
    assert "labels.html" in resp.text


# --- sample boxes -------------------------------------------------------------


def test_an_initialised_database_starts_empty(client):
    assert client.get("/api/boxes").json() == []


def test_a_new_database_gets_the_samples_once_and_never_again(fresh_store):
    with TestClient(main.app) as client:
        boxes = client.get("/api/boxes").json()
        assert [box["tag_id"] for box in boxes] == [1, 2, 3, 4, 5, 6]
        assert [box["name"] for box in boxes] == [
            "Camping",
            "Christmas",
            "Paint supplies",
            "Power tools",
            "Car care",
            "Cables",
        ]
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
        assert fresh_store.get_meta(samples.META_KEY) is not None
        assert {entry["actor"] for entry in _history(client, limit=200)} == {"samples"}

    # a second boot with the samples still there adds nothing
    with TestClient(main.app) as client:
        assert len(client.get("/api/boxes").json()) == 6
        for tag_id in range(1, 7):
            assert client.delete(f"/api/boxes/{tag_id}").status_code == 204
        assert client.get("/api/boxes").json() == []

    # and a boot on the emptied database does not bring them back
    with TestClient(main.app) as client:
        assert client.get("/api/boxes").json() == []


def test_samples_never_added_to_an_inventory_in_use(fresh_store):
    fresh_store.apply_schema()
    fresh_store.upsert_box(40, {"name": "Mine"}, "")

    with TestClient(main.app) as client:
        assert [box["tag_id"] for box in client.get("/api/boxes").json()] == [40]
        assert fresh_store.get_meta(samples.META_KEY) is not None
        client.delete("/api/boxes/40")

    with TestClient(main.app) as client:
        assert client.get("/api/boxes").json() == []


def test_nothing_in_the_api_brings_the_samples_back(client):
    paths = {route.path for route in main.app.routes}

    assert not [path for path in paths if "sample" in path or "seed" in path]
    for method in ("get", "post", "put", "delete"):
        assert getattr(client, method)("/api/samples").status_code in (404, 405)
    assert client.get("/api/boxes").json() == []


# --- history ------------------------------------------------------------------


def test_history_starts_empty(client):
    assert _history(client) == []


def test_creating_a_box_is_recorded(client, clock):
    box = client.put(
        "/api/boxes/7", json={"name": "Camping", "location": "Shelf A, top"}, headers={"Remote-User": "alice"}
    ).json()

    entries = _history(client)

    assert len(entries) == 1
    assert set(entries[0]) == HISTORY_KEYS
    assert uuid.UUID(entries[0]["id"])
    assert entries[0] == {
        "id": entries[0]["id"],
        "at": "2026-01-01T00:00:01.000000+00:00",
        "actor": "alice",
        "action": "box_created",
        "tag_id": 7,
        "box_id": box["id"],
        "box_name": "Camping",
        "item_id": None,
        "item_name": "",
        # notes was left empty, and an empty field is not a change
        "changes": {"name": [None, "Camping"], "location": [None, "Shelf A, top"]},
        "photos": [],
    }


def test_claiming_a_tag_with_nothing_in_it_is_still_recorded(client):
    client.put("/api/boxes/7")

    entries = _history(client)

    assert [(entry["action"], entry["box_name"], entry["changes"]) for entry in entries] == [("box_created", "", {})]


def test_updating_a_box_records_only_what_changed(client):
    client.put("/api/boxes/7", json={"name": "Camping", "location": "Shelf A", "notes": "heavy"})

    client.put(
        "/api/boxes/7", json={"name": "Camping", "location": "Shelf B", "notes": ""}, headers={"Remote-User": "bob"}
    )

    newest = _history(client)[0]
    assert newest["action"] == "box_updated"
    assert newest["actor"] == "bob"
    assert newest["box_name"] == "Camping"
    assert newest["item_id"] is None
    assert newest["changes"] == {"location": ["Shelf A", "Shelf B"], "notes": ["heavy", ""]}


def test_a_write_that_changes_nothing_records_nothing(client):
    client.put("/api/boxes/7", json={"name": "Camping", "notes": "heavy"})
    item = client.post("/api/boxes/7/items", json={"name": "Tent", "qty": 2}).json()
    before = _history(client)
    assert [entry["action"] for entry in before] == ["item_added", "box_created"]

    assert client.put("/api/boxes/7", json={"name": "Camping", "notes": "heavy"}).status_code == 200
    assert client.put("/api/boxes/7", json={"name": "  Camping "}).status_code == 200
    assert client.put("/api/boxes/7", json={}).status_code == 200
    assert client.put("/api/boxes/7").status_code == 200
    assert client.patch(f"/api/items/{item['id']}", json={"name": "Tent", "qty": 2}).status_code == 200
    assert client.patch(f"/api/items/{item['id']}", json={}).status_code == 200

    assert _history(client) == before


def test_refused_writes_record_nothing(client):
    item = client.post("/api/boxes/7/items", json={"name": "Tent"}).json()
    before = _history(client)

    assert client.put("/api/boxes/7", json={"name": "x" * 121}).status_code == 422
    assert client.post("/api/boxes/7/items", json={"name": ""}).status_code == 422
    assert client.patch(f"/api/items/{item['id']}", json={"qty": 0}).status_code == 422
    assert client.patch(f"/api/items/{uuid.uuid4()}", json={"qty": 2}).status_code == 404
    assert client.delete(f"/api/items/{uuid.uuid4()}").status_code == 404
    assert client.delete("/api/boxes/8").status_code == 404
    assert client.delete(f"/api/photos/{uuid.uuid4()}").status_code == 404
    assert client.delete(f"/api/photos/{uuid.uuid4()}", params={"erase": 1}).status_code == 404
    assert _upload(client, "/api/boxes/7/photos", b"not an image").status_code == 422

    assert _history(client) == before


def test_adding_an_item_is_recorded(client):
    box = client.put("/api/boxes/7", json={"name": "Camping"}).json()

    item = client.post("/api/boxes/7/items", json={"name": "Tent", "qty": 2}, headers={"Remote-User": "bob"}).json()

    newest = _history(client)[0]
    assert newest["action"] == "item_added"
    assert newest["actor"] == "bob"
    assert (newest["tag_id"], newest["box_id"], newest["box_name"]) == (7, box["id"], "Camping")
    assert (newest["item_id"], newest["item_name"]) == (item["id"], "Tent")
    assert newest["changes"] == {"name": [None, "Tent"], "qty": [None, 2]}


def test_an_item_that_brings_its_box_records_both(client):
    item = client.post("/api/boxes/12/items", json={"name": "Lantern"}).json()

    added, created = _history(client)

    assert (created["action"], created["box_id"], created["item_id"]) == ("box_created", item["box_id"], None)
    assert created["changes"] == {}
    assert (added["action"], added["item_id"], added["tag_id"]) == ("item_added", item["id"], 12)
    assert added["at"] > created["at"]


def test_updating_an_item_is_recorded(client):
    item = client.post("/api/boxes/7/items", json={"name": "Lantern", "qty": 1}).json()

    client.patch(f"/api/items/{item['id']}", json={"name": "Lantern, red", "qty": 1})

    newest = _history(client)[0]
    assert newest["action"] == "item_updated"
    assert (newest["item_id"], newest["item_name"]) == (item["id"], "Lantern, red")
    assert newest["changes"] == {"name": ["Lantern", "Lantern, red"]}


def test_removing_an_item_is_recorded(client):
    client.put("/api/boxes/7", json={"name": "Camping"})
    item = client.post("/api/boxes/7/items", json={"name": "Tarp", "qty": 3}).json()

    client.delete(f"/api/items/{item['id']}", headers={"Remote-User": "carol"})

    newest = _history(client)[0]
    assert newest["action"] == "item_removed"
    assert newest["actor"] == "carol"
    assert (newest["item_id"], newest["item_name"], newest["box_name"]) == (item["id"], "Tarp", "Camping")
    assert newest["changes"] == {"name": ["Tarp", None], "qty": [3, None]}


def test_deleting_a_box_is_recorded_with_what_was_in_it(client):
    box = client.put("/api/boxes/7", json={"name": "Camping", "location": "Shelf A"}).json()
    client.post("/api/boxes/7/items", json={"name": "Tent"})
    client.post("/api/boxes/7/items", json={"name": "Stakes", "qty": 12})

    client.delete("/api/boxes/7", headers={"Remote-User": "alice"})

    newest = _history(client)[0]
    assert newest["action"] == "box_deleted"
    assert newest["actor"] == "alice"
    assert (newest["tag_id"], newest["box_id"], newest["box_name"]) == (7, box["id"], "Camping")
    assert newest["changes"] == {
        "name": ["Camping", None],
        "location": ["Shelf A", None],
        "items": [[{"name": "Tent", "qty": 1}, {"name": "Stakes", "qty": 12}], None],
    }


def test_deleting_an_empty_box_still_says_it_held_nothing(client):
    client.put("/api/boxes/7")

    client.delete("/api/boxes/7")

    assert _history(client)[0]["changes"] == {"items": [[], None]}


def test_history_stays_after_the_box_is_deleted(client):
    client.put("/api/boxes/7", json={"name": "Camping"})
    item = client.post("/api/boxes/7/items", json={"name": "Tent"}).json()
    client.patch(f"/api/items/{item['id']}", json={"qty": 2})
    client.delete("/api/boxes/7")
    assert client.get("/api/boxes").json() == []

    actions = [entry["action"] for entry in _history(client, tag_id=7)]

    assert actions == ["box_deleted", "item_updated", "item_added", "box_created"]

    # the tag is free again, and the next box on it adds to the same history
    again = client.put("/api/boxes/7", json={"name": "Tools"}).json()
    entries = _history(client, tag_id=7)
    assert [entry["action"] for entry in entries] == ["box_created"] + actions
    assert entries[0]["box_id"] == again["id"]
    assert entries[1]["box_id"] != again["id"]


# --- history: merging ---------------------------------------------------------


def test_updates_in_quick_succession_become_one_entry(client, clock):
    client.put("/api/boxes/7", json={"name": "C"})
    for name in ("Ca", "Cam", "Camping"):
        client.put("/api/boxes/7", json={"name": name})
    client.put("/api/boxes/7", json={"notes": "heavy"})

    updated, created = _history(client)

    assert created["action"] == "box_created"
    assert created["changes"] == {"name": [None, "C"]}
    assert created["at"] == "2026-01-01T00:00:01.000000+00:00"
    assert updated["action"] == "box_updated"
    # the first before, the latest after, and the time of the last write
    assert updated["changes"] == {"name": ["C", "Camping"], "notes": ["", "heavy"]}
    assert updated["box_name"] == "Camping"
    assert updated["at"] == "2026-01-01T00:00:05.000000+00:00"


def test_item_updates_merge_too(client, clock):
    item = client.post("/api/boxes/7/items", json={"name": "Tent"}).json()
    client.patch(f"/api/items/{item['id']}", json={"qty": 2})
    client.patch(f"/api/items/{item['id']}", json={"qty": 3})
    client.patch(f"/api/items/{item['id']}", json={"name": "Tent, 4 person"})

    newest = _history(client)[0]

    assert len(_history(client)) == 3
    assert newest["action"] == "item_updated"
    assert newest["item_name"] == "Tent, 4 person"
    assert newest["changes"] == {"qty": [1, 3], "name": ["Tent", "Tent, 4 person"]}


def test_merging_stops_at_120_seconds(client, clock):
    client.put("/api/boxes/7", json={"name": "Camping"})
    client.put("/api/boxes/7", json={"name": "Camping gear"})
    first = _history(client)[0]

    # the clock adds a second itself, so this entry lands 119 seconds after the last
    clock.advance(118)
    client.put("/api/boxes/7", json={"name": "Camping kit"})
    merged = _history(client)
    assert len(merged) == 2
    assert merged[0]["id"] == first["id"]
    assert merged[0]["changes"] == {"name": ["Camping", "Camping kit"]}

    # and this one exactly 120 seconds after, which is no longer less than 120
    clock.advance(119)
    client.put("/api/boxes/7", json={"name": "Camping things"})
    entries = _history(client)
    assert len(entries) == 3
    assert entries[0]["changes"] == {"name": ["Camping kit", "Camping things"]}
    assert entries[1] == merged[0]
    at = [datetime.fromisoformat(entry["at"]) for entry in entries[:2]]
    assert at[0] - at[1] == timedelta(seconds=120)


def test_merging_needs_the_same_actor(client, clock):
    client.put("/api/boxes/7", json={"name": "Camping"}, headers={"Remote-User": "alice"})
    client.put("/api/boxes/7", json={"name": "Camping gear"}, headers={"Remote-User": "alice"})
    client.put("/api/boxes/7", json={"name": "Camping kit"}, headers={"Remote-User": "bob"})

    entries = _history(client)

    assert [(entry["action"], entry["actor"]) for entry in entries] == [
        ("box_updated", "bob"),
        ("box_updated", "alice"),
        ("box_created", "alice"),
    ]
    assert entries[0]["changes"] == {"name": ["Camping gear", "Camping kit"]}


def test_merging_needs_the_same_item(client, clock):
    tent = client.post("/api/boxes/7/items", json={"name": "Tent"}).json()
    tarp = client.post("/api/boxes/7/items", json={"name": "Tarp"}).json()
    client.patch(f"/api/items/{tent['id']}", json={"qty": 2})
    client.patch(f"/api/items/{tarp['id']}", json={"qty": 5})
    client.patch(f"/api/items/{tent['id']}", json={"qty": 3})

    updates = [entry for entry in _history(client) if entry["action"] == "item_updated"]

    assert [(entry["item_name"], entry["changes"]) for entry in updates] == [
        ("Tent", {"qty": [2, 3]}),
        ("Tarp", {"qty": [1, 5]}),
        ("Tent", {"qty": [1, 2]}),
    ]


def test_merging_needs_the_same_action_and_box(client, clock):
    item = client.post("/api/boxes/7/items", json={"name": "Tent"}).json()
    client.put("/api/boxes/7", json={"name": "Camping"})
    client.patch(f"/api/items/{item['id']}", json={"qty": 2})
    client.put("/api/boxes/7", json={"name": "Camping gear"})
    client.put("/api/boxes/8", json={"name": "Tools"})
    client.put("/api/boxes/8", json={"name": "Power tools"})
    client.put("/api/boxes/7", json={"name": "Camping kit"})

    assert [(entry["action"], entry["changes"]) for entry in _history(client, tag_id=7)] == [
        # box 8 was written in between, which does not come between two writes to box 7
        ("box_updated", {"name": ["Camping", "Camping kit"]}),
        ("item_updated", {"qty": [1, 2]}),
        ("box_updated", {"name": ["", "Camping"]}),
        ("item_added", {"name": [None, "Tent"], "qty": [None, 1]}),
        ("box_created", {}),
    ]
    assert [(entry["action"], entry["changes"]) for entry in _history(client, tag_id=8)] == [
        ("box_updated", {"name": ["Tools", "Power tools"]}),
        ("box_created", {"name": [None, "Tools"]}),
    ]


def test_an_update_never_merges_into_the_entry_that_created_the_box(client, clock):
    client.put("/api/boxes/7", json={"name": "Camping"})
    client.put("/api/boxes/7", json={"name": "Camping gear"})

    assert [entry["action"] for entry in _history(client)] == ["box_updated", "box_created"]


def test_a_field_back_at_its_first_value_is_dropped(client, clock):
    client.put("/api/boxes/7", json={"name": "Camping", "notes": "heavy"})
    client.put("/api/boxes/7", json={"name": "Camping gear", "notes": "light"})
    client.put("/api/boxes/7", json={"name": "Camping"})

    updated = _history(client)[0]

    assert updated["action"] == "box_updated"
    assert updated["changes"] == {"notes": ["heavy", "light"]}
    assert updated["box_name"] == "Camping"
    assert updated["at"] == "2026-01-01T00:00:03.000000+00:00"


def test_an_entry_left_with_no_changes_is_removed(client, clock):
    item = client.post("/api/boxes/7/items", json={"name": "Tent", "qty": 1}).json()
    before = _history(client)

    client.patch(f"/api/items/{item['id']}", json={"qty": 2})
    assert len(_history(client)) == len(before) + 1
    client.patch(f"/api/items/{item['id']}", json={"qty": 1})

    assert _history(client) == before


def test_a_change_undone_after_the_window_is_its_own_entry(client, clock):
    client.put("/api/boxes/7", json={"name": "Camping"})
    client.put("/api/boxes/7", json={"name": "Camping gear"})
    clock.advance(300)
    client.put("/api/boxes/7", json={"name": "Camping"})

    assert [entry["changes"] for entry in _history(client)[:2]] == [
        {"name": ["Camping gear", "Camping"]},
        {"name": ["Camping", "Camping gear"]},
    ]


def test_creating_and_removing_never_merge(client, clock):
    for name in ("Tent", "Tent"):
        item = client.post("/api/boxes/7/items", json={"name": name}).json()
        client.delete(f"/api/items/{item['id']}")

    assert [entry["action"] for entry in _history(client)] == [
        "item_removed",
        "item_added",
        "item_removed",
        "item_added",
        "box_created",
    ]


# --- history: reading ---------------------------------------------------------


def test_history_of_negative_tags_is_never_returned(client, store):
    client.put("/api/boxes/7", json={"name": "Camping"})
    history.put_box(store, -9001, {"name": "Test"}, "tester")
    history.put_box(store, -9001, {"name": "Test, renamed"}, "tester")
    history.delete_box(store, -9001, "tester")

    assert [entry["tag_id"] for entry in _history(client, limit=200)] == [7]
    hidden = store.list_history(-9001, 50, None, include_tests=True)
    assert [entry["action"] for entry in hidden] == ["box_deleted", "box_updated", "box_created"]
    assert client.get("/api/history", params={"tag_id": -9001}).status_code == 422


def test_history_pages_with_before(client, clock):
    for number in range(7):
        client.put(f"/api/boxes/{number}", json={"name": f"box {number}"})

    first = _history(client, limit=3)
    second = _history(client, limit=3, before=first[-1]["at"])
    third = _history(client, limit=3, before=second[-1]["at"])

    assert [entry["tag_id"] for entry in first] == [6, 5, 4]
    assert [entry["tag_id"] for entry in second] == [3, 2, 1]
    assert [entry["tag_id"] for entry in third] == [0]
    assert _history(client, limit=3, before=third[-1]["at"]) == []


def test_history_before_takes_the_timestamp_in_any_form(client, clock):
    client.put("/api/boxes/1", json={"name": "one"})
    client.put("/api/boxes/2", json={"name": "two"})
    newest = _history(client)[0]["at"]
    assert newest == "2026-01-01T00:00:02.000000+00:00"

    same_moment = [
        newest,
        "2026-01-01T00:00:02+00:00",
        "2026-01-01T00:00:02Z",
        "2026-01-01T01:00:02+01:00",
        "2026-01-01T00:00:02",
    ]
    for value in same_moment:
        assert [entry["tag_id"] for entry in _history(client, before=value)] == [1], value
    # a plus sign that was not encoded arrives as a space
    resp = client.get("/api/history?before=2026-01-01T00:00:02.000000 00:00")
    assert [entry["tag_id"] for entry in resp.json()] == [1]


def test_history_filters_by_tag(client):
    client.put("/api/boxes/7", json={"name": "Camping"})
    client.put("/api/boxes/8", json={"name": "Tools"})
    client.post("/api/boxes/7/items", json={"name": "Tent"})

    assert [entry["action"] for entry in _history(client, tag_id=7)] == ["item_added", "box_created"]
    assert [entry["action"] for entry in _history(client, tag_id=8)] == ["box_created"]
    assert _history(client, tag_id=9) == []


def test_history_limit_defaults_to_50(client):
    for number in range(60):
        client.post("/api/boxes/7/items", json={"name": f"item {number}"})

    assert len(_history(client)) == 50
    assert len(_history(client, limit=200)) == 61
    assert len(_history(client, limit=1)) == 1


@pytest.mark.parametrize(
    "params",
    [{"limit": 0}, {"limit": 201}, {"limit": "many"}, {"tag_id": 250}, {"tag_id": "x"}, {"before": "yesterday"}],
)
def test_history_query_validation(client, params):
    resp = client.get("/api/history", params=params)

    assert resp.status_code == 422
    assert isinstance(resp.json()["detail"], str)


def test_history_cannot_be_written_through_the_api(client):
    client.put("/api/boxes/7", json={"name": "Camping"})
    entry = _history(client)[0]

    assert client.post("/api/history", json=entry).status_code == 405
    assert client.delete("/api/history").status_code == 405
    for method in ("put", "patch", "delete"):
        assert getattr(client, method)(f"/api/history/{entry['id']}").status_code in (404, 405)
    assert _history(client) == [entry]


class HistoryFails(SqliteStore):
    def add_history(self, entry):
        raise StoreError(500, "history is on fire")

    def list_history(self, tag_id, limit, before, include_tests=False):
        raise StoreError(500, "history is on fire")


class ReadsBeforeFail(SqliteStore):
    failing = False

    def get_box(self, tag_id):
        if self.failing:
            raise StoreError(500, "cannot read")
        return super().get_box(tag_id)


def test_a_history_failure_does_not_fail_the_write(tmp_path):
    store = HistoryFails(tmp_path / "scannage.db")
    store.apply_schema()
    store.set_meta(samples.META_KEY, "set by the test")
    stores.set_store(store)
    try:
        with TestClient(main.app) as client:
            created = client.put("/api/boxes/7", json={"name": "Camping"})
            renamed = client.put("/api/boxes/7", json={"name": "Camping gear"})
            added = client.post("/api/boxes/7/items", json={"name": "Tent"})
            changed = client.patch(f"/api/items/{added.json()['id']}", json={"qty": 2})
            photo = _upload(client, "/api/boxes/7/photos")
            unphoto = client.delete(f"/api/photos/{photo.json()['id']}")
            erased = client.delete(f"/api/photos/{photo.json()['id']}", params={"erase": 1})
            gone = client.get(f"/api/photos/{photo.json()['id']}")
            removed = client.delete(f"/api/items/{added.json()['id']}")
            box = client.get("/api/boxes/7").json()
            deleted = client.delete("/api/boxes/7")
    finally:
        stores.set_store(None)

    responses = (created, renamed, added, changed, photo, unphoto, erased, gone, removed, deleted)
    assert [resp.status_code for resp in responses] == [200, 200, 201, 200, 201, 204, 204, 404, 204, 204]
    assert box["name"] == "Camping gear"
    assert box["items"] == []


def test_a_failed_read_before_the_write_does_not_fail_the_write(tmp_path):
    store = ReadsBeforeFail(tmp_path / "scannage.db")
    store.apply_schema()
    store.set_meta(samples.META_KEY, "set by the test")

    history.put_box(store, 7, {"name": "Camping"}, "")
    store.failing = True
    box = history.put_box(store, 7, {"name": "Camping gear"}, "")
    store.failing = False

    assert box["name"] == "Camping gear"
    assert store.get_box(7)["name"] == "Camping gear"
    # what it was before is unknown, so nothing is claimed about the change
    assert [entry["action"] for entry in store.list_history(7, 50, None)] == ["box_created"]


# --- photos -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fmt", "content_type", "mode"),
    [
        ("JPEG", "image/jpeg", "RGB"),
        ("PNG", "image/png", "RGB"),
        ("WEBP", "image/webp", "RGB"),
        ("PNG", "image/png", "L"),
        ("PNG", "image/png", "P"),
    ],
)
def test_upload_a_box_photo(client, fmt, content_type, mode):
    box = client.put("/api/boxes/7", json={"name": "Camping"}).json()

    resp = _upload(client, "/api/boxes/7/photos", _image(fmt, (64, 48), mode), content_type, **{"Remote-User": "alice"})

    assert resp.status_code == 201, resp.text
    photo = resp.json()
    assert set(photo) == PHOTO_KEYS
    assert uuid.UUID(photo["id"])
    assert photo["box_id"] == box["id"]
    assert photo["item_id"] is None
    assert (photo["width"], photo["height"]) == (64, 48)
    assert photo["created_by"] == "alice"
    assert photo["created_at"].endswith("+00:00")

    full = client.get(f"/api/photos/{photo['id']}")
    assert full.status_code == 200
    assert full.headers["content-type"] == "image/jpeg"
    assert full.headers["cache-control"] == PHOTO_CACHE
    assert full.headers["x-content-type-options"] == "nosniff"
    assert photo["size"] == len(full.content)
    stored = _opened(full.content)
    # whatever came in, a JPEG is what is kept
    assert stored.format == "JPEG"
    assert stored.mode == "RGB"
    assert stored.size == (64, 48)


def test_upload_an_item_photo(client):
    item = client.post("/api/boxes/7/items", json={"name": "Tent"}).json()

    resp = _upload(client, f"/api/items/{item['id']}/photos", **{"Remote-User": "bob"})

    assert resp.status_code == 201
    photo = resp.json()
    assert set(photo) == PHOTO_KEYS
    assert (photo["box_id"], photo["item_id"]) == (item["box_id"], item["id"])
    assert photo["created_by"] == "bob"
    assert _opened(client.get(f"/api/photos/{photo['id']}").content).format == "JPEG"


def test_photos_appear_in_box_and_item_shapes(client):
    item = client.post("/api/boxes/7/items", json={"name": "Tent"}).json()
    other = client.post("/api/boxes/7/items", json={"name": "Tarp"}).json()
    on_box = [_upload(client, "/api/boxes/7/photos").json() for _ in range(2)]
    on_item = _upload(client, f"/api/items/{item['id']}/photos").json()

    box = client.get("/api/boxes/7").json()

    # a box lists its own photos, and each item lists its own
    assert box["photos"] == on_box
    assert [entry["photos"] for entry in box["items"]] == [[on_item], []]
    assert client.get("/api/boxes").json() == [box]
    assert client.put("/api/boxes/7", json={"notes": "x"}).json()["photos"] == on_box
    assert client.patch(f"/api/items/{item['id']}", json={"qty": 2}).json()["photos"] == [on_item]
    assert client.patch(f"/api/items/{other['id']}", json={"qty": 2}).json()["photos"] == []
    assert b"data" not in client.get("/api/boxes").content
    assert b"thumb" not in client.get("/api/boxes").content


def test_a_photo_on_an_unclaimed_tag_creates_the_box(client):
    resp = _upload(client, "/api/boxes/12/photos", **{"Remote-User": "alice"})

    assert resp.status_code == 201
    box = client.get("/api/boxes/12").json()
    assert box["name"] == ""
    assert box["updated_by"] == "alice"
    assert box["photos"] == [resp.json()]
    assert [entry["action"] for entry in _history(client)] == ["photo_added", "box_created"]


def test_adding_a_photo_touches_the_box(client):
    box = client.put("/api/boxes/7", json={"name": "Camping"}, headers={"Remote-User": "alice"}).json()

    photo = _upload(client, "/api/boxes/7/photos", **{"Remote-User": "bob"}).json()

    after = client.get("/api/boxes/7").json()
    assert after["updated_by"] == "bob"
    assert after["updated_at"] == photo["created_at"]
    assert after["updated_at"] > box["updated_at"]


def test_the_content_type_header_is_not_what_decides(client):
    png = _image("PNG")

    assert _upload(client, "/api/boxes/7/photos", png, "image/jpeg").status_code == 201
    assert _upload(client, "/api/boxes/7/photos", png, "application/octet-stream").status_code == 201
    assert _upload(client, "/api/boxes/7/photos", b"<svg/>", "image/png").status_code == 422
    assert len(client.get("/api/boxes/7").json()["photos"]) == 2


def test_a_large_photo_is_scaled_to_1600_on_the_long_edge(client):
    wide = _upload(client, "/api/boxes/7/photos", _image("JPEG", (3200, 2400))).json()
    tall = _upload(client, "/api/boxes/7/photos", _image("PNG", (1000, 4000)), "image/png").json()

    assert (wide["width"], wide["height"]) == (1600, 1200)
    assert (tall["width"], tall["height"]) == (400, 1600)
    assert _opened(client.get(f"/api/photos/{wide['id']}").content).size == (1600, 1200)
    assert _opened(client.get(f"/api/photos/{tall['id']}").content).size == (400, 1600)


def test_a_small_photo_is_not_enlarged(client):
    photo = _upload(client, "/api/boxes/7/photos", _image("JPEG", (30, 20))).json()

    assert (photo["width"], photo["height"]) == (30, 20)
    assert _opened(client.get(f"/api/photos/{photo['id']}/thumb").content).size == (30, 20)


def test_thumb_is_at_most_320_on_the_long_edge(client):
    wide = _upload(client, "/api/boxes/7/photos", _image("JPEG", (3200, 2400))).json()
    tall = _upload(client, "/api/boxes/7/photos", _image("WEBP", (500, 1000)), "image/webp").json()

    resp = client.get(f"/api/photos/{wide['id']}/thumb")

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/jpeg"
    assert resp.headers["cache-control"] == PHOTO_CACHE
    thumb = _opened(resp.content)
    assert thumb.format == "JPEG"
    assert thumb.size == (320, 240)
    assert _opened(client.get(f"/api/photos/{tall['id']}/thumb").content).size == (160, 320)
    assert len(resp.content) < len(client.get(f"/api/photos/{wide['id']}").content)


def _photo_with_exif(orientation: int) -> bytes:
    """Red on the left and blue on the right as stored, with a camera's tags, a location among them."""
    image = Image.new("RGB", (80, 40), RED)
    image.paste(BLUE, (40, 0, 80, 40))
    exif = Image.Exif()
    exif[0x0112] = orientation
    exif[0x010F] = "Camera maker"
    exif[0x0110] = "Camera model"
    exif[0x0132] = "2026:05:01 10:00:00"
    gps = exif.get_ifd(0x8825)
    gps[1] = "N"
    gps[2] = (44.0, 58.0, 30.0)
    gps[3] = "W"
    gps[4] = (93.0, 15.0, 45.0)
    out = io.BytesIO()
    image.save(out, format="JPEG", exif=exif, quality=95, comment=b"taken in the garage")
    return out.getvalue()


def test_the_test_photo_really_carries_a_location():
    # otherwise the next test would pass without proving anything
    source = _opened(_photo_with_exif(6))

    assert source.getexif()[0x0112] == 6
    assert source.getexif().get_ifd(0x8825)[2] == (44.0, 58.0, 30.0)
    assert source.info["comment"] == b"taken in the garage"
    assert b"Camera maker" in _photo_with_exif(6)


def test_orientation_is_applied_and_metadata_is_stripped(client):
    raw = _photo_with_exif(6)

    photo = _upload(client, "/api/boxes/7/photos", raw).json()

    # orientation 6 means the camera was turned: the picture is shown a quarter turn clockwise
    assert (photo["width"], photo["height"]) == (40, 80)
    for path in (f"/api/photos/{photo['id']}", f"/api/photos/{photo['id']}/thumb"):
        body = client.get(path).content
        stored = _opened(body)
        assert stored.size == (40, 80)
        # what was the left edge is now the top
        assert _is_close(stored.getpixel((20, 10)), RED)
        assert _is_close(stored.getpixel((20, 70)), BLUE)
        assert len(stored.getexif()) == 0
        assert stored.getexif().get_ifd(0x8825) == {}
        assert set(stored.info) <= {"jfif", "jfif_version", "jfif_unit", "jfif_density"}
        for marker in (b"Exif", b"Camera maker", b"Camera model", b"2026:05:01", b"garage", b"ICC_PROFILE"):
            assert marker not in body, marker


def test_a_photo_that_is_already_upright_is_left_as_it_is(client):
    photo = _upload(client, "/api/boxes/7/photos", _photo_with_exif(1)).json()

    stored = _opened(client.get(f"/api/photos/{photo['id']}").content)

    assert stored.size == (80, 40)
    assert _is_close(stored.getpixel((10, 20)), RED)
    assert _is_close(stored.getpixel((70, 20)), BLUE)
    assert len(stored.getexif()) == 0


def test_png_text_and_transparency_do_not_reach_the_stored_photo(client):
    info = PngInfo()
    info.add_text("Comment", "taken in the garage")
    image = Image.new("RGBA", (40, 40), (0, 0, 0, 0))
    image.paste((*BLUE, 255), (0, 0, 20, 40))
    out = io.BytesIO()
    image.save(out, format="PNG", pnginfo=info)

    photo = _upload(client, "/api/boxes/7/photos", out.getvalue(), "image/png").json()

    body = client.get(f"/api/photos/{photo['id']}").content
    stored = _opened(body)
    assert b"garage" not in body
    assert _is_close(stored.getpixel((5, 20)), BLUE)
    # see-through becomes white, not black
    assert _is_close(stored.getpixel((35, 20)), (255, 255, 255))


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"not an image",
        b'{"name": "Camping"}',
        b"\xff\xd8\xff\xe0 a jpeg that stops here",
        b"\x89PNG\r\n\x1a\n and then nothing",
    ],
)
def test_something_that_is_not_an_image_is_422(client, body):
    resp = _upload(client, "/api/boxes/7/photos", body)

    assert resp.status_code == 422
    assert isinstance(resp.json()["detail"], str)
    # a refused photo does not claim the tag
    assert client.get("/api/boxes/7").status_code == 404


@pytest.mark.parametrize("fmt", ["GIF", "BMP", "TIFF"])
def test_other_image_formats_are_422(client, fmt):
    resp = _upload(client, "/api/boxes/7/photos", _image(fmt), "image/jpeg")

    assert resp.status_code == 422
    assert "JPEG, PNG or WebP" in resp.json()["detail"]
    assert client.get("/api/boxes/7").status_code == 404


def test_an_image_over_50_megapixels_is_422(client):
    # one bit per pixel keeps this small to build and to send
    huge = _image("PNG", (7100, 7100), "1")
    allowed = _image("PNG", (7000, 7000), "1")
    assert len(huge) < photos.MAX_UPLOAD_BYTES

    refused = _upload(client, "/api/boxes/7/photos", huge, "image/png")

    assert refused.status_code == 422
    assert "megapixels" in refused.json()["detail"]
    assert client.get("/api/boxes/7").status_code == 404
    accepted = _upload(client, "/api/boxes/7/photos", allowed, "image/png")
    assert accepted.status_code == 201
    assert (accepted.json()["width"], accepted.json()["height"]) == (1600, 1600)


def test_an_upload_over_8_mb_is_413(client):
    limit = 8 * 1024 * 1024
    assert photos.MAX_UPLOAD_BYTES == limit

    resp = _upload(client, "/api/boxes/7/photos", _image() + b"\0" * limit)

    assert resp.status_code == 413
    assert isinstance(resp.json()["detail"], str)
    assert client.get("/api/boxes/7").status_code == 404


def test_the_upload_limit_is_inclusive(client):
    image = _image()
    # bytes after the end of a JPEG are ignored by the decoder
    exactly = image + b"\0" * (photos.MAX_UPLOAD_BYTES - len(image))
    assert len(exactly) == photos.MAX_UPLOAD_BYTES

    assert _upload(client, "/api/boxes/7/photos", exactly).status_code == 201
    assert _upload(client, "/api/boxes/7/photos", exactly + b"\0").status_code == 413


def test_an_upload_that_hides_its_length_is_still_cut_off(client):
    def chunks():
        for _ in range(9):
            yield b"\0" * (1024 * 1024)

    resp = client.post("/api/boxes/7/photos", content=chunks(), headers={"Content-Type": "image/jpeg"})

    assert resp.status_code == 413
    assert client.get("/api/boxes/7").status_code == 404


def test_a_box_holds_at_most_12_photos(client):
    item = client.post("/api/boxes/7/items", json={"name": "Tent"}).json()
    for _ in range(12):
        assert _upload(client, "/api/boxes/7/photos").status_code == 201

    resp = _upload(client, "/api/boxes/7/photos")

    assert resp.status_code == 409
    assert isinstance(resp.json()["detail"], str)
    assert len(client.get("/api/boxes/7").json()["photos"]) == 12
    # the limit is per box and per item, so the item still has room
    assert _upload(client, f"/api/items/{item['id']}/photos").status_code == 201
    # and removing one makes room again
    first = client.get("/api/boxes/7").json()["photos"][0]
    assert client.delete(f"/api/photos/{first['id']}").status_code == 204
    assert _upload(client, "/api/boxes/7/photos").status_code == 201


def test_the_12_count_only_photos_that_are_on_the_box_or_item(client, store):
    item = client.post("/api/boxes/7/items", json={"name": "Tent"}).json()
    on_box = [_upload(client, "/api/boxes/7/photos").json() for _ in range(12)]
    on_item = [_upload(client, f"/api/items/{item['id']}/photos").json() for _ in range(12)]
    for photo in on_box[:5] + on_item[:4]:
        assert client.delete(f"/api/photos/{photo['id']}").status_code == 204

    # nine are kept, and none of them takes up room
    for _ in range(5):
        assert _upload(client, "/api/boxes/7/photos").status_code == 201
    for _ in range(4):
        assert _upload(client, f"/api/items/{item['id']}/photos").status_code == 201
    assert _upload(client, "/api/boxes/7/photos").status_code == 409
    assert _upload(client, f"/api/items/{item['id']}/photos").status_code == 409

    box = client.get("/api/boxes/7").json()
    assert (len(box["photos"]), len(box["items"][0]["photos"])) == (12, 12)
    kept = store.list_photos_by_ids([photo["id"] for photo in on_box + on_item])
    assert sorted(photo["id"] for photo in kept if photo["kept"]) == sorted(
        photo["id"] for photo in on_box[:5] + on_item[:4]
    )

    # a new box on the tag of a deleted one starts with all its room
    assert client.delete("/api/boxes/7").status_code == 204
    for _ in range(12):
        assert _upload(client, "/api/boxes/7/photos").status_code == 201
    assert _upload(client, "/api/boxes/7/photos").status_code == 409


def test_an_item_holds_at_most_12_photos(client):
    item = client.post("/api/boxes/7/items", json={"name": "Tent"}).json()
    other = client.post("/api/boxes/7/items", json={"name": "Tarp"}).json()
    for _ in range(12):
        assert _upload(client, f"/api/items/{item['id']}/photos").status_code == 201

    resp = _upload(client, f"/api/items/{item['id']}/photos")

    assert resp.status_code == 409
    assert isinstance(resp.json()["detail"], str)
    box = client.get("/api/boxes/7").json()
    assert [len(entry["photos"]) for entry in box["items"]] == [12, 0]
    assert _upload(client, f"/api/items/{other['id']}/photos").status_code == 201
    assert _upload(client, "/api/boxes/7/photos").status_code == 201


def test_item_photo_404(client):
    assert _upload(client, f"/api/items/{uuid.uuid4()}/photos").status_code == 404
    assert _upload(client, "/api/items/not-a-uuid/photos").status_code == 404
    assert client.get("/api/boxes").json() == []


def test_photo_404(client):
    for photo_id in (str(uuid.uuid4()), "not-a-uuid"):
        for resp in (
            client.get(f"/api/photos/{photo_id}"),
            client.get(f"/api/photos/{photo_id}/thumb"),
            client.delete(f"/api/photos/{photo_id}"),
        ):
            assert resp.status_code == 404
            assert isinstance(resp.json()["detail"], str)


def test_delete_photo(client):
    keep = _upload(client, "/api/boxes/7/photos").json()
    drop = _upload(client, "/api/boxes/7/photos", _image("JPEG", (640, 480))).json()
    before = _bytes(client, drop)

    resp = client.delete(f"/api/photos/{drop['id']}")

    assert resp.status_code == 204
    assert resp.content == b""
    # it is no longer on a box or item, so there is nothing to take off
    again = client.delete(f"/api/photos/{drop['id']}")
    assert again.status_code == 404
    assert isinstance(again.json()["detail"], str)
    assert client.get("/api/boxes/7").json()["photos"] == [keep]
    assert client.get("/api/boxes").json()[0]["photos"] == [keep]
    # and it is kept, at the same address, byte for byte
    assert _bytes(client, drop) == before
    assert _opened(before[0]).size == (640, 480)
    assert client.get(f"/api/photos/{keep['id']}").status_code == 200


@pytest.mark.parametrize("erase", ["0", "false"])
def test_erase_switched_off_removes_and_keeps(client, erase):
    photo = _upload(client, "/api/boxes/7/photos").json()
    before = _bytes(client, photo)

    assert client.delete(f"/api/photos/{photo['id']}", params={"erase": erase}).status_code == 204

    assert client.get("/api/boxes/7").json()["photos"] == []
    assert _bytes(client, photo) == before
    assert [entry["action"] for entry in _history(client)][0] == "photo_removed"


def test_erase_that_is_not_a_yes_or_no_is_422(client):
    photo = _upload(client, "/api/boxes/7/photos").json()

    resp = client.delete(f"/api/photos/{photo['id']}", params={"erase": "maybe"})

    assert resp.status_code == 422
    assert isinstance(resp.json()["detail"], str)
    assert client.get("/api/boxes/7").json()["photos"] == [photo]


def test_photos_are_kept_when_their_box_is_deleted(client):
    item = client.post("/api/boxes/7/items", json={"name": "Tent"}).json()
    on_box = _upload(client, "/api/boxes/7/photos", _image("JPEG", (640, 480))).json()
    on_item = _upload(client, f"/api/items/{item['id']}/photos", _image("PNG", (200, 400)), "image/png").json()
    elsewhere = _upload(client, "/api/boxes/8/photos").json()
    before = {photo["id"]: _bytes(client, photo) for photo in (on_box, on_item, elsewhere)}
    assert before[on_box["id"]] != before[on_item["id"]]

    assert client.delete("/api/boxes/7").status_code == 204

    for photo in (on_box, on_item, elsewhere):
        assert _bytes(client, photo) == before[photo["id"]]
    assert _opened(before[on_item["id"]][0]).size == (200, 400)
    # they are in no list of any box or item
    boxes = client.get("/api/boxes").json()
    assert [(box["tag_id"], box["photos"], box["items"]) for box in boxes] == [(8, [elsewhere], [])]
    # a new box on the same tag starts without them
    assert client.put("/api/boxes/7", json={"name": "Tools"}).json()["photos"] == []
    # and neither can be taken off anything
    for photo in (on_box, on_item):
        assert client.delete(f"/api/photos/{photo['id']}").status_code == 404
        assert _bytes(client, photo) == before[photo["id"]]


def test_photos_are_kept_when_their_item_is_deleted(client):
    item = client.post("/api/boxes/7/items", json={"name": "Tent"}).json()
    on_box = _upload(client, "/api/boxes/7/photos").json()
    on_item = _upload(client, f"/api/items/{item['id']}/photos", _image("JPEG", (640, 480))).json()
    before = _bytes(client, on_item)

    assert client.delete(f"/api/items/{item['id']}").status_code == 204

    assert _bytes(client, on_item) == before
    assert client.get(f"/api/photos/{on_box['id']}").status_code == 200
    box = client.get("/api/boxes/7").json()
    assert box["photos"] == [on_box]
    assert box["items"] == []
    assert client.delete(f"/api/photos/{on_item['id']}").status_code == 404


def test_photos_are_recorded_in_history(client):
    client.put("/api/boxes/7", json={"name": "Camping"})
    item = client.post("/api/boxes/7/items", json={"name": "Tent"}).json()
    on_box = _upload(client, "/api/boxes/7/photos", **{"Remote-User": "alice"}).json()
    on_item = _upload(client, f"/api/items/{item['id']}/photos", **{"Remote-User": "bob"}).json()
    client.delete(f"/api/photos/{on_item['id']}", headers={"Remote-User": "carol"})
    client.delete(f"/api/photos/{on_box['id']}", headers={"Remote-User": "dave"})

    entries = _history(client)[:4]

    assert [
        (entry["action"], entry["actor"], entry["item_id"], entry["item_name"], entry["changes"]) for entry in entries
    ] == [
        ("photo_removed", "dave", None, "", {"photo": [on_box["id"], None]}),
        ("photo_removed", "carol", item["id"], "Tent", {"photo": [on_item["id"], None]}),
        ("photo_added", "bob", item["id"], "Tent", {"photo": [None, on_item["id"]]}),
        ("photo_added", "alice", None, "", {"photo": [None, on_box["id"]]}),
    ]
    assert {(entry["tag_id"], entry["box_name"]) for entry in entries} == {(7, "Camping")}


def test_photos_never_merge_in_history(client, clock):
    for _ in range(3):
        _upload(client, "/api/boxes/7/photos")

    assert [entry["action"] for entry in _history(client)] == ["photo_added"] * 3 + ["box_created"]


def test_deleting_a_box_lists_the_photos_that_went(client):
    client.put("/api/boxes/7", json={"name": "Camping"})
    item = client.post("/api/boxes/7/items", json={"name": "Tent"}).json()
    on_box = [_upload(client, "/api/boxes/7/photos").json() for _ in range(2)]
    on_item = _upload(client, f"/api/items/{item['id']}/photos").json()

    client.delete("/api/boxes/7")

    newest = _history(client)[0]
    assert set(newest) == HISTORY_KEYS
    assert newest["action"] == "box_deleted"
    assert newest["changes"] == {
        "name": ["Camping", None],
        "items": [[{"name": "Tent", "qty": 1}], None],
        # the two on the box, then the one on its item
        "photos": [[on_box[0]["id"], on_box[1]["id"], on_item["id"]], None],
    }
    assert newest["photos"] == [_kept(on_box[0]), _kept(on_box[1]), _kept(on_item)]
    for photo in on_box + [on_item]:
        assert client.get(f"/api/photos/{photo['id']}").status_code == 200


def test_deleting_a_box_without_photos_leaves_the_key_out(client):
    client.put("/api/boxes/7", json={"name": "Camping"})
    client.post("/api/boxes/7/items", json={"name": "Tent"})

    client.delete("/api/boxes/7")

    newest = _history(client)[0]
    assert "photos" not in newest["changes"]
    assert newest["photos"] == []


def test_removing_an_item_lists_the_photos_that_went(client):
    item = client.post("/api/boxes/7/items", json={"name": "Tent"}).json()
    bare = client.post("/api/boxes/7/items", json={"name": "Tarp"}).json()
    on_box = _upload(client, "/api/boxes/7/photos").json()
    on_item = [_upload(client, f"/api/items/{item['id']}/photos").json() for _ in range(2)]

    client.delete(f"/api/items/{item['id']}")
    client.delete(f"/api/items/{bare['id']}")

    without, with_photos = _history(client)[:2]
    assert with_photos["changes"] == {
        "name": ["Tent", None],
        "qty": [1, None],
        "photos": [[on_item[0]["id"], on_item[1]["id"]], None],
    }
    assert with_photos["photos"] == [_kept(on_item[0]), _kept(on_item[1])]
    # no photos, no key
    assert without["changes"] == {"name": ["Tarp", None], "qty": [1, None]}
    assert without["photos"] == []
    assert client.get("/api/boxes/7").json()["photos"] == [on_box]


# --- kept photos in history ---------------------------------------------------


def test_every_entry_carries_the_photos_it_refers_to(client):
    client.put("/api/boxes/7", json={"name": "Camping"})
    item = client.post("/api/boxes/7/items", json={"name": "Tent"}).json()
    client.patch(f"/api/items/{item['id']}", json={"qty": 2})
    stays = _upload(client, "/api/boxes/7/photos").json()
    goes = _upload(client, f"/api/items/{item['id']}/photos").json()
    client.delete(f"/api/photos/{goes['id']}")
    client.put("/api/boxes/7", json={"notes": "heavy"})

    entries = _history(client)

    assert all(set(entry) == HISTORY_KEYS for entry in entries)
    assert [(entry["action"], entry["photos"]) for entry in entries] == [
        ("box_updated", []),
        ("photo_removed", [_kept(goes)]),
        # the entry that added it says the same: the photo is kept now
        ("photo_added", [_kept(goes)]),
        ("photo_added", [_live(stays)]),
        ("item_updated", []),
        ("item_added", []),
        ("box_created", []),
    ]
    assert _history(client, tag_id=7) == entries
    assert _history(client, limit=2, before=entries[1]["at"]) == entries[2:4]
    # what a photo is, never the image itself
    body = client.get("/api/history").content
    assert b'"data"' not in body
    assert b'"thumb"' not in body


def test_an_entry_from_before_photos_were_kept_holds_a_count(client, store):
    live = _upload(client, "/api/boxes/7/photos").json()
    old = {
        "id": str(uuid.uuid4()),
        "at": "2025-06-01T10:00:00.000000+00:00",
        "actor": "alice",
        "action": "box_deleted",
        "tag_id": 9,
        "box_id": str(uuid.uuid4()),
        "box_name": "Old box",
        "item_id": None,
        "item_name": "",
        "changes": {"name": ["Old box", None], "items": [[], None], "photos": [3, None]},
    }
    older = {
        **old,
        "id": str(uuid.uuid4()),
        "at": "2025-06-01T09:00:00.000000+00:00",
        "action": "item_removed",
        "item_id": str(uuid.uuid4()),
        "item_name": "Tent",
        "changes": {"name": ["Tent", None], "qty": [1, None], "photos": [1, None]},
    }
    store.add_history(old)
    store.add_history(older)

    assert _history(client, tag_id=9) == [{**old, "photos": []}, {**older, "photos": []}]
    # and it sits in a page next to entries that do refer to photos
    everything = _history(client)
    assert [entry["action"] for entry in everything] == ["photo_added", "box_created", "box_deleted", "item_removed"]
    assert [entry["photos"] for entry in everything] == [[_live(live)], [], [], []]


class CountsPhotoLookups(SqliteStore):
    lookups: list[list[str]] = []

    def list_photos_by_ids(self, photo_ids):
        self.lookups.append(list(photo_ids))
        return super().list_photos_by_ids(photo_ids)


def test_the_photos_of_a_page_are_fetched_in_one_go(tmp_path):
    store = CountsPhotoLookups(tmp_path / "scannage.db")
    store.lookups = []
    store.apply_schema()
    store.set_meta(samples.META_KEY, "set by the test")
    stores.set_store(store)
    try:
        with TestClient(main.app) as client:
            client.put("/api/boxes/8", json={"name": "Tools"})
            assert [entry["photos"] for entry in _history(client)] == [[]]
            # no entry refers to a photo, so the store is not asked for any
            assert store.lookups == []

            item = client.post("/api/boxes/7/items", json={"name": "Tent"}).json()
            on_box = [_upload(client, "/api/boxes/7/photos").json() for _ in range(3)]
            on_item = [_upload(client, f"/api/items/{item['id']}/photos").json() for _ in range(2)]
            client.delete(f"/api/photos/{on_box[0]['id']}")
            client.delete("/api/boxes/7")
            entries = _history(client)
    finally:
        stores.set_store(None)

    ids = [photo["id"] for photo in on_box + on_item]
    assert len(entries) == 10
    assert len(store.lookups) == 1
    assert sorted(set(store.lookups[0])) == sorted(ids)
    assert [photo["id"] for photo in entries[0]["photos"]] == ids[1:]
    assert {photo["kept"] for entry in entries for photo in entry["photos"]} == {True}


# --- erasing a kept photo -----------------------------------------------------


def test_erase_a_kept_photo(client, clock):
    client.put("/api/boxes/7", json={"name": "Camping"}, headers={"Remote-User": "alice"})
    stays = _upload(client, "/api/boxes/7/photos").json()
    goes = _upload(client, "/api/boxes/7/photos").json()
    client.delete("/api/boxes/7")
    before = _history(client)
    assert before[0]["photos"] == [_kept(stays), _kept(goes)]

    resp = client.delete(f"/api/photos/{goes['id']}", params={"erase": 1}, headers={"Remote-User": "bob"})

    assert resp.status_code == 204
    assert resp.content == b""
    for path in (f"/api/photos/{goes['id']}", f"/api/photos/{goes['id']}/thumb"):
        gone = client.get(path)
        assert gone.status_code == 404
        assert isinstance(gone.json()["detail"], str)
    assert client.get(f"/api/photos/{stays['id']}").status_code == 200

    erased, deleted, *earlier = _history(client)
    assert erased == {
        "id": erased["id"],
        "at": "2026-01-01T00:00:05.000000+00:00",
        "actor": "bob",
        "action": "photo_erased",
        "tag_id": 7,
        "box_id": goes["box_id"],
        "box_name": "Camping",
        "item_id": None,
        "item_name": "",
        "changes": {"photo": [goes["id"], None]},
        "photos": [],
    }
    # the entries that referred to it keep their ids and no longer offer the photo
    assert deleted["changes"]["photos"] == [[stays["id"], goes["id"]], None]
    assert deleted["photos"] == [_kept(stays)]
    assert [(entry["action"], entry["photos"]) for entry in earlier] == [
        ("photo_added", []),
        ("photo_added", [_kept(stays)]),
        ("box_created", []),
    ]
    assert [entry["changes"] for entry in earlier] == [entry["changes"] for entry in before[1:]]

    # there is nothing left to erase or to take off
    assert client.delete(f"/api/photos/{goes['id']}", params={"erase": 1}).status_code == 404
    assert client.delete(f"/api/photos/{goes['id']}").status_code == 404
    assert len(_history(client)) == len(before) + 1


def test_erase_a_photo_of_an_item_that_is_gone(client):
    client.put("/api/boxes/7", json={"name": "Camping"})
    item = client.post("/api/boxes/7/items", json={"name": "Tent"}).json()
    photo = _upload(client, f"/api/items/{item['id']}/photos").json()
    client.patch(f"/api/items/{item['id']}", json={"name": "Tent, 4 person"})
    client.delete(f"/api/items/{item['id']}")
    client.put("/api/boxes/7", json={"name": "Camping gear"})

    assert client.delete(f"/api/photos/{photo['id']}", params={"erase": "true"}).status_code == 204

    erased = _history(client)[0]
    assert (erased["action"], erased["tag_id"], erased["box_id"]) == ("photo_erased", 7, item["box_id"])
    assert (erased["box_name"], erased["item_id"]) == ("Camping gear", item["id"])
    assert erased["item_name"] == "Tent, 4 person"
    assert erased["changes"] == {"photo": [photo["id"], None]}
    assert client.get(f"/api/photos/{photo['id']}").status_code == 404
    # the box it was on is not touched
    assert client.get("/api/boxes/7").json()["name"] == "Camping gear"


def test_erase_a_photo_that_is_still_in_use_is_409(client):
    item = client.post("/api/boxes/7/items", json={"name": "Tent"}).json()
    on_box = _upload(client, "/api/boxes/7/photos").json()
    on_item = _upload(client, f"/api/items/{item['id']}/photos").json()
    served = {photo["id"]: _bytes(client, photo) for photo in (on_box, on_item)}
    before = _history(client)

    for photo in (on_box, on_item):
        resp = client.delete(f"/api/photos/{photo['id']}", params={"erase": 1})

        assert resp.status_code == 409
        assert isinstance(resp.json()["detail"], str)
        assert _bytes(client, photo) == served[photo["id"]]
    box = client.get("/api/boxes/7").json()
    assert box["photos"] == [on_box]
    assert box["items"][0]["photos"] == [on_item]
    assert _history(client) == before

    # taken off first, it can be erased
    assert client.delete(f"/api/photos/{on_box['id']}").status_code == 204
    assert client.delete(f"/api/photos/{on_box['id']}", params={"erase": 1}).status_code == 204
    assert client.get(f"/api/photos/{on_box['id']}").status_code == 404
    assert [entry["action"] for entry in _history(client)][:2] == ["photo_erased", "photo_removed"]


def test_erase_an_unknown_photo_is_404(client):
    kept = _upload(client, "/api/boxes/7/photos").json()
    client.delete(f"/api/photos/{kept['id']}")
    before = _history(client)

    for photo_id in (str(uuid.uuid4()), "not-a-uuid"):
        resp = client.delete(f"/api/photos/{photo_id}", params={"erase": 1})

        assert resp.status_code == 404
        assert isinstance(resp.json()["detail"], str)
    assert _history(client) == before
    assert client.get(f"/api/photos/{kept['id']}").status_code == 200


def test_only_erase_ever_deletes_a_photo(client, store):
    item = client.post("/api/boxes/7/items", json={"name": "Tent"}).json()
    added = [_upload(client, "/api/boxes/7/photos").json() for _ in range(3)]
    added += [_upload(client, f"/api/items/{item['id']}/photos").json() for _ in range(2)]
    other = client.post("/api/boxes/7/items", json={"name": "Tarp"}).json()
    added.append(_upload(client, f"/api/items/{other['id']}/photos").json())
    ids = [photo["id"] for photo in added]

    client.delete(f"/api/photos/{added[0]['id']}")
    client.delete(f"/api/photos/{added[3]['id']}")
    client.delete(f"/api/items/{other['id']}")
    client.delete("/api/boxes/7")
    client.put("/api/boxes/7", json={"name": "Tools"})
    client.delete("/api/boxes/7")

    assert sorted(photo["id"] for photo in store.list_photos_by_ids(ids)) == sorted(ids)
    assert all(client.get(f"/api/photos/{photo_id}").status_code == 200 for photo_id in ids)

    assert client.delete(f"/api/photos/{ids[2]}", params={"erase": 1}).status_code == 204

    assert sorted(photo["id"] for photo in store.list_photos_by_ids(ids)) == sorted(ids[:2] + ids[3:])
