import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TextIO

from django.apps import AppConfig
from django.core.management import CommandError
from django.core.management.base import OutputWrapper
from django.db import DEFAULT_DB_ALIAS
from django.db import connections
from django.db.migrations.recorder import MigrationRecorder

from deferred_migrations.models import DeferredOperation
from deferred_migrations.runner import describe_run_result
from deferred_migrations.runner import run_deferred_operations

_suppressed = False
_new_databases: set[str] = set()


@contextmanager
def phase_command_in_charge() -> Iterator[None]:
    # The phase commands decide when drops run: pre-deploy must never run them, even on a new database, and the notice would only be noise in the deploy Job's log.
    global _suppressed
    previously_suppressed = _suppressed
    _suppressed = True

    try:
        yield
    finally:
        _suppressed = previously_suppressed


# A database that has never been migrated has no old code running against it, so a plain migrate there can finish the job like Django's does.
def note_whether_database_is_new(sender: AppConfig, using: str = DEFAULT_DB_ALIAS, **kwargs: object) -> None:  # noqa: ARG001
    if MigrationRecorder(connections[using]).has_table():
        _new_databases.discard(using)
    else:
        _new_databases.add(using)


def after_migrate(sender: AppConfig, verbosity: int = 1, using: str = DEFAULT_DB_ALIAS, stdout: OutputWrapper | TextIO | None = None, **kwargs: object) -> None:  # noqa: ARG001
    is_new_database = using in _new_databases
    _new_databases.discard(using)

    if _suppressed:
        return

    # A database that has not applied deferred_migrations.0001_initial has no queue table, and a failed query would poison the transaction for whatever runs next.
    if DeferredOperation._meta.db_table not in connections[using].introspection.table_names():
        return

    waiting = DeferredOperation.objects.using(using).filter(status__in=[DeferredOperation.Status.PENDING, DeferredOperation.Status.FAILED]).count()

    if not waiting:
        return

    output = stdout or sys.stdout

    if is_new_database:
        result = run_deferred_operations(using=using)

        if verbosity >= 1:
            for line in describe_run_result(result):
                output.write(f"{line}\n")

        if (failed := result.failed) is not None:
            raise CommandError(f"Deferred operation {failed.pk} ({failed}) failed: {failed.last_error}")

        return

    if verbosity < 1:
        return

    if waiting == 1:
        message = '1 deferred operation is queued: the column or table it drops still exists. Run "python manage.py migrate_post_deploy" to complete it.'
    else:
        message = f'{waiting} deferred operations are queued: the columns and tables they drop still exist. Run "python manage.py migrate_post_deploy" to complete them.'

    output.write(f"{message}\n")
