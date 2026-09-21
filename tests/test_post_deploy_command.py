from io import StringIO
from unittest import mock

import pytest
from django.core.management import CommandError
from django.core.management import call_command

from deferred_migrations.models import DeferredOperation
from deferred_migrations.runner import RunResult


@pytest.mark.django_db
def test_fail_on_error_raises_only_when_a_row_failed() -> None:
    failed = DeferredOperation(pk=3, app_label="dm", migration_name="0002", operation_index=0, sequence=0, kind=DeferredOperation.Kind.DROP_COLUMN, table_name="t", column_name="c", sql="SELECT 1", last_error="boom")

    with mock.patch("deferred_migrations.management.commands.migrate_post_deploy.run_deferred_operations", return_value=RunResult(failed=failed)):
        call_command("migrate_post_deploy", stdout=StringIO())

        with pytest.raises(CommandError, match="Deferred operation 3 failed"):
            call_command("migrate_post_deploy", "--fail-on-error", stdout=StringIO())

    with mock.patch("deferred_migrations.management.commands.migrate_post_deploy.run_deferred_operations", return_value=RunResult()):
        call_command("migrate_post_deploy", "--fail-on-error", stdout=StringIO())
