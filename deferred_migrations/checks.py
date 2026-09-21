import os
from collections.abc import Iterable
from importlib import import_module

from django.apps import AppConfig
from django.apps import apps
from django.conf import settings
from django.core import checks
from django.core.management import find_commands
from django.core.management.base import BaseCommand

PACKAGE = "deferred_migrations"


def fix_on_makemigrations() -> bool:
    return getattr(settings, "DEFERRED_MIGRATIONS_FIX_ON_MAKEMIGRATIONS", True)


def allow_command_line_migrate() -> bool:
    return getattr(settings, "DEFERRED_MIGRATIONS_ALLOW_COMMAND_LINE_MIGRATE", False)


def ships_command(app_config: AppConfig, name: str) -> bool:
    return name in find_commands(os.path.join(app_config.path, "management"))


# Django takes a command from the earliest app in INSTALLED_APPS that ships it, so ours builds on the first one below us.
def underlying_command(name: str, app_configs: Iterable[AppConfig]) -> type[BaseCommand]:
    below_us = False

    for app_config in app_configs:
        if app_config.name == PACKAGE:
            below_us = True
        elif below_us and ships_command(app_config, name):
            return import_module(f"{app_config.name}.management.commands.{name}").Command

    # Looked up through the module each time, so a replacement such as pytest-django's silent migrate is picked up.
    return import_module(f"django.core.management.commands.{name}").Command


def command_shadowing(name: str, app_configs: Iterable[AppConfig]) -> list[str]:
    shadowing: list[str] = []

    for app_config in app_configs:
        if app_config.name == PACKAGE:
            return shadowing

        if ships_command(app_config, name):
            shadowing.append(app_config.name)

    return shadowing


def check_makemigrations_order(app_configs: Iterable[AppConfig] | None = None, **kwargs: object) -> list[checks.CheckMessage]:  # noqa: ARG001
    if not fix_on_makemigrations():
        return []

    return [
        checks.Warning(
            f"{name} ships its own makemigrations and is above deferred_migrations in INSTALLED_APPS, so deferred_migrations' makemigrations never runs and new migrations are not made deploy-safe automatically.",
            hint="Move deferred_migrations above it in INSTALLED_APPS.",
            id="deferred_migrations.W001",
        )
        for name in command_shadowing("makemigrations", apps.get_app_configs())
    ]


def check_migrate_order(app_configs: Iterable[AppConfig] | None = None, **kwargs: object) -> list[checks.CheckMessage]:  # noqa: ARG001
    if allow_command_line_migrate():
        return []

    return [
        checks.Warning(
            f"{name} ships its own migrate and is above deferred_migrations in INSTALLED_APPS, so a migrate typed at the command line is not stopped and can leave destructive operations queued.",
            hint="Move deferred_migrations above it in INSTALLED_APPS.",
            id="deferred_migrations.W002",
        )
        for name in command_shadowing("migrate", apps.get_app_configs())
    ]
