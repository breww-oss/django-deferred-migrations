from io import StringIO

import pytest
from django.apps import apps
from django.core.management import call_command
from django.core.management.commands import migrate as migrate_command
from django.db import connection
from django.test import override_settings

from deferred_migrations.models import DeferredOperation


@pytest.fixture
def verbose_migrate(monkeypatch: pytest.MonkeyPatch) -> None:
    # Some pytest-django configurations swap in a migrate command that forces verbosity=0, which would silently turn the "says nothing" tests below into assertions about empty output. Pin Django's own command so they test what they claim.
    real = next((base for base in migrate_command.Command.__mro__ if base.__module__ == "django.core.management.commands.migrate"), None)
    assert real is not None, "Could not find Django's real migrate command; the notice tests would pass vacuously."
    monkeypatch.setattr(migrate_command, "Command", real)


@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("test_app", "verbose_migrate")
def test_migrate_reports_the_single_operation_it_left_queued_once() -> None:
    out = StringIO()

    call_command("migrate", "deferred_migrations_testapp", stdout=out)

    assert '1 deferred operation is queued: the column or table it drops still exists. Run "python manage.py migrate_post_deploy" to complete it.' in out.getvalue()
    assert out.getvalue().count("deferred operation is queued") == 1


@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("test_app", "verbose_migrate")
def test_migrate_counts_only_the_operations_still_waiting() -> None:
    call_command("migrate", "deferred_migrations_testapp", verbosity=0)
    DeferredOperation.objects.create(
        app_label="deferred_migrations_testapp", migration_name="0002_remove_child_legacy", operation_index=1, sequence=0, kind=DeferredOperation.Kind.DROP_COLUMN, table_name="deferred_migrations_testapp_child", column_name="failed", sql="SELECT 'failed'", status=DeferredOperation.Status.FAILED
    )
    DeferredOperation.objects.create(
        app_label="deferred_migrations_testapp", migration_name="0002_remove_child_legacy", operation_index=2, sequence=0, kind=DeferredOperation.Kind.DROP_COLUMN, table_name="deferred_migrations_testapp_child", column_name="done", sql="SELECT 'done'", status=DeferredOperation.Status.DONE
    )
    DeferredOperation.objects.create(
        app_label="deferred_migrations_testapp", migration_name="0002_remove_child_legacy", operation_index=3, sequence=0, kind=DeferredOperation.Kind.DROP_COLUMN, table_name="deferred_migrations_testapp_child", column_name="skipped", sql="SELECT 'skipped'", status=DeferredOperation.Status.SKIPPED
    )
    out = StringIO()

    call_command("migrate", "deferred_migrations_testapp", stdout=out)

    assert '2 deferred operations are queued: the columns and tables they drop still exist. Run "python manage.py migrate_post_deploy" to complete them.' in out.getvalue()


@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("test_app", "verbose_migrate")
def test_migrate_says_nothing_when_the_queue_is_empty() -> None:
    out = StringIO()

    call_command("migrate", "deferred_migrations_testapp", "0001", stdout=out)

    assert DeferredOperation.objects.count() == 0
    assert "migrate_post_deploy" not in out.getvalue()


@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("test_app", "verbose_migrate")
def test_migrate_says_nothing_at_verbosity_zero() -> None:
    out = StringIO()

    call_command("migrate", "deferred_migrations_testapp", verbosity=0, stdout=out)

    assert DeferredOperation.objects.filter(status=DeferredOperation.Status.PENDING).count() == 1
    assert out.getvalue() == ""


@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("test_app", "verbose_migrate")
def test_migrate_pre_deploy_reports_what_it_queued_without_repeating_it_as_a_notice() -> None:
    out = StringIO()

    call_command("migrate_pre_deploy", stdout=out)

    assert "Queued for post-deploy:" in out.getvalue()
    assert "migrate_post_deploy" not in out.getvalue()


@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("verbose_migrate")
def test_migrate_says_nothing_when_the_queue_table_does_not_exist() -> None:
    migration_modules: dict[str, str | None] = {config.label: None for config in apps.get_app_configs()}
    migration_modules["deferred_migrations"] = "deferred_migrations.migrations"

    with override_settings(MIGRATION_MODULES=migration_modules):
        out = StringIO()

        try:
            # The signal fires at the end of this same command, by which point the queue table has gone.
            call_command("migrate", "deferred_migrations", "zero", stdout=out)

            assert DeferredOperation._meta.db_table not in connection.introspection.table_names()
            assert "migrate_post_deploy" not in out.getvalue()
        finally:
            call_command("migrate", "deferred_migrations", verbosity=0)
