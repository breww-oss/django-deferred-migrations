import random
import re
import time
from collections.abc import Callable
from collections.abc import Iterator
from contextlib import contextmanager

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import DatabaseError
from django.db.backends.base.base import BaseDatabaseWrapper
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from psycopg import errors as psycopg_errors

DATA_STATEMENT_KEYWORDS = frozenset({"SELECT", "INSERT", "UPDATE", "DELETE", "WITH", "MERGE", "COPY", "VALUES"})
LEADING_NOISE = re.compile(r"\A(?:\s+|--[^\n]*(?:\n|\Z)|/\*.*?\*/|\()+", re.DOTALL)
FIRST_WORD = re.compile(r"[A-Za-z]+")
CONCURRENT_STATEMENT = re.compile(r"\A(?:CREATE(?:\s+UNIQUE)?\s+INDEX\s+CONCURRENTLY|DROP\s+INDEX\s+CONCURRENTLY|REINDEX\s+(?:\([^)]*\)\s*)?(?:INDEX|TABLE|SCHEMA|DATABASE|SYSTEM)\s+CONCURRENTLY|REFRESH\s+MATERIALIZED\s+VIEW\s+CONCURRENTLY)\b", re.IGNORECASE)


class LockRetriesExhausted(Exception):
    def __init__(self, sql: str, attempts: int) -> None:
        super().__init__(f"Could not acquire a lock after {attempts} attempts for: {sql}")
        self.sql = sql
        self.attempts = attempts


def lock_timeout_setting() -> str:
    return getattr(settings, "DEFERRED_MIGRATIONS_DDL_LOCK_TIMEOUT", "2s")


def lock_retries_setting() -> int:
    value = getattr(settings, "DEFERRED_MIGRATIONS_LOCK_RETRIES", 10)

    if value < 1:
        raise ImproperlyConfigured(f"DEFERRED_MIGRATIONS_LOCK_RETRIES must be at least 1, got {value}")

    return value


def retry_delay_seconds(attempt: int) -> float:
    base = getattr(settings, "DEFERRED_MIGRATIONS_RETRY_BASE_SECONDS", 1)
    cap = getattr(settings, "DEFERRED_MIGRATIONS_RETRY_MAX_SECONDS", 30)
    return random.uniform(0, min(cap, base * 2 ** (attempt - 1)))


def is_concurrent_statement(sql: object) -> bool:
    return CONCURRENT_STATEMENT.match(LEADING_NOISE.sub("", str(sql), count=1)) is not None


def is_data_statement(sql: object) -> bool:
    text = LEADING_NOISE.sub("", str(sql), count=1)

    if (match := FIRST_WORD.match(text)) is None:
        return False

    return match.group(0).upper() in DATA_STATEMENT_KEYWORDS


def find_in_chain(error: BaseException, error_types: tuple[type[BaseException], ...]) -> BaseException | None:
    seen: set[int] = set()
    current: BaseException | None = error

    while current is not None and id(current) not in seen:
        if isinstance(current, error_types):
            return current

        seen.add(id(current))
        current = current.__cause__ or current.__context__

    return None


def is_lock_timeout(error: BaseException) -> bool:
    return find_in_chain(error, (psycopg_errors.LockNotAvailable,)) is not None


def set_lock_timeout(connection: BaseDatabaseWrapper, value: str, local: bool) -> None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT set_config('lock_timeout', %s, %s)", [value, local])


def current_lock_timeout(connection: BaseDatabaseWrapper) -> str:
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_setting('lock_timeout')")
        return cursor.fetchone()[0]


@contextmanager
def ddl_lock_timeout(connection: BaseDatabaseWrapper, sleep: Callable[[float], None] = time.sleep) -> Iterator[None]:
    editor_class = connection.SchemaEditorClass
    original_execute = editor_class.execute

    def execute(self: BaseDatabaseSchemaEditor, sql: object, params: object = ()) -> None:
        if self.collect_sql or is_data_statement(sql):
            return original_execute(self, sql, params)

        # Only the statement forms PostgreSQL actually runs concurrently may waive the timeout; searching the whole statement would waive it for anything merely mentioning the word, such as a column named "concurrently".
        timeout = "0" if is_concurrent_statement(sql) else lock_timeout_setting()
        database = self.connection

        if database.in_atomic_block:
            previous = current_lock_timeout(database)
            set_lock_timeout(database, timeout, local=True)
            original_execute(self, sql, params)
            set_lock_timeout(database, previous, local=True)
            return None

        attempts = lock_retries_setting()

        for attempt in range(1, attempts + 1):
            previous = current_lock_timeout(database)
            set_lock_timeout(database, timeout, local=False)

            try:
                original_execute(self, sql, params)
                return None
            except DatabaseError as error:
                if not is_lock_timeout(error):
                    raise

                if attempt == attempts:
                    raise LockRetriesExhausted(str(sql), attempts) from error

                sleep(retry_delay_seconds(attempt))
            finally:
                set_lock_timeout(database, previous, local=False)

        raise ValueError(f"Lock retry loop exited without returning or raising after {attempts} attempts")

    editor_class.execute = execute

    try:
        yield
    finally:
        editor_class.execute = original_execute
