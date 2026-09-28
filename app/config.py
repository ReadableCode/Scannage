"""Environment resolution.

Runs in two places with the same code (no docker-only paths):
  - container: env injected by the compose file
  - bare local run: .env in the repo root
"""

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    env_path = REPO_ROOT / ".env"
    if not env_path.is_file():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")


_load_dotenv()


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip().strip('"').strip("'")


def _flag(name: str) -> bool:
    return _env(name).lower() in ("1", "true", "yes", "on")


DICTIONARY = "ARUCO_MIP_36h12"
TAG_COUNT = 250
PORT = 8791

STORES = ("sqlite", "postgrest")

# An explicit choice, never a fallback: a misconfigured postgrest store fails
# at startup instead of quietly writing to a local file.
STORE = (_env("SCANNAGE_STORE") or "sqlite").lower()
if STORE not in STORES:
    raise ValueError(f"SCANNAGE_STORE must be one of {', '.join(STORES)}, got {STORE!r}")

SQLITE_PATH = Path(_env("SCANNAGE_SQLITE_PATH") or REPO_ROOT / "data" / "scannage.db")

# Empty means "derive it from each request".
BASE_URL = _env("SCANNAGE_BASE_URL").rstrip("/")

SEED_SAMPLES = _flag("SCANNAGE_SEED_SAMPLES")

APP_SCHEMA = _env("APP_SCHEMA") or "scannage"

POSTGRES_URL = _env("POSTGRES_URL")
POSTGRES_PORT = _env("POSTGRES_PORT") or "5432"
POSTGRES_DB = _env("POSTGRES_DB") or "apps"
POSTGRES_USER = _env("POSTGRES_USER")
POSTGRES_PASSWORD = _env("POSTGRES_PASSWORD")

POSTGREST_URL = _env("POSTGREST_URL").rstrip("/")
JWT_SECRET = _env("POSTGREST_JWT_SECRET") or _env("JWT_SECRET")

JWT_TTL_SECONDS = 600
HTTP_TIMEOUT = 10.0


def superuser_dsn() -> str:
    return (
        f"host={POSTGRES_URL} port={POSTGRES_PORT} dbname={POSTGRES_DB} "
        f"user={POSTGRES_USER} password={POSTGRES_PASSWORD} connect_timeout=5"
    )


def db_configured() -> bool:
    return bool(POSTGRES_URL and POSTGRES_USER and POSTGRES_PASSWORD)
