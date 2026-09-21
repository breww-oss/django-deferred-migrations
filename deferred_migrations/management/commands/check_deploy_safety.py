from argparse import ArgumentParser

from django.core.management import BaseCommand
from django.core.management import CommandError
from django.db import DEFAULT_DB_ALIAS
from django.db import connections
from django.db.migrations.loader import MigrationLoader

from deferred_migrations.safety.walker import check_installed_project


class Command(BaseCommand):
    help = "Report migrations that are unsafe while old and new code run side by side during a deploy."
    requires_system_checks = []

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument("--unapplied-only", action="store_true", help="Only report migrations not yet applied to --database.")
        parser.add_argument("--database", default=DEFAULT_DB_ALIAS)

    def handle(self, *args: str, unapplied_only: bool, database: str, **options: object) -> None:
        # MigrationLoader resolves the replacement map, so a squashed migration whose replaced members have run counts as applied; MigrationRecorder alone would re-check it.
        applied = set(MigrationLoader(connections[database], ignore_no_migrations=True).applied_migrations) if unapplied_only else None
        findings = check_installed_project(applied, database)

        for finding in findings:
            self.stderr.write(finding.format())

        if findings:
            raise CommandError(f"{len(findings)} deploy safety error(s). Run fix_deploy_safety for the mechanical ones.")

        self.stdout.write("No deploy safety errors.")
