from argparse import ArgumentParser

from django.apps import apps
from django.core.management import BaseCommand
from django.core.management import CommandError
from django.db.migrations.loader import MigrationLoader

from deferred_migrations.safety.baselines import baseline_path
from deferred_migrations.safety.baselines import is_first_party


class Command(BaseCommand):
    help = "Write deferred_migrations_baseline.txt for first-party apps so only later migrations are checked."
    requires_system_checks = []

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument("--app", action="append", dest="app_labels", default=[])
        parser.add_argument("--overwrite", action="store_true")

    def handle(self, *args: str, app_labels: list[str], overwrite: bool, **options: object) -> None:
        graph = MigrationLoader(None, ignore_no_migrations=True).graph

        for app_config in apps.get_app_configs():
            if not is_first_party(app_config) or (app_labels and app_config.label not in app_labels):
                continue

            leaves = graph.leaf_nodes(app_config.label)
            path = baseline_path(app_config.label)

            if not leaves or path is None:
                continue

            if len(leaves) > 1:
                raise CommandError(f"{app_config.label} has more than one leaf migration: {leaves}")

            if path.exists() and not overwrite:
                self.stdout.write(f"Kept existing baseline for {app_config.label}")
                continue

            path.write_text(f"{leaves[0][1]}\n")
            self.stdout.write(f"Wrote baseline {app_config.label}: {leaves[0][1]}")
