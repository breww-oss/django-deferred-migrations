import threading
import time

import psycopg
import pytest
from django.db import OperationalError
from django.db import connection
from django.db import transaction
from pytest_django import Settings

from deferred_migrations.locking import LockRetriesExhausted
from deferred_migrations.locking import ddl_lock_timeout
from deferred_migrations.locking import is_lock_timeout
from tests.migration_helpers import column_names


@pytest.fixture
def locked_probe_table() -> str:
    with connection.cursor() as cursor:
        cursor.execute("CREATE TABLE IF NOT EXISTS dm_lock_probe (id integer, note text)")
        cursor.execute("INSERT INTO dm_lock_probe VALUES (1, 'x')")

    return "dm_lock_probe"


def hold_lock(sql: str, release_after_seconds: float) -> threading.Thread:
    ready = threading.Event()

    def run() -> None:
        with psycopg.connect(**connection.get_connection_params()) as other:
            with other.cursor() as cursor:
                cursor.execute(sql)
                ready.set()
                threading.Event().wait(release_after_seconds)

            other.rollback()

    thread = threading.Thread(target=run)
    thread.start()
    assert ready.wait(5)
    return thread


@pytest.mark.django_db(transaction=True)
def test_atomic_ddl_fails_fast_with_the_original_lock_error(settings: Settings, locked_probe_table: str) -> None:
    settings.DEFERRED_MIGRATIONS_DDL_LOCK_TIMEOUT = "100ms"
    holder = hold_lock(f"LOCK TABLE {locked_probe_table} IN ACCESS SHARE MODE", 2)

    with pytest.raises(OperationalError) as caught, ddl_lock_timeout(connection), connection.schema_editor() as editor:
        editor.execute(f"ALTER TABLE {locked_probe_table} ADD COLUMN extra integer")

    holder.join()
    assert is_lock_timeout(caught.value)


@pytest.mark.django_db(transaction=True)
def test_non_atomic_ddl_retries_in_place_until_the_lock_is_released(settings: Settings, locked_probe_table: str) -> None:
    settings.DEFERRED_MIGRATIONS_DDL_LOCK_TIMEOUT = "100ms"
    settings.DEFERRED_MIGRATIONS_LOCK_RETRIES = 50
    delays: list[float] = []
    holder = hold_lock(f"LOCK TABLE {locked_probe_table} IN ACCESS SHARE MODE", 1)

    with ddl_lock_timeout(connection, sleep=lambda seconds: (delays.append(seconds), threading.Event().wait(0.1))), connection.schema_editor(atomic=False) as editor:
        editor.execute(f"ALTER TABLE {locked_probe_table} ADD COLUMN extra integer")

    holder.join()
    assert delays
    assert "extra" in column_names(locked_probe_table)


@pytest.mark.django_db(transaction=True)
def test_non_atomic_ddl_raises_when_retries_are_exhausted(settings: Settings, locked_probe_table: str) -> None:
    settings.DEFERRED_MIGRATIONS_DDL_LOCK_TIMEOUT = "50ms"
    settings.DEFERRED_MIGRATIONS_LOCK_RETRIES = 2
    holder = hold_lock(f"LOCK TABLE {locked_probe_table} IN ACCESS SHARE MODE", 2)

    with pytest.raises(LockRetriesExhausted), ddl_lock_timeout(connection, sleep=lambda seconds: None), connection.schema_editor(atomic=False) as editor:
        editor.execute(f"ALTER TABLE {locked_probe_table} ADD COLUMN extra integer")

    holder.join()


@pytest.mark.django_db(transaction=True)
def test_data_statements_wait_on_row_locks_beyond_the_ddl_timeout(settings: Settings, locked_probe_table: str) -> None:
    settings.DEFERRED_MIGRATIONS_DDL_LOCK_TIMEOUT = "50ms"
    holder = hold_lock(f"SELECT * FROM {locked_probe_table} WHERE id = 1 FOR UPDATE", 0.5)
    started = time.monotonic()

    with ddl_lock_timeout(connection, sleep=lambda seconds: pytest.fail("data statement was retried instead of waiting on the row lock")), connection.schema_editor(atomic=False) as editor:
        editor.execute(f"UPDATE {locked_probe_table} SET note = 'y' WHERE id = 1")

    elapsed = time.monotonic() - started
    holder.join()
    assert elapsed >= 0.4


@pytest.mark.django_db(transaction=True)
def test_concurrently_statements_have_no_timeout(settings: Settings, locked_probe_table: str) -> None:
    settings.DEFERRED_MIGRATIONS_DDL_LOCK_TIMEOUT = "50ms"
    holder = hold_lock(f"UPDATE {locked_probe_table} SET note = 'locked' WHERE id = 1", 0.5)
    started = time.monotonic()

    with ddl_lock_timeout(connection, sleep=lambda seconds: pytest.fail("CONCURRENTLY statement was retried instead of waiting without a timeout")), connection.schema_editor(atomic=False) as editor:
        editor.execute(f"CREATE INDEX CONCURRENTLY dm_lock_probe_note ON {locked_probe_table} (note)")

    elapsed = time.monotonic() - started
    holder.join()
    assert elapsed >= 0.4


@pytest.mark.django_db(transaction=True)
def test_django_fk_drop_sql_is_subject_to_the_timeout(settings: Settings) -> None:
    settings.DEFERRED_MIGRATIONS_DDL_LOCK_TIMEOUT = "100ms"

    with connection.cursor() as cursor:
        cursor.execute("CREATE TABLE dm_fk_parent (id integer PRIMARY KEY)")
        cursor.execute("CREATE TABLE dm_fk_child (id integer, parent_id integer CONSTRAINT dm_fk_child_parent_fk REFERENCES dm_fk_parent (id) DEFERRABLE INITIALLY DEFERRED)")

    holder = hold_lock("LOCK TABLE dm_fk_child IN ACCESS SHARE MODE", 2)

    with pytest.raises(OperationalError) as caught, ddl_lock_timeout(connection), transaction.atomic(), connection.schema_editor() as editor:
        editor.execute('SET CONSTRAINTS "dm_fk_child_parent_fk" IMMEDIATE; ALTER TABLE "dm_fk_child" DROP CONSTRAINT "dm_fk_child_parent_fk"')

    holder.join()
    assert is_lock_timeout(caught.value)
