from argparse import ArgumentParser

from django.core.exceptions import ImproperlyConfigured
from django.core.management import BaseCommand
from django.core.management import CommandError
from django.db import DEFAULT_DB_ALIAS
from django.db import connections

from deferred_migrations.notice import phase_command_in_charge
from deferred_migrations.runner import describe_run_result
from deferred_migrations.runner import run_deferred_operations


class Command(BaseCommand):
    help = "Run destructive schema operations queued by deferred migration operations, after the new code is fully rolled out."
    requires_system_checks = []

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument("--database", default=DEFAULT_DB_ALIAS)
        parser.add_argument("--wait-before-seconds", type=int, default=0)
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--fail-on-error", action="store_true")

    def handle(self, *args: str, database: str, wait_before_seconds: int, dry_run: bool, fail_on_error: bool, **options: object) -> None:
        if wait_before_seconds < 0:
            raise CommandError("--wait-before-seconds must be zero or greater.")

        if connections[database].vendor != "postgresql":
            raise ImproperlyConfigured("deferred_migrations only supports PostgreSQL.")

        with phase_command_in_charge():
            result = run_deferred_operations(using=database, wait_before_seconds=wait_before_seconds, dry_run=dry_run)

            for line in describe_run_result(result) or ["No deferred operations to run."]:
                self.stdout.write(line)

            if fail_on_error and result.failed is not None:
                raise CommandError(f"Deferred operation {result.failed.pk} failed.")
