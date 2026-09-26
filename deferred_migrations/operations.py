import copy
from collections.abc import Iterable

from django.contrib.postgres.operations import AddIndexConcurrently as DjangoAddIndexConcurrently
from django.contrib.postgres.operations import NotInTransactionMixin
from django.core.exceptions import FieldDoesNotExist
from django.db import models
from django.db import transaction
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.operations import AddConstraint
from django.db.migrations.operations import AddField
from django.db.migrations.operations import AlterField
from django.db.migrations.operations import DeleteModel
from django.db.migrations.operations import RemoveField
from django.db.migrations.operations import RenameModel
from django.db.migrations.operations.base import Operation
from django.db.migrations.state import ProjectState

from deferred_migrations.backfill import run_batched_update
from deferred_migrations.concurrent import add_constraint
from deferred_migrations.concurrent import attach_unique_index
from deferred_migrations.concurrent import build_index_concurrently
from deferred_migrations.concurrent import concurrent_index
from deferred_migrations.concurrent import constraint_validity
from deferred_migrations.concurrent import inline_unique_name
from deferred_migrations.concurrent import not_valid
from deferred_migrations.contenttypes import check_no_stale_content_type
from deferred_migrations.context import OperationKey
from deferred_migrations.context import operation_key
from deferred_migrations.eligibility import model_rename_problem
from deferred_migrations.models import DeferredOperation
from deferred_migrations.queue import check_identifier
from deferred_migrations.queue import delete_queued
from deferred_migrations.queue import queue_statement
from deferred_migrations.queue import record_model_rename
from deferred_migrations.queue import retarget_queued_rows
from deferred_migrations.queue import unrecord_model_rename
from deferred_migrations.schema import FK_SUFFIX
from deferred_migrations.schema import check_compatibility_view_allowed
from deferred_migrations.schema import column_exists
from deferred_migrations.schema import column_is_not_null
from deferred_migrations.schema import create_compatibility_view
from deferred_migrations.schema import drop_column_sql
from deferred_migrations.schema import drop_foreign_keys
from deferred_migrations.schema import drop_table_sql
from deferred_migrations.schema import drop_view_sql
from deferred_migrations.schema import relax_column
from deferred_migrations.schema import restore_column
from deferred_migrations.schema import restore_foreign_keys
from deferred_migrations.schema import table_exists
from deferred_migrations.triggers import FILL_PREFIX
from deferred_migrations.triggers import SYNC_PREFIX
from deferred_migrations.triggers import drop_trigger_sql
from deferred_migrations.triggers import install_trigger
from deferred_migrations.triggers import substitute
from deferred_migrations.triggers import sync_function_body
from deferred_migrations.triggers import trigger_name


def auto_created_throughs(fields: Iterable[models.Field]) -> list[type[models.Model]]:
    return [field.remote_field.through for field in fields if field.remote_field.through._meta.auto_created]


def defer_through_tables(schema_editor: BaseDatabaseSchemaEditor, key: OperationKey, throughs: list[type[models.Model]], first_sequence: int) -> int:
    # Reject every name before anything is altered, so a refusal cannot leave constraints dropped with no drop queued.
    for through in throughs:
        check_identifier("table", through._meta.db_table)

    for sequence, through in enumerate(throughs, start=first_sequence):
        drop_foreign_keys(schema_editor, through, through._meta.local_fields)
        queue_statement(schema_editor, key, sequence, DeferredOperation.Kind.DROP_TABLE, through._meta.db_table, "", drop_table_sql(schema_editor, through._meta.db_table))

    return first_sequence + len(throughs)


# A partial post-deploy run can have dropped a through table while the rest of its operation is still queued, so each table is restored or recreated on its own.
def restore_through_tables(schema_editor: BaseDatabaseSchemaEditor, throughs: list[type[models.Model]]) -> None:
    for through in throughs:
        if table_exists(schema_editor, through._meta.db_table):
            restore_foreign_keys(schema_editor, through, through._meta.local_fields)
        else:
            schema_editor.create_model(through)


class DeferredRemoveField(RemoveField):
    def __init__(self, model_name: str, name: str, renamed_to: str | None = None) -> None:
        super().__init__(model_name, name)
        self.renamed_to = renamed_to

    def deconstruct(self) -> tuple[str, list[object], dict[str, object]]:
        name, args, kwargs = super().deconstruct()

        if self.renamed_to is not None:
            kwargs["renamed_to"] = self.renamed_to

        return name, args, kwargs

    def database_forwards(self, app_label: str, schema_editor: BaseDatabaseSchemaEditor, from_state: ProjectState, to_state: ProjectState) -> None:
        model = from_state.apps.get_model(app_label, self.model_name)

        if not self.allow_migrate_model(schema_editor.connection.alias, model):
            return

        field = model._meta.get_field(self.name)
        key = operation_key(self)

        if self.renamed_to is not None:
            try:
                model._meta.get_field(self.renamed_to)
            except FieldDoesNotExist as error:
                raise ValueError(f"{self.describe()}: renamed_to={self.renamed_to!r} is not a field on {app_label}.{self.model_name}.") from error

        if field.many_to_many:
            defer_through_tables(schema_editor, key, auto_created_throughs([field]), 0)
            return

        check_identifier("table", model._meta.db_table)
        check_identifier("column", field.column)
        drop_foreign_keys(schema_editor, model, [field])
        # A sync trigger fills the old column from renamed_to; a default would be copied over new code's explicit NULL instead.
        relax_column(schema_editor, model, field, set_default=self.renamed_to is None)
        queue_statement(schema_editor, key, 0, DeferredOperation.Kind.DROP_COLUMN, model._meta.db_table, field.column, drop_column_sql(schema_editor, model._meta.db_table, field.column))

    def database_backwards(self, app_label: str, schema_editor: BaseDatabaseSchemaEditor, from_state: ProjectState, to_state: ProjectState) -> None:
        model = to_state.apps.get_model(app_label, self.model_name)

        if not self.allow_migrate_model(schema_editor.connection.alias, model):
            return

        field = model._meta.get_field(self.name)
        key = operation_key(self)

        if field.many_to_many:
            if not schema_editor.collect_sql and (throughs := auto_created_throughs([field])):
                restore_through_tables(schema_editor, throughs)
            else:
                super().database_backwards(app_label, schema_editor, from_state, to_state)
        elif not schema_editor.collect_sql and column_exists(schema_editor, model._meta.db_table, field.column):
            # A post-deploy run can drop the trigger and then fail on the column drop, leaving writes since then only under the new name.
            if self.renamed_to is not None:
                self.copy_from_renamed_column(schema_editor, model, field)

            restore_column(schema_editor, model, field)
        elif self.renamed_to is not None and not schema_editor.collect_sql:
            self.restore_from_renamed_column(schema_editor, model, field)
        else:
            super().database_backwards(app_label, schema_editor, from_state, to_state)

        delete_queued(schema_editor, key)

    # An identity copy that only writes rows that differ, so it is a no-op while the trigger still keeps both columns equal.
    def copy_from_renamed_column(self, schema_editor: BaseDatabaseSchemaEditor, model: type[models.Model], field: models.Field) -> None:
        qn = schema_editor.quote_name
        column = qn(field.column)
        source = qn(model._meta.get_field(self.renamed_to).column)
        run_batched_update(schema_editor.connection, model._meta.db_table, single_column_pk(model).column, f"{column} = {source}", f"{column} IS DISTINCT FROM {source}", f"restore {model._meta.db_table}.{field.column}")

    # The post-deploy drop has run and the trigger is gone, so the values written since the rename only exist under the new name.
    def restore_from_renamed_column(self, schema_editor: BaseDatabaseSchemaEditor, model: type[models.Model], field: models.Field) -> None:
        nullable = copy.copy(field)
        nullable.null = True
        schema_editor.add_field(model, nullable)
        self.copy_from_renamed_column(schema_editor, model, field)
        restore_column(schema_editor, model, field)

    def describe(self) -> str:
        return f"Remove field {self.name} from {self.model_name} (column dropped after deploy)"


class DeferredDeleteModel(DeleteModel):
    def database_forwards(self, app_label: str, schema_editor: BaseDatabaseSchemaEditor, from_state: ProjectState, to_state: ProjectState) -> None:
        model = from_state.apps.get_model(app_label, self.name)

        if not self.allow_migrate_model(schema_editor.connection.alias, model):
            return

        key = operation_key(self)
        # Every name is checked before anything is altered: this table here, the through tables inside defer_through_tables.
        check_identifier("table", model._meta.db_table)
        sequence = defer_through_tables(schema_editor, key, auto_created_throughs(model._meta.local_many_to_many), 0)
        drop_foreign_keys(schema_editor, model, model._meta.local_fields)
        queue_statement(schema_editor, key, sequence, DeferredOperation.Kind.DROP_TABLE, model._meta.db_table, "", drop_table_sql(schema_editor, model._meta.db_table))

    def database_backwards(self, app_label: str, schema_editor: BaseDatabaseSchemaEditor, from_state: ProjectState, to_state: ProjectState) -> None:
        model = to_state.apps.get_model(app_label, self.name)

        if not self.allow_migrate_model(schema_editor.connection.alias, model):
            return

        key = operation_key(self)

        if not schema_editor.collect_sql and table_exists(schema_editor, model._meta.db_table):
            restore_foreign_keys(schema_editor, model, model._meta.local_fields)
            restore_through_tables(schema_editor, auto_created_throughs(model._meta.local_many_to_many))
        else:
            super().database_backwards(app_label, schema_editor, from_state, to_state)

        delete_queued(schema_editor, key)

    def describe(self) -> str:
        return f"Delete model {self.name} (table dropped after deploy)"


def single_column_pk(model: type[models.Model]) -> models.Field:
    pk = model._meta.pk

    if isinstance(pk, models.CompositePrimaryKey):
        raise ValueError(f"{model._meta.label} needs a single-column primary key for trigger-based operations.")  # noqa: TRY004

    return pk


class DeferredRenameModel(RenameModel):
    # Never calls super(): Django's RenameModel would rename the table with no view and re-create the foreign keys pointing at it.
    def database_forwards(self, app_label: str, schema_editor: BaseDatabaseSchemaEditor, from_state: ProjectState, to_state: ProjectState) -> None:
        new_model = to_state.apps.get_model(app_label, self.new_name)

        if not self.allow_migrate_model(schema_editor.connection.alias, new_model):
            return

        if (problem := model_rename_problem(from_state, app_label, self.old_name)) is not None:
            raise ValueError(f"DeferredRenameModel({self.old_name!r}, {self.new_name!r}) cannot run: {problem}")

        old_table = from_state.apps.get_model(app_label, self.old_name)._meta.db_table
        new_table = new_model._meta.db_table
        check_identifier("table", old_table)
        check_identifier("table", new_table)
        check_compatibility_view_allowed(schema_editor, old_table)
        check_no_stale_content_type(schema_editor, app_label, self.old_name_lower, self.new_name_lower)
        key = operation_key(self)
        qn = schema_editor.quote_name
        schema_editor.execute(f"ALTER TABLE {qn(old_table)} RENAME TO {qn(new_table)}", params=None)
        create_compatibility_view(schema_editor, old_table, new_table)
        rewritten = retarget_queued_rows(schema_editor, old_table, new_table)
        record_model_rename(schema_editor, app_label, self.old_name_lower, self.new_name_lower, rewritten)
        queue_statement(schema_editor, key, 0, DeferredOperation.Kind.DROP_VIEW, new_table, old_table, drop_view_sql(schema_editor, old_table))

    def database_backwards(self, app_label: str, schema_editor: BaseDatabaseSchemaEditor, from_state: ProjectState, to_state: ProjectState) -> None:
        old_model = to_state.apps.get_model(app_label, self.old_name)

        if not self.allow_migrate_model(schema_editor.connection.alias, old_model):
            return

        old_table = old_model._meta.db_table
        new_table = from_state.apps.get_model(app_label, self.new_name)._meta.db_table
        qn = schema_editor.quote_name
        schema_editor.execute(drop_view_sql(schema_editor, old_table), params=None)
        schema_editor.execute(f"ALTER TABLE {qn(new_table)} RENAME TO {qn(old_table)}", params=None)
        unrecord_model_rename(schema_editor, app_label, self.old_name_lower, self.new_name_lower, new_table, old_table)
        delete_queued(schema_editor, operation_key(self))

    def describe(self) -> str:
        return f"Rename model {self.old_name} to {self.new_name} (old name served by a view until after deploy)"


class InstallColumnSync(Operation):
    reversible = True

    def __init__(self, model_name: str, from_field: str, to_field: str, forwards_sql: str, backwards_sql: str | None) -> None:
        self.model_name = model_name
        self.from_field = from_field
        self.to_field = to_field
        self.forwards_sql = forwards_sql
        self.backwards_sql = backwards_sql

    def deconstruct(self) -> tuple[str, list[object], dict[str, object]]:
        return (self.__class__.__name__, [], {"model_name": self.model_name, "from_field": self.from_field, "to_field": self.to_field, "forwards_sql": self.forwards_sql, "backwards_sql": self.backwards_sql})

    def state_forwards(self, app_label: str, state: ProjectState) -> None:
        pass

    def trigger(self, model: type[models.Model]) -> tuple[str, str, str, str]:
        table = model._meta.db_table
        from_column = model._meta.get_field(self.from_field).column
        to_column = model._meta.get_field(self.to_field).column
        return table, from_column, to_column, trigger_name(SYNC_PREFIX, table, from_column, to_column)

    def database_forwards(self, app_label: str, schema_editor: BaseDatabaseSchemaEditor, from_state: ProjectState, to_state: ProjectState) -> None:
        model = to_state.apps.get_model(app_label, self.model_name)

        if not self.allow_migrate_model(schema_editor.connection.alias, model):
            return

        single_column_pk(model)
        table, from_column, to_column, name = self.trigger(model)
        body = sync_function_body(schema_editor.quote_name(from_column), schema_editor.quote_name(to_column), self.forwards_sql, self.backwards_sql)
        install_trigger(schema_editor, table, name, body)
        queue_statement(schema_editor, operation_key(self), 0, DeferredOperation.Kind.DROP_TRIGGER, table, to_column, drop_trigger_sql(schema_editor, table, name))

    def database_backwards(self, app_label: str, schema_editor: BaseDatabaseSchemaEditor, from_state: ProjectState, to_state: ProjectState) -> None:
        model = from_state.apps.get_model(app_label, self.model_name)

        if not self.allow_migrate_model(schema_editor.connection.alias, model):
            return

        table, _from_column, _to_column, name = self.trigger(model)
        schema_editor.execute(drop_trigger_sql(schema_editor, table, name), params=None)
        delete_queued(schema_editor, operation_key(self))

    def describe(self) -> str:
        return f"Sync {self.model_name}.{self.from_field} into {self.to_field} with a trigger until after deploy"


class BackfillColumnSync(Operation):
    reversible = True

    def __init__(self, model_name: str, from_field: str, to_field: str, forwards_sql: str) -> None:
        self.model_name = model_name
        self.from_field = from_field
        self.to_field = to_field
        self.forwards_sql = forwards_sql

    def deconstruct(self) -> tuple[str, list[object], dict[str, object]]:
        return (self.__class__.__name__, [], {"model_name": self.model_name, "from_field": self.from_field, "to_field": self.to_field, "forwards_sql": self.forwards_sql})

    def state_forwards(self, app_label: str, state: ProjectState) -> None:
        pass

    def database_forwards(self, app_label: str, schema_editor: BaseDatabaseSchemaEditor, from_state: ProjectState, to_state: ProjectState) -> None:
        model = to_state.apps.get_model(app_label, self.model_name)

        if not self.allow_migrate_model(schema_editor.connection.alias, model):
            return

        if schema_editor.collect_sql:
            schema_editor.execute(f"-- BackfillColumnSync runs batched UPDATEs on {model._meta.db_table} at migrate time", params=None)
            return

        pk = single_column_pk(model)
        qn = schema_editor.quote_name
        from_ref = qn(model._meta.get_field(self.from_field).column)
        to_ref = qn(model._meta.get_field(self.to_field).column)
        expression = f"({substitute(self.forwards_sql, {'from': from_ref, 'to': to_ref})})"
        to_column = model._meta.get_field(self.to_field).column
        run_batched_update(schema_editor.connection, model._meta.db_table, pk.column, f"{to_ref} = {expression}", f"{to_ref} IS DISTINCT FROM {expression}", f"backfill {model._meta.db_table}.{to_column}")

    def database_backwards(self, app_label: str, schema_editor: BaseDatabaseSchemaEditor, from_state: ProjectState, to_state: ProjectState) -> None:
        pass

    def describe(self) -> str:
        return f"Backfill {self.model_name}.{self.to_field} from {self.from_field} in batches"


def row_placeholders(schema_editor: BaseDatabaseSchemaEditor, model: type[models.Model], prefix: str) -> dict[str, str]:
    return {field.name: f"{prefix}{schema_editor.quote_name(field.column)}" for field in model._meta.concrete_fields}


class InstallNotNullFill(Operation):
    reversible = True

    def __init__(self, model_name: str, name: str, fill_sql: str) -> None:
        self.model_name = model_name
        self.name = name
        self.fill_sql = fill_sql

    def deconstruct(self) -> tuple[str, list[object], dict[str, object]]:
        return (self.__class__.__name__, [], {"model_name": self.model_name, "name": self.name, "fill_sql": self.fill_sql})

    def state_forwards(self, app_label: str, state: ProjectState) -> None:
        pass

    def trigger(self, model: type[models.Model]) -> tuple[str, str, str]:
        table = model._meta.db_table
        column = model._meta.get_field(self.name).column
        return table, column, trigger_name(FILL_PREFIX, table, column)

    def database_forwards(self, app_label: str, schema_editor: BaseDatabaseSchemaEditor, from_state: ProjectState, to_state: ProjectState) -> None:
        model = to_state.apps.get_model(app_label, self.model_name)

        if not self.allow_migrate_model(schema_editor.connection.alias, model):
            return

        single_column_pk(model)
        table, column, name = self.trigger(model)
        target = f"NEW.{schema_editor.quote_name(column)}"
        fill = substitute(self.fill_sql, row_placeholders(schema_editor, model, "NEW."))
        body = f"\nBEGIN\n  IF {target} IS NULL THEN\n    {target} := ({fill});\n  END IF;\n  RETURN NEW;\nEND;\n"
        install_trigger(schema_editor, table, name, body)
        queue_statement(schema_editor, operation_key(self), 0, DeferredOperation.Kind.DROP_TRIGGER, table, column, drop_trigger_sql(schema_editor, table, name))

    def database_backwards(self, app_label: str, schema_editor: BaseDatabaseSchemaEditor, from_state: ProjectState, to_state: ProjectState) -> None:
        model = from_state.apps.get_model(app_label, self.model_name)

        if not self.allow_migrate_model(schema_editor.connection.alias, model):
            return

        table, _column, name = self.trigger(model)
        schema_editor.execute(drop_trigger_sql(schema_editor, table, name), params=None)
        delete_queued(schema_editor, operation_key(self))

    def describe(self) -> str:
        return f"Fill NULLs written to {self.model_name}.{self.name} with a trigger until after deploy"


class BackfillNotNull(Operation):
    reversible = True

    def __init__(self, model_name: str, name: str, fill_sql: str) -> None:
        self.model_name = model_name
        self.name = name
        self.fill_sql = fill_sql

    def deconstruct(self) -> tuple[str, list[object], dict[str, object]]:
        return (self.__class__.__name__, [], {"model_name": self.model_name, "name": self.name, "fill_sql": self.fill_sql})

    def state_forwards(self, app_label: str, state: ProjectState) -> None:
        pass

    def database_forwards(self, app_label: str, schema_editor: BaseDatabaseSchemaEditor, from_state: ProjectState, to_state: ProjectState) -> None:
        model = to_state.apps.get_model(app_label, self.model_name)

        if not self.allow_migrate_model(schema_editor.connection.alias, model):
            return

        if schema_editor.collect_sql:
            schema_editor.execute(f"-- BackfillNotNull runs batched UPDATEs on {model._meta.db_table} at migrate time", params=None)
            return

        pk = single_column_pk(model)
        column_name = model._meta.get_field(self.name).column
        column = schema_editor.quote_name(column_name)
        fill = substitute(self.fill_sql, row_placeholders(schema_editor, model, ""))
        run_batched_update(schema_editor.connection, model._meta.db_table, pk.column, f"{column} = ({fill})", f"{column} IS NULL", f"backfill {model._meta.db_table}.{column_name}")

    def database_backwards(self, app_label: str, schema_editor: BaseDatabaseSchemaEditor, from_state: ProjectState, to_state: ProjectState) -> None:
        pass

    def describe(self) -> str:
        return f"Backfill NULLs in {self.model_name}.{self.name} in batches"


class SetNotNull(AlterField):
    def state_forwards(self, app_label: str, state: ProjectState) -> None:
        current = state.models[app_label, self.model_name_lower].fields[self.name]

        if not current.null or self.field.null:
            raise ValueError(f"SetNotNull on {self.model_name}.{self.name} must change a nullable field to null=False.")

        _name, old_path, old_args, old_kwargs = current.deconstruct()
        _name, new_path, new_args, new_kwargs = self.field.deconstruct()
        old_kwargs.pop("null", None)
        new_kwargs.pop("null", None)

        if (old_path, old_args, old_kwargs) != (new_path, new_args, new_kwargs):
            raise ValueError(f"SetNotNull on {self.model_name}.{self.name} can only change nullability; use a separate AlterField for other changes.")

        super().state_forwards(app_label, state)

    def database_forwards(self, app_label: str, schema_editor: BaseDatabaseSchemaEditor, from_state: ProjectState, to_state: ProjectState) -> None:
        model = to_state.apps.get_model(app_label, self.model_name)

        if not self.allow_migrate_model(schema_editor.connection.alias, model):
            return

        table_name = model._meta.db_table
        column_name = model._meta.get_field(self.name).column
        qn = schema_editor.quote_name
        constraint = trigger_name("dm_nn", table_name, column_name)
        table = qn(table_name)
        column = qn(column_name)
        constraint_exists = False

        if not schema_editor.collect_sql:
            already_not_null = column_is_not_null(schema_editor, table_name, column_name)

            if already_not_null is None:
                raise ValueError(f"SetNotNull on {self.model_name}.{self.name} found no column {table_name}.{column_name}.")

            constraint_exists = constraint_validity(schema_editor, table_name, constraint) is not None

            if already_not_null:
                schema_editor.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {qn(constraint)}", params=None)
                return

        if not constraint_exists:
            schema_editor.execute(f"ALTER TABLE {table} ADD CONSTRAINT {qn(constraint)} CHECK ({column} IS NOT NULL) NOT VALID", params=None)

        schema_editor.execute(f"ALTER TABLE {table} VALIDATE CONSTRAINT {qn(constraint)}", params=None)
        schema_editor.execute(f"ALTER TABLE {table} ALTER COLUMN {column} SET NOT NULL", params=None)
        schema_editor.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {qn(constraint)}", params=None)

    def database_backwards(self, app_label: str, schema_editor: BaseDatabaseSchemaEditor, from_state: ProjectState, to_state: ProjectState) -> None:
        model = from_state.apps.get_model(app_label, self.model_name)

        if not self.allow_migrate_model(schema_editor.connection.alias, model):
            return

        schema_editor.execute(f"ALTER TABLE {schema_editor.quote_name(model._meta.db_table)} ALTER COLUMN {schema_editor.quote_name(model._meta.get_field(self.name).column)} DROP NOT NULL", params=None)

    def describe(self) -> str:
        return f"Set {self.model_name}.{self.name} NOT NULL without a long lock"


class AddIndexConcurrently(DjangoAddIndexConcurrently):
    def database_forwards(self, app_label: str, schema_editor: BaseDatabaseSchemaEditor, from_state: ProjectState, to_state: ProjectState) -> None:
        self._ensure_not_in_transaction(schema_editor)
        model = to_state.apps.get_model(app_label, self.model_name)

        if self.allow_migrate_model(schema_editor.connection.alias, model):
            build_index_concurrently(schema_editor, model._meta.db_table, concurrent_index(self.index.create_sql(model, schema_editor)))


class AddConstraintConcurrently(NotInTransactionMixin, AddConstraint):
    def __init__(self, model_name: str, constraint: models.BaseConstraint) -> None:
        if not isinstance(constraint, (models.UniqueConstraint, models.CheckConstraint)):
            raise ValueError(f"AddConstraintConcurrently builds a UniqueConstraint or CheckConstraint, not {constraint.__class__.__name__}. Use AddConstraint for {constraint.name}.")  # noqa: TRY004

        super().__init__(model_name, constraint)

    def database_forwards(self, app_label: str, schema_editor: BaseDatabaseSchemaEditor, from_state: ProjectState, to_state: ProjectState) -> None:
        self._ensure_not_in_transaction(schema_editor)
        model = to_state.apps.get_model(app_label, self.model_name)

        if not self.allow_migrate_model(schema_editor.connection.alias, model):
            return

        # Django returns None for a constraint the database cannot enforce, such as nulls_distinct before PostgreSQL 15, and creates nothing.
        if (statement := self.constraint.create_sql(model, schema_editor)) is None:
            return

        table = model._meta.db_table

        if isinstance(self.constraint, models.CheckConstraint):
            add_constraint(schema_editor, table, not_valid(statement), validate=True)
            return

        if statement.template == schema_editor.sql_create_unique_index:
            build_index_concurrently(schema_editor, table, concurrent_index(statement), clean_up_on_failure=True)
            return

        if statement.template != schema_editor.sql_create_unique:
            raise ValueError(f"AddConstraintConcurrently got an unexpected statement for {self.constraint.name}: {statement.template!r}.")

        attach_unique_index(schema_editor, table, statement)

    def describe(self) -> str:
        return f"Create constraint {self.constraint.name} on model {self.model_name} without blocking writes"


class AddFieldConcurrently(NotInTransactionMixin, AddField):
    def __init__(self, model_name: str, name: str, field: models.Field, preserve_default: bool = True) -> None:
        if field.many_to_many or field.primary_key:
            raise ValueError(f"AddFieldConcurrently cannot add {model_name}.{name}: many-to-many and primary key fields have no index or constraint to build concurrently. Use AddField.")

        super().__init__(model_name, name, field, preserve_default)

    def database_forwards(self, app_label: str, schema_editor: BaseDatabaseSchemaEditor, from_state: ProjectState, to_state: ProjectState) -> None:
        self._ensure_not_in_transaction(schema_editor)
        model = to_state.apps.get_model(app_label, self.model_name)

        if not self.allow_migrate_model(schema_editor.connection.alias, model):
            return

        field = model._meta.get_field(self.name)
        table = model._meta.db_table
        self.add_column(schema_editor, model, field)

        for statement in schema_editor._field_indexes_sql(model, field):
            build_index_concurrently(schema_editor, table, concurrent_index(statement))

        # Named as PostgreSQL names the inline UNIQUE of a plain AddField, not as Django names one AlterField adds, so the schema matches what makemigrations' AddField would have built.
        if field.unique:
            attach_unique_index(schema_editor, table, schema_editor._create_unique_sql(model, [field], name=inline_unique_name(schema_editor, table, field.column)))

        if field.remote_field is not None and field.db_constraint:
            add_constraint(schema_editor, table, not_valid(schema_editor._create_fk_sql(model, field, FK_SUFFIX)), validate=True)

    def add_column(self, schema_editor: BaseDatabaseSchemaEditor, model: type[models.Model], field: models.Field) -> None:
        # clone() cannot drop uniqueness: OneToOneField forces unique=True and its deconstruct() omits it.
        column = copy.copy(field)
        column._unique = False
        # Field.unique is a cached_property, so a value cached on the original would survive the copy.
        column.__dict__.pop("unique", None)
        column.db_index = False

        if column.remote_field is not None:
            column.db_constraint = False

        if not self.preserve_default:
            column.default = self.field.default

        if schema_editor.collect_sql:
            schema_editor.add_field(model, column)
            return

        table = model._meta.db_table

        # A column whose drop has not run was not created by this operation; letting add_field raise DuplicateColumn gives migrate_pre_deploy's explanation.
        if column_exists(schema_editor, table, field.column) and not self.drop_has_not_run(schema_editor, table, field.column):
            return

        # ADD COLUMN, the default drop and the comment commit together, so an interruption cannot leave a stray database default.
        with transaction.atomic(using=schema_editor.connection.alias):
            schema_editor.add_field(model, column)

    def drop_has_not_run(self, schema_editor: BaseDatabaseSchemaEditor, table: str, column: str) -> bool:
        return DeferredOperation.objects.using(schema_editor.connection.alias).filter(kind=DeferredOperation.Kind.DROP_COLUMN, table_name=table, column_name=column, status__in=[DeferredOperation.Status.PENDING, DeferredOperation.Status.FAILED, DeferredOperation.Status.SKIPPED]).exists()

    def describe(self) -> str:
        return f"Add field {self.name} to {self.model_name} without blocking writes"
