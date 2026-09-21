import copy
from dataclasses import dataclass
from pathlib import Path

from django.apps import apps
from django.core.management import CommandError
from django.db import DEFAULT_DB_ALIAS
from django.db import connections
from django.db.migrations.graph import MigrationGraph
from django.db.migrations.migration import Migration
from django.db.migrations.questioner import MigrationQuestioner
from django.db.migrations.questioner import NonInteractiveMigrationQuestioner
from django.db.migrations.state import ModelState
from django.db.migrations.state import ProjectState
from django.db.migrations.writer import MigrationWriter
from django.db.models import Field

from deferred_migrations.autofix import AutoFixer
from deferred_migrations.checks import fix_on_makemigrations
from deferred_migrations.checks import underlying_command

Underlying = underlying_command("makemigrations", apps.get_app_configs())


@dataclass
class RenameCandidate:
    app_label: str
    message: str


# Django's non-interactive questioner answers "no" to every rename, silently turning it into a remove plus an add that loses the data.
class RenameRecordingQuestioner(NonInteractiveMigrationQuestioner):
    def __init__(self, original: NonInteractiveMigrationQuestioner, from_state: ProjectState, to_state: ProjectState) -> None:
        super().__init__(defaults=original.defaults, specified_apps=original.specified_apps, dry_run=original.dry_run, verbosity=original.verbosity, log=original.log)
        self.from_state = from_state
        self.to_state = to_state
        self.candidates: list[RenameCandidate] = []

    # Django passes no app label here, so it is every app whose model of this name gained the new field and lost the old one.
    def ask_rename(self, model_name: str, old_name: str, new_name: str, field_instance: Field) -> bool:
        for (app_label, name), model_state in sorted(self.to_state.models.items()):
            previous = self.from_state.models.get((app_label, name))

            if name == model_name and previous is not None and old_name in previous.fields and new_name not in previous.fields and new_name in model_state.fields and old_name not in model_state.fields:
                self.candidates.append(RenameCandidate(app_label, f"{app_label}.{model_state.name}.{old_name} looks like it was renamed to {new_name}."))

        return False

    def ask_rename_model(self, old_model_state: ModelState, new_model_state: ModelState) -> bool:
        self.candidates.append(RenameCandidate(old_model_state.app_label, f"{old_model_state.app_label}.{old_model_state.name} looks like it was renamed to {new_model_state.name}."))
        return False


class RenameRefusingAutodetector(Underlying.autodetector):
    def __init__(self, from_state: ProjectState, to_state: ProjectState, questioner: MigrationQuestioner | None = None) -> None:
        if isinstance(questioner, NonInteractiveMigrationQuestioner):
            questioner = RenameRecordingQuestioner(questioner, from_state, to_state)

        super().__init__(from_state, to_state, questioner)

    def changes(self, graph: MigrationGraph, trim_to_apps: set[str] | None = None, convert_apps: set[str] | None = None, migration_name: str | None = None) -> dict[str, list[Migration]]:
        changes = super().changes(graph, trim_to_apps, convert_apps, migration_name)

        if refused := self.refused_renames(changes):
            advice = (
                'Run makemigrations interactively to confirm each rename, so the data carries over. If one really is a removal plus an unrelated new field or model, make the removal and the addition in two separate runs, or answer "no" interactively and suppress the resulting E014 with a reason.'
            )
            raise CommandError("\n".join([*refused, advice]))

        return changes

    # Django asks about renames in every app before trimming, and the trim keeps the apps the requested ones depend on, so what matters is whether the declined rename's app is about to be written.
    def refused_renames(self, changes: dict[str, list[Migration]]) -> list[str]:
        if not isinstance(self.questioner, RenameRecordingQuestioner):
            return []

        return [candidate.message for candidate in self.questioner.candidates if candidate.app_label in changes]


class Command(Underlying):
    # Set per run, not on the class: Django's commands.E001 requires the class attribute to match migrate's.
    def handle(self, *app_labels: str, **options: object) -> str | None:
        if fix_on_makemigrations():
            self.autodetector = RenameRefusingAutodetector

        return super().handle(*app_labels, **options)

    def write_migration_files(self, changes: dict[str, list[Migration]], update_previous_migration_paths: dict[str, str] | None = None) -> None:
        if not fix_on_makemigrations():
            return super().write_migration_files(changes, update_previous_migration_paths)

        # --dry-run and --check write nothing, so they report what would change without changing what Django prints.
        target = copy.deepcopy(changes) if self.dry_run else changes
        replaced = {app_label: Path(path).stem for app_label, path in (update_previous_migration_paths or {}).items()}
        report = AutoFixer(target, connections[DEFAULT_DB_ALIAS], replaced, allow_new_migrations=update_previous_migration_paths is None).run()
        super().write_migration_files(changes, update_previous_migration_paths)

        if not self.dry_run:
            self.write_non_atomic_flags(changes)

        if self.verbosity >= 1:
            for line in report.lines(self.dry_run):
                self.log(line)

    # Django 5.2's MigrationWriter drops Migration.atomic; 6.0 and later write this same line, so there the file already has it.
    def write_non_atomic_flags(self, changes: dict[str, list[Migration]]) -> None:
        for migration in (migration for app_migrations in changes.values() for migration in app_migrations if not migration.atomic):
            path = Path(MigrationWriter(migration).path)
            source = path.read_text(encoding="utf-8")

            if "\n    atomic = False\n" not in source:
                path.write_text(source.replace("\n    dependencies = [", "\n    atomic = False\n\n    dependencies = [", 1), encoding="utf-8")
