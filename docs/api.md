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
| `DELETE /api/boxes/{tag_id}` | | 204, or 404. Removes the box and its items and frees the tag. Their photos are kept |
| `POST /api/boxes/{tag_id}/items` | `{"name", "qty"}`, `qty` defaults to 1 | the item, 201. Creates the box if the tag was unclaimed |
| `PATCH /api/items/{item_id}` | `{"name", "qty"}`, both optional | the item, or 404 |
| `DELETE /api/items/{item_id}` | | 204, or 404. Its photos are kept |
| `GET /api/qr/{tag_id}.svg` | | QR code holding `{base_url}/b/{tag_id}` |

The history, photo and printed label endpoints are listed in their own
sections below.

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
```

`apply_schema` creates whatever tables, columns, indexes and triggers are
missing, and gives photos stored before `tag_id` existed the tag of their
box. It removes nothing.
`bootstrap` is what the app runs at startup: `apply_schema`, then the sample
boxes when the database is new (see Sample boxes).

`add_photo` takes an image that is already processed, as
`{"width", "height", "data", "thumb"}` with the two JPEGs as bytes.
`get_photo` returns the photo shape of a photo that is on a box or item, and
`None` for a kept one. `get_photo_data` returns the bytes of the image or of
its thumbnail, kept or not. No list ever reads the bytes.

`delete_photo`, `delete_item` and `delete_box` take photos off and never
delete one: the database keeps each photo as it goes (see Kept photos).
`get_kept_photo` returns a kept photo as the photo shape plus `tag_id`, the
tag it was on, and `removed_at`, and `None` for a photo that is still on a box
or item. `list_photos_by_ids` returns the photos with these ids that exist,
on a box or item or kept, each in the photo shape plus `kept`. Ids that match
nothing, or that are not ids, are left out. The order is not part of the
contract. `erase_photo` deletes a kept photo and returns whether there was
one. It never touches a photo that is on a box or item, and it is the only
call that deletes image bytes.

`update_history` and `delete_history` exist only for the merge rule under
History. `list_history` leaves out negative tag ids unless `include_tests` is
set, which only the merge rule and the tests against a live store do.

The stores record no history themselves. `app/history.py` wraps each write:
it reads the state before, calls the store, works out `changes` and records
the entry. The API writes only through it. A failure to record is logged and
the write still stands.

The API reads history through it as well. `list_history` returns entries as
they were written, without `photos`. `history.list_entries` adds `photos` to
each: it collects the photo ids of the whole page and asks the store for them
once, with `list_photos_by_ids`.

`list_tag_ids` returns the tags that have a box, ascending. It reads the
tags alone, never the items or photos of a box.

`list_printed` returns the printed labels in the shape under Printed labels,
ordered by `tag_id`. `record_printed` records one print of each of these
tags, each tag once however often it is given, all with the same time and
actor, and an empty list records nothing. `forget_printed` removes the row of
one tag and returns whether there was one. None of the three writes history
or touches a box. `list_tag_ids` and `list_printed` return every tag,
negative ones included; the API leaves those out.

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
  "changes": {"qty": [1, 2]},
  "photos": []
}
```

`action` is one of `box_created`, `box_updated`, `box_deleted`, `item_added`,
`item_updated`, `item_removed`, `photo_added`, `photo_removed`,
`photo_erased`.

`photos` holds the photos the entry refers to, see Kept photos.

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
| `photo_erased` | `{"photo": ["<photo id>", null]}` |

`box_deleted` and `item_removed` also carry
`"photos": [["<photo id>", "<photo id>"], null]` when photos went with the box
or item. For a box the list holds its own photos first and then those of its
items, item by item. Without photos the key is left out. An entry written
before photos were kept holds a count in place of the list.

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
| `DELETE /api/photos/{photo_id}` | | 204, or 404. The photo is kept, see Kept photos |

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
those of its items, and kept photos do not count. A `photo_id` that matches
nothing is a 404 on all three photo addresses.

Photos are sent with `Cache-Control: private, max-age=31536000, immutable`. A
photo never changes once stored, so its address can be cached for good.

Photos leave the inventory with their box or item, and are kept. See Kept
photos below.

## Kept photos

A photo is never lost by accident. Removing a photo, or deleting the box or
item it belongs to, takes it out of the inventory and keeps it. A kept photo
is in no `photos` list of a box or item. It stays at the same address, and it
is reachable from the history entry that removed it.

Every history entry gains `"photos": [...]`: the photos that entry refers to
and that still exist, in the photo shape plus `"kept": true` or `false`.
`kept` is `false` while the photo is still on a box or item. They come in the
order of the ids in `changes`. `box_id` and `item_id` of a kept photo are
those it had, of a box or item that may be gone.

| Action | Photos the entry refers to |
|---|---|
| `photo_added`, `photo_removed` | that one photo |
| `box_deleted` | every photo that was on the box and its items when it was deleted |
| `item_removed` | every photo that was on the item |
| anything else | none, an empty list |

For `box_deleted` and `item_removed`, `changes.photos` is
`[["<photo id>", "<photo id>"], null]`, the ids that went with it, and is left
out when there were none. Entries written before this existed may hold a
count there instead of a list; they refer to no photos.

| Method and path | Returns |
|---|---|
| `DELETE /api/photos/{photo_id}` | 204. Takes the photo off its box or item and keeps it. 404 when it is not on a box or item |
| `DELETE /api/photos/{photo_id}?erase=1` | 204. Erases a kept photo for good. 404 when there is no such photo, 409 when it is still on a box or item |

`erase=1` or `erase=true` erases. Without `erase`, or with `0` or `false`,
the photo is taken off and kept. A value that does not read as yes or no is a
422.

Erasing is the only way a photo leaves the database. It records a
`photo_erased` entry with `changes` `{"photo": ["<photo id>", null]}` on the
tag the photo belonged to. After that the photo's address answers 404 and it
is gone from every entry's `photos` list.

The `photo_erased` entry carries the `box_id` and `item_id` the photo had.
Its `box_name` and `item_name` are the names in the newest entry of that box
and of that item, looked for in the newest 200 entries of the tag, and empty
when there is none. Its own `photos` list is empty. The ids in `changes` of
the other entries stay as they were written.

A kept photo does not count towards the 12 of a box or item.

Kept photos live in their own table, `kept_photos`, which has the columns of
`photos` plus `removed_at` and references nothing. A trigger on `photos`
copies each row there before it is deleted, in the same transaction, whether
the photo is deleted on its own or goes with its box or item. Each photo
carries the `tag_id` of its box, filled in by a second trigger when it is
added, because the box is already gone by the time a delete of the box reaches
its photos. `removed_at` is written by the database. Who removed a photo is in
the history entry.

With the PostgREST store nothing is deleted until the store has seen that
`kept_photos` is served. Until then a delete of a box, item or photo is a 502.

## Printed labels

The app records which tags have had a label printed, so the label sheet can
offer the next ones and a reprint is a deliberate choice.

```json
{
  "tag_count": 250,
  "printed": [
    {
      "tag_id": 1,
      "first_printed_at": "2026-09-27T21:00:00.000000+00:00",
      "last_printed_at": "2026-09-27T21:00:00.000000+00:00",
      "times": 1,
      "printed_by": ""
    }
  ],
  "in_use": [1, 2],
  "next": [3, 4, 5]
}
```

`printed` is ordered by `tag_id`. `in_use` holds the tags that have a box,
ascending. `next` holds every tag that is neither printed nor in use, in the
order labels should be handed out: ascending from 1, with tag 0 last. It is
the whole list, so the page takes as many from the front as it needs.

| Method and path | Body | Returns |
|---|---|---|
| `GET /api/labels` | | the shape above |
| `POST /api/labels/printed` | `{"tag_ids": [3, 4, 5]}`, 1 to 250 ids, each 0 to 249 | the shape above, after recording. A tag printed before has `times` raised by one and `last_printed_at` moved; `first_printed_at` stays. An id repeated in the body counts once |
| `DELETE /api/labels/printed/{tag_id}` | | 204, or 404. Forgets that the tag was ever printed |

`printed_by` is the `Remote-User` of the latest print, empty without one.
Printing is not part of a box's history: a label can be printed long before
a box exists.

A browser cannot tell whether paper came out. A print is recorded when the
print button is pressed, and the page offers to take it back.

A body without `tag_ids`, with an empty list, with more than 250 ids, or
with an id that is not an integer from 0 to 249 is a 422, and nothing of it
is recorded. The 250 count the ids as sent, repeats included. A `tag_id`
outside 0 to 249 in the address of the `DELETE` is a 422. Both writes are
subject to the same origin check under Identity.

A tag can be printed and in use at once, and is then in both lists. Deleting
a box takes its tag out of `in_use` and leaves its label printed. Forgetting
a print and printing the tag again starts over, with `times` 1.

Tags with a negative id are never returned by the API, in `printed`, in
`in_use` or in `next`. Those belong to tests against a live store.

Printed labels live in their own table, `printed_tags`, one row per tag, with
`tag_id` as its key and `times` at least 1. It references nothing, so a row
needs no box and stays when a box is deleted. The times are written by the
app.

The SQLite store records a print in one statement per tag, inside one
transaction. The PostgREST store reads the rows of the tags in the print with
one request and writes them all back with one more, so two prints of the same
tag at the same moment can count as one.
