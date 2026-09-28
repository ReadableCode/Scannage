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
  "items": [],
  "photos": []
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
  "updated_at": "2026-09-27T21:00:00.000000+00:00",
  "photos": []
}
```

`photos` holds the shape described under Photos. Every box and every item
the API returns carries it, empty when there are none.

Timestamps are UTC ISO 8601 strings written by the app, not the database.
They always carry six decimal places, so they sort as text. Adding or
changing an item, or adding a photo, also updates `updated_at` and
`updated_by` on its box; deleting an item or a photo does not. Boxes are
ordered by `tag_id`. Items inside a box are ordered by `created_at`, then
`id`.

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

The history and photo endpoints are listed in their own sections below.

Errors are `{"detail": "..."}`, always with a string, validation errors
included. A store failure is 502. An `item_id` or `photo_id` that is not a
UUID is a 404.

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
    def apply_schema(self, force: bool = False) -> bool: ...
    def health(self) -> tuple[bool, str]: ...
    def list_boxes(self) -> list[dict]: ...
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
    def add_history(self, entry: dict) -> dict: ...
    def update_history(self, entry_id: str, changes: dict, at: str, box_name: str, item_name: str) -> None: ...
    def delete_history(self, entry_id: str) -> bool: ...

    def list_history(
        self, tag_id: int | None, limit: int, before: str | None, include_tests: bool = False
    ) -> list[dict]: ...

    def get_meta(self, key: str) -> str | None: ...
    def set_meta(self, key: str, value: str) -> None: ...
```

`apply_schema` creates whatever tables are missing and nothing else.
`bootstrap` is what the app runs at startup: `apply_schema`, then the sample
boxes when the database is new (see Sample boxes).

`add_photo` takes an image that is already processed, as
`{"width", "height", "data", "thumb"}` with the two JPEGs as bytes.
`get_photo` returns the photo shape and `get_photo_data` the bytes of the
image or of its thumbnail. No list ever reads the bytes.

`update_history` and `delete_history` exist only for the merge rule under
History. `list_history` leaves out negative tag ids unless `include_tests` is
set, which only the merge rule and the tests against a live store do.

The stores record no history themselves. `app/history.py` wraps each write:
it reads the state before, calls the store, works out `changes` and records
the entry. The API writes only through it. A failure to record is logged and
the write still stands.

`get_meta` and `set_meta` read and write `app_meta`, a small key/value table
for one-time flags such as `samples_seeded`. It is not exposed by the API.

The store layer accepts any integer `tag_id`. Only the API enforces the 0 to
249 range, which leaves negative numbers free for tests against a live store.

## Sample boxes

Six sample boxes, on tags 1 to 6, come with a new database. There is no
setting for them and no endpoint.

At startup, after the schema is in place, the store looks at the
`samples_seeded` flag in `app_meta`. When it is set, nothing happens, for
good: deleting every box does not bring the samples back. When it is not set
and there are no boxes, the samples are added and the flag is set. When it is
not set and there are boxes, only the flag is set. When adding them fails the
flag stays unset, the app still serves, and the next start tries again.

`scripts/init_db.py --samples` is the one way to get them back on purpose. It
adds each sample whose tag is unclaimed and never touches a box that exists.

In history the samples are recorded with the actor `samples`, which is also
their `updated_by`.

## History

Every change to the inventory is recorded and kept for good. History is
append only: nothing in the API edits or removes an entry, and entries stay
after the box they describe is deleted.

Entry:

```json
{
  "id": "5d0c1a52-2f0b-4f4e-9d0e-6a2b8f7f1c11",
  "at": "2026-09-27T21:00:00.000000+00:00",
  "actor": "",
  "action": "item_updated",
  "tag_id": 7,
  "box_id": "0b8f6c2e-5a1d-4a57-9d55-6f1f0b3c9a10",
  "box_name": "Camping",
  "item_id": "6f0a4f0e-7f7b-4a3e-8f57-0e1c6f0a9d21",
  "item_name": "Sleeping bag",
  "changes": {"qty": [1, 2]}
}
```

`action` is one of `box_created`, `box_updated`, `box_deleted`, `item_added`,
`item_updated`, `item_removed`, `photo_added`, `photo_removed`.

`changes` maps a field to `[before, after]`. A created thing has `null`
before, a removed thing has `null` after. `box_deleted` also carries
`"items": [[{"name": "Tent", "qty": 1}], null]`, so the entry says what was in
the box when it went. `box_name` and `item_name` are the names at the time,
`item_id` is `null` and `item_name` is empty for box level entries.

What `changes` holds for each action:

| Action | `changes` |
|---|---|
| `box_created` | `name`, `location`, `notes`, each `[null, value]`. A field left empty is left out, so claiming a tag with nothing in it records `{}` |
| `box_updated` | the fields that changed |
| `box_deleted` | the fields that were not empty, each `[value, null]`, and `items`, which is always there, `[[], null]` for an empty box |
| `item_added` | `name` and `qty`, each `[null, value]` |
| `item_updated` | the fields that changed |
| `item_removed` | `name` and `qty`, each `[value, null]` |
| `photo_added` | `{"photo": [null, "<photo id>"]}` |
| `photo_removed` | `{"photo": ["<photo id>", null]}` |

`box_deleted` and `item_removed` also carry `"photos": [<count>, null]` when
photos went with the box or item. For a box the count covers its own photos
and those of its items. Without photos the key is left out.

An entry for a photo on an item carries that item's `item_id` and
`item_name`. Adding an item or a photo to an unclaimed tag creates the box,
which records `box_created` first and then the `item_added` or `photo_added`.

`box_name` and `item_name` are the names after the write, and the names
before it for something that was deleted.

A write that changes nothing records nothing. Typing saves a field several
times in a row, so an `*_updated` entry is merged into the newest entry when
that entry is the same action on the same box and item by the same actor and
is less than 120 seconds old: `at` moves forward, and each field keeps its
first `before` and takes the latest `after`. A field that ends up back at its
first value is dropped, and an entry left with no changes is removed. This is
the only time an entry is touched after it is written.

The newest entry is the newest one on that tag, so a write to another box in
between does not stop two writes to this box from merging. A merge also
brings `box_name` and `item_name` up to date. Only `*_updated` entries merge:
an update is never folded into the entry that created the box or item.

| Method and path | Returns |
|---|---|
| `GET /api/history` | newest first. Query: `tag_id` (optional), `limit` (default 50, 1 to 200), `before` (optional timestamp, for the next page) |

The answer is a list of entries, ordered by `at`, newest first. `before`
returns the entries strictly older than it, so the `at` of the last entry of
one page is the `before` of the next. An empty list means there are no more.
`tag_id` outside 0 to 249, `limit` outside 1 to 200 and a `before` that is
not a timestamp are each a 422.

Entries with a negative `tag_id` are never returned by the API. Those belong
to tests against a live store.

## Photos

A box and an item can each carry photos. Nothing asks for one.

Photo:

```json
{
  "id": "a3c5e6f0-0f5c-4a0e-8b0a-0c9d3c1b7e22",
  "box_id": "0b8f6c2e-5a1d-4a57-9d55-6f1f0b3c9a10",
  "item_id": null,
  "width": 1600,
  "height": 1200,
  "size": 183222,
  "created_at": "2026-09-27T21:00:00.000000+00:00",
  "created_by": ""
}
```

A box gains `"photos": [...]` holding its box level photos, and an item gains
`"photos": [...]`. Both are ordered by `created_at`, then `id`. These lists
hold the shape above and never the image bytes.

| Method and path | Body | Returns |
|---|---|---|
| `POST /api/boxes/{tag_id}/photos` | the image bytes, `Content-Type` `image/jpeg`, `image/png` or `image/webp` | the photo, 201. Creates the box if the tag was unclaimed |
| `POST /api/items/{item_id}/photos` | same | the photo, 201, or 404 |
| `GET /api/photos/{photo_id}` | | the image, `image/jpeg` |
| `GET /api/photos/{photo_id}/thumb` | | a small version, `image/jpeg` |
| `DELETE /api/photos/{photo_id}` | | 204, or 404 |

The upload body is limited to 8 MB (413 above that). The server decodes the
image, turns it upright, scales it down to at most 1600 pixels on the long
edge, and stores it as JPEG with all metadata removed, location included. It
stores a second copy at most 320 pixels on the long edge as the thumbnail.
Something that does not decode as an image is a 422. A box or an item holds at
most 12 photos (409 above that).

What decides is what the bytes decode as, not the `Content-Type` that came
with them. JPEG, PNG and WebP are taken, any other format is a 422, and so is
an image of more than 50 megapixels. A small image is never enlarged.
Anything see-through is laid over white. `width`, `height` and `size` describe
the stored JPEG, `size` in bytes. The 12 of a box count its own photos, not
those of its items. A `photo_id` that matches nothing is a 404 on all three
photo addresses.

Photos are sent with `Cache-Control: private, max-age=31536000, immutable`. A
photo never changes once stored, so its address can be cached for good.

Photos are removed with their box or item. The history entry stays.
