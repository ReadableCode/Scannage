"""App-owned Postgres bootstrap, used by the postgrest store only.

Runs from the FastAPI lifespan (never the docker entrypoint), converges the
scannage schema inside the configured database, and is version-gated via
scannage.deploy_meta so it is a no-op on every boot after the first. Only
additive statements live here and in the SQL files; nothing can touch any
other schema. Failures are logged and the app still serves. The next boot
retries.
"""

from __future__ import annotations

import logging
from pathlib import Path

import psycopg

from . import config

log = logging.getLogger("scannage.bootstrap")

DEPLOY_DIR = Path(__file__).resolve().parent.parent / "deploy"
# A version bump runs every file again, which is safe because each statement
# is idempotent. That is how a database at an older version is brought up in place.
SCHEMA_FILES = ("02_schema.sql", "03_history_photos.sql", "04_kept_photos.sql", "05_printed_tags.sql")
SCHEMA_VERSION = 4

ROLE_SQL = """
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'scannage_user') THEN
        CREATE ROLE scannage_user NOLOGIN;
    END IF;
END $$;
GRANT scannage_user TO postgrest_authenticator;
"""

# One row, keyed, so stamping a version is an upsert and never a delete.
META_SQL = """
CREATE TABLE IF NOT EXISTS scannage.deploy_meta (
    id         integer PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    version    integer NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now()
)
"""

STAMP_SQL = """
INSERT INTO scannage.deploy_meta (id, version) VALUES (1, %s)
ON CONFLICT (id) DO UPDATE SET version = EXCLUDED.version, applied_at = now()
"""


def db_reachable() -> tuple[bool, str]:
    if not config.db_configured():
        return False, "POSTGRES_URL, POSTGRES_USER and POSTGRES_PASSWORD must be set"
    try:
        with psycopg.connect(config.superuser_dsn()) as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
    except psycopg.Error as exc:
        return False, str(exc).strip()
    return True, "ok"


def _applied_version(cur) -> int | None:
    cur.execute(
        """SELECT 1 FROM information_schema.tables
           WHERE table_schema = 'scannage' AND table_name = 'deploy_meta'"""
    )
    if cur.fetchone() is None:
        return None
    cur.execute("SELECT version FROM scannage.deploy_meta WHERE id = 1")
    row = cur.fetchone()
    return row[0] if row else None


def apply_schema(force: bool = False) -> bool:
    """Returns True when something was applied."""
    if not config.db_configured():
        log.warning("POSTGRES_* env missing, skipping schema bootstrap")
        return False
    with psycopg.connect(config.superuser_dsn()) as conn:
        with conn.cursor() as cur:
            if not force and _applied_version(cur) == SCHEMA_VERSION:
                log.info("schema already at version %s, nothing to apply", SCHEMA_VERSION)
                return False
            for role in ("postgrest_authenticator", "web_anon"):
                cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
                if cur.fetchone() is None:
                    raise RuntimeError(
                        f"cluster role {role!r} missing: the PostgREST roles "
                        "postgrest_authenticator and web_anon must exist before this app starts"
                    )
            cur.execute(ROLE_SQL)
            for name in SCHEMA_FILES:
                log.info("applying %s", name)
                cur.execute((DEPLOY_DIR / name).read_text())
            cur.execute(META_SQL)
            cur.execute(STAMP_SQL, (SCHEMA_VERSION,))
        conn.commit()
        # its own transaction: PostgREST must only reload once the schema is committed
        with conn.cursor() as cur:
            cur.execute("NOTIFY pgrst, 'reload schema'")
        conn.commit()
    log.info("schema converged to version %s", SCHEMA_VERSION)
    return True


def bootstrap_best_effort() -> None:
    try:
        apply_schema()
    except Exception:
        log.exception("schema bootstrap failed, serving anyway, will retry next boot")
