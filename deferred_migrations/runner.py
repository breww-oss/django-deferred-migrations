import time
from collections.abc import Callable
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from dataclasses import field

from django.db import DEFAULT_DB_ALIAS
from django.db import DatabaseError
from django.db import connections
from django.db import transaction
from django.db.backends.base.base import BaseDatabaseWrapper
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.recorder import MigrationRecorder
from django.db.models import F
from django.utils import timezone

from deferred_migrations.locking import ddl_lock_timeout
from deferred_migrations.locking import is_lock_timeout
from deferred_migrations.locking import lock_retries_setting
from deferred_migrations.locking import retry_delay_seconds
from deferred_migrations.models import DeferredOperation

ADVISORY_LOCK_KEY = 7_246_331_901


@dataclass(frozen=True)
class MigrationKeys:
    known: set[tuple[str, str]]
    applied: set[tuple[str, str]]

    @classmethod
    def from_connection(cls, connection: BaseDatabaseWrapper) -> "MigrationKeys":
        loader = MigrationLoader(connection, ignore_no_migrations=True)
        known = set(loader.graph.nodes)
        changed = True

        while changed:
            changed = False

            for squashed_key, squashed in loader.replacements.items():
                if squashed_key in known and not known.issuperset(squashed.replaces):
                    known.update(squashed.replaces)
                    changed = True

        return cls(known=known, applied=set(MigrationRecorder(connection).applied_migrations()))

    def contains(self, app_label: str, migration_name: str) -> bool:
        key = (app_label, migration_name)
        return key in self.known and key in self.applied


@dataclass
class RunResult:
    ran: list[DeferredOperation] = field(default_factory=list)
    would_run: list[DeferredOperation] = field(default_factory=list)
    failed: DeferredOperation | None = None
    blocked: list[DeferredOperation] = field(default_factory=list)
    skipped: list[DeferredOperation] = field(default_factory=list)
    unknown: list[DeferredOperation] = field(default_factory=list)
    lock_held_elsewhere: bool = False


@contextmanager
def advisory_lock(connection: BaseDatabaseWrapper) -> Iterator[bool]:
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", [ADVISORY_LOCK_KEY])
        acquired = cursor.fetchone()[0]

    try:
        yield acquired
    finally:
        if acquired:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", [ADVISORY_LOCK_KEY])


@dataclass
class ClassifiedRows:
    runnable: list[DeferredOperation] = field(default_factory=list)
    blocked: list[DeferredOperation] = field(default_factory=list)
    skipped: list[DeferredOperation] = field(default_factory=list)
    unknown: list[DeferredOperation] = field(default_factory=list)


def classify_rows(rows: list[DeferredOperation], keys: MigrationKeys) -> "ClassifiedRows":
    classified = ClassifiedRows()

    # A trigger reading a column, or a view selecting it, breaks every write or every drop on the table until it is gone, so nothing else on that table may run first.
    blocked_tables = {row.table_name for row in rows if row.kind in DeferredOperation.BLOCKING_KINDS and (not keys.contains(row.app_label, row.migration_name) or row.status == DeferredOperation.Status.SKIPPED)}

    for row in rows:
        if not keys.contains(row.app_label, row.migration_name):
            classified.unknown.append(row)
        elif row.status == DeferredOperation.Status.SKIPPED:
            classified.skipped.append(row)
        elif row.table_name in blocked_tables:
            classified.blocked.append(row)
        else:
            classified.runnable.append(row)

    return classified


# A compatibility view selects every column of its table, so PostgreSQL refuses to drop any of them, or the table, while it exists.
def run_order(row: DeferredOperation) -> tuple[bool, int]:
    return (row.kind != DeferredOperation.Kind.DROP_VIEW, row.pk)


def run_deferred_operations(using: str = DEFAULT_DB_ALIAS, wait_before_seconds: int = 0, dry_run: bool = False, migration_keys: MigrationKeys | None = None, sleep: Callable[[float], None] = time.sleep) -> RunResult:
    connection = connections[using]
    result = RunResult()

    with advisory_lock(connection) as acquired:
        if not acquired:
            result.lock_held_elsewhere = True
            return result

        keys = migration_keys or MigrationKeys.from_connection(connection)
        rows = sorted(DeferredOperation.objects.using(using).exclude(status=DeferredOperation.Status.DONE), key=run_order)

        classified = classify_rows(rows, keys)
        result.blocked = classified.blocked
        result.skipped = classified.skipped
        result.unknown = classified.unknown

        if dry_run:
            result.would_run = classified.runnable
            return result

        if not classified.runnable:
            return result

        sleep(wait_before_seconds)

        for row in classified.runnable:
            if error := execute_row(connection, row, sleep):
                DeferredOperation.objects.using(using).filter(pk=row.pk).update(status=DeferredOperation.Status.FAILED, attempts=F("attempts") + 1, last_error=error)
                row.refresh_from_db(using=using)
                result.failed = row
                return result

            DeferredOperation.objects.using(using).filter(pk=row.pk).update(status=DeferredOperation.Status.DONE, attempts=F("attempts") + 1, last_error="", executed_at=timezone.now())
            result.ran.append(row)

    return result


def execute_row(connection: BaseDatabaseWrapper, row: DeferredOperation, sleep: Callable[[float], None]) -> str | None:
    attempts = lock_retries_setting()

    for attempt in range(1, attempts + 1):
        try:
            with ddl_lock_timeout(connection, sleep=sleep), transaction.atomic(using=connection.alias), connection.schema_editor(atomic=False) as editor:
                editor.execute(row.sql, params=None)

            return None
        except DatabaseError as error:
            if is_lock_timeout(error) and attempt < attempts:
                sleep(retry_delay_seconds(attempt))
                continue

            return str(error)

    raise ValueError(f"Deferred operation retry loop exited without returning or raising after {attempts} attempts")


def describe_run_result(result: RunResult) -> list[str]:
    lines: list[str] = []

    if result.lock_held_elsewhere:
        return ["Another migrate_post_deploy run holds the queue lock; nothing was run."]

    for row in result.would_run:
        prefix = f"Blocking row {row.pk}" if row.status == DeferredOperation.Status.FAILED else f"Would run row {row.pk}"
        lines.append(f"{prefix}: {row} ({row.last_error or row.sql})")

    for row in result.ran:
        lines.append(f"Ran row {row.pk}: {row}")

    if result.failed is not None:
        lines.append(f'Failed row {result.failed.pk}: {result.failed} ({result.failed.last_error}). Later rows were not run. To skip it: manage.py deferred_migrations_resolve {result.failed.pk} --skip --reason "..."')

    for row in result.skipped:
        if row.kind == DeferredOperation.Kind.DROP_TRIGGER:
            lines.append(f"Skipped row {row.pk}: {row} ({row.resolution_reason}). This blocks every other row on {row.table_name} until it is unskipped.")
        elif row.kind == DeferredOperation.Kind.DROP_VIEW:
            lines.append(f"Skipped row {row.pk}: {row} ({row.resolution_reason}). The view {row.column_name} blocks every other row on {row.table_name} until it is dropped.")
        else:
            lines.append(f"Skipped row {row.pk}: {row} ({row.resolution_reason})")

    for row in result.blocked:
        lines.append(f"Blocked row {row.pk}: {row} (a trigger or view drop on {row.table_name} must run first)")

    for row in result.unknown:
        line = f"Left alone row {row.pk}: {row} (queued {row.created_at:%Y-%m-%d} by a migration this code does not know or has not applied)"

        if row.kind == DeferredOperation.Kind.DROP_TRIGGER:
            line += f" This blocks every other row on {row.table_name}; dropping a column the trigger reads would break every write to that table. To clear it, restore the migration that owns it so it becomes known again, or run this row's SQL by hand and delete the row."
        elif row.kind == DeferredOperation.Kind.DROP_VIEW:
            line += f" The view {row.column_name} selects every column of {row.table_name}, so it blocks every other row on that table. To clear it, restore the migration that owns it, or drop the view by hand and delete this row."

        lines.append(line)

    return lines
