"""Store selection: SCANNAGE_STORE picks one, and only that one is ever built."""

from __future__ import annotations

import threading

from .. import config
from .base import Store, StoreError
from .postgrest import PostgrestStore
from .sqlite import SqliteStore

__all__ = ["Store", "StoreError", "get_store", "set_store"]

_store: Store | None = None
_lock = threading.Lock()


def _build() -> Store:
    if config.STORE == "postgrest":
        return PostgrestStore()
    return SqliteStore(config.SQLITE_PATH)


def get_store() -> Store:
    global _store
    with _lock:
        if _store is None:
            _store = _build()
        return _store


def set_store(store: Store | None) -> None:
    """Swap the singleton. Tests point the app at a throwaway database with this."""
    global _store
    with _lock:
        _store = store
