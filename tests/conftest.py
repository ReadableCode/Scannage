"""Pins the settings the local tests depend on, before app.config is imported.

A key already in the environment wins over .env, so a developer's own .env
cannot point the SQLite and API tests at another store or base URL. The
real-database tests build their store directly and are not affected.
"""

import os
import tempfile
from pathlib import Path

os.environ["SCANNAGE_STORE"] = "sqlite"
os.environ["SCANNAGE_SQLITE_PATH"] = str(Path(tempfile.gettempdir()) / "scannage_pytest" / "unused.db")
os.environ["SCANNAGE_BASE_URL"] = ""
os.environ["SCANNAGE_SEED_SAMPLES"] = "0"
