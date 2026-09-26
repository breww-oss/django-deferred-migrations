import itertools

from django.db import DatabaseError
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.backends.ddl_references import Statement
from django.db.backends.utils import strip_quotes

# PostgreSQL's identifier limit: names are at most NAMEDATALEN - 1 bytes.
NAMEDATALEN = 64
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


# PostgreSQL's makeObjectName: trim the longer of the two names a byte at a time until "<table>_<column>_<label>" fits in NAMEDATALEN - 1 bytes, then clip each without splitting a multibyte character.
def object_name(table: str, column: str, label: str) -> str:
    table_bytes, column_bytes = table.encode(), column.encode()
    table_length, column_length = len(table_bytes), len(column_bytes)
    available = NAMEDATALEN - 1 - len(label) - 2

    while table_length + column_length > available:
        if table_length > column_length:
            table_length -= 1
        else:
            column_length -= 1

    return f"{table_bytes[:table_length].decode(errors='ignore')}_{column_bytes[:column_length].decode(errors='ignore')}_{label}"


# The name PostgreSQL gives the constraint when AddField writes UNIQUE inline: ChooseRelationName tries key, key1, key2... until no relation or constraint in the table's schema holds it.
# A plain unique index or constraint on exactly this column of this table is not a clash (indkey is an int2vector indexed from 0, so it is compared by element, not as an array): it is this operation's own work from an interrupted run, and re-using its name lets the re-run pick up where it stopped. A partial or expression index is a clash, since ADD CONSTRAINT ... USING INDEX refuses one.
# Django leaves a quoted db_table as written, while PostgreSQL names and stores the relation unquoted.
def inline_unique_name(schema_editor: BaseDatabaseSchemaEditor, table: str, column: str) -> str:
    table = strip_quotes(table)

    for attempt in itertools.count():
        name = object_name(table, column, "key" if attempt == 0 else f"key{attempt}")

        if schema_editor.collect_sql or not name_taken(schema_editor, table, column, name):
            return name


def name_taken(schema_editor: BaseDatabaseSchemaEditor, table: str, column: str, name: str) -> bool:
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT EXISTS (
                       SELECT 1 FROM pg_class c
                       WHERE c.relname = %(name)s AND c.relnamespace = t.relnamespace
                         AND NOT EXISTS (SELECT 1 FROM pg_index i WHERE i.indexrelid = c.oid AND i.indrelid = t.oid AND i.indisunique AND i.indnatts = 1 AND i.indkey[0] = a.attnum AND i.indpred IS NULL AND i.indexprs IS NULL)
                   )
                OR EXISTS (
                       SELECT 1 FROM pg_constraint co
                       WHERE co.conname = %(name)s AND co.connamespace = t.relnamespace
                         AND NOT (co.conrelid = t.oid AND co.contype = 'u' AND co.conkey = ARRAY[a.attnum])
                   )
            FROM pg_class t JOIN pg_attribute a ON a.attrelid = t.oid AND a.attname = %(column)s
            WHERE t.relname = %(table)s AND pg_catalog.pg_table_is_visible(t.oid)
            """,
            {"name": name, "table": table, "column": column},
        )
        row = cursor.fetchone()

    # No row means the table is not visible on the search_path, so nothing here can hold the name either.
    return row is not None and row[0]


def attach_unique_index(schema_editor: BaseDatabaseSchemaEditor, table: str, statement: Statement) -> None:
    build_index_concurrently(schema_editor, table, Statement(UNIQUE_INDEX_FOR_CONSTRAINT, **statement.parts), clean_up_on_failure=True)
    add_constraint(schema_editor, table, Statement(ATTACH_UNIQUE_INDEX, **statement.parts), validate=False)
