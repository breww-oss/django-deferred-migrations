import re
from io import StringIO

import pytest
from django.contrib.contenttypes.models import ContentType
from django.core.management import CommandError
from django.core.management import call_command
from django.db import ProgrammingError
from django.db import connection
from django.db import migrations
from django.db import models
from django.db.migrations.migration import Migration
from django.db.migrations.operations.base import Operation
from django.db.migrations.state import ProjectState

from deferred_migrations import contenttypes
from deferred_migrations.models import DeferredOperation
from deferred_migrations.models import ModelRename
from deferred_migrations.operations import DeferredRemoveField
from deferred_migrations.operations import DeferredRenameModel
from deferred_migrations.operations import InstallColumnSync
from deferred_migrations.runner import MigrationKeys
from deferred_migrations.runner import run_deferred_operations
from tests.migration_helpers import apply_operations
from tests.migration_helpers import column_names
from tests.migration_helpers import relation_kind
from tests.migration_helpers import unapply_operations


@pytest.fixture
def gadget_state(shop_state: ProjectState) -> ProjectState:
    return apply_operations(
        "dm_shop",
        shop_state,
        [
            migrations.CreateModel("Gadget", [("id", models.BigAutoField(primary_key=True)), ("name", models.CharField(max_length=50)), ("colour", models.CharField(max_length=20, default="red"))]),
            migrations.CreateModel("Part", [("id", models.BigAutoField(primary_key=True)), ("gadget", models.ForeignKey("dm_shop.Gadget", on_delete=models.CASCADE))]),
        ],
        name="0002_gadget",
    )


def rename() -> list[Operation]:
    return [DeferredRenameModel("Gadget", "Gizmo")]


def applied(*names: str) -> MigrationKeys:
    keys = {("dm_shop", name) for name in names}
    return MigrationKeys(known=keys, applied=keys)


@pytest.mark.django_db
def test_old_code_statements_work_through_the_view_during_the_overlap(gadget_state: ProjectState) -> None:
    apply_operations("dm_shop", gadget_state, rename(), name="0003_rename")

    with connection.cursor() as cursor:
        cursor.execute("INSERT INTO dm_shop_gadget (name, colour) VALUES ('a', 'blue') RETURNING id")
        gadget_id = cursor.fetchone()[0]
        cursor.execute("INSERT INTO dm_shop_gadget (id, name, colour) VALUES (%s, 'b', 'blue') ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name", [gadget_id])
        cursor.execute("INSERT INTO dm_shop_gadget (id, name, colour) VALUES (%s, 'c', 'blue') ON CONFLICT DO NOTHING", [gadget_id])
        cursor.execute("INSERT INTO dm_shop_part (gadget_id) VALUES (%s)", [gadget_id])
        cursor.execute("UPDATE dm_shop_gadget g SET colour = 'green' FROM dm_shop_part p WHERE p.gadget_id = g.id")
        cursor.execute("SELECT name FROM dm_shop_gadget WHERE id = %s FOR UPDATE", [gadget_id])
        assert cursor.fetchone()[0] == "b"
        cursor.execute("SELECT name, colour FROM dm_shop_gizmo WHERE id = %s", [gadget_id])
        assert cursor.fetchone() == ("b", "green")
        cursor.execute("DELETE FROM dm_shop_part")
        cursor.execute("DELETE FROM dm_shop_gadget WHERE id = %s", [gadget_id])
        cursor.execute("SELECT count(*) FROM dm_shop_gizmo")
        assert cursor.fetchone()[0] == 0


@pytest.mark.django_db
def test_forwards_leaves_a_view_a_rename_record_and_a_queued_view_drop(gadget_state: ProjectState) -> None:
    apply_operations("dm_shop", gadget_state, rename(), name="0003_rename")

    assert (relation_kind("dm_shop_gadget"), relation_kind("dm_shop_gizmo")) == ("v", "r")
    assert ModelRename.objects.filter(app_label="dm_shop", old_model="gadget", new_model="gizmo").exists()
    row = DeferredOperation.objects.get(kind=DeferredOperation.Kind.DROP_VIEW)
    assert (row.table_name, row.column_name, row.sql) == ("dm_shop_gizmo", "dm_shop_gadget", 'DROP VIEW IF EXISTS "dm_shop_gadget"')


@pytest.mark.django_db
def test_the_view_is_created_as_security_invoker(gadget_state: ProjectState) -> None:
    if connection.pg_version < 150000:
        pytest.skip("needs PostgreSQL 15")

    apply_operations("dm_shop", gadget_state, rename(), name="0003_rename")

    with connection.cursor() as cursor:
        cursor.execute("SELECT reloptions FROM pg_class WHERE oid = to_regclass('dm_shop_gadget')")
        assert "security_invoker=true" in cursor.fetchone()[0]


@pytest.mark.django_db
@pytest.mark.parametrize("role", ["dm_view_reader", 'dm "quoted" reader'])
def test_a_role_granted_on_the_table_can_use_the_view(gadget_state: ProjectState, role: str) -> None:
    quoted_role = '"' + role.replace('"', '""') + '"'

    with connection.cursor() as cursor:
        cursor.execute(f"CREATE ROLE {quoted_role} NOLOGIN")
        cursor.execute(f"GRANT SELECT, INSERT ON dm_shop_gadget TO {quoted_role}")

    apply_operations("dm_shop", gadget_state, rename(), name="0003_rename")

    with connection.cursor() as cursor:
        cursor.execute("SELECT has_table_privilege(%s, 'dm_shop_gadget', 'SELECT'), has_table_privilege(%s, 'dm_shop_gadget', 'INSERT'), has_table_privilege(%s, 'dm_shop_gadget', 'DELETE')", [role, role, role])
        assert cursor.fetchone() == (True, True, False)


@pytest.mark.django_db
def test_a_column_drop_queued_before_the_rename_is_retargeted_and_runs_after_the_view_drop(gadget_state: ProjectState) -> None:
    state = apply_operations("dm_shop", gadget_state, [DeferredRemoveField("gadget", "colour")], name="0003_remove")
    apply_operations("dm_shop", state, rename(), name="0004_rename")
    column_drop = DeferredOperation.objects.get(kind=DeferredOperation.Kind.DROP_COLUMN, column_name="colour")

    assert (column_drop.table_name, column_drop.sql) == ("dm_shop_gizmo", 'ALTER TABLE IF EXISTS "dm_shop_gizmo" DROP COLUMN IF EXISTS "colour"')

    result = run_deferred_operations(migration_keys=applied("0003_remove", "0004_rename"), sleep=lambda seconds: None)

    assert [row.kind for row in result.ran] == [DeferredOperation.Kind.DROP_VIEW, DeferredOperation.Kind.DROP_COLUMN]
    assert relation_kind("dm_shop_gadget") is None
    assert "colour" not in column_names("dm_shop_gizmo")


@pytest.mark.django_db
def test_a_skipped_row_is_retargeted_and_backwards_restores_exactly_the_rewritten_rows(gadget_state: ProjectState) -> None:
    state = apply_operations("dm_shop", gadget_state, [DeferredRemoveField("gadget", "colour")], name="0003_remove")
    DeferredOperation.objects.filter(column_name="colour").update(status=DeferredOperation.Status.SKIPPED)
    unrelated = DeferredOperation.objects.create(app_label="dm_shop", migration_name="0001_test", operation_index=9, sequence=0, kind=DeferredOperation.Kind.DROP_COLUMN, table_name="dm_shop_order", column_name="note", sql="SELECT 1")
    apply_operations("dm_shop", state, rename(), name="0004_rename")

    assert DeferredOperation.objects.get(column_name="colour").table_name == "dm_shop_gizmo"

    unapply_operations("dm_shop", state, rename(), name="0004_rename")

    restored = DeferredOperation.objects.get(column_name="colour")
    assert (restored.table_name, restored.sql) == ("dm_shop_gadget", 'ALTER TABLE IF EXISTS "dm_shop_gadget" DROP COLUMN IF EXISTS "colour"')
    assert DeferredOperation.objects.get(pk=unrelated.pk).table_name == "dm_shop_order"
    assert not DeferredOperation.objects.filter(kind=DeferredOperation.Kind.DROP_VIEW).exists()
    assert not ModelRename.objects.exists()
    assert (relation_kind("dm_shop_gadget"), relation_kind("dm_shop_gizmo")) == ("r", None)


def sync_colour_into_shade(gadget_state: ProjectState) -> ProjectState:
    return apply_operations("dm_shop", gadget_state, [migrations.AddField("gadget", "shade", models.CharField(max_length=20, null=True)), InstallColumnSync("gadget", from_field="colour", to_field="shade", forwards_sql="{from}", backwards_sql=None)], name="0003_sync")


def triggers_on(table: str) -> list[str]:
    with connection.cursor() as cursor:
        cursor.execute("SELECT tgname FROM pg_trigger WHERE tgrelid = to_regclass(%s) AND NOT tgisinternal", [table])
        return [row[0] for row in cursor.fetchall()]


# DROP TRIGGER ... ON a view silently does nothing, so an unretargeted row would leave the sync trigger on the table forever.
@pytest.mark.django_db
def test_a_queued_trigger_drop_is_retargeted_through_the_rename_and_restored_on_unapply(gadget_state: ProjectState) -> None:
    state = sync_colour_into_shade(gadget_state)
    apply_operations("dm_shop", state, rename(), name="0004_rename")

    assert 'ON "dm_shop_gizmo";' in DeferredOperation.objects.get(kind=DeferredOperation.Kind.DROP_TRIGGER).sql

    unapply_operations("dm_shop", state, rename(), name="0004_rename")

    assert 'ON "dm_shop_gadget";' in DeferredOperation.objects.get(kind=DeferredOperation.Kind.DROP_TRIGGER).sql


@pytest.mark.django_db
def test_a_retargeted_trigger_drop_removes_the_trigger_from_the_renamed_table(gadget_state: ProjectState) -> None:
    state = sync_colour_into_shade(gadget_state)
    apply_operations("dm_shop", state, rename(), name="0004_rename")

    assert len(triggers_on("dm_shop_gizmo")) == 1

    run_deferred_operations(migration_keys=applied("0002_gadget", "0003_sync", "0004_rename"), sleep=lambda seconds: None)

    assert triggers_on("dm_shop_gizmo") == []


@pytest.mark.django_db
def test_backwards_after_the_view_drop_ran_renames_the_table_back(gadget_state: ProjectState) -> None:
    apply_operations("dm_shop", gadget_state, rename(), name="0003_rename")
    run_deferred_operations(migration_keys=applied("0003_rename"), sleep=lambda seconds: None)

    unapply_operations("dm_shop", gadget_state, rename(), name="0003_rename")

    assert (relation_kind("dm_shop_gadget"), relation_kind("dm_shop_gizmo")) == ("r", None)


@pytest.mark.django_db
def test_a_model_with_a_pinned_table_is_refused_before_anything_changes(shop_state: ProjectState) -> None:
    state = apply_operations("dm_shop", shop_state, [migrations.CreateModel("Fixed", [("id", models.BigAutoField(primary_key=True))], options={"db_table": "dm_fixed"})], name="0002_fixed")

    with pytest.raises(ValueError, match="db_table"):
        apply_operations("dm_shop", state, [DeferredRenameModel("Fixed", "Moved")], name="0003_rename")

    assert relation_kind("dm_fixed") == "r"


@pytest.mark.django_db
def test_a_model_in_an_auto_created_many_to_many_is_refused(shop_state: ProjectState) -> None:
    with pytest.raises(ValueError, match="many-to-many"):
        apply_operations("dm_shop", shop_state, [DeferredRenameModel("Customer", "Client")], name="0002_rename")

    assert relation_kind("dm_shop_customer") == "r"


# Django keeps the old name silently when a row under the new name already exists.
@pytest.mark.django_db
def test_a_stale_content_type_under_the_new_name_is_refused(gadget_state: ProjectState) -> None:
    ContentType.objects.create(app_label="dm_shop", model="gadget")
    ContentType.objects.create(app_label="dm_shop", model="gizmo")

    with pytest.raises(ValueError, match="ContentType rows exist for both"):
        apply_operations("dm_shop", gadget_state, rename(), name="0003_rename")

    assert relation_kind("dm_shop_gadget") == "r"


@pytest.mark.django_db
def test_without_contenttypes_the_stale_row_check_is_skipped(gadget_state: ProjectState, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(contenttypes, "contenttypes_installed", lambda: False)
    ContentType.objects.create(app_label="dm_shop", model="gadget")
    ContentType.objects.create(app_label="dm_shop", model="gizmo")

    apply_operations("dm_shop", gadget_state, rename(), name="0003_rename")

    assert relation_kind("dm_shop_gadget") == "v"


@pytest.mark.django_db
def test_sqlmigrate_style_collection_changes_nothing(gadget_state: ProjectState) -> None:
    migration = Migration("0003_rename", "dm_shop")
    migration.operations = rename()

    with connection.schema_editor(collect_sql=True, atomic=False) as editor:
        migration.apply(gadget_state.clone(), editor, collect_sql=True)

    assert any(statement.startswith('CREATE VIEW "dm_shop_gadget"') for statement in editor.collected_sql)
    assert relation_kind("dm_shop_gadget") == "r"


@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("rename_test_app")
def test_pre_deploy_serves_the_old_name_and_post_deploy_drops_the_view() -> None:
    call_command("migrate_pre_deploy", stdout=StringIO())

    with connection.cursor() as cursor:
        cursor.execute("INSERT INTO deferred_migrations_testapp_gadget (name) VALUES ('old') RETURNING id")
        gadget_id = cursor.fetchone()[0]

    call_command("migrate_post_deploy", stdout=StringIO())

    assert relation_kind("deferred_migrations_testapp_gadget") is None

    with connection.cursor() as cursor:
        cursor.execute("INSERT INTO deferred_migrations_testapp_part (gadget_id) VALUES (%s)", [gadget_id])


@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("rename_reuse_test_app")
def test_reusing_the_old_name_in_the_same_deploy_names_the_queued_view_drop() -> None:
    call_command("migrate", "deferred_migrations_testapp", "0002", verbosity=0)
    queued = DeferredOperation.objects.get(kind=DeferredOperation.Kind.DROP_VIEW)

    with pytest.raises(CommandError, match=re.escape(f"A queued drop still holds this name: {queued}.")):
        call_command("migrate_pre_deploy", stdout=StringIO(), stderr=StringIO())


# Only a queued table or view drop frees the name; a column drop on a table of that name must not be offered as the cause.
@pytest.mark.parametrize(("kind", "error"), [(DeferredOperation.Kind.DROP_TABLE, CommandError), (DeferredOperation.Kind.DROP_COLUMN, ProgrammingError)])
@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("rename_reuse_test_app")
def test_a_duplicate_relation_is_blamed_only_on_a_queued_drop_that_frees_its_name(kind: str, error: type[Exception]) -> None:
    call_command("migrate", "deferred_migrations_testapp", "0002", verbosity=0)
    DeferredOperation.objects.filter(kind=DeferredOperation.Kind.DROP_VIEW).update(status=DeferredOperation.Status.DONE)
    DeferredOperation.objects.create(app_label="deferred_migrations_testapp", migration_name="0002_rename_gadget_gizmo", operation_index=99, sequence=0, kind=kind, table_name="deferred_migrations_testapp_gadget", column_name="legacy", sql="SELECT 1")

    with pytest.raises(error, match="already exists"):
        call_command("migrate_pre_deploy", stdout=StringIO(), stderr=StringIO())
