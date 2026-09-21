from io import StringIO

import pytest
from django.core.management import CommandError
from django.core.management import call_command
from django.db import models

from deferred_migrations.operations import DeferredRenameModel
from tests.makemigrations_harness import GeneratedMigrations
from tests.makemigrations_harness import forget_test_models
from tests.makemigrations_harness import run_makemigrations
from tests.makemigrations_harness import widget_fields
from tests.migration_helpers import define_test_model
from tests.migration_helpers import relation_kind


@pytest.mark.django_db
def test_an_eligible_model_rename_is_written_deferred_with_the_rename_record_dependency(generated_app: GeneratedMigrations) -> None:
    define_test_model("Gizmo", widget_fields())

    output = run_makemigrations()

    written = generated_app.load(generated_app.names()[1])
    assert [type(operation) for operation in written.operations] == [DeferredRenameModel]
    assert ("deferred_migrations", "0002_modelrename") in written.dependencies
    assert "deferred_migrations.E" not in output


@pytest.mark.django_db
def test_update_refuses_an_eligible_model_rename(generated_app: GeneratedMigrations) -> None:
    define_test_model("Widget", widget_fields(extra=models.TextField(null=True)))
    run_makemigrations(interactive=False)
    forget_test_models()
    define_test_model("Gizmo", widget_fields(extra=models.TextField(null=True)))

    with pytest.raises(CommandError, match="normal makemigrations run"):
        run_makemigrations("--update")


@pytest.mark.django_db(transaction=True)
def test_a_generated_model_rename_serves_the_old_name_until_post_deploy(generated_app: GeneratedMigrations) -> None:
    call_command("migrate", "deferred_migrations_testapp", verbosity=0)
    define_test_model("Gizmo", widget_fields())
    run_makemigrations()

    call_command("migrate_pre_deploy", stdout=StringIO())

    assert relation_kind("deferred_migrations_testapp_widget") == "v"

    call_command("migrate_post_deploy", stdout=StringIO())

    assert relation_kind("deferred_migrations_testapp_widget") is None
    assert relation_kind("deferred_migrations_testapp_gizmo") == "r"
