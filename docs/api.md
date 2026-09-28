# API and data contract

The frontend talks only to this API. The API talks only to the configured
store. Both stores return the same shapes.

## Tags

A printed tag is an ArUco marker from the `ARUCO_MIP_36h12` dictionary. It
encodes a number, `tag_id`, from 0 to 249. It never encodes contents, so a
label is printed once and the box behind it can change freely.

`TAG_COUNT` is 250. A `tag_id` outside 0 to 249 is rejected with 422.

## Shapes

Box:

```json
{
  "id": "0b8f6c2e-5a1d-4a57-9d55-6f1f0b3c9a10",
  "tag_id": 7,
  "name": "Camping",
  "location": "Shelf A, top",
  "notes": "",
  "created_at": "2026-09-27T21:00:00.000000+00:00",
  "updated_at": "2026-09-27T21:00:00.000000+00:00",
  "updated_by": "",
  "items": []
}
```

Item:

```json
{
  "id": "6f0a4f0e-7f7b-4a3e-8f57-0e1c6f0a9d21",
  "box_id": "0b8f6c2e-5a1d-4a57-9d55-6f1f0b3c9a10",
  "name": "Sleeping bag",
  "qty": 2,
  "created_at": "2026-09-27T21:00:00.000000+00:00",
  "updated_at": "2026-09-27T21:00:00.000000+00:00"
}
```

Timestamps are UTC ISO 8601 strings written by the app, not the database.
They always carry six decimal places, so they sort as text. Adding or
changing an item also updates `updated_at` and `updated_by` on its box;
deleting an item does not. Boxes are ordered by `tag_id`. Items inside a box
are ordered by `created_at`, then `id`.

Limits: `name` and `location` up to 120 characters, `notes` up to 2000,
item `name` 1 to 120, `qty` 1 to 9999. Strings are trimmed.

## Endpoints

| Method and path | Body | Returns |
|---|---|---|
| `GET /api/health` | | `{"status": "ok" or "degraded", "store": "sqlite" or "postgrest", "detail": "..."}`, 503 when degraded |
| `GET /api/config` | | `{"dictionary": "ARUCO_MIP_36h12", "tag_count": 250, "base_url": "https://host", "user": "", "store": "sqlite"}` |
| `GET /api/boxes` | | list of boxes, each with its items |
| `GET /api/boxes/{tag_id}` | | box, or 404 |
| `PUT /api/boxes/{tag_id}` | `{"name", "location", "notes"}`, all optional | the box, created if the tag was unclaimed |
| `DELETE /api/boxes/{tag_id}` | | 204, or 404. Removes the box and its items and frees the tag |
| `POST /api/boxes/{tag_id}/items` | `{"name", "qty"}`, `qty` defaults to 1 | the item, 201. Creates the box if the tag was unclaimed |
| `PATCH /api/items/{item_id}` | `{"name", "qty"}`, both optional | the item, or 404 |
| `DELETE /api/items/{item_id}` | | 204, or 404 |
| `GET /api/qr/{tag_id}.svg` | | QR code holding `{base_url}/b/{tag_id}` |

Errors are `{"detail": "..."}`, always with a string, validation errors
included. A store failure is 502. An `item_id` that is not a UUID is a 404.

The QR code is an SVG at error level M with a 4 module quiet zone. It has a
`viewBox` and a default size of 8 pixels per module, so it scales in a page
and draws on a canvas. It is sent with `Cache-Control: no-store`.

Pages: `/` is the app, `/b/{tag_id}` is the app opened on that box, `/labels`
is the printable label sheet. Static files are under `/static/`.

## Identity

The app has no login. It expects to sit behind a reverse proxy that handles
sign-in. When the proxy sends a `Remote-User` header, its value is stored as
`updated_by` and returned as `user` from `/api/config`. Without the header
both are empty.

Writes with an `Origin` header that does not match the request host are
rejected with 403.

## Store interface

`app/stores/base.py` defines the interface both stores implement:

```python
class Store(Protocol):
    name: str  # "sqlite" or "postgrest"

    def bootstrap(self) -> None: ...
    def health(self) -> tuple[bool, str]: ...
    def list_boxes(self) -> list[dict]: ...
    def get_box(self, tag_id: int) -> dict | None: ...
    def upsert_box(self, tag_id: int, fields: dict, actor: str) -> dict: ...
    def delete_box(self, tag_id: int) -> bool: ...
    def add_item(self, tag_id: int, name: str, qty: int, actor: str) -> dict: ...
    def update_item(self, item_id: str, fields: dict, actor: str) -> dict | None: ...
    def delete_item(self, item_id: str) -> bool: ...
    def get_meta(self, key: str) -> str | None: ...
    def set_meta(self, key: str, value: str) -> None: ...
```

`get_meta` and `set_meta` read and write `app_meta`, a small key/value table
for one-time flags such as `samples_seeded`. It is not exposed by the API.

The store layer accepts any integer `tag_id`. Only the API enforces the 0 to
249 range, which leaves negative numbers free for tests against a live store.
