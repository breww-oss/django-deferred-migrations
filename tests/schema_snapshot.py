from collections.abc import Sequence

from django.db import connection

from tests.migration_helpers import relation_kind


# Columns are keyed by name rather than ordinal position on purpose. PostgreSQL attaches no meaning
# to column order, and Django's own drop-and-re-add reorders too, so comparing positions would fail
# on a difference that is not a difference.
def _columns(table: str) -> list[tuple]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT column_name, data_type, character_maximum_length, numeric_precision, numeric_scale,
                   is_nullable, column_default, is_identity, identity_generation, is_generated, generation_expression
            FROM information_schema.columns
            WHERE table_schema = current_schema() AND table_name = %s
            ORDER BY column_name
            """,
            [table],
        )
        return cursor.fetchall()


def _indexes(table: str) -> list[tuple]:
    with connection.cursor() as cursor:
        cursor.execute("SELECT c.relname, pg_get_indexdef(i.indexrelid) FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid WHERE i.indrelid = %s::regclass ORDER BY 1", [table])
        return cursor.fetchall()


def _constraints(table: str) -> list[tuple]:
    with connection.cursor() as cursor:
        cursor.execute("SELECT conname, pg_get_constraintdef(oid), convalidated FROM pg_constraint WHERE conrelid = %s::regclass ORDER BY 1", [table])
        return cursor.fetchall()


def _triggers(table: str) -> list[tuple]:
    with connection.cursor() as cursor:
        cursor.execute("SELECT tgname, pg_get_triggerdef(oid) FROM pg_trigger WHERE tgrelid = %s::regclass AND NOT tgisinternal ORDER BY 1", [table])
        return cursor.fetchall()


def _sequences(table: str) -> list[str]:
    with connection.cursor() as cursor:
        cursor.execute("SELECT c.relname FROM pg_class c JOIN pg_depend d ON d.objid = c.oid JOIN pg_class t ON t.oid = d.refobjid WHERE c.relkind = 'S' AND t.relname = %s ORDER BY 1", [table])
        return [row[0] for row in cursor.fetchall()]


# Every function in the schema, not just ones matching a prefix: the package installs trigger
# functions during the deferred window, and the point of the comparison is that none survive it.
def _functions() -> list[tuple]:
    with connection.cursor() as cursor:
        # prokind = 'f' restricts this to plain functions. pg_get_functiondef raises on aggregates
        # and window functions, and nothing should make the snapshot itself a source of errors.
        cursor.execute("SELECT p.proname, pg_get_functiondef(p.oid) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = current_schema() AND p.prokind = 'f' ORDER BY 1, 2")
        return cursor.fetchall()


def _table_exists(table: str) -> bool:
    with connection.cursor() as cursor:
        cursor.execute("SELECT to_regclass(%s) IS NOT NULL", [table])
        return cursor.fetchone()[0]


# Each name is dropped by what it actually is. DeferredRenameModel leaves a compatibility view under
# the old name until the queued drop runs, so a name can be a table in one arm and a view in the
# other. Neither DROP statement tolerates the other kind even with IF EXISTS ("is not a view" /
# "is not a table"), and an error raised inside a test's finally would replace the real failure.
def drop_relations(names: Sequence[str]) -> None:
    with connection.cursor() as cursor:
        for name in names:
            match relation_kind(name):
                case "v":
                    cursor.execute(f"DROP VIEW {connection.ops.quote_name(name)} CASCADE")
                case "r":
                    cursor.execute(f"DROP TABLE {connection.ops.quote_name(name)} CASCADE")
                case None:
                    pass
                case kind:
                    raise AssertionError(f"Unexpected relation kind {kind!r} for {name}")

        # DROP TABLE removes a table's triggers but not the functions they call (triggers.py:33
        # creates them as separate objects). These tests are transactional, so a leftover would leak
        # into every later snapshot's "functions" key. The prefixes are SYNC_PREFIX and FILL_PREFIX
        # from deferred_migrations/triggers.py:6-7.
        cursor.execute("SELECT proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = current_schema() AND (proname LIKE 'dm1\\_sync\\_%' OR proname LIKE 'dm2\\_fill\\_%')")

        for (function,) in cursor.fetchall():
            cursor.execute(f"DROP FUNCTION IF EXISTS {connection.ops.quote_name(function)}() CASCADE")


def schema_snapshot(tables: Sequence[str]) -> dict[str, object]:
    snapshot: dict[str, object] = {"functions": _functions()}

    for table in tables:
        if not _table_exists(table):
            snapshot[table] = None
            continue

        snapshot[table] = {
            "columns": _columns(table),
            "indexes": _indexes(table),
            "constraints": _constraints(table),
            "triggers": _triggers(table),
            "sequences": _sequences(table),
        }

    return snapshot
