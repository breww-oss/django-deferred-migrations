from collections.abc import Callable

import pytest
from django.db import connection
from django.db import migrations
from django.db import models
from django.db.migrations.migration import Migration
from django.db.migrations.state import ProjectState
from django.db.models.functions import Lower
from pytest_django import Settings

from deferred_migrations.models import DeferredOperation
from deferred_migrations.operations import DeferredRemoveField
from deferred_migrations.operations import InstallColumnSync
from deferred_migrations.runner import MigrationKeys
from deferred_migrations.runner import run_deferred_operations
from deferred_migrations.schema import is_unique_field
from tests.migration_helpers import apply_operations
from tests.migration_helpers import column_names
from tests.migration_helpers import foreign_key_constraint_names
from tests.migration_helpers import nullability_and_default
from tests.migration_helpers import table_exists
from tests.migration_helpers import unapply_operations


@pytest.fixture
def customer_id(shop_state: ProjectState) -> int:
    with connection.cursor() as cursor:
        cursor.execute("INSERT INTO dm_shop_customer (name) VALUES ('Acme') RETURNING id")
        return cursor.fetchone()[0]


@pytest.mark.parametrize("removed", ["reference", "status", "created"])
@pytest.mark.django_db
def test_insert_omitting_the_removed_column_leaves_a_value_old_code_can_read(shop_state: ProjectState, customer_id: int, removed: str) -> None:
    apply_operations("dm_shop", shop_state, [DeferredRemoveField("order", removed)])
    columns = {"customer_id": customer_id, "reference": "R1", "status": "new", "code": "c1", "amount": 0, "created": "2026-01-01T00:00:00Z"}
    columns.pop(removed, None)

    with connection.cursor() as cursor:
        cursor.execute(f"INSERT INTO dm_shop_order ({', '.join(columns)}) VALUES ({', '.join(['%s'] * len(columns))}) RETURNING {removed}", list(columns.values()))
        stored = cursor.fetchone()[0]

    assert removed in column_names("dm_shop_order")
    assert stored is not None


@pytest.mark.django_db
def test_constant_default_is_set_in_the_database_so_old_pods_read_it(shop_state: ProjectState, customer_id: int) -> None:
    apply_operations("dm_shop", shop_state, [DeferredRemoveField("order", "status")])

    with connection.cursor() as cursor:
        cursor.execute("INSERT INTO dm_shop_order (customer_id, reference, code, amount, created) VALUES (%s, 'R1', 'c1', 0, now()) RETURNING status", [customer_id])
        assert cursor.fetchone()[0] == "new"


@pytest.mark.django_db
def test_not_null_column_with_no_python_default_gets_an_empty_string_rather_than_null(shop_state: ProjectState, customer_id: int) -> None:
    apply_operations("dm_shop", shop_state, [DeferredRemoveField("order", "reference")])

    with connection.cursor() as cursor:
        cursor.execute("INSERT INTO dm_shop_order (customer_id, status, code, amount, created) VALUES (%s, 'new', 'c1', 0, now()) RETURNING reference", [customer_id])
        assert cursor.fetchone()[0] == ""


@pytest.mark.django_db
def test_a_column_with_a_db_default_is_left_entirely_alone(shop_state: ProjectState) -> None:
    apply_operations("dm_shop", shop_state, [DeferredRemoveField("order", "token")])

    with connection.cursor() as cursor:
        cursor.execute("SELECT is_nullable, column_default FROM information_schema.columns WHERE table_schema = current_schema() AND table_name = 'dm_shop_order' AND column_name = 'token'")
        is_nullable, column_default = cursor.fetchone()

    assert is_nullable == "NO"
    assert "gen_random_uuid" in column_default


@pytest.mark.django_db
def test_unique_column_is_made_nullable_not_given_a_shared_default(shop_state: ProjectState, customer_id: int) -> None:
    apply_operations("dm_shop", shop_state, [DeferredRemoveField("order", "code")])

    with connection.cursor() as cursor:
        cursor.execute("INSERT INTO dm_shop_order (customer_id, reference, status, amount, created) VALUES (%s, 'R1', 'new', 0, now()), (%s, 'R2', 'new', 0, now()) RETURNING code", [customer_id, customer_id])
        assert [row[0] for row in cursor.fetchall()] == [None, None]


@pytest.mark.django_db
def test_foreign_key_constraint_is_dropped_so_new_code_can_delete_the_parent(shop_state: ProjectState, customer_id: int) -> None:
    with connection.cursor() as cursor:
        cursor.execute("INSERT INTO dm_shop_order (customer_id, reference, status, code, amount, created) VALUES (%s, 'R1', 'new', 'c1', 0, now())", [customer_id])

    apply_operations("dm_shop", shop_state, [DeferredRemoveField("order", "customer")])

    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM dm_shop_customer WHERE id = %s", [customer_id])

    assert foreign_key_constraint_names("dm_shop_order", "customer_id") == []


@pytest.mark.django_db
def test_foreign_key_constraint_with_a_non_django_name_is_still_dropped(shop_state: ProjectState) -> None:
    [django_name] = foreign_key_constraint_names("dm_shop_order", "customer_id")

    with connection.cursor() as cursor:
        cursor.execute(f'ALTER TABLE dm_shop_order RENAME CONSTRAINT "{django_name}" TO legacy_customer_fk')

    apply_operations("dm_shop", shop_state, [DeferredRemoveField("order", "customer")])

    assert foreign_key_constraint_names("dm_shop_order", "customer_id") == []


@pytest.mark.django_db
def test_drop_is_queued_and_the_runner_drops_the_column(shop_state: ProjectState) -> None:
    apply_operations("dm_shop", shop_state, [DeferredRemoveField("order", "note")], name="0002_remove_note")

    row = DeferredOperation.objects.get()
    assert (row.app_label, row.migration_name, row.kind, row.table_name, row.column_name) == ("dm_shop", "0002_remove_note", DeferredOperation.Kind.DROP_COLUMN, "dm_shop_order", "note")

    run_deferred_operations(migration_keys=MigrationKeys(known={("dm_shop", "0002_remove_note")}, applied={("dm_shop", "0002_remove_note")}), sleep=lambda seconds: None)

    assert "note" not in column_names("dm_shop_order")


@pytest.mark.django_db
def test_many_to_many_removal_drops_through_foreign_keys_and_queues_the_table(shop_state: ProjectState) -> None:
    apply_operations("dm_shop", shop_state, [DeferredRemoveField("order", "tags")])

    assert foreign_key_constraint_names("dm_shop_order_tags", "order_id") == []
    assert foreign_key_constraint_names("dm_shop_order_tags", "customer_id") == []
    assert DeferredOperation.objects.get().table_name == "dm_shop_order_tags"
    assert table_exists("dm_shop_order_tags")


@pytest.mark.django_db
def test_backwards_with_a_pending_row_restores_constraints_and_removes_the_row(shop_state: ProjectState) -> None:
    operations = [DeferredRemoveField("order", "customer")]
    apply_operations("dm_shop", shop_state, operations)

    unapply_operations("dm_shop", shop_state, operations)

    assert len(foreign_key_constraint_names("dm_shop_order", "customer_id")) == 1
    assert not DeferredOperation.objects.exists()


@pytest.mark.django_db
def test_backwards_after_the_drop_ran_readds_the_column_and_reapply_queues_again(shop_state: ProjectState) -> None:
    operations = [DeferredRemoveField("order", "note")]
    apply_operations("dm_shop", shop_state, operations)
    run_deferred_operations(migration_keys=MigrationKeys(known={("dm_shop", "0001_test")}, applied={("dm_shop", "0001_test")}), sleep=lambda seconds: None)

    unapply_operations("dm_shop", shop_state, operations)
    assert "note" in column_names("dm_shop_order")
    assert not DeferredOperation.objects.exists()

    apply_operations("dm_shop", shop_state, operations)
    assert DeferredOperation.objects.get().status == DeferredOperation.Status.PENDING


@pytest.mark.django_db
def test_running_forwards_twice_does_not_error_or_duplicate(shop_state: ProjectState) -> None:
    operation = DeferredRemoveField("order", "reference")
    apply_operations("dm_shop", shop_state, [operation])
    apply_operations("dm_shop", shop_state, [operation])

    assert DeferredOperation.objects.count() == 1


@pytest.mark.django_db
def test_sqlmigrate_style_collection_does_not_query_the_database(shop_state: ProjectState, django_assert_num_queries: Callable) -> None:
    migration = Migration("0002_remove_customer", "dm_shop")
    migration.operations = [DeferredRemoveField("order", "customer")]

    with connection.schema_editor(collect_sql=True, atomic=False) as editor, django_assert_num_queries(0):
        migration.apply(shop_state.clone(), editor, collect_sql=True)

    assert any("INSERT INTO" in statement for statement in editor.collected_sql)
    assert any("-- Drop foreign key constraints on dm_shop_order.customer_id" in statement for statement in editor.collected_sql)
    assert foreign_key_constraint_names("dm_shop_order", "customer_id") != []


# A functional constraint keeps its columns in expressions, not fields, so missing it would give the column a shared SET DEFAULT and make new code's second insert during a rollout collide.
@pytest.mark.django_db
def test_a_functional_unique_constraint_marks_its_column_unique(shop_state: ProjectState) -> None:
    state = apply_operations(
        "dm_shop",
        shop_state,
        [migrations.CreateModel("Coupon", [("id", models.BigAutoField(primary_key=True)), ("label", models.CharField(max_length=20, default="c"))], options={"constraints": [models.UniqueConstraint(Lower("label"), name="dm_shop_coupon_unique_label")]})],
    )
    model = state.apps.get_model("dm_shop", "coupon")

    assert is_unique_field(model, model._meta.get_field("label"))


@pytest.mark.parametrize("removed", ["reference", "status", "created", "code", "token", "note"])
@pytest.mark.django_db
def test_rollback_returns_the_column_to_exactly_how_it_started(shop_state: ProjectState, removed: str) -> None:
    before = nullability_and_default("dm_shop_order", removed)
    operations = [DeferredRemoveField("order", removed)]
    apply_operations("dm_shop", shop_state, operations)

    unapply_operations("dm_shop", shop_state, operations)

    assert nullability_and_default("dm_shop_order", removed) == before


# A version of the package that relaxed differently can leave a default this version would never set, and rollback must still clear it.
@pytest.mark.django_db
def test_rollback_clears_a_default_left_by_a_different_relaxation(shop_state: ProjectState) -> None:
    operations = [DeferredRemoveField("order", "code")]
    apply_operations("dm_shop", shop_state, operations)

    with connection.cursor() as cursor:
        cursor.execute("ALTER TABLE dm_shop_order ALTER COLUMN code SET DEFAULT ''")

    unapply_operations("dm_shop", shop_state, operations)

    assert nullability_and_default("dm_shop_order", "code") == ("NO", None)


@pytest.fixture
def renamed_label_state(shop_state: ProjectState) -> ProjectState:
    state = apply_operations("dm_shop", shop_state, [migrations.AddField("order", "label", models.CharField(max_length=20, null=True, default="x"))], name="0002_label")
    return apply_operations("dm_shop", state, [migrations.AddField("order", "tag", models.CharField(max_length=20, null=True, default="x")), InstallColumnSync("order", from_field="label", to_field="tag", forwards_sql="{from}", backwards_sql="{to}")], name="0003_rename")


# A default on the old column would be copied over new code's explicit NULL by the sync trigger.
@pytest.mark.django_db
def test_a_renamed_away_column_keeps_new_codes_explicit_null(renamed_label_state: ProjectState, customer_id: int) -> None:
    apply_operations("dm_shop", renamed_label_state, [DeferredRemoveField("order", "label", renamed_to="tag")], atomic=False, name="0004_remove")

    with connection.cursor() as cursor:
        cursor.execute("INSERT INTO dm_shop_order (customer_id, reference, status, code, amount, created, tag) VALUES (%s, 'R1', 'new', 'c1', 0, now(), NULL) RETURNING label, tag", [customer_id])
        assert cursor.fetchone() == (None, None)

    assert nullability_and_default("dm_shop_order", "label") == ("YES", None)


@pytest.mark.django_db
def test_a_not_null_renamed_away_column_only_loses_not_null(shop_state: ProjectState, customer_id: int) -> None:
    state = apply_operations("dm_shop", shop_state, [migrations.AddField("order", "state", models.CharField(max_length=20, default="new")), InstallColumnSync("order", from_field="status", to_field="state", forwards_sql="{from}", backwards_sql="{to}")], name="0002_rename")
    apply_operations("dm_shop", state, [DeferredRemoveField("order", "status", renamed_to="state")], atomic=False, name="0003_remove")

    with connection.cursor() as cursor:
        cursor.execute("INSERT INTO dm_shop_order (customer_id, reference, code, amount, created, state) VALUES (%s, 'R1', 'c1', 0, now(), 'paid') RETURNING status", [customer_id])
        assert cursor.fetchone()[0] == "paid"

    assert nullability_and_default("dm_shop_order", "status") == ("YES", None)


@pytest.mark.django_db
def test_a_misspelt_renamed_to_raises_before_anything_changes(shop_state: ProjectState) -> None:
    before = nullability_and_default("dm_shop_order", "status")

    with pytest.raises(ValueError, match="renamed_to='stat'"):
        apply_operations("dm_shop", shop_state, [DeferredRemoveField("order", "status", renamed_to="stat")])

    assert nullability_and_default("dm_shop_order", "status") == before
    assert not DeferredOperation.objects.filter(app_label="dm_shop").exists()


# Rolling back after the post-deploy drop must not re-add the old column empty.
@pytest.mark.django_db
def test_rolling_back_after_the_drop_copies_the_column_back_from_its_new_name(shop_state: ProjectState, customer_id: int, settings: Settings) -> None:
    settings.DEFERRED_MIGRATIONS_BACKFILL_PAUSE_SECONDS = 0
    rename = [migrations.AddField("order", "state", models.CharField(max_length=20, default="new")), InstallColumnSync("order", from_field="status", to_field="state", forwards_sql="{from}", backwards_sql="{to}")]
    state = apply_operations("dm_shop", shop_state, rename, name="0002_rename")
    remove = [DeferredRemoveField("order", "status", renamed_to="state")]
    apply_operations("dm_shop", state, remove, atomic=False, name="0003_remove")
    run_deferred_operations(migration_keys=MigrationKeys(known={("dm_shop", "0002_rename"), ("dm_shop", "0003_remove")}, applied={("dm_shop", "0002_rename"), ("dm_shop", "0003_remove")}), sleep=lambda seconds: None)

    with connection.cursor() as cursor:
        cursor.execute("INSERT INTO dm_shop_order (customer_id, reference, code, amount, created, state) VALUES (%s, 'R1', 'c1', 0, now(), 'shipped') RETURNING id", [customer_id])
        order_id = cursor.fetchone()[0]
        # Stands in for the commit app writes have before a rollback runs.
        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")

    unapply_operations("dm_shop", state, remove, atomic=False, name="0003_remove")

    with connection.cursor() as cursor:
        cursor.execute("SELECT status FROM dm_shop_order WHERE id = %s", [order_id])
        assert cursor.fetchone()[0] == "shipped"

    assert nullability_and_default("dm_shop_order", "status") == ("NO", None)


@pytest.mark.django_db
def test_rolling_back_after_only_the_trigger_drop_ran_copies_values_written_since(shop_state: ProjectState, customer_id: int, settings: Settings) -> None:
    settings.DEFERRED_MIGRATIONS_BACKFILL_PAUSE_SECONDS = 0
    rename = [migrations.AddField("order", "state", models.CharField(max_length=20, default="new")), InstallColumnSync("order", from_field="status", to_field="state", forwards_sql="{from}", backwards_sql="{to}")]
    state = apply_operations("dm_shop", shop_state, rename, name="0002_rename")
    remove = [DeferredRemoveField("order", "status", renamed_to="state")]
    apply_operations("dm_shop", state, remove, atomic=False, name="0003_remove")
    # Only 0002 is known, so its trigger drop runs and 0003's column drop is left queued.
    run_deferred_operations(migration_keys=MigrationKeys(known={("dm_shop", "0002_rename")}, applied={("dm_shop", "0002_rename")}), sleep=lambda seconds: None)

    with connection.cursor() as cursor:
        cursor.execute("INSERT INTO dm_shop_order (customer_id, reference, code, amount, created, state) VALUES (%s, 'R1', 'c1', 0, now(), 'shipped') RETURNING id", [customer_id])
        order_id = cursor.fetchone()[0]
        # Stands in for the commit app writes have before a rollback runs.
        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")

    unapply_operations("dm_shop", state, remove, atomic=False, name="0003_remove")

    with connection.cursor() as cursor:
        cursor.execute("SELECT status FROM dm_shop_order WHERE id = %s", [order_id])
        assert cursor.fetchone()[0] == "shipped"


# field.clone() rebuilds a ForeignKey through deconstruct(), leaving remote_field.model an unresolved string; add_field then crashes.
@pytest.mark.django_db
def test_rolling_back_a_renamed_foreign_key_after_the_drop_restores_the_column_and_its_constraint(shop_state: ProjectState, customer_id: int) -> None:
    rename = [migrations.AddField("order", "client", models.ForeignKey("dm_shop.Customer", on_delete=models.CASCADE, null=True)), InstallColumnSync("order", from_field="customer", to_field="client", forwards_sql="{from}", backwards_sql="{to}")]
    state = apply_operations("dm_shop", shop_state, rename, name="0002_rename")
    remove = [DeferredRemoveField("order", "customer", renamed_to="client")]
    apply_operations("dm_shop", state, remove, atomic=False, name="0003_remove")
    run_deferred_operations(migration_keys=MigrationKeys(known={("dm_shop", "0002_rename"), ("dm_shop", "0003_remove")}, applied={("dm_shop", "0002_rename"), ("dm_shop", "0003_remove")}), sleep=lambda seconds: None)

    with connection.cursor() as cursor:
        cursor.execute("INSERT INTO dm_shop_order (reference, status, code, amount, created, client_id) VALUES ('R1', 'new', 'c1', 0, now(), %s) RETURNING id", [customer_id])
        order_id = cursor.fetchone()[0]
        # Stands in for the commit app writes have before a rollback runs.
        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")

    unapply_operations("dm_shop", state, remove, atomic=False, name="0003_remove")

    with connection.cursor() as cursor:
        cursor.execute("SELECT customer_id FROM dm_shop_order WHERE id = %s", [order_id])
        assert cursor.fetchone()[0] == customer_id

    assert nullability_and_default("dm_shop_order", "customer_id") == ("NO", None)
    assert foreign_key_constraint_names("dm_shop_order", "customer_id") != []


def test_renamed_to_is_serialised_only_when_set() -> None:
    assert DeferredRemoveField("order", "status").deconstruct() == ("DeferredRemoveField", [], {"model_name": "order", "name": "status"})
    assert DeferredRemoveField("order", "status", renamed_to="state").deconstruct() == ("DeferredRemoveField", [], {"model_name": "order", "name": "status", "renamed_to": "state"})
