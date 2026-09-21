from argparse import ArgumentParser

from django.core.management import BaseCommand
from django.core.management import CommandError
from django.db import DEFAULT_DB_ALIAS
from django.db import connections

from deferred_migrations.models import DeferredOperation
from deferred_migrations.runner import advisory_lock


class Command(BaseCommand):
    help = "Skip a deferred operation that can never succeed, or return a skipped one to pending."
    requires_system_checks = []

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument("operation_id", type=int)
        action = parser.add_mutually_exclusive_group(required=True)
        action.add_argument("--skip", action="store_true")
        action.add_argument("--unskip", action="store_true")
        parser.add_argument("--reason", default="")
        parser.add_argument("--database", default=DEFAULT_DB_ALIAS)

    def handle(self, *args: str, operation_id: int, skip: bool, unskip: bool, reason: str, database: str, **options: object) -> None:
        # A post-deploy run works from a snapshot of the queue taken before its wait, so a status changed underneath it would be overwritten and the drop run anyway. Only the lock holder may change a status.
        with advisory_lock(connections[database]) as acquired:
            if not acquired:
                raise CommandError("A migrate_post_deploy run is in progress. Try again once it has finished.")

            self.resolve(operation_id, skip, unskip, reason, database)

    def resolve(self, operation_id: int, skip: bool, unskip: bool, reason: str, database: str) -> None:
        try:
            row = DeferredOperation.objects.using(database).get(pk=operation_id)
        except DeferredOperation.DoesNotExist:
            raise CommandError(f"No deferred operation with id {operation_id}.") from None

        if skip:
            if not reason.strip():
                raise CommandError("--skip requires --reason explaining why the operation can never run.")

            # Skipping a row that has already run would move it back into the outstanding set, where every later run reports it as skipped forever.
            if row.status not in (DeferredOperation.Status.PENDING, DeferredOperation.Status.FAILED):
                raise CommandError(f"Deferred operation {operation_id} is {row.status}; only pending or failed operations can be skipped.")

            if row.kind == DeferredOperation.Kind.DROP_TRIGGER:
                raise CommandError("Trigger drops cannot be skipped: dropping the column while the trigger remains would break every write to the table. Fix the cause and let it retry.")

            if row.kind == DeferredOperation.Kind.DROP_VIEW:
                raise CommandError(
                    f"Compatibility view drops cannot be skipped: the view depends on every column of {row.table_name}, so every other drop on it would be blocked for good. Instead, drop the view {row.column_name} by hand, then run migrate_post_deploy, which finds it gone and marks this row done."
                )

            self.change_status(database, row, (DeferredOperation.Status.PENDING, DeferredOperation.Status.FAILED), DeferredOperation.Status.SKIPPED, reason.strip())
            self.stdout.write(f"Skipped {row}.")
            return

        if unskip:
            if row.status != DeferredOperation.Status.SKIPPED:
                raise CommandError(f"Deferred operation {operation_id} is {row.status}, not skipped.")

            self.change_status(database, row, (DeferredOperation.Status.SKIPPED,), DeferredOperation.Status.PENDING, "")
            self.stdout.write(f"Returned {row} to pending.")

    # A post-deploy run can change the row between the read above and this write, so make the status we validated part of the predicate.
    def change_status(self, database: str, row: DeferredOperation, expected: tuple[str, ...], new_status: str, reason: str) -> None:
        changed = DeferredOperation.objects.using(database).filter(pk=row.pk, status__in=expected).update(status=new_status, resolution_reason=reason)

        if not changed:
            raise CommandError(f"Deferred operation {row.pk} changed while this command was running; check its status and try again.")
