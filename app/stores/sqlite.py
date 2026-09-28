"""SQLite store: one file, stdlib only.

Every call opens its own connection, so it is safe under the FastAPI
threadpool. Writes take the database lock up front (BEGIN IMMEDIATE), which
keeps each read-then-write sequence atomic.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .. import samples
from .base import (
    BOX_FIELDS,
    HISTORY_KEYS,
    ITEM_FIELDS,
    KEPT_PHOTO_KEYS,
    PHOTO_KEYS,
    PRINTED_KEYS,
    StoreError,
    attach_photos,
    chunks,
    clean_uuid,
    clean_uuids,
    new_id,
    once_each,
    pick,
    utc_now,
)

# Additive and idempotent only: safe to run on every boot.
SCHEMA = """
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

-- tag_id, box_id and item_id reference nothing, so an entry outlives its box
CREATE TABLE IF NOT EXISTS history (
    id        TEXT PRIMARY KEY,
    at        TEXT NOT NULL,
    actor     TEXT NOT NULL DEFAULT '',
    action    TEXT NOT NULL,
    tag_id    INTEGER NOT NULL,
    box_id    TEXT NOT NULL,
    box_name  TEXT NOT NULL DEFAULT '',
    item_id   TEXT,
    item_name TEXT NOT NULL DEFAULT '',
    changes   TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS history_tag_id_at_idx ON history (tag_id, at DESC);

CREATE INDEX IF NOT EXISTS history_at_idx ON history (at DESC);

CREATE TABLE IF NOT EXISTS photos (
    id         TEXT PRIMARY KEY,
    box_id     TEXT NOT NULL,
    item_id    TEXT,
    width      INTEGER NOT NULL,
    height     INTEGER NOT NULL,
    size       INTEGER NOT NULL,
    data       BLOB NOT NULL,
    thumb      BLOB NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL DEFAULT '',
    CONSTRAINT photos_box_id_fkey FOREIGN KEY (box_id) REFERENCES boxes (id) ON DELETE CASCADE,
    CONSTRAINT photos_item_id_fkey FOREIGN KEY (item_id) REFERENCES items (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS photos_box_id_idx ON photos (box_id);

CREATE INDEX IF NOT EXISTS photos_item_id_idx ON photos (item_id);

-- A photo that left the inventory. It references nothing, so no delete of a
-- box or an item can reach it.
CREATE TABLE IF NOT EXISTS kept_photos (
    id         TEXT PRIMARY KEY,
    box_id     TEXT NOT NULL,
    item_id    TEXT,
    width      INTEGER NOT NULL,
    height     INTEGER NOT NULL,
    size       INTEGER NOT NULL,
    data       BLOB NOT NULL,
    thumb      BLOB NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL DEFAULT '',
    tag_id     INTEGER,
    removed_at TEXT NOT NULL
);

-- One row per tag that has had a label printed. tag_id references nothing: a
-- label is printed before any box exists.
CREATE TABLE IF NOT EXISTS printed_tags (
    tag_id           INTEGER PRIMARY KEY,
    first_printed_at TEXT NOT NULL,
    last_printed_at  TEXT NOT NULL,
    times            INTEGER NOT NULL DEFAULT 1 CHECK (times >= 1),
    printed_by       TEXT NOT NULL DEFAULT ''
);
"""

# SQLite has no ADD COLUMN IF NOT EXISTS, so apply_schema looks before it adds.
# The tag rides on the photo because its box is already gone when a delete of
# the box reaches the photo.
PHOTO_TAG_COLUMN = "ALTER TABLE photos ADD COLUMN tag_id INTEGER"

# Each trigger is one statement with semicolons inside, so they are kept apart
# from SCHEMA, which is split on them.
TRIGGERS = (
    """
CREATE TRIGGER IF NOT EXISTS photos_fill_tag_id AFTER INSERT ON photos
WHEN NEW.tag_id IS NULL
BEGIN
    UPDATE photos SET tag_id = (SELECT tag_id FROM boxes WHERE id = NEW.box_id) WHERE id = NEW.id;
END
""",
    # Runs for a photo deleted on its own and for one deleted with its box or
    # item, inside the same transaction, so a photo cannot go without being kept.
    """
CREATE TRIGGER IF NOT EXISTS photos_keep BEFORE DELETE ON photos
BEGIN
    INSERT INTO kept_photos (
        id, box_id, item_id, width, height, size, data, thumb, created_at, created_by, tag_id, removed_at
    ) VALUES (
        OLD.id, OLD.box_id, OLD.item_id, OLD.width, OLD.height, OLD.size, OLD.data, OLD.thumb,
        OLD.created_at, OLD.created_by, OLD.tag_id, strftime('%Y-%m-%dT%H:%M:%f000+00:00', 'now')
    );
END
""",
)

# Photos stored before the column existed. Only rows without a tag are touched.
FILL_PHOTO_TAGS = """
UPDATE photos SET tag_id = (SELECT tag_id FROM boxes WHERE id = photos.box_id) WHERE tag_id IS NULL
"""

BOX_COLUMNS = "id, tag_id, name, location, notes, created_at, updated_at, updated_by"
ITEM_COLUMNS = "id, box_id, name, qty, created_at, updated_at"
PHOTO_COLUMNS = ", ".join(PHOTO_KEYS)
KEPT_PHOTO_COLUMNS = ", ".join(KEPT_PHOTO_KEYS)
KEPT_PHOTOS = "kept_photos"
# SQLite takes a limited number of values in one statement.
ID_CHUNK = 500
HISTORY_COLUMNS = ", ".join(HISTORY_KEYS)
PRINTED_COLUMNS = ", ".join(PRINTED_KEYS)

# A tag printed before keeps its first time and counts one more.
RECORD_PRINTED = f"""
INSERT INTO printed_tags ({PRINTED_COLUMNS}) VALUES (?, ?, ?, 1, ?)
ON CONFLICT (tag_id) DO UPDATE SET
    last_printed_at = excluded.last_printed_at,
    times = times + 1,
    printed_by = excluded.printed_by
"""


class SqliteStore:
    name = "sqlite"

    def __init__(self, path: Path | str):
        self.path = Path(path)

    @contextmanager
    def _tx(self, write: bool = False):
        conn = None
        try:
            # isolation_level=None: transactions are opened and closed here, not by the driver
            conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield conn
            conn.execute("COMMIT")
        except sqlite3.Error as exc:
            raise StoreError(500, f"sqlite: {exc}") from exc
        finally:
            if conn is not None:
                # closing with a transaction still open rolls it back
                conn.close()

    # --- lifecycle ------------------------------------------------------------

    def bootstrap(self) -> None:
        self.apply_schema()
        samples.seed_best_effort(self)

    def apply_schema(self, force: bool = False) -> bool:
        """Every statement is idempotent, so there is no version to gate on and nothing to force."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise StoreError(500, f"sqlite: cannot create {self.path.parent}: {exc}") from exc
        with self._tx(write=True) as conn:
            for statement in SCHEMA.split(";"):
                if statement.strip():
                    conn.execute(statement)
            if "tag_id" not in {row["name"] for row in conn.execute("PRAGMA table_info(photos)")}:
                conn.execute(PHOTO_TAG_COLUMN)
            for trigger in TRIGGERS:
                conn.execute(trigger)
            conn.execute(FILL_PHOTO_TAGS)
        return True

    def health(self) -> tuple[bool, str]:
        try:
            with self._tx() as conn:
                conn.execute("SELECT 1 FROM boxes LIMIT 1").fetchone()
        except StoreError as exc:
            return False, exc.detail
        return True, "ok"

    # --- reads ----------------------------------------------------------------

    @staticmethod
    def _items(conn: sqlite3.Connection, box_id: str | None = None) -> list[dict]:
        where, args = ("WHERE box_id = ?", (box_id,)) if box_id else ("", ())
        rows = conn.execute(f"SELECT {ITEM_COLUMNS} FROM items {where} ORDER BY created_at, id", args)
        return [dict(row) for row in rows]

    @staticmethod
    def _photos(conn: sqlite3.Connection, column: str = "", value: str = "") -> list[dict]:
        # metadata only: the image bytes are read one photo at a time
        where, args = (f"WHERE {column} = ?", (value,)) if column else ("", ())
        rows = conn.execute(f"SELECT {PHOTO_COLUMNS} FROM photos {where} ORDER BY created_at, id", args)
        return [dict(row) for row in rows]

    def _box(self, conn: sqlite3.Connection, column: str, value: int | str) -> dict | None:
        row = conn.execute(f"SELECT {BOX_COLUMNS} FROM boxes WHERE {column} = ?", (value,)).fetchone()
        if row is None:
            return None
        box = {**dict(row), "items": self._items(conn, row["id"])}
        return attach_photos([box], self._photos(conn, "box_id", row["id"]))[0]

    def list_boxes(self) -> list[dict]:
        with self._tx() as conn:
            boxes = [dict(row) for row in conn.execute(f"SELECT {BOX_COLUMNS} FROM boxes ORDER BY tag_id")]
            by_box: dict[str, list[dict]] = {}
            for item in self._items(conn):
                by_box.setdefault(item["box_id"], []).append(item)
            photos = self._photos(conn)
        return attach_photos([{**box, "items": by_box.get(box["id"], [])} for box in boxes], photos)

    def list_tag_ids(self) -> list[int]:
        # which tags have a box, without reading what is in them
        with self._tx() as conn:
            return [row["tag_id"] for row in conn.execute("SELECT tag_id FROM boxes ORDER BY tag_id")]

    def get_box(self, tag_id: int) -> dict | None:
        with self._tx() as conn:
            return self._box(conn, "tag_id", tag_id)

    def get_box_by_id(self, box_id: str) -> dict | None:
        clean_id = clean_uuid(box_id)
        if clean_id is None:
            return None
        with self._tx() as conn:
            return self._box(conn, "id", clean_id)

    # --- boxes ----------------------------------------------------------------

    @staticmethod
    def _ensure_box(conn: sqlite3.Connection, tag_id: int, actor: str, now: str) -> str:
        """Id of the box on this tag, claiming the tag with an empty box when it is free."""
        row = conn.execute("SELECT id FROM boxes WHERE tag_id = ?", (tag_id,)).fetchone()
        if row is not None:
            return row["id"]
        box_id = new_id()
        conn.execute(
            "INSERT INTO boxes (id, tag_id, created_at, updated_at, updated_by) VALUES (?, ?, ?, ?, ?)",
            (box_id, tag_id, now, now, actor),
        )
        return box_id

    @staticmethod
    def _update(conn: sqlite3.Connection, table: str, row_id: str, values: dict) -> None:
        # column names come from the fixed field tuples, never from the request
        assignments = ", ".join(f"{column} = ?" for column in values)
        conn.execute(f"UPDATE {table} SET {assignments} WHERE id = ?", (*values.values(), row_id))

    def upsert_box(self, tag_id: int, fields: dict, actor: str) -> dict:
        now = utc_now()
        with self._tx(write=True) as conn:
            box_id = self._ensure_box(conn, tag_id, actor, now)
            values = {**pick(fields, BOX_FIELDS), "updated_at": now, "updated_by": actor}
            self._update(conn, "boxes", box_id, values)
            box = self._box(conn, "tag_id", tag_id)
        assert box is not None
        return box

    def delete_box(self, tag_id: int) -> bool:
        with self._tx(write=True) as conn:
            # items and photos go with it through ON DELETE CASCADE, and the trigger keeps each photo
            return conn.execute("DELETE FROM boxes WHERE tag_id = ?", (tag_id,)).rowcount > 0

    # --- items ----------------------------------------------------------------

    def _item(self, conn: sqlite3.Connection, item_id: str) -> dict | None:
        row = conn.execute(f"SELECT {ITEM_COLUMNS} FROM items WHERE id = ?", (item_id,)).fetchone()
        if row is None:
            return None
        return {**dict(row), "photos": self._photos(conn, "item_id", item_id)}

    def get_item(self, item_id: str) -> dict | None:
        clean_id = clean_uuid(item_id)
        if clean_id is None:
            return None
        with self._tx() as conn:
            return self._item(conn, clean_id)

    def add_item(self, tag_id: int, name: str, qty: int, actor: str) -> dict:
        now = utc_now()
        item_id = new_id()
        with self._tx(write=True) as conn:
            box_id = self._ensure_box(conn, tag_id, actor, now)
            conn.execute(
                "INSERT INTO items (id, box_id, name, qty, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (item_id, box_id, name, qty, now, now),
            )
            self._update(conn, "boxes", box_id, {"updated_at": now, "updated_by": actor})
            item = self._item(conn, item_id)
        assert item is not None
        return item

    def update_item(self, item_id: str, fields: dict, actor: str) -> dict | None:
        clean_id = clean_uuid(item_id)
        if clean_id is None:
            return None
        values = pick(fields, ITEM_FIELDS)
        now = utc_now()
        with self._tx(write=True) as conn:
            item = self._item(conn, clean_id)
            if item is None or not values:
                return item
            self._update(conn, "items", clean_id, {**values, "updated_at": now})
            self._update(conn, "boxes", item["box_id"], {"updated_at": now, "updated_by": actor})
            return self._item(conn, clean_id)

    def delete_item(self, item_id: str) -> bool:
        clean_id = clean_uuid(item_id)
        if clean_id is None:
            return False
        with self._tx(write=True) as conn:
            # its photos go with it through ON DELETE CASCADE, and the trigger keeps each one
            return conn.execute("DELETE FROM items WHERE id = ?", (clean_id,)).rowcount > 0

    # --- photos ---------------------------------------------------------------

    def add_photo(self, box_id: str, item_id: str | None, image: dict, actor: str) -> dict:
        now = utc_now()
        photo = {
            "id": new_id(),
            "box_id": box_id,
            "item_id": item_id,
            "width": image["width"],
            "height": image["height"],
            "size": len(image["data"]),
            "created_at": now,
            "created_by": actor,
        }
        with self._tx(write=True) as conn:
            conn.execute(
                f"INSERT INTO photos ({PHOTO_COLUMNS}, data, thumb) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (*photo.values(), image["data"], image["thumb"]),
            )
            self._update(conn, "boxes", box_id, {"updated_at": now, "updated_by": actor})
        return photo

    def get_photo(self, photo_id: str) -> dict | None:
        clean_id = clean_uuid(photo_id)
        if clean_id is None:
            return None
        with self._tx() as conn:
            photos = self._photos(conn, "id", clean_id)
        return photos[0] if photos else None

    def get_photo_data(self, photo_id: str, thumb: bool = False) -> bytes | None:
        clean_id = clean_uuid(photo_id)
        if clean_id is None:
            return None
        column = "thumb" if thumb else "data"
        with self._tx() as conn:
            # a kept photo is served from the same address as before
            for table in ("photos", KEPT_PHOTOS):
                row = conn.execute(f"SELECT {column} FROM {table} WHERE id = ?", (clean_id,)).fetchone()
                if row is not None:
                    return bytes(row[column])
        return None

    def delete_photo(self, photo_id: str) -> bool:
        clean_id = clean_uuid(photo_id)
        if clean_id is None:
            return False
        with self._tx(write=True) as conn:
            # the trigger keeps it
            return conn.execute("DELETE FROM photos WHERE id = ?", (clean_id,)).rowcount > 0

    def get_kept_photo(self, photo_id: str) -> dict | None:
        clean_id = clean_uuid(photo_id)
        if clean_id is None:
            return None
        with self._tx() as conn:
            row = conn.execute(f"SELECT {KEPT_PHOTO_COLUMNS} FROM kept_photos WHERE id = ?", (clean_id,)).fetchone()
        return dict(row) if row is not None else None

    def list_photos_by_ids(self, photo_ids: list[str]) -> list[dict]:
        wanted = clean_uuids(photo_ids)
        found: list[dict] = []
        with self._tx() as conn:
            for table in ("photos", KEPT_PHOTOS):
                for chunk in chunks(wanted, ID_CHUNK):
                    marks = ", ".join("?" * len(chunk))
                    rows = conn.execute(
                        f"SELECT {PHOTO_COLUMNS} FROM {table} WHERE id IN ({marks}) ORDER BY created_at, id", chunk
                    )
                    found.extend({**dict(row), "kept": table == KEPT_PHOTOS} for row in rows)
        return found

    def erase_photo(self, photo_id: str) -> bool:
        clean_id = clean_uuid(photo_id)
        if clean_id is None:
            return False
        with self._tx(write=True) as conn:
            # the one place image bytes are deleted, and only ever those of a kept photo
            return conn.execute("DELETE FROM kept_photos WHERE id = ?", (clean_id,)).rowcount > 0

    # --- history --------------------------------------------------------------

    @staticmethod
    def _entry(row: sqlite3.Row) -> dict:
        return {**dict(row), "changes": json.loads(row["changes"])}

    def add_history(self, entry: dict) -> dict:
        values = {key: entry[key] for key in HISTORY_KEYS}
        with self._tx(write=True) as conn:
            conn.execute(
                f"INSERT INTO history ({HISTORY_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                tuple({**values, "changes": json.dumps(values["changes"])}.values()),
            )
        return values

    def update_history(self, entry_id: str, changes: dict, at: str, box_name: str, item_name: str) -> None:
        values = {"changes": json.dumps(changes), "at": at, "box_name": box_name, "item_name": item_name}
        with self._tx(write=True) as conn:
            self._update(conn, "history", entry_id, values)

    def delete_history(self, entry_id: str) -> bool:
        with self._tx(write=True) as conn:
            return conn.execute("DELETE FROM history WHERE id = ?", (entry_id,)).rowcount > 0

    def list_history(
        self, tag_id: int | None, limit: int, before: str | None, include_tests: bool = False
    ) -> list[dict]:
        clauses, args = [], []
        if tag_id is not None:
            clauses.append("tag_id = ?")
            args.append(tag_id)
        if not include_tests:
            clauses.append("tag_id >= 0")
        if before is not None:
            clauses.append("at < ?")
            args.append(before)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._tx() as conn:
            rows = conn.execute(
                f"SELECT {HISTORY_COLUMNS} FROM history {where} ORDER BY at DESC, id DESC LIMIT ?", (*args, limit)
            )
            return [self._entry(row) for row in rows]

    # --- printed labels -------------------------------------------------------

    def list_printed(self) -> list[dict]:
        with self._tx() as conn:
            return [dict(row) for row in conn.execute(f"SELECT {PRINTED_COLUMNS} FROM printed_tags ORDER BY tag_id")]

    def record_printed(self, tag_ids: list[int], actor: str) -> None:
        now = utc_now()
        with self._tx(write=True) as conn:
            conn.executemany(RECORD_PRINTED, [(tag_id, now, now, actor) for tag_id in once_each(tag_ids)])

    def forget_printed(self, tag_id: int) -> bool:
        with self._tx(write=True) as conn:
            return conn.execute("DELETE FROM printed_tags WHERE tag_id = ?", (tag_id,)).rowcount > 0

    # --- meta -----------------------------------------------------------------

    def get_meta(self, key: str) -> str | None:
        with self._tx() as conn:
            row = conn.execute("SELECT value FROM app_meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row is not None else None

    def set_meta(self, key: str, value: str) -> None:
        with self._tx(write=True) as conn:
            conn.execute(
                "INSERT INTO app_meta (key, value) VALUES (?, ?) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
