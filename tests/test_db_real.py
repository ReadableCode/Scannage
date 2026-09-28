"""Real-database tests: hit the actual Postgres, red if unreachable.

Runs the same version-gated bootstrap the app runs at startup (a no-op once
converged) and checks what it leaves behind. Reads only; no rows are written.
A database still at an older version is brought up to this one by that same
bootstrap, in place.
"""

import psycopg
import pytest

from app import bootstrap, config

TABLES = ("boxes", "items", "app_meta", "history", "photos", "kept_photos", "printed_tags")
ROLE = "scannage_user"
EVERYTHING = ("SELECT", "INSERT", "UPDATE", "DELETE")
# A kept photo is written by the trigger, read, and erased on purpose. It is never changed.
PRIVILEGES = {table: EVERYTHING for table in TABLES} | {"kept_photos": ("SELECT", "INSERT", "DELETE")}

PHOTO_COLUMNS = {
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
    "tag_id": "integer",
}


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


def _nullable(cur, table: str) -> list[str]:
    cur.execute(
        """SELECT column_name FROM information_schema.columns
           WHERE table_schema = 'scannage' AND table_name = %s AND is_nullable = 'YES'""",
        (table,),
    )
    return sorted(row[0] for row in cur.fetchall())


def _triggers(cur, table: str) -> dict[str, tuple[str, str, str, str]]:
    """Trigger name to (when, on what, for each what, what it runs)."""
    cur.execute(
        """SELECT trigger_name, action_timing, event_manipulation, action_orientation, action_statement
           FROM information_schema.triggers
           WHERE event_object_schema = 'scannage' AND event_object_table = %s""",
        (table,),
    )
    return {name: (timing, event, each, statement) for name, timing, event, each, statement in cur.fetchall()}


def _defaults(cur, table: str) -> dict[str, str | None]:
    cur.execute(
        """SELECT column_name, column_default FROM information_schema.columns
           WHERE table_schema = 'scannage' AND table_name = %s""",
        (table,),
    )
    return dict(cur.fetchall())


def _checks(cur, table: str) -> list[str]:
    cur.execute(
        """SELECT pg_get_constraintdef(oid) FROM pg_constraint
           WHERE conrelid = %s::regclass AND contype = 'c'""",
        (f"scannage.{table}",),
    )
    return [row[0] for row in cur.fetchall()]


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
        for privilege in EVERYTHING:
            cur.execute("SELECT has_table_privilege(%s, %s, %s)", (ROLE, f"scannage.{table}", privilege))
            wanted = privilege in PRIVILEGES[table]
            assert cur.fetchone()[0] is wanted, f"{ROLE} must {'have' if wanted else 'not have'} {privilege} on {table}"


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
    assert _columns(cur, "photos") == PHOTO_COLUMNS
    assert _nullable(cur, "photos") == ["item_id", "tag_id"]


def test_photos_cascade_with_their_box_and_their_item(cur):
    assert _foreign_keys(cur, "photos") == {
        "photos_box_id_fkey": ("scannage.boxes", "c"),
        "photos_item_id_fkey": ("scannage.items", "c"),
    }


def test_photo_indexes(cur):
    indexes = _indexes(cur, "photos")

    assert "(box_id)" in indexes["photos_box_id_idx"]
    assert "(item_id)" in indexes["photos_item_id_idx"]


def test_kept_photo_columns(cur):
    assert _columns(cur, "kept_photos") == {**PHOTO_COLUMNS, "removed_at": "timestamp with time zone"}
    assert _nullable(cur, "kept_photos") == ["item_id", "tag_id"]
    assert "(id)" in _indexes(cur, "kept_photos")["kept_photos_pkey"]


def test_kept_photos_reference_nothing_so_no_delete_reaches_them(cur):
    assert _foreign_keys(cur, "kept_photos") == {}
    cur.execute(
        """SELECT conname FROM pg_constraint
           WHERE confrelid = 'scannage.kept_photos'::regclass AND contype = 'f'"""
    )
    assert cur.fetchall() == [], "something references kept_photos"


def test_triggers_on_photos(cur):
    triggers = _triggers(cur, "photos")

    assert sorted(triggers) == ["photos_fill_tag_id", "photos_keep"], "photos must have exactly these two triggers"
    assert triggers["photos_keep"][:3] == ("BEFORE", "DELETE", "ROW"), "a photo must be kept before it is deleted"
    assert "photos_keep()" in triggers["photos_keep"][3]
    assert triggers["photos_fill_tag_id"][:3] == ("BEFORE", "INSERT", "ROW")
    assert "photos_fill_tag_id()" in triggers["photos_fill_tag_id"][3]


def test_triggers_on_photos_are_switched_on(cur):
    cur.execute(
        """SELECT tgname, tgenabled FROM pg_trigger
           WHERE tgrelid = 'scannage.photos'::regclass AND NOT tgisinternal"""
    )
    found = {name: state.decode() if isinstance(state, bytes) else state for name, state in cur.fetchall()}
    assert found == {"photos_fill_tag_id": "O", "photos_keep": "O"}, "O is on, D is off"


def test_nothing_stands_between_a_kept_photo_and_being_erased_on_purpose(cur):
    assert _triggers(cur, "kept_photos") == {}


def test_trigger_functions_run_as_the_role_doing_the_write(cur):
    cur.execute(
        """SELECT proname, prosecdef, prorettype::regtype::text FROM pg_proc
           WHERE pronamespace = 'scannage'::regnamespace AND proname = ANY(%s)""",
        (["photos_fill_tag_id", "photos_keep"],),
    )
    assert sorted(cur.fetchall()) == [("photos_fill_tag_id", False, "trigger"), ("photos_keep", False, "trigger")]


def test_every_photo_carries_the_tag_of_its_box(cur):
    cur.execute(
        """SELECT count(*) FROM scannage.photos AS photo
           LEFT JOIN scannage.boxes AS box ON box.id = photo.box_id
           WHERE photo.tag_id IS DISTINCT FROM box.tag_id"""
    )
    assert cur.fetchone() == (0,), "photos whose tag_id is missing or is not the tag of their box"


def test_printed_tag_columns(cur):
    assert _columns(cur, "printed_tags") == {
        "tag_id": "integer",
        "first_printed_at": "timestamp with time zone",
        "last_printed_at": "timestamp with time zone",
        "times": "integer",
        "printed_by": "text",
    }
    assert _nullable(cur, "printed_tags") == []
    assert "(tag_id)" in _indexes(cur, "printed_tags")["printed_tags_pkey"]


def test_printed_tag_defaults(cur):
    defaults = _defaults(cur, "printed_tags")

    assert defaults["tag_id"] is None, "a tag id is never made up by the database"
    assert defaults["times"] == "1"
    assert defaults["printed_by"] == "''::text"
    assert defaults["first_printed_at"] == defaults["last_printed_at"] == "now()"


def test_a_printed_tag_counts_at_least_one_print(cur):
    checks = _checks(cur, "printed_tags")

    assert len(checks) == 1
    assert "times >= 1" in checks[0]


def test_printed_tags_reference_nothing_so_a_label_needs_no_box(cur):
    assert _foreign_keys(cur, "printed_tags") == {}
    cur.execute(
        """SELECT conname FROM pg_constraint
           WHERE confrelid = 'scannage.printed_tags'::regclass AND contype = 'f'"""
    )
    assert cur.fetchall() == [], "something references printed_tags"
    assert _triggers(cur, "printed_tags") == {}


def test_bytea_is_sent_as_hex(cur):
    # the store reads photos through JSON and expects the hex form
    cur.execute("SELECT current_setting('bytea_output')")
    assert cur.fetchone() == ("hex",)


def test_schema_version_is_stamped(cur):
    assert bootstrap.SCHEMA_VERSION == 4
    cur.execute("SELECT version FROM scannage.deploy_meta WHERE id = 1")
    assert cur.fetchone() == (bootstrap.SCHEMA_VERSION,)


def test_bootstrap_is_a_no_op_once_converged(cur):
    assert bootstrap.apply_schema() is False
