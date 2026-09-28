"""Real-database tests: hit the actual Postgres, red if unreachable.

Runs the same version-gated bootstrap the app runs at startup (a no-op once
converged) and checks what it leaves behind. Reads only; no rows are written.
A database still at an older version is brought up to this one by that same
bootstrap, in place.
"""

import psycopg
import pytest

from app import bootstrap, config

TABLES = ("boxes", "items", "app_meta", "history", "photos")
ROLE = "scannage_user"


def _columns(cur, table: str) -> dict[str, str]:
    cur.execute(
        """SELECT column_name, data_type FROM information_schema.columns
           WHERE table_schema = 'scannage' AND table_name = %s""",
        (table,),
    )
    return dict(cur.fetchall())


def _foreign_keys(cur, table: str) -> dict[str, tuple[str, str]]:
    """Constraint name to (the table it points at, what happens on delete)."""
    cur.execute(
        """SELECT conname, confrelid::regclass::text, confdeltype FROM pg_constraint
           WHERE conrelid = %s::regclass AND contype = 'f'""",
        (f"scannage.{table}",),
    )
    found = {}
    for name, target, on_delete in cur.fetchall():
        found[name] = (target, on_delete.decode() if isinstance(on_delete, bytes) else on_delete)
    return found


def _indexes(cur, table: str) -> dict[str, str]:
    cur.execute("SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'scannage' AND tablename = %s", (table,))
    return dict(cur.fetchall())


@pytest.fixture(scope="module")
def cur():
    ok, detail = bootstrap.db_reachable()
    assert ok, f"database unreachable, this test must be red, not skipped: {detail}"
    bootstrap.apply_schema()
    with psycopg.connect(config.superuser_dsn()) as conn, conn.cursor() as cur:
        yield cur


def test_tables_exist(cur):
    cur.execute(
        """SELECT table_name FROM information_schema.tables
           WHERE table_schema = 'scannage' AND table_name = ANY(%s)""",
        (list(TABLES),),
    )
    assert sorted(row[0] for row in cur.fetchall()) == sorted(TABLES)


def test_role_exists_and_cannot_log_in(cur):
    cur.execute("SELECT rolcanlogin FROM pg_roles WHERE rolname = %s", (ROLE,))
    row = cur.fetchone()
    assert row is not None, f"role {ROLE} missing"
    assert row[0] is False


def test_role_is_granted_to_the_authenticator(cur):
    cur.execute("SELECT pg_has_role('postgrest_authenticator', %s, 'MEMBER')", (ROLE,))
    assert cur.fetchone()[0] is True


def test_role_can_use_the_tables(cur):
    cur.execute("SELECT has_schema_privilege(%s, 'scannage', 'USAGE')", (ROLE,))
    assert cur.fetchone()[0] is True
    for table in TABLES:
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            cur.execute("SELECT has_table_privilege(%s, %s, %s)", (ROLE, f"scannage.{table}", privilege))
            assert cur.fetchone()[0] is True, f"{ROLE} lacks {privilege} on {table}"


def test_nothing_is_granted_to_web_anon(cur):
    cur.execute(
        """SELECT table_name, privilege_type FROM information_schema.role_table_grants
           WHERE table_schema = 'scannage' AND grantee = 'web_anon'"""
    )
    assert cur.fetchall() == []


def test_items_cascade_with_their_box(cur):
    cur.execute(
        """SELECT confdeltype FROM pg_constraint
           WHERE conrelid = 'scannage.items'::regclass AND confrelid = 'scannage.boxes'::regclass
             AND contype = 'f'"""
    )
    rows = cur.fetchall()
    assert rows, "items has no foreign key to boxes"
    assert rows[0][0] in ("c", b"c"), "items must be removed with their box"


def test_items_foreign_key_has_the_name_the_store_embeds_by(cur):
    assert _foreign_keys(cur, "items") == {"items_box_id_fkey": ("scannage.boxes", "c")}


def test_history_columns(cur):
    assert _columns(cur, "history") == {
        "id": "uuid",
        "at": "timestamp with time zone",
        "actor": "text",
        "action": "text",
        "tag_id": "integer",
        "box_id": "uuid",
        "box_name": "text",
        "item_id": "uuid",
        "item_name": "text",
        "changes": "jsonb",
    }


def test_history_references_nothing_so_it_outlives_its_box(cur):
    assert _foreign_keys(cur, "history") == {}


def test_history_indexes(cur):
    indexes = _indexes(cur, "history")

    assert "(tag_id, at DESC)" in indexes["history_tag_id_at_idx"]
    assert "(at DESC)" in indexes["history_at_idx"]


def test_photo_columns(cur):
    assert _columns(cur, "photos") == {
        "id": "uuid",
        "box_id": "uuid",
        "item_id": "uuid",
        "width": "integer",
        "height": "integer",
        "size": "integer",
        "data": "bytea",
        "thumb": "bytea",
        "created_at": "timestamp with time zone",
        "created_by": "text",
    }
    cur.execute(
        """SELECT column_name FROM information_schema.columns
           WHERE table_schema = 'scannage' AND table_name = 'photos' AND is_nullable = 'YES'"""
    )
    assert [row[0] for row in cur.fetchall()] == ["item_id"]


def test_photos_cascade_with_their_box_and_their_item(cur):
    assert _foreign_keys(cur, "photos") == {
        "photos_box_id_fkey": ("scannage.boxes", "c"),
        "photos_item_id_fkey": ("scannage.items", "c"),
    }


def test_photo_indexes(cur):
    indexes = _indexes(cur, "photos")

    assert "(box_id)" in indexes["photos_box_id_idx"]
    assert "(item_id)" in indexes["photos_item_id_idx"]


def test_bytea_is_sent_as_hex(cur):
    # the store reads photos through JSON and expects the hex form
    cur.execute("SELECT current_setting('bytea_output')")
    assert cur.fetchone() == ("hex",)


def test_schema_version_is_stamped(cur):
    assert bootstrap.SCHEMA_VERSION == 2
    cur.execute("SELECT version FROM scannage.deploy_meta WHERE id = 1")
    assert cur.fetchone() == (bootstrap.SCHEMA_VERSION,)


def test_bootstrap_is_a_no_op_once_converged(cur):
    assert bootstrap.apply_schema() is False
