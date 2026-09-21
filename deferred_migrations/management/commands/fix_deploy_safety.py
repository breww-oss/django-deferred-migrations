import sys
from argparse import ArgumentParser
from collections import Counter
from pathlib import Path

from django.core.management import BaseCommand
from django.db import DEFAULT_DB_ALIAS
from django.db import connections
from django.db.migrations.loader import MigrationLoader

from deferred_migrations.safety.rewrite import rewrite_migration_source
from deferred_migrations.safety.rules import RENAME_RECORD_DEPENDENCY
from deferred_migrations.safety.rules import required_queue_dependency
from deferred_migrations.safety.walker import check_installed_project

FIXABLE = {"E001", "E002", "E008"}


class Command(BaseCommand):
    help = "Rewrite plain RemoveField / DeleteModel into their deferred versions and add the queue dependency."
    requires_system_checks = []

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument("app_label", nargs="?")
        parser.add_argument("migration_name", nargs="?")

    def handle(self, *args: str, app_label: str | None, migration_name: str | None, **options: object) -> None:
        loader = MigrationLoader(None, ignore_no_migrations=True)
        # A loader without a connection reports nothing as applied, so read that from the database; the fixer must never edit a migration that has already run.
        applied = set(MigrationLoader(connections[DEFAULT_DB_ALIAS], ignore_no_migrations=True).applied_migrations)
        findings = [finding for finding in check_installed_project(applied) if (app_label is None or finding.app_label == app_label) and (migration_name is None or finding.migration_name == migration_name)]
        by_migration: dict[tuple[str, str], Counter[str]] = {}

        for finding in findings:
            # The rewriter can only add the 0001 dependency; a DeferredRenameModel needs 0002, which is left for a person.
            needs_rename_record = finding.rule_id == "E008" and required_queue_dependency(loader.graph.nodes[(finding.app_label, finding.migration_name)]) == RENAME_RECORD_DEPENDENCY

            if finding.rule_id in FIXABLE and not needs_rename_record:
                by_migration.setdefault((finding.app_label, finding.migration_name), Counter())[finding.rule_id] += 1
            else:
                self.stdout.write(f"Needs a decision: {finding.format()}")

        for key, counts in sorted(by_migration.items()):
            migration = loader.graph.nodes[key]
            path = Path(sys.modules[migration.__class__.__module__].__file__)
            result = rewrite_migration_source(path.read_text(), counts["E001"], counts["E002"])

            if result.problems:
                for problem in result.problems:
                    self.stdout.write(f"Not changed {key[0]}.{key[1]}: {problem}")

                continue

            if result.changed:
                path.write_text(result.source)
                self.stdout.write(f"Fixed {key[0]}.{key[1]}")
