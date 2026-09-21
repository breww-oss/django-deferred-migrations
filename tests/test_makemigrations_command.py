import pytest
from django.apps import apps
from django.core import checks
from django.core.checks.commands import migrate_and_makemigrations_autodetector
from django.core.management import CommandError
from django.core.management.commands import makemigrations as django_makemigrations
from django.db import migrations
from django.db import models
from django.db.migrations.graph import MigrationGraph
from django.db.migrations.migration import Migration
from django.db.migrations.operations import AddField
from django.db.migrations.operations import RemoveField
from django.db.migrations.questioner import NonInteractiveMigrationQuestioner
from django.db.migrations.state import ProjectState
from django_linear_migrations.management.commands import makemigrations as linear_makemigrations
from pytest_django import Settings

from deferred_migrations.checks import check_makemigrations_order
from deferred_migrations.checks import command_shadowing
from deferred_migrations.checks import underlying_command
from deferred_migrations.management.commands.makemigrations import Command
from deferred_migrations.management.commands.makemigrations import RenameCandidate
from deferred_migrations.management.commands.makemigrations import RenameRecordingQuestioner
from deferred_migrations.management.commands.makemigrations import RenameRefusingAutodetector
from tests.makemigrations_harness import GeneratedMigrations
from tests.makemigrations_harness import run_makemigrations
from tests.makemigrations_harness import widget_fields
from tests.migration_helpers import define_test_model


def test_the_command_builds_on_django_linear_migrations_when_it_is_installed_below() -> None:
    assert underlying_command("makemigrations", apps.get_app_configs()) is linear_makemigrations.Command
    assert Command.__mro__[1] is linear_makemigrations.Command


def test_without_another_makemigrations_below_the_command_builds_on_djangos() -> None:
    configs = [config for config in apps.get_app_configs() if config.name != "django_linear_migrations"]

    assert underlying_command("makemigrations", configs) is django_makemigrations.Command


def test_an_app_above_that_ships_makemigrations_is_named_by_w001() -> None:
    linear = apps.get_app_config("django_linear_migrations")
    ours = apps.get_app_config("deferred_migrations")

    assert command_shadowing("makemigrations", [linear, ours]) == ["django_linear_migrations"]
    assert command_shadowing("makemigrations", [ours, linear]) == []


def test_w001_warns_when_an_app_that_ships_makemigrations_sits_above(monkeypatch: pytest.MonkeyPatch) -> None:
    linear = apps.get_app_config("django_linear_migrations")
    ours = apps.get_app_config("deferred_migrations")
    monkeypatch.setattr("deferred_migrations.checks.apps.get_app_configs", lambda: [linear, ours])

    warnings = check_makemigrations_order()

    assert [(type(warning), warning.id) for warning in warnings] == [(checks.Warning, "deferred_migrations.W001")]
    assert "django_linear_migrations" in warnings[0].msg


def test_this_project_raises_no_w001() -> None:
    assert check_makemigrations_order() == []


def test_djangos_check_that_migrate_and_makemigrations_share_an_autodetector_passes() -> None:
    assert migrate_and_makemigrations_autodetector() == []


@pytest.mark.django_db
def test_a_non_interactive_run_refuses_every_detected_field_rename_at_once(generated_app: GeneratedMigrations) -> None:
    define_test_model("Widget", widget_fields(flag=None, enabled=models.BooleanField(default=False), note=None, memo=models.TextField(null=True)))

    with pytest.raises(CommandError) as raised:
        run_makemigrations(interactive=False)

    assert "deferred_migrations_testapp.Widget.flag looks like it was renamed to enabled." in str(raised.value)
    assert "deferred_migrations_testapp.Widget.note looks like it was renamed to memo." in str(raised.value)
    assert "two separate runs" in str(raised.value)
    assert generated_app.names() == ["0001_initial"]


@pytest.mark.django_db
def test_a_non_interactive_run_refuses_a_detected_model_rename(generated_app: GeneratedMigrations) -> None:
    define_test_model("Gizmo", widget_fields())

    with pytest.raises(CommandError, match="Widget looks like it was renamed to Gizmo"):
        run_makemigrations(interactive=False)

    assert generated_app.names() == ["0001_initial"]


def refusing_autodetector(*candidates: RenameCandidate) -> RenameRefusingAutodetector:
    autodetector = RenameRefusingAutodetector(ProjectState(), ProjectState(), NonInteractiveMigrationQuestioner())
    autodetector.questioner.candidates.extend(candidates)
    return autodetector


def test_a_declined_rename_in_an_app_that_is_not_being_written_is_not_refused() -> None:
    autodetector = refusing_autodetector(RenameCandidate("other_app", "other_app.Thing.old looks like it was renamed to new."))

    assert autodetector.changes(MigrationGraph(), trim_to_apps={"deferred_migrations_testapp"}) == {}


# Django's trim keeps the migrations of apps the requested ones depend on, so a named app can pull in another app's declined rename.
def test_a_declined_rename_is_refused_in_every_app_about_to_be_written() -> None:
    autodetector = refusing_autodetector(
        RenameCandidate("requested_app", "requested_app.Thing.old looks like it was renamed to new."), RenameCandidate("dependency_app", "dependency_app.Else.a looks like it was renamed to b."), RenameCandidate("untouched_app", "untouched_app.Other.x looks like it was renamed to y.")
    )
    changes = {"requested_app": [Migration("0002_x", "requested_app")], "dependency_app": [Migration("0005_y", "dependency_app")]}

    assert autodetector.refused_renames(changes) == ["requested_app.Thing.old looks like it was renamed to new.", "dependency_app.Else.a looks like it was renamed to b."]


@pytest.mark.parametrize("other_app_fields", [["flag", "enabled"], ["enabled"]])
def test_a_same_named_model_elsewhere_whose_fields_did_not_change_is_not_blamed(other_app_fields: list[str]) -> None:
    from_state, to_state = ProjectState(), ProjectState()
    migrations.CreateModel("Widget", [("id", models.BigAutoField(primary_key=True)), ("flag", models.BooleanField(default=False))]).state_forwards("renamed_app", from_state)
    migrations.CreateModel("Widget", [("id", models.BigAutoField(primary_key=True)), ("enabled", models.BooleanField(default=False))]).state_forwards("renamed_app", to_state)

    for state in (from_state, to_state):
        migrations.CreateModel("Widget", [("id", models.BigAutoField(primary_key=True)), *[(name, models.BooleanField(default=False)) for name in other_app_fields]]).state_forwards("other_app", state)

    questioner = RenameRecordingQuestioner(NonInteractiveMigrationQuestioner(), from_state, to_state)

    questioner.ask_rename("widget", "flag", "enabled", models.BooleanField(default=False))

    assert [candidate.app_label for candidate in questioner.candidates] == ["renamed_app"]


@pytest.mark.django_db
def test_an_interactive_no_is_left_alone(generated_app: GeneratedMigrations) -> None:
    define_test_model("Widget", widget_fields(flag=None, enabled=models.BooleanField(default=False)))

    output = run_makemigrations(answer="n")

    assert len(generated_app.names()) == 2
    assert "deferred_migrations.E014" in output


@pytest.mark.django_db
def test_the_opt_out_setting_restores_djangos_behaviour(generated_app: GeneratedMigrations, settings: Settings) -> None:
    settings.DEFERRED_MIGRATIONS_FIX_ON_MAKEMIGRATIONS = False
    define_test_model("Widget", widget_fields(flag=None, enabled=models.BooleanField(default=False)))

    run_makemigrations(interactive=False)

    written = generated_app.load(generated_app.names()[1])
    assert sorted(type(operation).__name__ for operation in written.operations) == sorted([AddField.__name__, RemoveField.__name__])
