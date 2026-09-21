import pytest
from django.core.management import CommandError
from django.core.management import call_command
from django.db import connection
from django.db import migrations
from django.db import models

from deferred_migrations.autofix import AutoFixer
from deferred_migrations.operations import AddConstraintConcurrently
from deferred_migrations.operations import AddFieldConcurrently
from deferred_migrations.operations import AddIndexConcurrently
from deferred_migrations.safety.walker import check_installed_project
from tests.makemigrations_harness import TEST_APP
from tests.makemigrations_harness import GeneratedMigrations
from tests.makemigrations_harness import forget_test_models
from tests.makemigrations_harness import run_makemigrations
from tests.makemigrations_harness import widget_fields
from tests.migration_helpers import define_test_model
from tests.safety_graph import make_migration


@pytest.mark.django_db
def test_a_migration_of_only_concurrent_builds_becomes_non_atomic_in_place(generated_app: GeneratedMigrations) -> None:
    define_test_model("Widget", widget_fields(sku=models.CharField(max_length=10, null=True, unique=True)))

    output = run_makemigrations(interactive=False)

    assert generated_app.names() == ["0001_initial", "0002_widget_sku"]
    written = generated_app.load("0002_widget_sku")
    assert [type(operation) for operation in written.operations] == [AddFieldConcurrently]
    assert written.atomic is False
    assert ("deferred_migrations", "0001_initial") in written.dependencies
    assert "builds without blocking writes" in output
    assert "0 findings need a manual fix" in output


@pytest.mark.django_db
def test_an_index_and_a_check_constraint_are_swapped_too(generated_app: GeneratedMigrations) -> None:
    define_test_model("Widget", widget_fields(), {"indexes": [models.Index(fields=["name"], name="widget_name_idx")], "constraints": [models.CheckConstraint(condition=models.Q(count__gte=0), name="widget_count_positive")]})

    run_makemigrations(interactive=False)

    written = generated_app.load(generated_app.names()[1])
    assert sorted(type(operation).__name__ for operation in written.operations) == ["AddConstraintConcurrently", "AddIndexConcurrently"]
    assert written.atomic is False


@pytest.mark.django_db
def test_a_mixed_migration_keeps_its_plain_work_atomic_and_moves_the_builds_to_a_follow_up(generated_app: GeneratedMigrations) -> None:
    define_test_model("Widget", widget_fields(flag=None, vessel=models.IntegerField(null=True)), {"constraints": [models.UniqueConstraint(fields=["name", "vessel"], name="widget_name_vessel", nulls_distinct=False)]})

    output = run_makemigrations(interactive=False)

    first, follow_up = generated_app.names()[1:]
    original = generated_app.load(first)
    moved = generated_app.load(follow_up)
    assert original.atomic is True
    assert [type(operation).__name__ for operation in original.operations] == ["DeferredRemoveField", "AddField"]
    assert [type(operation) for operation in moved.operations] == [AddConstraintConcurrently]
    assert moved.atomic is False
    assert moved.dependencies == [(TEST_APP, first)]
    assert follow_up.endswith("_widget_widget_name_vessel_concurrent")
    assert generated_app.max_migration() == follow_up
    assert "moved to" in output


@pytest.mark.django_db
def test_a_0159_shaped_change_keeps_the_constraint_removal_atomic_and_builds_both_objects_in_a_follow_up(generated_app: GeneratedMigrations) -> None:
    define_test_model("Widget", widget_fields(), {"constraints": [models.UniqueConstraint(fields=["name"], name="widget_name_uniq")]})
    run_makemigrations(interactive=False)
    forget_test_models()
    define_test_model("Widget", widget_fields(parent=models.ForeignKey(f"{TEST_APP}.Widget", models.SET_NULL, null=True)), {"constraints": [models.UniqueConstraint(fields=["name", "parent"], name="widget_name_parent", nulls_distinct=False)]})

    run_makemigrations(interactive=False)

    names = generated_app.names()
    assert len(names) == 4
    original = generated_app.load(names[2])
    follow_up = generated_app.load(names[3])
    assert [type(operation) for operation in original.operations] == [migrations.RemoveConstraint]
    assert original.atomic is True
    assert [type(operation) for operation in follow_up.operations] == [AddFieldConcurrently, AddConstraintConcurrently]
    assert follow_up.atomic is False
    assert names[3].endswith("_concurrent_builds")
    assert ("deferred_migrations", "0001_initial") in follow_up.dependencies


@pytest.mark.django_db
def test_update_refuses_a_split(generated_app: GeneratedMigrations) -> None:
    define_test_model("Widget", widget_fields(extra=models.TextField(null=True)))
    run_makemigrations(interactive=False)
    forget_test_models()
    define_test_model("Widget", widget_fields(extra=models.TextField(null=True), vessel=models.IntegerField(null=True)), {"constraints": [models.UniqueConstraint(fields=["name", "vessel"], name="widget_name_vessel")]})

    with pytest.raises(CommandError, match="--update"):
        run_makemigrations("--update", interactive=False)


@pytest.mark.django_db
def test_update_converts_in_place(generated_app: GeneratedMigrations) -> None:
    define_test_model("Widget", widget_fields(sku=models.CharField(max_length=10, null=True)))
    run_makemigrations(interactive=False)
    forget_test_models()
    define_test_model("Widget", widget_fields(sku=models.CharField(max_length=10, null=True, db_index=True)))

    run_makemigrations("--update", interactive=False)

    names = generated_app.names()
    assert len(names) == 2
    written = generated_app.load(names[1])
    assert [type(operation) for operation in written.operations] == [AddFieldConcurrently]
    assert written.atomic is False


@pytest.mark.django_db
def test_a_dry_run_writes_nothing(generated_app: GeneratedMigrations) -> None:
    define_test_model("Widget", widget_fields(sku=models.CharField(max_length=10, null=True, unique=True)))

    output = run_makemigrations("--dry-run", interactive=False)

    assert generated_app.names() == ["0001_initial"]
    assert "Would make deploy-safe" in output


@pytest.mark.django_db
@pytest.mark.usefixtures("live_test_app")
def test_another_apps_migration_from_the_same_run_is_repointed_to_the_follow_up() -> None:
    split = make_migration(TEST_APP, "0004_child_parent2", [migrations.AddField("child", "memo", models.TextField(null=True)), migrations.AddField("child", "parent2", models.ForeignKey(f"{TEST_APP}.Parent", models.SET_NULL, null=True, related_name="+"))], [(TEST_APP, "0003_child_extra")])
    dependent = make_migration("deferred_migrations", "0003_after", [], [("deferred_migrations", "0002_modelrename"), (TEST_APP, "0004_child_parent2")])
    changes = {TEST_APP: [split], "deferred_migrations": [dependent]}

    fixer = AutoFixer(changes, connection)
    fixer.run()

    follow_up = changes[TEST_APP][-1]
    assert follow_up.name == "0005_child_parent2_concurrent"
    assert [type(operation) for operation in follow_up.operations] == [AddFieldConcurrently]
    assert (TEST_APP, "0005_child_parent2_concurrent") in dependent.dependencies
    assert (TEST_APP, "0004_child_parent2") not in dependent.dependencies
    fixer.graph.ensure_not_cyclic()
    assert [node for node in fixer.graph.leaf_nodes() if node[0] == TEST_APP] == [(TEST_APP, "0005_child_parent2_concurrent")]


@pytest.mark.django_db
@pytest.mark.usefixtures("live_test_app")
def test_a_migration_that_is_not_its_apps_last_in_the_run_is_left_as_written() -> None:
    first = make_migration(TEST_APP, "0004_first", [migrations.AddField("child", "memo", models.TextField(null=True)), migrations.AddField("child", "code", models.CharField(max_length=5, null=True, db_index=True))], [(TEST_APP, "0003_child_extra")])
    other = make_migration("deferred_migrations", "0003_between", [], [("deferred_migrations", "0002_modelrename"), (TEST_APP, "0004_first")])
    second = make_migration(TEST_APP, "0005_second", [], [(TEST_APP, "0004_first"), ("deferred_migrations", "0003_between")])
    changes = {TEST_APP: [first, second], "deferred_migrations": [other]}

    report = AutoFixer(changes, connection).run()

    assert type(first.operations[1]) is migrations.AddField
    assert [finding.rule_id for finding in report.findings] == ["E102"]
    assert any("a later migration in this app" in note for note in report.notes)


@pytest.mark.django_db
@pytest.mark.usefixtures("live_test_app")
def test_a_build_a_later_non_rerunnable_operation_depends_on_is_left_as_written() -> None:
    migration = make_migration(TEST_APP, "0004_code", [migrations.RemoveField("child", "note"), migrations.AddField("child", "code", models.CharField(max_length=5, null=True, db_index=True)), migrations.AlterUniqueTogether("child", {("code", "parent")})], [(TEST_APP, "0003_child_extra")])
    changes = {TEST_APP: [migration]}

    report = AutoFixer(changes, connection).run()

    assert type(migration.operations[1]) is migrations.AddField
    assert "E102" in [finding.rule_id for finding in report.findings]
    assert any("a later operation in the same migration depends on it" in note for note in report.notes)


@pytest.mark.django_db
@pytest.mark.usefixtures("live_test_app")
def test_a_rerunnable_operation_after_a_moved_build_moves_with_it() -> None:
    migration = make_migration(
        TEST_APP, "0004_code", [migrations.AddField("child", "memo", models.TextField(null=True)), migrations.AddField("child", "code", models.CharField(max_length=5, null=True, db_index=True)), migrations.AlterModelOptions("child", {"verbose_name": "Kid"})], [(TEST_APP, "0003_child_extra")]
    )
    changes = {TEST_APP: [migration]}

    AutoFixer(changes, connection).run()

    assert [type(operation) for operation in changes[TEST_APP][-1].operations] == [AddFieldConcurrently, migrations.AlterModelOptions]
    assert [type(operation) for operation in migration.operations] == [migrations.AddField]
    assert migration.operations[0].name == "memo"


@pytest.mark.django_db
@pytest.mark.usefixtures("live_test_app")
def test_a_state_only_neighbour_does_not_force_a_split() -> None:
    migration = make_migration(TEST_APP, "0004_code", [migrations.AlterModelOptions("child", {"verbose_name": "Kid"}), migrations.AddField("child", "code", models.CharField(max_length=5, null=True, db_index=True))], [(TEST_APP, "0003_child_extra")])
    changes = {TEST_APP: [migration]}

    AutoFixer(changes, connection).run()

    assert changes[TEST_APP] == [migration]
    assert migration.atomic is False
    assert type(migration.operations[1]) is AddFieldConcurrently


@pytest.mark.django_db
@pytest.mark.usefixtures("live_test_app")
def test_hand_written_code_keeps_its_migration_atomic_and_the_build_moves_out() -> None:
    migration = make_migration(TEST_APP, "0004_code", [migrations.RunPython(migrations.RunPython.noop, migrations.RunPython.noop), migrations.AddField("child", "code", models.CharField(max_length=5, null=True, db_index=True))], [(TEST_APP, "0003_child_extra")])
    changes = {TEST_APP: [migration]}

    AutoFixer(changes, connection).run()

    follow_up = changes[TEST_APP][-1]
    assert migration.atomic is True
    assert [type(operation) for operation in migration.operations] == [migrations.RunPython]
    assert [type(operation) for operation in follow_up.operations] == [AddFieldConcurrently]


@pytest.mark.django_db
@pytest.mark.usefixtures("live_test_app")
def test_a_build_followed_by_hand_written_code_is_left_as_written() -> None:
    migration = make_migration(TEST_APP, "0004_code", [migrations.AddField("child", "code", models.CharField(max_length=5, null=True, db_index=True)), migrations.RunPython(migrations.RunPython.noop, migrations.RunPython.noop)], [(TEST_APP, "0003_child_extra")])
    changes = {TEST_APP: [migration]}

    report = AutoFixer(changes, connection).run()

    assert changes[TEST_APP] == [migration]
    assert type(migration.operations[0]) is migrations.AddField
    assert "E102" in [finding.rule_id for finding in report.findings]
    assert any("hand-written RunSQL or RunPython follows it" in note for note in report.notes)


@pytest.mark.django_db
@pytest.mark.usefixtures("live_test_app")
def test_an_index_on_a_field_added_in_the_same_migration_is_built_after_it() -> None:
    migration = make_migration(TEST_APP, "0004_code", [migrations.RemoveField("child", "note"), migrations.AddField("child", "code", models.CharField(max_length=5, null=True)), migrations.AddIndex("child", models.Index(fields=["code"], name="child_code_idx"))], [(TEST_APP, "0003_child_extra")])
    changes = {TEST_APP: [migration]}

    AutoFixer(changes, connection).run()

    assert [type(operation) for operation in changes[TEST_APP][-1].operations] == [AddIndexConcurrently]
    assert [type(operation).__name__ for operation in migration.operations] == ["DeferredRemoveField", "AddField"]


@pytest.mark.django_db
@pytest.mark.usefixtures("live_test_app")
def test_an_alter_constraint_on_an_unrelated_field_does_not_force_a_split() -> None:
    migration = make_migration(
        TEST_APP,
        "0004_code",
        [
            migrations.AddField("child", "memo", models.TextField(null=True)),
            migrations.AddField("child", "code", models.CharField(max_length=5, null=True, db_index=True)),
            migrations.AlterConstraint("child", "some_other_constraint", models.UniqueConstraint(fields=["parent"], name="some_other_constraint", violation_error_message="Only one child per parent.")),
        ],
        [(TEST_APP, "0003_child_extra")],
    )
    changes = {TEST_APP: [migration]}

    AutoFixer(changes, connection).run()

    follow_up = changes[TEST_APP][-1]
    assert follow_up is not migration
    assert [type(operation) for operation in follow_up.operations] == [AddFieldConcurrently]
    assert [type(operation).__name__ for operation in migration.operations] == ["AddField", "AlterConstraint"]


# RunSQL and RunPython claim to reference every model, so only an explicit exclusion keeps a data step in its atomic migration.
@pytest.mark.django_db
@pytest.mark.usefixtures("live_test_app")
def test_a_data_step_before_a_moved_build_stays_in_the_atomic_migration() -> None:
    migration = make_migration(
        TEST_APP,
        "0004_code",
        [
            migrations.RunSQL("SELECT 1", migrations.RunSQL.noop),
            migrations.AddField("child", "memo", models.TextField(null=True)),
            migrations.AddField("child", "code", models.CharField(max_length=5, null=True, db_index=True)),
        ],
        [(TEST_APP, "0003_child_extra")],
    )
    changes = {TEST_APP: [migration]}

    AutoFixer(changes, connection).run()

    follow_up = changes[TEST_APP][-1]
    assert [type(operation) for operation in follow_up.operations] == [AddFieldConcurrently]
    assert [type(operation).__name__ for operation in migration.operations] == ["RunSQL", "AddField"]
    assert migration.atomic is True


@pytest.mark.django_db
@pytest.mark.usefixtures("live_test_app")
def test_a_rename_of_a_moved_index_leaves_the_build_as_written() -> None:
    migration = make_migration(
        TEST_APP,
        "0004_note_idx",
        [
            migrations.AddField("child", "memo", models.TextField(null=True)),
            migrations.AddIndex("child", models.Index(fields=["note"], name="child_note_idx")),
            migrations.RenameIndex("child", new_name="child_note_renamed", old_name="child_note_idx"),
        ],
        [(TEST_APP, "0003_child_extra")],
    )

    report = AutoFixer({TEST_APP: [migration]}, connection).run()

    assert type(migration.operations[1]) is migrations.AddIndex
    assert "E101" in [finding.rule_id for finding in report.findings]
    assert any("a later operation in the same migration depends on it" in note for note in report.notes)


@pytest.mark.django_db(transaction=True)
def test_the_generated_migrations_apply_and_pass_the_check(generated_app: GeneratedMigrations) -> None:
    if connection.pg_version < 150000:
        pytest.skip("needs PostgreSQL 15")

    define_test_model("Widget", widget_fields(flag=None, vessel=models.IntegerField(null=True)), {"constraints": [models.UniqueConstraint(fields=["name", "vessel"], name="widget_name_vessel", nulls_distinct=False)]})
    run_makemigrations(interactive=False)

    call_command("migrate_pre_deploy", verbosity=0)

    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'widget_name_vessel'")
        assert cursor.fetchone() == ("UNIQUE NULLS NOT DISTINCT (name, vessel)",)

    assert [finding for finding in check_installed_project() if finding.app_label == TEST_APP] == []
