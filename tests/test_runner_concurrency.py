import psycopg
import pytest
from django.core.management import CommandError
from django.core.management import call_command
from django.db import connection

from deferred_migrations.models import DeferredOperation
from deferred_migrations.runner import ADVISORY_LOCK_KEY
from deferred_migrations.runner import MigrationKeys
from deferred_migrations.runner import run_deferred_operations


@pytest.mark.django_db(transaction=True)
def test_a_second_runner_reports_the_lock_and_runs_nothing() -> None:
    row = DeferredOperation.objects.create(app_label="dm", migration_name="0001", operation_index=0, sequence=0, kind=DeferredOperation.Kind.DROP_COLUMN, table_name="dm_never_run", column_name="a", sql="ALTER TABLE dm_never_run DROP COLUMN a")
    keys = MigrationKeys(known={("dm", "0001")}, applied={("dm", "0001")})

    with psycopg.connect(**connection.get_connection_params()) as other:
        other.execute("SELECT pg_advisory_lock(%s)", [ADVISORY_LOCK_KEY])

        result = run_deferred_operations(migration_keys=keys, sleep=lambda seconds: None)

    row.refresh_from_db()

    assert result.lock_held_elsewhere
    assert result.ran == []
    assert row.status == DeferredOperation.Status.PENDING


# A post-deploy run holding the lock works from a snapshot taken before its wait; a skip written underneath it would be overwritten and the drop run anyway.
@pytest.mark.django_db(transaction=True)
def test_resolve_refuses_while_a_post_deploy_run_holds_the_lock() -> None:
    row = DeferredOperation.objects.create(app_label="dm", migration_name="0001", operation_index=0, sequence=0, kind=DeferredOperation.Kind.DROP_COLUMN, table_name="dm_never_run", column_name="a", sql="ALTER TABLE dm_never_run DROP COLUMN a", status=DeferredOperation.Status.FAILED)

    with psycopg.connect(**connection.get_connection_params()) as other:
        other.execute("SELECT pg_advisory_lock(%s)", [ADVISORY_LOCK_KEY])

        with pytest.raises(CommandError, match="migrate_post_deploy run is in progress"):
            call_command("deferred_migrations_resolve", str(row.pk), "--skip", "--reason", "Column is used by a reporting view")

    row.refresh_from_db()

    assert row.status == DeferredOperation.Status.FAILED
    assert row.resolution_reason == ""
