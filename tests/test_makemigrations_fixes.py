from io import StringIO

import pytest
from django.core.management import call_command
from django.db import connection
from django.db import models
from django.db.migrations.operations import RemoveField
from pytest_django import Settings

from deferred_migrations.autofix import AutoFixer
from deferred_migrations.operations import DeferredDeleteModel
from deferred_migrations.operations import DeferredRemoveField
from tests.makemigrations_harness import GeneratedMigrations
from tests.makemigrations_harness import forget_test_models
from tests.makemigrations_harness import run_makemigrations
from tests.makemigrations_harness import widget_fields
from tests.migration_helpers import column_names
from tests.migration_helpers import define_test_model
from tests.safety_graph import make_migration


@pytest.mark.django_db
def test_a_removal_is_written_deferred_with_the_queue_dependency(generated_app: GeneratedMigrations) -> None:
    define_test_model("Widget", widget_fields(note=None))

    output = run_makemigrations(interactive=False)

    written = generated_app.load(generated_app.names()[1])
    assert [type(operation) for operation in written.operations] == [DeferredRemoveField]
    assert ("deferred_migrations", "0001_initial") in written.dependencies
    assert "1 migration made deploy-safe; 0 findings need a manual fix" in output


@pytest.mark.django_db
def test_a_model_deletion_is_written_deferred(generated_app: GeneratedMigrations) -> None:
    run_makemigrations(interactive=False)

    written = generated_app.load(generated_app.names()[1])
    assert [type(operation) for operation in written.operations] == [DeferredDeleteModel]


@pytest.mark.django_db
def test_a_dry_run_writes_nothing_and_says_what_it_would_change(generated_app: GeneratedMigrations) -> None:
    define_test_model("Widget", widget_fields(note=None))

    output = run_makemigrations("--dry-run", interactive=False)

    assert generated_app.names() == ["0001_initial"]
    assert "Would make deploy-safe" in output
    assert "1 migration would be made deploy-safe; 0 findings need a manual fix" in output


@pytest.mark.django_db
def test_verbosity_zero_prints_no_fix_report(generated_app: GeneratedMigrations) -> None:
    define_test_model("Widget", widget_fields(note=None))

    output = run_makemigrations("--verbosity", "0", interactive=False)

    assert [type(operation) for operation in generated_app.load(generated_app.names()[1]).operations] == [DeferredRemoveField]
    assert "deploy-safe" not in output


@pytest.mark.django_db
def test_check_exits_without_writing(generated_app: GeneratedMigrations) -> None:
    define_test_model("Widget", widget_fields(note=None))

    with pytest.raises(SystemExit):
        run_makemigrations("--check", interactive=False)

    assert generated_app.names() == ["0001_initial"]


@pytest.mark.django_db
def test_a_finding_it_cannot_fix_is_reported_with_the_summary(generated_app: GeneratedMigrations) -> None:
    define_test_model("Widget", widget_fields(name=models.CharField(max_length=50, unique=True)))

    output = run_makemigrations(interactive=False)

    assert "deferred_migrations.E102" in output
    assert "0 migrations made deploy-safe; 1 finding needs a manual fix (see the fixing-deploy-safety skill)" in output


@pytest.mark.django_db
def test_update_rewrites_the_leaf_in_place_with_deferred_operations(generated_app: GeneratedMigrations) -> None:
    define_test_model("Widget", widget_fields(extra=models.TextField(null=True)))
    run_makemigrations(interactive=False)
    forget_test_models()
    define_test_model("Widget", widget_fields(extra=models.TextField(null=True), note=None))

    run_makemigrations("--update", interactive=False)

    assert len(generated_app.names()) == 2
    written = generated_app.load(generated_app.names()[1])
    assert DeferredRemoveField in [type(operation) for operation in written.operations]


@pytest.mark.django_db
def test_update_with_a_new_name_replaces_the_leaf(generated_app: GeneratedMigrations) -> None:
    define_test_model("Widget", widget_fields(extra=models.TextField(null=True)))
    run_makemigrations(interactive=False)
    forget_test_models()
    define_test_model("Widget", widget_fields(extra=models.TextField(null=True), note=None))

    run_makemigrations("--update", "--name", "trimmed", interactive=False)

    names = generated_app.names()
    assert len(names) == 2
    assert names[1].endswith("_trimmed")
    assert DeferredRemoveField in [type(operation) for operation in generated_app.load(names[1]).operations]


@pytest.mark.django_db(transaction=True)
def test_a_generated_removal_keeps_the_column_until_post_deploy(generated_app: GeneratedMigrations) -> None:
    call_command("migrate", "deferred_migrations_testapp", verbosity=0)
    define_test_model("Widget", widget_fields(note=None))
    run_makemigrations(interactive=False)

    call_command("migrate_pre_deploy", stdout=StringIO())

    assert "note" in column_names("deferred_migrations_testapp_widget")

    call_command("migrate_post_deploy", stdout=StringIO())

    assert "note" not in column_names("deferred_migrations_testapp_widget")


# check_deploy_safety skips a third-party app with no baseline, so the fixer must leave its migrations as Django wrote them.
@pytest.mark.django_db
def test_a_third_party_apps_migration_is_written_untouched(generated_app: GeneratedMigrations, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("deferred_migrations.autofix.is_first_party", lambda app_config: app_config.label != "deferred_migrations_testapp")
    monkeypatch.setattr("deferred_migrations.autofix.read_baseline", lambda app_label: None)
    define_test_model("Widget", widget_fields(note=None))

    output = run_makemigrations(interactive=False)

    written = generated_app.load(generated_app.names()[1])
    assert [type(operation) for operation in written.operations] == [RemoveField]
    assert ("deferred_migrations", "0001_initial") not in written.dependencies
    assert "deploy-safe" not in output


# live_test_app disables every other app's migrations module but keeps the package's own 0001 and 0002, so the new node has history to apply onto.
@pytest.mark.django_db
@pytest.mark.usefixtures("live_test_app")
def test_the_package_own_migrations_are_never_transformed() -> None:
    migration = make_migration("deferred_migrations", "0003_trim", [RemoveField("modelrename", "rewritten_operation_ids")], [("deferred_migrations", "0002_modelrename")])

    AutoFixer({"deferred_migrations": [migration]}, connection).run()

    assert type(migration.operations[0]) is RemoveField


# The autodetector writes a dependency on the user model as ("__setting__", "AUTH_USER_MODEL"), which names no graph node until it is resolved.
@pytest.mark.usefixtures("live_test_app")
def test_a_swappable_dependency_is_resolved_into_the_graph(settings: Settings) -> None:
    settings.DEFERRED_MIGRATIONS_TEST_SWAPPED_MODEL = "deferred_migrations.ModelRename"
    migration = make_migration("deferred_migrations_testapp", "0099_swapped", [], [("__setting__", "DEFERRED_MIGRATIONS_TEST_SWAPPED_MODEL")])

    fixer = AutoFixer({"deferred_migrations_testapp": [migration]}, connection)

    assert ("deferred_migrations", "0001_initial") in {parent.key for parent in fixer.graph.node_map[("deferred_migrations_testapp", "0099_swapped")].parents}
