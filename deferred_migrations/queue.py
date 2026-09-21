from django.db.backends.base.schema import BaseDatabaseSchemaEditor

from deferred_migrations.context import OperationKey
from deferred_migrations.models import DeferredOperation
from deferred_migrations.models import ModelRename
from deferred_migrations.schema import drop_column_sql
from deferred_migrations.schema import drop_table_sql


def check_identifier(label: str, name: str) -> None:
    # quote_name only wraps a name in double quotes, so an embedded quote or NUL would change the meaning of DDL this package stores and replays verbatim in a privileged Job days later.
    if '"' in name or "\x00" in name:
        raise ValueError(f"Cannot defer an operation on {label} {name!r}: an identifier may not contain a double quote or a NUL byte.")


def queue_statement(schema_editor: BaseDatabaseSchemaEditor, key: OperationKey, sequence: int, kind: DeferredOperation.Kind, table_name: str, column_name: str, sql: str) -> None:
    check_identifier("table", table_name)

    if column_name:
        check_identifier("column", column_name)

    table = schema_editor.quote_name(DeferredOperation._meta.db_table)
    schema_editor.execute(
        f"INSERT INTO {table} (app_label, migration_name, operation_index, sequence, kind, table_name, column_name, sql, status, attempts, last_error, resolution_reason, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'pending', 0, '', '', now()) ON CONFLICT (app_label, migration_name, operation_index, sequence) DO NOTHING",
        [key.app_label, key.migration_name, key.operation_index, sequence, kind, table_name, column_name, sql],
    )


def delete_queued(schema_editor: BaseDatabaseSchemaEditor, key: OperationKey) -> None:
    table = schema_editor.quote_name(DeferredOperation._meta.db_table)
    schema_editor.execute(f"DELETE FROM {table} WHERE app_label = %s AND migration_name = %s AND operation_index = %s", [key.app_label, key.migration_name, key.operation_index])


def retarget_statement(schema_editor: BaseDatabaseSchemaEditor, kind: str, sql: str, column_name: str, old_table: str, new_table: str) -> str:
    match kind:
        case DeferredOperation.Kind.DROP_COLUMN:
            return drop_column_sql(schema_editor, new_table, column_name)
        case DeferredOperation.Kind.DROP_TABLE:
            return drop_table_sql(schema_editor, new_table)
        case DeferredOperation.Kind.DROP_TRIGGER:
            # The trigger keeps its name through a table rename, so only its ON clause moves.
            old_clause = f" ON {schema_editor.quote_name(old_table)};"

            if sql.count(old_clause) != 1:
                raise ValueError(f"Cannot retarget the trigger drop {sql!r} from {old_table} to {new_table}.")

            return sql.replace(old_clause, f" ON {schema_editor.quote_name(new_table)};")
        case DeferredOperation.Kind.DROP_VIEW:
            return sql
        case _:
            raise ValueError(f"Unknown deferred operation kind of {kind}")


def rewrite_rows(schema_editor: BaseDatabaseSchemaEditor, rows: list[tuple[int, str, str, str]], old_table: str, new_table: str) -> None:
    table = schema_editor.quote_name(DeferredOperation._meta.db_table)

    for pk, kind, sql, column_name in rows:
        schema_editor.execute(f"UPDATE {table} SET table_name = %s, sql = %s WHERE id = %s", [new_table, retarget_statement(schema_editor, kind, sql, column_name, old_table, new_table), pk])


# Queued SQL names the table it was generated for, so after a rename it would hit the view: DROP COLUMN errors on a view and DROP TRIGGER ... ON a view silently does nothing. Skipped rows are included because --unskip would revive them.
def retarget_queued_rows(schema_editor: BaseDatabaseSchemaEditor, old_table: str, new_table: str) -> list[int]:
    if schema_editor.collect_sql:
        schema_editor.execute(f"-- Queued drops on {old_table} are retargeted to {new_table} at migrate time", params=None)
        return []

    table = schema_editor.quote_name(DeferredOperation._meta.db_table)

    with schema_editor.connection.cursor() as cursor:
        cursor.execute(f"SELECT id, kind, sql, column_name FROM {table} WHERE table_name = %s AND status IN (%s, %s, %s) ORDER BY id", [old_table, DeferredOperation.Status.PENDING, DeferredOperation.Status.FAILED, DeferredOperation.Status.SKIPPED])
        rows = cursor.fetchall()

    rewrite_rows(schema_editor, rows, old_table, new_table)
    return [row[0] for row in rows]


def record_model_rename(schema_editor: BaseDatabaseSchemaEditor, app_label: str, old_model: str, new_model: str, rewritten_ids: list[int]) -> None:
    table = schema_editor.quote_name(ModelRename._meta.db_table)
    schema_editor.execute(f"INSERT INTO {table} (app_label, old_model, new_model, rewritten_operation_ids, created_at) VALUES (%s, %s, %s, to_jsonb(%s::bigint[]), now())", [app_label, old_model, new_model, rewritten_ids])


def unrecord_model_rename(schema_editor: BaseDatabaseSchemaEditor, app_label: str, old_model: str, new_model: str, current_table: str, restored_table: str) -> None:
    records = schema_editor.quote_name(ModelRename._meta.db_table)

    if schema_editor.collect_sql:
        schema_editor.execute(f"-- Queued drops retargeted by the rename are pointed back at {restored_table} at migrate time", params=None)
        schema_editor.execute(f"DELETE FROM {records} WHERE app_label = %s AND old_model = %s AND new_model = %s", [app_label, old_model, new_model])
        return

    with schema_editor.connection.cursor() as cursor:
        cursor.execute(f"SELECT id, ARRAY(SELECT jsonb_array_elements_text(rewritten_operation_ids)::bigint) FROM {records} WHERE app_label = %s AND old_model = %s AND new_model = %s ORDER BY created_at DESC, id DESC LIMIT 1", [app_label, old_model, new_model])

        if (record := cursor.fetchone()) is None:
            return

        record_id, rewritten_ids = record
        cursor.execute(f"SELECT id, kind, sql, column_name FROM {schema_editor.quote_name(DeferredOperation._meta.db_table)} WHERE id = ANY(%s) AND table_name = %s ORDER BY id", [list(rewritten_ids), current_table])
        rows = cursor.fetchall()

    rewrite_rows(schema_editor, rows, current_table, restored_table)
    schema_editor.execute(f"DELETE FROM {records} WHERE id = %s", [record_id])
