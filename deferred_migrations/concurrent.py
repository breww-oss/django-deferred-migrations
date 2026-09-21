from django.db import DatabaseError
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.backends.ddl_references import Statement
from django.db.backends.utils import strip_quotes

INDEX_PREFIXES = ("CREATE UNIQUE INDEX ", "CREATE INDEX ")
# Django renders a fields-only unique constraint as ALTER TABLE ... ADD CONSTRAINT ... UNIQUE, which cannot run concurrently, so its own parts are re-rendered as a concurrent unique index that is then attached.
UNIQUE_INDEX_FOR_CONSTRAINT = "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS %(name)s ON %(table)s (%(columns)s)%(nulls_distinct)s"
ATTACH_UNIQUE_INDEX = "ALTER TABLE %(table)s ADD CONSTRAINT %(name)s UNIQUE USING INDEX %(name)s%(deferrable)s"


def statement_name(statement: Statement) -> str:
    return strip_quotes(str(statement.parts["name"]))


def concurrent_index(statement: Statement) -> Statement:
    for prefix in INDEX_PREFIXES:
        if statement.template.startswith(prefix):
            return Statement(f"{prefix}CONCURRENTLY IF NOT EXISTS {statement.template.removeprefix(prefix)}", **statement.parts)

    raise ValueError(f"Expected a CREATE INDEX statement, got {statement.template!r}.")


def not_valid(statement: Statement) -> Statement:
    return Statement(f"{statement.template} NOT VALID", **statement.parts)


def index_validity(schema_editor: BaseDatabaseSchemaEditor, table: str, name: str) -> bool | None:
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            "SELECT i.indisvalid FROM pg_index i JOIN pg_class ic ON ic.oid = i.indexrelid JOIN pg_class tc ON tc.oid = i.indrelid WHERE ic.relname = %s AND tc.relname = %s AND pg_catalog.pg_table_is_visible(tc.oid)",
            [name, table],
        )
        row = cursor.fetchone()

    return None if row is None else row[0]


def constraint_validity(schema_editor: BaseDatabaseSchemaEditor, table: str, name: str) -> bool | None:
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            "SELECT co.convalidated FROM pg_constraint co JOIN pg_class c ON c.oid = co.conrelid WHERE co.conname = %s AND c.relname = %s AND pg_catalog.pg_table_is_visible(c.oid)",
            [name, table],
        )
        row = cursor.fetchone()

    return None if row is None else row[0]


def drop_index_concurrently(schema_editor: BaseDatabaseSchemaEditor, name: str) -> None:
    schema_editor.execute(schema_editor.sql_delete_index_concurrently % {"name": schema_editor.quote_name(name)}, params=None)


def build_index_concurrently(schema_editor: BaseDatabaseSchemaEditor, table: str, statement: Statement, clean_up_on_failure: bool = False) -> None:
    name = statement_name(statement)

    if not schema_editor.collect_sql:
        validity = index_validity(schema_editor, table, name)

        if validity is True:
            return

        # IF NOT EXISTS would skip the INVALID leftover of a failed build, and USING INDEX refuses one.
        if validity is False:
            drop_index_concurrently(schema_editor, name)

    if not clean_up_on_failure:
        schema_editor.execute(statement, params=None)
        return

    try:
        schema_editor.execute(statement, params=None)
    except DatabaseError:
        # A failed unique build leaves an INVALID index that still rejects duplicate writes from the release still serving.
        drop_index_concurrently(schema_editor, name)
        raise


def add_constraint(schema_editor: BaseDatabaseSchemaEditor, table: str, statement: Statement, validate: bool) -> None:
    name = statement_name(statement)
    validity = None if schema_editor.collect_sql else constraint_validity(schema_editor, table, name)

    if validity is None:
        schema_editor.execute(statement, params=None)

    if validate and validity is not True:
        schema_editor.execute(f"ALTER TABLE {schema_editor.quote_name(table)} VALIDATE CONSTRAINT {schema_editor.quote_name(name)}", params=None)


def attach_unique_index(schema_editor: BaseDatabaseSchemaEditor, table: str, statement: Statement) -> None:
    build_index_concurrently(schema_editor, table, Statement(UNIQUE_INDEX_FOR_CONSTRAINT, **statement.parts), clean_up_on_failure=True)
    add_constraint(schema_editor, table, Statement(ATTACH_UNIQUE_INDEX, **statement.parts), validate=False)
