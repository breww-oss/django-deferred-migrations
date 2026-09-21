import re
import time
from argparse import ArgumentParser
from typing import TextIO

from django.core.exceptions import ImproperlyConfigured
from django.core.management import BaseCommand
from django.core.management import CommandError
from django.core.management import call_command
from django.core.management.base import OutputWrapper
from django.db import DEFAULT_DB_ALIAS
from django.db import OperationalError
from django.db import ProgrammingError
from django.db import connections
from django.db.models import Max
from django.db.models import Q
from psycopg import errors as psycopg_errors

from deferred_migrations.context import progress_output
from deferred_migrations.locking import LockRetriesExhausted
from deferred_migrations.locking import ddl_lock_timeout
from deferred_migrations.locking import find_in_chain
from deferred_migrations.locking import is_lock_timeout
from deferred_migrations.locking import lock_retries_setting
from deferred_migrations.locking import retry_delay_seconds
from deferred_migrations.models import DeferredOperation
from deferred_migrations.notice import phase_command_in_charge
from deferred_migrations.runner import MigrationKeys


class Command(BaseCommand):
    help = "Apply migrations before new code rolls out: check deploy safety, then migrate with a DDL lock timeout and retry."
    requires_system_checks = []
    # PostgreSQL leaves diag.table_name and diag.column_name empty for these errors, so the names can only come from its message.
    DUPLICATE_COLUMN_MESSAGE = re.compile(r'^column "(?P<column>[^"]+)" of relation "(?P<table>[^"]+)" already exists$')
    DUPLICATE_TABLE_MESSAGE = re.compile(r'^relation "(?P<table>[^"]+)" already exists$')

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument("--database", default=DEFAULT_DB_ALIAS)

    # OutputWrapper appends a newline to every write, which breaks rich's in-place redraws, and a caller that passes its own self.stdout wraps it twice.
    def raw_stdout(self) -> TextIO:
        stream = self.stdout

        while isinstance(stream, OutputWrapper):
            stream = stream._out

        return stream

    def handle(self, *args: str, database: str, **options: object) -> None:
        connection = connections[database]

        if connection.vendor != "postgresql":
            raise ImproperlyConfigured("deferred_migrations only supports PostgreSQL.")

        with phase_command_in_charge():
            self.apply_own_migrations(database)
            call_command("check_deploy_safety", unapplied_only=True, database=database, stdout=self.stdout, stderr=self.stderr)
            self.delete_rows_from_unapplied_migrations(database)
            last_row_before = DeferredOperation.objects.using(database).aggregate(last=Max("pk"))["last"] or 0
            attempts = lock_retries_setting()

            for attempt in range(1, attempts + 1):
                try:
                    with ddl_lock_timeout(connection), progress_output(self.raw_stdout(), int(options["verbosity"])):
                        call_command("migrate", database=database, interactive=False, stdout=self.stdout, stderr=self.stderr)

                    break
                except LockRetriesExhausted as error:
                    raise CommandError(f"A non-atomic migration could not acquire a lock: {error}") from error
                except OperationalError as error:
                    if not is_lock_timeout(error):
                        raise

                    if attempt == attempts:
                        raise CommandError(f"Gave up after {attempts} attempts waiting for a lock: {error}") from error

                    self.stdout.write(f"Lock timeout ({error}); retrying migrate (attempt {attempt + 1} of {attempts}).")
                    time.sleep(retry_delay_seconds(attempt))
                except ProgrammingError as error:
                    duplicate = find_in_chain(error, (psycopg_errors.DuplicateColumn, psycopg_errors.DuplicateTable))

                    if duplicate is None:
                        raise

                    is_column = isinstance(duplicate, psycopg_errors.DuplicateColumn)
                    pattern = self.DUPLICATE_COLUMN_MESSAGE if is_column else self.DUPLICATE_TABLE_MESSAGE

                    # A message in another server language will not match; the original error is still the right thing to show.
                    if (match := pattern.match(duplicate.diag.message_primary or "")) is None:
                        raise

                    # A skipped drop never runs, so it holds the name just as firmly as one still waiting.
                    unrun = [DeferredOperation.Status.PENDING, DeferredOperation.Status.FAILED, DeferredOperation.Status.SKIPPED]

                    if is_column:
                        holders = list(DeferredOperation.objects.using(database).filter(status__in=unrun, table_name=match["table"], column_name=match["column"]))
                    else:
                        # A queued view drop holds its view's name in column_name, against the renamed table.
                        holders = list(DeferredOperation.objects.using(database).filter(status__in=unrun).filter(Q(kind=DeferredOperation.Kind.DROP_TABLE, table_name=match["table"]) | Q(kind=DeferredOperation.Kind.DROP_VIEW, column_name=match["table"])))

                    if not holders:
                        raise

                    if all(row.status == DeferredOperation.Status.SKIPPED for row in holders):
                        raise CommandError(f"{error} A skipped drop still holds this name: {', '.join(str(row) for row in holders)}. It will never run, so either return it to pending with deferred_migrations_resolve --unskip and run migrate_post_deploy, or use a different name.") from error

                    raise CommandError(f"{error} A queued drop still holds this name: {', '.join(str(row) for row in holders)}. Run migrate_post_deploy once the previous release is fully rolled out, or use a different name.") from error

            queued = DeferredOperation.objects.using(database).filter(pk__gt=last_row_before).order_by("id")

            for row in queued:
                self.stdout.write(f"Queued for post-deploy: {row}")

    def apply_own_migrations(self, database: str) -> None:
        # Every later step queries DeferredOperation, so its table must exist and match this code before the main migrate runs.
        call_command("migrate", DeferredOperation._meta.app_label, database=database, interactive=False, verbosity=0, stdout=self.stdout, stderr=self.stderr)

    def delete_rows_from_unapplied_migrations(self, database: str) -> None:
        # An unrecorded migration that is still in the graph is about to be re-run from its first operation and will re-queue every row, so its old rows must go: ON CONFLICT DO NOTHING would otherwise keep a stale row and silently discard the corrected one.
        # A migration that has left the graph is never coming back to re-queue anything, and a trigger or view drop lost that way would leave the trigger or view reading a column a later drop removes, breaking every write to the table, so those rows are kept.
        keys = MigrationKeys.from_connection(connections[database])
        unresolved = DeferredOperation.objects.using(database).filter(Q(status=DeferredOperation.Status.PENDING) | Q(status=DeferredOperation.Status.FAILED))
        unrecorded = [row for row in unresolved if (row.app_label, row.migration_name) not in keys.applied]
        stale = [row.pk for row in unrecorded if row.kind not in DeferredOperation.BLOCKING_KINDS or (row.app_label, row.migration_name) in keys.known]
        DeferredOperation.objects.using(database).filter(pk__in=stale).delete()
