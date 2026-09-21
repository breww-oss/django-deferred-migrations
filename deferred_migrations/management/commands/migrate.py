from functools import cache

from django.apps import apps
from django.core.management import CommandError
from django.core.management.base import BaseCommand

from deferred_migrations.checks import allow_command_line_migrate
from deferred_migrations.checks import underlying_command


@cache
def guarded_migrate(underlying: type[BaseCommand]) -> type[BaseCommand]:
    class Command(underlying):
        # Only a person typing migrate is stopped: Django's test database setup, flush and third-party tools call it from code and keep working.
        def handle(self, *args: str, **options: object) -> str | None:
            if self._called_from_command_line and not allow_command_line_migrate() and not self.only_reads_or_records(options):
                raise CommandError(
                    "migrate cannot tell whether old code is still running against this database, so it is disabled at the command line. "
                    'In a deploy, run "python manage.py migrate_pre_deploy" before the new code rolls out and "python manage.py migrate_post_deploy" after it. '
                    'For local development, run "python manage.py migrate_full". To allow migrate here anyway, set DEFERRED_MIGRATIONS_ALLOW_COMMAND_LINE_MIGRATE = True.'
                )

            return super().handle(*args, **options)

        # --fake and --prune change only migration records, never the schema; --fake-initial can apply migrations for real, so it is not among them.
        @staticmethod
        def only_reads_or_records(options: dict[str, object]) -> bool:
            return any(options[name] for name in ("plan", "check_unapplied", "fake", "prune"))

    return Command


# Resolved on every load rather than at import, because the command below can be replaced later: pytest-django swaps in a silent migrate for its --no-migrations test databases.
def __getattr__(name: str) -> type[BaseCommand]:
    if name == "Command":
        return guarded_migrate(underlying_command("migrate", apps.get_app_configs()))

    raise AttributeError(name)
