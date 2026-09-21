from collections.abc import Callable
from dataclasses import dataclass

from django.apps import apps
from django.db import DEFAULT_DB_ALIAS
from django.db import connections
from django.db.backends.base.base import BaseDatabaseWrapper
from django.db.migrations.graph import MigrationGraph
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.migration import Migration
from django.db.migrations.operations import AlterField
from django.db.migrations.operations import AlterModelTable
from django.db.migrations.operations import RenameField
from django.db.migrations.operations import RenameModel
from django.db.migrations.operations import SeparateDatabaseAndState
from django.db.migrations.operations.base import Operation
from django.db.migrations.operations.fields import FieldOperation
from django.db.migrations.operations.models import IndexOperation
from django.db.migrations.operations.models import ModelOperation
from django.db.migrations.state import ModelState
from django.db.migrations.state import ProjectState

from deferred_migrations.safety.baselines import is_first_party
from deferred_migrations.safety.baselines import read_baseline
from deferred_migrations.safety.findings import Finding
from deferred_migrations.safety.names import ModelKey
from deferred_migrations.safety.names import PhysicalNameKey
from deferred_migrations.safety.names import physical_names

RENAME_CAPABLE = (RenameField, RenameModel, AlterField, AlterModelTable)


@dataclass
class MigrationContext:
    key: tuple[str, str]
    migration: Migration
    graph: MigrationGraph
    models_at_start: set[ModelKey]


@dataclass
class OperationContext:
    migration: MigrationContext
    index: int
    operation: Operation
    before_models: dict[ModelKey, ModelState]
    state_after: ProjectState
    connection: BaseDatabaseWrapper
    names_before: dict[PhysicalNameKey, str] | None = None
    names_after: dict[PhysicalNameKey, str] | None = None
    checked: bool = True
    # The operations that run after this one, which for a nested operation starts with the rest of its wrapper's database_operations.
    following: tuple[Operation, ...] = ()
    nested: bool = False
    # Models renamed by a DeferredRenameModel earlier in the unapplied migrations: their compatibility view still exists during this deploy.
    renamed_in_batch: frozenset[ModelKey] = frozenset()

    @property
    def app_label(self) -> str:
        return self.migration.migration.app_label

    def finding(self, rule_id: str, message: str) -> Finding:
        return Finding(rule_id, self.migration.key[0], self.migration.key[1], self.index, message)


def full_plan(graph: MigrationGraph) -> list[tuple[str, str]]:
    plan: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    for leaf in sorted(graph.leaf_nodes()):
        for key in graph.forwards_plan(leaf):
            if key not in seen:
                seen.add(key)
                plan.append(key)

    return plan


def touched_model_keys(app_label: str, operation: Operation) -> set[ModelKey]:
    if isinstance(operation, RenameModel):
        return {(app_label, operation.old_name_lower), (app_label, operation.new_name_lower)}

    if isinstance(operation, (FieldOperation, IndexOperation)):
        return {(app_label, operation.model_name_lower)}

    if isinstance(operation, ModelOperation):
        return {(app_label, operation.name_lower)}

    return set()


def models_before(state: ProjectState, app_label: str, operation: Operation) -> dict[ModelKey, ModelState]:
    return {model_key: state.models[model_key].clone() for model_key in touched_model_keys(app_label, operation) if model_key in state.models}


# Rules test the operation they are given, so a removal nested inside SeparateDatabaseAndState is invisible unless its database operations are checked too. State-only operations change no schema.
def database_operations(operation: Operation) -> tuple[Operation, ...]:
    if isinstance(operation, SeparateDatabaseAndState):
        return tuple(operation.database_operations)

    return ()


def apply_suppressions(migration: Migration, findings: list[Finding]) -> list[Finding]:
    allowed = migration.__class__.__dict__.get("deploy_safety_allowed", {})
    return [finding for finding in findings if finding.rule_id not in allowed or finding.rule_id == "E009"]


# track_renamed_in_batch is only meaningful when checked() marks exactly the unapplied migrations: in full mode a model renamed long ago would block type changes on it forever.
def check_graph(graph: MigrationGraph, checked: Callable[[tuple[str, str]], bool], connection: BaseDatabaseWrapper, baseline_findings: list[Finding] | None = None, track_renamed_in_batch: bool = False) -> list[Finding]:
    from deferred_migrations.operations import DeferredRenameModel
    from deferred_migrations.safety.pairing import PairingTracker
    from deferred_migrations.safety.rules import MIGRATION_RULES
    from deferred_migrations.safety.rules import OPERATION_RULES

    state = ProjectState()
    tracker = PairingTracker()
    renamed_in_batch: set[ModelKey] = set()
    findings: list[Finding] = list(baseline_findings or [])
    max_name_length = connection.ops.max_name_length()

    for key in full_plan(graph):
        migration = graph.nodes[key]
        is_checked = checked(key)
        migration_context = MigrationContext(key, migration, graph, set(state.models))
        migration_findings: list[Finding] = []

        if is_checked:
            for rule in MIGRATION_RULES:
                migration_findings.extend(rule(migration_context))

        for index, operation in enumerate(migration.operations):
            before_models = models_before(state, migration.app_label, operation) if is_checked else {}
            # SeparateDatabaseAndState applies only its state_operations to the state, so its database operations must be captured against the state as it stands now.
            nested = database_operations(operation) if is_checked else ()
            nested_before = [models_before(state, migration.app_label, nested_operation) for nested_operation in nested]
            rename_capable = is_checked and isinstance(operation, RENAME_CAPABLE)
            names_before = physical_names(state, max_name_length) if rename_capable else None
            operation.state_forwards(migration.app_label, state)
            names_after = physical_names(state, max_name_length) if rename_capable else None
            following = tuple(migration.operations[index + 1 :])
            context = OperationContext(migration_context, index, operation, before_models, state, connection, names_before, names_after, is_checked, following, renamed_in_batch=frozenset(renamed_in_batch))
            tracker.record(context)

            if isinstance(operation, RenameModel) and (context.app_label, operation.old_name_lower) in renamed_in_batch:
                renamed_in_batch.discard((context.app_label, operation.old_name_lower))
                renamed_in_batch.add((context.app_label, operation.new_name_lower))

            if track_renamed_in_batch and is_checked and isinstance(operation, DeferredRenameModel):
                renamed_in_batch.add((context.app_label, operation.new_name_lower))

            if not is_checked:
                continue

            for rule in OPERATION_RULES:
                migration_findings.extend(rule(context))

            for nested_index, nested_operation in enumerate(nested):
                nested_context = OperationContext(migration_context, index, nested_operation, nested_before[nested_index], state, connection, None, None, is_checked, (*nested[nested_index + 1 :], *following), nested=True, renamed_in_batch=frozenset(renamed_in_batch))

                for rule in OPERATION_RULES:
                    migration_findings.extend(rule(nested_context))

        findings.extend(apply_suppressions(migration, migration_findings))

    for finding in tracker.findings(checked):
        findings.extend(apply_suppressions(graph.nodes[(finding.app_label, finding.migration_name)], [finding]))

    return findings


def check_installed_project(applied: set[tuple[str, str]] | None = None, database: str = DEFAULT_DB_ALIAS) -> list[Finding]:
    loader = MigrationLoader(None, ignore_no_migrations=True)
    graph = loader.graph
    first_party = {config.label for config in apps.get_app_configs() if is_first_party(config)}
    skip: dict[str, set[tuple[str, str]]] = {}
    baseline_findings: list[Finding] = []

    for app_label in loader.migrated_apps:
        baseline = read_baseline(app_label)

        if baseline is None:
            if app_label not in first_party:
                skip[app_label] = {key for key in graph.nodes if key[0] == app_label}

            continue

        if (app_label, baseline) not in graph.nodes:
            baseline_findings.append(Finding("E010", app_label, baseline, None, f"Baseline file names {app_label}.{baseline}, which is not in the migration graph. Point it at an existing migration (for example the squashed migration that replaced it)."))
            skip[app_label] = {key for key in graph.nodes if key[0] == app_label}
            continue

        skip[app_label] = {key for key in graph.forwards_plan((app_label, baseline)) if key[0] == app_label}

    def checked(key: tuple[str, str]) -> bool:
        if key in skip.get(key[0], set()):
            return False

        return applied is None or key not in applied

    return check_graph(graph, checked, connections[database], baseline_findings, track_renamed_in_batch=applied is not None)
