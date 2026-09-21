import hashlib

from django.db.backends.base.base import BaseDatabaseWrapper
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

SYNC_PREFIX = "dm1_sync"
FILL_PREFIX = "dm2_fill"


def trigger_name(prefix: str, *parts: str) -> str:
    # NUL cannot appear in a PostgreSQL identifier, so no two different part lists can join to the same string.
    digest = hashlib.sha256("\x00".join(parts).encode()).hexdigest()[:16]
    return f"{prefix}_{digest}"


def substitute(sql: str, replacements: dict[str, str]) -> str:
    for placeholder, value in replacements.items():
        sql = sql.replace("{" + placeholder + "}", value)

    return sql


def install_trigger(schema_editor: BaseDatabaseSchemaEditor, table: str, name: str, body: str) -> None:
    if not schema_editor.collect_sql:
        with schema_editor.connection.cursor() as cursor:
            cursor.execute("SELECT prosrc FROM pg_proc p WHERE p.proname = %s AND p.pronargs = 0 AND pg_catalog.pg_function_is_visible(p.oid)", [name])
            existing = cursor.fetchone()

        if existing is not None and existing[0] != body:
            raise ValueError(f"A trigger function named {name} already exists with a different body; refusing to replace another operation's trigger.")

    quoted = schema_editor.quote_name(name)
    schema_editor.execute(f"CREATE OR REPLACE FUNCTION {quoted}() RETURNS trigger LANGUAGE plpgsql AS $deferred_migrations${body}$deferred_migrations$", params=None)
    schema_editor.execute(f"CREATE OR REPLACE TRIGGER {quoted} BEFORE INSERT OR UPDATE ON {schema_editor.quote_name(table)} FOR EACH ROW EXECUTE FUNCTION {quoted}()", params=None)


def drop_trigger_sql(schema_editor: BaseDatabaseSchemaEditor, table: str, name: str) -> str:
    quoted = schema_editor.quote_name(name)
    return f"DROP TRIGGER IF EXISTS {quoted} ON {schema_editor.quote_name(table)}; DROP FUNCTION IF EXISTS {quoted}()"


def sync_function_body(from_ref: str, to_ref: str, forwards_sql: str, backwards_sql: str | None) -> str:
    new_from = f"NEW.{from_ref}"
    new_to = f"NEW.{to_ref}"
    forwards = f"({substitute(forwards_sql, {'from': new_from, 'to': new_to})})"
    insert_backwards = ""
    update_backwards = ""

    if backwards_sql is not None:
        backwards = f"({substitute(backwards_sql, {'from': new_from, 'to': new_to})})"
        insert_backwards = f"\n    ELSIF {new_to} IS NOT NULL AND {new_to} IS DISTINCT FROM {forwards} THEN\n      {new_from} := {backwards};"
        update_backwards = f"\n    ELSIF {new_to} IS DISTINCT FROM OLD.{to_ref} AND {new_from} IS NOT DISTINCT FROM OLD.{from_ref} AND {new_to} IS DISTINCT FROM {forwards} THEN\n      {new_from} := {backwards};"

    return (
        "\nBEGIN\n"
        "  IF TG_OP = 'INSERT' THEN\n"
        f"    IF {new_to} IS NULL AND {new_from} IS NOT NULL THEN\n      {new_to} := {forwards};{insert_backwards}\n    END IF;\n"
        "  ELSE\n"
        f"    IF {new_from} IS DISTINCT FROM OLD.{from_ref} AND {new_to} IS NOT DISTINCT FROM OLD.{to_ref} THEN\n      {new_to} := {forwards};{update_backwards}\n    END IF;\n"
        "  END IF;\n"
        "  RETURN NEW;\n"
        "END;\n"
    )


def quote_for_params(connection: BaseDatabaseWrapper, name: str) -> str:
    # These statements always carry a params sequence, so psycopg parses them for placeholders and a % in an identifier has to be escaped.
    return connection.ops.quote_name(name).replace("%", "%%")
