"""SQLite store: one file, stdlib only.

Every call opens its own connection, so it is safe under the FastAPI
threadpool. Writes take the database lock up front (BEGIN IMMEDIATE), which
keeps each read-then-write sequence atomic.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .base import (
    BOX_FIELDS,
    ITEM_FIELDS,
    StoreError,
    clean_uuid,
    new_id,
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
"""

BOX_COLUMNS = "id, tag_id, name, location, notes, created_at, updated_at, updated_by"
ITEM_COLUMNS = "id, box_id, name, qty, created_at, updated_at"


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
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise StoreError(500, f"sqlite: cannot create {self.path.parent}: {exc}") from exc
        with self._tx(write=True) as conn:
            for statement in SCHEMA.split(";"):
                if statement.strip():
                    conn.execute(statement)

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

    def _box(self, conn: sqlite3.Connection, tag_id: int) -> dict | None:
        row = conn.execute(f"SELECT {BOX_COLUMNS} FROM boxes WHERE tag_id = ?", (tag_id,)).fetchone()
        if row is None:
            return None
        return {**dict(row), "items": self._items(conn, row["id"])}

    def list_boxes(self) -> list[dict]:
        with self._tx() as conn:
            boxes = [dict(row) for row in conn.execute(f"SELECT {BOX_COLUMNS} FROM boxes ORDER BY tag_id")]
            by_box: dict[str, list[dict]] = {}
            for item in self._items(conn):
                by_box.setdefault(item["box_id"], []).append(item)
        return [{**box, "items": by_box.get(box["id"], [])} for box in boxes]

    def get_box(self, tag_id: int) -> dict | None:
        with self._tx() as conn:
            return self._box(conn, tag_id)

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
            box = self._box(conn, tag_id)
        assert box is not None
        return box

    def delete_box(self, tag_id: int) -> bool:
        with self._tx(write=True) as conn:
            # items go with it through ON DELETE CASCADE
            return conn.execute("DELETE FROM boxes WHERE tag_id = ?", (tag_id,)).rowcount > 0

    # --- items ----------------------------------------------------------------

    @staticmethod
    def _item(conn: sqlite3.Connection, item_id: str) -> dict | None:
        row = conn.execute(f"SELECT {ITEM_COLUMNS} FROM items WHERE id = ?", (item_id,)).fetchone()
        return dict(row) if row is not None else None

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
            return conn.execute("DELETE FROM items WHERE id = ?", (clean_id,)).rowcount > 0

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
