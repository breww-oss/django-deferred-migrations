import sys
from argparse import ArgumentParser

from django.core.management import BaseCommand
from django.core.management import CommandError
from django.core.management import call_command
from django.db import DEFAULT_DB_ALIAS

from deferred_migrations.runner import describe_run_result
from deferred_migrations.runner import run_deferred_operations


class Command(BaseCommand):
    help = "Run migrate_pre_deploy then migrate_post_deploy straight away, for local development. Never use it in a deploy: the drops would run while old code is still serving."
    requires_system_checks = []

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument("--database", default=DEFAULT_DB_ALIAS)
        parser.add_argument("--prompt-before-post", action="store_true", help="Apply migrations, then ask before running the queued post-deploy operations, so the between-phases state can be tried out first.")
        parser.add_argument("--fail-on-error", action="store_true")

    def handle(self, *args: str, database: str, prompt_before_post: bool, fail_on_error: bool, **options: object) -> None:
        if prompt_before_post and not sys.stdin.isatty():
            raise CommandError("--prompt-before-post needs an interactive terminal.")

        call_command("migrate_pre_deploy", database=database, verbosity=options["verbosity"], stdout=self.stdout, stderr=self.stderr)

        if prompt_before_post and not self.confirm_post_deploy(database):
            return

        call_command("migrate_post_deploy", database=database, fail_on_error=fail_on_error, verbosity=options["verbosity"], stdout=self.stdout, stderr=self.stderr)

    def confirm_post_deploy(self, database: str) -> bool:
        result = run_deferred_operations(using=database, dry_run=True)
        lines = describe_run_result(result)

        for line in lines:
            self.stdout.write(line)

        # Rows can be queued yet unable to run (blocked, skipped, unknown, or another run holds the lock), and that is exactly when "nothing queued" would mislead.
        if not result.would_run:
            self.stdout.write("No post-deploy operations can run now." if lines else "No post-deploy operations are queued.")
            return False

        if input("Run post-deploy operations now? [y/N]: ").strip().lower() in ("y", "yes"):
            return True

        self.stdout.write('Post-deploy operations left pending. Run "python manage.py migrate_post_deploy" when ready.')
        return False
