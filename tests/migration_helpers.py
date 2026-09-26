from django.db import DEFAULT_DB_ALIAS
from django.db import connection
from django.db import connections
from django.db import models
from django.db.migrations.migration import Migration
from django.db.migrations.operations.base import Operation
from django.db.migrations.state import ProjectState


def apply_operations(app_label: str, project_state: ProjectState, operations: list[Operation], atomic: bool = True, name: str = "0001_test") -> ProjectState:
    migration = Migration(name, app_label)
    migration.operations = operations
    migration.atomic = atomic

    with connection.schema_editor(atomic=atomic) as editor:
        return migration.apply(project_state.clone(), editor)


# Like MigrationExecutor, Migration.unapply takes the state from BEFORE the migration was applied.
def unapply_operations(app_label: str, state_before: ProjectState, operations: list[Operation], atomic: bool = True, name: str = "0001_test") -> ProjectState:
    project_state = state_before
    migration = Migration(name, app_label)
    migration.operations = operations
    migration.atomic = atomic

    with connection.schema_editor(atomic=atomic) as editor:
        return migration.unapply(project_state.clone(), editor)


def column_names(table: str) -> set[str]:
    with connection.cursor() as cursor:
        return {column.name for column in connection.introspection.get_table_description(cursor, table)}


def table_exists(table: str) -> bool:
    with connection.cursor() as cursor:
        return table in connection.introspection.table_names(cursor)


def foreign_key_constraint_names(table: str, column: str) -> list[str]:
    with connection.cursor() as cursor:
        constraints = connection.introspection.get_constraints(cursor, table)

    return [name for name, info in constraints.items() if info["foreign_key"] and info["columns"] == [column]]


def nullability_and_default(table: str, column: str) -> tuple[str, str | None]:
    with connection.cursor() as cursor:
        cursor.execute("SELECT is_nullable, column_default FROM information_schema.columns WHERE table_schema = current_schema() AND table_name = %s AND column_name = %s", [table, column])
        return cursor.fetchone()


def relation_kind(name: str, using: str = DEFAULT_DB_ALIAS) -> str | None:
    with connections[using].cursor() as cursor:
        cursor.execute("SELECT relkind FROM pg_class WHERE oid = to_regclass(%s)", [name])
        row = cursor.fetchone()

    return None if row is None else row[0]


def define_test_model(name: str, fields: dict[str, models.Field], options: dict[str, object] | None = None) -> type[models.Model]:
    meta = type("Meta", (), {"app_label": "deferred_migrations_testapp", **(options or {})})
    return type(name, (models.Model,), {"__module__": "tests.testapp.models", "Meta": meta, **fields})
