import pytest
from django.core.exceptions import ImproperlyConfigured
from django.db import connection
from django.db import migrations
from django.db.migrations.operations.base import Operation
from django.db.migrations.state import ProjectState

from deferred_migrations.context import OperationKey
from deferred_migrations.context import operation_key
from deferred_migrations.models import DeferredOperation
from deferred_migrations.queue import delete_queued
from deferred_migrations.queue import queue_statement
from tests.migration_helpers import apply_operations


class RecordKey(Operation):
    def __init__(self) -> None:
        self.seen: list[OperationKey] = []

    def state_forwards(self, app_label: str, state: ProjectState) -> None:
        pass

    def database_forwards(self, app_label: str, schema_editor: object, from_state: ProjectState, to_state: ProjectState) -> None:
        self.seen.append(operation_key(self))

    def database_backwards(self, app_label: str, schema_editor: object, from_state: ProjectState, to_state: ProjectState) -> None:
        pass


@pytest.mark.django_db
def test_operation_key_identifies_the_running_migration_and_index() -> None:
    first = RecordKey()
    second = RecordKey()

    apply_operations("dm_ctx", ProjectState(), [first, second], name="0007_example")

    assert first.seen == [OperationKey("dm_ctx", "0007_example", 0)]
    assert second.seen == [OperationKey("dm_ctx", "0007_example", 1)]


def test_operation_key_outside_a_migration_raises() -> None:
    with pytest.raises(ImproperlyConfigured, match="can only run inside a migration"):
        operation_key(RecordKey())


@pytest.mark.parametrize(("table_name", "column_name"), [('dm_ctx_"thing', "old"), ("dm_ctx_thing", 'ol"d'), ("dm_ctx_thing", "ol\x00d")])
@pytest.mark.django_db
def test_queue_statement_refuses_an_identifier_quote_name_cannot_safely_quote(table_name: str, column_name: str) -> None:
    with connection.schema_editor() as editor, pytest.raises(ValueError, match="double quote or a NUL byte"):
        queue_statement(editor, OperationKey("dm_ctx", "0001_test", 3), 0, DeferredOperation.Kind.DROP_COLUMN, table_name, column_name, "SELECT 1")

    assert not DeferredOperation.objects.exists()


@pytest.mark.django_db
def test_queue_statement_ignores_duplicates_and_delete_removes_every_status() -> None:
    key = OperationKey("dm_ctx", "0001_test", 3)

    with connection.schema_editor() as editor:
        queue_statement(editor, key, 0, DeferredOperation.Kind.DROP_COLUMN, "dm_ctx_thing", "old", 'ALTER TABLE "dm_ctx_thing" DROP COLUMN IF EXISTS "old"')
        queue_statement(editor, key, 0, DeferredOperation.Kind.DROP_COLUMN, "dm_ctx_thing", "old", "SELECT 'a different statement'")

    row = DeferredOperation.objects.get()
    assert row.sql == 'ALTER TABLE "dm_ctx_thing" DROP COLUMN IF EXISTS "old"'
    assert row.status == DeferredOperation.Status.PENDING

    DeferredOperation.objects.filter(pk=row.pk).update(status=DeferredOperation.Status.DONE)

    with connection.schema_editor() as editor:
        delete_queued(editor, key)

    assert not DeferredOperation.objects.exists()


@pytest.mark.django_db
def test_operation_key_for_an_operation_nested_in_separate_database_and_state_raises() -> None:
    nested = RecordKey()

    with pytest.raises(ImproperlyConfigured, match="top-level operation"):
        apply_operations("dm_ctx", ProjectState(), [migrations.SeparateDatabaseAndState(database_operations=[nested])], name="0008_nested")
