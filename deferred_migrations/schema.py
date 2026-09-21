from collections.abc import Iterable

from django.db import models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.models import GeneratedField
from django.db.models import Model
from django.db.models.fields import NOT_PROVIDED
from django.db.models.fields import Field

from deferred_migrations.expressions import expression_field_names

# The suffix Django's add_field and _alter_field pass to _create_fk_sql, so a foreign key built here gets Django's own name.
FK_SUFFIX = "_fk_%(to_table)s_%(to_column)s"


def drop_table_sql(schema_editor: BaseDatabaseSchemaEditor, table: str) -> str:
    return f"DROP TABLE IF EXISTS {schema_editor.quote_name(table)}"


def drop_column_sql(schema_editor: BaseDatabaseSchemaEditor, table: str, column: str) -> str:
    return f"ALTER TABLE IF EXISTS {schema_editor.quote_name(table)} DROP COLUMN IF EXISTS {schema_editor.quote_name(column)}"


def table_exists(schema_editor: BaseDatabaseSchemaEditor, table: str) -> bool:
    connection = schema_editor.connection

    with connection.cursor() as cursor:
        return table in connection.introspection.table_names(cursor)


def column_exists(schema_editor: BaseDatabaseSchemaEditor, table: str, column: str) -> bool:
    if not table_exists(schema_editor, table):
        return False

    connection = schema_editor.connection

    with connection.cursor() as cursor:
        return column in {info.name for info in connection.introspection.get_table_description(cursor, table)}


def column_is_not_null(schema_editor: BaseDatabaseSchemaEditor, table: str, column: str) -> bool | None:
    if not table_exists(schema_editor, table):
        return None

    connection = schema_editor.connection

    with connection.cursor() as cursor:
        for info in connection.introspection.get_table_description(cursor, table):
            if info.name == column:
                return not info.null_ok

    return None


def is_unique_field(model: type[Model], field: Field) -> bool:
    if field.unique:
        return True

    for constraint in model._meta.constraints:
        if not isinstance(constraint, models.UniqueConstraint):
            continue

        # A functional constraint such as UniqueConstraint(Lower("name"), "brewery") puts its positional arguments in expressions, leaving fields empty.
        if field.name in constraint.fields or any(field.name in expression_field_names(expression) for expression in constraint.expressions):
            return True

    return any(field.name in together for together in model._meta.unique_together)


def constrained_foreign_keys(fields: Iterable[Field]) -> list[models.ForeignKey]:
    return [field for field in fields if isinstance(field, models.ForeignKey) and field.db_constraint]


def drop_foreign_keys(schema_editor: BaseDatabaseSchemaEditor, model: type[Model], fields: Iterable[Field]) -> None:
    for field in constrained_foreign_keys(fields):
        if schema_editor.collect_sql:
            schema_editor.execute(f"-- Drop foreign key constraints on {model._meta.db_table}.{field.column} (names are looked up at migrate time)", params=None)
            continue

        for name in schema_editor._constraint_names(model, [field.column], foreign_key=True):
            schema_editor.execute(schema_editor._delete_fk_sql(model, name))


def restore_foreign_keys(schema_editor: BaseDatabaseSchemaEditor, model: type[Model], fields: Iterable[Field]) -> None:
    for field in constrained_foreign_keys(fields):
        if schema_editor._constraint_names(model, [field.column], foreign_key=True):
            continue

        schema_editor.execute(schema_editor._create_fk_sql(model, field, FK_SUFFIX))


def database_default(schema_editor: BaseDatabaseSchemaEditor, model: type[Model], field: Field) -> object | None:
    # One value shared by every row new code inserts would collide on the second insert.
    if is_unique_field(model, field):
        return None

    # A callable default would bake one shared value into the column, so leave those to DROP NOT NULL.
    if field.has_default() and callable(field.default):
        return None

    if (default := schema_editor.effective_default(field)) is not None:
        return default

    # Django only offers "" to fields declared blank, but old code can read "" from any string column, where NULL would break it.
    if not field.null and field.empty_strings_allowed:
        return field.get_db_prep_save("", schema_editor.connection)

    return None


def relax_column(schema_editor: BaseDatabaseSchemaEditor, model: type[Model], field: Field, set_default: bool = True) -> None:
    # PostgreSQL computes a generated column, and a db_default already fills inserts that omit the column.
    if isinstance(field, GeneratedField) or field.db_default is not NOT_PROVIDED:
        return

    table = schema_editor.quote_name(model._meta.db_table)
    column = schema_editor.quote_name(field.column)

    if set_default and (default := database_default(schema_editor, model, field)) is not None:
        schema_editor.execute(f"ALTER TABLE {table} ALTER COLUMN {column} SET DEFAULT %s", [default])
        return

    if not field.null:
        schema_editor.execute(f"ALTER TABLE {table} ALTER COLUMN {column} DROP NOT NULL", params=None)


def restore_column(schema_editor: BaseDatabaseSchemaEditor, model: type[Model], field: Field) -> None:
    if isinstance(field, GeneratedField):
        return

    table = schema_editor.quote_name(model._meta.db_table)
    column = schema_editor.quote_name(field.column)
    # Converge on what the field declares rather than re-deriving what relax_column did, which can differ if the package changed between apply and rollback. Django keeps no column default other than db_default.
    changes = [] if field.db_default is not NOT_PROVIDED else [f"ALTER COLUMN {column} DROP DEFAULT"]

    if not field.null:
        changes.append(f"ALTER COLUMN {column} SET NOT NULL")

    if changes:
        schema_editor.execute(f"ALTER TABLE {table} {', '.join(changes)}", params=None)

    restore_foreign_keys(schema_editor, model, [field])


def drop_view_sql(schema_editor: BaseDatabaseSchemaEditor, view: str) -> str:
    return f"DROP VIEW IF EXISTS {schema_editor.quote_name(view)}"


def supports_security_invoker(schema_editor: BaseDatabaseSchemaEditor) -> bool:
    return schema_editor.connection.pg_version >= 150000


# Without security_invoker a view runs as its owner, which would bypass row-level security on the table.
def check_compatibility_view_allowed(schema_editor: BaseDatabaseSchemaEditor, table: str) -> None:
    if schema_editor.collect_sql or supports_security_invoker(schema_editor):
        return

    with schema_editor.connection.cursor() as cursor:
        cursor.execute("SELECT relrowsecurity FROM pg_class WHERE oid = to_regclass(%s)", [schema_editor.quote_name(table)])
        row = cursor.fetchone()

    if row is not None and row[0]:
        raise ValueError(f"{table} has row-level security, which a compatibility view would bypass before PostgreSQL 15. Upgrade PostgreSQL, or keep the old name with Meta.db_table.")


def create_compatibility_view(schema_editor: BaseDatabaseSchemaEditor, view: str, table: str) -> None:
    qn = schema_editor.quote_name
    options = " WITH (security_invoker = true)" if supports_security_invoker(schema_editor) else ""
    schema_editor.execute(f"CREATE VIEW {qn(view)}{options} AS SELECT * FROM {qn(table)}", params=None)

    if schema_editor.collect_sql:
        schema_editor.execute(f"-- Grants on {table} are copied onto the view {view} at migrate time", params=None)
        return

    with schema_editor.connection.cursor() as cursor:
        # The schema is the one the unqualified name above resolves to on the search_path, which need not be current_schema().
        cursor.execute(
            "SELECT grantee, privilege_type FROM information_schema.role_table_grants"
            " WHERE table_schema = (SELECT n.nspname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE c.relname = %s AND pg_catalog.pg_table_is_visible(c.oid))"
            " AND table_name = %s AND privilege_type IN ('SELECT', 'INSERT', 'UPDATE', 'DELETE') AND grantee <> current_user",
            [table, table],
        )
        grants = cursor.fetchall()

    for grantee, privilege in grants:
        # quote_name does not escape an embedded double quote, and a role name can contain one.
        target = "PUBLIC" if grantee == "PUBLIC" else '"' + grantee.replace('"', '""') + '"'
        schema_editor.execute(f"GRANT {privilege} ON {qn(view)} TO {target}", params=None)
