from unittest import mock

import pytest
from django.core.management import CommandError
from django.core.management import ManagementUtility
from django.core.management import call_command
from django.db import connection
from django.db import connections
from pytest_django import Settings

from deferred_migrations.models import DeferredOperation
from deferred_migrations.models import ModelRename
from deferred_migrations.runner import RunResult
from tests.migration_helpers import column_names


@pytest.mark.parametrize(("argv", "allowed", "refused"), [(["migrate"], False, True), (["migrate", "--fake-initial"], False, True), (["migrate", "--plan"], False, False), (["migrate", "--check"], False, False), (["migrate", "--fake"], False, False), (["migrate"], True, False)])
@pytest.mark.django_db
def test_migrate_typed_at_the_command_line_is_refused_unless_it_only_reads_or_records_or_is_allowed(argv: list[str], allowed: bool, refused: bool, settings: Settings, capsys: pytest.CaptureFixture[str]) -> None:
    settings.DEFERRED_MIGRATIONS_ALLOW_COMMAND_LINE_MIGRATE = allowed

    # run_from_argv closes every connection when it finishes, which would end the test's transaction.
    with mock.patch.object(connections, "close_all"):
        try:
            ManagementUtility(["manage.py", *argv, "--skip-checks"]).execute()
            exit_code = 0
        except SystemExit as exited:
            exit_code = exited.code

    assert (exit_code, "migrate_full" in capsys.readouterr().err) == ((1, True) if refused else (0, False))


# A database that has never been migrated cannot have old code running against it, so migrate finishes the job there, as Django's own does.
@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("test_app")
def test_migrate_on_a_new_database_also_runs_the_queued_drops() -> None:
    with connection.cursor() as cursor:
        cursor.execute(f"DROP TABLE django_migrations, {DeferredOperation._meta.db_table}, {ModelRename._meta.db_table}")

    call_command("migrate", verbosity=0)

    assert "legacy" not in column_names("deferred_migrations_testapp_child")
    assert DeferredOperation.objects.get(app_label="deferred_migrations_testapp").status == DeferredOperation.Status.DONE


@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("test_app")
def test_migrate_on_a_new_database_raises_when_a_queued_drop_fails() -> None:
    with connection.cursor() as cursor:
        cursor.execute(f"DROP TABLE django_migrations, {DeferredOperation._meta.db_table}, {ModelRename._meta.db_table}")

    failed = DeferredOperation(
        pk=7, app_label="deferred_migrations_testapp", migration_name="0002_remove_child_legacy", operation_index=0, sequence=0, kind=DeferredOperation.Kind.DROP_COLUMN, table_name="deferred_migrations_testapp_child", column_name="legacy", sql="SELECT 1", last_error="lock timeout"
    )

    with mock.patch("deferred_migrations.notice.run_deferred_operations", return_value=RunResult(failed=failed)), pytest.raises(CommandError, match=r"Deferred operation 7 .* failed: lock timeout"):
        call_command("migrate", verbosity=0)


@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("test_app")
def test_migrate_on_an_existing_database_leaves_the_drops_queued() -> None:
    call_command("migrate", "deferred_migrations_testapp", verbosity=0)

    assert "legacy" in column_names("deferred_migrations_testapp_child")
    assert DeferredOperation.objects.get(app_label="deferred_migrations_testapp").status == DeferredOperation.Status.PENDING
