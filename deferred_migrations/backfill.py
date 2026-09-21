import time
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field
from functools import partial
from typing import ClassVar

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import OperationalError
from django.db import transaction
from django.db.backends.base.base import BaseDatabaseWrapper

from deferred_migrations.locking import LockRetriesExhausted
from deferred_migrations.locking import is_lock_timeout
from deferred_migrations.locking import lock_retries_setting
from deferred_migrations.locking import lock_timeout_setting
from deferred_migrations.locking import retry_delay_seconds
from deferred_migrations.progress import BatchReport
from deferred_migrations.progress import ProgressReporter
from deferred_migrations.progress import make_reporter
from deferred_migrations.triggers import quote_for_params


@dataclass(frozen=True)
class BackfillSettings:
    target_seconds: float
    min_rows: int
    max_rows: int
    pause_seconds: float

    @classmethod
    def from_settings(cls) -> "BackfillSettings":
        config = cls(
            target_seconds=getattr(settings, "DEFERRED_MIGRATIONS_BACKFILL_TARGET_SECONDS", 0.5),
            min_rows=getattr(settings, "DEFERRED_MIGRATIONS_BACKFILL_MIN_ROWS", 100),
            max_rows=getattr(settings, "DEFERRED_MIGRATIONS_BACKFILL_MAX_ROWS", 10000),
            pause_seconds=getattr(settings, "DEFERRED_MIGRATIONS_BACKFILL_PAUSE_SECONDS", 0.05),
        )

        if config.target_seconds <= 0:
            raise ImproperlyConfigured(f"DEFERRED_MIGRATIONS_BACKFILL_TARGET_SECONDS must be above 0, got {config.target_seconds}")

        if config.min_rows < 1:
            raise ImproperlyConfigured(f"DEFERRED_MIGRATIONS_BACKFILL_MIN_ROWS must be at least 1, got {config.min_rows}")

        if config.max_rows < config.min_rows:
            raise ImproperlyConfigured(f"DEFERRED_MIGRATIONS_BACKFILL_MAX_ROWS ({config.max_rows}) must be at least DEFERRED_MIGRATIONS_BACKFILL_MIN_ROWS ({config.min_rows})")

        if config.pause_seconds < 0:
            raise ImproperlyConfigured(f"DEFERRED_MIGRATIONS_BACKFILL_PAUSE_SECONDS must not be negative, got {config.pause_seconds}")

        return config


# After pt-osc's --chunk-time: size each key range so its UPDATE takes about target_seconds, from a weighted average of rows scanned per second.
@dataclass
class BatchSizer:
    STARTING_ROWS: ClassVar[int] = 1000
    SAMPLE_WEIGHT: ClassVar[float] = 0.25
    MAX_GROWTH: ClassVar[int] = 2

    target_seconds: float
    min_rows: int
    max_rows: int
    size: int = field(init=False)
    rate: float | None = field(init=False, default=None)

    def __post_init__(self) -> None:
        self.size = self.clamp(self.STARTING_ROWS)

    def clamp(self, rows: int) -> int:
        return max(self.min_rows, min(self.max_rows, rows))

    def record(self, rows_scanned: int, seconds: float) -> None:
        sample = rows_scanned / max(seconds, 1e-6)
        self.rate = sample if self.rate is None else self.SAMPLE_WEIGHT * sample + (1 - self.SAMPLE_WEIGHT) * self.rate
        # Growth is capped so one fast batch over cached pages or a gap in the keys cannot produce a huge next one; shrinking is immediate.
        self.size = self.clamp(min(int(self.rate * self.target_seconds), self.size * self.MAX_GROWTH))

    def shrink(self) -> bool:
        if self.size <= self.min_rows:
            return False

        self.size = max(self.min_rows, self.size // 2)
        return True


def key_bounds(connection: BaseDatabaseWrapper, table: str, pk_column: str) -> tuple[object, object]:
    qn = partial(quote_for_params, connection)

    with connection.cursor() as cursor:
        cursor.execute(f"SELECT min({qn(pk_column)}), max({qn(pk_column)}) FROM {qn(table)}", [])
        return cursor.fetchone()


def next_boundary(connection: BaseDatabaseWrapper, table: str, pk_column: str, after: object, rows: int) -> tuple[object, bool]:
    qn = partial(quote_for_params, connection)
    lower_clause = "" if after is None else f"WHERE {qn(pk_column)} > %s"
    lower_params = [] if after is None else [after]

    with connection.cursor() as cursor:
        cursor.execute(f"SELECT {qn(pk_column)} FROM {qn(table)} {lower_clause} ORDER BY {qn(pk_column)} OFFSET %s LIMIT 1", [*lower_params, rows - 1])

        if (boundary := cursor.fetchone()) is not None:
            return boundary[0], False

        cursor.execute(f"SELECT max({qn(pk_column)}) FROM {qn(table)} {lower_clause}", lower_params)
        return cursor.fetchone()[0], True


def range_update(connection: BaseDatabaseWrapper, table: str, pk_column: str, set_sql: str, where_sql: str, lower: object, upper: object) -> tuple[str, list[object]]:
    qn = partial(quote_for_params, connection)
    lower_sql = "" if lower is None else f"{qn(pk_column)} > %s AND "
    params = [upper] if lower is None else [lower, upper]
    return f"UPDATE {qn(table)} SET {set_sql.replace('%', '%%')} WHERE {lower_sql}{qn(pk_column)} <= %s AND ({where_sql.replace('%', '%%')})", params


def run_update(connection: BaseDatabaseWrapper, sql: str, params: list[object]) -> int:
    with transaction.atomic(using=connection.alias), connection.cursor() as cursor:
        cursor.execute("SELECT set_config('lock_timeout', %s, true)", [lock_timeout_setting()])
        cursor.execute(sql, params)
        return cursor.rowcount


def run_batched_update(connection: BaseDatabaseWrapper, table: str, pk_column: str, set_sql: str, where_sql: str, label: str, sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic, reporter: ProgressReporter | None = None) -> int:
    config = BackfillSettings.from_settings()
    sizer = BatchSizer(config.target_seconds, config.min_rows, config.max_rows)
    lowest, highest = key_bounds(connection, table, pk_column)

    if lowest is None:
        return 0

    reporter = reporter or make_reporter(label)
    reporter.start(lowest, highest)
    retries = lock_retries_setting()
    last_pk: object = None
    updated = 0
    timeouts = 0

    try:
        while True:
            requested = sizer.size
            upper, is_last = next_boundary(connection, table, pk_column, last_pk, requested)

            if upper is None:
                return updated

            sql, params = range_update(connection, table, pk_column, set_sql, where_sql, last_pk, upper)
            started = clock()

            try:
                written = run_update(connection, sql, params)
            except OperationalError as error:
                if not is_lock_timeout(error):
                    raise

                # The budget counts every timeout for this starting key, halving attempts included, so a stuck row always ends in LockRetriesExhausted.
                timeouts += 1

                if timeouts >= retries:
                    raise LockRetriesExhausted(sql, retries) from error

                # Halving cannot exclude a locked row that is first in the range, so at the minimum size this falls back to backoff.
                if not sizer.shrink():
                    sleep(retry_delay_seconds(timeouts))

                continue

            # Only successful batches are sampled: a lock wait would poison the average.
            sizer.record(requested, clock() - started)
            timeouts = 0
            updated += written
            last_pk = upper
            reporter.advance(BatchReport(upper, updated, sizer.rate or 0.0, sizer.size))

            if is_last:
                return updated

            sleep(config.pause_seconds)
    finally:
        reporter.finish()
