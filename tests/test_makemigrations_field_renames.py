from io import StringIO

import pytest
from django.core.management import CommandError
from django.core.management import call_command
from django.db import connection
from django.db import models
from django.db.migrations.operations import AddField
from django.db.migrations.operations import RenameField
from pytest_django import Settings

from deferred_migrations.models import DeferredOperation
from deferred_migrations.operations import AddFieldConcurrently
from deferred_migrations.operations import AddIndexConcurrently
from deferred_migrations.operations import BackfillColumnSync
from deferred_migrations.operations import DeferredRemoveField
from deferred_migrations.operations import InstallColumnSync
from deferred_migrations.operations import SetNotNull
from tests.makemigrations_harness import GeneratedMigrations
from tests.makemigrations_harness import forget_test_models
from tests.makemigrations_harness import run_makemigrations
from tests.makemigrations_harness import widget_fields
from tests.migration_helpers import column_names
from tests.migration_helpers import define_test_model
from tests.migration_helpers import nullability_and_default


def operation_types(generated_app: GeneratedMigrations, name: str) -> list[type]:
    return [type(operation) for operation in generated_app.load(name).operations]


@pytest.mark.django_db
def test_a_not_null_field_with_a_constant_default_becomes_synced_columns_and_a_follow_up(generated_app: GeneratedMigrations) -> None:
    define_test_model("Widget", widget_fields(flag=None, enabled=models.BooleanField(default=False)))

    output = run_makemigrations()

    names = generated_app.names()
    assert names[2] == "0003_rename_widget_flag_to_enabled_backfill"
    assert operation_types(generated_app, names[1]) == [AddField, InstallColumnSync]
    assert generated_app.load(names[1]).operations[0].preserve_default
    follow_up = generated_app.load(names[2])
    assert (follow_up.atomic, operation_types(generated_app, names[2])) == (False, [BackfillColumnSync, DeferredRemoveField])
    assert follow_up.operations[1].renamed_to == "enabled"
    assert generated_app.max_migration() == names[2]
    assert "deferred_migrations.E" not in output


@pytest.mark.django_db
def test_a_not_null_field_without_a_default_is_added_nullable_and_tightened_after_the_backfill(generated_app: GeneratedMigrations) -> None:
    define_test_model("Widget", widget_fields(count=None, total=models.IntegerField()))

    output = run_makemigrations()

    names = generated_app.names()
    assert generated_app.load(names[1]).operations[0].field.null
    assert operation_types(generated_app, names[2]) == [BackfillColumnSync, SetNotNull, DeferredRemoveField]
    assert "deferred_migrations.E" not in output


@pytest.mark.django_db
def test_a_nullable_field_needs_no_set_not_null(generated_app: GeneratedMigrations) -> None:
    define_test_model("Widget", widget_fields(note=None, memo=models.TextField(null=True)))

    run_makemigrations()

    assert operation_types(generated_app, generated_app.names()[2]) == [BackfillColumnSync, DeferredRemoveField]


@pytest.mark.django_db
def test_two_renames_in_one_app_share_one_follow_up_named_last(generated_app: GeneratedMigrations) -> None:
    define_test_model("Widget", widget_fields(flag=None, enabled=models.BooleanField(default=False), note=None, memo=models.TextField(null=True)))

    run_makemigrations()

    names = generated_app.names()
    assert names[-1] == "0003_backfill_renamed_columns"
    assert generated_app.max_migration() == names[-1]
    assert operation_types(generated_app, names[-1]).count(DeferredRemoveField) == 2


@pytest.mark.django_db
def test_an_indexed_field_rename_stays_a_rename_field_and_says_why(generated_app: GeneratedMigrations) -> None:
    define_test_model("Widget", widget_fields(code=None, ref=models.CharField(max_length=20, db_index=True)))

    output = run_makemigrations()

    assert len(generated_app.names()) == 2
    assert RenameField in operation_types(generated_app, generated_app.names()[1])
    assert "was left as a RenameField" in output
    assert "deferred_migrations.E005" in output


@pytest.mark.django_db
def test_a_rename_alongside_an_unrelated_index_is_still_expanded(generated_app: GeneratedMigrations) -> None:
    define_test_model("Widget", widget_fields(flag=None, enabled=models.BooleanField(default=False)), {"indexes": [models.Index(fields=["name"], name="widget_name_idx")]})

    run_makemigrations()

    names = generated_app.names()
    assert names[-1] == "0003_rename_widget_flag_to_enabled_backfill"
    assert RenameField not in operation_types(generated_app, names[1])
    assert InstallColumnSync in operation_types(generated_app, names[1])
    assert AddIndexConcurrently in operation_types(generated_app, names[-1])


@pytest.mark.django_db
def test_an_indexed_field_added_alongside_a_rename_is_built_in_the_rename_follow_up(generated_app: GeneratedMigrations) -> None:
    define_test_model("Widget", widget_fields(flag=None, enabled=models.BooleanField(default=False), sku=models.CharField(max_length=10, null=True, db_index=True)))

    run_makemigrations()

    names = generated_app.names()
    assert len(names) == 3
    assert names[-1] == "0003_rename_widget_flag_to_enabled_backfill"
    assert AddFieldConcurrently in operation_types(generated_app, names[-1])
    assert AddFieldConcurrently not in operation_types(generated_app, names[1])


@pytest.mark.django_db
def test_a_rename_to_a_field_a_new_meta_index_references_stays_a_rename_field(generated_app: GeneratedMigrations) -> None:
    define_test_model("Widget", widget_fields(name=None, title=models.CharField(max_length=50)), {"indexes": [models.Index(fields=["title"], name="widget_title_idx")]})

    output = run_makemigrations()

    assert len(generated_app.names()) == 3
    assert RenameField in operation_types(generated_app, generated_app.names()[1])
    assert operation_types(generated_app, generated_app.names()[2]) == [AddIndexConcurrently]
    assert "is referenced by Meta indexes, constraints or together options" in output


@pytest.mark.django_db
def test_findings_for_unwritten_migrations_match_check_deploy_safety_once_written(generated_app: GeneratedMigrations) -> None:
    define_test_model("Widget", widget_fields(code=None, ref=models.CharField(max_length=20, db_index=True)))

    output = run_makemigrations()
    stderr = StringIO()

    with pytest.raises(CommandError):
        call_command("check_deploy_safety", stdout=StringIO(), stderr=stderr)

    reported = {line for line in output.splitlines() if "deferred_migrations.E" in line}
    assert any("deferred_migrations.E005" in line for line in reported)
    assert reported == set(stderr.getvalue().splitlines())


@pytest.mark.django_db
def test_update_refuses_an_eligible_rename(generated_app: GeneratedMigrations) -> None:
    define_test_model("Widget", widget_fields(extra=models.TextField(null=True)))
    run_makemigrations(interactive=False)
    forget_test_models()
    define_test_model("Widget", widget_fields(extra=models.TextField(null=True), flag=None, enabled=models.BooleanField(default=False)))

    with pytest.raises(CommandError, match="normal makemigrations run"):
        run_makemigrations("--update")


@pytest.mark.django_db(transaction=True)
def test_both_columns_stay_equal_through_the_overlap_and_the_old_one_goes_after(generated_app: GeneratedMigrations, settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TTY_INTERACTIVE", "0")
    settings.DEFERRED_MIGRATIONS_BACKFILL_PAUSE_SECONDS = 0
    call_command("migrate", "deferred_migrations_testapp", verbosity=0)

    with connection.cursor() as cursor:
        cursor.execute("INSERT INTO deferred_migrations_testapp_widget (name, flag, code, count) SELECT 'w', n % 2 = 0, 'c', n FROM generate_series(1, 50) n")

    define_test_model("Widget", widget_fields(flag=None, enabled=models.BooleanField(default=False)))
    run_makemigrations()
    stdout = StringIO()

    call_command("migrate_pre_deploy", stdout=stdout)

    assert "backfill deferred_migrations_testapp_widget.enabled: " in stdout.getvalue()

    with connection.cursor() as cursor:
        cursor.execute("INSERT INTO deferred_migrations_testapp_widget (name, flag, code, count) VALUES ('old', true, 'c', 1)")
        cursor.execute("INSERT INTO deferred_migrations_testapp_widget (name, enabled, code, count) VALUES ('new', true, 'c', 1)")
        cursor.execute("SELECT count(*) FROM deferred_migrations_testapp_widget WHERE flag IS DISTINCT FROM enabled")
        assert cursor.fetchone()[0] == 0

    queued = list(DeferredOperation.objects.filter(app_label="deferred_migrations_testapp"))
    assert sorted(row.kind for row in queued) == sorted([DeferredOperation.Kind.DROP_COLUMN, DeferredOperation.Kind.DROP_TRIGGER])

    for row in queued:
        queued_by = generated_app.load(row.migration_name).operations[row.operation_index]
        assert isinstance(queued_by, InstallColumnSync if row.kind == DeferredOperation.Kind.DROP_TRIGGER else DeferredRemoveField)

    call_command("migrate_post_deploy", stdout=StringIO())

    assert "flag" not in column_names("deferred_migrations_testapp_widget")


@pytest.mark.django_db(transaction=True)
def test_rolling_back_after_post_deploy_keeps_values_written_under_the_new_name(generated_app: GeneratedMigrations, settings: Settings) -> None:
    settings.DEFERRED_MIGRATIONS_BACKFILL_PAUSE_SECONDS = 0
    call_command("migrate", "deferred_migrations_testapp", verbosity=0)
    define_test_model("Widget", widget_fields(count=None, total=models.IntegerField()))
    run_makemigrations()
    call_command("migrate_pre_deploy", stdout=StringIO())
    call_command("migrate_post_deploy", stdout=StringIO())

    with connection.cursor() as cursor:
        cursor.execute("INSERT INTO deferred_migrations_testapp_widget (name, flag, code, total) VALUES ('new', false, 'c', 7) RETURNING id")
        widget_id = cursor.fetchone()[0]

    call_command("migrate", "deferred_migrations_testapp", "0001", verbosity=0)

    with connection.cursor() as cursor:
        cursor.execute("SELECT count FROM deferred_migrations_testapp_widget WHERE id = %s", [widget_id])
        assert cursor.fetchone()[0] == 7

    assert nullability_and_default("deferred_migrations_testapp_widget", "count") == ("NO", None)
