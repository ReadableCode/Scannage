"""The HTTP API against a real SQLite store under tmp_path."""

import uuid
import xml.etree.ElementTree as ET

import pytest
from fastapi.testclient import TestClient

from app import config, main, qr, samples, stores
from app.stores.base import StoreError
from app.stores.sqlite import SqliteStore

SVG_NS = "{http://www.w3.org/2000/svg}"


@pytest.fixture()
def store(tmp_path):
    store = SqliteStore(tmp_path / "scannage.db")
    stores.set_store(store)
    yield store
    stores.set_store(None)


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
        client.get(f"/api/qr/{tag_id}.svg"),
    ]

    for resp in responses:
        assert resp.status_code == 422, resp.request.url
        assert isinstance(resp.json()["detail"], str)
    assert client.get("/api/boxes").json() == []


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
    evil = {"Origin": "https://evil.example.com"}

    responses = [
        client.put("/api/boxes/7", json={"name": "taken"}, headers=evil),
        client.delete("/api/boxes/7", headers=evil),
        client.post("/api/boxes/7/items", json={"name": "planted"}, headers=evil),
        client.patch(f"/api/items/{item['id']}", json={"qty": 9}, headers=evil),
        client.delete(f"/api/items/{item['id']}", headers=evil),
    ]

    for resp in responses:
        assert resp.status_code == 403, resp.request.method
        assert resp.json() == {"detail": "cross-origin request rejected"}
    box = client.get("/api/boxes/7").json()
    assert box["name"] == ""
    assert box["items"] == [item]


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


# --- sample seeding -----------------------------------------------------------


def test_samples_are_off_by_default(client):
    assert client.get("/api/boxes").json() == []


def test_samples_seed_once_and_never_again(store, monkeypatch):
    monkeypatch.setattr(config, "SEED_SAMPLES", True)

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
        assert store.get_meta(samples.META_KEY) is not None

    # a second boot with the samples still there adds nothing
    with TestClient(main.app) as client:
        assert len(client.get("/api/boxes").json()) == 6
        for tag_id in range(1, 7):
            assert client.delete(f"/api/boxes/{tag_id}").status_code == 204
        assert client.get("/api/boxes").json() == []

    # and a boot on the emptied store does not bring them back
    with TestClient(main.app) as client:
        assert client.get("/api/boxes").json() == []


def test_samples_never_added_to_an_inventory_in_use(store, monkeypatch):
    monkeypatch.setattr(config, "SEED_SAMPLES", True)
    store.bootstrap()
    store.upsert_box(40, {"name": "Mine"}, "")

    with TestClient(main.app) as client:
        assert [box["tag_id"] for box in client.get("/api/boxes").json()] == [40]
        assert store.get_meta(samples.META_KEY) is not None
        client.delete("/api/boxes/40")

    with TestClient(main.app) as client:
        assert client.get("/api/boxes").json() == []
