from deferred_migrations.safety.rewrite import rewrite_migration_source

GENERATED = """from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("sales", "0219_previous"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="invoice",
            name="legacy_ref",
        ),
        migrations.DeleteModel(
            name="OldThing",
        ),
    ]
"""


def test_rewrites_operations_import_and_dependency() -> None:
    result = rewrite_migration_source(GENERATED, remove_field_count=1, delete_model_count=1)

    assert result.problems == []
    assert "DeferredRemoveField(\n" in result.source
    assert "DeferredDeleteModel(\n" in result.source
    assert "migrations.RemoveField" not in result.source
    assert "from deferred_migrations.operations import DeferredDeleteModel\nfrom deferred_migrations.operations import DeferredRemoveField\n" in result.source
    assert '        ("deferred_migrations", "0001_initial"),\n        ("sales", "0219_previous"),' in result.source


def test_second_run_changes_nothing() -> None:
    once = rewrite_migration_source(GENERATED, remove_field_count=1, delete_model_count=1).source

    again = rewrite_migration_source(once, remove_field_count=0, delete_model_count=0)

    assert again.source == once
    assert not again.changed


def test_count_mismatch_is_refused_and_reported() -> None:
    result = rewrite_migration_source(GENERATED, remove_field_count=2, delete_model_count=1)

    assert result.source == GENERATED
    assert result.problems


def test_direct_imports_are_refused() -> None:
    source = GENERATED.replace("from django.db import migrations\n", "from django.db import migrations\nfrom django.db.migrations import RemoveField\n")

    result = rewrite_migration_source(source, remove_field_count=1, delete_model_count=1)

    assert result.source == source
    assert result.problems


def test_migrations_using_separate_database_and_state_are_refused() -> None:
    source = GENERATED.replace("    operations = [\n", "    operations = [\n        migrations.SeparateDatabaseAndState(state_operations=[], database_operations=[]),\n")

    result = rewrite_migration_source(source, remove_field_count=1, delete_model_count=1)

    assert result.source == source
    assert result.problems


def test_empty_dependencies_list() -> None:
    source = GENERATED.replace('    dependencies = [\n        ("sales", "0219_previous"),\n    ]\n', "    dependencies = []\n")

    result = rewrite_migration_source(source, remove_field_count=1, delete_model_count=1)

    assert '    dependencies = [\n        ("deferred_migrations", "0001_initial"),\n    ]\n' in result.source


def test_a_parenthesised_import_is_refused_rather_than_corrupted() -> None:
    source = 'from django.db import migrations\n\nfrom deferred_migrations.operations import (\n    DeferredDeleteModel,\n)\n\n\nclass Migration(migrations.Migration):\n    dependencies = []\n\n    operations = [migrations.RemoveField(model_name="order", name="code")]\n'

    result = rewrite_migration_source(source, remove_field_count=1, delete_model_count=0)

    assert not result.changed
    assert result.source == source
    assert result.problems == ["The deferred_migrations import is parenthesised; rewrite this migration by hand."]
