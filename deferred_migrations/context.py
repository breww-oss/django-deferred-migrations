import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TextIO

from django.core.exceptions import ImproperlyConfigured
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.migration import Migration
from django.db.migrations.operations.base import Operation
from django.db.migrations.state import ProjectState

_current_migration: ContextVar[Migration | None] = ContextVar("deferred_migrations_current_migration", default=None)


@dataclass(frozen=True)
class OperationKey:
    app_label: str
    migration_name: str
    operation_index: int


@dataclass(frozen=True)
class OutputTarget:
    stream: TextIO
    verbosity: int


_output: ContextVar[OutputTarget | None] = ContextVar("deferred_migrations_output", default=None)


# Operations only receive a schema editor, so the command that runs them sets where their progress goes.
@contextmanager
def progress_output(stream: TextIO, verbosity: int) -> Iterator[None]:
    token = _output.set(OutputTarget(stream, verbosity))

    try:
        yield
    finally:
        _output.reset(token)


def current_output() -> OutputTarget:
    return _output.get() or OutputTarget(sys.stdout, 1)


def install_migration_context() -> None:
    if Migration.apply.__dict__.get("deferred_migrations_wrapped"):
        return

    original_apply = Migration.apply
    original_unapply = Migration.unapply

    def apply(self: Migration, project_state: ProjectState, schema_editor: BaseDatabaseSchemaEditor, collect_sql: bool = False) -> ProjectState:
        token = _current_migration.set(self)

        try:
            return original_apply(self, project_state, schema_editor, collect_sql)
        finally:
            _current_migration.reset(token)

    def unapply(self: Migration, project_state: ProjectState, schema_editor: BaseDatabaseSchemaEditor, collect_sql: bool = False) -> ProjectState:
        token = _current_migration.set(self)

        try:
            return original_unapply(self, project_state, schema_editor, collect_sql)
        finally:
            _current_migration.reset(token)

    apply.deferred_migrations_wrapped = True
    Migration.apply = apply
    Migration.unapply = unapply


def operation_key(operation: Operation) -> OperationKey:
    migration = _current_migration.get()

    if migration is None:
        raise ImproperlyConfigured(f"{operation.__class__.__name__} can only run inside a migration. Is deferred_migrations in INSTALLED_APPS?")

    for index, candidate in enumerate(migration.operations):
        if candidate is operation:
            return OperationKey(migration.app_label, migration.name, index)

    raise ImproperlyConfigured(f"{operation.__class__.__name__} must be a top-level operation of {migration.app_label}.{migration.name}, not nested inside another operation.")
