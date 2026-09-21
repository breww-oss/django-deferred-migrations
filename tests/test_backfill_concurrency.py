import threading

import psycopg
import pytest
from django.db import connection
from pytest_django import Settings

from deferred_migrations.backfill import run_batched_update
from deferred_migrations.progress import NullReporter


@pytest.mark.django_db(transaction=True)
def test_a_batch_blocked_by_a_user_row_lock_times_out_and_retries(settings: Settings) -> None:
    settings.DEFERRED_MIGRATIONS_DDL_LOCK_TIMEOUT = "100ms"
    settings.DEFERRED_MIGRATIONS_BACKFILL_MIN_ROWS = 1000
    settings.DEFERRED_MIGRATIONS_BACKFILL_MAX_ROWS = 1000

    with connection.cursor() as cursor:
        cursor.execute("CREATE TABLE dm_backfill_probe (id integer PRIMARY KEY, a integer, b integer)")
        cursor.execute("INSERT INTO dm_backfill_probe SELECT n, n, NULL FROM generate_series(1, 5) n")

    ready = threading.Event()

    def hold_row() -> None:
        with psycopg.connect(**connection.get_connection_params()) as other:
            other.execute("SELECT * FROM dm_backfill_probe WHERE id = 3 FOR UPDATE")
            ready.set()
            threading.Event().wait(1)
            other.rollback()

    holder = threading.Thread(target=hold_row)
    holder.start()
    ready.wait(5)
    delays: list[float] = []

    updated = run_batched_update(connection, "dm_backfill_probe", "id", '"b" = "a"', '"b" IS DISTINCT FROM "a"', "backfill dm_backfill_probe.b", sleep=lambda seconds: (delays.append(seconds), threading.Event().wait(0.2)), reporter=NullReporter())

    holder.join()
    assert updated == 5
    assert delays
