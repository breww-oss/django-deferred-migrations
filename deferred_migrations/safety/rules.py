import re
from collections.abc import Callable
from dataclasses import replace

from django.contrib.postgres.operations import AddConstraintNotValid
from django.contrib.postgres.operations import AddIndexConcurrently as DjangoAddIndexConcurrently
from django.contrib.postgres.operations import RemoveIndexConcurrently
from django.contrib.postgres.operations import ValidateConstraint
from django.db import models
from django.db.migrations.migration import Migration
from django.db.migrations.operations import AddConstraint
from django.db.migrations.operations import AddField
from django.db.migrations.operations import AddIndex
from django.db.migrations.operations import AlterField
from django.db.migrations.operations import AlterIndexTogether
from django.db.migrations.operations import AlterModelManagers
from django.db.migrations.operations import AlterModelOptions
from django.db.migrations.operations import AlterUniqueTogether
from django.db.migrations.operations import CreateModel
from django.db.migrations.operations import DeleteModel
from django.db.migrations.operations import RemoveField
from django.db.migrations.operations import RenameField
from django.db.migrations.operations import RenameModel
from django.db.migrations.operations import RunPython
from django.db.migrations.operations import RunSQL
from django.db.migrations.operations import SeparateDatabaseAndState
from django.db.migrations.operations.base import Operation
from django.db.models import GeneratedField
from django.db.models import Value
from django.db.models.fields import NOT_PROVIDED
from django.db.models.fields import Field
from django.db.models.functions import Now

from deferred_migrations.eligibility import model_rename_problem
from deferred_migrations.expressions import expression_field_names
from deferred_migrations.operations import AddConstraintConcurrently
from deferred_migrations.operations import AddFieldConcurrently
from deferred_migrations.operations import AddIndexConcurrently
from deferred_migrations.operations import BackfillColumnSync
from deferred_migrations.operations import BackfillNotNull
from deferred_migrations.operations import DeferredDeleteModel
from deferred_migrations.operations import DeferredRemoveField
from deferred_migrations.operations import DeferredRenameModel
from deferred_migrations.operations import InstallColumnSync
from deferred_migrations.operations import InstallNotNullFill
from deferred_migrations.operations import SetNotNull
from deferred_migrations.safety.findings import RULE_IDS
from deferred_migrations.safety.findings import Finding
from deferred_migrations.safety.names import PhysicalNameKey
from deferred_migrations.safety.names import column_name
from deferred_migrations.safety.names import has_own_table
from deferred_migrations.safety.walker import MigrationContext
from deferred_migrations.safety.walker import OperationContext

QUEUE_DEPENDENCY = ("deferred_migrations", "0001_initial")
RENAME_RECORD_DEPENDENCY = ("deferred_migrations", "0002_modelrename")
# AddFieldConcurrently reads the queue to refuse a column whose drop is still pending.
QUEUE_OPERATIONS = (DeferredRemoveField, DeferredDeleteModel, DeferredRenameModel, InstallColumnSync, InstallNotNullFill, BackfillColumnSync, BackfillNotNull, AddFieldConcurrently)

CONCURRENT_OPERATIONS = (DjangoAddIndexConcurrently, RemoveIndexConcurrently, AddFieldConcurrently, AddConstraintConcurrently)
# Django's own AddIndexConcurrently is left out: it fails on a re-run once its index exists.
RERUN_SAFE_OPERATIONS = (AddIndexConcurrently, AddFieldConcurrently, AddConstraintConcurrently, RemoveIndexConcurrently, ValidateConstraint, BackfillColumnSync, BackfillNotNull, SetNotNull, DeferredRemoveField, DeferredDeleteModel, RunSQL, RunPython, AlterModelOptions, AlterModelManagers)


def is_safe_to_rerun(operation: Operation) -> bool:
    if isinstance(operation, SeparateDatabaseAndState):
        return all(is_safe_to_rerun(nested) for nested in operation.database_operations)

    return isinstance(operation, RERUN_SAFE_OPERATIONS)


def required_queue_dependency(migration: Migration) -> tuple[str, str] | None:
    if any(isinstance(operation, DeferredRenameModel) for operation in migration.operations):
        return RENAME_RECORD_DEPENDENCY

    if any(isinstance(operation, QUEUE_OPERATIONS) for operation in migration.operations):
        return QUEUE_DEPENDENCY

    return None


def queue_dependency_rule(context: MigrationContext) -> list[Finding]:
    migration = context.migration

    if migration.app_label == QUEUE_DEPENDENCY[0] or (needed := required_queue_dependency(migration)) is None:
        return []

    # The package's own migrations are disabled (e.g. MIGRATION_MODULES = None in tests), so there is nothing to depend on.
    if needed not in context.graph.nodes:
        return []

    if needed in context.graph.forwards_plan(context.key):
        return []

    return [Finding("E008", context.key[0], context.key[1], None, f"Uses deferred_migrations operations but does not depend on {needed}. Add it to dependencies.")]


def suppression_rule(context: MigrationContext) -> list[Finding]:
    allowed = context.migration.__class__.__dict__.get("deploy_safety_allowed", {})
    findings: list[Finding] = []

    for rule_id, reason in allowed.items():
        if rule_id not in RULE_IDS:
            findings.append(Finding("E009", context.key[0], context.key[1], None, f"deploy_safety_allowed names unknown rule {rule_id}."))
        elif not isinstance(reason, str) or not reason.strip():
            findings.append(Finding("E009", context.key[0], context.key[1], None, f"deploy_safety_allowed entry for {rule_id} needs a reason explaining why it is safe."))

    return findings


def model_key(context: OperationContext) -> tuple[str, str]:
    return (context.app_label, context.operation.model_name_lower)


def existed_before_migration(context: OperationContext, key: tuple[str, str]) -> bool:
    return key in context.migration.models_at_start


def before_field(context: OperationContext, name: str) -> Field | None:
    before = context.before_models.get(model_key(context))
    return None if before is None else before.fields.get(name)


def nested_advice(context: OperationContext) -> str:
    # The deferred operations key their queue rows by position in the migration, so they raise ImproperlyConfigured inside SeparateDatabaseAndState.
    if context.nested:
        return " It is inside SeparateDatabaseAndState, where the deferred operations cannot go, so take it out of the wrapper first."

    return ""


def readd_keeps_the_column_readable(context: OperationContext, removed: Field, added: Field) -> bool:
    # A generated column is computed by PostgreSQL and never written by old code, and re-typing one is the point of the RemoveField + AddField recipe.
    if isinstance(removed, GeneratedField) and isinstance(added, GeneratedField):
        return True

    # A state field's ForeignKey cannot resolve its target, so db_type() raises; a relational re-add is never exempt.
    if any(isinstance(field, models.ForeignKey) for field in (removed, added)):
        return False

    return removed.db_type(context.connection) == added.db_type(context.connection)


def remove_field_rule(context: OperationContext) -> list[Finding]:
    operation = context.operation

    if not isinstance(operation, RemoveField) or isinstance(operation, DeferredRemoveField):
        return []

    key = model_key(context)
    before = context.before_models.get(key)

    if before is None or not has_own_table(before):
        return []

    removed_field = before.fields[operation.name]
    removed_column = column_name(operation.name, removed_field)

    # Re-adding the same column in the same atomic migration means it never disappears, but only while the column keeps a type old code can read.
    if context.migration.migration.atomic and removed_column is not None:
        for later in context.following:
            if isinstance(later, AddField) and later.model_name_lower == operation.model_name_lower and column_name(later.name, later.field) == removed_column and readd_keeps_the_column_readable(context, removed_field, later.field):
                return []

    return [context.finding("E001", f"RemoveField({operation.model_name!r}, {operation.name!r}) drops the column while old code still reads it.{nested_advice(context)} Use DeferredRemoveField with the same arguments.")]


def delete_model_rule(context: OperationContext) -> list[Finding]:
    operation = context.operation

    if not isinstance(operation, DeleteModel) or isinstance(operation, DeferredDeleteModel):
        return []

    before = context.before_models.get((context.app_label, operation.name_lower))

    if before is None or not has_own_table(before):
        return []

    return [context.finding("E002", f"DeleteModel({operation.name!r}) drops the table while old code still uses it.{nested_advice(context)} Use DeferredDeleteModel.")]


def is_constant_default(field: Field) -> bool:
    return field.has_default() and not callable(field.default)


# The sync trigger fills the target for old code's inserts, and a constant default fills the rows that exist, whether or not Django keeps it in state.
def is_sync_target_with_constant_default(context: OperationContext, operation: AddField) -> bool:
    if not is_constant_default(operation.field):
        return False

    return any(isinstance(other, InstallColumnSync) and other.model_name.lower() == operation.model_name_lower and other.to_field == operation.name for other in context.migration.migration.operations)


def not_null_add_rule(context: OperationContext) -> list[Finding]:
    operation = context.operation

    if isinstance(operation, AddField):
        field = operation.field

        if not existed_before_migration(context, model_key(context)) or field.null or field.db_default is not NOT_PROVIDED:
            return []

        if isinstance(field, (GeneratedField, models.ManyToManyField)) or is_sync_target_with_constant_default(context, operation):
            return []

        return [context.finding("E003", f"AddField({operation.model_name!r}, {operation.name!r}) is NOT NULL with no db_default, so old code's inserts fail. Add a constant db_default, or null=True.")]

    if isinstance(operation, AlterField) and not isinstance(operation, SetNotNull):
        if (old := before_field(context, operation.name)) is None:
            return []

        if old.db_default is not NOT_PROVIDED and operation.field.db_default is NOT_PROVIDED and not operation.field.null:
            return [context.finding("E003", f"AlterField({operation.model_name!r}, {operation.name!r}) removes db_default from a NOT NULL field; old code relies on it for inserts. Keep the default or make the column nullable.")]

    return []


def not_null_alter_rule(context: OperationContext) -> list[Finding]:
    operation = context.operation

    if not isinstance(operation, AlterField) or isinstance(operation, SetNotNull):
        return []

    if (old := before_field(context, operation.name)) is None:
        return []

    if old.null and not operation.field.null:
        return [context.finding("E004", f"AlterField({operation.model_name!r}, {operation.name!r}) makes the column NOT NULL while old code may still write NULL. Use InstallNotNullFill + BackfillNotNull + SetNotNull.")]

    return []


def generated_dependency_rule(context: OperationContext) -> list[Finding]:
    operation = context.operation

    if not isinstance(operation, DeferredRemoveField):
        return []

    model_state = context.state_after.models.get(model_key(context))

    if model_state is None:
        return []

    dependants = [name for name, field in model_state.fields.items() if isinstance(field, GeneratedField) and operation.name in expression_field_names(field.expression)]

    if not dependants:
        return []

    return [context.finding("E011", f"{operation.model_name}.{operation.name} is used by generated field(s) {', '.join(dependants)}; the queued DROP COLUMN would fail and block the queue. Remove or change those fields first.")]


TYPE_PATTERN = re.compile(r"^(?P<base>[a-z ]+?)\s*(?:\((?P<args>[^)]*)\))?$")
STRING_TYPES = frozenset({"varchar", "text"})


def parse_type(db_type: str) -> tuple[str, list[int]]:
    match = TYPE_PATTERN.match(db_type.strip().lower())

    if match is None:
        return db_type, []

    # Only the length and precision arguments matter; types such as geometry(Point,4326) carry arguments that are not numbers.
    args = [int(part) for part in (match.group("args") or "").split(",") if part.strip().isdigit()]
    return match.group("base").strip(), args


def is_allowed_type_change(old: str | None, new: str | None, indexed: bool) -> bool:
    if old == new:
        return True

    if old is None or new is None:
        return False

    old_base, old_args = parse_type(old)
    new_base, new_args = parse_type(new)

    if old_base == "varchar" and new_base == "varchar":
        return not new_args or (bool(old_args) and new_args[0] >= old_args[0])

    if old_base in STRING_TYPES and new_base == "text":
        return not indexed

    if old_base == "numeric" and new_base == "numeric" and len(old_args) == 2 and len(new_args) == 2:
        return new_args[0] >= old_args[0] and new_args[1] == old_args[1]

    return False


def creates_index(field: Field) -> bool:
    if isinstance(field, models.ManyToManyField):
        return False

    return bool(field.db_index or field.unique) or (isinstance(field, models.ForeignKey) and field.db_constraint)


def rename_rule(context: OperationContext) -> list[Finding]:
    operation = context.operation

    # A DeferredRenameModel keeps the old name working through a view; ineligible ones are E012's job.
    if isinstance(operation, DeferredRenameModel):
        return []

    if context.names_before is None or context.names_after is None:
        return []

    def remap(key: PhysicalNameKey) -> PhysicalNameKey:
        if isinstance(operation, RenameModel) and (key.app_label, key.model_name) == (context.app_label, operation.old_name_lower):
            return replace(key, model_name=operation.new_name_lower)

        if isinstance(operation, RenameField) and (key.app_label, key.model_name, key.field_name) == (context.app_label, operation.model_name_lower, operation.old_name):
            return replace(key, field_name=operation.new_name)

        return key

    changed = [(before_value, context.names_after[remap(key)]) for key, before_value in context.names_before.items() if remap(key) in context.names_after and context.names_after[remap(key)] != before_value]

    if not changed:
        return []

    details = ", ".join(f"{old} -> {new}" for old, new in changed)
    advice = "Regenerate it with an interactive makemigrations so the rename is made deploy-safe automatically; for an indexed, unique or foreign-key field, or a model in an auto-created many-to-many table, keep the old names with db_column / db_table."
    return [context.finding("E005", f"This operation renames physical names old code still uses ({details}). {advice}")]


def deferred_rename_model_rule(context: OperationContext) -> list[Finding]:
    operation = context.operation

    if not isinstance(operation, DeferredRenameModel):
        return []

    if (problem := model_rename_problem(context.state_after, context.app_label, operation.new_name)) is None:
        return []

    return [context.finding("E012", f"DeferredRenameModel({operation.old_name!r}, {operation.new_name!r}) cannot run: {problem}")]


def type_change_rule(context: OperationContext) -> list[Finding]:
    operation = context.operation

    if not isinstance(operation, AlterField) or isinstance(operation, SetNotNull):
        return []

    if (old := before_field(context, operation.name)) is None:
        return []

    new = operation.field

    if any(isinstance(field, (models.ForeignKey, models.ManyToManyField, GeneratedField)) for field in (old, new)):
        return []

    old_type, new_type = old.db_type(context.connection), new.db_type(context.connection)

    # indexed=False on purpose: a string column widened to text is safe for old code whatever its indexes, and the index rebuild it causes is index_rule's E102, not this rule's.
    if is_allowed_type_change(old_type, new_type, indexed=False):
        return []

    return [context.finding("E006", f"AlterField({operation.model_name!r}, {operation.name!r}) changes the column type from {old_type} to {new_type}, which rewrites the table or breaks old code. Use InstallColumnSync + BackfillColumnSync.")]


def renamed_model_type_change_rule(context: OperationContext) -> list[Finding]:
    operation = context.operation

    if not isinstance(operation, AlterField) or isinstance(operation, SetNotNull) or model_key(context) not in context.renamed_in_batch:
        return []

    if (old := before_field(context, operation.name)) is None:
        return []

    new = operation.field

    if any(isinstance(field, (models.ForeignKey, models.ManyToManyField, GeneratedField)) for field in (old, new)):
        return []

    if old.db_type(context.connection) == new.db_type(context.connection):
        return []

    return [context.finding("E013", f"AlterField({operation.model_name!r}, {operation.name!r}) changes a column type on a model renamed earlier in this deploy. PostgreSQL refuses while the compatibility view exists, so ship the change in the next deploy.")]


def index_rule(context: OperationContext) -> list[Finding]:
    operation = context.operation

    if isinstance(operation, AddIndex) and not isinstance(operation, DjangoAddIndexConcurrently):
        if existed_before_migration(context, model_key(context)):
            return [context.finding("E101", f"AddIndex on {operation.model_name} blocks writes while the index builds. Use deferred_migrations.operations.AddIndexConcurrently in an atomic = False migration; makemigrations writes it automatically.")]

        return []

    if isinstance(operation, AddField) and not isinstance(operation, AddFieldConcurrently) and existed_before_migration(context, model_key(context)) and creates_index(operation.field):
        return [context.finding("E102", f"AddField({operation.model_name!r}, {operation.name!r}) builds an index or FK constraint while blocking writes. Use deferred_migrations.operations.AddFieldConcurrently in an atomic = False migration; makemigrations writes it automatically.")]

    if isinstance(operation, AlterField):
        if (old := before_field(context, operation.name)) is None:
            return []

        new = operation.field

        if isinstance(old, models.ManyToManyField) or isinstance(new, models.ManyToManyField):
            return []

        unique_added = new.unique and not new.primary_key and (not old.unique or old.primary_key)
        plain_index_added = new.db_index and not new.unique and (not old.db_index or old.unique)
        fk_constraint_added = isinstance(new, models.ForeignKey) and new.db_constraint and (not isinstance(old, models.ForeignKey) or not old.db_constraint)

        primary_key_added = new.primary_key and not old.primary_key

        if primary_key_added:
            return [context.finding("E102", f"AlterField({operation.model_name!r}, {operation.name!r}) adds a primary key while blocking reads and writes. Build the unique index concurrently, handle NOT NULL separately, then attach it with ADD CONSTRAINT ... PRIMARY KEY USING INDEX.")]

        if unique_added:
            return [context.finding("E102", f"AlterField({operation.model_name!r}, {operation.name!r}) adds a unique constraint while blocking writes. Build the unique index concurrently first: CREATE UNIQUE INDEX CONCURRENTLY, then ADD CONSTRAINT ... UNIQUE USING INDEX (see the README recipe).")]

        if plain_index_added:
            return [context.finding("E102", f"AlterField({operation.model_name!r}, {operation.name!r}) builds an index while blocking writes. Add it without the index, then AddIndexConcurrently.")]

        if fk_constraint_added:
            return [context.finding("E102", f"AlterField({operation.model_name!r}, {operation.name!r}) adds a foreign key constraint while blocking writes. Add it NOT VALID, then VALIDATE CONSTRAINT (see the README FK recipe).")]

        if creates_index(new) and not isinstance(old, models.ForeignKey) and not isinstance(new, models.ForeignKey):
            old_base = parse_type(old.db_type(context.connection) or "")[0]
            new_base = parse_type(new.db_type(context.connection) or "")[0]

            if old_base in STRING_TYPES and new_base == "text" and old_base != "text":
                return [context.finding("E102", f"AlterField({operation.model_name!r}, {operation.name!r}) changes an indexed string column to text, which rebuilds its index while blocking writes.")]

    return []


def constraint_rule(context: OperationContext) -> list[Finding]:
    operation = context.operation

    if isinstance(operation, AddConstraint) and not isinstance(operation, (AddConstraintNotValid, AddConstraintConcurrently)) and existed_before_migration(context, model_key(context)):
        return [context.finding("E103", f"AddConstraint on {operation.model_name} validates or indexes the whole table under a lock. Use deferred_migrations.operations.AddConstraintConcurrently in an atomic = False migration; makemigrations writes it automatically.")]

    if isinstance(operation, (AlterUniqueTogether, AlterIndexTogether)):
        key = (context.app_label, operation.name_lower)
        before = context.before_models.get(key)

        if before is None or key not in context.migration.models_at_start:
            return []

        old = {tuple(entry) for entry in before.options.get(operation.option_name, set())}
        new = {tuple(entry) for entry in operation.option_value or set()}

        if new - old:
            return [context.finding("E103", f"{operation.__class__.__name__} on {operation.name} builds an index under a lock. Use a concurrent index (see the README recipe).")]

    return []


def table_rewrite_rule(context: OperationContext) -> list[Finding]:
    operation = context.operation

    if not isinstance(operation, AddField) or not existed_before_migration(context, model_key(context)):
        return []

    field = operation.field

    if isinstance(field, GeneratedField) and field.db_persist:
        return [context.finding("E104", f"AddField({operation.model_name!r}, {operation.name!r}) adds a stored generated column, which rewrites the table under an exclusive lock.")]

    # Django stores constant db_defaults raw (only wrapping them in Value lazily), so a non-expression is constant too.
    is_constant = not hasattr(field.db_default, "resolve_expression") or isinstance(field.db_default, (Value, Now))

    if field.db_default is not NOT_PROVIDED and not is_constant:
        return [context.finding("E104", f"AddField({operation.model_name!r}, {operation.name!r}) has a volatile db_default, which rewrites the table under an exclusive lock. Add it nullable, then InstallNotNullFill + BackfillNotNull + SetNotNull, then AlterField to add the db_default.")]

    return []


def concurrent_in_transaction_rule(context: OperationContext) -> list[Finding]:
    if isinstance(context.operation, CONCURRENT_OPERATIONS) and context.migration.migration.atomic:
        return [context.finding("E105", f"{context.operation.__class__.__name__} cannot run inside a transaction. Set atomic = False on the migration.")]

    return []


# A non-atomic migration interrupted part-way is re-run from its first operation, so each operation must tolerate its own work already being done.
def rerun_safety_rule(context: MigrationContext) -> list[Finding]:
    if context.migration.atomic:
        return []

    return [
        Finding("E106", context.key[0], context.key[1], index, f"{operation.describe()} is in an atomic = False migration but fails if the migration is re-run after an interruption. Move it to an atomic migration, or use the deferred_migrations operation that does the same job.")
        for index, operation in enumerate(context.migration.operations)
        if not is_safe_to_rerun(operation)
    ]


def same_definition(first: Field, second: Field) -> bool:
    # Django only asks the rename question for identically-defined fields, so this is the shape a declined rename leaves behind.
    return first.deconstruct()[1:] == second.deconstruct()[1:]


def rename_written_as_removal_rule(context: OperationContext) -> list[Finding]:
    operation = context.operation
    others = [other for other in context.migration.migration.operations if other is not operation]
    advice = "If it is really a rename, the data is lost: regenerate it with an interactive makemigrations and answer yes to the rename question. If it is intentional, suppress E014 with deploy_safety_allowed and a reason."

    if isinstance(operation, RemoveField):
        before = context.before_models.get(model_key(context))

        if before is None or not has_own_table(before) or operation.name not in before.fields:
            return []

        removed = before.fields[operation.name]
        matches = [other.name for other in others if isinstance(other, AddField) and other.model_name_lower == operation.model_name_lower and other.name != operation.name and same_definition(removed, other.field)]

        if matches:
            return [context.finding("E014", f"{operation.model_name}.{operation.name} is removed and {matches[0]} is added with the same definition in this migration. {advice}")]

    if isinstance(operation, DeleteModel):
        before = context.before_models.get((context.app_label, operation.name_lower))

        if before is None or not has_own_table(before):
            return []

        matches = [other.name for other in others if isinstance(other, CreateModel) and other.name_lower != operation.name_lower and before.fields.keys() == {name for name, _field in other.fields} and all(same_definition(before.fields[name], field) for name, field in other.fields)]

        if matches:
            return [context.finding("E014", f"Model {operation.name} is deleted and {matches[0]} is created with the same fields in this migration. {advice}")]

    return []


OPERATION_RULES: list[Callable[[OperationContext], list[Finding]]] = [
    remove_field_rule,
    delete_model_rule,
    not_null_add_rule,
    not_null_alter_rule,
    generated_dependency_rule,
    rename_rule,
    deferred_rename_model_rule,
    type_change_rule,
    renamed_model_type_change_rule,
    index_rule,
    constraint_rule,
    table_rewrite_rule,
    rename_written_as_removal_rule,
    concurrent_in_transaction_rule,
]
MIGRATION_RULES: list[Callable[[MigrationContext], list[Finding]]] = [queue_dependency_rule, suppression_rule, rerun_safety_rule]
