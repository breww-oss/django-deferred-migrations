from datetime import timedelta

import pytest
from django.db import connection
from django.db.migrations.loader import MigrationLoader
from django.utils import timezone

from deferred_migrations.models import DeferredOperation
from deferred_migrations.runner import MigrationKeys
from deferred_migrations.runner import describe_run_result
from deferred_migrations.runner import run_deferred_operations
from tests.migration_helpers import column_names


@pytest.fixture
def scratch_table() -> str:
    with connection.cursor() as cursor:
        cursor.execute("CREATE TABLE dm_runner_scratch (id integer, a integer, b integer, c integer)")

    return "dm_runner_scratch"


def known(*migrations: tuple[str, str]) -> MigrationKeys:
    return MigrationKeys(known=set(migrations), applied=set(migrations))


@pytest.mark.django_db
def test_runs_known_applied_rows_in_id_order(scratch_table: str) -> None:
    # Inserted newest first, and with a statement that only succeeds once the earlier row has freed the name, so an unordered read runs them the wrong way round and fails.
    DeferredOperation.objects.create(pk=2, app_label="dm", migration_name="0003", operation_index=0, sequence=0, kind=DeferredOperation.Kind.DROP_COLUMN, table_name=scratch_table, column_name="b", sql=f"ALTER TABLE {scratch_table} RENAME COLUMN b TO a")
    DeferredOperation.objects.create(pk=1, app_label="dm", migration_name="0002", operation_index=0, sequence=0, kind=DeferredOperation.Kind.DROP_COLUMN, table_name=scratch_table, column_name="a", sql=f"ALTER TABLE {scratch_table} DROP COLUMN a")

    result = run_deferred_operations(migration_keys=known(("dm", "0002"), ("dm", "0003")), sleep=lambda seconds: None)

    assert [row.migration_name for row in result.ran] == ["0002", "0003"]
    assert set(DeferredOperation.objects.values_list("status", flat=True)) == {DeferredOperation.Status.DONE}
    assert column_names(scratch_table) == {"id", "a", "c"}


@pytest.mark.django_db
def test_stops_at_the_first_failure_leaving_later_rows_pending(scratch_table: str) -> None:
    broken = DeferredOperation.objects.create(app_label="dm", migration_name="0002", operation_index=0, sequence=0, kind=DeferredOperation.Kind.DROP_COLUMN, table_name=scratch_table, column_name="missing", sql=f"ALTER TABLE {scratch_table} DROP COLUMN missing")
    later = DeferredOperation.objects.create(app_label="dm", migration_name="0003", operation_index=0, sequence=0, kind=DeferredOperation.Kind.DROP_COLUMN, table_name=scratch_table, column_name="b", sql=f"ALTER TABLE {scratch_table} DROP COLUMN b")

    result = run_deferred_operations(migration_keys=known(("dm", "0002"), ("dm", "0003")), sleep=lambda seconds: None)

    broken.refresh_from_db()
    later.refresh_from_db()
    assert result.failed == broken
    assert (broken.status, broken.attempts) == (DeferredOperation.Status.FAILED, 1)
    assert "missing" in broken.last_error
    assert later.status == DeferredOperation.Status.PENDING


@pytest.mark.django_db
def test_failed_rows_are_retried_on_the_next_run(scratch_table: str) -> None:
    row = DeferredOperation.objects.create(app_label="dm", migration_name="0002", operation_index=0, sequence=0, kind=DeferredOperation.Kind.DROP_COLUMN, table_name=scratch_table, column_name="a", sql=f"ALTER TABLE {scratch_table} DROP COLUMN a", status=DeferredOperation.Status.FAILED, attempts=2)

    run_deferred_operations(migration_keys=known(("dm", "0002")), sleep=lambda seconds: None)

    row.refresh_from_db()
    assert (row.status, row.attempts) == (DeferredOperation.Status.DONE, 3)


@pytest.mark.django_db
def test_rows_from_unknown_or_unapplied_migrations_are_left_alone_and_reported(scratch_table: str) -> None:
    unknown = DeferredOperation.objects.create(app_label="dm", migration_name="0009_newer_release", operation_index=0, sequence=0, kind=DeferredOperation.Kind.DROP_COLUMN, table_name=scratch_table, column_name="a", sql=f"ALTER TABLE {scratch_table} DROP COLUMN a")
    unapplied = DeferredOperation.objects.create(app_label="dm", migration_name="0004", operation_index=0, sequence=0, kind=DeferredOperation.Kind.DROP_COLUMN, table_name=scratch_table, column_name="b", sql=f"ALTER TABLE {scratch_table} DROP COLUMN b")

    result = run_deferred_operations(migration_keys=MigrationKeys(known={("dm", "0004")}, applied=set()), sleep=lambda seconds: None)

    assert result.unknown == [unknown, unapplied]
    assert set(DeferredOperation.objects.values_list("status", flat=True)) == {DeferredOperation.Status.PENDING}


@pytest.mark.django_db
def test_skipped_rows_never_run_and_do_not_block_other_tables(scratch_table: str) -> None:
    DeferredOperation.objects.create(app_label="dm", migration_name="0002", operation_index=0, sequence=0, kind=DeferredOperation.Kind.DROP_COLUMN, table_name="some_other_table", column_name="x", sql="ALTER TABLE some_other_table DROP COLUMN x", status=DeferredOperation.Status.SKIPPED)
    later = DeferredOperation.objects.create(app_label="dm", migration_name="0003", operation_index=0, sequence=0, kind=DeferredOperation.Kind.DROP_COLUMN, table_name=scratch_table, column_name="a", sql=f"ALTER TABLE {scratch_table} DROP COLUMN a")

    run_deferred_operations(migration_keys=known(("dm", "0002"), ("dm", "0003")), sleep=lambda seconds: None)

    later.refresh_from_db()
    assert later.status == DeferredOperation.Status.DONE


@pytest.mark.django_db
def test_a_skipped_trigger_row_blocks_later_rows_on_the_same_table(scratch_table: str) -> None:
    DeferredOperation.objects.create(app_label="dm", migration_name="0002", operation_index=0, sequence=0, kind=DeferredOperation.Kind.DROP_TRIGGER, table_name=scratch_table, column_name="c", sql="SELECT 1", status=DeferredOperation.Status.SKIPPED)
    column_drop = DeferredOperation.objects.create(app_label="dm", migration_name="0003", operation_index=0, sequence=0, kind=DeferredOperation.Kind.DROP_COLUMN, table_name=scratch_table, column_name="a", sql=f"ALTER TABLE {scratch_table} DROP COLUMN a")

    result = run_deferred_operations(migration_keys=known(("dm", "0002"), ("dm", "0003")), sleep=lambda seconds: None)

    column_drop.refresh_from_db()
    assert result.blocked == [column_drop]
    assert column_drop.status == DeferredOperation.Status.PENDING


@pytest.mark.django_db
def test_a_trigger_row_from_an_unknown_migration_blocks_later_rows_on_the_same_table(scratch_table: str) -> None:
    trigger_row = DeferredOperation.objects.create(app_label="dm", migration_name="0002_pruned", operation_index=0, sequence=0, kind=DeferredOperation.Kind.DROP_TRIGGER, table_name=scratch_table, column_name="c", sql="SELECT 1")
    column_drop = DeferredOperation.objects.create(app_label="dm", migration_name="0003", operation_index=0, sequence=0, kind=DeferredOperation.Kind.DROP_COLUMN, table_name=scratch_table, column_name="a", sql=f"ALTER TABLE {scratch_table} DROP COLUMN a")
    other_table_drop = DeferredOperation.objects.create(app_label="dm", migration_name="0003", operation_index=1, sequence=1, kind=DeferredOperation.Kind.DROP_COLUMN, table_name="dm_runner_other", column_name="a", sql="SELECT 1")

    result = run_deferred_operations(dry_run=True, migration_keys=known(("dm", "0003")), sleep=lambda seconds: None)

    assert result.unknown == [trigger_row]
    assert result.blocked == [column_drop]
    assert result.would_run == [other_table_drop]
    assert any(f"Left alone row {trigger_row.pk}" in line and scratch_table in line for line in describe_run_result(result))


@pytest.mark.django_db
def test_a_trigger_row_queued_after_a_column_drop_still_blocks_that_drop(scratch_table: str) -> None:
    column_drop = DeferredOperation.objects.create(app_label="dm", migration_name="0003", operation_index=0, sequence=0, kind=DeferredOperation.Kind.DROP_COLUMN, table_name=scratch_table, column_name="a", sql=f"ALTER TABLE {scratch_table} DROP COLUMN a")
    trigger_row = DeferredOperation.objects.create(app_label="dm", migration_name="0004_pruned", operation_index=0, sequence=0, kind=DeferredOperation.Kind.DROP_TRIGGER, table_name=scratch_table, column_name="c", sql="SELECT 1")

    result = run_deferred_operations(migration_keys=known(("dm", "0003")), sleep=lambda seconds: None)

    column_drop.refresh_from_db()
    assert result.unknown == [trigger_row]
    assert result.blocked == [column_drop]
    assert column_drop.status == DeferredOperation.Status.PENDING
    assert "a" in column_names(scratch_table)


@pytest.mark.django_db
def test_dry_run_reports_a_skipped_trigger_row_and_the_row_it_blocks(scratch_table: str) -> None:
    trigger_row = DeferredOperation.objects.create(
        app_label="dm", migration_name="0002", operation_index=0, sequence=0, kind=DeferredOperation.Kind.DROP_TRIGGER, table_name=scratch_table, column_name="c", sql="SELECT 1", status=DeferredOperation.Status.SKIPPED, resolution_reason="Trigger still needed by a reporting view"
    )
    column_drop = DeferredOperation.objects.create(app_label="dm", migration_name="0003", operation_index=0, sequence=0, kind=DeferredOperation.Kind.DROP_COLUMN, table_name=scratch_table, column_name="a", sql=f"ALTER TABLE {scratch_table} DROP COLUMN a")

    result = run_deferred_operations(dry_run=True, migration_keys=known(("dm", "0002"), ("dm", "0003")), sleep=lambda seconds: None)

    assert result.would_run == []
    assert result.blocked == [column_drop]
    assert result.skipped == [trigger_row]
    lines = describe_run_result(result)
    assert any(f"Skipped row {trigger_row.pk}" in line for line in lines)
    assert any(f"Blocked row {column_drop.pk}" in line for line in lines)


@pytest.mark.django_db
def test_a_run_where_every_runnable_row_is_blocked_does_not_sleep(scratch_table: str) -> None:
    DeferredOperation.objects.create(app_label="dm", migration_name="0002", operation_index=0, sequence=0, kind=DeferredOperation.Kind.DROP_TRIGGER, table_name=scratch_table, column_name="c", sql="SELECT 1", status=DeferredOperation.Status.SKIPPED)
    DeferredOperation.objects.create(app_label="dm", migration_name="0003", operation_index=0, sequence=0, kind=DeferredOperation.Kind.DROP_COLUMN, table_name=scratch_table, column_name="a", sql=f"ALTER TABLE {scratch_table} DROP COLUMN a")
    waits: list[float] = []

    result = run_deferred_operations(wait_before_seconds=300, migration_keys=known(("dm", "0002"), ("dm", "0003")), sleep=waits.append)

    assert waits == []
    assert len(result.blocked) == 1


@pytest.mark.django_db
def test_empty_queue_returns_without_waiting() -> None:
    waits: list[float] = []

    result = run_deferred_operations(wait_before_seconds=300, migration_keys=known(), sleep=waits.append)

    assert waits == []
    assert result.ran == []


@pytest.mark.django_db
def test_waits_before_running_when_rows_are_runnable(scratch_table: str) -> None:
    DeferredOperation.objects.create(app_label="dm", migration_name="0002", operation_index=0, sequence=0, kind=DeferredOperation.Kind.DROP_COLUMN, table_name=scratch_table, column_name="a", sql=f"ALTER TABLE {scratch_table} DROP COLUMN a")
    waits: list[float] = []

    run_deferred_operations(wait_before_seconds=300, migration_keys=known(("dm", "0002")), sleep=waits.append)

    assert waits == [300]


@pytest.mark.django_db
def test_dry_run_executes_nothing_and_lists_the_blocking_row_first(scratch_table: str) -> None:
    failed = DeferredOperation.objects.create(
        app_label="dm", migration_name="0002", operation_index=0, sequence=0, kind=DeferredOperation.Kind.DROP_COLUMN, table_name=scratch_table, column_name="a", sql=f"ALTER TABLE {scratch_table} DROP COLUMN a", status=DeferredOperation.Status.FAILED, last_error="lock timeout"
    )

    result = run_deferred_operations(dry_run=True, migration_keys=known(("dm", "0002")), sleep=lambda seconds: None)

    failed.refresh_from_db()
    assert result.would_run == [failed]
    assert failed.status == DeferredOperation.Status.FAILED
    assert describe_run_result(result)[0].startswith(f"Blocking row {failed.pk}")


@pytest.mark.django_db
@pytest.mark.usefixtures("squashed_test_app")
def test_rows_from_a_migration_replaced_by_a_squash_still_run(scratch_table: str) -> None:
    row = DeferredOperation.objects.create(app_label="deferred_migrations_testapp", migration_name="0002_remove_child_legacy", operation_index=0, sequence=0, kind=DeferredOperation.Kind.DROP_COLUMN, table_name=scratch_table, column_name="a", sql=f"ALTER TABLE {scratch_table} DROP COLUMN a")

    run_deferred_operations(sleep=lambda seconds: None)

    row.refresh_from_db()
    assert row.status == DeferredOperation.Status.DONE


@pytest.mark.django_db
@pytest.mark.usefixtures("squashed_test_app")
def test_from_connection_expands_the_migrations_a_squash_replaced() -> None:
    loader = MigrationLoader(connection, ignore_no_migrations=True)
    keys = MigrationKeys.from_connection(connection)

    assert ("deferred_migrations_testapp", "0002_remove_child_legacy") not in loader.graph.nodes
    assert keys.contains("deferred_migrations_testapp", "0002_remove_child_legacy")


@pytest.mark.django_db
def test_the_age_of_an_unknown_row_is_reported_so_callers_can_warn_on_it(scratch_table: str) -> None:
    old = DeferredOperation.objects.create(app_label="dm", migration_name="0009", operation_index=0, sequence=0, kind=DeferredOperation.Kind.DROP_COLUMN, table_name=scratch_table, column_name="a", sql="SELECT 1", created_at=timezone.now() - timedelta(days=20))

    result = run_deferred_operations(migration_keys=known(), sleep=lambda seconds: None)

    assert result.unknown == [old]
    assert f"queued {old.created_at:%Y-%m-%d}" in describe_run_result(result)[0]


@pytest.mark.django_db
def test_view_drops_run_before_every_other_row_whatever_their_ids(scratch_table: str) -> None:
    with connection.cursor() as cursor:
        cursor.execute(f"CREATE VIEW dm_runner_view AS SELECT * FROM {scratch_table}")

    DeferredOperation.objects.create(app_label="dm", migration_name="0002", operation_index=0, sequence=0, kind=DeferredOperation.Kind.DROP_COLUMN, table_name=scratch_table, column_name="b", sql=f"ALTER TABLE {scratch_table} DROP COLUMN b")
    DeferredOperation.objects.create(app_label="dm", migration_name="0003", operation_index=0, sequence=0, kind=DeferredOperation.Kind.DROP_VIEW, table_name=scratch_table, column_name="dm_runner_view", sql="DROP VIEW IF EXISTS dm_runner_view")

    result = run_deferred_operations(migration_keys=known(("dm", "0002"), ("dm", "0003")), sleep=lambda seconds: None)

    assert [row.kind for row in result.ran] == [DeferredOperation.Kind.DROP_VIEW, DeferredOperation.Kind.DROP_COLUMN]
    assert "b" not in column_names(scratch_table)


@pytest.mark.django_db
def test_a_view_drop_from_an_unknown_migration_blocks_its_table(scratch_table: str) -> None:
    DeferredOperation.objects.create(app_label="dm", migration_name="0009", operation_index=0, sequence=0, kind=DeferredOperation.Kind.DROP_VIEW, table_name=scratch_table, column_name="dm_runner_view", sql="DROP VIEW IF EXISTS dm_runner_view")
    column_drop = DeferredOperation.objects.create(app_label="dm", migration_name="0002", operation_index=0, sequence=0, kind=DeferredOperation.Kind.DROP_COLUMN, table_name=scratch_table, column_name="b", sql=f"ALTER TABLE {scratch_table} DROP COLUMN b")

    result = run_deferred_operations(migration_keys=known(("dm", "0002")), sleep=lambda seconds: None)

    assert result.blocked == [column_drop]
    assert any("dm_runner_view" in line for line in describe_run_result(result))
