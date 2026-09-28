"""Real-database tests: hit the actual Postgres, red if unreachable.

Runs the same version-gated bootstrap the app runs at startup (a no-op once
converged) and checks what it leaves behind. Reads only; no rows are written.
"""

import psycopg
import pytest

from app import bootstrap, config

TABLES = ("boxes", "items", "app_meta")
ROLE = "scannage_user"


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


def test_schema_version_is_stamped(cur):
    cur.execute("SELECT version FROM scannage.deploy_meta WHERE id = 1")
    assert cur.fetchone() == (bootstrap.SCHEMA_VERSION,)


def test_bootstrap_is_a_no_op_once_converged(cur):
    assert bootstrap.apply_schema() is False
