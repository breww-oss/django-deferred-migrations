import importlib
import re
from io import StringIO
from pathlib import Path

import pytest
from django.apps import apps
from django.core.management import CommandError
from django.core.management import call_command
from django.db import ProgrammingError
from django.db import connection
from django.db import connections
from django.db.migrations.recorder import MigrationRecorder
from django.test import override_settings

from deferred_migrations.models import DeferredOperation
from deferred_migrations.runner import MigrationKeys
from tests.migration_helpers import column_names


@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("test_app")
def test_stacked_migrations_in_one_app_apply_in_a_single_pre_deploy_run_then_post_deploy_drops() -> None:
    call_command("migrate_pre_deploy", stdout=StringIO())

    assert {"legacy", "extra"} <= column_names("deferred_migrations_testapp_child")
    assert DeferredOperation.objects.get(app_label="deferred_migrations_testapp").status == DeferredOperation.Status.PENDING

    call_command("migrate_post_deploy", stdout=StringIO())

    assert "legacy" not in column_names("deferred_migrations_testapp_child")


@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("test_app")
def test_migration_keys_from_connection_know_applied_test_app_migrations() -> None:
    call_command("migrate", "deferred_migrations_testapp", "0002", verbosity=0)

    keys = MigrationKeys.from_connection(connection)

    assert keys.contains("deferred_migrations_testapp", "0002_remove_child_legacy")
    assert not keys.contains("deferred_migrations_testapp", "0003_child_extra")


@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("test_app")
def test_pre_deploy_deletes_only_unresolved_rows_from_unapplied_migrations() -> None:
    call_command("migrate", "deferred_migrations_testapp", "0002", verbosity=0)
    applied_row = DeferredOperation.objects.get(app_label="deferred_migrations_testapp", migration_name="0002_remove_child_legacy")
    stale_row = DeferredOperation.objects.create(app_label="deferred_migrations_testapp", migration_name="0003_child_extra", operation_index=0, sequence=0, kind=DeferredOperation.Kind.DROP_COLUMN, table_name="deferred_migrations_testapp_child", column_name="stale", sql="SELECT 'stale'")
    done_row = DeferredOperation.objects.create(
        app_label="deferred_migrations_testapp", migration_name="0003_child_extra", operation_index=0, sequence=1, kind=DeferredOperation.Kind.DROP_COLUMN, table_name="deferred_migrations_testapp_child", column_name="done", sql="SELECT 'done'", status=DeferredOperation.Status.DONE
    )

    call_command("migrate_pre_deploy", stdout=StringIO())

    assert DeferredOperation.objects.get(pk=applied_row.pk).status == DeferredOperation.Status.PENDING
    assert not DeferredOperation.objects.filter(pk=stale_row.pk).exists()
    assert DeferredOperation.objects.filter(pk=done_row.pk).exists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("test_app")
def test_pre_deploy_keeps_a_trigger_drop_from_a_migration_that_has_left_the_graph_but_deletes_a_column_drop() -> None:
    call_command("migrate", "deferred_migrations_testapp", "0002", verbosity=0)
    trigger_row = DeferredOperation.objects.create(app_label="deferred_migrations_testapp", migration_name="0099_pruned", operation_index=0, sequence=0, kind=DeferredOperation.Kind.DROP_TRIGGER, table_name="deferred_migrations_testapp_child", column_name="stale", sql="SELECT 'trigger'")
    column_row = DeferredOperation.objects.create(app_label="deferred_migrations_testapp", migration_name="0099_pruned", operation_index=0, sequence=1, kind=DeferredOperation.Kind.DROP_COLUMN, table_name="deferred_migrations_testapp_child", column_name="stale", sql="SELECT 'column'")

    call_command("migrate_pre_deploy", stdout=StringIO())

    assert DeferredOperation.objects.filter(pk=trigger_row.pk).exists()
    assert not DeferredOperation.objects.filter(pk=column_row.pk).exists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("test_app")
def test_pre_deploy_deletes_a_trigger_drop_from_an_unapplied_migration_that_is_still_in_the_graph() -> None:
    call_command("migrate", "deferred_migrations_testapp", "0002", verbosity=0)
    trigger_row = DeferredOperation.objects.create(app_label="deferred_migrations_testapp", migration_name="0003_child_extra", operation_index=0, sequence=0, kind=DeferredOperation.Kind.DROP_TRIGGER, table_name="deferred_migrations_testapp_child", column_name="stale", sql="SELECT 'trigger'")
    column_row = DeferredOperation.objects.create(app_label="deferred_migrations_testapp", migration_name="0003_child_extra", operation_index=0, sequence=1, kind=DeferredOperation.Kind.DROP_COLUMN, table_name="deferred_migrations_testapp_child", column_name="stale", sql="SELECT 'column'")

    call_command("migrate_pre_deploy", stdout=StringIO())

    assert not DeferredOperation.objects.filter(pk__in=[trigger_row.pk, column_row.pk]).exists()


# Raises SystemCheckError if django-linear-migrations' max_migration.txt check disagrees with the test app's graph.
@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("test_app")
def test_django_linear_migrations_checks_pass_alongside_deferred_operations() -> None:
    call_command("check", "--tag", "models", stdout=StringIO())


@pytest.mark.django_db(transaction=True)
def test_migrate_pre_deploy_bootstraps_its_own_table_before_a_first_deploy() -> None:
    migration_modules: dict[str, str | None] = {config.label: None for config in apps.get_app_configs()}
    migration_modules["deferred_migrations"] = "deferred_migrations.migrations"

    with override_settings(MIGRATION_MODULES=migration_modules):
        try:
            call_command("migrate", "deferred_migrations", "zero", verbosity=0)

            assert DeferredOperation._meta.db_table not in connection.introspection.table_names()

            call_command("migrate_pre_deploy", stdout=StringIO(), stderr=StringIO())

            assert DeferredOperation._meta.db_table in connection.introspection.table_names()
            assert DeferredOperation.objects.count() == 0
        finally:
            call_command("migrate", "deferred_migrations", verbosity=0)


@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("unsafe_test_app")
def test_pre_deploy_refuses_to_migrate_when_a_migration_is_not_deploy_safe() -> None:
    call_command("migrate", "deferred_migrations_testapp", "0002", verbosity=0)

    with pytest.raises(CommandError, match="deploy safety error"):
        call_command("migrate_pre_deploy", stdout=StringIO(), stderr=StringIO())

    assert "note" in column_names("deferred_migrations_testapp_child")
    assert not MigrationRecorder(connection).migration_qs.filter(app="deferred_migrations_testapp", name="0003_remove_child_note").exists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("readd_test_app")
def test_pre_deploy_names_the_queued_drop_when_a_migration_re_adds_its_column() -> None:
    call_command("migrate", "deferred_migrations_testapp", "0002", verbosity=0)
    queued = DeferredOperation.objects.get(app_label="deferred_migrations_testapp", migration_name="0002_remove_child_legacy")

    with pytest.raises(CommandError, match=re.escape(f"A queued drop still holds this name: {queued}.")):
        call_command("migrate_pre_deploy", stdout=StringIO(), stderr=StringIO())


@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("readd_test_app")
def test_pre_deploy_says_how_to_free_a_name_a_skipped_drop_holds() -> None:
    call_command("migrate", "deferred_migrations_testapp", "0002", verbosity=0)
    DeferredOperation.objects.filter(app_label="deferred_migrations_testapp").update(status=DeferredOperation.Status.SKIPPED)
    skipped = DeferredOperation.objects.get(app_label="deferred_migrations_testapp", migration_name="0002_remove_child_legacy")

    with pytest.raises(CommandError, match=re.escape(f"A skipped drop still holds this name: {skipped}.")):
        call_command("migrate_pre_deploy", stdout=StringIO(), stderr=StringIO())


@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("readd_test_app")
def test_pre_deploy_re_raises_a_duplicate_column_that_no_queued_drop_holds() -> None:
    call_command("migrate", "deferred_migrations_testapp", "0002", verbosity=0)
    DeferredOperation.objects.filter(app_label="deferred_migrations_testapp").update(status=DeferredOperation.Status.DONE)

    with pytest.raises(ProgrammingError, match="already exists"):
        call_command("migrate_pre_deploy", stdout=StringIO(), stderr=StringIO())


# The fixer must never edit a migration that has already run somewhere; its 0003 is a plain RemoveField it would otherwise rewrite.
@pytest.mark.parametrize(("applied_up_to", "rewritten"), [("0003", []), ("0002", ["0003_remove_child_note.py"])])
@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("unsafe_test_app")
def test_fix_deploy_safety_rewrites_only_migrations_not_yet_applied(monkeypatch: pytest.MonkeyPatch, applied_up_to: str, rewritten: list[str]) -> None:
    call_command("migrate", "deferred_migrations_testapp", applied_up_to, verbosity=0)
    written: list[Path] = []
    monkeypatch.setattr(Path, "write_text", lambda path, *args, **kwargs: written.append(path))

    call_command("fix_deploy_safety", "deferred_migrations_testapp", stdout=StringIO())

    assert [path.name for path in written] == rewritten


# What counts as applied comes from --database, so a project migrated on a second database is judged by that database's history, not default's.
@pytest.mark.parametrize(("database", "rewritten"), [("native", []), ("default", ["0003_remove_child_note.py"])])
@pytest.mark.django_db(transaction=True, databases=["default", "native"])
@pytest.mark.usefixtures("unsafe_test_app")
def test_fix_deploy_safety_reads_applied_migrations_from_the_database_it_is_given(monkeypatch: pytest.MonkeyPatch, database: str, rewritten: list[str]) -> None:
    call_command("migrate", "deferred_migrations_testapp", database="native", verbosity=0)
    written: list[Path] = []
    monkeypatch.setattr(Path, "write_text", lambda path, *args, **kwargs: written.append(path))

    try:
        call_command("fix_deploy_safety", "deferred_migrations_testapp", database=database, stdout=StringIO())
    finally:
        call_command("migrate", "deferred_migrations_testapp", "zero", database="native", verbosity=0)
        MigrationRecorder(connections["native"]).migration_qs.filter(app="deferred_migrations_testapp").delete()

    assert [path.name for path in written] == rewritten


# The rewriter can only add the queue's 0001 dependency, so a rename missing 0002 must be left for a person.
@pytest.mark.django_db
@pytest.mark.usefixtures("rename_test_app")
def test_fix_deploy_safety_leaves_a_missing_rename_record_dependency_for_a_person(monkeypatch: pytest.MonkeyPatch) -> None:
    rename = importlib.import_module("tests.testapp.rename_migrations.0002_rename_gadget_gizmo").Migration
    monkeypatch.setattr(rename, "dependencies", [("deferred_migrations_testapp", "0001_initial")])
    written: list[Path] = []
    monkeypatch.setattr(Path, "write_text", lambda path, *args, **kwargs: written.append(path))
    stdout = StringIO()

    call_command("fix_deploy_safety", "deferred_migrations_testapp", stdout=stdout)

    assert written == []
    assert "Needs a decision: deferred_migrations_testapp.0002_rename_gadget_gizmo deferred_migrations.E008" in stdout.getvalue()


@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("test_app")
def test_pre_deploy_keeps_a_view_drop_from_a_migration_that_has_left_the_graph() -> None:
    call_command("migrate", "deferred_migrations_testapp", "0002", verbosity=0)
    pruned = DeferredOperation.objects.create(app_label="deferred_migrations_testapp", migration_name="0099_pruned", operation_index=0, sequence=0, kind=DeferredOperation.Kind.DROP_VIEW, table_name="deferred_migrations_testapp_child", column_name="old_view", sql="SELECT 'pruned'")
    rerunnable = DeferredOperation.objects.create(app_label="deferred_migrations_testapp", migration_name="0003_child_extra", operation_index=0, sequence=0, kind=DeferredOperation.Kind.DROP_VIEW, table_name="deferred_migrations_testapp_child", column_name="old_view", sql="SELECT 'rerun'")

    call_command("migrate_pre_deploy", stdout=StringIO())

    assert DeferredOperation.objects.filter(pk=pruned.pk).exists()
    assert not DeferredOperation.objects.filter(pk=rerunnable.pk).exists()
