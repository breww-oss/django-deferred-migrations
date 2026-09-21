import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import pytest
from django.apps import AppConfig
from django.apps import apps
from django.contrib.postgres.functions import RandomUUID
from django.core.management import call_command
from django.db import connection
from django.db import migrations
from django.db import models
from django.db.migrations.recorder import MigrationRecorder
from django.db.migrations.state import ProjectState
from django.test import override_settings

from tests.makemigrations_harness import WIDGET_INITIAL
from tests.makemigrations_harness import GeneratedMigrations
from tests.migration_helpers import apply_operations


@pytest.fixture
def shop_state() -> ProjectState:
    return apply_operations(
        "dm_shop",
        ProjectState(),
        [
            migrations.CreateModel("Customer", [("id", models.BigAutoField(primary_key=True)), ("name", models.CharField(max_length=50))]),
            migrations.CreateModel(
                "Order",
                [
                    ("id", models.BigAutoField(primary_key=True)),
                    ("customer", models.ForeignKey("dm_shop.Customer", on_delete=models.CASCADE)),
                    ("reference", models.CharField(max_length=20)),
                    ("status", models.CharField(max_length=20, default="new")),
                    ("code", models.CharField(max_length=20, unique=True, default="x")),
                    ("token", models.UUIDField(db_default=RandomUUID())),
                    ("amount", models.DecimalField(max_digits=12, decimal_places=2, default=0)),
                    ("amount_pence", models.BigIntegerField(null=True)),
                    ("note", models.TextField(null=True)),
                    ("created", models.DateTimeField(auto_now_add=True)),
                    ("tags", models.ManyToManyField("dm_shop.Customer", related_name="tagged_orders")),
                ],
            ),
        ],
    )


# Every other app is unmigrated here, so migrate only touches the test app.
# deferred_migrations keeps its real migrations: the test app depends on 0001 by name, which Django does not ignore for unmigrated apps.
@contextmanager
def isolated_test_app_settings(test_app_migrations: str) -> Iterator[None]:
    migration_modules: dict[str, str | None] = {config.label: None for config in apps.get_app_configs()}
    migration_modules["deferred_migrations"] = "deferred_migrations.migrations"
    migration_modules["deferred_migrations_testapp"] = test_app_migrations
    # Registered here rather than in INSTALLED_APPS because the finally block below pops it: a static entry would be removed by the first test and never restored.
    app_config = AppConfig.create("tests.testapp.apps.TestAppConfig")
    app_config.apps = apps
    # Models a test defines register into this dict, which get_models() then reads.
    app_config.models = apps.all_models[app_config.label]

    try:
        apps.app_configs[app_config.label] = app_config
        apps.clear_cache()

        with override_settings(MIGRATION_MODULES=migration_modules):
            yield
    finally:
        apps.app_configs.pop(app_config.label, None)
        apps.all_models.pop(app_config.label, None)
        apps.clear_cache()


@pytest.fixture
def live_test_app() -> Iterator[None]:
    with isolated_test_app_settings("tests.testapp.migrations"):
        yield


@contextmanager
def migratable_test_app(test_app_migrations: str) -> Iterator[None]:
    with isolated_test_app_settings(test_app_migrations):
        try:
            yield
        finally:
            try:
                call_command("migrate", "deferred_migrations_testapp", "zero", verbosity=0)
            finally:
                MigrationRecorder(connection).migration_qs.filter(app="deferred_migrations_testapp").delete()


@pytest.fixture
def test_app() -> Iterator[None]:
    with migratable_test_app("tests.testapp.migrations"):
        yield


@pytest.fixture
def nonatomic_test_app() -> Iterator[None]:
    with migratable_test_app("tests.testapp.nonatomic_migrations"):
        yield


# 0003 adds back the column 0002 queued a drop for, so migrating it collides with the column still waiting to be dropped.
@pytest.fixture
def readd_test_app() -> Iterator[None]:
    with migratable_test_app("tests.testapp.readd_migrations"):
        yield


# 0003 is a plain RemoveField, so check_deploy_safety reports E001 for this app.
@pytest.fixture
def unsafe_test_app() -> Iterator[None]:
    with migratable_test_app("tests.testapp.unsafe_migrations"):
        yield


# The state a released squash leaves behind: django_migrations still names the migrations it replaced, while only the squash is on disk.
@pytest.fixture
def squashed_test_app() -> Iterator[None]:
    with isolated_test_app_settings("tests.testapp.squashed_migrations"):
        recorder = MigrationRecorder(connection)
        recorder.record_applied("deferred_migrations_testapp", "0001_initial")
        recorder.record_applied("deferred_migrations_testapp", "0002_remove_child_legacy")

        try:
            yield
        finally:
            recorder.migration_qs.filter(app="deferred_migrations_testapp").delete()


@pytest.fixture
def rename_test_app() -> Iterator[None]:
    with migratable_test_app("tests.testapp.rename_migrations"):
        yield


# 0003 creates a model under the name 0002's compatibility view still holds.
@pytest.fixture
def rename_reuse_test_app() -> Iterator[None]:
    with migratable_test_app("tests.testapp.rename_reuse_migrations"):
        yield


# A plain RenameModel in history: on a fresh database Django's injected RenameContentType looks up a ContentType that does not exist yet.
@pytest.fixture
def plain_rename_test_app() -> Iterator[None]:
    with migratable_test_app("tests.testapp.plain_rename_migrations"):
        yield


# A migrations package in a temporary directory, seeded with a Widget model, that makemigrations writes into.
@pytest.fixture
def generated_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[GeneratedMigrations]:
    package = f"dm_generated_{uuid4().hex[:8]}"
    directory = tmp_path / package / "migrations"
    directory.mkdir(parents=True)
    (tmp_path / package / "__init__.py").write_text("")
    (directory / "__init__.py").write_text("")
    (directory / "0001_initial.py").write_text(WIDGET_INITIAL)
    (directory / "max_migration.txt").write_text("0001_initial\n")
    monkeypatch.syspath_prepend(str(tmp_path))

    try:
        with migratable_test_app(f"{package}.migrations"):
            yield GeneratedMigrations(f"{package}.migrations", directory)
    finally:
        for name in [name for name in sys.modules if name == package or name.startswith(f"{package}.")]:
            del sys.modules[name]


# Tests that run CONCURRENTLY commit their DDL, so the tables they create outlive the test unless dropped.
@pytest.fixture
def scratch_tables() -> Iterator[list[str]]:
    tables: list[str] = []

    yield tables

    with connection.cursor() as cursor:
        for table in reversed(tables):
            cursor.execute(f"DROP TABLE IF EXISTS {connection.ops.quote_name(table)} CASCADE")
