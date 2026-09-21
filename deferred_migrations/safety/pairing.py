from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import replace

from django.db.migrations.operations import AddField
from django.db.migrations.operations import RenameModel
from django.db.models.fields import NOT_PROVIDED

from deferred_migrations.operations import BackfillColumnSync
from deferred_migrations.operations import BackfillNotNull
from deferred_migrations.operations import InstallColumnSync
from deferred_migrations.operations import InstallNotNullFill
from deferred_migrations.operations import SetNotNull
from deferred_migrations.safety.findings import Finding
from deferred_migrations.safety.rules import is_constant_default
from deferred_migrations.safety.walker import OperationContext


@dataclass(frozen=True)
class SyncKey:
    app_label: str
    model_name: str
    from_field: str
    to_field: str


@dataclass(frozen=True)
class FillKey:
    app_label: str
    model_name: str
    name: str


@dataclass
class InstallRecord:
    key: tuple[str, str]
    index: int
    sql: str
    backfilled: bool = False


class PairingTracker:
    def __init__(self) -> None:
        self.installs: dict[SyncKey | FillKey, InstallRecord] = {}
        self.issues: list[Finding] = []

    def record(self, context: OperationContext) -> None:
        operation = context.operation
        key = context.migration.key
        atomic = context.migration.migration.atomic

        if isinstance(operation, RenameModel):
            self.follow_rename(key[0], operation.old_name_lower, operation.new_name_lower)
            return

        if isinstance(operation, InstallColumnSync):
            self.installs[SyncKey(key[0], operation.model_name.lower(), operation.from_field, operation.to_field)] = InstallRecord(key, context.index, operation.forwards_sql)

            if context.checked:
                if not atomic:
                    self.issues.append(context.finding("E007", "InstallColumnSync must be in an atomic migration."))

                self.issues.extend(self.sync_target_issues(context, operation))
        elif isinstance(operation, InstallNotNullFill):
            self.installs[FillKey(key[0], operation.model_name.lower(), operation.name)] = InstallRecord(key, context.index, operation.fill_sql)

            if context.checked and not atomic:
                self.issues.append(context.finding("E007", "InstallNotNullFill must be in an atomic migration."))
        elif isinstance(operation, BackfillColumnSync):
            self.check_follow_up(context, SyncKey(key[0], operation.model_name.lower(), operation.from_field, operation.to_field), operation.forwards_sql, "BackfillColumnSync")
        elif isinstance(operation, BackfillNotNull):
            self.check_follow_up(context, FillKey(key[0], operation.model_name.lower(), operation.name), operation.fill_sql, "BackfillNotNull")
        elif isinstance(operation, SetNotNull):
            self.record_set_not_null(context, operation)

    def follow_rename(self, app_label: str, old_name: str, new_name: str) -> None:
        for install_key in [install_key for install_key in self.installs if (install_key.app_label, install_key.model_name) == (app_label, old_name)]:
            self.installs[replace(install_key, model_name=new_name)] = self.installs.pop(install_key)

    def check_follow_up(self, context: OperationContext, install_key: SyncKey | FillKey, sql: str | None, label: str) -> None:
        install = self.installs.get(install_key)
        # SetNotNull validates against the rows as they stand, so the backfill must already have run; a backfill in a later migration would satisfy the end-of-run check but not this one.
        needs_backfill_already_done = sql is None
        backfilled_before_now = install is not None and install.backfilled

        if install is not None and sql is not None:
            install.backfilled = True

        if not context.checked:
            return

        if context.migration.migration.atomic:
            self.issues.append(context.finding("E007", f"{label} must be in a migration with atomic = False."))

        if install is None or install.key == context.migration.key:
            self.issues.append(context.finding("E007", f"{label} needs a matching install operation in an earlier migration of the same app."))
        elif sql is not None and install.sql != sql:
            self.issues.append(context.finding("E007", f"{label} SQL does not match its install operation's SQL."))
        elif needs_backfill_already_done and not backfilled_before_now:
            self.issues.append(context.finding("E007", f"{label} runs before its backfill, so it would validate rows the backfill has not filled yet. Put the backfill in an earlier migration."))

    IDENTITY_SQL = "{from}"

    def identity_sync_into(self, app_label: str, model_name: str, field_name: str) -> SyncKey | None:
        for install_key, install in self.installs.items():
            if isinstance(install_key, SyncKey) and (install_key.app_label, install_key.model_name, install_key.to_field) == (app_label, model_name, field_name) and install.sql.strip() == self.IDENTITY_SQL:
                return install_key

        return None

    # The sync trigger copies a NOT NULL source unchanged, so once an identity backfill has run the target has no NULLs left and needs no fill trigger.
    def record_set_not_null(self, context: OperationContext, operation: SetNotNull) -> None:
        app_label = context.migration.key[0]
        fill_key = FillKey(app_label, operation.model_name_lower, operation.name)
        sync_key = None if fill_key in self.installs else self.identity_sync_into(app_label, operation.model_name_lower, operation.name)

        if sync_key is None:
            self.check_follow_up(context, fill_key, None, "SetNotNull")
            return

        self.check_follow_up(context, sync_key, None, "SetNotNull")

        # An unchecked migration has no before_models snapshot, and applied history is never re-reported.
        if not context.checked:
            return

        source = context.before_models[(app_label, operation.model_name_lower)].fields.get(sync_key.from_field)

        if source is not None and source.null:
            self.issues.append(context.finding("E007", f"SetNotNull on {operation.name} relies on its sync from {sync_key.from_field}, which is nullable, so the target can still be NULL. Use InstallNotNullFill + BackfillNotNull first."))

    def sync_target_issues(self, context: OperationContext, operation: InstallColumnSync) -> list[Finding]:
        model_state = context.state_after.models[(context.app_label, operation.model_name.lower())]
        target = model_state.fields[operation.to_field]
        source = model_state.fields[operation.from_field]
        issues: list[Finding] = []

        if target.db_default is not NOT_PROVIDED:
            issues.append(context.finding("E007", f"Sync target {operation.to_field} must not have a db_default; the trigger could not tell old code's inserts need filling."))

        if not target.null:
            added_with_constant_default = any(isinstance(other, AddField) and other.model_name_lower == operation.model_name.lower() and other.name == operation.to_field and is_constant_default(other.field) for other in context.migration.migration.operations)

            if not added_with_constant_default:
                issues.append(context.finding("E007", f"NOT NULL sync target {operation.to_field} must be added in the same migration with a constant default."))

            if source.null:
                issues.append(context.finding("E007", f"NOT NULL sync target {operation.to_field} needs a NOT NULL source; {operation.from_field} is nullable."))

        return issues

    def findings(self, checked: Callable[[tuple[str, str]], bool]) -> list[Finding]:
        missing = [Finding("E007", install.key[0], install.key[1], install.index, "Install operation has no matching backfill in a later non-atomic migration.") for install in self.installs.values() if not install.backfilled and checked(install.key)]
        return [*self.issues, *missing]
