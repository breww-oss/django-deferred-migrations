from dataclasses import dataclass
from dataclasses import field

from django.apps import apps
from django.conf import settings
from django.core.management import CommandError
from django.db import models
from django.db.backends.base.base import BaseDatabaseWrapper
from django.db.migrations import swappable_dependency
from django.db.migrations.autodetector import MigrationAutodetector
from django.db.migrations.graph import MigrationGraph
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.migration import Migration
from django.db.migrations.operations import AddConstraint
from django.db.migrations.operations import AddField
from django.db.migrations.operations import AddIndex
from django.db.migrations.operations import AlterConstraint
from django.db.migrations.operations import AlterField
from django.db.migrations.operations import AlterIndexTogether
from django.db.migrations.operations import AlterUniqueTogether
from django.db.migrations.operations import DeleteModel
from django.db.migrations.operations import RemoveConstraint
from django.db.migrations.operations import RemoveField
from django.db.migrations.operations import RemoveIndex
from django.db.migrations.operations import RenameField
from django.db.migrations.operations import RenameIndex
from django.db.migrations.operations import RenameModel
from django.db.migrations.operations import RunPython
from django.db.migrations.operations import RunSQL
from django.db.migrations.operations import SeparateDatabaseAndState
from django.db.migrations.operations.base import Operation
from django.db.migrations.operations.models import IndexOperation
from django.db.migrations.state import ProjectState
from django.db.models import Field

from deferred_migrations.checks import PACKAGE
from deferred_migrations.eligibility import field_rename_problem
from deferred_migrations.eligibility import model_rename_problem
from deferred_migrations.eligibility import option_field_names
from deferred_migrations.operations import AddConstraintConcurrently
from deferred_migrations.operations import AddFieldConcurrently
from deferred_migrations.operations import AddIndexConcurrently
from deferred_migrations.operations import BackfillColumnSync
from deferred_migrations.operations import DeferredDeleteModel
from deferred_migrations.operations import DeferredRemoveField
from deferred_migrations.operations import DeferredRenameModel
from deferred_migrations.operations import InstallColumnSync
from deferred_migrations.operations import SetNotNull
from deferred_migrations.safety.baselines import is_first_party
from deferred_migrations.safety.baselines import read_baseline
from deferred_migrations.safety.findings import Finding
from deferred_migrations.safety.rules import is_constant_default
from deferred_migrations.safety.rules import is_safe_to_rerun
from deferred_migrations.safety.rules import required_queue_dependency
from deferred_migrations.safety.walker import check_graph


@dataclass
class FixReport:
    changed: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    fixed: set[tuple[str, str]] = field(default_factory=set)

    def lines(self, dry_run: bool) -> list[str]:
        prefix = "Would make deploy-safe" if dry_run else "Made deploy-safe"
        lines = [f"{prefix}: {change}" for change in self.changed]
        lines += self.notes
        lines += [finding.format() for finding in self.findings]

        if self.changed or self.findings:
            fixed, found = len(self.fixed), len(self.findings)
            migrations = "1 migration" if fixed == 1 else f"{fixed} migrations"
            findings = "1 finding needs" if found == 1 else f"{found} findings need"
            made = "would be made" if dry_run else "made"
            lines.append(f"{migrations} {made} deploy-safe; {findings} a manual fix (see the fixing-deploy-safety skill).")

        return lines


def expand_field_rename(operation: RenameField, field: Field) -> tuple[list[Operation], list[Operation]]:
    model_name, old_name, new_name = operation.model_name, operation.old_name, operation.new_name
    final = field.clone()
    follow_up: list[Operation] = [BackfillColumnSync(model_name, from_field=old_name, to_field=new_name, forwards_sql="{from}")]

    if field.null:
        added = AddField(model_name, new_name, final)
    elif is_constant_default(field):
        # PostgreSQL 11+ adds a column with a constant default without rewriting the table, and Django then drops the database default.
        added = AddField(model_name, new_name, final, preserve_default=True)
    else:
        nullable = field.clone()
        nullable.null = True
        added = AddField(model_name, new_name, nullable)
        follow_up.append(SetNotNull(model_name, new_name, final))

    follow_up.append(DeferredRemoveField(model_name, old_name, renamed_to=new_name))
    return [added, InstallColumnSync(model_name, from_field=old_name, to_field=new_name, forwards_sql="{from}", backwards_sql="{to}")], follow_up


def concurrent_version(operation: Operation) -> Operation | None:
    # Matched by exact type: subclasses such as the deferred operations are not plain builds.
    if type(operation) is AddField and not operation.field.many_to_many and not operation.field.primary_key:
        return AddFieldConcurrently(operation.model_name, operation.name, operation.field, operation.preserve_default)

    if type(operation) is AddIndex:
        return AddIndexConcurrently(operation.model_name, operation.index)

    if type(operation) is AddConstraint and isinstance(operation.constraint, (models.UniqueConstraint, models.CheckConstraint)):
        return AddConstraintConcurrently(operation.model_name, operation.constraint)

    return None


def fields_built(build: AddFieldConcurrently | AddIndexConcurrently | AddConstraintConcurrently) -> set[str]:
    if isinstance(build, AddFieldConcurrently):
        return {build.name}

    if isinstance(build, AddIndexConcurrently):
        return option_field_names(build.index)

    return option_field_names(build.constraint)


def name_built(build: AddFieldConcurrently | AddIndexConcurrently | AddConstraintConcurrently) -> str | None:
    if isinstance(build, AddIndexConcurrently):
        return build.index.name

    if isinstance(build, AddConstraintConcurrently):
        return build.constraint.name

    return None


# Django's index and constraint operations answer references_field with True for every model, so they are matched on their own fields and names instead.
def depends_on_build(operation: Operation, build: AddFieldConcurrently | AddIndexConcurrently | AddConstraintConcurrently, app_label: str) -> bool:
    if isinstance(operation, IndexOperation):
        if operation.model_name_lower != build.model_name_lower:
            return False

        if isinstance(operation, AddIndex):
            return bool(option_field_names(operation.index) & fields_built(build))

        if isinstance(operation, AddConstraint):
            return bool(option_field_names(operation.constraint) & fields_built(build))

        if isinstance(operation, (RemoveIndex, RemoveConstraint, AlterConstraint)):
            return operation.name == name_built(build)

        if isinstance(operation, RenameIndex):
            return name_built(build) is not None and operation.old_name == name_built(build)

        return True

    # RunSQL, RunPython and SeparateDatabaseAndState claim to reference every model; moving one would silently take a data step out of its atomic migration.
    if isinstance(operation, (RunSQL, RunPython, SeparateDatabaseAndState)):
        return False

    if isinstance(operation, (AlterUniqueTogether, AlterIndexTogether)):
        return operation.name_lower == build.model_name_lower and any(fields_built(build) & set(group) for group in operation.option_value or ())

    return any(operation.references_field(build.model_name, field_name, app_label) for field_name in fields_built(build))


# E106 trusts RunSQL and RunPython to be idempotent, but the autofix never makes an author's code non-atomic on their behalf.
def runs_hand_written_code(operation: Operation) -> bool:
    if isinstance(operation, SeparateDatabaseAndState):
        return any(runs_hand_written_code(nested) for nested in operation.database_operations)

    return isinstance(operation, (RunSQL, RunPython))


def rename_follow_up_suffix(operations: list[Operation]) -> str:
    removals = [operation for operation in operations if isinstance(operation, DeferredRemoveField)]

    if len(removals) == 1:
        return f"rename_{removals[0].model_name_lower}_{removals[0].name}_to_{removals[0].renamed_to}_backfill"

    return "backfill_renamed_columns"


def concurrent_follow_up_suffix(operations: list[Operation]) -> str:
    if len(operations) == 1:
        return f"{operations[0].migration_name_fragment}_concurrent"

    return "concurrent_builds"


class AutoFixer:
    def __init__(self, changes: dict[str, list[Migration]], connection: BaseDatabaseWrapper, replaced: dict[str, str] | None = None, allow_new_migrations: bool = True) -> None:
        self.changes = changes
        self.connection = connection
        self.replaced = replaced or {}
        self.allow_new_migrations = allow_new_migrations
        self.report = FixReport()
        self.loader = MigrationLoader(None, ignore_no_migrations=True)
        self.graph = self.build_graph()
        self.follow_ups: dict[str, Migration] = {}

    def run(self) -> FixReport:
        self.expand_field_renames()
        self.defer_model_renames()
        # One walk serves both passes: a deferred removal changes the state exactly as the plain one, so it cannot change an index or constraint finding.
        findings = self.findings()
        self.defer_removals(findings)
        self.make_builds_concurrent(findings)
        self.add_queue_dependencies()
        self.report.findings = self.findings()
        return self.report

    # The same scope as check_deploy_safety, which skips third-party apps that have no baseline.
    def fixes_app(self, app_label: str) -> bool:
        return app_label != PACKAGE and (is_first_party(apps.get_app_config(app_label)) or read_baseline(app_label) is not None)

    def new_keys(self) -> set[tuple[str, str]]:
        return {(app_label, migration.name) for app_label, migrations in self.changes.items() if self.fixes_app(app_label) for migration in migrations}

    # Two passes: a migration can depend on another app's migration from the same run, which must already be a node.
    def build_graph(self) -> MigrationGraph:
        graph = self.loader.graph

        for app_label, migrations in self.changes.items():
            for migration in migrations:
                graph.add_node((app_label, migration.name), migration)

        for app_label, migrations in self.changes.items():
            for migration in migrations:
                self.add_dependencies(app_label, migration)

        # --update always writes the merged leaf under a new name, so the leaf it replaces must leave the graph or its operations would apply twice.
        for app_label, old_name in self.replaced.items():
            if (app_label, old_name) in graph.nodes and (new_migrations := self.changes.get(app_label)):
                graph.remove_replaced_nodes((app_label, new_migrations[-1].name), [(app_label, old_name)])

        return graph

    def add_to_graph(self, app_label: str, migration: Migration) -> None:
        self.loader.graph.add_node((app_label, migration.name), migration)
        self.add_dependencies(app_label, migration)

    def add_dependencies(self, app_label: str, migration: Migration) -> None:
        graph = self.loader.graph

        for dependency in migration.dependencies:
            # The autodetector writes swappable dependencies as ("__setting__", name); the migration writer only turns them into swappable_dependency() in the file.
            if dependency[0] == "__setting__":
                dependency = swappable_dependency(getattr(settings, dependency[1]))

            if (resolved := self.loader.check_key(dependency, app_label)) is not None and resolved in graph.nodes:
                graph.add_dependency(migration, (app_label, migration.name), resolved)

    def state_before(self, app_label: str, migration: Migration, index: int) -> ProjectState:
        state = self.graph.make_state(nodes=(app_label, migration.name), at_end=False, real_apps=self.loader.unmigrated_apps)

        for operation in migration.operations[:index]:
            operation.state_forwards(app_label, state)

        return state

    def findings(self) -> list[Finding]:
        new_keys = self.new_keys()
        return check_graph(self.graph, lambda key: key in new_keys, self.connection, track_renamed_in_batch=True)

    def expand_field_renames(self) -> None:
        follow_ups: dict[str, list[Operation]] = {}

        for app_label, migrations in self.changes.items():
            if not self.fixes_app(app_label):
                continue

            for migration in migrations:
                index = 0

                while index < len(migration.operations):
                    operation = migration.operations[index]

                    if type(operation) is RenameField and (expanded := self.expand_if_eligible(app_label, migration, index, operation)) is not None:
                        immediate, later = expanded
                        migration.operations[index : index + 1] = immediate
                        follow_ups.setdefault(app_label, []).extend(later)
                        self.record_change(app_label, migration, f"{operation.describe()} keeps both columns in sync until after deploy")
                        index += len(immediate)
                        continue

                    index += 1

        for app_label, operations in follow_ups.items():
            self.append_follow_up(app_label, operations, rename_follow_up_suffix(operations))

    def expand_if_eligible(self, app_label: str, migration: Migration, index: int, operation: RenameField) -> tuple[list[Operation], list[Operation]] | None:
        state = self.state_before(app_label, migration, index)
        field = state.models[(app_label, operation.model_name_lower)].fields[operation.old_name]

        # A rename that keeps its column through db_column changes nothing physical and is already safe.
        if field.db_column is not None:
            return None

        problem = field_rename_problem(state, app_label, operation.model_name_lower, operation.old_name, self.connection) or self.later_rename_blocker(app_label, migration, index, operation)

        if problem is not None:
            self.report.notes.append(f"{app_label}.{migration.name}: {operation.describe()} was left as a RenameField, which check_deploy_safety reports as E005: {problem} This version only renames plain columns automatically.")
            return None

        if not self.allow_new_migrations:
            raise CommandError(f"{operation.describe()} needs a generated follow-up migration, which --update cannot write. Run a normal makemigrations run instead.")

        return expand_field_rename(operation, field)

    def later_rename_blocker(self, app_label: str, migration: Migration, index: int, operation: RenameField) -> str | None:
        position = self.changes[app_label].index(migration)
        later = [*migration.operations[index + 1 :], *(other for following in self.changes[app_label][position + 1 :] for other in following.operations)]
        meta_reason = f"{app_label}.{operation.model_name}.{operation.new_name} is referenced by Meta indexes, constraints or together options."

        for other in later:
            if isinstance(other, (AddField, AlterField, RemoveField, RenameField)) and other.model_name_lower == operation.model_name_lower:
                if (other.old_name if isinstance(other, RenameField) else other.name) == operation.new_name:
                    return f"{operation.new_name} is changed again later in this run."
            elif isinstance(other, AddIndex) and other.model_name_lower == operation.model_name_lower:
                if operation.new_name in option_field_names(other.index):
                    return meta_reason
            elif isinstance(other, AddConstraint) and other.model_name_lower == operation.model_name_lower:
                if operation.new_name in option_field_names(other.constraint):
                    return meta_reason
            elif isinstance(other, (AlterUniqueTogether, AlterIndexTogether)) and other.name_lower == operation.model_name_lower:
                if any(operation.new_name in group for group in other.option_value or ()):
                    return meta_reason

        return None

    def append_follow_up(self, app_label: str, operations: list[Operation], suffix: str) -> Migration:
        migrations = self.changes[app_label]
        number = max(MigrationAutodetector.parse_number(migration.name) or 0 for migration in migrations) + 1
        follow_up = Migration(f"{number:04d}_{suffix}", app_label)
        follow_up.atomic = False
        follow_up.dependencies = [(app_label, migrations[-1].name)]
        follow_up.operations = operations
        # Appended last so django-linear-migrations records it in max_migration.txt.
        migrations.append(follow_up)
        self.follow_ups[app_label] = follow_up
        self.add_to_graph(app_label, follow_up)
        self.report.fixed.add((app_label, follow_up.name))
        return follow_up

    def defer_model_renames(self) -> None:
        for app_label, migrations in self.changes.items():
            if not self.fixes_app(app_label):
                continue

            for migration in migrations:
                for index, operation in enumerate(migration.operations):
                    if type(operation) is not RenameModel:
                        continue

                    state = self.state_before(app_label, migration, index)

                    # A pinned table keeps its name, so the rename is already safe.
                    if state.models[(app_label, operation.old_name_lower)].options.get("db_table"):
                        continue

                    if (problem := model_rename_problem(state, app_label, operation.old_name)) is not None:
                        self.report.notes.append(f"{app_label}.{migration.name}: {operation.describe()} was left as a RenameModel, which check_deploy_safety reports as E005: {problem}")
                        continue

                    if not self.allow_new_migrations:
                        raise CommandError(f"{operation.describe()} is made deploy-safe only by a normal makemigrations run, not --update.")

                    migration.operations[index] = DeferredRenameModel(operation.old_name, operation.new_name)
                    self.record_change(app_label, migration, f"{operation.describe()} serves the old name through a view until after deploy")

    def defer_removals(self, findings: list[Finding]) -> None:
        for finding in findings:
            if finding.rule_id not in ("E001", "E002") or finding.operation_index is None:
                continue

            migration = next(migration for migration in self.changes[finding.app_label] if migration.name == finding.migration_name)
            operation = migration.operations[finding.operation_index]

            # Matched by exact type: the deferred operations subclass these, and a finding on a SeparateDatabaseAndState points at the wrapper, which is left for a person.
            if type(operation) is RemoveField:
                migration.operations[finding.operation_index] = DeferredRemoveField(operation.model_name, operation.name)
            elif type(operation) is DeleteModel:
                migration.operations[finding.operation_index] = DeferredDeleteModel(operation.name)
            else:
                continue

            self.record_change(finding.app_label, migration, f"{operation.describe()} is deferred until after deploy")

    def make_builds_concurrent(self, findings: list[Finding]) -> None:
        swapped: dict[tuple[str, str], dict[int, Operation]] = {}

        for finding in findings:
            if finding.rule_id not in ("E101", "E102", "E103") or finding.operation_index is None:
                continue

            migration = next(migration for migration in self.changes[finding.app_label] if migration.name == finding.migration_name)
            original = migration.operations[finding.operation_index]

            if (replacement := concurrent_version(original)) is None:
                continue

            migration.operations[finding.operation_index] = replacement
            swapped.setdefault((finding.app_label, migration.name), {})[finding.operation_index] = original

        for (app_label, name), originals in swapped.items():
            migration = next(migration for migration in self.changes[app_label] if migration.name == name)
            self.place_concurrent_builds(app_label, migration, originals)

    def place_concurrent_builds(self, app_label: str, migration: Migration, originals: dict[int, Operation]) -> None:
        described = ", ".join(original.describe() for original in originals.values())

        if all(is_safe_to_rerun(operation) and not runs_hand_written_code(operation) for operation in migration.operations):
            migration.atomic = False
            self.record_change(app_label, migration, f"{described} builds without blocking writes in a non-atomic migration")
            return

        # Hand-written code is opaque, so a step after a build may read what it creates, and moving the build would run it too late.
        if any(runs_hand_written_code(operation) for operation in migration.operations[min(originals) + 1 :]):
            self.revert_concurrent_builds(app_label, migration, originals, "hand-written RunSQL or RunPython follows it, which may depend on what it builds; split it by hand")
            return

        if (moving := self.operations_to_move(app_label, migration, set(originals))) is None:
            self.revert_concurrent_builds(app_label, migration, originals, "a later operation in the same migration depends on it and cannot be re-run safely; split it by hand")
            return

        migrations = self.changes[app_label]
        position = migrations.index(migration)
        follow_up = self.follow_ups.get(app_label)
        moved = [migration.operations[index] for index in moving]

        if follow_up is not None and position + 1 < len(migrations) and migrations[position + 1] is follow_up:
            follow_up.operations.extend(moved)
        elif position == len(migrations) - 1:
            if not self.allow_new_migrations:
                raise CommandError(f"{described} needs a generated follow-up migration to build without blocking writes, which --update cannot write. Run a normal makemigrations run instead.")

            follow_up = self.append_follow_up(app_label, moved, concurrent_follow_up_suffix(moved))
        else:
            self.revert_concurrent_builds(app_label, migration, originals, "a later migration in this app, generated in this run, follows it; split it by hand")
            return

        migration.operations = [operation for index, operation in enumerate(migration.operations) if index not in moving]
        self.record_change(app_label, migration, f"{described} builds without blocking writes, moved to {follow_up.name}")
        self.repoint_dependents((app_label, migration.name), (app_label, follow_up.name))

    # A later operation that touches what a moved build creates must run after it, so it moves too when it can be re-run.
    def operations_to_move(self, app_label: str, migration: Migration, swapped: set[int]) -> list[int] | None:
        moving = set(swapped)

        for index in range(min(swapped) + 1, len(migration.operations)):
            if index in moving:
                continue

            operation = migration.operations[index]
            builds = [migration.operations[position] for position in swapped if position < index]

            if not any(depends_on_build(operation, build, app_label) for build in builds):
                continue

            if not is_safe_to_rerun(operation):
                return None

            moving.add(index)

        return sorted(moving)

    # The graph keeps the old edge, which MigrationGraph cannot remove; it is harmless because the follow-up itself depends on the original.
    def repoint_dependents(self, old: tuple[str, str], new: tuple[str, str]) -> None:
        for migrations in self.changes.values():
            for migration in migrations:
                key = (migration.app_label, migration.name)

                if key != new and old in migration.dependencies:
                    migration.dependencies = [new if dependency == old else dependency for dependency in migration.dependencies]
                    self.graph.add_dependency(migration, key, new)

    def revert_concurrent_builds(self, app_label: str, migration: Migration, originals: dict[int, Operation], reason: str) -> None:
        for index, original in originals.items():
            migration.operations[index] = original

        self.report.notes.append(f"{app_label}.{migration.name}: {', '.join(original.describe() for original in originals.values())} left as written, which check_deploy_safety reports: {reason}.")

    def add_queue_dependencies(self) -> None:
        for app_label, migrations in self.changes.items():
            if not self.fixes_app(app_label):
                continue

            for migration in migrations:
                needed = required_queue_dependency(migration)

                if needed is None or needed in migration.dependencies or needed not in self.graph.nodes:
                    continue

                migration.dependencies.append(needed)
                self.graph.add_dependency(migration, (app_label, migration.name), needed)

    def record_change(self, app_label: str, migration: Migration, description: str) -> None:
        self.report.changed.append(f"{app_label}.{migration.name}: {description}")
        self.report.fixed.add((app_label, migration.name))
