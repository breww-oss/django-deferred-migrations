import itertools
import threading
from io import StringIO

import psycopg
import pytest
from django.core.exceptions import ImproperlyConfigured
from django.db import connection
from pytest_django import Settings

from deferred_migrations.backfill import BackfillSettings
from deferred_migrations.backfill import BatchSizer
from deferred_migrations.backfill import run_batched_update
from deferred_migrations.context import progress_output
from deferred_migrations.locking import LockRetriesExhausted
from deferred_migrations.progress import BatchReport


class RecordingReporter:
    def __init__(self) -> None:
        self.reports: list[BatchReport] = []
        self.bounds: tuple[object, object] | None = None
        self.finished = False

    def start(self, lowest: object, highest: object) -> None:
        self.bounds = (lowest, highest)

    def advance(self, report: BatchReport) -> None:
        self.reports.append(report)

    def finish(self) -> None:
        self.finished = True


def test_the_first_sample_sets_the_rate_and_the_size_follows_the_target() -> None:
    sizer = BatchSizer(target_seconds=0.5, min_rows=100, max_rows=10000)

    sizer.record(1000, 0.25)

    assert (sizer.rate, sizer.size) == (4000, 2000)


def test_later_samples_are_weighted_a_quarter_against_the_history() -> None:
    sizer = BatchSizer(target_seconds=0.5, min_rows=100, max_rows=10000)
    sizer.record(1000, 0.25)

    sizer.record(2000, 0.1)

    assert (sizer.rate, sizer.size) == (8000, 4000)


def test_the_size_at_most_doubles_per_batch() -> None:
    sizer = BatchSizer(target_seconds=0.5, min_rows=100, max_rows=10000)

    sizer.record(1000, 0.001)

    assert sizer.size == 2000


def test_the_size_is_clamped_to_the_configured_range() -> None:
    fast = BatchSizer(target_seconds=0.5, min_rows=100, max_rows=1500)
    slow = BatchSizer(target_seconds=0.5, min_rows=100, max_rows=1500)

    fast.record(1000, 0.25)
    slow.record(1000, 60)

    # 4000 rows/s wants 2000 rows, capped at 1500; 16.7 rows/s wants 8 rows, raised to 100.
    assert (fast.size, slow.size) == (1500, 100)


def test_the_size_shrinks_immediately_when_a_batch_is_slow() -> None:
    sizer = BatchSizer(target_seconds=0.5, min_rows=100, max_rows=10000)

    sizer.record(1000, 5.0)

    assert sizer.size == 100


def test_shrinking_halves_down_to_the_minimum_and_then_reports_it_cannot() -> None:
    sizer = BatchSizer(target_seconds=0.5, min_rows=300, max_rows=10000)

    assert sizer.shrink()
    assert sizer.size == 500
    assert sizer.shrink()
    assert sizer.size == 300
    assert not sizer.shrink()
    assert sizer.size == 300


def test_the_starting_size_is_clamped_into_range() -> None:
    assert BatchSizer(target_seconds=0.5, min_rows=1, max_rows=64).size == 64
    assert BatchSizer(target_seconds=0.5, min_rows=2000, max_rows=5000).size == 2000


@pytest.mark.parametrize(("name", "value"), [("DEFERRED_MIGRATIONS_BACKFILL_TARGET_SECONDS", 0), ("DEFERRED_MIGRATIONS_BACKFILL_MIN_ROWS", 0), ("DEFERRED_MIGRATIONS_BACKFILL_MAX_ROWS", 50), ("DEFERRED_MIGRATIONS_BACKFILL_PAUSE_SECONDS", -1)])
def test_invalid_settings_are_refused(settings: Settings, name: str, value: float) -> None:
    setattr(settings, name, value)

    with pytest.raises(ImproperlyConfigured, match=name):
        BackfillSettings.from_settings()


@pytest.fixture
def probe_table() -> str:
    with connection.cursor() as cursor:
        cursor.execute("CREATE TABLE dm_backfill_probe (id integer PRIMARY KEY, a integer, b integer)")
        cursor.execute("INSERT INTO dm_backfill_probe SELECT n, n, NULL FROM generate_series(1, 200) n")

    return "dm_backfill_probe"


@pytest.mark.django_db
def test_batch_sizes_follow_the_measured_rate(settings: Settings, probe_table: str) -> None:
    settings.DEFERRED_MIGRATIONS_BACKFILL_MIN_ROWS = 1
    settings.DEFERRED_MIGRATIONS_BACKFILL_MAX_ROWS = 64
    settings.DEFERRED_MIGRATIONS_BACKFILL_PAUSE_SECONDS = 0
    ticks = itertools.count(step=1.0)
    reporter = RecordingReporter()

    updated = run_batched_update(connection, probe_table, "id", '"b" = "a"', '"b" IS DISTINCT FROM "a"', "backfill dm_backfill_probe.b", clock=lambda: next(ticks), reporter=reporter)

    # Each batch "takes" one second, so a 64-row batch measures 64 rows/s; at a 0.5s target the next is 32, then the average of 32 and 64 weighted 1:3 is 56, giving 28.
    assert [report.batch_size for report in reporter.reports[:2]] == [32, 28]
    assert updated == 200
    assert reporter.bounds == (1, 200)
    assert reporter.finished


@pytest.mark.django_db
def test_an_empty_table_writes_nothing_and_reports_nothing(probe_table: str) -> None:
    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM dm_backfill_probe")

    reporter = RecordingReporter()

    assert run_batched_update(connection, probe_table, "id", '"b" = "a"', '"b" IS DISTINCT FROM "a"', "backfill dm_backfill_probe.b", reporter=reporter) == 0
    assert reporter.bounds is None


# TTY_INTERACTIVE=0 because an inherited FORCE_COLOR would otherwise make rich treat the StringIO as a terminal.
@pytest.mark.django_db
def test_progress_goes_to_the_output_the_command_set(settings: Settings, probe_table: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TTY_INTERACTIVE", "0")
    settings.DEFERRED_MIGRATIONS_BACKFILL_PAUSE_SECONDS = 0
    stream = StringIO()

    with progress_output(stream, 1):
        run_batched_update(connection, probe_table, "id", '"b" = "a"', '"b" IS DISTINCT FROM "a"', "backfill dm_backfill_probe.b")

    assert stream.getvalue().startswith("backfill dm_backfill_probe.b: 200/200 (100%), 200 written")


@pytest.mark.django_db(transaction=True)
def test_a_lock_timeout_halves_the_range_so_rows_before_the_locked_one_are_written(settings: Settings) -> None:
    settings.DEFERRED_MIGRATIONS_DDL_LOCK_TIMEOUT = "100ms"
    settings.DEFERRED_MIGRATIONS_LOCK_RETRIES = 30
    settings.DEFERRED_MIGRATIONS_BACKFILL_MIN_ROWS = 1
    settings.DEFERRED_MIGRATIONS_BACKFILL_MAX_ROWS = 8
    settings.DEFERRED_MIGRATIONS_BACKFILL_PAUSE_SECONDS = 0

    with connection.cursor() as cursor:
        cursor.execute("CREATE TABLE dm_backfill_halving (id integer PRIMARY KEY, a integer, b integer)")
        cursor.execute("INSERT INTO dm_backfill_halving SELECT n, n, NULL FROM generate_series(1, 5) n")

    ready = threading.Event()

    def hold_row() -> None:
        with psycopg.connect(**connection.get_connection_params()) as other:
            other.execute("SELECT * FROM dm_backfill_halving WHERE id = 3 FOR UPDATE")
            ready.set()
            threading.Event().wait(1)
            other.rollback()

    holder = threading.Thread(target=hold_row)
    holder.start()
    ready.wait(5)
    reporter = RecordingReporter()
    ticks = itertools.count(step=1.0)

    try:
        updated = run_batched_update(connection, "dm_backfill_halving", "id", '"b" = "a"', '"b" IS DISTINCT FROM "a"', "backfill dm_backfill_halving.b", sleep=lambda seconds: threading.Event().wait(0.2), clock=lambda: next(ticks), reporter=reporter)
    finally:
        holder.join()

    # 8 rows cover the locked row 3; halving to 4 still does, and 2 leaves rows 1 and 2, which commit while row 3 is held.
    assert reporter.reports[0].position == 2
    # The clock ticks once per attempt start and once per success, so the first success measures 2 rows in 1s; a timed-out attempt folded into the rate would change it.
    assert reporter.reports[0].rows_per_second == 2.0
    assert updated == 5


@pytest.mark.django_db(transaction=True)
def test_exhausting_the_lock_budget_raises(settings: Settings) -> None:
    settings.DEFERRED_MIGRATIONS_DDL_LOCK_TIMEOUT = "50ms"
    settings.DEFERRED_MIGRATIONS_LOCK_RETRIES = 3
    settings.DEFERRED_MIGRATIONS_BACKFILL_MIN_ROWS = 1

    with connection.cursor() as cursor:
        cursor.execute("CREATE TABLE dm_backfill_budget (id integer PRIMARY KEY, a integer, b integer)")
        cursor.execute("INSERT INTO dm_backfill_budget VALUES (1, 1, NULL)")

    ready = threading.Event()
    release = threading.Event()

    def hold_row() -> None:
        with psycopg.connect(**connection.get_connection_params()) as other:
            other.execute("SELECT * FROM dm_backfill_budget WHERE id = 1 FOR UPDATE")
            ready.set()
            release.wait(10)
            other.rollback()

    holder = threading.Thread(target=hold_row)
    holder.start()
    ready.wait(5)

    sleeps: list[float] = []

    try:
        with pytest.raises(LockRetriesExhausted):
            run_batched_update(connection, "dm_backfill_budget", "id", '"b" = "a"', '"b" IS DISTINCT FROM "a"', "backfill dm_backfill_budget.b", sleep=sleeps.append, reporter=RecordingReporter())
    finally:
        release.set()
        holder.join()

    # Three retries end while the batch is still halving from 1,000 rows, so a backoff sleep would mean the halvings were not counted.
    assert sleeps == []
