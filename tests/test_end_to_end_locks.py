import threading
from collections.abc import Iterator
from contextlib import contextmanager
from io import StringIO

import psycopg
import pytest
from django.core.management import CommandError
from django.core.management import call_command
from django.db import connection
from django.db.migrations.recorder import MigrationRecorder
from pytest_django import Settings

from deferred_migrations.models import DeferredOperation
from tests.migration_helpers import column_names


@contextmanager
def access_share_lock_on_child() -> Iterator[None]:
    ready = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with psycopg.connect(**connection.get_connection_params()) as other:
            other.execute("LOCK TABLE deferred_migrations_testapp_child IN ACCESS SHARE MODE")
            ready.set()
            release.wait(30)
            other.rollback()

    holder = threading.Thread(target=hold)
    holder.start()
    ready.wait(5)

    try:
        yield
    finally:
        release.set()
        holder.join()


def use_fast_lock_retries(settings: Settings, retries: int) -> None:
    settings.DEFERRED_MIGRATIONS_DDL_LOCK_TIMEOUT = "100ms"
    settings.DEFERRED_MIGRATIONS_RETRY_BASE_SECONDS = 0.05
    settings.DEFERRED_MIGRATIONS_RETRY_MAX_SECONDS = 0.1
    settings.DEFERRED_MIGRATIONS_LOCK_RETRIES = retries


@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("test_app")
def test_atomic_migration_is_retried_after_a_competing_lock_is_released(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    settings.DEFERRED_MIGRATIONS_DDL_LOCK_TIMEOUT = "100ms"
    settings.DEFERRED_MIGRATIONS_LOCK_RETRIES = 3
    call_command("migrate", "deferred_migrations_testapp", "0001", verbosity=0)
    ready = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with psycopg.connect(**connection.get_connection_params()) as other:
            other.execute("LOCK TABLE deferred_migrations_testapp_child IN ACCESS SHARE MODE")
            ready.set()
            release.wait(30)
            other.rollback()

    holder = threading.Thread(target=hold)

    # Release the lock only once migrate has actually timed out on it, so the test does not depend on how long the command takes to reach the DDL.
    def release_and_retry_at_once(attempt: int) -> float:
        release.set()
        holder.join(10)
        return 0

    monkeypatch.setattr("deferred_migrations.management.commands.migrate_pre_deploy.retry_delay_seconds", release_and_retry_at_once)
    holder.start()
    ready.wait(5)
    output = StringIO()

    try:
        call_command("migrate_pre_deploy", stdout=output)
    finally:
        release.set()
        holder.join(10)

    assert "retrying migrate (attempt 2 of 3)" in output.getvalue()
    assert "extra" in column_names("deferred_migrations_testapp_child")


@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("test_app")
def test_atomic_migration_gives_up_after_the_last_retry_with_nothing_applied(settings: Settings) -> None:
    use_fast_lock_retries(settings, retries=2)
    call_command("migrate", "deferred_migrations_testapp", "0001", verbosity=0)

    with access_share_lock_on_child(), pytest.raises(CommandError, match="Gave up after 2 attempts"):
        call_command("migrate_pre_deploy", stdout=StringIO())

    assert not MigrationRecorder(connection).migration_qs.filter(app="deferred_migrations_testapp", name="0002_remove_child_legacy").exists()
    assert not DeferredOperation.objects.filter(app_label="deferred_migrations_testapp", migration_name="0002_remove_child_legacy").exists()
    assert "legacy" in column_names("deferred_migrations_testapp_child")


@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("nonatomic_test_app")
def test_non_atomic_migration_that_exhausts_statement_retries_is_not_rerun(settings: Settings) -> None:
    use_fast_lock_retries(settings, retries=2)
    call_command("migrate", "deferred_migrations_testapp", "0001", verbosity=0)

    with access_share_lock_on_child(), pytest.raises(CommandError, match="non-atomic migration could not acquire a lock"):
        call_command("migrate_pre_deploy", stdout=StringIO())

    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM deferred_migrations_testapp_migrationrun")
        assert cursor.fetchone()[0] == 1


@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("test_app")
def test_interrupted_migration_that_was_edited_requeues_its_real_statement() -> None:
    DeferredOperation.objects.create(
        app_label="deferred_migrations_testapp", migration_name="0002_remove_child_legacy", operation_index=0, sequence=0, kind=DeferredOperation.Kind.DROP_COLUMN, table_name="deferred_migrations_testapp_child", column_name="old_edit", sql="SELECT 'old_edit'", status=DeferredOperation.Status.FAILED
    )

    call_command("migrate_pre_deploy", stdout=StringIO())

    row = DeferredOperation.objects.get(app_label="deferred_migrations_testapp", migration_name="0002_remove_child_legacy", operation_index=0, sequence=0)
    assert row.status == DeferredOperation.Status.PENDING
    assert row.column_name == "legacy"
    assert "legacy" in row.sql
