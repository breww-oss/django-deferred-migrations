from unittest import mock

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.db import connection
from pytest_django import Settings

from deferred_migrations.locking import ddl_lock_timeout
from deferred_migrations.locking import is_concurrent_statement
from deferred_migrations.locking import is_data_statement
from deferred_migrations.locking import lock_retries_setting


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("UPDATE sales_invoice SET x = 1", True),
        ("  -- backfill\n  update sales_invoice set x = 1", True),
        ("/* note */ (SELECT 1)", True),
        ("WITH moved AS (DELETE FROM a RETURNING *) INSERT INTO b SELECT * FROM moved", True),
        ("INSERT INTO a VALUES (1) ON CONFLICT DO NOTHING", True),
        ("MERGE INTO a USING b ON a.id = b.id WHEN MATCHED THEN DELETE", True),
        ('ALTER TABLE "a" DROP COLUMN "b"', False),
        ('SET CONSTRAINTS "fk" IMMEDIATE; ALTER TABLE "a" DROP CONSTRAINT "fk"', False),
        ("DO $$ BEGIN PERFORM 1; END $$", False),
        ("CALL do_things()", False),
        ("", False),
    ],
)
def test_is_data_statement(sql: str, expected: bool) -> None:
    assert is_data_statement(sql) is expected


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ('CREATE INDEX CONCURRENTLY "i" ON "t" ("c")', True),
        ('CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS "i" ON "t" ("c")', True),
        ('DROP INDEX CONCURRENTLY IF EXISTS "i"', True),
        ('  -- rebuild\n  reindex index concurrently "i"', True),
        ('REINDEX (VERBOSE) TABLE CONCURRENTLY "t"', True),
        ('REFRESH MATERIALIZED VIEW CONCURRENTLY "v"', True),
        ('ALTER TABLE "t" DROP COLUMN "concurrently"', False),
        ('ALTER TABLE "t" ADD COLUMN "concurrently_checked" boolean', False),
        ('CREATE INDEX "i" ON "t" ("c")', False),
        ('DROP INDEX "i" /* CONCURRENTLY */', False),
        ("", False),
    ],
)
def test_is_concurrent_statement(sql: str, expected: bool) -> None:
    assert is_concurrent_statement(sql) is expected


@pytest.mark.parametrize("value", [0, -1])
def test_lock_retries_setting_rejects_non_positive_values(settings: Settings, value: int) -> None:
    settings.DEFERRED_MIGRATIONS_LOCK_RETRIES = value

    with pytest.raises(ImproperlyConfigured):
        lock_retries_setting()


@pytest.mark.django_db
def test_ddl_inside_atomic_block_runs_with_the_timeout_and_restores_it(settings: Settings) -> None:
    settings.DEFERRED_MIGRATIONS_DDL_LOCK_TIMEOUT = "1234ms"

    with ddl_lock_timeout(connection), connection.schema_editor() as editor:
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_setting('lock_timeout')")
            before = cursor.fetchone()[0]

        editor.execute("CREATE TABLE dm_lock_probe AS SELECT current_setting('lock_timeout') AS value")

        with connection.cursor() as cursor:
            cursor.execute("SELECT value FROM dm_lock_probe")
            during = cursor.fetchone()[0]
            cursor.execute("SELECT current_setting('lock_timeout')")
            after = cursor.fetchone()[0]

    assert during == "1234ms"
    assert after == before


@pytest.mark.django_db
def test_an_identifier_containing_the_word_concurrently_keeps_the_ddl_timeout(settings: Settings) -> None:
    settings.DEFERRED_MIGRATIONS_DDL_LOCK_TIMEOUT = "1234ms"

    with ddl_lock_timeout(connection), connection.schema_editor() as editor:
        editor.execute("CREATE TABLE dm_lock_probe AS SELECT current_setting('lock_timeout') AS concurrently_checked")
        editor.execute("""ALTER TABLE dm_lock_probe ADD COLUMN "concurrently" text DEFAULT current_setting('lock_timeout')""")

        with connection.cursor() as cursor:
            cursor.execute('SELECT concurrently_checked, "concurrently" FROM dm_lock_probe')
            assert cursor.fetchone() == ("1234ms", "1234ms")


@pytest.mark.django_db
def test_data_statements_keep_the_session_timeout(settings: Settings) -> None:
    settings.DEFERRED_MIGRATIONS_DDL_LOCK_TIMEOUT = "1234ms"

    with connection.cursor() as cursor:
        cursor.execute("CREATE TABLE dm_lock_probe (value text)")

    with ddl_lock_timeout(connection), connection.schema_editor() as editor:
        editor.execute("INSERT INTO dm_lock_probe SELECT current_setting('lock_timeout')")

    with connection.cursor() as cursor:
        cursor.execute("SELECT value FROM dm_lock_probe")
        assert cursor.fetchone()[0] != "1234ms"


@pytest.mark.django_db
def test_collect_sql_never_touches_the_session_timeout() -> None:
    with mock.patch("deferred_migrations.locking.set_lock_timeout") as mock_set, ddl_lock_timeout(connection), connection.schema_editor(collect_sql=True, atomic=False) as editor:
        editor.execute('ALTER TABLE "a" DROP COLUMN "b"')

    mock_set.assert_not_called()
    assert editor.collected_sql == ['ALTER TABLE "a" DROP COLUMN "b";']
