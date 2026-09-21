import pytest
from django.core.management import CommandError
from django.core.management import call_command

from deferred_migrations.models import DeferredOperation


@pytest.fixture
def failed_column_row() -> DeferredOperation:
    return DeferredOperation.objects.create(app_label="dm", migration_name="0002", operation_index=0, sequence=0, kind=DeferredOperation.Kind.DROP_COLUMN, table_name="t", column_name="c", sql="SELECT 1", status=DeferredOperation.Status.FAILED)


@pytest.mark.django_db
def test_skip_requires_a_reason(failed_column_row: DeferredOperation) -> None:
    with pytest.raises(CommandError, match="--reason"):
        call_command("deferred_migrations_resolve", str(failed_column_row.pk), "--skip")


@pytest.mark.django_db
def test_skip_records_the_reason(failed_column_row: DeferredOperation) -> None:
    call_command("deferred_migrations_resolve", str(failed_column_row.pk), "--skip", "--reason", "Column is used by a reporting view")

    failed_column_row.refresh_from_db()
    assert (failed_column_row.status, failed_column_row.resolution_reason) == (DeferredOperation.Status.SKIPPED, "Column is used by a reporting view")


@pytest.mark.django_db
def test_skip_refuses_trigger_rows() -> None:
    row = DeferredOperation.objects.create(app_label="dm", migration_name="0002", operation_index=0, sequence=0, kind=DeferredOperation.Kind.DROP_TRIGGER, table_name="t", column_name="c", sql="SELECT 1", status=DeferredOperation.Status.FAILED)

    with pytest.raises(CommandError, match="trigger"):
        call_command("deferred_migrations_resolve", str(row.pk), "--skip", "--reason", "anything")


@pytest.mark.django_db
def test_skip_refuses_a_row_that_has_already_run() -> None:
    row = DeferredOperation.objects.create(app_label="dm", migration_name="0002", operation_index=0, sequence=0, kind=DeferredOperation.Kind.DROP_COLUMN, table_name="t", column_name="c", sql="SELECT 1", status=DeferredOperation.Status.DONE)

    with pytest.raises(CommandError, match="only pending or failed"):
        call_command("deferred_migrations_resolve", str(row.pk), "--skip", "--reason", "anything")

    row.refresh_from_db()
    assert row.status == DeferredOperation.Status.DONE


@pytest.mark.django_db
def test_unskip_returns_a_row_to_pending(failed_column_row: DeferredOperation) -> None:
    DeferredOperation.objects.filter(pk=failed_column_row.pk).update(status=DeferredOperation.Status.SKIPPED, resolution_reason="x")

    call_command("deferred_migrations_resolve", str(failed_column_row.pk), "--unskip")

    failed_column_row.refresh_from_db()
    assert failed_column_row.status == DeferredOperation.Status.PENDING


@pytest.mark.django_db
def test_unknown_id_errors() -> None:
    with pytest.raises(CommandError, match="No deferred operation"):
        call_command("deferred_migrations_resolve", "999999", "--unskip")


@pytest.mark.django_db
def test_skip_refuses_view_drops() -> None:
    row = DeferredOperation.objects.create(app_label="dm", migration_name="0002", operation_index=0, sequence=0, kind=DeferredOperation.Kind.DROP_VIEW, table_name="t", column_name="old_t", sql="DROP VIEW IF EXISTS old_t", status=DeferredOperation.Status.FAILED)

    with pytest.raises(CommandError, match="drop the view old_t by hand"):
        call_command("deferred_migrations_resolve", str(row.pk), "--skip", "--reason", "anything")
